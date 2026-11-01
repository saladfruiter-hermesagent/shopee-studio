"""Video jobs: Google Flow generation on the VM worker, credit budget, pause/alerts and Telegram delivery.

Each product reaches Telegram in a single sendVideo: the subtitled video with the description and links frozen
in the job's snapshot. Jobs created before that change (no snapshot) still reply to their product photo."""
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

VIDEO_TASK_SECONDS = 6 * 3600     # A queued job may wait for the worker this long.
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit.
PAUSE_CODES = {"login_required", "challenge", "cost_mismatch", "no_credits", "model_mismatch", "no_project"}
RETRY_CODES = {"browser_busy"}
MAX_ATTEMPTS = 3
# Stages reported by the worker, in order. From "approving" on, credits may have been spent.
STAGES = ["working", "preparing", "approving", "submitted", "downloading", "subtitling", "uploading"]
CHARGED = {"approving", "submitted", "downloading", "subtitling", "uploading"}
ACTIVE = {"queued", *STAGES}
# A product with a job in one of these states does not get another automatic job.
BLOCKING = ACTIVE | {"rendered", "sending", "sent", "delivery_unknown", "delivery_failed", "unknown"}
LABELS = {"queued": "na fila", "working": "worker iniciou", "preparing": "preparando no Flow",
          "approving": "aprovando custo", "submitted": "gerando no Flow", "downloading": "baixando",
          "subtitling": "legendando", "uploading": "enviando ao painel", "rendered": "pronto para enviar",
          "sending": "enviando ao Telegram", "sent": "enviado", "delivery_unknown": "entrega não confirmada",
          "delivery_failed": "envio ao Telegram falhou", "failed": "falhou (sem custo)",
          "unknown": "incerto (créditos podem ter sido gastos)", "skipped": "não gerado"}
# Product delivery state that mirrors each job state (single-message flow only).
DELIVERY = {**{state: "pending" for state in ACTIVE}, "rendered": "pending", "sending": "sending", "sent": "sent",
            "delivery_unknown": "unknown", "delivery_failed": "refused", "failed": "failed", "unknown": "failed",
            "skipped": "failed"}


def today(tz):
    return datetime.now(tz).strftime("%Y-%m-%d")


