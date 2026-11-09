"""Modern mode: sales-ordered collection funnel, anti-competition score, sales trend and product-type dedup.

Pure functions only: the panel (core.py) fetches pages, keeps the sales history and calls these. The Legacy mode
never touches this module."""
import math
import time
from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo

from content import MARKETING_WORDS, name_tokens, strip_accents

TZ = ZoneInfo("America/Sao_Paulo")
SATURATION = 1500           # sales above this stop adding demand (1,500 counts the same as 5,000)
SELLER_RATE_LIMIT = 0.05    # extra seller commission that pushes a listing up the affiliate showcase
SELLER_FACTOR = 0.7
SHOWCASE_ZONE = 100         # sales positions everyone sees; the first `modern_showcase_cut` are excluded
SHOWCASE_FACTOR = 0.6
FRESH = ((14, 1.4), (30, 1.15))
PRICE_PIVOT = 400.0
PRICE_EXPONENT = 0.8
TREND_LIMITS = (0.5, 2.5)
POTENTIAL_MIN = 7
# Reference only (labels in the interface); the funnel uses whatever IDs the user configures.
CATEGORY_HINT = {"100013": "Celulares e Dispositivos", "100644": "Computadores e Acessórios", "100535": "Áudio",
                 "100634": "Jogos e Consoles", "100635": "Câmeras e Drones",
                 "100715": "Casa e Construção · Ferramentas", "100719": "Casa e Construção · Iluminação",
                 "101189": "Casa e Construção · Pisos e Revestimentos", "101270": "Decoração de Natal",
                 "100722": "Casa e Construção · Festas"}
FUNNEL = {"sem_dados": "sem dados", "repetido": "repetidos", "ja_enviado": "já enviados ou descartados",
          "nota": "nota baixa", "vendas": "poucas vendas", "preco": "fora da faixa de preço",
          "desconto": "desconto insuficiente", "nome": "fora do filtro de nome", "top": "top de afiliados",
          "vitrine": "vitrine", "estourado": "vendas acima do teto", "tipo": "tipo de produto repetido"}
_MARKETING = {strip_accents(word).casefold() for word in MARKETING_WORDS}


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _present(value):
    return value is not None and str(value).strip() != ""


def rate(value):
    """Commission rates come as fractions (0.05); a value above 1 is read as a percentage (5 -> 0.05)."""
    value = _number(value)
    return value / 100 if value > 1 else value


def percent(value):
    """Discounts come as percentages (25); a fraction below 1 is read as 0.25 -> 25."""
    value = _number(value)
    return value * 100 if 0 < value < 1 else value


def br(value, decimals=2):
    return f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def money(value):
    return "R$ " + br(value)


def categories(settings):
    return [c for c in settings.get("modern_categories", "").split(",") if c]


def product_type(name):
    """First two meaningful words of the name, in alphabetical order ('piso vinilico'): two listings of the same
    type should not reach the audience in the same week. The order is ignored because sellers swap it
    ('Furadeira Parafusadeira' x 'Parafusadeira Furadeira', seen in the real data on 09/11/2026)."""
    words = [w for w in name_tokens(name) if len(w) >= 3 and not any(c.isdigit() for c in w) and w not in _MARKETING]
    return " ".join(sorted(words[:2])) if len(words) >= 2 else ""


def growth(points, sales, today):
    """Weekly sales growth against the point closest to 7 days ago (at least 2 days old); None without history.
    `points` = [(YYYY-MM-DD, sales)]."""
    best = None
    for day, value in points:
        age = (today - date.fromisoformat(day)).days
        if age < 2:
            continue
        if best is None or abs(age - 7) < abs(best[0] - 7):
            best = (age, value)
    if best is None:
        return None
    age, reference = best
    return (sales - reference) / max(reference, 30) * 7 / age


def funnel_text(read, counts, kept):
    cuts = ", ".join(f"{counts[k]} {label}" for k, label in FUNNEL.items() if counts.get(k))
    return f"Funil do Modo Moderno: {read} anúncios lidos, {kept} candidatos" + (f" (cortes: {cuts})." if cuts else ".")


