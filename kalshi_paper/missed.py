"""Trades the live bot missed: python3 -m kalshi_paper missed

Replays the current rules on public 1-minute history (the backtest's method, see study.py) for
every window since this rules version started running live, then checks each backtest trade
against the live decision log:

  offline     no live check within a minute of the trade (the bot was stopped or the Mac asleep)
  warming_up  the bot was running but still warming up after a restart (it needs about 30 minutes
              of its own price data at window opens before it can price a window)
  online      the bot was checking normally; the trade shows whether it also traded that window

Backtest trades use minute bars and fills at the displayed ask, so they are a little optimistic.
"""

from __future__ import annotations

import bisect
import time
from decimal import Decimal

from . import sizing, study

WARMUP_REASONS = {"insufficient_basis_history", "insufficient_history"}
NEAR_S = 60          # a live check within this many seconds of the trade's minute counts as watching it
OFFLINE_GAP_S = 120  # no check for this long counts as an offline period
LEAD_S = 3 * 3600    # history before the start: volatility and the strike-gap average need it


def offline_periods(times: list, now: float, gap_s: float = OFFLINE_GAP_S) -> list:
    """[start, end] stretches with no live check for at least gap_s, including the one running now."""
    out = [[a, b] for a, b in zip(times, times[1:]) if b - a >= gap_s]
    if times and now - times[-1] >= gap_s:
        out.append([times[-1], now])
    return out


def _live_state(times: list, reasons: list, t: float) -> str:
    lo, hi = bisect.bisect_left(times, t - NEAR_S), bisect.bisect_right(times, t + NEAR_S)
    if lo == hi:
        return "offline"
    return "warming_up" if all(reasons[i] in WARMUP_REASONS for i in range(lo, hi)) else "online"


def _book(trades: list) -> dict:
    settled = [t for t in trades if t["won"] is not None]
    return {"trades": len(trades), "won": sum(1 for t in settled if t["won"]),
            "pnl": round(sum(t["pnl"] for t in settled), 2)}


def report(decisions: list, windows: list, spot: dict, rules, now: float | None = None,
           start_usd: float = 300.0) -> dict:
    """decisions: this rules version's live log, oldest first. windows/spot: the history cache.

    start_usd: the paper bankroll's starting amount, which sizes the backtest trades."""
    now = time.time() if now is None else now
    live = sorted((d for d in decisions if d.get("evaluated_at") is not None), key=lambda d: d["evaluated_at"])
    if not live:
        return {"version": rules.version, "generated_at": now, "since": None}
    since = live[0]["evaluated_at"]
    times, reasons = [d["evaluated_at"] for d in live], [d.get("reason") for d in live]
    by_ticker = {w["ticker"]: w for w in windows}
    recent = [w for w in windows if w["close_ts"] > since - LEAD_S]
    in_range = [w for w in recent if w["close_ts"] > since]
    through = max((w["close_ts"] for w in in_range), default=None)

    rows = study.observations(recent, spot, rules)
    trades = []
    for tr in study.paper_trades(rows, rules):
        w = by_ticker[tr["ticker"]]
        at = w["open_ts"] + 60 * tr["minute"]
        if at < since:
            continue
        trades.append({"ticker": tr["ticker"], "at": at, "close_ts": w["close_ts"], "side": tr["side"],
                       "price": tr["price"], "won": tr["won"], "live": _live_state(times, reasons, at)})
    # Size each backtest trade as the bot would have if it had run the whole time: one bankroll
    # from the paper account's starting amount, compounding (see sizing.py).
    trades.sort(key=lambda t: t["at"])
    sim = sizing.simulate(trades, start_usd, rules)
    taken = {x["i"]: x for x in sim["taken"]}
    for i, t in enumerate(trades):
        x = taken.get(i)
        t["contracts"], t["pnl"] = (x["contracts"], round(x["pnl"], 2)) if x else (0, 0.0)
    n = (f"{rules.contracts_per_100usd:g} per $100 of bankroll" if rules.contracts_per_100usd
         else rules.paper_contracts)

    live_acts = {}
    for d in live:
        if d.get("decision") == "ACT" and d.get("intent") and d["market_ticker"] not in live_acts:
            live_acts[d["market_ticker"]] = d
    live_trades = []
    for ticker, d in live_acts.items():
        i, w = d["intent"], by_ticker.get(ticker)
        won = None if w is None else (w["result"] == "yes") == (i["side"] == "YES")
        cost = float(Decimal(i["limit_price"]) + Decimal(i["fee_per_contract"])) * i["contracts"]
        live_trades.append({"ticker": ticker, "at": d["evaluated_at"], "side": i["side"], "price": float(i["limit_price"]),
                            "contracts": i["contracts"], "won": won,
                            "pnl": None if won is None else round((i["contracts"] if won else 0) - cost, 2)})
    for t in trades:
        a = live_acts.get(t["ticker"])
        t["live_traded"] = a["intent"]["side"] if a else None

    # Window coverage: how many of a window's tradable minutes the bot was checking normally.
    coverage = {"watched": 0, "partly": 0, "missed": 0}
    for w in in_range:
        mins = [w["open_ts"] + 60 * m for m in range(1, 15) if w["close_ts"] - (w["open_ts"] + 60 * m) >= rules.min_seconds_left]
        on = sum(1 for t in mins if _live_state(times, reasons, t) == "online")
        coverage["watched" if on == len(mins) else "partly" if on else "missed"] += 1
    gaps = offline_periods(times, now)
    span = now - since
    offline_s = sum(b - a for a, b in gaps)
    return {
        "version": rules.version, "generated_at": now, "since": since, "through": through,
        "contracts": n, "start_usd": start_usd, "windows": len(in_range), "coverage": coverage,
        "online_pct": round(100 * (1 - offline_s / span), 1) if span > 0 else None,
        "offline_s": round(offline_s), "offline_count": len(gaps), "offline_periods": gaps[-200:],
        "backtest": {"all": _book(trades), **{k: _book([t for t in trades if t["live"] == k])
                                              for k in ("offline", "warming_up", "online")}},
        "live": _book(live_trades),
        "trades": trades, "live_trades": live_trades,
    }


