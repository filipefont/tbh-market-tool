# Spec — Encomendas (Buy Orders) da Steam: ranking de "melhores para vender por $"

> **Autor:** levantamento técnico assistido (Claude)
> **Data:** 2026-06-23
> **App alvo:** TBH: Task Bar Hero — `appid 3678970`
> **Status:** proposta (pré-implementação). Os achados abaixo foram **testados ao vivo** contra a Steam.

---

## 1. Objetivo

Criar um filtro/ranking que liste os itens com as **encomendas (buy orders)** mais
valiosas, para responder à pergunta: *"quais itens são os melhores para vender por
dinheiro ($) agora?"*. O ranking deve levar em conta **preço da maior encomenda**,
**quantidade de encomendas (demanda)** e o **valor agregado** do book de compra.

Conceito-chave: na Steam, "encomenda" = **buy order** = um comprador que já depositou
saldo na carteira e está esperando para comprar a um preço fixo. Vender para a maior
encomenda é a forma de transformar item em **saldo na hora**, sem ficar listado
esperando. Por isso "maior encomenda + boa demanda" ≈ "melhor item para liquidar por $".

---

## 2. Diagnóstico: conseguimos acessar os dados? (TL;DR: **sim**)

Testei quatro caminhos ao vivo. Resumo:

| Fonte | Vivo? | Traz encomendas? | Precisa de quê | Veredito |
|---|:---:|:---:|---|---|
| `priceoverview` (já usado no projeto) | ✅ | ❌ | `market_hash_name` | Só lado de **venda** (lowest/median/volume). |
| `itemordershistogram` | ✅ | ✅ | **`item_nameid`** numérico | Funciona, mas exige um ID interno que não temos. |
| Scrape do `nameid` na página (legado) | ❌ | — | regex `Market_LoadOrderSpread(N)` | **Quebrado**: a Steam migrou para SSR e removeu o inline. |
| **Página de listagem (SSR hydration blob)** | ✅ | ✅ | `appid` + `market_hash_name` | **Solução recomendada** — order book inteiro embutido. |

### 2.1 `priceoverview` — insuficiente

```
GET /market/priceoverview/?appid=3678970&currency=7&market_hash_name=Soulstone%20-%20Hell
→ {"success":true,"lowest_price":"R$ 366,86","volume":"87","median_price":"R$ 0,40"}
```
Só dá o lado da **venda**. Não existe campo de buy order aqui. Serve para preço, não para encomendas.

### 2.2 `itemordershistogram` — vivo, mas travado no `nameid`

```
GET /market/itemordershistogram?country=BR&language=portuguese&currency=7&item_nameid=<N>&two_factor=0
→ HTTP 200, {"success":1,"buy_order_graph":[...],"sell_order_graph":[...],
            "highest_buy_order":"...","buy_order_summary":"..."}
```
O endpoint **continua vivo e devolve dados reais em BRL**. O problema é o parâmetro
`item_nameid`: um inteiro **global** da Steam (não é o `appid`, não é o `classid`, não é
o hash). Passar `market_hash_name` aqui dá **HTTP 400**.

### 2.3 O método clássico de obter o `nameid` está **quebrado**

Por anos, a forma padrão de descobrir o `nameid` era baixar a página do item e extrair
via regex `Market_LoadOrderSpread( 12345 )`. **Isso não existe mais**: a Steam migrou as
páginas de mercado para um front-end SSR novo (bundles `public/ssr/*.js`, hidratação
estilo React Query). Confirmei na página do `Soulstone - Hell`:
- `Market_LoadOrderSpread` → **0 ocorrências**
- `item_nameid` / `nameid` → **0 ocorrências**
- A lógica do order book vive num *chunk* lazy do webpack (mapeado por manifest), não no
  HTML nem nos bundles iniciais.

→ Resolver `hash_name → nameid` virou um beco sem saída barato. **Mas não precisamos dele.**

### 2.4 A descoberta: o order book vem **embutido na própria página** ✅

A página SSR nova **pré-renderiza o order book inteiro** num blob de hidratação,
indexado por `appid + market_hash_name` (e **não** por nameid):

