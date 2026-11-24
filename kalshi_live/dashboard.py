"""Live-account dashboard: python3 -m kalshi_live dashboard --env live  ->  http://127.0.0.1:8766

Shows only the real account: cash, the current market check, every order the bot sent, its
fill, and its result; P&L by day (Eastern time), what kinds of trades made or lost money, and a
"what if" calculator (`what_if`, /api/whatif) that replays the bot's real trades from any starting
amount. It reads the bot's files (data/trading-<env>/) and needs no API key.
Results come from Kalshi's public market data (kalshi_paper's read-only client), fetched
here about a minute after each window with a fill closes, and cached in settlements.jsonl.

It listens on 127.0.0.1 only. On a server, open it from your own computer through SSH:
  ssh -N -L 8766:127.0.0.1:8766 root@SERVER_IP     then browse to http://localhost:8766
"""

from __future__ import annotations

import errno
import json
import math
import threading
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from kalshi_paper import sizing
from kalshi_paper.http import FetchError, UrllibTransport
from kalshi_paper.rules import RULES
from kalshi_paper.scorer import fetch_settlement
from kalshi_paper.storage import append_jsonl, read_jsonl
from kalshi_paper.timeutil import utc_date

from .trader import Limits, Store, _d

SERVER_STARTED = time.time()
STALE_S = 20          # no update from the bot for this long = not responding
SETTLE_AFTER_S = 60   # ask for a window's result this long after it closes
ET = ZoneInfo("America/New_York")
WINDOW_S = 900        # KXBTC15M windows are 15 minutes
PRICE_BANDS = [(0.0, 0.35, "under 35¢"), (0.35, 0.65, "35–65¢"), (0.65, 1.01, "over 65¢")]
MINUTE_BANDS = [(0, 5, "0–5 min in"), (5, 10, "5–10 min in"), (10, 16, "10–15 min in")]


def _f(x) -> float:
    return float(_d(x))


def et_day(ts: float) -> str:
    """YYYY-MM-DD in Eastern time (the user's day); the bot's limits use the UTC day instead."""
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


def _group(trades: list) -> dict:
    n = len(trades)
    contracts = sum(t["filled"] for t in trades)
    pnl = sum(t["pnl"] for t in trades)
    return {"trades": n, "won": sum(t["result"] == "won" for t in trades), "pnl": round(pnl, 2),
            "contracts": contracts, "per_contract": pnl / contracts if contracts else None,
            "avg_price": sum(t["paid"] for t in trades) / n if n else None}


def summarize(done: list, now: float) -> dict:
    """Day-by-day P&L (Eastern time, by the day each window closed), records, and what made or lost money."""
    done = sorted(done, key=lambda x: x["close_ts"] or x["at"])
    days: dict = {}
    for x in done:
        days.setdefault(et_day(x["close_ts"] or x["at"]), []).append(x)
    rows, total = [], 0.0
    for d, ts in days.items():
        g = _group(ts)
        total += g["pnl"]
        rows.append({"day": d, **g, "fees": round(sum(t["fees"] for t in ts), 2), "total": round(total, 2)})
    peak = cum = 0.0
    drawdown = 0.0
    for x in done:
        cum += x["pnl"]
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
    streak = 0
    for x in reversed(done):
        if streak == 0 or (streak > 0) == (x["result"] == "won"):
            streak += 1 if x["result"] == "won" else -1
        else:
            break
    today = et_day(now)
    week = {et_day(now - 86400 * k) for k in range(7)}
    by = {"side": [], "price": [], "minute": []}
    for side in ("YES", "NO"):
        by["side"].append({"label": f"bought {side}", **_group([x for x in done if x["side"] == side])})
    for lo, hi, label in PRICE_BANDS:
        by["price"].append({"label": label, **_group([x for x in done if lo <= x["paid"] < hi])})
    for lo, hi, label in MINUTE_BANDS:
        g = [x for x in done if x["close_ts"] and lo * 60 <= x["at"] - (x["close_ts"] - WINDOW_S) < hi * 60]
        by["minute"].append({"label": label, **_group(g)})
    up = [r for r in rows if r["pnl"] > 0]
    down = [r for r in rows if r["pnl"] < 0]
    allg = _group(done)
    return {
        "days": rows[::-1],
        "records": {
            "today": round(sum(r["pnl"] for r in rows if r["day"] == today), 2),
            "today_trades": sum(r["trades"] for r in rows if r["day"] == today),
            "week": round(sum(r["pnl"] for r in rows if r["day"] in week), 2),
            "week_trades": sum(r["trades"] for r in rows if r["day"] in week),
            "best_day": max(rows, key=lambda r: r["pnl"]) if rows else None,
            "worst_day": min(rows, key=lambda r: r["pnl"]) if rows else None,
            "up_days": len(up), "down_days": len(down), "days": len(rows),
            "avg_day": round(total / len(rows), 2) if rows else None,
            "avg_trade": round(allg["pnl"] / allg["trades"], 2) if allg["trades"] else None,
            "per_contract": allg["per_contract"], "contracts": allg["contracts"],
            "max_drawdown": round(drawdown, 2), "streak": streak,
            "biggest_win": max((x["pnl"] for x in done), default=None),
            "biggest_loss": min((x["pnl"] for x in done), default=None),
        },
        "breakdown": by,
    }


