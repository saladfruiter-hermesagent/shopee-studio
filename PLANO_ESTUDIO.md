# Plano — Estúdio (geração de vídeo assistida, no painel)

> Estado: **S1 em produção; S2 implementada no PR do branch `feat/estudio-s2`, aguardando teste do usuário.** Inspirado
> no ShôStudio (shoradar.com.br), adaptado ao que o Shopee Studio já tem (Flow/Veo via worker, legendas, selo, entrega
> no Telegram).

## 0. Objetivo e hipótese

O vídeo automático de hoje nasce de **uma foto** (a única que a Open API devolve) e de um roteiro curto e
genérico. A IA inventa o que a foto não mostra (verso, escala, material, mecanismo) e o vídeo não tem gancho.

Hipótese a testar: com o usuário no processo — **fotos enviadas pelo celular, roteiro escolhido/editado e
prévia antes de entregar** — o vídeo fica mais fiel e mais forte e traz **mais cliques e vendas** que o
automático.

Escala combinada: **~2 vídeos por dia** feitos no Estúdio, durante algumas semanas, medindo o retorno.

## 1. Decisões

| # | Decisão | Estado |
|---|---|---|
| E1 | A consulta diária seleciona **4** produtos: os **2 mais bem colocados** vão sempre para o Estúdio (ficam esperando o usuário, sem geração automática) e os **outros 2** seguem o fluxo automático. A intenção é dar mais capricho aos melhores, não fazer uma comparação equilibrada. | confirmada pelo usuário (`top_count = 4` já salvo em produção, conferido em 10/11/2026) |
| E2 | Fotos extras vêm **do usuário, pelo celular**, na própria tela do Estúdio. Nenhuma raspagem da Shopee; a D10 (galeria automática abortada) continua valendo para o fluxo automático. | confirmada pelo usuário |
| E3 | Toda geração mostra o custo em créditos lido no compositor e só sai com aprovação explícita na tela (mesma regra 2 do README). Refazer uma cena é uma nova aprovação. Vale também para a aba Manual. | implementada na S2 (pedido do usuário em 12/11/2026) |
| E4 | Nada vai ao Telegram sem o usuário aprovar a prévia. | implementada na S2 (estado `review`) |
| E5 | Primeira entrega com **1 cena + várias referências**; várias cenas só depois de medir (ver §6, risco de vozes diferentes). | implementada na S2 (1 cena, até 4 referências) |
| E6 | Medição por vídeo: **vendas automáticas** pelo `conversionReport` (canal Shopee Video, cruzando `itemId`/`shopId`); **views e cliques no produto opcionais**, preenchidos pelo usuário quando quiser (campo rápido ou print lido por modelo). Sem os opcionais, a comparação usa só vendas. A API não tem views nem cliques (S0-b). | confirmada pelo usuário (opção 2) |
| E7 | Uso **só em casa** (Wi-Fi): o painel continua só na LAN, sem acesso remoto. | confirmada pelo usuário |
| E8 | Os vídeos **Do dia** (auto + Estúdio) entram no teto diário (`video_daily_credits`, hoje 100): 4 × 15 = 60 → sobram 40 (2 regerações). Várias cenas (S4) estouram: 2 auto (30) + 2 × 3 cenas (90) = 120. | confirmada pelo usuário; teto a rever na S4 |
| E9 | ~~Subir `top_count`~~ — resolvido pelo usuário (4). | encerrada |
| E10 | Produto do Estúdio **sem nenhuma geração enviada até 22h** (horário de Brasília) cai para o fluxo automático: entra na fila normal do worker, com a foto da API e o roteiro automático, contando no teto do dia (os 15 créditos já estavam previstos em E8). Rascunho aberto no Estúdio (fotos, roteiro escolhido) **não** impede o fallback e não é usado por ele; basta ter **uma** geração enviada (mesmo falha ou na fila) para o produto ficar com o Estúdio. Com a fila do Flow lenta, a entrega pode passar da meia-noite — o crédito conta no dia em que entrou na fila. | confirmada pelo usuário |
| E11 | Aba **Manual**: o usuário cola o link de qualquer produto e faz o vídeo pelo mesmo fluxo (fotos → ficha → formato → roteiro → gerar → prévia → Telegram), quando quiser e **sem limite de quantidade**. **Não entra no teto diário** (o `enqueue` pula a checagem de `video_daily_credits` para origem `manual`); só a aprovação de custo por geração (E3) segura o gasto. Origem `manual` na medição. | confirmada pelo usuário |
| E12 | Estilo de roteiro **Problema real + chamadas na tela** para os novos testes (pedido em 12/11/2026): anúncio curtíssimo que parte de um problema real, câmera que chama atenção, 3 chamadas estilizadas no meio do vídeo para a pessoa tocar porque não pode perder, sem legenda da narração; o roteiro reforça que sem o produto o dia a dia seria pior. Fica ao lado do Clássico (escolha por rascunho). Limite mantido: a urgência vem do problema; preço, desconto, estoque, "corra", "só hoje" e "últimas unidades" continuam proibidos (não dá para provar). Ajuste em 12/11/2026: fala de **14–19 palavras** (23 enchiam os 10 s e o vídeo parecia cortado) e cenas de 8–10 s em plano aberto, câmera parada e produto inteiro, com ~2 s de respiro; fim sem selo de nota/vendas e sem botão (o clique fica com a 3ª chamada). | confirmada pelo usuário; implementada (worker 1.4.0) |

