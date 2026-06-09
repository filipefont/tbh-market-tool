# Plano de implementação — Fundação de histórico (SQLite)

> Desenho técnico do **roadmap #1** do `TBH-Market-Tool_Plano-de-Acao.md`.
> **✅ IMPLEMENTADO** (bulk sim · gravar tudo · backfill sim) — ver "Status" abaixo.
> Revisado contra o `build.py` atual (App ID Steam: 3678970).

## Status da implementação

Tudo desta etapa foi entregue no `build.py` e validado:

| Componente | Onde | Validação |
|---|---|---|
| Tabela + índice (`init_history`) | `build.py` | criada no boot do CLI/serve; idempotente |
| Gravação parametrizada (`record_history`) | `build.py` | `executemany` com `?` — **imune a SQL injection** |
| Hook `priceoverview` (`merge_enriched`) | `/api/price`, `do_enrich`, CLI | 1 ponto por preço válido (pula `nodata`) |
| Hook `bulk` (`record_bulk_history`) | `do_refresh`, `--refresh` | 662 pontos USD por varredura |
| Backfill único (`seed_history_from_enriched`) | boot | 706 pontos do `enriched.json`; **não duplica** em re-boots |
| Leitura (`history_series`) + `GET /api/history` | servidor | parametrizado; whitelist `valid_names` + token + `Host` |

**Testes executados:** `PRAGMA integrity_check = ok`; nome malicioso (`x'; DROP TABLE …`) → tratado
como literal (0 linhas, tabela intacta) e barrado por 400 na rota; sem token → 403; backfill não
duplica. Banco em `data/history.db` (não versionar / não embutir na página).

**Fora desta entrega (próximos passos):** gráfico/sparkline na UI consumindo `/api/history`,
médias móveis, tendência e alertas.

---

---

## 1. Objetivo e princípio

Hoje o `enriched.json` guarda **só o snapshot mais recente** e sobrescreve (via `merge_enriched`).
A ideia é **registrar cada coleta de preço** num banco *append-only*, criando uma **série temporal
própria** — sem depender do `pricehistory` da Steam (que exige login).

Isso é a **fundação** que destrava o resto do roadmap: gráficos, médias móveis, tendência e alertas.

**Princípio-chave:** o histórico fica **só no servidor** (banco em `data/`), **nunca embutido na
página**. Hoje o `index.html` embute o snapshot atual (`rows`) inline; jogar histórico ali incharia
a página. O histórico é consultado **sob demanda** por um endpoint.

---

## 2. Tecnologia e custo

| Item | Decisão | Custo |
|---|---|---|
| Banco | **SQLite** via `sqlite3` (biblioteca padrão do Python) | **R$ 0**, zero dependências novas |
| Arquivo | `data/history.db` (local) | disco local |
| Serviço externo | **Nenhum** | **R$ 0** |

**Por que SQLite e não JSON/CSV:** precisamos **consultar por item + intervalo de tempo** e agregar
(média móvel). Um `.json` cresceria sem índice e teria que ser lido inteiro; SQLite indexa e consulta
em milissegundos. Mantemos tudo **no `build.py`** (sem novos arquivos) para preservar a simplicidade
de arquivo único.

---

## 3. Esquema do banco

```sql
CREATE TABLE IF NOT EXISTS price_history (
  name      TEXT    NOT NULL,
  currency  TEXT    NOT NULL,     -- 'usd' | 'brl'
  ts        INTEGER NOT NULL,     -- epoch s (= fetchedAt)
  low       REAL,                 -- menor venda
  med       REAL,                 -- mediana
  vol       INTEGER,              -- volume 24h
  source    TEXT    NOT NULL      -- 'bulk' | 'priceoverview'
);
CREATE INDEX IF NOT EXISTS ix_hist ON price_history(name, currency, ts);
```

**Campo `source`:** além do `priceoverview` (preço real sob demanda), dá para registrar o **bulk**.
Cada `🔄 Atualizar mercado` traz o menor preço (USD) dos **662 itens de uma vez** — é a fonte de
série histórica mais rica e barata (uma "foto" completa do catálogo a cada refresh).

---

## 4. Componentes a desenvolver (no `build.py`)

