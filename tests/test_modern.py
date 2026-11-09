import base64
import json
import os
import random
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import analysis  # noqa: E402
import modern  # noqa: E402
import subtitles  # noqa: E402
from app import create_app  # noqa: E402
from content import (FORMATS, modern_fallback_script, modern_script_problems, opening, pick_format,  # noqa: E402
                     spoken_numbers, video_prompt)
from core import (DEFAULTS, TZ, Engine, Store, fetch_modern, fetch_top, modern_query, shopee_query,  # noqa: E402
                  validate_settings)
from test_studio import FakeRunner, good_output, node  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=TZ).timestamp()
MODERN = dict(DEFAULTS, mode="modern")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
PROJECT = "https://flow.google.com/project/test-project"


WORDS = ["Organizador", "Luminária", "Escova", "Garrafa", "Mochila", "Tapete", "Ventilador", "Panela", "Cadeira",
         "Relógio", "Teclado", "Suporte", "Lâmpada", "Toalha", "Espelho"]
OPENERS = ["Esse", "Aquele", "Seu", "Nosso", "Todo", "Cada", "Meu", "Um", "Novo", "Lindo"]


def mnode(i=1, **overrides):
    """A listing that passes every Modern cut with the default settings."""
    value = node(i, productName=f"{WORDS[i % len(WORDS)]} premium casa {i}", price="100",
                 commission="7", appExistRate="0.07", ratingStar="4.8", sales=300, _position=150 + i,
                 _source="geral", sellerCommissionRate="0.02", priceDiscountRate=10,
                 periodStartTime=NOW - 60 * 86400, periodEndTime=0,
                 imageUrl=f"https://down-br.img.susercontent.com/file/{i}")
    value.update(overrides)
    return value


def modern_output(opener="Esse"):
    narration = (f"{opener} organizador deixa a cozinha em ordem num instante e tudo fica à mão na rotina. "
                 "Veja de perto e toque no produto marcado.")
    return dict(good_output(), narration=narration, hook="Close das mãos abrindo o produto",
                cta="Mãos apontam para o produto marcado")


def varied():
    """Model stub for analyses: a new opening on every call (the Modern check refuses repeated openings)."""
    calls = iter(OPENERS * 3)
    return lambda images: modern_output(next(calls))


class SettingsTest(unittest.TestCase):
    def test_legacy_is_the_default_and_its_query_is_untouched(self):
        self.assertEqual(DEFAULTS["mode"], "legacy")
        query = shopee_query(dict(DEFAULTS, keyword="pote"), 2)
        self.assertIn("sortType: 5", query)
        self.assertNotIn("sellerCommissionRate", query)
        self.assertNotIn("productCatId", query)

    def test_modern_query(self):
        query = modern_query(3, category="100715", keyword="piso")
        for part in ("listType: 0", "sortType: 2", "productCatId: 100715", 'keyword: "piso"', "page: 3", "limit: 50",
                     "sellerCommissionRate", "priceDiscountRate", "productCatIds"):
            self.assertIn(part, query)
        top = modern_query(1, list_type=2, sort_type=None, fields="itemId")
        self.assertIn("listType: 2", top)
        self.assertNotIn("sortType", top)

    def test_validation(self):
        result = validate_settings(dict(mode="modern", modern_categories=" 100715, 100719,100715 "))
        self.assertEqual(result["modern_categories"], "100715,100719")
        for bad in (dict(mode="turbo"), dict(modern_categories="abc"), dict(modern_categories="12"),
                    dict(modern_categories=",".join(str(100000 + i) for i in range(21))),
                    dict(modern_min_price=500.0, modern_max_price=100.0), dict(modern_min_sales=100, modern_max_sales=50),
                    dict(modern_min_discount=95), dict(modern_fidelity_min=11), dict(modern_pages=0),
                    dict(snapshot_time="25:00"), dict(modern_name_filter="x" * 201)):
            with self.assertRaises(ValueError, msg=bad):
                validate_settings(bad)
        self.assertEqual(validate_settings(dict(modern_max_sales=0, modern_min_sales=100))["modern_max_sales"], 0)


