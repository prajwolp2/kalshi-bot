"""Slippage tracker: what the paper trades would have made as real orders. Paper only.

A paper trade assumes it bought at the ask displayed when the bot decided. A real order would
land a moment later, and these markets move several cents in seconds. For every paper trade,
this looks at the bot's own next checks of the same market (one every 2 s) and reprices the
trade five ways:

  shown      the ask displayed at decision time (what the paper bankroll assumes)
  market_2s  a market order landing at the next check, about 2 s later: pays that check's ask
  market_5s  the same, landing about 5 s later (a slow connection)
  limit      a limit order at the shown price: fills at the next check's ask if that ask is at or
             below the shown price; otherwise it misses and the trade never happens
  limit_2c   a limit order 2c above the shown price: fills at the next check's ask if within 2c

The next check stands in for when a real order would arrive: the bot's Kalshi data is already
up to a few seconds old when it decides, plus the time an order takes to reach Kalshi.

If fewer contracts than the order were displayed at the fill price, the rest is priced one cent
(one tick) higher. The decision log keeps only the best price level, so that part is approximate.
Fees are recomputed at each fill price for the whole order.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

from .model import taker_fee_per_contract

MODELS = ("shown", "market_2s", "market_5s", "limit", "limit_2c")
LABELS = {"shown": "Shown price (paper)", "market_2s": "Market order, ~2 s later",
          "market_5s": "Market order, ~5 s later", "limit": "Limit at shown price",
          "limit_2c": "Limit up to 2¢ above"}
NEXT_S = (0.5, 6.0)    # the next check: at least 0.5 s and at most 6 s after the decision
LATE_S = (5.0, 15.0)   # the first check at least 5 s after, if within 15 s


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def quote(d: dict):
    """(time, yes_ask, no_ask, contracts at yes_ask, contracts at no_ask) from a logged decision, or None."""
    f = d.get("features")
    if not f or d.get("evaluated_at") is None:
        return None
    row = (float(d["evaluated_at"]), _num(f.get("yes_ask")), _num(f.get("no_ask")),
           _num(f.get("best_no_bid_size")), _num(f.get("best_yes_bid_size")))
    return row if None not in row else None


def act(d: dict, version: str):
    """A paper trade from a logged ACT decision, or None."""
    i = d.get("intent")
    if d.get("decision") != "ACT" or not i or d.get("evaluated_at") is None:
        return None
    return {"at": float(d["evaluated_at"]), "ticker": d.get("market_ticker"), "close_time": d.get("close_time"),
            "version": version, "side": i["side"], "price": float(i["limit_price"]), "contracts": int(i["contracts"]),
            "shown_size": _num(i.get("displayed_size_at_price"))}


def _after(quotes, t, lo, hi):
    for q in quotes:
        if lo <= q[0] - t <= hi:
            return q
        if q[0] - t > hi:
            return None
    return None


def _fill_price(ask: float, size: float, n: int) -> float:
    """Average price for n contracts when only `size` are displayed at `ask` (the rest one tick higher)."""
    if size >= n:
        return ask
    return (size * ask + (n - size) * min(0.99, ask + 0.01)) / n


def _pnl(price: float, n: int, won: bool | None):
    fee = float(taker_fee_per_contract(Decimal(str(round(price, 4))), n))
    if won is None:
        return None, fee
    return round(n * ((1.0 if won else 0.0) - price - fee), 2), fee


def reprice(trade: dict, quotes: list, result: str | None, contracts: int | None = None) -> dict:
    """One paper trade under each order model. quotes: this market's (time, yes_ask, no_ask, ...) rows, oldest first."""
    n = contracts or trade["contracts"]
    yes = trade["side"] == "YES"
    won = None if result is None else (result == "yes") == yes
    t, shown = trade["at"], trade["price"]
    nxt, late = _after(quotes, t, *NEXT_S), _after(quotes, t, *LATE_S)
    side = (lambda q: (q[1], q[3]) if yes else (q[2], q[4]))
    fills = {"shown": _fill_price(shown, trade["shown_size"] if trade["shown_size"] is not None else n, n)}
    if nxt:
        ask, size = side(nxt)
        fills["market_2s"] = _fill_price(ask, size, n)
        fills["limit"] = _fill_price(ask, size, n) if ask <= shown + 1e-9 else None
        fills["limit_2c"] = _fill_price(ask, size, n) if ask <= shown + 0.02 + 1e-9 else None
    if late:
        ask, size = side(late)
        fills["market_5s"] = _fill_price(ask, size, n)
    out = {**trade, "contracts": n, "result": result, "won": won,
           "next_ask": side(nxt)[0] if nxt else None, "next_after_s": round(nxt[0] - t, 1) if nxt else None,
           "late_ask": side(late)[0] if late else None, "late_after_s": round(late[0] - t, 1) if late else None,
           "models": {}}
    for m in MODELS:
        if m not in fills:
            out["models"][m] = {"measured": False}
        elif fills[m] is None:
            out["models"][m] = {"measured": True, "filled": False, "pnl": 0.0 if won is not None else None}
        else:
            pnl, fee = _pnl(fills[m], n, won)
            out["models"][m] = {"measured": True, "filled": True, "price": round(fills[m], 4), "fee": fee, "pnl": pnl}
    return out