def rank_modern(nodes, settings, excluded=None, top_ids=None, history=None, recent_types=None, now=None):
    """Return (candidates, selected keys, warning) with the same contract as core.rank_offers."""
    excluded, top_ids = excluded or set(), top_ids or set()
    history, recent_types = history or {}, recent_types or set()
    now = now or time.time()
    today = datetime.fromtimestamp(now, TZ).date()
    terms = [strip_accents(t).casefold().strip() for t in settings["modern_name_filter"].split(",") if t.strip()]
    counts, seen_keys, seen_names, candidates = Counter(), set(), set(), []
    for node in nodes:
        item = str(node.get("itemId") or "")
        key = f"{node.get('shopId', '')}:{item}"
        name = str(node.get("productName") or "Produto")
        start, end = _number(node.get("periodStartTime")), _number(node.get("periodEndTime"))
        price = _number(node.get("price")) or _number(node.get("priceMin"))
        existing = _number(node.get("appExistRate"))
        payout = price * existing if existing > 0 else _number(node.get("commission"))
        link = str(node.get("offerLink") or "")
        if (not item or not link.startswith("https://") or price <= 0 or payout <= 0
                or (start > 0 and start > now) or (end > 0 and end < now)):
            counts["sem_dados"] += 1
            continue
        normalized = " ".join(name_tokens(name))
        if key in seen_keys or (normalized and normalized in seen_names):
            counts["repetido"] += 1
            continue
        seen_keys.add(key)
        seen_names.add(normalized)
        rating, sales = _number(node.get("ratingStar")), max(0, int(_number(node.get("sales"))))
        discount = percent(node.get("priceDiscountRate"))
        position = int(_number(node.get("_position")))
        kind = product_type(name)
        reason = None
        if key in excluded:
            reason = "ja_enviado"
        elif rating < settings["modern_min_rating"]:
            reason = "nota"
        elif sales < settings["modern_min_sales"]:
            reason = "vendas"
        elif price < settings["modern_min_price"] or (settings["modern_max_price"] and price > settings["modern_max_price"]):
            reason = "preco"
        elif settings["modern_min_discount"] and discount < settings["modern_min_discount"]:
            reason = "desconto"
        elif terms and not any(t in strip_accents(name).casefold() for t in terms):
            reason = "nome"
        elif settings["modern_exclude_top"] and item in top_ids:
            reason = "top"
        elif position and position <= settings["modern_showcase_cut"]:
            reason = "vitrine"
        elif settings["modern_max_sales"] and sales > settings["modern_max_sales"]:
            reason = "estourado"
        elif kind and kind in recent_types:
            reason = "tipo"
        if reason:
            counts[reason] += 1
            continue
        candidates.append(_score(node, dict(
            key=key, item_id=item, shop_id=str(node.get("shopId", "")), name=name, shop=str(node.get("shopName") or ""),
            price=price, commission=payout, app_existing_rate=existing, app_new_rate=_number(node.get("appNewRate")),
            commission_basis="cliente existente" if existing > 0 else "comissão informada pela API",
            rating=rating, sales=sales, image=str(node.get("imageUrl") or "") if str(node.get("imageUrl") or "").startswith("https://") else "",
            link=link, expires=end if end > 0 else None, mode="modern", position=position or None,
            source=str(node.get("_source") or "geral"), discount=discount if _present(node.get("priceDiscountRate")) else None,
            product_type=kind), history.get(item, []), today, now))
    warning = funnel_text(len(nodes), counts, len(candidates))
    if not candidates:
        raise ValueError("Nenhuma oferta passou pelo funil do Modo Moderno. " + warning)
    candidates.sort(key=lambda p: (-p["score"], p["key"]))
    selected = select(candidates, settings)
    if len(selected) < settings["top_count"]:
        warning += f" Selecionadas {len(selected)} de {settings['top_count']}; loja, tipo e cota por categoria foram preservados."
    return candidates, set(selected), warning


