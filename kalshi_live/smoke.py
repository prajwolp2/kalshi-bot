"""Exchange check before any real trading: python3 -m kalshi_live smoke --env demo|live

1. reads the balance (the key and signing work)
2. finds the open KXBTC15M market on that exchange
3. places a 1-contract BUY YES at $0.01, reads it back, cancels it
4. places a 1-contract BUY NO at $0.01, reads it back, cancels it

Steps 3-4 check the V2 order mapping: Kalshi must record outcome_side yes/no and a 1-cent price
for that side. At 1 cent nothing should fill; if one does, it costs about a cent. Both orders
also expire by themselves after 2 minutes in case a cancel fails. `run` with real orders refuses
to start until this has passed with the same key.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal

from kalshi_paper.bankroll import max_trade_cost
from kalshi_paper.rules import RULES
from kalshi_paper.timeutil import iso_utc, parse_iso_utc

from .api import SERIES, ApiError, NetworkError

PENNY = Decimal("0.01")


def smoke(client, store, log=print, clock=time.time) -> bool:
    checks, ok = [], True

    def check(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        checks.append({"check": name, "passed": passed, "detail": detail})
        log(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))

    log(f"Kalshi {client.env} exchange check ({client.base})")
    try:
        bal, shards = client.balances()
        check("signed request (balance)", True, f"cash ${bal:.2f}"
              + (" (" + ", ".join(f"shard {k}: ${v:.2f}" for k, v in sorted(shards.items())) + ")" if shards else ""))
    except (ApiError, NetworkError, KeyError, ValueError) as e:
        check("signed request (balance)", False, str(e)[:300])
        return _save(store, client, checks, False, clock)

    now = clock()
    try:
        markets = [m for m in client.open_markets(SERIES)
                   if m.get("ticker", "").startswith(SERIES + "-") and m.get("close_time")
                   and parse_iso_utc(m["close_time"]) - now > 120]
    except (ApiError, NetworkError, ValueError) as e:
        check("find an open market", False, str(e)[:300])
        return _save(store, client, checks, False, clock)
    if not markets:
        check("find an open market", False, f"no open {SERIES} market with 2+ minutes left; try again in a minute")
        return _save(store, client, checks, False, clock)
    market = min(markets, key=lambda m: parse_iso_utc(m["close_time"]))
    ticker = market["ticker"]
    check("find an open market", True, ticker)
    shard = market.get("exchange_index")
    if shard is not None and shards:
        need = max_trade_cost(RULES)
        have = shards.get(int(shard), Decimal(0))
        check(f"cash on exchange shard {shard}, where {SERIES} trades", have >= need,
              f"${have:.2f}" + ("" if have >= need else
                                f"; one trade needs up to ${need:.2f}. API orders only use this shard's cash: "
                                "move funds to it on kalshi.com (Exchange Indexes)"))

    for side in ("YES", "NO"):
        name = f"BUY {side} 1 @ $0.01 recorded correctly"
        cid = f"kb-smoke-{uuid.uuid4().hex}"
        try:
            r = client.buy(ticker, side, PENNY, 1, cid, time_in_force="good_till_canceled",
                           expiration_ts=int(clock()) + 120)
            order_id = r["order_id"]
        except (ApiError, NetworkError, KeyError) as e:
            check(name, False, str(e)[:300])
            continue
        o = None
        try:
            o = client.find_order(order_id, ticker, cid)
        except (ApiError, NetworkError) as e:
            check(name, False, str(e)[:300])
        if o is not None:
            s = side.lower()
            price = o.get(f"{s}_price_dollars")
            good = (o.get("ticker") == ticker and o.get("outcome_side") == s
                    and price not in (None, "") and Decimal(str(price)) == PENNY)
            check(name, good, f"Kalshi recorded outcome_side={o.get('outcome_side')} {s}_price={price} "
                              f"status={o.get('status')} filled={o.get('fill_count_fp')}")
        # Cancel unless Kalshi says it's already done; also when the order couldn't be read back.
        if o is None or o.get("status") not in ("canceled", "executed"):
            try:
                client.cancel(order_id, ticker)
                check(f"cancel the {side} order", True)
            except (ApiError, NetworkError) as e:
                check(f"cancel the {side} order", False, f"{e} (it expires by itself within 2 minutes)")
    return _save(store, client, checks, ok, clock)


def _save(store, client, checks, ok, clock) -> bool:
    now = clock()
    store.smoke_path.write_text(json.dumps({"passed": ok, "at": now, "at_iso": iso_utc(now), "env": client.env,
                                            "key_id": client.signer.key_id, "checks": checks}, indent=2))
    return ok
