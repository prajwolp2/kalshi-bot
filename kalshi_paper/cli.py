"""Command line.

  python3 -m kalshi_paper run     [--interval 5] [--minutes N] [--data-dir data/live]
  python3 -m kalshi_paper score   [--data-dir data/live]
  python3 -m kalshi_paper replay  [--data-dir data/live]
  python3 -m kalshi_paper demo    [--data-dir data/demo]
  python3 -m kalshi_paper history [--days 7] [--data-dir data/history]
  python3 -m kalshi_paper study   [--recent-days 1] [--data-dir data/history]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
import time
from pathlib import Path

from . import bankroll, kelly
from .collector import Collector
from .evaluator import EvalState, evaluate
from .http import FetchError, UrllibTransport
from .rules import RULES
from .scorer import fetch_settlement, format_report, score, tickers_to_settle
from .storage import append_jsonl, gzip_file, read_jsonl
from .timeutil import utc_date

MAX_CONSECUTIVE_API_ERRORS = 30  # bounded retries (Kalshi Developer Agreement 4.3)


def _paths(data_dir: str, rules) -> dict:
    d = Path(data_dir)
    return {
        "dir": d,
        "decisions": d / f"decisions-{rules.version}.jsonl",
        "settlements": d / "settlements.jsonl",
        "score": d / f"score-{rules.version}.json",
    }


def _snapshot_files(d: Path) -> list:
    """Daily snapshot files, oldest first: today's plain .jsonl plus finished days compressed to .jsonl.gz."""
    files = list(d.glob("snapshots-*.jsonl")) + list(d.glob("snapshots-*.jsonl.gz"))
    return sorted(files, key=lambda f: f.name.removesuffix(".gz"))


_compressing = threading.Lock()


def _compress_finished_days(d: Path, today: str) -> None:
    """Gzip every snapshot file from a day before `today` (UTC). Runs in a background thread."""
    if not _compressing.acquire(blocking=False):
        return  # one pass at a time
    try:
        for f in sorted(d.glob("snapshots-*.jsonl")):
            if f.name < f"snapshots-{today}.jsonl" and not f.with_name(f.name + ".gz").exists():
                try:
                    out = gzip_file(f)
                    print(f"compressed {f.name} -> {out.name} ({out.stat().st_size / 1e6:.1f} MB)", flush=True)
                except OSError as e:
                    print(f"could not compress {f.name}: {e}", file=sys.stderr, flush=True)
    finally:
        _compressing.release()


