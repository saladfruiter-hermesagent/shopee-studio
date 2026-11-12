"""Estúdio: rascunhos de vídeo assistidos pelo usuário (PLANO_ESTUDIO.md, etapas S1 e S2).

O usuário escolhe um produto (um dos reservados da consulta do dia ou qualquer anúncio colado pelo link), manda
fotos pelo celular, revisa a ficha visual escrita pelo modelo, escolhe o formato e um entre três roteiros. Depois
aprova o custo e o worker gera 1 cena no Flow com até 4 fotos de referência (S2); o vídeo pronto espera a prévia e
só vai ao Telegram quando o usuário aprova. Produto reservado sem nenhuma geração até o horário de corte cai para o
vídeo automático (`fallback`)."""
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
from content import (CAPTION_LIMIT, CLOSING, FORBIDDEN_CLAIMS, MODERN_NARRATION_WORDS, SCRIPT_FIELD_LIMIT, VIDEO_SECONDS,
                     CaptionTooLong, _digits, _folded, caption_text, copy_problems, has_emoji, modern_script_problems,
                     opening, script_problems, spoken_numbers, text_length, video_caption)
from video import ACTIVE, Videos, badge

MAX_PHOTOS = 6                 # fotos do usuário por produto, além da foto da API
MAX_PHOTO_BYTES = 2_500_000    # depois de reduzida no navegador (~1280 px)
MAX_FLOW_REFS = 4              # referências anexadas no Flow (S0: 4 entraram em Elementos pelo mesmo custo)
WORKER_MIN_VERSION = (1, 4, 0)  # anexa várias fotos, baixa as do painel e aplica as chamadas na tela
FACTS_LIMIT = 1500
NARRATION_LIMIT = 300
ANGLE_LIMIT = 60
FACTS_SECONDS, SCRIPTS_SECONDS = 300, 480
SHORT_HOSTS = ("s.shopee.com.br", "shp.ee", "shope.ee")
# Formatos do Estúdio = como a câmera filma e quem aparece (os mesmos do ShôStudio). A narrativa (problema→solução,
# antes/depois, teste…) fica para os ângulos dos 3 roteiros. O modo automático continua com content.FORMATS.
STUDIO_FORMAT_SPECS = {
    "pov": dict(label="POV", person=False, speaks=False,
                text="POV — câmera em primeira pessoa, como se fossem os olhos de quem usa o produto; só as mãos aparecem.",
                look="Vídeo vertical 9:16 em primeira pessoa (POV), gravado com celular, cenário real de casa."),
    "hands_on": dict(label="Hands-on", person=False, speaks=False,
                     text="hands-on — close das mãos manuseando, abrindo, encaixando e testando o produto sobre uma "
                          "superfície, com foco nos detalhes.",
                     look="Vídeo vertical 9:16 em close, câmera fixa acima ou à frente das mãos, luz natural."),
    "selfie": dict(label="Selfie frontal", person=True, speaks=True,
                   text="selfie frontal — uma pessoa segura o celular com o braço estendido (câmera frontal), mostra o "
                        "produto perto do rosto e comenta enquanto usa.",
                   look="Vídeo vertical 9:16 de selfie com a câmera frontal do celular, braço estendido, luz natural."),
    "falando": dict(label="Falando para a câmera", person=True, speaks=True,
                    text="falando para a câmera — uma pessoa em plano médio, câmera parada, fala direto para a lente "
                         "como numa recomendação a uma amiga, segurando e demonstrando o produto.",
                    look="Vídeo vertical 9:16 em plano médio, câmera parada na altura dos olhos, ambiente de casa."),
    "provador": dict(label="Provador", person=True, speaks=False,
                     text="provador — uma pessoa veste, calça ou usa o produto diante do espelho ou da câmera, mostrando "
                          "caimento, tamanho real e detalhes de perto.",
                     look="Vídeo vertical 9:16 estilo provador, pessoa de corpo inteiro ou meio corpo, luz natural."),
    "ugc": dict(label="Formato livre (UGC)", person=True, speaks=True,
                text="formato livre (UGC) — vídeo caseiro, como se gravado por uma cliente real com o celular: ambiente "
                     "comum, luz natural, câmera na mão, cortes rápidos entre uso e resultado; a pessoa pode aparecer e falar.",
                look="Vídeo vertical 9:16 estilo UGC realista, gravado com celular na mão, nada de aparência de comercial."),
}
STUDIO_FORMATS = {key: spec["text"] for key, spec in STUDIO_FORMAT_SPECS.items()}
PERSON_FORMATS = {key for key, spec in STUDIO_FORMAT_SPECS.items() if spec["person"]}  # a regra de só mãos não vale
SPEAKING_FORMATS = {key for key, spec in STUDIO_FORMAT_SPECS.items() if spec["speaks"]}  # a pessoa fala em cena
NARRATIVE_ANGLES = ("problema e solução", "antes e depois", "teste ou desafio", "uso na rotina", "detalhes de perto",
                    "unboxing", "curiosidade de como funciona")
