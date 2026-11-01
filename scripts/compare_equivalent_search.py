#!/usr/bin/env python3
"""Compare the old equivalent search (6 tokens, 1 page, 8 candidates) with the progressive search.

Default: recorded fixtures, no network. Never touches Engine.collect, the production database, Telegram or Flow.

    python scripts/compare_equivalent_search.py                       # fixtures/run29.json
    SHOPEE_APP_ID=... SHOPEE_SECRET=... python scripts/compare_equivalent_search.py --live \
        --products produtos.json --record scripts/fixtures/run29.json  # real API, saves sanitized pages
    python scripts/compare_equivalent_search.py --evaluate            # + model rounds (agy on this VM)

Candidates listed here are NOT approved links: only the model decides "mesmo produto", and the results
still need a visual review.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analysis  # noqa: E402
from core import DEFAULTS, fetch_product_page  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "run29.json"
NODE_FIELDS = ("itemId", "shopId", "productName", "shopName", "imageUrl", "price", "priceMin", "ratingStar", "sales")
LIMITS = dict(queries=3, pages=2, calls=6)


def sanitize(node):
    """Public listing data only; the affiliate link is replaced by the plain product URL."""
    clean = {k: node.get(k) for k in NODE_FIELDS}
    clean["offerLink"] = f"https://shopee.com.br/product/{node.get('shopId')}/{node.get('itemId')}"
    return clean


class Recorded:
    """Search function backed by recorded pages; a page never recorded counts as empty."""

    def __init__(self, responses):
        self.responses = responses

    def __call__(self, keyword, page, deadline=None, stats=None):
        if stats is not None:
            stats["http_attempts"] = stats.get("http_attempts", 0) + 1
        data = self.responses.get(keyword, {}).get(str(page))
        return data or dict(nodes=[], pageInfo=dict(hasNextPage=False))


class Live:
    def __init__(self, record):
        self.credentials = dict(app_id=os.environ.get("SHOPEE_APP_ID", ""), secret=os.environ.get("SHOPEE_SECRET", ""))
        if not self.credentials["app_id"] or not self.credentials["secret"]:
            sys.exit("Defina SHOPEE_APP_ID e SHOPEE_SECRET no ambiente para o modo --live.")
        self.record = record

    def __call__(self, keyword, page, deadline=None, stats=None):
        data = fetch_product_page(dict(DEFAULTS, keyword=keyword, page_limit=50), page, self.credentials, sort_type=1,
                                  deadline=deadline, stats=stats)
        data = dict(nodes=[sanitize(n) for n in data["nodes"]], pageInfo=data.get("pageInfo") or {})
        self.record.setdefault(keyword, {})[str(page)] = data
        return data


def old_search(product, search):
    keyword = analysis.equivalence_keyword(product["name"])
    data = search(keyword, 1)
    eligible = analysis.select_candidates(data["nodes"], product, set(), 10 ** 6)
    return dict(keyword=keyword, results=len(data["nodes"]), eligible=len(eligible),
                candidates=[c["key"] for c in eligible[:8]])


def new_search(product, search, total=24, batch=8):
    pool = analysis.CandidatePool(product, set(), search, LIMITS)
    handed = []
    while len(handed) < total:
        chunk = pool.next_batch(min(batch, total - len(handed)))
        if not chunk:
            break
        handed += chunk
    report = pool.report()
    return dict(queries=[q["text"] for q in report["queries"]], calls=report["calls"], raw=report["raw"],
                unique=report["unique"], discarded=report["discarded"], eligible=report["eligible"],
                candidates=[c["key"] for c in handed], names=[c["name"][:90] for c in handed])


def evaluate(products, search):
    """Run Engine.analyze (content + rounds) in a throwaway store, with agy executed in-process."""
    sys.path.insert(0, str(ROOT / "worker"))
    import studio_worker
    from core import Engine, Store
    results = []
    with tempfile.TemporaryDirectory() as folder:
        store = Store(folder)
        store.worker_seen("compare", "local", ["llm"])
        stop = threading.Event()

        def heartbeat():  # Like the real worker: stays online while agy runs a long task.
            while not stop.is_set():
                store.worker_touch("compare")
                stop.wait(10)

        def worker():
            while not stop.is_set():
                task = store.claim_task("compare", ["llm"])
                if not task:
                    time.sleep(0.5)
                    continue
                try:
                    output = studio_worker.run_llm(task["payload"], task["seconds_left"])
                    store.finish_task(task["id"], "compare", True, dict(output=output))
                except Exception as error:
                    store.finish_task(task["id"], "compare", False, error=str(error))

        for target in (heartbeat, worker):
            threading.Thread(target=target, daemon=True).start()
        engine = Engine(store, fetcher=lambda _: [], searcher=search)
        used = {p["key"] for p in products}
        try:
            for product in products:
                started = time.time()
                result = engine.analyze(dict(product), store.settings(), set(used))
                used.update(link["key"] for link in result["links"])
                verdicts = {}
                for candidate in result["equivalents"]:
                    verdicts[candidate["verdict"]] = verdicts.get(candidate["verdict"], 0) + 1
                results.append(dict(key=product["key"], name=product["name"], status=result["status"],
                                    links=len(result["links"]), verdicts=verdicts,
                                    rounds=len(result["search"]["rounds"]), stopped=result["search"]["stopped"],
                                    calls=result["search"]["calls"], seconds=round(time.time() - started, 1),
                                    round_details=result["search"]["rounds"], notes=result["notes"],
                                    attempts=result["attempts"],
                                    matches=[dict(key=c["key"], name=c["name"], reason=c["reason"])
                                             for c in result["equivalents"] if c["verdict"] == "match"],
                                    reviewed=[dict(key=c["key"], name=c["name"][:120], verdict=c["verdict"],
                                                   reason=c["reason"], round=c.get("round"))
                                              for c in result["equivalents"]]))
                print(f"  {product['name'][:60]}: {verdicts} em {results[-1]['seconds']} s", file=sys.stderr)
        finally:
            stop.set()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--live", action="store_true", help="consulta a API real da Shopee")
    parser.add_argument("--products", type=Path, help="JSON com os produtos (modo --live)")
    parser.add_argument("--record", type=Path, help="grava as páginas reais, sem credenciais, neste arquivo")
    parser.add_argument("--evaluate", action="store_true", help="roda também as rodadas do modelo (agy local)")
    parser.add_argument("--output", type=Path, help="salva o relatório JSON neste arquivo")
    parser.add_argument("--only", help="só os produtos cujo nome contém este texto")
    args = parser.parse_args()

    if args.live:
        if not args.products:
            sys.exit("Informe --products no modo --live.")
        products, responses = json.loads(args.products.read_text(encoding="utf-8")), {}
        search = Live(responses)
    else:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        products, responses = fixture["products"], fixture["responses"]
        search = Recorded(responses)

    if args.only:
        products = [p for p in products if args.only.casefold() in p["name"].casefold()]
    report = []
    for product in products:
        old, new = old_search(product, search), new_search(product, search)
        report.append(dict(key=product["key"], name=product["name"], old=old, new=new,
                           new_only=len(set(new["candidates"]) - set(old["candidates"]))))
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(dict(products=products, responses=responses), ensure_ascii=False, indent=1),
                               encoding="utf-8")
    result = dict(mode="live" if args.live else "fixture", note="Candidatos não são links aprovados.", products=report)
    if args.evaluate:
        result["evaluation"] = evaluate(products, Recorded(responses) if not args.live else search)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    for entry in report:
        print(f"{entry['name'][:70]}\n  antes: '{entry['old']['keyword']}' → {entry['old']['results']} resultados, "
              f"{entry['old']['eligible']} elegíveis, {len(entry['old']['candidates'])} ao modelo\n"
              f"  agora: {len(entry['new']['queries'])} consulta(s), {entry['new']['calls']} página(s), "
              f"{entry['new']['raw']} resultados, {entry['new']['unique']} únicos, {entry['new']['eligible']} elegíveis, "
              f"{len(entry['new']['candidates'])} ao modelo ({entry['new_only']} fora da busca antiga)")
    for entry in result.get("evaluation", []):
        print(f"[modelo] {entry['name'][:60]}: {entry['links']} link(s), {entry['verdicts']}, {entry['rounds']} rodada(s), "
              f"{entry['stopped']}, {entry['seconds']} s")


if __name__ == "__main__":
    main()
