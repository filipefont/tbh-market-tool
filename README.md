# TBH Market Tool

Cruza os itens do jogo (Task Bar Hero) com os preços do mercado Steam para ranquear
o retorno em **gold** (venda no NPC) e em **gold por dinheiro real** (gold/$, gold/R$).

## Arquitetura em 2 camadas

- **BULK (barato, completo):** baixa todos os ~653 itens listados via o endpoint de busca da
  Steam (preço = menor venda, em USD). ~69 requisições, cacheado em `data/steam_market.json`.
  Dá o ranking completo por gold e gold/$.
- **PRECISO (sob demanda):** `priceoverview` por item → preço **real** (USD **ou** BRL) + mediana +
  volume 24h (liquidez). 1 requisição/item. Usado no top-N, em item específico, ou nos botões
  da página (modo servidor). Resultados ficam em `data/enriched.json` e sobrevivem a rebuilds.

> A Steam **ignora a moeda no endpoint de busca** (sempre USD), mas **respeita no `priceoverview`**.
> Como ordenar por gold/$ é escala linear, a conversão por taxa fixa **não muda o ranking** — por
> isso o bulk usa USD + taxa estimada (editável na página), e o valor exato vem da camada precisa.

## Uso

```bash
# Estático (gera index.html para abrir direto no navegador)
python3 build.py                       # do cache
python3 build.py --refresh             # rebaixa o bulk
python3 build.py --enrich-top 25       # preço real do top-25 (BRL por padrão)
python3 build.py --enrich-top 25 --currency usd
python3 build.py price "Eclipse Amulet (Arcana) A"          # 1 item na hora (BRL)
python3 build.py price "Eclipse Amulet (Arcana) A" --currency usd

# Servidor local interativo (recomendado p/ atualizar pela página)
python3 build.py serve                 # abre http://127.0.0.1:8765
```

### Na página
- **Toggle USD / BRL** + campo de **taxa** editável (recalcula a estimativa na hora). Se um item
  só tem preço real numa moeda, a outra é **convertida pela taxa** e marcada com **≈** (clique em
  ↻ p/ o preço real nativo).
- Ordenação clicando (ou Enter/Espaço) nos cabeçalhos; busca por nome (`/` foca); botão
  **✕ Limpar** e chip com **N visíveis / total**.
- **Filtros padronizados** (busca + multi-seleção): **Grade**, **Categoria** (GEAR/MATERIAL/…),
  **Tipo** (gearType), **Classe** e **Atributos** são dropdowns iguais — cada um com **caixa de
  busca** e **seleção múltipla**. Vários valores no mesmo filtro = **OU**; o de **Atributos** é
  **E** (item precisa ter todos), e cada atributo marcado vira uma **coluna** de valor ordenável.
- **Filtros por faixa** (dropdown **Faixas**): mín–máx de **gold**, **nível** e **preço**, mais
  **listagens ≥**.
- **Cobertura total dos tradáveis**: itens vendáveis que **não estão no snapshot do bulk** também
  aparecem (com gold e metadados), marcados com **— (sem bulk)** no preço; busca o preço sob
  demanda pelo **↻** (modo servidor). Para esconder esses, use *disponibilidade → esconder sem oferta*.
- **Δ de preço** (coluna **Δ 24h**, ordenável): variação % do preço (USD) calculada do histórico
  (`history.db`) no momento do build — **▲ verde / ▼ vermelho**, com **24h e 7d** no tooltip e no
  painel de detalhes. Mostra **—** enquanto não houver histórico suficiente (enche com o tempo).
- **Chips de filtros ativos**: faixa abaixo dos controles lista cada filtro aplicado; clique no
  **✕** do chip para remover só aquele (ou **✕ limpar tudo**).
- **Estado na URL**: os filtros vão para a querystring — a página é **compartilhável** (abrir o
  link reabre já filtrado). Sem params, cai nas preferências salvas (localStorage).
- **Detalhes do item** (clique na linha ou **Enter**): abre um **painel lateral** com ícone
  grande, atributos e valores, parte/variante/grupo/slots/tradável, gold e preços, ações
  (↗ Steam, copiar nome, favoritar) e — no **modo servidor** — um **mini-gráfico** do histórico
  de preço. Fecha com **Esc**, **✕** ou clique fora.
- **Miniatura do item**: ícone da wiki (`/icons/<icon>.png`, lazy-load) com borda na **cor da
  raridade**; placeholder se faltar. Cores das grades **oficiais do jogo** (CSS da wiki,
  `--c-<grade>`); a coluna **Grade** ordena por **raridade** (COMMON→COSMIC via `gradeRank`).
- **Tooltips estilizados** (tema escuro do app) e **números grandes abreviados** (`2,7M`, com o
  valor cheio no tooltip); o trecho buscado é **realçado** no nome do item.
- **Filtro de disponibilidade**: *só com giro 24h* (esconde itens consultados sem venda nas
  últimas 24h; mantém os ainda **não consultados** como "desconhecido") ou *esconder sem oferta*
  (tira os sem nenhuma listagem de venda / loja vazia).
