# Plano — Modo Moderno (seleção anticoncorrência, roteiro variado e vídeo conferido)

> Plano autocontido para quem for implementar (humano ou outro LLM). Leia inteiro antes de mexer no código.
> Origem: comparação com o processo `shopee-trend-video` de um amigo do usuário (documento de 04/11/2026),
> analisada em 09/11/2026. O item "galeria com 3 fotos reais no Flow" (captura da página do produto pelo
> navegador logado) **ficou fora deste plano** a pedido do usuário — ele vai avaliar à parte.

## 0. Resumo

O painel ganha uma escolha em **Preferências → Modo**:

| Modo | O que é |
|---|---|
| **Modo Legacy** | Exatamente o comportamento anterior a esta atualização: coleta por comissão (`sortType: 5`), ranking por pesos (comissão/nota/vendas), roteiro e vídeo como hoje. **É o padrão** — quem atualiza não muda nada sem escolher. |
| **Modo Moderno** | Coleta por vendas e por categoria, funil anticoncorrência, pontuação multiplicativa com frescor e tendência, roteiro com formatos variados e conferência de números, vídeo 1080×1920 com selo de compra e nota de fidelidade. |

O modo vale para as **próximas consultas**. Cada produto guarda o modo com que foi escolhido (`product["mode"]`),
e o vídeo desse produto segue esse modo mesmo que a preferência mude depois (o vídeo é coerente com a seleção).

## 1. Contexto obrigatório (ler antes)

- Repositório: `~/projects/shopee_studio` (worktree de trabalho: `.worktrees/melhorias`, branch `melhorias`).
  Upstream `saladfruiter/shopee-studio`; o PR sai do fork `saladfruiter-hermesagent/shopee-studio`. **O merge é do
  usuário.** Não fazer deploy no TheVault nem atualizar o worker em produção sem pedido explícito.
- `README.md` descreve o comportamento implementado; `PLANO.md` guarda pendências gerais. Mudou comportamento,
  atualize o README.
- Testes: `source ~/projects/shopee_studio/.venv/bin/activate && python -m unittest discover -s tests -v`
  (Python 3.13, sem rede, sem Telegram, sem Flow). Linha de base antes deste plano: **98 testes OK**.