class RankingTest(unittest.TestCase):
    def rank(self, nodes, settings=None, **kwargs):
        return modern.rank_modern(nodes, dict(MODERN, **(settings or {})), now=NOW, **kwargs)

    def test_reference_example_from_the_plan(self):
        piso = mnode(1, productName="Kit 12 Piso Vinílico Autocolante Régua Madeira 91x15cm", price="109.90",
                     appExistRate="0.07", commission="7.69", ratingStar="4.9", sales=206, _position=118,
                     sellerCommissionRate="0.04", periodStartTime=NOW - 71 * 86400)
        today = datetime.fromtimestamp(NOW, TZ).date()
        history = {"1": [((today - timedelta(days=7)).isoformat(), 206 / 1.68 * 1.0)]}
        # growth = (206 - ref) / ref = 0.68 when ref = 206 / 1.68
        products, selected, warning = self.rank([piso], history=history)
        product = products[0]
        self.assertAlmostEqual(product["factors"]["trend"], 1.68, places=2)
        self.assertAlmostEqual(product["score"], 903.3, delta=1.5)
        self.assertEqual(product["competition"], "baixa")
        self.assertEqual(product["mode"], "modern")
        self.assertEqual(product["product_type"], "piso vinilico autocolante")
        self.assertEqual(selected, {"1:1"})
        self.assertIn("Tendência +68%/semana", "\n".join(product["reasons"]))
        self.assertIn("Funil do Modo Moderno: 1 anúncios lidos, 1 candidatos", warning)

    def test_every_cut_is_counted(self):
        nodes = [mnode(1, ratingStar="4.4"), mnode(2, sales=10), mnode(3, price="50"), mnode(4, price="2000"),
                 mnode(5, _position=10), mnode(6, sales=9000), mnode(7), mnode(8), mnode(9, offerLink=""),
                 mnode(10), mnode(11, productName=mnode(10)["productName"])]
        products, _, warning = self.rank(nodes, top_ids={"7"}, excluded={"8:8"})
        self.assertEqual([p["key"] for p in products], ["10:10"])
        for text in ("1 sem dados", "1 repetidos", "1 já enviados ou descartados", "1 nota baixa", "1 poucas vendas",
                     "2 fora da faixa de preço", "1 top de afiliados", "1 vitrine", "1 vendas acima do teto"):
            self.assertIn(text, warning)

    def test_discount_name_and_type_filters(self):
        nodes = [mnode(1, priceDiscountRate=30), mnode(2, priceDiscountRate=5), mnode(3, priceDiscountRate=None)]
        products, _, warning = self.rank(nodes, dict(modern_min_discount=25))
        self.assertEqual([p["key"] for p in products], ["1:1"])
        self.assertIn("2 desconto insuficiente", warning)
        nodes = [mnode(1, productName="Piso Vinílico Autocolante 91x15"), mnode(2, productName="Luminária de mesa LED")]
        products, _, _ = self.rank(nodes, dict(modern_name_filter="piso, VINIL"))
        self.assertEqual([p["key"] for p in products], ["1:1"])
        products, _, warning = self.rank(nodes, recent_types={"piso vinilico autocolante"})
        self.assertEqual([p["key"] for p in products], ["2:2"])
        self.assertIn("1 tipo de produto repetido", warning)

    def test_factors(self):
        base = self.rank([mnode(1)])[0][0]["factors"]
        self.assertEqual((base["seller"], base["showcase"], base["fresh"], base["trend"], base["price"]), (1.0, 1.0, 1.0, 1.0, 1.0))
        self.assertEqual(self.rank([mnode(1, sellerCommissionRate="0.05")])[0][0]["factors"]["seller"], 0.7)
        self.assertEqual(self.rank([mnode(1, sellerCommissionRate="5")])[0][0]["factors"]["seller"], 0.7)
        self.assertEqual(self.rank([mnode(1, sellerCommissionRate="4")])[0][0]["factors"]["seller"], 1.0)
        self.assertEqual(self.rank([mnode(1, _position=80)])[0][0]["factors"]["showcase"], 0.6)
        for days, factor in ((10, 1.4), (20, 1.15), (40, 1.0)):
            self.assertEqual(self.rank([mnode(1, periodStartTime=NOW - days * 86400)])[0][0]["factors"]["fresh"], factor)
        self.assertAlmostEqual(self.rank([mnode(1, price="800")])[0][0]["factors"]["price"], 0.5 ** 0.8, places=3)
        saturated = self.rank([mnode(1, sales=3000)])[0][0]
        self.assertEqual(saturated["factors"]["demand"], 750)
        self.assertEqual(saturated["competition"], "média")
        unknown = self.rank([mnode(1, sellerCommissionRate=None, periodStartTime=0)])[0][0]
        self.assertIn("Comissão extra do vendedor: dado indisponível", unknown["reasons"])
        self.assertEqual(unknown["factors"]["seller"], 1.0)

    def test_trend_limits_and_history_rules(self):
        today = date(2026, 11, 9)
        self.assertIsNone(modern.growth([], 100, today))
        self.assertIsNone(modern.growth([("2026-11-08", 50)], 100, today))  # one day is too recent
        self.assertAlmostEqual(modern.growth([("2026-11-02", 100), ("2026-11-06", 80)], 150, today), 0.5)
        self.assertAlmostEqual(modern.growth([("2026-11-05", 10)], 40, today), 30 / 30 * 7 / 4)
        hot = self.rank([mnode(1)], history={"1": [("2026-11-02", 30)]})[0][0]
        cold = self.rank([mnode(1)], history={"1": [("2026-11-02", 3000)]})[0][0]
        self.assertEqual((hot["factors"]["trend"], cold["factors"]["trend"]), (2.5, 0.5))

    def test_selection_rules(self):
        nodes = [mnode(1, shopId=9), mnode(2, shopId=9), mnode(3, shopId=9)]
        _, selected, warning = self.rank(nodes, dict(per_shop=2, top_count=5))
        self.assertEqual(len(selected), 2)
        self.assertIn("Selecionadas 2 de 5", warning)
        same = [mnode(1, productName="Piso vinílico autocolante cinza"), mnode(2, productName="Piso vinílico autocolante bege")]
        self.assertEqual(len(self.rank(same)[1]), 1)
        spread = [mnode(i, _source="A", sales=1000) for i in range(1, 5)] + [mnode(i, _source="B", sales=100) for i in range(5, 7)]
        _, selected, _ = self.rank(spread, dict(top_count=4))
        self.assertEqual(sum(key in selected for key in ("5:5", "6:6")), 2)  # fair share for the weaker category

    def test_potential_reorders(self):
        products, _, _ = self.rank([mnode(1, sales=1000), mnode(2, sales=500), mnode(3, sales=200)])
        modern.apply_potential(products, {"1:1": (4, "pouco a mostrar"), "2:2": (9, "demonstração clara")})
        self.assertEqual([p["key"] for p in products][:2], ["2:2", "1:1"])
        self.assertTrue(products[0]["potential_ok"])
        self.assertIn("Potencial de vídeo 9/10: demonstração clara", products[0]["reasons"])
        self.assertEqual(modern.select(products, dict(MODERN, top_count=1)), ["2:2"])

    def test_no_candidate_raises_with_the_funnel(self):
        with self.assertRaisesRegex(ValueError, "funil do Modo Moderno.*nota baixa"):
            self.rank([mnode(1, ratingStar="3")])