## 2. Fluxo da tela (celular primeiro)

1. **Produto** — duas entradas para o mesmo fluxo:
   - **Do dia**: um dos 2 produtos reservados ao Estúdio pela consulta diária (E1).
   - **Manual** (E11): colar o link da Shopee (completo `…-i.<shop>.<item>`, `/product/<shop>/<item>` ou curto
     `s.shopee.com.br`/`shp.ee`). O link curto é resolvido lendo só o redirecionamento (cabeçalho `Location`), sem
     abrir a página — não depende de login nem esbarra no captcha. Com `itemId`/`shopId`, busca a oferta na Open
     API (nome, preço, comissão, vendas, nota, foto, `offerLink`). Avisos na tela, sem bloquear: produto **sem
     oferta de afiliado** (sem comissão — não faz sentido postar), já **entregue antes** (repetido), ou na lista
     dos **mais divulgados** (`listType 2`, que o Modo Moderno já baixa por dia = concorrência alta).
2. **Fotos** — a foto da API já vem; o usuário adiciona até ~6 fotos (galeria do celular ou câmera). O navegador
   reduz para ~1080 px JPEG antes de enviar. Marca qual é a principal.
3. **Ficha visual** — o modelo (agy/fallbacks) olha as fotos e devolve fatos visíveis: cor, material, tamanho
   aparente, partes, como se usa, texto da embalagem. O usuário corrige se precisar. O roteiro e o prompt **só
   podem citar o que está na ficha**, que é o que reduz a invenção.
4. **Formato** (alterado a pedido do usuário em 11/11: os 6 do ShôStudio) — POV, Hands-on, Selfie frontal, Falando para a câmera, Provador, Formato livre (UGC); a narrativa (problema→solução, antes/depois, teste…) virou a abordagem de cada um dos 3 roteiros. Antes: POV, só mãos, unboxing, antes/depois, provador, problema→solução… cada um com instrução de
   câmera e ação.
5. **Roteiro** — o modelo propõe **3 versões** com estrutura fixa: gancho (0–2 s, visual + frase), demonstração,
   prova/resultado visível, chamada para o produto marcado. O usuário escolhe uma e edita à vontade. As regras de
   hoje (`modern_script_problems`: números só do nome, sem abertura repetida, sem promessa) continuam valendo.
6. **Gerar** — mostra referências, prompt final e o custo; o usuário aprova; vai para a fila do worker.
7. **Prévia** — o vídeo pronto dentro de uma moldura que imita a Shopee Vídeo (só CSS no painel). Ações:
   aprovar e enviar ao Telegram, regerar (novo custo), descartar.
8. **Resultado** (D+3 e D+7) — campos para views, cliques no produto e vendas daquele vídeo (§5).

## 3. Peças técnicas (o que muda onde)

- **Painel (`app.py`)**: hoje o corpo de requisição é limitado a 64 KB, com exceção só para o worker. Precisa de
  uma rota de upload de foto com limite próprio (ex.: 2 MB por foto, sessão logada + CSRF), validação de JPEG/PNG
  e gravação no volume `/data`. Novas tabelas para sessão do Estúdio (produto, fotos, ficha, formato, roteiros,
  escolha, custo, estado) e métricas por vídeo.
- **Front (`templates/index.html`, `static/app.js`)**: nova aba "Estúdio", pensada para 390 px.
- **Tarefa do worker (`video.py`)**: payload passa de `image_url` única para `images: [...]` (compatível: worker
  antigo usa a primeira). Reaproveita `finish` (1080×1920, selo, quadros, fidelidade).
- **Worker (`worker/flow.py`)**: anexar várias imagens. O compositor já está em **Elementos** e aceitou 4
  referências pelo mesmo custo (S0-a). `attach_image` passa a ser chamado em laço; o worker deve **conferir** que
  o modo é Elementos (hoje não confere; só herda o que está salvo) e que a caixa tem exatamente N anexos.
- **Fidelidade**: passa a comparar os quadros com **todas** as fotos, não só a da API.

## 4. Etapas