def _score(node, product, points, today, now):
    sales, rating, price, payout = product["sales"], product["rating"], product["price"], product["commission"]
    position = product["position"] or 0
    demand = sales if sales <= SATURATION else SATURATION * SATURATION / sales
    f_rating = (rating / 5) ** 3
    f_commission = math.sqrt(max(payout, 0.5))
    seller = rate(node.get("sellerCommissionRate")) if _present(node.get("sellerCommissionRate")) else None
    f_seller = SELLER_FACTOR if seller is not None and seller >= SELLER_RATE_LIMIT else 1.0
    f_showcase = SHOWCASE_FACTOR if position and position <= SHOWCASE_ZONE else 1.0
    start = _number(node.get("periodStartTime"))
    days = max(0.0, (now - start) / 86400) if start > 0 else None
    f_fresh = next((factor for limit, factor in FRESH if days is not None and days <= limit), 1.0)
    weekly = growth(points, sales, today)
    f_trend = min(TREND_LIMITS[1], max(TREND_LIMITS[0], 1 + weekly)) if weekly is not None else 1.0
    f_price = 1.0 if price <= PRICE_PIVOT else (PRICE_PIVOT / price) ** PRICE_EXPONENT
    raw = demand * f_rating * f_commission * f_seller * f_showcase * f_fresh * f_trend * f_price
    low = sales <= SATURATION and position > SHOWCASE_ZONE and (seller or 0) < SELLER_RATE_LIMIT
    product.update(seller_rate=seller, offer_days=round(days, 1) if days is not None else None,
                   growth=round(weekly, 4) if weekly is not None else None, competition="baixa" if low else "média",
                   score=round(raw, 2), potential=None, potential_ok=True,
                   factors=dict(demand=round(demand, 2), rating=round(f_rating, 4), commission=round(f_commission, 4),
                                seller=f_seller, showcase=f_showcase, fresh=f_fresh, trend=round(f_trend, 4),
                                price=round(f_price, 4), raw_score=round(raw, 4)))
    where = f"categoria {product['source']}" if product["source"] != "geral" else "lista geral"
    reasons = [f"Modo Moderno · pontuação {br(raw)} · concorrência {product['competition']}",
               f"Vendas {sales} (30 dias, segundo a API)" + (f" → demanda limitada a {br(demand, 0)}" if sales > SATURATION else ""),
               f"Avaliação {br(rating, 1)}/5 → ×{br(f_rating, 2)}",
               f"Comissão estimada {money(payout)} ({product['commission_basis']}) → ×{br(f_commission, 2)}",
               ("Comissão extra do vendedor: dado indisponível" if seller is None else
                f"Comissão extra do vendedor {br(seller * 100, 1)}%" + (f" → ×{br(SELLER_FACTOR, 1)} (atrai afiliados)" if f_seller < 1 else "")),
               (f"Posição {position} por vendas na {where}" + (f" → ×{br(SHOWCASE_FACTOR, 1)} (perto da vitrine)" if f_showcase < 1 else "")
                if position else "Posição por vendas: dado indisponível"),
               ("Início da oferta: dado indisponível" if days is None else
                f"Oferta há {int(days)} dia(s)" + (f" → frescor ×{br(f_fresh, 2)}" if f_fresh > 1 else "")),
               ("Tendência: sem histórico de vendas suficiente (neutro)" if weekly is None else
                f"Tendência {'+' if weekly >= 0 else ''}{br(weekly * 100, 0)}%/semana → ×{br(f_trend, 2)}"),
               f"Preço {money(price)}" + (f" → ×{br(f_price, 2)} (ticket alto converte menos em vídeo curto)" if f_price < 1 else "")]
    if product["discount"] is not None:
        reasons.append(f"Desconto informado {br(product['discount'], 0)}%")
    product["reasons"] = reasons
    return product


def _order(product):
    return (not product.get("potential_ok", True), -product["score"], product["key"])


def select(candidates, settings):
    """Best first; at most `per_shop` per shop, never two of the same product type and, with several sources,
    a fair share per category before filling the rest."""
    ordered = sorted(candidates, key=_order)
    sources = list(dict.fromkeys(p["source"] for p in ordered))
    quota = math.ceil(settings["top_count"] / len(sources)) if len(sources) > 1 else settings["top_count"]
    selected, shops, kinds, per_source = [], Counter(), set(), Counter()
    for limited in (True, False):
        for product in ordered:
            if len(selected) == settings["top_count"]:
                return selected
            if (product["key"] in selected or shops[product["shop_id"]] >= settings["per_shop"]
                    or (product["product_type"] and product["product_type"] in kinds)
                    or (limited and per_source[product["source"]] >= quota)):
                continue
            selected.append(product["key"])
            shops[product["shop_id"]] += 1
            per_source[product["source"]] += 1
            if product["product_type"]:
                kinds.add(product["product_type"])
    return selected


def potential_pool(candidates, settings):
    return sorted(candidates, key=_order)[:min(12, max(6, 3 * settings["top_count"]))]


def apply_potential(candidates, verdicts):
    """Mix the score with the model's 0-10 video potential: final = score / best x potential / 10 (0-100 scale).
    Listings the model did not rate count as potential 5 and go after the rated ones with potential >= 7."""
    best = max(p["factors"]["raw_score"] for p in candidates) or 1
    for product in candidates:
        verdict = verdicts.get(product["key"])
        value = verdict[0] if verdict else None
        product["potential"] = value
        product["potential_ok"] = value is not None and value >= POTENTIAL_MIN
        product["score"] = round(product["factors"]["raw_score"] / best * (value if value is not None else 5) / 10 * 100, 2)
        line = (f"Potencial de vídeo {value}/10: {verdict[1]}" if verdict else "Potencial de vídeo: não avaliado (fora dos melhores)")
        product["reasons"] = [r for r in product["reasons"] if not r.startswith("Potencial de vídeo")] + [line]
    candidates.sort(key=_order)
    return candidates
