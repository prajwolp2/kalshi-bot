"""A paper bankroll for live runs: start with a fixed amount, stop before it can go below zero.

Paper only: no money moves anywhere. The balance is the starting amount, plus the P&L of
settled paper intents, minus the cost of paper intents still waiting to settle. The run
stops once the balance can't cover one more worst-case paper trade.

The account is saved in data/live/bankroll-<version>.json, so a restart continues it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .model import taker_fee_per_contract
from .scorer import _pnl


def path_for(data_dir, rules) -> Path:
    return Path(data_dir) / f"bankroll-{rules.version}.json"


def start_or_load(path: Path, amount: float, now: float) -> dict:
    """The saved account if there is one (its starting amount wins), else a new one starting now."""
    if path.exists():
        return json.loads(path.read_text())
    acct = {"start_ts": now, "start_usd": f"{Decimal(str(amount)):.2f}", "note": "PAPER bankroll, no real money"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acct, indent=2))
    return acct


def max_trade_cost(rules) -> Decimal:
    """Most one paper trade can cost: every contract at the top of the price band plus the spread, plus the fee."""
    price = Decimal(str(rules.max_yes_mid)) + Decimal(str(rules.max_spread))
    c = rules.paper_contracts
    return price * c + taker_fee_per_contract(price, c, Decimal(str(rules.taker_fee_rate))) * c


def counted(decisions, acct: dict) -> list:
    """The ACT decisions this account covers: those made since it started."""
    return [d for d in decisions if d.get("decision") == "ACT" and (d.get("evaluated_at") or 0) >= acct["start_ts"]]


def status(acts: list, settlements: dict, acct: dict) -> dict:
    start = Decimal(acct["start_usd"])
    settled_pnl, open_cost, wins, settled = Decimal(0), Decimal(0), 0, 0
    for d in acts:
        i = d["intent"]
        price, fee, c = Decimal(i["limit_price"]), Decimal(i["fee_per_contract"]), i["contracts"]
        s = settlements.get(d["market_ticker"])
        if s:
            per, win = _pnl(i["side"], price, fee, s["result"])
            settled_pnl += per * c
            wins += win
            settled += 1
        else:
            open_cost += (price + fee) * c
    return {"start": start, "settled_pnl": settled_pnl, "open_cost": open_cost,
            "balance": start + settled_pnl - open_cost, "trades": len(acts), "settled": settled, "wins": wins}


def describe(st: dict) -> str:
    sign = "-" if st["settled_pnl"] < 0 else "+"
    line = (f"paper bankroll ${st['balance']:.2f} of ${st['start']:.2f} "
            f"(settled {sign}${abs(st['settled_pnl']):.2f} over {st['settled']} trade(s), {st['wins']} won")
    if st["open_cost"]:
        line += f"; ${st['open_cost']:.2f} in a trade waiting to settle"
    return line + ")"