| Etapa | Conteúdo | Gasta crédito? |
|---|---|---|
| S0 | Sondagens: (a) Flow com 2–3 imagens em "Elementos", só `dry_run` (anexa, lê custo, não envia); (b) campos do `conversionReport` — dá para separar vendas por item e por canal Shopee Vídeo?; (c) onde o app mostra views/cliques/vendas por vídeo; (d) `productOfferV2` filtrando por `itemId`/`shopId` (o código ainda não usa) e o que vem para produto fora do programa; (e) resolver link curto só pelo redirecionamento, de dentro do container | não |
| S1 | Tela do Estúdio até o roteiro: abas **Do dia** e **Manual**, produto, upload, ficha visual, formatos, 3 roteiros editáveis; reserva das posições 1–2 para o Estúdio e o fallback das 22h (E10) | não (só chamadas de modelo) |
| S2 | Geração de 1 cena com várias referências, prévia na moldura, aprovar → Telegram | sim, 1 geração de teste combinada antes |
| S3 | Registro de métricas por vídeo e comparação Estúdio × automático no painel | não |
| S4 | Várias cenas (até 3) com narração única (§6) — só se S3 mostrar ganho | sim |

Cada etapa = branch + PR, testes verdes e versão de teste no pc1 antes do merge.

### Resultados da S0 (10/11/2026, nada gerado, nenhum crédito gasto)

| Item | Resultado |
|---|---|
| (a) Flow com várias imagens | O compositor **já está** em `Vídeo · Elementos · 9:16 · 720p · 10s · x1` com **Omni 1.1 Flash** (o worker hoje anexa 1 imagem nesse modo). Anexando imagens já existentes da biblioteca do projeto, **4 entraram sem aviso de limite** (4 miniaturas na caixa, confirmado em captura) e o custo continuou **15 créditos**. Cada anexo vira um botão `Elemento` na caixa; clicar nele remove. Depois da sonda a caixa ficou limpa (0 anexos, sem texto) e as configurações intactas. Limite real acima de 4 não foi testado. O seletor de mídia também tem abas **Vozes, Personagens e Avatares** — candidatas para manter a mesma voz entre cenas na S4 (não exploradas). |
| (b) `conversionReport` | Traz por item vendido: `itemId`, `shopId`, `channelType` (todas as 15 vendas de 30 dias = **"Shopee Video"**), `referrer` (`Shopeevideo-Shopee`), `attributionType`, status, comissão e `clickTime`/`purchaseTime`. **Vendas por produto dá para puxar automaticamente**; `utmContent` vem vazio (sem subId na marcação do vídeo). Só **2 das 15** vendas são de produtos que o painel já sugeriu. |
| (c) métricas por vídeo no app | dispensado: views e cliques ficaram opcionais (E6) |
| (d) produto pelo ID | `productOfferV2(itemId:…, shopId:…)` e só `itemId` devolvem o produto com nome, comissão, vendas etc.; ID inexistente devolve lista vazia (sem erro) → "fora do programa" = lista vazia. |
| (e) link curto | `s.shopee.com.br/…` responde **301** com `Location: shopee.com.br/opaanlp/<shopId>/<itemId>` — resolvido de dentro do container sem abrir a página nem esbarrar em captcha. |

## 5. Medição (o que define "deu certo")

O vídeo é postado na **Shopee Vídeo com o produto marcado**, então o clique vem da marcação no app, não do link.
Isso derruba a ideia de medir por `subId` no link (que valeria só para links compartilhados fora do app).
A Open API **não tem endpoint de cliques**, mas o `conversionReport` traz as **vendas por item no canal Shopee
Video** (S0-b): a coluna "vendas" pode ser preenchida automaticamente, cruzando `itemId`/`shopId` do vídeo. Ficam
manuais só **views e cliques no produto**.

Proposta (E6, confirmada): para **os 4 vídeos do dia** (2 do Estúdio, 2 automáticos) e para os **manuais**, o
painel guarda um registro por vídeo. **Vendas** entram sozinhas (consulta diária ao `conversionReport`, janela de
alguns dias após a entrega). **Views e cliques** são campos opcionais, preenchidos quando o usuário quiser (lista
"sem métrica" com vídeos de 3+ dias; print lido por modelo opcional). Com ~15 vendas/mês no total, só vendas é um
sinal lento: a comparação fica mais rápida quando há views/cliques. Em ~15 dias são ~30 vídeos do Estúdio e ~30
automáticos, além dos manuais.

**Leitura dos números (decisão do usuário, sem equilíbrio):** o Estúdio fica sempre com as posições 1 e 2, porque
a intenção é caprichar nos melhores. Por isso a comparação Estúdio × automático mistura dois efeitos (produto
melhor + vídeo melhor). Serve para acompanhar o retorno ao longo do tempo, não para provar que só o vídeo fez a
diferença. O painel mostra as três origens separadas: Estúdio, automático, manual.

