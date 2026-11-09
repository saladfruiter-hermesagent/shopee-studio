"""Google Flow automation for the VM worker (Playwright, dedicated persistent Chromium profile).

Flow is driven through its project agent: the prompt goes in the prompt box with the product image
attached, the agent answers with a permission card ("... custa N créditos?"), and only an exact
"Aprovar" click spends credits. Every step before that click is free and safe to abort.
"""
import os
import re
import subprocess
import time
from pathlib import Path

CHROME = Path.home() / ".cache" / "ms-playwright" / "chromium-1234" / "chrome-linux64" / "chrome"
PROMPT_BOX = 'div[role="textbox"][contenteditable="true"]'
PROMPT_BOX_VISIBLE = PROMPT_BOX + ":visible"
COST = re.compile(r"(\d+)\s*cr[eé]ditos?", re.I)
NO_CREDITS = re.compile(r"cr[eé]ditos? insuficientes|n[aã]o (tem|há) cr[eé]ditos|sem cr[eé]ditos|limite de cr[eé]ditos", re.I)
FAILED = re.compile(r"n[aã]o (foi|consegui) (poss[ií]vel )?gerar|falh(a|ou) na gera|viola|pol[ií]tica|n[aã]o posso gerar", re.I)
FAILED_CARD = re.compile(r"^\s*warning|falha|algo deu errado|tente de novo", re.I)
CHALLENGE = re.compile(r"n[aã]o sou um rob[oô]|unusual traffic|tr[aá]fego incomum|captcha|verify it'?s you|confirme que [eé] voc[eê]", re.I)
COST_PANEL = re.compile(r"usar\s+(\d+)\s*cr[eé]ditos", re.I)


class FlowError(Exception):
    """`code` tells the panel what to do; `charged` means credits may already be spent."""

    def __init__(self, code, message, charged=False):
        super().__init__(message)
        self.code, self.charged = code, charged