def _money(v):
    return f"{'-' if v < 0 else '+'}${abs(v):.2f}"


def _when(ts):
    return time.strftime("%b %d %H:%M", time.localtime(ts))


def format_report(r: dict) -> str:
    if not r.get("since"):
        return f"no live decisions for {r['version']} yet"
    b, c = r["backtest"], r["coverage"]
    hours = (r["generated_at"] - r["since"]) / 3600
    lines = [f"Rules {r['version']} live since {_when(r['since'])} ({hours:.1f} h); history through "
             f"{_when(r['through']) if r['through'] else '-'} (local time)",
             f"Bot running {r['online_pct']}% of the time; {r['offline_count']} offline stretch(es), "
             f"{r['offline_s'] / 3600:.1f} h in total",
             f"Windows: {c['watched']} watched, {c['partly']} partly, {c['missed']} missed (of {r['windows']})", "",
             f"Backtest of {r['version']} on those windows ({r['contracts']} contracts, minute bars):"]
    for k, label in (("all", "all trades"), ("offline", "  bot offline"), ("warming_up", "  bot warming up"),
                     ("online", "  bot online")):
        x = b[k]
        lines.append(f"  {label:18} {x['trades']:>4} trades {x['won']:>4} won  {_money(x['pnl']):>10}")
    lv = r["live"]
    lines += [f"Live paper trades:   {lv['trades']:>4} trades {lv['won']:>4} won  {_money(lv['pnl']):>10}", "",
              "Missed trades (bot offline or warming up):"]
    for t in r["trades"]:
        if t["live"] != "online":
            res = "won" if t["won"] else "lost"
            lines.append(f"  {_when(t['at'])}  {t['side']:3} x{t['contracts']:<4} at {round(t['price'] * 100):>2}c  {res:4} "
                         f"{_money(t['pnl']):>8}  ({t['live'].replace('_', ' ')})")
    lines += ["", "Backtest fills assume the displayed price at one look per minute, so they run a little optimistic."]
    return "\n".join(lines)