SCRIPT_FIELDS = ("hook", "demo", "benefit", "cta", "narration")
PHOTO_ID = re.compile(r"u\d{1,3}")
# Estilo do roteiro. "problema" (testes a partir de 12/11): dor real do dia a dia, câmera que chama atenção, 3 chamadas
# escritas na tela pelo worker e a fala SEM legenda. "classico": o da S1, com a legenda da narração.
SCRIPT_STYLES = {
    "problema": dict(label="Problema real + chamadas na tela",
                     text="Começa por uma dor real do dia a dia, a câmera prende o olhar, o produto resolve e 3 chamadas "
                          "grandes aparecem na tela. A fala fica sem legenda."),
    "classico": dict(label="Clássico",
                     text="Gancho, demonstração, prova e chamada, com a legenda da narração."),
}
# A fala do estilo "problema" é mais curta que a do automático (18–25): em ~2,3 palavras/s, 14–19 palavras terminam
# entre 6 e 8 s e sobram ~2 s só com o produto inteiro no quadro — sem isso o vídeo "acaba do nada".
PROBLEM_NARRATION_WORDS = (14, 19)
CALLOUTS = 3
CALLOUT_LIMIT = 26
CALLOUT_ROLES = ("a dor · 0–3 s", "a virada · 3–6 s", "não perca · 6–10 s")

FACTS_SCHEMA = {"type": "object", "properties": {
    "facts": {"type": "array", "items": {"type": "string"}},
    "avoid": {"type": "array", "items": {"type": "string"}}}, "required": ["facts", "avoid"]}
SCRIPT_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in ("angle", *SCRIPT_FIELDS)},
                 "required": ["angle", *SCRIPT_FIELDS]}
SCRIPTS_SCHEMA = {"type": "object", "properties": {
    "description": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
    "scripts": {"type": "array", "items": SCRIPT_SCHEMA}}, "required": ["description", "hashtags", "scripts"]}
PROBLEM_SCRIPT_SCHEMA = {"type": "object", "properties": dict(SCRIPT_SCHEMA["properties"],
                                                              callouts={"type": "array", "items": {"type": "string"}}),
                         "required": ["angle", *SCRIPT_FIELDS, "callouts"]}
PROBLEM_SCRIPTS_SCHEMA = dict(SCRIPTS_SCHEMA, properties=dict(SCRIPTS_SCHEMA["properties"],
                                                              scripts={"type": "array", "items": PROBLEM_SCRIPT_SCHEMA}))