def summarize(rows: list) -> dict:
    """Totals per order model over trades that are settled and measured under every model."""
    full = [r for r in rows if r["won"] is not None and all(r["models"][m]["measured"] for m in MODELS)]
    out = {"trades": len(rows), "compared": len(full), "models": {}}
    for m in MODELS:
        filled = [r for r in full if r["models"][m]["filled"]]
        extra = [r["models"][m]["price"] + r["models"][m]["fee"] - r["price"] - r["models"]["shown"]["fee"] for r in filled]
        out["models"][m] = {
            "label": LABELS[m], "filled": len(filled), "missed": len(full) - len(filled),
            "won": sum(1 for r in filled if r["won"]),
            "pnl": round(sum(r["models"][m]["pnl"] for r in full), 2),
            "extra_cents_per_contract": round(100 * sum(extra) / len(extra), 2) if extra else None,
            "missed_winners": sum(1 for r in full if not r["models"][m]["filled"] and r["won"]),
        }
    moved = [(r["won"], 100 * (r["next_ask"] - r["price"])) for r in rows if r["next_ask"] is not None and r["won"] is not None]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    out["next_check_move_cents"] = {"all": avg([c for _, c in moved]), "winners": avg([c for w, c in moved if w]),
                                    "losers": avg([c for w, c in moved if not w]),
                                    "worse": sum(1 for _, c in moved if c > 0.5), "same": sum(1 for _, c in moved if abs(c) <= 0.5),
                                    "better": sum(1 for _, c in moved if c < -0.5)}
    return out


class Tracker:
    """Reads every decisions-<version>.jsonl in a folder incrementally, keeping only quotes and trades."""

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.offsets: dict = {}
        self.quotes: dict = {}   # ticker -> [(t, yes_ask, no_ask, yes_size, no_size)]
        self.acts: list = []
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            for path in sorted(self.dir.glob("decisions-v*.jsonl")):
                version = path.stem.removeprefix("decisions-")
                off = self.offsets.get(path, 0)
                with open(path, "rb") as f:
                    f.seek(off)
                    chunk = f.read()
                end = chunk.rfind(b"\n") + 1
                for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    q = quote(d)
                    if q:
                        self.quotes.setdefault(d.get("market_ticker"), []).append(q)
                    a = act(d, version)
                    if a:
                        self.acts.append(a)
                self.offsets[path] = off + end

    def report(self, settlements: dict, contracts: int | None = None) -> dict:
        self.refresh()
        with self._lock:
            acts = sorted(self.acts, key=lambda a: a["at"])
            rows = [reprice(a, sorted(self.quotes.get(a["ticker"], [])), (settlements.get(a["ticker"]) or {}).get("result"),
                            contracts) for a in acts]
        return {"contracts": contracts, "summary": summarize(rows), "trades": rows}


def format_report(rep: dict) -> str:
    s = rep["summary"]
    size = f"{rep['contracts']} contracts per order" if rep["contracts"] else "each trade's own size"
    lines = [f"Slippage tracker - {s['trades']} paper trade(s), {s['compared']} settled and measured ({size})", ""]
    mv = s["next_check_move_cents"]
    if mv["all"] is not None:
        lines += [f"Ask at the next check (~2 s later) vs the price shown: {mv['all']:+.2f}c on average "
                  f"({mv['worse']} worse, {mv['same']} same, {mv['better']} better)",
                  f"  on trades that won {mv['winners'] if mv['winners'] is not None else '-'}c, "
                  f"on trades that lost {mv['losers'] if mv['losers'] is not None else '-'}c", ""]
    lines.append(f"{'order type':28} {'filled':>7} {'missed':>7} {'won':>5} {'P&L':>10} {'extra cost':>12}")
    for m in MODELS:
        x = s["models"][m]
        extra = f"{x['extra_cents_per_contract']:+.2f}c/ct" if x["extra_cents_per_contract"] is not None else "-"
        lines.append(f"{x['label']:28} {x['filled']:>7} {x['missed']:>7} {x['won']:>5} "
                     f"{'-' if x['pnl'] < 0 else '+'}${abs(x['pnl']):>8.2f} {extra:>12}")
    lines += ["", "Missed limit orders count as $0. Contracts beyond the displayed size are priced 1c higher (approximate)."]
    return "\n".join(lines)