def _last_snapshot(d: Path) -> dict | None:
    """The newest complete snapshot in d, reading only the end of the newest file (they get large)."""
    files = _snapshot_files(d)
    if not files:
        return None
    if files[-1].suffix == ".gz":  # can't seek inside gzip: stream it and keep the last line
        lines = []
        with gzip.open(files[-1], "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                lines = [line]
    else:
        with open(files[-1], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            snap = json.loads(line)
        except ValueError:
            continue  # a line cut by a crash, or the partial first line of the tail
        if isinstance(snap, dict):
            return snap
    return None


def format_line(d: dict) -> str:
    parts = [d["evaluated_at_iso"][11:19] + "Z", d.get("market_ticker") or "-"]
    f = d.get("features")
    if f:
        secs = int(f["seconds_left"])
        mom = f.get("momentum")
        parts += [
            f"{secs // 60:2d}:{secs % 60:02d} left",
            f"S={f['spot_composite']:,.2f}",
            f"K={f['target_price']:,.2f}",
        ]
        if f.get("basis_usd") is not None:
            parts.append(f"gap={f['basis_usd']:+.1f}")
        parts += [
            f"p_fair={f['p_fair']:.3f}",
            f"yes {f['best_yes_bid']}/{f['yes_ask']}",
            f"m={mom['z']:+.2f}" if mom else "m=warming-up",
        ]
    reason = d["reason"] + (f" ({d['detail']})" if d.get("detail") else "")
    parts += [d["decision"], reason]
    if d["decision"] == "ACT":
        i = d["intent"]
        parts.append(f"PAPER BUY {i['side']} x{i['contracts']} @ {i['limit_price']} (not an order, not a fill)")
    return "  ".join(parts)


_KELLY_FIELDS = ("yes_mid", "spread", "p_fair", "yes_ask", "no_ask", "best_no_bid_size", "best_yes_bid_size")


def _compact(d: dict) -> dict:
    """The parts of a decision the quarter-Kelly tracker needs (keeps memory small on long runs)."""
    f = d.get("features")
    return {"evaluated_at": d.get("evaluated_at"), "market_ticker": d.get("market_ticker"),
            "decision": d.get("decision"), "intent": {"side": d["intent"]["side"]} if d.get("intent") else None,
            "close_time": d.get("close_time"), "features": {k: f.get(k) for k in _KELLY_FIELDS} if f else None}


def _bankroll_exhausted(bank: dict, rules, transport=None, now=None) -> bool:
    """Fetch due settlements (at most once a minute), print changes, and say whether to stop.

    Settles the main bankroll's trades and, if the quarter-Kelly tracker is on, every closed
    window it saw. Only the main bankroll can stop the run; the tracker just stops trading.
    """
    now = time.time() if now is None else now
    minute_check = now - bank["checked"] >= 60
    if minute_check:
        bank["checked"] = now
        transport = transport or UrllibTransport()
        p = _paths(bank["data_dir"], rules)
        due = bank["acts"] + [d for d in bank.get("kdecs", []) if d["features"]]
        for ticker in tickers_to_settle(due, bank["settled"], now):
            try:
                s = fetch_settlement(ticker, transport)
            except (FetchError, ValueError):
                continue  # try again next minute
            if s:
                bank["settled"][ticker] = s
                append_jsonl(p["settlements"], s)
    if bank.get("kelly") and (minute_check or bank.get("klast") is None):
        k = kelly.simulate(bank["kdecs"], bank["settled"], bank["kelly"], rules)
        if (k["balance"], len(k["trades"])) != bank.get("klast"):
            bank["klast"] = (k["balance"], len(k["trades"]))
            print(kelly.describe(k), flush=True)
    if not bank.get("acct"):
        return False
    st = bankroll.status(bank["acts"], bank["settled"], bank["acct"])
    if st["balance"] != bank["last"]:
        bank["last"] = st["balance"]
        print(bankroll.describe(st), flush=True)
    if st["balance"] < bank["floor"]:
        print(f"stopping: the paper bankroll (${st['balance']:.2f}) can't cover another trade "
              f"(up to ${bank['floor']:.2f}). Started with ${st['start']:.2f}.", flush=True)
        return True
    return False


def cmd_run(args) -> int:
    rules = RULES
    p = _paths(args.data_dir, rules)
    p["dir"].mkdir(parents=True, exist_ok=True)
    state = EvalState.from_decisions(read_jsonl(p["decisions"]))
    # Keep the strike-vs-our-price measurements across a restart; the rules ignore ones over 2 hours old.
    seed_basis = (_last_snapshot(p["dir"]) or {}).get("basis_log") or []
    collector = Collector(UrllibTransport(), seed_basis=seed_basis)
    print("kalshi_paper - PAPER ONLY. Public read-only data; no orders, no API keys.")
    print(f"rules {rules.version} (hash {rules.hash()}), every {args.interval:g}s"
          + (f" for {args.minutes:g} min" if args.minutes else " until Ctrl-C"))
    print(f"snapshots -> {p['dir']}/snapshots-YYYY-MM-DD.jsonl   decisions -> {p['decisions']}")
    print(f"remembered {len(collector.basis)} window-open gap measurement(s) from the last run; "
          f"trading waits for {rules.min_basis_windows} recent ones", flush=True)
    bank = None
    if args.bankroll or args.kelly:
        logged = read_jsonl(p["decisions"])
        bank = {"acct": None, "acts": [], "settled": {s["ticker"]: s for s in read_jsonl(p["settlements"])},
                "floor": bankroll.max_trade_cost(rules), "checked": 0.0, "last": None, "data_dir": args.data_dir}
        if args.bankroll:
            acct = bankroll.start_or_load(bankroll.path_for(p["dir"], rules), args.bankroll, time.time())
            bank.update(acct=acct, acts=bankroll.counted(logged, acct))
            print(f"{bankroll.describe(bankroll.status(bank['acts'], bank['settled'], acct))}; "
                  f"stops below ${bank['floor']:.2f}, the most one paper trade can cost", flush=True)
        if args.kelly:
            kacct = bankroll.start_or_load(kelly.path_for(p["dir"], rules), args.kelly, time.time())
            bank.update(kelly=kacct, kdecs=[_compact(d) for d in logged if (d.get("evaluated_at") or 0) >= kacct["start_ts"]])
            print(f"{kelly.describe(kelly.simulate(bank['kdecs'], bank['settled'], kacct, rules))}: quarter Kelly, "
                  f"capped at {kelly.PARAMS.cap:.0%} of cash and {kelly.PARAMS.max_contracts} contracts per trade, "
                  "on the main rules' trades", flush=True)
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    errors = n = acts = 0
    day = utc_date(time.time())
    # Finished days' snapshots are compressed (~10x smaller) in the background; replay still reads them.
    threading.Thread(target=_compress_finished_days, args=(p["dir"], day), daemon=True).start()
    try:
        while True:
            t0 = time.time()
            if bank and _bankroll_exhausted(bank, rules):
                break
            snap = collector.collect()
            snap_day = utc_date(snap["collected_at"])
            if snap_day != day:
                day = snap_day
                threading.Thread(target=_compress_finished_days, args=(p["dir"], day), daemon=True).start()
            append_jsonl(p["dir"] / f"snapshots-{snap_day}.jsonl", snap)
            dec = evaluate(snap, rules, state)
            state.record(dec)
            append_jsonl(p["decisions"], dec)
            print(format_line(dec), flush=True)
            n += 1
            acts += dec["decision"] == "ACT"
            if bank and bank["acct"] and dec["decision"] == "ACT":
                bank["acts"].append(dec)
            if bank and bank.get("kelly"):
                bank["kdecs"].append(_compact(dec))
            errors = errors + 1 if dec["reason"].startswith("api_error") else 0
            if errors >= MAX_CONSECUTIVE_API_ERRORS:
                print(f"stopping: {errors} consecutive API errors; check connectivity", file=sys.stderr)
                return 2
            # Exponential backoff on errors: 5, 10, 20, 40, 60, 60 ... seconds
            delay = args.interval if errors == 0 else min(60.0, args.interval * 2 ** errors)
            if deadline and time.time() + delay > deadline:
                break
            time.sleep(max(0.0, delay - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        collector.close()
    print(f"done: {n} snapshots, {acts} paper intent(s). Next: python3 -m kalshi_paper score")
    return 0


def cmd_score(args) -> int:
    rules = RULES
    p = _paths(args.data_dir, rules)
    decisions = read_jsonl(p["decisions"])
    if not decisions:
        print(f"no decisions in {p['decisions']} yet - run the collector first")
        return 1
    cached = {s["ticker"]: s for s in read_jsonl(p["settlements"])}
    transport = UrllibTransport()
    for ticker in tickers_to_settle(decisions, cached, time.time()):
        try:
            s = fetch_settlement(ticker, transport)
        except (FetchError, ValueError) as e:
            print(f"could not fetch settlement for {ticker}: {e}", file=sys.stderr)
            continue
        if s:
            cached[ticker] = s
            append_jsonl(p["settlements"], s)
        time.sleep(0.2)
    report = score(decisions, cached, rules)
    p["score"].write_text(json.dumps(report, indent=2))
    print(format_report(report))
    print(f"\nsaved: {p['score']}")
    return 0


def cmd_replay(args) -> int:
    """Re-evaluate this rules version's saved snapshots from scratch; decisions must match the log exactly.

    Runs under different rules versions share the daily snapshot files. Only snapshots
    this version logged a decision for are replayed: a live run starts from its own
    version's log, so another run's snapshots would give the replay one-per-window
    memory the live run never had.
    """
    rules = RULES
    p = _paths(args.data_dir, rules)
    snaps = []
    for f in _snapshot_files(p["dir"]):
        snaps += read_jsonl(f)
    snaps.sort(key=lambda s: s["collected_at"])
    logged = {d["snapshot_id"]: d for d in read_jsonl(p["decisions"])}
    ours = [s for s in snaps if s["snapshot_id"] in logged]
    no_snapshot = len(set(logged) - {s["snapshot_id"] for s in ours})
    state = EvalState()
    same = diff = 0
    for s in ours:
        d = evaluate(s, rules, state)
        state.record(d)
        old = logged[s["snapshot_id"]]
        if json.loads(json.dumps(d)) == old:
            same += 1
        else:
            diff += 1
            if diff <= 5:
                keys = sorted(k for k in set(d) | set(old) if json.loads(json.dumps(d.get(k))) != old.get(k))
                print(f"mismatch {s['snapshot_id']}: fields {keys}")
    print(f"replayed {len(ours)} snapshots with rules {rules.version}: {same} identical, {diff} different "
          f"({len(snaps) - len(ours)} snapshots from other runs or rules versions not replayed)")
    if no_snapshot:
        print(f"{no_snapshot} logged decision(s) have no saved snapshot, so they cannot be checked")
    return 0 if diff == 0 and no_snapshot == 0 else 1


def cmd_demo(args) -> int:
    from .demo import main as demo_main
    return demo_main(Path(args.data_dir))


def cmd_dashboard(args) -> int:
    from .dashboard import serve
    serve(args.data_dir, RULES, args.port)
    return 0


def cmd_history(args) -> int:
    from . import history
    now = int(time.time())
    print(f"fetching public history for the last {args.days:g} day(s) into {args.data_dir} "
          "(read-only GETs, about 5 per second)", flush=True)
    try:
        r = history.update(args.data_dir, RULES.series_ticker, now - int(args.days * 86400), now, UrllibTransport(),
                           log=lambda s: print(s, flush=True))
    except history.HistoryError as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 2
    windows, spot = history.load(args.data_dir, RULES.series_ticker)
    print(f"{r['windows_fetched']} new window(s), {r['spot_minutes_added']} new Coinbase minute(s). "
          f"Cache: {len(windows)} windows, {len(spot):,} minutes. Next: python3 -m kalshi_paper study")
    return 0


def cmd_study(args) -> int:
    from . import history, study
    windows, spot = history.load(args.data_dir, RULES.series_ticker)
    if not windows:
        print(f"no history in {args.data_dir} yet - run: python3 -m kalshi_paper history")
        return 1
    report = study.run(windows, spot, RULES, recent_days=args.recent_days)
    out = Path(args.data_dir) / f"study-{RULES.version}.json"
    out.write_text(json.dumps(report, indent=2))
    print(study.format_report(report))
    print(f"\nsaved: {out}")
    return 0


def cmd_backtest(args) -> int:
    from . import history, study
    windows, spot = history.load(args.data_dir, RULES.series_ticker)
    if not windows:
        print(f"no history in {args.data_dir} yet - run: python3 -m kalshi_paper history --days 335")
        return 1
    report = study.monthly(windows, spot, RULES, kelly_start=args.kelly_start)
    out = Path(args.data_dir) / f"backtest-{RULES.version}.json"
    out.write_text(json.dumps(report, indent=2))
    print(study.format_monthly(report))
    print(f"\nsaved: {out}")
    return 0


def cmd_slippage(args) -> int:
    from . import slippage
    d = Path(args.data_dir)
    settled = {x["ticker"]: x for x in read_jsonl(d / "settlements.jsonl")}
    rep = slippage.Tracker(d).report(settled, args.contracts)
    print(slippage.format_report(rep))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m kalshi_paper",
                                 description="Paper-only research prototype for Kalshi KXBTC15M.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="collect live public data and log paper ACT/SKIP decisions")
    r.add_argument("--interval", type=float, default=5.0, help="seconds between snapshots (default 5)")
    r.add_argument("--minutes", type=float, default=None, help="stop after N minutes (default: until Ctrl-C)")
    r.add_argument("--data-dir", default="data/live")
    r.add_argument("--bankroll", type=float, default=None,
                   help="PAPER bankroll in dollars: stop once it can't cover another paper trade")
    r.add_argument("--kelly", type=float, default=None,
                   help="also track a quarter-Kelly PAPER bankroll of this many dollars alongside the rules")
    s = sub.add_parser("score", help="fetch settlements and score the paper decisions")
    s.add_argument("--data-dir", default="data/live")
    rp = sub.add_parser("replay", help="re-evaluate saved snapshots and check decisions reproduce")
    rp.add_argument("--data-dir", default="data/live")
    d = sub.add_parser("demo", help="run the SYNTHETIC scenarios (trigger, skip cases, failures)")
    d.add_argument("--data-dir", default="data/demo")
    db = sub.add_parser("dashboard", help="local live dashboard of the running bot (this computer only)")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--data-dir", default="data/live")
    h = sub.add_parser("history", help="cache public 1-minute history: settled windows, Kalshi and Coinbase candles")
    h.add_argument("--days", type=float, default=7.0, help="how far back (default 7, at most 400; the series began 2025-12-10)")
    h.add_argument("--data-dir", default="data/history")
    st = sub.add_parser("study", help="check the rules on cached history: recent days vs the days before")
    st.add_argument("--recent-days", type=float, default=1.0, help="length of the 'recent' period (default 1)")
    st.add_argument("--data-dir", default="data/history")
    bt = sub.add_parser("backtest", help="both strategies month by month over all cached history")
    bt.add_argument("--kelly-start", type=float, default=100.0, help="quarter-Kelly starting bankroll (default 100)")
    bt.add_argument("--data-dir", default="data/history")
    sl = sub.add_parser("slippage", help="reprice every paper trade as a real order would have filled")
    sl.add_argument("--contracts", type=int, default=None, help="order size to test (default: each trade's own)")
    sl.add_argument("--data-dir", default="data/live")
    args = ap.parse_args(argv)
    if args.cmd == "run" and args.interval < 1:
        ap.error("--interval must be at least 1 second")
    if args.cmd == "history" and not 0 < args.days <= 400:
        ap.error("--days must be more than 0 and at most 400")
    if args.cmd == "study" and not args.recent_days > 0:
        ap.error("--recent-days must be more than 0")
    return {"run": cmd_run, "score": cmd_score, "replay": cmd_replay, "demo": cmd_demo,
            "history": cmd_history, "study": cmd_study, "dashboard": cmd_dashboard,
            "backtest": cmd_backtest, "slippage": cmd_slippage}[args.cmd](args)
