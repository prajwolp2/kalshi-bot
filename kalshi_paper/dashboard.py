"""Local live dashboard: python3 -m kalshi_paper dashboard  ->  http://127.0.0.1:8765

Reads the files the running bot writes in data/live (decisions, settlements, paper bankroll)
and serves one page that refreshes every 5 seconds. Read-only: it never talks to Kalshi or
the exchanges, and it listens on this computer only (127.0.0.1), so the data stays local.
"""

from __future__ import annotations

import errno
import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import bankroll, kelly, slippage
from .storage import read_jsonl
from .timeutil import parse_iso_utc

RUNNING_IF_UPDATED_WITHIN_S = 30
WINDOW_SERIES_S = 900


class DecisionLog:
    """Reads decisions-<version>.jsonl incrementally, keeping only what the page needs."""

    def __init__(self, path: Path):
        self.path, self.offset, self.rows = path, 0, []
        self._lock = threading.Lock()  # the server answers requests in parallel threads

    def refresh(self) -> list:
        with self._lock:
            if not self.path.exists():
                return self.rows
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1  # leave a half-written last line for next time
            for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
                try:
                    self.rows.append(json.loads(line))
                except ValueError:
                    continue
            self.offset += end
            return list(self.rows)


class HistoryProgress:
    """Progress of `history` downloads and the latest backtest, read from data/history.

    Counts cached windows incrementally (the cache file grows to ~15 MB), and estimates speed
    from how fast that count grew over the last few minutes.
    """

    WINDOW_RE = re.compile(r"(\d+) settled windows in range, (\d+) not cached yet")

    def __init__(self, history_dir: Path, rules):
        self.dir, self.rules = history_dir, rules
        self.offset, self.count, self.by_month, self.last_close = 0, 0, Counter(), None
        self.samples: list = []  # (time, count)
        self._lock = threading.Lock()

    def _scan(self):
        path = self.dir / f"settled-{self.rules.series_ticker}.jsonl"
        if not path.exists():
            return
        with open(path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
            try:
                w = json.loads(line)
            except ValueError:
                continue
            self.count += 1
            self.by_month[time.strftime("%Y-%m", time.localtime(w["close_ts"]))] += 1
            self.last_close = w["close_ts"]
        self.offset += end

    def state(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            self._scan()
            self.samples = [s for s in self.samples if now - s[0] <= 300] + [(now, self.count)]
            log = self.dir / "fetch.log"
            text = log.read_text(errors="replace") if log.exists() else ""
            m = self.WINDOW_RE.search(text)
            in_range = int(m.group(1)) if m else None
            to_fetch = int(m.group(2)) if m else None
            cached_at_start = in_range - to_fetch if m else None
            done = "new window(s)" in text or "Cache:" in text
            failed = "stopped:" in text or "Traceback" in text
            files = [self.dir / f"settled-{self.rules.series_ticker}.jsonl", log, self.dir / "coinbase-btcusd-1m.jsonl"]
            last_write = max((p.stat().st_mtime for p in files if p.exists()), default=None)
            running = bool(m) and not done and not failed and last_write is not None and now - last_write < 120
            first = self.samples[0]
            rate = (self.count - first[1]) / (now - first[0]) if now - first[0] >= 20 else None
            fetched = self.count - cached_at_start if m else None
            remaining = max(0, to_fetch - fetched) if m else None
            out = {"running": running, "done": done, "failed": failed, "cached": self.count,
                   "in_range": in_range, "to_fetch": to_fetch, "fetched": fetched, "remaining": remaining,
                   "rate_per_s": round(rate, 2) if rate else None,
                   "eta_s": round(remaining / rate) if rate and remaining is not None else None,
                   "last_window_close": self.last_close, "last_write": last_write,
                   "months": sorted(self.by_month.items()),
                   "coinbase_phase": bool(m) and not done and remaining == 0,
                   "log_tail": text.strip().splitlines()[-3:] if text else []}
            bt = self.dir / f"backtest-{self.rules.version}.json"
            if bt.exists():
                b = json.loads(bt.read_text())
                out["backtest"] = {"saved_at": bt.stat().st_mtime, "kelly_start": b["kelly_start"], "kelly_end": b["kelly_end"],
                                   "months": [{"month": x["month"], "windows": x["windows"], "looks": x.get("minute_looks", 0),
                                               "main_trades": (x.get("main") or {}).get("trades", 0),
                                               "main": x.get("main"), "kelly": x.get("kelly"),
                                               "brier_model": x.get("brier_model"), "brier_market": x.get("brier_market")}
                                              for x in b["months"]]}
            return out


def _f(d, key):
    v = (d.get("features") or {}).get(key)
    return float(v) if isinstance(v, (int, float, str)) and v not in ("", None) else None


def _money(x):
    return round(float(x), 2)


def state(log: DecisionLog, data_dir: Path, rules, now: float | None = None, slip: slippage.Tracker | None = None) -> dict:
    now = time.time() if now is None else now
    rows = log.refresh()
    settled = {s["ticker"]: s for s in read_jsonl(data_dir / "settlements.jsonl")}
    acct_path = bankroll.path_for(data_dir, rules)
    acct = json.loads(acct_path.read_text()) if acct_path.exists() else None
    since = acct["start_ts"] if acct else 0.0
    mine = [d for d in rows if (d.get("evaluated_at") or 0) >= since]

    last = rows[-1] if rows else None
    out = {"now": now, "rules_version": rules.version, "rules_hash": rules.hash(),
           "running": bool(last and now - (last.get("evaluated_at") or 0) < RUNNING_IF_UPDATED_WITHIN_S),
           "last_update": last.get("evaluated_at") if last else None, "checks": len(mine)}

    # Current window: the latest decision, plus the model-vs-market series for its window.
    if last:
        withf = next((d for d in reversed(rows) if d.get("features")), None)
        out["current"] = {
            "at": last.get("evaluated_at"), "ticker": last.get("market_ticker"), "close_time": last.get("close_time"),
            "decision": last.get("decision"), "reason": last.get("reason"), "detail": last.get("detail"),
            "features_at": withf.get("evaluated_at") if withf else None,
            "btc": _f(withf, "spot_composite") if withf else None, "gap": _f(withf, "basis_usd") if withf else None,
            "target": _f(withf, "target_price") if withf else None, "p_model": _f(withf, "p_fair") if withf else None,
            "yes_bid": _f(withf, "best_yes_bid") if withf else None, "yes_ask": _f(withf, "yes_ask") if withf else None,
            "momentum_z": ((withf.get("features") or {}).get("momentum") or {}).get("z") if withf else None,
        }
        t = last.get("market_ticker")
        out["window_series"] = [
            [d["evaluated_at"], _f(d, "p_fair"), _f(d, "yes_mid"), _f(d, "spot_composite"), _f(d, "target_price")]
            for d in rows[-400:] if d.get("market_ticker") == t and d.get("features")
            and now - d["evaluated_at"] <= WINDOW_SERIES_S]

    # Paper trades and the bankroll.
    acts = bankroll.counted(rows, acct) if acct else [d for d in rows if d.get("decision") == "ACT"]
    trades = []
    for d in acts:
        i = d["intent"]
        cost = (float(i["limit_price"]) + float(i["fee_per_contract"])) * i["contracts"]
        s = settled.get(d["market_ticker"])
        won = s and ((i["side"] == "YES") == (s["result"] == "yes"))
        trades.append({"at": d["evaluated_at"], "ticker": d["market_ticker"], "close_time": d.get("close_time"),
                       "side": i["side"], "price": float(i["limit_price"]), "contracts": i["contracts"],
                       "cost": _money(cost), "status": ("won" if won else "lost") if s else "open",
                       "pnl": _money((i["contracts"] if won else 0) - cost) if s else None})
    out["trades"] = trades
    if acct:
        st = bankroll.status(acts, settled, acct)
        out["bankroll"] = {k: _money(v) if k in ("start", "settled_pnl", "open_cost", "balance") else v
                           for k, v in st.items()}
        out["bankroll"]["stop_below"] = _money(bankroll.max_trade_cost(rules))
        bal, curve = float(st["start"]), [[acct["start_ts"], float(st["start"])]]
        for tr in sorted((t for t in trades if t["status"] != "open"), key=lambda t: t["close_time"] or ""):
            bal += tr["pnl"]
            close = parse_iso_utc(tr["close_time"]) if tr["close_time"] else tr["at"]
            curve.append([close, round(bal, 2)])
        out["balance_curve"] = curve

    kpath = kelly.path_for(data_dir, rules)
    if kpath.exists():
        kacct = json.loads(kpath.read_text())
        k = kelly.simulate(rows, settled, kacct, rules)
        bal, curve = k["start"], [[kacct["start_ts"], k["start"]]]
        for tr in sorted((t for t in k["trades"] if t["status"] != "open"), key=lambda t: t["close_ts"]):
            bal += tr["pnl"]
            curve.append([tr["close_ts"], round(bal, 2)])
        out["kelly"] = {key: k[key] for key in ("start", "balance", "settled_pnl", "open_cost", "settled", "wins")}
        out["kelly"].update(trades=k["trades"][-50:], count=len(k["trades"]), curve=curve,
                            params={"fraction": kelly.PARAMS.fraction, "cap": kelly.PARAMS.cap,
                                    "min_edge": kelly.PARAMS.min_edge,
                                    "max_contracts": kelly.PARAMS.max_contracts})

    shadows = []
    for d in mine:
        sh = d.get("shadow_no_trend_filter") or {}
        if sh.get("would_act"):
            s = settled.get(d["market_ticker"])
            shadows.append({"at": d["evaluated_at"], "side": sh["side"], "price": float(sh["limit_price"]),
                            "result": s["result"] if s else None})
    out["shadow"] = shadows
    out["skip_reasons"] = Counter(d.get("reason") for d in mine if d.get("decision") == "SKIP").most_common(8)
    if slip is not None:
        out["slippage"] = {}
        for n in (10, 30):
            r = slip.report(settled, n)
            out["slippage"][str(n)] = {"summary": r["summary"], "trades": r["trades"][-60:]}
    return out


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalshi paper bot</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#16161a;--sub:#5d5c58;--mute:#8b8a85;--line:#e4e2db;--blue:#2a78d6;--orange:#eb6834;--good:#1b8a3a;--bad:#c9372c;--goodbg:#e8f5ec;--badbg:#fbeaea;--neutralbg:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--ink:#f0efec;--sub:#c3c2b7;--mute:#8b8a85;--line:#33332f;--blue:#3987e5;--orange:#d95926;--good:#4cc46e;--bad:#ef6a5f;--goodbg:#173322;--badbg:#3a1b19;--neutralbg:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 40px}h1{font-size:20px;font-weight:600;margin:0}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;margin-bottom:16px}.pill{font-size:12px;padding:3px 10px;border-radius:99px;background:var(--neutralbg);color:var(--sub)}
.pill.on{background:var(--goodbg);color:var(--good)}.pill.off{background:var(--badbg);color:var(--bad)}.paper{margin-left:auto;font-size:12px;color:var(--sub)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.k{font-size:13px;color:var(--sub)}.v{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}.s{font-size:12px;color:var(--mute)}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:12px}@media (max-width:760px){.two{grid-template-columns:minmax(0,1fr)}}
h2{font-size:15px;font-weight:600;margin:0 0 10px}.row{display:flex;justify-content:space-between;gap:12px;padding:3px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}.row:last-child{border:0}
.row span:first-child{color:var(--sub)}.chart{position:relative;height:220px}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}
th{text-align:left;font-weight:500;color:var(--sub);font-size:12px;border-bottom:1px solid var(--line);padding:6px 4px}td{padding:6px 4px;border-bottom:1px solid var(--line)}
.won{color:var(--good)}.lost{color:var(--bad)}.open{color:var(--sub)}.tablewrap{overflow-x:auto}.bar{height:8px;border-radius:0 4px 4px 0;background:var(--blue)}
.reason{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr) 48px;gap:10px;align-items:center;font-size:13px;padding:3px 0}.muted{color:var(--mute);font-size:13px;font-weight:400}
.sec{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;font-size:17px;font-weight:600;margin:26px 0 10px}.dot{width:12px;height:12px;border-radius:3px;flex:none}
.card.main{border-top:3px solid var(--blue)}.card.kel{border-top:3px solid var(--orange)}.two+.card,.grid+.two{margin-top:0}
#hcard{margin-bottom:12px}
.seg{margin-left:auto;display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.seg button{border:0;background:var(--card);color:var(--sub);font:inherit;font-size:12px;padding:4px 10px;cursor:pointer}
.seg button.on{background:var(--neutralbg);color:var(--ink);font-weight:600}.cents{font-size:12px;color:var(--mute)}
</style></head><body><main>
<div class="top"><h1>Kalshi BTC 15-min paper bot</h1><span id="status" class="pill">connecting…</span><span id="rules" class="pill"></span>
<span class="paper">PAPER ONLY · no real money · no orders</span></div>
<div class="sec"><span class="dot" style="background:var(--mute)"></span>Market now <span class="muted">· shared by both strategies · <span id="chk">–</span> checks this run, one every 2 s</span></div>
<div class="two">
<div class="card"><h2>Current window <span class="muted" id="win"></span></h2><div id="cur"></div></div>
<div class="card"><h2>Model vs Kalshi, this window</h2><div class="chart"><canvas id="c1" role="img" aria-label="Model probability and Kalshi price for the current window"></canvas></div></div>
</div>

<div class="sec"><span class="dot" style="background:var(--blue)"></span>Main bot <span class="muted">· trend + edge of 2¢ or more after fees · 10 contracts · one trade per window</span></div>
<div class="grid">
<div class="card main"><div class="k">Paper bankroll</div><div class="v" id="bal">–</div><div class="s" id="bal_s"></div></div>
<div class="card main"><div class="k">Settled P&amp;L</div><div class="v" id="pnl">–</div><div class="s" id="pnl_s"></div></div>
<div class="card main"><div class="k">Paper trades</div><div class="v" id="ntr">–</div><div class="s" id="ntr_s"></div></div>
<div class="card main"><div class="k">Win rate</div><div class="v" id="mwin">–</div><div class="s" id="mwin_s"></div></div>
</div>
<div class="two">
<div class="card"><h2>Bankroll after each settled trade</h2><div class="chart"><canvas id="c2" role="img" aria-label="Main bot paper bankroll over time"></canvas></div></div>
<div class="card"><h2>Why it skipped</h2><div id="reasons"></div></div>
</div>
<div class="card"><h2>Trades</h2><div class="tablewrap"><table><thead><tr><th>Time (ET)</th><th>Window ends</th><th>Side</th><th>Price</th><th>Cost</th><th>Result</th><th>P&amp;L</th></tr></thead><tbody id="trades"></tbody></table></div>
<p class="muted" id="shadow"></p></div>

<div class="sec"><span class="dot" style="background:var(--mute)"></span>Slippage tracker <span class="muted">· every paper trade so far (all rule versions), repriced as a real order from the bot's next checks</span>
<span class="seg" id="slipsize"><button data-n="10" class="on">10 contracts</button><button data-n="30">30 contracts</button></span></div>
<div class="grid" id="slipcards"></div>
<div class="two">
<div class="card"><h2>How the price moved right after a signal <span class="muted">· ask at the next check vs the price shown</span></h2><div id="slipmove"></div></div>
<div class="card"><h2>What the order types mean</h2><div class="muted" style="font-size:13px;line-height:1.6">
<b>Shown price</b>: what the paper bankroll assumes, the ask displayed when the bot decided.<br>
<b>Market order</b>: pays whatever the ask is when the order lands, about 2 s (or 5 s) later.<br>
<b>Limit at shown price</b>: only buys if the ask is still at or below the shown price; otherwise the trade is missed ($0).<br>
<b>Limit up to 2¢ above</b>: same, but accepts up to 2¢ more.<br>
Fees are recomputed at each price. Contracts beyond what was displayed are priced 1¢ higher (approximate).</div></div>
</div>
<div class="card"><h2>Per trade <span class="muted" id="slipn"></span></h2><div class="tablewrap"><table><thead><tr><th>Time (ET)</th><th>Rules</th><th>Side</th><th>Shown</th><th>~2 s later</th><th>~5 s later</th><th>Result</th><th>P&amp;L shown</th><th>Market ~2 s</th><th>Limit</th></tr></thead><tbody id="sliprows"></tbody></table></div></div>

<div class="sec"><span class="dot" style="background:var(--orange)"></span>Quarter-Kelly tracker <span class="muted" id="kparams"></span></div>
<div class="grid">
<div class="card kel"><div class="k">Paper bankroll</div><div class="v" id="kbal">–</div><div class="s" id="kbal_s"></div></div>
<div class="card kel"><div class="k">Settled P&amp;L</div><div class="v" id="kpnl">–</div><div class="s" id="kpnl_s"></div></div>
<div class="card kel"><div class="k">Trades</div><div class="v" id="kntr">–</div><div class="s" id="kntr_s"></div></div>
<div class="card kel"><div class="k">Win rate</div><div class="v" id="kwin">–</div><div class="s" id="kwin_s"></div></div>
</div>
<div class="two">
<div class="card"><h2>Bankroll after each settled trade</h2><div class="chart"><canvas id="c3" role="img" aria-label="Quarter-Kelly paper bankroll over time"></canvas></div></div>
<div class="card"><h2>How it's sizing trades <span class="muted">· last 50 trades</span></h2><div id="ksize"></div></div>
</div>
<div class="card"><h2>Trades</h2><div class="tablewrap"><table><thead><tr><th>Time (ET)</th><th>Window ends</th><th>Side</th><th>Price</th><th>Win chance</th><th>Risk</th><th>Cost</th><th>Result</th><th>P&amp;L</th></tr></thead><tbody id="ktrades"></tbody></table></div></div>

<div class="sec"><span class="dot" style="background:var(--mute)"></span>Research <span class="muted">· public history download and backtest</span></div>
<div class="card" id="hcard" style="display:none"><h2>History download <span class="muted" id="hstatus"></span></h2>
<div style="height:10px;border-radius:5px;background:var(--neutralbg);overflow:hidden;margin:4px 0 10px"><div id="hbar" style="height:100%;width:0;background:var(--blue)"></div></div>
<div class="grid" style="margin-bottom:8px">
<div><div class="k">Windows downloaded</div><div class="v" id="hdone">–</div><div class="s" id="hdone_s"></div></div>
<div><div class="k">Speed</div><div class="v" id="hrate">–</div><div class="s">windows per second, last 5 min</div></div>
<div><div class="k">Time left</div><div class="v" id="heta">–</div><div class="s" id="heta_s"></div></div></div>
<div class="k" style="margin-bottom:4px">Windows cached by month</div><div id="hmonths"></div>
<p class="muted" id="hlog"></p></div>
<div id="bcard" style="display:none">
<div class="sec"><span class="dot" style="background:var(--blue)"></span>Backtest · Main bot <span class="muted" id="bsaved"></span></div>
<div class="two">
<div class="card main"><h2>Profit by month <span class="muted">· 10 contracts a trade</span></h2><div class="chart"><canvas id="c4" role="img" aria-label="Main bot backtest profit by month"></canvas></div></div>
<div class="card main"><h2>Summary</h2><div id="bmsum"></div></div>
</div>
<div class="card"><div class="tablewrap"><table><thead><tr><th>Month</th><th>Windows</th><th>Trades</th><th>Won</th><th>Win rate</th><th>Avg price</th><th>P&amp;L</th><th>Running total</th><th>vs luck (SD)</th><th>Accuracy model / Kalshi</th></tr></thead><tbody id="bmrows"></tbody></table></div></div>
<div class="sec"><span class="dot" style="background:var(--orange)"></span>Backtest · Quarter Kelly <span class="muted">· one bankroll carried month to month</span></div>
<div class="two">
<div class="card kel"><h2>Bankroll at each month end</h2><div class="chart"><canvas id="c5" role="img" aria-label="Quarter-Kelly backtest bankroll by month"></canvas></div></div>
<div class="card kel"><h2>Summary</h2><div id="bksum"></div></div>
</div>
<div class="card"><div class="tablewrap"><table><thead><tr><th>Month</th><th>Trades</th><th>Won</th><th>Win rate</th><th>Bankroll start</th><th>Bankroll end</th><th>Month return</th></tr></thead><tbody id="bkrows"></tbody></table></div>
<p class="muted">Backtests replay one look per minute with fills at the displayed price, so they run a little optimistic compared with the live bot.</p></div>
</div>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const $=id=>document.getElementById(id);
const dark=matchMedia('(prefers-color-scheme: dark)').matches;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const money=v=>v==null?'–':(v<0?'−':'')+'$'+Math.abs(v).toFixed(2);
const signed=v=>v==null?'–':(v<0?'−':v>0?'+':'')+'$'+Math.abs(v).toFixed(2);
const et=ts=>new Date(ts*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
const etIso=s=>s?et(Date.parse(s)/1000):'–';
const pct=v=>v==null?'–':(v*100).toFixed(1)+'%';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let c1,c2,c3,c4,c5;
function charts(){
  if(!window.Chart)return;Chart.defaults.color=css('--mute');Chart.defaults.font.family=getComputedStyle(document.body).fontFamily;
  const grid={color:css('--line')};
  c1=new Chart($('c1'),{type:'line',data:{datasets:[
    {label:'Model',data:[],borderColor:css('--blue'),borderWidth:2,pointRadius:0,tension:.2},
    {label:'Kalshi mid',data:[],borderColor:css('--orange'),borderWidth:2,borderDash:[5,4],pointRadius:0,tension:.2}]},
    options:{animation:false,responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{boxWidth:12}},tooltip:{callbacks:{title:i=>i.length?et(i[0].parsed.x):'',label:i=>i.dataset.label+': '+pct(i.parsed.y)}}},
      scales:{x:{type:'linear',grid:{display:false},ticks:{maxTicksLimit:6,callback:v=>et(v)}},y:{min:0,max:1,grid,ticks:{callback:v=>Math.round(v*100)+'%'}}}}});
  const bank=(el,label,color)=>new Chart($(el),{type:'line',data:{datasets:[{label,data:[],borderColor:css(color),backgroundColor:css(color),borderWidth:2,stepped:true,pointRadius:3},
    {label:'Start',data:[],borderColor:css('--mute'),borderWidth:1,borderDash:[3,3],pointRadius:0}]},
    options:{animation:false,responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{filter:i=>i.datasetIndex===0,callbacks:{title:i=>i.length?et(i[0].parsed.x):'',label:i=>money(i.parsed.y)}}},
      scales:{x:{type:'linear',grid:{display:false},ticks:{maxTicksLimit:6,callback:v=>et(v)}},y:{grid,ticks:{callback:v=>'$'+v}}}}});
  c2=bank('c2','Main bot','--blue');c3=bank('c3','Quarter Kelly','--orange');
}
function setCurve(ch,cv,start,now){if(!ch||!cv)return;const pts=cv.concat([[now,cv.at(-1)[1]]]).map(r=>({x:r[0],y:r[1]}));
  ch.data.datasets[0].data=pts;ch.data.datasets[1].data=[{x:pts[0].x,y:start},{x:now,y:start}];ch.update();}