class FetchTest(unittest.TestCase):
    def test_positions_sources_and_partial_failure(self):
        calls = []

        def pages(settings, page, credentials, query=None):
            calls.append(query)
            if "100719" in query and page == 2:
                raise ValueError("Shopee retornou HTTP 500.")
            count = 2 if page < 3 else 1
            return dict(nodes=[dict(itemId=f"{page}{i}") for i in range(count)], pageInfo=dict(hasNextPage=page < 3))
        result = fetch_modern(dict(MODERN, modern_categories="100715,100719", modern_pages=5), dict(app_id="1", secret="s"),
                              page_fetcher=pages)
        first = [n for n in result["nodes"] if n["_source"] == "100715"]
        self.assertEqual([n["_position"] for n in first], [1, 2, 3, 4, 5])
        self.assertEqual(len([n for n in result["nodes"] if n["_source"] == "100719"]), 2)
        self.assertIn("Categoria 100719: página 2 falhou", result["warnings"][0])
        with self.assertRaisesRegex(ValueError, "credenciais"):
            fetch_modern(MODERN, dict(app_id="", secret=""))
        ids = fetch_top(dict(MODERN, modern_top_pages=2), dict(app_id="1", secret="s"),
                        page_fetcher=lambda *a, **k: dict(nodes=[dict(itemId=7), dict(itemId=8)], pageInfo=dict(hasNextPage=True)))
        self.assertEqual(ids, {"7", "8"})


class EngineModernTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sleeper = patch("core.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.store = Store(self.temp.name)
        self.store.save_settings(dict(mode="modern", flow_project_url=PROJECT))
        self.top_calls = 0

        def top(settings):
            self.top_calls += 1
            return {"6"}
        self.runner = FakeRunner(varied())
        self.engine = Engine(self.store, searcher=lambda *a, **k: dict(nodes=[], pageInfo={}), runner=self.runner,
                             modern_fetcher=lambda s: dict(nodes=[mnode(i, sales=300 + 10 * i) for i in range(1, 8)],
                                                           warnings=["aviso da coleta"]),
                             top_fetcher=top)
        self.engine.rng = random.Random(1)

    def test_collect_modern(self):
        run_id = self.engine.collect()
        run = self.store.run(run_id)
        selected = [p for p in run["products"] if p["selected"]]
        self.assertEqual(len(selected), 5)
        self.assertNotIn("6:6", [p["key"] for p in run["products"]])  # top affiliate
        self.assertTrue(all(p["mode"] == "modern" for p in selected))
        self.assertIn("aviso da coleta", run["warning"])
        self.assertIn("1 top de afiliados", run["warning"])
        self.assertEqual(run["settings"]["mode"], "modern")
        formats = [p["analysis"]["format"] for p in selected]
        self.assertTrue(all(f in FORMATS for f in formats))
        for index in range(1, len(formats)):  # never one of the previous three
            self.assertNotIn(formats[index], formats[max(0, index - 3):index])
        self.assertIn("REGRAS DO MODO MODERNO", self.runner.calls[0]["prompt"])
        self.assertIn("Somente mãos aparecem", selected[0]["analysis"]["prompt"])
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        self.assertEqual(self.store.sales_history(["1"])["1"], [(day, 310)])
        self.engine.collect()
        self.assertEqual(self.top_calls, 1)  # the top list is cached for the day

    def test_top_failure_is_a_warning(self):
        self.engine.top_fetcher = lambda s: (_ for _ in ()).throw(ValueError("HTTP 500"))
        run = self.store.run(self.engine.collect())
        self.assertIn("Top de afiliados indisponível", run["warning"])
        self.assertIn("6:6", [p["key"] for p in run["products"]])

    def test_legacy_mode_does_not_use_the_modern_path(self):
        self.store.save_settings(dict(mode="legacy"))
        self.engine.fetcher = lambda s: [node(i, imageUrl=f"https://down-br.img.susercontent.com/file/{i}") for i in range(1, 7)]
        self.engine.runner = FakeRunner()
        self.engine.modern_fetcher = lambda s: self.fail("modern fetcher used in Legacy mode")
        run = self.store.run(self.engine.collect())
        self.assertTrue(all("mode" not in p for p in run["products"]))
        self.assertNotIn("format", run["products"][0]["analysis"])
        self.assertNotIn("Somente mãos", run["products"][0]["analysis"]["prompt"])

    def test_video_potential(self):
        self.store.save_settings(dict(modern_video_potential=True, top_count=2))

        analyses = varied()

        def output(images):
            if images and images[0]["name"] == "C1":
                return dict(items=[dict(id="C1", score=3, reason="nada a mostrar"), dict(id="C2", score=9, reason="ótimo")])
            return analyses(images)
        self.engine.runner = FakeRunner(output)
        run = self.store.run(self.engine.collect())
        selected = [p for p in run["products"] if p["selected"]]
        self.assertEqual(selected[0]["potential"], 9)
        self.assertIn("Potencial de vídeo avaliado", run["warning"])

    def test_recent_types_block_the_same_type_next_week(self):
        run_id = self.engine.collect()
        with self.store.db() as db:
            db.execute("INSERT INTO deliveries(run_id,product_key,state,snapshot) VALUES(?,?,?,?)",
                       (run_id, "1:1", "sent", json.dumps(dict(name=mnode(1)["productName"], links=[], caption=""))))
        self.assertIn(modern.product_type(mnode(1)["productName"]), self.store.recent_types(7))
        self.assertEqual(self.store.recent_types(0), set())

    def test_sales_history_keeps_sixty_days(self):
        self.store.record_sales([dict(itemId=1, sales=10)], "2026-08-01")
        self.store.record_sales([dict(itemId=1, sales=20)], "2026-11-01")
        self.store.record_sales([dict(itemId=1, sales=25), dict(itemId=2)], "2026-11-01")
        self.assertEqual(self.store.sales_history([1, 2]), {"1": [("2026-11-01", 25)]})

    def test_daily_snapshot(self):
        nodes = [dict(itemId=1, sales=10)]
        calls = []
        self.engine.modern_fetcher = lambda s: calls.append(1) or dict(nodes=nodes, warnings=[])
        self.store.save_settings(dict(schedule_enabled=False))
        early = datetime(2026, 11, 9, 6, 0, tzinfo=TZ)
        self.engine.tick(early)
        self.assertEqual(calls, [])  # before 07:00
        self.engine.tick(early.replace(hour=8))
        self.assertEqual(calls, [])  # no Shopee credentials: nothing to read
        self.store.save_shopee(dict(app_id="123", secret="segredo-de-teste"))
        self.engine.tick(early.replace(hour=8))
        self.engine.tick(early.replace(hour=9))
        self.assertEqual(len(calls), 1)  # once a day
        self.assertEqual(self.store.sales_history(["1"])["1"], [("2026-11-09", 10)])
        self.engine.modern_fetcher = lambda s: (calls.append(1), (_ for _ in ()).throw(ValueError("HTTP 500")))[1]
        day = early.replace(day=10, hour=8)
        for minutes in (0, 10, 40, 80, 120, 160):
            self.engine.tick(day + timedelta(minutes=minutes))
        self.assertEqual(len(calls), 4)  # three attempts, 30 minutes apart, then give up for the day
        self.store.save_settings(dict(sales_snapshot=False))
        self.engine.tick(early.replace(day=11, hour=8))
        self.assertEqual(len(calls), 4)


class ScriptTest(unittest.TestCase):
    NAME = "Kit 12 Piso Vinílico Autocolante 91x15cm"

    def problems(self, narration, openings=(), **fields):
        script = dict(hook="Close das mãos", demo="Mãos aplicando", benefit="Resultado", cta="Mãos apontam", narration=narration)
        script.update(fields)
        return modern_script_problems(script, self.NAME, openings)

    def test_valid_narration(self):
        text = "Doze réguas de noventa e um por quinze transformam o piso em minutos, sem obra. Toque no produto marcado."
        self.assertEqual(self.problems(text), [])

    def test_rules(self):
        good = "Esse piso transforma a sala em minutos, sem obra nem sujeira, e fica lindo de verdade. Toque no produto marcado."
        self.assertEqual(self.problems(good), [])
        self.assertTrue(any("número falado" in p and "20" in p for p in
                            self.problems(good.replace("Esse piso", "Vinte réguas"))))
        self.assertTrue(any("número falado" in p and "3" in p for p in self.problems(good.replace("em minutos", "em 3 minutos"))))
        self.assertTrue(any("Olha" in p for p in self.problems("Olha " + good)))
        self.assertTrue(any("abertura" in p for p in self.problems(good, openings=["esse piso"])))
        self.assertTrue(any("produto marcado" in p for p in self.problems(good.replace("produto marcado", "link"))))
        self.assertTrue(any("palavras" in p for p in self.problems("Piso lindo. Toque no produto marcado.")))
        self.assertTrue(any("rosto" in p for p in self.problems(good, cta="Pessoa sorri para a câmera")))

    def test_numbers(self):
        self.assertEqual(spoken_numbers("vinte e cinco peças, dois mil e 1,5 litro"), {"25", "2000", "1.5"})
        self.assertEqual(spoken_numbers("um produto e uma caixa"), set())
        self.assertEqual(opening("Esse piso, gente!"), "esse piso")

    def test_format_choice_and_prompt(self):
        rng = random.Random(3)
        recent = ["uso_real", "unboxing", "antes_depois"]
        for _ in range(30):
            self.assertNotIn(pick_format(recent, rng), recent)
        prompt = video_prompt(dict(hook="a", demo="b", benefit="c", cta="d", narration="e"), True, "unboxing")
        self.assertIn("Formato: unboxing", prompt)
        self.assertIn("Somente mãos aparecem", prompt)
        self.assertNotIn("Somente mãos", video_prompt(dict(hook="a", demo="b", benefit="c", cta="d", narration="e")))

    def test_fallback_and_check_feedback(self):
        script = modern_fallback_script(dict(name=self.NAME))
        self.assertFalse(script["narration"].lower().startswith("olha"))
        self.assertIn("produto marcado", script["narration"])
        product = dict(name=self.NAME, image="", key="1:1", shop="", price=1, link="https://x")
        check = analysis.make_check([], product, dict(format="uso_real", openings=["conheca kit"]))
        self.assertTrue(check(dict(good_output(), narration="Olha só que prático: fecha com um clique. Toque no produto do vídeo.")))
        legacy = analysis.make_check([])
        self.assertEqual(legacy(good_output()), [])  # the Legacy narration ("Olha que prático…") stays valid


class VideoModernTest(unittest.TestCase):
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
        self.engine.modern_fetcher = lambda s: dict(nodes=[mnode(i) for i in range(1, 4)], warnings=[])
        self.engine.top_fetcher = lambda s: set()
        self.engine.searcher = lambda *a, **k: dict(nodes=[], pageInfo={})
        self.fidelity = dict(score=9, reason="mesmo produto")
        self.engine.runner = FakeRunner(lambda images: self.fidelity if any(i["name"] == "F1" for i in images) else modern_output())
        self.sent = []
        self.engine.sender = lambda url, body, headers=None: self.sent.append(url.rsplit("/", 1)[1]) or {"ok": True, "result": {"message_id": 7}}
        self.store.save_telegram(dict(token="123456:" + "A" * 30, chat_id="-1001234", thread_id=""))
        self.store.save_settings(dict(mode="modern", top_count=1, flow_project_url=PROJECT))
        self.worker = {"Authorization": f"Bearer {self.store.set_worker_token()}"}

    def claim(self):
        self.engine.send(self.engine.collect())
        return self.client.post("/api/worker/claim", json=dict(worker_id="vm.video", kinds=["video"]), headers=self.worker).json["task"]

    def post(self, task, path, worker="vm.video", **body):
        return self.client.post(f"/api/worker/tasks/{task['id']}/{path}", json=dict(worker_id=worker, **body), headers=self.worker)

    def finish(self, task, frames=True):
        if frames:
            self.assertEqual(self.post(task, "frames", frames=[base64.b64encode(JPEG).decode()] * 3).status_code, 200)
        self.client.post(f"/api/worker/tasks/{task['id']}/video", data=MP4,
                         headers={**self.worker, "X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"})
        with patch("video.threading.Thread") as thread:
            thread.side_effect = lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})()
            self.post(task, "result", ok=True, output=dict(width=1080, height=1920, duration=10.0, badge=True))
        with self.store.db() as db:
            return json.loads(db.execute("SELECT meta FROM videos ORDER BY id DESC").fetchone()[0])

    def test_modern_payload(self):
        task = self.claim()
        finish = task["payload"]["finish"]
        self.assertEqual(finish["size"], [1080, 1920])
        self.assertEqual(finish["badge"], dict(rating="4,8", sold="300 vendidos"))
        self.assertTrue(finish["frames"])

    def test_frames_validation(self):
        task = self.claim()
        good = base64.b64encode(JPEG).decode()
        self.assertEqual(self.post(task, "frames", worker="other.video", frames=[good]).status_code, 400)
        self.assertEqual(self.post(task, "frames", frames=[base64.b64encode(b"GIF89a").decode()]).status_code, 400)
        self.assertEqual(self.post(task, "frames", frames=[good] * 4).status_code, 400)
        self.assertEqual(self.post(task, "frames", frames=["não é base64"]).status_code, 400)
        big = base64.b64encode(b"\xff\xd8" + b"\x00" * 310 * 1024).decode()
        self.assertEqual(self.post(task, "frames", frames=[big]).status_code, 400)
        self.assertEqual(self.client.post(f"/api/worker/tasks/{task['id']}/frames", json=dict(worker_id="vm.video", frames=[good]),
                                          headers={}).status_code, 401)

    def test_high_fidelity_delivers_without_alert(self):
        meta = self.finish(self.claim())
        self.assertEqual(meta["fidelity"]["score"], 9)
        self.assertEqual(self.sent, ["sendVideo"])
        self.assertEqual(list(Path(self.temp.name, "videos").glob("*.jpg")), [])  # frames removed after the check

    def test_low_fidelity_still_delivers_and_alerts(self):
        self.fidelity = dict(score=2, reason="o vídeo mostra outro produto")
        meta = self.finish(self.claim())
        self.assertEqual(meta["fidelity"]["score"], 2)
        self.assertEqual(self.sent, ["sendVideo", "sendMessage"])

    def test_missing_frames_mean_not_rated_but_delivered(self):
        meta = self.finish(self.claim(), frames=False)
        self.assertIn("error", meta["fidelity"])
        self.assertEqual(self.sent, ["sendVideo"])

    def test_legacy_job_has_no_finish_and_no_fidelity(self):
        self.store.save_settings(dict(mode="legacy"))
        self.engine.fetcher = lambda s: [node(i, imageUrl=f"https://down-br.img.susercontent.com/file/{i}") for i in range(1, 4)]
        self.engine.runner = FakeRunner()
        task = self.claim()
        self.assertNotIn("finish", task["payload"])
        meta = self.finish(task, frames=False)
        self.assertNotIn("fidelity", meta)
        self.assertEqual(self.sent, ["sendVideo"])


