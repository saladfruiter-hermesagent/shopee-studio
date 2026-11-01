import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import subtitles  # noqa: E402
from app import create_app  # noqa: E402
from core import Store  # noqa: E402
from test_studio import FakeRunner, node  # noqa: E402

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
PROJECT = "https://flow.google.com/project/test-project"


def run_inline(target, args, daemon):
    return type("T", (), {"start": lambda self: target(*args)})()


class VideoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sleeper = patch("core.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        with patch.dict(os.environ, {"PANEL_PASSWORD": "test-password-only"}):
            self.app = create_app(self.temp.name, testing=True)
        self.client = self.app.test_client()
        self.store, self.engine = self.app.extensions["store"], self.app.extensions["engine"]
        self.engine.fetcher = lambda _: [node(i, imageUrl=f"https://down-br.img.susercontent.com/file/{i}") for i in range(1, 4)]
        self.engine.searcher = lambda *a: []
        self.engine.runner = FakeRunner()
        self.sent = []
        self.engine.sender = self.sender
        self.store.save_telegram(dict(token="123456:" + "A" * 30, chat_id="-1001234", thread_id="7"))
        self.store.save_settings(dict(top_count=3, per_shop=1, flow_project_url=PROJECT))
        self.token = self.store.set_worker_token()
        self.worker = {"Authorization": f"Bearer {self.token}"}

    def sender(self, url, body, headers=None):
        method = url.rsplit("/", 1)[1]
        if headers and headers["Content-Type"].startswith("multipart/"):
            fields = {k.decode(): v.decode(errors="replace") for k, v in re.findall(rb'name="(\w+)"\r\n\r\n(.*?)\r\n--', body, re.S)}
            fields["file"] = body
        else:
            fields = json.loads(body)
        self.sent.append((method, fields))
        if getattr(self, "refuse", None):
            self.refuse(method)
        return {"ok": True, "result": {"message_id": 100 + len(self.sent)}}

    def deliver(self):
        run_id = self.engine.collect()
        self.engine.send(run_id)
        return run_id

    def videos(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM videos ORDER BY id")]

    def claim(self):
        return self.client.post("/api/worker/claim", json=dict(worker_id="vm.video", kinds=["video"]), headers=self.worker).json["task"]

    def call(self, task, path, **body):
        return self.client.post(f"/api/worker/tasks/{task['id']}/{path}", json=dict(worker_id="vm.video", **body), headers=self.worker)

    def upload(self, task, data=MP4):
        return self.client.post(f"/api/worker/tasks/{task['id']}/video", data=data,
                                headers={**self.worker, "X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"})

    def finish(self, task, meta=None):
        self.upload(task)
        with patch("video.threading.Thread") as thread:
            thread.side_effect = run_inline
            return self.call(task, "result", ok=True, output=meta or dict(width=720, height=1280, duration=10.0))

    def csrf(self):
        return {"X-CSRF-Token": self.client.post("/api/login", json={"password": "test-password-only"}).json["csrf"]}

    def test_send_only_queues_videos_and_sends_nothing_yet(self):
        run_id = self.deliver()
        self.assertEqual(self.sent, [])  # no photo, no text, no prompt before the video is ready
        videos = self.videos()
        self.assertEqual([v["state"] for v in videos], ["queued"] * 3)
        self.assertEqual(sum(v["credits"] for v in videos), 45)
        self.assertTrue(all(v["photo_message_id"] is None and json.loads(v["snapshot"])["caption"] for v in videos))
        self.assertEqual({d["state"] for d in self.store.deliveries(run_id).values()}, {"pending"})
        run = self.store.run(run_id)
        self.assertEqual(run["delivery"], "queued")
        self.assertTrue(all(p["delivery"] == "pending" for p in run["products"] if p["selected"]))
        # Pending products are reserved: a new collection cannot pick them again.
        self.assertTrue({p["key"] for p in run["products"] if p["selected"]} <= self.store.exclusions())
        task = self.claim()
        payload = task["payload"]
        self.assertEqual((payload["model"], payload["expected_cost"], payload["project_url"]), ("Omni 1.1 Flash", 15, PROJECT))
        self.assertIn("10 segundos", payload["prompt"])
        self.assertTrue(payload["narration"])
        self.assertTrue(payload["image_url"].startswith("https://down-br.img.susercontent.com/"))

    def test_ready_video_is_one_message_with_description_and_links(self):
        run_id = self.deliver()
        task = self.claim()
        for stage, credits in (("working", None), ("preparing", None), ("approving", 15), ("submitted", 15),
                               ("downloading", None), ("subtitling", None), ("uploading", None)):
            body = dict(stage=stage) if credits is None else dict(stage=stage, credits=credits)
            self.assertEqual(self.call(task, "progress", **body).status_code, 200)
        big = MP4 + b"\x00" * 70000  # Above the 64 KB global limit: only this endpoint accepts it.
        self.assertEqual(self.upload(task, big).status_code, 200)
        with patch("video.threading.Thread") as thread:
            thread.side_effect = run_inline
            self.assertEqual(self.call(task, "result", ok=True, output=dict(width=720, height=1280, duration=10.0,
                                                                            subtitles=4)).status_code, 200)
        self.assertEqual([m for m, _ in self.sent], ["sendVideo"])
        fields = self.sent[0][1]
        self.assertNotIn("reply_parameters", fields)
        self.assertEqual((fields["parse_mode"], fields["message_thread_id"]), ("HTML", "7"))
        self.assertIn("<pre>Mantém os alimentos frescos", fields["caption"])
        key = self.videos()[0]["product_key"]
        link = next(p["link"] for p in self.store.run(run_id)["products"] if p["key"] == key)
        self.assertIn("1. " + link, fields["caption"])
        self.assertIn(b"ftypmp42", fields["file"])
        video = self.videos()[0]
        self.assertEqual((video["state"], video["file"]), ("sent", None))
        self.assertFalse(list((Path(self.temp.name) / "videos").glob("*.mp4")))
        delivery = self.store.deliveries(run_id)[key]
        self.assertEqual((delivery["state"], delivery["message_id"]), ("sent", 101))
        self.assertEqual(self.store.run(run_id)["delivery"], "queued")  # two products still waiting
        for _ in range(2):
            self.finish(self.claim())
        run = self.store.run(run_id)
        self.assertEqual((run["delivery"], len(self.sent)), ("sent", 3))
        self.assertTrue(run["sent_at"])

    def test_snapshot_freezes_the_message(self):
        run_id = self.deliver()
        key = self.videos()[0]["product_key"]
        product = next(p for p in self.store.run(run_id)["products"] if p["key"] == key)
        product["analysis"]["caption"] = "Texto alterado depois #Outro #Texto"
        self.store.save_analysis(run_id, product)
        self.finish(self.claim())
        self.assertIn("Mantém os alimentos frescos", self.sent[0][1]["caption"])
        self.assertNotIn("alterado", self.sent[0][1]["caption"])

    def test_cap_disabled_and_missing_project_leave_products_failed_without_messages(self):
        self.store.save_settings(dict(video_daily_credits=20))
        run_id = self.deliver()
        states = [(v["state"], v["credits"]) for v in self.videos()]
        self.assertEqual(states, [("queued", 15), ("skipped", 0), ("skipped", 0)])
        self.assertIn("teto diário", self.videos()[1]["error"])
        deliveries = self.store.deliveries(run_id)
        failed = [d for d in deliveries.values() if d["state"] == "failed"]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all("teto diário" in d["error"] for d in failed))
        self.assertEqual(self.sent, [])
        # Nothing reached Telegram for them: they are not excluded from the next collections.
        self.assertFalse({d["product_key"] for d in failed} & self.store.exclusions())
        for settings, expected in ((dict(video_enabled=False), "desligada"), (dict(video_enabled=True, flow_project_url=""), "URL")):
            self.store.save_settings(settings)
            with self.store.db() as db:
                db.execute("DELETE FROM videos")
                db.execute("UPDATE runs SET delivery='not_sent'")
                db.execute("DELETE FROM deliveries")
            self.engine.send(self.store.runs()[0]["id"])
            self.assertTrue(all(v["state"] == "skipped" and expected in v["error"] for v in self.videos()))
            self.assertEqual(self.store.runs()[0]["delivery"], "partial")
            self.assertEqual(self.sent, [])

    def test_upload_and_auth_guards(self):
        self.deliver()
        task = self.claim()
        headers = {**self.worker, "X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"}
        self.assertEqual(self.client.post(f"/api/worker/tasks/{task['id']}/video", data=b"not a video" * 200, headers=headers).status_code, 400)
        self.assertEqual(self.client.post(f"/api/worker/tasks/{task['id']}/video", data=MP4,
                                          headers={**headers, "X-Worker-Id": "other"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/worker/tasks/{task['id']}/video", data=MP4,
                                          headers={"X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"}).status_code, 401)
        self.assertEqual(self.client.post("/api/worker/claim", json=dict(worker_id="w", kinds=["shell"]), headers=self.worker).status_code, 400)
        # Other endpoints keep the 64 KB limit.
        self.assertEqual(self.call(task, "progress", stage="working", pad="x" * 70000).status_code, 413)

    def test_cost_mismatch_frees_credits_pauses_and_alerts(self):
        run_id = self.deliver()
        task = self.claim()
        self.call(task, "progress", stage="preparing")
        self.call(task, "result", ok=False, code="cost_mismatch", error="Flow pediu 30 créditos", charged=False)
        video = self.videos()[0]
        self.assertEqual((video["state"], video["credits"]), ("failed", 0))
        self.assertEqual(self.store.deliveries(run_id)[video["product_key"]]["state"], "failed")
        pause = self.engine.videos.flag("video_pause")
        self.assertEqual(pause["code"], "cost_mismatch")
        method, alert = self.sent[0]  # the operational alert is the only message
        self.assertEqual((len(self.sent), method, alert["message_thread_id"]), (1, "sendMessage", 7))
        self.assertIn("pausados", alert["text"])
        # While paused the worker may not approve a cost, and new products are skipped.
        task2 = self.claim()
        self.assertEqual(self.call(task2, "progress", stage="approving", credits=15).status_code, 400)
        self.assertIsNone(self.client.post("/api/videos/resume", json={}, headers=self.csrf()).json["paused"])
        self.assertEqual(self.call(task2, "progress", stage="approving", credits=15).status_code, 200)

    def test_failure_after_approval_keeps_credits_as_uncertain(self):
        run_id = self.deliver()
        task = self.claim()
        self.call(task, "progress", stage="approving", credits=15)
        self.call(task, "result", ok=False, code="render_timeout", error="não ficou pronto", charged=True)
        video = self.videos()[0]
        self.assertEqual((video["state"], video["credits"]), ("unknown", 15))
        delivery = self.store.deliveries(run_id)[video["product_key"]]
        self.assertEqual(delivery["state"], "failed")
        self.assertIn("créditos", delivery["error"])
        self.assertIsNone(self.engine.videos.flag("video_pause"))
        self.assertEqual(self.engine.videos.used_today(), 45)

    def test_busy_browser_is_retried_then_given_up(self):
        self.store.save_settings(dict(top_count=1))
        self.deliver()
        for attempt in range(3):
            task = self.claim()
            self.assertEqual(task["payload"]["video_id"], 1)
            self.call(task, "result", ok=False, code="browser_busy", error="perfil em uso")
        video = self.videos()[0]
        self.assertEqual((video["state"], video["attempts"]), ("failed", 3))

    def test_expired_tasks_are_reconciled(self):
        run_id = self.deliver()
        task = self.claim()
        self.call(task, "progress", stage="submitted", credits=15)
        with self.store.db() as db:
            db.execute("UPDATE tasks SET deadline=? WHERE kind='video'", (time.time() - 1,))
        self.engine.videos.reconcile()
        states = [(v["state"], v["credits"]) for v in self.videos()]
        self.assertEqual(states, [("unknown", 15), ("failed", 0), ("failed", 0)])
        self.assertEqual({d["state"] for d in self.store.deliveries(run_id).values()}, {"failed"})
        self.assertEqual(self.store.run(run_id)["delivery"], "partial")
        self.assertEqual(self.sent, [])

    def test_telegram_refusal_keeps_file_for_manual_resend_without_new_credits(self):
        run_id = self.deliver()
        def refuse(method):
            if method == "sendVideo":
                raise urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"description":"file too big"}'))
        self.refuse = refuse
        self.finish(self.claim())
        video = self.videos()[0]
        self.assertEqual(video["state"], "delivery_failed")
        self.assertEqual(self.store.deliveries(run_id)[video["product_key"]]["state"], "refused")
        self.assertIn(video["product_key"], self.store.exclusions())  # the ready file stays reserved for Reenviar
        self.refuse = None
        result = self.client.post("/api/videos/1/resend", json={}, headers=self.csrf())
        self.assertEqual(result.json["state"], "sent")
        self.assertEqual(len(self.videos()), 3)          # no new generation
        self.assertEqual(self.engine.videos.used_today(), 45)
        self.assertEqual(self.store.deliveries(run_id)[video["product_key"]]["state"], "sent")

    def test_uncertain_send_is_never_repeated(self):
        run_id = self.deliver()
        def timeout(method):
            if method == "sendVideo":
                raise TimeoutError("read timeout")
        self.refuse = timeout
        self.finish(self.claim())
        video = self.videos()[0]
        self.assertEqual(video["state"], "delivery_unknown")
        self.assertEqual(self.store.deliveries(run_id)[video["product_key"]]["state"], "unknown")
        self.assertIn(video["product_key"], self.store.exclusions())
        self.refuse = None
        count = len(self.sent)
        self.engine.videos.reconcile()
        self.assertEqual(len(self.sent), count)
        self.assertEqual(self.client.post("/api/videos/1/resend", json={}, headers=self.csrf()).status_code, 400)

    def test_restart_keeps_video_tasks_and_flags_interrupted_delivery(self):
        run_id = self.deliver()
        with self.store.db() as db:
            db.execute("UPDATE videos SET state='sending' WHERE id=1")
            db.execute("UPDATE deliveries SET state='sending' WHERE product_key=(SELECT product_key FROM videos WHERE id=1)")
        Store(self.temp.name)
        self.assertEqual(self.videos()[0]["state"], "delivery_unknown")
        self.assertEqual(self.store.deliveries(run_id)[self.videos()[0]["product_key"]]["state"], "unknown")
        self.assertIsNotNone(self.claim())

    def test_manual_generation_reuses_the_snapshot_and_respects_the_budget(self):
        run_id = self.deliver()
        headers = self.csrf()
        key = self.videos()[0]["product_key"]
        snapshot = self.videos()[0]["snapshot"]
        self.assertEqual(self.client.post("/api/videos", json=dict(run_id=run_id, key=key), headers=headers).status_code, 400)
        with self.store.db() as db:
            db.execute("UPDATE videos SET state='failed',credits=0 WHERE id=1")
            db.execute("UPDATE deliveries SET state='failed' WHERE product_key=?", (key,))
        self.assertEqual(self.client.post("/api/videos", json=dict(run_id=run_id, key=key), headers=headers).status_code, 200)
        self.assertEqual((self.videos()[-1]["snapshot"], self.videos()[-1]["state"]), (snapshot, "queued"))
        self.assertEqual(self.store.deliveries(run_id)[key]["state"], "pending")
        self.assertEqual(self.client.post("/api/videos", json=dict(run_id=run_id, key="9:9"), headers=headers).status_code, 400)
        state = self.client.get("/api/state").json["video"]
        self.assertEqual((state["used"], state["cap"], state["cost"]), (45, 75, 15))

    def test_failed_product_sent_by_another_run_cannot_be_regenerated(self):
        first = self.deliver()
        key = self.videos()[0]["product_key"]
        with self.store.db() as db:
            db.execute("UPDATE videos SET state='failed',credits=0 WHERE id=1")
            db.execute("UPDATE deliveries SET state='failed' WHERE run_id=? AND product_key=?", (first, key))
            db.execute("INSERT INTO deliveries(run_id,product_key,state,snapshot) VALUES(999,?,'sent','{}')", (key,))
        response = self.client.post("/api/videos", json=dict(run_id=first, key=key), headers=self.csrf())
        self.assertEqual(response.status_code, 400)
        self.assertIn("outra consulta", response.json["error"])

    def test_legacy_job_still_replies_to_its_photo(self):
        run_id = self.deliver()
        key = self.videos()[0]["product_key"]
        path = Path(self.temp.name) / "videos" / "video-99.mp4"
        path.write_bytes(MP4)
        with self.store.db() as db:
            db.execute("INSERT INTO videos(id,run_id,product_key,day,state,credits,photo_message_id,file,meta) "
                       "VALUES(99,?,?,'2026-10-31','rendered',15,555,'video-99.mp4','{}')", (run_id, key))
        self.engine.videos.deliver(99)
        method, fields = self.sent[0]
        self.assertEqual((method, json.loads(fields["reply_parameters"])["message_id"]), ("sendVideo", 555))
        self.assertEqual(self.store.deliveries(run_id)[key]["state"], "pending")  # the new-flow job is untouched

    def test_caption_overflow_blocks_only_that_product(self):
        self.engine.collect()
        run_id = self.store.runs()[0]["id"]
        run = self.store.run(run_id)
        product = next(p for p in run["products"] if p["selected"])
        product["analysis"]["links"] = [dict(key=f"{i}:{i}", link="https://shopee.com.br/" + "a" * 300 + str(i))
                                        for i in range(6)]
        self.store.save_analysis(run_id, product)
        self.engine.send(run_id)
        delivery = self.store.deliveries(run_id)[product["key"]]
        self.assertEqual(delivery["state"], "blocked")
        self.assertIn("1024", delivery["error"])
        self.assertEqual(len(self.videos()), 2)
        self.assertEqual(self.sent, [])


class SubtitleTest(unittest.TestCase):
    def words(self, text, start=0.0, step=0.3):
        return [dict(start=start + i * step, end=start + i * step + 0.25, word=" " + w) for i, w in enumerate(text.split())]

    def test_script_spelling_wins_when_speech_matches(self):
        heard = self.words("olha so que pratico fecha com um clique")
        aligned = subtitles.align_to_script(heard, "Olha só que prático: fecha com um clique.")
        self.assertEqual(" ".join(w["word"] for w in aligned), "Olha só que prático: fecha com um clique.")
        self.assertEqual(aligned[0]["start"], 0.0)

    def test_improvised_speech_is_kept(self):
        aligned = subtitles.align_to_script(self.words("compre agora mesmo"), "Toque no produto do vídeo.")
        self.assertEqual([w["word"] for w in aligned], ["compre", "agora", "mesmo"])

    def test_cues_split_evenly_without_orphans(self):
        words = self.words("Veja o produto no carrinho.")
        result = subtitles.cues(words)
        self.assertEqual([c["text"] for c in result], ["Veja o", "produto no carrinho."])
        self.assertTrue(all(c["end"] > c["start"] for c in result))
        pause = self.words("Um", 0) + self.words("dois", 2.0)
        self.assertEqual(len(subtitles.cues(pause)), 2)

    def test_ass_escapes_override_braces(self):
        text = subtitles.ass([dict(start=0, end=1.5, text="{\\b1}oi")], 720, 1280)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.50,Fala,,0,0,0,,(b1)oi", text)
        self.assertIn("PlayResY: 1280", text)

    def test_silent_video_is_kept_without_subtitles(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(subtitles, "probe",
                                                                  return_value=dict(width=720, height=1280, duration=10, audio=True)):
            source, target = Path(folder) / "a.mp4", Path(folder) / "b.mp4"
            source.write_bytes(MP4)
            meta = subtitles.subtitle_video(source, "Olha só", target, folder, transcriber=lambda *a: [])
            self.assertEqual((meta["subtitles"], target.read_bytes()), (0, MP4))


class FlowTextTest(unittest.TestCase):
    def test_cost_and_failure_patterns(self):
        import flow
        self.assertEqual(flow.COST.search("Quer que eu inicie essa 1 geração de vídeo, que custa 15 créditos?").group(1), "15")
        self.assertTrue(flow.NO_CREDITS.search("Você não tem créditos suficientes"))
        self.assertTrue(flow.CHALLENGE.search("Confirme que é você"))
        self.assertFalse(flow.FAILED.search("O processo de geração foi iniciado com sucesso"))


if __name__ == "__main__":
    unittest.main()