class Videos:
    def __init__(self, engine, tz):
        self.engine, self.store, self.tz = engine, engine.store, tz
        self.lock = threading.Lock()
        self.directory = self.store.path.parent / "videos"
        self.directory.mkdir(exist_ok=True)

    # ---------- state ----------
    def flag(self, key, value=None, clear=False):
        with self.store.db() as db:
            if clear:
                db.execute("DELETE FROM flags WHERE key=?", (key,))
                return None
            if value is not None:
                db.execute("INSERT INTO flags VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(value)))
                return value
            row = db.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def used_today(self):
        with self.store.db() as db:
            return db.execute("SELECT COALESCE(SUM(credits),0) FROM videos WHERE day=?", (today(self.tz),)).fetchone()[0]

    def summary(self):
        settings = self.store.settings()
        return dict(enabled=settings["video_enabled"], used=self.used_today(), cap=settings["video_daily_credits"],
                    cost=settings["video_cost"], paused=self.flag("video_pause"),
                    worker_online=self.store.worker_online("video"), labels=LABELS)

    def get(self, video_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            raise ValueError("Vídeo não encontrado.")
        return self._public(dict(row))

    @staticmethod
    def _public(row):
        row["meta"] = json.loads(row["meta"]) if row.get("meta") else {}
        row["label"] = LABELS.get(row["state"], row["state"])
        row.pop("file", None)
        return row

    @staticmethod
    def latest_static(store, run_id):
        """Latest job per product of a run (later rows win)."""
        with store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM videos WHERE run_id=? ORDER BY id", (run_id,))]
        return {row["product_key"]: Videos._public(row) for row in rows}

    def update(self, video_id, **values):
        values["updated"] = datetime.now(self.tz).isoformat()
        with self.store.db() as db:
            db.execute(f"UPDATE videos SET {','.join(k + '=?' for k in values)} WHERE id=?", (*values.values(), video_id))

    # ---------- queueing ----------
    def enqueue(self, run_id, product, photo_message_id=None, manual=False, snapshot=None):
        """Create the job, or a 'skipped' record explaining why no credits will be spent.
        `snapshot` = caption and links of the single message sent when the video is ready."""
        settings = self.store.settings()
        analysis = product.get("analysis") or {}
        with self.lock:
            with self.store.db() as db:
                active = db.execute(f"SELECT id FROM videos WHERE run_id=? AND product_key=? AND state IN ({','.join('?' * len(BLOCKING))})",
                                    (run_id, product["key"], *BLOCKING)).fetchone()
            if active and not manual:
                return None
            if active and manual:
                raise ValueError("Este produto já tem um vídeo gerado ou em andamento.")
            reason = None
            pause = self.flag("video_pause")
            if not settings["video_enabled"]:
                reason = "geração de vídeo desligada nas Preferências"
            elif pause:
                reason = "vídeos pausados: " + pause["reason"]
            elif not settings["flow_project_url"]:
                reason = "URL do projeto do Flow não configurada"
            elif not product.get("image"):
                reason = "anúncio sem imagem de referência"
            elif self.used_today() + settings["video_cost"] > settings["video_daily_credits"]:
                reason = f"teto diário de {settings['video_daily_credits']} créditos atingido"
            now = datetime.now(self.tz).isoformat()
            with self.store.db() as db:
                video_id = db.execute(
                    "INSERT INTO videos(run_id,product_key,day,state,credits,attempts,photo_message_id,error,created,updated,snapshot) "
                    "VALUES(?,?,?,?,?,0,?,?,?,?,?)",
                    (run_id, product["key"], today(self.tz), "skipped" if reason else "queued",
                     0 if reason else settings["video_cost"], photo_message_id, reason, now, now,
                     json.dumps(snapshot) if snapshot else None)).lastrowid
            if reason:
                if manual:
                    raise ValueError("Vídeo não gerado: " + reason + ".")
                self.store.log("info", f"Vídeo de {product['name'][:50]} não gerado: {reason}.")
                self.sync(video_id)
                return video_id
            self._task(video_id, product, analysis, settings)
            self.sync(video_id)
            self.store.log("info", f"Vídeo de {product['name'][:50]} na fila do worker ({settings['video_cost']} créditos reservados).")
            return video_id

    def _task(self, video_id, product, analysis, settings):
        payload = dict(video_id=video_id, product=product["name"][:120], image_url=product["image"],
                       prompt=analysis.get("prompt") or "", narration=analysis.get("narration") or "",
                       model=settings["video_model"], expected_cost=settings["video_cost"],
                       project_url=settings["flow_project_url"])
        task_id = self.store.create_task("video", payload, deadline=VIDEO_TASK_SECONDS)
        with self.store.db() as db:
            db.execute("UPDATE videos SET task_id=?,attempts=attempts+1,state='queued' WHERE id=?", (task_id, video_id))

    def enqueue_manual(self, run_id, key):
        run = self.store.run(run_id)
        product = next((p for p in run["products"] if p["key"] == key), None)
        delivery = self.store.deliveries(run_id).get(key)
        if not product or not delivery:
            raise ValueError("Só é possível gerar vídeo de produto de uma consulta já enviada.")
        if delivery.get("snapshot"):
            if delivery["state"] != "failed":
                raise ValueError("Só é possível gerar de novo um produto com falha; aguarde, confira ou reenvie o vídeo pronto.")
            if key in self.store.exclusions():
                raise ValueError("Este produto já foi enviado, está em andamento em outra consulta ou foi descartado.")
            # Same message as the original reservation: later edits never change what was approved.
            return self.enqueue(run_id, product, manual=True, snapshot=json.loads(delivery["snapshot"]))
        if delivery["state"] != "sent" or not delivery["photo_message_id"]:
            raise ValueError("Só é possível gerar vídeo de produto já enviado ao Telegram.")
        return self.enqueue(run_id, product, delivery["photo_message_id"], manual=True)

    def sync(self, video_id):
        """Mirror a single-message job on its product delivery and refresh the run status."""
        with self.store.db() as db:
            row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row or not row["snapshot"]:
            return
        state, error = DELIVERY.get(row["state"], "pending"), row["error"] or "sem detalhe"
        message = {"failed": {"failed": f"Vídeo falhou sem custo: {error} Use Gerar vídeo para tentar de novo.",
                              "unknown": f"Geração incerta; créditos podem ter sido gastos. Confira o Flow antes de gerar "
                                         f"de novo. {error}",
                              "skipped": f"Vídeo não gerado: {error}."}.get(row["state"]),
                   "refused": f"Telegram recusou o vídeo ({error}). O arquivo foi guardado: use Reenviar vídeo.",
                   "unknown": "Entrega não confirmada; confira o Telegram. Não haverá reenvio automático."}.get(state)
        with self.store.db() as db:
            db.execute("UPDATE deliveries SET state=?,error=?,message_id=COALESCE(?,message_id),sent_at=? "
                       "WHERE run_id=? AND product_key=? AND snapshot IS NOT NULL",
                       (state, message, row["message_id"] if state == "sent" else None,
                        row["updated"] if state == "sent" else None, row["run_id"], row["product_key"]))
        self.store.refresh_delivery(row["run_id"])

    def by_task(self, task_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM videos WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Tarefa sem vídeo associado.")
        return dict(row)

    # ---------- worker callbacks ----------
    def progress(self, task_id, worker_id, stage, credits=None):
        task = self.store.task(task_id)
        if task["status"] != "claimed" or task["worker_id"] != worker_id:
            raise ValueError("Tarefa expirada ou de outro worker.")
        if stage not in STAGES:
            raise ValueError("Etapa desconhecida.")
        video = self.by_task(task_id)
        if stage == "approving" and self.flag("video_pause"):
            raise ValueError("Vídeos pausados; não aprove o custo.")
        values = dict(state=stage)
        if credits is not None:
            if isinstance(credits, bool) or not isinstance(credits, int) or not 0 <= credits <= 1000:
                raise ValueError("Créditos inválidos.")
            values["credits"] = credits
        self.update(video["id"], **values)
        if stage == "submitted":
            self.store.log("info", f"Vídeo #{video['id']}: geração aprovada no Flow ({credits} créditos).")

    def upload(self, task_id, worker_id, data):
        task = self.store.task(task_id)
        if task["status"] != "claimed" or task["worker_id"] != worker_id:
            raise ValueError("Tarefa expirada ou de outro worker.")
        if len(data) > MAX_VIDEO_BYTES or len(data) < 1024 or data[4:8] != b"ftyp":
            raise ValueError("Arquivo de vídeo inválido (MP4 até 50 MB).")
        video = self.by_task(task_id)
        path = self.directory / f"video-{video['id']}.mp4"
        path.write_bytes(data)
        self.update(video["id"], file=path.name)

    def finish(self, task_id, ok, output, code="", error="", charged=False):
        video = self.by_task(task_id)
        if ok:
            if not video["file"]:
                self.update(video["id"], state="unknown", error="Worker concluiu sem enviar o arquivo.")
                self.sync(video["id"])
                return
            meta = output if isinstance(output, dict) else {}
            self.update(video["id"], state="rendered", meta=json.dumps(meta)[:4000], error=None)
            self.store.log("info", f"Vídeo #{video['id']} pronto ({meta.get('subtitles', 0)} legendas).")
            threading.Thread(target=self.deliver, args=(video["id"],), daemon=True).start()
            return
        message = str(error or "falha sem detalhe")[:400]
        spent = charged or video["state"] in CHARGED
        if code in RETRY_CODES and not spent and video["attempts"] < MAX_ATTEMPTS:
            run = self.store.run(video["run_id"])
            product = next(p for p in run["products"] if p["key"] == video["product_key"])
            self.update(video["id"], error=message)
            self._task(video["id"], product, product.get("analysis") or {}, self.store.settings())
            self.store.log("info", f"Vídeo #{video['id']}: {message} Nova tentativa na fila.")
            return
        self.update(video["id"], state="unknown" if spent else "failed", credits=video["credits"] if spent else 0,
                    error=message)
        self.sync(video["id"])
        self.store.log("error", f"Vídeo #{video['id']} {'incerto' if spent else 'falhou'}: {message}")
        if code in PAUSE_CODES:
            self.pause(code, message)

    def reconcile(self):
        """Jobs whose task expired: free the reservation unless credits may have been spent."""
        with self.store.db() as db:
            db.execute("UPDATE tasks SET status='expired' WHERE kind='video' AND status IN ('queued','claimed') AND deadline<?",
                       (time.time(),))
            rows = [dict(r) for r in db.execute(f"""SELECT v.* FROM videos v JOIN tasks t ON t.id=v.task_id
                WHERE t.status='expired' AND v.state IN ({','.join('?' * len(ACTIVE))})""", tuple(ACTIVE))]
        for video in rows:
            spent = video["state"] in CHARGED
            self.update(video["id"], state="unknown" if spent else "failed", credits=video["credits"] if spent else 0,
                        error="O worker não concluiu no prazo." + (" Confira o projeto no Flow." if spent else ""))
            self.sync(video["id"])
        with self.store.db() as db:
            pending = [r[0] for r in db.execute("SELECT id FROM videos WHERE state='rendered'")]
        for video_id in pending:
            self.deliver(video_id)

    # ---------- pause / alerts ----------
    def pause(self, code, message):
        if self.flag("video_pause"):
            return
        self.flag("video_pause", dict(code=code, reason=message[:300], since=datetime.now(self.tz).isoformat()))
        self.store.log("error", "Vídeos pausados: " + message)
        hint = {"login_required": "Faça login no Flow pela VM (worker/flow_login.sh).",
                "challenge": "Resolva a verificação do Google pela VM (worker/flow_login.sh).",
                "cost_mismatch": "Confira o custo e o modelo no Flow.",
                "no_credits": "Confira o saldo de créditos do Flow.",
                "model_mismatch": "Ajuste o modelo padrão de vídeo no projeto do Flow ou nas Preferências.",
                "no_project": "Informe a URL do projeto do Flow nas Preferências."}.get(code, "")
        self.alert(f"Vídeos do Flow pausados: {message}\n{hint}\nDepois, toque em Retomar em Preferências → Vídeo.")

    def resume(self):
        self.flag("video_pause", clear=True)
        self.store.log("info", "Geração de vídeos retomada.")

    def alert(self, text):
        telegram = self.store.telegram()
        if not telegram["token"] or not telegram["chat_id"]:
            return
        payload = {"chat_id": telegram["chat_id"], "text": text[:3500]}
        if telegram["thread_id"]:
            payload["message_thread_id"] = int(telegram["thread_id"])
        try:
            self.engine.telegram_call(telegram["token"], "sendMessage", payload)
        except Exception as error:
            self.store.log("error", "Alerta do Telegram não enviado: " + str(error)[:200].replace(telegram["token"], "[oculto]"))

    # ---------- delivery ----------
    def deliver(self, video_id, manual=False):
        """Send the ready video. Single-message jobs carry the snapshot caption (description + links);
        legacy jobs reply to their product photo. A refusal keeps the file; an uncertain result is never retried."""
        from core import TelegramRejected
        with self.lock:
            with self.store.db() as db:
                row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
                allowed = ("rendered", "delivery_failed") if manual else ("rendered",)
                if not row or row["state"] not in allowed or not row["file"]:
                    if manual:
                        raise ValueError("Vídeo não está disponível para envio.")
                    return
                db.execute("UPDATE videos SET state='sending' WHERE id=?", (video_id,))
        self.sync(video_id)
        video = dict(row)
        path = self.directory / video["file"]
        telegram = self.store.telegram()
        token = telegram["token"]
        meta = json.loads(video["meta"] or "{}")
        snapshot = json.loads(video["snapshot"]) if video.get("snapshot") else None
        if snapshot:
            name = snapshot.get("name") or "Produto"
            payload = {"chat_id": telegram["chat_id"], "caption": snapshot["caption"], "parse_mode": "HTML",
                       "supports_streaming": "true"}
        else:
            run = self.store.run(video["run_id"])
            name = next((p["name"] for p in run["products"] if p["key"] == video["product_key"]), "Produto")
            payload = {"chat_id": telegram["chat_id"], "caption": f"Vídeo legendado · {name[:90]}", "supports_streaming": "true",
                       "reply_parameters": {"message_id": video["photo_message_id"], "allow_sending_without_reply": True}}
        for key in ("width", "height"):
            if meta.get(key):
                payload[key] = str(meta[key])
        if meta.get("duration"):
            payload["duration"] = str(round(meta["duration"]))
        if telegram["thread_id"]:
            payload["message_thread_id"] = telegram["thread_id"]
        try:
            if not token or not telegram["chat_id"]:
                raise TelegramRejected("Telegram não configurado.")
            message_id = self.engine.telegram_call(token, "sendVideo", payload,
                                                   upload=("video", re.sub(r"\W", "", name)[:30] + ".mp4", "video/mp4",
                                                           path.read_bytes()))
        except TelegramRejected as error:
            self.update(video_id, state="delivery_failed", error=str(error)[:300])
            self.sync(video_id)
            self.store.log("error", f"Vídeo #{video_id} recusado pelo Telegram: {error}")
            return
        except Exception as error:
            self.update(video_id, state="delivery_unknown", error="Entrega não confirmada; confira o Telegram.")
            self.sync(video_id)
            self.store.log("error", f"Vídeo #{video_id}: entrega não confirmada ({str(error)[:150].replace(token, '[oculto]')}).")
            return
        self.update(video_id, state="sent", message_id=message_id, error=None, file=None)
        self.sync(video_id)
        path.unlink(missing_ok=True)
        self.store.log("info", f"Vídeo #{video_id} enviado ao Telegram" + (" com descrição e links." if snapshot
                                                                          else " em resposta ao produto."))