class BadgeTest(unittest.TestCase):
    def test_badge_events_and_styles(self):
        events = subtitles.badge_events(10.0, "4,9", "206 {vendidos}")
        self.assertTrue(all(e.startswith("Dialogue: 1,0:00:06.50,0:00:10.00,") for e in events))
        self.assertIn("★ 4,9 • 206 (vendidos)", events[0])
        self.assertIn("CONFIRA NO PRODUTO MARCADO", events[1])
        self.assertIn("\\t(3000,3250,\\fscx106\\fscy106)", events[1])
        text = subtitles.ass([dict(start=0, end=1, text="Oi")], 1080, 1920, dict(duration=10.0, rating="4,9", sold="1 vendido"))
        self.assertIn("Style: Selo,", text)
        self.assertIn("Style: Botao,", text)
        self.assertNotIn("Style: Selo,", subtitles.ass([dict(start=0, end=1, text="Oi")], 720, 1280))

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "ffmpeg ausente")
    def test_real_render_scales_and_burns_the_badge(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "raw.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=24:duration=2",
                            "-f", "lavfi", "-i", "sine=duration=2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", str(raw)], check=True, timeout=60)
            meta = subtitles.subtitle_video(raw, "Toque no produto marcado.", Path(folder) / "final.mp4", folder,
                                            transcriber=lambda *a: [], finish=dict(size=[540, 960], badge=dict(rating="4,9", sold="2 vendidos")))
            self.assertEqual((meta["width"], meta["height"], meta["badge"], meta["audio"]), (540, 960, True, True))
            self.assertEqual(len(subtitles.extract_frames(raw, folder)), 3)


if __name__ == "__main__":
    unittest.main()
