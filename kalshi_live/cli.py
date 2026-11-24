"""Command line for real orders (see deploy/SERVER.md).

  python3 -m kalshi_live smoke  --env demo|live     exchange check: 1-cent orders, read back, cancelled
  python3 -m kalshi_live run    --env demo|live [--dry-run] [limits, e.g. --daily-loss-pct 30] [--hold 4]
  python3 -m kalshi_live status --env demo|live     cash, today's orders, HALT state
  python3 -m kalshi_live dashboard --env demo|live  live-account dashboard at http://127.0.0.1:8766
  python3 -m kalshi_live fund-shard --env live --amount 200   move your cash to the shard KXBTC15M trades on
  python3 -m kalshi_live halt   --env demo|live     stop placing orders (the bot keeps running)
  python3 -m kalshi_live resume --env demo|live     allow orders again

Credentials come from the environment, never the command line:
  KALSHI_KEY_ID     the API key ID from kalshi.com (or demo.kalshi.co for --env demo)
  KALSHI_KEY_FILE   path to the downloaded private key file (chmod 600)
Real-money orders (--env live without --dry-run) also need KALSHI_REAL_MONEY=yes and a passed
`smoke --env live` with the same key.
"""

from __future__ import annotations

import argparse
import os
import time
from decimal import Decimal

from kalshi_paper.rules import RULES
from kalshi_paper.timeutil import iso_utc, utc_date

from .trader import Limits, Store, Trader, _d


def _client(env: str):
    from .api import Client
    from .signing import Signer
    key_id, key_file = os.environ.get("KALSHI_KEY_ID", ""), os.environ.get("KALSHI_KEY_FILE", "")
    if not key_id or not key_file:
        raise SystemExit("set KALSHI_KEY_ID and KALSHI_KEY_FILE (see deploy/SERVER.md)")
    try:
        return Client(env, Signer(key_id, key_file))
    except ModuleNotFoundError:
        raise SystemExit("the `cryptography` package is missing: run deploy/setup.sh, or pip install cryptography")
    except (OSError, ValueError) as e:
        raise SystemExit(f"could not load the key: {e}")


def _store(args) -> Store:
    return Store(args.data_dir or f"data/trading-{args.env}", args.env)


def _log(s: str) -> None:
    print(s, flush=True)


def live_gate(args, store: Store, key_id: str, environ=os.environ) -> str | None:
    """Why real-money ordering may not start, or None."""
    if args.env != "live" or args.dry_run:
        return None
    if environ.get("KALSHI_REAL_MONEY") != "yes":
        return "real-money orders need KALSHI_REAL_MONEY=yes in the environment (secrets/kalshi.env)"
    if not store.smoke_passed(key_id):
        return "run `python3 -m kalshi_live smoke --env live` first (it must pass with this key)"
    return None


def cmd_smoke(args) -> int:
    from .smoke import smoke
    client, store = _client(args.env), _store(args)
    ok = smoke(client, store, _log)
    _log(f"\n{'PASSED' if ok else 'FAILED'}; saved {store.smoke_path}")
    return 0 if ok else 1


def describe_cap(usd, pct, of: str) -> str:
    parts = ([f"{float(pct) * 100:g}% {of}"] if pct is not None else []) + ([f"${usd}"] if usd is not None else [])
    return " or ".join(parts) + (" (whichever is less)" if len(parts) == 2 else "") if parts else "no limit"


def limits_from_args(args) -> Limits:
    """--max-order-pct / --daily-loss-pct scale with the account (defaults 25% and 30%; 0 = off);
    --max-order / --daily-loss add a fixed dollar cap on top (off unless given)."""
    usd = lambda v: Decimal(str(v)) if v is not None else None
    pct = lambda v: Decimal(str(v)) / 100 if v else None
    return Limits(max_order_usd=usd(args.max_order), daily_loss_usd=usd(args.daily_loss),
                  max_order_pct=pct(args.max_order_pct), daily_loss_pct=pct(args.daily_loss_pct),
                  stop_below_usd=Decimal(str(args.stop_below)), max_orders_per_day=args.max_orders_per_day,
                  price_cushion=Decimal(str(args.cushion)))


