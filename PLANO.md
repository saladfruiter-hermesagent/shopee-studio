# Plano — Shopee Studio: pendências

## Estado atual (11/11/2026)

| Peça | Situação |
|---|---|
| Produção | thevault, imagem `ghcr.io/saladfruiter/shopee-studio:sha-cc51d32`, container `healthy`. Deploy em 11/11/2026 19:48 (BRT). |
| Acesso | **`https://shopee.home.local`** (Caddy2 do thevault, TLS pela CA interna, `reverse_proxy 192.168.1.90:8787`; registro A no Pi-hole) e `http://192.168.1.90:8787` na LAN. |
| Preferências em produção | modo **Moderno**, `top_count` 4, vídeo ligado, teto **100** créditos/dia, custo **15** por vídeo, `studio_slots` 2, consulta diária 06:00, snapshot e corte do Estúdio 22:00. |
| Conferido no deploy | HTML e `/static` em `gzip`; `/static/*?v=<sha>` com `Cache-Control: public, max-age=31536000, immutable`, e sem `?v` em `no-store`; `<meta name="color-scheme" content="light dark">` presente. |
| Entregas no ar | busca de equivalentes + mensagem única por produto (PR #15), Estúdio S1 (PR #28), painel dos PRs #29 e #30. |
| Testes | `python -m unittest discover -s tests` → 165 OK. |

Planos irmãos: [`PLANO_MODO_MODERNO.md`](PLANO_MODO_MODERNO.md) (seleção anticoncorrência, roteiro variado, selo e
fidelidade do vídeo — **nenhuma decisão pendente**) e [`PLANO_ESTUDIO.md`](PLANO_ESTUDIO.md) (geração assistida, S2–S4
abertas). O comportamento implementado está no `README.md`; este documento guarda só o que falta.

## 1. Verificações que dependem do usuário

1. **Mensagem única no Telegram real (aceite E3.6):** conferir no cliente que o produto chega como **uma** mensagem,
   com o vídeo legendado, a descrição copiável e os links numerados abrindo. Não gasta crédito — serve o primeiro
   vídeo entregue depois do deploy.
2. **Revisão visual dos 18 links aprovados** da comparação real de 31/10 (fotos lado a lado do principal e de cada
   aprovado); ver a última seção deste documento.
3. **Estúdio S1 no `pc1`/celular:** abas Do dia/Manual, fotos do celular, ficha visual e os 3 roteiros. Está em uso em
   produção (`studio_slots` 2), mas nunca foi conferido por você no navegador.

## 2. Painel — o que sobrou da auditoria de front (modern-web-guidance)

**PWA instalável (12/11/2026):** `/manifest.webmanifest` + `/sw.js` + ícones `static/pwa-*` e o registro no
`app.js`. `Page.getAppManifest` e `Page.getInstallabilityErrors` do Chrome voltam vazios, e o service worker
ativa com escopo `/`. Falta o usuário instalar no Android (`https://shopee.home.local` → ⋮ → Instalar
aplicativo); se o Chrome reclamar do certificado, a CA do Caddy precisa estar instalada no aparelho.

P1 a P8 estão implementados e no ar (tabela abaixo). Restam, por decisão sua:

- **Contraste do tema claro:** 6 pares abaixo de 4,5:1 que **já existiam** antes do modo escuro (`--muted` em textos
  de 10–11 px, `nav-label`, rodapé da sidebar, pílula de filtro, símbolo do estado vazio). Mexer neles muda o visual
  claro, por isso ficaram fora. PR pequeno, só de tokens.
- **Porta 8787 publicada em HTTP:** o Caddy já serve HTTPS; parar de publicar a porta deixa o painel só no endereço
  seguro. Mexe no `compose.yaml`, depende de confirmação.
- **`COOKIE_SECURE=true`:** o `README` recomenda com proxy HTTPS e o container roda com o padrão (`false`). Ligar
  endurece o cookie de sessão e **derruba o login por `http://192.168.1.90:8787`** — só faz sentido junto do item
  anterior. Precisa de `Update Stack`.
- **`log` no bloco `shopee.home.local` do Caddy:** hoje só o `ds` tem log de acesso. Serve para diagnosticar problema
  de login/cópia pelo celular (`docker logs Caddy2`).

Auditoria em 11/11/2026 no código que roda em produção. O `legacy-scan` do modern-web-guidance deu **0 achados** nas
33 guias com regra determinística: o ganho não era remover padrão antigo, e sim aplicar padrão moderno ausente.

| # | Item | Estado |
|---|---|---|
| P1 | Modal aberto deixa o resto da página inerte: o retorno de "Copiar texto" passou a ser `#copy-status` (`role="status"` no diálogo + texto do botão) e `toast()` enfileira a mensagem para aparecer quando o modal fechar | feito |
| P2 | Polling de 5 s só redesenha cards e histórico quando a assinatura dos dados muda; foco do card é restaurado depois do render; `content-visibility: auto` + `contain-intrinsic-size` nos cards a partir do 7º | feito |
| P3 | `view()` com `document.startViewTransition` (sem `prefers-reduced-motion: reduce`), foco no título da seção e `aria-current="page"` na aba | feito |
| P4 | `<dialog>` com `aria-labelledby`, `closedby="any"` e `overscroll-behavior: contain` no diálogo e no `::backdrop` | feito |
| P5 | Preferências: `novalidate` + validação por campo (preço, vendas, notas, categorias, URL do Flow, modelo, pesos 100), `aria-invalid`/`aria-describedby`, foco no primeiro problema, aviso fixo para recusa do servidor, `<fieldset>/<legend>` nos grupos e custo em créditos no botão de confirmar envio | feito |
| P6 | Estúdio: input de arquivo visualmente escondido (mas focável, com anel no label), abas com `role="tab"`/`aria-selected`/`aria-controls` + setas/Home/End, fotos com `width`/`height`, botões de foto com 44 px de alvo; imagens da grade com `fetchpriority` na primeira fileira | feito |
| P7 | Modo escuro: 57 tokens com `light-dark()` cobrindo os 106 usos de cor, `<meta name="color-scheme">`, paleta clara como fallback dentro de `@supports` (navegador antigo vê o de antes), contraste conferido por cálculo (branco/verde 8,8:1; painel verde 4,9–6,4:1) | feito |
| P8 | Servidor: `/static` com `?v=<APP_VERSION>` em cache `immutable` e HTML/API em `no-store`; gzip nas respostas de texto (app.js 35.801 → 12.519 B, style.css 20.308 → 6.528 B, HTML 22 KB → 7,7 KB), `Vary: Accept-Encoding`, Range/206 e ETag tratados | feito |
| P8b | HTTPS na LAN: `https://shopee.home.local` no Caddy2 do thevault (CA interna), com `isSecureContext=true` e `navigator.clipboard` disponível — o "Copiar texto" deixou de depender de `execCommand` | feito em 11/11 |

Medições antes/depois (banco fictício com 200 ofertas, Playwright, `127.0.0.1`): `refresh()` 52–56 ms com 5.800 nós
reconstruídos a cada 5 s → **0 reconstruções** quando nada muda; foco no `select` de triagem sobrevivia 6,5 s antes de
ir para o `body` → **permanece**; toast com modal aberto ficava atrás do diálogo (`elementFromPoint` devolvia `modal`)
→ a mensagem de cópia aparece dentro do modal e os demais avisos esperam o fechamento.

Nada aqui gasta crédito do Flow, chama a Shopee ou o Telegram: é `templates/index.html`, `static/app.js`,
`static/studio.js`, `static/style.css` e o `after_request` do `app.py`, validados com banco fictício (receita em
`references/painel-validacao-local.md` da skill).

## 3. Estúdio — [`PLANO_ESTUDIO.md`](PLANO_ESTUDIO.md)

- **S2 (implementada, aguardando seu teste):** passo 5 do rascunho — gerar no Flow com até 4 referências (Elementos),
  aprovação do custo, prévia na moldura da Shopee Vídeo, enviar/descartar; Manual fora do teto. Para entrar em produção:
  merge + `Update Stack` **e** atualizar o worker da VM para 1.3.0 (`worker/install.sh`). **Gasta créditos** — a
  primeira geração real (15 créditos) é combinada com você.
- **S3:** métricas por vídeo (vendas automáticas do `conversionReport`; views/cliques opcionais).
- **S4:** várias cenas com narração única — depende da S3.

## 4. Critérios de aceite ainda não verificados com dados reais

- Kit diferente e formato diferente: na comparação real com a regra por foto, kits de 10 potes, calças slim e
  espelhos de outro recorte saíram como diferentes. Medida conflitante nos dois títulos é rejeitada pelo código
  (testado).
- Mensagem única recebida no cliente Telegram do usuário, com a descrição copiável e todos os links abrindo.

## 5. Detalhes adicionais dos anúncios — etapa condicionada

Com a regra por foto, esta etapa deixou de ser necessária por enquanto. Se o piloto voltar a ficar limitado por falta
de voltagem, composição ou variante, investigar uma fonte acessível de detalhes do anúncio. O fluxo atual compara
título e imagem; não presumir que especificações e variantes estejam disponíveis na consulta usada. Validar uma fonte
real antes de integrar, registrar origem dos atributos e manter incerto quando não houver confirmação. Não contornar
login/CAPTCHA nem tratar conteúdo do anúncio como instruções. Essa etapa depende dos resultados do piloto.

## 6. Riscos

- Mais consultas e rodadas aumentam latência e uso de quota. A busca não garante seis links; os critérios de
  identidade continuam.
- O envio depende do vídeo pronto: falha de geração, teto atingido ou vídeo desligado deixam produtos sem entrega até
  intervenção, com o motivo visível no card (política confirmada pelo usuário).
- Não alterar teto de créditos, segurança do worker, prevenção de geração duplicada ou fluxo de login/2FA/CAPTCHA.
- Arquivos não rastreados fora do escopo: `daily_shopee_top5.ps1`, `docker-schedule.sh`, `get_telegram_chat_id.ps1`,
  `install_daily_task.ps1`.

## Regra de equivalência (definida pelo usuário em 31/10/2026)

Não precisa ser completamente igual: basta ser o mesmo produto na comparação por foto. Bem semelhante é aceito.

- Outro ângulo, fundo, iluminação, foto de outro vendedor ou pequenas diferenças de apresentação não impedem a
  aprovação. Falta de marca, voltagem ou modelo no título também não.
- Fica de fora o que, na foto, é outro produto (formato, design, cor ou componentes) e o que o título informa com
  medida, capacidade, voltagem ou quantidade de kit diferente da informada pelo principal. Esse conflito de medidas
  também é conferido no código, mesmo quando o modelo aprova.
- Incerto fica só para foto que não permite comparar. Incertos continuam fora dos links.
- Substitui a regra anterior ("foto igual não prova identidade; não inferir marca, voltagem ou variante ausente").

## Decisões tomadas sem perguntar

- Com a regra por foto, medida, capacidade, voltagem ou kit informados de forma diferente nos dois títulos continuam
  rejeitando (no modelo e no código). Valor ausente em um dos lados não conta como diferença.
- `candidate_budget` continua sendo o tamanho do lote (8; 0 desliga a busca). O total de 24 é um campo novo
  (`candidate_total`), assim como consultas, páginas, buscas, rodadas e prazo; todos ficam em Preferências → Limites
  da busca.
- Produto aguardando vídeo fica reservado (excluído de novas consultas). Produto com falha antes do Telegram volta a
  ser elegível; se outra consulta já o enviou, "Gerar vídeo ↻" é recusado.
- Vídeo recusado pelo Telegram mantém o produto reservado enquanto o arquivo espera o "Reenviar vídeo".
- Pausa da fila, teto atingido e vídeo desligado deixam o produto "não entregue" (com "Gerar vídeo ↻"), não pendente.
- A prévia de uma consulta já enviada mostra a mensagem congelada e o estado de cada produto.
- Fallback do modelo continua entregando o texto automático, agora com o aviso dentro da legenda do vídeo.

## Histórico — entregas E1 a E3 (todas em produção)

Resumo do que foi implementado e testado; o detalhe de comportamento está no `README.md`.

- **E1 — busca de equivalentes:** até 3 consultas montadas do título sem perder medidas, decimais, quantidades e
  códigos (`2,3litros`, `170x70cm`, `GDC 150`, `kit 20`, `5.0`, `i12`); coleta limitada (páginas 1 de todas as
  consultas antes das 2, até 2 páginas e 6 buscas por produto), deduplicação, descartes por motivo exclusivo e
  consulta que falha não derruba as outras; ordenação que coloca medida conflitante (127V × 220V) por último.
- **E2 — avaliação em rodadas:** lotes de 8, até 3 rodadas e 24 candidatos, parando em 5 aprovados; a 1ª rodada
  escreve legenda e roteiro e as seguintes só comparam; prazo único de 10 min por produto (busca + modelo + novas
  tentativas + fallbacks); diagnóstico persistido em `analysis.search` e exposto no card.
- **E3 — entrega:** uma mensagem por produto (`sendVideo`), retrato congelado em `deliveries.snapshot`/
  `videos.snapshot`, limite de 1.024 caracteres na legenda (bloqueia o produto sem cortar links), estados de entrega
  idempotentes e migração só aditiva.

## Histórico — comparação real da execução #29 (31/10/2026)

Evidência que sustenta a regra de equivalência; não é tarefa pendente. Páginas reais gravadas em
`scripts/fixtures/run29.json`, sem credenciais e sem links de afiliado. Modelo: agy Gemini 3.8 Flash na VM.

| Produto | Ao modelo | Avaliação | Links | Encerrou | Tempo |
|---|---|---|---|---|---|
| Bicicleta ergométrica 120kg | 8 | 8 mesmo produto | 6 | 5 equivalentes (1 rodada) | 64 s |
| Kit 20 potes 2,3L/1,1L/480ml | 11 | 4 mesmo produto · 7 diferentes | 5 | resultados esgotados | 143 s |
| Serra mármore Bosch GDC 150 | 8 | 8 mesmo produto (127V e 220V) | 6 | 5 equivalentes | 62 s |
| Espelho orgânico 170x70 | 24 | 2 mesmo produto · 22 diferentes | 3 | orçamento de candidatos | 254 s |
| Kit calças jeans skinny | 24 | 2 mesmo produto · 22 diferentes | 3 | orçamento de candidatos | 272 s |

- **18 links novos** nos 5 produtos (antes: 0), cerca de 13 minutos no total. Risco aceito: um link pode levar a uma
  variante não informada no título (ex.: serra 127V × 220V quando o principal não diz a voltagem) — daí a revisão
  visual do item 2 da seção 1.
- A busca progressiva levou ao modelo **107 candidatos contra 29** da busca antiga (88 fora da antiga). Com a regra
  estrita anterior nenhum link era aprovado: o gargalo era a evidência exigida, não a busca.
- Custo de tempo: 3,4 a 10 minutos por produto (60–100 s por rodada); a análise diária de 5 produtos sai de ~6 para
  ~25 minutos.
