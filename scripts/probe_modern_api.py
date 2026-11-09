#!/usr/bin/env python3
"""Probe the Shopee Affiliate Open API for what the Modern mode needs, without printing secrets or links.

    SHOPEE_APP_ID=... SHOPEE_SECRET=... python scripts/probe_modern_api.py [--category 100715]

Three one-page queries (10 listings each): the general list by sales, one category by sales and the top-affiliate
list (listType 2). For each one it prints whether the API accepted it and how many listings carry each field the
Modern mode reads. No offer link, shop name or credential is printed.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import fetch_product_page, modern_query, safe_error  # noqa: E402

FIELDS = ("sales", "ratingStar", "sellerCommissionRate", "priceDiscountRate", "productCatIds", "periodStartTime",
          "appExistRate", "commission")


def probe(label, query, credentials):
    print(f"\n== {label}")
    try:
        data = fetch_product_page(None, 1, credentials, query=query)
    except ValueError as error:
        print("   RECUSADA:", safe_error(error)[:300])
        return
    nodes = data["nodes"]
    print(f"   OK · {len(nodes)} anúncio(s) · próxima página: {(data.get('pageInfo') or {}).get('hasNextPage')}")
    for field in FIELDS:
        values = [n.get(field) for n in nodes if n.get(field) not in (None, "", [])]
        example = repr(values[0])[:60] if values else "-"
        print(f"   {field:22} {len(values):2}/{len(nodes)}  ex.: {example}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--category", default="100715", help="ID de categoria para testar productCatId")
    args = parser.parse_args()
    credentials = dict(app_id=os.environ.get("SHOPEE_APP_ID", ""), secret=os.environ.get("SHOPEE_SECRET", ""))
    if not credentials["app_id"] or not credentials["secret"]:
        print("Defina SHOPEE_APP_ID e SHOPEE_SECRET no ambiente (não são impressos).", file=sys.stderr)
        return 2
    probe("Lista geral por vendas (sortType 2)", modern_query(1, limit=10), credentials)
    probe(f"Categoria {args.category} por vendas (productCatId)", modern_query(1, limit=10, category=args.category), credentials)
    probe("Top de afiliados (listType 2)", modern_query(1, limit=10, list_type=2, sort_type=None), credentials)
    return 0


if __name__ == "__main__":
    sys.exit(main())
