#!/usr/bin/env python3
"""Shopee Studio worker for the hermes VM: Antigravity CLI (agy) tasks and Google Flow videos.

Model tasks need only the standard library. Video tasks need the worker venv (Playwright + faster-whisper),
imported lazily. Two independent loops run, so a long video never delays the daily product analysis.
Configuration (first match wins):
  env SHOPEE_STUDIO_URL / SHOPEE_STUDIO_WORKER_TOKEN
  ~/.config/shopee-studio-worker/config.json  {"panel_url": "...", "token": "...", "flow_profile": "...",
                                               "display": ":95", "whisper_model": "small"}
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

VERSION = "1.2.0"
CONFIG = Path.home() / ".config" / "shopee-studio-worker" / "config.json"
WORK = Path.home() / ".cache" / "shopee-studio-worker"
IMAGE_HOSTS = ("susercontent.com", "shopee.com.br", "shopee.com", "img.shopee")
IMAGE_LIMIT = 6 * 1024 * 1024
IDLE_SECONDS = 5


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


DEFAULT_PROFILE = Path.home() / ".local" / "share" / "flow-vm-test" / "profile"


def load_config():
    config = {}
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
    url = os.environ.get("SHOPEE_STUDIO_URL") or config.get("panel_url", "")
    token = os.environ.get("SHOPEE_STUDIO_WORKER_TOKEN") or config.get("token", "")
    if not url or not token:
        sys.exit(f"Configure panel_url e token em {CONFIG} (modo 600).")
    config.update(panel_url=url.rstrip("/"), token=token)
    config.setdefault("flow_profile", str(DEFAULT_PROFILE))
    config.setdefault("display", ":95")
    config.setdefault("whisper_model", "small")
    return config


def call(url, token, path, payload, timeout=30):
    request = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def save_image(image, folder):
    name = "".join(c for c in str(image.get("name", "img")) if c.isalnum())[:12] or "img"
    if image.get("data"):
        data = base64.b64decode(image["data"])
        ext = ".jpg" if data[:2] == b"\xff\xd8" else ".png"  # fidelity frames are JPEG
    else:
        parsed = urlparse(image.get("url", ""))
        if parsed.scheme != "https" or not any((parsed.hostname or "").endswith(h) for h in IMAGE_HOSTS):
            raise ValueError(f"imagem {name} fora dos domínios da Shopee")
        request = urllib.request.Request(image["url"], headers={"User-Agent": "ShopeeStudioWorker/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            kind = response.headers.get_content_type()
            data = response.read(IMAGE_LIMIT + 1)
        if not kind.startswith("image/") or len(data) > IMAGE_LIMIT:
            raise ValueError(f"imagem {name} inválida")
        ext = {"image/png": ".png", "image/webp": ".webp"}.get(kind, ".jpg")
    path = folder / (name + ext)
    path.write_bytes(data)
    return name, path


def run_llm(payload, seconds_left):
    agy = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    timeout = max(30, min(int(payload.get("timeout", 180)), seconds_left - 15))
    WORK.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="task-", dir=WORK))
    try:
        mapping = []
        for image in payload.get("images") or []:
            try:
                name, path = save_image(image, folder)
                mapping.append(f"Imagem {name}: {path}")
            except Exception as error:  # A missing candidate image only weakens that comparison.
                mapping.append(f"Imagem {image.get('name')}: indisponível ({error})")
        prompt = payload["prompt"] + ("\n\nARQUIVOS DE IMAGEM (somente leitura):\n" + "\n".join(mapping) if mapping else "")
        schema = folder / "schema.json"
        schema.write_text(json.dumps(payload["schema"]), encoding="utf-8")
        # No --dangerously-skip-permissions: listing titles are untrusted, so terminal tools stay denied.
        command = [agy, "-p", prompt, "--model", payload["model"], "--add-dir", str(folder),
                   "--output-format", "json", "--json-schema", str(schema), "--print-timeout", f"{timeout}s"]
        process = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=timeout + 30)
        if process.returncode != 0:
            raise RuntimeError(f"agy saiu com código {process.returncode}: {process.stderr.strip()[-300:]}")
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("agy não devolveu JSON: " + (process.stderr.strip()[-300:] or process.stdout[:200])) from None
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"agy status {result.get('status')}: {process.stderr.strip()[-300:]}")
        output = result.get("structured_output")
        if not isinstance(output, dict):
            raise RuntimeError("agy respondeu sem saída estruturada.")
        return output
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def upload_video(url, token, task_id, worker_id, path):
    request = urllib.request.Request(f"{url}/api/worker/tasks/{task_id}/video", data=Path(path).read_bytes(), method="POST",
                                     headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {token}",
                                              "X-Worker-Id": worker_id})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def run_video(config, task, worker_id):
    """Generate in Flow, subtitle the narration, upload the MP4. Returns metadata; raises FlowError."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from flow import Flow, FlowError
    import subtitles
    url, token, payload = config["panel_url"], config["token"], task["payload"]
    progress = lambda stage, **extra: call(url, token, f"/api/worker/tasks/{task['id']}/progress",
                                           dict(worker_id=worker_id, stage=stage, **extra))
    WORK.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="video-", dir=WORK))
    charged = False
    try:
        progress("working")
        _, image = save_image(dict(name=f"produto{task['id']}", url=payload["image_url"]), folder)
        def stage(name, **extra):
            nonlocal charged
            charged = charged or name in ("submitted", "downloading")
            try:
                progress(name, **extra)
            except Exception as error:  # Progress is informative after approval; the job goes on.
                log(f"Tarefa {task['id']}: progresso '{name}' não registrado ({error})")
        def before_approve(cost):
            nonlocal charged
            progress("approving", credits=cost)  # Raises if the panel refuses: the generation is then rejected.
            charged = True
        with Flow(config["flow_profile"], config["display"], log=log) as flow:
            try:
                raw = flow.generate(payload["project_url"], image, payload["prompt"], int(payload["expected_cost"]),
                                    payload["model"], before_approve, stage, folder)
            except Exception:
                flow.screenshot(WORK / "ultimo-erro.png")
                raise
        finish = payload.get("finish") or None  # Modern mode: badge, 1080x1920 and fidelity frames
        if finish and finish.get("frames"):
            try:
                frames = subtitles.extract_frames(raw, folder)
                call(url, token, f"/api/worker/tasks/{task['id']}/frames",
                     dict(worker_id=worker_id, frames=[base64.b64encode(f.read_bytes()).decode() for f in frames]))
            except Exception as error:  # The fidelity check is advisory: the video still goes on.
                log(f"Tarefa {task['id']}: quadros para a fidelidade não enviados ({error})")
        stage("subtitling")
        final = folder / "final.mp4"
        meta = subtitles.subtitle_video(raw, payload.get("narration", ""), final, folder, config["whisper_model"],
                                        finish=finish)
        if not 6 <= meta["duration"] <= 20 or meta["height"] <= meta["width"]:
            raise FlowError("render_failed", f"Vídeo fora do esperado: {meta['width']}x{meta['height']}, "
                                             f"{meta['duration']:.1f} s.", charged=True)
        stage("uploading")
        upload_video(url, token, task["id"], worker_id, final)
        return meta
    except FlowError as error:
        error.charged = error.charged or charged
        raise
    except Exception as error:
        raise VideoError("download_failed" if charged else "ui_changed", str(error)[:400], charged) from error
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class VideoError(Exception):
    def __init__(self, code, message, charged):
        super().__init__(message)
        self.code, self.charged = code, charged


