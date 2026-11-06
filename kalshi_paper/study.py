"""Study the current rules on public 1-minute history: python3 -m kalshi_paper study

Rebuilds what the bot would have seen at the end of each minute of every cached window
(see history.py), with the same formulas as the live bot, and reports the most recent
day(s) and the days before them side by side:

  - calibration: the bot's fair value vs Kalshi's own mid (Brier score, lower is better)
  - trends: after a confirmed 3-minute move, did the result follow through beyond Kalshi's price?
  - the strategy and its no-trend-filter shadow, as paper trades on minute bars

Minute bars are coarse: one look per minute instead of every 5 s, Coinbase alone instead of
three exchanges (with Coinbase's own gap to the strike), and fills assumed at the displayed
ask. Use this to check that a pattern holds in more than one period and to compare rule
versions, not to forecast profit.
"""

from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from statistics import mean

from .evaluator import move_already_extended
from .model import fair_probability, momentum, settlement_basis, sigma_per_second, taker_fee_per_contract

BOOTSTRAP = 200
CAVEATS = [
    "Minute bars: one look per minute (the live bot looks every 2 s), Coinbase only (live uses a "
    "3-exchange median), and paper fills at the displayed ask with no slippage or size limit.",
    "Minutes inside one window are strongly correlated; the 95% ranges resample whole windows.",
    "'SD from zero edge' compares paper P&L with what paying exactly fair value would give (minus fees).",
    "Quarter Kelly compounds: the book's real depth (and slippage on large orders) is unknown here, so "
    "each trade is capped at the tracker's max_contracts; large late-period bankrolls are the least reliable part.",
]


def _spot_at(spot: dict, t: int):
    """Coinbase close of the minute starting at t, forward-filling up to two missing minutes."""
    for back in range(3):
        v = spot.get(t - 60 * back)
        if v is not None:
            return v
    return None