```
GET /market/listings/3678970/Soulstone%20-%20Hell
```
Dentro do HTML, no estado desidratado do React Query:
```json
"queryKey":["market","orderbook",3678970,"Soulstone - Hell"],
"state":{"data":{
  "amtMaxBuyOrder": 242,           // maior ENCOMENDA, em centavos da moeda → R$ 2,42
  "amtMinSellOrder": 36686,        // menor venda, em centavos → R$ 366,86
  "eCurrency": 7,                  // 7 = BRL, 1 = USD (mesmos códigos do projeto)
  "cBuyOrders": 5284,              // TOTAL de encomendas (demanda agregada) ✅
  "cSellOrders": 2,                // total de ofertas de venda
  "rgCompactBuyOrders": [242,100, 129,2, 113,7, 107,13, 93,50, ...],  // book de COMPRA
  "rgCompactSellOrders":[36686,1, 75661,1]                            // book de VENDA
}}
```

Os arrays `rgCompact*` são pares achatados `[preço_centavos, quantidade, preço, qtd, ...]`
em ordem decrescente de preço de compra. Para o `Soulstone - Hell` isso significa:
maior encomenda **R$ 2,42 × 100 unidades**, depois R$ 1,29 × 2, R$ 1,13 × 7, e assim por
diante — com **5.284 encomendas no total**.

**Isto é exatamente o que o usuário pediu**, entregue por `hash_name` (que já temos em
`data/items.json` / `steam_market.json`), sem nameid e sem segundo endpoint.

---

## 3. Solução recomendada

**Coletar o order book parseando o blob de hidratação da página de listagem**, com a
mesma disciplina de throttle/cache que o projeto já aplica ao `priceoverview`.

### 3.1 Por que esta abordagem (e não o histograma)
- ✅ Indexada por `market_hash_name` — **sem necessidade de `nameid`**.
- ✅ Uma única requisição por item (mesmo modelo de custo do `enrich`).
- ✅ Traz **tudo de uma vez**: maior encomenda, total de encomendas, book completo de
  compra e venda, e o código da moeda (`eCurrency`).
- ✅ Reaproveita o pacer adaptativo + cooldown 429 que já existe em `build.py`.

### 3.2 Contrato de dados (novo bloco em `data/enriched.json` ou arquivo dedicado)
Proposta: gravar em `data/orderbook.json`, espelhando o estilo de `enriched.json`:
```json
{
  "Soulstone - Hell": {
    "cur": 7,
    "buyMax": 2.42,
    "buyOrders": 5284,
    "sellMin": 366.86,
    "sellOrders": 2,
    "buyBook": [[2.42,100],[1.29,2],[1.13,7]],
    "buyNotional": 1234.56,
    "spreadPct": 99.3,
    "fetchedAt": 1782234483
  }
}
```
- `cur` = `eCurrency` da página (ver §5.1 sobre moeda no CI).
- `buyNotional` = Σ(preço × qtd) sobre o book compacto = "saldo total se eu preenchesse
  todas as encomendas visíveis".
- `spreadPct` = (sellMin − buyMax)/sellMin × 100 = distância entre comprar e vender.

### 3.3 Parsing (robusto, sem depender de JSON perfeito no HTML)
O blob é JSON escapado dentro do HTML. O parse mais resiliente localiza o trecho
`"queryKey":["market","orderbook",<appid>,"<hash>"]` e lê o `state.data` associado. Como
o `data` aparece **antes** do `queryKey` no payload, o parser deve casar o objeto que
contém `rgCompactBuyOrders` imediatamente anterior ao `queryKey` do item.

Esboço (Python, alinhado ao estilo de `build.py`):
```python
import json, re, urllib.parse

ORDERBOOK_RE = re.compile(
    r'"amtMaxBuyOrder":(?P<max>\d+).*?'
    r'"amtMinSellOrder":(?P<min>\d+).*?'
    r'"eCurrency":(?P<cur>\d+).*?'
    r'"cBuyOrders":(?P<nbuy>\d+),"cSellOrders":(?P<nsell>\d+),'
    r'"rgCompactBuyOrders":\[(?P<buy>[\d,]*)\],'
    r'"rgCompactSellOrders":\[(?P<sell>[\d,]*)\]',
    re.S)

def parse_orderbook(html, hash_name):
    # garante que estamos no bloco do item certo (não em outro item da página)
    anchor = f'"orderbook",{APPID},"{hash_name}"'
    pos = html.find(anchor)
    if pos < 0:
        return None
    # o data vem ANTES do queryKey; busca o último match antes do anchor
    m = None
    for m in ORDERBOOK_RE.finditer(html, 0, pos + 1):
        pass
    if not m:
        return None
    def pairs(s):
        nums = [int(x) for x in s.split(",") if x != ""]
        return [[nums[i] / 100.0, nums[i + 1]] for i in range(0, len(nums) - 1, 2)]
    buy = pairs(m["buy"])
    return {
        "cur": int(m["cur"]),
        "buyMax": int(m["max"]) / 100.0,
        "buyOrders": int(m["nbuy"]),
        "sellMin": int(m["min"]) / 100.0,
        "sellOrders": int(m["nsell"]),
        "buyBook": buy,
        "buyNotional": round(sum(p * q for p, q in buy), 2),
    }
```
> Nota: validar com 5–10 itens reais antes de confiar no regex; se a Steam mudar o SSR,
> só este parser quebra (superfície de manutenção pequena e isolada).

