"""Quarter-Kelly paper tracker, run alongside the main rules. Paper only.

Since v0.3.0 it trades only when the main rules ACT (same side, same price), and sizes that
trade by risk instead of a fixed 10 contracts. (Through v0.2.0 it bought any edge of 1c or more
with no trend filter; on the full history that took $100 to $2.33 by February 2026.) Size:

    Kelly fraction for buying at all-in cost c with win chance p:  (p - c) / (1 - c)
    risk this trade = min(5%, quarter of that) of the cash on hand

Results are realized when the window's settlement is on file, no earlier than 60 s after the
close, so the bankroll at each trade is only what the bot could have known at that moment.
Deterministic: the same decisions and settlements always give the same trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .model import taker_fee_per_contract
from .timeutil import parse_iso_utc


@dataclass(frozen=True)
class KellyParams:
    fraction: float = 0.25        # quarter Kelly: the model's probabilities are no better than Kalshi's
    cap: float = 0.05             # never risk more than 5% of cash on one trade
    min_edge: float = 0.01        # at least 1c after fees at the Kelly size (the main rules already need 2c)
    follow_rules: bool = True     # only the main rules' ACT trades (v0.3.0); False = any edge, either side
    max_contracts: int = 200      # liquidity limit: never more than this many contracts in one order
    settle_delay_s: float = 60.0  # results count from 60 s after the window closes
    floor_usd: float = 1.0        # stop trading once cash is below this


PARAMS = KellyParams()


def path_for(data_dir, rules) -> Path:
    return Path(data_dir) / f"kelly-{rules.version}.json"


def _fee_pc(price: float, n: int, rules) -> float:
    return float(taker_fee_per_contract(Decimal(str(round(price, 4))), max(n, 1), Decimal(str(rules.taker_fee_rate))))


def simulate(decisions, settlements: dict, acct: dict, rules, params: KellyParams = PARAMS) -> dict:
    start = float(acct["start_usd"])
    cash, trades, open_, taken = start, [], [], set()

    def realize(now):
        nonlocal cash
        for tr in list(open_):
            s = settlements.get(tr["ticker"])
            if s and tr["close_ts"] + params.settle_delay_s <= now:
                won = (tr["side"] == "YES") == (s["result"] == "yes")
                tr["status"], tr["pnl"] = ("won" if won else "lost"), round((tr["contracts"] if won else 0) - tr["cost"], 2)
                cash += tr["contracts"] if won else 0
                open_.remove(tr)

    for d in decisions:
        t = d.get("evaluated_at") or 0
        if t < acct["start_ts"]:
            continue
        realize(t)
        f, ticker = d.get("features"), d.get("market_ticker")
        if not f or not ticker or ticker in taken or cash < params.floor_usd:
            continue
        if params.follow_rules and d.get("decision") != "ACT":
            continue
        only = (d.get("intent") or {}).get("side") if params.follow_rules else None
        try:
            mid, spread, p = float(f["yes_mid"]), float(f["spread"]), float(f["p_fair"])
            sides = (("YES", p, float(f["yes_ask"]), float(f["best_no_bid_size"])),
                     ("NO", 1 - p, float(f["no_ask"]), float(f["best_yes_bid_size"])))
            close_ts = parse_iso_utc(d["close_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (rules.min_yes_mid <= mid <= rules.max_yes_mid) or round(spread, 4) > rules.max_spread:
            continue
        best = None
        for side, prob, price, depth in sides:
            if only and side != only:
                continue
            c = price + _fee_pc(price, rules.paper_contracts, rules)
            if prob - c >= params.min_edge and (best is None or prob - c > best[0]):
                best = (prob - c, side, price, prob, c, depth)
        if not best:
            continue
        edge, side, price, prob, c, depth = best
        risk = min(params.cap, params.fraction * edge / (1 - c))
        n = min(int(risk * cash / c), int(depth), params.max_contracts)
        if n < 1:
            continue
        cost = n * (price + _fee_pc(price, n, rules))
        if cost > cash:
            continue
        tr = {"at": t, "ticker": ticker, "close_time": d["close_time"], "close_ts": close_ts, "side": side,
              "price": price, "contracts": n, "cost": round(cost, 2), "win_chance": round(prob, 4),
              "edge": round(edge, 4), "risk_pct": round(100 * cost / cash, 2), "status": "open", "pnl": None}
        cash -= cost
        trades.append(tr)
        open_.append(tr)
        taken.add(ticker)
    realize(float("inf"))
    settled = [tr for tr in trades if tr["status"] != "open"]
    return {"start": start, "balance": round(cash, 2),
            "settled_pnl": round(sum(tr["pnl"] for tr in settled), 2),
            "open_cost": round(sum(tr["cost"] for tr in trades if tr["status"] == "open"), 2),
            "trades": trades, "settled": len(settled), "wins": sum(tr["status"] == "won" for tr in settled)}


def describe(st: dict) -> str:
    sign = "-" if st["settled_pnl"] < 0 else "+"
    line = (f"quarter-Kelly tracker ${st['balance']:.2f} of ${st['start']:.2f} "
            f"(settled {sign}${abs(st['settled_pnl']):.2f} over {st['settled']} trade(s), {st['wins']} won")
    if st["open_cost"]:
        line += f"; ${st['open_cost']:.2f} in an open trade"
    return line + ")"
