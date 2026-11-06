"""Score paper decisions against how each window actually settled.

Reports three things, all with optimistic fill assumptions spelled out:
  - the strategy's paper intents (with the momentum filter)
  - the same rules without the momentum filter ("shadow"), to see if the pattern adds anything
  - calibration: is p_fair a better forecast than Kalshi's own mid price? (Brier score)
"""

from __future__ import annotations

import json
import time
from collections import Counter
from decimal import Decimal

from .collector import KALSHI_BASE
from .http import FetchError, get
from .timeutil import iso_utc, parse_iso_utc

ASSUMPTIONS = [
    "Paper P&L assumes every intent filled in full at the displayed ask at retrieval time. "
    "Real orders could fill worse, partially, or not at all; this is an upper bound, not a forecast.",
    "Fees: taker fee 0.07 x C x P x (1-P) per order, rounded up to the next cent. "
    "No maker fees, rebates or rounding refunds are modeled.",
    "Calibration snapshots inside one 15-minute window are strongly correlated, so the effective "
    "sample size is closer to the number of windows than the number of snapshots.",
    "Settlement uses Kalshi's own reported result. The spot venues are a stand-in for BRTI.",
]


def fetch_settlement(ticker: str, transport, clock=time.time) -> dict | None:
    """Kalshi's result for a window, or None if it hasn't settled yet."""
    resp = get(f"{KALSHI_BASE}/markets/{ticker}", transport)
    if resp.status != 200:
        raise FetchError(f"http_{resp.status} for {ticker}")
    m = (json.loads(resp.body) or {}).get("market") or {}
    if m.get("ticker") != ticker:
        raise FetchError(f"ticker mismatch: asked {ticker}, got {m.get('ticker')}")
    if m.get("status") in ("settled", "finalized") and m.get("result") in ("yes", "no"):
        return {"ticker": ticker, "result": m["result"], "status": m["status"],
                "expiration_value": m.get("expiration_value"), "floor_strike": m.get("floor_strike"),
                "settlement_ts": m.get("settlement_ts"), "fetched_at": clock()}
    return None


def tickers_to_settle(decisions, cached: dict, now: float, min_after_close_s: float = 60.0) -> list[str]:
    """Windows that produced at least one valid evaluation, have closed, and aren't cached yet."""
    out = set()
    for d in decisions:
        t, close = d.get("market_ticker"), d.get("close_time")
        if not t or not close or d.get("features") is None or t in cached:
            continue
        try:
            if parse_iso_utc(close) + min_after_close_s < now:
                out.add(t)
        except ValueError:
            continue
    return sorted(out)


def _pnl(side: str, price: Decimal, fee: Decimal, result: str) -> tuple[Decimal, bool]:
    win = (side == "YES") == (result == "yes")
    return ((Decimal(1) - price - fee) if win else (-price - fee)), win


def _book(trades_in, settlements):
    trades, total, wins = [], Decimal(0), 0
    for t in trades_in:
        s = settlements.get(t["ticker"])
        row = dict(t, result=s["result"] if s else None)
        if s:
            per, win = _pnl(t["side"], Decimal(t["price"]), Decimal(t["fee"]), s["result"])
            row.update(win=win, pnl_per_contract=str(per), pnl_dollars=str(per * t["contracts"]))
            total += per * t["contracts"]
            wins += win
        trades.append(row)
    settled = [r for r in trades if r["result"]]
    return {"intents": len(trades), "settled": len(settled), "wins": wins,
            "paper_pnl_dollars": str(total), "trades": trades}