def heartbeat(url, token, worker_id, done):
    """Keep the worker visible to the panel while a long agy call runs."""
    while not done.wait(30):
        try:
            call(url, token, "/api/worker/heartbeat", dict(worker_id=worker_id), timeout=10)
        except Exception:
            pass


def loop(config, kind, worker_id):
    url, token = config["panel_url"], config["token"]
    while True:
        try:
            task = call(url, token, "/api/worker/claim", dict(worker_id=worker_id, version=VERSION, kinds=[kind]))["task"]
        except urllib.error.HTTPError as error:
            log(f"Painel recusou ({error.code}); confira o token.")
            time.sleep(60)
            continue
        except (OSError, ValueError) as error:
            log(f"Painel indisponível: {error}")
            time.sleep(30)
            continue
        if not task:
            time.sleep(IDLE_SECONDS)
            continue
        started = time.time()
        log(f"Tarefa {task['id']} ({task['kind']}, {task['payload'].get('model')})")
        busy = threading.Event()
        threading.Thread(target=heartbeat, args=(url, token, worker_id, busy), daemon=True).start()
        try:
            if kind == "video":
                output = run_video(config, task, worker_id)
            else:
                output = run_llm(task["payload"], task["seconds_left"])
            report = dict(worker_id=worker_id, ok=True, output=output)
        except Exception as error:
            report = dict(worker_id=worker_id, ok=False, error=str(error)[:480], code=getattr(error, "code", ""),
                          charged=bool(getattr(error, "charged", False)))
        finally:
            busy.set()
        try:
            call(url, token, f"/api/worker/tasks/{task['id']}/result", report)
            log(f"Tarefa {task['id']}: {'ok' if report['ok'] else report['error']} em {time.time() - started:.0f} s")
        except Exception as error:
            log(f"Tarefa {task['id']}: resultado não aceito ({error})")


def video_ready():
    try:
        import importlib.util
        return all(importlib.util.find_spec(m) for m in ("playwright", "faster_whisper"))
    except Exception:
        return False


def main():
    config = load_config()
    host = socket.gethostname()[:50]
    log(f"Worker {VERSION} em {host}; painel {config['panel_url']}")
    loops = [("llm", host)]
    if video_ready():
        loops.append(("video", host + ".video"))
    else:
        log("Playwright/faster-whisper ausentes neste Python: só tarefas de modelo (rode worker/install.sh).")
    threads = [threading.Thread(target=loop, args=(config, kind, worker_id), name=kind, daemon=True)
               for kind, worker_id in loops]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
