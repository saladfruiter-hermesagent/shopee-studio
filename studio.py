"""Estúdio: rascunhos de vídeo assistidos pelo usuário (PLANO_ESTUDIO.md, etapa S1).

O usuário escolhe um produto (um dos reservados da consulta do dia ou qualquer anúncio colado pelo link), manda
fotos pelo celular, revisa a ficha visual escrita pelo modelo, escolhe o formato e um entre três roteiros. A geração
no Flow (S2) ainda não está aqui. Produto reservado sem nenhuma geração até o horário de corte cai para o vídeo
automático (`fallback`)."""
import base64
import binascii
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from analysis import DATA_RULES
from content import (CAPTION_LIMIT, CLOSING, FORMATS, MODERN_NARRATION_WORDS, SCRIPT_FIELD_LIMIT, VIDEO_SECONDS,
                     caption_text, copy_problems, modern_script_problems, opening, script_problems, text_length)

MAX_PHOTOS = 6                 # fotos do usuário por produto, além da foto da API
MAX_PHOTO_BYTES = 2_500_000    # depois de reduzida no navegador (~1280 px)
FACTS_LIMIT = 1500
NARRATION_LIMIT = 300
ANGLE_LIMIT = 60
FACTS_SECONDS, SCRIPTS_SECONDS = 300, 480
SHORT_HOSTS = ("s.shopee.com.br", "shp.ee", "shope.ee")
STUDIO_FORMATS = {**FORMATS, "provador": "provador — o produto no corpo de uma pessoa (vestindo, calçando ou usando), "
                                         "mostrando caimento e tamanho real."}
PERSON_FORMATS = {"provador"}  # formatos em que aparece uma pessoa: a regra de só mãos não vale
SCRIPT_FIELDS = ("hook", "demo", "benefit", "cta", "narration")
PHOTO_ID = re.compile(r"u\d{1,3}")

FACTS_SCHEMA = {"type": "object", "properties": {
    "facts": {"type": "array", "items": {"type": "string"}},
    "avoid": {"type": "array", "items": {"type": "string"}}}, "required": ["facts", "avoid"]}
SCRIPT_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in ("angle", *SCRIPT_FIELDS)},
                 "required": ["angle", *SCRIPT_FIELDS]}
SCRIPTS_SCHEMA = {"type": "object", "properties": {
    "description": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
    "scripts": {"type": "array", "items": SCRIPT_SCHEMA}}, "required": ["description", "hashtags", "scripts"]}