def cmd_run(args) -> int:
    from kalshi_paper.collector import Collector
    from kalshi_paper.http import UrllibTransport
    from .trader import run
    client, store = _client(args.env), _store(args)
    why = live_gate(args, store, client.signer.key_id)
    if why:
        raise SystemExit(f"not starting: {why}")
    limits = limits_from_args(args)
    trader = Trader(client, store, limits, RULES, dry_run=args.dry_run, log=_log)
    mode = "DRY RUN (orders logged, not sent)" if args.dry_run else (
        "REAL MONEY" if args.env == "live" else "Kalshi demo exchange (mock money)")
    _log(f"kalshi_live {mode}; rules {RULES.version}; every {args.interval:g}s; data {store.dir}")
    _log(f"limits: {describe_cap(limits.max_order_usd, limits.max_order_pct, 'of equity')} per order, "
         f"stop for the day once this bot's own trades lose {describe_cap(limits.daily_loss_usd, limits.daily_loss_pct, 'of the day-start equity')}, "
         f"HALT below ${limits.stop_below_usd} equity, {limits.max_orders_per_day} orders a day, "
         f"pay up to {limits.price_cushion * 100:g}c over the shown ask")
    _log(f"order size: {RULES.contracts_per_100usd:g} contracts per $100 of cash, at least {RULES.paper_contracts}, "
         f"at most {RULES.max_contracts}; trading starts after ~30 min of warm-up")
    _log(f"hold: a trigger must stay on, same side on every look, for {args.hold:g}s before an order" if args.hold > 0
         else "hold: off (orders on the first look that triggers)")
    if store.halted():
        _log(f"HALTED: {store.halted()} (decisions continue; `resume` to allow orders)")
    if args.env == "demo":
        _log("note: market data is the real exchange's; demo order books differ, so demo orders may not fill")
    collector = Collector(UrllibTransport())
    try:
        return run(trader, collector, store.dir / f"decisions-{RULES.version}.jsonl", args.interval, log=_log,
                   hold_s=args.hold)
    except KeyboardInterrupt:
        _log("stopped")
        return 0
    finally:
        collector.close()


def cmd_status(args) -> int:
    store = _store(args)
    cash = None
    if not args.offline:
        client = _client(args.env)
        try:
            cash, shards = client.balances()
            shard = client.market_shard()
            if shards:
                _log("cash by exchange shard: " + ", ".join(
                    f"{k}: ${v:.2f}" + (" <- KXBTC15M trades here" if k == shard else "") for k, v in sorted(shards.items())))
        except Exception as e:  # status is informational
            _log(f"balance check failed: {e}")
    now = time.time()
    orders = store.orders()
    today = [r for r in orders if r["status"] != "blocked" and utc_date(r["at"]) == utc_date(now)]
    sent = [r for r in orders if r["status"] not in ("blocked", "dry_run")]
    filled = [r for r in sent if _d(r.get("filled")) > 0]
    spent = sum((_d(r.get("cost_usd")) + _d(r.get("fees_usd")) for r in filled), Decimal(0))
    _log(f"{args.env}: cash {'$%.2f' % cash if cash is not None else 'not checked'}; HALT: {store.halted() or 'no'}")
    _log(f"orders: {len(sent)} sent ({len(filled)} filled, ${spent:.2f} spent incl. fees); {len(today)} today (UTC)")
    for r in orders[-args.last:]:
        _log(f"  {iso_utc(r['at'])[:19]}Z {r['ticker']} {r['side']} {r['status']}"
             + (f" x{r.get('filled')} for ${_d(r.get('cost_usd')) + _d(r.get('fees_usd')):.2f}" if r.get("filled") else "")
             + (f" ({r.get('reason') or r.get('error') or ''})" if r["status"] in ("blocked", "rejected", "error", "unknown") else ""))
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import serve
    serve(_store(args), port=args.port)
    return 0


def cmd_fund_shard(args) -> int:
    from .funding import fund_shard
    ok = fund_shard(_client(args.env), _store(args), args.amount, from_shard=args.from_shard, to_shard=args.to_shard,
                    confirm=(lambda prompt: "yes") if args.yes else input, log=_log)
    return 0 if ok else 1


