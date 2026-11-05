# Login na Shopee via Camofox

Sessão autenticada da Shopee em browser anti-detecção, usada quando o painel/worker precisa
enxergar páginas que só existem logado (pedidos, notificações, carrinho, área de afiliado) ou
quando o objetivo é validar preço/oferta como um usuário real.

Status: **verificado de ponta a ponta em 2026-11-05** — login feito pelo usuário na janela
visível, cookies persistidos e reaproveitados pelo container headless, navegação autenticada
(busca + página de produto) sem novo captcha.

## Arquitetura (não é o worker do Flow)

O login da Shopee **não** usa o Chromium/Playwright do worker (`Xvfb :95`, perfil do Flow).
Usa o **Camofox** — servidor Node em volta do Camoufox (Firefox com spoofing de fingerprint) —
rodando como dois containers no thevault:

| Container | Portas | Papel |
|---|---|---|
| `camofox` | 9377 | headless, é o que o Hermes dirige no dia a dia |
| `camofox-vnc` | 9378 (API) + 6080 (noVNC) | janela visível, só para login/captcha manual |

- Volume compartilhado: `/mnt/user/appdata/camofox` → `/root/.camofox` (`.admin.key`,
  `.vnc.password`, `profiles/`).
- Identidade **compartilhada** entre os dois containers: `managed_persistence: true` no Hermes dá
  um `userId` determinístico por perfil (`hermes_20aaad259c`), e o servidor grava
  `profiles/<sha256(userId)>/storage-state.json`.
- `SESSION_TIMEOUT_MS=0` no `camofox-vnc`: sem isso o browser é fechado por ociosidade e a janela
  do noVNC fica preta enquanto o usuário vai conectar.
- A doc do Hermes sobre o plugin, as armadilhas e o script de checkpoint estão na skill
  `camofox-setup`. Este arquivo é o recorte da Shopee.

## Procedimento

### 1. Abrir a Shopee na janela visível e validar que existe janela

```bash
B=http://192.168.1.90:9378; U=hermes_20aaad259c
TAB=$(curl -s -X POST $B/tabs -H 'Content-Type: application/json' \
  -d "{\"userId\":\"$U\",\"listItemId\":\"shopee-login\",\"url\":\"https://shopee.com.br\"}")
TID=$(echo "$TAB" | python3 -c "import sys,json;print(json.load(sys.stdin)['tabId'])")

# antes de mandar o usuário conectar: a janela PRECISA existir no display
ssh thevault "docker exec camofox-vnc sh -c 'DISPLAY=:0 xwininfo -root -tree | head -20'"
```

Saudável: `"Navigator" ("camoufox") 1920x1034+0+0`.
Só janelas de `1x1`/`10x10` ⇒ VNC preto, mesmo com `/health` e `/vnc/status` verdes.

### 2. Usuário entra no noVNC e faz o login

- `http://192.168.1.90:6080/vnc.html` → **Connect** → senha de
  `ssh thevault "cat /mnt/user/appdata/camofox/.vnc.password"`.
- Captcha de quebra-cabeça (`/verify/captcha?scene=crawler_item`) é esperado: arrastar o slider.
- Login **por e-mail/telefone no formulário**. **Não** usar “Entrar com Google”: o popup de OAuth
  é adotado como *managed tab* e a janela principal deixa de existir → tela preta.

### 3. Confirmar que logou (antes de qualquer checkpoint)

```bash
curl -s "$B/tabs/$TID/snapshot?userId=$U" > /tmp/s.json
python3 - <<'EOF'
import json; s=json.load(open('/tmp/s.json'))
print('url :', s['url'])
print('logado?', 'notificações' in s['snapshot'] and 'Entre' not in s['snapshot'])
EOF
```

Marcadores observados no login válido: URL com `?is_from_login=true`, link `"5 notificações"`
(`/user/notifications/order`), ausência de `"Entre"`/`"Cadastrar"`, nome da conta no topo.

### 4. Checkpoint + recarga no headless

