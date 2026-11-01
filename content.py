"""Copy rules, video prompt assembly and the Telegram video caption."""
import html
import re
import unicodedata

CAPTION_LIMIT = 150          # description + hashtags, defined by the user
TELEGRAM_CAPTION_LIMIT = 1024  # sendVideo caption: 0-1024 characters after entities parsing (Bot API)
VIDEO_SECONDS = 10
MAX_LINKS = 6                # selected listing + up to 5 validated equivalents
NARRATION_MAX_WORDS = 25
SCRIPT_FIELD_LIMIT = 220
FORBIDDEN_TAGS = {"shopee", "achadinhos", "achadinho", "achados", "viral", "fyp", "foryou",
                  "foryoupage", "parati", "pravoce", "tiktok", "shopeevideo", "promo", "promocao"}
# Claims the model cannot verify from the listing data.
FORBIDDEN_CLAIMS = re.compile(r"R\$|\d+\s*%|\bdesconto|\bgr[aá]tis\b|\bfrete\b|\bpromo[cç][aã]o|\bliquida[cç][aã]o|"
                              r"[uú]ltimas unidades|\bestoque\b|\bs[oó] hoje\b|\bcorra\b|\bcupom", re.I)
HASHTAG = re.compile(r"#[^\W_][^\W]{1,39}")
STOPWORDS = {"com", "para", "sem", "por", "uma", "um", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
             "nos", "nas", "kit", "novo", "nova", "the", "and", "for", "with", "original", "promoção", "oferta",
             "envio", "imediato", "pronta", "entrega", "frete", "grátis", "unidade", "unidades", "pcs", "peças"}


def text_length(value):
    """Telegram and most apps count UTF-16 code units; never shorter than code points."""
    return len(str(value).encode("utf-16-le")) // 2


def caption_text(description, hashtags):
    return " ".join([str(description).strip(), *hashtags]).strip()


def has_emoji(value):
    return any(ord(char) > 0xFFFF or unicodedata.category(char) in ("So", "Cs") for char in value)


def copy_problems(description, hashtags, limit=CAPTION_LIMIT):
    """Return reasons why a caption is not publishable; empty list means valid."""
    problems = []
    if not isinstance(description, str) or not description.strip():
        return ["descrição vazia"]
    if not isinstance(hashtags, list) or not all(isinstance(tag, str) for tag in hashtags):
        return ["hashtags devem ser uma lista de textos"]
    description = description.strip()
    if "\n" in description or "#" in description:
        problems.append("descrição deve ser uma frase única, sem hashtags dentro")
    if description.endswith(("…", "...")):
        problems.append("descrição não pode terminar com reticências")
    if has_emoji(description) or any(has_emoji(tag) for tag in hashtags):
        problems.append("não use emojis")
    if FORBIDDEN_CLAIMS.search(description):
        problems.append("não cite preço, desconto, frete, estoque ou urgência")
    if not 2 <= len(hashtags) <= 3:
        problems.append("use 2 ou 3 hashtags")
    seen = set()
    for tag in hashtags:
        if not HASHTAG.fullmatch(tag):
            problems.append(f"hashtag inválida: {tag}")
            continue
        folded = strip_accents(tag[1:]).casefold()
        if folded in FORBIDDEN_TAGS:
            problems.append(f"hashtag genérica não permitida: {tag}")
        if folded in seen:
            problems.append(f"hashtag repetida: {tag}")
        seen.add(folded)
    total = text_length(caption_text(description, hashtags))
    if total > limit:
        problems.append(f"legenda com {total} caracteres; o máximo é {limit} incluindo hashtags e espaços")
    return problems


def script_problems(script):
    problems = []
    for field in ("hook", "demo", "benefit", "cta", "narration"):
        value = script.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{field} vazio")
        elif len(value) > SCRIPT_FIELD_LIMIT:
            problems.append(f"{field} com mais de {SCRIPT_FIELD_LIMIT} caracteres")
        elif FORBIDDEN_CLAIMS.search(value):
            problems.append(f"{field}: não cite preço, desconto, frete, estoque ou urgência")
    narration = script.get("narration")
    if isinstance(narration, str) and len(narration.split()) > NARRATION_MAX_WORDS:
        problems.append(f"narração com mais de {NARRATION_MAX_WORDS} palavras não cabe em {VIDEO_SECONDS} s")
    return problems