def ensure_display(display):
    number = display.lstrip(":")
    lock = Path(f"/tmp/.X{number}-lock")
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip()), 0)
            return None  # A live X server already owns this display.
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # Stale lock from a crashed server: Xvfb below refuses to start if it is really in use.
    process = subprocess.Popen(["Xvfb", display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{number}").exists() and process.poll() is None:
            return process
        time.sleep(0.1)
    raise FlowError("ui_changed", "Xvfb não iniciou.")


class Flow:
    def __init__(self, profile, display=":95", log=print):
        self.profile, self.display, self.log = str(profile), display, log
        self.playwright = self.context = self.page = None
        self.project_url = ""
        self.new_thumbs = []

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # Only the VM worker venv has Playwright.
        ensure_display(self.display)
        self.playwright = sync_playwright().start()
        try:
            # --no-sandbox only for this dedicated profile: approved by the user (Ubuntu blocks userns sandboxing).
            self.context = self.playwright.chromium.launch_persistent_context(
                self.profile, executable_path=str(CHROME) if CHROME.exists() else None, headless=False,
                args=["--no-sandbox", "--window-size=1440,900", "--no-first-run", "--no-default-browser-check"],
                viewport={"width": 1440, "height": 900}, accept_downloads=True, locale="pt-BR",
                env={**os.environ, "DISPLAY": self.display})
        except Exception as error:
            self.playwright.stop()
            busy = "ProcessSingleton" in str(error) or "SingletonLock" in str(error) or "in use" in str(error)
            raise FlowError("browser_busy" if busy else "ui_changed",
                            "Perfil do Flow em uso por outro navegador (login em andamento?)." if busy
                            else f"Chromium não abriu: {str(error)[:200]}") from None
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(30000)
        return self

    def __exit__(self, *exc):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()

    # ---------- helpers ----------
    def screenshot(self, path):
        try:
            self.page.screenshot(path=str(path))
        except Exception:
            pass

    def text(self):
        return self.page.evaluate("document.body ? document.body.innerText : ''")

    def click_label(self, label, last=True, timeout=10):
        """Click a button by aria-label through the DOM (hover-only controls report as not visible)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            done = self.page.evaluate("""([label, last]) => {
                const all = [...document.querySelectorAll('button')].filter(b => b.getAttribute('aria-label') === label && !b.hidden);
                const visible = all.filter(b => b.offsetParent !== null);
                const target = (visible.length ? visible : all)[last ? (visible.length ? visible : all).length - 1 : 0];
                if (!target) return false; target.click(); return true; }""", [label, last])
            if done:
                return
            time.sleep(0.5)
        raise FlowError("ui_changed", f"Botão '{label}' não encontrado no Flow.")

    def check_session(self):
        url = self.page.url
        if "accounts.google.com" in url or "/signin" in url:
            raise FlowError("login_required", "Sessão do Google expirada: faça login no navegador da VM.")
        if CHALLENGE.search(self.text()):
            raise FlowError("challenge", "O Google pediu verificação humana (CAPTCHA ou confirmação de identidade).")

    # ---------- steps ----------
    def open_project(self, url):
        if not re.match(r"^https://(flow\.google\.com|labs\.google)/", url or ""):
            raise FlowError("no_project", "Configure a URL do projeto do Flow nas Preferências.")
        self.project_url = url
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        deadline = time.time() + 90
        while time.time() < deadline:
            self.check_session()
            if self.page.locator(PROMPT_BOX_VISIBLE).count():
                return
            if self.page.locator('a[href*="accounts.google.com/ServiceLogin"], a[href*="signin"]').count():
                raise FlowError("login_required", "Flow aberto sem login: faça login no navegador da VM.")
            time.sleep(1)
        raise FlowError("ui_changed", "Caixa de comando do Flow não apareceu.")

    def open_settings(self):
        """Abre o painel do compositor (UI nova: 'Gatilho de configurações'), se ainda não estiver aberto."""
        opened = self.page.evaluate("""() => {
            const panes = [...document.querySelectorAll('.cdk-overlay-container .cdk-overlay-pane')];
            if (panes.some(p => p.offsetParent !== null && [...p.querySelectorAll('button[role=radio]')]
                    .some(b => /^(360p|720p|4s|6s|8s|10s)$/.test(b.innerText.trim())))) return 'aberto';
            const button = [...document.querySelectorAll('button')]
                .find(b => b.getAttribute('aria-label') === 'Gatilho de configurações'
                        && b.closest('flow-base-prompt-box'));
            if (!button) return 'ausente';
            button.click(); return 'clique'; }""")
        if opened == "ausente":
            raise FlowError("ui_changed", "Botão de configurações da caixa de comando não encontrado.")
        self.settings_pane()

    def settings_pane(self, timeout=20):
        """Painel do compositor que mostra resolução/duração e o custo da geração."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            handle = self.page.evaluate_handle("""() => {
                const panes = [...document.querySelectorAll('.cdk-overlay-container .cdk-overlay-pane')]
                    .filter(p => p.offsetParent !== null);
                return panes.find(p => [...p.querySelectorAll('button[role=radio]')]
                    .some(b => /^(360p|720p|4s|6s|8s|10s)$/.test(b.innerText.trim()))) || null; }""")
            pane = handle.as_element()
            if pane:
                return pane
            time.sleep(0.5)
        raise FlowError("ui_changed", "Painel de configurações da caixa de comando não abriu.")

    def pick(self, pane, label):
        """Marca uma opção do painel. Clica no button[role=radio] de verdade: o clique via JS é ignorado."""
        handle = pane.evaluate_handle("""(pane, label) => [...pane.querySelectorAll('button[role=radio]')]
            .find(b => b.innerText.trim().endsWith(label)) || null""", label)
        button = handle.as_element()
        if not button:
            raise FlowError("ui_changed", f"Opção '{label}' não encontrada nas configurações do Flow.")
        if button.get_attribute("aria-checked") == "true":
            return
        button.click()
        time.sleep(0.5)
        if button.get_attribute("aria-checked") != "true":
            raise FlowError("ui_changed", f"O Flow não marcou a opção '{label}'.")

    def agent_off(self):
        """Garante o modo Agente DESLIGADO: só assim o compositor mostra a chave Imagem/Vídeo.

        Com o Agente ligado o Flow não oferece a escolha de gerar vídeo e o resultado sai errado,
        então o worker confere o estado do botão em toda tarefa e desliga se estiver ligado.
        """
        for _ in range(4):
            state = self.page.evaluate("""() => {
                const chip = document.querySelector('flow-agent-mode-toggle-chip button');
                const box = document.querySelector('flow-base-prompt-box');
                const botao = chip || (box ? [...box.querySelectorAll('button')]
                    .find(b => (b.getAttribute('aria-label') || b.innerText.trim()) === 'Agente') : null);
                if (!botao) return 'ausente';
                return botao.getAttribute('aria-pressed') === 'true' ? 'ligado' : 'desligado';
            }""")
            if state == "ausente":
                raise FlowError("ui_changed", "Botão do modo Agente não encontrado no compositor do Flow.")
            if state == "desligado":
                return
            self.page.evaluate("""() => {
                const chip = document.querySelector('flow-agent-mode-toggle-chip button');
                const box = document.querySelector('flow-base-prompt-box');
                const botao = chip || (box ? [...box.querySelectorAll('button')]
                    .find(b => (b.getAttribute('aria-label') || b.innerText.trim()) === 'Agente') : null);
                if (botao) botao.click();
            }""")
            time.sleep(3)
        raise FlowError("ui_changed", "Não consegui desligar o modo Agente do Flow; sem isso não há chave de vídeo.")

    def ensure_settings(self, model, expected_cost=None):
        """Agente desligado + compositor em Vídeo, 9:16, 720p, 10 s, x1 e modelo esperado.

        Devolve o custo que o Flow mostra para a geração. Com o Agente desligado não existe cartão
        de confirmação: os créditos saem no envio, então quem chama avisa o painel antes.
        """
        self.agent_off()  # Sem isso o compositor não oferece a chave Imagem/Vídeo.
        self.open_settings()
        pane = self.settings_pane()
        if not pane.evaluate("""(pane) => [...pane.querySelectorAll('button[role=radio]')]
                .some(b => b.innerText.trim().endsWith('Vídeo'))"""):
            raise FlowError("ui_changed", "O painel do compositor não oferece a opção Vídeo "
                                          "(modo Agente ainda ativo?).")
        for label in ("Vídeo", "9:16", "720p", "10s", "x1"):
            self.pick(pane, label)
        state = pane.evaluate("""(pane) => ({
            modelo: (pane.querySelector('button[aria-label="Selecionar família de modelos"]')?.innerText || '')
                .replace('arrow_drop_down', '').trim(),
            video: (() => { const b = [...pane.querySelectorAll('button[role=radio]')]
                .find(b => b.innerText.trim().endsWith('Vídeo')); return b ? b.getAttribute('aria-checked') : null; })(),
            text: pane.innerText})""")
        if state.get("video") != "true":
            raise FlowError("ui_changed", "O Flow não marcou a opção Vídeo no painel do compositor.")
        if model.casefold() not in state["modelo"].casefold():
            raise FlowError("model_mismatch", f"Modelo de vídeo no compositor é '{state['modelo']}', esperado '{model}'.")
        cost = COST_PANEL.search(state["text"])
        if not cost:
            raise FlowError("ui_changed", "O Flow não mostrou o custo da geração no compositor.")
        if expected_cost is not None and int(cost.group(1)) != int(expected_cost):
            raise FlowError("cost_mismatch", f"O compositor do Flow estima {cost.group(1)} créditos; o esperado era "
                                             f"{expected_cost}. Nada foi gerado.")
        self.page.keyboard.press("Escape")  # O painel salva conforme muda; fechar libera a caixa de comando.
        time.sleep(1)
        return int(cost.group(1))

    def new_session(self):
        """Abre uma sessão nova para o agente não herdar cartões/vídeos da tarefa anterior."""
        try:
            self.click_label("Iniciar nova sessão", timeout=5)
            time.sleep(2)
        except FlowError:
            self.log("Flow: botão de nova sessão ausente; seguindo na sessão atual.")
        for _ in range(4):
            try:
                self.page.wait_for_selector(PROMPT_BOX_VISIBLE, timeout=8000)
                return
            except Exception:
                self.page.keyboard.press("Escape")  # Um painel aberto deixa a caixa oculta.
                time.sleep(1)
        raise FlowError("ui_changed", "Caixa de comando do Flow não ficou visível.")

    def attach_image(self, image):
        name = Path(image).name
        self.click_label("Adicionar elementos à caixa de comando", last=False)
        upload = self.page.locator(".cdk-overlay-container button", has_text="Enviar mídia").first
        upload.wait_for(timeout=15000)
        with self.page.expect_file_chooser(timeout=15000) as chooser:
            upload.click()
        chooser.value.set_files(str(image))
        item = self.page.locator(".cdk-overlay-container button", has_text=name).first
        item.wait_for(timeout=90000)
        for _ in range(60):  # The item exists while still uploading; wait until it is clickable.
            if self.page.evaluate("""(name) => { const b = [...document.querySelectorAll('.cdk-overlay-container button')]
                .find(b => b.innerText.includes(name)); return !!b && !b.disabled && !b.querySelector('mat-progress-spinner, [role=progressbar]'); }""", name):
                break
            time.sleep(1)
        item.click()
        # O picker novo insere a imagem direto no comando; versões anteriores pediam "Incluir no comando".
        include = self.page.locator(".cdk-overlay-container button", has_text="Incluir no comando")
        try:
            include.first.wait_for(timeout=8000)
        except Exception:
            include = None
        if include is not None:
            for _ in range(3):  # O botão é re-renderizado enquanto a imagem sobe e pode sair do DOM no clique.
                try:
                    include.first.click(timeout=8000)
                    break
                except Exception:
                    self.page.wait_for_timeout(1000)
        for _ in range(30):
            if self.page.evaluate("document.querySelectorAll('flow-creative-agent-prompt-box img, flow-base-prompt-box img').length"):
                return
            time.sleep(1)
        raise FlowError("ui_changed", "A imagem não entrou na caixa de comando.")

    def tiles(self):
        return self.page.evaluate("document.querySelectorAll('flow-video-tile').length")

    def submit(self, prompt):
        """Envia o comando e confirma pelo estado do compositor.

        O Flow novo mantém o clique em "Iniciar geração" e não aceita Enter de forma confiável; a
        confirmação é: caixa de texto vazia, botão de gerar desativado, ou uma geração nova
        aparecendo ("Gerando" / tile novo). Nunca reenvia sem antes não haver sinal algum.
        """
        box = self.page.locator(PROMPT_BOX_VISIBLE).first
        box.click()
        self.page.keyboard.insert_text(prompt)
        time.sleep(2)

        def estado():
            return self.page.evaluate("""() => {
                const caixa = document.querySelector('flow-base-prompt-box');
                const texto = [...document.querySelectorAll('div[role=textbox][contenteditable=true]')]
                    .filter(b => b.offsetParent !== null).map(b => (b.innerText || '').trim()).join('');
                const botao = caixa ? [...caixa.querySelectorAll('button')]
                    .find(b => (b.getAttribute('aria-label') || '') === 'Iniciar geração') : null;
                return {vazio: texto.length === 0,
                        desativado: !!botao && (botao.disabled || botao.getAttribute('aria-disabled') === 'true'),
                        tiles: document.querySelectorAll('flow-video-tile').length,
                        gerando: (document.body.innerText.match(/\\bGerando\\b/g) || []).length};
            }""")

        antes = estado()
        if antes["vazio"]:
            raise FlowError("ui_changed", "Não consegui escrever o comando na caixa de comando do Flow.")
        self.click_label("Iniciar geração")
        for tentativa in range(2):
            for _ in range(15):
                time.sleep(1)
                agora = estado()
                if (agora["vazio"] or agora["desativado"] or agora["tiles"] > antes["tiles"]
                        or agora["gerando"] > antes["gerando"]):
                    return
            if tentativa == 0:  # Nenhum sinal de envio: o clique não pegou, Enter costuma resolver.
                self.page.locator(PROMPT_BOX_VISIBLE).first.click()
                self.page.keyboard.press("End")
                self.page.keyboard.press("Enter")
                time.sleep(2)
        raise FlowError("ui_changed", "O Flow não confirmou o envio do comando (nenhum sinal de geração).")

    def counts(self):
        """Estado usado para reconhecer o resultado novo: miniaturas da galeria e bolhas do chat."""
        return {"tiles": len(self.tile_thumbs()),
                "bubbles": self.page.evaluate("() => document.querySelectorAll('flow-chat-bubble').length"),
                "cards": self.page.evaluate("() => document.querySelectorAll('flow-permission-message').length"),
                "thumbs": self.tile_thumbs()}

    def card_if_any(self, expected_cost, before=None, timeout=20):
        """Cartão de confirmação, se aparecer. Com o Agente desligado não existe: a geração começa direto.

        Fica como rede de segurança caso o modo Agente ligue sozinho — sem isso o worker geraria
        com os parâmetros errados sem perceber.
        """
        before = before or {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_session()
            text = self.page.evaluate("""(before) => {
                const cards = [...document.querySelectorAll('flow-permission-message')].slice(before.cards || 0);
                const open = cards.filter(c => c.querySelector('[role=radio][aria-label="Aprovar"][aria-disabled="false"]')
                    && !c.querySelector('[role=radio][aria-checked="true"]'));
                return open.length ? open.pop().innerText : '';
            }""", before)
            if text:
                match = COST.search(text)
                if not match:
                    raise FlowError("ui_changed", "Cartão de confirmação sem custo legível: " + text[:150])
                return int(match.group(1)), text.strip()
            agent = self.agent_text()
            if NO_CREDITS.search(agent):
                raise FlowError("no_credits", "Flow sem créditos suficientes: " + agent[-200:])
            time.sleep(2)
        return None, ""

    def agent_text(self):
        return self.page.evaluate("[...document.querySelectorAll('flow-chat-bubble')].slice(-3).map(b => b.innerText).join('\\n')")

    def answer(self, approve):
        """Pick exactly "Aprovar" or "Rejeitar" (never "Sempre aprovar": it would disable the cost check)."""
        label = "Aprovar" if approve else "Rejeitar"
        option = self.page.locator(f'flow-permission-message [role="radio"][aria-label="{label}"][aria-disabled="false"]').last
        if not option.count():
            raise FlowError("ui_changed", f"Opção {label} não encontrada no cartão de confirmação.")
        option.click()
        for _ in range(20):
            time.sleep(0.5)
            if not self.page.locator(f'flow-permission-message [role="radio"][aria-label="{label}"][aria-disabled="false"][aria-checked="false"]').count():
                return
        raise FlowError("ui_changed", f"O Flow não registrou a opção {label}.", charged=approve)

    def tile_thumbs(self):
        """Miniaturas da galeria (uma por vídeo pronto, em ordem do mais recente para o mais antigo)."""
        return self.page.evaluate("""() => [...document.querySelectorAll('flow-video-tile img')]
            .map(i => i.src || '').filter(Boolean)""")

    def wait_render(self, before, timeout=1800):
        """Espera o vídeo novo aparecer na galeria, recarregando o projeto de tempos em tempos.

        O cartão do chat NÃO serve como sinal: vídeo concluído e vídeo 'na fila' têm o mesmo DOM
        (`img.video-thumbnail` é a imagem de origem, não o vídeo). O único sinal confiável de
        conclusão é uma miniatura nova em `flow-video-tile`; falha aparece como 'Falha / Algo deu
        errado' na resposta do agente. A fila do Flow pode levar mais de 15 min ('alta demanda').
        """
        seen = set((before or {}).get("thumbs") or [])
        deadline = time.time() + timeout
        next_reload = time.time() + 90
        while time.time() < deadline:
            self.check_session()
            last = self.page.evaluate("""() => ([...document.querySelectorAll('flow-chat-bubble')].pop()?.innerText || '')""")
            if FAILED_CARD.search(last):
                raise FlowError("render_failed", "Flow informou falha na geração: " + last.strip()[-250:], charged=True)
            thumbs = self.tile_thumbs()
            fresh = [src for src in thumbs if src not in seen]
            if fresh:
                self.new_thumbs = fresh
                return
            if time.time() >= next_reload:
                self.log("Flow: vídeo ainda não apareceu na galeria; recarregando o projeto para conferir.")
                self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=90000)
                self.page.wait_for_selector(PROMPT_BOX_VISIBLE, timeout=60000)
                time.sleep(3)
                next_reload = time.time() + 90
                continue
            time.sleep(5)
        raise FlowError("render_timeout", "O vídeo não ficou pronto no prazo; confira o projeto no Flow.", charged=True)

    def download(self, destination):
        """Baixa em 720p o tile novo (o das miniaturas detectadas) ou, na falta dele, o mais recente."""
        index = self.page.evaluate("""(fresh) => {
            const tiles = [...document.querySelectorAll('flow-video-tile')];
            const i = tiles.findIndex(t => [...t.querySelectorAll('img')].some(im => fresh.includes(im.src || '')));
            return i < 0 ? 0 : i; }""", list(getattr(self, "new_thumbs", []) or []))
        self.page.evaluate("""(index) => { const tile = [...document.querySelectorAll('flow-video-tile')][index];
            const more = tile && [...tile.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Mais opções');
            if (more) more.click(); }""", index)
        item = self.page.locator('.cdk-overlay-container [role="menuitem"]', has_text=re.compile("download", re.I)).first
        item.wait_for(timeout=15000)
        item.click()
        size = self.page.locator('.cdk-overlay-container [role="menuitem"]', has_text=re.compile("720p|Tamanho original", re.I)).first
        size.wait_for(timeout=15000)
        with self.page.expect_download(timeout=180000) as pending:
            size.click()
        pending.value.save_as(str(destination))
        return Path(destination)

    def generate(self, project_url, image, prompt, expected_cost, model, before_approve, on_stage,
                 workdir, dry_run=False):
        self.open_project(project_url)
        on_stage("preparing")
        cost = self.ensure_settings(model, expected_cost)  # Agente desligado + chave Vídeo
        self.new_session()
        self.attach_image(image)
        before = self.counts()
        command = (f"Gere agora exatamente 1 vídeo com o modelo {model}, proporção 9:16, 10 segundos, usando a "
                   f"imagem anexada como referência do produto. Não faça perguntas. {prompt}")
        if dry_run:
            self.log(f"Flow: dry_run; nada enviado ({cost} créditos seriam gastos).")
            return None
        before_approve(cost)  # Com o Agente desligado os créditos saem no envio: avisa o painel antes.
        self.submit(command)
        on_stage("submitted", credits=cost)
        approved, text = self.card_if_any(expected_cost, before)  # Só existe se o Agente estiver ligado.
        if approved is not None:
            self.log(f"Flow pediu confirmação: {text[:160]}")
            if approved != expected_cost or not re.search(r"\b1 gera[cç][aã]o\b", text):
                self.answer(False)
                raise FlowError("cost_mismatch", f"Flow pediu {approved} créditos ('{text[:120]}'); o esperado era "
                                                 f"{expected_cost} para 1 geração. Geração recusada.")
            self.answer(True)
        self.wait_render(before)
        on_stage("downloading")
        return self.download(Path(workdir) / "flow.mp4")
