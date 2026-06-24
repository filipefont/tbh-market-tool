#!/usr/bin/env python3
"""
TBH Market Tool — cruza os itens do jogo com os preços do mercado Steam e ranqueia
o retorno em gold (NPC) e em gold por dinheiro real (gold/$, gold/R$).

Camadas:
  BULK    busca todos os ~653 itens listados (preço = menor venda, USD). Barato (~69 req),
          dá o ranking completo. Cacheado em data/steam_market.json.
  PRECISO priceoverview por item -> preço real (USD/BRL) + mediana + volume 24h. 1 req/item.
          Sob demanda: top-N do ranking, item específico, ou botões na página (modo servidor).

Uso:
    python3 build.py                          # gera index.html (estático) do cache
    python3 build.py --refresh                # rebaixa o bulk das APIs
    python3 build.py --enrich-top 25          # preço real (priceoverview) do top-25
    python3 build.py price "Eclipse Amulet (Arcana) A"   # consulta 1 item na hora
    python3 build.py serve                    # SERVIDOR local: página interativa
                                              #   (toggle USD/BRL, atualizar mercado e itens)

Segurança (modo serve): bind só em 127.0.0.1, validação de Host, token CSRF, whitelist de
nomes (anti-SSRF), throttle + cache TTL p/ proteger a API da Steam. Detalhes no README.
"""
import argparse
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

APPID = 3678970  # TBH: Task Bar Hero (Steam app)
ITEMS_URL = "https://www.taskbarherowiki.com/data/items.json"
STEAM_URL = (
    "https://steamcommunity.com/market/search/render/"
    f"?appid={APPID}&norender=1&count=100&start={{start}}"
)
PRICEOVERVIEW_URL = (
    "https://steamcommunity.com/market/priceoverview/"
    f"?appid={APPID}&currency={{currency}}&market_hash_name={{name}}"
)
# Página de listagem (SSR): embute o order book (encomendas/buy orders) num blob de
# hidratação. É a fonte das encomendas — ver .spec/encomendas-steam.md.
LISTING_URL = f"https://steamcommunity.com/market/listings/{APPID}/{{name}}"
CURRENCIES = {"usd": 1, "brl": 7}  # códigos de moeda da Steam
CUR_BY_CODE = {v: k for k, v in CURRENCIES.items()}  # 7 -> "brl", 1 -> "usd"
ITEMS_CACHE = os.path.join(DATA, "items.json")
STEAM_CACHE = os.path.join(DATA, "steam_market.json")
ENRICHED_CACHE = os.path.join(DATA, "enriched.json")
ORDERBOOK_CACHE = os.path.join(DATA, "orderbook.json")  # encomendas (buy orders) por item/moeda
HISTORY_DB = os.path.join(DATA, "history.db")  # série histórica própria (snapshots de preço)
BOOK_DEPTH = 12       # nº de níveis de preço guardados do book de compra (mantém o cache enxuto)
ORDERBOOK_TTL = 1800  # segundos: não re-busca o order book do mesmo item nesse intervalo
HEADERS = {"User-Agent": "TBH-Market-Tool/1.0 (uso pessoal)"}

# Trava de grade na reabertura do mercado (25/06/2026, v1.00.20): os 3 grades mais altos ficam
# SEM listagem por tempo indeterminado — EXCETO Soulstones. Marcamos esses itens como "intradável
# (trava de grade)" p/ não exibir "sem oferta"/⚠️ liquidez falsa. O dev libera depois, em anúncio à
# parte: quando isso ocorrer, basta esvaziar GRADE_LOCKED (set vazio) — nada mais muda.
# Ver .spec/roadmap-d25-reabertura.md.
GRADE_LOCKED = {"COSMIC", "DIVINE", "CELESTIAL"}

# Reabertura do mercado: 25/06/2026 04:00 BRT = 07:00 UTC. Âncora p/ "Δ desde a reabertura"
# (variação acumulada de cada item desde que o mercado voltou). calendar.timegm((2026,6,25,7,0,0)).
MARKET_REOPEN_TS = 1782370800


def is_grade_locked(name, grade):
    """True se o item está sob a trava de grade da reabertura (grade top-3 e não-Soulstone)."""
    return grade in GRADE_LOCKED and not (name or "").startswith("Soulstone")

# --- Proteção da API da Steam: throttle global ADAPTATIVO + cooldown no 429 ----------------
# A Steam limita o priceoverview a ~20-30 req/min por IP, mas escala p/ cooldown temporário
# (tudo vira 429) se você fica colado no teto. O ritmo aqui é AIMD (additive-increase /
# multiplicative-decrease): começa folgado, ACELERA devagar quando tudo vai bem e DESACELERA
# forte ao tomar 429 — assim acha sozinho o limite real do dia sem precisar ficar chutando.
# Além disso, um 429 arma um COOLDOWN GLOBAL: TODAS as threads pausam (não adianta seguir
# martelando um IP em soft-ban) e honramos o header Retry-After quando a Steam o envia.
STEAM_MIN_INTERVAL = 5.0     # piso do espaçamento (s) — ritmo "rápido" quando a Steam coopera
STEAM_MAX_INTERVAL = 20.0    # teto do espaçamento (s) sob pressão de rate-limit
STEAM_JITTER = 0.8           # variação aleatória somada ao espaçamento (quebra o padrão "metralhadora")
STEAM_DECAY_AFTER = 5        # nº de sucessos seguidos p/ acelerar 1 passo (rumo ao piso)
STEAM_DECAY_STEP = 1.0       # quanto reduz o intervalo por passo de aceleração (s)
STEAM_BACKOFF_FACTOR = 1.8   # multiplicador do intervalo a cada 429 (desacelera forte)
STEAM_COOLDOWN_DEFAULT = 90  # pausa global (s) no 429 quando não há Retry-After
STEAM_COOLDOWN_MAX = 600     # teto da pausa global (s)
PRICE_TTL = 600              # segundos: não refaz priceoverview do mesmo item nesse intervalo

_steam_lock = threading.Lock()
# estado compartilhado do pacing adaptativo (sempre lido/escrito sob _steam_lock)
_steam = {"last_call": 0.0, "interval": STEAM_MIN_INTERVAL, "ok_streak": 0, "cooldown_until": 0.0}


def steam_pace_state():
    """Snapshot do ritmo atual (p/ exibir no status/diagnóstico). Não bloqueia."""
    with _steam_lock:
        now = time.monotonic()
        return {
            "interval": round(_steam["interval"], 1),
            "cooldown": max(0, round(_steam["cooldown_until"] - now)),
        }


def _note_rate_limited(retry_after=None):
    """Tomou 429: desacelera (multiplicative-decrease) e arma o cooldown global p/ todos."""
    with _steam_lock:
        _steam["ok_streak"] = 0
        _steam["interval"] = min(STEAM_MAX_INTERVAL, _steam["interval"] * STEAM_BACKOFF_FACTOR)
        pause = retry_after if (retry_after and retry_after > 0) else STEAM_COOLDOWN_DEFAULT
        pause = min(STEAM_COOLDOWN_MAX, pause)
        _steam["cooldown_until"] = max(_steam["cooldown_until"], time.monotonic() + pause)
        return _steam["interval"], pause


def _note_ok():
    """Requisição OK: depois de uma sequência de sucessos, acelera 1 passo (additive-increase)."""
    with _steam_lock:
        _steam["ok_streak"] += 1
        if _steam["ok_streak"] >= STEAM_DECAY_AFTER and _steam["interval"] > STEAM_MIN_INTERVAL:
            _steam["interval"] = max(STEAM_MIN_INTERVAL, _steam["interval"] - STEAM_DECAY_STEP)
            _steam["ok_streak"] = 0


def _throttle():
    """Espaça as chamadas à Steam pelo intervalo adaptativo atual e respeita o cooldown global.
    Solta o lock enquanto dorme p/ não serializar as threads no relógio de parede."""
    while True:
        with _steam_lock:
            now = time.monotonic()
            spacing = _steam["interval"] + random.uniform(0, STEAM_JITTER)
            wait = max(_steam["cooldown_until"] - now, spacing - (now - _steam["last_call"]))
            if wait <= 0:
                _steam["last_call"] = now
                return
        time.sleep(wait)


def _retry_after_seconds(err):
    """Lê o header Retry-After de um HTTPError (segundos). None se ausente/ilegível."""
    try:
        raw = err.headers.get("Retry-After") if err.headers else None
        return int(raw) if raw and raw.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def _fetch(url, parser, retries=4, backoff=3.0, throttle=False):
    """Loop de busca com throttle adaptativo + backoff/cooldown. `parser(resp)` extrai o
    corpo (json.load p/ JSON, .read().decode p/ HTML) — assim JSON e HTML compartilham a
    mesma proteção de rate-limit da Steam."""
    last = None
    for attempt in range(retries):
        try:
            if throttle:
                _throttle()
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = parser(r)
            if throttle:
                _note_ok()
            return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                # rate-limit: desacelera o ritmo global e pausa TODAS as threads (cooldown).
                # O Retry-After da Steam (quando vem) manda; senão usamos o default.
                new_interval, pause = _note_rate_limited(_retry_after_seconds(e))
                print(f"    ! HTTP 429; cooldown {pause:.0f}s · ritmo agora ~{new_interval:.0f}s/req")
                time.sleep(pause)
            else:
                wait = backoff * (attempt + 1)
                print(f"    ! HTTP {e.code}; retry em {wait:.0f}s")
                time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = e
            wait = backoff * (attempt + 1)
            print(f"    ! falha ({e}); retry em {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError(f"Falha ao buscar {url}: {last}")


def fetch_json(url, retries=4, backoff=3.0, throttle=False):
    return _fetch(url, json.load, retries, backoff, throttle)


def fetch_text(url, retries=4, backoff=3.0, throttle=False):
    return _fetch(url, lambda r: r.read().decode("utf-8", "replace"), retries, backoff, throttle)


def parse_money(text):
    """'R$ 113,11' / '$0.20' / '1.234,56' -> float. Trata vírgula decimal (BR)."""
    if not text:
        return None
    s = re.sub(r"[^\d,.\-]", "", text)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):       # vírgula é o decimal -> BR
            s = s.replace(".", "").replace(",", ".")
        else:                                  # ponto é o decimal -> US
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(text):
    if not text:
        return None
    s = re.sub(r"[^\d]", "", text)
    return int(s) if s else None


def _price_overview(name, curkey="brl"):
    """Consulta PRECISA de um item. Retorna (status, po) com status in:
       'ok'     -> dados válidos (po preenchido)
       'nodata' -> a Steam respondeu mas não há listagem/preço (permanente; não adianta repetir)
       'error'  -> falha de rede / rate-limit (transitório; vale repetir mais tarde)."""
    code = CURRENCIES.get(curkey, CURRENCIES["brl"])
    url = PRICEOVERVIEW_URL.format(currency=code, name=urllib.parse.quote(name))
    try:
        d = fetch_json(url, retries=3, backoff=4.0, throttle=True)
    except RuntimeError:
        return "error", None
    if not d.get("success"):
        return "nodata", None
    return "ok", {
        "low": parse_money(d.get("lowest_price")),
        "lowText": d.get("lowest_price"),
        "med": parse_money(d.get("median_price")),
        "medText": d.get("median_price"),
        "vol": parse_int(d.get("volume")),
        "fetchedAt": int(time.time()),  # epoch s: alimenta o indicador de frescor na página
    }


def price_overview(name, curkey="brl"):
    """Compat: devolve só o dict de preço (ou None) — usado por CLI, calibrate e /api/price."""
    return _price_overview(name, curkey)[1]


def listings_overview(name, curkey="brl"):
    """Verifica AO VIVO se há oferta comprável agora.

    Obs.: a Steam descontinuou o JSON de /market/listings/.../render para este appid (passou a
    servir uma página renderizada por JS), então usamos o priceoverview: 'lowest_price' presente
    = existe listagem comprável agora; success:false / sem preço = indisponível ('erro na loja').
    Retorna {buyable, low, vol, fetchedAt} (verdict) ou None se a Steam não respondeu (transitório)."""
    status, po = _price_overview(name, curkey)
    if status == "error":
        return None                                  # rede/limite: indeterminado -> caller faz 502
    now = int(time.time())
    if status == "nodata" or not po or po.get("low") is None:
        return {"buyable": False, "low": None, "vol": None, "fetchedAt": now}
    return {"buyable": True, "low": po["low"], "vol": po.get("vol"), "fetchedAt": now}


# --- Encomendas / buy orders (order book) ------------------------------------------------
# A página de listagem SSR embute o order book num blob React Query (chave
# ["market","orderbook",APPID,hash_name]). Daí saem: maior encomenda, total de encomendas
# (demanda) e o book de compra/venda. Sem nameid, por hash_name. Ver .spec/encomendas-steam.md.
# amtMaxBuyOrder/amtMinSellOrder vêm `null` quando o item não tem encomenda OU não tem venda
# (ex.: item só com buy orders e cSellOrders:0). Aceitar `null` é essencial: senão o regex não
# casa e o item — justamente um com encomenda ativa — é descartado e nunca atualiza.
ORDERBOOK_RE = re.compile(
    r'"amtMaxBuyOrder":(?P<max>-?\d+|null),'
    r'"amtMinSellOrder":(?P<min>-?\d+|null),'
    r'"eCurrency":(?P<cur>\d+),'
    r'"cBuyOrders":(?P<nbuy>\d+),'
    r'"cSellOrders":(?P<nsell>\d+),'
    r'"rgCompactBuyOrders":\[(?P<buy>[\d,]*)\],'
    r'"rgCompactSellOrders":\[(?P<sell>[\d,]*)\]')


def _cents(v):
    """'1297' -> 12.97; 'null' (sem encomenda/venda) -> None."""
    return None if v == "null" else int(v) / 100.0


def _compact_pairs(s):
    """'242,100,129,2' -> [[2.42,100],[1.29,2]] (centavos -> moeda, em pares preço/qtd)."""
    nums = [int(x) for x in s.split(",") if x != ""]
    return [[nums[i] / 100.0, nums[i + 1]] for i in range(0, len(nums) - 1, 2)]


def parse_orderbook(html, name):
    """Extrai o order book do item `name` do HTML da página de listagem. Retorna o dict de
    encomendas (com `cur` = código de moeda da Steam) ou None se o item não está na página
    ou o formato mudou. Robusto: confirma o item pela âncora da queryKey e casa o bloco de
    dados imediatamente anterior (os dados vêm ANTES da queryKey no payload desidratado)."""
    # O blob desidratado é uma string JSON DENTRO do JSON da página -> aspas vêm escapadas
    # (\" e até \\\"). Normaliza removendo as barras antes das aspas p/ casar o conteúdo.
    html = re.sub(r'\\+"', '"', html)
    anchor = f'"orderbook",{APPID},"{name}"'
    pos = html.find(anchor)
    if pos < 0:
        return None
    m = None
    for m in ORDERBOOK_RE.finditer(html, 0, pos):  # último match antes da âncora
        pass
    if not m:
        return None
    buy = _compact_pairs(m["buy"])
    return {
        "cur": int(m["cur"]),
        "buyMax": _cents(m["max"]),
        "buyOrders": int(m["nbuy"]),
        "sellMin": _cents(m["min"]),
        "sellOrders": int(m["nsell"]),
        "buyBook": buy[:BOOK_DEPTH],
        "buyNotional": round(sum(p * q for p, q in buy), 2),
    }


def _order_book(name):
    """Busca o order book de `name`. Retorna (status, curkey, book):
       'ok'     -> book preenchido na moeda `curkey` (derivada do eCurrency da página)
       'nodata' -> página respondeu mas sem order book (item sem mercado/formato mudou)
       'error'  -> falha de rede / rate-limit (transitório)."""
    url = LISTING_URL.format(name=urllib.parse.quote(name))
    try:
        html = fetch_text(url, retries=3, backoff=4.0, throttle=True)
    except RuntimeError:
        return "error", None, None
    book = parse_orderbook(html, name)
    if not book:
        return "nodata", None, None
    curkey = CUR_BY_CODE.get(book.pop("cur"), "usd")  # moeda da geolocalização do fetch
    book["fetchedAt"] = int(time.time())
    return "ok", curkey, book


def order_book(name):
    """Conveniência: (curkey, book) ou (None, None). Usado por CLI/diagnóstico."""
    status, curkey, book = _order_book(name)
    return (curkey, book) if status == "ok" else (None, None)


# --- Caches ------------------------------------------------------------------------------
def get_items(refresh):
    if not refresh and os.path.exists(ITEMS_CACHE):
        print(f"[items] cache: {ITEMS_CACHE}")
        return json.load(open(ITEMS_CACHE, encoding="utf-8"))
    print("[items] baixando da wiki...")
    data = fetch_json(ITEMS_URL)
    json.dump(data, open(ITEMS_CACHE, "w", encoding="utf-8"))
    print(f"[items] {len(data)} itens salvos")
    return data


def get_steam(refresh, log=print):
    if not refresh and os.path.exists(STEAM_CACHE):
        log(f"[steam] cache: {STEAM_CACHE}")
        return json.load(open(STEAM_CACHE, encoding="utf-8"))
    log("[steam] paginando o mercado...")
    first = fetch_json(STEAM_URL.format(start=0), throttle=True)
    total = int(first.get("total_count", 0))
    results = list(first.get("results", []))
    step = max(len(results), 1)  # a Steam ignora count>10 e devolve ~10 por página
    log(f"[steam] total_count={total}; pagesize={step}")
    for start in range(step, total, step):
        d = fetch_json(STEAM_URL.format(start=start), throttle=True)
        results.extend(d.get("results", []))
        log(f"[steam] {len(results)}/{total}")
    json.dump(results, open(STEAM_CACHE, "w", encoding="utf-8"))
    log(f"[steam] {len(results)} listagens salvas")
    return results


