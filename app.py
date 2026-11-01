"""Shopee Studio: local authenticated dashboard, served by Waitress."""
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from core import Engine, Store, TZ, load_env, safe_error, telegram_preview
from llm import public_llm

ROOT = Path(__file__).parent
load_env(ROOT / ".env")


def create_app(data_dir=None, testing=False):
    app = Flask(__name__)
    directory = Path(data_dir or os.environ.get("DATA_DIR", ROOT / "data"))
    directory.mkdir(parents=True, exist_ok=True)
    secret_file = directory / ".session-secret"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_hex(32), encoding="utf8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
    app.config.update(SECRET_KEY=secret_file.read_text().strip(), TESTING=testing,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false") == "true",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=65536)
    store = Store(directory)
    engine = Engine(store)
    app.extensions.update(store=store, engine=engine)
    password = os.environ.get("PANEL_PASSWORD", "")
    password_hash = generate_password_hash(password) if password else ""
    login_attempts = {}
    auth_lock = threading.Lock()
    model_tests, tests_lock = {}, threading.Lock()

    @app.before_request
    def guard():
        if request.path == "/healthz" or request.path.startswith("/static/"):
            return None
        if request.path == "/":
            return None
        if request.path.startswith("/api/worker/"):
            # The VM worker uses its own bearer token; no browser session or CSRF.
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not store.check_worker_token(supplied):
                return jsonify(error="Token do worker inválido."), 401
            if request.path.endswith("/video"):
                # Only the authenticated video upload may exceed the global 64 KB body limit.
                request.max_content_length = 52 * 1024 * 1024
                if request.mimetype != "application/octet-stream":
                    return jsonify(error="Envie o vídeo como application/octet-stream."), 415
                return None
            if request.method != "GET" and not request.is_json:
                return jsonify(error="Requisição JSON obrigatória."), 415
            return None
        if not password_hash and not testing:
            return jsonify(error="Defina PANEL_PASSWORD no .env e reinicie o serviço."), 503
        if request.method != "GET":
            # JSON-only mutation plus same-origin check prevents cross-origin form posts.
            if not request.is_json:
                return jsonify(error="Requisição JSON obrigatória."), 415
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return jsonify(error="Origem não permitida."), 403
        if request.path != "/api/login" and not session.get("authenticated"):
            return jsonify(error="Entre no painel para continuar."), 401
        if request.path != "/api/login" and request.method != "GET":
            token = request.headers.get("X-CSRF-Token", "")
            if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                return jsonify(error="Sessão expirada. Entre novamente."), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://*.susercontent.com https://cf.shopee.com.br; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=safe_error(error)), 400

    @app.errorhandler(Exception)
    def unexpected(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        store.log("error", "Falha interna: " + safe_error(error))
        return jsonify(error="Falha interna. Consulte o histórico do serviço."), 500

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/healthz")
    def health():
        with store.db() as db:
            db.execute("SELECT 1")
        # A collection may legitimately span several pages.
        alive = time.time() - engine.heartbeat < 180 or engine.lock.locked()
        return jsonify(alive=alive), 200 if alive else 503

    @app.post("/api/login")
    def login():
        address = request.remote_addr
        with auth_lock:
            now = time.time()
            attempts = [t for t in login_attempts.get(address, []) if now - t < 300]
            if len(attempts) >= 10:
                return jsonify(error="Muitas tentativas. Aguarde cinco minutos."), 429
            supplied = (request.get_json() or {}).get("password", "")
            if not password_hash or not isinstance(supplied, str) or not check_password_hash(password_hash, supplied):
                login_attempts[address] = attempts + [now]
                return jsonify(error="Senha incorreta."), 401
            login_attempts.pop(address, None)
        session.clear()
        session.update(authenticated=True, csrf=secrets.token_hex(24))
        session.permanent = True
        return jsonify(csrf=session["csrf"])

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/state")
    def state():
        runs = store.runs()
        settings = store.settings()
        now = datetime.now(TZ)
        hour, minute = map(int, settings["schedule_time"].split(":"))
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        with store.db() as db:
            slot = db.execute("SELECT * FROM schedule WHERE day=?", (now.strftime("%Y-%m-%d"),)).fetchone()
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")]
            counts = dict(db.execute("SELECT state,COUNT(*) FROM decisions GROUP BY state").fetchall())
        if next_run <= now:
            if slot and slot["status"] == "retry":
                next_run = datetime.fromtimestamp(slot["next_retry"], TZ)
            elif slot and slot["status"] in ("done", "failed", "unknown"):
                next_run += timedelta(days=1)
            else:
                next_run = now
        credentials = store.telegram_public()
        return jsonify(csrf=session["csrf"], runs=runs, settings=settings,
                       latest=store.run(runs[0]["id"]) if runs else None,
                       next_run=next_run.isoformat() if settings["schedule_enabled"] else None,
                       busy=engine.lock.locked(), events=events, counts=counts,
                       telegram=credentials, schedule=dict(slot) if slot else None,
                       shopee_configured=bool(store.shopee()["app_id"] and store.shopee()["secret"]),
                       shopee_app_id=store.shopee()["app_id"],
                       llm=public_llm(store.llm()), worker=store.worker_status(), video=engine.videos.summary(),
                       version=os.environ.get("APP_VERSION", "local"))

    @app.get("/api/runs/<int:run_id>")
    def get_run(run_id):
        return jsonify(store.run(run_id))

    @app.get("/api/products/<path:key>/history")
    def history(key):
        return jsonify(store.history(key))

    @app.post("/api/settings")
    def settings():
        return jsonify(store.save_settings(request.get_json()))

    @app.post("/api/shopee")
    def shopee_settings():
        store.save_shopee(request.get_json())
        return jsonify(configured=True)

    @app.post("/api/telegram")
    def telegram_settings():
        store.save_telegram(request.get_json())
        return jsonify(store.telegram_public())

    @app.post("/api/decisions")
    def decisions():
        body = request.get_json()
        store.decision(body.get("key"), body.get("state"))
        return jsonify(ok=True)

    @app.post("/api/run-now")
    def collect():
        # The lock is acquired synchronously by the worker; collect itself arbitrates races.
        if engine.lock.locked():
            return jsonify(error="Já existe uma operação em andamento."), 409
        def work():
            try:
                engine.collect()
            except Exception:
                pass  # Engine records the error.
        threading.Thread(target=work, daemon=True).start()
        return jsonify(accepted=True), 202

    @app.get("/api/runs/<int:run_id>/preview")
    def preview(run_id):
        run = store.run(run_id)
        if run["delivery"] == "not_sent":
            products, deliveries = engine.sendable_products(run), None
        else:  # After sending: the messages frozen for each product, with their state.
            deliveries = store.deliveries(run_id)
            products = [p for p in run["products"] if p["key"] in deliveries]
        return jsonify(**telegram_preview(products, deliveries), destination=store.telegram_public(),
                       can_send=run["status"] == "ready" and run["delivery"] == "not_sent" and bool(products))

    @app.post("/api/runs/<int:run_id>/send")
    def send(run_id):
        return jsonify(queued=engine.send(run_id))

    @app.post("/api/llm")
    def llm_settings():
        return jsonify(store.save_llm(request.get_json()))

    @app.post("/api/llm/test")
    def llm_test():
        provider_id = (request.get_json() or {}).get("provider_id")
        if not isinstance(provider_id, str):
            raise ValueError("Informe o modelo a testar.")
        test_id = secrets.token_hex(8)
        with tests_lock:
            for old in [k for k, v in model_tests.items() if time.time() - v["created"] > 3600]:
                model_tests.pop(old)
            model_tests[test_id] = dict(status="running", message="Testando visão…", created=time.time())
        def work():
            try:
                message, status = engine.runner.test(provider_id), "ok"
            except Exception as error:
                message, status = "Falhou: " + safe_error(error), "failed"
            with tests_lock:
                model_tests[test_id].update(status=status, message=message)
            store.log("info" if status == "ok" else "error", "Teste de modelo — " + message)
        threading.Thread(target=work, daemon=True).start()
        return jsonify(test_id=test_id), 202

    @app.get("/api/llm/tests/<test_id>")
    def llm_test_status(test_id):
        with tests_lock:
            test = model_tests.get(test_id)
        if not test:
            raise ValueError("Teste não encontrado.")
        return jsonify(status=test["status"], message=test["message"])

    @app.post("/api/worker-token")
    def worker_token():
        return jsonify(token=store.set_worker_token())

    def worker_identity(body):
        worker_id = body.get("worker_id") if isinstance(body, dict) else None
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", worker_id):
            raise ValueError("worker_id inválido.")
        return worker_id

    @app.post("/api/worker/claim")
    def worker_claim():
        body = request.get_json()
        worker_id = worker_identity(body)
        kinds = body.get("kinds")
        if not isinstance(kinds, list) or not kinds or not all(k in ("llm", "video") for k in kinds):
            raise ValueError("kinds inválido.")
        version = str(body.get("version", ""))[:40]
        store.worker_seen(worker_id, version, kinds)
        task = store.claim_task(worker_id, kinds)
        return (jsonify(task=task), 200) if task else (jsonify(task=None), 200)

    @app.post("/api/worker/heartbeat")
    def worker_heartbeat():
        store.worker_touch(worker_identity(request.get_json()))
        return jsonify(ok=True)

    @app.post("/api/worker/tasks/<int:task_id>/result")
    def worker_result(task_id):
        body = request.get_json()
        worker_id = worker_identity(body)
        store.worker_touch(worker_id)
        if type(body.get("ok")) is not bool:
            raise ValueError("ok deve ser verdadeiro ou falso.")
        output, code = body.get("output"), str(body.get("code", ""))[:40]
        store.finish_task(task_id, worker_id, body["ok"], dict(output=output, code=code), str(body.get("error", ""))[:500])
        if store.task(task_id)["kind"] == "video":
            engine.videos.finish(task_id, body["ok"], output, code, body.get("error", ""), body.get("charged") is True)
        return jsonify(ok=True)

    @app.post("/api/worker/tasks/<int:task_id>/progress")
    def worker_progress(task_id):
        body = request.get_json()
        worker_id = worker_identity(body)
        store.worker_touch(worker_id)
        engine.videos.progress(task_id, worker_id, body.get("stage"), body.get("credits"))
        return jsonify(ok=True)

    @app.post("/api/worker/tasks/<int:task_id>/video")
    def worker_video(task_id):
        worker_id = worker_identity({"worker_id": request.headers.get("X-Worker-Id")})
        store.worker_touch(worker_id)
        engine.videos.upload(task_id, worker_id, request.get_data(cache=False))
        return jsonify(ok=True)

    @app.post("/api/videos")
    def video_create():
        body = request.get_json() or {}
        if not isinstance(body.get("run_id"), int) or not isinstance(body.get("key"), str):
            raise ValueError("Informe a consulta e o produto.")
        return jsonify(video_id=engine.videos.enqueue_manual(body["run_id"], body["key"]))

    @app.post("/api/videos/<int:video_id>/resend")
    def video_resend(video_id):
        engine.videos.deliver(video_id, manual=True)
        return jsonify(engine.videos.get(video_id))

    @app.post("/api/videos/resume")
    def video_resume():
        engine.videos.resume()
        return jsonify(engine.videos.summary())

    if not testing:
        engine.start()
    return app


if __name__ == "__main__":
    from waitress import serve
    application = create_app()
    serve(application, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8787")),
          threads=6, channel_timeout=60)
