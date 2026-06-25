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
import hashlib
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
# Dados estendidos do jogo (mesma wiki/keyspace dos itens): efeitos das gemas, stages (farm) e
# tabelas de drop. Fundação p/ as abas de Efeitos/Farm/Craft — ver .spec/roadmap-viabilidade.md.
EFFECTS_URL = "https://www.taskbarherowiki.com/data/effects.json"
STAGES_URL = "https://www.taskbarherowiki.com/data/stages.json"
DROPS_URL = "https://www.taskbarherowiki.com/data/drops.json"
# Receitas de craft — host DISTINTO (`taskbarhero.wiki`, ≠ `.com` dos itens), porém o keyspace
# casa com items.json (validado 100%). `crafting` traz materiais nomeados + result.itemsByGrade
# (pool real de itens por grade) e result.gradeOdds (prob. por grade). Ver .spec/roadmap-viabilidade.md §3.3.
RECIPES_URL = "https://taskbarhero.wiki/data/recipes.json"
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
EFFECTS_CACHE = os.path.join(DATA, "effects.json")
STAGES_CACHE = os.path.join(DATA, "stages.json")
DROPS_CACHE = os.path.join(DATA, "drops.json")
RECIPES_CACHE = os.path.join(DATA, "recipes.json")  # receitas de craft (taskbarhero.wiki)
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
# Selo "NOVO": itens vistos no mercado pela 1ª vez A PARTIR da reabertura (firstSeen >= âncora).
# Persistido em data/first_seen.json (sobrevive via actions/cache). Expira após NEW_MAX_AGE p/ não
# ficar "novo" para sempre. Itens já existentes ganham firstSeen na 1ª coleta (pré-reabertura) — logo
# NÃO são marcados; só os que surgirem em/após 25/06.
FIRST_SEEN_CACHE = os.path.join(DATA, "first_seen.json")
NEW_MAX_AGE = 14 * 86400  # 14 dias


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