- **Baseline** de gold/moeda (média e mediana) recalculado conforme o filtro.
- Colunas: **Lvl** (nível do equipamento), **Gold (Cubo)** (gold ao vender no Cubo/Alquimia),
  e o cabeçalho **Gold / R$** (ou **Gold / $**) muda conforme a moeda escolhida.
- **Mini-barra** de proporção na coluna *gold/moeda (est.)*; o **1º colocado da ordenação atual**
  ganha 🏆 + faixa dourada; **badges coloridos por raridade**; ⚠️ em itens de **baixa liquidez**.
- **🔥 Arbitragem**: marca itens cujo gold/moeda está **≥ 2× a mediana** do filtro (ótimo negócio).
- Tooltip de **valor líquido ao vender** no preço real (após ~15% de taxa do Mercado Steam).
- **Disponibilidade (coluna Disp.)**: bolinha de **liquidez** (verde/laranja/vermelho, a partir de
  listagens + volume) e botão **🛒** que verifica **ao vivo** se há oferta comprável agora —
  detecta o caso de "loja com erro / item indisponível". (A verificação usa o `priceoverview`,
  pois a Steam descontinuou o JSON de listagens para este appid.)
- **⭐ Favoritos**: estrela por linha + botão **⭐ favoritos** que filtra só os marcados
  (persistido no navegador). O nome do item aparece **na cor da grade**.
- Botão dedicado **↗ Steam** por linha abre a listagem (o nome deixou de ser o link).
- **Atalhos de teclado**: `/` foca a busca · `Esc` limpa filtros · `↑/↓` navegam as linhas ·
  `Enter` abre a linha selecionada na Steam.
- Preferências (moeda, taxa, ordenação, filtros, favoritos) **persistem** entre visitas (localStorage).
- Layout **responsivo** (esconde colunas secundárias no celular) e com **coluna Item fixa**.
- **Modo servidor** (indicador verde): **🔄 Atualizar mercado** (rebaixa o bulk), **↻** por linha
  e **🎯 Preço real dos visíveis** (busca em lote, respeitando o throttle; Shift+clique força
  todos). O preço real mostra uma **etiqueta de idade colorida** (verde ≤6h, laranja, vermelho).
  Só **um trabalho roda por vez** — os botões se desabilitam enquanto há atualização em andamento.
  Aberto como arquivo (file://), a página funciona em modo somente-leitura e avisa via toast.
- **🔁 Auto** (atualização priorizada): mantém os preços frescos em segundo plano sem clicar,
  numa fila por prioridade — **favoritos** → nunca consultados → líquidos vencidos → mortos —
  com **TTL escalonado por liquidez** (favorito 2h · líquido 6h · baixa liquidez 12h · sem giro
  24h). Roda em lotes pequenos, **cede a vez** aos botões manuais e respeita o limite da Steam.

## Como os dados se cruzam

Chave de junção = nome no mercado Steam, reconstruído a partir do item do jogo:
- GEAR → `Nome (Grade) Variante`  (ex.: `Chain Gloves (Legendary) A`)
- Material/box → nome exato

100% dos itens do mercado casaram com a base do jogo.

## Segurança

O modo servidor foi desenhado para não comprometer a máquina nem as APIs consumidas:

- **Bind só em `127.0.0.1`** (nunca exposto na rede) e **validação do header `Host`**
  (mitiga DNS-rebinding).
- **Token CSRF por sessão**: endpoints de API exigem o header `X-TBH-Token`. Um site malicioso
  aberto no navegador não consegue forjá-lo (sem CORS habilitado, não lê a resposta nem o token).
- **Sem serviço de arquivos do disco** — apenas rotas explícitas, então não há *path traversal*.
- **Whitelist de nomes** nas consultas de preço: o `name` precisa existir na base cruzada
  (impede usar o servidor como *proxy* aberto / SSRF para a Steam).
- **Proteção da API da Steam**: throttle global **adaptativo (AIMD)** entre chamadas — começa em
  ~5s (abaixo do teto de ~20-30 req/min), **acelera** devagar quando a Steam coopera e
  **desacelera** forte a cada **HTTP 429** (até ~20s/req). Um 429 também arma um **cooldown
  global** que pausa **todas** as threads (honrando o header **`Retry-After`** quando vem), em vez
  de seguir martelando um IP em soft-ban. Mais: **dedupe por TTL** (não refaz `priceoverview` de
  item buscado há < 10 min), **cache** dos preços, **um único** refresh/lote em massa por vez, e
  **retry** dos itens limitados numa 2ª passada (os sem dados ganham carimbo de tempo p/ não serem
  reconsultados antes do frescor expirar).
- **Anti-XSS**: nomes são escapados no render e o JSON embutido neutraliza `</script>`.

## Fontes

- Itens: https://www.taskbarherowiki.com/data/items.json
- Mercado: https://steamcommunity.com/market/search/ (appid 3678970)