def strip_accents(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def clean_name(name):
    name = re.sub(r"[\[\(【].*?[\]\)】]", " ", str(name or ""))
    return re.sub(r"\s+", " ", name).strip() or "Produto"


def name_tokens(name):
    words = re.findall(r"[^\W_]+", strip_accents(clean_name(name)).casefold())
    return [w for w in words if len(w) >= 2 and w not in STOPWORDS]


MARKETING_WORDS = {"promoção", "promocao", "oferta", "envio", "imediato", "frete", "grátis", "gratis", "pronta",
                   "entrega", "liquidação", "liquidacao", "lançamento", "lancamento", "barato", "menor", "preço",
                   "preco", "brinde", "atacado", "revenda", "full", "24h"}


def display_words(name):
    """Words of the listing name without seller marketing such as 'frete grátis'."""
    return [w for w in clean_name(name).replace("|", " ").replace("/", " / ").split()
            if strip_accents(w.strip(".,;:!-")).casefold() not in {strip_accents(m) for m in MARKETING_WORDS}
            and not FORBIDDEN_CLAIMS.search(w)]


def fit_words(words, limit):
    """Join whole words up to the limit; never slices a word."""
    result = ""
    for word in words:
        candidate = f"{result} {word}".strip()
        if text_length(candidate) > limit:
            break
        result = candidate
    return result


def fallback_copy(product, limit=CAPTION_LIMIT):
    """Deterministic caption used only when every model fails; flagged in the delivery."""
    words = [w for w in re.findall(r"[^\W_]+", " ".join(display_words(product.get("name")))) if len(w) >= 3
             and w.casefold() not in STOPWORDS and not w.isdigit()]
    tags = []
    for word in words:
        tag = "#" + strip_accents(word).capitalize()
        if HASHTAG.fullmatch(tag) and strip_accents(word).casefold() not in FORBIDDEN_TAGS \
                and tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
        if len(tags) == 2:
            break
    while len(tags) < 2:
        tags.append(["#Casa", "#DiaADia"][len(tags)])
    budget = limit - text_length(" ".join(tags)) - 2
    short = fit_words(display_words(product.get("name")), max(10, budget)).rstrip(" ,;:-/|") or "Produto"
    return dict(description=short + ".", hashtags=tags)


def fallback_script(product):
    short = fit_words(display_words(product.get("name")), 60).rstrip(" ,;:-/|") or "produto"
    return dict(hook=f"Close do {short} chamando atenção logo no primeiro segundo.",
                demo="Mão mostrando o produto em uso real, de ângulos diferentes.",
                benefit="Mostre o resultado prático do uso, sem exagerar.",
                cta="Pessoa sorri e aponta para o produto, convidando a tocar nele no vídeo.",
                narration=f"Olha só: {fit_words(short.split(), 40)}. Veja como funciona. Toque no produto do vídeo.")


def video_prompt(script):
    """Assemble the Flow prompt; fixed rules keep the reference product intact."""
    return "\n".join([
        f"Vídeo publicitário vertical 9:16 de {VIDEO_SECONDS} segundos do produto da imagem de referência.",
        "Preserve exatamente formato, cores, proporções e componentes do produto. Não acrescente acessórios.",
        f"0–2 s, gancho: {script['hook'].strip()}",
        f"2–6 s, demonstração: {script['demo'].strip()}",
        f"6–8 s, benefício: {script['benefit'].strip()}",
        f"8–10 s, chamada: {script['cta'].strip()}",
        f"Narração em português do Brasil, voz natural e animada: \"{script['narration'].strip()}\"",
        "Sem textos, legendas, logotipos ou marcas d'água na imagem. Sem preços, descontos ou promessas.",
    ])


def money(value):
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class CaptionTooLong(ValueError):
    """The single video message cannot carry every approved link within Telegram's caption limit."""


UNVALIDATED = "Aviso: texto automático, sem validação do modelo."


def video_caption(product):
    """HTML caption of the single Telegram message: short name, copyable description and numbered links.

    Links are never cut or dropped: identification is shortened first, and if even that does not fit,
    the delivery is refused so the panel can ask for a decision."""
    analysis = product.get("analysis") or {}
    copy = analysis.get("caption") or caption_text(**fallback_copy(product))
    links = [entry["link"] for entry in analysis.get("links") or [] if entry.get("link")] or [product["link"]]
    body = [f"<pre>{html.escape(copy)}</pre>"]
    if analysis.get("status") != "ok":
        body.append(html.escape(UNVALIDATED))
    body += ["Links:"] + [f"{i}. {html.escape(link)}" for i, link in enumerate(links, 1)]
    words = display_words(product.get("name"))
    for limit in (60, 30, 0):
        name = fit_words(words, limit).rstrip(" ,;:-/|") if limit else ""
        caption = "\n".join(([f"<b>{html.escape(name)}</b>"] if name else []) + body)
        if text_length(plain(caption)) <= TELEGRAM_CAPTION_LIMIT:
            return caption
    raise CaptionTooLong(f"A mensagem com {len(links)} link(s) passa de {TELEGRAM_CAPTION_LIMIT} caracteres, "
                         "o limite da legenda do vídeo no Telegram. Nada foi cortado; decida no painel.")


def plain(html_text):
    """Text Telegram counts after parsing the HTML entities."""
    return html.unescape(re.sub(r"</?(b|pre)>", "", html_text))
