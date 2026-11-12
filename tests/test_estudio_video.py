"""Estúdio S2 (PLANO_ESTUDIO.md): geração no Flow com várias referências, aprovação do custo, prévia antes do
Telegram (E3/E4), Do dia no teto e Manual fora dele (E8/E11)."""
import base64
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import test_estudio as s1  # module import: the S1 TestCase is borrowed, not collected again here
from core import TZ
from test_studio import FakeRunner
from video import fidelity_prompt

JPEG, SHORT = s1.JPEG, s1.SHORT

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


def run_inline(target, args, daemon):
    return type("T", (), {"start": lambda self: target(*args)})()


class VideoRunner(s1.StudioRunner):
    """Studio answers + the fidelity check (schema with "score") + the daily analysis of `send` (FakeRunner)."""
    analysis = FakeRunner()

    def run(self, prompt, schema, images, check, log=None, deadline=None):
        if not {"scripts", "facts", "score"} & set(schema["properties"]):
            return self.analysis.run(prompt, schema, images, check, log, deadline)
        if "score" in schema["properties"]:
            self.calls.append(dict(prompt=prompt, images=images))
            output = dict(score=8, reason="mesmo produto")
            assert check(output) == []
            return output, "Fake", ["Fake: ok"]
        return super().run(prompt, schema, images, check, log, deadline)


