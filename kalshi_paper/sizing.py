"""Order size that grows with the paper bankroll. Paper only.

    contracts = floor(bankroll / 100 x contracts_per_100usd)
    then at least paper_contracts, at most max_contracts, and never more than the cash can pay for

Sizing never changes WHICH trades the bot takes: the signal and the edge check use the fee at
the base size (paper_contracts), so every rule version with the same signals takes the same
trades and only the size differs. The fee actually charged is the fee at the real size.

simulate() replays a list of trades in time order with one bankroll that compounds: a trade's
result counts from 60 s after its window closes, so each size uses only what the bot could
have known at that moment (the same timing as the live bankroll and the quarter-Kelly tracker).
"""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

from .model import taker_fee_per_contract

SETTLE_DELAY_S = 60.0


def contracts_for(balance, cost_per_contract: float, rules) -> int:
    """How many contracts to buy with this much cash at this all-in cost per contract."""
    base = rules.paper_contracts
    k = rules.contracts_per_100usd
    if k is None or balance is None:
        return base
    balance = float(balance)
    n = max(base, math.floor(balance / 100.0 * k + 1e-9))
    n = min(n, rules.max_contracts)
    if cost_per_contract > 0:
        n = min(n, max(base, math.floor(balance / cost_per_contract)))
    return n


def fee_per_contract(price: float, n: int, rules) -> float:
    return float(taker_fee_per_contract(Decimal(str(round(price, 4))), max(n, 1), Decimal(str(rules.taker_fee_rate))))


def simulate(trades: list, start: float, rules, slippage: float = 0.0, stop_below: float | None = None) -> dict:
    """Compound one bankroll through trades sorted by time.

    Each trade needs: at (decision time), close_ts, side, price, won. slippage is added to the
    price of every contract (a what-if for fills worse than the displayed ask).
    """
    from .bankroll import max_trade_cost

    stop = float(max_trade_cost(rules)) if stop_below is None else stop_below
    cash, open_, out = float(start), [], []
    peak, max_dd, curve = cash, 0.0, []

    def realize(now):
        nonlocal cash, peak, max_dd
        for tr in sorted(open_, key=lambda x: x["close_ts"]):
            if tr["close_ts"] + SETTLE_DELAY_S <= now:
                cash += tr["contracts"] if tr["won"] else 0.0
                open_.remove(tr)
                peak = max(peak, cash)
                max_dd = max(max_dd, 1 - cash / peak if peak else 0.0)
                curve.append((tr["close_ts"], round(cash, 2)))

    stopped_at = None
    for i, t in enumerate(trades):
        realize(t["at"])
        if cash < stop:
            stopped_at = t["at"]
            break
        price = t["price"] + slippage
        if price >= 1:
            continue
        guess = price + fee_per_contract(price, rules.paper_contracts, rules)
        n = contracts_for(cash, guess, rules)
        cost = n * (price + fee_per_contract(price, n, rules))
        if cost > cash:
            continue
        cash -= cost
        tr = {"i": i, "at": t["at"], "close_ts": t["close_ts"], "contracts": n, "cost": cost, "won": t["won"],
              "pnl": (n if t["won"] else 0.0) - cost}
        out.append(tr)
        open_.append(tr)
    realize(float("inf"))
    return {"start": float(start), "end": round(cash, 2), "trades": len(out), "won": sum(tr["won"] for tr in out),
            "max_drawdown": round(max_dd, 4), "max_contracts": max((tr["contracts"] for tr in out), default=0),
            "taken": out, "curve": curve, "stopped_at": stopped_at}


def rolling(trades: list, start: float, rules, days: int = 180, step_days: int = 7, slippage: float = 0.0) -> dict:
    """Runs of `days` from `start`, one starting every `step_days`: how often each run ended up, or went bust."""
    import statistics

    if not trades:
        return {"runs": 0}
    ends, busts, t0 = [], 0, trades[0]["at"]
    while t0 + days * 86400 <= trades[-1]["at"]:
        s = simulate([t for t in trades if t0 <= t["at"] < t0 + days * 86400], start, rules, slippage=slippage)
        ends.append(s["end"])
        busts += s["stopped_at"] is not None
        t0 += step_days * 86400
    if not ends:
        return {"runs": 0}
    e = sorted(ends)
    return {"runs": len(e), "days": days, "start": start, "median": round(statistics.median(e), 2),
            "worst_10pct": e[len(e) // 10], "best_10pct": e[9 * len(e) // 10],
            "bust_pct": round(100 * busts / len(e), 1), "lost_pct": round(100 * sum(x < start for x in e) / len(e), 1)}


def with_sizing(rules, contracts_per_100usd, max_contracts=None):
    """The same rules with a different sizing setting (for what-if backtests)."""
    kw = {"contracts_per_100usd": contracts_per_100usd}
    if max_contracts is not None:
        kw["max_contracts"] = max_contracts
    return replace(rules, **kw)
