# Worker da VM hermes

O painel roda no Unraid em container somente leitura. O Antigravity CLI (`agy`) e o Chromium logado no Google Flow ficam na VM `hermes`. O worker liga os dois: busca tarefas no painel, executa e devolve o resultado.

Dois laços independentes rodam no mesmo serviço: `hermes` (tarefas de modelo) e `hermes.video` (vídeos). Assim, um vídeo em andamento não atrasa a análise diária dos produtos.

## Como funciona

- O worker chama `POST /api/worker/claim` a cada 5 segundos com o token do worker. O painel nunca abre conexão para a VM.
- Cada análise de produto vira uma tarefa `llm`. O worker baixa as imagens do anúncio e dos candidatos para uma pasta temporária, roda `agy -p … --model <modelo> --add-dir <pasta> --output-format json --json-schema …` e devolve a saída estruturada. Ao terminar, apaga a pasta.
- O `agy` roda **sem** `--dangerously-skip-permissions`. Títulos de anúncios são texto de terceiros. Sem essa opção, o modo headless nega ferramentas de terminal: uma instrução maliciosa no título não executa comandos na VM. A leitura das imagens continua funcionando, e isso foi testado na VM.
- Sem contato por 90 segundos, o worker fica offline. O painel pula os modelos `agy` e segue para os fallbacks OpenAI-compatíveis configurados.
- Tarefas têm prazo. Se uma tarefa vencer antes de o worker responder, o resultado atrasado é recusado. O painel não espera tarefas criadas antes de reiniciar.

## Vídeos (Google Flow)

- Perfil do navegador: `~/.local/share/flow-vm-test/profile` (chave `flow_profile` no config). Ele é exclusivo do worker e usa `--no-sandbox`, autorizado para esse perfil. O Chromium é o do Playwright 1.62.0 (`~/.cache/ms-playwright/chromium-1234`), numa tela virtual Xvfb `:95`. Ele abre por tarefa e fecha no fim, para liberar memória para a transcrição.
- Ambiente: `~/.local/share/shopee-studio-worker/venv` (Python 3.12, Playwright, faster-whisper). As versões estão fixadas no `install.sh` para reaproveitar os wheels do cache do uv; o disco da VM é pequeno.
- Transcrição: faster-whisper `small` em CPU int8 (chave `whisper_model`). Usa ~600 MB de RAM e leva ~10 s por vídeo de 10 s.
- Cada etapa é informada ao painel. A etapa `approving` é registrada **antes** do clique em Aprovar; se o painel recusar (fila pausada), o worker escolhe Rejeitar. Na falha, a tela do Flow fica em `~/.cache/shopee-studio-worker/ultimo-erro.png`.
- **Interface nova do Flow (nov/2026):** o projeto abre com uma caixa de comando única, e o modo Agente é um chip desligado por padrão. O worker:
  1. **desliga** o chip **Agente** (`flow-agent-mode-toggle-chip`, `aria-pressed=false`) e confere o estado — é o que libera a chave Imagem/Vídeo no painel do compositor;
  2. ~~em **Configurações do agente**~~ — **não usar**: com o Agente desligado esse painel nem existe;
  3. no painel **Gatilho de configurações** do compositor fixa 9:16, 720p, 10 s e lê o custo estimado antes de enviar; custo diferente do esperado em Preferências → `cost_mismatch`, sem gastar crédito.
  Os seletores antigos (`flow-settings-view` aberto por botão `tune`) não existem mais fora do modo Agente — era isso que fazia a fila pausar com "Modelo padrão de vídeo é ''".
- **Modo Agente DESLIGADO é obrigatório.** Só com o Agente desligado o painel do compositor oferece a chave **Imagem / Vídeo**; ligado, o Flow não deixa escolher vídeo e o resultado sai errado. O worker confere o `aria-pressed` do chip (`flow-agent-mode-toggle-chip button` / botão `Agente` da caixa de comando) em toda tarefa e desliga se estiver ligado. Depois abre **Gatilho de configurações** e marca **Vídeo**, 9:16, 720p, 10 s e x1, conferindo o modelo e o custo.
- **Sem cartão de confirmação.** Com o Agente desligado a geração começa no clique de enviar: o worker avisa o painel (`approving`) **antes** e registra os créditos como gastos ao enviar. `card_if_any()` fica só como rede de segurança por 20 s, caso o Agente ligue sozinho.
- **Detecção do resultado:** o cartão do chat do agente **não** serve como sinal — vídeo concluído e vídeo na fila têm o mesmo DOM (`img.video-thumbnail` é a imagem de origem). O worker considera pronto quando aparece uma **miniatura nova em `flow-video-tile`** na galeria (recarrega o projeto a cada 90 s para conferir), baixa o tile novo em 720p e trata "Falha / Algo deu errado" na resposta do agente como falha. A fila do Flow pode passar de 15 min ("alta demanda") — o prazo é 30 min.


### Login, 2FA ou CAPTCHA

Quando o Google pede login ou verificação, a fila pausa e chega um alerta no Telegram. Na VM:

    ~/.local/share/shopee-studio-worker/flow_login.sh        # ou: worker/flow_login.sh pc1-lan

O script para o worker e abre o navegador do perfil numa tela virtual. Ele também expõe a tela no PC por noVNC, através de um túnel SSH reverso: no PC, abra http://127.0.0.1:6086/vnc.html. Nada fica exposto na LAN. Faça o login e tecle Ctrl+C no terminal; o script fecha tudo e reinicia o worker. Depois, toque em **Retomar vídeos** no painel.

## Instalação (uma vez)

1. No painel: Preferências → Worker da VM → **Gerar novo token**. Copie o token.
2. Na VM `hermes`, dentro do checkout do repositório:

       worker/install.sh http://192.168.1.90:8787

   Cole o token quando o script pedir. Ele fica em `~/.config/shopee-studio-worker/config.json` com modo 600.
3. Para o serviço continuar após o logout e voltar após um reboot, o usuário precisa de linger: `loginctl enable-linger hermes`.

Atualizar o worker é rodar `worker/install.sh` de novo. O script preserva o config, instala as dependências de vídeo, baixa o modelo de transcrição (~460 MB na primeira vez) e reinicia o serviço.

## Operação

    systemctl --user status shopee-studio-worker
    journalctl --user -u shopee-studio-worker -f
    systemctl --user stop shopee-studio-worker     # o painel passa a usar os fallbacks

Trocar o token no painel desconecta o worker. Depois, edite `config.json` com o token novo e reinicie o serviço.