class StudioVideoTest(unittest.TestCase):
    post, send, videos = s1.StudioTest.post, s1.StudioTest.send, s1.StudioTest.videos

    def setUp(self):  # same fixture as the S1 tests, plus the video worker
        s1.StudioTest.setUp(self)
        self.runner = VideoRunner()
        self.engine.runner = self.runner
        self.sent = []
        self.engine.sender = self.sender
        self.token = self.store.set_worker_token()
        self.worker = {"Authorization": f"Bearer {self.token}"}

    def sender(self, url, body, headers=None):
        method = url.rsplit("/", 1)[1]
        if headers and headers["Content-Type"].startswith("multipart/"):
            fields = {k.decode(): v.decode(errors="replace") for k, v in re.findall(rb'name="(\w+)"\r\n\r\n(.*?)\r\n--', body, re.S)}
        else:
            fields = json.loads(body)
        self.sent.append((method, fields))
        return {"ok": True, "result": {"message_id": 500 + len(self.sent)}}

    # ---------- helpers ----------
    def connect_worker(self, version="1.3.0"):
        self.client.post("/api/worker/claim", json=dict(worker_id="vm.video", version=version, kinds=["llm"]),
                         headers=self.worker)  # registers the worker without taking video tasks
        with self.store.db() as db:
            db.execute("UPDATE workers SET kinds=? WHERE id='vm.video'", (json.dumps(["video"]),))

    def draft(self, origin="manual", photos=1):
        if origin == "manual":
            draft = self.post("/studio/manual", dict(url=SHORT)).json
        else:
            self.run_id = self.send(2)
            draft = self.post("/studio/day", dict(run_id=self.run_id, key="3:3")).json
        sid = draft["id"]
        for _ in range(photos):
            self.post(f"/studio/{sid}/photos", dict(data=base64.b64encode(JPEG).decode()))
        self.post(f"/studio/{sid}/facts", dict(text="- Pote transparente com tampa azul\n- Quatro travas"))
        self.post(f"/studio/{sid}/format", dict(format="hands_on"))
        self.assertEqual(self.post(f"/studio/{sid}/scripts").status_code, 202)
        self.post(f"/studio/{sid}/scripts/1", dict(choose=True))
        return sid

    def generate(self, sid, cost=15):
        return self.post(f"/studio/{sid}/generate", dict(cost=cost))

    def claim(self):
        """Next Studio task (automatic videos of the same run are claimed and left aside)."""
        while True:
            task = self.client.post("/api/worker/claim", json=dict(worker_id="vm.video", version="1.3.0", kinds=["video"]),
                                    headers=self.worker).json["task"]
            if task is None or task["payload"].get("studio_id"):
                return task

    def call(self, task, path, **body):
        return self.client.post(f"/api/worker/tasks/{task['id']}/{path}", json=dict(worker_id="vm.video", **body),
                                headers=self.worker)

    def render(self, task):
        """Worker path up to the upload: approving → submitted → frames → MP4 → result ok."""
        self.assertEqual(self.call(task, "progress", stage="approving", credits=15).status_code, 200)
        self.call(task, "progress", stage="submitted", credits=15)
        self.assertEqual(self.call(task, "frames", frames=[base64.b64encode(JPEG).decode()]).status_code, 200)
        self.client.post(f"/api/worker/tasks/{task['id']}/video", data=MP4,
                         headers={**self.worker, "X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"})
        with patch("video.threading.Thread") as thread:
            thread.side_effect = run_inline
            return self.call(task, "result", ok=True, output=dict(width=1080, height=1920, duration=10.0, subtitles=4))

    def get(self, sid):
        return self.client.get(f"/api/studio/{sid}").json

    # ---------- testes ----------
    def test_generation_needs_the_approved_cost_and_a_new_worker(self):
        sid = self.draft()
        refused = self.generate(sid)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("não se conectou", refused.json["error"])
        self.connect_worker("1.2.1")
        old = self.generate(sid)
        self.assertIn("1.2.1", old.json["error"])
        self.assertEqual(self.get(sid)["generation"]["worker_problem"], old.json["error"])
        self.connect_worker()
        stale = self.generate(sid, cost=10)
        self.assertEqual(stale.status_code, 400)
        self.assertIn("15 créditos", stale.json["error"])
        self.assertEqual(self.generate(sid, cost=True).status_code, 400)
        self.assertEqual(self.videos(), [])  # refusals never create a row
        started = self.generate(sid)
        self.assertEqual(started.status_code, 202, started.json)
        video = started.json["video"]
        self.assertEqual((video["state"], video["credits"], video["origin"]), ("queued", 15, "manual"))
        again = self.generate(sid)
        self.assertEqual(again.status_code, 400)
        self.assertIn("em andamento", again.json["error"])

    def test_payload_carries_every_reference_and_the_chosen_script(self):
        self.connect_worker()
        sid = self.draft(photos=4)  # API photo + 4 of the user = 5; the Flow gets 4, main first
        self.post(f"/studio/{sid}/photos/u3/main")
        self.assertEqual(self.generate(sid).status_code, 202)
        task = self.claim()
        payload = task["payload"]
        self.assertEqual(payload["images"], [dict(photo="u3"), dict(url="https://down-br.img.susercontent.com/file/t"),
                                             dict(photo="u1"), dict(photo="u2")])
        self.assertEqual(payload["image_url"], "https://down-br.img.susercontent.com/file/t")
        self.assertIn("As imagens anexadas mostram o mesmo produto", payload["prompt"])
        draft = self.get(sid)
        self.assertEqual(payload["prompt"], draft["prompt"])
        self.assertEqual(payload["narration"], draft["scripts"][1]["narration"])
        self.assertEqual((payload["expected_cost"], payload["studio_id"]), (15, sid))
        self.assertTrue(payload["finish"]["frames"] and payload["finish"]["badge"])
        self.assertEqual(len(draft["generation"]["refs"]), 4)

    def test_worker_downloads_only_the_photos_of_its_task(self):
        self.connect_worker()
        sid = self.draft(photos=2)
        self.generate(sid)
        task = self.claim()
        url = f"/api/worker/tasks/{task['id']}/images/"
        mine = {**self.worker, "X-Worker-Id": "vm.video"}
        served = self.client.get(url + "u1", headers=mine)
        self.assertEqual((served.status_code, served.mimetype, served.data), (200, "image/jpeg", JPEG))
        self.assertEqual(self.client.get(url + "u9", headers=mine).status_code, 400)  # not in this task
        self.assertEqual(self.client.get(url + "u1", headers={**self.worker, "X-Worker-Id": "other"}).status_code, 400)
        self.assertEqual(self.client.get(url + "u1", headers={"X-Worker-Id": "vm.video"}).status_code, 401)

    def test_ready_video_waits_for_the_preview_and_goes_out_only_when_approved(self):
        self.connect_worker()
        sid = self.draft()
        self.generate(sid)
        task = self.claim()
        self.assertEqual(self.render(task).status_code, 200)
        draft = self.get(sid)
        video = draft["video"]
        self.assertEqual(video["state"], "review")
        self.assertEqual(self.sent, [])  # E4: nothing reaches Telegram on its own
        self.assertEqual(video["meta"]["fidelity"]["score"], 8)
        fidelity = self.runner.calls[-1]
        self.assertEqual([i["name"] for i in fidelity["images"]], ["R1", "R2", "F1"])  # API + user photo + frame
        self.assertIn("R1, R2 são fotos do produto", fidelity["prompt"])
        self.engine.videos.reconcile()  # the periodic delivery never picks a Studio preview
        self.assertEqual(self.sent, [])
        # The <video> element streams with Range requests.
        whole = self.client.get(video["url"])
        self.assertEqual((whole.status_code, whole.mimetype, whole.data), (200, "video/mp4", MP4))
        part = self.client.get(video["url"], headers={"Range": "bytes=0-99"})
        self.assertEqual((part.status_code, len(part.data)), (206, 100))
        download = self.client.get(video["url"] + "?download=1")
        self.assertIn("attachment", download.headers["Content-Disposition"])
        # Caption edited after the generation: the message uses the one approved now.
        self.post(f"/studio/{sid}/caption", dict(description="Fecha com um clique e não vaza na bolsa",
                                                 hashtags=["#Pote", "#Cozinha"]))
        sent = self.post(f"/studio/{sid}/videos/{video['id']}/send")
        self.assertEqual(sent.status_code, 200, sent.json)
        self.assertEqual(sent.json["video"]["state"], "sent")
        method, fields = self.sent[-1]
        self.assertEqual(method, "sendVideo")
        self.assertIn("Fecha com um clique", fields["caption"])
        self.assertIn("https://", fields["caption"])
        self.assertTrue(sent.json["video"]["has_file"])  # kept for the preview and the download
        self.assertEqual(self.client.get(video["url"]).status_code, 200)
        self.assertEqual(self.post(f"/studio/{sid}/videos/{video['id']}/send").status_code, 400)  # never twice
        self.assertIn("já foi enviado", self.generate(sid).json["error"])

    def test_manual_videos_stay_out_of_the_daily_cap(self):
        self.connect_worker()
        self.store.save_settings(dict(video_daily_credits=15))
        sid = self.draft()
        self.assertEqual(self.generate(sid).status_code, 202)
        self.assertEqual(self.engine.videos.used_today(), 0)
        self.assertEqual(self.engine.videos.manual_credits()["today"], 15)
        self.assertEqual(self.client.get("/api/studio").json["manual_credits"]["month"], 15)
        self.assertEqual(self.client.get("/api/studio").json["manual"][0]["video"]["state"], "queued")

    def test_day_video_counts_in_the_cap_and_keeps_the_product_with_the_studio(self):
        self.connect_worker()
        self.store.save_settings(dict(video_daily_credits=30))
        sid = self.draft("dia")  # the automatic video of product 1:1 already reserved 15
        self.assertEqual(self.engine.videos.used_today(), 15)
        self.assertEqual(self.generate(sid).status_code, 202)
        self.assertEqual(self.engine.videos.used_today(), 30)
        self.assertEqual(self.store.deliveries(self.run_id)["3:3"]["state"], "pending")
        self.assertEqual(self.studio.waiting(), 1)  # 2:2 still waits for the user; 3:3 has a generation
        other = self.post("/studio/day", dict(run_id=self.run_id, key="2:2")).json
        for path, payload in (("facts", dict(text="- Tampa azul")), ("format", dict(format="pov"))):
            self.post(f"/studio/{other['id']}/{path}", payload)
        self.post(f"/studio/{other['id']}/scripts")
        self.post(f"/studio/{other['id']}/scripts/0", dict(choose=True))
        capped = self.generate(other["id"])
        self.assertEqual(capped.status_code, 400)
        self.assertIn("Teto diário", capped.json["error"])
        # The video goes to the preview; the delivery of the day stays pending until the user decides.
        self.render(self.claim())
        self.assertEqual(self.get(sid)["video"]["state"], "review")
        self.assertEqual(self.store.deliveries(self.run_id)["3:3"]["state"], "pending")
        self.studio.fallback(datetime.now(TZ) + timedelta(days=2))
        self.assertEqual(sum(v["product_key"] == "3:3" for v in self.videos()), 1)  # no automatic video for it
        sent = self.post(f"/studio/{sid}/videos/{self.get(sid)['video']['id']}/send").json
        self.assertEqual(sent["video"]["state"], "sent")
        delivery = self.store.deliveries(self.run_id)["3:3"]
        self.assertEqual(delivery["state"], "sent")
        frozen = json.loads(delivery["snapshot"])
        self.assertTrue(frozen["studio"] and frozen["links"])
        self.assertIn("Mantém os alimentos frescos", self.sent[-1][1]["caption"])  # caption of the Studio
        self.assertEqual(self.sent[-1][1]["caption"], frozen["caption"])

    def test_discard_and_generate_again(self):
        self.connect_worker()
        sid = self.draft("dia")
        self.generate(sid)
        self.render(self.claim())
        first = self.get(sid)["video"]
        path = Path(self.temp.name, "videos", f"video-{first['id']}.mp4")
        self.assertTrue(path.exists())
        self.assertIn("espera a sua decisão", self.generate(sid).json["error"])
        discarded = self.post(f"/studio/{sid}/videos/{first['id']}/discard").json
        self.assertEqual(discarded["video"]["state"], "discarded")
        self.assertFalse(path.exists())
        delivery = self.store.deliveries(self.run_id)["3:3"]
        self.assertEqual(delivery["state"], "failed")
        self.assertIn("descartado", delivery["error"])
        self.assertEqual(self.engine.videos.used_today(), 30)  # credits stay counted
        self.assertEqual(self.post(f"/studio/{sid}/videos/{first['id']}/discard").status_code, 400)
        again = self.generate(sid)
        self.assertEqual(again.status_code, 202, again.json)
        self.assertEqual([v["state"] for v in again.json["videos"]], ["queued", "discarded"])
        self.assertEqual(self.store.deliveries(self.run_id)["3:3"]["state"], "pending")

    def test_failure_before_the_charge_frees_a_new_try(self):
        self.connect_worker()
        sid = self.draft()
        self.generate(sid)
        task = self.claim()
        self.call(task, "result", ok=False, code="ui_changed", error="Caixa de comando não apareceu.", charged=False)
        video = self.get(sid)["video"]
        self.assertEqual((video["state"], video["credits"]), ("failed", 0))
        self.assertEqual(self.generate(sid).status_code, 202)

    def test_busy_browser_retries_the_same_approved_payload(self):
        self.connect_worker()
        sid = self.draft()
        self.generate(sid)
        task = self.claim()
        self.post(f"/studio/{sid}/scripts/0", dict(choose=True))  # the draft changes after the approval
        self.call(task, "result", ok=False, code="browser_busy", error="Perfil em uso.", charged=False)
        retry = self.claim()
        self.assertNotEqual(retry["id"], task["id"])
        self.assertEqual(retry["payload"], task["payload"])
        self.assertEqual(self.get(sid)["video"]["state"], "queued")

    def test_delete_waits_for_the_job_and_keeps_the_credits(self):
        self.connect_worker()
        sid = self.draft()
        self.generate(sid)
        refused = self.post(f"/studio/{sid}/delete")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("em andamento", refused.json["error"])
        self.render(self.claim())
        video_id = self.get(sid)["video"]["id"]
        self.assertEqual(self.post(f"/studio/{sid}/delete").status_code, 200)
        self.assertEqual([(v["id"], v["state"], v["credits"], v["file"]) for v in self.videos()],
                         [(video_id, "discarded", 15, None)])
        self.assertEqual(list(Path(self.temp.name, "videos").glob("*.mp4")), [])
        self.assertEqual(self.client.get(f"/api/studio/{sid}/videos/{video_id}").status_code, 400)

    def test_video_routes_require_login_and_the_right_draft(self):
        self.connect_worker()
        sid = self.draft()
        other = self.post("/studio/manual", dict(url=SHORT)).json["id"]
        self.generate(sid)
        self.render(self.claim())
        video_id = self.get(sid)["video"]["id"]
        self.assertEqual(self.client.get(f"/api/studio/{other}/videos/{video_id}").status_code, 400)
        self.assertEqual(self.post(f"/studio/{other}/videos/{video_id}/send").status_code, 400)
        self.client.post("/api/logout", json={}, headers=self.headers)
        self.assertEqual(self.client.get(f"/api/studio/{sid}/videos/{video_id}").status_code, 401)

    def test_fidelity_prompt_keeps_the_listing_wording(self):
        self.assertIn("A imagem P é a foto do anúncio.", fidelity_prompt("Pote", 3))
        self.assertIn("R1, R2 são fotos do produto", fidelity_prompt("Pote", 2, ["R1", "R2"]))

    def test_worker_names_each_reference_and_fetches_user_photos_from_the_panel(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
        import studio_worker
        opened, saved = [], []

        class Response:
            headers = type("H", (), {"get_content_type": lambda self: "image/jpeg"})()
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self, limit): return JPEG

        def urlopen(request, timeout):
            opened.append((request.full_url, request.get_header("Authorization"), request.get_header("X-worker-id")))
            return Response()

        def save_image(image, folder):
            saved.append(image)
            path = folder / (image["name"] + ".jpg")
            path.write_bytes(JPEG)
            return image["name"], path

        config = dict(panel_url="http://painel", token="tok")
        task = dict(id=217, payload=dict(images=[dict(photo="u3"), dict(url="https://down-br.img.susercontent.com/x")]))
        with tempfile.TemporaryDirectory() as folder, patch.object(studio_worker.urllib.request, "urlopen", urlopen), \
                patch.object(studio_worker, "save_image", save_image):
            paths = studio_worker.task_images(config, task, "vm.video", Path(folder))
            self.assertEqual([p.name for p in paths], ["p217r1.jpg", "p217r2.jpg"])  # unique per task: gallery anchor
            self.assertEqual(paths[0].read_bytes(), JPEG)
            self.assertEqual(opened, [("http://painel/api/worker/tasks/217/images/u3", "Bearer tok", "vm.video")])
            self.assertEqual(saved, [dict(name="p217r2", url="https://down-br.img.susercontent.com/x")])
            legacy = studio_worker.task_images(config, dict(id=9, payload=dict(image_url="https://x.susercontent.com/a")),
                                               "vm.video", Path(folder))
            self.assertEqual(legacy[0].name, "produto9.jpg")  # automatic video: same name as before


if __name__ == "__main__":
    unittest.main()