- Estilo: Python compacto, sem dependências novas no painel (`requirements.txt` = Flask, waitress, tzdata; o
  container **não tem ffmpeg nem numpy**). Textos da interface em português do Brasil; comentários/docstrings em
  inglês, como no código existente. Migrações de banco **só aditivas** (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD`).
- Regras inegociáveis do projeto (não podem regredir):
  1. `agy` roda **sem** `--dangerously-skip-permissions` (títulos de anúncio são texto de terceiros).
  2. Custo do Flow aprovado/registrado **antes** do envio; nada de "Sempre aprovar"; teto diário de créditos.
  3. Falha depois da aprovação = vídeo incerto; **nunca** regerar sozinho.
  4. Sem vídeo, nada é entregue automaticamente.
  5. Entrega incerta no Telegram nunca é repetida automaticamente.
  6. Legenda ≤ 150 caracteres UTF-16, 2–3 hashtags, proibidas `#Shopee/#Achadinhos/#Viral/#Fyp…`, sem emojis,
     sem preço/desconto/frete/urgência — **também no Modo Moderno** (o filtro de desconto só seleciona; o texto
     nunca fala de desconto).
- Corpo JSON global limitado a 64 KB (`app.py`, `MAX_CONTENT_LENGTH`); só o upload do vídeo passa disso.

## 2. Decisões (confirmadas e padrões propostos)

| # | Decisão | Estado |
|---|---|---|
| D1 | Chave de modo `mode` = `legacy` \| `modern`, padrão `legacy`. | confirmada pelo usuário |
| D2 | Item 13 (galeria de fotos reais) fora do plano. | confirmada pelo usuário |
| D3 | Configurações do Modo Moderno usam chaves próprias (`modern_*`); as do Legacy não mudam de significado. Compartilhadas: palavra-chave, quantidade de ofertas, máximo por loja, agenda, envio automático e vídeo. | padrão proposto |
| D4 | Registro diário de vendas (base da tendência) tem chave própria `sales_snapshot` (padrão **ligado**) e roda **nos dois modos**, para o histórico já existir quando o usuário passar ao Moderno. Não altera seleção nem envio do Legacy; só faz consultas de leitura à API. | padrão proposto — confirmar |
| D5 | Fidelidade abaixo do mínimo (padrão 6/10) **não bloqueia** a entrega: o card mostra a nota e chega um alerta separado no Telegram. Falha da conferência = "não avaliada", nunca bloqueia. | padrão proposto — confirmar |
| D6 | Selo de compra nos últimos 3,5 s: cápsula "★ 4,9 • 206 vendidos" + botão laranja pulsante "CONFIRA NO PRODUTO MARCADO", acima da legenda falada (a faixa inferior fica livre para o card do app). | padrão proposto — confirmar no vídeo de teste |
| D7 | Potencial de vídeo pela IA (nota 0–10 antes de escolher) fica atrás de uma chave própria, **desligada** por padrão (custa uma chamada de modelo por consulta). | padrão proposto |
| D8 | Constantes da fórmula (0,7 / 0,6 / 1,4 / 1,15 / R$ 400 / 1.500 vendas) ficam fixas no código, documentadas, sem campo na interface (YAGNI). | padrão proposto |

## 3. Fatos da API usados (verificar com o script da Tarefa M0)

Fonte: documento do amigo + documentação pública da Open API. **Não confirmados com as nossas credenciais.**

- `productOfferV2(listType: 0, sortType: 2, …)` → ordenado por **vendas** (o nosso Legacy usa `sortType: 5`, comissão).
- `productCatId: <int>` filtra por categoria; `listType: 2` = itens de maior desempenho entre afiliados ("top").
- Campos novos lidos só no Moderno: `sellerCommissionRate`, `priceDiscountRate`, `productCatIds`.
- `sales` = vendas dos últimos 30 dias (segundo o documento).
- Se a API recusar algum campo/argumento, **só o Modo Moderno falha**, com a mensagem da Shopee no card; o Legacy
  continua usando exatamente a mesma query de antes (`shopee_query` intocada).
- Campo ausente na resposta → fator neutro (1,0) e motivo "dado indisponível" no card. Nunca inventar valor.

Categorias de referência (Brasil, do documento do amigo — conferir no M0):

| ID | Categoria |
|---|---|
| 100013 | Celulares e Dispositivos |
| 100644 | Computadores e Acessórios |
| 100535 | Áudio |
| 100634 | Jogos e Consoles |
| 100635 | Câmeras e Drones |
| 100715 | Casa e Construção · Ferramentas |
| 100719 | Casa e Construção · Iluminação |
| 101189 | Casa e Construção · Pisos e Revestimentos |
| 101270 | Decoração de Natal |
| 100722 | Casa e Construção · Festas |

## 4. Entregas (cada uma = um commit testado; o conjunto vai num PR para teste do usuário)

### M0 — Script de sondagem da API (gate de dados reais)

**Arquivo:** `scripts/probe_modern_api.py`

- Lê `SHOPEE_APP_ID`/`SHOPEE_SECRET` do ambiente (nunca imprime). Usa `core.fetch_product_page`/`modern_query`.
- Faz 3 consultas de 1 página (limit 10): `sortType 2` sem categoria; `sortType 2` com `productCatId` (argumento
  `--category`, padrão 100715); `listType 2`.
- Imprime, por consulta: OK/erro da Shopee, quantos nós, e para cada campo novo (`sellerCommissionRate`,
  `priceDiscountRate`, `productCatIds`, `sales`, `periodStartTime`) quantos nós trazem valor e um exemplo
  **sem link nem nome de loja**.
- Aceite: o usuário roda (`SHOPEE_APP_ID=… SHOPEE_SECRET=… python scripts/probe_modern_api.py`) e o resultado
  confirma ou ajusta a seção 3. Sem credenciais o script sai com mensagem clara, código 2.

### M1 — Fundação: chave de modo, configurações, histórico de vendas

**`core.py`**
1. `DEFAULTS` ganha (tipos definem a validação existente):
   ```
   mode="legacy",
   modern_categories="", modern_name_filter="", modern_pages=10,
   modern_min_price=80.0, modern_max_price=1500.0, modern_min_rating=4.5,
   modern_min_sales=50, modern_max_sales=5000, modern_min_discount=0,
   modern_showcase_cut=30, modern_exclude_top=True, modern_top_pages=20,
   modern_type_days=7, modern_video_potential=False, modern_fidelity_min=6,
   sales_snapshot=True, snapshot_time="07:00"
   ```
2. `validate_settings`: `mode ∈ {legacy, modern}`; `modern_categories` = IDs numéricos (4–9 dígitos) separados por
   vírgula, até 20, normalizados para `"100715,100719"`; `modern_name_filter` ≤ 200 caracteres; faixas:
   `modern_pages 1–50`, `modern_top_pages 1–50`, `modern_min_sales 0–100000`, `modern_max_sales 0–10000000`
   (0 = sem teto; se > 0 tem de ser ≥ mínimo), `modern_min_discount 0–90`, `modern_showcase_cut 0–500`,
   `modern_type_days 0–60`, `modern_fidelity_min 0–10`; preços ≥ 0 e máximo ≥ mínimo (0 = sem teto);
   `modern_min_rating 0–5`; `snapshot_time` HH:MM.
3. `Store.__init__`: tabela `sales_history(item_id TEXT, day TEXT, sales INTEGER, PRIMARY KEY(item_id, day))`.
4. `Store.record_sales(nodes, day)`: grava `itemId → sales` do dia (substitui o valor do mesmo dia) e apaga
   linhas com mais de 60 dias. `Store.sales_history(item_ids)` → `{item_id: [(day, sales), …]}` ordenado.
5. `Engine.tick`: se `sales_snapshot` ligado, depois de `snapshot_time` e ainda não feito hoje (flag
   `sales_snapshot` = `{day, attempts, next_retry, status}` na tabela `flags`), busca as fontes do Moderno
   (M2 `fetch_modern(..., top=False)`) e grava. Falha: até 3 tentativas com 30 min de intervalo, depois desiste
   no dia; tudo registrado no log. Não segura o `Engine.lock` (não conflita com consulta).

**Interface (`templates/index.html`, `static/app.js`, `static/style.css`)**
- Novo painel no topo de Preferências, "MODO", com dois rádios `name="mode"`:
  "Modo Legacy — como era antes desta atualização" e "Modo Moderno — vendas por categoria, menos concorrência,
  vídeo conferido". Uma frase explica cada um ao lado do rádio (não em tooltip).
- O formulário recebe a classe `mode-legacy`/`mode-modern` conforme o rádio (muda ao clicar, antes de salvar).
  CSS: `.mode-modern .legacy-only{display:none}` e `.mode-legacy .modern-only{display:none}`.
  Campos só do Legacy (preço, nota, vendas, comissão, nota alternativa, pesos, limites da consulta) ganham
  `legacy-only`. O painel novo "MODO MODERNO" (`modern-only`) traz: categorias (com a tabela de referência num
  `<details>`), filtro de nome, preço mín./máx., nota mínima, vendas mín./máx., desconto mínimo, potencial de vídeo
  pela IA, fidelidade mínima; avançado (`<details>`): páginas por categoria, corte da vitrine, excluir top de
  afiliados + páginas do top, dias sem repetir o tipo. Painel "ROTINA" ganha "Registrar vendas diárias" e o horário.
- O carregamento existente (`elements.namedItem(key).value = …`) já funciona com `RadioNodeList`; conferir.
- Resumo do topo (`#filter-summary`) mostra o modo ativo.

**Testes (`tests/test_modern.py`)**: validação (modo inválido, categorias inválidas, normalização, faixas);
`record_sales`/`sales_history`/poda; o snapshot do `tick` roda uma vez por dia, respeita o horário e as tentativas;
o modo Legacy continua com os 98 testes antigos verdes e `shopee_query` idêntica.

### M2 — Coleta e ranking do Modo Moderno

**Novo módulo `modern.py`** (funções puras, sem rede; o painel importa):

- `CATEGORY_HINT` (tabela da seção 3, só para a interface/rótulos).
- `growth(history, now_sales, today)`: ponto de referência mais próximo de 7 dias atrás com pelo menos 2 dias de
  idade; `g = (agora − ref) / max(ref, 30) × 7 / dias`; devolve `None` sem histórico suficiente.
- `product_type(name)`: 3 primeiras palavras relevantes de `content.name_tokens(name)` sem números puros
  (ex.: "piso vinilico autocolante"); `""` se não houver 2 palavras.
- `rank_modern(nodes, settings, excluded, top_ids, history, recent_types, now)` → `(candidates, selected, warning)`,
  mesmo contrato de `core.rank_offers`. Cada nó traz `_position` (posição na lista por vendas da sua fonte) e
  `_source` (ID da categoria ou "geral"), postos pela coleta.

  **Funil (nesta ordem; contagem por motivo vai no aviso da consulta):**
  | Motivo | Regra |
  |---|---|
  | sem dados | sem `itemId`, sem link `https`, sem preço, sem comissão, oferta fora da validade |
  | repetido | mesma chave `shop:item` ou mesmo nome normalizado já visto na rodada |
  | já enviado | chave em `Store.exclusions()` (mesma regra do Legacy) |
  | nota | `ratingStar < modern_min_rating` |
  | vendas | `sales < modern_min_sales` |
  | preço | `price < modern_min_price` ou (`modern_max_price` > 0 e `price > modern_max_price`) |
  | desconto | `modern_min_discount` > 0 e `priceDiscountRate < modern_min_discount` (sem o campo = fora) |
  | nome | `modern_name_filter` preenchido e nenhum termo no nome (sem acento, minúsculas) |
  | top de afiliados | `modern_exclude_top` e `itemId ∈ top_ids` |
  | vitrine | `_position ≤ modern_showcase_cut` |
  | estourado | `modern_max_sales` > 0 e `sales > modern_max_sales` |
  | tipo recente | `product_type` ∈ tipos entregues/reservados nos últimos `modern_type_days` dias |

  **Pontuação** (comissão = mesma base conservadora do Legacy: `price × appExistRate`, senão `commission`):
  ```
  demanda  = sales se sales ≤ 1500, senão 1500 × 1500 / sales
  nota     = (rating / 5) ** 3
  comissão = sqrt(max(comissão_R$, 0.5))
  vendedor = 0.7 se sellerCommissionRate ≥ 0.05 (aceitar 5 ou 0.05: valor > 1 é porcentagem), senão 1.0
  vitrine  = 0.6 se posição ≤ 100, senão 1.0      (as ≤ corte já saíram no funil)
  frescor  = 1.4 se oferta ≤ 14 dias, 1.15 se ≤ 30, senão 1.0 (sem periodStartTime = 1.0)
  tendência= clamp(1 + growth, 0.5, 2.5); sem histórico = 1.0
  preço    = 1.0 até R$ 400; acima, (400 / preço) ** 0.8
  score    = demanda × nota × comissão × vendedor × vitrine × frescor × tendência × preço
  ```
  Concorrência: "baixa" se vendas ≤ 1500 **e** posição > 100 **e** extra do vendedor < 5%; senão "média".

  **Produto** = mesmo dicionário do Legacy (chaves `key, item_id, shop_id, name, shop, price, commission,
  commission_basis, rating, sales, image, link, expires, score, reasons`) **mais** `mode="modern"`, `position`,
  `source`, `discount`, `seller_rate`, `offer_days`, `growth`, `competition`, `product_type`, `factors` (dicionário
  com cada fator e `raw_score`). `reasons` lista cada fator em português com o valor e o efeito
  (ex.: "Tendência +68%/semana → ×1,68", "Tendência: sem histórico (neutro)").

  **Seleção**: ordena por `score`; respeita `per_shop`; nunca dois do mesmo `product_type`; com mais de uma fonte,
  primeiro no máximo `ceil(top_count / nº de fontes)` por fonte, depois completa sem a cota. Aviso quando
  selecionar menos que `top_count`.

- `apply_potential(candidates, verdicts)` (usado no M5): `score_final = score / maior × potencial / 10`; quem tem
  potencial ≥ 7 vem antes.

**`core.py`**
- `modern_query(page, limit, list_type=0, sort_type=2, category=None, keyword="")` — query nova, com os campos
  extras; `shopee_query` do Legacy **não muda**.
- `fetch_modern(settings, credentials, top=True)` → `dict(nodes, top_ids, warnings, requests)`: para cada fonte
  (categorias configuradas, ou "geral" sem categoria), páginas 1..`modern_pages` de 50 itens (`sortType 2`,
  palavra-chave se houver), anota `_position`/`_source`, para quando `hasNextPage` é falso. Top de afiliados
  (`listType 2`, `modern_top_pages` páginas) com cache do dia na tabela `flags` (`top_affiliates`); falha no top
  vira aviso e o fator fica neutro (nada é excluído por ele).
- `Engine.__init__` ganha `modern_fetcher` injetável (testes). `Engine.collect` ramifica pelo modo: Moderno →
  `fetch_modern` → `store.record_sales` → `rank_modern(..., history=store.sales_history(...),
  recent_types=store.recent_types(days))`. O resto (gravação de ofertas, análise, envio) é o mesmo.
- `Store.recent_types(days)`: tipos (`modern.product_type`) dos nomes congelados em `deliveries.snapshot` com
  estado `pending/sending/sent/unknown/refused` cujas consultas começaram nos últimos `days` dias.

**Interface**: o modal "Por que esta oferta?" já lista `reasons`; o card mostra "Concorrência: baixa/média" e o
modo da consulta.

**Testes**: cada corte do funil (um teste por motivo, com a contagem no aviso); a fórmula com o exemplo do
documento (piso vinílico: 206 vendas, nota 4,9, comissão R$ 7,69, +68%/semana, posição 118 → score ≈ 903,5);
saturação de vendas; fator de preço; vendedor 4% vs 5% e "5" vs "0.05"; frescor 10/20/40 dias; tendência
com/sem histórico e limites 0,5/2,5; tipo repetido entre consultas e dentro da seleção; cota por fonte;
`fetch_modern` com páginas falsas (posição contínua entre páginas, fim por `hasNextPage`, cache do top, falha do
top vira aviso); `collect` no Moderno grava `mode="modern"` e as vendas do dia.

### M3 — Roteiro do Modo Moderno

**`content.py`**
- `FORMATS` (7): `uso_real`, `problema_solucao`, `antes_depois`, `unboxing`, `pov_rotina`, `teste_desafio`,
  `detalhes_macro`, cada um com uma frase de instrução em português.
- `pick_format(recent, rng)`: sorteia entre os formatos fora dos 3 últimos usados.
- `modern_script_problems(script, name, banned_openings)`:
  - narração com 18 a 25 palavras;
  - a última frase cita "produto marcado";
  - não começa com "Olha"/"Gente" e não repete a abertura (2 primeiras palavras normalizadas) dos últimos 8 vídeos;
  - **todo número falado existe no nome do anúncio** (dígitos com vírgula/ponto normalizados; números por extenso
    de "dois" a "vinte", "trinta", "quarenta", "cinquenta", "cem", "mil" convertidos; "um/uma" ignorados);
  - cenas só com mãos: rejeita "rosto", "sorri…", "selfie", "olhando para a câmera" em gancho/demonstração/
    benefício/chamada.
- `modern_fallback_script(product)`: roteiro padrão só com mãos, sem "Olha", terminando em "produto marcado".
- `video_prompt(script, modern=False)`: no Moderno acrescenta "Somente mãos aparecem; nenhum rosto." e o formato.

**`analysis.py`**: `build_prompt(product, candidates, creative=None)` — com `creative` (Moderno) a TAREFA 3 recebe
formato obrigatório, regra de mãos, 18–25 palavras terminando em "produto marcado", aberturas proibidas e a regra
dos números. `make_check(candidates, extra=None)` soma os problemas extras. `finalize`/`fallback` gravam
`format` e usam o roteiro/prompt moderno.

**`core.py`**: `Engine.analyze(..., creative=None)`; `collect` no Moderno monta `creative` por produto a partir de
`Store.recent_creative(8)` (formatos e aberturas das últimas análises modernas selecionadas) e atualiza a lista
entre um produto e outro da mesma consulta.

**Testes**: cada regra do validador; formato nunca repete os 3 últimos; abertura repetida recusada e corrigida
pelo laço de feedback do `Runner`; legado continua aceitando a narração antiga ("Olha que prático…").

### M4 — Vídeo do Modo Moderno (worker + painel)

**Painel (`video.py`)**
- `_task`: se `product["mode"] == "modern"`, o payload ganha
  `finish=dict(size=[1080, 1920], badge=dict(rating="4,9", sold="206 vendidos"), frames=True)`. Worker antigo
  ignora a chave (compatível).
- Endpoint novo `POST /api/worker/tasks/<id>/frames` (JSON até 1 MB, só nesse caminho, mesmo token do worker):
  `{worker_id, frames: [base64 JPEG ×1–3]}`; valida tarefa reivindicada pelo mesmo worker, assinatura JPEG
  (`FF D8`), ≤ 300 KB cada; grava `video-<id>-f<n>.jpg` no diretório de vídeos.
- `finish(ok)`: no Moderno, a thread de entrega roda `check_fidelity(video_id)` antes de `deliver`.
  `check_fidelity`: `Runner.run` com a foto do anúncio (P) e os quadros (F1–F3, `data` base64, `image/jpeg`),
  schema `{score: int 0–10, reason: str}`, prazo 6 min; grava `meta.fidelity = {score, reason, provider}` ou
  `{error}`; apaga os quadros. Nota < `modern_fidelity_min` → `alert()` "Fidelidade N/10 no vídeo de <nome>:
  <motivo>. Confira antes de publicar." Nunca bloqueia a entrega (D5). `reconcile()` continua entregando vídeos
  `rendered` sem refazer a conferência.
- Card: `videoText` mostra "Fidelidade N/10 · motivo" (ou "não avaliada").

**Worker (`worker/subtitles.py`, `worker/studio_worker.py`)**
- `badge_events(duration, rating, sold, width, height)`: eventos ASS nos últimos 3,5 s — cápsula escura "★ 4,9 •
  206 vendidos" e botão laranja "CONFIRA NO PRODUTO MARCADO" pulsando (`\t` de escala a cada 0,5 s), acima da
  legenda falada (MarginV ≈ 33% e 26% da altura; a fala fica em 17%).
- `subtitle_video(..., finish=None)`: com `finish`, um único ffmpeg: fundo desfocado + vídeo centralizado em
  1080×1920 (serve para qualquer proporção), 30 fps, ASS com fala + selo, `loudnorm` e AAC 128k. Sem fala
  detectada, o selo e o redimensionamento continuam (não copia mais o arquivo cru).
- `extract_frames(source, folder)`: 3 quadros (20%, 50%, 80% da duração) do clipe **cru**, 360 px de largura, JPEG.
- `run_video`: com `payload["finish"]`, aplica o acabamento e envia os quadros (`/frames`) antes do vídeo; falha
  ao enviar quadros só registra no log. `save_image` reconhece JPEG em `data` (extensão `.jpg`). `VERSION` 1.2.0.

**Testes**: payload moderno × legado; endpoint `/frames` (token, worker errado, não-JPEG, grande demais, 4 quadros);
conferência com `FakeRunner` (nota alta, nota baixa → alerta, falha → "não avaliada", sempre entrega);
`badge_events` (horários nos últimos 3,5 s, escapes); com ffmpeg disponível, render real de um vídeo sintético
de 2 s 720×1280 → 1080×1920 com selo (teste pulado sem ffmpeg).

### M5 — Potencial de vídeo pela IA (opcional, chave desligada)

- `collect` no Moderno com `modern_video_potential`: depois do ranking, uma chamada `Runner.run` com os
  `min(12, max(6, 3 × top_count))` melhores (título + imagem; IDs C1…): schema `{items: [{id, score 0–10, reason}]}`.
  `modern.apply_potential` reordena; refaz a seleção. Falha do modelo → aviso e seleção só pelo score.
- Motivo no card: "Potencial de vídeo 8/10: <motivo>".
- Testes: reordenação, potencial < 7 só entra se faltar opção, falha mantém a seleção original.

### M6 — Documentação e entrega para teste

- `README.md`: seção "Modos: Legacy e Moderno" com funil, fórmula, roteiro, vídeo, fidelidade e o registro diário.
  `worker/README.md`: acabamento moderno e quadros. `PLANO.md`: ponteiro para este arquivo.
- Versão de teste isolada para o usuário (padrão do projeto): painel em `http://192.168.1.250:8792` com banco
  fictício (sem credenciais, sem worker, sem Telegram de produção), com uma consulta Legacy e uma Moderna de
  exemplo para conferir os cards, os motivos e as Preferências.
- Vídeo de exemplo do acabamento moderno gerado a partir de um vídeo já existente (sem gastar créditos do Flow).
- PR a partir do fork; **merge e deploy só com o OK do usuário** (deploy: backup do SQLite, tag de rollback,
  fora das 12:00, sem vídeo em andamento; o worker em produção só muda com `worker/install.sh`).

## 5. Critérios de aceite

1. Com `mode=legacy`, consultas, ranking, prompts e vídeos são idênticos aos de antes (testes antigos verdes,
   query Legacy inalterada).
2. Com `mode=modern`, nenhum item do top de afiliados, das primeiras 30 posições, acima de 5.000 vendas, com nota
   < 4,5, vendas < 50 ou fora de R$ 80–1.500 entra na seleção (valores padrão), e o aviso da consulta mostra a
   contagem de cada corte.
3. O exemplo do documento produz score ≈ 903 e concorrência "baixa".
4. Roteiro moderno recusado e corrigido quando: número falado fora do nome, abertura repetida, "Olha"/"Gente",
   rosto em cena, falta "produto marcado", menos de 18 ou mais de 25 palavras.
5. Vídeo moderno sai em 1080×1920 com selo nos últimos 3,5 s; fidelidade aparece no card; nota baixa gera alerta
   e não bloqueia a entrega.
6. Nenhuma regra inegociável da seção 1 regrediu.

## 6. Riscos

- Campos/argumentos da API não confirmados (M0). Mitigação: Legacy intocado; fator neutro para campo ausente.
- Mais requisições por consulta (10 páginas × categorias + 20 do top, com cache diário do top). Limite de taxa da
  Shopee desconhecido; os valores ficam ajustáveis em Preferências.
- Tendência só fica útil após ~1 semana de registro diário.
- Conferência de fidelidade usa a mesma cadeia de modelos: com o agy, soma ~1–3 min antes da entrega.
- Vídeo 1080×1920 fica maior (ainda bem abaixo dos 50 MB do Telegram).

## Andamento

| Entrega | Estado |
|---|---|
| M0 sondagem da API | — |
| M1 fundação | — |
| M2 ranking moderno | — |
| M3 roteiro moderno | — |
| M4 vídeo moderno | — |
| M5 potencial de vídeo | — |
| M6 docs e versão de teste | — |
