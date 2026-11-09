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
COST = re.compile(r"(\d+)\s*cr[eé]ditos?", re.I)
NO_CREDITS = re.compile(r"cr[eé]ditos? insuficientes|n[aã]o (tem|há) cr[eé]ditos|sem cr[eé]ditos|limite de cr[eé]ditos", re.I)
FAILED = re.compile(r"n[aã]o (foi|consegui) (poss[ií]vel )?gerar|falh(a|ou) na gera|viola|pol[ií]tica|n[aã]o posso gerar", re.I)
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
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        deadline = time.time() + 90
        while time.time() < deadline:
            self.check_session()
            if self.page.locator(PROMPT_BOX).count():
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

    def agent_mode(self):
        """Liga o modo Agente da caixa de comando: sem ele não existe cartão de confirmação de custo."""
        for _ in range(3):
            state = self.page.evaluate("""() => {
                const chip = document.querySelector('flow-agent-mode-toggle-chip button');
                const box = document.querySelector('flow-base-prompt-box');
                return {pressed: chip ? chip.getAttribute('aria-pressed') : null,
                        agent: !!box && [...box.querySelectorAll('button')]
                            .some(b => b.getAttribute('aria-label') === 'Instruções para o agente')};
            }""")
            if state["agent"] or state["pressed"] == "true":
                return
            if not self.page.evaluate("""() => { const c = document.querySelector('flow-agent-mode-toggle-chip button');
                    if (!c) return false; c.click(); return true; }"""):
                break
            time.sleep(3)
        raise FlowError("ui_changed", "Não foi possível ligar o modo Agente da caixa de comando do Flow.")

    def agent_settings(self):
        """Abre o painel 'Configurações do agente neste projeto' e devolve o painel visível."""
        handle = self.page.evaluate_handle("""() => {
            const box = document.querySelector('flow-base-prompt-box');
            return box ? [...box.querySelectorAll('button')]
                .find(b => b.getAttribute('aria-label') === 'Configurações') || null : null; }""")
        button = handle.as_element()
        if not button:
            raise FlowError("ui_changed", "Botão de configurações do agente não encontrado no compositor.")
        button.click()
        deadline = time.time() + 20
        while time.time() < deadline:
            view = self.page.evaluate_handle("""() => [...document.querySelectorAll('flow-settings-view')]
                .find(v => v.offsetParent !== null) || null""").as_element()
            if view:
                return view
            time.sleep(0.5)
        raise FlowError("ui_changed", "Painel de configurações do agente não abriu.")

    def confirm_before_generating(self, view):
        """Marca 'Sempre' em 'Confirmar antes de gerar'. Sem isso o agente gasta créditos sem perguntar."""
        checked = view.evaluate("""(view) => {
            const radio = [...view.querySelectorAll('input.mdc-radio__native-control')]
                .find(r => (r.closest('mat-radio-button')?.innerText || '').trim().startsWith('Sempre'));
            if (!radio) return 'ausente';
            if (radio.checked) return 'ok';
            radio.click(); return 'clicado';
        }""")
        if checked == "ausente":
            raise FlowError("ui_changed", "Opção 'Confirmar antes de gerar' não encontrada.")
        time.sleep(1)
        if view.evaluate("""(view) => {
            const radio = [...view.querySelectorAll('input.mdc-radio__native-control')]
                .find(r => (r.closest('mat-radio-button')?.innerText || '').trim().startsWith('Sempre'));
            return radio ? radio.checked : false; }"""):
            return
        raise FlowError("ui_changed", "O Flow não marcou 'Confirmar antes de gerar' como 'Sempre'.")

    def ensure_settings(self, model, expected_cost=None):
        """Modo Agente + 'Sempre' confirmar, vídeo 9:16 x1, modelo esperado e compositor em 720p/10 s/x1."""
        self.agent_mode()
        view = self.agent_settings()
        self.confirm_before_generating(view)
        state = view.evaluate("""(view) => {
            const head = [...view.querySelectorAll('*')]
                .find(e => e.childElementCount === 0 && e.textContent.trim() === 'Padrão para geração de vídeo');
            const section = head && head.closest('.settings-section');
            if (!section) return {error: 'seção de vídeo não encontrada'};
            for (const label of ['9:16', 'x1']) {
                const toggle = [...section.querySelectorAll('button[role=radio]')]
                    .find(b => b.innerText.trim().endsWith(label));
                if (toggle && toggle.getAttribute('aria-checked') !== 'true') toggle.click();
            }
            return {model: (section.querySelector('button[aria-label="Modelo padrão de geração de vídeo"]')?.innerText || '')
                .replace('arrow_drop_down', '').trim()};
        }""")
        if state.get("error"):
            raise FlowError("ui_changed", "Configurações do agente: " + state["error"])
        if model.casefold() not in state["model"].casefold():
            raise FlowError("model_mismatch", f"Modelo padrão de vídeo no Flow é '{state['model']}', esperado '{model}'.")
        save = view.evaluate_handle("""(view) => [...view.querySelectorAll('button')]
            .find(b => b.innerText.trim() === 'Salvar') || null""").as_element()
        if not save:
            raise FlowError("ui_changed", "Botão 'Salvar' não encontrado nas configurações do agente.")
        try:
            save.click(timeout=8000)
        except Exception:  # Painel estreito às vezes reporta o botão como fora de vista.
            view.evaluate("""(view) => [...view.querySelectorAll('button')]
                .find(b => b.innerText.trim() === 'Salvar')?.click()""")
        time.sleep(2)
        for _ in range(10):
            if not self.page.evaluate("() => [...document.querySelectorAll('flow-settings-view')].some(v => v.offsetParent !== null)"):
                break
            time.sleep(0.5)

        self.open_settings()
        pane = self.settings_pane()
        for label in ("9:16", "720p", "10s"):
            self.pick(pane, label)
        state = pane.evaluate("""(pane) => ({
            model: (pane.querySelector('button[aria-label="Selecionar família de modelos"]')?.innerText || '')
                .replace('arrow_drop_down', '').trim(),
            text: pane.innerText})""")
        if model.casefold() not in state["model"].casefold():
            raise FlowError("model_mismatch", f"Modelo de vídeo no compositor é '{state['model']}', esperado '{model}'.")
        cost = COST_PANEL.search(state["text"])
        if not cost:
            raise FlowError("ui_changed", "O Flow não mostrou o custo da geração no compositor.")
        if expected_cost is not None and int(cost.group(1)) != int(expected_cost):
            raise FlowError("cost_mismatch", f"O compositor do Flow estima {cost.group(1)} créditos; o esperado era "
                                             f"{expected_cost}. Nada foi gerado.")
        self.page.keyboard.press("Escape")  # The panel saves as it goes; closing frees the prompt box.
        time.sleep(1)

    def new_session(self):
        try:
            self.click_label("Iniciar nova sessão", timeout=5)
            time.sleep(2)
        except FlowError:
            self.log("Flow: botão de nova sessão ausente; seguindo na sessão atual.")
        self.page.wait_for_selector(PROMPT_BOX, timeout=20000)

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
        include = self.page.locator(".cdk-overlay-container button", has_text="Incluir no comando").first
        include.wait_for(timeout=15000)
        include.click()
        for _ in range(30):
            if self.page.evaluate("document.querySelectorAll('flow-creative-agent-prompt-box img, flow-base-prompt-box img').length"):
                return
            time.sleep(1)
        raise FlowError("ui_changed", "A imagem não entrou na caixa de comando.")

    def tiles(self):
        return self.page.evaluate("document.querySelectorAll('flow-video-tile').length")

    def submit(self, prompt):
        box = self.page.locator(PROMPT_BOX).first
        box.click()
        self.page.keyboard.insert_text(prompt)
        time.sleep(2)
        pending = lambda: self.page.evaluate(f"(document.querySelector('{PROMPT_BOX}')?.innerText || '').trim().length")
        for attempt in range(3):
            if attempt == 0:
                self.click_label("Iniciar geração")
            else:  # The send button can stay inert right after an upload; Enter submits the box.
                self.page.locator(PROMPT_BOX).first.click()
                self.page.keyboard.press("End")
                self.page.keyboard.press("Enter")
            for _ in range(10):
                time.sleep(1)
                if not pending():
                    return
        raise FlowError("ui_changed", "O Flow não aceitou o envio do comando.")

    def wait_permission(self, timeout=240):
        """Return (cost, text) of the agent's permission card; fail on questions or credit problems."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_session()
            card = self.page.evaluate("""() => {
                const cards = [...document.querySelectorAll('flow-permission-message')];
                const open = cards.filter(c => c.querySelector('[role=radio][aria-label="Aprovar"][aria-disabled="false"]')
                    && !c.querySelector('[role=radio][aria-checked="true"]'));
                const last = open.pop();
                const question = document.querySelector('flow-a2ui-multiple-choice, flow-a2ui-video-option');
                return {text: last ? last.innerText : '', question: question ? question.innerText.slice(0, 300) : ''};
            }""")
            if card["text"]:
                match = COST.search(card["text"])
                if not match:
                    raise FlowError("ui_changed", "Cartão de confirmação sem custo legível: " + card["text"][:150])
                return int(match.group(1)), card["text"].strip()
            agent = self.agent_text()
            if NO_CREDITS.search(agent):
                raise FlowError("no_credits", "Flow sem créditos suficientes: " + agent[-200:])
            if card["question"]:
                raise FlowError("agent_question", "O agente do Flow fez uma pergunta em vez de pedir confirmação: " + card["question"])
            time.sleep(2)
        raise FlowError("ui_changed", "O agente do Flow não pediu confirmação no prazo.")

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

    def wait_render(self, before, timeout=900):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_session()
            state = self.page.evaluate("""(before) => {
                const tiles = [...document.querySelectorAll('flow-video-tile')];
                const first = tiles[0];
                const ready = !!first && !!first.querySelector('img.thumbnail[src], video[src]') && !first.querySelector('mat-progress-spinner, [role=progressbar]');
                return {count: tiles.length, ready, label: first ? first.innerText.trim().slice(0, 80) : ''};
            }""", before)
            if state["count"] > before and state["ready"]:
                return
            agent = self.agent_text()
            if FAILED.search(agent) and state["count"] <= before:
                raise FlowError("render_failed", "Flow informou falha na geração: " + agent[-250:], charged=True)
            time.sleep(5)
        raise FlowError("render_timeout", "O vídeo não ficou pronto no prazo; confira o projeto no Flow.", charged=True)

    def download(self, destination):
        self.page.evaluate("""() => { const tile = document.querySelector('flow-video-tile');
            const more = tile && [...tile.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Mais opções');
            if (more) more.click(); }""")
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
        self.ensure_settings(model, expected_cost)
        self.new_session()
        self.attach_image(image)
        before = self.tiles()
        self.submit(f"Gere agora exatamente 1 vídeo com o modelo {model}, proporção 9:16, 10 segundos, usando a "
                    f"imagem anexada como referência do produto. Não faça perguntas. {prompt}")
        cost, text = self.wait_permission()
        self.log(f"Flow pediu confirmação: {text[:160]}")
        if cost != expected_cost or not re.search(r"\b1 gera[cç][aã]o\b", text):
            self.answer(False)
            raise FlowError("cost_mismatch", f"Flow pediu {cost} créditos ('{text[:120]}'); o esperado era {expected_cost} "
                                             "para 1 geração. Geração recusada.")
        if dry_run:
            self.answer(False)
            return None
        try:
            before_approve(cost)  # The panel records "credits may be spent" before the click.
        except Exception:
            self.answer(False)
            raise
        self.answer(True)
        on_stage("submitted", credits=cost)
        self.wait_render(before)
        on_stage("downloading")
        return self.download(Path(workdir) / "flow.mp4")