def score(decisions: list[dict], settlements: dict, rules, now: float | None = None) -> dict:
    """Pure: build the report from decision rows and {ticker: settlement}."""
    cutoff = parse_iso_utc(rules.tuned_on_data_through) if rules.tuned_on_data_through else None
    used = [d for d in decisions if cutoff is None or (d.get("evaluated_at") or 0) > cutoff]
    synthetic = any(d.get("synthetic") for d in used)

    acts = [{"ticker": d["market_ticker"], "at": d["evaluated_at_iso"], "side": d["intent"]["side"],
             "price": d["intent"]["limit_price"], "fee": d["intent"]["fee_per_contract"],
             "contracts": d["intent"]["contracts"]}
            for d in used if d["decision"] == "ACT"]
    shadows = [{"ticker": d["market_ticker"], "at": d["evaluated_at_iso"], "side": sh["side"],
                "price": sh["limit_price"], "fee": sh["fee_per_contract"], "contracts": sh["contracts"]}
               for d in used if (sh := d.get("shadow_no_trend_filter") or {}).get("would_act")]

    cal = [(d["features"]["p_fair"], d["features"]["yes_mid"], settlements[d["market_ticker"]]["result"] == "yes",
            d["market_ticker"])
           for d in used if d.get("features") and d.get("market_ticker") in settlements]
    calibration = {"snapshots": len(cal), "windows": len({c[3] for c in cal})}
    if cal:
        calibration["brier_model"] = sum((p - y) ** 2 for p, _, y, _ in cal) / len(cal)
        calibration["brier_market_mid"] = sum((m - y) ** 2 for _, m, y, _ in cal) / len(cal)

    windows = {d["market_ticker"] for d in used if d.get("market_ticker") and d.get("features")}
    return {
        "report": "paper evaluation",
        "data": "SYNTHETIC" if synthetic else "live public data",
        "rules_version": rules.version,
        "rules_hash": rules.hash(),
        "generated_at": iso_utc(now if now is not None else time.time()),
        "tuned_on_data_through": rules.tuned_on_data_through,
        "decisions_used": len(used),
        "decisions_excluded_as_tuning_data": len(decisions) - len(used),
        "first_decision": used[0]["evaluated_at_iso"] if used else None,
        "last_decision": used[-1]["evaluated_at_iso"] if used else None,
        "valid_windows": len(windows),
        "settled_windows": len(windows & set(settlements)),
        "decision_counts": dict(Counter(d["decision"] for d in used)),
        "skip_reasons": dict(Counter(d["reason"] for d in used if d["decision"] == "SKIP").most_common()),
        "strategy_with_trend_filter": _book(acts, settlements),
        "shadow_without_trend_filter": _book(shadows, settlements),
        "calibration": calibration,
        "assumptions": ASSUMPTIONS,
    }


def format_report(r: dict) -> str:
    lines = [
        f"Paper evaluation - rules {r['rules_version']} (hash {r['rules_hash']}) - {r['data']}",
        f"Decisions: {r['decisions_used']} {r['decision_counts']}  "
        f"({r['first_decision']} to {r['last_decision']})",
    ]
    if r["decisions_excluded_as_tuning_data"]:
        lines.append(f"Excluded as tuning data (on/before {r['tuned_on_data_through']}): "
                     f"{r['decisions_excluded_as_tuning_data']}")
    lines.append(f"Windows with valid inputs: {r['valid_windows']}, settled: {r['settled_windows']}")
    lines.append("Skip reasons: " + ", ".join(f"{k} x{v}" for k, v in r["skip_reasons"].items()))
    for key, title in (("strategy_with_trend_filter", f"Strategy {r['rules_version']} (with trend filter)"),
                       ("shadow_without_trend_filter", "Shadow (no trend filter)")):
        b = r[key]
        lines.append(f"{title}: {b['intents']} paper intent(s), {b['settled']} settled, {b['wins']} won, "
                     f"paper P&L ${Decimal(b['paper_pnl_dollars']):+.2f}")
        for t in b["trades"]:
            res = t["result"] or "pending"
            pnl = f" -> {res}, ${Decimal(t['pnl_dollars']):+.2f}" if t["result"] else " -> pending"
            lines.append(f"    {t['at']} {t['ticker']} BUY {t['side']} x{t['contracts']} @ {t['price']} "
                         f"(fee {Decimal(t['fee']):.3f}/contract){pnl}")
    c = r["calibration"]
    if c.get("snapshots"):
        lines.append(f"Calibration (Brier, lower is better): model {c['brier_model']:.4f} vs "
                     f"market mid {c['brier_market_mid']:.4f} over {c['snapshots']} snapshots "
                     f"in {c['windows']} window(s)")
    else:
        lines.append("Calibration: no settled windows yet")
    lines.append("Assumptions:")
    lines += [f"  - {a}" for a in r["assumptions"]]
    return "\n".join(lines)
