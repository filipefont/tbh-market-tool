#!/usr/bin/env python3
"""Smoke test do build (sem dependências externas).

Garante que:
- o build PÚBLICO é somente-leitura (sem botões de atualização) e tem o rótulo de
  última atualização + meta og:image;
- o index.html gerado tem itens (DATA não-vazio);
- o modo SERVIDOR mantém os controles de atualização.

Uso: python3 tests/smoke.py   (rode da raiz do repo)
Sai com código !=0 se algo regredir.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import build  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# 1) index.html público já gerado (se existir nesta árvore)
idx = os.path.join(ROOT, "index.html")
if os.path.exists(idx):
    html = open(idx, encoding="utf-8").read()
    check("const PUBLIC = true" in html, "index.html não está em modo público (rode build.py --public)")
    check('id="refresh"' not in html, "index.html ainda expõe botão de atualização")
    check("preços atualizados" in html, "index.html sem rótulo de última atualização")
    check("og:image" in html and "__SITE__" not in html, "index.html sem meta og:image válida")
    check("let DATA = []" not in html, "index.html sem itens (DATA vazio)")

# 2) modo SERVIDOR mantém os controles
srv = build.render_html([], 5.4, token="x", public=False)
check("const PUBLIC = false" in srv, "modo servidor marcado como público")
check('id="refresh"' in srv, "modo servidor sem botão de atualização")

# 3) modo PÚBLICO não vaza placeholder nem controles
pub = build.render_html([], 5.4, public=True)
check("__SERVER_CONTROLS__" not in pub, "placeholder __SERVER_CONTROLS__ não substituído")
check('id="refresh"' not in pub, "build público vazou botão de atualização")
check("og:image" in pub and "__SITE__" not in pub, "build público sem og:image válida")

# 4) parser de ENCOMENDAS (order book) — fixture sintética com o escape duplo da página SSR.
# Protege contra regressão no regex/unescape se a Steam mexer no SSR (.spec §5.5).
# dados vêm ANTES da queryKey no payload desidratado (a âncora confirma o item)
_blob = (
    r'{\"amtMaxBuyOrder\":242,\"amtMinSellOrder\":36686,\"eCurrency\":7,'
    + r'\"cBuyOrders\":5269,\"cSellOrders\":2,'
    + r'\"rgCompactBuyOrders\":[242,100,140,10],\"rgCompactSellOrders\":[36686,1]}'
    + r'...\"queryKey\":[\"market\",\"orderbook\",%d,\"Widget A\"]...' % build.APPID)
bk = build.parse_orderbook(_blob, "Widget A")
check(bk is not None, "parse_orderbook não extraiu o order book da fixture (SSR mudou?)")
if bk:
    check(bk["buyMax"] == 2.42, f"parse_orderbook buyMax errado: {bk.get('buyMax')}")
    check(bk["buyOrders"] == 5269, f"parse_orderbook buyOrders errado: {bk.get('buyOrders')}")
    check(bk["cur"] == 7, f"parse_orderbook moeda errada: {bk.get('cur')}")
    check(bk["buyBook"][:2] == [[2.42, 100], [1.4, 10]], "parse_orderbook book de compra errado")
check(build.parse_orderbook("sem order book aqui", "Widget A") is None,
      "parse_orderbook deveria retornar None sem âncora")

# 4b) item SÓ com encomenda (sem venda): amtMinSellOrder=null e rgCompactSellOrders vazio.
# Era o caso que o regex descartava -> item nunca atualizava. Deve casar e dar sellMin=None.
_blob_null = (
    r'{\"amtMaxBuyOrder\":1297,\"amtMinSellOrder\":null,\"eCurrency\":7,'
    + r'\"cBuyOrders\":130,\"cSellOrders\":0,'
    + r'\"rgCompactBuyOrders\":[1297,1,1053,2],\"rgCompactSellOrders\":[]}'
    + r'...\"queryKey\":[\"market\",\"orderbook\",%d,\"Widget B\"]...' % build.APPID)
bn = build.parse_orderbook(_blob_null, "Widget B")
check(bn is not None, "parse_orderbook descartou item com amtMinSellOrder=null (só encomenda)")
if bn:
    check(bn["buyMax"] == 12.97, f"parse_orderbook buyMax errado (null sell): {bn.get('buyMax')}")
    check(bn["sellMin"] is None, f"parse_orderbook sellMin deveria ser None: {bn.get('sellMin')}")
    check(bn["sellOrders"] == 0, f"parse_orderbook sellOrders errado: {bn.get('sellOrders')}")
    check(build._spread_pct(bn) is None, "spread sem venda deveria ser None")

# 5) junção dos dados estendidos (effects/stages) — usa os caches versionados (offline).
# Protege a fundação: se a wiki mudar o keyspace/nomes, a cobertura cai e o teste acusa.
import os as _os  # noqa: E402
import json  # noqa: E402
if _os.path.exists(build.EFFECTS_CACHE) and _os.path.exists(build.ITEMS_CACHE):
    _items = json.load(open(build.ITEMS_CACHE, encoding="utf-8"))
    _eff = json.load(open(build.EFFECTS_CACHE, encoding="utf-8"))
    _k2n = {it.get("key"): build.join_key(it) for it in _items}
    _matched = sum(1 for e in _eff if e.get("key") in _k2n)
    _rate = _matched / len(_eff) if _eff else 0
    check(_rate >= build.EXTRAS_MATCH_MIN,
          f"cobertura de efeitos {_rate:.0%} < {build.EXTRAS_MATCH_MIN:.0%} (wiki mudou keyspace?)")
    # a junção realmente anexa effects/droppedIn às linhas?
    _gem = next((build.join_key(it) for it in _items if it.get("key") in {e.get("key") for e in _eff}), None)
    if _gem:
        _rows = [{"name": _gem}]
        build.attach_game_extras(_rows, _items, refresh=False)
        check(_rows[0].get("effects"), f"attach_game_extras não anexou efeitos em '{_gem}'")

if fails:
    print("SMOKE: FALHOU")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("SMOKE: OK")