def what_if(trades: list, start_usd: float, since_ts: float = 0.0, fixed: int | None = None,
            limits: Limits | None = None, rules=RULES, per100: float | None = None) -> dict:
    """Replay the bot's real settled trades (same windows, sides, prices paid and results) from a different
    starting amount. Size per trade: the rules' bankroll sizing on this simulated cash (contracts per $100,
    at least the minimum, at most the maximum), or `fixed` contracts; then the live limits: the most one
    order may cost, the cash on hand, the daily loss stop (UTC day, like the bot) and the halt floor.
    Bigger orders than the bot really sent might not have filled at the same price: this assumes they did."""
    limits = limits or Limits()
    if per100:
        rules = replace(rules, contracts_per_100usd=float(per100))
    floor, per_day = float(limits.stop_below_usd), limits.max_orders_per_day
    cash = float(start_usd)
    curve, days, taken, skipped = [], {}, [], {"limits": 0, "cash": 0}
    peak, drawdown, halted_at = cash, 0.0, None
    day_lost: dict = {}
    day_orders: dict = {}
    day_start: dict = {}
    for t in sorted((t for t in trades if t["at"] >= since_ts), key=lambda t: t["at"]):
        if halted_at is None and cash < floor:
            halted_at = t["at"]
        if halted_at is not None:
            skipped["limits"] += 1
            continue
        ud = utc_date(t["at"])
        day_start.setdefault(ud, cash)
        if day_lost.get(ud, 0.0) >= float(limits.loss_cap(day_start[ud])) or day_orders.get(ud, 0) >= per_day:
            skipped["limits"] += 1
            continue
        p = t["paid"]
        if fixed:
            n = int(fixed)
        else:
            n = sizing.contracts_for(cash, p + sizing.fee_per_contract(p, rules.paper_contracts, rules), rules)
        budget = min(float(limits.order_cap(cash)), cash)
        while n >= 1 and n * (p + sizing.fee_per_contract(p, n, rules)) > budget:
            n -= 1
        if n < 1:
            skipped["cash"] += 1
            continue
        cost = n * (p + sizing.fee_per_contract(p, n, rules))
        pnl = (n if t["result"] == "won" else 0.0) - cost
        cash += pnl
        day_orders[ud] = day_orders.get(ud, 0) + 1
        day_lost[ud] = day_lost.get(ud, 0.0) - pnl
        peak = max(peak, cash)
        drawdown = max(drawdown, (peak - cash) / peak if peak else 0.0)
        ts = t["close_ts"] or t["at"]
        curve.append([ts, round(cash, 2)])
        d = days.setdefault(et_day(ts), {"trades": 0, "won": 0, "pnl": 0.0})
        d["trades"] += 1
        d["won"] += t["result"] == "won"
        d["pnl"] += pnl
        taken.append({"n": n, "pnl": pnl, "result": t["result"]})
    day_rows = [{"day": k, **v, "pnl": round(v["pnl"], 2)} for k, v in days.items()]
    end = round(cash, 2)
    return {
        "start": round(float(start_usd), 2), "end": end, "profit": round(end - start_usd, 2),
        "return_pct": round(100 * (end / start_usd - 1), 2) if start_usd else None,
        "trades": len(taken), "won": sum(x["result"] == "won" for x in taken), "skipped": skipped,
        "contracts_min": min((x["n"] for x in taken), default=0), "contracts_max": max((x["n"] for x in taken), default=0),
        "max_drawdown_pct": round(100 * drawdown, 1), "halted_at": halted_at,
        "best_day": max(day_rows, key=lambda r: r["pnl"]) if day_rows else None,
        "worst_day": min(day_rows, key=lambda r: r["pnl"]) if day_rows else None,
        "days": day_rows[::-1], "curve": curve,
        "since": since_ts or (min((t["at"] for t in trades), default=None)),
        "sizing": f"{fixed} contracts a trade" if fixed else
                  f"{rules.contracts_per_100usd:g} contracts per $100 of cash, at least {rules.paper_contracts}, at most {rules.max_contracts}",
        "limits": limits.describe(),
    }


def limits_from(live: dict | None) -> Limits:
    """The limits the bot is running with now (from its status file), else the defaults."""
    lim = (live or {}).get("limits") or {}
    base = Limits()
    d = lambda k: (Decimal(str(lim[k])) if lim[k] is not None else None) if k in lim else getattr(base, k)
    return Limits(max_order_usd=d("max_order_usd"), daily_loss_usd=d("daily_loss_usd"),
                  max_order_pct=d("max_order_pct"), daily_loss_pct=d("daily_loss_pct"),
                  stop_below_usd=d("stop_below_usd") or Decimal(0),
                  max_orders_per_day=int(lim.get("max_orders_per_day", base.max_orders_per_day)),
                  price_cushion=d("price_cushion") or Decimal(0))


OLD_LIMITS = Limits(max_order_usd=Decimal(100), daily_loss_usd=Decimal(150), stop_below_usd=Decimal(100))  # before v0.4.2
OLD_PER100 = 7.5


def settled_trades(store: Store) -> list:
    """Every real, filled, settled order (what `what_if` replays)."""
    return [x for x in _rows(store)[0] if not x["dry_run"] and x["filled"] > 0 and x["result"] in ("won", "lost")]


def whatif_query(store: Store, query: str) -> dict:
    """GET /api/whatif?start=500&since=2026-11-10&fixed=&per100=&limits=now|old|off
    (since = an Eastern-time date; fixed = contracts a trade; per100 = contracts per $100 instead of the rules';
    limits: the bot's current ones, the pre-v0.4.2 settings, or none)."""
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    try:
        start = float(q.get("start", "500"))
    except ValueError:
        return {"error": "the starting amount must be a number"}
    if not (1 <= start <= 10_000_000) or not math.isfinite(start):
        return {"error": "the starting amount must be between $1 and $10,000,000"}
    since = 0.0
    if q.get("since"):
        try:
            since = datetime.strptime(q["since"], "%Y-%m-%d").replace(tzinfo=ET).timestamp()
        except ValueError:
            return {"error": "the start date must look like 2026-11-10"}
    fixed = None
    if q.get("fixed"):
        try:
            fixed = int(q["fixed"])
        except ValueError:
            return {"error": "contracts a trade must be a whole number"}
        if not 1 <= fixed <= RULES.max_contracts:
            return {"error": f"contracts a trade must be between 1 and {RULES.max_contracts}"}
    per100 = None
    if q.get("per100"):
        try:
            per100 = float(q["per100"])
        except ValueError:
            return {"error": "contracts per $100 must be a number"}
        if not 0.5 <= per100 <= 100:
            return {"error": "contracts per $100 must be between 0.5 and 100"}
    mode = q.get("limits", "now")
    if mode == "off":  # only the cash on hand limits the bets
        limits = Limits(max_order_usd=None, daily_loss_usd=None, stop_below_usd=Decimal(0), max_orders_per_day=10**6)
    elif mode == "old":  # the settings before v0.4.2: $100 per order, $150 daily stop, 7.5 per $100
        limits, per100 = OLD_LIMITS, per100 or OLD_PER100
    else:
        limits = limits_from(store.read_live())
    out = what_if(settled_trades(store), start, since, fixed, limits, per100=per100)
    out["limits_applied"] = mode != "off"
    out["limits_mode"] = mode if mode in ("off", "old") else "now"
    return out


