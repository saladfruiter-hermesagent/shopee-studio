# Shopee Studio

Painel pessoal para pesquisar, selecionar e acompanhar ofertas de afiliados Shopee. Roda em um container no Unraid, com SQLite persistente e interface em português.

## O que faz

- Consulta a API oficial, trata respostas sem campo errors e repete falhas transitórias até três vezes.
- Filtra nicho, preço, avaliação, vendas e comissão; explica a classificação.
- Consulta produtos diretamente, sem rodízio ou filtro de categoria. Analisa até o limite de páginas configurado e classifica as ofertas encontradas por comissão, avaliação e vendas.
- Usa comissão estimada de cliente existente quando disponível, sem misturar taxas de novos clientes. Normalização logarítmica reduz a influência de valores extremos.
- Respeita o limite por loja na seleção.
- Exclui permanentemente das novas consultas os anúncios já enviados ao Telegram, identificados por loja e item. Uma entrega incerta também fica excluída para evitar um possível reenvio; produtos descartados ou já divulgados ficam excluídos até mudar sua triagem.
- Histórico de consultas, preços observados, decisões e entregas; SQLite persiste nas atualizações.
- Consulta manual apenas prepara a prévia. Envio manual exige confirmar no painel.
- Configuração do Telegram pela interface: token, chat/grupo e tópico (message_thread_id). Token vazio preserva o já salvo; tópico vazio remove o destino de tópico.
- Para cada produto selecionado, busca anúncios do mesmo produto e pede a um modelo de visão para compará-los pela foto. Regra definida pelo usuário: basta ser o mesmo produto na foto; a foto não precisa ser idêntica, e um anúncio bem semelhante é aceito. Falta de marca, voltagem ou modelo no título não impede. Fica de fora o que, na foto, é outro produto e o que o título informa com medida, capacidade, voltagem ou kit diferente do principal; o painel confere esse conflito de medidas também no código, mesmo que o modelo aprove. Incerto fica reservado para foto que não permite comparar. Cada produto sai com até 6 links: o selecionado e até 5 equivalentes, numa lista única.
- A busca de equivalentes é progressiva e limitada (padrões editáveis em Preferências → Limites da busca):
  - Monta até 3 consultas do título sem perder medidas, decimais, quantidades e códigos (`2,3litros`, `170x70cm`, `GDC 150`, `kit 20`): identidade + atributos, a mesma medida com outra grafia (`2,3l`) e uma consulta mais ampla, sem palavras comerciais.
  - Busca primeiro a página 1 de cada consulta; a página 2 só quando faltam candidatos (até 2 páginas de 50 e 6 buscas por produto; novas tentativas HTTP são contadas à parte).
  - Remove repetidos, o próprio anúncio, anúncios já enviados, descartados ou usados nesta consulta, sem imagem, fora de 0,5× a 2× o preço ou com título diferente demais. Ordena primeiro quem mantém as medidas do anúncio e por último quem tem medida conflitante (127V × 220V), alternando entre as consultas.
  - Compara em lotes de 8, até 3 rodadas e 24 candidatos. A primeira rodada também escreve a legenda e o roteiro; as seguintes só comparam anúncios novos. Para ao achar 5 equivalentes, ao esgotar resultados ou no prazo de 10 minutos por produto (vale para chamadas, novas tentativas e fallbacks). Uma rodada que falha não apaga os aprovados das anteriores. Lote 0 desliga a busca.
  - O card mostra o resumo ("Busca: 1 mesmo produto · 15 incertos · 8 diferentes"), com detalhes expansíveis: consultas e páginas, resultados brutos e únicos, descartes por motivo, rodadas, motivo de encerramento e duração. Consultas antigas, sem esse diagnóstico, continuam legíveis.