Critério sugerido para seguir para a S4: views, cliques e taxa de clique (cliques/views) do Estúdio subindo de
forma consistente nas semanas de uso.

## 6. Riscos

- **Vozes diferentes entre cenas**: o Flow gera a fala junto de cada clipe; juntar 3 cenas pode trocar a voz no
  meio. Saída provável: gerar cenas sem fala e uma narração única (TTS) por cima no worker. Por isso fica na S4.
- **Créditos**: 1 cena ≈ 15 créditos; 3 cenas ≈ 45; regerar soma. Do dia: 4 × 15 = 60 dentro do teto de 100.
  **Manuais ficam fora do teto e sem limite (E11)**: o gasto deles só aparece no saldo do Flow. O painel mostra
  "créditos gastos em manuais hoje/no mês" para não haver surpresa, e a aprovação por geração continua obrigatória.
- **Fila do Flow lenta** (até ~1 h em horários de pico): a prévia pode demorar; a tela precisa mostrar o estado.
- **Medição manual** pode ser esquecida: a lista de pendentes no painel deixa visível o que falta.
- Fotos enviadas podem ter rosto/dados pessoais: ficam só no volume, apagadas junto da sessão.

## 7. Perguntas em aberto

Nenhuma decisão pendente.

### Decisões da S2 tomadas sem perguntar

- **Até 4 referências** no Flow (a principal primeiro, depois a ordem das fotos): é o que a S0 testou sem aviso de
  limite e pelo mesmo custo. Fotos além disso servem à ficha visual, não ao vídeo.
- **Worker mínimo 1.4.0** (era 1.3.0 antes do estilo com chamadas) para gerar no Estúdio: o merge não atualiza a VM, e o worker antigo ignoraria as fotos extras
  (geraria com uma só). O painel recusa com a instrução de rodar `worker/install.sh`.
- O vídeo pronto do Estúdio é um estado próprio (`review`), não o `rendered` do automático: assim a entrega periódica
  (`reconcile`) nunca o manda sozinho.
- **Arquivo mantido depois do envio** (para rever e baixar e postar na Shopee Vídeo); sai ao descartar ou excluir o
  rascunho. Os registros em `videos` ficam com os créditos.
- **Gerar de novo depois de "incerto"** é permitido, com aviso explícito no `confirm` para conferir o Flow antes (no
  automático não há botão nesse caso). Depois de **enviado**, o rascunho não gera outro: crie outro rascunho.
- A legenda do Telegram é a **salva no momento do envio** (pode ser editada depois da geração). No Do dia, os links são
  os congelados no envio da consulta (com os equivalentes).
- Descartar não devolve créditos (já foram gastos no Flow) e mantém o produto do dia com o Estúdio (não cai no corte).

## Andamento

| Etapa | Como ficou | Estado |
|---|---|---|
| S0 | Sondagens sem crédito (seção 4) | feita em 10/11/2026 |
| S1 | Menu **Estúdio** (abas Do dia / Manual), `studio.py`: reserva dos N melhores de cada envio (`studio_slots`, padrão **0** — produção não muda até o usuário pôr **2** em Preferências → Vídeo), corte `studio_cutoff` (22:00) com fallback para o vídeo automático quando não há **nenhuma** linha em `videos` do produto; aba Manual por link (curto ou completo) + `productOfferV2(itemId, shopId)`; até 6 fotos do celular (reduzidas no navegador); ficha visual e 3 roteiros pelo modelo, em segundo plano; edição com as regras mostradas sem bloquear; prompt do Flow do roteiro escolhido. Sem geração, sem crédito. | implementada, testes `tests/test_estudio.py`; aguardando teste do usuário no pc1 |
| S2 | Passo **5 · Vídeo** do rascunho: até 4 referências (principal primeiro) em **Elementos**, `confirm` com custo/teto e recusa se o custo mudou; Do dia no teto, Manual fora (gasto do dia/mês na tela); worker 1.3.0 baixa as fotos do usuário do painel e confere N anexos; vídeo pronto fica em `review` com prévia numa moldura da Shopee Vídeo, fidelidade contra as referências e ações enviar / descartar e gerar outro / descartar; arquivo mantido para rever e baixar. Validação sem crédito no Flow real: 2 anexos, custo 15, nada enviado. | implementada, testes `tests/test_estudio_video.py` (178 no total); falta o teste do usuário e **1 geração real combinada** |
| S3 | Métricas por vídeo (vendas automáticas do `conversionReport`, views/cliques opcionais) | pendente |
| S4 | Várias cenas com narração única | pendente, depende da S3 |