---

## 4. Métricas e filtros para o ranking "melhor para vender por $"

Vender para a maior encomenda só é bom se a encomenda **existe de fato e aguenta volume**.
Logo o ranking combina **preço** com **profundidade/demanda**:

| Métrica | Fórmula | O que diz |
|---|---|---|
| `buyMax` | `amtMaxBuyOrder` | Quanto entra no bolso por unidade, **agora**. |
| `buyOrders` | `cBuyOrders` | Demanda total — encomenda funda não "evapora". |
| `topQty` | qtd do 1º nível do book | Quantas unidades você despeja no melhor preço. |
| `topValue` | `buyMax × topQty` | $ instantâneo no melhor preço, sem derrapar. |
| `buyNotional` | Σ(preço×qtd) do book | $ total se preencher todas as encomendas. |
| `spreadPct` | (sellMin − buyMax)/sellMin | Quão "justa" está a compra vs a venda. |
| **`scoreLiquidez`** | normalizar e combinar `buyMax`×log(`buyOrders`) | Ranking composto sugerido (alto preço **e** alta demanda). |

**Filtros propostos na UI:**
- Ordenar por: `buyMax`, `buyOrders`, `topValue`, `buyNotional`, `scoreLiquidez`.
- Slider "demanda mínima" (`buyOrders ≥ X`) para esconder itens sem comprador real.
- Toggle "esconder spread > 50%" (descarta listagens-troll, ver §5.3).
- Coluna visual: maior encomenda vs menor venda lado a lado (mini order book).

**Recomendação de ranking padrão:** ordenar por `scoreLiquidez` desc, que prioriza itens
caros **e** com muitas encomendas — os que dá pra liquidar por $ rápido sem desabar o preço.

---

## 5. Riscos e pontos de atenção

### 5.1 Moeda no GitHub Actions (⚠️ validar)
A página de listagem renderiza na moeda **da geolocalização/cookie**, não por parâmetro.
Localmente (Brasil) veio `eCurrency: 7` (BRL). **Os runners do GitHub Actions ficam nos
EUA → provavelmente virá `eCurrency: 1` (USD).** Tratamento:
1. **Sempre ler `eCurrency`** e gravar junto (`cur`).
2. Se vier USD, converter os preços para BRL com uma taxa única (o projeto já lida com
   `CURRENCIES = {"usd":1, "brl":7}`; dá pra derivar a taxa comparando o `priceoverview`
   do mesmo item nas duas moedas, que já é coletado).
   Quantidades (`cBuyOrders`, `topQty`) são **independentes de moeda** — não precisam conversão.
3. Alternativa a testar: forçar BRL via cookie `steamCountry`/parâmetro de país no fetch
   do CI. **Recomendo um teste no próprio Action antes de fechar a implementação.**

### 5.2 Profundidade do book compacto
`rgCompactBuyOrders` traz o book **agregado/comprimido** (níveis de preço, não cada ordem),
e provavelmente truncado nos níveis mais baixos. Para `buyMax`, `buyOrders` e `topValue`
isso é **suficiente**. `buyNotional` fica subestimado em itens muito fundos — ok, é um
piso, e a UI deve rotular como "valor do book visível".

### 5.3 Listagens-troll / outliers
O `Soulstone - Hell` mostrou `sellMin = R$ 366,86` com `median = R$ 0,40` — claramente uma
venda-isca. O ranking **deve usar encomendas (lado de compra), que são mais difíceis de
manipular** (exigem saldo depositado), e o filtro de spread (§4) descarta esses ruídos.

### 5.4 Rate limit e custo
Cada página ≈ 150 KB (vs ~80 bytes do `priceoverview`). Mantém-se **1 req/item** sob o
pacer adaptativo + cooldown 429 já existente. Para o build público, coletar order book só
do **top-N por interesse** (como já se faz com `--enrich-top`), com TTL próprio.

