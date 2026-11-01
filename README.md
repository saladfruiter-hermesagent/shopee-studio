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
- Login no painel, proteção CSRF e limitação de tentativas de login. Segredos ficam fora do Git e da imagem.

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

## Unraid / Compose Manager

1. Crie uma stack chamada shopee-studio com o conteúdo de compose.yaml deste repositório.
2. Coloque um .env junto ao Compose, baseado em .env.example. Defina PANEL_PASSWORD. As credenciais da Shopee e do Telegram podem ser preenchidas depois pela interface.
3. Execute Compose Up. Acesse http://IP-DO-UNRAID:8787 e use a senha PANEL_PASSWORD.
4. Em Preferências, confira filtros, configure Telegram e o tópico. Faça uma consulta e revise a prévia. Anúncios já enviados pelo aplicativo ficam excluídos permanentemente.
5. Habilite envio automático quando o destino e o ranking estiverem corretos. Por padrão, só gera prévias.

A porta 8787 deve estar livre. PANEL_PORT altera a porta externa. Use BIND_IP para restringir à interface LAN desejada. Com proxy HTTPS, use COOKIE_SECURE=true e acesse apenas o endereço HTTPS. Não exponha diretamente a porta na internet.

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

## Scripts legados

Na cópia local original, daily_shopee_top5.ps1, docker-schedule.sh e install_daily_task.ps1 foram preservados; eles não fazem parte desta publicação nem são usados pelo novo container. O fluxo recomendado é o painel. Não execute os dois agendadores ao mesmo tempo.