| # | Função / mudança | O que faz | ~Linhas |
|---|---|---|---|
| 1 | `HISTORY_DB` (constante) | caminho `data/history.db` | 1 |
| 2 | `init_history()` | cria tabela + índice se não existir; chamada no boot do `serve` e no CLI | ~8 |
| 3 | `record_history(rows)` | insere N pontos; thread-safe; **try/except** (falha de log nunca quebra a coleta de preço) | ~15 |
| 4 | Hook em `merge_enriched()` | onde já gravamos o snapshot atual, também grava 1 ponto `priceoverview` por preço válido (pula `nodata`) | ~6 |
| 5 | Hook em `do_refresh()` / `--refresh` | grava os 662 pontos `bulk` (USD) a cada varredura | ~5 |
| 6 | `seed_history_from_enriched()` | **backfill** único: lê o `enriched.json` atual e insere um ponto inicial por item | ~12 |
| 7 | `history_series(name, currency, since, limit)` | consulta de leitura (lista de pontos ordenados) | ~10 |
| 8 | Endpoint `GET /api/history` | retorna os pontos em JSON; protegido por `_auth` + whitelist `valid_names` (anti-SSRF); **não** chama a Steam, então não passa pelo `_busy` | ~12 |

**Total estimado:** ~70–90 linhas, **nenhum arquivo novo**, **nenhuma dependência**. Não mexe na
lógica de preço existente — só adiciona um "gravador" paralelo.

---

## 5. Concorrência e segurança

- O servidor é multithread (`ThreadingHTTPServer` + threads de lote/refresh). `sqlite3` não
  compartilha conexão entre threads, então cada escrita abre sua própria conexão curta
  (`with sqlite3.connect(...)`) sob um **`_history_lock`** dedicado (como já fazemos no
  `enriched.json`). As escritas são raras (1 a cada ~3s pelo throttle), então é folgado.
- O endpoint de leitura herda toda a proteção atual: **bind local**, validação de `Host`, **token
  CSRF**, e **whitelist de nomes** (só consulta itens da base cruzada).
- **Isolamento de falha:** se o `record_history` der erro (disco cheio, lock), ele engole a exceção
  e loga — o preço continua sendo salvo normalmente no `enriched.json`. Histórico é *best-effort*,
  nunca bloqueia o fluxo principal.

---

## 6. Custo de disco e performance

Cada linha ≈ 60–100 bytes no SQLite.

| Cenário | Pontos/dia | Por ano | Tamanho/ano |
|---|---|---|---|
| Bulk diário (662 × 1 refresh) | ~662 | ~240 mil | **~20 MB** |
| Bulk + enrich pesado (662 × 2 moedas) | ~1.300 | ~485 mil | **~40 MB** |

**Desprezível.** Insert é instantâneo; consulta por item usa o índice. **Zero impacto** no
carregamento da página (o banco nem entra nela). Opcionalmente, um **prune de retenção** (ex.: manter
1 ano) caso queira teto fixo.

---

## 7. Decisões a confirmar

| # | Decisão | Opções | Recomendado |
|---|---|---|---|
| 1 | Registrar o **bulk** também? | sim (série completa grátis a cada refresh) / só `priceoverview` | **sim** |
| 2 | **Cadência / dedup** | gravar toda coleta / pular se valor não mudou | **gravar tudo** (já são esparsas pelo TTL; dedup só se houver ruído) |
| 3 | **Backfill** do `enriched.json`? | sim (1 ponto inicial por item) / começar vazio | **sim** |

---

## 8. Fora de escopo (vem depois)

Esta etapa é só a **fundação de dados + endpoint de leitura**. Não inclui:

- **Gráfico/sparkline na UI** (roadmap médio prazo — em cima do endpoint `/api/history`).
- **Médias móveis, tendência, alertas** (dependem de semanas de dados acumulados).

Motivo: gráfico sem dado acumulado não mostra nada. Primeiro o banco enche, depois visualizamos.

---

## 9. Plano de validação

1. `py_compile` + build.
2. Smoke test do servidor: buscar 1 item → confirmar **1 linha** em `price_history`; rodar
   `/api/refresh` curto → confirmar pontos `bulk`.
3. `GET /api/history?name=...&currency=brl` → retorna os pontos; nome fora da whitelist → **400**.
4. Concorrência: lote pequeno + leitura simultânea → `PRAGMA integrity_check` = `ok`.

---

## 10. Resumo

- **~70–90 linhas** no `build.py`, **R$ 0**, sem dependências nem serviços externos.
- **Sem risco** ao fluxo de preços atual (gravador paralelo, *best-effort*).
- **~1 sessão** de trabalho.
- Ganho: **série histórica própria** que destrava praticamente todo o resto do roadmap.

> Padrão recomendado para aprovação: **bulk sim · gravar tudo · backfill sim**.
