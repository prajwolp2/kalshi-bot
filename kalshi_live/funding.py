"""Move your own cash to the exchange shard the bot trades on:  deploy/kb fund-shard --amount 200

Kalshi holds cash per exchange shard, and API orders only use the cash on the market's shard
(KXBTC15M: shard 2). The website moves money between shards for its own orders; for the bot you
move it once with this command. The money stays in your Kalshi account.

You run this yourself; it asks you to type "yes" first (unless --yes). The trading loop never
moves money.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

from kalshi_paper.storage import append_jsonl
from kalshi_paper.timeutil import iso_utc

from .api import ApiError, NetworkError


def fund_shard(client, store, amount, from_shard: int = 0, to_shard: int | None = None, confirm=input,
               log=print, sleep=time.sleep, clock=time.time, wait_s: int = 20) -> bool:
    total, shards = client.balances()
    if not shards:
        log("Kalshi didn't send a balance per shard, so there is nothing to move.")
        return False
    if to_shard is None:
        to_shard = client.market_shard()
        if to_shard is None:
            log("Kalshi didn't say which shard KXBTC15M trades on; pass --to (crypto is 2).")
            return False
    have, there = shards.get(from_shard, Decimal(0)), shards.get(to_shard, Decimal(0))
    log("cash by exchange shard: " + ", ".join(f"{k}: ${v:.2f}" for k, v in sorted(shards.items())))
    if str(amount).lower() == "all":
        amount = have.quantize(Decimal("0.01"), rounding="ROUND_DOWN")
    amount = Decimal(str(amount))
    if amount <= 0:
        log(f"nothing to move: shard {from_shard} has ${have:.2f}")
        return False
    if amount > have:
        log(f"can't move ${amount:.2f}: shard {from_shard} only has ${have:.2f}")
        return False
    log(f"\nMove ${amount:.2f} from shard {from_shard} (${have:.2f}) to shard {to_shard} (${there:.2f}).")
    log("The money stays in your Kalshi account; only the shard it sits on changes.")
    if confirm('Type "yes" to move it: ').strip().lower() != "yes":
        log("not moved")
        return False
    tid = str(uuid.uuid4())
    rec = {"at": clock(), "at_iso": iso_utc(clock()), "env": client.env, "amount_usd": str(amount),
           "from_shard": from_shard, "to_shard": to_shard, "client_transfer_id": tid}
    try:
        r = client.move_between_shards(amount, from_shard, to_shard, tid)
    except (ApiError, NetworkError, ValueError) as e:
        rec.update(status="failed", error=str(e)[:300])
        append_jsonl(store.dir / f"transfers-{client.env}.jsonl", rec)
        log(f"Kalshi didn't accept the transfer: {e}")
        return False
    rec.update(status="accepted", transfer_id=r.get("transfer_id"))
    append_jsonl(store.dir / f"transfers-{client.env}.jsonl", rec)
    log(f"accepted (transfer {r.get('transfer_id')}); waiting for it to land...")
    for _ in range(wait_s):
        sleep(1)
        try:
            _, after = client.balances()
        except (ApiError, NetworkError, KeyError, ValueError):
            continue
        if after.get(to_shard, Decimal(0)) >= there + amount - Decimal("0.01"):
            log("done. cash by exchange shard: " + ", ".join(f"{k}: ${v:.2f}" for k, v in sorted(after.items())))
            return True
    log("Kalshi accepted it but the balance hasn't changed yet; check again in a minute with: deploy/kb status")
    return True