### 5.5 Acoplamento ao SSR da Steam
O parser depende do formato do blob de hidratação. É uma superfície pequena e isolada
(§3.3); se a Steam mudar, só o `parse_orderbook` quebra. Mitigação: teste de fumaça
(`tests/smoke.py`) que valida o parse contra ao menos 1 item real e alerta se vier vazio.

---

## 6. Plano de implementação (incremental)

1. **`build.py` — coletor**
   - Nova função `fetch_orderbook(hash_name)`: GET da página de listagem (reusa
     `fetch_json`/throttle, mas lê HTML), aplica `parse_orderbook`, retorna o dict de §3.2.
   - Flag CLI `--orderbook-top N` (espelha `--enrich-top`), com `ORDERBOOK_TTL`.
   - Persistir em `data/orderbook.json`. Tratar `eCurrency` (§5.1).
2. **UI (`index.html` / gerador no `build.py`)**
   - Nova aba/coluna "Encomendas": `buyMax`, `buyOrders`, `topValue`, `spreadPct`.
   - Controles de ordenação + slider de demanda mínima + toggle anti-troll (§4).
   - Mini order book (top 3–5 níveis de compra) no detalhe do item.
3. **Workflow (`.github/workflows/publish.yml`)**
   - Acrescentar passo de coleta de order book do top-N (sem inchar o git: mesmo padrão
     atual de build público somente-leitura).
   - **Validar a moeda no runner** (§5.1) antes de publicar números.
4. **Testes (`tests/smoke.py`)**
   - Asserts: `parse_orderbook` extrai `buyMax/buyOrders` de um HTML real fixturado;
     alerta se `rgCompactBuyOrders` vier vazio (sinal de mudança no SSR).

---

## 6.1 Como ficou implementado (as-built)

Entregue na branch `feat/encomendas-steam`:

- **`build.py`**
  - `_fetch` genérico → `fetch_json` + `fetch_text` (HTML compartilha o throttle/cooldown).
  - `parse_orderbook(html, name)` + `_order_book`/`order_book`. **Correção importante:** o blob
    de hidratação vem **duplamente escapado** (`\"amtMaxBuyOrder\"`); o parser normaliza o
    escape (`re.sub(r'\\+"', '"', html)`) antes de casar. A âncora `["market","orderbook",appid,
    hash]` confirma o item.
  - Cache atômico `data/orderbook.json` (`load/save/merge_orderbook`, `is_book_fresh`, TTL 1800s),
    keyed por moeda (`eCurrency` → `usd`/`brl`) — o front-end converte como já faz com o preço real.
  - Tabela `order_history` (buy_max, buy_orders, sell_min, sell_orders) + `record_order_history`.
  - `enrich_orderbook(rows, N)` (seleção pelos mais líquidos) e CLI: `--orderbook-top N` e
    subcomando `book <itens...>`.
  - `build_rows` injeta `row["book"]`; o servidor local já passa a enxergar as encomendas.
- **Front-end (template no `build.py`)**: colunas **"Maior enc."** e **"Encomendas"** (ordenáveis),
  conversão de moeda (`bookInfo`), líquido/spread/notional no tooltip e no painel de detalhe,
  filtro **"só com encomenda"**, e `buyScore` (preço líquido ponderado pela demanda).
- **Workflow**: passo `--orderbook-top 30` antes da geração pública (dados vão p/ o `actions/cache`).
- **Teste**: `tests/smoke.py` valida o parser contra uma fixture com o escape duplo (guarda §5.5).

Validado ao vivo: `Soulstone - Hell` → maior encomenda R$ 2,42 (líq. R$ 2,06), 5.269 encomendas,
spread 99%, book R$ 1.664,81.

## 7. Conclusão

**Sim, conseguimos os dados de encomendas** — e por um caminho melhor do que o histograma
clássico: a página de listagem SSR **já entrega o order book completo por `hash_name`**
(`amtMaxBuyOrder`, `cBuyOrders`, book de compra/venda), sem depender do `item_nameid` que
ficou inacessível. Com isso dá pra montar o ranking "melhores para vender por $" cruzando
**preço da maior encomenda × demanda (nº de encomendas) × valor do book**, reaproveitando o
throttle e o pipeline de build que o projeto já tem. O único ponto a validar antes de
fechar é a **moeda no GitHub Actions** (§5.1).