- O mesmo modelo escreve a legenda (frase + 2 ou 3 hashtags, no máximo 150 caracteres no total) e o roteiro de 10 segundos (gancho, demonstração, benefício, chamada e narração). O código confere a contagem de caracteres (UTF-16), as hashtags proibidas (#Shopee, #Achadinhos, #Viral, #Fyp…), emojis, reticências e promessas de preço, desconto, frete ou urgência. Resposta fora das regras volta ao modelo para correção. Não há título de vídeo separado.
- Modelo configurável em Preferências: principal + fallbacks em ordem. Padrão de fábrica: Antigravity CLI (agy) com gemini-3.8-flash-medium e gemini-4-argon-medium como fallback, executados pelo worker da VM (veja worker/README.md). Também aceita endpoints OpenAI-compatíveis (OpenRouter, llama.cpp) com chave protegida, modelo visual e modelo de texto opcional. Ajuste ali o tempo limite, as novas tentativas e quantos candidatos são comparados. O botão Testar visão manda uma imagem real ao modelo. Se todos os modelos falharem, o produto sai com o próprio link e um texto automático marcado como "sem validação do modelo".
- No Telegram, cada produto chega numa única mensagem: o vídeo legendado, com o nome curto, a descrição + hashtags num bloco copiável e os links numerados na legenda do próprio vídeo. Nada é enviado antes de o vídeo ficar pronto: não há foto antes nem prompt em mensagem separada (o prompt e o roteiro ficam no painel). A foto do anúncio continua sendo usada pelo modelo e pelo Flow. A legenda do vídeo tem limite de 1.024 caracteres no Telegram: o painel encurta primeiro o nome e, se mesmo assim não couber, bloqueia aquele produto com o motivo no card. Links nunca são cortados nem removidos.
- Agenda diariamente em America/Sao_Paulo, recupera o horário perdido do dia ao reiniciar e tenta falhas novamente após 5 e 10 minutos.
- Entrega incerta não é repetida automaticamente: confira o Telegram. Não há garantia de exactly-once na API do Telegram.
- Estados da entrega por produto: aguardando vídeo, entregue, não entregue (com o motivo e "Gerar vídeo ↻"), recusado pelo Telegram (arquivo guardado, "Reenviar vídeo" sem gastar créditos), não confirmado (confira o Telegram) e bloqueado. A consulta aparece como "Aguardando vídeos" enquanto houver vídeo pendente, "Confirmado" quando todos foram entregues e "Parcial" quando algo falhou. O produto só conta como enviado depois da confirmação do Telegram; produtos aguardando vídeo ficam reservados e não entram em outra consulta.
- **Enviar sem vídeo:** cada card da seleção que ainda não foi entregue tem o botão **"Enviar sem vídeo"**. Ele mostra a mensagem exata que vai ao Telegram (nome curto, descrição e links numerados) e, ao confirmar, envia na hora: **foto do anúncio + legenda**, ou só texto se a foto não baixar. Serve quando o vídeo não sai (fila do Flow, teto de créditos, falha) e é uma alternativa ao envio com vídeo: o produto fica "entregue" e um vídeo ainda na fila para ele é cancelado. O envio não se repete para o mesmo produto.
- Login no painel, proteção CSRF e limitação de tentativas de login. Segredos ficam fora do Git e da imagem.

## Painel no navegador

- Trocar de seção usa *view transition* quando o navegador suporta e o sistema não pediu menos movimento; o foco vai para o título da seção (leitores de tela anunciam a troca) e a aba atual fica marcada com `aria-current`.
- O polling de 5 s só redesenha os cards e a tabela do histórico quando algo muda de fato: o foco e o `select` de triagem em uso sobrevivem à atualização. Cards abaixo das duas primeiras fileiras usam `content-visibility: auto` (com estimativa de altura) para não custar layout enquanto estão fora da tela.
- Com um modal aberto o resto da página fica inerte: em vez de um toast atrás do diálogo, o retorno do "Copiar texto" aparece dentro dele (`role="status"` + texto do botão) e qualquer outro aviso espera o modal fechar.
- O modal tem nome acessível (`aria-labelledby`), fecha com Esc, com o gesto de voltar do navegador (quando suportado) e com clique fora (`closedby="any"`); a página não rola atrás dele.
- **Modo escuro:** a paleta são tokens com `light-dark()`; o painel segue o tema do sistema (`color-scheme: light dark` + `<meta name="color-scheme">`). Navegador sem `light-dark()` (anterior ao Chrome 123/Safari 17.5) recebe exatamente a paleta clara de antes.
- **Erro de formulário no campo:** Preferências valida antes de enviar (preço máximo × mínimo, teto de vendas, notas, categorias, URL do Flow, modelo, pesos somando 100), marca o campo com `aria-invalid` + mensagem ligada por `aria-describedby` e leva o foco para o primeiro problema. Recusa do servidor aparece num aviso fixo acima de "Salvar preferências", não só no toast.
- O botão de confirmar envio diz o gasto: **"Confirmar: 3 vídeos · 45 créditos"** (produtos × custo por vídeo).
- **Estúdio:** abas com `role="tab"`/`aria-selected` e navegação por setas/Home/End; o seletor de fotos é alcançável por teclado (input visualmente escondido, não `display:none`); fotos declaram `width`/`height`.
- **Entrega dos arquivos:** `/static` vai com `?v=<APP_VERSION>` e fica em cache longo (`immutable`); HTML e API seguem `no-store`. As respostas de texto saem com `gzip` (app.js de ~35 KB para ~12 KB) — o Waitress não comprime sozinho.

### Instalar como app no celular (PWA)

O painel é instalável: `/manifest.webmanifest`, `/sw.js` e os ícones `static/pwa-*.png`. No Android, abra o painel no Chrome e use ⋮ → **Instalar aplicativo**.

- **Precisa ser HTTPS com certificado válido no aparelho.** Com aviso de certificado o Chrome recusa a instalação mesmo com o manifest correto — nesse caso instale a CA raiz do Caddy (`/mnt/user/appdata/caddy2_custom/data/caddy/pki/authorities/local/root.crt`) no Android.
- Em `http://IP:8787` não há service worker (contexto inseguro): no máximo o Chrome oferece "Adicionar à tela inicial", que abre no navegador e não é o app instalado.
- O service worker cacheia **só** `/static/*?v=<versão>`; HTML e `/api/*` sempre vêm da rede, para não mostrar fila, teto de créditos ou entregas desatualizados. O registro está em `static/app.js` e só roda em contexto seguro — em HTTP o painel continua idêntico.
- Manifest e service worker são **públicos por necessidade**: o Chrome os busca antes de qualquer login. O guard do `app.py` libera exatamente esses dois caminhos (mais `/healthz` e `/static/`); qualquer arquivo novo servido na raiz precisa entrar nessa lista, senão volta 401 e a instalação não aparece.

## Modos: Legacy e Moderno

Preferências → **Modo** escolhe como os produtos e os vídeos são escolhidos. Vale para as próximas consultas; cada produto guarda o modo com que foi escolhido (`mode`), e o vídeo dele segue esse modo.

- **Modo Legacy** (padrão): exatamente o comportamento anterior — tudo o que está descrito acima.
- **Modo Moderno**: "vende bem, mas pouca gente divulga". Plano e decisões em [`PLANO_MODO_MODERNO.md`](PLANO_MODO_MODERNO.md).

### Seleção do Modo Moderno

- Coleta por **vendas** (`sortType: 2`) em cada categoria configurada (`productCatId`, até 20 IDs) ou na lista geral, até `modern_pages` páginas de 50, com 1 s entre páginas. A posição de cada anúncio é recalculada pelas vendas lidas, porque a lista geral não vem ordenada por vendas. A busca por categoria às vezes responde `error [10000]: System Error`; essa resposta é repetida até 5 vezes com espera crescente, e uma página que continua falhando só encurta aquela categoria (aviso na consulta).
- Funil, nesta ordem (a contagem de cada corte aparece no aviso da consulta): dados incompletos ou oferta fora da validade, repetidos (mesma chave ou mesmo nome normalizado), já enviados/descartados, nota < 4,5, vendas < 50, preço fora de R$ 80–1.500, desconto abaixo do mínimo (opcional, para Black Friday), fora do filtro de nome, **top de afiliados** (`listType: 2`, 20 páginas, lista guardada por um dia), **vitrine** (30 primeiras posições), **estourados** (> 5.000 vendas) e **tipo de produto** já entregue ou reservado nos últimos 7 dias (2 primeiras palavras relevantes do nome, em qualquer ordem).
- Pontuação = demanda × (nota/5)³ × √comissão × vendedor × vitrine × frescor × tendência × preço:
  demanda = vendas, limitada a 1.500 (acima disso, 1.500 × 1.500 / vendas); vendedor ×0,7 com comissão extra do vendedor ≥ 5%; vitrine ×0,6 até a posição 100; frescor ×1,4 até 14 dias de oferta e ×1,15 até 30; tendência = 1 + crescimento semanal das vendas, entre ×0,5 e ×2,5 (neutro sem histórico); preço ×1 até R$ 400 e (400/preço)^0,8 acima. A comissão usa a mesma base conservadora do Legacy. Campo ausente na API vira fator neutro, com "dado indisponível" no card.
- Concorrência "baixa" quando vendas ≤ 1.500, posição > 100 e comissão extra do vendedor < 5%; senão "média".
- Seleção: melhor pontuação primeiro, respeitando o máximo por loja, nunca dois do mesmo tipo e, com mais de uma categoria, uma cota por categoria antes de completar.
- Opcional: **potencial de vídeo** — uma chamada ao modelo dá nota 0–10 aos melhores (`min(12, max(6, 3 × quantidade))`: 9 com a quantidade padrão de 3); pontuação final = pontuação/maior × potencial/10, e quem tem potencial ≥ 7 vem antes. **Não elimina ninguém**: só reordena antes da seleção, que continua limitada pela quantidade, pelo máximo por loja, pelo tipo e pela cota por categoria. Falha do modelo mantém a seleção pela pontuação.
- **Registro diário de vendas** (Preferências → Rotina, padrão ligado às 07:00): lê as mesmas fontes do Moderno e guarda as vendas de cada anúncio por 60 dias, base da tendência (referência mais próxima de 7 dias atrás, com pelo menos 2 dias). Roda nos dois modos, só lê a API, até 3 tentativas por dia com 30 min de intervalo; sem credenciais da Shopee, não roda. As consultas do Moderno também gravam as vendas lidas.

### Roteiro do Modo Moderno

Além das regras de legenda e roteiro de sempre, o código recusa (e devolve ao modelo para correção): narração fora de 18–25 palavras, última frase sem "produto marcado", abertura com "Olha"/"Gente" ou igual à de um dos últimos 8 vídeos, **número falado que não está no nome do anúncio** (dígitos e números por extenso) e rosto/sorriso nas cenas (só mãos). Cada vídeo recebe um de 7 formatos (`uso_real`, `problema_solucao`, `antes_depois`, `unboxing`, `pov_rotina`, `teste_desafio`, `detalhes_macro`), nunca um dos 3 últimos usados; o prompt do Flow leva o formato e a regra de só mãos.

### Vídeo do Modo Moderno

- O worker (1.2.0+) recebe `finish` na tarefa: renderiza em **1080×1920, 30 fps** (fundo desfocado quando a proporção não é 9:16), com áudio normalizado e, nos últimos 3,5 s, o **selo de compra** "★ nota • N vendidos" e o botão laranja pulsante "CONFIRA NO PRODUTO MARCADO", acima da legenda falada. Worker antigo ignora `finish` e entrega como no Legacy.
- **Fidelidade:** o worker manda 3 quadros do clipe cru (`/api/worker/tasks/<id>/frames`); antes de entregar, o modelo de visão compara os quadros com a foto do anúncio e dá nota 0–10, que aparece no card. Nota abaixo do mínimo (padrão 6) gera um alerta no Telegram, mas **não bloqueia** a entrega; falha na conferência vira "não avaliada".
- Créditos, aprovação de custo, teto diário, estados de entrega e regras de não repetição são os mesmos do Legacy.

### Conferir a API

`SHOPEE_APP_ID=… SHOPEE_SECRET=… python scripts/probe_modern_api.py [--category 100715]` faz 3 consultas de uma página e mostra quais campos voltam, sem imprimir segredos nem links. Conferido em 09/11/2026: `sellerCommissionRate`, `priceDiscountRate` (em %), `productCatIds`, `periodStartTime` e `listType: 2` respondem; `sortType: 2` ordena por vendas dentro de uma categoria, mas não na lista geral. `periodStartTime` é o início do período da oferta e quase sempre igual entre anúncios, então o frescor raramente pesa; a paginação repete anúncios entre páginas (o funil descarta).

## Worker da VM

O Antigravity CLI e o navegador logado no Google Flow só existem na VM hermes, então o painel não os chama direto. Um worker na VM busca as tarefas pelo endpoint /api/worker/*, autenticado por um token gerado em Preferências → Worker da VM, e devolve o resultado. Instalação e operação: worker/README.md. Sem o worker, os modelos agy são pulados na hora e a cadeia segue para os fallbacks; os vídeos esperam na fila por até 6 horas.

## Vídeo no Google Flow

Ao enviar uma consulta (manual ou automático), o painel põe um vídeo por produto na fila e reserva o custo esperado (padrão 15 créditos), dentro do teto diário (padrão 75 créditos, horário de Brasília). A descrição e os links de cada produto ficam congelados nesse momento. O worker da VM:

1. Abre o projeto do Flow configurado e confere as configurações do agente: confirmar antes de gerar, 9:16, 1 versão e o modelo esperado (padrão Omni 1.1 Flash).
2. Inicia uma sessão nova, anexa a foto do anúncio e envia o prompt de 10 segundos.
3. Lê o custo no cartão de confirmação do Flow. Só escolhe "Aprovar" (nunca "Sempre aprovar") se o Flow pedir o custo esperado para 1 geração. Antes de aprovar, registra no painel que os créditos podem ser gastos.
4. Espera o vídeo, baixa em 720p, transcreve a narração com faster-whisper (modelo small, CPU) e usa o roteiro só para corrigir a grafia das palavras faladas. Aplica a legenda com ffmpeg e envia o MP4 ao painel.
5. O painel manda o vídeo legendado ao Telegram numa única mensagem, com a descrição e os links congelados no envio. Cada produto sai quando o seu vídeo fica pronto, sem esperar os outros.

O Flow gera o vídeo sem textos na imagem, e a legenda é aplicada depois, para evitar erros de grafia da IA de vídeo. A legenda reproduz o que foi falado, inclusive repetições da narração gerada.

Regras de segurança:
- Login expirado, verificação do Google, custo diferente do esperado, falta de créditos, modelo diferente ou projeto não configurado pausam a fila e mandam um alerta no Telegram. Resolva o motivo (para login: worker/flow_login.sh) e toque em Retomar vídeos em Preferências.
- Uma falha depois da aprovação marca o vídeo como incerto e mantém os créditos contados. Ela nunca gera de novo sozinha; confira o projeto no Flow. Falhas antes da aprovação liberam a reserva.
- Sem vídeo, nada é entregue: teto atingido, geração desligada, anúncio sem imagem ou falha de geração deixam o produto "não entregue", com o motivo no card, sem foto nem texto substituto. O botão "Gerar vídeo ↻" tenta de novo dentro do teto, com a mesma mensagem congelada. "Reenviar vídeo" manda de novo um vídeo pronto que o Telegram recusou, sem gastar créditos. Worker offline: o produto fica "aguardando vídeo" por até 6 horas.
- Vídeos criados antes desta versão (com a foto já enviada) continuam respondendo à mensagem da foto.

## Estúdio (geração assistida)

Plano e decisões em [`PLANO_ESTUDIO.md`](PLANO_ESTUDIO.md). Menu **Estúdio**, pensado para o celular, em duas abas:

- **Do dia:** com Preferências → Vídeo → **Produtos para o Estúdio por envio** maior que 0 (padrão 0, desligado), os
  N produtos de maior pontuação de cada envio não entram na fila automática: ficam reservados, "aguardando vídeo", e o
  card mostra "Estúdio até …". Se até o **horário de corte** (padrão 22:00, horário de Brasília) não houver **nenhuma**
  geração para o produto, ele segue para o vídeo automático de sempre (foto da API, roteiro da consulta, dentro do teto
  diário); rascunho aberto não impede. Envio feito depois do corte reserva até o corte do dia seguinte. A descrição e os
  links continuam congelados no envio.
- **Manual:** cole o link de qualquer anúncio (completo ou curto `s.shopee.com.br`; o link curto é resolvido só pelo
  redirecionamento, sem abrir a página nem passar por captcha). O painel busca o anúncio na Open API
  (`productOfferV2(itemId, shopId)`); produto fora do programa de afiliados é recusado. Avisos sem bloquear: já entregue
  ou reservado antes, no top de afiliados do dia, sem foto. Vídeos manuais ficam fora do teto diário.

Em cada rascunho:
1. **Fotos:** a foto do anúncio mais até 6 fotos suas, reduzidas no navegador (lado maior 1.280 px, JPEG) antes de
   subir; ★ marca a principal. Ficam só no volume (`/data/studio/<id>`) e são apagadas com o rascunho.
2. **Ficha visual:** o modelo descreve só o que aparece nas fotos (P = anúncio, F1… = suas fotos) e o que o vídeo por
   IA tende a errar. Você corrige; roteiro e prompt só podem usar o que está nela.
3. **Formato** (como a câmera filma e quem aparece): **POV** e **Hands-on** (só mãos, voz em off), **Selfie frontal**,
   **Falando para a câmera** e **Formato livre (UGC)** (pessoa falando para a câmera, boca sincronizada) e **Provador**
   (pessoa usando o produto, voz em off). Nos formatos com pessoa a regra de só mãos não vale e o roteiro descreve a
   pessoa no gancho para ela se manter igual. O modo automático continua com os 7 formatos dele.
4. **Roteiros:** 3 abordagens diferentes no formato escolhido (o modelo escolhe entre problema e solução, antes e
   depois, teste, uso na rotina, detalhes, unboxing e curiosidade), cada um com gancho (0–2 s), demonstração, prova e chamada, e a legenda. Valem as
   regras do Modo Moderno (18–25 palavras, termina em "produto marcado", sem "Olha"/"Gente" nem abertura repetida,
   nenhum número fora do nome) e as 3 narrações começam diferente. Edição livre: o card mostra o que fugiu das regras
   sem bloquear. Ao escolher um, aparece o prompt do Flow.
   **Estilo do roteiro** (escolhido antes de gerar; padrão na tela: o novo):
   - **Problema real + chamadas na tela** (E12): cada roteiro parte de uma dor real do dia a dia que o produto resolve
     (plausível pela ficha), a câmera prende o olhar no 1º segundo (aproximação até a lente, chicote, câmera caindo na
     cena) e o contraste deixa claro que sem o produto a rotina volta a ser pior. Cenas em 0–3 s (problema + câmera),
     3–6 s (o produto resolve), 6–8 s (rotina melhor), 8–10 s (toque no produto). Traz **3 chamadas escritas na tela**
     (até 26 caracteres, editáveis): a dor (0,5–3 s, caixa vermelha), a virada (3,3–6 s, amarela) e o não-perca (6,3 s
     até o fim, laranja pulsando). As chamadas entram na faixa de cima com efeito de "pop" e
     leve inclinação; o prompt do Flow pede a ação no centro/metade de baixo e continua proibindo texto desenhado pela
     IA. **A fala não ganha legenda** (nem é transcrita). Regras das chamadas: sem emojis, sem preço/desconto/frete/
     estoque/cupom/"corra"/"só hoje"/"últimas unidades", nenhum número fora do nome, as 3 diferentes e a 3ª sem
     "produto marcado" (o botão do fim já diz). A urgência vem do problema, nunca de escassez inventada.
     **Fim com respiro:** a fala fica em **14–19 palavras** (clássico e automático continuam em 18–25) porque em ~2,3
     palavras/s 23 palavras enchiam os 10 s e o vídeo parecia cortado. As cenas de 8–10 s são um plano mais aberto,
     câmera parada e o **produto inteiro** no quadro, com ~2 s sem fala; o prompt do Flow pede esse encerramento
     calmo e a tela mostra a faixa de palavras do estilo escolhido.
   - **Clássico**: o da S1 (gancho 0–2 s, demonstração, prova, chamada) com a legenda da narração. Rascunhos antigos
     ficam nele.
5. **Vídeo (S2):** "Gerar vídeo · N créditos" mostra as fotos que vão ao Flow (até **4**, a principal primeiro), o
   prompt e o custo, e só cria a tarefa depois do `confirm` (E3). O painel recusa sem criar nada se o custo mudou desde
   que a tela abriu, se o worker de vídeo for anterior ao **1.4.0**, com a geração desligada ou pausada, roteiros de
   outro formato ou produto do dia que já passou do corte. **Do dia** conta no teto diário; **Manual** fica fora (o
   painel mostra quanto foi gasto em manuais no dia e no mês). Um vídeo por vez por rascunho.
   O worker anexa as referências em **Elementos** (fotos suas são baixadas do painel com o token do worker,
   `GET /api/worker/tasks/<id>/images/<foto>`, só as listadas na tarefa), confere que a caixa tem exatamente N anexos,
   gera 1 cena de 10 s e acabamento em 1080×1920 (legenda da fala desligada no estilo novo). No estilo
   **Problema real** o fim é limpo: **sem** o selo "★ nota • N vendidos" e **sem** o botão "CONFIRA NO PRODUTO
   MARCADO" — quem puxa o clique é a 3ª chamada, pulsando sobre o plano do produto inteiro. O automático e o
   clássico continuam com o selo e o botão. Pronto, o vídeo fica em **"pronto,
   aguardando sua aprovação"** (`review`): nada vai ao Telegram sozinho (E4). A prévia toca dentro de uma moldura que
   imita a Shopee Vídeo, com a fidelidade (quadros × até 3 fotos de referência), a legenda que vai ao Telegram e as ações
   **Aprovar e enviar ao Telegram** (uma mensagem com vídeo, legenda do Estúdio — a salva no momento do envio — e os
   links; no Do dia, os mesmos links congelados no envio da consulta), **Descartar e gerar outro** (novo custo) e
   **Descartar** (os créditos continuam contados). O arquivo fica no painel depois do envio para rever e baixar; vai
   embora ao descartar ou excluir o rascunho (as linhas de `videos` ficam, com os créditos).

Chamadas ao modelo rodam em segundo plano (a página acompanha); reiniciar o painel no meio marca a chamada como falha
e mantém o rascunho. A geração usa a mesma fila, pausa e regras de crédito dos vídeos automáticos: falha antes do envio
ao Flow libera nova tentativa sem custo; falha depois fica "incerta" e o painel pede para conferir o Flow antes de
gerar de novo. Produto do dia com qualquer geração (mesmo descartada) não cai no vídeo automático do corte.

## Unraid / Compose Manager

1. Crie uma stack chamada shopee-studio com o conteúdo de compose.yaml deste repositório.
2. Coloque um .env junto ao Compose, baseado em .env.example. Defina PANEL_PASSWORD. As credenciais da Shopee e do Telegram podem ser preenchidas depois pela interface.
3. Execute Compose Up. Acesse http://IP-DO-UNRAID:8787 e use a senha PANEL_PASSWORD. Na instalação do thevault o endereço usado é **`https://shopee.home.local`**: o container `Caddy2` faz `reverse_proxy 192.168.1.90:8787` com TLS pela CA interna dele (o registro A fica no Pi-hole). Para publicar em outro host, siga a nomenclatura `<serviço>.home.local` do Caddyfile existente.
4. Em Preferências, confira filtros, configure Telegram e o tópico. Faça uma consulta e revise a prévia. Anúncios já enviados pelo aplicativo ficam excluídos permanentemente.
5. Habilite envio automático quando o destino e o ranking estiverem corretos. Por padrão, só gera prévias.

A porta 8787 deve estar livre. PANEL_PORT altera a porta externa. Use BIND_IP para restringir à interface LAN desejada. Com proxy HTTPS, use COOKIE_SECURE=true e acesse apenas o endereço HTTPS. Não exponha diretamente a porta na internet.

### HTTPS na LAN (Caddy)

Atrás do Caddy o painel funciona sem ajuste no código: o proxy preserva o `Host`, então a checagem de `Origin` do painel (que recusa origem diferente do host) continua válida, e o HTTPS torna a página um contexto seguro — é o que habilita `navigator.clipboard` no botão "Copiar texto" (sem TLS ele cai para `document.execCommand`, que é o caminho usado em `http://IP:8787`).

- `COOKIE_SECURE=true` deixa o cookie de sessão com `Secure`: endurece a sessão e **impede o login pelo endereço HTTP direto**. Hoje o container roda com o padrão (`false`), então os dois endereços funcionam; ligar só faz sentido depois de fechar a porta 8787 e usar exclusivamente o HTTPS.
- Trocar a imagem não exige `Ctrl+F5`: os arquivos são servidos com `?v=<APP_VERSION>` e cache `immutable`, então a versão nova muda a URL sozinha.

O volume shopee-studio_shopee-data guarda banco e chave de sessão. Faça backup desse volume com o container parado. Ele também contém os segredos da Shopee e do Telegram configurados na interface; trate o backup como confidencial. Não use docker compose down -v ao atualizar.

## Atualizações GitHub → Unraid

Cada push na branch main executa os testes e publica:
- ghcr.io/saladfruiter/shopee-studio:latest
- ghcr.io/saladfruiter/shopee-studio:sha-COMMIT_COMPLETO

A stack de produção usa image:, sem build local. O Compose Manager pode comparar a imagem instalada com a imagem publicada e exibir atualização disponível ao usar Check for Updates. No Compose Manager Plus, habilite Auto Check for Updates e selecione Every Time para consultar ao abrir a página de stacks. Os demais intervalos limitam a frequência dessa consulta; a opção descreve uma checagem ao carregar a página, não um monitor em segundo plano. Uma alteração só fica disponível quando o workflow termina com sucesso.

O primeiro pacote GHCR precisa ter visibilidade Public em GitHub → Packages → shopee-studio → Package settings. Um repositório público sozinho não torna o pacote público. Depois disso, o servidor não precisa de login para baixar.

A atualização é manual: use Update Stack / Compose Up com pull, ou:
    docker compose pull
    docker compose up -d

O serviço não se atualiza sozinho. A interface exibe o SHA da versão instalada. Para reverter, use uma tag sha- anterior em image: e recrie o container; preserve o volume.

## Desenvolvimento e testes

Python 3.13:
    python -m venv .venv
    # Ative o ambiente virtual
    pip install -r requirements.txt
    python -m unittest discover -s tests -v
    # Copie .env.example para .env e preencha
    python app.py

Servidor WSGI Waitress, porta 8787. Com Docker local:
    docker compose -f compose.dev.yaml up --build

Os testes usam respostas simuladas e não enviam mensagens. Não existe senha padrão. Não publique .env, data/, arquivos zip antigos ou logs.

Comparar a busca antiga com a progressiva, sem Engine.collect, banco de produção, Telegram ou Flow:

    python scripts/compare_equivalent_search.py                      # páginas gravadas em scripts/fixtures/run29.json
    SHOPEE_APP_ID=... SHOPEE_SECRET=... python scripts/compare_equivalent_search.py --live \
        --products produtos.json --record scripts/fixtures/run29.json # API real; grava as páginas sem credenciais
    python scripts/compare_equivalent_search.py --evaluate           # + rodadas do modelo com o agy local

O relatório lista candidatos, não links aprovados: só o modelo decide "mesmo produto", e os aprovados precisam de conferência visual.

## Limites e operação

- A classificação é uma heurística configurável, não previsão de vendas ou comissão garantida.
- Concorrência de vídeos e quantidade de avaliações não são coletadas; valide manualmente.
- A Open API traz nome, uma imagem, loja e preços, mas não as especificações nem as variantes do anúncio. A comparação de equivalentes depende dessa imagem e do título. Como fotos parecidas são aceitas, um link pode levar a uma variante diferente (voltagem, cor) não informada no título: confira os links antes de vincular ao vídeo. A elegibilidade e a comissão no canal Vídeo não são verificadas.
- Anúncios equivalentes já enviados também ficam excluídos das próximas consultas, para não gerar outro vídeo do mesmo produto.
- Um vídeo leva cerca de 2 minutos na VM (geração ~1 min, legenda ~15 s). A fidelidade do produto no vídeo depende do Flow: revise antes de publicar.
- Uma consulta com análise leva de 1 a 3 minutos por produto com o agy (uma chamada por rodada), limitada a 10 minutos por produto. O painel mostra o produto e a rodada em andamento. Produtos analisados no momento de um reinício são descartados, e a consulta fica como falha.
- Mais consultas e rodadas aumentam a latência e o uso de quota da API. A busca não garante 6 links: incertos continuam de fora.
- A coleta lê até o limite de páginas configurado, ordenadas por comissão. O ranking considera os produtos encontrados nesse intervalo, não o catálogo inteiro da Shopee. Aumente o limite ou use uma palavra-chave se faltarem anúncios inéditos.
- Histórico registra ofertas elegíveis observadas; não é histórico completo do catálogo.
- Logs recentes ficam limitados a 1.000 eventos. Consultas/ofertas são mantidas no SQLite para preservar histórico.
- Um único processo/container deve usar o banco. Não escale réplicas.
- Desative eventual tarefa antiga ShopeeAffiliateTop5Daily no Windows ao migrar para o Unraid, para evitar dois agendamentos.

## Login na Shopee (sessão autenticada)

O painel usa a Open API; para páginas que só existem logado (pedidos, notificações, carrinho,
área de afiliado) existe um caminho separado, por browser anti-detecção: **Camofox** em dois
containers no thevault — `camofox` (headless, 9377) e `camofox-vnc` (janela visível, 9378 + noVNC
6080) — com perfil compartilhado.

Fluxo resumido: abrir a Shopee no container do VNC → usuário resolve o captcha e entra com
e-mail/telefone (**não** “Entrar com Google”: o popup quebra a janela) → `POST /stop` com
`x-admin-key` grava `storage-state.json` → `docker restart camofox` para o headless reler o perfil
→ navegar por URL. Verificado em 2026-11-05: 28 cookies `shopee.com.br` persistidos, busca e
página de produto autenticadas sem novo captcha.

Procedimento completo, comandos prontos e armadilhas: [`docs/login-shopee-camofox.md`](docs/login-shopee-camofox.md).
Detalhes do plugin, da variante VNC e do script de checkpoint: skill `camofox-setup`.

## Scripts legados

Na cópia local original, daily_shopee_top5.ps1, docker-schedule.sh e install_daily_task.ps1 foram preservados; eles não fazem parte desta publicação nem são usados pelo novo container. O fluxo recomendado é o painel. Não execute os dois agendadores ao mesmo tempo.