def observations(windows: list, spot: dict, rules) -> list:
    """One row per window-minute the rules would have evaluated, oldest first."""
    study_rules = replace(rules, basis_min_points=2)  # minute bars give 2 points per open, live gives ~6
    basis_log, rows = [], []
    recent_rows = max(rules.basis_windows, int(rules.basis_max_age_s // 900)) + 4
    for w in sorted(windows, key=lambda w: w["open_ts"]):
        o, c, strike = w["open_ts"], w["close_ts"], w["strike"]
        a, b = _spot_at(spot, o - 120), _spot_at(spot, o - 60)
        basis_log.append([o, w["ticker"], strike, (a + b) / 2 if a and b else None, 2 if a and b else 0])
        quotes = {q[0]: (q[1], q[2]) for q in w.get("candles") or []}
        y = 1 if w["result"] == "yes" else 0
        for m in range(1, 15):
            t = o + 60 * m
            left = c - t
            if left < rules.min_seconds_left:
                break
            bid, ask = quotes.get(t, (None, None))
            closes = [_spot_at(spot, t - 60 - 60 * j) for j in range(rules.vol_lookback_minutes, -1, -1)]
            if bid is None or not (0 < bid < ask < 1) or any(x is None for x in closes):
                continue
            # Only rows from the last basis_max_age_s can count, and windows open 15 minutes apart, so
            # the tail of the log gives the same answer as the whole log without rescanning months of it.
            gap = settlement_basis(basis_log[-recent_rows:], t, study_rules)
            sig = sigma_per_second(closes)
            if gap is None or not (sig > 0 and math.isfinite(sig)):
                continue
            s = closes[-1]
            p, _, _ = fair_probability(s + gap[0], strike, sig * rules.vol_multiplier, left,
                                       rules.settlement_avg_seconds)
            hist = [[t - 60 * k, closes[-1 - k], closes[-1 - k], closes[-1 - k]] for k in (5, 3, 1)]
            mom = momentum(hist, t, s, sig, rules)
            rows.append({"ticker": w["ticker"], "close_ts": c, "minute": m, "y": y, "p": p,
                         "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "basis": gap[0],
                         "has_momentum": mom is not None, "trend": mom["trend"] if mom else None,
                         # diagnostics only (no rule reads these): volatility, move size, 1-hour drift
                         "sigma": sig, "z": mom["z"] if mom else None, "spot": s,
                         "ret_60m": math.log(s / closes[0]), "seconds_left": left})
    return rows


def paper_trades(rows: list, rules, trend_filter: bool = True, contracts: int | None = None) -> list:
    """First trigger per window under the rules (or the shadow without the trend filter).

    contracts overrides the rules' order size for what-if sizing; the fee is rounded per order, so it changes slightly.
    """
    by_window = defaultdict(list)
    for r in rows:
        by_window[r["ticker"]].append(r)
    rate, n = Decimal(str(rules.taker_fee_rate)), contracts or rules.paper_contracts
    trades = []
    for ticker in sorted(by_window, key=lambda k: by_window[k][0]["close_ts"]):
        for r in sorted(by_window[ticker], key=lambda r: r["minute"]):
            if not (rules.min_yes_mid <= r["mid"] <= rules.max_yes_mid) or round(r["ask"] - r["bid"], 4) > rules.max_spread:
                continue
            if trend_filter and (not r["has_momentum"] or r["trend"] is None):
                continue
            best = None
            for side, prob, price in (("YES", r["p"], r["ask"]), ("NO", 1 - r["p"], 1 - r["bid"])):
                if trend_filter and side != ("YES" if r["trend"] == "UP" else "NO"):
                    continue
                if trend_filter and move_already_extended(side, r["ret_60m"], rules):
                    continue
                fee = float(taker_fee_per_contract(Decimal(str(round(price, 4))), n, rate))
                edge = prob - price - fee
                if edge >= rules.edge_threshold and (best is None or edge > best[0]):
                    best = (edge, side, price, fee)
            if best:
                edge, side, price, fee = best
                won = (r["y"] == 1) == (side == "YES")
                trades.append({"ticker": ticker, "minute": r["minute"], "side": side, "price": round(price, 4),
                               "fee": fee, "edge": edge, "prob": price + fee + edge, "won": won,
                               "pnl": n * ((1 if won else 0) - price - fee)})
                break
    return trades


def _book(trades: list, contracts: int) -> dict:
    if not trades:
        return {"trades": 0}
    pnl = sum(t["pnl"] for t in trades)
    fees = sum(contracts * t["fee"] for t in trades)
    sd = math.sqrt(sum(contracts ** 2 * t["price"] * (1 - t["price"]) for t in trades))
    return {"trades": len(trades), "won": sum(t["won"] for t in trades),
            "avg_price": round(mean(t["price"] for t in trades), 4), "pnl": round(pnl, 2),
            "model_win_chance": round(mean(t["prob"] for t in trades), 4) if all("prob" in t for t in trades) else None,
            "pnl_per_contract": round(pnl / (contracts * len(trades)), 4),
            "sd_from_zero_edge": round((pnl + fees) / sd, 2) if sd else None}


def _by_window(rows):
    g = defaultdict(list)
    for r in rows:
        g[r["ticker"]].append(r)
    return g


def _range95(rows, stat, seed=7):
    """2.5th-97.5th percentile of stat over bootstrap resamples of whole windows."""
    g = _by_window(rows)
    keys = sorted(g)
    rnd = random.Random(seed)
    vals = sorted(stat([r for _ in keys for r in g[rnd.choice(keys)]]) for _ in range(BOOTSTRAP))
    return [round(vals[int(0.025 * BOOTSTRAP)], 4), round(vals[int(0.975 * BOOTSTRAP) - 1], 4)]


def _brier(rows, key):
    return mean((r[key] - r["y"]) ** 2 for r in rows)


def _follow_through(rows):
    return mean((1 if r["trend"] == "UP" else -1) * (r["y"] - r["mid"]) for r in rows)


def _period(rows: list, rules) -> dict | None:
    if not rows:
        return None
    g = _by_window(rows)
    first = {k: v[0] for k, v in g.items()}
    moves = [r for r in rows if r["trend"]]
    closer = sum(1 for v in g.values() if _brier(v, "p") < _brier(v, "mid"))
    return {
        "windows": len(g), "minute_looks": len(rows),
        "yes_rate": round(mean(r["y"] for r in first.values()), 4),
        "basis_usd": round(mean(r["basis"] for r in first.values()), 2),
        "brier_model": round(_brier(rows, "p"), 4), "brier_market": round(_brier(rows, "mid"), 4),
        "brier_model_minus_market_range95": _range95(rows, lambda s: _brier(s, "p") - _brier(s, "mid")),
        "model_closer_windows": closer,
        "trend_moves": len(moves), "trend_windows": len({r["ticker"] for r in moves}),
        "trend_follow_through": round(_follow_through(moves), 4) if moves else None,
        "trend_follow_through_range95": _range95(moves, _follow_through) if moves else None,
        "strategy": _book(paper_trades(rows, rules, True), rules.paper_contracts),
        "shadow": _book(paper_trades(rows, rules, False), rules.paper_contracts),
    }


def _utc(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def run(windows: list, spot: dict, rules, recent_days: float = 1.0) -> dict:
    rows = observations(windows, spot, rules)
    end = max(w["close_ts"] for w in windows)
    split = end - recent_days * 86400
    return {
        "report": "study", "data": "public 1-minute history (Kalshi candles, Coinbase candles)",
        "rules_version": rules.version, "rules_hash": rules.hash(),
        "first_close_utc": _utc(min(w["close_ts"] for w in windows)), "last_close_utc": _utc(end),
        "recent_days": recent_days, "split_utc": _utc(split),
        "recent": _period([r for r in rows if r["close_ts"] > split], rules),
        "earlier": _period([r for r in rows if r["close_ts"] <= split], rules),
        "caveats": CAVEATS,
    }


def _as_decisions(trades: list, rows: list, windows_by_ticker: dict) -> list:
    """The main rule's paper trades as ACT decisions, in the shape kelly.simulate reads (the live tracker's own code)."""
    at = {(r["ticker"], r["minute"]): r for r in rows}
    out = []
    for t in trades:
        r = at[(t["ticker"], t["minute"])]
        out.append({"evaluated_at": windows_by_ticker[t["ticker"]]["open_ts"] + 60 * t["minute"],
                    "market_ticker": t["ticker"], "decision": "ACT", "intent": {"side": t["side"], "limit_price": str(t["price"])},
                    "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["close_ts"])),
                    "features": {"p_fair": r["p"], "yes_ask": str(r["ask"]), "no_ask": str(round(1 - r["bid"], 4)),
                                 "yes_mid": r["mid"], "spread": str(round(r["ask"] - r["bid"], 4)),
                                 "best_no_bid_size": "1000000", "best_yes_bid_size": "1000000"}})
    return sorted(out, key=lambda d: d["evaluated_at"])


def monthly(windows: list, spot: dict, rules, kelly_start: float = 100.0, month_of=None) -> dict:
    """Both strategies month by month. Quarter Kelly carries one bankroll across months."""
    from . import kelly  # local import: kelly imports nothing from here, but keep study importable alone

    month_of = month_of or (lambda ts: time.strftime("%Y-%m", time.localtime(ts)))
    ws = sorted(windows, key=lambda w: w["open_ts"])
    by_t = {w["ticker"]: w for w in ws}
    groups: dict = {}
    for i, w in enumerate(ws):
        groups.setdefault(month_of(w["close_ts"]), []).append(i)
    months, balance = [], kelly_start
    for month, idx in groups.items():
        lead = ws[max(0, idx[0] - rules.basis_windows):idx[0]]  # earlier windows only feed the price-gap estimate
        mine = {ws[i]["ticker"] for i in idx}
        rows = [r for r in observations(lead + [ws[i] for i in idx], spot, rules) if r["ticker"] in mine]
        if not rows:
            months.append({"month": month, "windows": len(idx), "minute_looks": 0})
            continue
        main_trades = paper_trades(rows, rules, True)
        main = _book(main_trades, rules.paper_contracts)
        acct = {"start_ts": min(by_t[t]["open_ts"] for t in mine), "start_usd": f"{balance:.2f}"}
        settled = {t: {"result": by_t[t]["result"]} for t in mine}
        k = kelly.simulate(_as_decisions(main_trades, rows, by_t), settled, acct, rules)
        months.append({
            "month": month, "windows": len(idx), "minute_looks": len(rows),
            "brier_model": round(_brier(rows, "p"), 4), "brier_market": round(_brier(rows, "mid"), 4),
            "main": main,
            "main_20": _book(paper_trades(rows, rules, True, contracts=20), 20),
            "kelly": {"trades": len(k["trades"]), "won": k["wins"], "start": balance, "end": k["balance"],
                      "return_pct": round(100 * (k["balance"] / balance - 1), 2) if balance else None},
        })
        balance = k["balance"]
    return {"report": "monthly backtest", "rules_version": rules.version, "rules_hash": rules.hash(),
            "kelly_start": kelly_start, "kelly_end": balance, "months": months, "caveats": CAVEATS}


def format_monthly(r: dict) -> str:
    lines = [f"Monthly backtest - rules {r['rules_version']} on public 1-minute history (paper, 10 contracts for the main strategy)",
             "", f"{'month':8} {'windows':>7} | {'main: trades  won (model said)   P&L        SD':48} | "
                 f"{'quarter Kelly: trades  bankroll':31} | {'Brier model/market':18}"]
    tot = {"trades": 0, "won": 0, "pnl": 0.0}
    for m in r["months"]:
        if not m.get("minute_looks"):
            lines.append(f"{m['month']:8} {m['windows']:>7} | no usable minutes")
            continue
        b, k = m["main"], m["kelly"]
        if b.get("trades"):
            tot["trades"] += b["trades"]
            tot["won"] += b["won"]
            tot["pnl"] += b["pnl"]
            said = f"({b['model_win_chance']:.0%})" if b.get("model_win_chance") is not None else ""
            main = f"{b['trades']:>6} {b['won'] / b['trades']:>4.0%} {said:>12} {_money(b['pnl']):>10} {b['sd_from_zero_edge']:+6.1f}"
        else:
            main = f"{0:>6}"
        lines.append(f"{m['month']:8} {m['windows']:>7} | {main:48} | {k['trades']:>6}  ${k['start']:>8,.2f} -> ${k['end']:>8,.2f} "
                     f"| {m['brier_model']:.4f}/{m['brier_market']:.4f}")
    if tot["trades"]:
        lines += ["", f"Main strategy, all months: {tot['trades']} trades, won {tot['won'] / tot['trades']:.0%}, "
                      f"P&L {_money(tot['pnl'])} at 10 contracts a trade",
                  f"Same rules at 20 contracts a trade: P&L "
                  f"{_money(sum(m.get('main_20', {}).get('pnl', 0) for m in r['months']))}",
                  f"Quarter Kelly: ${r['kelly_start']:,.2f} -> ${r['kelly_end']:,.2f}"]
    lines += ["", "Caveats:"] + [f"  - {c}" for c in r["caveats"]]
    return "\n".join(lines)


def _trades_line(label, b):
    if not b.get("trades"):
        return f"  {label}: no paper trades"
    return (f"  {label}: {b['trades']} paper trades, won {b['won'] / b['trades']:.0%}, avg price "
            f"{b['avg_price']:.2f}, P&L {_money(b['pnl'])} ({100 * b['pnl_per_contract']:+.1f}c/contract, "
            f"{b['sd_from_zero_edge']:+.1f} SD from zero edge)")


def _money(v):
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def format_report(r: dict) -> str:
    lines = [f"Study - rules {r['rules_version']} (hash {r['rules_hash']}) on {r['data']}",
             f"Windows closing {r['first_close_utc']} to {r['last_close_utc']} UTC"]
    for key, title in (("recent", f"Recent: the last {r['recent_days']:g} day(s), after {r['split_utc']} UTC"),
                       ("earlier", f"Earlier: up to {r['split_utc']} UTC")):
        p = r[key]
        lines.append("")
        if not p:
            lines.append(f"{title}: no data")
            continue
        lines.append(f"{title}: {p['windows']} windows, {p['minute_looks']:,} minute looks, YES {p['yes_rate']:.0%}")
        lines.append(f"  Gap, strike minus Coinbase at each open: {_money(p['basis_usd'])}")
        lo, hi = p["brier_model_minus_market_range95"]
        lines.append(f"  Calibration (Brier, lower is better): model {p['brier_model']:.4f}, Kalshi mid "
                     f"{p['brier_market']:.4f}; model minus mid 95% range {lo:+.4f} to {hi:+.4f}")
        lines.append(f"  Model closer to the result than Kalshi's mid in {p['model_closer_windows']} of {p['windows']} windows")
        if p["trend_moves"]:
            lo, hi = p["trend_follow_through_range95"]
            lines.append(f"  After a confirmed 3-minute move, result beyond Kalshi's price: {p['trend_follow_through']:+.3f} "
                         f"({p['trend_moves']} looks in {p['trend_windows']} windows; 95% range {lo:+.3f} to {hi:+.3f})")
        lines.append(_trades_line("Strategy, with trend filter", p["strategy"]))
        lines.append(_trades_line("Shadow, no trend filter    ", p["shadow"]))
    lines.append("")
    lines.append("Caveats:")
    lines += [f"  - {c}" for c in r["caveats"]]
    return "\n".join(lines)