```bash
# grava storage-state em disco (sem isso o login só existe na memória do servidor)
ssh thevault "curl -s -X POST http://127.0.0.1:9378/stop \
  -H \"x-admin-key: \$(cat /mnt/user/appdata/camofox/.admin.key)\""

# o container que já tinha contexto vivo NÃO relê o disco: reiniciar é obrigatório
ssh thevault "docker restart camofox"
```

Verificado no checkpoint: **28 cookies** de `shopee.com.br` (`SPC_F`, `SPC_SEC_SI`, `_sapid`,
`csrftoken`, `REC_T_ID`, `language`, `ssr-tz` …) em
`profiles/d5398c65390561cf6b4279f85ac3f8d9/storage-state.json`.

### 5. Navegar autenticado (headless)

```bash
B=http://192.168.1.90:9377
# abrir
curl -s -X POST $B/tabs -H 'Content-Type: application/json' \
  -d "{\"userId\":\"$U\",\"listItemId\":\"shopee-headless\",\"url\":\"https://shopee.com.br\"}"
# navegar numa aba existente
curl -s -X POST "$B/tabs/$TID/navigate" -H 'Content-Type: application/json' \
  -d "{\"userId\":\"$U\",\"url\":\"https://shopee.com.br/search?keyword=fone%20bluetooth\"}"
# ler / capturar
curl -s "$B/tabs/$TID/snapshot?userId=$U"
curl -s "$B/tabs/$TID/links?userId=$U"      # devolve URLs; produto casa com -i.<shopid>.<itemid>
curl -s "$B/tabs/$TID/screenshot?userId=$U" -o /tmp/p.png   # PNG cru, não JSON
```

## Armadilhas

1. **Anti-bot por comportamento.** Cliques rápidos via API (dois em <1 s) derrubaram a sessão em
   `/verify/captcha?scene=crawler_item`. Prefira `navigate` por URL e deixe cliques para o humano.
2. **Popup de OAuth mata a janela** (ver passo 2). Recuperar: fechar aba órfã, `docker restart
   camofox-vnc`, criar a aba de novo.
3. **Não dirigir os dois containers na mesma identidade ao mesmo tempo.** Feche a aba do VNC antes
   de assumir no headless; os dois escrevem o mesmo `storage-state.json`.
4. **`docker stop` não é checkpoint confiável.** Só `POST /stop` com `x-admin-key`.
5. **Checkpoint sem restart não aparece** no headless: contexto vivo tem precedência sobre o disco.
6. **Sessão expira.** O `storage-state` guarda cookies; quando a Shopee invalidar, repetir do passo 1.
7. **`tabId` velho engana.** Aba criada por API é reciclada por ociosidade (`tab_reaper_empty_session`);
   snapshot vazio ou `tabNotFound` é aba morta, **não** login perdido — recrie com `POST /tabs` antes
   de concluir qualquer coisa.

## Confidencialidade

`storage-state.json` contém cookies de sessão autenticada: tratar como segredo (mesmo cuidado do
volume `shopee-data`). O Hermes nunca recebe a senha da Shopee — ela é digitada pelo usuário na
janela do noVNC. Cópias temporárias da senha do VNC (`/mnt/user/data/camofox-vnc-senha.txt`,
servidor HTTP na 8899 do host Hermes) devem ser removidas depois do uso.

## Evidência do teste (2026-11-05)

| Verificação | Resultado |
|---|---|
| Login no VNC | `https://shopee.com.br/?is_from_login=true` · “5 notificações” · conta `igaoeu` |
| Checkpoint | `storage state persisted` (reason `admin_stop`) · 28 cookies Shopee |
| Headless após restart | sessão logada, sem “Entre” |
| Busca `fone bluetooth` | resultados com preço/desconto |
| Produto aberto | QCY HT08 MeloBuds Pro — R$165,30 no Pix · 4,9/5 · 5,6 mil avaliações · 10 mil+ vendidos |
| Captcha no headless | nenhum durante a navegação por URL |