def _num(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def format_label(key):
    spec = STUDIO_FORMAT_SPECS.get(key)
    return spec["label"] if spec else ""


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
    person, speaks = video_format in PERSON_FORMATS, video_format in SPEAKING_FORMATS
    banned_text = ", ".join(f'"{o}"' for o in banned) or "nenhuma"
    people = ("Aparece uma pessoa. Descreva-a uma vez no \"hook\" (idade aproximada, cabelo, roupa simples, onde está) "
              "para o vídeo manter a mesma pessoa; o público é brasileiro e a cena é do dia a dia, sem cara de comercial."
              if person else
              "Só mãos em cena (visão em primeira pessoa ou close): nenhum rosto, sorriso ou pessoa de frente para a câmera.")
    voice = ("A narração é FALADA pela pessoa em cena, olhando para a câmera, em tom de conversa (como quem indica a uma "
             "amiga), não de locutor." if speaks else "A narração é uma voz em off, natural e animada.")
    return f"""Você escreve roteiros de vídeos curtos de venda para a Shopee Vídeo ({VIDEO_SECONDS} s, vertical 9:16).
O vídeo é feito por uma IA que recebe as fotos do produto como referência ({", ".join(names)}).
{DATA_RULES}

{_data(product)}

FICHA DO PRODUTO (conferida pelo afiliado; é a única fonte de atributos — não cite nada que não esteja nela, no nome
ou nas fotos):
<ficha>
{facts.strip() or "(sem ficha: use só o nome do anúncio e as fotos)"}
</ficha>

FORMATO OBRIGATÓRIO (como a câmera filma e quem aparece): {STUDIO_FORMATS[video_format]}

Escreva 3 roteiros neste formato, cada um com uma ABORDAGEM diferente; escolha as 3 que melhor vendem este produto entre:
{", ".join(NARRATIVE_ANGLES)}.
Cada roteiro tem:
- "angle": a abordagem em até 6 palavras.
- "hook" (0–2 s): a primeira imagem tem de prender sozinha — o produto ou o problema aparece já no primeiro segundo.
- "demo" (2–6 s): uma ação concreta e observável com o produto.
- "benefit" (6–8 s): a PROVA — o resultado visível que mostra o benefício.
- "cta" (8–10 s): gesto ou cena que convida a tocar no produto marcado.
  Cenas filmáveis, em português, até {SCRIPT_FIELD_LIMIT} caracteres cada; sem textos, setas ou elementos gráficos na tela.
- "narration": a fala completa, PT-BR natural, de {low} a {high} palavras. A primeira frase é o gancho falado (pergunta ou
  afirmação forte); a última frase cita "{CLOSING}" (ex.: "Toque no {CLOSING}."). Não comece com "Olha" nem "Gente", nem com
  estas aberturas já usadas: {banned_text}. Os 3 roteiros começam de jeitos diferentes. Todo número falado tem de
  aparecer no nome do anúncio; na dúvida, não fale números. Sem preço, desconto, frete, estoque ou urgência.
  {voice}
- {people}

LEGENDA (uma só, vale para qualquer roteiro): "description" = uma frase com um benefício verificável e o contexto de uso,
sem emojis, sem preço, desconto, frete ou urgência; "hashtags" = 2 ou 3 (produto, uso/contexto). Proibido #Shopee,
#Achadinhos, #Viral, #Fyp. description + espaço + hashtags com no máximo {CAPTION_LIMIT} caracteres.
Responda somente com o JSON do schema."""


def problem_scripts_prompt(product, facts, video_format, banned, names):
    """Estilo "problema": anúncio curtíssimo que parte de uma dor real, câmera que prende o olhar, 3 chamadas na tela."""
    low, high = MODERN_NARRATION_WORDS
    person, speaks = video_format in PERSON_FORMATS, video_format in SPEAKING_FORMATS
    banned_text = ", ".join(f'"{o}"' for o in banned) or "nenhuma"
    people = ("Aparece uma pessoa. Descreva-a uma vez no \"hook\" (idade aproximada, cabelo, roupa simples, onde está) "
              "para o vídeo manter a mesma pessoa; público brasileiro, cena do dia a dia, sem cara de comercial."
              if person else
              "Só mãos em cena (visão em primeira pessoa ou close): nenhum rosto, sorriso ou pessoa de frente para a câmera.")
    voice = ("A fala é da pessoa em cena, olhando para a câmera, em tom de conversa (como quem avisa uma amiga)."
             if speaks else "A fala é uma voz em off, natural e com energia.")
    low, high = PROBLEM_NARRATION_WORDS
    return f"""Você cria anúncios curtíssimos ({VIDEO_SECONDS} s, vertical 9:16) para a Shopee Vídeo. Quem assiste está
rolando o feed e decide em 1 segundo se para. O vídeo é feito por uma IA que recebe as fotos do produto como referência
({", ".join(names)}).
{DATA_RULES}

{_data(product)}

FICHA DO PRODUTO (conferida pelo afiliado; é a única fonte de atributos — não cite nada que não esteja nela, no nome
ou nas fotos):
<ficha>
{facts.strip() or "(sem ficha: use só o nome do anúncio e as fotos)"}
</ficha>

FORMATO OBRIGATÓRIO (como a câmera filma e quem aparece): {STUDIO_FORMATS[video_format]}

ESTRATÉGIA (vale para os 3 roteiros):
1. Parta de um PROBLEMA REAL do dia a dia que este produto resolve: algo que a pessoa vive e reconhece na hora (perda de
   tempo, bagunça, desconforto, esforço, gasto, constrangimento). Tem de ser plausível pela ficha: não invente função.
2. A CÂMERA chama a atenção no primeiro segundo com um movimento intencional: aproximação rápida até quase encostar na
   lente, chicote (whip pan), câmera que cai na cena, o produto entrando na frente da lente ou mudança brusca de foco.
   Escreva o movimento na cena.
3. A virada: o produto entra e resolve o problema na frente da câmera, de forma visível.
4. O contraste: com o produto o dia a dia fica claramente melhor; quem assiste tem de sentir que sem ele a rotina volta a
   ser pior e que esta é uma oportunidade que não dá para deixar passar.
Cada um dos 3 roteiros ataca um problema real DIFERENTE (ou um momento diferente do mesmo problema).

Cada roteiro tem:
- "angle": o problema real em até 6 palavras.
- "hook" (0–3 s): o problema acontecendo + o movimento de câmera que prende o olhar.
- "demo" (3–6 s): o produto entrando e resolvendo, com uma ação concreta e observável.
- "benefit" (6–8 s): o resultado visível — a rotina melhor.
- "cta" (8–10 s): o RESPIRO final — o produto INTEIRO no quadro, câmera parada (plano mais aberto que as cenas
  anteriores), com um gesto curto convidando a tocar no produto marcado. Nada de aproximar da lente nem fechar o
  enquadramento: quem assiste precisa ver o produto completo antes de o vídeo acabar.
  Cenas filmáveis, em português, até {SCRIPT_FIELD_LIMIT} caracteres cada; sem textos, setas ou elementos gráficos na
  cena (as chamadas abaixo são aplicadas depois, por cima do vídeo).
- "callouts": exatamente 3 chamadas que aparecem ESCRITAS na tela, grandes e estilizadas, de 3 a {CALLOUT_LIMIT}
  caracteres cada, sem emojis, ditas como um aviso direto a quem assiste:
  1 (0–3 s) dá nome à dor (ex.: "CHEGA DE CABO EMBOLADO");
  2 (3–6 s) a virada, a solução que está ali (ex.: "RESOLVE EM SEGUNDOS");
  3 (6–10 s) o não-perca: lembra que sem ele o dia a dia volta a ser pior (ex.: "NÃO FIQUE SEM O SEU",
  "SUA ROTINA PEDE ISSO"). Não escreva "produto marcado" nela: o botão do fim do vídeo já diz isso.
  A urgência vem do problema, nunca de escassez ou preço: proibido preço, desconto, porcentagem, frete, estoque,
  cupom, "corra", "só hoje", "últimas unidades" e qualquer número que não esteja no nome do anúncio.
- "narration": a fala completa, PT-BR natural, de {low} a {high} palavras (não vai legenda na tela) — mais longa que
  isso não caberia: a fala tem de TERMINAR antes do fim do vídeo, deixando ~2 s de silêncio sobre o plano final. A primeira frase
  é a dor (pergunta ou afirmação que a pessoa reconhece); depois vem a solução; a última frase lembra que sem ele o
  dia fica pior e cita "{CLOSING}" (ex.: "Não volte para o sufoco: toque no {CLOSING}."). Não comece com "Olha" nem
  "Gente", nem com estas aberturas já usadas: {banned_text}. Os 3 roteiros começam de jeitos diferentes. Todo número
  falado tem de aparecer no nome do anúncio. Sem preço, desconto, frete, estoque ou urgência inventada.
  {voice}
- {people}

LEGENDA DO TELEGRAM (uma só, vale para qualquer roteiro): "description" = uma frase com o problema que o produto
resolve e o contexto de uso, sem emojis, sem preço, desconto, frete ou urgência; "hashtags" = 2 ou 3 (produto,
uso/contexto). Proibido #Shopee, #Achadinhos, #Viral, #Fyp. description + espaço + hashtags com no máximo
{CAPTION_LIMIT} caracteres.
Responda somente com o JSON do schema."""


def callout_issues(callouts, name):
    """Chamadas escritas na tela (estilo "problema"): curtas, diferentes, sem escassez/preço nem número inventado."""
    if not isinstance(callouts, list) or len(callouts) != CALLOUTS or not all(isinstance(c, str) for c in callouts):
        return [f"chamadas: escreva exatamente {CALLOUTS}"]
    problems = []
    for index, text in enumerate(callouts, 1):
        text = text.strip()
        if not 3 <= len(text) <= CALLOUT_LIMIT:
            problems.append(f"chamada {index}: de 3 a {CALLOUT_LIMIT} caracteres")
        if has_emoji(text):
            problems.append(f"chamada {index}: sem emojis")
        if FORBIDDEN_CLAIMS.search(text):
            problems.append(f"chamada {index}: sem preço, desconto, frete, estoque ou urgência inventada")
        invented = spoken_numbers(text) - _digits(_folded(name))
        if invented:
            problems.append(f"chamada {index}: número que não está no nome do anúncio ({', '.join(sorted(invented))})")
    if CLOSING in _folded(callouts[-1]):
        problems.append(f"chamada 3: o botão do fim já diz \"{CLOSING}\"; use o não-perca (ex.: NÃO FIQUE SEM O SEU)")
    if len({_folded(c).strip() for c in callouts}) < CALLOUTS:
        problems.append("as 3 chamadas precisam ser diferentes")
    return problems


def script_issues(script, name, video_format, banned=(), style="classico"):
    """Problems of one script under the Studio rules (the same as the Modern mode, faces allowed in 'provador')."""
    problems = script_problems(script)
    problems += modern_script_problems(script, name, banned, hands_only=video_format not in PERSON_FORMATS,
                                      words=PROBLEM_NARRATION_WORDS if style == "problema" else None)
    angle = script.get("angle")
    if not isinstance(angle, str) or not angle.strip() or len(angle) > ANGLE_LIMIT:
        problems.append(f"angle vazio ou com mais de {ANGLE_LIMIT} caracteres")
    if style == "problema":
        problems += callout_issues(script.get("callouts"), name)
    return problems


def scripts_check(name, video_format, banned, style="classico"):
    def check(output):
        if not isinstance(output, dict):
            return ["resposta não é um objeto JSON"]
        problems = copy_problems(output.get("description"), output.get("hashtags"))
        scripts = output.get("scripts")
        if not isinstance(scripts, list) or len(scripts) != 3 or not all(isinstance(s, dict) for s in scripts):
            return problems + ["scripts deve ter exatamente 3 roteiros"]
        for index, script in enumerate(scripts, 1):
            problems += [f"roteiro {index}: {p}" for p in script_issues(script, name, video_format, banned, style)]
        openings = [opening(s.get("narration") or "") for s in scripts]
        if len(set(openings)) < len(openings):
            problems.append("os 3 roteiros precisam começar a narração de jeitos diferentes")
        return problems
    return check


def _bullets(lines, limit):
    """'- a' lines joined with '; ', cut at a whole item (never mid-word)."""
    text = ""
    for line in lines:
        item = line[2:].strip().rstrip(".;")
        if item and len(text) + len(item) + 2 <= limit:
            text += ("; " if text else "") + item
    return text


def studio_prompt(script, video_format, facts, references, style="classico"):
    """Flow prompt of a Studio draft. Style "problema": problem → solution → better routine, attention-grabbing camera,
    and the action kept away from the top band, where the worker burns the callouts."""
    spec = STUDIO_FORMAT_SPECS.get(video_format) or STUDIO_FORMAT_SPECS["hands_on"]
    facts_part, _, avoid_part = facts.partition("Cuidados para a IA de vídeo:")
    facts_line = _bullets([l for l in facts_part.splitlines() if l.startswith("- ")], 700)
    avoid_line = _bullets([l for l in avoid_part.splitlines() if l.startswith("- ")], 450)
    lines = [f"{str(spec['look']).rstrip('.')}, {VIDEO_SECONDS} segundos, mostrando o produto das imagens de referência.",
             ("As imagens anexadas mostram o mesmo produto de ângulos diferentes. " if references > 1 else "")
             + "Preserve exatamente formato, cores, proporções e componentes do produto. Não acrescente acessórios.",
             f"Formato: {spec['text']}"]
    if not spec["person"]:
        lines.append("Somente mãos aparecem em cena: nenhum rosto e nenhuma pessoa de frente para a câmera.")
    if facts_line:
        lines.append(f"Fatos do produto (não contradiga): {facts_line}")
    if avoid_line:
        lines.append(f"Cuidados: {avoid_line}")
    speech = script["narration"].strip()
    if style == "problema":
        lines += ["Anúncio curto: um problema real do dia a dia, o produto resolvendo na frente da câmera e a rotina "
                  "claramente melhor no final. Câmera com movimento intencional e energia desde o primeiro segundo.",
                  "Mantenha o produto e a ação no centro e na metade de baixo do quadro; a faixa de cima fica livre.",
                  f"0–3 s, problema e câmera: {script['hook'].strip()}", f"3–6 s, o produto resolve: {script['demo'].strip()}",
                  f"6–8 s, resultado: {script['benefit'].strip()}",
                  f"8–10 s, respiro final (sem fala, produto inteiro e câmera parada): {script['cta'].strip()}",
                  "A fala termina antes do fim: o último trecho é só a imagem calma do produto inteiro, sem corte seco "
                  "e sem movimento brusco de câmera no final."]
    else:
        lines += [f"0–2 s, gancho: {script['hook'].strip()}", f"2–6 s, demonstração: {script['demo'].strip()}",
                  f"6–8 s, prova: {script['benefit'].strip()}", f"8–10 s, chamada: {script['cta'].strip()}"]
    lines += [
              (f"A pessoa fala olhando para a câmera, em português do Brasil, tom de conversa, com a boca sincronizada: "
               f"\"{speech}\"" if spec["speaks"] else f"Narração em off em português do Brasil, voz natural e animada: \"{speech}\""),
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
                    cost=settings["video_cost"], manual_credits=self.engine.videos.manual_credits(),
                    styles=[dict(key=k, label=v["label"], text=v["text"]) for k, v in SCRIPT_STYLES.items()],
                    callout_roles=list(CALLOUT_ROLES),
                    formats=[dict(key=k, label=f["label"], text=f["text"], person=f["person"], speaks=f["speaks"])
                             for k, f in STUDIO_FORMAT_SPECS.items()])

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
        video = self._videos(row["id"])[:1]
        return dict(id=row["id"], origin=row["origin"], name=product["name"], image=product.get("image") or "",
                    price=product.get("price", 0), commission=product.get("commission", 0),
                    scripts=len(scripts.get("items") or []), chosen=row["chosen"], created=row["created"],
                    video=dict(state=video[0]["state"], label=video[0]["label"]) if video else None)

    def _videos(self, session_id):
        """Generations of a draft, newest first (public fields only)."""
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM videos WHERE studio_id=? ORDER BY id DESC", (session_id,))]
        return [Videos._public(row) for row in rows]

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
        videos = self._videos(session_id)
        if videos and videos[0]["state"] in ACTIVE | {"sending"}:
            raise ValueError("Há um vídeo deste rascunho em andamento; espere terminar antes de excluir.")
        for video in videos:  # A preview nobody approved would keep the product of the day pending forever.
            if video["state"] in ("review", "delivery_failed"):
                self.engine.videos.discard(video["id"])
        with self.store.db() as db:
            files = [r[0] for r in db.execute("SELECT file FROM videos WHERE studio_id=? AND file IS NOT NULL",
                                              (session_id,))]
            # The job rows stay (credits and history); only the files go with the draft.
            db.execute("UPDATE videos SET file=NULL WHERE studio_id=?", (session_id,))
            db.execute("DELETE FROM studio WHERE id=?", (session_id,))
        for name in files:
            (self.engine.videos.directory / name).unlink(missing_ok=True)
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

    def start(self, session_id, kind, style=None):
        """Model call in the background (agy can take minutes); the page polls the draft."""
        if kind not in ("facts", "scripts"):
            raise ValueError("Ação inválida.")
        style = style or "classico"
        if style not in SCRIPT_STYLES:
            raise ValueError("Estilo de roteiro desconhecido.")
        with self.lock:
            row = self._row(session_id)
            job = json.loads(row["job"] or "null") or {}
            if job.get("status") == "running":
                raise ValueError("Já existe uma chamada ao modelo em andamento para este produto.")
            if not self.images(row):
                raise ValueError("Sem fotos: mantenha a foto do anúncio ou envie fotos suas.")
            if kind == "scripts" and row["format"] not in STUDIO_FORMATS:
                raise ValueError("Escolha o formato antes de pedir os roteiros.")
            label = "Descrevendo as fotos…" if kind == "facts" else "Escrevendo 3 roteiros…"
            self._save(session_id, job=dict(kind=kind, status="running", message=label, started=time.time()))
        self.start_thread(lambda: self._work(session_id, kind, style))
        return self.get(session_id)

    def _work(self, session_id, kind, style="classico"):
        try:
            message = self._facts(session_id) if kind == "facts" else self._scripts(session_id, style)
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

    def _scripts(self, session_id, style="classico"):
        row = self._row(session_id)
        product, images, video_format = json.loads(row["product"]), self.images(row), row["format"]
        banned = self.store.recent_creative(8)["openings"][:8]
        problem = style == "problema"
        prompt = problem_scripts_prompt if problem else scripts_prompt
        output, provider, _ = self.engine.runner.run(
            prompt(product, row["facts"] or "", video_format, banned, [i["name"] for i in images]),
            PROBLEM_SCRIPTS_SCHEMA if problem else SCRIPTS_SCHEMA, images,
            scripts_check(product["name"], video_format, banned, style),
            log=lambda m: self.store.log("info", "Modelo: " + m), deadline=time.time() + SCRIPTS_SECONDS)
        items = []
        for script in output["scripts"]:
            item = dict({k: script[k].strip() for k in ("angle", *SCRIPT_FIELDS)}, edited=False)
            if problem:
                item["callouts"] = [c.strip() for c in script["callouts"]]
            items.append(item)
        caption = dict(description=output["description"].strip(), hashtags=[t.strip() for t in output["hashtags"]])
        self._save(session_id, scripts=dict(items=items, caption=caption, provider=provider, format=video_format,
                                            banned=banned, style=style), chosen=None)
        return (f"3 roteiros \"{SCRIPT_STYLES[style]['label']}\" escritos no formato {format_label(video_format)} "
                f"({provider}).")

    def update_script(self, session_id, index, script=None, choose=False):
        row = self._row(session_id)
        scripts = json.loads(row["scripts"] or "null") or {}
        items = scripts.get("items") or []
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(items):
            raise ValueError("Roteiro não encontrado.")
        if script is not None:
            if not isinstance(script, dict) or set(script) - {"angle", "callouts", *SCRIPT_FIELDS}:
                raise ValueError("Campos do roteiro inválidos.")
            script = dict(script)
            callouts = script.pop("callouts", None)
            if callouts is not None:
                if scripts.get("style") != "problema":
                    raise ValueError("Este roteiro não tem chamadas na tela.")
                if not isinstance(callouts, list) or len(callouts) != CALLOUTS \
                        or not all(isinstance(c, str) and len(c) <= 60 for c in callouts):
                    raise ValueError(f"Chamadas: {CALLOUTS} textos curtos.")
            for key, value in script.items():
                limit = NARRATION_LIMIT if key == "narration" else ANGLE_LIMIT if key == "angle" else SCRIPT_FIELD_LIMIT
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError(f"{key}: texto de até {limit} caracteres.")
            items[index].update({k: v.strip() for k, v in script.items()}, edited=True)
            if callouts is not None:
                items[index]["callouts"] = [c.strip() for c in callouts]
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

    # ---------- vídeo no Flow (S2) ----------
    def references(self, row):
        """Photos that go to the Flow, main one first, up to MAX_FLOW_REFS. The listing photo goes by URL; the
        user's photos by id, downloaded by the worker from the panel with its own token."""
        product, photos = json.loads(row["product"]), json.loads(row["photos"] or "[]")
        refs = []
        for photo in sorted(photos, key=lambda p: not p.get("main")):
            if photo["source"] == "api":
                if product.get("image"):
                    refs.append(dict(id="api", url=product["image"]))
            elif (self.directory / str(row["id"]) / photo["file"]).is_file():
                refs.append(dict(id=photo["id"], photo=photo["id"]))
        return refs[:MAX_FLOW_REFS]

    def fidelity_images(self, session_id, images):
        """References of a finished job for the fidelity check (R1..R3); photos deleted since are skipped."""
        result = []
        for image in images[:3]:
            name = f"R{len(result) + 1}"
            if image.get("url"):
                result.append(dict(name=name, url=image["url"]))
            elif image.get("photo"):
                try:
                    path, kind = self.photo_file(session_id, image["photo"])
                except ValueError:
                    continue
                result.append(dict(name=name, type=kind, data=base64.b64encode(path.read_bytes()).decode()))
        return result

    def worker_problem(self):
        """Why the video worker cannot take a Studio job (None when it can). Offline is fine: the job waits."""
        workers = [w for w in self.store.worker_status()["workers"] if "video" in w["kinds"]]
        if not workers:
            return "O worker de vídeo da VM ainda não se conectou ao painel."
        version = workers[0]["version"] or "?"
        if tuple(int(n) for n in re.findall(r"\d+", version)[:3]) < WORKER_MIN_VERSION:
            return (f"O worker de vídeo da VM está na versão {version}; o Estúdio precisa da "
                    f"{'.'.join(map(str, WORKER_MIN_VERSION))} ou mais nova (rode worker/install.sh na VM).")
        return None

    def _snapshot(self, row, product, caption):
        """Message of the Telegram delivery: the Studio caption, with the links already frozen for the product of
        the day (equivalents included) or the listing link of a manual product."""
        links = [dict(key=product["key"], link=product["link"])] if product.get("link") else []
        if row["origin"] == "dia":
            delivery = self.store.deliveries(row["run_id"]).get(row["product_key"]) or {}
            links = (json.loads(delivery["snapshot"]) if delivery.get("snapshot") else {}).get("links") or links
        if not links:
            raise ValueError("Produto sem link de afiliado para a mensagem do Telegram.")
        try:
            text = video_caption(dict(name=product["name"], link=links[0]["link"],
                                      analysis=dict(caption=caption, links=links, status="ok")))
        except CaptionTooLong as error:
            raise ValueError(str(error)) from None
        return dict(caption=text, links=links, name=product["name"][:120], studio_session=row["id"])

    def generate(self, session_id, cost):
        """The user approved the cost on the screen (E3): one generation of 1 scene with the chosen script."""
        settings = self.store.settings()
        if isinstance(cost, bool) or cost != settings["video_cost"]:
            raise ValueError(f"O custo por vídeo agora é {settings['video_cost']} créditos; confira e aprove de novo.")
        row = self._row(session_id)
        draft = self.get(session_id)
        if draft["chosen"] is None or not draft["prompt"]:
            raise ValueError("Escolha um roteiro antes de gerar o vídeo.")
        if draft["scripts_format"] and draft["scripts_format"] != draft["format"]:
            raise ValueError("Os roteiros foram escritos para outro formato: gere os roteiros de novo ou volte o formato.")
        if draft["reservation"] and draft["reservation"]["fallback"]:
            raise ValueError("Passou do horário de corte: este produto já seguiu para o vídeo automático.")
        problem = self.worker_problem()
        if problem:
            raise ValueError(problem)
        refs = self.references(row)
        if not refs:
            raise ValueError("Sem fotos: mantenha a foto do anúncio ou envie fotos suas.")
        product = json.loads(row["product"])
        script = draft["scripts"][draft["chosen"]]
        style = draft["scripts_style"] or "classico"
        snapshot = self._snapshot(row, product, draft["caption"]["text"] if draft["caption"] else "")
        finish = dict(size=[1080, 1920], badge=badge(product), frames=True)
        if style == "problema":
            # The worker burns the 3 callouts and leaves the spoken narration without subtitles.
            # Fim limpo (pedido do usuário, 12/11): sem "★ nota • N vendidos" e sem "CONFIRA NO PRODUTO MARCADO".
            # O clique fica com a 3ª chamada, que pulsa até o fim sobre o plano do produto inteiro.
            finish.pop("badge", None)
            finish.update(subtitles=False, callouts=[c.strip().upper() for c in script.get("callouts") or []][:CALLOUTS])
        payload = dict(product=product["name"][:120], studio_id=session_id,
                       images=[{k: v for k, v in ref.items() if k != "id"} for ref in refs],
                       # Older workers read only image_url; the version check above keeps them away.
                       image_url=next((ref["url"] for ref in refs if ref.get("url")), ""),
                       prompt=studio_prompt(script, draft["scripts_format"] or draft["format"], row["facts"] or "",
                                            len(refs), style),
                       narration=script["narration"], finish=finish)
        self.engine.videos.enqueue_studio(session_id, row["origin"], row["run_id"] if row["origin"] == "dia" else None,
                                          row["product_key"], product["name"], payload, snapshot)
        return self.get(session_id)

    def _video(self, session_id, video_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM videos WHERE id=? AND studio_id=?", (video_id, session_id)).fetchone()
        if not row:
            raise ValueError("Vídeo não encontrado neste rascunho.")
        return dict(row)

    def send_video(self, session_id, video_id):
        """Preview approved (E4): one Telegram message with the video, the Studio caption and the links."""
        row, video = self._row(session_id), self._video(session_id, video_id)
        if video["state"] not in ("review", "delivery_failed"):
            raise ValueError("Este vídeo não está esperando envio.")
        draft = self.get(session_id)
        product = json.loads(row["product"])
        # The caption may have been edited after the generation: the message uses the one approved now.
        fresh = self._snapshot(row, product, draft["caption"]["text"] if draft["caption"] else "")
        snapshot = dict(json.loads(video["snapshot"] or "{}"), **fresh)
        with self.store.db() as db:
            db.execute("UPDATE videos SET snapshot=? WHERE id=?", (json.dumps(snapshot), video_id))
            if row["origin"] == "dia":
                delivery = db.execute("SELECT snapshot FROM deliveries WHERE run_id=? AND product_key=?",
                                      (row["run_id"], row["product_key"])).fetchone()
                if delivery and delivery[0]:
                    frozen = dict(json.loads(delivery[0]), caption=snapshot["caption"])
                    db.execute("UPDATE deliveries SET snapshot=? WHERE run_id=? AND product_key=?",
                               (json.dumps(frozen), row["run_id"], row["product_key"]))
        self.engine.videos.deliver(video_id, manual=True)
        return self.get(session_id)

    def discard_video(self, session_id, video_id):
        self._video(session_id, video_id)
        self.engine.videos.discard(video_id)
        return self.get(session_id)

    def video_file(self, session_id, video_id):
        video = self._video(session_id, video_id)
        path = self.engine.videos.directory / video["file"] if video["file"] else None
        if not path or not path.is_file():
            raise ValueError("Arquivo do vídeo não está mais no painel.")
        return path

    def worker_photo(self, task, photo_id):
        """Photo of a Studio job for the worker that claimed it; only ids listed in the job payload."""
        payload = task["payload"]
        if not payload.get("studio_id") or photo_id not in [i.get("photo") for i in payload.get("images") or []]:
            raise ValueError("Foto não faz parte desta tarefa.")
        return self.photo_file(payload["studio_id"], photo_id)

    # ---------- leitura ----------
    def get(self, session_id):
        row = self._row(session_id)
        product = json.loads(row["product"])
        photos = json.loads(row["photos"] or "[]")
        scripts = json.loads(row["scripts"] or "null") or {}
        video_format = row["format"] if row["format"] in STUDIO_FORMATS else None  # formato antigo: escolher de novo
        public_photos = [dict(id=p["id"], source=p["source"], main=bool(p.get("main")),
                              url=product.get("image") if p["source"] == "api" else f"/api/studio/{row['id']}/photos/{p['id']}")
                         for p in photos]
        items = []
        script_format = scripts.get("format") or video_format
        style = scripts.get("style") or "classico"  # drafts written before the styles existed are "classico"
        for script in scripts.get("items") or []:
            problems = script_issues(script, product["name"], script_format, scripts.get("banned") or (), style)
            items.append(dict(script, problems=problems, words=len(script.get("narration", "").split())))
        caption = scripts.get("caption")
        if caption:
            text = caption_text(caption["description"], caption["hashtags"])
            caption = dict(caption, text=text, length=text_length(text),
                           problems=copy_problems(caption["description"], caption["hashtags"]))
        chosen = row["chosen"]
        refs = self.references(row)
        prompt = (studio_prompt(items[chosen], script_format, row["facts"] or "", len(refs), style)
                  if chosen is not None and chosen < len(items) else "")
        reservation = None
        if row["origin"] == "dia":
            delivery = self.store.deliveries(row["run_id"]).get(row["product_key"]) or {}
            snapshot = json.loads(delivery["snapshot"]) if delivery.get("snapshot") else {}
            reservation = dict(deadline=snapshot.get("studio_deadline"), fallback=bool(snapshot.get("studio_fallback")),
                               delivery=delivery.get("state"))
        job = json.loads(row["job"] or "null")
        settings, videos = self.store.settings(), self._videos(row["id"])
        for video in videos:
            video["url"] = f"/api/studio/{row['id']}/videos/{video['id']}" if video["has_file"] else None
        photo_urls = {p["id"]: p["url"] for p in public_photos}
        generation = dict(cost=settings["video_cost"], used=self.engine.videos.used_today(),
                          cap=settings["video_daily_credits"], manual=self.engine.videos.manual_credits(),
                          refs=[photo_urls.get(r["id"]) for r in refs], max_refs=MAX_FLOW_REFS,
                          enabled=settings["video_enabled"], paused=self.engine.videos.flag("video_pause"),
                          worker_problem=self.worker_problem(), worker_online=self.store.worker_online("video"))
        return dict(id=row["id"], origin=row["origin"], run_id=row["run_id"], key=row["product_key"],
                    product={k: product.get(k) for k in ("name", "shop", "price", "commission", "rating", "sales",
                                                         "image", "link", "item_id", "shop_id")},
                    warnings=product.get("warnings") or [], photos=public_photos,
                    user_photos=sum(p["source"] == "user" for p in photos), max_photos=MAX_PHOTOS,
                    facts=row["facts"] or "", format=video_format,
                    format_label=format_label(video_format) if video_format else "",
                    scripts=items, scripts_format=scripts.get("format"), provider=scripts.get("provider"),
                    scripts_style=style if items else None,
                    narration_words=list(PROBLEM_NARRATION_WORDS if style == "problema" else MODERN_NARRATION_WORDS),
                    caption=caption, chosen=chosen, prompt=prompt, job=job, reservation=reservation,
                    video=videos[0] if videos else None, videos=videos[:10], generation=generation,
                    created=row["created"], updated=row["updated"])