def settle_due(store: Store, transport, now: float | None = None) -> int:
    """Fetch results for filled windows that have closed. Returns how many were added."""
    now = time.time() if now is None else now
    have = {s["ticker"] for s in read_jsonl(store.settlements_path)}
    due = sorted({r["ticker"] for r in store.orders() if _d(r.get("filled")) > 0 and r.get("close_ts")
                  and r["close_ts"] + SETTLE_AFTER_S < now and r["ticker"] not in have})
    added = 0
    for t in due:
        try:
            s = fetch_settlement(t, transport)
        except (FetchError, ValueError):
            continue
        if s:
            append_jsonl(store.settlements_path, s)
            added += 1
    return added


def _rows(store: Store) -> tuple[list, list]:
    """(order rows with fill, cost and result; trades the limits blocked)."""
    settled = {s["ticker"]: s for s in read_jsonl(store.settlements_path)}
    rows, blocked = [], []
    for r in store.orders():
        if r["status"] == "blocked":
            blocked.append({"at": r["at"], "ticker": r["ticker"], "side": r["side"], "reason": r.get("reason")})
            continue
        filled, cost, fees = _d(r.get("filled")), _d(r.get("cost_usd")), _d(r.get("fees_usd"))
        result = pnl = None
        if filled > 0:
            s = settled.get(r["ticker"])
            if s:
                won = (s["result"] == "yes") == (r["side"] == "YES")
                result, pnl = ("won" if won else "lost"), (filled if won else Decimal(0)) - cost - fees
            else:
                result = "open"
        rows.append({
            "at": r["at"], "ticker": r["ticker"], "side": r["side"], "status": r["status"],
            "close_ts": r.get("close_ts"), "contracts": r.get("contracts"), "filled": float(filled),
            "shown": _f(r.get("shown_ask")), "limit": _f(r.get("limit_price")) if r.get("limit_price") else None,
            "paid": float(cost / filled) if filled > 0 else None, "fees": float(fees), "cost": float(cost + fees),
            "result": result, "pnl": float(pnl) if pnl is not None else None,
            "settled_value": (settled.get(r["ticker"]) or {}).get("expiration_value"),
            "note": r.get("reason") or r.get("error") or r.get("halt_reason") or r.get("verify_error"),
            "dry_run": r["status"] == "dry_run",
        })
    return rows, blocked