def cmd_halt(args) -> int:
    store = _store(args)
    store.halt(args.reason)
    _log(f"halted: no new orders until `python3 -m kalshi_live resume --env {args.env}`")
    return 0


def cmd_resume(args) -> int:
    store = _store(args)
    _log("orders allowed again" if store.resume() else "was not halted")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m kalshi_live", description="Real Kalshi orders for the kalshi_paper rules.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--env", choices=("demo", "live"), required=True)
        p.add_argument("--data-dir", default=None, help="default data/trading-<env>")
        return p

    common(sub.add_parser("smoke", help="exchange check with 1-cent orders that are cancelled"))
    r = common(sub.add_parser("run", help="trade the rules with real orders"))
    r.add_argument("--dry-run", action="store_true", help="log the orders it would send, send none")
    r.add_argument("--interval", type=float, default=2.0)
    r.add_argument("--max-order-pct", type=float, default=25.0,
                   help="most one order may cost, as a %% of equity (default 25; 0 = off)")
    r.add_argument("--daily-loss-pct", type=float, default=30.0,
                   help="stop for the UTC day once this bot's own trades today lost this %% of the day's starting equity (default 30; 0 = off)")
    r.add_argument("--max-order", type=float, default=None, help="also cap one order at this many dollars (default: no dollar cap)")
    r.add_argument("--daily-loss", type=float, default=None, help="also stop for the day after losing this many dollars (default: no dollar cap)")
    r.add_argument("--stop-below", type=float, default=100.0, help="HALT if equity falls below this (default 100)")
    r.add_argument("--max-orders-per-day", type=int, default=60)
    r.add_argument("--cushion", type=float, default=0.01, help="pay up to this much over the shown ask (default 0.01)")
    r.add_argument("--hold", type=float, default=4.0,
                   help="seconds a trigger must stay on before an order (default 4; 0 = order on the first look)")
    s = common(sub.add_parser("status", help="cash, recent orders, HALT state"))
    s.add_argument("--last", type=int, default=10)
    s.add_argument("--offline", action="store_true", help="don't call Kalshi, just read the logs")
    db = common(sub.add_parser("dashboard", help="live-account dashboard on 127.0.0.1 (no API key needed)"))
    db.add_argument("--port", type=int, default=8766)
    fs = common(sub.add_parser("fund-shard", help="move your cash to the exchange shard the bot trades on"))
    fs.add_argument("--amount", required=True, help='dollars to move, or "all"')
    fs.add_argument("--from", dest="from_shard", type=int, default=0, help="shard to take it from (default 0)")
    fs.add_argument("--to", dest="to_shard", type=int, default=None, help="shard to move it to (default: KXBTC15M's)")
    fs.add_argument("--yes", action="store_true", help="don't ask before moving")
    h = common(sub.add_parser("halt", help="stop placing orders"))
    h.add_argument("--reason", default="halted by hand")
    common(sub.add_parser("resume", help="allow orders again"))
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if args.interval < 1:
            ap.error("--interval must be at least 1 second")
        if not 0 <= args.cushion <= 0.05:
            ap.error("--cushion must be between 0 and 0.05")
        if (args.max_order is not None and args.max_order <= 0) or (args.daily_loss is not None and args.daily_loss <= 0) \
                or args.stop_below < 0:
            ap.error("limits must be positive")
        if not (0 <= args.max_order_pct <= 100 and 0 <= args.daily_loss_pct <= 100):
            ap.error("--max-order-pct and --daily-loss-pct must be between 0 and 100")
        if not args.max_order_pct and args.max_order is None:
            ap.error("set --max-order-pct or --max-order: orders need a cap")
        if not args.daily_loss_pct and args.daily_loss is None:
            ap.error("set --daily-loss-pct or --daily-loss: the bot needs a daily stop")
        if not 0 <= args.hold <= 60:
            ap.error("--hold must be between 0 and 60 seconds")
    return {"smoke": cmd_smoke, "run": cmd_run, "status": cmd_status, "halt": cmd_halt,
            "resume": cmd_resume, "dashboard": cmd_dashboard, "fund-shard": cmd_fund_shard}[args.cmd](args)