def _get_cached(url, cache, label, refresh):
    """Baixa+cacheia um JSON da wiki (mesma disciplina do get_items). Best-effort: se falhar e
    houver cache, usa o cache; se não houver, retorna lista vazia (a feature só não aparece)."""
    if not refresh and os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except (ValueError, OSError):
            pass
    try:
        data = fetch_json(url)
        json.dump(data, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[{label}] {len(data)} registros salvos")
        return data
    except (RuntimeError, OSError) as e:
        print(f"[{label}] falha ({e}); usando cache se houver")
        try:
            return json.load(open(cache, encoding="utf-8")) if os.path.exists(cache) else []
        except (ValueError, OSError):
            return []


def get_effects(refresh):
    return _get_cached(EFFECTS_URL, EFFECTS_CACHE, "effects", refresh)


def get_stages(refresh):
    return _get_cached(STAGES_URL, STAGES_CACHE, "stages", refresh)


def get_drops(refresh):
    return _get_cached(DROPS_URL, DROPS_CACHE, "drops", refresh)


def get_recipes(refresh):
    # host distinto (taskbarhero.wiki) pode recusar nosso UA no --refresh; o cache versionado cobre.
    return _get_cached(RECIPES_URL, RECIPES_CACHE, "recipes", refresh)


# Piso de cobertura das junções de dados estendidos (hoje 100%). Queda = a wiki mudou keyspace
# ou renomeou; não derruba o build, só ALERTA (igual à saúde da junção do mercado).
EXTRAS_MATCH_MIN = 0.95


def attach_game_extras(rows, items, refresh=False):
    """Anexa efeitos das gemas (row['effects']) e locais de drop/farm (row['droppedIn']) às linhas,
    cruzando por `key` do item. Best-effort: nunca quebra o build. Imprime cobertura e ALERTA se
    o casamento cair (itens novos/renomeados da wiki)."""
    effects = get_effects(refresh)
    stages = get_stages(refresh)
    # drops.json (~1MB, probabilidade exata do craft/drop) NÃO é buscado ainda — só quando a feature
    # de % de drop existir. get_drops() já está pronto p/ isso. Hoje o farm usa a `rate` dos stages.
    if not effects and not stages:
        return
    key2name = {it.get("key"): join_key(it) for it in items}

    # efeitos por nome de mercado (enxuto: só o que a UI precisa)
    eff_by_name, eff_total, eff_ok = {}, 0, 0
    for e in effects:
        eff_total += 1
        name = key2name.get(e.get("key"))
        if not name:
            continue
        eff_ok += 1
        eff_by_name[name] = [
            {"slot": g.get("slot"), "stat": g.get("stat"), "disp": g.get("disp"), "chance": g.get("chance")}
            for g in (e.get("groups") or [])
        ]

    # locais de drop por nome de mercado (a partir dos stages)
    farm_by_name = {}
    drop_keys, drop_ok = set(), set()
    for s in stages:
        for d in (s.get("drops") or []):
            ik = d.get("itemKey")
            drop_keys.add(ik)
            name = key2name.get(ik)
            if not name:
                continue
            drop_ok.add(ik)
            farm_by_name.setdefault(name, []).append({
                "stage": s.get("label"), "level": s.get("level"),
                "rate": d.get("rate"), "source": d.get("source")})

    for r in rows:
        if r["name"] in eff_by_name:
            r["effects"] = eff_by_name[r["name"]]
        if r["name"] in farm_by_name:
            r["droppedIn"] = farm_by_name[r["name"]]

    er = eff_ok / eff_total if eff_total else 1.0
    dr = len(drop_ok) / len(drop_keys) if drop_keys else 1.0
    print(f"[extras] efeitos {eff_ok}/{eff_total} ({er:.0%}) · "
          f"itens com drop {len(drop_ok)}/{len(drop_keys)} ({dr:.0%}) · "
          f"{sum(1 for r in rows if r.get('effects'))} gemas e "
          f"{sum(1 for r in rows if r.get('droppedIn'))} farmáveis nas linhas")
    if min(er, dr) < EXTRAS_MATCH_MIN:
        print(f"::warning::[extras] cobertura abaixo de {EXTRAS_MATCH_MIN:.0%} — wiki mudou keyspace/nomes?")


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


def merge_orderbook_partials(pattern):
    """Consolida os parciais dos shards (--orderbook-out) em orderbook.json + order_history.
    Job de merge do CI: roda DEPOIS dos runners paralelos, é o único que escreve no cache/histórico
    compartilhado (os shards só geram JSONs parciais), então não há corrida de escrita."""
    import glob
    files = sorted(glob.glob(pattern))
    combined = {}
    for fp in files:
        try:
            part = json.load(open(fp, encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"  ! parcial inválido ignorado {fp}: {e}")
            continue
        for name, bycur in (part or {}).items():
            combined.setdefault(name, {}).update(bycur)
        print(f"  + {fp}: {len(part or {})} itens")
    if combined:
        merge_orderbook(combined)   # atualiza orderbook.json (sob lock) + record_order_history
    print(f"[merge] {len(combined)} itens consolidados de {len(files)} parciais")


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


def mark_new_items(rows):
    """Registra o 1º avistamento de cada item NO MERCADO (data/first_seen.json) e marca como NOVO
    os que surgiram a partir da reabertura. Best-effort: nunca quebra o build."""
    try:
        seen = json.load(open(FIRST_SEEN_CACHE, encoding="utf-8")) if os.path.exists(FIRST_SEEN_CACHE) else {}
    except (ValueError, OSError):
        seen = {}
    now = int(time.time())
    changed = False
    for r in rows:
        if r.get("noBulk"):          # só conta quem está REALMENTE listado no mercado
            continue
        ts = seen.get(r["name"])
        if ts is None:
            seen[r["name"]] = ts = now
            changed = True
        if ts >= MARKET_REOPEN_TS and (now - ts) < NEW_MAX_AGE:
            r["isNew"] = True
    if changed:
        try:
            tmp = FIRST_SEEN_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(seen, f, ensure_ascii=False)
            os.replace(tmp, FIRST_SEEN_CACHE)
        except OSError:
            pass


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
<meta name="theme-color" content="#0e1014">
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
<!-- fontes do layout "Cubo" (redesign opt-in). Só usadas quando data-ui="cubo". -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<!-- A/B de layout: define data-ui ANTES do CSS pintar (evita flash). Atual = default; Cubo = opt-in.
     URL ?ui=cubo|atual tem prioridade e fica salvo; senão usa o localStorage. -->
<script>(function(){try{var u=new URLSearchParams(location.search).get("ui");
var v=u||localStorage.getItem("tbh_ui")||"atual";if(v!=="cubo")v="atual";
document.documentElement.setAttribute("data-ui",v);if(u)localStorage.setItem("tbh_ui",v);
}catch(e){document.documentElement.setAttribute("data-ui","atual");}})();</script>
<style>
  /* Identidade "terminal de mercado": base escura, acento ESMERALDA (ticker), ouro p/ destaque,
     números em mono tabular. Cores de grade dos itens (raridade) ficam à parte. */
  :root { color-scheme: dark; --row:#0e1014; --row-alt:#161922; --row-hover:#1c2029;
          --accent:#19c37d; --accent-ink:#08130d; --gold:#f4c430;
          --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, Segoe UI, sans-serif; margin:0; background:#0e1014; color:#e6e8ee; }
  /* números (preços, gold, Δ, contagens) em mono tabular — reforça o ar de "terminal" */
  .money, .ppr, td.g, .g.abbr, .lvl, .abbr, .trend, .gprice, #eCount, .mvchip b, .age {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  header { padding:14px 20px; background:#1b1e26; border-bottom:1px solid #2a2e3a; }
  h1 { margin:0 0 4px; font-size:18px; }
  .meta { font-size:12px; color:#9aa3b8; }
  .chip { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px;
          background:#22263180; border:1px solid #2a2e3a; color:#c2c9da; }
  a { color:#4fd1a5; text-decoration:none; } a:hover { text-decoration:underline; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:10px 20px;
              background:#171a21; position:sticky; top:0; z-index:5; border-bottom:1px solid #2a2e3a; }
  .group { display:flex; gap:8px; align-items:center; }
  .group + .group { border-left:1px solid #2a2e3a; padding-left:8px; }
  input, select, button { background:#0f1116; color:#e6e8ee; border:1px solid #2a2e3a;
                          border-radius:6px; padding:7px 10px; font-size:13px; }
  button { cursor:pointer; } button:hover:not(:disabled) { background:#222634; }
  button:disabled { opacity:.5; cursor:default; }
  input:focus-visible, select:focus-visible, button:focus-visible, th:focus-visible {
    outline:2px solid #2bd48f; outline-offset:1px; }
  .seg { display:flex; border:1px solid #2a2e3a; border-radius:6px; overflow:hidden; }
  .seg button { border:0; border-radius:0; padding:7px 14px; }
  .seg button.on { background:#19c37d; color:var(--accent-ink); }
  .wrap { overflow:auto; max-height:calc(100vh - 168px); }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { padding:8px 12px; text-align:right; white-space:nowrap; border-bottom:1px solid #21242e; }
  th:nth-child(-n+2), td:nth-child(-n+2) { text-align:left; }
  thead th { position:sticky; top:0; background:#1b1e26; cursor:pointer; user-select:none; z-index:2; }
  /* ao navegar por ↑/↓ (scrollIntoView), reserva a altura do thead sticky p/ a linha não
     ficar escondida atrás do cabeçalho ao voltar ao topo */
  tbody tr { scroll-margin-top: 44px; }
  thead th:hover { background:#242838; }
  th .arrow { color:#2bd48f; font-size:11px; }
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
  tbody tr.sel td:first-child { background:#21304d; box-shadow:inset 3px 0 0 #2bd48f; }
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
  a.steam:hover { color:var(--accent-ink); background:#19c37d; border-color:#19c37d; text-decoration:none; }
  /* botão de alternância (filtro de favoritos) */
  button.toggle.on { background:#19c37d; color:var(--accent-ink); border-color:#19c37d; }
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
  /* abas (Mercado / Efeitos) */
  .tabs { display:flex; gap:4px; padding:6px 20px 0; }
  .tab { background:transparent; border:1px solid transparent; border-bottom:none; color:#9aa3b8;
         padding:7px 14px; border-radius:8px 8px 0 0; cursor:pointer; font-size:13px; }
  .tab.on { color:#e8ebf2; background:#ffffff0d; border-color:#ffffff1f; font-weight:600;
            box-shadow:inset 0 -2px 0 var(--accent); }
  /* alternância de view: por padrão mostra o Mercado; .view-effects / .view-farm / .view-craft trocam de aba */
  #effectsView, #farmView, #craftView, #bagView { display:none; }
  body.view-effects #effectsView, body.view-farm #farmView, body.view-craft #craftView, body.view-bag #bagView { display:block; }
  body.view-effects #marketControls, body.view-effects #activeFilters, body.view-effects #movers, body.view-effects .wrap, body.view-effects #pager,
  body.view-farm #marketControls, body.view-farm #activeFilters, body.view-farm #movers, body.view-farm .wrap, body.view-farm #pager,
  body.view-bag #marketControls, body.view-bag #activeFilters, body.view-bag #movers, body.view-bag .wrap, body.view-bag #pager,
  body.view-craft #marketControls, body.view-craft #activeFilters, body.view-craft #movers, body.view-craft .wrap, body.view-craft #pager { display:none; }
  /* barra de paginação */
  #pager { display:flex; flex-wrap:wrap; align-items:center; gap:6px; padding:8px 20px; font-size:12px; color:#9aa3b8; }
  #pager:empty { display:none; }
  #pager button { padding:4px 9px; min-width:30px; } #pager button.cur { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); font-weight:600; }
  #pager .pgap { color:#5b6378; } #pager select { padding:4px 8px; }
  #pager .pinfo { margin-right:auto; }
  /* cards de stage (farm) */
  .farmprompt { grid-column:1/-1; text-align:center; padding:48px 20px; color:#cdd3e0; font-size:15px; }
  .farmprompt .meta { margin-top:6px; }
  #farmGrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:10px; padding:6px 20px 24px; }
  .stagecard { border:1px solid #ffffff14; background:#ffffff07; border-radius:10px; padding:10px 12px; }
  .stagecard .sh { display:flex; align-items:baseline; gap:8px; margin-bottom:6px; }
  .stagecard .slabel { font-weight:700; color:var(--accent); }
  .stagecard .sname { color:#cdd3e0; }
  .stagecard .sboss { margin-left:auto; font-size:11px; color:#9aa3b8; }
  .evchip { margin-left:auto; font-size:11px; font-weight:600; color:var(--accent-ink); background:var(--accent);
            border-radius:6px; padding:1px 7px; cursor:help; font-family:var(--font-mono); white-space:nowrap; }
  .evtops { font-size:11px; color:#8b93a7; margin-bottom:5px; }
  .evtop { color:var(--accent); cursor:pointer; } .evtop:hover { text-decoration:underline; }
  .stagedrop { display:flex; align-items:center; gap:6px; font-size:12px; padding:3px 0; border-top:1px solid #ffffff0a; }
  .stagedrop.trad { cursor:pointer; }
  .stagedrop.trad:hover { background:#ffffff0e; }
  .stagedrop .dn { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .stagedrop .dr { margin-left:auto; color:#9aa3b8; white-space:nowrap; }
  .stagedrop .dp { color:var(--accent); white-space:nowrap; font-family:var(--font-mono); }
  .stagecard .icon.sm { width:18px; height:18px; }
  /* grade de gemas */
  #effectsGrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px; padding:6px 20px 24px; }
  .gemcard { border:1px solid #ffffff14; background:#ffffff07; border-radius:10px; padding:10px 12px; cursor:pointer; }
  .gemcard:hover { background:#ffffff0e; }
  .gemcard .gh { display:flex; align-items:center; gap:8px; }
  .gemcard .gname { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .gemcard .gbadge { margin-left:auto; font-size:10px; font-weight:600; border:1px solid; border-radius:6px; padding:1px 6px; white-space:nowrap; }
  /* faixa de economia (preço + encomenda) em destaque, separada dos efeitos */
  .gemcard .gprices { display:flex; gap:14px; margin:7px 0; font-family:var(--font-mono); }
  .gemcard .gp { font-size:13px; color:#cdd3e0; } .gemcard .gp .lbl { font-size:10px; color:#8b93a7; font-family:system-ui; }
  .gemcard .gp b { color:#e8ebf2; }
  /* bloco "Efeitos" claramente apartado */
  .gemcard .gsec { border-top:1px solid #ffffff14; padding-top:5px; }
  .gemcard .glabel { display:block; font-size:10px; letter-spacing:.05em; text-transform:uppercase; color:#8b93a7; margin-bottom:2px; }
  .gemcard .geff { display:flex; justify-content:space-between; gap:8px; font-size:12px; padding:2px 0; }
  .gemcard .geff .gs { color:#9aa3b8; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .gemcard .geff .gv { color:var(--accent); white-space:nowrap; font-weight:600; }
  /* aba Craft (receitas) */
  .crafthint { padding:2px 20px 6px; margin:0; font-size:12px; color:#8b93a7; line-height:1.5; }
  .crafthint b { color:#cdd3e0; }
  .craftprompt { grid-column:1/-1; text-align:center; padding:48px 20px; color:#cdd3e0; font-size:15px; }
  #craftGrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:10px; padding:6px 20px 24px; }
  .craftcard { border:1px solid #ffffff14; background:#ffffff07; border-radius:10px; padding:11px 13px; }
  .craftcard .ch { display:flex; align-items:center; gap:8px; margin-bottom:7px; }
  .craftcard .ctype { font-weight:700; color:#e8ebf2; }
  .craftcard .ctier { font-size:11px; color:#9aa3b8; }
  .craftcard .clvl { font-size:11px; color:#8b93a7; }
  .vbadge { margin-left:auto; font-size:10px; font-weight:700; border:1px solid; border-radius:6px; padding:2px 7px; white-space:nowrap; cursor:help; }
  .vbadge.craft { color:#9ff0bf; border-color:#2e7d4f; background:#1c3a2a; }
  .vbadge.gamble { color:#f4d58a; border-color:#7d6a2e; background:#3a341c; }
  .vbadge.sell { color:#8fc9ff; border-color:#2e5a7d; background:#1c2c3a; }
  .vbadge.unknown { color:#9aa3b8; border-color:#ffffff1f; background:#ffffff0a; }
  .craftcard .cecon { display:flex; gap:14px; margin:6px 0; font-family:var(--font-mono); flex-wrap:wrap; }
  .craftcard .ce { font-size:13px; color:#cdd3e0; } .craftcard .ce .lbl { font-size:10px; color:#8b93a7; font-family:system-ui; display:block; }
  .craftcard .ce b { color:#e8ebf2; } .craftcard .ce.win b { color:#5fd38d; } .craftcard .ce.cost b { color:#e0b07a; }
  .craftcard .crange { font-size:12px; color:#9aa3b8; margin:3px 0 6px; font-family:var(--font-mono); }
  .craftcard .crange .pk { color:#5fd38d; } .craftcard .crange .lo { color:#c2c9da; }
  .craftcard .csec { border-top:1px solid #ffffff14; padding-top:6px; margin-top:4px; }
  .craftcard .clabel { display:block; font-size:10px; letter-spacing:.05em; text-transform:uppercase; color:#8b93a7; margin-bottom:3px; }
  .craftcard .cmats { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:6px; }
  .cmat { display:inline-flex; align-items:center; gap:4px; font-size:11px; border:1px solid #ffffff14; background:#ffffff0a; border-radius:6px; padding:2px 6px; }
  .cmat .icon.sm { width:16px; height:16px; } .cmat .cmp { color:var(--accent); font-family:var(--font-mono); } .cmat .cmp.na { color:#e07a7a; }
  .crow { display:flex; align-items:center; gap:7px; font-size:12px; padding:3px 0; border-top:1px solid #ffffff0a; }
  .crow .gbadge { font-size:9px; font-weight:700; border:1px solid; border-radius:5px; padding:1px 5px; white-space:nowrap; }
  .crow .cpct { color:#8b93a7; width:42px; text-align:right; font-family:var(--font-mono); }
  .crow .cbn { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; cursor:pointer; }
  .crow .cbn:hover { text-decoration:underline; }
  .crow .cbp { margin-left:auto; color:var(--accent); font-family:var(--font-mono); white-space:nowrap; }
  .crow .cbp.beat { color:#5fd38d; font-weight:600; }
  .crow.noprice .cpct, .crow.noprice .gbadge { opacity:.5; } .crow .cnp { margin-left:auto; color:#6b7283; font-size:11px; }
  /* faixa de "top movers" (maiores variações) — chips clicáveis */
  #movers { padding:0 20px; display:flex; flex-wrap:wrap; align-items:center; gap:6px; }
  #movers:empty { display:none; }
  .mvlbl { font-size:11px; color:#8b93a7; }
  .mvchip { font-size:11px; max-width:200px; display:inline-flex; align-items:center; gap:4px;
            border:1px solid #ffffff14; background:#ffffff0a; border-radius:999px; padding:2px 9px; cursor:pointer; }
  .mvchip:hover { background:#ffffff14; }
  .mvchip .mvn { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:120px; }
  .mvchip.up { color:#5fd38d; } .mvchip.down { color:#e07a7a; }
  /* colunas de preço "real" levemente destacadas vs estimado */
  td.real, th.real { background:#1a1f1a40; }
  .g { color:#f4c430; } .money { color:#5fd38d; } .ppr { color:#4fd1a5; font-weight:600; }
  .check { color:#5fd38d; font-size:11px; margin-left:4px; }
  .conv { color:#c2a24b; font-weight:600; cursor:help; }   /* preço convertido (≈) */
  .badge { font-size:11px; padding:1px 8px; border-radius:10px; background:#2a2e3a;
           border:1px solid transparent; }
  .low { color:#e07a7a; } .muted { color:#9aa3b8; }
  .warn { margin-left:5px; cursor:help; }
  /* selo de item intradável pela trava de grade da reabertura */
  .lock { font-size:10px; font-weight:600; color:#b9a0e0; border:1px solid #b9a0e055;
          background:#b9a0e01a; border-radius:6px; padding:1px 6px; cursor:help; white-space:nowrap; }
  /* selo "NOVO": item listado a partir da reabertura */
  .newb { font-size:9px; font-weight:700; letter-spacing:.04em; color:#1c1f26; background:#5fd38d;
          border-radius:5px; padding:0 5px; cursor:help; vertical-align:1px; }
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
    background:linear-gradient(90deg, #4fd1a533, #4fd1a50d); border-radius:0 3px 3px 0; z-index:0; }
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
  /* selo "AO VIVO": reforça que o dado é atualizado automaticamente (≠ snapshot congelado) */
  .live { display:inline-block; font-size:10px; font-weight:700; letter-spacing:.04em; color:#1c1f26;
          background:#5fd38d; border-radius:6px; padding:1px 6px; margin-right:6px; vertical-align:1px; }
  .live::before { content:"● "; animation:livepulse 1.6s ease-in-out infinite; }
  .live.stale { background:#e0a35f; }
  @keyframes livepulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  @media (prefers-reduced-motion: reduce){ .live::before{ animation:none } }
  .ok { background:#4caf50; } .off { background:#777; }
  /* toasts (substituem alert()) */
  #toasts { position:fixed; right:16px; bottom:16px; z-index:50; display:flex;
            flex-direction:column; gap:8px; max-width:min(92vw,380px); }
  .toast { background:#1b1e26; border:1px solid #2a2e3a; border-left:3px solid #2bd48f;
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
  .dattr .an { color:#cdd6ff; } .dattr .av { font-weight:600; color:#4fd1a5; }
  #detail .dactions { display:flex; flex-wrap:wrap; gap:8px; margin-top:2px; }
  #detail .dactions a, #detail .dactions button { font-size:12px; }
  .spark { width:100%; height:54px; display:block; }
  .sparkwrap { background:#0f1116; border:1px solid #21242e; border-radius:8px; padding:8px; }
  /* ===== Seletor A/B de layout (sempre visível, nos dois temas) ===== */
  .uiswitch { margin-left:10px; vertical-align:middle; }
  .uiswitch button { padding:4px 10px; font-size:11px; }
  .uiswitch .beta { font-size:9px; opacity:.75; margin-left:3px; }
  /* ===== Aba/seção "Minha Mochila" (stub — em desenvolvimento, nos dois temas) ===== */
  .badge-new { font-size:9px; font-weight:700; color:#08130d; background:#19c37d; border-radius:10px;
               padding:1px 6px; margin-left:5px; vertical-align:middle; }
  #bagView { padding:46px 20px; }
  .bagdev { max-width:580px; margin:0 auto; text-align:center; background:#161922;
            border:1px solid #2a2e3a; border-radius:16px; padding:36px 30px; }
  .bagdev .ico { font-size:44px; line-height:1; }
  .bagdev h2 { margin:16px 0 8px; font-size:20px; }
  .bagdev p { color:#9aa3b8; font-size:14px; line-height:1.55; margin:6px auto; max-width:460px; }
  .bagdev .devtag { display:inline-block; margin-top:6px; font-size:11px; font-weight:700;
                    color:#1c1404; background:#f4c430; border-radius:20px; padding:4px 12px; }
  .bagdev .connect { margin-top:20px; opacity:.55; cursor:not-allowed; }
  .bagdev .road { margin-top:20px; text-align:left; display:inline-block; color:#9aa3b8; font-size:13px; }
  .bagdev .road li { margin:4px 0; }

  /* ======================= LAYOUT "CUBO" — redesign opt-in (data-ui="cubo") =======================
     Fase 1: re-skin (paleta menta/âmbar + fontes Space Grotesk/Figtree/IBM Plex Mono) sobre a
     estrutura atual. Fase 2 (layout sidebar+hero+cartões) virá em cima destes tokens. */
  html[data-ui="cubo"] {
    --row:#11161d; --row-alt:#0e131a; --row-hover:#161d27;
    --accent:#2dd4a7; --accent-ink:#06140f; --gold:#f0a93b;
    --font-mono:'IBM Plex Mono', ui-monospace, Menlo, Consolas, monospace;
    --cb-bg:#0c0f14; --cb-surface:#11161d; --cb-sidebar:#0a0d12;
    --cb-border:#232a35; --cb-border-soft:#1b2029; --cb-text:#eef1f5;
    --cb-muted:#aeb6c2; --cb-faint:#7e8696;
  }
  html[data-ui="cubo"] body { background:var(--cb-bg); color:var(--cb-text);
    font-family:'Figtree', system-ui, sans-serif; }
  html[data-ui="cubo"] h1 { font-family:'Space Grotesk','Figtree',sans-serif; letter-spacing:-.3px; }
  html[data-ui="cubo"] header { background:var(--cb-sidebar); border-bottom-color:var(--cb-border-soft); }
  html[data-ui="cubo"] .meta { color:var(--cb-faint); }
  html[data-ui="cubo"] .chip { background:#11161d; border-color:var(--cb-border); color:var(--cb-muted); }
  html[data-ui="cubo"] a { color:var(--accent); }
  html[data-ui="cubo"] .controls { background:var(--cb-surface); border-bottom-color:var(--cb-border-soft); }
  html[data-ui="cubo"] .group + .group { border-left-color:var(--cb-border); }
  html[data-ui="cubo"] input, html[data-ui="cubo"] select, html[data-ui="cubo"] button {
    background:#0e131a; border-color:var(--cb-border); color:var(--cb-text); border-radius:9px; }
  html[data-ui="cubo"] button:hover:not(:disabled) { background:#1a212b; }
  html[data-ui="cubo"] .seg { border-color:var(--cb-border); border-radius:9px; }
  html[data-ui="cubo"] .seg button.on { background:var(--accent); color:var(--accent-ink); }
  /* botões com identidade antiga -> identidade Cubo (menta/âmbar) */
  html[data-ui="cubo"] button.toggle.on { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
  html[data-ui="cubo"] a.steam { border-color:var(--cb-border); color:var(--cb-muted); }
  html[data-ui="cubo"] a.steam:hover { background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }
  html[data-ui="cubo"] .fav:hover, html[data-ui="cubo"] .fav.on { color:var(--gold); }
  html[data-ui="cubo"] #pager button.cur { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
  html[data-ui="cubo"] .fchip { background:#11161d; border-color:var(--cb-border); color:var(--cb-muted); }
  html[data-ui="cubo"] #activeFilters { background:var(--cb-surface); border-bottom-color:var(--cb-border-soft); }
  /* barra de controles do Mercado (moeda/ordenação/visão) alinhada à direita, como no modelo */
  html[data-ui="cubo"] #marketControls { justify-content:flex-end; background:transparent; border-bottom:0; padding-top:14px; }
  html[data-ui="cubo"] .cb-visao { display:inline-flex; align-items:center; gap:6px; font-size:12.5px; color:var(--cb-muted); }
  html[data-ui="cubo"] .cb-visao select { height:36px; border-radius:9px; }
  html[data-ui="cubo"] .tab { color:var(--cb-faint); }
  html[data-ui="cubo"] .tab.on { color:var(--cb-text); background:#11201b; border-color:#2dd4a733;
    box-shadow:inset 0 -2px 0 var(--accent); font-weight:600; }
  html[data-ui="cubo"] table { font-family:'Figtree', sans-serif; }
  html[data-ui="cubo"] thead th { background:var(--cb-surface); color:var(--cb-faint); }
  html[data-ui="cubo"] th, html[data-ui="cubo"] td { border-bottom-color:var(--cb-border-soft); }
  html[data-ui="cubo"] .bagdev { background:var(--cb-surface); border-color:var(--cb-border); }

  /* ---- Fase 2: toggle cartões/tabela + hero + grade de cartões (só no layout Cubo) ---- */
  .cubo-only { display:none !important; }
  html[data-ui="cubo"] .cubo-only { display:inline-flex !important; }
  #cuboView { display:none; padding:14px 20px 2px; }
  body.cubo-cards #cuboView { display:block; }
  body.cubo-cards .wrap { display:none; }
  html[data-ui="cubo"] .cbtile { display:inline-flex; align-items:center; justify-content:center;
    flex:0 0 auto; width:42px; height:42px; border-radius:11px; border:1px solid;
    font-family:'IBM Plex Mono',monospace; font-size:10px; font-weight:600; }
  html[data-ui="cubo"] .cbtile.lg { width:54px; height:54px; border-radius:13px; font-size:13px; }
  html[data-ui="cubo"] .cbtile { position:relative; overflow:hidden; }
  html[data-ui="cubo"] .cbtile img { position:absolute; inset:0; width:100%; height:100%;
    object-fit:contain; padding:5px; image-rendering:pixelated; }   /* cobre a abreviação quando carrega */
  html[data-ui="cubo"] .cc-dot { width:8px; height:8px; border-radius:3px; display:inline-block; flex:0 0 auto; }
  html[data-ui="cubo"] .cc-mut, html[data-ui="cubo"] .cc-lbl { color:var(--cb-faint); }
  /* hero "melhor negócio agora" */
  html[data-ui="cubo"] .cubohero { display:grid; grid-template-columns:1fr auto; gap:18px; align-items:center;
    background:linear-gradient(120deg,#11201b,#0e1318 72%); border:1px solid #1e2b27; border-radius:16px;
    padding:20px 22px; margin-bottom:16px; cursor:pointer; }
  html[data-ui="cubo"] .cubohero:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  html[data-ui="cubo"] .ch-badges { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  html[data-ui="cubo"] .ch-top { font-size:11px; font-weight:700; color:#1c1404; background:var(--gold); border-radius:6px; padding:3px 9px; }
  html[data-ui="cubo"] .ch-deal { font-size:11px; font-weight:700; color:var(--accent-ink); background:var(--accent); border-radius:20px; padding:3px 10px; }
  html[data-ui="cubo"] .ch-id { display:flex; align-items:center; gap:14px; margin-top:14px; }
  html[data-ui="cubo"] .ch-name { font-family:'Space Grotesk',sans-serif; font-size:23px; font-weight:600; letter-spacing:-.3px; }
  html[data-ui="cubo"] .ch-sub { display:flex; align-items:center; gap:6px; margin-top:5px; font-size:12.5px; }
  html[data-ui="cubo"] .ch-nums { display:flex; gap:24px; margin-top:18px; flex-wrap:wrap; }
  html[data-ui="cubo"] .ch-sep { border-left:1px solid #1e2b27; padding-left:24px; }
  html[data-ui="cubo"] .ch-big { font-family:'Space Grotesk',sans-serif; font-size:32px; font-weight:700; line-height:1; }
  html[data-ui="cubo"] .ch-big.accent { color:var(--accent); }
  html[data-ui="cubo"] .ch-delta { align-self:flex-end; font-size:13px; color:var(--cb-faint); white-space:nowrap; }
  /* grade de cartões */
  html[data-ui="cubo"] #cuboGrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(258px,1fr)); gap:14px; }
  html[data-ui="cubo"] .cubocard { background:var(--cb-surface); border:1px solid var(--cb-border);
    border-top:2px solid var(--gc); border-radius:14px; padding:15px; display:flex; flex-direction:column; gap:12px; cursor:pointer; }
  html[data-ui="cubo"] .cubocard:hover { border-color:#2dd4a766; }
  html[data-ui="cubo"] .cubocard:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  html[data-ui="cubo"] .cc-top { display:flex; align-items:flex-start; gap:11px; }
  html[data-ui="cubo"] .cc-id { flex:1; min-width:0; }
  html[data-ui="cubo"] .cc-name { font-family:'Space Grotesk',sans-serif; font-weight:600; font-size:15px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  html[data-ui="cubo"] .cc-sub { display:flex; align-items:center; gap:6px; margin-top:4px; font-size:11.5px; }
  html[data-ui="cubo"] .cc-rk { display:flex; flex-direction:column; align-items:flex-end; gap:6px; flex:0 0 auto; }
  html[data-ui="cubo"] .cc-rank { font-size:10.5px; color:#6a7280; font-family:'IBM Plex Mono',monospace; }
  html[data-ui="cubo"] .cc-metric { display:flex; align-items:flex-end; justify-content:space-between; }
  html[data-ui="cubo"] .cc-big { font-family:'Space Grotesk',sans-serif; font-size:25px; font-weight:700; color:var(--accent); line-height:1; }
  html[data-ui="cubo"] .cc-lbl { font-size:10.5px; margin-top:3px; }
  html[data-ui="cubo"] .cc-foot { display:flex; justify-content:space-between; border-top:1px solid var(--cb-border-soft);
    padding-top:10px; font-size:11.5px; color:var(--cb-faint); }
  html[data-ui="cubo"] .cc-foot b { color:#cdd3dd; font-family:'IBM Plex Mono',monospace; }
  html[data-ui="cubo"] .cubocard .trend, html[data-ui="cubo"] .cubohero .trend { font-size:12.5px; }
  html[data-ui="cubo"] .cbempty { padding:34px; text-align:center; color:var(--cb-faint);
    grid-column:1/-1; background:var(--cb-surface); border:1px solid var(--cb-border); border-radius:14px; }
  html[data-ui="cubo"] .lock { font-size:11px; color:#e0a86a; }
  /* mini-sparkline (Fase 3) */
  html[data-ui="cubo"] .cc-spark { display:inline-block; }
  html[data-ui="cubo"] .cbspark { width:72px; height:24px; display:block; }
  html[data-ui="cubo"] .ch-spark .cbspark { width:180px; height:60px; }
  html[data-ui="cubo"] .cc-metric .cc-right { display:flex; flex-direction:column; align-items:flex-end; gap:2px; }
  html[data-ui="cubo"] .ch-spark { display:flex; flex-direction:column; align-items:center; gap:6px; }
  /* respeita "reduzir movimento": desliga pulsos/flutuações (o count-up é tratado no JS) */
  @media (prefers-reduced-motion: reduce){
    .dot, [style*="animation"] { animation:none !important; }
  }
  @media (max-width:560px){
    html[data-ui="cubo"] #cuboGrid { grid-template-columns:1fr; }
    html[data-ui="cubo"] .cubohero { grid-template-columns:1fr; }
    html[data-ui="cubo"] .ch-delta { align-self:flex-start; }
  }

  /* ---- Fase 2b: sidebar (nav vertical + filtros + raridade + stat) no layout Cubo ---- */
  #cuboSidebar { display:none; }
  html[data-ui="cubo"] nav.tabs { display:none; }   /* navegação vira vertical na sidebar */
  html[data-ui="cubo"] body { display:grid; grid-template-columns:248px minmax(0,1fr);
    grid-template-areas:"head head" "side main" "foot foot"; }
  html[data-ui="cubo"] header { grid-area:head; }
  html[data-ui="cubo"] footer { grid-area:foot; }
  html[data-ui="cubo"] #cuboMain { grid-area:main; min-width:0; }
  html[data-ui="cubo"] #cuboSidebar { grid-area:side; display:block; background:var(--cb-sidebar);
    border-right:1px solid var(--cb-border-soft); padding:16px 14px;
    position:sticky; top:0; align-self:start; overflow:visible; }   /* visible: não corta os painéis dos filtros */
  html[data-ui="cubo"] #cbNav { display:flex; flex-direction:column; gap:3px; }
  html[data-ui="cubo"] #cbNav button { text-align:left; background:transparent; border:1px solid transparent;
    border-radius:9px; padding:9px 11px; color:var(--cb-muted); font-size:13.5px; cursor:pointer; }
  html[data-ui="cubo"] #cbNav button:hover { background:#11161d; }
  html[data-ui="cubo"] #cbNav button.on { background:#11201b; color:var(--accent); font-weight:600; }
  html[data-ui="cubo"] .cb-sec-t { font-size:10px; text-transform:uppercase; letter-spacing:.7px;
    color:#6a7280; margin:18px 4px 8px; }
  html[data-ui="cubo"] #cbFilterSlot .group { display:flex; flex-direction:column; align-items:stretch;
    gap:8px; border:0; padding:0; }
  /* padroniza só as CAIXAS (gatilhos) da lateral via filho-direto — NÃO o conteúdo dos painéis */
  html[data-ui="cubo"] #cbFilterSlot .dropdown { display:block; width:100%; }
  html[data-ui="cubo"] #cbFilterSlot .ddbtn,
  html[data-ui="cubo"] #cbFilterSlot > .group > input,
  html[data-ui="cubo"] #cbFilterSlot > .group > select,
  html[data-ui="cubo"] #cbFilterSlot > .group > button {
    width:100%; height:38px; box-sizing:border-box; padding:0 11px; font-size:13px; border-radius:9px; }
  html[data-ui="cubo"] #cbFilterSlot .ddbtn { display:flex; align-items:center; justify-content:space-between;
    gap:8px; text-align:left; background:#11161d; border:1px solid var(--cb-border); color:var(--cb-muted); }
  html[data-ui="cubo"] #cbFilterSlot .ddbtn.act { background:#11201b; border-color:#2dd4a755; color:var(--accent); }
  html[data-ui="cubo"] #cbFilterSlot > .group > select { line-height:36px; }
  html[data-ui="cubo"] #cuboSidebar #q { width:100%; height:38px; box-sizing:border-box; padding:0 11px 0 32px;
    background-repeat:no-repeat; background-position:11px center;
    background-image:url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='14'%20height='14'%20fill='none'%20stroke='%237e8696'%20stroke-width='2'%3E%3Ccircle%20cx='6'%20cy='6'%20r='4.5'/%3E%3Cpath%20d='M10%2010l3%203'/%3E%3C/svg%3E"); }
  html[data-ui="cubo"] #cbFilterSlot .ddpanel { min-width:100%; }   /* painel acompanha o gatilho; checkboxes normais */
  html[data-ui="cubo"] #cbFilterSlot .toggle { display:flex; align-items:center; justify-content:flex-start; gap:7px; }
  html[data-ui="cubo"] #cbFilterSlot #clear { display:none; }       /* usamos o "limpar" do cabeçalho de Filtros */
  html[data-ui="cubo"] #cbFilterSlot #resultcount { display:none; } /* contagem já aparece no card de stat */
  html[data-ui="cubo"] .cb-rarity { display:flex; flex-wrap:wrap; gap:6px; }
  html[data-ui="cubo"] .cb-rarity span { display:flex; align-items:center; gap:6px; font-size:11px;
    color:var(--cb-muted); background:#11161d; border:1px solid var(--cb-border); border-radius:20px; padding:3px 9px; }
  html[data-ui="cubo"] .cb-rarity i { width:9px; height:9px; border-radius:3px; display:inline-block; }
  html[data-ui="cubo"] .cb-stat { margin-top:18px; background:linear-gradient(135deg,#11201b,#0e1318);
    border:1px solid #1e2b27; border-radius:12px; padding:14px; }
  html[data-ui="cubo"] .cb-stat .k { font-size:10px; text-transform:uppercase; letter-spacing:.7px; color:#6a7280; }
  html[data-ui="cubo"] .cb-stat .v { font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700;
    color:var(--accent); margin-top:6px; }
  html[data-ui="cubo"] .cb-stat .s { font-size:11.5px; color:var(--cb-faint); margin-top:3px; }
  /* divisórias entre seções + cabeçalho de Filtros com "limpar" (igual ao modelo) */
  html[data-ui="cubo"] .cb-div { height:1px; background:var(--cb-border-soft); margin:16px 0; }
  html[data-ui="cubo"] .cb-fhead { display:flex; align-items:center; justify-content:space-between; }
  html[data-ui="cubo"] .cb-fhead .cb-sec-t { margin:18px 4px 8px; }
  html[data-ui="cubo"] .cb-clear { background:none; border:0; color:var(--accent); font-size:11px; padding:0 4px; cursor:pointer; height:auto; }
  html[data-ui="cubo"] .cb-clear:hover { text-decoration:underline; background:none; }
  /* cabeçalho do site no estilo Cubo: logo ◳ + wordmark à esquerda, controles à direita */
  html[data-ui="cubo"] header { display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:14px 22px; }
  html[data-ui="cubo"] header h1, html[data-ui="cubo"] header .chip, html[data-ui="cubo"] header #baseline { display:none; }
  html[data-ui="cubo"] header .meta { margin-left:auto; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  html[data-ui="cubo"] .cb-brand { display:inline-flex; align-items:center; gap:12px; }
  html[data-ui="cubo"] .cb-logo { width:36px; height:36px; flex:0 0 36px; border-radius:10px; color:#06140f;
    display:flex; align-items:center; justify-content:center; font-size:19px;
    font-family:'Space Grotesk',sans-serif; font-weight:700; background:linear-gradient(135deg,#2dd4a7,#1a8d6e); }
  html[data-ui="cubo"] .cb-brandtext { display:flex; flex-direction:column; line-height:1.2; }
  html[data-ui="cubo"] .cb-word { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:18px; letter-spacing:-.3px; }
  html[data-ui="cubo"] .cb-tag { font-size:11.5px; color:var(--cb-faint); }
  html[data-ui="cubo"] .cb-connect { background:var(--accent); color:var(--accent-ink); border-color:var(--accent);
    font-weight:600; border-radius:9px; }
  html[data-ui="cubo"] .cb-connect:hover:not(:disabled) { background:var(--accent); filter:brightness(1.06); }
  html[data-ui="cubo"] body.view-effects .cb-filters, html[data-ui="cubo"] body.view-effects #cbSearchSlot,
  html[data-ui="cubo"] body.view-farm .cb-filters, html[data-ui="cubo"] body.view-farm #cbSearchSlot,
  html[data-ui="cubo"] body.view-craft .cb-filters, html[data-ui="cubo"] body.view-craft #cbSearchSlot,
  html[data-ui="cubo"] body.view-bag .cb-filters, html[data-ui="cubo"] body.view-bag #cbSearchSlot { display:none; }
  @media (max-width:760px){
    html[data-ui="cubo"] body { grid-template-columns:1fr; grid-template-areas:"head" "side" "main" "foot"; }
    html[data-ui="cubo"] #cuboSidebar { position:static; max-height:none; border-right:0;
      border-bottom:1px solid var(--cb-border-soft); }
    html[data-ui="cubo"] #cbNav { flex-direction:row; flex-wrap:wrap; }
  }
</style></head>
<body>
<header>
  <div class="cb-brand cubo-only">
    <span class="cb-logo">◳</span>
    <span class="cb-brandtext"><span class="cb-word">Cubo</span><span class="cb-tag">Mercado do Task Bar Hero</span></span>
  </div>
  <h1>TBH Market Tool — Itens × Mercado Steam</h1>
  <div class="meta">
    <span class="chip" data-tip="data/hora em que o ranking (bulk) foi gerado — horário do build (UTC no GitHub Actions); veja o horário local em 'preços atualizados'">📅 bulk: __GENERATED__</span>
    <span class="chip"><span id="count">__N__</span> itens</span>
    <span id="status" aria-live="polite"><span class="dot off"></span>verificando servidor…</span>
    <span class="seg uiswitch" id="uiSwitch" role="group" aria-label="layout do site"
        data-tip="alterne entre o layout atual e o novo (Cubo, em validação) — sua escolha fica salva e vai no link">
      <button type="button" data-ui="atual">Atual</button><button type="button" data-ui="cubo">Cubo<span class="beta">beta</span></button>
    </span>
    <button type="button" class="cb-connect cubo-only" id="cbConnect"
        data-tip="ler o save do jogo e ver o valor da sua mochila (em desenvolvimento)">⬆ Conectar save</button>
  </div>
  <div class="meta" id="baseline" style="margin-top:6px"></div>
</header>
<nav class="tabs" role="tablist" aria-label="seções">
  <button id="tabMarket" class="tab on" role="tab" aria-selected="true" data-tip="ranking de itens × mercado">Mercado</button>
  <button id="tabEffects" class="tab" role="tab" aria-selected="false" data-tip="gemas/decorações: efeito por slot + preço">Efeitos (gemas)</button>
  <button id="tabFarm" class="tab" role="tab" aria-selected="false" data-tip="stages: onde dropam os itens (+ preço quando tradável)">Farm</button>
  <button id="tabCraft" class="tab" role="tab" aria-selected="false" data-tip="craft (receitas): custo dos reagentes × valor dos itens da pull — vale craftar ou vender os materiais?">Craft</button>
  <button id="tabBag" class="tab" role="tab" aria-selected="false" data-tip="valor da sua mochila a partir do save do jogo — em desenvolvimento">🎒 Minha Mochila<span class="badge-new">em breve</span></button>
</nav>
<aside id="cuboSidebar" aria-label="navegação e filtros (layout Cubo)">
  <div id="cbSearchSlot"></div>
  <div class="cb-sec-t">Navegação</div>
  <nav id="cbNav" aria-label="seções"></nav>
  <div class="cb-div"></div>
  <div class="cb-filters">
    <div class="cb-fhead"><span class="cb-sec-t">Filtros</span><button type="button" id="cbClear" class="cb-clear">limpar</button></div>
    <div id="cbFilterSlot"></div>
  </div>
  <div class="cb-div"></div>
  <div class="cb-sec-t">Raridade</div>
  <div id="cbRarity" class="cb-rarity"></div>
  <div id="cbStat" class="cb-stat"></div>
</aside>
<div id="cuboMain">
<div class="controls" id="marketControls">
  <div class="group" id="filterGroup">
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
    <button id="sellNow" class="toggle" aria-pressed="false"
        data-tip="o que você RECEBE agora vendendo na encomenda (líquido −taxa Steam): ordena por isso e mostra só itens com encomenda ativa">💰 Vender agora</button>
    <button id="soulFilter" class="toggle" aria-pressed="false"
        data-tip="Soulstones — únicos itens de grade alta tradáveis no reabrir do mercado">🔮 Soulstones</button>
    <button id="clear" data-tip="limpa busca e todos os filtros">✕ Limpar</button>
  </div>
  <div class="group">
    <div class="seg cubo-only" id="cuboModeSeg" role="group" aria-label="visualização"
        data-tip="cartões ou tabela (tabela = todas as infos)">
      <button type="button" data-m="cards">▦ Cartões</button><button type="button" data-m="table">▤ Tabela</button>
    </div>
    <label class="cubo-only cb-visao" data-tip="ordenar o ranking por">
      <select id="cuboSort" aria-label="ordenar por">
        <option value="goldPerEst">gold / moeda</option>
        <option value="goldPerReal">preço real (gold/moeda)</option>
        <option value="buyScore">encomenda · vender agora</option>
        <option value="chg24">maior alta 24h</option>
        <option value="gold">gold (cubo)</option>
        <option value="priceEst">preço</option>
      </select>
    </label>
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
<div id="movers" aria-label="maiores variações de preço"></div>
<div id="cuboView" aria-label="ranking em cartões (layout Cubo)">
  <div id="cuboHero"></div>
  <div id="cuboGrid"></div>
</div>
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
<div id="pager" aria-label="paginação"></div>
<section id="effectsView" aria-label="efeitos das gemas">
  <div class="controls">
    <div class="group">
      <input type="text" id="eq" placeholder="buscar gema..." aria-label="buscar gema por nome">
      <select id="eSlot" aria-label="filtrar por slot"><option value="">slot: todos</option></select>
      <select id="eStat" aria-label="filtrar por atributo"><option value="">atributo: todos</option></select>
      <select id="eSort" aria-label="ordenar">
        <option value="price">menor preço</option>
        <option value="buyNet">maior encomenda líquida</option>
        <option value="ppr">melhor gold/moeda</option>
        <option value="name">nome (A–Z)</option>
      </select>
      <span class="chip" id="eCount"></span>
    </div>
  </div>
  <div id="effectsGrid"></div>
</section>
<section id="farmView" aria-label="stages / farm">
  <div class="controls">
    <div class="group">
      <input type="text" id="fq" placeholder="buscar stage ou item..." aria-label="buscar stage ou item dropado">
      <select id="fAct" aria-label="filtrar por ato"><option value="">ato: todos</option></select>
      <select id="fSort" aria-label="ordenar stages">
        <option value="ev">maior valor/caixa</option>
        <option value="label">stage (1-1, 1-2…)</option>
        <option value="level">nível</option>
      </select>
      <label class="meta"><input type="checkbox" id="fTrad"> só com drop tradável</label>
      <span class="chip" id="fCount"></span>
    </div>
  </div>
  <div id="farmGrid"></div>
</section>
<section id="craftView" aria-label="craft / receitas">
  <div class="controls">
    <div class="group">
      <input type="text" id="cq" placeholder="buscar tipo ou item..." aria-label="buscar receita por tipo ou item resultante">
      <select id="cType" aria-label="filtrar por tipo de equipamento"><option value="">tipo: todos</option></select>
      <select id="cVerdict" aria-label="filtrar por veredito">
        <option value="">veredito: todos</option>
        <option value="craft">✅ vale craftar</option>
        <option value="gamble">🎲 aposta</option>
        <option value="sell">💰 vender reagentes</option>
        <option value="unknown">— sem preço</option>
      </select>
      <select id="cSort" aria-label="ordenar receitas">
        <option value="margin">melhor margem (EV − custo)</option>
        <option value="pwin">maior chance de lucro</option>
        <option value="ceil">maior teto</option>
        <option value="tier">tier</option>
      </select>
      <span class="chip" id="cCount"></span>
    </div>
  </div>
  <p class="crafthint">Cada receita junta os <b>reagentes</b> (materiais) e sorteia 1 item da <b>pull</b> conforme as chances por grade. Comparamos o <b>custo dos reagentes</b> com o <b>valor de revenda</b> dos itens possíveis: se nada na pull supera os reagentes, o melhor é <b>vendê-los</b>. Preços de <b>venda</b> (USD) cruzados com o mercado; itens sem oferta contam como sem valor de revenda.</p>
  <div id="craftGrid"></div>
</section>
<section id="bagView" aria-label="minha mochila">
  <div class="bagdev">
    <div class="ico">🎒</div>
    <h2>Minha Mochila — valor do seu inventário</h2>
    <p>Conecte o save do jogo e veja o valor da sua mochila cruzado com os preços do mercado —
       quanto vale, o que compensa vender e onde estão suas melhores oportunidades.</p>
    <span class="devtag">🚧 em desenvolvimento</span>
    <div>
      <button type="button" class="connect" id="bagConnect" disabled
        data-tip="recurso em desenvolvimento — vai ler o save localmente, sem upload">⬆ Conectar save (em breve)</button>
    </div>
    <ul class="road">
      <li>① Ler o save do jogo <b>localmente no navegador</b> (privado, sem upload)</li>
      <li>② Cruzar os itens com o ranking de preços atual</li>
      <li>③ Mostrar valor total, melhores vendas e encomendas ativas</li>
    </ul>
  </div>
</section>
</div><!-- /#cuboMain -->
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
    Object.assign({ cur, rate, sortK, sortDir, realMode, pageSize, auto:autoOn }, filterState())
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
let page = 1;                                            // paginação da tabela do Mercado
let pageSize = [20,50,100,200].includes(P.pageSize) ? P.pageSize : 20;
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
  if(d.gradeLock) return false;           // travado na reabertura: nada a consultar
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

// ---- Aba de Efeitos (gemas/decorações) --------------------------------------------------
let curView = "market";
// Layout Cubo (Fase 2): no Mercado, alterna entre grade de CARTÕES e a TABELA atual.
let cuboMode = "cards";
try{ const m=localStorage.getItem("tbh_cubomode"); if(m==="cards"||m==="table") cuboMode=m; }catch(e){}
function isCubo(){ return document.documentElement.getAttribute("data-ui")==="cubo"; }
// liga/desliga a classe que faz o CSS mostrar os cartões e esconder a tabela (só Cubo+Mercado+cards)
function syncCuboMode(){
  document.body.classList.toggle("cubo-cards", isCubo() && curView==="market" && cuboMode==="cards");
}
function setView(v){
  curView = v;
  document.body.classList.toggle("view-effects", v==="effects");
  document.body.classList.toggle("view-farm", v==="farm");
  document.body.classList.toggle("view-craft", v==="craft");
  document.body.classList.toggle("view-bag", v==="bag");
  for(const [id,name] of [["tabMarket","market"],["tabEffects","effects"],["tabFarm","farm"],["tabCraft","craft"],["tabBag","bag"]]){
    const on=v===name; $(id).classList.toggle("on", on); $(id).setAttribute("aria-selected", String(on)); }
  try{ localStorage.setItem("tbh_view", v); }catch(e){}
  syncCuboMode(); syncCuboNav();
  if(v==="effects") renderEffects(); else if(v==="farm") renderFarm();
  else if(v==="craft") renderCraft(); else if(v==="bag") renderBag(); else render();
}
// Minha Mochila: stub. A UI "em desenvolvimento" já está no HTML; aqui fica o ponto de entrada
// para plugar o backend depois (ler save local -> cruzar com DATA -> render do valor da mochila).
function renderBag(){ /* TODO(backend): parse do save + valor do inventário. Ver roadmap-redesign-cubo.md */ }
function gemRows(){ return DATA.filter(d=>d.effects && d.effects.length); }
function populateEffectFilters(){
  const gems = gemRows();
  if($("eSlot").options.length > 1 || !gems.length) return;   // idempotente; espera DATA chegar
  const slots = [...new Set(gems.flatMap(d=>d.effects.map(g=>g.slot)).filter(Boolean))].sort();
  const stats = [...new Set(gems.flatMap(d=>d.effects.map(g=>g.stat)).filter(Boolean))].sort();
  $("eSlot").insertAdjacentHTML("beforeend", slots.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join(""));
  $("eStat").insertAdjacentHTML("beforeend", stats.map(s=>`<option value="${esc(s)}">${esc(attrLabel(s))}</option>`).join(""));
}
function renderEffects(){
  const q=$("eq").value.toLowerCase(), slot=$("eSlot").value, stat=$("eStat").value, sortBy=$("eSort").value;
  const gems = gemRows().map(derive).filter(d=>
    (!q || d.name.toLowerCase().includes(q)) &&
    (!slot || d.effects.some(g=>g.slot===slot)) &&
    (!stat || d.effects.some(g=>g.stat===stat)));
  const asc = sortBy==="price" || sortBy==="name";
  const val = d => sortBy==="price" ? (d.priceEst ?? Infinity)
              : sortBy==="buyNet" ? (d.buyNet ?? -Infinity)
              : sortBy==="ppr" ? (d.goldPerEst ?? -Infinity) : d.name;
  gems.sort((a,b)=>{ const x=val(a),y=val(b); return (typeof x==="string"? x.localeCompare(y) : x-y) * (asc?1:-1); });
  $("eCount").textContent = `${gems.length} gemas`;
  $("effectsGrid").innerHTML = gems.map(gemCard).join("") || `<div class="meta" style="padding:20px">Nenhuma gema corresponde.</div>`;
  $("effectsGrid").querySelectorAll(".gemcard").forEach(c=>{
    const open=()=>{ const d=DATA.find(x=>x.name===c.dataset.name); if(d) openDetail(d); };
    c.onclick=open; c.onkeydown=e=>{ if(e.key==="Enter") open(); };
  });
}
function gemCard(d){
  const gc=GRADE_COLORS[d.grade]||"#cdd3e0";
  const icon = d.icon
    ? `<img class="icon" src="${ICON_BASE}${encodeURIComponent(d.icon)}.png" alt="" loading="lazy" style="border-color:${gc}66" onerror="this.classList.add('noimg');this.removeAttribute('src')">`
    : `<span class="icon noimg"></span>`;
  const price = d.priceEst!=null ? sym()+d.priceEst.toFixed(2) : "—";
  const net = d.buyNet!=null ? `<span class="gp"><span class="lbl">enc.</span> <b>${sym()}${d.buyNet.toFixed(2)}</b></span>` : "";
  const effs = d.effects.map(g=>`<div class="geff"><span class="gs">${esc(g.slot||"")} · ${esc(attrLabel(g.stat||""))}${g.chance!=null&&g.chance<1?` <span class="muted">(${Math.round(g.chance*100)}%)</span>`:""}</span><span class="gv">${esc(g.disp||"")}</span></div>`).join("");
  return `<div class="gemcard" data-name="${esc(d.name)}" tabindex="0" title="ver detalhes">
    <div class="gh">${icon}<span class="gname" style="color:${gc}">${esc(d.name)}</span><span class="gbadge" style="color:${gc};border-color:${gc}55;background:${gc}1a">${esc(d.grade)}</span></div>
    <div class="gprices"><span class="gp"><span class="lbl">preço</span> <b>${price}</b></span>${net}</div>
    <div class="gsec"><span class="glabel">Efeitos</span>${effs||'<div class="meta">—</div>'}</div></div>`;
}

// ---- Aba Farm (stages) -----------------------------------------------------------------
let STAGES = null;
async function ensureStages(){
  if(STAGES===null){
    try{ STAGES = await (await fetch("api/stages.json",{cache:"no-cache"})).json(); }
    catch(e){ STAGES = []; }
    populateFarmFilters();
  }
  return STAGES;
}
function populateFarmFilters(){
  if($("fAct").options.length>1 || !STAGES || !STAGES.length) return;
  const acts=[...new Set(STAGES.map(s=>s.act).filter(a=>a!=null))].sort((a,b)=>a-b);
  $("fAct").insertAdjacentHTML("beforeend", acts.map(a=>`<option value="${a}">Ato ${a}</option>`).join(""));
}
async function renderFarm(){
  const box=$("farmGrid"); if(!box) return;
  box.innerHTML = `<div class="meta" style="padding:20px">carregando…</div>`;
  await ensureStages();
  // orientada a filtro: só mostra os cards quando há busca/ato/“só tradável”. Sem isso, um prompt.
  if(!($("fq").value.trim() || $("fAct").value || $("fTrad").checked)){
    box.innerHTML = `<div class="farmprompt">🔍 <b>Busque um item ou escolha uma fase</b>
      <div class="meta">para ver onde farmar, os drops e o valor por caixa</div></div>`;
    $("fCount").textContent = "";
    return;
  }
  const byName = new Map(DATA.map(d=>[d.name, d]));   // cruza o drop com o item de mercado
  const q=$("fq").value.toLowerCase(), act=$("fAct").value, onlyTrad=$("fTrad").checked, sortBy=$("fSort").value;
  let stages = STAGES.filter(s=>{
    if(act && String(s.act)!==act) return false;
    if(onlyTrad && !(s.drops||[]).some(d=>byName.has(d.name))) return false;
    if(q){ const hay=((s.label||"")+" "+(s.name||"")+" "+(s.drops||[]).map(d=>d.name).join(" ")).toLowerCase();
           if(!hay.includes(q)) return false; }
    return true;
  });
  const fx = cur==="brl" ? (rate>0?rate:1) : 1;   // EV vem em USD -> moeda atual
  stages = stages.slice().sort((a,b)=>
    sortBy==="label" ? (a.label||"").localeCompare(b.label||"", undefined, {numeric:true})
    : sortBy==="level" ? (a.level??0)-(b.level??0)
    : (b.ev||0)-(a.ev||0));   // ev: maior primeiro
  $("fCount").textContent = `${stages.length} stages`;
  box.innerHTML = stages.map(s=>stageCard(s, byName, fx)).join("") || `<div class="meta" style="padding:20px">Nenhum stage corresponde.</div>`;
  box.querySelectorAll(".stagedrop.trad,.evtop[data-name]").forEach(el=> el.onclick=()=>{ const d=DATA.find(x=>x.name===el.dataset.name); if(d) openDetail(d); });
}
function stageCard(s, byName, fx){
  const drops=(s.drops||[]).map(d=>{
    const row=byName.get(d.name), gc=GRADE_COLORS[d.grade]||"#9aa3b8";
    const icon=d.icon?`<img class="icon sm" src="${ICON_BASE}${encodeURIComponent(d.icon)}.png" alt="" loading="lazy" onerror="this.classList.add('noimg');this.removeAttribute('src')">`:`<span class="icon sm noimg"></span>`;
    const price = row ? derive(row).priceEst : null;
    const pTag = price!=null ? `<span class="dp">${sym()}${price.toFixed(2)}</span>` : "";
    return `<div class="stagedrop${row?' trad':''}"${row?` data-name="${esc(d.name)}" title="ver ${esc(d.name)}"`:""}>${icon}<span class="dn" style="color:${gc}">${esc(d.name)}</span>${pTag}<span class="dr">taxa ${esc(String(d.rate??"—"))}</span></div>`;
  }).join("");
  // valor tradável esperado por caixa (aprox.) + itens que mais contribuem
  const evTag = s.ev>0 ? `<span class="evchip" data-tip="valor tradável esperado por caixa aberta (aprox.; só itens com mercado; ignora roll de grade)">≈ ${sym()}${(s.ev*fx).toFixed(2)}/caixa</span>` : "";
  const top = (s.top&&s.top.length) ? `<div class="evtops">vale por: ${s.top.map(([n,v])=>`<span class="evtop" data-name="${esc(n)}" title="ver ${esc(n)} (${sym()}${(v*fx).toFixed(2)}/caixa)">${esc(n)}</span>`).join(" · ")}</div>` : "";
  return `<div class="stagecard">
    <div class="sh"><span class="slabel">${esc(s.label||"?")}</span>${s.level!=null?`<span class="meta">Lv ${s.level}</span>`:""}<span class="sname">${esc(s.name||"")}</span>${evTag||(s.boss?`<span class="sboss">boss: ${esc(s.boss)}</span>`:"")}</div>
    ${top}${drops||'<div class="meta">sem drops</div>'}</div>`;
}

// ---- Aba Craft (receitas) --------------------------------------------------------------
// Cada receita: reagentes (materiais) × pull (itens por grade). Veredito vem do build:
//   craft = EV de revenda ≥ custo · gamble = teto > custo mas EV < custo · sell = nada bate o custo
//   unknown = algum material sem preço de mercado. Preços em USD -> moeda atual pela taxa.
let CRAFT = null;
const TYPE_LABEL = {MainWeapon:"Arma princ.", SubWeapon:"Arma sec.", Helmet:"Elmo", Armor:"Armadura",
  Gloves:"Luvas", Boots:"Botas", Accessory:"Acessório"};
const VERDICT_META = {
  craft:{cls:"craft", txt:"✅ vale craftar", tip:"o valor médio de revenda da pull é maior que o custo dos reagentes"},
  gamble:{cls:"gamble", txt:"🎲 aposta", tip:"existe item na pull que vale mais que os reagentes, mas na média (EV) você perde — é aposta"},
  sell:{cls:"sell", txt:"💰 venda os reagentes", tip:"nenhum item possível da pull vale mais que os reagentes — melhor vendê-los"},
  unknown:{cls:"unknown", txt:"— sem preço", tip:"algum material não tem preço de mercado agora; não dá p/ decidir"},
};
async function ensureCraft(){
  if(CRAFT===null){
    try{ CRAFT = await (await fetch("api/craft.json",{cache:"no-cache"})).json(); }
    catch(e){ CRAFT = []; }
    populateCraftFilters();
  }
  return CRAFT;
}
function populateCraftFilters(){
  if($("cType").options.length>1 || !CRAFT || !CRAFT.length) return;
  const types=[...new Set(CRAFT.map(c=>c.type).filter(Boolean))];
  $("cType").insertAdjacentHTML("beforeend",
    types.map(t=>`<option value="${esc(t)}">${esc(TYPE_LABEL[t]||t)}</option>`).join(""));
}
async function renderCraft(){
  const box=$("craftGrid"); if(!box) return;
  box.innerHTML = `<div class="meta" style="padding:20px">carregando…</div>`;
  await ensureCraft();
  if(!CRAFT.length){
    box.innerHTML = `<div class="craftprompt">⚙️ <b>Sem dados de craft</b><div class="meta">o feed api/craft.json não está disponível neste build</div></div>`;
    $("cCount").textContent = ""; return;
  }
  const q=$("cq").value.toLowerCase(), type=$("cType").value, verd=$("cVerdict").value, sortBy=$("cSort").value;
  const fx = cur==="brl" ? (rate>0?rate:1) : 1;   // USD -> moeda atual
  let list = CRAFT.filter(c=>
    (!type || c.type===type) &&
    (!verd || c.verdict===verd) &&
    (!q || (TYPE_LABEL[c.type]||c.type||"").toLowerCase().includes(q)
        || (c.mats||[]).some(m=>(m.name||"").toLowerCase().includes(q))
        || (c.grades||[]).some(g=>g.best && g.best.name.toLowerCase().includes(q))));
  const margin = c => (c.ev!=null && c.cost!=null) ? c.ev-c.cost : -Infinity;
  list = list.slice().sort((a,b)=>
    sortBy==="pwin" ? (b.pWin??-1)-(a.pWin??-1)
    : sortBy==="ceil" ? (b.ceil??-1)-(a.ceil??-1)
    : sortBy==="tier" ? (a.tier??0)-(b.tier??0) || (a.type||"").localeCompare(b.type||"")
    : margin(b)-margin(a));
  $("cCount").textContent = `${list.length} receitas`;
  box.innerHTML = list.map(c=>craftCard(c, fx)).join("") || `<div class="meta" style="padding:20px">Nenhuma receita corresponde.</div>`;
  box.querySelectorAll(".crow.trad").forEach(el=>{
    el.onclick=()=>{ const d=DATA.find(x=>x.name===el.dataset.name); if(d) openDetail(d); };
  });
}
function craftCard(c, fx){
  const m=VERDICT_META[c.verdict]||VERDICT_META.unknown;
  const money = v => v==null ? "—" : sym()+(v*fx).toFixed(2);
  // reagentes
  const mats=(c.mats||[]).map(mt=>{
    const icon=mt.icon?`<img class="icon sm" src="${ICON_BASE}${encodeURIComponent(mt.icon)}.png" alt="" loading="lazy" onerror="this.classList.add('noimg');this.removeAttribute('src')">`:`<span class="icon sm noimg"></span>`;
    const pr=mt.price!=null?`<span class="cmp">${money(mt.price)}</span>`:`<span class="cmp na" title="sem preço de mercado">s/preço</span>`;
    return `<span class="cmat">${icon}${mt.count>1?`<b>${mt.count}×</b>`:""}<span>${esc(mt.name||"?")}</span>${pr}</span>`;
  }).join("");
  // economia: custo · EV (revenda média) · chance de lucro
  const econ = `<div class="cecon">
    <span class="ce cost"><span class="lbl">reagentes</span><b>${money(c.cost)}</b></span>
    <span class="ce"><span class="lbl">EV revenda</span><b>${money(c.ev)}</b></span>
    <span class="ce ${c.pWin&&c.cost!=null&&c.ev>c.cost?"win":""}"><span class="lbl">chance lucro</span><b>${c.pWin!=null?Math.round(c.pWin*100)+"%":"—"}</b></span>
  </div>`;
  const range = (c.floor!=null||c.ceil!=null)
    ? `<div class="crange">faixa da pull: <span class="lo">${money(c.floor)}</span> — <span class="pk">${money(c.ceil)}</span>${c.best?` <span class="muted">(teto: ${esc(c.best.grade)} ${esc(c.best.name)})</span>`:""}</div>` : "";
  // itens da pull por grade (mostra os que têm preço; resume os sem preço)
  const rows=(c.grades||[]).map(g=>{
    const gc=GRADE_COLORS[g.grade]||"#9aa3b8";
    const badge=`<span class="gbadge" style="color:${gc};border-color:${gc}55;background:${gc}1a">${esc(g.grade)}</span>`;
    const pct=`<span class="cpct">${g.pct!=null?g.pct+"%":""}</span>`;
    if(g.best){
      const beat = c.cost!=null && g.best.price>c.cost;
      const clk = g.best.mname ? ` trad" data-name="${esc(g.best.mname)}` : "";
      return `<div class="crow${clk}" title="ver ${esc(g.best.name)}">${pct}${badge}<span class="cbn" style="color:${gc}">${esc(g.best.name)}</span><span class="cbp${beat?" beat":""}">${money(g.best.price)}${g.n>1?` <span class="muted">·${g.n} c/ preço</span>`:""}</span></div>`;
    }
    return `<div class="crow noprice">${pct}${badge}<span class="cbn muted">${g.ntot} ${g.ntot>1?"itens":"item"}</span><span class="cnp">sem oferta</span></div>`;
  }).join("");
  return `<div class="craftcard">
    <div class="ch"><span class="ctype">${esc(TYPE_LABEL[c.type]||c.type||"?")}</span><span class="ctier">T${c.tier}</span><span class="clvl">Lv ${c.lvl&&c.lvl[0]!=null?c.lvl[0]:"?"}–${c.lvl&&c.lvl[1]!=null?c.lvl[1]:"?"}</span><span class="vbadge ${m.cls}" title="${esc(m.tip)}">${m.txt}</span></div>
    ${econ}${range}
    <div class="csec"><span class="clabel">Reagentes</span><div class="cmats">${mats||'<span class="meta">—</span>'}</div></div>
    <div class="csec"><span class="clabel">Pull (melhor item por grade)</span>${rows||'<div class="meta">—</div>'}</div></div>`;
}

// Top movers: maiores altas/quedas de preço. Usa "desde a reabertura" (chgReopen) quando há dado;
// senão cai p/ 24h. Independe dos filtros (lê o DATA inteiro). Chips abrem o item.
function renderMovers(){
  const box=$("movers"); if(!box) return;
  const useReopen = DATA.some(d=>d.chgReopen!=null);
  const key = useReopen ? "chgReopen" : "chg24";
  const lbl = useReopen ? "desde a reabertura" : "24h";
  const withc = DATA.filter(d=>d[key]!=null);
  if(withc.length<3){ box.innerHTML=""; return; }
  const up=[...withc].filter(d=>d[key]>0).sort((a,b)=>b[key]-a[key]).slice(0,5);
  const down=[...withc].filter(d=>d[key]<0).sort((a,b)=>a[key]-b[key]).slice(0,5);
  if(!up.length && !down.length){ box.innerHTML=""; return; }
  const chip=(d,cls)=>`<button class="mvchip ${cls}" data-name="${esc(d.name)}" title="abrir ${esc(d.name)} (${d[key]>0?"+":""}${d[key]}%)">`
    + `${cls==="up"?"▲":"▼"} <span class="mvn">${esc(d.name)}</span> <b>${d[key]>0?"+":""}${d[key]}%</b></button>`;
  box.innerHTML = `<span class="mvlbl">🔥 Maiores variações (${lbl}):</span> `
    + up.map(d=>chip(d,"up")).join("") + down.map(d=>chip(d,"down")).join("");
  box.querySelectorAll(".mvchip").forEach(b=> b.onclick=()=>{ const d=DATA.find(x=>x.name===b.dataset.name); if(d) openDetail(d); });
}

// barra de paginação da tabela do Mercado: info + números (com reticências) + tamanho por página.
function renderPager(total, pages, off, shown){
  const box=$("pager"); if(!box) return;
  const info=`<span class="pinfo">mostrando <b>${total?off+1:0}–${off+shown}</b> de <b>${total}</b></span>`;
  let nav="";
  if(pages>1){
    nav+=`<button data-pg="${page-1}" ${page<=1?'disabled':''} aria-label="anterior">‹</button>`;
    const want=[...new Set([1,pages,page,page-1,page+1])].filter(p=>p>=1&&p<=pages).sort((a,b)=>a-b);
    let prev=0;
    for(const p of want){ if(p-prev>1) nav+=`<span class="pgap">…</span>`; nav+=`<button data-pg="${p}" class="${p===page?'cur':''}">${p}</button>`; prev=p; }
    nav+=`<button data-pg="${page+1}" ${page>=pages?'disabled':''} aria-label="próxima">›</button>`;
  }
  const size=`<label>por página <select id="pgSize">${[20,50,100,200].map(n=>`<option value="${n}"${n===pageSize?' selected':''}>${n}</option>`).join("")}</select></label>`;
  box.innerHTML=info+nav+size;
  box.querySelectorAll("button[data-pg]").forEach(b=> b.onclick=()=>{ const p=+b.dataset.pg; if(p>=1&&p<=pages&&p!==page){ page=p; render(); document.querySelector(".wrap")?.scrollTo?.({top:0}); } });
  $("pgSize").onchange=e=>{ pageSize=+e.target.value; page=1; savePrefs(); render(); };
}

// ---- Layout Cubo: hero "melhor negócio" + grade de cartões (Fase 2) ---------------------
function cuboAbbr(d){
  const s=(d.gearType||d.type||d.name||"").replace(/[^A-Za-z]/g,"");
  return (s.slice(0,3).toUpperCase()||"·");
}
function cuboTile(d, gc, lg){
  return `<span class="cbtile${lg?' lg':''}" style="background:${gc}22;border-color:${gc}33;color:${gc}">${cuboAbbr(d)}</span>`;
}
// tile com o ÍCONE do item (borda na cor da raridade); se a imagem faltar/falhar, mostra a abreviação
function cuboIcon(d, gc, lg){
  const img = d.icon ? `<img src="${ICON_BASE}${encodeURIComponent(d.icon)}.png" alt="" loading="lazy" decoding="async" onerror="this.style.display='none'">` : "";
  return `<span class="cbtile${lg?' lg':''}" style="background:${gc}1a;border-color:${gc}40">${img}<span class="cbabbr" style="color:${gc}">${cuboAbbr(d)}</span></span>`;
}
function cuboSub(d, gc){
  const t = d.gearType ? titleCase(d.gearType) : (d.type?titleCase(d.type):"");
  return `<span class="cc-dot" style="background:${gc}"></span><span style="color:${gc}">${esc(d.grade||"—")}</span>`
       + `<span class="cc-mut"> · ${esc(t||"—")}${d.level!=null?" · lvl "+d.level:""}</span>`;
}
function cuboPrice(d){
  if(d.gradeLock===true) return '<span class="lock">intradável</span>';
  return d.priceEst!=null ? sym()+d.priceEst.toFixed(2) : "—";
}
// Fase 3 — sparklines (de HIST/api/history.json), count-up e respeito a "reduzir movimento"
function deltaColor(d){ const c=d.chg24; return c>0?"#3fbf7f":(c<0?"#ef6b6b":"#8a93a6"); }
function cuboSparkSvg(name, color){
  const s = HIST && HIST[name]; if(!s || s.length<2) return "";
  const vs = s.map(p=>p[1]).filter(v=>v!=null); if(vs.length<2) return "";
  const W=64,H=20, lo=Math.min(...vs), hi=Math.max(...vs), rng=(hi-lo)||1;
  const pts = vs.map((v,i)=>{ const x=(i/(vs.length-1))*W;
    const y=Math.max(1,Math.min(H-1, H-((v-lo)/rng)*H)); return x.toFixed(1)+","+y.toFixed(1); }).join(" ");
  return `<svg class="cbspark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}
// preenche os placeholders .cc-spark depois que o feed de histórico chega (1 fetch, cacheado)
function injectCuboSparks(){
  document.querySelectorAll("#cuboView .cc-spark").forEach(el=>{
    if(el.dataset.done) return;
    el.innerHTML = cuboSparkSvg(el.dataset.name, el.dataset.color||"#8a93a6");
    el.dataset.done = "1";
  });
}
const REDUCE_MOTION = (()=>{ try{ return matchMedia("(prefers-reduced-motion: reduce)").matches; }catch(e){ return false; } })();
function countUp(el, target){
  if(REDUCE_MOTION || !el){ if(el) el.textContent=fmt(Math.round(target)); return; }
  const dur=900, t0=performance.now();
  function step(t){ let p=Math.min(1,(t-t0)/dur); p=1-Math.pow(1-p,3);
    el.textContent=fmt(Math.round(target*p)); if(p<1) requestAnimationFrame(step); }
  requestAnimationFrame(step);
}
let cbStatAnimated=false;
function cuboHeroHtml(d, gc){
  return `<div class="cubohero" data-name="${esc(d.name)}" tabindex="0" role="button"
      aria-label="melhor negócio: ${esc(d.name)}" style="--gc:${gc}">
    <div class="ch-main">
      <div class="ch-badges"><span class="ch-top">★ TOP</span><span class="ch-deal">Melhor negócio agora</span></div>
      <div class="ch-id">${cuboIcon(d,gc,true)}<div style="min-width:0">
        <div class="ch-name">${esc(d.name)}</div>
        <div class="ch-sub">${cuboSub(d,gc)}</div></div></div>
      <div class="ch-nums">
        <div><div class="ch-big accent">${d.goldPerEst!=null?fmtAbbr(d.goldPerEst):"—"}</div><div class="cc-lbl">gold por ${sym().trim()}</div></div>
        <div class="ch-sep"><div class="ch-big">${fmtAbbr(d.gold)}</div><div class="cc-lbl">gold no cubo · ${cuboPrice(d)}</div></div>
      </div>
    </div>
    <div class="ch-spark"><span class="cc-spark" data-name="${esc(d.name)}" data-color="#2dd4a7"></span><span class="ch-delta">${trendCell(d)} · 24h</span></div>
  </div>`;
}
function cuboCardHtml(d, gc, rank){
  const liq = liqClass(liqScore(d.listings, d.vol));
  return `<div class="cubocard" data-name="${esc(d.name)}" tabindex="0" role="button"
      aria-label="${esc(d.name)}" style="--gc:${gc}">
    <div class="cc-top">${cuboIcon(d,gc)}
      <div class="cc-id"><div class="cc-name" style="color:${gc}">${highlightName(d.name)}</div>
        <div class="cc-sub">${cuboSub(d,gc)}</div></div>
      <div class="cc-rk"><span class="cc-rank">#${rank}</span><span class="liq ${liq}" title="liquidez"></span></div>
    </div>
    <div class="cc-metric">
      <div><div class="cc-big">${d.goldPerEst!=null?fmtAbbr(d.goldPerEst):"—"}</div><div class="cc-lbl">gold / ${sym().trim()}</div></div>
      <div class="cc-right"><span class="cc-spark" data-name="${esc(d.name)}" data-color="${deltaColor(d)}"></span>${trendCell(d)}</div>
    </div>
    <div class="cc-foot"><span>Gold <b data-tip="${fmt(d.gold)} gold">${fmtAbbr(d.gold)}</b></span><span>Preço <b>${cuboPrice(d)}</b></span></div>
  </div>`;
}
function renderCuboCards(rows, maxPpr, dealCut){
  const hero=$("cuboHero"), grid=$("cuboGrid");
  if(!rows.length){
    hero.innerHTML="";
    grid.innerHTML=`<div class="cbempty">Nenhum item corresponde aos filtros.
      <button id="clearEmpty" style="margin-left:8px">✕ Limpar filtros</button></div>`;
    const cb=$("clearEmpty"); if(cb) cb.onclick=clearFilters;
    if($("pager")) $("pager").innerHTML="";
    return;
  }
  const pages=Math.max(1, Math.ceil(rows.length/pageSize));
  if(page>pages) page=pages;
  const off=(page-1)*pageSize, pageRows=rows.slice(off, off+pageSize);
  renderPager(rows.length, pages, off, pageRows.length);
  // hero = líder da ordenação/filtro atuais (só na 1ª página); cartões = o restante da página
  if(page===1 && pageRows.length){
    hero.innerHTML=cuboHeroHtml(pageRows[0], GRADE_COLORS[pageRows[0].grade]||"#9aa3b8");
  } else hero.innerHTML="";
  const cards=(page===1)?pageRows.slice(1):pageRows;
  grid.innerHTML=cards.map((d,i)=>cuboCardHtml(d, GRADE_COLORS[d.grade]||"#9aa3b8", off+(page===1?2:1)+i)).join("");
  ensureHist().then(injectCuboSparks);   // mini-sparklines (1 fetch cacheado; preenche depois)
}
// ---- Sidebar do Cubo (Fase 2b): nav vertical, legenda de raridade, stat, realocação de filtros ----
const CB_NAV = [["market","◆ Ranking"],["effects","✦ Efeitos"],["farm","⛏ Farm"],["craft","⚒ Craft"],["bag","🎒 Minha Mochila"]];
function buildCuboNav(){
  const el=$("cbNav"); if(!el || el.dataset.built) return;
  el.innerHTML = CB_NAV.map(([v,l])=>`<button type="button" data-view="${v}">${l}</button>`).join("");
  el.querySelectorAll("button").forEach(b=> b.onclick=()=>setView(b.dataset.view));
  el.dataset.built="1";
  syncCuboNav();
}
function syncCuboNav(){
  document.querySelectorAll("#cbNav button").forEach(b=>b.classList.toggle("on", b.dataset.view===curView));
}
function buildCuboRarity(){
  const el=$("cbRarity"); if(!el || el.dataset.built || !DATA.length) return;
  const present=[...new Set(DATA.map(d=>d.grade).filter(Boolean))]
    .sort((a,b)=>(GRADE_RANK[a]??99)-(GRADE_RANK[b]??99));
  el.innerHTML = present.map(g=>`<span><i style="background:${GRADE_COLORS[g]||'#9aa3b8'}"></i>${esc(titleCase(g))}</span>`).join("");
  el.dataset.built="1";
}
function updateCuboStat(visible){
  const el=$("cbStat"); if(!el) return;
  el.innerHTML = `<div class="k">Itens monitorados</div><div class="v">${fmt(DATA.length)}</div>`
    + `<div class="s">${visible!=null? fmt(visible)+" visíveis no filtro" : "mercado cruzado com a Steam"}</div>`;
  if(!cbStatAnimated && DATA.length){ cbStatAnimated=true; countUp(el.querySelector(".v"), DATA.length); }
}
// move o GRUPO de filtros (busca + dropdowns + toggles) entre a barra do topo e a sidebar.
// move o nó real (preserva os event listeners já fiados); idempotente.
function relocateFilters(toCubo){
  const slot=$("cbFilterSlot"), mc=$("marketControls"), grp=$("filterGroup"),
        sslot=$("cbSearchSlot"), q=$("q");
  if(!slot||!mc||!grp) return;
  if(toCubo){
    if(grp.parentElement!==slot) slot.appendChild(grp);
    if(q && sslot && q.parentElement!==sslot) sslot.appendChild(q);        // busca no TOPO da sidebar
  } else {
    if(q && q.parentElement!==grp) grp.insertBefore(q, grp.firstChild);    // busca volta p/ início do grupo
    if(grp.parentElement!==mc) mc.insertBefore(grp, mc.firstChild);
  }
}
function render(){
  const rows = currentRows();
  renderMovers();

  // baseline na moeda corrente
  const ppr = rows.map(d=>d.goldPerEst).filter(v=>v>0).sort((a,b)=>a-b);
  const mean = ppr.length ? ppr.reduce((s,v)=>s+v,0)/ppr.length : 0;
  const med = ppr.length ? ppr[Math.floor(ppr.length/2)] : 0;
  const maxPpr = ppr.length ? ppr[ppr.length-1] : 0;   // p/ a mini-barra (2.1)
  $("baseline").innerHTML = `<span class="chip">📊 baseline gold/${sym().trim()} (est.)</span> `
    + `média <b>${fmt(mean)}</b> · mediana <b>${fmt(med)}</b> · <b>${fmt(rows.length)}</b> visíveis de ${fmt(DATA.length)}`;
  $("count").textContent = DATA.length;
  $("resultcount").textContent = `${rows.length} / ${DATA.length}`;
  buildCuboRarity(); updateCuboStat(rows.length);   // sidebar do Cubo (no-op visual fora do Cubo)
  renderChips();
  updateHeaders();
  updateEnrichBtn(rows);
  const dealCut = med * 2;   // arbitragem: gold/moeda >= 2× a mediana do filtro = ótimo negócio
  const acols = selAttrList();   // colunas de atributo visíveis nesta render

  // Layout Cubo (cartões): reusa rows/baseline/movers já computados; a tabela fica escondida.
  if(isCubo() && curView==="market" && cuboMode!=="table"){
    renderCuboCards(rows, maxPpr, dealCut); markSort(); return;
  }

  if(!rows.length){
    tbody.innerHTML = `<tr><td class="empty" colspan="${colCount()}">
      Nenhum item corresponde aos filtros.
      <button id="clearEmpty" style="margin-left:8px">✕ Limpar filtros</button></td></tr>`;
    $("clearEmpty").onclick = clearFilters;
    if($("pager")) $("pager").innerHTML = "";
    markSort();
    return;
  }

  // paginação: rendeiza só a página atual (o baseline/contagem usam o conjunto inteiro)
  const pages = Math.max(1, Math.ceil(rows.length/pageSize));
  if(page>pages) page=pages;
  const off=(page-1)*pageSize, pageRows=rows.slice(off, off+pageSize);
  renderPager(rows.length, pages, off, pageRows.length);
  tbody.innerHTML = pageRows.map((d,gi0)=>{
    const i = off+gi0;   // índice GLOBAL na ordenação (p/ o 🏆 do líder ficar só na 1ª página)
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
    const newBadge = d.isNew ? `<span class="newb" data-tip="listado no mercado a partir da reabertura">NOVO</span> ` : "";
    const nameHtml = `<span class="itemname"${gc?` style="color:${gc}"`:""} data-tip="clique para ver detalhes">${newBadge}${uniq}${highlightName(d.name)}</span>`;
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
  const sn = sortK==="buyNet" && $("avail").value==="buy";
  $("sellNow").classList.toggle("on", sn);
  $("sellNow").setAttribute("aria-pressed", String(sn));
  const sl = $("q").value.trim().toLowerCase()==="soulstone";
  $("soulFilter").classList.toggle("on", sl);
  $("soulFilter").setAttribute("aria-pressed", String(sl));
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
// filtro/ordenação/moeda muda -> volta p/ pág.1 e re-renderiza a ABA ativa (mercado/efeitos/farm/craft)
const rerender = ()=>{ page=1; savePrefs(); syncURL();
  if(curView==="effects") renderEffects(); else if(curView==="farm") renderFarm();
  else if(curView==="craft") renderCraft(); else render(); };
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
// abas (Mercado / Efeitos) + filtros da aba de Efeitos
$("tabMarket").onclick = ()=>setView("market");
$("tabEffects").onclick = ()=>setView("effects");
$("tabFarm").onclick = ()=>setView("farm");
$("tabCraft").onclick = ()=>setView("craft");
$("tabBag").onclick = ()=>setView("bag");

// A/B de layout (Atual × Cubo). data-ui já foi setado no <head> (anti-flash); aqui só tratamos a
// troca pelo usuário: aplica no <html>, persiste e reflete na URL (link compartilhável).
function setUI(v){
  v = (v==="cubo") ? "cubo" : "atual";
  document.documentElement.setAttribute("data-ui", v);
  try{ localStorage.setItem("tbh_ui", v); }catch(e){}
  try{ const u=new URL(location.href);
       if(v==="atual") u.searchParams.delete("ui"); else u.searchParams.set("ui", v);
       history.replaceState(null, "", u); }catch(e){}
  document.querySelectorAll("#uiSwitch button").forEach(b=>b.classList.toggle("on", b.dataset.ui===v));
  relocateFilters(v==="cubo");        // filtros na sidebar (Cubo) ou na barra do topo (Atual)
  buildCuboNav(); buildCuboRarity();
  if(v==="cubo") applyCuboBar();   // aplica modo/ordenação salvos ao entrar no Cubo
  syncCuboMode();
  if(curView==="market") render(); else updateCuboStat(null);   // re-render Mercado conforme layout
}
(function initUISwitch(){
  const cur = document.documentElement.getAttribute("data-ui") || "atual";
  document.querySelectorAll("#uiSwitch button").forEach(b=>{
    b.classList.toggle("on", b.dataset.ui===cur);
    b.onclick = ()=>setUI(b.dataset.ui);
  });
})();
// Barra do Cubo: toggle Cartões/Tabela + seletor de ordenação ao lado (como no modelo).
const CB_SORT_ASC = new Set(["name","grade","gearType","classes"]);
let cuboSort = "goldPerEst";
try{ const s=localStorage.getItem("tbh_cubosort"); if(s) cuboSort=s; }catch(e){}
function applyCuboBar(){   // reflete modo + ordenação salvos nos controles e no estado de sort
  sortK = cuboSort; sortDir = CB_SORT_ASC.has(cuboSort) ? 1 : -1;
  const ss=$("cuboSort"); if(ss) ss.value=cuboSort;
  document.querySelectorAll("#cuboModeSeg button").forEach(b=>b.classList.toggle("on", b.dataset.m===cuboMode));
}
function setCuboMode(m){
  cuboMode = (m==="table") ? "table" : "cards";
  try{ localStorage.setItem("tbh_cubomode", cuboMode); }catch(e){}
  document.querySelectorAll("#cuboModeSeg button").forEach(b=>b.classList.toggle("on", b.dataset.m===cuboMode));
  syncCuboMode();
  if(curView==="market") render();
}
function setCuboSort(k){
  cuboSort = k;
  try{ localStorage.setItem("tbh_cubosort", k); }catch(e){}
  sortK = k; sortDir = CB_SORT_ASC.has(k) ? 1 : -1;
  if(curView==="market") render();
}
(function initCuboBar(){
  document.querySelectorAll("#cuboModeSeg button").forEach(b=>{
    b.classList.toggle("on", b.dataset.m===cuboMode);
    b.onclick = ()=>setCuboMode(b.dataset.m);
  });
  const sel=$("cuboSort"); if(sel){ sel.value=cuboSort; sel.onchange=()=>setCuboSort(sel.value); }
  const cc=$("cbClear"); if(cc) cc.onclick=clearFilters;        // "limpar" do cabeçalho de Filtros
  const cs=$("cbConnect"); if(cs) cs.onclick=()=>setView("bag"); // "Conectar save" -> aba Mochila
  if(isCubo()){ sortK=cuboSort; sortDir=CB_SORT_ASC.has(cuboSort)?1:-1; }   // 1º render correto
  buildCuboNav();
  relocateFilters(isCubo());
  syncCuboMode();
})();
// abrir detalhe ao clicar/Enter num cartão ou no hero (reusa openDetail com o item cru de DATA)
(function wireCuboClicks(){
  const view=$("cuboView"); if(!view) return;
  const open = el => { const raw = el && DATA.find(x=>x.name===el.dataset.name); if(raw) openDetail(raw); };
  view.addEventListener("click", e=>{ const el=e.target.closest("[data-name]"); if(el) open(el); });
  view.addEventListener("keydown", e=>{ if(e.key!=="Enter"&&e.key!==" ") return;
    const el=e.target.closest("[data-name]"); if(el){ e.preventDefault(); open(el); } });
})();
["eq","eSlot","eStat","eSort"].forEach(id=>$(id).addEventListener("input", ()=>{ if(curView==="effects") renderEffects(); }));
["fq","fAct","fSort","fTrad"].forEach(id=>$(id).addEventListener("input", ()=>{ if(curView==="farm") renderFarm(); }));
["cq","cType","cVerdict","cSort"].forEach(id=>$(id).addEventListener("input", ()=>{ if(curView==="craft") renderCraft(); }));
populateEffectFilters();
try{ const sv=localStorage.getItem("tbh_view");
  if(sv==="effects" && gemRows().length) setView("effects");
  else if(sv==="farm") setView("farm");
  else if(sv==="craft") setView("craft"); }catch(e){}
$("rate").addEventListener("input", ()=>{ rate=parseFloat($("rate").value)||rate; rerenderDebounced(); });
$("clear").onclick = clearFilters;
$("favFilter").onclick = ()=>{
  showFavs = !showFavs;
  $("favFilter").classList.toggle("on", showFavs);
  $("favFilter").setAttribute("aria-pressed", String(showFavs));
  rerender();
};
// preset "Vender agora": liquidação imediata. Alterna entre o preset (ordena por líquido da
// encomenda + só com encomenda) e o estado padrão (gold/moeda, todos).
function sellNowActive(){ return sortK==="buyNet" && $("avail").value==="buy"; }
$("sellNow").onclick = ()=>{
  if(sellNowActive()){ sortK="goldPerEst"; sortDir=-1; $("avail").value=""; }
  else { sortK="buyNet"; sortDir=-1; $("avail").value="buy"; }
  rerender();
};
// atalho Soulstones: alterna a busca por "Soulstone" (no reabrir, os únicos tradáveis de grade alta).
function soulActive(){ return $("q").value.trim().toLowerCase()==="soulstone"; }
$("soulFilter").onclick = ()=>{ $("q").value = soulActive() ? "" : "Soulstone"; rerender(); };

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
  if($("avail").value){ const m={vol:"só com giro 24h",offer:"esconder sem oferta",buy:"só com encomenda"};
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
  // efeitos da gema (effects.json): por slot, stat e intensidade (chance se < 100%)
  const effHtml = (d.effects&&d.effects.length)
    ? d.effects.map(g=>`<div class="dattr"><span class="an">${esc(g.slot||"")} · ${esc(attrLabel(g.stat||""))}${g.chance!=null&&g.chance<1?` <span class="muted">(${Math.round(g.chance*100)}%)</span>`:""}</span><span class="av">${esc(g.disp||"")}</span></div>`).join("")
    : "";
  // locais de drop (stages.json): onde farmar este item, com a taxa relativa
  const farmHtml = (d.droppedIn&&d.droppedIn.length)
    ? d.droppedIn.map(f=>`<div class="dattr"><span class="an">${esc(f.stage||"?")}${f.level!=null?` <span class="muted">Lv ${f.level}</span>`:""}${f.source?` <span class="muted">· ${esc(f.source)}</span>`:""}</span><span class="av">taxa ${esc(String(f.rate??"—"))}</span></div>`).join("")
    : "";
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
      ${effHtml?`<div class="dsec"><h3>Efeitos (gema)</h3>${effHtml}</div>`:""}
      ${farmHtml?`<div class="dsec"><h3>Onde dropa (farm)</h3>${farmHtml}</div>`:""}
      <div class="dsec"><h3>Economia</h3><div class="dgrid">${kvHtml(econ)}</div></div>
      <div class="dsec" id="dHist"><h3>Histórico de preço</h3><div class="meta">carregando…</div></div>
      <div class="dactions">
        <a class="steam" href="${steamUrl(d.name)}" target="_blank" rel="noopener noreferrer">↗ Steam</a>
        <button id="dCopy">⧉ copiar nome</button>
        <button id="dLink" data-tip="copia um link que reabre o site já neste item">🔗 copiar link</button>
        <button id="dFav">${isFav?'★ favoritado':'☆ favoritar'}</button>
      </div>
    </div>`;
  $("detail").hidden=false;
  requestAnimationFrame(()=>{ $("detail").classList.add("open"); $("detailOverlay").classList.add("open"); });
  $("detail").querySelector(".dclose").onclick=closeDetail;
  $("dCopy").onclick=()=>{ (navigator.clipboard?navigator.clipboard.writeText(d.name):Promise.reject())
    .then(()=>toast("Nome copiado.","ok"), ()=>toast("Não foi possível copiar.","error")); };
  $("dLink").onclick=()=>{ const url=location.origin+location.pathname+"?q="+encodeURIComponent(d.name);
    (navigator.clipboard?navigator.clipboard.writeText(url):Promise.reject())
    .then(()=>toast("Link copiado.","ok"), ()=>toast("Não foi possível copiar.","error")); };
  $("dFav").onclick=()=>{ toggleFav(d.name); openDetail(raw); };
  if(serverOn) loadHistory(d.name); else loadHistoryStatic(d.name);
}
async function loadHistory(name){
  const box=$("dHist"); if(!box) return;
  try{
    const r=await api(`/api/history?currency=${cur}&name=${encodeURIComponent(name)}`);
    drawSpark(box, (r&&r.points)||[]);
  }catch(e){ box.innerHTML=`<h3>Histórico de preço</h3><div class="meta">sem histórico</div>`; }
}
// público: histórico do feed estático (api/history.json), buscado 1x e cacheado. Série em USD ->
// converte p/ a moeda atual (como as estimativas) antes de desenhar a mesma sparkline.
let HIST = null;
async function ensureHist(){
  if(HIST===null){
    try{ HIST = await (await fetch("api/history.json", {cache:"no-cache"})).json(); }
    catch(e){ HIST = {}; }
  }
  return HIST;
}
async function loadHistoryStatic(name){
  const box=$("dHist"); if(!box) return;
  const h=await ensureHist();
  const f = cur==="brl" ? (rate>0?rate:1) : 1;
  const pts=(h[name]||[]).map(([ts,v])=>({ts, low:v*f}));
  drawSpark(box, pts);
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
      <path d="${dpath}" fill="none" stroke="#4fd1a5" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>
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
  // selo "AO VIVO": dados atualizados automaticamente (≠ print congelado). Some se o build ficar
  // velho (Action travada) p/ não enganar — vira aviso de desatualizado.
  const ageH = (Date.now()/1000 - GEN_EPOCH)/3600;
  const live = ageH < 6
    ? `<span class="live" title="dados ao vivo — atualizados automaticamente; não é um print congelado">AO VIVO</span> `
    : `<span class="live stale" title="atualização automática atrasada — pode estar desatualizado">desatualizado</span> `;
  $("status").innerHTML = `${live}<span class="dot ok"></span>somente leitura · preços atualizados ${rel} <span class="muted">(${when})</span>`;
}
(async function detectServer(){
  if(PUBLIC){            // Pages: sem servidor, sem atualização pela web
    serverOn = false;
    if(!DATA.length){    // build público embute DATA=[] e busca o feed estático (index.html enxuto)
      try{ DATA = await (await fetch("api/data.json", {cache:"no-cache"})).json(); }
      catch(e){ toast("Não foi possível carregar os dados.", "error"); }
    }
    showLastUpdate();
    populateEffectFilters();
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
    const d = await api("/api/data"); if(d && d.rows){ DATA = d.rows; populateEffectFilters(); }
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


# --- Desacoplamento de assets ------------------------------------------------------------
# O front-end mora num único template, mas servimos CSS e JS como arquivos ESTÁTICOS próprios
# (cacheáveis: só o index.html — shell pequeno + DATA — muda a cada build). O split é feito UMA vez,
# em tempo de import: <style> -> styles.css, <script> -> app.js, deixando inline só as 4 linhas de
# config (DATA/TOKEN/PUBLIC/GEN_EPOCH, que mudam por build). Scripts clássicos compartilham o escopo
# léxico global, então o app.js enxerga as consts declaradas no script de config inline.
_JS_SPLIT_MARK = "const $ = id => document.getElementById(id);"


def _split_template(tmpl):
    m_css = re.search(r"<style>(.*?)</style>", tmpl, re.S)
    # ancora no script PRINCIPAL (o que começa com `let DATA`) p/ ignorar scripts inline curtos no
    # <head> (ex.: o theme-setter anti-flash do A/B). `.*` guloso ainda casa até o último </script>.
    m_js = re.search(r"<script>(\nlet DATA.*)</script>", tmpl, re.S)
    js = m_js.group(1)
    i = js.index(_JS_SPLIT_MARK)
    config_js, app_js = js[:i], js[i:]                       # config (placeholders) | código estável
    css = m_css.group(1).strip("\n")
    # cache-busting por hash de conteúdo (?v=): um index.html novo SEMPRE puxa o asset casado.
    # Sem isso, o browser servia o app.js velho do cache (max-age=600) contra o index novo —
    # aba/feature recém-deployada "não funcionava" até o cache expirar. O hash só muda quando o
    # asset muda, então builds sem mudança de código reaproveitam o cache normalmente.
    css_v = hashlib.sha1(css.encode("utf-8")).hexdigest()[:8]
    js_v = hashlib.sha1(app_js.encode("utf-8")).hexdigest()[:8]
    shell = tmpl.replace(m_css.group(0), f'<link rel="stylesheet" href="assets/styles.css?v={css_v}">')
    shell = shell.replace(
        m_js.group(0),
        "<script>" + config_js + f"</script>\n<script src=\"assets/app.js?v={js_v}\" defer></script>")
    for ph in ("__DATA__", "__TOKEN__", "__PUBLIC__", "__GEN_EPOCH__", "__SITE__",
               "__N__", "__RATE__", "__GENERATED__", "__SERVER_CONTROLS__"):
        assert ph not in css and ph not in app_js, f"placeholder {ph} vazou p/ um asset estático"
    return shell, css, app_js


_SHELL, CSS_CONTENT, APP_JS = _split_template(HTML_TEMPLATE)


def write_assets(adir=None):
    """Grava os assets estáticos (CSS + app.js). Cacheáveis; mudam raramente (só com deploy de código)."""
    adir = adir or os.path.join(HERE, "assets")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "styles.css"), "w", encoding="utf-8") as f:
        f.write(CSS_CONTENT)
    with open(os.path.join(adir, "app.js"), "w", encoding="utf-8") as f:
        f.write(APP_JS)


def render_html(rows, brl_rate, token="", public=False):
    if public:
        # Pages (http): DATA vem de api/data.json via fetch -> index.html fica enxuto. file:// não
        # entra aqui (build estático local embute DATA p/ funcionar ao abrir o arquivo direto).
        data = "[]"
    else:
        data = json.dumps(rows, ensure_ascii=False)
        # anti-XSS: impede quebra do </script> e injeção via conteúdo do JSON (só p/ DATA inline)
        data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    out = _SHELL
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
    # pula itens travados na reabertura (intradáveis): não há preço a buscar -> prioriza tradáveis
    cands = [r for r in rows if not r.get("gradeLock")][:top_n]
    for r in cands:
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


def enrich_orderbook(rows, top_n, shards=1, shard=0, out_path=None):
    """Coleta as encomendas (buy orders) dos itens. `top_n <= 0` coleta TODOS os candidatos.
    Respeita o TTL p/ não remartelar e o throttle global da Steam.

    Candidato = item TRADÁVEL e NÃO travado por grade. NÃO filtramos por nº de listagens: um item
    pode ter encomenda (buy order) com ZERO vendas — aliás é o caso mais valioso na reabertura (muita
    demanda, ninguém vendendo; ex.: Frozen Orb (Arcana) com 645 encomendas e 0 ofertas). Como esses
    itens não aparecem na busca de VENDAS da Steam (`listings`=0), filtrar por listagem os perdia.
    Ordena por VALOR (gold) desc — assim os itens caros (onde estão as maiores encomendas) são
    coletados primeiro e não ficam famintos se o passo estourar o tempo no CI.

    SHARDING (paralelismo no CI): com `shards > 1`, cada runner processa só a fatia
    `candidates[shard::shards]` (stride). O corte por passada (e não por bloco contíguo) distribui
    itens caros e baratos por igual entre os shards -> tempos de execução equilibrados. Como cada
    runner tem IP próprio, os shards rodam em paralelo sem brigar pelo mesmo limite de taxa da Steam.

    `out_path` (modo shard): grava a coleta deste runner como um PARCIAL em JSON ({name:{cur:bk}}) e
    NÃO toca o cache/histórico compartilhado — quem consolida é `--merge-orderbook` no job de merge,
    evitando corrida de escrita em orderbook.json/history.db entre runners paralelos.

    Sem `out_path`, salva o cache incrementalmente (a cada ORDERBOOK_FLUSH_EVERY itens): uma coleta
    longa pode estourar o timeout do passo do CI; sem flush, todo o progresso se perderia."""
    book = load_orderbook()
    candidates = sorted((r for r in rows if r.get("tradable") and not r.get("gradeLock")),
                        key=lambda r: r.get("gold") or 0, reverse=True)
    if shards > 1:
        candidates = candidates[shard::shards]   # fatia deste runner (stride: caros/baratos juntos)
    limit = len(candidates) if top_n <= 0 else top_n
    collected = {}         # parcial deste shard (vai p/ out_path; não mexe no cache compartilhado)
    pending, done = {}, 0  # `pending`: coletas ainda não persistidas (zera a cada flush)
    alvo = "todos" if top_n <= 0 else str(limit)
    escopo = f" [shard {shard + 1}/{shards}]" if shards > 1 else ""
    print(f"\n[orderbook] encomendas dos {alvo} itens (tradáveis, por valor){escopo}...")

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
        # só guarda se há atividade real (encomenda OU venda). Varremos TODOS os tradáveis agora,
        # então muitos voltam mortos (sem buy e sem sell) — guardá-los incharia o cache e contaria
        # como "com encomenda" um item vazio. Itens só-com-encomenda (buyMax sem sellMin) são o alvo.
        if bk.get("buyMax") is None and bk.get("sellMin") is None:
            continue
        book.setdefault(r["name"], {})[curkey] = bk
        pending.setdefault(r["name"], {})[curkey] = bk
        collected.setdefault(r["name"], {})[curkey] = bk
        r["book"] = book[r["name"]]
        sp = _spread_pct(bk)
        bm = f"{bk['buyMax']:.2f}" if bk['buyMax'] is not None else "—"
        print(f"  + {r['name']}: maior enc {bm} {curkey.upper()} · "
              f"{bk['buyOrders']} enc · spread {sp if sp is not None else '—'}%")
        done += 1
        if not out_path and done % ORDERBOOK_FLUSH_EVERY == 0:
            flush()
    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(collected, f, ensure_ascii=False)
        os.replace(tmp, out_path)
        print(f"[orderbook] shard {shard + 1}/{shards}: {done} itens -> {out_path}")
    else:
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


HISTORY_FEED_MAX = 60   # pontos por item no feed de histórico (limita o tamanho conforme acumula)


def _history_feed():
    """Série de preço (USD) por item, p/ os gráficos do site público: {name: [[ts, v], ...]}.
    Só itens com >=2 pontos; mantém os últimos HISTORY_FEED_MAX. Best-effort (vazio se faltar BD)."""
    if not os.path.exists(HISTORY_DB):
        return {}
    feed = {}
    try:
        with sqlite3.connect(HISTORY_DB) as conn:
            for name, ts, v in conn.execute(
                "SELECT name, ts, COALESCE(low, med) FROM price_history "
                "WHERE currency='usd' AND COALESCE(low, med) IS NOT NULL ORDER BY name, ts"):
                feed.setdefault(name, []).append([ts, round(v, 2)])
    except sqlite3.Error:
        return {}
    return {n: pts[-HISTORY_FEED_MAX:] for n, pts in feed.items() if len(pts) >= 2}


def _box_ev_index(price_usd):
    """Por caixa (dropKey): EV em USD por caixa ABERTA + itens tradáveis que mais contribuem.
    Resolve drops.json: pool "Base" -> entries (probability) -> ITEMGROUP -> itens (uniforme no grupo)
    -> preço de mercado (só tradáveis contam $). drops.json (~1MB) fica SÓ no build (não vai ao front).
    Ressalva: ignora o roll de grade do item dropado (usa o item-base do grupo) e a `rate` não é
    normalizada -> é uma APROXIMAÇÃO do valor tradável por caixa, não um EV por kill."""
    drops = get_drops(False)
    if not drops:
        return {}
    key2name = {it.get("key"): join_key(it) for it in get_items(False)}

    def base_pool(tbl):
        for p in (tbl.get("pools") or []):
            if p.get("label") == "Base":
                return p
        return (tbl.get("pools") or [None])[0]

    idx = {}
    for tbl in drops:
        pool = base_pool(tbl)
        if not pool:
            continue
        ev, contrib = 0.0, {}
        for e in pool.get("entries", []):
            grp = e.get("items") or ([e["rewardKey"]] if e.get("rewardKey") else [])
            if not grp:
                continue
            pe = (e.get("probability") or 0) / len(grp)        # prob por item (uniforme no grupo)
            for k in grp:
                nm = key2name.get(k)
                pr = price_usd.get(nm) if nm else None
                if pr:
                    ev += pe * pr
                    contrib[nm] = contrib.get(nm, 0) + pe * pr
        if ev > 0:
            idx[tbl["dropKey"]] = (ev, sorted(contrib.items(), key=lambda x: -x[1])[:6])
    return idx


def _stages_feed(rows=None):
    """Stages (farm) compactos p/ o site público: cards + cruzamento com o mercado + EV/caixa (USD)
    e top itens tradáveis (via _box_ev_index). Best-effort (vazio se faltar cache)."""
    price_usd = {r["name"]: r["usd"] for r in (rows or []) if r.get("usd")}
    ev_idx = _box_ev_index(price_usd) if price_usd else {}
    out = []
    for s in get_stages(False):
        ev, top = 0.0, {}
        for d in (s.get("drops") or []):
            be = ev_idx.get(d.get("dropKey"))
            if be:
                ev += be[0]
                for n, v in be[1]:
                    top[n] = top.get(n, 0) + v
        out.append({
            "label": s.get("label"), "act": s.get("act"), "no": s.get("stageNo"),
            "level": s.get("level"), "name": s.get("name"),
            "type": s.get("type"), "difficulty": s.get("difficulty"),
            "boss": (s.get("boss") or {}).get("name"),
            "ev": round(ev, 2),    # EV USD/caixa (soma das caixas), só itens tradáveis
            "top": [[n, round(v, 2)] for n, v in sorted(top.items(), key=lambda x: -x[1])[:5]],
            "drops": [{"name": d.get("name"), "icon": d.get("icon"), "grade": d.get("grade"),
                       "rate": d.get("rate"), "source": d.get("source")}
                      for d in (s.get("drops") or [])],
        })
    return out


def _craft_feed(rows=None):
    """Craft (receitas `crafting`) p/ o site: cruza os MATERIAIS (reagentes) e os ITENS-RESULTADO
    com o mercado p/ responder 'vale craftar ou é melhor vender os reagentes?'. Modelo min–máx
    (piso/teto da pull) + `pWin` (chance de sair um item que vale mais que os reagentes). recipes.json
    fica SÓ no build (não vai ao front). Best-effort: vazio se faltar cache/junção.

    Ressalvas: usa preço de VENDA (priceoverview, USD) — base já cruzada nas linhas. Grades sem
    listagem (ex.: travados na reabertura, ou itens de grade baixa que ninguém anuncia) entram como
    'sem preço'; por isso muitas pulls de grade baixa aparecem sem valor de mercado, e o veredito olha
    o MELHOR resultado possível vs. o custo dos reagentes (exatamente o pedido)."""
    recipes = get_recipes(False)
    crafting = recipes.get("crafting") if isinstance(recipes, dict) else None
    if not crafting:
        return []
    price_usd = {r["name"]: r["usd"] for r in (rows or []) if r.get("usd")}
    if not price_usd:
        return []
    items = get_items(False)
    by = {it.get("key"): it for it in items}
    key2name = {it.get("key"): join_key(it) for it in items}

    def price_of(k):
        it = by.get(k)
        if not it or not it.get("tradable"):
            return None                       # intradável não tem preço de mercado
        return price_usd.get(key2name.get(k))

    out = []
    for rc in crafting:
        res = rc.get("result") or {}
        # custo dos reagentes = Σ count × preço do material (None se algum material não tem preço)
        mats, cost, cost_known = [], 0.0, True
        for m in (rc.get("materials") or []):
            it = by.get(m.get("id"))
            cnt = m.get("count", 1)
            pr = price_usd.get(key2name.get(m.get("id"))) if it else None
            mats.append({"name": (m.get("name") or {}).get("en-US") or (it or {}).get("name"),
                         "icon": (it or {}).get("icon"), "grade": m.get("grade"),
                         "count": cnt, "price": round(pr, 2) if pr else None})
            if pr is None:
                cost_known = False
            else:
                cost += cnt * pr
        cost = round(cost, 2) if cost_known else None
        odds = {o.get("grade"): o.get("pct", 0) for o in (res.get("gradeOdds") or [])}
        grades, best, floor, ceil, pwin, ev = [], None, None, None, 0.0, 0.0
        for g, keys in (res.get("itemsByGrade") or {}).items():
            pct = odds.get(g, 0)
            priced = [(k, p) for k in keys if (p := price_of(k))]
            gv = {"grade": g, "pct": pct, "n": len(priced), "ntot": len(keys),
                  "floor": None, "ceil": None, "best": None}
            if priced:
                bk, bp = max(priced, key=lambda x: x[1])
                lo = min(p for _, p in priced)
                gv["floor"], gv["ceil"] = round(lo, 2), round(bp, 2)
                gv["best"] = {"name": by[bk].get("name"), "icon": by[bk].get("icon"),
                              "mname": key2name.get(bk), "price": round(bp, 2)}
                floor = lo if floor is None else min(floor, lo)
                ceil = bp if ceil is None else max(ceil, bp)
                if best is None or bp > best[2]:
                    best = (g, bk, bp)
                # valor esperado da pull: itens sem preço (intradáveis/sem oferta) contam 0 — é o
                # valor de REVENDA médio de 1 craft, base honesta p/ comparar com vender o reagente.
                ev += pct / 100.0 * (sum(p for _, p in priced) / len(keys))
                if cost_known and cost > 0:
                    # chance de a pull dar item acima do custo: uniforme sobre TODOS os itens do grade
                    pwin += pct / 100.0 * (sum(1 for _, p in priced if p > cost) / len(keys))
            grades.append(gv)
        if not cost_known:
            verdict = "unknown"               # sem preço de material -> não dá p/ decidir
        elif not best or best[2] <= cost:
            verdict = "sell"                  # nada na pull bate o custo -> venda os reagentes
        elif ev >= cost:
            verdict = "craft"                 # positivo na média: craftar compensa
        else:
            verdict = "gamble"                # dá p/ tentar (teto > custo), mas na média perde (aposta)
        out.append({
            "type": rc.get("type"), "tier": rc.get("tier"),
            "lvl": [res.get("levelMin"), res.get("levelMax")], "distinct": res.get("distinct"),
            "mats": mats, "cost": cost, "odds": res.get("gradeOdds") or [], "grades": grades,
            "floor": round(floor, 2) if floor is not None else None,
            "ceil": round(ceil, 2) if ceil is not None else None,
            "ev": round(ev, 2) if cost_known else None,
            "best": ({"grade": best[0], "name": by[best[1]].get("name"),
                      "icon": by[best[1]].get("icon"), "mname": key2name.get(best[1]),
                      "price": round(best[2], 2)} if best else None),
            "pWin": round(pwin, 4) if cost_known else None, "verdict": verdict,
        })
    # melhores oportunidades primeiro: craft (+ margem de EV), aposta, vender, sem custo
    rank = {"craft": 0, "gamble": 1, "sell": 2, "unknown": 3}

    def margin(v):
        return (v["ev"] - v["cost"]) if (v["ev"] is not None and v["cost"] is not None) else -1.0

    out.sort(key=lambda v: (rank[v["verdict"]], -margin(v), v.get("type") or "", v.get("tier") or 0))
    return out


def write_static(rows, brl_rate, public=False):
    rows.sort(key=lambda r: r["gold"] / r["usd"] if r["usd"] else 0, reverse=True)
    write_assets()                       # assets/styles.css + assets/app.js (cacheáveis)
    if public:                           # feeds públicos (N1): o site busca; outros podem consumir
        api_dir = os.path.join(HERE, "api")
        os.makedirs(api_dir, exist_ok=True)
        with open(os.path.join(api_dir, "data.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        with open(os.path.join(api_dir, "history.json"), "w", encoding="utf-8") as f:
            json.dump(_history_feed(), f, ensure_ascii=False)
        with open(os.path.join(api_dir, "stages.json"), "w", encoding="utf-8") as f:
            json.dump(_stages_feed(rows), f, ensure_ascii=False)
        with open(os.path.join(api_dir, "craft.json"), "w", encoding="utf-8") as f:
            json.dump(_craft_feed(rows), f, ensure_ascii=False)
    out = os.path.join(HERE, "index.html")
    open(out, "w", encoding="utf-8").write(render_html(rows, brl_rate, public=public))
    extra = " + api/{data,history,stages,craft}.json (público)" if public else ""
    print(f"[ok] gerado: {out} + assets/{{styles.css,app.js}}{extra}")


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
            elif u.path == "/assets/app.js":           # asset estático (memória; sem auth)
                self._send(200, APP_JS, "application/javascript")
            elif u.path == "/assets/styles.css":
                self._send(200, CSS_CONTENT, "text/css")
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
    ap.add_argument("--orderbook-shards", type=int, default=1, metavar="N",
                    help="divide a varredura de encomendas em N fatias (paralelismo no CI)")
    ap.add_argument("--orderbook-shard", type=int, default=0, metavar="I",
                    help="índice da fatia (0..N-1) a processar quando --orderbook-shards > 1")
    ap.add_argument("--orderbook-out", default=None, metavar="PATH",
                    help="grava a coleta do shard num JSON parcial (não toca o cache compartilhado)")
    ap.add_argument("--merge-orderbook", default=None, metavar="GLOB",
                    help="consolida os parciais dos shards (ex.: 'shards/*.json') no cache + histórico")
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

    # job de merge do CI: consolida os parciais dos shards no orderbook.json ANTES do build_rows
    # (que lê o cache p/ anexar `book` às linhas) -> o site sai com as encomendas de todos os shards.
    if args.merge_orderbook:
        merge_orderbook_partials(args.merge_orderbook)

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
        enrich_orderbook(rows, args.orderbook_top, args.orderbook_shards,
                         args.orderbook_shard, args.orderbook_out)
        if args.orderbook_out:
            return  # shard: só coleta e grava o parcial; a consolidação/build é no job de merge
    mark_new_items(rows)             # selo "NOVO" p/ itens listados a partir da reabertura
    attach_game_extras(rows, items, args.refresh)  # efeitos das gemas + locais de drop (farm)
    report_join_health(rows, unmatched, steam, args.public)  # saúde da junção + alerta (CI summary)
    write_static(rows, brl_rate, args.public)
    print("\nTop 10 por gold/$ (bulk):")
    for r in rows[:10]:
        print(f"  {r['gold'] / r['usd']:>12,.0f}/$  ${r['usd']:>6.2f}  {r['gold']:>16,}  {r['name']}")


if __name__ == "__main__":
    main()