def load_enriched():
    if os.path.exists(ENRICHED_CACHE):
        try:
            return json.load(open(ENRICHED_CACHE, encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def price_age(po):
    """Idade (s) de um registro de preço real, ou None se não tiver carimbo."""
    ts = po.get("fetchedAt") if isinstance(po, dict) else None
    return (time.time() - ts) if ts else None


def is_price_fresh(name, curkey, ttl, enriched=None):
    """True se já temos esse item/moeda buscado há menos de `ttl` segundos (evita remartelo)."""
    enriched = load_enriched() if enriched is None else enriched
    age = price_age((enriched.get(name) or {}).get(curkey))
    return age is not None and age < ttl


# Serializa leitura-modificação-escrita do enriched.json: várias ações (lote, item avulso)
# podem gravar "ao mesmo tempo"; sem isso, uma sobrescreve a outra e/ou o arquivo corrompe.
_enriched_lock = threading.Lock()


def save_enriched(enriched):
    """Escrita atômica: grava num .tmp e troca de uma vez (os.replace é atômico no SO)."""
    tmp = ENRICHED_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False)
    os.replace(tmp, ENRICHED_CACHE)


def merge_enriched(updates):
    """Aplica {name: {curkey: po}} sobre o arquivo atual, sob lock, sem perder o que já existe.
    Também registra os pontos na série histórica. Retorna o dict resultante (já salvo)."""
    with _enriched_lock:
        enriched = load_enriched()
        for name, bycur in updates.items():
            enriched.setdefault(name, {}).update(bycur)
        save_enriched(enriched)
    record_history(_history_rows_from_updates(updates, "priceoverview"))  # fora do lock do JSON
    return enriched


# --- Cache do order book (encomendas) — mesma disciplina atômica/lock do enriched ---------
_orderbook_lock = threading.Lock()


def load_orderbook():
    if os.path.exists(ORDERBOOK_CACHE):
        try:
            return json.load(open(ORDERBOOK_CACHE, encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def save_orderbook(book):
    tmp = ORDERBOOK_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False)
    os.replace(tmp, ORDERBOOK_CACHE)


def merge_orderbook(updates):
    """Aplica {name: {curkey: book}} sobre orderbook.json, sob lock, e registra no histórico."""
    with _orderbook_lock:
        book = load_orderbook()
        for name, bycur in updates.items():
            book.setdefault(name, {}).update(bycur)
        save_orderbook(book)
    record_order_history(updates)  # fora do lock do JSON
    return book


def is_book_fresh(name, ttl, book=None):
    """True se o order book do item foi buscado há menos de `ttl` s (em qualquer moeda)."""
    book = load_orderbook() if book is None else book
    for entry in (book.get(name) or {}).values():
        age = price_age(entry)
        if age is not None and age < ttl:
            return True
    return False


# --- Histórico de preços (SQLite) --------------------------------------------------------
# SEGURANÇA: TODAS as queries usam placeholders (?), nunca interpolação de string -> imune a
# SQL injection. Nomes de tabela/coluna são literais fixos; entradas externas (name, currency)
# ainda passam por whitelist (valid_names / CURRENCIES) na rota. Escrita é best-effort: um erro
# no histórico jamais derruba o fluxo de preços.
_history_lock = threading.Lock()


def init_history():
    """Cria a tabela e o índice se não existirem (idempotente)."""
    try:
        with _history_lock, sqlite3.connect(HISTORY_DB) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS price_history ("
                "name TEXT NOT NULL, currency TEXT NOT NULL, ts INTEGER NOT NULL, "
                "low REAL, med REAL, vol INTEGER, source TEXT NOT NULL)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_hist ON price_history(name, currency, ts)")
            # série de encomendas: evolução de demanda (buy_orders) e maior encomenda (buy_max)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS order_history ("
                "name TEXT NOT NULL, currency TEXT NOT NULL, ts INTEGER NOT NULL, "
                "buy_max REAL, buy_orders INTEGER, sell_min REAL, sell_orders INTEGER, "
                "source TEXT NOT NULL)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_ohist ON order_history(name, currency, ts)")
    except sqlite3.Error as e:  # pragma: no cover
        print(f"    ! histórico indisponível ({e})")


def _history_rows_from_updates(updates, source):
    """Extrai tuplas de histórico de {name: {curkey: po}}. Pula 'nodata' e pontos sem low/med."""
    rows, now = [], int(time.time())
    for name, bycur in (updates or {}).items():
        for curkey, po in bycur.items():
            if not isinstance(po, dict) or po.get("nodata"):
                continue
            low, med = po.get("low"), po.get("med")
            if low is None and med is None:
                continue
            rows.append((name, curkey, int(po.get("fetchedAt") or now),
                         low, med, po.get("vol"), source))
    return rows


def record_history(rows):
    """Append-only, parametrizado (anti-SQL-injection), best-effort (engole erro)."""
    if not rows:
        return
    try:
        with _history_lock, sqlite3.connect(HISTORY_DB) as conn:
            conn.executemany(
                "INSERT INTO price_history(name, currency, ts, low, med, vol, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    except sqlite3.Error as e:  # pragma: no cover
        print(f"    ! histórico não gravado ({e})")


def record_order_history(updates, source="orderbook"):
    """Grava pontos de encomendas a partir de {name: {curkey: book}}. Best-effort."""
    rows, now = [], int(time.time())
    for name, bycur in (updates or {}).items():
        for curkey, bk in bycur.items():
            if not isinstance(bk, dict):
                continue
            rows.append((name, curkey, int(bk.get("fetchedAt") or now),
                         bk.get("buyMax"), bk.get("buyOrders"),
                         bk.get("sellMin"), bk.get("sellOrders"), source))
    if not rows:
        return
    try:
        with _history_lock, sqlite3.connect(HISTORY_DB) as conn:
            conn.executemany(
                "INSERT INTO order_history(name, currency, ts, buy_max, buy_orders, "
                "sell_min, sell_orders, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    except sqlite3.Error as e:  # pragma: no cover
        print(f"    ! histórico de encomendas não gravado ({e})")


def record_bulk_history(rows):
    """Snapshot do bulk: menor venda (USD) de todos os itens com preço, source='bulk'."""
    now = int(time.time())
    record_history([(r["name"], "usd", now, r.get("usd"), None, None, "bulk")
                    for r in rows if r.get("usd")])


def history_series(name, currency, since=None, limit=2000):
    """Pontos ordenados por tempo de um item/moeda. Tudo parametrizado; o chamador valida
    name/currency contra a whitelist. since (epoch s) e limit são inteiros saneados."""
    limit = max(1, min(int(limit or 2000), 20000))
    sql = ("SELECT ts, low, med, vol, source FROM price_history "
           "WHERE name = ? AND currency = ?")
    params = [name, currency]
    if since:
        sql += " AND ts >= ?"
        params.append(int(since))
    sql += " ORDER BY ts ASC LIMIT ?"
    params.append(limit)
    try:
        with sqlite3.connect(HISTORY_DB) as conn:
            cur = conn.execute(sql, params)
            return [{"ts": r[0], "low": r[1], "med": r[2], "vol": r[3], "source": r[4]}
                    for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def seed_history_from_enriched():
    """Backfill único: se o histórico está vazio, semeia 1 ponto por item já em enriched.json."""
    try:
        with _history_lock, sqlite3.connect(HISTORY_DB) as conn:
            already = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    except sqlite3.Error:
        return
    if already:
        return  # já populado -> não duplica
    rows = _history_rows_from_updates(load_enriched(), "priceoverview")
    if rows:
        record_history(rows)
        print(f"[history] backfill: {len(rows)} pontos a partir do enriched.json")


# --- Cruzamento --------------------------------------------------------------------------
def join_key(it):
    """Reproduz o nome do mercado Steam a partir do item do jogo."""
    if it["type"] == "GEAR":
        return f"{it['name']} ({it['grade'].title()}) {it.get('variant') or 'A'}"
    return it["name"]


def _item_attrs(it):
    """atributos (base + inherent) do item; mesmo stat nos dois grupos -> soma value, junta disp."""
    attrs = {}
    st = it.get("stats")
    if isinstance(st, dict):
        for grp in ("base", "inherent"):
            for a in st.get(grp) or []:
                nm = a.get("stat")
                if not nm:
                    continue
                cur = attrs.get(nm)
                if cur:
                    cur["value"] += a.get("value") or 0
                    if a.get("disp") and a["disp"] not in cur["disp"]:
                        cur["disp"] += " / " + a["disp"]
                else:
                    attrs[nm] = {"value": a.get("value") or 0, "disp": a.get("disp") or ""}
    return attrs


def _row_from_item(it, name, usd, listings, enriched, book=None):
    """Monta uma linha. usd=None => item sem preço do bulk (marcado noBulk)."""
    row = {
        "name": name,
        "type": it["type"],
        "grade": it.get("grade"),
        "gradeRank": it.get("gradeRank"),   # 0..9 p/ ordenar grade por raridade
        "icon": it.get("icon"),             # nome do ícone (miniatura via wiki)
        "level": it.get("level"),   # nível do equipamento (None p/ material/box)
        "gold": it.get("gold"),
        "usd": round(usd, 4) if usd else None,
        "listings": listings or 0,
        "real": enriched.get(name),  # {usd:{...}, brl:{...}} ou None
        "book": (book or {}).get(name),  # encomendas: {brl:{buyMax,buyOrders,...}} ou None
        # metadados do item (colunas/filtros/tooltip na página)
        "gearType": it.get("gearType"),     # SWORD, BOW, SHIELD, BRACER, ORB...
        "gearGroup": it.get("gearGroup"),   # WEAPON / ARMOR / ACCESSORY
        "parts": it.get("parts"),           # MAIN_WEAPON, HELMET, AMULET, RING...
        "classes": it.get("classes") or [], # [Knight] / [All] / ...
        "variant": it.get("variant"),       # A / B
        "tradable": it.get("tradable"),     # vendável no mercado?
    }
    if is_grade_locked(name, it.get("grade")):
        row["gradeLock"] = True             # reabertura: grade top-3 sem listagem (≠ Soulstone)
    if not usd or usd <= 0:
        row["noBulk"] = True                # sem preço no snapshot do bulk
    if it.get("slots"):
        row["slots"] = it["slots"]          # {decoration, engraving, inscription}
    um = it.get("uniqueMod")
    if um and um != "0":
        row["uniqueMod"] = um               # modificador único (itens especiais)
    attrs = _item_attrs(it)
    if attrs:
        row["attrs"] = attrs   # { stat: {value, disp} }
    return row


def build_rows(items, steam, enriched=None, book=None):
    by_key = {}
    for it in items:
        by_key.setdefault(join_key(it), it)

    best = {}  # dedup: a paginação repete itens; mantém a maior liquidez
    for s in steam:
        n = s["name"]
        if n not in best or (s.get("sell_listings") or 0) > (best[n].get("sell_listings") or 0):
            best[n] = s

    enriched = enriched or {}
    book = book if book is not None else load_orderbook()
    rows, unmatched, seen = [], [], set()
    # 1) itens com preço do bulk (snapshot da Steam)
    for name, s in best.items():
        it = by_key.get(name)
        if not it:
            unmatched.append(name)
            continue
        gold = it.get("gold")
        usd = (s.get("sell_price") or 0) / 100.0  # vem em centavos
        if not gold or usd <= 0:
            continue
        rows.append(_row_from_item(it, name, usd, s.get("sell_listings") or 0, enriched, book))
        seen.add(name)
    # 2) tradáveis com gold que NÃO entraram no bulk: aparecem sem preço (busca sob demanda)
    for it in items:
        if not it.get("tradable") or not it.get("gold"):
            continue
        name = join_key(it)
        if name in seen:
            continue
        seen.add(name)
        rows.append(_row_from_item(it, name, None, 0, enriched, book))
    attach_trends(rows)   # Δ de preço (▲▼ %) a partir do histórico (USD)
    return rows, unmatched


# Piso de casamento item↔mercado. Hoje ~100%; uma queda indica nomes novos/renomeados (ex.: v1.00.20
# da reabertura) que o nosso base da wiki ainda não cobre. Não derruba o build — só ALERTA.
JOIN_MATCH_MIN = 0.98


def report_join_health(rows, unmatched, steam, public=False):
    """Imprime a saúde da junção e métricas-chave; ALERTA se o casamento cair. No CI (GitHub
    Actions), também escreve um resumo no job-summary. Best-effort: nunca quebra o build."""
    market = len({s["name"] for s in steam})
    matched = max(market - len(unmatched), 0)
    rate = matched / market if market else 1.0
    locked = sum(1 for r in rows if r.get("gradeLock"))
    with_book = sum(1 for r in rows if r.get("book"))
    with_real = sum(1 for r in rows if r.get("real"))
    print(f"\n[join] {len(rows)} linhas | casados {matched}/{market} ({rate:.1%}) | "
          f"{len(unmatched)} sem correspondência | {locked} intradáveis (grade-lock) | "
          f"{with_book} c/ encomenda | {with_real} c/ preço real")
    low = rate < JOIN_MATCH_MIN
    if low:
        print(f"::warning::[join] casamento {rate:.1%} < {JOIN_MATCH_MIN:.0%} — itens novos/renomeados? "
              f"rode --refresh e cheque a base da wiki")
    if unmatched:
        amostra = ", ".join(sorted(unmatched)[:15])
        print(f"[join] sem match (amostra): {amostra}{' …' if len(unmatched) > 15 else ''}")
    # resumo no job-summary do GitHub Actions (só no build público, o canônico do deploy)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and public:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(
                    f"### Build do site público\n"
                    f"- **Linhas:** {len(rows)} · **encomendas:** {with_book} · **preço real:** {with_real}\n"
                    f"- **Casamento item↔mercado:** {matched}/{market} (**{rate:.1%}**)"
                    f"{' ⚠️ **abaixo do piso**' if low else ' ✅'}\n"
                    f"- **Intradáveis (grade-lock):** {locked}\n")
                if unmatched:
                    f.write(f"- **Sem match ({len(unmatched)}):** {', '.join(sorted(unmatched)[:20])}"
                            f"{' …' if len(unmatched) > 20 else ''}\n")
        except OSError:
            pass


def attach_trends(rows, windows=(("chg24", 86400), ("chg7", 7 * 86400))):
    """Anexa variação % de preço (série USD do history.db) em cada linha. Silencioso se faltar BD.
    Inclui `chgReopen`: variação desde a reabertura do mercado (âncora absoluta) — fica vazio
    enquanto não houver ponto a partir de MARKET_REOPEN_TS (antes de 25/06 e nos itens sem dado)."""
    if not os.path.exists(HISTORY_DB):
        return
    now = int(time.time())
    series = {}
    try:
        with sqlite3.connect(HISTORY_DB) as conn:
            for name, ts, v in conn.execute(
                "SELECT name, ts, COALESCE(low, med) FROM price_history "
                "WHERE currency='usd' AND COALESCE(low, med) IS NOT NULL ORDER BY name, ts"):
                series.setdefault(name, []).append((ts, v))
    except sqlite3.Error:
        return

    def pct(pts, secs):
        if len(pts) < 2:
            return None
        new = pts[-1][1]
        cutoff = now - secs
        old = None
        for ts, v in pts:           # último ponto em/antes do corte
            if ts <= cutoff:
                old = v
            else:
                break
        if old is None or not old or old <= 0 or new is None:
            return None             # sem ponto antigo o bastante p/ a janela: não inventa
        return round((new - old) / old * 100, 1)

    def pct_since(pts, anchor):
        """Variação do 1º ponto EM/APÓS `anchor` (base da reabertura) até o mais recente."""
        if len(pts) < 2:
            return None
        new = pts[-1][1]
        base = next((v for ts, v in pts if ts >= anchor), None)
        if not base or base <= 0 or new is None or pts[-1][0] <= anchor:
            return None             # sem ponto pós-reabertura (ou só a própria base): não inventa
        return round((new - base) / base * 100, 1)

    for r in rows:
        pts = series.get(r["name"])
        if not pts:
            continue
        for key, secs in windows:
            c = pct(pts, secs)
            if c is not None:
                r[key] = c
        cr = pct_since(pts, MARKET_REOPEN_TS)
        if cr is not None:
            r["chgReopen"] = cr


# --- HTML (placeholders __TOKEN__ etc.; sem .format p/ o JS ficar legível) ----------------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TBH Market Tool — Itens × Mercado Steam</title>
<meta name="description" content="Ranking dos itens do Task Bar Hero cruzados com o Mercado Steam: retorno em gold e em gold por real (gold/R$). Preços atualizados automaticamente.">
<link rel="icon" type="image/svg+xml" href="assets/favicon.svg">
<meta property="og:type" content="website">
<meta property="og:site_name" content="TBH Market Tool">
<meta property="og:title" content="TBH Market Tool — Itens × Mercado Steam">
<meta property="og:description" content="Ranking de itens do Task Bar Hero por retorno em gold e gold/R$, com preços do Mercado Steam.">
<meta property="og:url" content="__SITE__/">
<meta property="og:image" content="__SITE__/assets/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="TBH Market Tool — Itens × Mercado Steam">
<meta name="twitter:description" content="Ranking de itens do Task Bar Hero por retorno em gold e gold/R$.">
<meta name="twitter:image" content="__SITE__/assets/og.png">
<style>
  :root { color-scheme: dark; --row:#13151a; --row-alt:#161922; --row-hover:#1c2029; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, Segoe UI, sans-serif; margin:0; background:#13151a; color:#e6e8ee; }
  header { padding:14px 20px; background:#1b1e26; border-bottom:1px solid #2a2e3a; }
  h1 { margin:0 0 4px; font-size:18px; }
  .meta { font-size:12px; color:#9aa3b8; }
  .chip { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px;
          background:#22263180; border:1px solid #2a2e3a; color:#c2c9da; }
  a { color:#7ab8ff; text-decoration:none; } a:hover { text-decoration:underline; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:10px 20px;
              background:#171a21; position:sticky; top:0; z-index:5; border-bottom:1px solid #2a2e3a; }
  .group { display:flex; gap:8px; align-items:center; }
  .group + .group { border-left:1px solid #2a2e3a; padding-left:8px; }
  input, select, button { background:#0f1116; color:#e6e8ee; border:1px solid #2a2e3a;
                          border-radius:6px; padding:7px 10px; font-size:13px; }
  button { cursor:pointer; } button:hover:not(:disabled) { background:#222634; }
  button:disabled { opacity:.5; cursor:default; }
  input:focus-visible, select:focus-visible, button:focus-visible, th:focus-visible {
    outline:2px solid #5b9dff; outline-offset:1px; }
  .seg { display:flex; border:1px solid #2a2e3a; border-radius:6px; overflow:hidden; }
  .seg button { border:0; border-radius:0; padding:7px 14px; }
  .seg button.on { background:#2f6df0; color:#fff; }
  .wrap { overflow:auto; max-height:calc(100vh - 168px); }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { padding:8px 12px; text-align:right; white-space:nowrap; border-bottom:1px solid #21242e; }
  th:nth-child(-n+2), td:nth-child(-n+2) { text-align:left; }
  thead th { position:sticky; top:0; background:#1b1e26; cursor:pointer; user-select:none; z-index:2; }
  /* ao navegar por ↑/↓ (scrollIntoView), reserva a altura do thead sticky p/ a linha não
     ficar escondida atrás do cabeçalho ao voltar ao topo */
  tbody tr { scroll-margin-top: 44px; }
  thead th:hover { background:#242838; }
  th .arrow { color:#5b9dff; font-size:11px; }
  th .hint { color:#5b6378; font-size:11px; cursor:help; margin-left:3px; }
  /* coluna "Item" fixa ao rolar na horizontal */
  thead th:first-child { left:0; z-index:3; }
  tbody td:first-child { position:sticky; left:0; background:var(--row); }
  tbody tr:nth-child(even) { background:var(--row-alt); }
  tbody tr:nth-child(even) td:first-child { background:var(--row-alt); }
  tbody tr:hover { background:var(--row-hover); }
  tbody tr:hover td:first-child { background:var(--row-hover); }
  /* 1º lugar da ordenação/filtro atuais — destaque evidente (fundo dourado + faixa) */
  tbody tr.best { background:#2c2710; }
  tbody tr.best:hover { background:#37300f; }
  tbody tr.best td { border-bottom-color:#3a3411; }
  tbody tr.best td:first-child { background:#2c2710; box-shadow:inset 4px 0 0 #f4c430; }
  tbody tr.best:hover td:first-child { background:#37300f; }
  .rank1 { font-size:13px; margin-right:2px; }
  /* linha selecionada pelo teclado (↑/↓) — vem depois do zebra/best p/ vencer no empate */
  tbody tr.sel td { background:#21304d; }
  tbody tr.sel td:first-child { background:#21304d; box-shadow:inset 3px 0 0 #5b9dff; }
  /* célula do item: estrela + nome colorido + botão Steam (flex no wrapper, td segue célula) */
  td.itemcell .itemwrap { display:flex; align-items:center; gap:6px; }
  .icon { width:30px; height:30px; flex:0 0 30px; border-radius:6px; object-fit:contain;
    background:#0f1116; border:1px solid #2a2e3a; image-rendering:pixelated; }
  .icon.noimg { display:inline-block; }   /* placeholder quando a imagem falha/ausente */
  .itemname { font-weight:600; }
  .fav { background:none; border:0; padding:0 2px; font-size:15px; line-height:1; color:#5b6378;
         cursor:pointer; }
  .fav:hover { color:#f4c430; background:none; }
  .fav.on { color:#f4c430; }
  a.steam { display:inline-flex; align-items:center; gap:3px; font-size:11px; color:#9aa3b8;
            border:1px solid #2a2e3a; border-radius:5px; padding:2px 7px; white-space:nowrap; }
  a.steam:hover { color:#fff; background:#2f6df0; border-color:#2f6df0; text-decoration:none; }
  /* botão de alternância (filtro de favoritos) */
  button.toggle.on { background:#2f6df0; color:#fff; border-color:#2f6df0; }
  /* disponibilidade / liquidez: bolinha por faixa + botão de verificação ao vivo */
  .liq { display:inline-block; width:9px; height:9px; border-radius:50%; cursor:help;
         vertical-align:middle; margin-right:4px; }
  .liq.hi { background:#4caf50; } .liq.mid { background:#e0a86a; } .liq.lo { background:#e07a7a; }
  .liq.none { background:#5b6378; }
  .buy { font-size:12px; padding:3px 7px; margin-left:2px; }
  .buychk.ok2 { color:#5fd38d; font-weight:600; cursor:help; }
  .buychk.no { color:#e07a7a; font-weight:600; cursor:help; }
  /* marcador de oportunidade (gold/moeda bem acima da mediana) */
  .deal { color:#5fd38d; font-weight:700; cursor:help; margin-left:4px; }
  .lvl { color:#c2c9da; }
  .trend { font-weight:600; font-variant-numeric:tabular-nums; white-space:nowrap; cursor:help; }
  .trend.up { color:#5fd38d; } .trend.down { color:#e07a7a; } .trend.flat { color:#9aa3b8; }
  /* colunas de preço "real" levemente destacadas vs estimado */
  td.real, th.real { background:#1a1f1a40; }
  .g { color:#f4c430; } .money { color:#5fd38d; } .ppr { color:#7ab8ff; font-weight:600; }
  .check { color:#5fd38d; font-size:11px; margin-left:4px; }
  .conv { color:#c2a24b; font-weight:600; cursor:help; }   /* preço convertido (≈) */
  .badge { font-size:11px; padding:1px 8px; border-radius:10px; background:#2a2e3a;
           border:1px solid transparent; }
  .low { color:#e07a7a; } .muted { color:#9aa3b8; }
  .warn { margin-left:5px; cursor:help; }
  /* selo de item intradável pela trava de grade da reabertura */
  .lock { font-size:10px; font-weight:600; color:#b9a0e0; border:1px solid #b9a0e055;
          background:#b9a0e01a; border-radius:6px; padding:1px 6px; cursor:help; white-space:nowrap; }
  /* indicador de frescor do preço real — sempre visível, cor por faixa */
  .age { display:inline-block; margin-left:6px; font-size:10px; font-weight:600;
         padding:1px 6px; border-radius:9px; cursor:help; vertical-align:middle;
         border:1px solid transparent; }
  .age.fresh { color:#5fd38d; background:#5fd38d1a; border-color:#5fd38d44; }
  .age.stale { color:#e0a86a; background:#e0a86a1a; border-color:#e0a86a55; }
  .age.old   { color:#e07a7a; background:#e07a7a1a; border-color:#e07a7a66; }
  .px { font-size:13px; padding:4px 9px; margin-left:6px; min-width:32px; }
  /* mini barra de proporção na coluna gold/moeda (est.) */
  td.bar { position:relative; }
  td.bar::before { content:""; position:absolute; left:0; top:3px; bottom:3px; width:var(--p,0);
    background:linear-gradient(90deg, #7ab8ff33, #7ab8ff0d); border-radius:0 3px 3px 0; z-index:0; }
  td.bar > .v { position:relative; z-index:1; }
  .empty { text-align:center; color:#9aa3b8; padding:32px 12px; }
  /* dropdown multi-seleção (filtros padronizados) */
  .dropdown { position:relative; display:inline-block; }
  .ddbtn.act { border-color:#4b69ff; color:#cdd6ff; }
  .ddpanel { position:absolute; z-index:30; top:calc(100% + 4px); left:0; min-width:210px;
    max-height:340px; overflow:auto; background:#0f1116; border:1px solid #2a2e3a;
    border-radius:8px; padding:6px; box-shadow:0 8px 28px #0009; }
  .ddpanel label { display:flex; align-items:center; gap:7px; padding:4px 6px; border-radius:6px;
    font-size:12px; cursor:pointer; white-space:nowrap; }
  .ddpanel label:hover { background:#1b1e26; }
  .ddpanel input[type=checkbox] { width:auto; margin:0; }
  .ddsearch { position:sticky; top:0; width:100%; box-sizing:border-box; margin:0 0 6px;
    background:#1b1e26; z-index:1; }
  /* painel de faixas (min–max) */
  .rangerow { display:grid; grid-template-columns:62px 1fr 1fr; gap:6px; align-items:center;
    margin:0 2px 7px; font-size:12px; color:#c2c9da; }
  .rangerow input { width:100%; box-sizing:border-box; }
  /* chips de filtros ativos */
  #activeFilters { display:flex; flex-wrap:wrap; gap:6px; align-items:center; padding:8px 20px;
    background:#141720; border-bottom:1px solid #2a2e3a; }
  #activeFilters:empty { display:none; }
  .fchip { display:inline-flex; align-items:center; gap:5px; font-size:11px; padding:3px 6px 3px 9px;
    border-radius:11px; background:#222631; border:1px solid #343a49; color:#dce2ee; }
  .fchip b { font-weight:600; color:#fff; }
  .fchip button { background:none; border:0; color:#9aa3b8; padding:0 2px; font-size:13px;
    line-height:1; cursor:pointer; border-radius:50%; }
  .fchip button:hover { color:#ff8a8a; }
  #fclearall { font-size:11px; color:#9aa3b8; background:none; border:1px dashed #3a4150;
    border-radius:11px; padding:3px 9px; }
  th.attrcol { color:#cdd6ff; }
  td.attrcell { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  td.sub { color:#aeb6c7; font-size:12px; white-space:nowrap; }
  .uniq { color:#e4ae39; cursor:help; }
  #status { font-size:12px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; }
  .ok { background:#4caf50; } .off { background:#777; }
  /* toasts (substituem alert()) */
  #toasts { position:fixed; right:16px; bottom:16px; z-index:50; display:flex;
            flex-direction:column; gap:8px; max-width:min(92vw,380px); }
  .toast { background:#1b1e26; border:1px solid #2a2e3a; border-left:3px solid #5b9dff;
           border-radius:8px; padding:10px 12px; font-size:13px; color:#e6e8ee;
           box-shadow:0 6px 24px #0008; white-space:pre-line; animation:tin .18s ease-out; }
  .toast.error { border-left-color:#e07a7a; } .toast.ok { border-left-color:#5fd38d; }
  .toast button { background:none; border:0; color:#9aa3b8; float:right; padding:0 0 0 8px; font-size:14px; }
  @keyframes tin { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
  footer { padding:10px 20px; font-size:11px; color:#8b93a7; }
  .support { display:inline-block; margin-top:6px; color:#c2a24b; cursor:help; }
  a.support { text-decoration:none; } a.support:hover { text-decoration:underline; }
  /* mobile: esconde colunas secundárias — Preço est.(5), Vol 24h(9), Listagens(10) */
  @media (max-width:720px) {
    .controls { padding:10px 12px; }
    header { padding:12px; }
    #t th:nth-child(5), #t td:nth-child(5),
    #t th:nth-child(9), #t td:nth-child(9),
    #t th:nth-child(10), #t td:nth-child(10) { display:none; }
    th, td { padding:8px 9px; }
    #detail { width:100%; }
  }
  /* nome do item: realce da busca + dica de clique */
  tbody td.itemcell { cursor:pointer; }
  .itemname mark { background:#f4c43055; color:inherit; border-radius:2px; padding:0 1px; }
  .abbr { cursor:help; }
  /* tooltip global estilizado (data-tip) */
  #tip { position:fixed; z-index:90; max-width:300px; background:#0b0d12; color:#e6e8ee;
    border:1px solid #343a49; border-radius:7px; padding:7px 10px; font-size:12px; line-height:1.4;
    box-shadow:0 8px 26px #000a; pointer-events:none; opacity:0; transition:opacity .1s;
    white-space:pre-line; }
  #tip.show { opacity:1; }
  /* drawer de detalhes */
  #detailOverlay { position:fixed; inset:0; background:#0006; z-index:60; opacity:0;
    pointer-events:none; transition:opacity .15s; }
  #detailOverlay.open { opacity:1; pointer-events:auto; }
  #detail { position:fixed; top:0; right:0; height:100vh; width:380px; max-width:92vw; z-index:61;
    background:#161922; border-left:1px solid #2a2e3a; box-shadow:-12px 0 40px #0007;
    transform:translateX(100%); transition:transform .18s ease-out; overflow-y:auto;
    display:flex; flex-direction:column; }
  #detail.open { transform:none; }
  #detail .dhead { display:flex; gap:12px; align-items:flex-start; padding:16px;
    border-bottom:1px solid #2a2e3a; position:sticky; top:0; background:#161922; }
  #detail .dhead img, #detail .dhead .icon { width:56px; height:56px; flex:0 0 56px; }
  #detail h2 { margin:0 0 4px; font-size:16px; line-height:1.25; }
  #detail .dclose { margin-left:auto; background:none; border:0; color:#9aa3b8; font-size:18px;
    cursor:pointer; padding:0 4px; }
  #detail .dclose:hover { color:#fff; }
  #detail .dbody { padding:14px 16px; display:flex; flex-direction:column; gap:14px; }
  .dsec h3 { margin:0 0 7px; font-size:11px; text-transform:uppercase; letter-spacing:.6px;
    color:#8b93a7; }
  .dgrid { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:13px; }
  .dgrid .k { color:#9aa3b8; } .dgrid .v { text-align:right; font-variant-numeric:tabular-nums; }
  .dattr { display:flex; justify-content:space-between; gap:10px; font-size:13px; padding:2px 0; }
  .dattr .an { color:#cdd6ff; } .dattr .av { font-weight:600; color:#7ab8ff; }
  #detail .dactions { display:flex; flex-wrap:wrap; gap:8px; margin-top:2px; }
  #detail .dactions a, #detail .dactions button { font-size:12px; }
  .spark { width:100%; height:54px; display:block; }
  .sparkwrap { background:#0f1116; border:1px solid #21242e; border-radius:8px; padding:8px; }
</style></head>
<body>
<header>
  <h1>TBH Market Tool — Itens × Mercado Steam</h1>
  <div class="meta">
    <span class="chip" data-tip="data/hora em que o ranking (bulk) foi gerado — horário do build (UTC no GitHub Actions); veja o horário local em 'preços atualizados'">📅 bulk: __GENERATED__</span>
    <span class="chip"><span id="count">__N__</span> itens</span>
    <span id="status" aria-live="polite"><span class="dot off"></span>verificando servidor…</span>
  </div>
  <div class="meta" id="baseline" style="margin-top:6px"></div>
</header>
<div class="controls">
  <div class="group">
    <input type="text" id="q" placeholder="buscar por nome..." aria-label="buscar por nome">
    <div class="dropdown" id="f_grade"></div>
    <div class="dropdown" id="f_type"></div>
    <div class="dropdown" id="f_gtype"></div>
    <div class="dropdown" id="f_cls"></div>
    <div class="dropdown" id="f_attr"></div>
    <div class="dropdown" id="f_range"></div>
    <select id="avail" aria-label="filtrar por disponibilidade no mercado"
        data-tip="disponibilidade: esconde itens indisponíveis (sem giro 24h ou sem oferta de venda)">
      <option value="">disponibilidade: todos</option>
      <option value="vol">só com giro 24h</option>
      <option value="offer">esconder sem oferta</option>
      <option value="buy">só com encomenda</option>
    </select>
    <span class="chip" id="resultcount" data-tip="itens visíveis / total"></span>
    <button id="favFilter" class="toggle" aria-pressed="false"
        data-tip="mostrar só os itens favoritados (⭐)">⭐ favoritos</button>
    <button id="clear" data-tip="limpa busca e todos os filtros">✕ Limpar</button>
  </div>
  <div class="group">
    <div class="seg" id="cur" role="group" aria-label="moeda">
      <button data-c="usd">USD $</button><button data-c="brl" class="on">BRL R$</button>
    </div>
    <label class="meta" id="rateWrap">taxa R$ <input type="number" id="rate" step="0.001"
        value="__RATE__" style="width:78px" aria-label="taxa USD para BRL"></label>
    <div class="seg" id="realmode" role="group" aria-label="qual preço real exibir">
      <button data-r="low" class="on">menor venda</button><button data-r="med">mediana</button>
    </div>
  </div>
  __SERVER_CONTROLS__
</div>
<div id="activeFilters" aria-label="filtros ativos"></div>
<div class="wrap">
<table id="t"><thead><tr>
  <th data-k="name" tabindex="0">Item</th>
  <th data-k="grade" tabindex="0">Grade</th>
  <th data-k="gearType" tabindex="0">Tipo<span class="hint" data-tip="tipo do equipamento (Sword, Bow, Shield, Bracer, Orb...) — passe o mouse no item p/ parte, variante, grupo, slots e tradável">ⓘ</span></th>
  <th data-k="classes" tabindex="0">Classe<span class="hint" data-tip="classe que pode usar (Knight, Ranger, Sorcerer, Priest, Hunter, Slayer ou All)">ⓘ</span></th>
  <th data-k="level" tabindex="0">Lvl<span class="hint" data-tip="nível do equipamento (— para materiais e caixas)">ⓘ</span></th>
  <th data-k="gold" tabindex="0">Gold (Cubo)<span class="hint" data-tip="gold recebido ao desmanchar/vender o item no Cubo (Alquimia) do jogo">ⓘ</span></th>
  <th data-k="priceEst" tabindex="0">Preço (est.)<span class="hint" data-tip="menor venda do bulk (USD) convertida pela taxa — barato e completo, mas aproximado">ⓘ</span></th>
  <th data-k="chg24" tabindex="0">Δ 24h<span class="hint" data-tip="variação % do preço (USD) nas últimas 24h, do histórico; passe o mouse para ver 7d. — = sem histórico suficiente">ⓘ</span></th>
  <th data-k="goldPerEst" tabindex="0"><span class="hlbl" data-sfx="(est.)">Gold / moeda</span><span class="hint" data-tip="gold ÷ preço estimado — quanto gold cada 1 unidade da moeda compra (maior = melhor negócio)">ⓘ</span></th>
  <th class="real" data-k="priceReal" tabindex="0">Preço real<span class="hint" data-tip="priceoverview da Steam na moeda escolhida — exato, sob demanda (modo servidor)">ⓘ</span></th>
  <th class="real" data-k="goldPerReal" tabindex="0"><span class="hlbl" data-sfx="(real)">Gold / moeda</span><span class="hint" data-tip="gold ÷ preço real">ⓘ</span></th>
  <th data-k="vol" tabindex="0">Vol 24h<span class="hint" data-tip="unidades vendidas nas últimas 24h — liquidez">ⓘ</span></th>
  <th data-k="listings" tabindex="0">Listagens<span class="hint" data-tip="quantidade à venda agora">ⓘ</span></th>
  <th class="book" data-k="buyMax" tabindex="0">Maior enc.<span class="hint" data-tip="maior ENCOMENDA (buy order) ativa, na moeda escolhida — o preço que um comprador paga AGORA. Vender nela vira saldo na hora. Passe o mouse p/ o líquido (−15% taxa Steam) e o spread">ⓘ</span></th>
  <th class="book" data-k="buyOrders" tabindex="0">Encomendas<span class="hint" data-tip="total de encomendas (demanda agregada): quantas unidades há querendo comprar. Maior = mais fácil liquidar por dinheiro sem derrubar o preço">ⓘ</span></th>
  <th data-k="liq" tabindex="0">Disp.<span class="hint" data-tip="disponibilidade/liquidez: bolinha pela heurística (listagens + volume); 🛒 confirma ofertas reais compráveis ao vivo">ⓘ</span></th>
</tr></thead><tbody></tbody></table>
</div>
<div id="toasts"></div>
<div id="detailOverlay"></div>
<aside id="detail" role="dialog" aria-modal="true" aria-label="detalhes do item" hidden></aside>
<div id="tip" role="tooltip"></div>
<footer>Fonte itens: taskbarherowiki.com · preços: steamcommunity.com/market (appid 3678970).
  "est." = menor venda do bulk (USD) convertida pela taxa · "real" = priceoverview (mediana) na moeda ·
  "Maior enc." = maior encomenda (buy order) ativa, "Encomendas" = demanda agregada — ordene por elas p/ achar o melhor item p/ vender por $.
  <!-- DOACAO: quando houver link (Ko-fi/PIX), trocar o <span> por <a href="LINK" target="_blank" rel="noopener noreferrer" class="support">…</a> -->
  <br><span class="support" data-tip="link de apoio em breve — obrigado pelo interesse!">💛 Apoie o projeto · doações <strong>em breve</strong></span></footer>
<script>
let DATA = __DATA__;
const TOKEN = "__TOKEN__";
const PUBLIC = __PUBLIC__;          // build do Pages: somente leitura (sem atualização pela web)
const GEN_EPOCH = __GEN_EPOCH__;    // epoch (s) em que o site/preços foram gerados
const $ = id => document.getElementById(id);
const fmt = n => (n==null ? "—" : Math.round(n).toLocaleString("pt-BR"));
const esc = s => { const d=document.createElement("div"); d.textContent=s??""; return d.innerHTML; };
const tbody = document.querySelector("#t tbody");
const BASE_COLS = 16;       // colunas fixas; +1 por atributo filtrado
const colCount = () => BASE_COLS + selAttrs.size;
const MARKET_FEE = 0.15;    // taxa estimada do Mercado Steam (~13–15%) p/ valor líquido ao vender

// score de liquidez 0–100 a partir de listagens (sempre) + volume 24h (quando há preço real)
function liqScore(listings, vol){
  const L = listings || 0;
  const ls = Math.min(1, Math.log10(L + 1) / Math.log10(200));   // ~200 listagens ≈ topo
  if(vol == null) return Math.round(ls * 60);                    // sem volume: teto menor (incerto)
  const vs = Math.min(1, Math.log10(vol + 1) / Math.log10(1000));
  return Math.round((ls * 0.45 + vs * 0.55) * 100);
}
function liqClass(score){ return score>=66 ? "hi" : score>=33 ? "mid" : score>0 ? "lo" : "none"; }

// ---- preferências persistidas (1.1) ----
const LS_KEY = "tbh-prefs-v1";
function loadPrefs(){ try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; } catch(e){ return {}; } }
// estado dos filtros (compartilhado por localStorage e URL)
function filterState(){
  return { q:$("q").value, grade:[...selGrade], type:[...selType], gtype:[...selGType],
    cls:[...selCls], attrs:[...selAttrs], avail:$("avail").value, showFavs,
    gmin:ranges.goldMin, gmax:ranges.goldMax, lmin:ranges.lvlMin, lmax:ranges.lvlMax,
    pmin:ranges.priceMin, pmax:ranges.priceMax, ml:ranges.minlist };
}
function savePrefs(){
  try { localStorage.setItem(LS_KEY, JSON.stringify(
    Object.assign({ cur, rate, sortK, sortDir, realMode, auto:autoOn }, filterState())
  )); } catch(e){}
}
// reflete o estado dos filtros na URL (compartilhável); só os que estão ativos
function syncURL(){
  const s = filterState(), p = new URLSearchParams();
  if(s.q) p.set("q", s.q);
  for(const k of ["grade","type","gtype","cls","attrs"]) if(s[k].length) p.set(k, s[k].join(","));
  if(s.avail) p.set("avail", s.avail);
  if(s.showFavs) p.set("fav","1");
  for(const k of ["gmin","gmax","lmin","lmax","pmin","pmax"]) if(s[k]!=null) p.set(k, s[k]);
  if(s.ml) p.set("ml", s.ml);
  const qs = p.toString();
  history.replaceState(null, "", qs ? "?"+qs : location.pathname);
}
const P = loadPrefs();
let cur = P.cur || "brl";
let rate = (typeof P.rate === "number" && P.rate>0) ? P.rate : (parseFloat($("rate").value) || 5.4);
let sortK = P.sortK || "goldPerEst";
let sortDir = (P.sortDir===1||P.sortDir===-1) ? P.sortDir : -1;
let realMode = P.realMode || "low";
let showFavs = !!P.showFavs;     // filtro "só favoritos" ativo?
let selRow = -1;                 // linha selecionada por teclado (↑/↓)
let serverOn = false;
let jobBusy = false;   // um trabalho na Steam por vez (lote, refresh, item avulso, calibrar)

// ---- favoritos (⭐) persistidos ----
const FAV_KEY = "tbh-favs-v1";
function loadFavs(){ try { return new Set(JSON.parse(localStorage.getItem(FAV_KEY)) || []); }
  catch(e){ return new Set(); } }
let favs = loadFavs();
function saveFavs(){ try { localStorage.setItem(FAV_KEY, JSON.stringify([...favs])); } catch(e){} }
function toggleFav(name){ favs.has(name) ? favs.delete(name) : favs.add(name); saveFavs(); render(); }
const sym = () => cur==="usd" ? "$" : "R$ ";

// cabeçalhos dependentes da moeda: "Gold / R$ (est.)" ou "Gold / $ (est.)"
function updateHeaders(){
  document.querySelectorAll(".hlbl").forEach(el=>{
    el.textContent = `Gold / ${sym().trim()} ${el.dataset.sfx}`;
  });
}

// trava/destrava os botões de ação enquanto um trabalho roda (espelha a regra do servidor)
function lockJobs(activeId){
  jobBusy = true;
  ["refresh","refreshVisible","calib"].forEach(id=>{ const el=$(id); if(el && id!==activeId) el.disabled = true; });
  render();   // desabilita também os ↻ por linha
}
function unlockJobs(){
  jobBusy = false;
  const _r=$("refresh"); if(_r) _r.disabled = false;
  const _c=$("calib"); if(_c) _c.disabled = !serverOn;
  render();   // reavalia o botão de lote e reabilita os ↻
}

// miniatura dos itens (mesma fonte da wiki dos itens)
const ICON_BASE = "https://www.taskbarherowiki.com/icons/";
// cores oficiais por grade/raridade (extraídas do CSS da wiki: --c-<grade>)
const GRADE_COLORS = {
  COMMON:"#9aa4b2", UNCOMMON:"#4ade80", RARE:"#38bdf8", LEGENDARY:"#f59e0b",
  IMMORTAL:"#ef4444", ARCANA:"#a855f7", BEYOND:"#ec4899", CELESTIAL:"#22d3ee",
  DIVINE:"#f2e7c4", COSMIC:"#e879f9"
};

// ---- toasts (substituem alert(), 1.4) ----
function toast(msg, type){
  const t = document.createElement("div");
  t.className = "toast" + (type ? " "+type : "");
  const b = document.createElement("button"); b.textContent = "✕"; b.setAttribute("aria-label","fechar");
  const span = document.createElement("span"); span.textContent = msg;
  b.onclick = () => t.remove();
  t.append(b, span); $("toasts").appendChild(t);
  if(type !== "error"){ setTimeout(()=>t.remove(), 6000); }
}

// frescor do preço real (5.1): acima disso, o lote re-busca e a etiqueta fica laranja
const STALE_S = 6*3600;        // 6 horas
// idade legível de um preço real
function ago(ts){
  if(!ts) return null;
  const s = Date.now()/1000 - ts;
  if(s < 90) return "agora";
  if(s < 3600) return Math.round(s/60)+" min";
  if(s < 86400) return Math.round(s/3600)+" h";
  return Math.round(s/86400)+" d";
}
// faixa de frescor -> classe de cor da etiqueta de idade
function ageClass(ts){
  if(!ts) return "old";
  const s = Date.now()/1000 - ts;
  if(s <= STALE_S) return "fresh";
  if(s <= 3*86400) return "stale";
  return "old";
}
// preço real mais recente em QUALQUER moeda (serve p/ converter e p/ saber o frescor)
function freshestReal(d){
  const r = d.real || {}; let best=null;
  for(const k of ["usd","brl"]){ const o=r[k];
    if(o && o.fetchedAt && (!best || o.fetchedAt>best.fetchedAt)) best=o; }
  return best;
}
// item precisa de (re)busca? sem nenhum preço real, ou o mais fresco já passou do limite
function needsUpdate(d){
  const b = freshestReal(d);
  if(!b) return true;
  return (Date.now()/1000 - b.fetchedAt) > STALE_S;
}

// humaniza identificadores em CAIXA/snake: MAIN_WEAPON -> Main Weapon, SWORD -> Sword
const titleCase = s => String(s||"").split(/[_ ]+/).filter(Boolean)
  .map(w=>w[0].toUpperCase()+w.slice(1).toLowerCase()).join(" ");

// ============ FILTROS PADRONIZADOS (busca + multi-seleção) ============
const ATTRS = [...new Set(DATA.flatMap(d=>d.attrs?Object.keys(d.attrs):[]))].sort();
const attrLabel = a => a.replace(/([a-z0-9])([A-Z])/g,"$1 $2");   // DamageReduction -> Damage Reduction
const selAttrList = () => ATTRS.filter(a=>selAttrs.has(a));       // ordem estável

// estado inicial: a URL tem prioridade sobre o localStorage
const toArr = x => Array.isArray(x) ? x.map(String) : (x ? [String(x)] : []);
function numOr(v){ const n=parseFloat(v); return isNaN(n)?null:n; }
function readURLState(){
  const p = new URLSearchParams(location.search);
  if(![...p.keys()].length) return null;
  const arr = k => (p.get(k) ? p.get(k).split(",").filter(Boolean) : []);
  return { q:p.get("q")||"", grade:arr("grade"), type:arr("type"), gtype:arr("gtype"),
    cls:arr("cls"), attrs:arr("attrs"), avail:p.get("avail")||"", showFavs:p.get("fav")==="1",
    gmin:numOr(p.get("gmin")), gmax:numOr(p.get("gmax")), lmin:numOr(p.get("lmin")),
    lmax:numOr(p.get("lmax")), pmin:numOr(p.get("pmin")), pmax:numOr(p.get("pmax")),
    ml:numOr(p.get("ml"))||0 };
}
const INIT = readURLState() || P;

const selGrade = new Set(toArr(INIT.grade));
const selType  = new Set(toArr(INIT.type));
const selGType = new Set(toArr(INIT.gtype));
const selCls   = new Set(toArr(INIT.cls));
let   selAttrs = new Set(toArr(INIT.attrs).filter(a=>ATTRS.includes(a)));
showFavs = !!INIT.showFavs;
const ranges = { goldMin:INIT.gmin??null, goldMax:INIT.gmax??null, lvlMin:INIT.lmin??null,
  lvlMax:INIT.lmax??null, priceMin:INIT.pmin??null, priceMax:INIT.pmax??null,
  minlist:(INIT.ml||(INIT.minlist?+INIT.minlist:0))||0 };

// só um painel dropdown aberto por vez
let openDD = null;
function closeDD(){ if(openDD){ openDD.panel.hidden=true; openDD.btn.setAttribute("aria-expanded","false"); openDD=null; } }

// fábrica: botão + painel (busca + checkboxes), gerencia um Set e chama onChange
function makeMultiSelect({mount, label, options, selected, onChange, sortByLabel=true}){
  const wrap = $(mount);
  const opts = sortByLabel ? options.slice().sort((a,b)=>a.label.localeCompare(b.label)) : options.slice();
  const btn = document.createElement("button");
  btn.type="button"; btn.className="ddbtn"; btn.setAttribute("aria-haspopup","true"); btn.setAttribute("aria-expanded","false");
  const panel = document.createElement("div");
  panel.className="ddpanel"; panel.hidden=true; panel.setAttribute("role","group"); panel.setAttribute("aria-label","filtrar por "+label);
  const search = document.createElement("input");
  search.type="text"; search.className="ddsearch"; search.placeholder="filtrar "+label.toLowerCase()+"..."; search.setAttribute("aria-label","buscar "+label);
  const list = document.createElement("div");
  list.innerHTML = opts.map(o=>`<label data-a="${esc(o.label.toLowerCase())}"><input type="checkbox" value="${esc(o.value)}"${selected.has(o.value)?" checked":""}> ${esc(o.label)}</label>`).join("");
  const empty = document.createElement("div"); empty.className="meta"; empty.style.padding="4px 6px"; empty.hidden=true; empty.textContent="nada encontrado";
  panel.append(search, list, empty);
  wrap.append(btn, panel);
  function updateBtn(){ btn.textContent = (selected.size ? `${label} (${selected.size})` : label) + " ▾"; btn.classList.toggle("act", selected.size>0); }
  list.querySelectorAll("input").forEach(cb=> cb.onchange = ()=>{
    cb.checked ? selected.add(cb.value) : selected.delete(cb.value); updateBtn(); onChange();
  });
  search.oninput = ()=>{
    const q = search.value.trim().toLowerCase(); let shown=0;
    list.querySelectorAll("label").forEach(l=>{ const hit=!q||l.dataset.a.includes(q); l.style.display=hit?"":"none"; if(hit)shown++; });
    empty.hidden = shown>0;
  };
  btn.onclick = (e)=>{ e.stopPropagation(); const opening=panel.hidden; closeDD();
    if(opening){ panel.hidden=false; btn.setAttribute("aria-expanded","true"); openDD={panel,btn}; search.focus(); } };
  panel.addEventListener("click", e=>e.stopPropagation());
  updateBtn();
  return { selected, updateBtn, label,
    syncChecks(){ list.querySelectorAll("input").forEach(cb=>cb.checked=selected.has(cb.value));
      search.value=""; list.querySelectorAll("label").forEach(l=>l.style.display=""); empty.hidden=true; updateBtn(); } };
}

const GRADE_RANK = {}; DATA.forEach(d=>{ if(d.grade!=null && d.gradeRank!=null) GRADE_RANK[d.grade]=d.gradeRank; });
const gradeOpts = [...new Set(DATA.map(d=>d.grade))].filter(Boolean)
  .sort((a,b)=>(GRADE_RANK[a]??99)-(GRADE_RANK[b]??99)).map(g=>({value:g,label:g}));
const typeOpts  = [...new Set(DATA.map(d=>d.type))].filter(Boolean).map(t=>({value:t,label:titleCase(t)}));
const gtypeOpts = [...new Set(DATA.map(d=>d.gearType))].filter(Boolean).map(t=>({value:t,label:titleCase(t)}));
const clsOpts   = [...new Set(DATA.flatMap(d=>d.classes||[]))].filter(Boolean).map(c=>({value:c,label:c}));
const attrOpts  = ATTRS.map(a=>({value:a,label:attrLabel(a)}));

const F = {
  grade: makeMultiSelect({mount:"f_grade", label:"Grade",     options:gradeOpts, selected:selGrade, onChange:()=>rerender(), sortByLabel:false}),
  type:  makeMultiSelect({mount:"f_type",  label:"Categoria", options:typeOpts,  selected:selType,  onChange:()=>rerender()}),
  gtype: makeMultiSelect({mount:"f_gtype", label:"Tipo",      options:gtypeOpts, selected:selGType, onChange:()=>rerender()}),
  cls:   makeMultiSelect({mount:"f_cls",   label:"Classe",    options:clsOpts,   selected:selCls,   onChange:()=>rerender()}),
  attr:  makeMultiSelect({mount:"f_attr",  label:"Atributos", options:attrOpts,  selected:selAttrs, onChange:()=>{ syncAttrCols(); rerender(); }}),
};

// insere/remove as colunas de valor (uma por atributo marcado), logo após a coluna Lvl
function syncAttrCols(){
  if(sortK && sortK.startsWith("av_") && !selAttrs.has(sortK.slice(3))){ sortK="goldPerEst"; sortDir=-1; }
  document.querySelectorAll("th[data-attrcol]").forEach(th=>th.remove());
  let after = document.querySelector('th[data-k="level"]');
  selAttrList().forEach(a=>{
    const th = document.createElement("th");
    th.dataset.k = "av_"+a; th.dataset.attrcol = a; th.tabIndex = 0;
    th.className = "attrcol"; th.textContent = attrLabel(a);
    th.dataset.tip = "valor do atributo — clique para ordenar";
    th.onclick = ()=>sortBy(th.dataset.k);
    th.onkeydown = (e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); sortBy(th.dataset.k); } };
    after.after(th); after = th;
  });
}

// ---- painel de faixas (gold, nível, preço, listagens mínimas) ----
function numId(id){ const v=parseFloat($(id).value); return isNaN(v)?null:v; }
function readRanges(){ ranges.goldMin=numId("r_goldMin"); ranges.goldMax=numId("r_goldMax");
  ranges.lvlMin=numId("r_lvlMin"); ranges.lvlMax=numId("r_lvlMax");
  ranges.priceMin=numId("r_priceMin"); ranges.priceMax=numId("r_priceMax");
  ranges.minlist=numId("r_minlist")||0; }
function rangeCount(){ return [ranges.goldMin,ranges.goldMax,ranges.lvlMin,ranges.lvlMax,ranges.priceMin,ranges.priceMax]
  .filter(v=>v!=null).length + (ranges.minlist>0?1:0); }
function updateRangeBtn(){ const n=rangeCount(), b=$("rangeBtn");
  b.textContent=(n?`Faixas (${n})`:"Faixas")+" ▾"; b.classList.toggle("act", n>0); }
(function buildRangePanel(){
  const wrap=$("f_range");
  const btn=document.createElement("button"); btn.type="button"; btn.id="rangeBtn"; btn.className="ddbtn";
  btn.setAttribute("aria-haspopup","true"); btn.setAttribute("aria-expanded","false");
  const panel=document.createElement("div"); panel.className="ddpanel"; panel.hidden=true; panel.style.minWidth="248px";
  const v = x => x==null ? "" : x;
  panel.innerHTML =
    `<div class="rangerow"><span>Gold</span><input type="number" id="r_goldMin" placeholder="mín" value="${v(ranges.goldMin)}"><input type="number" id="r_goldMax" placeholder="máx" value="${v(ranges.goldMax)}"></div>
     <div class="rangerow"><span>Nível</span><input type="number" id="r_lvlMin" placeholder="mín" value="${v(ranges.lvlMin)}"><input type="number" id="r_lvlMax" placeholder="máx" value="${v(ranges.lvlMax)}"></div>
     <div class="rangerow"><span>Preço</span><input type="number" id="r_priceMin" placeholder="mín" value="${v(ranges.priceMin)}"><input type="number" id="r_priceMax" placeholder="máx" value="${v(ranges.priceMax)}"></div>
     <div class="rangerow"><span>Listagens ≥</span><input type="number" id="r_minlist" placeholder="0" value="${ranges.minlist||''}" style="grid-column:2/4"></div>`;
  wrap.append(btn,panel);
  panel.querySelectorAll("input").forEach(inp=> inp.oninput = ()=>{ readRanges(); updateRangeBtn(); rerenderDebounced(); });
  btn.onclick=(e)=>{ e.stopPropagation(); const op=panel.hidden; closeDD();
    if(op){ panel.hidden=false; btn.setAttribute("aria-expanded","true"); openDD={panel,btn}; } };
  panel.addEventListener("click", e=>e.stopPropagation());
  updateRangeBtn();
})();

document.addEventListener("click", ()=>closeDD());   // clique fora fecha o dropdown aberto

// restaura DOM (busca, moeda, taxa, modo, disponibilidade, favoritos)
$("q").value = INIT.q || "";
if(INIT.avail) $("avail").value = INIT.avail;
$("rate").value = rate.toFixed(3).replace(/0+$/,"").replace(/\.$/,"");
$("cur").querySelectorAll("button").forEach(b=>b.classList.toggle("on", b.dataset.c===cur));
$("realmode").querySelectorAll("button").forEach(b=>b.classList.toggle("on", b.dataset.r===realMode));
$("rateWrap").style.display = cur==="brl" ? "" : "none";
$("favFilter").classList.toggle("on", showFavs);
$("favFilter").setAttribute("aria-pressed", String(showFavs));
syncAttrCols();

// preço estimado/real e gold por moeda, na moeda corrente
function priceEst(d){ if(d.usd==null) return null; return cur==="usd" ? d.usd : d.usd*rate; }
// objeto de preço real na moeda atual; se não houver, CONVERTE da outra moeda pela taxa
function realInfo(d){
  const r = d.real || {};
  const native = r[cur];
  if(native && (native.low!=null || native.med!=null)) return {obj:native, converted:false};
  const otherKey = cur==="usd" ? "brl" : "usd";
  const other = r[otherKey];
  if(other && (other.low!=null || other.med!=null)){
    const f = cur==="usd" ? (rate>0 ? 1/rate : 0) : rate;   // converte other -> moeda atual
    return { obj:{ low: other.low!=null ? other.low*f : null,
                   med: other.med!=null ? other.med*f : null,
                   vol: other.vol, fetchedAt: other.fetchedAt },
             converted:true, from:otherKey };
  }
  return {obj: native || null, converted:false};   // native pode ser placeholder "sem dados"
}
function pickPrice(o){ if(!o) return null; return realMode==="low" ? (o.low ?? o.med) : (o.med ?? o.low); }
// order book (encomendas) na moeda atual; converte preços da outra moeda pela taxa.
// Quantidades (buyOrders, qtd do book) NÃO se convertem — são contagens.
function bookInfo(d){
  const b = d.book || {};
  const native = b[cur];
  if(native) return {obj:native, converted:false};
  const otherKey = cur==="usd" ? "brl" : "usd";
  const other = b[otherKey];
  if(other){
    const f = cur==="usd" ? (rate>0 ? 1/rate : 0) : rate;
    return { obj:{ ...other,
              buyMax: other.buyMax!=null ? other.buyMax*f : null,
              sellMin: other.sellMin!=null ? other.sellMin*f : null,
              buyNotional: other.buyNotional!=null ? other.buyNotional*f : null,
              buyBook: (other.buyBook||[]).map(([p,q])=>[p*f,q]) },
             converted:true, from:otherKey };
  }
  return {obj:null, converted:false};
}
function derive(d){
  const pe = priceEst(d);
  const ri = realInfo(d), pr = pickPrice(ri.obj);
  const vol = ri.obj?.vol ?? null;
  const bi = bookInfo(d), bk = bi.obj;
  const buyMax = bk ? bk.buyMax : null;
  const buyOrders = bk ? bk.buyOrders : null;
  const sellMin = bk ? (bk.sellMin ?? null) : null;
  const topQty = (bk && bk.buyBook && bk.buyBook[0]) ? bk.buyBook[0][1] : null;
  const buyNet = buyMax!=null ? buyMax*(1-MARKET_FEE) : null;           // líquido (−taxa Steam)
  // score "melhor p/ vender por $": preço líquido da encomenda PONDERADO pela demanda (log)
  const buyScore = (buyNet!=null && buyOrders) ? buyNet*Math.log10(buyOrders+1) : null;
  return { ...d, priceEst:pe, goldPerEst: (pe!=null && pe>0) ? d.gold/pe : null,
           priceReal:pr, goldPerReal: pr>0 ? d.gold/pr : null,
           realConverted: ri.converted, realFrom: ri.from||null,
           vol, fetchedAt: ri.obj?.fetchedAt ?? null,
           liq: liqScore(d.listings, vol),     // p/ ordenar pela coluna Disp.
           buyMax, buyOrders, sellMin, buyNet, buyScore,
           buyTopValue: (buyMax!=null && topQty!=null) ? buyMax*topQty : null,
           buyNotional: bk ? (bk.buyNotional ?? null) : null,
           buyBook: bk ? (bk.buyBook || []) : [],
           buyConverted: bi.converted,
           spreadPct: (sellMin && buyMax!=null && sellMin>0) ? Math.round((sellMin-buyMax)/sellMin*100) : null };
}
// tooltip do item: campos que não viraram coluna (parte, variante, grupo, tradável, slots, único)
function detailTitle(d){
  const t=[];
  if(d.gearGroup) t.push("Grupo: "+titleCase(d.gearGroup));
  if(d.parts) t.push("Parte: "+titleCase(d.parts));
  if(d.variant) t.push("Variante: "+d.variant);
  t.push(d.tradable ? "Tradável ✓" : "Não-tradável ✕");
  if(d.slots){ const s=d.slots;
    t.push(`Slots — deco ${s.decoration||0} · engrav ${s.engraving||0} · inscr ${s.inscription||0}`); }
  if(d.uniqueMod) t.push("Único: "+attrLabel(d.uniqueMod));
  return t.join(" · ");
}
function steamUrl(name){
  return "https://steamcommunity.com/market/listings/3678970/" + encodeURIComponent(name);
}

// disponibilidade no mercado: "" todos · "vol" só com giro 24h · "offer" esconder sem oferta.
// "vol" mantém itens AINDA NÃO consultados (giro desconhecido ≠ giro zero) p/ não sumir tudo;
// só esconde os que foram buscados e confirmaram vol 0/nulo (sem venda nas últimas 24h).
function availPass(d, mode){
  if(!mode) return true;
  if(mode==="offer") return !(d.listings<=0 || (d.buy && d.buy.buyable===false));
  if(mode==="buy") return d.buyOrders>0;   // só itens com encomenda ativa conhecida
  if(d.fetchedAt==null) return true;   // sem consulta real → desconhecido, não esconde
  return d.vol>0;                       // giro 24h confirmado
}
// valor p/ ordenar: chaves "av_X" leem o atributo X; o resto é campo direto da linha
function sortVal(d, k){
  if(k && k.startsWith("av_")){ const a=d.attrs && d.attrs[k.slice(3)]; return a ? a.value : null; }
  if(k === "grade") return d.gradeRank ?? -1;   // ordena por raridade, não alfabético
  const v = d[k];
  return Array.isArray(v) ? v.join(", ") : v;   // ex.: classes -> "Knight"
}
// v dentro de [min,max] (limites opcionais); v null só passa se não houver limite
function inRange(v, mn, mx){
  if(v==null) return mn==null && mx==null;
  if(mn!=null && v<mn) return false;
  if(mx!=null && v>mx) return false;
  return true;
}
function currentRows(){
  const q=$("q").value.toLowerCase(), av=$("avail").value;
  const wantAttrs=selAttrList();
  let rows = DATA.map(derive).filter(d =>
    (!q||d.name.toLowerCase().includes(q)) &&
    (selGrade.size===0||selGrade.has(d.grade)) &&
    (selType.size===0||selType.has(d.type)) &&
    (selGType.size===0||selGType.has(d.gearType)) &&
    (selCls.size===0||(d.classes||[]).some(c=>selCls.has(c))) &&
    (!wantAttrs.length || wantAttrs.every(a=>d.attrs && d.attrs[a])) &&
    d.listings>=(ranges.minlist||0) &&
    inRange(d.gold, ranges.goldMin, ranges.goldMax) &&
    inRange(d.level, ranges.lvlMin, ranges.lvlMax) &&
    inRange(d.priceEst, ranges.priceMin, ranges.priceMax) &&
    (!showFavs||favs.has(d.name)) && availPass(d, av));
  rows.sort((a,b)=>{ const x=sortVal(a,sortK), y=sortVal(b,sortK);
    if(x==null) return 1; if(y==null) return -1;
    return (typeof x==="string"? x.localeCompare(y) : x-y)*sortDir; });
  return rows;
}

function render(){
  const rows = currentRows();

  // baseline na moeda corrente
  const ppr = rows.map(d=>d.goldPerEst).filter(v=>v>0).sort((a,b)=>a-b);
  const mean = ppr.length ? ppr.reduce((s,v)=>s+v,0)/ppr.length : 0;
  const med = ppr.length ? ppr[Math.floor(ppr.length/2)] : 0;
  const maxPpr = ppr.length ? ppr[ppr.length-1] : 0;   // p/ a mini-barra (2.1)
  $("baseline").innerHTML = `<span class="chip">📊 baseline gold/${sym().trim()} (est.)</span> `
    + `média <b>${fmt(mean)}</b> · mediana <b>${fmt(med)}</b> · <b>${fmt(rows.length)}</b> visíveis de ${fmt(DATA.length)}`;
  $("count").textContent = DATA.length;
  $("resultcount").textContent = `${rows.length} / ${DATA.length}`;
  renderChips();
  updateHeaders();
  updateEnrichBtn(rows);
  const dealCut = med * 2;   // arbitragem: gold/moeda >= 2× a mediana do filtro = ótimo negócio
  const acols = selAttrList();   // colunas de atributo visíveis nesta render

  if(!rows.length){
    tbody.innerHTML = `<tr><td class="empty" colspan="${colCount()}">
      Nenhum item corresponde aos filtros.
      <button id="clearEmpty" style="margin-left:8px">✕ Limpar filtros</button></td></tr>`;
    $("clearEmpty").onclick = clearFilters;
    markSort();
    return;
  }

  tbody.innerHTML = rows.map((d,i)=>{
    // destaque do 1º lugar = primeira linha da ORDENAÇÃO/FILTRO atuais (não o máx. de gold/est)
    const first = i===0 ? " best" : "";
    const rank = i===0 ? `<span class="rank1" title="1º na ordenação atual">🏆</span> ` : "";
    const barP = maxPpr>0 ? (d.goldPerEst/maxPpr*100) : 0;
    const pxDis = jobBusy ? " disabled" : "";
    const px = serverOn
      ? `<button class="px"${pxDis} data-name="${esc(d.name)}" title="buscar preço real agora" aria-label="buscar preço real">↻</button>` : "";
    const gc = GRADE_COLORS[d.grade];
    const badgeStyle = gc ? ` style="color:${gc};border-color:${gc}55;background:${gc}1a"` : "";
    const isFav = favs.has(d.name);
    const star = `<button class="fav${isFav?' on':''}" data-name="${esc(d.name)}" aria-label="favoritar" title="${isFav?'remover dos favoritos':'favoritar'}">${isFav?'⭐':'☆'}</button>`;
    // miniatura: borda na cor da raridade; se a imagem falhar, vira placeholder vazio
    const iconImg = d.icon
      ? `<img class="icon" src="${ICON_BASE}${encodeURIComponent(d.icon)}.png" alt="" loading="lazy" decoding="async"${gc?` style="border-color:${gc}66"`:""} onerror="this.onerror=null;this.classList.add('noimg');this.removeAttribute('src')">`
      : `<span class="icon noimg"></span>`;
    const uniq = d.uniqueMod ? `<span class="uniq" data-tip="modificador único: ${esc(attrLabel(d.uniqueMod))}">✦</span> ` : "";
    const nameHtml = `<span class="itemname"${gc?` style="color:${gc}"`:""} data-tip="clique para ver detalhes">${uniq}${highlightName(d.name)}</span>`;
    const steamBtn = `<a class="steam" href="${steamUrl(d.name)}" target="_blank" rel="noopener noreferrer" title="abrir listagem na Steam" aria-label="abrir na Steam">↗ Steam</a>`;
    const hasReal = d.priceReal!=null;
    const conv = hasReal && d.realConverted;   // preço veio convertido da outra moeda
    const convMark = conv
      ? `<span class="conv" title="convertido de ${d.realFrom==='brl'?'R$':'US$'} pela taxa — clique em ↻ p/ preço real em ${cur.toUpperCase()}">≈</span> ` : "";
    const check = (hasReal && !conv) ? `<span class="check" title="preço real obtido da Steam">✓</span>` : "";
    const age = ago(d.fetchedAt);   // etiqueta de idade SEMPRE visível quando há consulta real
    const ac = ageClass(d.fetchedAt);
    const ageTag = age
      ? `<span class="age ${ac}" title="preço real atualizado há ${age}${ac!=='fresh'?' — convém re-buscar':''}">${ac==='fresh'?'':'⏱ '}${age}</span>`
      : "";
    // trava de grade na reabertura: grade top-3 sem listagem (≠ Soulstone). Não é "sem oferta":
    // suprime os ⚠️ de liquidez (falsos aqui) e mostra um selo "intradável" no lugar do preço.
    const gLock = d.gradeLock===true;
    const lockTag = gLock ? `<span class="lock" title="grade restrito de listagem na reabertura do mercado — liberação em anúncio futuro do jogo (Soulstones são exceção)">intradável</span>` : "";
    const volWarn = (!gLock && d.vol!=null&&d.vol<5) ? `<span class="warn" title="liquidez baixa: menos de 5 vendas/24h">⚠️</span>` : "";
    const listWarn = (!gLock && d.listings<10) ? `<span class="warn" title="poucas listagens: preço pode oscilar">⚠️</span>` : "";
    // arbitragem: marca quando gold/moeda (est.) está bem acima da mediana do filtro
    const deal = (d.goldPerEst>0 && dealCut>0 && d.goldPerEst>=dealCut)
      ? `<span class="deal" title="ótimo negócio: gold/${sym().trim()} ≥ 2× a mediana do filtro">🔥</span>` : "";
    // valor líquido ao VENDER no mercado (após ~${MARKET_FEE*100}% de taxa) — informativo
    const netTitle = hasReal
      ? ` title="líquido ao vender no mercado: ${sym()}${(d.priceReal*(1-MARKET_FEE)).toFixed(2)} (−${Math.round(MARKET_FEE*100)}% taxa Steam)"` : "";
    // encomendas (buy orders): maior encomenda + demanda
    const bConv = d.buyConverted ? `<span class="conv" title="encomenda convertida da outra moeda pela taxa">≈</span> ` : "";
    const buyTitle = d.buyMax!=null
      ? ` title="líquido ao vender na encomenda: ${sym()}${d.buyNet.toFixed(2)} (−${Math.round(MARKET_FEE*100)}% taxa Steam)${d.spreadPct!=null?` · spread compra/venda ${d.spreadPct}%`:''}${d.buyNotional!=null?` · book de compra ${sym()}${d.buyNotional.toFixed(2)}`:''}"` : "";
    // disponibilidade/liquidez: heurística (bolinha) + verificação ao vivo (🛒)
    const score = liqScore(d.listings, d.vol);
    const liqDot = `<span class="liq ${liqClass(score)}" title="liquidez ${score}/100 — ${fmt(d.listings)} listagens${d.vol!=null?`, vol 24h ${fmt(d.vol)}`:''}"></span>`;
    const buy = d.buy;   // resultado da verificação ao vivo, se já feita
    let buyMark = "";
    if(buy){
      buyMark = buy.buyable
        ? `<span class="buychk ok2" title="comprável · menor ${sym()}${(buy.low??0).toFixed(2)} · verificado ao vivo">✓</span>`
        : `<span class="buychk no" title="sem oferta comprável agora (loja com erro/indisponível) · verificado ao vivo">✕</span>`;
    }
    const buyBtn = serverOn
      ? `<button class="buy"${pxDis} data-name="${esc(d.name)}" title="verificar ao vivo se há oferta comprável" aria-label="verificar compra">🛒</button>` : "";
    return `<tr class="${first.trim()}" data-name="${esc(d.name)}">
      <td class="itemcell"><span class="itemwrap">${star}${iconImg}${rank}${nameHtml}${steamBtn}</span></td>
      <td><span class="badge"${badgeStyle}>${esc(d.grade)}</span></td>
      <td class="sub">${d.gearType?esc(titleCase(d.gearType)):"—"}</td>
      <td class="sub">${(d.classes&&d.classes.length)?esc(d.classes.join(", ")):"—"}</td>
      <td class="lvl">${d.level!=null?d.level:"—"}</td>
      ${acols.map(a=>`<td class="attrcell">${d.attrs&&d.attrs[a]?esc(d.attrs[a].disp):"—"}</td>`).join("")}
      <td class="g abbr" data-tip="${fmt(d.gold)} gold">${fmtAbbr(d.gold)}</td>
      <td class="money">${gLock?lockTag:(d.priceEst!=null?(sym()+d.priceEst.toFixed(2)):`<span class="muted" data-tip="sem preço no bulk${serverOn?' — clique ↻ p/ buscar':''}">—</span>`)}</td>
      <td>${trendCell(d)}</td>
      <td class="ppr bar" style="--p:${barP}%"><span class="v abbr" data-tip="${fmt(d.goldPerEst)}">${fmtAbbr(d.goldPerEst)}</span>${deal}</td>
      <td class="money real"${netTitle}>${hasReal?(convMark+sym()+d.priceReal.toFixed(2)):"—"}${check}${ageTag}${px}</td>
      <td class="ppr real">${d.goldPerReal!=null?`<span class="abbr" data-tip="${fmt(d.goldPerReal)}">${fmtAbbr(d.goldPerReal)}</span>`:"—"}</td>
      <td class="${(d.vol!=null&&d.vol<5)?'low':''}">${fmt(d.vol)}${volWarn}</td>
      <td class="${d.listings<10?'low':''}">${fmt(d.listings)}${listWarn}</td>
      <td class="money book"${buyTitle}>${d.buyMax!=null?(bConv+sym()+d.buyMax.toFixed(2)):'—'}</td>
      <td class="${(d.buyOrders!=null&&d.buyOrders<5)?'low':''}"${buyTitle}>${d.buyOrders!=null?fmt(d.buyOrders):'—'}</td>
      <td>${liqDot}${buyMark}${buyBtn}</td>
    </tr>`; }).join("");

  markSort();
  selRow = -1;   // a tabela foi reconstruída: zera a navegação por teclado
  document.querySelectorAll(".px").forEach(b=> b.onclick = ()=>fetchPrice(b.dataset.name, b));
  document.querySelectorAll(".fav").forEach(b=> b.onclick = ()=>toggleFav(b.dataset.name));
  document.querySelectorAll(".buy").forEach(b=> b.onclick = ()=>checkListing(b.dataset.name, b));
}

// seta de ordenação + aria-sort (4.2)
function markSort(){
  document.querySelectorAll("th").forEach(th=>{
    th.querySelector(".arrow")?.remove();
    if(th.dataset.k===sortK){
      th.setAttribute("aria-sort", sortDir<0 ? "descending" : "ascending");
      const s=document.createElement("span"); s.className="arrow";
      s.textContent=sortDir<0?" ▼":" ▲"; th.appendChild(s);
    } else { th.removeAttribute("aria-sort"); }
  });
}

function clearFilters(){
  $("q").value="";
  [selGrade, selType, selGType, selCls, selAttrs].forEach(s=>s.clear());
  Object.values(F).forEach(f=>f.syncChecks());
  ranges.goldMin=ranges.goldMax=ranges.lvlMin=ranges.lvlMax=ranges.priceMin=ranges.priceMax=null;
  ranges.minlist=0;
  ["r_goldMin","r_goldMax","r_lvlMin","r_lvlMax","r_priceMin","r_priceMax","r_minlist"]
    .forEach(id=>{ const e=$(id); if(e) e.value=""; });
  updateRangeBtn(); syncAttrCols();
  $("avail").value="";
  showFavs=false; $("favFilter").classList.remove("on"); $("favFilter").setAttribute("aria-pressed","false");
  rerender();
}

// debounce p/ a busca (1.7)
const debounce = (fn,ms)=>{ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a),ms); }; };
const rerender = ()=>{ savePrefs(); syncURL(); render(); };
const rerenderDebounced = debounce(rerender, 150);

function sortBy(k){
  if(k===sortK) sortDir*=-1; else { sortK=k; sortDir=["name","grade","gearType","classes"].includes(k)?1:-1; }
  rerender();
}
document.querySelectorAll("th").forEach(th=>{
  th.onclick=()=>sortBy(th.dataset.k);
  th.onkeydown=(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); sortBy(th.dataset.k); } };
});
$("cur").querySelectorAll("button").forEach(b=>b.onclick=()=>{
  cur=b.dataset.c;
  $("cur").querySelectorAll("button").forEach(x=>x.classList.toggle("on", x===b));
  $("rateWrap").style.display = cur==="brl" ? "" : "none";
  rerender();
});
$("realmode").querySelectorAll("button").forEach(b=>b.onclick=()=>{
  realMode=b.dataset.r;
  $("realmode").querySelectorAll("button").forEach(x=>x.classList.toggle("on", x===b));
  rerender();
});
$("q").addEventListener("input", rerenderDebounced);
$("avail").addEventListener("input", rerender);
$("rate").addEventListener("input", ()=>{ rate=parseFloat($("rate").value)||rate; rerenderDebounced(); });
$("clear").onclick = clearFilters;
$("favFilter").onclick = ()=>{
  showFavs = !showFavs;
  $("favFilter").classList.toggle("on", showFavs);
  $("favFilter").setAttribute("aria-pressed", String(showFavs));
  rerender();
};

// ---- navegação por teclado pelas linhas (↑/↓) + abrir na Steam (Enter) ----
function visibleRows(){
  return [...tbody.querySelectorAll("tr")].filter(tr=>!tr.querySelector(".empty"));
}
function selectRow(i){
  const trs = visibleRows();
  if(!trs.length) return;
  i = Math.max(0, Math.min(trs.length-1, i));
  trs.forEach(tr=>tr.classList.remove("sel"));
  trs[i].classList.add("sel");
  trs[i].scrollIntoView({block:"nearest"});
  selRow = i;
}
function rowItem(tr){ const n = tr && tr.dataset.name; return n ? DATA.find(d=>d.name===n) : null; }
function openSelDetail(){ const trs=visibleRows(); const d=selRow>=0?rowItem(trs[selRow]):null; if(d) openDetail(d); }

// atalhos: "/" foca busca · Esc fecha/limpa · ↑/↓ navegam · Enter abre detalhes
document.addEventListener("keydown", e=>{
  const el = document.activeElement, tag = el ? el.tagName : "";
  const inField = /^(INPUT|SELECT|TEXTAREA)$/.test(tag);
  if(e.key==="/" && !inField){ e.preventDefault(); $("q").focus(); return; }
  if(e.key==="Escape"){
    if(openDD){ closeDD(); return; }                       // 1º: fecha dropdown aberto
    if(!$("detail").hidden){ closeDetail(); return; }       // 2º: fecha o drawer
    if(el===$("q") && $("q").value){ $("q").value=""; rerender(); }
    else { clearFilters(); }
    if(el && el.blur) el.blur();
    return;
  }
  if(inField) return;   // enquanto digita, não sequestra setas/Enter
  if(e.key==="ArrowDown"){ e.preventDefault(); selectRow(selRow+1); }
  else if(e.key==="ArrowUp"){ e.preventDefault(); selectRow(selRow<0?0:selRow-1); }
  else if(e.key==="Enter"){ openSelDetail(); }
});

// ============ números, destaque, chips, tooltip e drawer ============
// abrevia números grandes (2,7M / 1,2B / 12,3k); valor cheio fica no data-tip
function fmtAbbr(n){
  if(n==null) return "—";
  const a=Math.abs(n);
  const f=(x,s)=> x.toLocaleString("pt-BR",{maximumFractionDigits:1})+s;
  if(a>=1e9) return f(n/1e9,"B");
  if(a>=1e6) return f(n/1e6,"M");
  if(a>=1e4) return f(n/1e3,"k");   // só abrevia a partir de 10k (abaixo cabe inteiro)
  return fmt(n);
}
// célula de tendência: ▲/▼ % (24h) com tooltip 24h·7d; "—" quando não há histórico
function trendCell(d){
  const c=d.chg24;
  if(c==null) return '<span class="muted">—</span>';
  const cls=c>0?"up":(c<0?"down":"flat"), arr=c>0?"▲":(c<0?"▼":"■");
  const tip=`24h: ${c>0?"+":""}${c}%`+(d.chg7!=null?` · 7d: ${d.chg7>0?"+":""}${d.chg7}%`:"")
            +(d.chgReopen!=null?` · desde a reabertura: ${d.chgReopen>0?"+":""}${d.chgReopen}%`:"");
  return `<span class="trend ${cls}" data-tip="${tip}">${arr} ${Math.abs(c)}%</span>`;
}
// realça o trecho buscado no nome (sobre o texto já escapado)
function highlightName(name){
  const q=$("q").value.trim();
  if(!q) return esc(name);
  const i=name.toLowerCase().indexOf(q.toLowerCase());
  if(i<0) return esc(name);
  return esc(name.slice(0,i))+"<mark>"+esc(name.slice(i,i+q.length))+"</mark>"+esc(name.slice(i+q.length));
}

// chips de filtros ativos (cada um removível)
function renderChips(){
  const box=$("activeFilters"); box.innerHTML="";
  const add=(label,onRemove)=>{
    const c=document.createElement("span"); c.className="fchip";
    const s=document.createElement("span"); s.textContent=label; c.appendChild(s);
    const b=document.createElement("button"); b.type="button"; b.textContent="✕";
    b.setAttribute("aria-label","remover filtro "+label);
    b.onclick=()=>{ onRemove(); rerender(); };
    c.appendChild(b); box.appendChild(c);
  };
  if($("q").value) add(`Busca: "${$("q").value}"`, ()=>{ $("q").value=""; });
  const groups=[["Grade",selGrade,"grade",v=>v],["Categoria",selType,"type",titleCase],
    ["Tipo",selGType,"gtype",titleCase],["Classe",selCls,"cls",v=>v]];
  groups.forEach(([lab,set,key,fmtv])=>[...set].forEach(v=>
    add(`${lab}: ${fmtv(v)}`, ()=>{ set.delete(v); F[key].syncChecks(); })));
  [...selAttrs].forEach(v=> add(`Atributo: ${attrLabel(v)}`,
    ()=>{ selAttrs.delete(v); F.attr.syncChecks(); syncAttrCols(); }));
  const rl={goldMin:["Gold ≥","r_goldMin"],goldMax:["Gold ≤","r_goldMax"],lvlMin:["Nível ≥","r_lvlMin"],
    lvlMax:["Nível ≤","r_lvlMax"],priceMin:["Preço ≥","r_priceMin"],priceMax:["Preço ≤","r_priceMax"]};
  Object.keys(rl).forEach(k=>{ if(ranges[k]!=null) add(`${rl[k][0]} ${ranges[k]}`,
    ()=>{ ranges[k]=null; const e=$(rl[k][1]); if(e)e.value=""; updateRangeBtn(); }); });
  if(ranges.minlist>0) add(`Listagens ≥ ${ranges.minlist}`,
    ()=>{ ranges.minlist=0; const e=$("r_minlist"); if(e)e.value=""; updateRangeBtn(); });
  if(showFavs) add("⭐ Favoritos",
    ()=>{ showFavs=false; $("favFilter").classList.remove("on"); $("favFilter").setAttribute("aria-pressed","false"); });
  if($("avail").value){ const m={vol:"só com giro 24h",offer:"esconder sem oferta"};
    add(m[$("avail").value]||$("avail").value, ()=>{ $("avail").value=""; }); }
  if(box.children.length>1){ const b=document.createElement("button"); b.id="fclearall";
    b.textContent="✕ limpar tudo"; b.onclick=clearFilters; box.appendChild(b); }
}

// ---- tooltip global estilizado (data-tip) ----
const tipEl=$("tip");
function showTip(t){
  const txt=t.getAttribute("data-tip"); if(!txt){ hideTip(); return; }
  tipEl.textContent=txt; tipEl.classList.add("show");
  const r=t.getBoundingClientRect(), tr=tipEl.getBoundingClientRect();
  let left=Math.max(6, Math.min(r.left+r.width/2-tr.width/2, innerWidth-tr.width-6));
  let top=r.bottom+6; if(top+tr.height>innerHeight-6) top=r.top-tr.height-6;
  tipEl.style.left=left+"px"; tipEl.style.top=Math.max(6,top)+"px";
}
function hideTip(){ tipEl.classList.remove("show"); }
document.addEventListener("mouseover", e=>{ const t=e.target.closest&&e.target.closest("[data-tip]"); if(t) showTip(t); });
document.addEventListener("mouseout", e=>{ if(e.target.closest&&e.target.closest("[data-tip]")) hideTip(); });
document.addEventListener("focusin", e=>{ const t=e.target.closest&&e.target.closest("[data-tip]"); if(t) showTip(t); });
document.addEventListener("focusout", hideTip);
window.addEventListener("scroll", hideTip, true);

// ---- drawer de detalhes ----
function closeDetail(){
  $("detail").classList.remove("open"); $("detailOverlay").classList.remove("open");
  $("detail").hidden=true;
}
$("detailOverlay").onclick=closeDetail;
function kvHtml(pairs){ return pairs.map(([k,v])=>`<div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div>`).join(""); }
function openDetail(raw){
  const d=derive(raw); const gc=GRADE_COLORS[d.grade]||"#9aa3b8";
  const iconImg = d.icon
    ? `<img class="icon" src="${ICON_BASE}${encodeURIComponent(d.icon)}.png" alt="" style="border-color:${gc}66" onerror="this.onerror=null;this.classList.add('noimg');this.removeAttribute('src')">`
    : `<span class="icon noimg"></span>`;
  const meta=[];
  if(d.gearType) meta.push(["Tipo", titleCase(d.gearType)]);
  if(d.classes&&d.classes.length) meta.push(["Classe", d.classes.join(", ")]);
  if(d.gearGroup) meta.push(["Grupo", titleCase(d.gearGroup)]);
  if(d.parts) meta.push(["Parte", titleCase(d.parts)]);
  if(d.level!=null) meta.push(["Nível", d.level]);
  if(d.variant) meta.push(["Variante", d.variant]);
  if(d.slots) meta.push(["Slots", `deco ${d.slots.decoration||0} · engrav ${d.slots.engraving||0} · inscr ${d.slots.inscription||0}`]);
  meta.push(["Tradável", d.gradeLock?"intradável (trava de grade na reabertura)":(d.tradable?"sim":"não")]);
  if(d.uniqueMod) meta.push(["Único", attrLabel(d.uniqueMod)]);
  const attrKeys = d.attrs ? Object.keys(d.attrs).sort() : [];
  const attrHtml = attrKeys.length
    ? attrKeys.map(a=>`<div class="dattr"><span class="an">${esc(attrLabel(a))}</span><span class="av">${esc(d.attrs[a].disp)}</span></div>`).join("")
    : `<div class="meta">sem atributos</div>`;
  const econ=[["Gold (Cubo)", fmt(d.gold)],
    ["Preço (est.)", d.priceEst!=null ? sym()+d.priceEst.toFixed(2) : "— (sem bulk)"],
    [`Gold / ${sym().trim()} (est.)`, d.goldPerEst!=null ? fmt(d.goldPerEst) : "—"]];
  if(d.chg24!=null) econ.push(["Δ 24h", `${d.chg24>0?"+":""}${d.chg24}%`]);
  if(d.chg7!=null)  econ.push(["Δ 7d",  `${d.chg7>0?"+":""}${d.chg7}%`]);
  if(d.chgReopen!=null) econ.push(["Δ desde a reabertura", `${d.chgReopen>0?"+":""}${d.chgReopen}%`]);
  if(d.priceReal!=null){ econ.push(["Preço real", sym()+d.priceReal.toFixed(2)]);
    if(d.goldPerReal!=null) econ.push([`Gold / ${sym().trim()} (real)`, fmt(d.goldPerReal)]); }
  if(d.vol!=null) econ.push(["Vol 24h", fmt(d.vol)]);
  econ.push(["Listagens", fmt(d.listings)]);
  if(d.buyMax!=null){
    econ.push(["Maior encomenda", (d.buyConverted?"≈ ":"")+sym()+d.buyMax.toFixed(2)]);
    econ.push(["Líquido na encomenda", sym()+d.buyNet.toFixed(2)+` (−${Math.round(MARKET_FEE*100)}% taxa)`]);
  }
  if(d.buyOrders!=null) econ.push(["Encomendas (demanda)", fmt(d.buyOrders)]);
  if(d.spreadPct!=null) econ.push(["Spread compra/venda", d.spreadPct+"%"]);
  if(d.buyNotional!=null) econ.push(["Valor do book de compra", sym()+d.buyNotional.toFixed(2)]);
  const isFav=favs.has(d.name);
  $("detail").innerHTML = `
    <div class="dhead">
      ${iconImg}
      <div style="min-width:0">
        <h2 style="color:${gc}">${d.uniqueMod?'✦ ':''}${esc(d.name)}</h2>
        <span class="badge" style="color:${gc};border-color:${gc}55;background:${gc}1a">${esc(d.grade)}</span>
      </div>
      <button class="dclose" aria-label="fechar detalhes">✕</button>
    </div>
    <div class="dbody">
      <div class="dsec"><h3>Detalhes</h3><div class="dgrid">${kvHtml(meta)}</div></div>
      <div class="dsec"><h3>Atributos</h3>${attrHtml}</div>
      <div class="dsec"><h3>Economia</h3><div class="dgrid">${kvHtml(econ)}</div></div>
      <div class="dsec" id="dHist"><h3>Histórico de preço</h3><div class="meta">${serverOn?'carregando…':'disponível no modo servidor'}</div></div>
      <div class="dactions">
        <a class="steam" href="${steamUrl(d.name)}" target="_blank" rel="noopener noreferrer">↗ Steam</a>
        <button id="dCopy">⧉ copiar nome</button>
        <button id="dFav">${isFav?'★ favoritado':'☆ favoritar'}</button>
      </div>
    </div>`;
  $("detail").hidden=false;
  requestAnimationFrame(()=>{ $("detail").classList.add("open"); $("detailOverlay").classList.add("open"); });
  $("detail").querySelector(".dclose").onclick=closeDetail;
  $("dCopy").onclick=()=>{ (navigator.clipboard?navigator.clipboard.writeText(d.name):Promise.reject())
    .then(()=>toast("Nome copiado.","ok"), ()=>toast("Não foi possível copiar.","error")); };
  $("dFav").onclick=()=>{ toggleFav(d.name); openDetail(raw); };
  if(serverOn) loadHistory(d.name);
}
async function loadHistory(name){
  const box=$("dHist"); if(!box) return;
  try{
    const r=await api(`/api/history?currency=${cur}&name=${encodeURIComponent(name)}`);
    drawSpark(box, (r&&r.points)||[]);
  }catch(e){ box.innerHTML=`<h3>Histórico de preço</h3><div class="meta">sem histórico</div>`; }
}
function drawSpark(box, raw){
  const pts=(raw||[]).map(p=>({t:p.ts, v:(p.low!=null?p.low:p.med)})).filter(p=>p.v!=null);
  if(pts.length<2){ box.innerHTML=`<h3>Histórico de preço</h3><div class="meta">histórico insuficiente</div>`; return; }
  const W=320,H=46,pad=3;
  const xs=pts.map(p=>p.t), vs=pts.map(p=>p.v);
  const minT=Math.min(...xs),maxT=Math.max(...xs),minV=Math.min(...vs),maxV=Math.max(...vs);
  const sx=t=> pad+(maxT===minT?0:(t-minT)/(maxT-minT))*(W-2*pad);
  const sy=v=> pad+(maxV===minV?0.5:(1-(v-minV)/(maxV-minV)))*(H-2*pad);
  const dpath=pts.map((p,i)=>(i?"L":"M")+sx(p.t).toFixed(1)+" "+sy(p.v).toFixed(1)).join(" ");
  const last=vs[vs.length-1];
  box.innerHTML=`<h3>Histórico de preço (${sym().trim()})</h3>
    <div class="sparkwrap"><svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <path d="${dpath}" fill="none" stroke="#7ab8ff" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>
      <div class="meta" style="display:flex;justify-content:space-between;margin-top:5px">
        <span>mín ${sym()}${minV.toFixed(2)}</span><span>máx ${sym()}${maxV.toFixed(2)}</span><span>último ${sym()}${last.toFixed(2)}</span>
      </div></div>`;
}

// clicar na linha abre o drawer (botões/links têm ação própria)
tbody.addEventListener("click", e=>{
  if(e.target.closest("button, a")) return;
  const d=rowItem(e.target.closest("tr"));
  if(d) openDetail(d);
});

// ---- modo servidor: detecção + ações (protegidas por token) ----
async function api(path, opts={}){
  opts.headers = Object.assign({"X-TBH-Token": TOKEN}, opts.headers||{});
  const r = await fetch(path, opts);
  if(!r.ok) throw new Error("HTTP "+r.status);
  return r.json();
}
async function fetchPrice(name, btn){
  if(!serverOn) return false;
  if(jobBusy){ toast("Aguarde o trabalho atual terminar.", "info"); return false; }
  jobBusy = true;
  ["refresh","refreshVisible","calib"].forEach(id=>$(id).disabled=true);
  if(btn){ btn.disabled=true; btn.textContent="…"; }
  let okv=false;
  try{
    const r = await api(`/api/price?currency=${cur}&name=${encodeURIComponent(name)}`);
    const row = DATA.find(d=>d.name===name);
    if(row){ row.real = row.real||{}; row.real[cur] = r; }
    okv=true;
  }catch(e){ toast("Falha ao buscar preço de "+name+": "+e.message, "error"); }
  unlockJobs();   // jobBusy=false + re-render (reabilita botões e ↻)
  return okv;
}
// verifica AO VIVO se o item tem oferta comprável (detecta erro/indisponível na loja)
async function checkListing(name, btn){
  if(!serverOn) return;
  if(jobBusy){ toast("Aguarde o trabalho atual terminar.", "info"); return; }
  jobBusy = true;
  ["refresh","refreshVisible","calib"].forEach(id=>$(id).disabled=true);
  if(btn){ btn.disabled=true; btn.textContent="…"; }
  try{
    const r = await api(`/api/listings?currency=${cur}&name=${encodeURIComponent(name)}`);
    const row = DATA.find(d=>d.name===name);
    if(row){ row.buy = r; }
    toast(r.buyable
      ? `${name}: comprável — menor ${sym()}${(r.low??0).toFixed(2)} (ao vivo).`
      : `${name}: sem oferta comprável agora (loja com erro/indisponível).`,
      r.buyable ? "ok" : "info");
  }catch(e){ toast("Falha ao verificar "+name+": "+e.message, "error"); }
  unlockJobs();
}
async function refreshAll(){
  if(!serverOn){
    toast("A atualização só funciona no modo servidor.\n\n1) No terminal (WSL), na pasta do projeto, rode:\n   python3 build.py serve\n2) Abra http://127.0.0.1:8765 (ou use o atalho iniciar-servidor.bat).", "info");
    return;
  }
  if(jobBusy){ toast("Aguarde o trabalho atual terminar.", "info"); return; }
  const btn=$("refresh");
  const done = (msg, type)=>{ unlockJobs(); btn.textContent="🔄 Atualizar mercado"; if(msg) toast(msg, type); };
  lockJobs("refresh"); btn.disabled=true; btn.textContent="⏳ Atualizando…";
  try{
    await api("/api/refresh", {method:"POST"});
    const poll = setInterval(async ()=>{
      try{
        const s = await api("/api/refresh-status");
        $("status").innerHTML = `<span class="dot ok"></span>${esc(s.message)}`;
        const m = (s.message.match(/\d+\/\d+/) || [])[0];
        btn.textContent = "⏳ " + (m || "atualizando…");
        if(!s.running){ clearInterval(poll);
          const d = await api("/api/data"); DATA = d.rows;
          $("status").innerHTML = `<span class="dot ok"></span>servidor conectado · ${esc(s.message)}`;
          done(s.message, "ok"); }
      }catch(e){ clearInterval(poll); done("Perdi contato com a atualização: "+e.message, "error"); }
    }, 2000);
  }catch(e){ done("Falha: "+e.message, "error"); }
}
$("refresh")?.addEventListener("click", refreshAll);

// rótulo/estado do botão conforme quantos itens visíveis precisam de (re)busca (5.4)
function updateEnrichBtn(rows){
  const b = $("refreshVisible");
  if(!serverOn || jobBusy || b.dataset.busy==="1") return;
  const pend = rows.filter(needsUpdate).length;
  b.disabled = pend===0;
  b.textContent = pend ? `🎯 Preço real (${pend})` : "🎯 Tudo atualizado";
  b.title = pend
    ? `busca o preço real de ${pend} item(ns) sem preço ou com mais de 6h · Shift+clique re-busca TODOS os visíveis`
    : "todos os itens visíveis têm preço recente · Shift+clique força re-busca de todos";
}

// busca o preço real dos itens visíveis em LOTE no servidor (ritmo seguro + retry) (5.4)
let enrichPoll = null;
// mescla só o preço real (campo `real`) vindo do servidor nas linhas locais — preserva campos
// só-do-cliente (ex.: `buy` da verificação ao vivo) e evita trocar o array DATA inteiro
function mergeRealFrom(rows){
  if(!rows) return 0;
  const byName = new Map(rows.map(r=>[r.name, r]));
  let changed=0;
  DATA.forEach(d=>{ const r=byName.get(d.name); if(r && r.real){ d.real=r.real; changed++; } });
  return changed;
}
async function refreshVisible(ev){
  if(!serverOn){ toast("Disponível só no modo servidor (python3 build.py serve).", "info"); return; }
  if(jobBusy){ toast("Aguarde o trabalho atual terminar.", "info"); return; }
  const force = !!(ev && ev.shiftKey);
  const rows = currentRows();
  const targets = (force ? rows : rows.filter(needsUpdate)).map(d=>d.name);
  if(!targets.length){
    toast(force ? "Nenhum item visível." : "Todos os itens visíveis já têm preço recente.", "ok"); return; }
  const mins = Math.ceil(targets.length*5.4/60);
  if(targets.length>15 && !confirm(
      `Buscar preço real de ${targets.length} item(ns)${force?" (forçando todos)":""}?\n`+
      `Leva ~${mins} min — 1 consulta a cada ~5s para não estourar o limite da Steam.\n`+
      `Pode continuar usando a página enquanto roda.`)) return;
  const btn=$("refreshVisible");
  const finish = (msg,type)=>{ btn.dataset.busy=""; unlockJobs(); if(msg) toast(msg, type); };
  lockJobs("refreshVisible");
  btn.dataset.busy="1"; btn.disabled=true; btn.textContent="🎯 iniciando…";
  try{
    await api("/api/enrich-batch", { method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ currency:cur, names:targets }) });
  }catch(e){ finish("Falha ao iniciar o lote: "+e.message, "error"); return; }
  if(enrichPoll) clearInterval(enrichPoll);
  let lastDone = -1;
  enrichPoll = setInterval(async ()=>{
    try{
      const s = await api("/api/enrich-status");
      btn.textContent = `🎯 ${s.done}/${s.total}…`;
      $("status").innerHTML = `<span class="dot ok"></span>${esc(s.message)}`;
      // progrediu? traz os preços já obtidos e re-renderiza (atualização incremental)
      if(s.done !== lastDone){
        lastDone = s.done;
        try{ const d = await api("/api/data"); if(mergeRealFrom(d && d.rows)) render(); }catch(_){}
      }
      if(!s.running){
        clearInterval(enrichPoll); enrichPoll=null;
        try{ const d = await api("/api/data"); mergeRealFrom(d && d.rows); }catch(_){}
        render();
        $("status").innerHTML = `<span class="dot ok"></span>servidor conectado · atualização habilitada`;
        finish(`Lote concluído: ${s.message}.`, s.fail ? "info" : "ok");
      }
    }catch(e){
      clearInterval(enrichPoll); enrichPoll=null;
      finish("Perdi contato com o lote: "+e.message, "error");
    }
  }, 1500);
}
$("refreshVisible")?.addEventListener("click", refreshVisible);

// ---- Atualização automática priorizada (5.x) ------------------------------------------
// Loop em segundo plano que mantém os preços frescos sem você clicar nada, priorizando o
// que importa e com TTL ESCALONADO por liquidez (favoritos atualizam mais; itens mortos,
// raramente). Cada passo dispara um lote PEQUENO no /api/enrich-batch — assim reaproveita
// toda a robustez do servidor (carimbo de "sem dados", dedupe por TTL, histórico, ritmo
// adaptativo anti-429). Só roda quando nenhum job manual está em andamento (cede a vez).
const TTL_FAV=2*3600, TTL_LIQ=6*3600, TTL_LOWLIQ=12*3600, TTL_DEAD=24*3600;
const AUTO_BATCH=6;              // itens por passo — pequeno p/ liberar os botões rápido entre passos
let autoOn=!!P.auto, autoPoll=null, autoTick=null;

// há quanto tempo o item foi consultado (s); Infinity se nunca (entra na frente da fila)
function realAge(d){ const fr=freshestReal(d); return fr&&fr.fetchedAt ? (Date.now()/1000-fr.fetchedAt) : Infinity; }
// "morto": consultado e sem giro 24h (nodata ou vol 0/nulo)
function isDead(d){ const fr=freshestReal(d); return !!fr && (fr.nodata || !(fr.vol>0)); }
function itemTTL(d){
  if(favs.has(d.name)) return TTL_FAV;          // favorito: sempre o mais fresco
  if(isDead(d)) return TTL_DEAD;                 // sem giro há tempos: rechecagem espaçada (revival)
  if(d.vol!=null && d.vol<5) return TTL_LOWLIQ;  // baixa liquidez: meio-termo
  return TTL_LIQ;                                 // líquido
}
function dueForAuto(d){ return realAge(d) > itemTTL(d); }
// prioridade: favoritos(0) > nunca consultados(1) > líquidos stale(2) > mortos(3); dentro do tier, mais antigo primeiro
function autoTier(d){ if(favs.has(d.name)) return 0; const fr=freshestReal(d); if(!fr) return 1; return isDead(d)?3:2; }
function autoCandidates(){
  return DATA.map(derive).filter(dueForAuto)
    .sort((a,b)=>{ const t=autoTier(a)-autoTier(b); return t!==0 ? t : realAge(b)-realAge(a); })
    .slice(0, AUTO_BATCH).map(d=>d.name);
}
function setAuto(on){
  autoOn=on; savePrefs();
  const b=$("autoBtn");
  b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
  b.textContent = on ? "🔁 Auto ✓" : "🔁 Auto";
  if(on){ autoStep(); } // tenta já no primeiro clique (se ocioso)
}
async function autoStep(){
  if(!autoOn || !serverOn || jobBusy) return;     // cede a vez aos jobs manuais
  const names=autoCandidates();
  if(!names.length) return;                        // tudo dentro do TTL: nada a fazer agora
  lockJobs("auto");
  try{
    await api("/api/enrich-batch", { method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ currency:cur, names }) });
  }catch(e){ unlockJobs(); return; }
  if(autoPoll) clearInterval(autoPoll);
  let lastDone=-1;
  autoPoll=setInterval(async ()=>{
    try{
      const s=await api("/api/enrich-status");
      $("status").innerHTML = `<span class="dot ok"></span>🔁 auto · ${esc(s.message)}`;
      if(s.done !== lastDone){       // preços já obtidos aparecem durante o passo
        lastDone = s.done;
        try{ const d=await api("/api/data"); if(mergeRealFrom(d && d.rows)) render(); }catch(_){}
      }
      if(!s.running){
        clearInterval(autoPoll); autoPoll=null;
        try{ const d=await api("/api/data"); if(mergeRealFrom(d && d.rows)) render(); }catch(_){}
        $("status").innerHTML = `<span class="dot ok"></span>servidor conectado · 🔁 auto ${autoOn?'ligado':'desligado'}`;
        unlockJobs();
      }
    }catch(e){ clearInterval(autoPoll); autoPoll=null; unlockJobs(); }
  }, 1500);
}
$("autoBtn")?.addEventListener("click", ()=>{ if(!serverOn){ toast("Disponível só no modo servidor.", "info"); return; } setAuto(!autoOn); });
// relógio do loop: confere periodicamente; o ritmo real é ditado pelo throttle do servidor
autoTick=setInterval(()=>{ if(autoOn && serverOn && !jobBusy && !autoPoll) autoStep(); }, 8000);

async function calibrate(){
  if(!serverOn) return;
  if(jobBusy){ toast("Aguarde o trabalho atual terminar.", "info"); return; }
  const btn=$("calib"), old=btn.textContent;
  lockJobs("calib"); btn.disabled=true; btn.textContent="📐 amostrando…";
  try{
    const r = await api("/api/calibrate?currency=brl&n=8");
    rate = r.rate; $("rate").value = r.rate.toFixed(3);
    if(cur!=="brl"){ $("cur").querySelector('[data-c="brl"]').click(); }
    toast(`Taxa calibrada: R$ ${r.rate.toFixed(3)} por US$ 1 (de ${r.samples} itens reais da Steam, por mediana).`, "ok");
  }catch(e){ toast("Falha ao calibrar: "+e.message, "error"); }
  btn.textContent=old; unlockJobs();
}
$("calib")?.addEventListener("click", calibrate);

// rótulo de "última atualização" (build público): data local do visitante + "há Xh"
function showLastUpdate(){
  const a = ago(GEN_EPOCH);
  const when = new Date(GEN_EPOCH*1000).toLocaleString("pt-BR");
  const rel = a ? (a==="agora" ? "agora mesmo" : "há "+a) : "";
  $("status").innerHTML = `<span class="dot ok"></span>somente leitura · preços atualizados ${rel} <span class="muted">(${when})</span>`;
}
(async function detectServer(){
  if(PUBLIC){            // Pages: sem servidor, sem atualização pela web
    serverOn = false;
    showLastUpdate();
    render();
    return;
  }
  try{
    await api("/api/ping");
    serverOn = true;
    $("status").innerHTML = `<span class="dot ok"></span>servidor conectado · atualização habilitada`;
    $("calib").disabled = false;
    $("refreshVisible").disabled = false;
    $("autoBtn").disabled = false;
    const d = await api("/api/data"); if(d && d.rows){ DATA = d.rows; }
    if(autoOn) setAuto(true);   // retoma o auto se estava ligado na visita anterior
  }catch(e){
    $("status").innerHTML = `<span class="dot off"></span>modo estático — rode <code>python3 build.py serve</code> para atualizar preços`;
  }
  render();
})();
</script>
</body></html>
"""


# URL pública do site (usada em og:url / og:image). Atualizar se migrar para domínio próprio.
SITE_URL = "https://filipefont.github.io/tbh-market-tool"


# Controles que só fazem sentido com o servidor local rodando (omitidos no build público
# do Pages: lá a atualização de preços é feita só pela GitHub Action).
SERVER_CONTROLS_HTML = """<div class="group">
    <button id="calib" disabled aria-label="calibrar taxa"
        title="estima a taxa USD→BRL a partir de uma amostra real da Steam">📐 Calibrar taxa</button>
    <button id="refreshVisible" disabled aria-label="buscar preço real dos itens visíveis"
        title="busca o preço real de cada item visível (modo servidor)">🎯 Preço real dos visíveis</button>
    <button id="autoBtn" class="toggle" disabled aria-pressed="false" aria-label="atualização automática priorizada"
        title="atualiza preços em segundo plano, priorizando favoritos · TTL escalonado por liquidez · respeita o limite da Steam">🔁 Auto</button>
    <button id="refresh" aria-label="atualizar mercado"
        title="rebaixa todos os preços do mercado (modo servidor)">🔄 Atualizar mercado</button>
  </div>"""


def render_html(rows, brl_rate, token="", public=False):
    data = json.dumps(rows, ensure_ascii=False)
    # anti-XSS: impede quebra do </script> e injeção via conteúdo do JSON
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    out = HTML_TEMPLATE
    for k, v in {
        # URL pública (og:image/og:url) — trocar aqui se migrar para domínio próprio
        "__SITE__": SITE_URL,
        # build público (Pages): sem controles de atualização; o servidor local mantém os botões
        "__SERVER_CONTROLS__": "" if public else SERVER_CONTROLS_HTML,
        "__PUBLIC__": "true" if public else "false",
        "__GENERATED__": time.strftime("%Y-%m-%d %H:%M"),
        "__GEN_EPOCH__": str(int(time.time())),
        "__N__": str(len(rows)),
        "__RATE__": f"{brl_rate:.2f}",
        "__TOKEN__": token,
        "__DATA__": data,
    }.items():
        out = out.replace(k, v)
    return out


# --- Comandos CLI ------------------------------------------------------------------------
def median(values):
    s = sorted(values)
    n = len(s)
    return 0 if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def calibrate_rate(rows, n, curkey="brl"):
    """Estima a taxa USD->moeda real amostrando N itens de MAIOR liquidez (preços confiáveis).
    Retorna (taxa, qtd_amostras). Amostra por listagens porque itens com muita venda têm
    preço estável; os mais baratos sofrem com arredondamento (R$ 0,01) e distorcem a razão."""
    sample = sorted((r for r in rows if r["usd"] > 0), key=lambda r: r["listings"], reverse=True)
    ratios = []
    for r in sample[:n]:
        po = price_overview(r["name"], curkey)
        ref = po and (po["low"] or po["med"])
        if ref:
            ratios.append(ref / r["usd"])
    if not ratios:
        return None, 0
    return round(median(ratios), 3), len(ratios)


def cmd_price(names, curkey):
    items = get_items(False)
    by_key = {join_key(it): it for it in items}
    enriched = load_enriched()
    updates = {}
    for i, name in enumerate(names):
        po = price_overview(name, curkey)
        gold = (by_key.get(name) or {}).get("gold")
        sym = "$" if curkey == "usd" else "R$"
        print(f"\n• {name}")
        if not po:
            print("    sem dados (nome exato? item listado no mercado?)")
            continue
        print(f"    menor venda : {po['lowText']}")
        print(f"    mediana     : {po['medText']}")
        print(f"    volume 24h  : {po['vol']}")
        ref = po["med"] or po["low"]
        if gold and ref:
            print(f"    gold        : {gold:,}")
            print(f"    gold / {sym}  : {gold / ref:,.0f}")
        enriched.setdefault(name, {})[curkey] = po
        updates.setdefault(name, {})[curkey] = po
    save_enriched(enriched)
    record_history(_history_rows_from_updates(updates, "priceoverview"))


def enrich(rows, top_n, curkey):
    enriched = load_enriched()
    updates = {}
    print(f"\n[enrich] priceoverview ({curkey.upper()}) das {top_n} melhores por gold/$...")
    for r in rows[:top_n]:
        po = price_overview(r["name"], curkey)
        if not po:
            print(f"  - {r['name']}: sem dados")
            continue
        enriched.setdefault(r["name"], {})[curkey] = po
        updates.setdefault(r["name"], {})[curkey] = po
        r["real"] = enriched[r["name"]]
        ref = po["med"] or po["low"]
        print(f"  + {r['name']}: {ref} vol {po['vol']} gold/moeda {round(r['gold']/ref) if ref else '—'}")
    save_enriched(enriched)
    record_history(_history_rows_from_updates(updates, "priceoverview"))


def _spread_pct(bk):
    sm, bm = bk.get("sellMin"), bk.get("buyMax")
    return round((sm - bm) / sm * 100) if sm and bm is not None else None


ORDERBOOK_FLUSH_EVERY = 20  # salva o cache a cada N coletas (resiliência a timeout/crash do CI)


def enrich_orderbook(rows, top_n):
    """Coleta as encomendas (buy orders) dos itens mais líquidos (onde há demanda real).
    `top_n <= 0` coleta TODOS os candidatos. A seleção é por nº de listagens — proxy de mercado
    ativo. Respeita o TTL p/ não remartelar e o throttle global da Steam.

    Salva o cache incrementalmente (a cada ORDERBOOK_FLUSH_EVERY itens): uma coleta longa pode
    estourar o timeout do passo do CI; sem flush, todo o progresso se perderia."""
    book = load_orderbook()
    candidates = sorted((r for r in rows if r.get("listings")),
                        key=lambda r: r["listings"], reverse=True)
    limit = len(candidates) if top_n <= 0 else top_n
    pending, done = {}, 0  # `pending`: coletas ainda não persistidas (zera a cada flush)
    alvo = "todos" if top_n <= 0 else str(limit)
    print(f"\n[orderbook] encomendas dos {alvo} itens mais líquidos...")

    def flush():
        if pending:
            save_orderbook(book)
            record_order_history(pending)
            pending.clear()

    for r in candidates:
        if done >= limit:
            break
        if is_book_fresh(r["name"], ORDERBOOK_TTL, book):
            continue
        status, curkey, bk = _order_book(r["name"])
        if status != "ok":
            print(f"  - {r['name']}: {status}")
            continue
        book.setdefault(r["name"], {})[curkey] = bk
        pending.setdefault(r["name"], {})[curkey] = bk
        r["book"] = book[r["name"]]
        sp = _spread_pct(bk)
        bm = f"{bk['buyMax']:.2f}" if bk['buyMax'] is not None else "—"
        print(f"  + {r['name']}: maior enc {bm} {curkey.upper()} · "
              f"{bk['buyOrders']} enc · spread {sp if sp is not None else '—'}%")
        done += 1
        if done % ORDERBOOK_FLUSH_EVERY == 0:
            flush()
    flush()
    print(f"[orderbook] {done} itens com encomenda coletados")


def cmd_book(names):
    """Consulta ad-hoc das encomendas de 1+ itens (e persiste no cache + histórico)."""
    updates = {}
    for name in names:
        status, curkey, bk = _order_book(name)
        print(f"\n• {name}")
        if status != "ok":
            print(f"    sem order book ({status})")
            continue
        sym = "$" if curkey == "usd" else "R$"
        sp = _spread_pct(bk)
        bm, sm = bk["buyMax"], bk["sellMin"]
        print(f"    moeda           : {curkey.upper()}")
        if bm is not None:
            print(f"    maior encomenda : {sym} {bm:.2f}  (líquido ~{sym} {bm * 0.85:.2f})")
        else:
            print(f"    maior encomenda : — (sem encomenda)")
        print(f"    encomendas      : {bk['buyOrders']}")
        if sm is not None:
            print(f"    menor venda     : {sym} {sm:.2f}  ({bk['sellOrders']} ofertas)")
        else:
            print(f"    menor venda     : — (sem venda)")
        print(f"    spread          : {sp if sp is not None else '—'}%")
        print(f"    valor do book   : {sym} {bk['buyNotional']:.2f}")
        updates.setdefault(name, {})[curkey] = bk
    if updates:
        merge_orderbook(updates)


def write_static(rows, brl_rate, public=False):
    rows.sort(key=lambda r: r["gold"] / r["usd"] if r["usd"] else 0, reverse=True)
    out = os.path.join(HERE, "index.html")
    open(out, "w", encoding="utf-8").write(render_html(rows, brl_rate, public=public))
    print(f"[ok] gerado: {out}" + (" (público/somente leitura)" if public else ""))


# --- Servidor local seguro ---------------------------------------------------------------
def run_server(brl_rate, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = secrets.token_urlsafe(24)
    state = {
        "refresh": {"running": False, "message": "ocioso"},
        "enrich": {"running": False, "message": "ocioso",
                   "done": 0, "total": 0, "ok": 0, "fail": 0},
    }

    def load_rows():
        items = get_items(False)
        steam = get_steam(False)
        rows, _ = build_rows(items, steam, load_enriched())
        rows.sort(key=lambda r: r["gold"] / r["usd"] if r["usd"] else 0, reverse=True)
        return rows

    rows_cache = {"rows": load_rows()}
    valid_names = {r["name"] for r in rows_cache["rows"]}  # whitelist anti-SSRF

    def do_refresh():
        try:
            state["refresh"] = {"running": True, "message": "baixando mercado..."}
            items = get_items(True)
            steam = get_steam(True, log=lambda m: state["refresh"].update(message=m))
            rows, _ = build_rows(items, steam, load_enriched())
            rows.sort(key=lambda r: r["gold"] / r["usd"] if r["usd"] else 0, reverse=True)
            rows_cache["rows"] = rows
            valid_names.clear()
            valid_names.update(r["name"] for r in rows)
            record_bulk_history(rows)   # snapshot completo do catálogo (USD) na série histórica
            state["refresh"] = {"running": False, "message": f"{len(rows)} itens atualizados"}
        except Exception as e:  # noqa: BLE001
            state["refresh"] = {"running": False, "message": f"erro: {e}"}

    def _apply_real(name, bycur):
        """Mescla os preços recém-obtidos no rows_cache SEM apagar a outra moeda já presente."""
        for r in rows_cache["rows"]:
            if r["name"] == name:
                r["real"] = {**(r.get("real") or {}), **bycur}

    def do_enrich(names, curkey):
        """Busca o preço real de vários itens, em background, com ritmo seguro.
        Itens limitados pela Steam (status 'error') vão para uma 2ª passada; os 'nodata'
        recebem um carimbo de tempo p/ não serem remartelados antes do TTL de frescor.
        Os resultados são gravados via merge_enriched (atômico + lock) p/ não brigar com
        consultas avulsas que estejam acontecendo em paralelo."""
        # dedupe por TTL: pula o que já foi buscado há < PRICE_TTL (não rebate na Steam à toa —
        # é justamente o re-martelo de itens recém-buscados que dispara o 429).
        snap = load_enriched()
        skipped = sum(1 for n in names if is_price_fresh(n, curkey, PRICE_TTL, snap))
        names = [n for n in names if not is_price_fresh(n, curkey, PRICE_TTL, snap)]
        total = len(names)
        ok = fail = 0
        retry = []
        updates = {}   # {name: {curkey: po}} acumulado p/ um único merge no fim
        state["enrich"] = {"running": True, "message": "iniciando...",
                           "done": 0, "total": total, "ok": 0, "fail": 0}

        def handle(name):
            nonlocal ok, fail
            status, po = _price_overview(name, curkey)
            if status == "ok":
                bycur = {curkey: po}
            elif status == "nodata":
                # marca como "sem dados" com carimbo: evita repetir antes do frescor expirar
                bycur = {curkey: {
                    "low": None, "lowText": None, "med": None, "medText": None,
                    "vol": None, "fetchedAt": int(time.time()), "nodata": True,
                }}
            else:
                return False  # 'error' -> tenta de novo depois
            updates.setdefault(name, {}).update(bycur)
            _apply_real(name, bycur)
            ok += (status == "ok")
            fail += (status == "nodata")
            return True

        try:
            for i, name in enumerate(names, 1):
                if not handle(name):
                    retry.append(name)
                state["enrich"].update(done=i, ok=ok, fail=fail,
                    message=f"{i}/{total} · ok {ok} · sem dados {fail}"
                            + (f" · repetir {len(retry)}" if retry else ""))
            for j, name in enumerate(retry, 1):
                if not handle(name):
                    fail += 1  # falhou de novo: deixa pendente p/ próxima rodada
                state["enrich"].update(ok=ok, fail=fail,
                    message=f"repetindo limitados {j}/{len(retry)} · ok {ok}")
            merge_enriched(updates)
            state["enrich"] = {"running": False, "done": total, "total": total,
                               "ok": ok, "fail": fail,
                               "message": f"{ok} com preço · {fail} sem dados"
                                          + (f" · {skipped} já recentes" if skipped else "")}
        except Exception as e:  # noqa: BLE001
            merge_enriched(updates)
            state["enrich"] = {"running": False, "done": total, "total": total,
                               "ok": ok, "fail": fail, "message": f"erro: {e}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silencia log padrão
            pass

        def _safe(self):
            # anti DNS-rebinding: só aceita Host localhost; anti-CSRF p/ ações: exige token
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "host inválido"}); return False
            return True

        def _auth(self):
            if self.headers.get("X-TBH-Token") != token:
                self._send(403, {"error": "token inválido"}); return False
            return True

        def _busy(self):
            # um único trabalho na Steam por vez (evita corrida e excesso de chamadas)
            if state["refresh"]["running"]:
                return "atualização do mercado em andamento"
            if state["enrich"]["running"]:
                return "lote de preços em andamento"
            return None

        def _send(self, code, obj, ctype="application/json"):
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._safe():
                return
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                self._send(200, render_html(rows_cache["rows"], brl_rate, token), "text/html")
            elif u.path == "/api/ping":
                if self._auth():
                    self._send(200, {"ok": True})
            elif u.path == "/api/data":
                if self._auth():
                    self._send(200, {"rows": rows_cache["rows"]})
            elif u.path == "/api/refresh-status":
                if self._auth():
                    self._send(200, state["refresh"])
            elif u.path == "/api/enrich-status":
                if self._auth():
                    self._send(200, state["enrich"])
            elif u.path == "/api/calibrate":
                if not self._auth():
                    return
                busy = self._busy()
                if busy:
                    self._send(409, {"error": busy}); return
                curkey = (q.get("currency") or ["brl"])[0]
                try:
                    n = min(max(int((q.get("n") or ["8"])[0]), 1), 20)
                except ValueError:
                    n = 8
                if curkey not in CURRENCIES:
                    self._send(400, {"error": "moeda inválida"}); return
                rate, used = calibrate_rate(rows_cache["rows"], n, curkey)
                if not rate:
                    self._send(502, {"error": "sem amostra da Steam"}); return
                self._send(200, {"rate": rate, "samples": used})
            elif u.path == "/api/price":
                if not self._auth():
                    return
                busy = self._busy()
                if busy:
                    self._send(409, {"error": busy}); return
                name = (q.get("name") or [""])[0]
                curkey = (q.get("currency") or ["brl"])[0]
                if curkey not in CURRENCIES or len(name) > 120 or name not in valid_names:
                    self._send(400, {"error": "parâmetros inválidos"}); return
                po = price_overview(name, curkey)
                if not po:
                    self._send(502, {"error": "sem dados da Steam"}); return
                merge_enriched({name: {curkey: po}})   # atômico + lock (sobrevive a rebuilds)
                _apply_real(name, {curkey: po})
                self._send(200, po)
            elif u.path == "/api/history":
                if not self._auth():
                    return
                name = (q.get("name") or [""])[0]
                curkey = (q.get("currency") or ["brl"])[0]
                # whitelist: moeda válida + nome existente na base cruzada (anti-SSRF/abuso)
                if curkey not in CURRENCIES or name not in valid_names:
                    self._send(400, {"error": "parâmetros inválidos"}); return
                try:
                    since = int((q.get("since") or ["0"])[0]) or None
                except ValueError:
                    since = None
                pts = history_series(name, curkey, since=since)   # query 100% parametrizada
                self._send(200, {"name": name, "currency": curkey, "points": pts})
            elif u.path == "/api/listings":
                if not self._auth():
                    return
                busy = self._busy()
                if busy:
                    self._send(409, {"error": busy}); return
                name = (q.get("name") or [""])[0]
                curkey = (q.get("currency") or ["brl"])[0]
                if curkey not in CURRENCIES or name not in valid_names:
                    self._send(400, {"error": "parâmetros inválidos"}); return
                lo = listings_overview(name, curkey)
                if lo is None:   # Steam não respondeu (transitório) -> deixa o cliente tentar de novo
                    self._send(502, {"error": "Steam não respondeu — tente de novo"}); return
                self._send(200, lo)
            else:
                self._send(404, {"error": "rota inexistente"})

        def do_POST(self):
            if not self._safe() or not self._auth():
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/refresh":
                busy = self._busy()
                if busy:
                    self._send(409, {"error": busy}); return
                threading.Thread(target=do_refresh, daemon=True).start()
                self._send(202, {"started": True})
            elif path == "/api/enrich-batch":
                busy = self._busy()
                if busy:
                    self._send(409, {"error": busy}); return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 200000:
                    self._send(400, {"error": "corpo inválido"}); return
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "json inválido"}); return
                curkey = body.get("currency", "brl")
                names = body.get("names")
                if curkey not in CURRENCIES or not isinstance(names, list) or not names:
                    self._send(400, {"error": "parâmetros inválidos"}); return
                if len(names) > 700:
                    self._send(400, {"error": "lote grande demais"}); return
                # whitelist anti-SSRF: só nomes da base cruzada, sem duplicar
                seen, clean = set(), []
                for n in names:
                    if isinstance(n, str) and n in valid_names and n not in seen:
                        seen.add(n); clean.append(n)
                if not clean:
                    self._send(400, {"error": "nenhum nome válido"}); return
                threading.Thread(target=do_enrich, args=(clean, curkey), daemon=True).start()
                self._send(202, {"started": True, "total": len(clean)})
            else:
                self._send(404, {"error": "rota inexistente"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)  # bind só local
    url = f"http://127.0.0.1:{port}"
    print("=" * 60)
    print(f"  SERVIDOR NO AR:  {url}")
    print("  ABRA ESSA URL NO NAVEGADOR (não o arquivo index.html).")
    print("  Mantenha esta janela aberta. Ctrl+C para parar.")
    print("=" * 60)
    _open_browser(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] encerrado")


def _open_browser(url):
    """Abre o navegador. No WSL tenta o navegador do Windows; senão usa o padrão."""
    import shutil
    import subprocess

    def task():
        time.sleep(1.0)
        for opener in ("wslview", "explorer.exe"):
            if shutil.which(opener):
                try:
                    subprocess.Popen([opener, url],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:  # noqa: BLE001
                    pass
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=task, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="TBH Market Tool")
    ap.add_argument("--refresh", action="store_true", help="rebaixa o bulk das APIs")
    ap.add_argument("--public", action="store_true",
                    help="build público (Pages): somente leitura, sem controles de atualização")
    ap.add_argument("--brl-rate", type=float, default=5.40, help="taxa USD->BRL p/ estimativa")
    ap.add_argument("--enrich-top", type=int, default=0, metavar="N",
                    help="preço real (priceoverview) das N melhores por gold/$")
    ap.add_argument("--calibrate", type=int, default=0, metavar="N",
                    help="estima a taxa USD->BRL a partir de N itens reais (mais líquidos)")
    ap.add_argument("--currency", choices=list(CURRENCIES), default="brl",
                    help="moeda das consultas precisas (price/enrich)")
    ap.add_argument("--orderbook-top", type=int, default=None, metavar="N",
                    help="coleta as encomendas (buy orders) dos N itens mais líquidos (0 = todos)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("price", help="consulta PRECISA de 1+ itens (sob demanda)")
    p.add_argument("names", nargs="+")
    b = sub.add_parser("book", help="consulta as encomendas (buy orders) de 1+ itens")
    b.add_argument("names", nargs="+")
    s = sub.add_parser("serve", help="servidor local interativo")
    s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    init_history()                 # garante a tabela antes de qualquer comando
    seed_history_from_enriched()   # backfill único (no-op se já populado)

    if args.cmd == "price":
        cmd_price(args.names, args.currency)
        return
    if args.cmd == "book":
        cmd_book(args.names)
        return
    if args.cmd == "serve":
        run_server(args.brl_rate, args.port)
        return

    items = get_items(args.refresh)
    steam = get_steam(args.refresh)
    rows, unmatched = build_rows(items, steam, load_enriched())
    rows.sort(key=lambda r: r["gold"] / r["usd"] if r["usd"] else 0, reverse=True)
    if args.refresh:
        record_bulk_history(rows)  # só registra quando o bulk é REbaixado (dado fresco)
    brl_rate = args.brl_rate
    if args.calibrate:
        print(f"[calibrate] amostrando {args.calibrate} itens mais líquidos...")
        r, used = calibrate_rate(rows, args.calibrate, "brl")
        if r:
            brl_rate = r
            print(f"[calibrate] taxa estimada: R$ {r:.3f}/US$ (de {used} amostras)")
        else:
            print("[calibrate] sem amostra; mantendo taxa padrão")
    if args.enrich_top:
        enrich(rows, args.enrich_top, args.currency)
    if args.orderbook_top is not None:
        enrich_orderbook(rows, args.orderbook_top)
    report_join_health(rows, unmatched, steam, args.public)  # saúde da junção + alerta (CI summary)
    write_static(rows, brl_rate, args.public)
    print("\nTop 10 por gold/$ (bulk):")
    for r in rows[:10]:
        print(f"  {r['gold'] / r['usd']:>12,.0f}/$  ${r['usd']:>6.2f}  {r['gold']:>16,}  {r['name']}")


if __name__ == "__main__":
    main()
