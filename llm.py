"""Vision/text model chain: Antigravity CLI through the VM worker, OpenAI-compatible endpoints directly."""
import base64
import json
import re
import secrets
import struct
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlparse

PROVIDER_TYPES = {"agy", "openai"}
AGY_MODELS = ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
              "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high",
              "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-sonnet-5-5-high",
              "claude-opus-5-5-low", "claude-opus-5-5-medium", "claude-opus-5-5-high", "gpt-oss-120b-medium"]
# candidate_budget is the batch size of each model call (0 disables the search); candidate_total caps all rounds.
SEARCH_LIMITS = (("candidate_budget", 0, 15), ("candidate_total", 1, 60), ("search_queries", 1, 3), ("search_pages", 1, 3),
                 ("search_calls", 1, 9), ("equivalence_rounds", 1, 5), ("search_deadline", 60, 1800))
LLM_DEFAULTS = dict(timeout=180, retries=1, candidate_budget=8, candidate_total=24, search_queries=3, search_pages=2,
                    search_calls=6, equivalence_rounds=3, search_deadline=600, providers=[
    dict(id="agy-flash", type="agy", label="Antigravity · Gemini 3.8 Flash", model="gemini-3.8-flash-medium",
         endpoint="", api_key="", text_model="", enabled=True),
    dict(id="agy-argon", type="agy", label="Antigravity · Gemini 4 Argon", model="gemini-4-argon-medium",
         endpoint="", api_key="", text_model="", enabled=True),
])
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@+-]{1,120}")
WORKER_ONLINE_SECONDS = 90
MIN_CALL_SECONDS = 30  # Below this, a model call cannot finish: stop instead of starting it.
IMAGE_HOSTS = ("susercontent.com", "shopee.com.br", "shopee.com", "img.shopee")
IMAGE_LIMIT = 6 * 1024 * 1024


class ModelError(Exception):
    """A provider attempt failed; the chain continues with the next one."""


def validate_llm(payload, existing=None):
    existing = existing or LLM_DEFAULTS
    if not isinstance(payload, dict) or set(payload) - set(LLM_DEFAULTS):
        raise ValueError("Configuração de modelo inválida.")
    result = {key: payload.get(key, existing.get(key, LLM_DEFAULTS[key])) for key in LLM_DEFAULTS}
    for key, low, high in (("timeout", 30, 900), ("retries", 0, 3), *SEARCH_LIMITS):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not low <= value <= high:
            raise ValueError(f"{key}: use um inteiro de {low} a {high}.")
        result[key] = int(value)
    providers = result["providers"]
    if not isinstance(providers, list) or not 1 <= len(providers) <= 6:
        raise ValueError("Informe de 1 a 6 modelos (principal e fallbacks).")
    previous = existing.get("providers")
    saved = {p["id"]: p for p in (previous if isinstance(previous, list) else [])}
    clean, ids = [], set()
    for index, provider in enumerate(providers, 1):
        if not isinstance(provider, dict) or set(provider) - {"id", "type", "label", "model", "endpoint", "api_key", "text_model", "enabled"}:
            raise ValueError(f"Modelo {index}: campos inválidos.")
        values = {k: provider.get(k, "") for k in ("id", "type", "label", "model", "endpoint", "api_key", "text_model")}
        if any(not isinstance(v, str) for v in values.values()) or type(provider.get("enabled", True)) is not bool:
            raise ValueError(f"Modelo {index}: use texto nos campos.")
        values = {k: v.strip() for k, v in values.items()}
        pid = values["id"] if re.fullmatch(r"[a-z0-9-]{3,40}", values["id"]) else "m-" + secrets.token_hex(4)
        if pid in ids:
            pid = "m-" + secrets.token_hex(4)
        ids.add(pid)
        if values["type"] not in PROVIDER_TYPES:
            raise ValueError(f"Modelo {index}: tipo deve ser Antigravity (agy) ou OpenAI-compatível.")
        if not MODEL_NAME.fullmatch(values["model"]):
            raise ValueError(f"Modelo {index}: nome do modelo inválido.")
        if values["text_model"] and not MODEL_NAME.fullmatch(values["text_model"]):
            raise ValueError(f"Modelo {index}: modelo de texto inválido.")
        if len(values["label"]) > 80:
            raise ValueError(f"Modelo {index}: nome de exibição com até 80 caracteres.")
        key = values["api_key"] or saved.get(pid, {}).get("api_key", "")
        if values["type"] == "openai":
            endpoint = values["endpoint"].rstrip("/")
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or len(endpoint) > 300:
                raise ValueError(f"Modelo {index}: endpoint deve ser uma URL http(s), ex.: https://openrouter.ai/api/v1.")
            if len(key) > 512:
                raise ValueError(f"Modelo {index}: chave muito longa.")
        else:
            endpoint, key = "", ""
        clean.append(dict(id=pid, type=values["type"], label=values["label"] or values["model"], model=values["model"],
                          endpoint=endpoint, api_key=key, text_model=values["text_model"],
                          enabled=provider.get("enabled", True)))
    if not any(p["enabled"] for p in clean):
        raise ValueError("Deixe pelo menos um modelo ativo.")
    result["providers"] = clean
    return result


