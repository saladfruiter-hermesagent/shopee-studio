"""Estúdio (PLANO_ESTUDIO.md, S1): reserva do dia, corte das 22h, aba Manual, fotos, ficha e roteiros."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import studio as studio_module
from app import create_app
from core import TZ, Store, fetch_offer_by_id, offer_query, validate_settings
from llm import ModelError
from test_studio import node

PROJECT = "https://flow.google.com/project/test-project"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4000
SHORT = "https://s.shopee.com.br/7VHrLRDZk9"
LONG = "https://shopee.com.br/opaanlp/822103155/58253826160?__mobile__=1"

SCRIPTS = [
    dict(angle="dor da bagunça", hook="Close das mãos fechando o pote com um clique",
         demo="Mãos enchem o pote e viram de cabeça para baixo", benefit="Nada vaza: o pote segue fechado e seco",
         cta="Mãos apontam para o pote convidando a tocar no produto marcado",
         narration="Cansado de bagunça na cozinha? Este pote fecha com um clique e mantém tudo fresco por mais tempo. "
                   "Toque no produto marcado."),
    dict(angle="vedação", hook="Mãos testam a tampa de um pote velho que não fecha",
         demo="Mãos trocam pelo pote novo e travam a tampa", benefit="A geladeira aparece organizada com os potes",
         cta="Mãos seguram o pote perto da câmera e apontam para o produto marcado",
         narration="Sabe aquele pote que nunca fecha direito? Este aqui veda com um clique e organiza a geladeira "
                   "inteira. Toque no produto marcado."),
    dict(angle="teste na bolsa", hook="Mãos colocam o pote com líquido dentro da bolsa",
         demo="Mãos chacoalham a bolsa com o pote dentro", benefit="A bolsa continua seca por dentro",
         cta="Mãos tiram o pote e apontam para o produto marcado",
         narration="Repara como a tampa trava sozinha e segura o líquido sem vazar na bolsa. Prático para o dia a dia. "
                   "Toque no produto marcado."),
]
SCRIPTS_OUTPUT = dict(description="Mantém os alimentos frescos e a geladeira organizada",
                      hashtags=["#PoteHermetico", "#CozinhaOrganizada"], scripts=SCRIPTS)
CALLOUTS = [["CHEGA DE POTE VAZANDO", "FECHA COM UM CLIQUE", "NÃO FIQUE SEM O SEU"],
            ["GELADEIRA BAGUNÇADA?", "TUDO NO LUGAR", "SUA COZINHA PEDE ISSO"],
            ["BOLSA MOLHADA DE NOVO?", "NADA VAZA", "SEM ELE NÃO DÁ"]]
# Estilo "problema": cada fala cabe em 14–19 palavras (o vídeo tem 10 s e o fim precisa de ~2 s de respiro).
PROBLEM_SCRIPTS = [
    dict(SCRIPTS[0], narration="Cansado de bagunça na cozinha? Este pote fecha com um clique e mantém tudo fresco. "
                              "Toque no produto marcado."),
    dict(SCRIPTS[1], narration="Sabe aquele pote que vaza? Este aqui veda com um clique e organiza a geladeira. "
                               "Toque no produto marcado."),
    dict(SCRIPTS[2], narration="Repara como a tampa trava e segura o líquido sem vazar na bolsa. "
                               "Toque no produto marcado."),
]
FACTS_OUTPUT = dict(facts=["Pote transparente com tampa azul", "Quatro travas laterais na tampa",
                           "Cabe na palma da mão", "Kit de potes (nome)"], avoid=["texto da embalagem"])


class StudioRunner:
    """Answers by schema; every answer must pass the real validation, like the real Runner requires."""
    def __init__(self):
        self.calls, self.error = [], None

    def run(self, prompt, schema, images, check, log=None, deadline=None):
        self.calls.append(dict(prompt=prompt, images=images))
        if self.error:
            raise self.error
        output = SCRIPTS_OUTPUT if "scripts" in schema["properties"] else FACTS_OUTPUT
        if "callouts" in schema["properties"].get("scripts", {}).get("items", {}).get("properties", {}):
            # Estilo "problema": fala mais curta (14–19 palavras) para sobrar respiro no fim e as 3 chamadas.
            output = dict(output, scripts=[dict(PROBLEM_SCRIPTS[i], callouts=CALLOUTS[i]) for i in range(len(SCRIPTS))])
        assert check(output) == [], check(output)
        return json.loads(json.dumps(output)), "Fake", ["Fake: ok"]


class LinkTest(unittest.TestCase):
    def test_product_ids_from_listing_urls(self):
        cases = {"https://shopee.com.br/Pote-Hermetico-i.822103155.58253826160?sp_atk=x": ("822103155", "58253826160"),
                 "https://shopee.com.br/product/12/34": ("12", "34"), LONG: ("822103155", "58253826160"),
                 "https://shopee.com.br/x?itemid=34&shopid=12": ("12", "34")}
        for url, ids in cases.items():
            self.assertEqual(studio_module.product_ids(url), ids, url)
        self.assertIsNone(studio_module.product_ids("https://shopee.com.br/"))

    def test_short_link_reads_only_the_redirect(self):
        seen = []
        def redirector(url):
            seen.append(url)
            return LONG if url == SHORT else None
        self.assertEqual(studio_module.resolve_link(f"Olha esse achado: {SHORT} !", redirector),
                         ("822103155", "58253826160"))
        self.assertEqual(seen, [SHORT])

    def test_rejects_links_outside_shopee(self):
        for text in ("https://evil.com/x-i.1.2", "https://shopee.com.br.evil.com/x-i.1.2", "sem link nenhum"):
            with self.assertRaises(ValueError):
                studio_module.resolve_link(text, lambda url: None)
        with self.assertRaises(ValueError):  # short link that goes nowhere useful
            studio_module.resolve_link(SHORT, lambda url: "https://evil.com/-i.1.2")

    def test_offer_by_id(self):
        query = offer_query(58253826160, 822103155)
        self.assertIn("itemId: 58253826160", query)
        self.assertIn("shopId: 822103155", query)
        fetched = []
        def fetcher(settings, page, credentials, query=None):
            fetched.append(query)
            return {"nodes": [node(7), node(8)], "pageInfo": {}}
        credentials = dict(app_id="1", secret="s")
        self.assertEqual(fetch_offer_by_id(credentials, 8, 8, fetcher)["itemId"], 8)
        self.assertIsNone(fetch_offer_by_id(credentials, 9, None, fetcher))
        self.assertNotIn("shopId:", fetched[-1])


class StudioTest(unittest.TestCase):
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
        # Product 3 scores best, then 2, then 1 (more sales).
        self.engine.fetcher = lambda _: [node(i, sales=100 * i, imageUrl=f"https://down-br.img.susercontent.com/file/{i}")
                                         for i in range(1, 4)]
        self.engine.searcher = lambda *a: []
        self.runner = StudioRunner()
        from test_studio import FakeRunner
        self.engine.runner = FakeRunner()
        self.engine.sender = lambda url, body, headers=None: {"ok": True, "result": {"message_id": 1}}
        self.store.save_telegram(dict(token="123456:" + "A" * 30, chat_id="-1001234", thread_id=""))
        self.store.save_settings(dict(top_count=3, per_shop=1, flow_project_url=PROJECT))
        self.studio = self.engine.studio
        self.studio.start_thread = lambda target: target()
        self.studio.redirector = lambda url: LONG if url == SHORT else None
        self.offers = {"58253826160": node(58253826160, shopId=822103155, productName="Kit 15 Telhas PVC 2,42x0,88m",
                                           imageUrl="https://down-br.img.susercontent.com/file/t")}
        self.engine.offer_fetcher = lambda item, shop=None: self.offers.get(str(item))
        self.headers = {"X-CSRF-Token": self.client.post("/api/login", json={"password": "test-password-only"}).json["csrf"]}

    def post(self, path, payload=None):
        return self.client.post("/api" + path, json=payload or {}, headers=self.headers)

    def send(self, slots):
        self.store.save_settings(dict(studio_slots=slots))
        run_id = self.engine.collect()
        self.engine.send(run_id)
        return run_id

    def videos(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM videos ORDER BY id")]

    def use_runner(self):
        self.engine.runner = self.runner

    # ---------- reserva e corte ----------
    def test_settings_defaults_and_limits(self):
        self.assertEqual((validate_settings({})["studio_slots"], validate_settings({})["studio_cutoff"]), (0, "22:00"))
        for payload in ({"studio_slots": 11}, {"studio_slots": -1}, {"studio_cutoff": "25:00"}, {"studio_cutoff": "22h"}):
            with self.assertRaises(ValueError):
                validate_settings(payload)

    def test_no_slots_keeps_send_unchanged(self):
        run_id = self.send(0)
        self.assertEqual([v["state"] for v in self.videos()], ["queued"] * 3)
        self.assertFalse(any(json.loads(d["snapshot"]).get("studio") for d in self.store.deliveries(run_id).values()))
        self.assertEqual(self.studio.waiting(), 0)

    def test_send_reserves_the_best_products_for_the_studio(self):
        run_id = self.send(2)
        run = self.store.run(run_id)
        selected = [p for p in run["products"] if p["selected"]]
        self.assertEqual([p["key"] for p in selected], ["3:3", "2:2", "1:1"])  # score order
        self.assertEqual([v["product_key"] for v in self.videos()], ["1:1"])  # only the third gets the automatic video
        deliveries = self.store.deliveries(run_id)
        for key in ("3:3", "2:2"):
            snapshot = json.loads(deliveries[key]["snapshot"])
            self.assertTrue(snapshot["studio"])
            self.assertEqual(deliveries[key]["state"], "pending")
            self.assertTrue(snapshot["caption"] and snapshot["links"])
        self.assertEqual(run["delivery"], "queued")
        self.assertEqual({p["key"] for p in selected} <= self.store.exclusions(), True)  # nobody picks them again
        self.assertEqual([bool(p["studio_deadline"]) for p in selected], [True, True, False])
        self.assertEqual(self.studio.waiting(), 2)
        self.assertEqual(self.client.get("/api/state").json["studio_waiting"], 2)
        today = self.client.get("/api/studio").json["today"]
        self.assertEqual([i["key"] for i in today], ["3:3", "2:2"])

    def test_deadline_is_today_or_tomorrow(self):
        morning = datetime(2026, 11, 10, 10, 0, tzinfo=TZ)
        night = datetime(2026, 11, 10, 23, 0, tzinfo=TZ)
        self.assertEqual(self.studio.deadline(morning), datetime(2026, 11, 10, 22, 0, tzinfo=TZ))
        self.assertEqual(self.studio.deadline(night), datetime(2026, 11, 11, 22, 0, tzinfo=TZ))

    def test_cutoff_without_generation_falls_back_to_the_automatic_video(self):
        run_id = self.send(2)
        deadline = datetime.fromisoformat(json.loads(self.store.deliveries(run_id)["3:3"]["snapshot"])["studio_deadline"])
        self.studio.fallback(deadline - timedelta(minutes=1))
        self.assertEqual(len(self.videos()), 1)
        self.studio.fallback(deadline + timedelta(minutes=1))
        videos = self.videos()
        self.assertEqual(sorted(v["product_key"] for v in videos), ["1:1", "2:2", "3:3"])
        self.assertTrue(all(v["state"] == "queued" for v in videos))
        fallen = [json.loads(v["snapshot"]) for v in videos if v["product_key"] != "1:1"]
        self.assertTrue(all(s["studio"] and s["studio_fallback"] and s["caption"] for s in fallen))
        self.studio.fallback(deadline + timedelta(hours=2))  # idempotent
        self.assertEqual(len(self.videos()), 3)
        self.assertEqual(self.studio.waiting(), 0)
        self.assertTrue(self.client.get("/api/studio").json["today"][0]["fallback"])

    def test_any_generation_keeps_the_product_with_the_studio(self):
        run_id = self.send(2)
        with self.store.db() as db:  # S2 will create this row when the user generates
            db.execute("INSERT INTO videos(run_id,product_key,day,state,credits) VALUES(?,?,?,?,?)",
                       (run_id, "3:3", "2026-11-10", "failed", 0))
        self.studio.fallback(datetime.now(TZ) + timedelta(days=2))
        self.assertEqual(sorted(v["product_key"] for v in self.videos()), ["1:1", "2:2", "3:3"])
        self.assertEqual(sum(v["product_key"] == "3:3" for v in self.videos()), 1)

    def test_fallback_respects_the_daily_cap(self):
        self.store.save_settings(dict(video_daily_credits=15))
        run_id = self.send(2)
        self.studio.fallback(datetime.now(TZ) + timedelta(days=2))
        deliveries = self.store.deliveries(run_id)
        self.assertEqual(deliveries["3:3"]["state"], "failed")
        self.assertIn("teto diário", deliveries["3:3"]["error"])

    # ---------- rascunhos ----------
    def test_day_draft_is_opened_once(self):
        run_id = self.send(2)
        first = self.post("/studio/day", dict(run_id=run_id, key="3:3"))
        self.assertEqual(first.status_code, 200)
        draft = first.json
        self.assertEqual((draft["origin"], draft["photos"][0]["source"]), ("dia", "api"))
        self.assertTrue(draft["reservation"]["deadline"])
        self.assertEqual(self.post("/studio/day", dict(run_id=run_id, key="3:3")).json["id"], draft["id"])
        refused = self.post("/studio/day", dict(run_id=run_id, key="1:1"))
        self.assertEqual(refused.status_code, 400)
        self.assertIn("não está reservado", refused.json["error"])
        self.assertEqual(self.client.get("/api/studio").json["today"][0]["session_id"], draft["id"])

    def test_manual_draft_from_short_link(self):
        self.store.flag("top_affiliates", dict(day=datetime.now(TZ).strftime("%Y-%m-%d"), pages=20, ids=["58253826160"]))
        result = self.post("/studio/manual", dict(url="Achei: " + SHORT))
        self.assertEqual(result.status_code, 200, result.json)
        draft = result.json
        self.assertEqual((draft["origin"], draft["key"]), ("manual", "822103155:58253826160"))
        self.assertTrue(draft["product"]["link"].startswith("https://"))
        self.assertTrue(any("top de afiliados" in w for w in draft["warnings"]))
        self.assertEqual(self.client.get("/api/studio").json["manual"][0]["id"], draft["id"])
        missing = self.post("/studio/manual", dict(url="https://shopee.com.br/x-i.1.999"))
        self.assertEqual(missing.status_code, 400)
        self.assertIn("fora do programa", missing.json["error"])
        self.assertEqual(self.post("/studio/manual", dict(url="https://evil.com/x-i.1.2")).status_code, 400)

    def test_manual_draft_warns_about_a_product_already_sent(self):
        run_id = self.send(0)
        self.offers["1"] = node(1, imageUrl="https://down-br.img.susercontent.com/file/1")
        draft = self.post("/studio/manual", dict(url="https://shopee.com.br/x-i.1.1")).json
        self.assertTrue(any("Já entregue" in w for w in draft["warnings"]), draft)
        self.assertTrue(run_id)

    def test_photos_upload_serve_and_limits(self):
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        path = f"/studio/{draft['id']}/photos"
        data = base64.b64encode(JPEG).decode()
        added = self.post(path, dict(data=data))
        self.assertEqual(added.status_code, 200)
        photo = added.json["photos"][-1]
        self.assertEqual((photo["id"], photo["source"], photo["main"]), ("u1", "user", False))
        served = self.client.get(photo["url"])
        self.assertEqual((served.status_code, served.mimetype, served.data), (200, "image/jpeg", JPEG))
        self.assertEqual(self.post(path, dict(data=base64.b64encode(b"GIF89a....").decode())).status_code, 400)
        self.assertEqual(self.post(path, dict(data="@@@")).status_code, 400)
        big = base64.b64encode(b"\xff\xd8" + b"\x00" * 2_600_000).decode()
        self.assertEqual(self.post(path, dict(data=big)).status_code, 400)
        huge = base64.b64encode(b"\xff\xd8" + b"\x00" * 3_200_000).decode()  # > 4 MB of JSON
        self.assertEqual(self.post(path, dict(data=huge)).status_code, 413)
        for _ in range(5):
            self.assertEqual(self.post(path, dict(data=data)).status_code, 200)
        seventh = self.post(path, dict(data=data))
        self.assertEqual(seventh.status_code, 400)
        self.assertIn("Até 6 fotos", seventh.json["error"])
        main = self.post(f"{path}/u2/main").json
        self.assertEqual([p["id"] for p in main["photos"] if p["main"]], ["u2"])
        removed = self.post(f"{path}/api/remove").json
        self.assertNotIn("api", [p["id"] for p in removed["photos"]])
        self.assertNotEqual(self.client.get(f"/api/studio/{draft['id']}/photos/..%2Fx").status_code, 200)
        self.assertEqual(self.client.get(f"/api/studio/{draft['id']}/photos/x1").status_code, 400)
        self.assertEqual(self.client.get(f"/api/studio/{draft['id']}/photos/u99").status_code, 400)

    def test_facts_use_every_photo(self):
        self.use_runner()
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        self.post(f"/studio/{draft['id']}/photos", dict(data=base64.b64encode(JPEG).decode()))
        started = self.post(f"/studio/{draft['id']}/facts")
        self.assertEqual(started.status_code, 202)
        result = self.client.get(f"/api/studio/{draft['id']}").json
        self.assertEqual(result["job"]["status"], "ok", result["job"])
        self.assertIn("- Pote transparente com tampa azul", result["facts"])
        self.assertIn("Cuidados para a IA de vídeo:", result["facts"])
        images = self.runner.calls[0]["images"]
        self.assertEqual([i["name"] for i in images], ["P", "F1"])
        self.assertEqual(base64.b64decode(images[1]["data"]), JPEG)
        self.assertIn("<dados>", self.runner.calls[0]["prompt"])
        edited = self.post(f"/studio/{draft['id']}/facts", dict(text="- Tampa azul\n- Quatro travas")).json
        self.assertEqual(edited["facts"], "- Tampa azul\n- Quatro travas")
        self.assertEqual(self.post(f"/studio/{draft['id']}/facts", dict(text="x" * 1501)).status_code, 400)

    def test_model_failure_keeps_the_draft(self):
        self.use_runner()
        self.runner.error = ModelError("Todos os modelos falharam: agy offline")
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        self.post(f"/studio/{draft['id']}/facts")
        result = self.client.get(f"/api/studio/{draft['id']}").json
        self.assertEqual(result["job"]["status"], "failed")
        self.assertIn("agy offline", result["job"]["message"])
        self.assertEqual(result["facts"], "")

    def test_running_job_is_not_started_twice_and_dies_with_a_restart(self):
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        self.studio.start_thread = lambda target: None  # the call stays "running"
        self.assertEqual(self.post(f"/studio/{draft['id']}/facts").status_code, 202)
        second = self.post(f"/studio/{draft['id']}/facts")
        self.assertEqual(second.status_code, 400)
        self.assertIn("em andamento", second.json["error"])
        Store(self.temp.name)  # panel restart
        job = self.client.get(f"/api/studio/{draft['id']}").json["job"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("reiniciou", job["message"])

    def test_three_scripts_choice_and_prompt(self):
        self.use_runner()
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        sid = draft["id"]
        refused = self.post(f"/studio/{sid}/scripts")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("formato", refused.json["error"])
        self.assertEqual(self.post(f"/studio/{sid}/format", dict(format="nada")).status_code, 400)
        self.post(f"/studio/{sid}/facts", dict(text="- Pote transparente com tampa azul\n- Quatro travas"))
        self.post(f"/studio/{sid}/format", dict(format="hands_on"))
        self.assertEqual(self.post(f"/studio/{sid}/scripts").status_code, 202)
        result = self.client.get(f"/api/studio/{sid}").json
        self.assertEqual(result["job"]["status"], "ok", result["job"])
        self.assertEqual(len(result["scripts"]), 3)
        self.assertTrue(all(s["problems"] == [] for s in result["scripts"]))
        self.assertEqual(result["caption"]["problems"], [])
        prompt = self.runner.calls[-1]["prompt"]
        self.assertIn("Pote transparente com tampa azul", prompt)
        self.assertIn("hands-on — close das mãos", prompt)
        self.assertIn("problema e solução, antes e depois", prompt)
        self.assertIn("voz em off", prompt)
        self.assertEqual(result["prompt"], "")
        chosen = self.post(f"/studio/{sid}/scripts/1", dict(choose=True)).json
        self.assertEqual(chosen["chosen"], 1)
        self.assertIn("Formato: hands-on", chosen["prompt"])
        self.assertIn("Narração em off", chosen["prompt"])
        self.assertIn("Somente mãos", chosen["prompt"])
        self.assertIn("Fatos do produto (não contradiga): Pote transparente com tampa azul; Quatro travas", chosen["prompt"])
        self.assertIn(SCRIPTS[1]["narration"], chosen["prompt"])
        self.assertEqual(self.post(f"/studio/{sid}/scripts/3", dict(choose=True)).status_code, 400)

    def test_edited_script_shows_problems_without_blocking(self):
        self.use_runner()
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        sid = draft["id"]
        self.post(f"/studio/{sid}/format", dict(format="pov"))
        self.post(f"/studio/{sid}/scripts")
        edited = self.post(f"/studio/{sid}/scripts/0", dict(script=dict(narration="Gente, que pote bom."),
                                                             choose=True)).json
        problems = edited["scripts"][0]["problems"]
        self.assertTrue(edited["scripts"][0]["edited"])
        self.assertTrue(any("Olha" in p for p in problems), problems)
        self.assertTrue(any("palavras" in p for p in problems), problems)
        self.assertIn("Gente, que pote bom.", edited["prompt"])
        self.assertEqual(self.post(f"/studio/{sid}/scripts/0", dict(script=dict(rosto="x"))).status_code, 400)
        caption = self.post(f"/studio/{sid}/caption", dict(description="Frete grátis hoje", hashtags=["#Shopee"])).json
        self.assertTrue(caption["caption"]["problems"])

    def test_scripts_check_rules(self):
        check = studio_module.scripts_check("Kit pote", "pov", ())
        self.assertEqual(check(SCRIPTS_OUTPUT), [])
        self.assertIn("scripts deve ter exatamente 3 roteiros", check(dict(SCRIPTS_OUTPUT, scripts=SCRIPTS[:2])))
        same = [dict(s, narration=SCRIPTS[0]["narration"]) for s in SCRIPTS]
        self.assertIn("os 3 roteiros precisam começar a narração de jeitos diferentes", check(dict(SCRIPTS_OUTPUT, scripts=same)))
        face = [dict(SCRIPTS[0], hook="Mulher sorrindo para a câmera com o pote")] + SCRIPTS[1:]
        self.assertTrue(any("só mãos" in p for p in check(dict(SCRIPTS_OUTPUT, scripts=face))))
        provador = studio_module.scripts_check("Kit pote", "provador", ())
        self.assertFalse(any("só mãos" in p for p in provador(dict(SCRIPTS_OUTPUT, scripts=face))))
        prompt = studio_module.studio_prompt(SCRIPTS[0], "provador", "", 1)
        self.assertNotIn("Somente mãos", prompt)
        self.assertNotIn("mesmo produto de ângulos diferentes", prompt)

    def test_formats_follow_the_studio_list(self):
        formats = self.client.get("/api/studio").json["formats"] 
        self.assertEqual([f["label"] for f in formats], ["POV", "Hands-on", "Selfie frontal", "Falando para a câmera",
                                                         "Provador", "Formato livre (UGC)"])
        rules = {f["key"]: (f["person"], f["speaks"]) for f in formats}
        self.assertEqual(rules, {"pov": (False, False), "hands_on": (False, False), "selfie": (True, True),
                                 "falando": (True, True), "provador": (True, False), "ugc": (True, True)})
        for key in ("uso_real", "teste_desafio"):  # os formatos do modo automático não valem aqui
            draft = self.post("/studio/manual", dict(url=SHORT)).json
            self.assertEqual(self.post(f"/studio/{draft['id']}/format", dict(format=key)).status_code, 400)
        ugc = studio_module.studio_prompt(SCRIPTS[0], "ugc", "", 2)
        self.assertTrue(ugc.startswith("Vídeo vertical 9:16 estilo UGC realista"), ugc)
        self.assertIn("A pessoa fala olhando para a câmera", ugc)
        self.assertNotIn("Somente mãos", ugc)
        self.assertIn("Narração em off", studio_module.studio_prompt(SCRIPTS[0], "provador", "", 1))
        facts = "- Tampa azul.\n- " + "x" * 690 + "\n- Quatro travas\nCuidados para a IA de vídeo:\n- Não mostrar encaixe por clique"
        prompt = studio_module.studio_prompt(SCRIPTS[0], "pov", facts, 1)
        self.assertIn("Fatos do produto (não contradiga): Tampa azul; Quatro travas\n", prompt)  # item longo fica fora inteiro
        self.assertIn("Cuidados: Não mostrar encaixe por clique\n", prompt)
        talking = studio_module.scripts_prompt(dict(name="Kit pote"), "", "falando", (), ["P"])
        self.assertIn("FALADA pela pessoa em cena", talking)
        self.assertIn("Descreva-a uma vez", talking)
        self.assertIn("Só mãos em cena", studio_module.scripts_prompt(dict(name="Kit pote"), "", "pov", (), ["P"]))

    def test_old_format_must_be_chosen_again(self):
        self.use_runner()
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        with self.store.db() as db:
            db.execute("UPDATE studio SET format='antes_depois' WHERE id=?", (draft["id"],))
        result = self.client.get(f"/api/studio/{draft['id']}").json
        self.assertEqual((result["format"], result["format_label"]), (None, ""))
        refused = self.post(f"/studio/{draft['id']}/scripts")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("formato", refused.json["error"])

    def test_delete_removes_the_photos(self):
        draft = self.post("/studio/manual", dict(url=SHORT)).json
        self.post(f"/studio/{draft['id']}/photos", dict(data=base64.b64encode(JPEG).decode()))
        folder = self.studio.directory / str(draft["id"])
        self.assertTrue(folder.exists())
        self.assertEqual(self.post(f"/studio/{draft['id']}/delete").status_code, 200)
        self.assertFalse(folder.exists())
        self.assertEqual(self.client.get(f"/api/studio/{draft['id']}").status_code, 400)

    def test_requires_login(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/studio").status_code, 401)
        self.assertEqual(anonymous.post("/api/studio/manual", json=dict(url=SHORT)).status_code, 401)


if __name__ == "__main__":
    unittest.main()