def _num(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def format_label(key):
    return STUDIO_FORMATS[key].split(" — ")[0]


# ---------- link → produto ----------
def find_url(text):
    match = re.search(r"https?://\S+", str(text or ""))
    if not match:
        raise ValueError("Cole o link do produto na Shopee (começa com https://).")
    return match.group(0).rstrip(").,;!>\"'")


def _shopee_host(host):
    host = (host or "").lower()
    return host == "shopee.com.br" or host.endswith(".shopee.com.br") or host in SHORT_HOSTS


def product_ids(url):
    """(shop_id, item_id) of a Shopee Brasil listing URL, or None when the URL does not carry them."""
    parsed = urlparse(url)
    match = (re.search(r"-i\.(\d{1,15})\.(\d{1,15})", parsed.path)
             or re.search(r"/(?:product|opaanlp)/(\d{1,15})/(\d{1,15})", parsed.path))
    if match:
        return match.group(1), match.group(2)
    query = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
    shop, item = (query.get("shopid") or [""])[0], (query.get("itemid") or [""])[0]
    if shop.isdigit() and item.isdigit():
        return shop, item
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_redirect(url, timeout=15):
    """Destination of a short link, read from the Location header only: the page itself is never opened,
    so no login or captcha is involved (S0: s.shopee.com.br answers 301)."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ShopeeStudio)"})
    try:
        with opener.open(request, timeout=timeout):
            return None
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 303, 307, 308):
            return error.headers.get("Location")
        raise ValueError(f"A Shopee respondeu HTTP {error.code} ao abrir o link curto.") from None
    except OSError:
        raise ValueError("Não consegui abrir o link curto da Shopee (rede ou tempo esgotado).") from None


def resolve_link(text, redirector=read_redirect):
    url = find_url(text)
    for _ in range(4):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not _shopee_host(parsed.hostname):
            raise ValueError("Link fora da Shopee Brasil: use o link do anúncio em shopee.com.br ou o link curto.")
        ids = product_ids(url)
        if ids:
            return ids
        if parsed.scheme != "https":
            break
        location = redirector(url)
        if not location:
            break
        url = urljoin(url, location)
    raise ValueError("Não encontrei o produto nesse link. Use o link do anúncio ou o link curto da Shopee.")


def product_from_node(node):
    """Same fields as a ranked offer, without the selection filters (the user chose this listing)."""
    price = _num(node.get("price")) or _num(node.get("priceMin"))
    rate = _num(node.get("appExistRate"))
    shop, item = str(node.get("shopId") or ""), str(node.get("itemId") or "")
    image = str(node.get("imageUrl") or "")
    link = str(node.get("offerLink") or "")
    end = _num(node.get("periodEndTime"))
    return dict(key=f"{shop}:{item}", item_id=item, shop_id=shop, name=str(node.get("productName") or "Produto"),
                shop=str(node.get("shopName") or ""), price=price,
                commission=price * rate if rate > 0 else _num(node.get("commission")),
                rating=_num(node.get("ratingStar")), sales=int(max(0, _num(node.get("sales")))),
                image=image if image.startswith("https://") else "", link=link if link.startswith("https://") else "",
                mode="modern", expires=end if end > 0 else None)


# ---------- prompts ----------
def _data(product):
    return (f"<dados>\nAnúncio: {product['name'][:300]} | loja: {product.get('shop', '')[:80]} | "
            f"preço: R$ {product.get('price', 0):.2f}\n</dados>")


def facts_prompt(product, names):
    refs = ", ".join(names)
    return f"""Você descreve um produto para um roteirista de vídeo curto de venda.
{DATA_RULES}

{_data(product)}

Imagens: {refs}. P (se houver) é a foto do anúncio; F1, F2... são fotos do produto feitas pelo afiliado.
"facts": de 4 a 12 frases curtas só com o que está VISÍVEL nas fotos ou escrito no nome do anúncio: formato, cor,
material aparente, tamanho aparente (compare com mãos ou objetos ao lado), partes e componentes, como se usa (se a foto
mostrar) e texto legível da embalagem. Termine com " (nome)" a frase que veio só do nome do anúncio. Não invente função,
medida, resultado ou qualidade que não dá para ver.
"avoid": até 4 detalhes que uma IA de vídeo tende a errar neste produto (ex.: texto da embalagem, transparência,
encaixes, quantidade de peças). Responda somente com o JSON do schema."""


def facts_check(output):
    if not isinstance(output, dict):
        return ["resposta não é um objeto JSON"]
    facts, avoid = output.get("facts"), output.get("avoid")
    if not isinstance(facts, list) or not all(isinstance(f, str) and f.strip() for f in facts):
        return ["facts deve ser uma lista de frases"]
    if not 3 <= len(facts) <= 12:
        return [f"facts com {len(facts)} frases; use de 4 a 12"]
    if not isinstance(avoid, list) or not all(isinstance(a, str) for a in avoid) or len(avoid) > 5:
        return ["avoid deve ser uma lista de até 4 frases"]
    if any(len(f) > 220 for f in facts + avoid):
        return ["cada frase com até 200 caracteres"]
    return []


def facts_text(output):
    lines = ["- " + f.strip() for f in output["facts"]]
    avoid = [a.strip() for a in output["avoid"] if a.strip()]
    if avoid:
        lines += ["Cuidados para a IA de vídeo:"] + ["- " + a for a in avoid]
    text = ""
    for line in lines:  # whole lines only, never a cut sentence
        if len(text) + len(line) + 1 > FACTS_LIMIT:
            break
        text += ("\n" if text else "") + line
    return text


def scripts_prompt(product, facts, video_format, banned, names):
    low, high = MODERN_NARRATION_WORDS
    person = video_format in PERSON_FORMATS
    banned_text = ", ".join(f'"{o}"' for o in banned) or "nenhuma"
    people = ("Pode aparecer uma pessoa usando o produto; o rosto não precisa aparecer." if person else
              "Só mãos em cena (visão em primeira pessoa ou close): nenhum rosto, sorriso ou pessoa de frente para a câmera.")
    return f"""Você escreve roteiros de vídeos curtos de venda para a Shopee Vídeo ({VIDEO_SECONDS} s, vertical 9:16).
O vídeo é feito por uma IA que recebe as fotos do produto como referência ({", ".join(names)}).
{DATA_RULES}

{_data(product)}

FICHA DO PRODUTO (conferida pelo afiliado; é a única fonte de atributos — não cite nada que não esteja nela, no nome
ou nas fotos):
<ficha>
{facts.strip() or "(sem ficha: use só o nome do anúncio e as fotos)"}
</ficha>

FORMATO OBRIGATÓRIO: {STUDIO_FORMATS[video_format]}

Escreva 3 roteiros com ângulos DIFERENTES entre si (ex.: a dor do dia a dia, o resultado, a curiosidade de como funciona).
Cada roteiro tem:
- "angle": o ângulo em até 6 palavras.
- "hook" (0–2 s): a primeira imagem tem de prender sozinha — o produto ou o problema aparece já no primeiro segundo.
- "demo" (2–6 s): uma ação concreta e observável com o produto.
- "benefit" (6–8 s): a PROVA — o resultado visível que mostra o benefício.
- "cta" (8–10 s): gesto ou cena que convida a tocar no produto marcado.
  Cenas filmáveis, em português, até {SCRIPT_FIELD_LIMIT} caracteres cada; sem textos, setas ou elementos gráficos na tela.
- "narration": a fala completa, PT-BR natural, de {low} a {high} palavras. A primeira frase é o gancho falado (pergunta ou
  afirmação forte); a última frase cita "{CLOSING}" (ex.: "Toque no {CLOSING}."). Não comece com "Olha" nem "Gente", nem com
  estas aberturas já usadas: {banned_text}. Os 3 roteiros começam de jeitos diferentes. Todo número falado tem de
  aparecer no nome do anúncio; na dúvida, não fale números. Sem preço, desconto, frete, estoque ou urgência.
- {people}

LEGENDA (uma só, vale para qualquer roteiro): "description" = uma frase com um benefício verificável e o contexto de uso,
sem emojis, sem preço, desconto, frete ou urgência; "hashtags" = 2 ou 3 (produto, uso/contexto). Proibido #Shopee,
#Achadinhos, #Viral, #Fyp. description + espaço + hashtags com no máximo {CAPTION_LIMIT} caracteres.
Responda somente com o JSON do schema."""


def script_issues(script, name, video_format, banned=()):
    """Problems of one script under the Studio rules (the same as the Modern mode, faces allowed in 'provador')."""
    problems = script_problems(script)
    problems += modern_script_problems(script, name, banned, hands_only=video_format not in PERSON_FORMATS)
    angle = script.get("angle")
    if not isinstance(angle, str) or not angle.strip() or len(angle) > ANGLE_LIMIT:
        problems.append(f"angle vazio ou com mais de {ANGLE_LIMIT} caracteres")
    return problems


def scripts_check(name, video_format, banned):
    def check(output):
        if not isinstance(output, dict):
            return ["resposta não é um objeto JSON"]
        problems = copy_problems(output.get("description"), output.get("hashtags"))
        scripts = output.get("scripts")
        if not isinstance(scripts, list) or len(scripts) != 3 or not all(isinstance(s, dict) for s in scripts):
            return problems + ["scripts deve ter exatamente 3 roteiros"]
        for index, script in enumerate(scripts, 1):
            problems += [f"roteiro {index}: {p}" for p in script_issues(script, name, video_format, banned)]
        openings = [opening(s.get("narration") or "") for s in scripts]
        if len(set(openings)) < len(openings):
            problems.append("os 3 roteiros precisam começar a narração de jeitos diferentes")
        return problems
    return check


def studio_prompt(script, video_format, facts, references):
    """Flow prompt of a Studio draft (shown in S1, sent in S2)."""
    facts_line = "; ".join(line[2:].strip() for line in facts.splitlines() if line.startswith("- "))[:900]
    lines = [f"Vídeo publicitário vertical 9:16 de {VIDEO_SECONDS} segundos do produto das imagens de referência.",
             ("As imagens anexadas mostram o mesmo produto de ângulos diferentes. " if references > 1 else "")
             + "Preserve exatamente formato, cores, proporções e componentes do produto. Não acrescente acessórios.",
             f"Formato: {STUDIO_FORMATS[video_format]}"]
    if video_format not in PERSON_FORMATS:
        lines.append("Somente mãos aparecem em cena: nenhum rosto e nenhuma pessoa de frente para a câmera.")
    if facts_line:
        lines.append(f"Fatos do produto (não contradiga): {facts_line}")
    lines += [f"0–2 s, gancho: {script['hook'].strip()}", f"2–6 s, demonstração: {script['demo'].strip()}",
              f"6–8 s, prova: {script['benefit'].strip()}", f"8–10 s, chamada: {script['cta'].strip()}",
              f"Narração em português do Brasil, voz natural e animada: \"{script['narration'].strip()}\"",
              "Sem textos, legendas, logotipos ou marcas d'água na imagem. Sem preços, descontos ou promessas."]
    return "\n".join(lines)


class Studio:
    def __init__(self, engine, tz):
        self.engine, self.store, self.tz = engine, engine.store, tz
        self.directory = self.store.path.parent / "studio"
        self.directory.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.redirector = read_redirect
        self.start_thread = lambda target: threading.Thread(target=target, daemon=True).start()

    # ---------- reserva do dia e corte ----------
    def deadline(self, now):
        """Cutoff of the reservation: today's cutoff, or tomorrow's when the send happens after it."""
        hour, minute = map(int, self.store.settings()["studio_cutoff"].split(":"))
        cut = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return cut if now < cut else cut + timedelta(days=1)

    def _reservations(self, pending_only=False):
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute(
                """SELECT d.run_id,d.product_key,d.state,d.error,d.snapshot,o.body FROM deliveries d
                   JOIN offers o ON o.run_id=d.run_id AND o.product_key=d.product_key
                   WHERE d.snapshot LIKE '%"studio": true%'""" + (" AND d.state='pending'" if pending_only else "")
                + " ORDER BY d.run_id DESC, d.rowid LIMIT 200")]
        result = []
        for row in rows:
            snapshot = json.loads(row["snapshot"])
            if snapshot.get("studio"):
                result.append(dict(row, snapshot=snapshot, product=json.loads(row["body"])))
        return result

    def fallback(self, now):
        """Reserved products with no generation of any kind by the cutoff go to the automatic video (E10)."""
        for row in self._reservations(pending_only=True):
            snapshot = row["snapshot"]
            deadline = snapshot.get("studio_deadline")
            if deadline and now < datetime.fromisoformat(deadline):
                continue
            with self.store.db() as db:
                if db.execute("SELECT 1 FROM videos WHERE run_id=? AND product_key=?",
                              (row["run_id"], row["product_key"])).fetchone():
                    continue  # Something was already generated (or skipped) for it: not a fallback case.
                snapshot["studio_fallback"] = True
                db.execute("UPDATE deliveries SET snapshot=? WHERE run_id=? AND product_key=?",
                           (json.dumps(snapshot), row["run_id"], row["product_key"]))
            run = self.store.run(row["run_id"])
            product = next((p for p in run["products"] if p["key"] == row["product_key"]), None)
            if not product:
                continue
            self.store.log("info", f"Estúdio: {product['name'][:50]} ficou sem geração até o corte; "
                                   "seguiu para o vídeo automático.")
            try:
                self.engine.videos.enqueue(row["run_id"], product, snapshot=snapshot)
            except Exception as error:
                with self.store.db() as db:
                    db.execute("UPDATE deliveries SET state='failed',error=? WHERE run_id=? AND product_key=?",
                               ("Vídeo automático não entrou na fila: " + str(error)[:200], row["run_id"],
                                row["product_key"]))
                self.store.refresh_delivery(row["run_id"])

    def today(self):
        """Reserved products (newest first): the 'Do dia' tab."""
        latest = {}
        with self.store.db() as db:
            sessions = {(r[0], r[1]): r[2] for r in db.execute(
                "SELECT run_id,product_key,id FROM studio WHERE origin='dia'")}
        items = []
        for row in self._reservations()[:30]:
            if row["run_id"] not in latest:
                latest[row["run_id"]] = self.engine.videos.latest_static(self.store, row["run_id"])
            video = latest[row["run_id"]].get(row["product_key"])
            product, snapshot = row["product"], row["snapshot"]
            items.append(dict(run_id=row["run_id"], key=row["product_key"], name=product["name"],
                              image=product.get("image") or "", price=product.get("price", 0),
                              commission=product.get("commission", 0), rating=product.get("rating", 0),
                              sales=product.get("sales", 0), deadline=snapshot.get("studio_deadline"),
                              fallback=bool(snapshot.get("studio_fallback")), delivery=row["state"],
                              error=row["error"], video=dict(state=video["state"], label=video["label"]) if video else None,
                              session_id=sessions.get((row["run_id"], row["product_key"]))))
        return items

    def waiting(self):
        """Reserved products still waiting for the user (no video job yet): the badge of the Estúdio menu."""
        with self.store.db() as db:
            return db.execute("""SELECT COUNT(*) FROM deliveries d WHERE d.state='pending'
                                 AND d.snapshot LIKE '%"studio": true%'
                                 AND NOT EXISTS (SELECT 1 FROM videos v WHERE v.run_id=d.run_id
                                                 AND v.product_key=d.product_key)""").fetchone()[0]

    def overview(self):
        settings = self.store.settings()
        with self.store.db() as db:
            manual = [self._lite(dict(r)) for r in db.execute(
                "SELECT * FROM studio WHERE origin='manual' ORDER BY id DESC LIMIT 20")]
        return dict(slots=settings["studio_slots"], cutoff=settings["studio_cutoff"], today=self.today(), manual=manual,
                    formats=[dict(key=k, label=format_label(k), text=v) for k, v in STUDIO_FORMATS.items()])

    # ---------- rascunhos ----------
    def _row(self, session_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM studio WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Rascunho do Estúdio não encontrado.")
        return dict(row)

    def _save(self, session_id, **values):
        values["updated"] = datetime.now(timezone.utc).isoformat()
        with self.store.db() as db:
            db.execute(f"UPDATE studio SET {','.join(k + '=?' for k in values)} WHERE id=?",
                       (*[json.dumps(v) if isinstance(v, (dict, list)) else v for v in values.values()], session_id))

    def _create(self, origin, product, run_id=None):
        photos = [dict(id="api", source="api", main=True)] if product.get("image") else []
        now = datetime.now(timezone.utc).isoformat()
        with self.store.db() as db:
            return db.execute("INSERT INTO studio(origin,run_id,product_key,product,photos,facts,format,created,updated) "
                              "VALUES(?,?,?,?,?,'',NULL,?,?)",
                              (origin, run_id, product["key"], json.dumps(product), json.dumps(photos), now, now)).lastrowid

    def _lite(self, row):
        product, scripts = json.loads(row["product"]), json.loads(row["scripts"] or "null") or {}
        return dict(id=row["id"], origin=row["origin"], name=product["name"], image=product.get("image") or "",
                    price=product.get("price", 0), commission=product.get("commission", 0),
                    scripts=len(scripts.get("items") or []), chosen=row["chosen"], created=row["created"])

    def open_day(self, run_id, key):
        delivery = self.store.deliveries(run_id).get(key)
        snapshot = json.loads(delivery["snapshot"]) if delivery and delivery.get("snapshot") else {}
        if not snapshot.get("studio"):
            raise ValueError("Este produto não está reservado para o Estúdio.")
        with self.store.db() as db:
            row = db.execute("SELECT id FROM studio WHERE origin='dia' AND run_id=? AND product_key=?",
                             (run_id, key)).fetchone()
        if row:
            return self.get(row[0])
        run = self.store.run(run_id)
        offer = next((p for p in run["products"] if p["key"] == key), None)
        if not offer:
            raise ValueError("Produto não encontrado na consulta.")
        product = {k: offer.get(k) for k in ("key", "item_id", "shop_id", "name", "shop", "price", "commission",
                                              "rating", "sales", "image", "link", "mode", "expires")}
        product["caption"] = (offer.get("analysis") or {}).get("caption") or ""
        product["warnings"] = []
        return self.get(self._create("dia", product, run_id))

    def create_manual(self, text):
        shop, item = resolve_link(text, self.redirector)
        node = self.engine.offer_fetcher(item, shop)
        if not node:
            raise ValueError("Produto fora do programa de afiliados: a API da Shopee não devolveu oferta para ele, "
                             "então não há comissão nem link de afiliado.")
        product = product_from_node(node)
        if not product["link"]:
            raise ValueError("A API da Shopee não devolveu link de afiliado para este produto.")
        warnings = []
        if product["key"] in self.store.exclusions():
            warnings.append("Já entregue, reservado ou descartado antes: um vídeo novo repete o produto.")
        top = self.store.flag("top_affiliates") or {}
        if top.get("day") == datetime.now(self.tz).strftime("%Y-%m-%d") and product["item_id"] in set(top.get("ids") or []):
            warnings.append("Está no top de afiliados da Shopee: concorrência alta.")
        if not product["image"]:
            warnings.append("Anúncio sem foto na API: envie fotos suas.")
        product.update(caption="", warnings=warnings)
        session_id = self._create("manual", product)
        self.store.log("info", f"Estúdio: rascunho manual #{session_id} criado para {product['name'][:50]}.")
        return self.get(session_id)

    def delete(self, session_id):
        self._row(session_id)
        with self.store.db() as db:
            db.execute("DELETE FROM studio WHERE id=?", (session_id,))
        shutil.rmtree(self.directory / str(session_id), ignore_errors=True)

    # ---------- fotos ----------
    def add_photo(self, session_id, data):
        if not isinstance(data, str) or not data:
            raise ValueError("Envie a foto (JPEG ou PNG).")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Foto corrompida no envio; tente de novo.") from None
        jpeg = raw.startswith(b"\xff\xd8")
        if not jpeg and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Foto inválida: envie JPEG ou PNG.")
        if len(raw) > MAX_PHOTO_BYTES:
            raise ValueError("Foto grande demais (até 2,5 MB depois de reduzida).")
        with self.lock:
            photos = json.loads(self._row(session_id)["photos"] or "[]")
            users = [p for p in photos if p["source"] == "user"]
            if len(users) >= MAX_PHOTOS:
                raise ValueError(f"Até {MAX_PHOTOS} fotos suas por produto; remova uma antes.")
            number = max([int(p["id"][1:]) for p in users] or [0]) + 1
            name = f"u{number}" + (".jpg" if jpeg else ".png")
            folder = self.directory / str(session_id)
            folder.mkdir(exist_ok=True)
            (folder / name).write_bytes(raw)
            photos.append(dict(id=f"u{number}", source="user", file=name, main=not any(p.get("main") for p in photos)))
            self._save(session_id, photos=photos)
        return self.get(session_id)

    def photo_file(self, session_id, photo_id):
        if not PHOTO_ID.fullmatch(str(photo_id)):
            raise ValueError("Foto não encontrada.")
        photos = json.loads(self._row(session_id)["photos"] or "[]")
        photo = next((p for p in photos if p["id"] == photo_id), None)
        path = self.directory / str(session_id) / photo["file"] if photo else None
        if not path or not path.is_file():
            raise ValueError("Foto não encontrada.")
        return path, "image/jpeg" if path.suffix == ".jpg" else "image/png"

    def update_photo(self, session_id, photo_id, action):
        if action not in ("remove", "main"):
            raise ValueError("Ação inválida.")
        with self.lock:
            photos = json.loads(self._row(session_id)["photos"] or "[]")
            photo = next((p for p in photos if p["id"] == photo_id), None)
            if not photo:
                raise ValueError("Foto não encontrada.")
            if action == "main":
                for p in photos:
                    p["main"] = p is photo
            else:
                photos.remove(photo)
                if photo.get("file"):
                    (self.directory / str(session_id) / photo["file"]).unlink(missing_ok=True)
                if photo.get("main") and photos:
                    photos[0]["main"] = True
            self._save(session_id, photos=photos)
        return self.get(session_id)

    def images(self, row):
        """Images for the model: P = listing photo (if kept), F1.. = the user's photos, main one first."""
        product, photos = json.loads(row["product"]), json.loads(row["photos"] or "[]")
        ordered = sorted(photos, key=lambda p: not p.get("main"))
        images, number = [], 0
        for photo in ordered:
            if photo["source"] == "api":
                if product.get("image"):
                    images.append(dict(name="P", url=product["image"]))
            else:
                path = self.directory / str(row["id"]) / photo["file"]
                if path.is_file():
                    number += 1
                    images.append(dict(name=f"F{number}", type="image/jpeg" if path.suffix == ".jpg" else "image/png",
                                       data=base64.b64encode(path.read_bytes()).decode()))
        return images

    # ---------- ficha, formato e roteiros ----------
    def save_facts(self, session_id, text):
        if not isinstance(text, str) or len(text) > FACTS_LIMIT:
            raise ValueError(f"Ficha com até {FACTS_LIMIT} caracteres.")
        self._row(session_id)
        self._save(session_id, facts=text.strip())
        return self.get(session_id)

    def set_format(self, session_id, video_format):
        if video_format not in STUDIO_FORMATS:
            raise ValueError("Formato desconhecido.")
        self._row(session_id)
        self._save(session_id, format=video_format)
        return self.get(session_id)

    def start(self, session_id, kind):
        """Model call in the background (agy can take minutes); the page polls the draft."""
        if kind not in ("facts", "scripts"):
            raise ValueError("Ação inválida.")
        with self.lock:
            row = self._row(session_id)
            job = json.loads(row["job"] or "null") or {}
            if job.get("status") == "running":
                raise ValueError("Já existe uma chamada ao modelo em andamento para este produto.")
            if not self.images(row):
                raise ValueError("Sem fotos: mantenha a foto do anúncio ou envie fotos suas.")
            if kind == "scripts" and not row["format"]:
                raise ValueError("Escolha o formato antes de pedir os roteiros.")
            label = "Descrevendo as fotos…" if kind == "facts" else "Escrevendo 3 roteiros…"
            self._save(session_id, job=dict(kind=kind, status="running", message=label, started=time.time()))
        self.start_thread(lambda: self._work(session_id, kind))
        return self.get(session_id)

    def _work(self, session_id, kind):
        try:
            message = self._facts(session_id) if kind == "facts" else self._scripts(session_id)
            status = "ok"
        except Exception as error:  # ModelError or anything else: the draft stays, the job says why.
            message, status = "Falhou: " + str(error)[:400], "failed"
        try:
            self._save(session_id, job=dict(kind=kind, status=status, message=message, finished=time.time()))
        except ValueError:
            return  # draft deleted meanwhile
        self.store.log("info" if status == "ok" else "error", f"Estúdio #{session_id}: {message}")

    def _facts(self, session_id):
        row = self._row(session_id)
        product, images = json.loads(row["product"]), self.images(row)
        output, provider, _ = self.engine.runner.run(
            facts_prompt(product, [i["name"] for i in images]), FACTS_SCHEMA, images, facts_check,
            log=lambda m: self.store.log("info", "Modelo: " + m), deadline=time.time() + FACTS_SECONDS)
        self._save(session_id, facts=facts_text(output))
        return f"ficha visual escrita com {len(images)} foto(s) ({provider})."

    def _scripts(self, session_id):
        row = self._row(session_id)
        product, images, video_format = json.loads(row["product"]), self.images(row), row["format"]
        banned = self.store.recent_creative(8)["openings"][:8]
        output, provider, _ = self.engine.runner.run(
            scripts_prompt(product, row["facts"] or "", video_format, banned, [i["name"] for i in images]),
            SCRIPTS_SCHEMA, images, scripts_check(product["name"], video_format, banned),
            log=lambda m: self.store.log("info", "Modelo: " + m), deadline=time.time() + SCRIPTS_SECONDS)
        items = [dict({k: s[k].strip() for k in ("angle", *SCRIPT_FIELDS)}, edited=False) for s in output["scripts"]]
        caption = dict(description=output["description"].strip(), hashtags=[t.strip() for t in output["hashtags"]])
        self._save(session_id, scripts=dict(items=items, caption=caption, provider=provider, format=video_format,
                                            banned=banned), chosen=None)
        return f"3 roteiros escritos no formato {format_label(video_format)} ({provider})."

    def update_script(self, session_id, index, script=None, choose=False):
        row = self._row(session_id)
        scripts = json.loads(row["scripts"] or "null") or {}
        items = scripts.get("items") or []
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(items):
            raise ValueError("Roteiro não encontrado.")
        if script is not None:
            if not isinstance(script, dict) or set(script) - {"angle", *SCRIPT_FIELDS}:
                raise ValueError("Campos do roteiro inválidos.")
            for key, value in script.items():
                limit = NARRATION_LIMIT if key == "narration" else ANGLE_LIMIT if key == "angle" else SCRIPT_FIELD_LIMIT
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError(f"{key}: texto de até {limit} caracteres.")
            items[index].update({k: v.strip() for k, v in script.items()}, edited=True)
            scripts["items"] = items
            self._save(session_id, scripts=scripts)
        if choose:
            self._save(session_id, chosen=index)
        return self.get(session_id)

    def save_caption(self, session_id, description, hashtags):
        row = self._row(session_id)
        scripts = json.loads(row["scripts"] or "null") or {}
        if not scripts.get("items"):
            raise ValueError("Gere os roteiros antes de editar a legenda.")
        if not isinstance(description, str) or not isinstance(hashtags, list) or len(description) > 300 \
                or len(hashtags) > 5 or not all(isinstance(t, str) and len(t) <= 40 for t in hashtags):
            raise ValueError("Legenda inválida.")
        scripts["caption"] = dict(description=description.strip(), hashtags=[t.strip() for t in hashtags if t.strip()])
        self._save(session_id, scripts=scripts)
        return self.get(session_id)

    # ---------- leitura ----------
    def get(self, session_id):
        row = self._row(session_id)
        product = json.loads(row["product"])
        photos = json.loads(row["photos"] or "[]")
        scripts = json.loads(row["scripts"] or "null") or {}
        video_format = row["format"]
        public_photos = [dict(id=p["id"], source=p["source"], main=bool(p.get("main")),
                              url=product.get("image") if p["source"] == "api" else f"/api/studio/{row['id']}/photos/{p['id']}")
                         for p in photos]
        items = []
        script_format = scripts.get("format") or video_format
        for script in scripts.get("items") or []:
            problems = script_issues(script, product["name"], script_format, scripts.get("banned") or ())
            items.append(dict(script, problems=problems, words=len(script.get("narration", "").split())))
        caption = scripts.get("caption")
        if caption:
            text = caption_text(caption["description"], caption["hashtags"])
            caption = dict(caption, text=text, length=text_length(text),
                           problems=copy_problems(caption["description"], caption["hashtags"]))
        chosen = row["chosen"]
        prompt = (studio_prompt(items[chosen], script_format, row["facts"] or "", len(public_photos))
                  if chosen is not None and chosen < len(items) else "")
        reservation = None
        if row["origin"] == "dia":
            delivery = self.store.deliveries(row["run_id"]).get(row["product_key"]) or {}
            snapshot = json.loads(delivery["snapshot"]) if delivery.get("snapshot") else {}
            reservation = dict(deadline=snapshot.get("studio_deadline"), fallback=bool(snapshot.get("studio_fallback")),
                               delivery=delivery.get("state"))
        job = json.loads(row["job"] or "null")
        return dict(id=row["id"], origin=row["origin"], run_id=row["run_id"], key=row["product_key"],
                    product={k: product.get(k) for k in ("name", "shop", "price", "commission", "rating", "sales",
                                                         "image", "link", "item_id", "shop_id")},
                    warnings=product.get("warnings") or [], photos=public_photos,
                    user_photos=sum(p["source"] == "user" for p in photos), max_photos=MAX_PHOTOS,
                    facts=row["facts"] or "", format=video_format,
                    format_label=format_label(video_format) if video_format else "",
                    scripts=items, scripts_format=scripts.get("format"), provider=scripts.get("provider"),
                    caption=caption, chosen=chosen, prompt=prompt, job=job, reservation=reservation,
                    created=row["created"], updated=row["updated"])