const rate=(w,n)=>n?Math.round(100*w/n)+'%':'–';
function row(k,v){return `<div class="row"><span>${k}</span><span>${v}</span></div>`}
function render(s){
  const st=$('status');st.textContent=s.running?'running · updated '+Math.max(0,Math.round(s.now-s.last_update))+'s ago':(s.last_update?'stopped · last update '+et(s.last_update)+' ET':'no data yet');
  st.className='pill '+(s.running?'on':'off');$('rules').textContent='rules '+s.rules_version;
  const b=s.bankroll;
  if(b){$('bal').textContent=money(b.balance);$('bal_s').textContent=`of ${money(b.start)} · stops below ${money(b.stop_below)}`+(b.open_cost?` · ${money(b.open_cost)} in an open trade`:'');
    $('pnl').textContent=signed(b.settled_pnl);$('pnl').className='v '+(b.settled_pnl>0?'won':b.settled_pnl<0?'lost':'');$('pnl_s').textContent=`${b.settled} settled trade(s)`;}
  const n=s.trades.length,w=s.trades.filter(t=>t.status==='won').length,l=s.trades.filter(t=>t.status==='lost').length;
  $('ntr').textContent=n;$('ntr_s').textContent=`${w} won · ${l} lost · ${n-w-l} open`;
  $('mwin').textContent=rate(w,w+l);
  const avgP=s.trades.length?s.trades.reduce((a,t)=>a+t.price,0)/s.trades.length:null;
  $('mwin_s').textContent=avgP!=null?`avg price paid ${Math.round(avgP*100)}¢, so it needs about ${Math.round(avgP*100)+1}% to break even`:'';
  $('chk').textContent=s.checks.toLocaleString();
  const c=s.current;
  if(c){const left=c.close_time?Math.max(0,Date.parse(c.close_time)/1000-s.now):null;
    $('win').textContent=(c.close_time?'ends '+etIso(c.close_time)+' ET':'')+(left!=null?` · ${Math.floor(left/60)}:${String(Math.floor(left%60)).padStart(2,'0')} left`:'');
    const mid=c.yes_bid!=null&&c.yes_ask!=null?(c.yes_bid+c.yes_ask)/2:null;
    $('cur').innerHTML=row('BTC (our 3-exchange price)',c.btc?'$'+c.btc.toLocaleString(undefined,{maximumFractionDigits:2}):'–')
      +row('Target to beat',c.target?'$'+c.target.toLocaleString(undefined,{maximumFractionDigits:2}):'–')
      +row('Gap correction',c.gap!=null?signed(c.gap):'–')
      +row('Model: chance of YES',pct(c.p_model))+row('Kalshi YES bid / ask',c.yes_bid!=null?`${Math.round(c.yes_bid*100)}¢ / ${Math.round(c.yes_ask*100)}¢`:'–')
      +row('Momentum z (trend if ≥ +1 or ≤ −1)',c.momentum_z!=null?c.momentum_z.toFixed(2):'warming up')
      +row('Last decision',`<b>${esc(c.decision)}</b> ${esc(c.reason)}`);
    if(c1){c1.data.datasets[0].data=(s.window_series||[]).map(r=>({x:r[0],y:r[1]}));c1.data.datasets[1].data=(s.window_series||[]).map(r=>({x:r[0],y:r[2]}));c1.update();}}
  if(b)setCurve(c2,s.balance_curve,b.start,s.now);
  const k=s.kelly;
  if(!k){$('kparams').textContent='· not running (start the bot with --kelly 100)';}
  if(k){setCurve(c3,k.curve,k.start,s.now);
    $('kparams').textContent=`· the main bot's trades, sized by risk · ${k.params.fraction*100}% of the Kelly amount, at most ${k.params.cap*100}% of cash and ${k.params.max_contracts} contracts per trade`;
    $('kbal').textContent=money(k.balance);$('kbal_s').textContent=`of ${money(k.start)}`+(k.open_cost?` · ${money(k.open_cost)} in an open trade`:'');
    $('kpnl').textContent=signed(k.settled_pnl);$('kpnl').className='v '+(k.settled_pnl>0?'won':k.settled_pnl<0?'lost':'');$('kpnl_s').textContent=`${k.settled} settled trade(s)`;
    $('kntr').textContent=k.count;$('kntr_s').textContent=`${k.wins} won · ${k.settled-k.wins} lost · ${k.count-k.settled} open`;
    $('kwin').textContent=rate(k.wins,k.settled);
    const kt=k.trades,avg=f=>kt.length?kt.reduce((a,t)=>a+f(t),0)/kt.length:null;
    $('kwin_s').textContent=kt.length?`avg price paid ${Math.round(avg(t=>t.price)*100)}¢, so it needs about ${Math.round(avg(t=>t.price)*100)+1}% to break even`:'';
    $('ksize').innerHTML=kt.length?row('Average risk per trade',avg(t=>t.risk_pct).toFixed(1)+'% of cash')+row('Largest risk',Math.max(...kt.map(t=>t.risk_pct)).toFixed(1)+'% of cash')
      +row('Average size',avg(t=>t.contracts).toFixed(1)+' contracts ('+money(avg(t=>t.cost))+')')+row('Average edge it saw',(avg(t=>t.edge)*100).toFixed(1)+'¢ per contract')
      +row('Average win chance it saw',pct(avg(t=>t.win_chance))):'<p class="muted">No trades yet</p>';
    $('ktrades').innerHTML=k.trades.slice().reverse().map(t=>`<tr><td>${et(t.at)}</td><td>${etIso(t.close_time)}</td><td>${t.side}</td><td>${Math.round(t.price*100)}¢ ×${t.contracts}</td><td>${pct(t.win_chance)}</td><td>${t.risk_pct.toFixed(1)}%</td><td>${money(t.cost)}</td><td class="${t.status}">${t.status==='open'?'waiting':t.status}</td><td class="${t.status}">${signed(t.pnl)}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">No trades yet</td></tr>';}
  const tot=s.skip_reasons.reduce((a,r)=>a+r[1],0)||1,max=s.skip_reasons.length?s.skip_reasons[0][1]:1;
  $('reasons').innerHTML=s.skip_reasons.map(r=>`<div class="reason"><span>${esc(r[0])}</span><div class="bar" style="width:${Math.max(2,100*r[1]/max)}%"></div><span>${r[1]}</span></div>`).join('')||'<p class="muted">none yet</p>';
  $('trades').innerHTML=s.trades.slice().reverse().map(t=>`<tr><td>${et(t.at)}</td><td>${etIso(t.close_time)}</td><td>${t.side}</td><td>${Math.round(t.price*100)}¢ ×${t.contracts}</td><td>${money(t.cost)}</td><td class="${t.status}">${t.status==='open'?'waiting':t.status}</td><td class="${t.status}">${signed(t.pnl)}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">No paper trades yet</td></tr>';
  renderSlip(s.slippage);
  const sh=s.shadow;$('shadow').textContent=sh.length?`Comparison version (no trend filter) flagged ${sh.length} trade(s) this run; results show in python3 -m kalshi_paper score.`:'';
}
let slipN='10',lastSlip=null;
document.querySelectorAll('#slipsize button').forEach(b=>b.onclick=()=>{slipN=b.dataset.n;document.querySelectorAll('#slipsize button').forEach(x=>x.classList.toggle('on',x===b));renderSlip(lastSlip);});
const cents=v=>v==null?'–':Math.round(v*100)+'¢';
const dc=(a,b)=>a==null?'':` <span class="cents">${a-b>0.0049?'+':''}${Math.round((a-b)*100)}¢</span>`;
function renderSlip(sl){lastSlip=sl;if(!sl)return;const r=sl[slipN];if(!r)return;const S=r.summary,M=S.models,cls=v=>v>0?'won':v<0?'lost':'';
  const card=(m)=>{const x=M[m];return `<div class="card"><div class="k">${esc(x.label)}</div><div class="v ${cls(x.pnl)}">${signed(x.pnl)}</div><div class="s">${x.filled} filled${x.missed?` · ${x.missed} missed (${x.missed_winners} winners)`:''}${x.extra_cents_per_contract!=null&&m!=='shown'?` · ${x.extra_cents_per_contract>0?'+':''}${x.extra_cents_per_contract.toFixed(2)}¢ a contract vs shown`:''}</div></div>`};
  $('slipcards').innerHTML=['shown','market_2s','market_5s','limit','limit_2c'].map(card).join('');
  const mv=S.next_check_move_cents,f=v=>v==null?'–':(v>0?'+':'')+v.toFixed(2)+'¢';
  $('slipmove').innerHTML=S.compared?row('All settled trades',`${f(mv.all)} on average`)+row('Trades that won',f(mv.winners))+row('Trades that lost',f(mv.losers))
    +row('Moved against you / same / in your favor',`${mv.worse} / ${mv.same} / ${mv.better}`)+row('Trades compared',`${S.compared} of ${S.trades} (rest still open or not measured)`):'<p class="muted">No settled trades yet</p>';
  $('slipn').textContent=`· last ${r.trades.length} · ${slipN} contracts per order`;
  const pn=m=>!m.measured?'–':m.filled===false?'<span class="muted">missed</span>':signed(m.pnl);
  $('sliprows').innerHTML=r.trades.slice().reverse().map(t=>{const st=t.won==null?'open':t.won?'won':'lost';
    return `<tr><td>${et(t.at)}</td><td>${esc(t.version)}</td><td>${t.side}</td><td>${cents(t.price)}</td><td>${cents(t.next_ask)}${t.next_ask!=null?dc(t.next_ask,t.price):''}</td><td>${cents(t.late_ask)}${t.late_ask!=null?dc(t.late_ask,t.price):''}</td><td class="${st}">${st==='open'?'waiting':st}</td><td class="${st}">${pn(t.models.shown)}</td><td>${pn(t.models.market_2s)}</td><td>${pn(t.models.limit)}</td></tr>`}).join('')||'<tr><td colspan="10" class="muted">No paper trades yet</td></tr>';}
const dur=s=>s==null?'–':s<90?Math.round(s)+'s':s<5400?Math.round(s/60)+' min':(s/3600).toFixed(1)+' h';
function renderHistory(h){
  if(!h||!h.in_range&&!h.cached){return;}
  $('hcard').style.display='';
  const pctDone=h.in_range?Math.min(100,100*h.cached/h.in_range):0;
  $('hbar').style.width=pctDone.toFixed(1)+'%';
  $('hstatus').textContent=h.done?'· finished':h.failed?'· stopped with an error':h.coinbase_phase?'· Kalshi done, fetching Coinbase prices':h.running?'· running':'· not running (Mac asleep, or not started)';
  $('hdone').textContent=h.cached.toLocaleString();$('hdone_s').textContent=h.in_range?`of ${h.in_range.toLocaleString()} (${pctDone.toFixed(1)}%)`:'';
  $('hrate').textContent=h.rate_per_s!=null?h.rate_per_s.toFixed(1):'–';
  $('heta').textContent=h.done?'done':dur(h.eta_s);$('heta_s').textContent=h.eta_s&&!h.done?'finishes about '+new Date((s_now()+h.eta_s)*1000).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})+' (+5–10 min for Coinbase)':'';
  const mx=Math.max(1,...h.months.map(m=>m[1]));
  $('hmonths').innerHTML=h.months.map(m=>`<div class="reason"><span>${m[0]}</span><div class="bar" style="width:${Math.max(2,100*m[1]/mx)}%"></div><span>${m[1].toLocaleString()}</span></div>`).join('');
  $('hlog').textContent=(h.log_tail||[]).join(' · ');
  const b=h.backtest;
  if(b){$('bcard').style.display='';$('bsaved').textContent='· run '+new Date(b.saved_at*1000).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
    const ms=b.months.filter(m=>m.looks);
    const cls=v=>v>0?'won':v<0?'lost':'';
    let tr=0,won=0,run=0,best=null,worst=null,up=0;
    $('bmrows').innerHTML=b.months.map(m=>{if(!m.looks)return `<tr><td>${m.month}</td><td>${m.windows.toLocaleString()}</td><td colspan="8" class="muted">no usable minutes yet</td></tr>`;
      const x=m.main||{};const p=x.trades?x.pnl:0;run+=p;if(x.trades){tr+=x.trades;won+=x.won;up+=p>0;
        if(!best||p>best[1])best=[m.month,p];if(!worst||p<worst[1])worst=[m.month,p];}
      return `<tr><td>${m.month}</td><td>${m.windows.toLocaleString()}</td><td>${x.trades||0}</td><td>${x.won||0}</td><td>${rate(x.won,x.trades)}</td><td>${x.trades?Math.round(x.avg_price*100)+'¢':'–'}</td><td class="${cls(p)}">${signed(p)}</td><td class="${cls(run)}">${signed(run)}</td><td>${x.sd_from_zero_edge!=null?(x.sd_from_zero_edge>0?'+':'')+x.sd_from_zero_edge.toFixed(1):'–'}</td><td>${m.brier_model?.toFixed(4)} / ${m.brier_market?.toFixed(4)}</td></tr>`}).join('');
    $('bmsum').innerHTML=tr?row('Total profit (10 contracts a trade)',`<span class="${cls(run)}">${signed(run)}</span>`)+row('Trades',`${tr.toLocaleString()} (${rate(won,tr)} won)`)
      +row('Months in profit',`${up} of ${ms.length}`)+row('Best month',best?`${best[0]} ${signed(best[1])}`:'–')+row('Worst month',worst?`${worst[0]} ${signed(worst[1])}`:'–'):'<p class="muted">No trades yet</p>';
    let ktr=0,kwon=0,kup=0,peak=b.kelly_start,dd=0;
    $('bkrows').innerHTML=b.months.map(m=>{if(!m.looks)return `<tr><td>${m.month}</td><td colspan="6" class="muted">no usable minutes yet</td></tr>`;
      const k=m.kelly||{};ktr+=k.trades||0;kwon+=k.won||0;kup+=(k.return_pct||0)>0;peak=Math.max(peak,k.end);dd=Math.max(dd,peak?100*(1-k.end/peak):0);
      return `<tr><td>${m.month}</td><td>${k.trades||0}</td><td>${k.won||0}</td><td>${rate(k.won,k.trades)}</td><td>${money(k.start)}</td><td>${money(k.end)}</td><td class="${cls(k.return_pct)}">${k.return_pct!=null?(k.return_pct>0?'+':'')+k.return_pct.toFixed(1)+'%':'–'}</td></tr>`}).join('');
    $('bksum').innerHTML=ktr?row('Bankroll',`${money(b.kelly_start)} → <span class="${cls(b.kelly_end-b.kelly_start)}">${money(b.kelly_end)}</span>`)
      +row('Total return',`${((b.kelly_end/b.kelly_start-1)*100).toFixed(1)}%`)+row('Trades',`${ktr.toLocaleString()} (${rate(kwon,ktr)} won)`)
      +row('Months in profit',`${kup} of ${ms.length}`)+row('Worst drop from a month-end peak',`${dd.toFixed(1)}%`):'<p class="muted">No trades yet</p>';
    if(window.Chart){const labels=ms.map(m=>m.month);
      if(!c4){c4=new Chart($('c4'),{type:'bar',data:{labels:[],datasets:[{data:[],backgroundColor:[],borderRadius:4,maxBarThickness:28}]},
        options:{animation:false,responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:i=>signed(i.parsed.y)}}},scales:{x:{grid:{display:false}},y:{grid:{color:css('--line')},ticks:{callback:v=>'$'+v}}}}});
        c5=new Chart($('c5'),{type:'line',data:{labels:[],datasets:[{data:[],borderColor:css('--orange'),backgroundColor:css('--orange'),borderWidth:2,pointRadius:3}]},
        options:{animation:false,responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:i=>money(i.parsed.y)}}},scales:{x:{grid:{display:false}},y:{grid:{color:css('--line')},ticks:{callback:v=>'$'+v}}}}});}
      const pn=ms.map(m=>m.main&&m.main.trades?m.main.pnl:0);
      c4.data.labels=labels;c4.data.datasets[0].data=pn;c4.data.datasets[0].backgroundColor=pn.map(v=>v>=0?css('--good'):css('--bad'));c4.update();
      c5.data.labels=['start'].concat(labels);c5.data.datasets[0].data=[b.kelly_start].concat(ms.map(m=>m.kelly.end));c5.update();}}
}
let lastNow=Date.now()/1000;const s_now=()=>lastNow;
async function tick(){try{const r=await fetch('/api/state',{cache:'no-store'});const s=await r.json();lastNow=s.now;render(s);}catch(e){const st=$('status');st.textContent='dashboard server not reachable';st.className='pill off';}
  try{const h=await fetch('/api/history',{cache:'no-store'});renderHistory(await h.json());}catch(e){}}
charts();tick();setInterval(tick,5000);
</script></body></html>"""


def serve(data_dir: str, rules, port: int = 8765) -> None:
    d = Path(data_dir)
    log = DecisionLog(d / f"decisions-{rules.version}.jsonl")
    hist = HistoryProgress(d.parent / "history", rules)
    slip = slippage.Tracker(d)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            if self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif self.path.startswith("/api/state"):
                body, ctype = json.dumps(state(log, d, rules, slip=slip)).encode(), "application/json"
            elif self.path.startswith("/api/history"):
                body, ctype = json.dumps(hist.state()).encode(), "application/json"
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
        print(f"Port {port} is already in use - most likely the dashboard is already running.\n"
              f"Open http://127.0.0.1:{port} in your browser, or start another copy with --port {port + 1}.")
        return
    print(f"dashboard: http://127.0.0.1:{port}  (reads {d}; Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
