"""Persistent offer collection, ranking, delivery and scheduling."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import analysis
import video as video_module
from content import CaptionTooLong, plain, video_caption
from llm import LLM_DEFAULTS, ModelError, Runner, WORKER_ONLINE_SECONDS, public_llm, validate_llm

TZ = ZoneInfo("America/Sao_Paulo")
DEFAULTS = dict(keyword="", min_rating=4.5, relax_rating=False,
                fallback_rating=4.0, min_price=0.0, max_price=500.0, min_sales=0,
                min_commission=0.0, repeat_days=7, per_shop=2,
                top_count=5, commission_weight=50, rating_weight=30, sales_weight=20,
                schedule_time="12:00", schedule_enabled=True, auto_send=False,
                max_pages=100, page_limit=20,
                video_enabled=True, video_daily_credits=75, video_cost=15, video_model="Omni 1.1 Flash",
                flow_project_url="")
DECISIONS = {"new", "planned", "published", "discarded"}
API = "https://open-api.affiliate.shopee.com.br/graphql"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def load_env(path):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def safe_error(error):
    message = str(error)
    for key in ("SHOPEE_SECRET", "TELEGRAM_BOT_TOKEN", "PANEL_PASSWORD", "PANEL_SECRET"):
        value = os.environ.get(key)
        if value:
            message = message.replace(value, "[oculto]")
    return message[:1000]


def validate_settings(payload, existing=None):
    # Ignore category_id left by older versions.
    result = dict(DEFAULTS)
    result.update({key: value for key, value in (existing or {}).items() if key in DEFAULTS})
    if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
        raise ValueError("Configuração desconhecida.")
    for key, value in payload.items():
        original = DEFAULTS[key]
        if isinstance(original, bool):
            if type(value) is not bool:
                raise ValueError(f"{key}: informe verdadeiro ou falso.")
        elif isinstance(original, int):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or int(value) != value:
                raise ValueError(f"{key}: informe um inteiro.")
            value = int(value)
        elif isinstance(original, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key}: número inválido.")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"{key}: texto inválido.")
        result[key] = value
    if not 1 <= len(result["video_model"].strip()) <= 60:
        raise ValueError("Modelo de vídeo: informe o nome mostrado no Flow (até 60 caracteres).")
    result["video_model"] = result["video_model"].strip()
    result["flow_project_url"] = result["flow_project_url"].strip()
    if result["flow_project_url"] and not re.fullmatch(r"https://(flow\.google\.com|labs\.google)/\S{3,280}", result["flow_project_url"]):
        raise ValueError("URL do projeto do Flow inválida (https://flow.google.com/project/...).")
    if len(result["keyword"]) > 120:
        raise ValueError("Palavra-chave: máximo de 120 caracteres.")
    try:
        datetime.strptime(result["schedule_time"], "%H:%M")
    except ValueError:
        raise ValueError("Horário inválido. Use HH:MM.") from None
    if not 0 <= result["fallback_rating"] <= result["min_rating"] <= 5:
        raise ValueError("Notas devem estar entre 0 e 5; a alternativa não pode superar a mínima.")
    for key in ("min_price", "max_price", "min_sales", "min_commission"):
        if result[key] < 0:
            raise ValueError(f"{key}: não pode ser negativo.")
    if result["max_price"] and result["max_price"] < result["min_price"]:
        raise ValueError("Preço máximo menor que o mínimo.")
    for key, low, high in (("repeat_days", 0, 365), ("top_count", 1, 10),
                           ("per_shop", 1, 10),
                           ("max_pages", 1, 500), ("page_limit", 1, 50),
                           ("video_daily_credits", 0, 2000), ("video_cost", 1, 500)):
        if not low <= result[key] <= high:
            raise ValueError(f"{key}: use {low} a {high}.")
    weights = [result[k] for k in ("commission_weight", "rating_weight", "sales_weight")]
    if any(w < 0 or w > 100 for w in weights) or sum(weights) != 100:
        raise ValueError("Os pesos devem somar 100%.")
    return result


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / "shopee.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS shopee (id INTEGER PRIMARY KEY CHECK(id=1), app_id TEXT, secret TEXT);
                CREATE TABLE IF NOT EXISTS telegram (id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, chat_id TEXT, thread_id TEXT);
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT,
                    source TEXT, settings TEXT, warning TEXT, error TEXT, scanned INTEGER DEFAULT 0,
                    message_id INTEGER, delivery TEXT DEFAULT 'not_sent', sent_at TEXT);
                CREATE TABLE IF NOT EXISTS offers (
                    run_id INTEGER, product_key TEXT, body TEXT, selected INTEGER,
                    PRIMARY KEY(run_id, product_key));
                CREATE INDEX IF NOT EXISTS offers_product ON offers(product_key,run_id);
                CREATE TABLE IF NOT EXISTS decisions (
                    product_key TEXT PRIMARY KEY, state TEXT, updated TEXT);
                CREATE TABLE IF NOT EXISTS schedule (
                    day TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0, next_retry REAL DEFAULT 0,
                    status TEXT, run_id INTEGER);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, created TEXT, level TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS llm (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_auth (id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT, created TEXT);
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY, last_seen REAL, version TEXT, kinds TEXT);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, created TEXT,
                    deadline REAL, worker_id TEXT, claimed_at TEXT, finished TEXT, result TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,kind,id);
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY, run_id INTEGER, product_key TEXT, day TEXT, state TEXT, task_id INTEGER,
                    credits INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, photo_message_id INTEGER, message_id INTEGER,
                    file TEXT, meta TEXT, error TEXT, created TEXT, updated TEXT);
                CREATE INDEX IF NOT EXISTS videos_run ON videos(run_id, product_key);
                CREATE INDEX IF NOT EXISTS videos_task ON videos(task_id);
                CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS deliveries (
                    run_id INTEGER, product_key TEXT, state TEXT, photo_message_id INTEGER,
                    text_message_id INTEGER, sent_at TEXT, error TEXT, PRIMARY KEY(run_id, product_key));
            """)
            # Additive migrations only: old rows keep their data and stay readable.
            for table, column, kind in (("runs", "progress", "TEXT"), ("videos", "snapshot", "TEXT"),
                                        ("deliveries", "snapshot", "TEXT"), ("deliveries", "message_id", "INTEGER")):
                if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            db.execute("INSERT OR IGNORE INTO llm VALUES(1,?)", (json.dumps(LLM_DEFAULTS),))
            first = dict(DEFAULTS)
            for env, key in (("SHOPEE_KEYWORD", "keyword"),
                             ("SHOPEE_MIN_RATING", "min_rating"), ("SHOPEE_PAGE_LIMIT", "page_limit"),
                             ("SHOPEE_MAX_PAGES", "max_pages")):
                if os.environ.get(env):
                    value = os.environ[env]
                    first[key] = type(DEFAULTS[key])(value)
            first["auto_send"] = os.environ.get("AUTO_SEND", "false").lower() == "true"
            first = validate_settings(first)
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(first),))
            stored_settings = json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
            migrated_settings = validate_settings({}, stored_settings)
            if migrated_settings != stored_settings:
                db.execute("UPDATE settings SET body=? WHERE id=1", (json.dumps(migrated_settings),))
            # A process crash during delivery is ambiguous; never automatically resend it.
            db.execute("UPDATE runs SET delivery='unknown' WHERE delivery='sending'")
            db.execute("UPDATE runs SET status='failed', error='Execução interrompida; consulte os logs.', finished=? WHERE status IN ('running','analyzing')", (utcnow(),))
            db.execute("UPDATE schedule SET status='retry', next_retry=0 WHERE status='running'")
            db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
            # Nobody waits for model tasks created before a restart; video jobs live on their own.
            db.execute("UPDATE tasks SET status='expired' WHERE kind='llm' AND status IN ('queued','claimed')")
            db.execute("UPDATE videos SET state='delivery_unknown',error='Painel reiniciou durante o envio; confira o Telegram.' WHERE state='sending'")
            queued = [r[0] for r in db.execute("SELECT id FROM runs WHERE delivery='queued'")]
        for run_id in queued:
            self.refresh_delivery(run_id)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.db() as db:
            return validate_settings({}, json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0]))

    def save_settings(self, payload):
        value = validate_settings(payload, self.settings())
        with self.db() as db:
            db.execute("UPDATE settings SET body=? WHERE id=1", (json.dumps(value),))
        self.log("info", "Configurações atualizadas.")
        return value

    def log(self, level, message):
        with self.db() as db:
            db.execute("INSERT INTO events(created,level,message) VALUES(?,?,?)", (utcnow(), level, safe_error(message)))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")

    def runs(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 60")]

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Consulta não encontrada.")
            result = dict(row)
            result["settings"] = json.loads(result["settings"])
            result["products"] = []
            for entry in db.execute("""
                SELECT o.body,o.selected,COALESCE(d.state,'new') decision
                FROM offers o LEFT JOIN decisions d ON o.product_key=d.product_key
                WHERE o.run_id=? ORDER BY o.selected DESC""", (run_id,)):
                product = json.loads(entry["body"])
                product.update(selected=bool(entry["selected"]), decision=entry["decision"])
                result["products"].append(product)
            result["products"].sort(key=lambda p: (not p["selected"], -p["score"]))
        videos = video_module.Videos.latest_static(self, run_id)
        delivered = self.deliveries(run_id)
        for product in result["products"]:
            entry = delivered.get(product["key"]) or {}
            product["video"] = videos.get(product["key"])
            product["delivery"] = entry.get("state")
            product["delivery_error"] = entry.get("error") or None
            # "video": one message with the subtitled video; "legacy": photo + prompt, video as a reply.
            product["delivery_mode"] = ("video" if entry.get("snapshot") else "legacy") if entry else None
        return result

    def decision(self, key, state):
        if state not in DECISIONS:
            raise ValueError("Estado inválido.")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM offers WHERE product_key=?", (key,)).fetchone():
                raise ValueError("Produto não encontrado.")
            db.execute("INSERT INTO decisions VALUES(?,?,?) ON CONFLICT(product_key) DO UPDATE SET state=excluded.state,updated=excluded.updated",
                       (key, state, utcnow()))

    def shopee(self):
        with self.db() as db:
            row = db.execute("SELECT app_id,secret FROM shopee WHERE id=1").fetchone()
        return dict(row) if row else dict(app_id=os.environ.get("SHOPEE_APP_ID", ""), secret=os.environ.get("SHOPEE_SECRET", ""))

    def save_shopee(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"app_id", "secret"}:
            raise ValueError("Configuração Shopee inválida.")
        if any(not isinstance(v, str) for v in payload.values()):
            raise ValueError("Use texto nas credenciais.")
        app_id = payload.get("app_id", "").strip()
        secret = payload.get("secret", "").strip() or self.shopee()["secret"]
        if not app_id.isdigit() or not secret or len(secret) > 512:
            raise ValueError("Preencha um App ID numérico e o segredo da Shopee.")
        with self.db() as db:
            db.execute("INSERT INTO shopee VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET app_id=excluded.app_id,secret=excluded.secret", (app_id, secret))
        self.log("info", "Credenciais da Shopee atualizadas no servidor.")

    def telegram(self):
        with self.db() as db:
            row = db.execute("SELECT token,chat_id,thread_id FROM telegram WHERE id=1").fetchone()
        if row:
            return dict(row)
        return dict(token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                    chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
                    thread_id=os.environ.get("TELEGRAM_THREAD_ID", ""))

    def telegram_public(self):
        config = self.telegram()
        return dict(configured=bool(config["token"] and config["chat_id"]),
                    token_saved=bool(config["token"]), chat_id=config["chat_id"],
                    thread_id=config["thread_id"])

    def save_telegram(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"token", "chat_id", "thread_id"}:
            raise ValueError("Configuração Telegram inválida.")
        if any(not isinstance(v, str) for v in payload.values()):
            raise ValueError("Use texto nos campos do Telegram.")
        token = payload.get("token", "").strip() or self.telegram()["token"]
        chat = payload.get("chat_id", "").strip()
        topic = payload.get("thread_id", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Token do bot inválido.")
        if not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,}", chat):
            raise ValueError("ID do chat inválido.")
        if topic and (not topic.isdigit() or not 1 <= int(topic) <= 2147483647):
            raise ValueError("ID do tópico precisa ser um inteiro positivo.")
        with self.db() as db:
            db.execute("INSERT INTO telegram VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,chat_id=excluded.chat_id,thread_id=excluded.thread_id",
                       (token, chat, topic))
        self.log("info", "Destino Telegram atualizado. Token preservado em armazenamento local.")

    def exclusions(self):
        """Exclude every offer already sent, or possibly sent, to Telegram, plus its validated equivalents.
        Products waiting for their video are reserved too, so no other run creates a second job for them."""
        with self.db() as db:
            excluded = {r[0] for r in db.execute("SELECT product_key FROM decisions WHERE state IN ('discarded','published')")}
            for row in db.execute("""
                SELECT o.product_key,o.body FROM offers o JOIN runs r ON r.id=o.run_id
                WHERE o.selected=1 AND r.delivery IN ('sent','unknown','sending')
                  AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.run_id=r.id AND d.snapshot IS NOT NULL)"""):
                excluded.add(row[0])
                links = (json.loads(row[1]).get("analysis") or {}).get("links") or []
                excluded.update(entry["key"] for entry in links if entry.get("key"))
            for row in db.execute("""SELECT product_key,snapshot FROM deliveries
                                     WHERE snapshot IS NOT NULL AND state IN ('pending','sending','sent','unknown','refused')"""):
                excluded.add(row[0])
                excluded.update(entry["key"] for entry in json.loads(row[1]).get("links") or [] if entry.get("key"))
            return excluded

    def refresh_delivery(self, run_id):
        """Run status from its product deliveries: queued while any video is pending, sent when all are
        confirmed, partial when nothing is pending but something failed, was blocked or is uncertain."""
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE run_id=? AND snapshot IS NOT NULL", (run_id,))]
            if not rows:
                return None
            states = [r["state"] for r in rows]
            sent = sorted((r for r in rows if r["state"] == "sent"), key=lambda r: r["sent_at"] or "")
            if any(state in ("pending", "sending") for state in states):
                status = "queued"
            else:
                status = "sent" if all(state == "sent" for state in states) else "partial"
            parts = [f"{states.count(state)} {label}" for state, label in (
                ("pending", "aguardando vídeo"), ("failed", "com falha"), ("blocked", "bloqueado(s)"),
                ("refused", "recusado(s) pelo Telegram"), ("unknown", "sem confirmação do Telegram")) if states.count(state)]
            error = f"{len(sent)} de {len(rows)} entregue(s); " + ", ".join(parts) + "." if parts else None
            db.execute("UPDATE runs SET delivery=?,sent_at=?,message_id=?,error=? WHERE id=?",
                       (status, sent[-1]["sent_at"] if sent else None, sent[0]["message_id"] if sent else None,
                        error, run_id))
        return status

    def history(self, key):
        with self.db() as db:
            return [dict(started=r["started"], **json.loads(r["body"])) for r in db.execute("""
                SELECT r.started,o.body FROM offers o JOIN runs r ON r.id=o.run_id
                WHERE o.product_key=? ORDER BY r.id DESC LIMIT 60""", (key,))]

    def llm(self):
        with self.db() as db:
            return validate_llm({}, json.loads(db.execute("SELECT body FROM llm WHERE id=1").fetchone()[0]))

    def save_llm(self, payload):
        value = validate_llm(payload, self.llm())
        with self.db() as db:
            db.execute("UPDATE llm SET body=? WHERE id=1", (json.dumps(value),))
        self.log("info", "Modelos de visão/texto atualizados. Chaves preservadas no servidor.")
        return public_llm(value)

    def set_worker_token(self):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO worker_auth VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,created=excluded.created",
                       (hashlib.sha256(token.encode()).hexdigest(), utcnow()))
        self.log("info", "Novo token do worker gerado; o anterior deixou de valer.")
        return token

    def check_worker_token(self, token):
        with self.db() as db:
            row = db.execute("SELECT token_hash FROM worker_auth WHERE id=1").fetchone()
        return bool(row and row[0] and token) and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row[0])

    def worker_seen(self, worker_id, version, kinds):
        with self.db() as db:
            db.execute("INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,version=excluded.version,kinds=excluded.kinds",
                       (worker_id, time.time(), version, json.dumps(kinds)))

    def worker_touch(self, worker_id):
        with self.db() as db:
            db.execute("UPDATE workers SET last_seen=? WHERE id=?", (time.time(), worker_id))

    def worker_online(self, kind="llm"):
        with self.db() as db:
            rows = db.execute("SELECT kinds FROM workers WHERE last_seen>?", (time.time() - WORKER_ONLINE_SECONDS,)).fetchall()
        return any(kind in json.loads(r[0] or "[]") for r in rows)

    def worker_status(self):
        with self.db() as db:
            token = db.execute("SELECT created FROM worker_auth WHERE id=1").fetchone()
            rows = [dict(r) for r in db.execute("SELECT * FROM workers ORDER BY last_seen DESC LIMIT 5")]
            queued = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','claimed')").fetchone()[0]
        for row in rows:
            row["online"] = time.time() - row["last_seen"] < WORKER_ONLINE_SECONDS
            row["last_seen"] = datetime.fromtimestamp(row["last_seen"], timezone.utc).isoformat()
            row["kinds"] = json.loads(row["kinds"] or "[]")
        return dict(token_created=token[0] if token else None, workers=rows, pending_tasks=queued)

    def create_task(self, kind, payload, deadline):
        with self.db() as db:
            return db.execute("INSERT INTO tasks(kind,payload,status,created,deadline) VALUES(?,?,'queued',?,?)",
                              (kind, json.dumps(payload), utcnow(), time.time() + deadline)).lastrowid

    def task(self, task_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Tarefa não encontrada.")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def expire_task(self, task_id):
        with self.db() as db:
            db.execute("UPDATE tasks SET status='expired' WHERE id=? AND status IN ('queued','claimed')", (task_id,))

    def claim_task(self, worker_id, kinds):
        with self.db() as db:
            db.execute("UPDATE tasks SET status='expired' WHERE status IN ('queued','claimed') AND deadline<?", (time.time(),))
            marks = ",".join("?" for _ in kinds)
            row = db.execute(f"SELECT id FROM tasks WHERE status='queued' AND kind IN ({marks}) ORDER BY id LIMIT 1",
                             list(kinds)).fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET status='claimed',worker_id=?,claimed_at=? WHERE id=? AND status='queued'",
                       (worker_id, utcnow(), row[0]))
        task = self.task(row[0])
        return dict(id=task["id"], kind=task["kind"], payload=task["payload"],
                    seconds_left=max(0, int(task["deadline"] - time.time())))

    def finish_task(self, task_id, worker_id, ok, result=None, error=""):
        with self.db() as db:
            changed = db.execute("UPDATE tasks SET status=?,finished=?,result=?,error=? WHERE id=? AND worker_id=? AND status='claimed'",
                                 ("done" if ok else "failed", utcnow(), json.dumps(result or {}), safe_error(error)[:500],
                                  task_id, worker_id)).rowcount
        if changed != 1:
            raise ValueError("Tarefa expirada ou de outro worker; resultado ignorado.")

    def set_progress(self, run_id, status, progress):
        with self.db() as db:
            db.execute("UPDATE runs SET status=?,progress=? WHERE id=?", (status, progress, run_id))

    def save_analysis(self, run_id, product):
        with self.db() as db:
            db.execute("UPDATE offers SET body=? WHERE run_id=? AND product_key=?",
                       (json.dumps(product), run_id, product["key"]))

    def deliveries(self, run_id):
        with self.db() as db:
            return {r["product_key"]: dict(r) for r in db.execute("SELECT * FROM deliveries WHERE run_id=?", (run_id,))}