def public_llm(config):
    providers = [dict({k: v for k, v in p.items() if k != "api_key"}, key_saved=bool(p["api_key"]))
                 for p in config["providers"]]
    return dict(config, providers=providers, agy_models=AGY_MODELS)


def extract_json(text):
    text = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ModelError("Resposta sem JSON.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ModelError("JSON inválido na resposta do modelo.") from None


def fetch_image(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not any(parsed.hostname and parsed.hostname.endswith(h) for h in IMAGE_HOSTS):
        raise ModelError("Imagem fora dos domínios da Shopee.")
    request = urllib.request.Request(url, headers={"User-Agent": "ShopeeStudio/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        kind = response.headers.get_content_type()
        data = response.read(IMAGE_LIMIT + 1)
    if not kind.startswith("image/") or len(data) > IMAGE_LIMIT:
        raise ModelError("Imagem inválida ou grande demais.")
    return kind, data


def test_image():
    """64x64 solid red PNG built in memory: proves the model reads image input."""
    width = height = 64
    raw = b"".join(b"\x00" + b"\xd0\x10\x10" * width for _ in range(height))
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Runner:
    """Walk the configured chain: main model first, then each enabled fallback."""

    def __init__(self, store, http=None, image_fetcher=fetch_image, poll=1.0):
        self.store, self.http, self.image_fetcher, self.poll = store, http or self._http, image_fetcher, poll

    @staticmethod
    def _http(url, body, headers, timeout):
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def run(self, prompt, schema, images, check, log=None, deadline=None):
        """Return (output, provider_label, attempts). Raises ModelError when the whole chain fails.
        `deadline` (epoch seconds) bounds every attempt, retry and fallback; it is never extended."""
        config = self.store.llm()
        attempts = []
        for provider in [p for p in config["providers"] if p["enabled"]]:
            feedback = ""
            for attempt in range(config["retries"] + 1):
                timeout = config["timeout"]
                if deadline is not None:
                    left = int(deadline - time.time())
                    if left < MIN_CALL_SECONDS:
                        attempts.append("prazo da análise esgotado; nenhuma nova chamada")
                        raise ModelError("Prazo da análise esgotado: " + " | ".join(attempts)[:900])
                    timeout = min(timeout, left)
                try:
                    output = self.call(provider, prompt + feedback, schema, images, timeout, deadline)
                    problems = check(output)
                    if not problems:
                        attempts.append(f"{provider['label']}: ok")
                        return output, provider["label"], attempts
                    attempts.append(f"{provider['label']}: resposta recusada ({'; '.join(problems)[:300]})")
                    feedback = ("\n\nSUA RESPOSTA ANTERIOR FOI RECUSADA PELA VALIDAÇÃO AUTOMÁTICA: "
                                + "; ".join(problems) + ". Corrija e responda novamente só com o JSON completo.")
                except (WorkerOffline, ProviderRefused) as error:
                    attempts.append(f"{provider['label']}: {error}")
                    break  # Retrying the same provider cannot help: go to the next one.
                except Exception as error:  # Any provider failure falls through to the next attempt.
                    attempts.append(f"{provider['label']}: {str(error)[:200]}")
                if log:
                    log(attempts[-1])
        raise ModelError("Todos os modelos falharam: " + " | ".join(attempts)[:900])

    def call(self, provider, prompt, schema, images, timeout, deadline=None):
        if provider["type"] == "agy":
            return self.call_worker(provider, prompt, schema, images, timeout, deadline)
        return self.call_openai(provider, prompt, images, timeout)

    def call_openai(self, provider, prompt, images, timeout):
        content = [{"type": "text", "text": prompt + ("\n\nAs imagens anexadas seguem esta ordem: "
                                                       + ", ".join(i["name"] for i in images) if images else "")}]
        for image in images:
            if image.get("data"):
                kind, data = image.get("type", "image/png"), base64.b64decode(image["data"])
            else:
                kind, data = self.image_fetcher(image["url"])
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{kind};base64,{base64.b64encode(data).decode()}"}})
        model = provider["model"] if images or not provider["text_model"] else provider["text_model"]
        body = json.dumps({"model": model, "temperature": 0.4,
                           "messages": [{"role": "user", "content": content if images else content[0]["text"]}]}).encode()
        headers = {"Authorization": f"Bearer {provider['api_key']}"} if provider["api_key"] else {}
        try:
            result = self.http(provider["endpoint"] + "/chat/completions", body, headers, timeout)
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500 and error.code not in (408, 429):
                raise ProviderRefused(f"HTTP {error.code} do endpoint (confira chave, modelo e endpoint).") from None
            raise ModelError(f"HTTP {error.code} do endpoint.") from None
        except (OSError, TimeoutError):
            raise ModelError("Endpoint indisponível ou sem resposta no prazo.") from None
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("Resposta OpenAI-compatível sem conteúdo.") from None
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return extract_json(text)

    def call_worker(self, provider, prompt, schema, images, timeout, deadline=None):
        if not self.store.worker_online():
            raise WorkerOffline("worker da VM offline; pulando para o próximo modelo")
        task_id = self.store.create_task("llm", dict(model=provider["model"], prompt=prompt, schema=schema,
                                                       images=images, timeout=timeout), deadline=timeout + 120)
        # Queue and agy start-up get 120 s on top of the call timeout, but never past the analysis deadline.
        limit = time.time() + timeout + 120 if deadline is None else min(time.time() + timeout + 120, deadline)
        while time.time() < limit:
            task = self.store.task(task_id)
            if task["status"] == "done":
                output = task["result"].get("output")
                if not isinstance(output, dict):
                    raise ModelError("Worker devolveu resposta sem JSON.")
                return output
            if task["status"] == "failed":
                raise ModelError("agy: " + str(task["error"] or "falha sem detalhe")[:200])
            if task["status"] == "queued" and not self.store.worker_online():
                self.store.expire_task(task_id)
                raise WorkerOffline("worker da VM ficou offline antes de assumir a tarefa")
            time.sleep(self.poll)
        self.store.expire_task(task_id)
        raise ModelError("agy não respondeu no prazo.")

    def test(self, provider_id):
        config = self.store.llm()
        provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
        if not provider:
            raise ValueError("Modelo não encontrado; salve a configuração antes de testar.")
        image = dict(name="TESTE", type="image/png", data=base64.b64encode(test_image()).decode())
        schema = {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]}
        prompt = ("Teste de visão. Olhe a imagem TESTE e responda só JSON {\"color\": \"<cor predominante em português>\"}. "
                  "Não use ferramentas de terminal.")
        started = time.time()
        output = self.call(provider, prompt, schema, [image], min(config["timeout"], 240))
        color = str(output.get("color", "")).casefold()
        if "vermelh" not in color and "red" not in color:
            raise ModelError(f"o modelo respondeu, mas não reconheceu a imagem (disse: {color[:40] or 'vazio'}).")
        return f"{provider['label']}: visão confirmada em {time.time() - started:.0f} s (imagem vermelha reconhecida)."


class WorkerOffline(ModelError):
    pass


class ProviderRefused(ModelError):
    """Authentication or request refused (4xx): retrying the same provider is pointless."""