def state(store: Store, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    live = store.read_live()
    rows, blocked = _rows(store)
    real = [x for x in rows if not x["dry_run"]]
    fills = [x for x in real if x["filled"] > 0]
    done = [x for x in fills if x["result"] in ("won", "lost")]
    today = utc_date(now)
    curve, total = [], 0.0
    for x in sorted(done, key=lambda x: x["close_ts"] or x["at"]):
        total += x["pnl"]
        curve.append({"t": x["close_ts"] or x["at"], "pnl": round(total, 2), "trade": x["pnl"], "ticker": x["ticker"],
                      "side": x["side"], "result": x["result"]})
    tried = [x for x in real if x["status"] in ("filled", "partly_filled", "not_filled")]
    slips = [x["paid"] - x["shown"] for x in fills if x["paid"] is not None]
    if live is None:
        health = {"state": "waiting", "text": "waiting for the bot's first update"}
    elif now - live["at"] > STALE_S:
        health = {"state": "stale", "text": f"bot not responding (last update {int(now - live['at'])} s ago)"}
    elif live.get("halted"):
        health = {"state": "halted", "text": f"HALTED: {live['halted']}"}
    else:
        health = {"state": "running", "text": "running"}
    return {
        "now": now, "server_started": SERVER_STARTED, "env": store.env, "health": health, "live": live,
        "stats": {
            "orders": len(real), "filled": len(fills), "not_filled": sum(x["status"] == "not_filled" for x in real),
            "problems": sum(x["status"] in ("unknown", "rejected", "error") for x in real),
            "fill_rate": len(fills) / len(tried) if tried else None,
            "open": sum(x["result"] == "open" for x in fills), "settled": len(done),
            "won": sum(x["result"] == "won" for x in done),
            "pnl": round(sum(x["pnl"] for x in done), 2),
            "pnl_today": round(sum(x["pnl"] for x in done if utc_date(x["at"]) == today), 2),
            "orders_today": sum(1 for x in real if utc_date(x["at"]) == today),
            "fees": round(sum(x["fees"] for x in fills), 2),
            "spent": round(sum(x["cost"] for x in fills), 2),
            "avg_slip": sum(slips) / len(slips) if slips else None,
            "dry_runs": sum(x["dry_run"] for x in rows),
        },
        "curve": curve, "orders": rows[::-1][:200], "blocked": blocked[::-1][:15],
        "summary": summarize(done, now),
        "first_trade": min((x["at"] for x in done), default=None),
        "sizing": {"per_100usd": RULES.contracts_per_100usd, "min": RULES.paper_contracts, "max": RULES.max_contracts},
    }


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalshi BTC bot</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#16161a;--sub:#5d5c58;--mute:#8b8a85;--line:#e4e2db;--blue:#2a78d6;--orange:#eb6834;--good:#1b8a3a;--bad:#c9372c;--warn:#a86a00;--goodbg:#e8f5ec;--badbg:#fbeaea;--warnbg:#fbf1dc;--neutralbg:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--ink:#f0efec;--sub:#c3c2b7;--mute:#8b8a85;--line:#33332f;--blue:#3987e5;--orange:#d95926;--good:#4cc46e;--bad:#ef6a5f;--warn:#e0a63a;--goodbg:#173322;--badbg:#3a1b19;--warnbg:#3a2e14;--neutralbg:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1160px;margin:0 auto;padding:20px 16px 40px}h1{font-size:20px;font-weight:600;margin:0}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-bottom:16px}.pill{font-size:12px;padding:3px 10px;border-radius:99px;background:var(--neutralbg);color:var(--sub)}
.pill.on{background:var(--goodbg);color:var(--good)}.pill.off{background:var(--badbg);color:var(--bad)}.pill.warn{background:var(--warnbg);color:var(--warn)}.pill.real{background:var(--ink);color:var(--bg);font-weight:600}
.upd{margin-left:auto;font-size:12px;color:var(--mute)}
.banner{display:none;border-radius:12px;padding:12px 16px;margin-bottom:12px;font-weight:500}.banner.off{display:block;background:var(--badbg);color:var(--bad)}.banner.warn{display:block;background:var(--warnbg);color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0;margin-bottom:12px}.grid .card,.two .card{margin-bottom:0}
.k{font-size:13px;color:var(--sub)}.v{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}.s{font-size:12px;color:var(--mute)}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:12px}@media (max-width:760px){.two{grid-template-columns:minmax(0,1fr)}}
h2{font-size:15px;font-weight:600;margin:0 0 10px;display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.row{display:flex;justify-content:space-between;gap:12px;padding:3px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}.row:last-child{border:0}
.row span:first-child{color:var(--sub)}.row span:last-child{text-align:right}.chart{position:relative;height:230px}.chart.tall{height:260px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}
th{text-align:left;font-weight:500;color:var(--sub);font-size:12px;border-bottom:1px solid var(--line);padding:6px 4px;white-space:nowrap}td{padding:6px 4px;border-bottom:1px solid var(--line);white-space:nowrap}
th.n,td.n{text-align:right}tr.grp td{color:var(--sub);font-size:12px;font-weight:600;padding-top:12px;border-bottom:0}
.won{color:var(--good)}.lost{color:var(--bad)}.open{color:var(--sub)}.tablewrap{overflow-x:auto}.muted{color:var(--mute);font-size:13px;font-weight:400}
.empty{color:var(--mute);font-size:14px;padding:18px 0;text-align:center}.tag{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--neutralbg);color:var(--sub)}
.tag.won{background:var(--goodbg);color:var(--good)}.tag.lost{background:var(--badbg);color:var(--bad)}
.act{color:var(--good);font-weight:600}.why{overflow-wrap:anywhere}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-left:auto}.seg button{font:inherit;font-size:12px;border:0;background:transparent;color:var(--sub);padding:3px 10px;cursor:pointer}
.seg button.on{background:var(--neutralbg);color:var(--ink);font-weight:600}
form.wi{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end;margin-bottom:12px}
form.wi label{display:flex;flex-direction:column;font-size:12px;color:var(--sub);gap:3px}
form.wi input,form.wi select{font:inherit;font-size:14px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);min-width:0}
form.wi input[type=number]{width:120px}form.wi .fixed{width:80px}
form.wi button{font:inherit;font-size:14px;font-weight:600;padding:7px 16px;border:0;border-radius:8px;background:var(--blue);color:#fff;cursor:pointer}
.wires .grid{margin-bottom:8px}.note{font-size:12px;color:var(--mute);margin-top:8px}.err{color:var(--bad);font-size:14px}
.more{font:inherit;font-size:13px;border:0;background:none;color:var(--blue);cursor:pointer;padding:6px 0 0}
</style></head><body><main>
<div class="top"><h1>Kalshi BTC 15-min bot</h1><span id="health" class="pill">connecting…</span><span id="mode" class="pill"></span><span id="rules" class="pill"></span><span class="upd" id="upd"></span></div>
<div id="banner" class="banner"></div>
<div class="grid">
<div class="card"><div class="k">Cash on Kalshi</div><div class="v" id="cash">–</div><div class="s" id="cash_s"></div></div>
<div class="card"><div class="k">Today (ET)</div><div class="v" id="today">–</div><div class="s" id="today_s"></div></div>
<div class="card"><div class="k">Last 7 days</div><div class="v" id="week">–</div><div class="s" id="week_s"></div></div>
<div class="card"><div class="k">All-time P&amp;L</div><div class="v" id="pnl">–</div><div class="s" id="pnl_s"></div></div>
<div class="card"><div class="k">Win rate</div><div class="v" id="win">–</div><div class="s" id="win_s"></div></div>
<div class="card"><div class="k">Profit per contract</div><div class="v" id="ppc">–</div><div class="s" id="ppc_s"></div></div>
</div>
<div class="two">
<div class="card"><h2>Current window <span class="muted" id="wname"></span></h2><div id="cur"></div></div>
<div class="card"><h2>Model vs Kalshi, this window</h2><div class="chart"><canvas id="c1" role="img" aria-label="Model chance of YES and Kalshi YES bid and ask over the current window"></canvas></div></div>
</div>
<div class="card"><h2>P&amp;L by day <span class="muted">· Eastern time, by the day each window closed</span></h2>
<div class="chart tall"><canvas id="c3" role="img" aria-label="Profit or loss for each day"></canvas></div>
<div class="tablewrap"><table><thead><tr><th>Day</th><th class="n">Trades</th><th class="n">Won</th><th class="n">Avg price</th><th class="n">Fees</th><th class="n">P&amp;L</th><th class="n">Running total</th></tr></thead><tbody id="days"></tbody></table></div>
<button class="more" id="daysmore" type="button"></button><div class="empty" id="dayse" style="display:none">No settled trades yet.</div></div>
<div class="two">
<div class="card"><h2>Settled P&amp;L <span class="seg" id="rng"><button type="button" data-r="day">Today</button><button type="button" data-r="week">7 days</button><button type="button" data-r="all" class="on">All</button></span></h2>
<div class="chart"><canvas id="c2" role="img" aria-label="Cumulative settled profit and loss"></canvas></div><div class="empty" id="c2e" style="display:none">No settled trades in this range.</div></div>
<div class="card"><h2>Records</h2><div id="rec"></div></div>
</div>
<div class="card"><h2>What made and lost money <span class="muted">· all settled trades</span></h2>
<div class="tablewrap"><table><thead><tr><th></th><th class="n">Trades</th><th class="n">Won</th><th class="n">Avg price</th><th class="n">P&amp;L</th><th class="n">Per contract</th></tr></thead><tbody id="brk"></tbody></table></div></div>
<div class="card wires"><h2>What if you had started with…</h2>
<form class="wi" id="wi">
<label>Starting amount ($)<input type="number" id="wi_start" min="1" step="any" value="500" required></label>
<label>From<input type="date" id="wi_since"></label>
<label>Bet size<select id="wi_mode"><option value="rule">The bot's rule (grows with cash)</option><option value="fixed">A fixed number of contracts</option></select></label>
<label id="wi_fixed_l" style="display:none">Contracts a trade<input class="fixed" type="number" id="wi_fixed" min="1" max="1000" step="1" value="10"></label>
<label>Settings<select id="wi_lim"><option value="now">The bot’s current settings</option><option value="old">The old settings (7.5 per $100, $100/order, $150/day)</option><option value="off">No limits (only the cash on hand)</option></select></label>
<button type="submit">Calculate</button></form>
<div id="wi_out"><div class="empty">Enter an amount and press Calculate. It replays every trade the bot really made since that date: same windows, sides, prices and results, sized for your amount.</div></div></div>
<div class="two">
<div class="card"><h2>Fills, costs and limits</h2><div id="fills"></div></div>
<div class="card"><h2>Trades the limits stopped <span class="muted">· most recent first</span></h2><div id="blocked"></div></div>
</div>
<div class="card"><h2>Orders <span class="muted" id="ordn"></span></h2><div class="tablewrap"><table><thead><tr><th>Time (ET)</th><th>Side</th><th>Result</th><th class="n">P&amp;L</th><th class="n">Contracts</th><th class="n">Shown</th><th class="n">Paid</th><th class="n">Fees</th><th class="n">Cost</th><th>Order</th><th>Window ends</th></tr></thead><tbody id="orders"></tbody></table></div><div class="empty" id="orde">No orders yet.</div></div>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const $=id=>document.getElementById(id);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const usd=v=>Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const money=v=>v==null?'–':(v<0?'−':'')+'$'+usd(v);
const signed=v=>v==null?'–':(v<0?'−':v>0?'+':'')+'$'+usd(v);
const cents=v=>v==null?'–':Math.round(v*100)+'¢';
const pc=v=>v==null?'–':(v<0?'−':v>0?'+':'')+Math.abs(v*100).toFixed(1)+'¢';
const cls=v=>v>0?'won':v<0?'lost':'';
const et=ts=>new Date(ts*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
const etDay=ts=>new Date(ts*1000).toLocaleDateString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric'});
const etIsoDay=ts=>new Date(ts*1000).toLocaleDateString('en-CA',{timeZone:'America/New_York'});
const dayName=d=>new Date(d+'T12:00:00Z').toLocaleDateString('en-US',{timeZone:'UTC',weekday:'short',month:'short',day:'numeric'});
const etIso=s=>s?et(Date.parse(s)/1000):'–';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const row=(a,b)=>`<div class="row"><span>${a}</span><span>${b}</span></div>`;
const pct=(a,b)=>b?Math.round(100*a/b)+'%':'–';
const cap=(usd,share,of,base)=>{const p=[];if(share!=null)p.push(`${+(share*100).toFixed(1)}% ${of}${base!=null?` (${money(share*base)} now)`:''}`);if(usd!=null)p.push(money(+usd));return p.length?p.join(' or ')+(p.length>1?', whichever is less':''):'no limit'};
const REASON={all_conditions_met:'<span class="act">trade</span>',insufficient_basis_history:'warming up (needs two window opens, ~30 min after a restart)',
 insufficient_history:'warming up (needs 5 min of its own price history)',no_trend:'no clear BTC trend',edge_below_threshold:'price not cheap enough vs the model',
 price_out_of_band:'price outside the band the rules trade',spread_too_wide:'bid–ask spread too wide',too_close_to_settlement:'last 90 seconds of the window',
 already_acted_this_window:'already traded this window',trigger_not_held:'trigger seen, waiting to see if it holds',move_already_extended:'BTC already moved a lot this hour',insufficient_depth:'not enough contracts offered',
 no_open_market:'no open window right now',clock_skew:'server clock off from Kalshi',basis_out_of_range:'our BTC price too far from Kalshi’s index'};
const reason=r=>r?(REASON[r]||esc(r.replace(/_/g,' '))):'–';
let c1,c2,c3,c4,last=null,range='all',allDays=false,sinceSet=false;
function charts(){
  if(!window.Chart)return;Chart.defaults.color=css('--mute');Chart.defaults.font.family=getComputedStyle(document.body).fontFamily;
  const grid={color:css('--line')};const blue=css('--blue'),orange=css('--orange');
  c1=new Chart($('c1'),{type:'line',data:{datasets:[
    {label:'Model chance of YES',data:[],borderColor:blue,borderWidth:2,pointRadius:0,tension:.2},
    {label:'Kalshi YES ask',data:[],borderColor:orange,borderWidth:2,pointRadius:0,stepped:true,fill:'+1',backgroundColor:orange+'22'},
    {label:'Kalshi YES bid',data:[],borderColor:orange,borderWidth:1,borderDash:[4,3],pointRadius:0,stepped:true}]},
    options:{animation:false,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    scales:{x:{type:'linear',grid,ticks:{callback:v=>et(v),maxTicksLimit:5}},y:{min:0,max:1,grid,ticks:{callback:v=>Math.round(v*100)+'¢'}}},
    plugins:{legend:{labels:{boxWidth:12,boxHeight:2}},tooltip:{callbacks:{title:i=>et(i[0].parsed.x),label:i=>`${i.dataset.label}: ${(i.parsed.y*100).toFixed(1)}¢`}}}}});
  c2=new Chart($('c2'),{type:'line',data:{datasets:[{label:'Settled P&L',data:[],borderColor:blue,borderWidth:2,pointRadius:2,pointHoverRadius:5,tension:0}]},
    options:{animation:false,maintainAspectRatio:false,interaction:{mode:'nearest',intersect:false},
    scales:{x:{type:'linear',grid,ticks:{callback:v=>etDay(v),maxTicksLimit:5,maxRotation:0}},y:{grid,ticks:{callback:v=>signed(v)}}},
    plugins:{legend:{display:false},tooltip:{callbacks:{title:i=>{const p=i[0].raw;return `${etDay(p.x)} ${et(p.x)} · ${p.side} ${p.result}`},
      label:i=>`trade ${signed(i.raw.trade)} · total ${signed(i.raw.y)}`}}}}});
  c3=new Chart($('c3'),{type:'bar',data:{labels:[],datasets:[{label:'P&L',data:[],borderRadius:4,borderSkipped:'start',maxBarThickness:36}]},
    options:{animation:false,maintainAspectRatio:false,scales:{x:{grid:{display:false}},y:{grid,ticks:{callback:v=>signed(v)}}},
    plugins:{legend:{display:false},tooltip:{callbacks:{label:i=>{const d=i.raw.d;return `${signed(d.pnl)} · ${d.trades} trades, ${d.won} won · total ${signed(d.total)}`}}}},
    parsing:{xAxisKey:'x',yAxisKey:'y'}}});
}
function drawCurve(s){
  if(!c2)return;const cut=range==='day'?etIsoDay(s.now):range==='week'?etIsoDay(s.now-6*86400):null;
  let pts=s.curve;if(cut)pts=pts.filter(p=>etIsoDay(p.t)>=cut);
  const base=cut&&pts.length?pts[0].pnl-pts[0].trade:0;
  c2.data.datasets[0].data=pts.map(p=>({x:p.t,y:+(p.pnl-base).toFixed(2),trade:p.trade,side:p.side,result:p.result}));
  c2.data.datasets[0].pointBackgroundColor=pts.map(p=>p.result==='won'?css('--good'):css('--bad'));c2.data.datasets[0].pointBorderColor=c2.data.datasets[0].pointBackgroundColor;
  c2.options.scales.x.ticks.callback=range==='day'?(v=>et(v)):(v=>etDay(v));c2.update();
  $('c2').parentNode.style.display=pts.length?'':'none';$('c2e').style.display=pts.length?'none':'';
}
function render(s){
  last=s;const h=s.health,L=s.live,S=s.stats,R=s.summary.records;
  const hp=$('health');hp.textContent=h.state==='running'?'running':h.state==='halted'?'halted':h.state==='stale'?'not responding':'waiting';
  hp.className='pill '+(h.state==='running'?'on':h.state==='waiting'?'':'off');
  const b=$('banner');
  if(h.state==='halted'){b.className='banner off';b.textContent=h.text+' — no orders until you run deploy/kb resume.'}
  else if(h.state==='stale'){b.className='banner off';b.textContent=h.text+'. Check: systemctl status kalshi-live'}
  else if(h.state==='waiting'){b.className='banner warn';b.textContent='Waiting for the bot. If this lasts more than a minute, the bot may not be running yet.'}
  else b.className='banner';
  const m=$('mode');
  if(L){m.textContent=L.dry_run?'DRY RUN · nothing sent':(L.env==='live'?'REAL MONEY':'demo exchange');m.className='pill '+(L.dry_run?'warn':L.env==='live'?'real':'');$('rules').textContent='rules '+L.rules_version}
  $('upd').textContent=L?'updated '+Math.max(0,Math.round(s.now-L.at))+' s ago':'';
  const cash=L&&L.cash!=null?+L.cash:null,eq=L?+L.equity:null;
  $('cash').textContent=money(cash);
  const tot=L&&L.total_cash!=null?+L.total_cash:null,shard=L?L.exchange_index:null;
  $('cash_s').textContent=(eq!=null&&cash!=null&&Math.abs(eq-cash)>.005?`${money(eq)} counting the open trade`:'available to trade')
    +(shard!=null&&tot!=null&&Math.abs(tot-cash)>.005?` · ${money(tot)} in the whole account`:'');
  const set=(id,v,sub)=>{const e=$(id);e.textContent=signed(v);e.className='v '+cls(v);$(id+'_s').textContent=sub};
  set('today',R.today,`${R.today_trades} trades settled today`);
  set('week',R.week,`${R.week_trades} trades`);
  set('pnl',S.pnl,`${S.settled} settled${S.open?` · ${S.open} waiting`:''} · after fees`);
  $('win').textContent=S.settled?Math.round(100*S.won/S.settled)+'%':'–';$('win_s').textContent=S.settled?`${S.won} won, ${S.settled-S.won} lost`:'no settled trades yet';
  const p=$('ppc');p.textContent=pc(R.per_contract);p.className='v '+cls(R.per_contract);$('ppc_s').textContent=R.avg_trade!=null?`${signed(R.avg_trade)} a trade on average · ${Math.round(R.contracts).toLocaleString()} contracts`:'';
  // current window
  const d=L&&L.decision;
  if(d&&d.ticker){
    $('wname').textContent='· ends '+etIso(d.close_time);
    const left=d.seconds_left!=null?`${Math.floor(d.seconds_left/60)}:${String(Math.floor(d.seconds_left%60)).padStart(2,'0')}`:'–';
    const gap=d.spot!=null&&d.strike!=null?d.spot-d.strike:null;
    $('cur').innerHTML=row('Market',esc(d.ticker))+row('Time left',left)
     +row('BTC vs target',d.spot!=null?`$${Math.round(d.spot).toLocaleString()} vs $${Math.round(d.strike).toLocaleString()} (${gap>=0?'+':'−'}$${Math.abs(Math.round(gap))})`:'–')
     +row('Model chance of YES',d.p_fair!=null?(d.p_fair*100).toFixed(1)+'%':'–')
     +row('Kalshi YES bid / ask',d.yes_bid!=null?`${cents(+d.yes_bid)} / ${cents(+d.yes_ask)}`:'–')
     +row('Trend',d.trend?(d.trend==='UP'?'up':'down')+` (z ${d.momentum_z.toFixed(2)})`:d.momentum_z!=null?`none (z ${d.momentum_z.toFixed(2)})`:'–')
     +row('Last check',reason(d.reason));
  } else $('cur').innerHTML='<div class="empty">'+(d?reason(d.reason):'No data yet')+'</div>';
  if(c1){const w=(L&&L.window)||[];c1.data.datasets[0].data=w.map(p=>({x:p[0],y:p[1]}));c1.data.datasets[1].data=w.map(p=>({x:p[0],y:p[3]}));c1.data.datasets[2].data=w.map(p=>({x:p[0],y:p[2]}));c1.update()}
  drawCurve(s);
  // days
  const D=s.summary.days;$('dayse').style.display=D.length?'none':'';
  if(c3){const asc=D.slice(0,60).reverse();c3.data.labels=asc.map(x=>dayName(x.day));c3.data.datasets[0].data=asc.map(x=>({x:dayName(x.day),y:x.pnl,d:x}));
    c3.data.datasets[0].backgroundColor=asc.map(x=>x.pnl>=0?css('--good'):css('--bad'));c3.update()}
  $('c3').parentNode.style.display=D.length?'':'none';
  const shown=allDays?D:D.slice(0,14);
  $('days').innerHTML=shown.map(x=>`<tr><td>${dayName(x.day)}</td><td class="n">${x.trades}</td><td class="n">${x.won} (${pct(x.won,x.trades)})</td><td class="n">${cents(x.avg_price)}</td><td class="n">${money(x.fees)}</td><td class="n ${cls(x.pnl)}">${signed(x.pnl)}</td><td class="n ${cls(x.total)}">${signed(x.total)}</td></tr>`).join('');
  $('daysmore').style.display=D.length>14?'':'none';$('daysmore').textContent=allDays?'Show the last 14 days':`Show all ${D.length} days`;
  // records
  const dd=x=>x?`${signed(x.pnl)} · ${dayName(x.day)}`:'–';
  $('rec').innerHTML=row('Best day',dd(R.best_day))+row('Worst day',dd(R.worst_day))
   +row('Winning / losing days',`${R.up_days} / ${R.down_days} of ${R.days}`)+row('Average day',signed(R.avg_day))
   +row('Biggest win / loss',`${signed(R.biggest_win)} / ${signed(R.biggest_loss)}`)
   +row('Deepest drop from a high',R.max_drawdown?'−$'+usd(R.max_drawdown):'–')
   +row('Current streak',R.streak?`${Math.abs(R.streak)} ${R.streak>0?'win':'loss'}${Math.abs(R.streak)>1?(R.streak>0?'s':'es'):''} in a row`:'–');
  // breakdown
  const B=s.summary.breakdown,grp=(t,rows)=>`<tr class="grp"><td colspan="6">${t}</td></tr>`+rows.map(x=>`<tr><td>${x.label}</td><td class="n">${x.trades}</td><td class="n">${x.trades?pct(x.won,x.trades):'–'}</td><td class="n">${cents(x.avg_price)}</td><td class="n ${cls(x.pnl)}">${x.trades?signed(x.pnl):'–'}</td><td class="n ${cls(x.per_contract)}">${pc(x.per_contract)}</td></tr>`).join('');
  $('brk').innerHTML=grp('Side',B.side)+grp('Price paid',B.price)+grp('When in the 15-minute window',B.minute);
  // fills & limits
  const lim=L&&L.limits,dayLoss=L&&L.day_start_equity!=null&&eq!=null?Math.max(0,+L.day_start_equity-eq):null;
  $('fills').innerHTML=row('Fill rate',S.fill_rate!=null?Math.round(S.fill_rate*100)+'% of orders':'–')
   +row('Paid vs price shown',S.avg_slip!=null?(S.avg_slip>=0?'+':'−')+Math.abs(S.avg_slip*100).toFixed(2)+'¢ a contract on average':'–')
   +row('Spent on fills (incl. fees)',money(S.spent))+row('Fees paid',money(S.fees))
   +row('Bet size rule',s.sizing.per_100usd?`${s.sizing.per_100usd} contracts per $100 of cash (${s.sizing.min}–${s.sizing.max})`:`${s.sizing.min} contracts`)
   +(lim?row('Orders today (UTC day)',`${S.orders_today} of ${lim.max_orders_per_day}`)
   +row('Daily loss stop',cap(lim.daily_loss_usd,lim.daily_loss_pct,'of the day’s start',L.day_start_equity!=null?+L.day_start_equity:null))
   +row('Lost today',dayLoss!=null?money(dayLoss):'–')
   +row('Most per order',cap(lim.max_order_usd,lim.max_order_pct,'of the balance',eq)+` · up to ${Math.round(+lim.price_cushion*100)}¢ over the shown ask`)
   +row('Halts below',money(+lim.stop_below_usd)+' equity'):'');
  // orders
  $('ordn').textContent=s.orders.length?`· ${s.orders.length} most recent`:'';$('orde').style.display=s.orders.length?'none':'';
  $('orders').innerHTML=s.orders.map(o=>{const res=o.result?`<span class="tag ${o.result}">${o.result}</span>`:'';
    const st={filled:'filled',partly_filled:'part filled',not_filled:'not filled',dry_run:'dry run',unknown:'no response',rejected:'rejected',error:'error'}[o.status]||o.status;
    return `<tr><td>${etDay(o.at)} ${et(o.at)}</td><td>${o.side}</td><td>${res}</td><td class="n ${cls(o.pnl)}">${o.pnl!=null?signed(o.pnl):'–'}</td>
    <td class="n">${o.filled?o.filled:(o.contracts??'–')}${o.filled&&o.contracts&&o.filled<o.contracts?' of '+o.contracts:''}</td>
    <td class="n">${cents(o.shown)}</td><td class="n">${o.paid!=null?(o.paid*100).toFixed(1)+'¢':'–'}</td><td class="n">${o.filled?money(o.fees):'–'}</td><td class="n">${o.filled?money(o.cost):'–'}</td>
    <td title="${esc(o.note)}">${st}</td><td>${o.close_ts?et(o.close_ts):'–'}</td></tr>`}).join('');
  $('blocked').innerHTML=s.blocked.length?s.blocked.map(x=>`<div class="row"><span>${etDay(x.at)} ${et(x.at)} · ${esc(x.side)}</span><span class="why">${esc(String(x.reason||'').replace(/_/g,' '))}</span></div>`).join(''):'<div class="empty">None so far.</div>';
  if(!sinceSet&&s.first_trade){const f=etIsoDay(s.first_trade);$('wi_since').value=f;$('wi_since').min=f;$('wi_since').max=etIsoDay(s.now);sinceSet=true}
}
async function whatif(e){
  if(e)e.preventDefault();const out=$('wi_out');
  const q=new URLSearchParams({start:$('wi_start').value,since:$('wi_since').value});
  if($('wi_mode').value==='fixed')q.set('fixed',$('wi_fixed').value);q.set('limits',$('wi_lim').value);
  out.innerHTML='<div class="empty">Calculating…</div>';
  let r;try{r=await (await fetch('/api/whatif?'+q,{cache:'no-store'})).json()}catch(err){out.innerHTML='<div class="err">Couldn’t reach the dashboard.</div>';return}
  if(r.error){out.innerHTML=`<div class="err">${esc(r.error)}</div>`;return}
  if(!r.trades){out.innerHTML='<div class="empty">The bot made no settled trades in that range.</div>';return}
  const card=(k,v,sub,c='')=>`<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${sub}</div></div>`;
  const sk=r.skipped.limits+r.skipped.cash;
  out.innerHTML=`<div class="grid">${card('You’d have now',money(r.end),`from ${money(r.start)} on ${dayName(etIsoDay(r.since))}`)}
   ${card('Profit',signed(r.profit),`${r.trades} trades, ${r.won} won (${pct(r.won,r.trades)})`,cls(r.profit))}
   ${card('Return',(r.return_pct>0?'+':r.return_pct<0?'−':'')+Math.abs(r.return_pct).toFixed(1)+'%',`${r.contracts_min}–${r.contracts_max} contracts a trade`,cls(r.return_pct))}
   ${card('Deepest drop',r.max_drawdown_pct?'−'+r.max_drawdown_pct+'%':'0%','from a high, along the way')}</div>
   <div class="chart"><canvas id="c4" role="img" aria-label="Simulated balance after each trade"></canvas></div>
   <div class="row"><span>Best day / worst day</span><span>${r.best_day?signed(r.best_day.pnl)+' ('+dayName(r.best_day.day)+')':'–'} / ${r.worst_day?signed(r.worst_day.pnl)+' ('+dayName(r.worst_day.day)+')':'–'}</span></div>
   <div class="row"><span>Bet size</span><span>${esc(r.sizing)}</span></div>
   <div class="row"><span>Limits</span><span>${r.limits_applied?`${cap(r.limits.max_order_usd,r.limits.max_order_pct,'of the balance')} per order · stop for the day after losing ${cap(r.limits.daily_loss_usd,r.limits.daily_loss_pct,'of the day’s start')} · halt below ${money(+r.limits.stop_below_usd)}`:'none (only the cash on hand)'}</span></div>
   ${sk?`<div class="row"><span>Trades it couldn’t take</span><span>${r.skipped.limits?r.skipped.limits+' stopped by the limits':''}${r.skipped.limits&&r.skipped.cash?' · ':''}${r.skipped.cash?r.skipped.cash+' not enough cash':''}</span></div>`:''}
   ${r.halted_at?`<div class="err">This account would have fallen below ${money(r.limits.stop_below_usd)} and halted on ${etDay(r.halted_at)}.</div>`:''}
   <div class="note">Uses the bot’s real trades: same windows, sides, prices paid and results. Orders bigger than the bot really sent might not have filled at the same price, so large amounts are optimistic. Past results, not a forecast.</div>`;
  const old=c4;if(window.Chart){if(old)old.destroy();const grid={color:css('--line')};
    c4=new Chart($('c4'),{type:'line',data:{datasets:[{label:'Balance',data:[{x:r.since,y:r.start},...r.curve.map(p=>({x:p[0],y:p[1]}))],borderColor:css('--blue'),borderWidth:2,pointRadius:0,tension:0}]},
    options:{animation:false,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    scales:{x:{type:'linear',grid,ticks:{callback:v=>etDay(v),maxTicksLimit:6,maxRotation:0}},y:{grid,ticks:{callback:v=>money(v)}}},
    plugins:{legend:{display:false},tooltip:{callbacks:{title:i=>`${etDay(i[0].parsed.x)} ${et(i[0].parsed.x)}`,label:i=>'balance '+money(i.parsed.y)}}}}})}
}
$('wi').addEventListener('submit',whatif);
$('wi_mode').addEventListener('change',()=>{$('wi_fixed_l').style.display=$('wi_mode').value==='fixed'?'':'none'});
$('rng').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;range=b.dataset.r;[...$('rng').children].forEach(x=>x.classList.toggle('on',x===b));if(last)drawCurve(last)});
$('daysmore').addEventListener('click',()=>{allDays=!allDays;if(last)render(last)});
async function tick(){try{const r=await fetch('/api/state',{cache:'no-store'});const s=await r.json();
  if(window.srvStarted&&s.server_started!==window.srvStarted){location.reload();return}window.srvStarted=s.server_started;render(s);}
  catch(e){const h=$('health');h.textContent='dashboard not reachable';h.className='pill off';
    const b=$('banner');b.className='banner off';b.textContent='Can’t reach the dashboard. Is the SSH tunnel still open?'}}
charts();tick();setInterval(tick,3000);
</script></body></html>"""


def serve(store: Store, port: int = 8766, transport=None) -> None:
    transport = transport or UrllibTransport()
    stop = threading.Event()

    def settler():
        while not stop.is_set():
            try:
                settle_due(store, transport)
            except Exception as e:  # keep the page up even if a result fetch breaks
                print(f"settlement check failed: {e}", flush=True)
            stop.wait(30)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            if self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif self.path.startswith("/api/state"):
                body, ctype = json.dumps(state(store)).encode(), "application/json"
            elif self.path.startswith("/api/whatif"):
                body, ctype = json.dumps(whatif_query(store, urlparse(self.path).query)).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, 10048):
            raise
        print(f"Port {port} is already in use - the dashboard is probably already running.")
        return
    threading.Thread(target=settler, daemon=True).start()
    print(f"live dashboard: http://127.0.0.1:{port}  (reads {store.dir}; this machine only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