def shopee_query(settings, page, sort_type=5):
    args = ["listType: 0", f"sortType: {sort_type}", f"page: {page}", f"limit: {settings['page_limit']}"]
    if settings["keyword"]:
        args.append("keyword: " + json.dumps(settings["keyword"], ensure_ascii=False))
    return """query { productOfferV2(%s) {
      nodes { itemId shopId productName shopName offerLink productLink imageUrl price priceMin priceMax
        commission commissionRate appExistRate appNewRate ratingStar sales
        periodStartTime periodEndTime }
      pageInfo { page hasNextPage }
    } }""" % ", ".join(args)


def request_json(url, body, headers=None, timeout=25):
    request = urllib.request.Request(url, data=body, method="POST",
                                    headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_product_page(settings, page, credentials, sort_type=5, deadline=None, stats=None):
    """One logical page request. `deadline` bounds the request, its retries and their waits;
    `stats["http_attempts"]` counts every HTTP attempt, so retries can be reported apart from pages."""
    app_id, secret = credentials["app_id"], credentials["secret"]
    body = json.dumps({"query": shopee_query(settings, page, sort_type)},
                      ensure_ascii=False, separators=(",", ":")).encode()
    for attempt in range(3):
        timeout = 25
        if deadline is not None:
            timeout = min(25, deadline - time.time())
            if timeout < 1:
                raise ValueError("Prazo da busca esgotado antes da resposta da Shopee.")
        if stats is not None:
            stats["http_attempts"] = stats.get("http_attempts", 0) + 1
        timestamp = str(int(time.time()))
        signature = hashlib.sha256(app_id.encode() + timestamp.encode() + body + secret.encode()).hexdigest()
        try:
            result = request_json(API, body, {"Authorization": f"SHA256 Credential={app_id}, Signature={signature}, Timestamp={timestamp}"},
                                  timeout=timeout)
            break
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise ValueError(f"Shopee retornou HTTP {error.code}.") from None
            wait = 2 ** (attempt + 1)
        except (OSError, TimeoutError):
            if attempt == 2:
                raise ValueError("Shopee indisponível após três tentativas.") from None
            wait = 2 ** (attempt + 1)
        if deadline is not None and time.time() + wait >= deadline:
            raise ValueError("Prazo da busca esgotado durante as novas tentativas na Shopee.")
        time.sleep(wait)
    if result.get("errors"):
        raise ValueError("Shopee: " + "; ".join(e.get("message", "Erro GraphQL") for e in result["errors"]))
    data = (result.get("data") or {}).get("productOfferV2")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise ValueError("Resposta da Shopee sem lista de ofertas válida.")
    page_info = data.get("pageInfo") or {}
    if page_info.get("hasNextPage") and not data["nodes"]:
        raise ValueError("Paginação inconsistente: página vazia com continuação.")
    return data


def fetch_offer_pages(settings, credentials=None, sort_type=5,
                      max_pages=None, require_complete=True):
    credentials = credentials or dict(app_id=os.environ.get("SHOPEE_APP_ID"), secret=os.environ.get("SHOPEE_SECRET"))
    app_id, secret = credentials["app_id"], credentials["secret"]
    if not app_id or not secret:
        raise ValueError("Configure SHOPEE_APP_ID e SHOPEE_SECRET no .env.")
    page_count = settings["max_pages"] if max_pages is None else max_pages
    all_nodes, complete = [], False
    for page in range(1, page_count + 1):
        data = fetch_product_page(settings, page, credentials, sort_type)
        all_nodes.extend(data["nodes"])
        if not (data.get("pageInfo") or {}).get("hasNextPage"):
            complete = True
            break
    if require_complete and not complete:
        raise ValueError("Limite de páginas atingido. Restrinja o nicho ou aumente o limite; ranking incompleto não foi enviado.")
    return all_nodes


def fetch_offers(settings, credentials=None):
    """Rank a bounded product sample; the full Shopee catalog cannot be paginated in one run."""
    return fetch_offer_pages(settings, credentials, sort_type=5, require_complete=False)


def rank_offers(nodes, settings, excluded=None, now=None):
    excluded, now = excluded or set(), now or time.time()
    unique = {}
    for node in nodes:
        key = f"{node.get('shopId', '')}:{node.get('itemId', '')}"
        if not node.get("itemId") or key in unique or key in excluded:
            continue
        start = number(node.get("periodStartTime"), 0)
        end = number(node.get("periodEndTime"), float("inf"))
        if (start > 0 and start > now) or (end > 0 and end < now):
            continue
        price = number(node.get("price")) or number(node.get("priceMin"))
        rate = number(node.get("appExistRate"))
        new_rate = number(node.get("appNewRate"))
        # Conservative basis: existing customers, with API commission as fallback.
        payout = price * rate if rate > 0 else number(node.get("commission"))
        rating, sales = number(node.get("ratingStar")), max(0, number(node.get("sales")))
        link = str(node.get("offerLink") or "")
        if not link.startswith("https://"):
            continue
        image = str(node.get("imageUrl") or "")
        image = image if image.startswith("https://") else ""
        if price <= 0 or payout <= 0 or price < settings["min_price"] or (settings["max_price"] and price > settings["max_price"]):
            continue
        if sales < settings["min_sales"] or payout < settings["min_commission"]:
            continue
        unique[key] = dict(key=key, item_id=str(node["itemId"]), shop_id=str(node.get("shopId", "")),
                           name=str(node.get("productName") or "Produto"), shop=str(node.get("shopName") or ""),
                           price=price, commission=payout, app_existing_rate=rate, app_new_rate=new_rate,
                           commission_basis="cliente existente" if rate > 0 else "comissão informada pela API",
                           rating=rating, sales=int(sales), image=image,
                           link=link, expires=end if math.isfinite(end) and end > 0 else None)
    eligible = list(unique.values())
    candidates = [p for p in eligible if p["rating"] >= settings["min_rating"]]
    warning = ""
    if len(candidates) < settings["top_count"] and settings["relax_rating"]:
        candidates = [p for p in eligible if p["rating"] >= settings["fallback_rating"]]
        warning = f"Nota mínima flexibilizada para {settings['fallback_rating']:.1f}, conforme sua configuração."
    if not candidates:
        raise ValueError("Nenhuma oferta inédita passou pelos filtros.")
    max_payout = max(p["commission"] for p in candidates)
    max_sales = max(p["sales"] for p in candidates)
    for p in candidates:
        components = dict(commission=math.log1p(p["commission"]) / math.log1p(max_payout),
                          rating=max(0, min(1, p["rating"] / 5)),
                          sales=math.log1p(p["sales"]) / math.log1p(max_sales) if max_sales else 0)
        p["score"] = round(sum(components[k] * settings[k + "_weight"] for k in components), 2)
        p["reasons"] = [f"Comissão estimada R$ {p['commission']:.2f} ({p['commission_basis']})",
                        f"Avaliação {p['rating']:.1f}/5", f"{p['sales']} vendas informadas"]
    candidates.sort(key=lambda p: (-p["score"], p["key"]))
    selected, shops = [], {}
    for p in candidates:
        shop = p["shop_id"]
        if shops.get(shop, 0) >= settings["per_shop"]:
            continue
        selected.append(p["key"])
        shops[shop] = shops.get(shop, 0) + 1
        if len(selected) == settings["top_count"]:
            break
    if len(selected) < settings["top_count"]:
        warning += f" Selecionadas {len(selected)} de {settings['top_count']}; filtros e diversidade foram preservados."
    return candidates, set(selected), warning.strip()


class TelegramRejected(Exception):
    """Telegram answered with a definite refusal: the message was not delivered."""


def search_page(credentials, keyword, page, deadline=None, stats=None):
    """One page of up to 50 listings ordered by relevance to the equivalent-search keyword."""
    if not credentials.get("app_id") or not credentials.get("secret"):
        raise ValueError("Credenciais da Shopee ausentes.")
    return fetch_product_page(dict(DEFAULTS, keyword=keyword, page_limit=50), page, credentials, sort_type=1,
                              deadline=deadline, stats=stats)


def round_entry(number, batch, reviewed, seconds, provider="", error=None):
    counts = {verdict: sum(c["verdict"] == verdict for c in reviewed) for verdict in ("match", "uncertain", "incompatible")}
    return dict(round=number, candidates=len(batch), **counts, seconds=round(seconds, 1), provider=provider, error=error)


DELIVERY_LABELS = {"pending": "aguardando vídeo", "sending": "enviando", "sent": "entregue", "unknown": "não confirmado",
                   "failed": "não entregue", "blocked": "bloqueado", "refused": "recusado pelo Telegram"}


def telegram_preview(products, deliveries=None):
    """The final message of each product, exactly as it goes out once its video is ready. After sending, the
    frozen snapshot and the delivery state are shown instead of the current analysis."""
    messages = []
    for product in products:
        entry = (deliveries or {}).get(product["key"]) or {}
        snapshot = json.loads(entry["snapshot"]) if entry.get("snapshot") else None
        try:
            if snapshot:
                caption = plain(snapshot["caption"]) if snapshot.get("caption") else ""
                problem = entry.get("error") if not snapshot.get("caption") else None
            else:
                caption, problem = plain(video_caption(product)), None
        except CaptionTooLong as error:
            caption, problem = "", str(error)
        messages.append(dict(key=product["key"], name=product["name"], caption=caption, problem=problem,
                             state=DELIVERY_LABELS.get(entry.get("state")) if snapshot else None,
                             status=(product.get("analysis") or {}).get("status", "fallback")))
    header = (f"{len(products)} produto(s) · {datetime.now(TZ):%d/%m/%Y} · comissões estimadas, não garantidas no canal Vídeo.\n"
              "Cada produto chega numa única mensagem: o vídeo legendado com a descrição e os links. Nada é enviado "
              "antes de o vídeo ficar pronto; sem vídeo, o produto fica pendente ou com falha no painel.")
    body = "\n\n".join(f"── VÍDEO {i} (legenda da mensagem" + (f" · {m['state']}" if m["state"] else "") + ") ──\n"
                         + (m["caption"] or "BLOQUEADO: " + str(m["problem"])) for i, m in enumerate(messages, 1))
    return dict(text=header + "\n\n" + body, messages=messages)


def multipart(fields, name, filename, kind, data):
    boundary = "----studio" + secrets.token_hex(12)
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                 f'Content-Type: {kind}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Engine:
    def __init__(self, store, fetcher=None, sender=request_json, searcher=None, runner=None):
        self.store, self.sender = store, sender
        self.fetcher = fetcher or (lambda settings: fetch_offers(settings, store.shopee()))
        # searcher(keyword, page, deadline, stats) -> {"nodes": [...], "pageInfo": {...}}
        self.searcher = searcher or (lambda keyword, page, deadline=None, stats=None:
                                     search_page(store.shopee(), keyword, page, deadline, stats))
        self.runner = runner or Runner(store)
        self.clock = time.time
        self.lock = threading.Lock()
        self.heartbeat = time.time()
        self.stop = threading.Event()
        self.videos = video_module.Videos(self, TZ)

    def collect(self, source="manual"):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Uma operação já está em andamento.")
        try:
            settings = self.store.settings()
            with self.store.db() as db:
                run_id = db.execute("INSERT INTO runs(started,status,source,settings) VALUES(?,'running',?,?)",
                                    (utcnow(), source, json.dumps(settings))).lastrowid
            self.store.log("info", f"Consulta #{run_id} iniciada ({source}).")
            try:
                excluded = self.store.exclusions()
                nodes = self.fetcher(settings)
                candidates, selected, warning = rank_offers(nodes, settings, excluded)
                with self.store.db() as db:
                    for product in candidates:
                        db.execute("INSERT INTO offers VALUES(?,?,?,?)", (run_id, product["key"], json.dumps(product), int(product["key"] in selected)))
                    db.execute("UPDATE runs SET settings=?,scanned=?,warning=? WHERE id=?",
                               (json.dumps(settings), len(nodes), warning, run_id))
                chosen = [p for p in candidates if p["key"] in selected]
                # Equivalents never repeat a sent offer, another selected product or another product's link.
                used = set(excluded) | selected
                for index, product in enumerate(chosen, 1):
                    self.store.set_progress(run_id, "analyzing", f"Produto {index}/{len(chosen)}: equivalentes e conteúdo")
                    self.heartbeat = time.time()
                    product["analysis"] = self.analyze(product, settings, used, progress=lambda number, count, index=index: (
                        self.store.set_progress(run_id, "analyzing", f"Produto {index}/{len(chosen)}: rodada {number} "
                                                                    f"de equivalentes ({count} candidato(s))")))
                    used.update(link["key"] for link in product["analysis"]["links"])
                    self.store.save_analysis(run_id, product)
                with self.store.db() as db:
                    db.execute("UPDATE runs SET status='ready',finished=?,progress=NULL WHERE id=?", (utcnow(), run_id))
                fallbacks = sum(p["analysis"]["status"] != "ok" for p in chosen)
                self.store.log("info" if not fallbacks else "error",
                               f"Consulta #{run_id}: {len(nodes)} ofertas consultadas, {len(selected)} selecionadas"
                               + (f", {fallbacks} sem validação do modelo." if fallbacks else "."))
            except Exception as error:
                with self.store.db() as db:
                    db.execute("UPDATE runs SET status='failed',finished=?,error=?,progress=NULL WHERE id=?", (utcnow(), safe_error(error), run_id))
                self.store.log("error", f"Consulta #{run_id}: {safe_error(error)}")
                raise
            return run_id
        finally:
            self.lock.release()

    def analyze(self, product, settings, excluded, progress=None):
        """Content once, then equivalence rounds over new candidates until five matches, the candidate
        budget, the round limit, the global deadline or the end of the results."""
        config = self.store.llm()
        started = self.clock()
        deadline = started + config["search_deadline"]
        size = config["candidate_budget"]
        total = config["candidate_total"] if size else 0
        notes, rounds = [], []
        pool = analysis.CandidatePool(product, excluded, self.searcher, dict(
            queries=config["search_queries"], pages=config["search_pages"], calls=config["search_calls"]),
            deadline=deadline, clock=self.clock)
        log = lambda message: self.store.log("info", "Modelo: " + message)

        def report(stopped, equivalents):
            search = pool.report()
            search.update(collection_stopped=search["stopped"], stopped=stopped, rounds=rounds,
                          approved=sum(c["verdict"] == "match" for c in equivalents), evaluated=len(equivalents),
                          seconds=round(self.clock() - started, 1),
                          limits=dict(batch=size, total=total, rounds=config["equivalence_rounds"],
                                      deadline=config["search_deadline"], queries=config["search_queries"],
                                      pages=config["search_pages"], calls=config["search_calls"]))
            return search

        batch = pool.next_batch(min(size, total))
        if progress:
            progress(1, len(batch))
        round_started = self.clock()
        try:
            output, provider, attempts = self.runner.run(
                analysis.build_prompt(product, batch), analysis.SCHEMA, analysis.images_for(product, batch),
                analysis.make_check(batch), log=log, deadline=deadline)
        except ModelError as error:
            self.store.log("error", f"{product['name'][:60]}: {safe_error(error)}")
            result = analysis.fallback(product, batch, safe_error(error)[:400], [], notes)
            rounds.append(round_entry(1, batch, result["equivalents"], self.clock() - round_started,
                                      error=safe_error(error)[:200]))
            result["search"] = report("falha do modelo" if size else "busca desativada", result["equivalents"])
            return result
        result = analysis.finalize(product, batch, output, provider, attempts, notes)
        equivalents = result["equivalents"]
        rounds.append(round_entry(1, batch, equivalents, self.clock() - round_started, provider))
        stopped = None if size else "busca desativada"
        while stopped is None:
            if sum(c["verdict"] == "match" for c in equivalents) >= analysis.MAX_LINKS - 1:
                stopped = "5 equivalentes aprovados"
            elif len(equivalents) >= total:
                stopped = "orçamento de candidatos"
            elif len(rounds) >= config["equivalence_rounds"]:
                stopped = "limite de rodadas"
            elif self.clock() >= deadline:
                stopped = "prazo"
            if stopped:
                break
            batch = pool.next_batch(min(size, total - len(equivalents)))
            if not batch:
                stopped = "prazo" if pool.stopped == "prazo" else ("limite de chamadas" if pool.stopped == "limite de chamadas"
                                                                   else "resultados esgotados")
                break
            number = len(rounds) + 1
            if progress:
                progress(number, len(batch))
            self.heartbeat = time.time()
            round_started = self.clock()
            try:
                output, provider, extra = self.runner.run(
                    analysis.build_equivalence_prompt(product, batch), analysis.EQUIVALENCE_SCHEMA,
                    analysis.images_for(product, batch), analysis.make_equivalence_check(batch), log=log, deadline=deadline)
            except ModelError as error:  # Earlier approvals stay; this batch is simply not validated.
                reviewed = analysis.unreviewed(batch, f"Não avaliado: a rodada {number} falhou.", number)
                equivalents = equivalents + reviewed
                rounds.append(round_entry(number, batch, reviewed, self.clock() - round_started,
                                          error=safe_error(error)[:200]))
                late = str(error).startswith("Prazo da análise esgotado")
                result["notes"].append(f"Rodada {number} de equivalência " + ("interrompida pelo prazo." if late
                                       else f"falhou: {safe_error(error)[:200]}"))
                stopped = "prazo" if late else f"falha na rodada {number}"
                break
            reviewed = analysis.review(product, batch, output, number)
            equivalents = equivalents + reviewed
            result["attempts"] = result["attempts"] + extra
            rounds.append(round_entry(number, batch, reviewed, self.clock() - round_started, provider))
        result["equivalents"] = equivalents
        result["links"] = analysis.links_for(product, equivalents)
        result["search"] = report(stopped, equivalents)
        self.store.log("info", f"{product['name'][:60]}: {len(result['links'])} link(s) após {len(rounds)} rodada(s) "
                               f"({stopped}), legenda com {result['caption_length']} caracteres ({result['provider']}).")
        return result

    def sendable_products(self, run):
        excluded = self.store.exclusions()
        return [p for p in run["products"] if p["selected"] and p["key"] not in excluded
                and p["decision"] not in ("discarded", "published")]

    def telegram_call(self, token, method, payload, upload=None):
        """`upload` = (field, filename, content type, bytes) sends multipart instead of JSON."""
        url = f"https://api.telegram.org/bot{token}/{method}"
        for attempt in range(2):
            try:
                if upload:
                    fields = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in payload.items()}
                    body, kind = multipart(fields, *upload)
                    result = self.sender(url, body, {"Content-Type": kind})
                else:
                    result = self.sender(url, json.dumps(payload).encode())
            except urllib.error.HTTPError as error:
                try:
                    detail = json.loads(error.read() or b"{}")
                except (ValueError, OSError):
                    detail = {}
                if error.code == 429 and attempt == 0:
                    time.sleep(min(30, int((detail.get("parameters") or {}).get("retry_after", 5))))
                    continue
                if 400 <= error.code < 500:
                    raise TelegramRejected(f"HTTP {error.code}: {str(detail.get('description', ''))[:200]}") from None
                raise
            if not result.get("ok"):
                raise TelegramRejected(str(result.get("description") or "Telegram recusou o envio.")[:200])
            return result["result"]["message_id"]
        raise TelegramRejected("limite de envio do Telegram")

    def send(self, run_id):
        """Reserve one video per product. Nothing goes to Telegram here: each product is delivered later in a
        single sendVideo, with description and links, once its subtitled video is ready."""
        if not self.lock.acquire(blocking=False):
            raise ValueError("Uma operação já está em andamento.")
        try:
            run = self.store.run(run_id)
            if run["status"] != "ready" or run["delivery"] != "not_sent":
                raise ValueError("Consulta não está disponível para envio. Envios confirmados ou incertos não são repetidos.")
            if datetime.now(timezone.utc) - datetime.fromisoformat(run["finished"]) > timedelta(hours=24):
                raise ValueError("Consulta com mais de 24 horas. Atualize as ofertas antes de enviar.")
            products = self.sendable_products(run)
            if not products:
                raise ValueError("Nenhuma oferta selecionada disponível para envio.")
            if any(p["expires"] and p["expires"] < time.time() for p in products):
                raise ValueError("Há ofertas expiradas. Faça uma nova consulta.")
            telegram = self.store.telegram()
            if not telegram["token"] or not telegram["chat_id"]:
                raise ValueError("Configure o Telegram no .env.")
            captions = {}
            for product in products:  # Validates every caption before reserving anything.
                try:
                    captions[product["key"]] = (video_caption(product), None)
                except CaptionTooLong as error:
                    captions[product["key"]] = (None, str(error))
            with self.store.db() as db:
                changed = db.execute("UPDATE runs SET delivery='queued',error=NULL WHERE id=? AND delivery='not_sent'",
                                     (run_id,)).rowcount
                if changed != 1:
                    raise ValueError("Envio já iniciado.")
            queued = 0
            for product in products:
                caption, problem = captions[product["key"]]
                links = [dict(key=l["key"], link=l["link"]) for l in (product.get("analysis") or {}).get("links") or []
                         if l.get("key") and l.get("link")] or [dict(key=product["key"], link=product["link"])]
                snapshot = dict(caption=caption, links=links, name=product["name"][:120])
                state = "blocked" if problem else "pending"
                with self.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO deliveries(run_id,product_key,state,error,snapshot) VALUES(?,?,?,?,?)",
                               (run_id, product["key"], state, problem, json.dumps(snapshot)))
                if problem:
                    self.store.log("error", f"{product['name'][:40]}: entrega bloqueada. {problem}")
                    continue
                try:
                    video_id = self.videos.enqueue(run_id, product, snapshot=snapshot)
                except Exception as error:
                    video_id = None
                    with self.store.db() as db:
                        db.execute("UPDATE deliveries SET state='failed',error=? WHERE run_id=? AND product_key=?",
                                   ("Vídeo não entrou na fila: " + safe_error(error)[:200], run_id, product["key"]))
                if video_id and self.videos.get(video_id)["state"] == "queued":
                    queued += 1
            status = self.store.refresh_delivery(run_id)
            self.store.log("info" if status != "partial" else "error",
                           f"Envio #{run_id}: {queued} de {len(products)} produto(s) aguardando vídeo; cada um vai ao "
                           "Telegram numa única mensagem quando o vídeo ficar pronto.")
            return queued
        finally:
            self.lock.release()

    def tick(self, now=None):
        now = now or datetime.now(TZ)
        self.heartbeat = time.time()
        try:
            self.videos.reconcile()
        except Exception as error:
            self.store.log("error", "Vídeos: " + safe_error(error))
        settings = self.store.settings()
        if not settings["schedule_enabled"] or now.strftime("%H:%M") < settings["schedule_time"]:
            return
        day = now.strftime("%Y-%m-%d")
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO schedule(day,status) VALUES(?,'pending')", (day,))
            slot = dict(db.execute("SELECT * FROM schedule WHERE day=?", (day,)).fetchone())
            if slot["status"] in ("done", "failed", "unknown", "running") or slot["next_retry"] > now.timestamp():
                return
            if self.lock.locked():
                return
            db.execute("UPDATE schedule SET status='running',attempts=attempts+1 WHERE day=?", (day,))
        try:
            run_id = slot["run_id"]
            if run_id is not None:
                previous = self.store.run(run_id)
                if previous["delivery"] in ("sent", "queued", "partial"):
                    with self.store.db() as db:
                        db.execute("UPDATE schedule SET status='done' WHERE day=?", (day,))
                    return
                if previous["delivery"] in ("sending", "unknown"):
                    with self.store.db() as db:
                        db.execute("UPDATE schedule SET status='unknown' WHERE day=?", (day,))
                    return
            if run_id is None:
                run_id = self.collect("scheduled")
                with self.store.db() as db:
                    db.execute("UPDATE schedule SET run_id=? WHERE day=?", (run_id, day))
            if settings["auto_send"]:
                self.send(run_id)
            with self.store.db() as db:
                db.execute("UPDATE schedule SET status='done' WHERE day=?", (day,))
        except Exception as error:
            attempts = slot["attempts"] + 1
            with self.store.db() as db:
                db.execute("UPDATE schedule SET status=?,next_retry=? WHERE day=?",
                           ("failed" if attempts >= 3 else "retry", now.timestamp() + 300 * attempts, day))
            self.store.log("error", f"Agendamento tentativa {attempts}/3: {safe_error(error)}")

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as error:
                    self.store.log("error", "Agendador: " + safe_error(error))
                self.stop.wait(15)
        threading.Thread(target=loop, name="scheduler", daemon=True).start()
