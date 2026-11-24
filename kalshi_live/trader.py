"""Turns the paper rules' ACT decisions into real orders, behind hard limits.

An ACT from kalshi_paper.evaluator (unchanged rules) that has held for the `--hold` time (see
Hold; 4 seconds by default) becomes at most one immediate-or-cancel limit BUY: the displayed ask
plus a small cushion (only if the trade still has edge after it).
Whatever doesn't fill at once is cancelled by the exchange, so nothing is left resting.

Before every order, in this order (the first that fails blocks it and is logged):
  synthetic snapshot / other rules version   never trade on test data or a mismatched version
  HALT file present                          `python3 -m kalshi_live halt` or a safety stop wrote it
  this window already has an order           also across restarts (the order log is read back)
  fresh balance unavailable                  no order without knowing the cash
  equity below the floor                     writes HALT: needs `resume` by hand
  today's loss limit reached                 this bot's OWN settled trades today; no more orders until the next UTC day
  today's order cap reached
  order too small after the caps             cost cap per order, and never more than the cash

After an order the bot reads it back from Kalshi. If the exchange recorded a different market,
side or price than intended, or a cost above the cap, it writes HALT and stops ordering.

The account is shared with the gold bot (gold-bot's gold_live), so neither bot's money looks
like the other's loss:
- The daily loss limit counts only this bot's own orders, from Kalshi's published results.
- Equity (for the floor) = cash + the larger of: the cost of this bot's filled orders whose
  window hasn't had time to settle (5 minutes after close), and Kalshi's value of every open
  position on the account (both bots). So a just-placed order, or money the gold bot has tied
  up in a trade, doesn't look like a loss.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from kalshi_paper.evaluator import EvalState, evaluate
from kalshi_paper.model import taker_fee_per_contract
from kalshi_paper.storage import append_jsonl, read_jsonl
from kalshi_paper.timeutil import iso_utc, parse_iso_utc, utc_date

from .api import MAX_PRICE, ApiError, NetworkError

SETTLE_GRACE_S = 300          # a filled window counts as still open until 5 minutes after close
SETTLE_AFTER_S = 60           # ask for a filled window's result from 1 minute after it closes
SETTLE_RETRY_S = 15           # ...and again every 15 s until Kalshi publishes it
BALANCE_EVERY_S = 15          # cash check between orders (the balance endpoint allows 1 call per second)
DECISION_SAMPLE_S = 60        # SKIP decisions are logged once a minute (ACTs always) to keep the disk small
MAX_CONSECUTIVE_API_ERRORS = 30


@dataclass(frozen=True)
class Limits:
    """Hard limits. The order cap and the daily loss stop can be dollars, a share of the account, or both
    (the tighter one applies; None = not used). A share keeps the limit in proportion as the account grows
    or shrinks: `run` uses 25% of equity per order and 30% of the day's starting equity by default."""
    max_order_usd: Decimal | None = Decimal("25")   # most one order can cost, fees included
    daily_loss_usd: Decimal | None = Decimal("40")  # stop for the rest of the UTC day once this bot's own trades today lost this much
    stop_below_usd: Decimal = Decimal("100")        # stop for good (HALT) if equity falls below this
    max_orders_per_day: int = 60                    # the backtest's busiest month averaged ~27 a day
    price_cushion: Decimal = Decimal("0.01")        # pay up to this much above the displayed ask
    max_order_pct: Decimal | None = None            # e.g. 0.25: one order costs at most 25% of equity
    daily_loss_pct: Decimal | None = None           # e.g. 0.30: stop for the day after losing 30% of the day's starting equity

    def order_cap(self, equity) -> Decimal:
        """Most one order may cost now."""
        caps = [c for c in (self.max_order_usd,
                            self.max_order_pct * Decimal(equity) if self.max_order_pct is not None else None) if c is not None]
        return min(caps) if caps else Decimal("Infinity")

    def loss_cap(self, day_start_equity) -> Decimal:
        """How much this bot may lose today (UTC) before it stops for the day."""
        caps = [c for c in (self.daily_loss_usd,
                            self.daily_loss_pct * Decimal(day_start_equity) if self.daily_loss_pct is not None else None)
                if c is not None]
        return min(caps) if caps else Decimal("Infinity")

    def describe(self) -> dict:
        """For the dashboard (live-<env>.json): each setting, as text."""
        t = lambda v: None if v is None else str(v)
        return {"max_order_usd": t(self.max_order_usd), "daily_loss_usd": t(self.daily_loss_usd),
                "max_order_pct": t(self.max_order_pct), "daily_loss_pct": t(self.daily_loss_pct),
                "stop_below_usd": str(self.stop_below_usd), "max_orders_per_day": self.max_orders_per_day,
                "price_cushion": str(self.price_cushion)}


class Store:
    """Files in the trading data directory. The order log is append-only: one line per state change."""

    def __init__(self, data_dir, env: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.env = env
        self.orders_path = self.dir / f"orders-{env}.jsonl"
        self.halt_path = self.dir / "HALT"
        self.day_path = self.dir / f"day-{env}.json"
        self.smoke_path = self.dir / f"smoke-{env}.json"

    def log(self, rec: dict) -> None:
        append_jsonl(self.orders_path, rec)

    def orders(self) -> list:
        """Latest state of every order attempt, oldest first."""
        latest = {}
        for r in read_jsonl(self.orders_path):
            latest[r["id"]] = r
        return sorted(latest.values(), key=lambda r: r["at"])

    def ordered_tickers(self) -> set:
        return {r["ticker"] for r in self.orders() if r["status"] != "blocked"}

    def orders_on(self, day: str) -> list:
        return [r for r in self.orders() if r["status"] != "blocked" and utc_date(r["at"]) == day]

    def halted(self) -> str | None:
        if not self.halt_path.exists():
            return None
        try:
            return json.loads(self.halt_path.read_text()).get("reason") or "halted"
        except ValueError:
            return "halted"

    def halt(self, reason: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.halt_path.write_text(json.dumps({"at": now, "at_iso": iso_utc(now), "reason": reason}, indent=2))

    def resume(self) -> bool:
        if self.halt_path.exists():
            self.halt_path.unlink()
            return True
        return False

    def day_start(self, day: str, equity: Decimal) -> Decimal:
        """Equity at the first check of `day` (UTC), saved so a restart keeps the same baseline."""
        try:
            saved = json.loads(self.day_path.read_text())
        except (OSError, ValueError):
            saved = {}
        if saved.get("day") != day:
            saved = {"day": day, "start_equity": str(equity)}
            self.day_path.write_text(json.dumps(saved, indent=2))
        return Decimal(saved["start_equity"])

    @property
    def live_path(self) -> Path:
        return self.dir / f"live-{self.env}.json"

    @property
    def settlements_path(self) -> Path:
        return self.dir / "settlements.jsonl"

    def write_live(self, state: dict) -> None:
        tmp = self.live_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.live_path)  # atomic: the dashboard never reads half a file

    def read_live(self) -> dict | None:
        try:
            return json.loads(self.live_path.read_text())
        except (OSError, ValueError):
            return None

    def smoke_passed(self, key_id: str) -> bool:
        try:
            s = json.loads(self.smoke_path.read_text())
        except (OSError, ValueError):
            return False
        return bool(s.get("passed")) and s.get("key_id") == key_id


def _d(x, default="0") -> Decimal:
    return Decimal(str(x if x not in (None, "") else default))


class Hold:
    """A trigger must stay on for `seconds` before it becomes an order (`run --hold`, 0 = off).

    The rules trigger on a single look. In the paper bot's look-by-look logs (2026-11-06 to 11-09:
    80 windows, 167 triggers) about half the triggers were gone by the next look 2 s later. So the
    live loop orders only once the rules have said ACT for the same market and side on every look
    for `seconds`. Any other answer in between (a skip of any kind, the other side, a new window,
    a gap between looks longer than the hold) starts the count again.

    Until the hold is complete the ACT is turned into SKIP trigger_not_held. The evaluator then
    doesn't count the window as traded, and the order is sized and priced from the look that
    completes the hold, which has passed every rule again at that moment's prices.
    """

    SLACK_S = 0.5  # looks are about 2 s apart, give or take: 3.6 s after the first look counts as 4

    def __init__(self, seconds: float, interval: float = 2.0, log=print):
        self.seconds, self.log = float(seconds), log
        self.max_gap = max(self.seconds, 2 * interval)  # a longer pause between two looks starts the count again
        self.key = None            # (market, side) of the trigger being watched
        self.since = self.last = 0.0

    def _drop(self) -> None:
        if self.key is not None:
            self.log(f"{iso_utc(self.last)[11:19]}Z {self.key[0]} {self.key[1]} trigger gone after "
                     f"{self.last - self.since:.0f}s: no order")
        self.key = None

    def check(self, dec: dict) -> dict:
        """The decision to act on: `dec` itself, or a SKIP while its trigger hasn't held long enough."""
        if self.seconds <= 0:
            return dec
        now = dec.get("evaluated_at")
        if dec.get("decision") != "ACT" or now is None:
            self._drop()
            return dec
        i = dec["intent"]
        key = (dec["market_ticker"], i["side"])
        if key != self.key or not 0 <= now - self.last <= self.max_gap:
            self._drop()
            self.key, self.since = key, now
            self.log(f"{iso_utc(now)[11:19]}Z {key[0]} trigger: {key[1]} at {i['limit_price']} "
                     f"(edge {i['edge_per_contract'] * 100:+.1f}c); ordering if it holds {self.seconds:g}s")
        self.last = now
        held = now - self.since
        info = {"side": key[1], "since": self.since, "held_s": round(held, 2), "need_s": self.seconds}
        if held >= self.seconds - self.SLACK_S:
            self.key = None
            return {**dec, "hold": info}
        return {**dec, "decision": "SKIP", "reason": "trigger_not_held",
                "detail": f"{key[1]} trigger seen for {held:.0f}s; it must hold {self.seconds:g}s",
                "intent": None, "waiting_intent": i, "hold": info}


class Trader:
    def __init__(self, client, store: Store, limits: Limits, rules, dry_run: bool = False, log=print,
                 clock=time.time, settle_fetch=None):
        self.client, self.store, self.limits, self.rules = client, store, limits, rules
        self.dry_run, self.log, self.clock = dry_run, log, clock
        self.settle_fetch = settle_fetch        # ticker -> Kalshi's result dict or None (default: public market data)
        self._settle_retry: dict = {}
        self.cash: Decimal | None = None        # cash on the market's exchange shard: what an order can use
        self.total_cash: Decimal | None = None  # cash across all shards (what kalshi.com shows)
        self.positions: Decimal | None = None   # Kalshi's value of every open position on the account (both bots)
        self.shard: int | None = None           # the market's exchange shard (crypto is 2)
        self._cash_at = 0.0

    # -- account -----------------------------------------------------------------------------
    def refresh_shard(self) -> None:
        """Ask Kalshi which exchange shard KXBTC15M trades on (once a window; keeps the last answer on error)."""
        try:
            s = self.client.market_shard()
        except (ApiError, NetworkError, KeyError, TypeError, ValueError) as e:
            self.log(f"could not check the market's exchange shard: {e}")
            return
        if s is not None and s != self.shard:
            self.shard = s
            self.cash = None  # re-read the cash for this shard
            self.log(f"KXBTC15M trades on Kalshi exchange shard {s}; orders can only use the cash held there")

    def refresh_balance(self, force: bool = False) -> bool:
        now = self.clock()
        if not force and self.cash is not None and now - self._cash_at < BALANCE_EVERY_S:
            return True
        try:
            total, shards, positions = self.client.account()
            self.total_cash, self.positions, self._cash_at = total, positions, now
            self.cash = shards.get(self.shard, Decimal(0)) if (self.shard is not None and shards) else total
            return True
        except ApiError as e:
            self.log(f"balance check failed: {e}")
            if e.status in (401, 403):
                self.store.halt(f"Kalshi rejected the API key (HTTP {e.status})", now)
        except (NetworkError, KeyError, ValueError, TypeError) as e:
            self.log(f"balance check failed: {e}")
        if force:
            self.cash = None
        return False

    def underfunded_shard(self) -> bool:
        """The money is in the account but not on the market's shard: a funding problem, not a loss."""
        if self.shard is None or self.total_cash is None or self.cash is None:
            return False
        return self.cash < self.limits.stop_below_usd <= self.total_cash

    def equity(self, now: float) -> Decimal:
        """Cash + the larger of this bot's unsettled orders at cost and every open position on the account."""
        pending = sum((_d(r.get("cost_usd")) + _d(r.get("fees_usd")) for r in self.store.orders()
                       if r.get("filled") and r.get("close_ts") and r["close_ts"] + SETTLE_GRACE_S > now),
                      Decimal(0))
        held = pending if self.positions is None else max(pending, self.positions)
        return (self.cash or Decimal(0)) + held

    def today_pnl(self, now: float) -> Decimal:
        """This bot's own result today (UTC): orders placed today whose window has settled.
        Unsettled orders count as zero. The gold bot's trades on the same account don't count."""
        day = utc_date(now)
        settled = {s["ticker"]: s for s in read_jsonl(self.store.settlements_path)}
        pnl = Decimal(0)
        for r in self.store.orders():
            f = _d(r.get("filled"))
            s = settled.get(r["ticker"])
            if f > 0 and s and utc_date(r["at"]) == day:
                won = (s["result"] == "yes") == (r["side"] == "YES")
                pnl += (f if won else Decimal(0)) - _d(r.get("cost_usd")) - _d(r.get("fees_usd"))
        return pnl

    def settle_due(self, now: float) -> int:
        """Fetch Kalshi's result for this bot's filled windows that have closed (cached in
        settlements.jsonl, the same file the dashboard uses). Returns how many were added."""
        have = {s["ticker"] for s in read_jsonl(self.store.settlements_path)}
        due = sorted({r["ticker"] for r in self.store.orders() if _d(r.get("filled")) > 0 and r.get("close_ts")
                      and r["close_ts"] + SETTLE_AFTER_S < now and r["ticker"] not in have
                      and now >= self._settle_retry.get(r["ticker"], 0)})
        if not due:
            return 0
        if self.settle_fetch is None:
            from kalshi_paper.http import UrllibTransport
            from kalshi_paper.scorer import fetch_settlement
            transport = UrllibTransport()
            self.settle_fetch = lambda t: fetch_settlement(t, transport)
        added = 0
        for t in due:
            self._settle_retry[t] = now + SETTLE_RETRY_S
            try:
                s = self.settle_fetch(t)
            except Exception as e:  # results are bookkeeping: never let them stop trading
                self.log(f"could not fetch the result for {t}: {type(e).__name__}: {e}")
                continue
            if s:
                append_jsonl(self.store.settlements_path, s)
                added += 1
        return added

    # -- one order ---------------------------------------------------------------------------
    def _blocked(self, dec: dict, now: float) -> str | None:
        if dec.get("synthetic"):
            return "synthetic_snapshot"
        if dec.get("rules_version") != self.rules.version:
            return "rules_version_mismatch"
        h = self.store.halted()
        if h:
            return f"halted: {h}"
        if dec["market_ticker"] in self.store.ordered_tickers():
            return "already_ordered_this_window"
        if not self.refresh_balance(force=True):
            return "balance_unavailable"
        eq = self.equity(now)
        if eq < self.limits.stop_below_usd and self.underfunded_shard():
            return (f"fund_exchange_shard: only ${self.cash:.2f} of your ${self.total_cash:.2f} is on exchange shard "
                    f"{self.shard}, where this market trades; move funds there on kalshi.com (Exchange Indexes)")
        if eq < self.limits.stop_below_usd:
            self.store.halt(f"equity ${eq:.2f} fell below the ${self.limits.stop_below_usd:.2f} floor", now)
            return "below_floor"
        day = utc_date(now)
        start = self.store.day_start(day, eq)  # the day's baseline (also the dashboard's)
        lost = -self.today_pnl(now)
        cap = self.limits.loss_cap(start)
        if lost >= cap:
            return f"daily_loss_limit (this bot is down ${lost:.2f} today; the limit is ${cap:.2f})"
        if len(self.store.orders_on(day)) >= self.limits.max_orders_per_day:
            return "daily_order_cap"
        return None

    def _size(self, intent: dict) -> tuple[int, Decimal, Decimal]:
        """(contracts, limit price, worst-case cost) after the cushion, the cost cap and the cash."""
        ask = Decimal(intent["limit_price"])
        edge = Decimal(str(intent["edge_per_contract"]))
        cushion = self.limits.price_cushion if edge - self.limits.price_cushion > 0 else Decimal(0)
        limit = min(ask + cushion, MAX_PRICE)
        rate = Decimal(str(self.rules.taker_fee_rate))
        budget = min(self.limits.order_cap(self.equity(self.clock())), self.cash or Decimal(0))

        def cost(k):
            return k * (limit + taker_fee_per_contract(limit, k, rate))

        n = int(intent["contracts"])
        n = min(n, math.floor(budget / (limit + Decimal("0.0001"))))  # quick upper bound, then exact
        while n >= 1 and cost(n) > budget:
            n -= 1
        return n, limit, cost(n) if n >= 1 else Decimal(0)

    def place(self, dec: dict) -> dict:
        """Act on one ACT decision. Never raises: anything unexpected writes HALT."""
        now = self.clock()
        i = dec["intent"]
        rec = {"id": f"kb-{uuid.uuid4().hex}", "at": now, "at_iso": iso_utc(now), "env": self.client.env,
               "ticker": dec["market_ticker"], "side": i["side"], "shown_ask": i["limit_price"],
               "snapshot_id": dec.get("snapshot_id"), "rules_version": dec.get("rules_version"),
               "close_ts": parse_iso_utc(dec["close_time"]) if dec.get("close_time") else None,
               "held_s": (dec.get("hold") or {}).get("held_s"), "hold_s": (dec.get("hold") or {}).get("need_s"),
               "dry_run": self.dry_run}
        try:
            return self._place(dec, rec, now)
        except Exception as e:  # fail closed
            rec.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
            self.store.log(rec)
            self.store.halt(f"unexpected error while ordering: {rec['error']}", now)
            self.log(f"ORDER ERROR {rec['ticker']}: {rec['error']} -> halted")
            return rec

    def _place(self, dec, rec, now):
        reason = self._blocked(dec, now)
        if reason:
            rec.update(status="blocked", reason=reason)
            self.store.log(rec)
            self.log(f"not ordering {rec['ticker']} {rec['side']}: {reason}")
            return rec
        n, limit, max_cost = self._size(dec["intent"])
        if n < 1:
            rec.update(status="blocked", reason="order_too_small", cash_usd=str(self.cash))
            self.store.log(rec)
            self.log(f"not ordering {rec['ticker']} {rec['side']}: can't afford one contract within the caps")
            return rec
        rec.update(status="sending", contracts=n, limit_price=str(limit), max_cost_usd=f"{max_cost:.4f}",
                   cash_usd=str(self.cash), client_order_id=rec["id"])
        self.store.log(rec)  # logged before sending: a crash mid-request never re-sends this window
        what = f"BUY {rec['side']} x{n} {rec['ticker']} at up to ${limit} (shown ${rec['shown_ask']}), max ${max_cost:.2f}"
        if self.dry_run:
            rec.update(status="dry_run")
            self.store.log(rec)
            self.log(f"DRY RUN, not sent: {what}")
            return rec
        try:
            resp = self.client.buy(rec["ticker"], rec["side"], limit, n, rec["id"])
        except NetworkError as e:
            rec.update(status="unknown", error=str(e)[:300])
            self.store.log(rec)
            self.log(f"ORDER UNKNOWN (no response; not retried, check `status`): {what}: {e}")
            return rec
        except ApiError as e:
            rec.update(status="rejected", http_status=e.status, error=e.body[:300])
            self.store.log(rec)
            self.log(f"ORDER REJECTED HTTP {e.status}: {what}: {e.body[:200]}")
            if "insufficient_balance" in e.body:
                self.log(f"  cash on exchange shard {self.shard}: ${self.cash}; total: ${self.total_cash}. API orders only use "
                         "the cash on the market's shard: move funds on kalshi.com (Exchange Indexes)")
            if e.status in (401, 403):
                self.store.halt(f"Kalshi rejected the API key (HTTP {e.status})", now)
            return rec
        rec.update(order_id=resp.get("order_id"), fill_count=resp.get("fill_count"),
                   average_fill_price=resp.get("average_fill_price"), average_fee_paid=resp.get("average_fee_paid"))
        self._verify(rec, n, limit, max_cost, now)
        self.store.log(rec)
        self.cash = None  # re-read before the next order
        self.log(f"ORDER {rec['status'].upper()}: {what}; filled {rec.get('filled')} for "
                 f"${_d(rec.get('cost_usd')) + _d(rec.get('fees_usd')):.2f}"
                 + (f"; HALTED: {rec['halt_reason']}" if rec.get("halt_reason") else ""))
        return rec

    def _verify(self, rec, n, limit, max_cost, now):
        """Read the order back and check Kalshi recorded what we meant. Fills in filled/cost/fees."""
        o = None
        try:
            o = self.client.find_order(rec["order_id"], rec["ticker"], rec["id"])
        except (ApiError, NetworkError) as e:
            rec["verify_error"] = str(e)[:300]
        problems = []
        if o is None:
            filled = _d(rec.get("fill_count"))
            fee = _d(rec.get("average_fee_paid"))
            rec.update(verified=False, filled=str(filled), cost_usd=f"{filled * limit:.4f}",
                       fees_usd=f"{filled * fee:.4f}", cost_estimated=True)
        else:
            side = rec["side"].lower()
            if o.get("ticker") not in (None, rec["ticker"]):
                problems.append(f"market {o.get('ticker')} instead of {rec['ticker']}")
            if o.get("outcome_side") not in (None, side):
                problems.append(f"outcome_side {o.get('outcome_side')} instead of {side}")
            recorded = o.get(f"{side}_price_dollars")
            if recorded not in (None, "") and Decimal(str(recorded)) != limit:
                problems.append(f"{side} price {recorded} instead of {limit}")
            filled = _d(o.get("fill_count_fp"), rec.get("fill_count") or "0")
            cost = _d(o.get("taker_fill_cost_dollars")) + _d(o.get("maker_fill_cost_dollars"))
            fees = _d(o.get("taker_fees_dollars")) + _d(o.get("maker_fees_dollars"))
            if filled > n:
                problems.append(f"filled {filled} contracts, more than the {n} ordered")
            if cost + fees > max_cost + Decimal("0.02"):
                problems.append(f"cost ${cost + fees:.2f} above the ${max_cost:.2f} cap")
            rec.update(verified=not problems, filled=str(filled), cost_usd=str(cost), fees_usd=str(fees),
                       order_status=o.get("status"))
        f = _d(rec["filled"])
        rec["status"] = "filled" if f >= n else "partly_filled" if f > 0 else "not_filled"
        if problems:
            rec["halt_reason"] = "order check failed: " + "; ".join(problems)
            self.store.halt(rec["halt_reason"], now)

    def write_state(self, dec: dict, window: list) -> None:
        """live-<env>.json for the dashboard: cash, mode, HALT and the latest check. Rewritten every loop."""
        now = self.clock()
        f = dec.get("features") or {}
        mom = f.get("momentum") or {}
        state = {
            "at": now, "env": self.client.env, "dry_run": self.dry_run, "rules_version": self.rules.version,
            "cash": str(self.cash) if self.cash is not None else None, "equity": str(self.equity(now)),
            "total_cash": str(self.total_cash) if self.total_cash is not None else None, "exchange_index": self.shard,
            "halted": self.store.halted(),
            "limits": self.limits.describe(),
            "day_start_equity": str(self.store.day_start(utc_date(now), self.equity(now))) if self.cash is not None else None,
            "decision": {"at": dec.get("evaluated_at"), "ticker": dec.get("market_ticker"), "close_time": dec.get("close_time"),
                         "decision": dec.get("decision"), "reason": dec.get("reason"), "detail": dec.get("detail"),
                         "seconds_left": f.get("seconds_left"), "spot": f.get("spot_adjusted"), "strike": f.get("target_price"),
                         "p_fair": f.get("p_fair"), "yes_bid": f.get("best_yes_bid"), "yes_ask": f.get("yes_ask"),
                         "no_ask": f.get("no_ask"), "momentum_z": mom.get("z"), "trend": mom.get("trend"),
                         "edge_yes": f.get("edge_yes"), "edge_no": f.get("edge_no")},
            "window": window,
        }
        self.store.write_live(state)

    def heartbeat(self, dec: dict) -> str:
        now = self.clock()
        orders = self.store.orders_on(utc_date(now))
        filled = [r for r in orders if r.get("filled") and _d(r["filled"]) > 0]
        cash = f"${self.cash:.2f}" if self.cash is not None else "unknown"
        if self.shard is not None and self.total_cash is not None:
            cash += f" on shard {self.shard} (${self.total_cash:.2f} total)"
        h = self.store.halted()
        return (f"{iso_utc(now)[11:19]}Z cash {cash}, equity ${self.equity(now):.2f}; today {len(orders)} order(s), "
                f"{len(filled)} filled; last check: {dec['decision']} {dec['reason']}"
                + (f"; HALTED: {h}" if h else "") + (" [DRY RUN]" if self.dry_run else ""))


def run(trader: Trader, collector, decisions_path: Path, interval: float, log=print, clock=time.time,
        sleep=time.sleep, max_loops: int | None = None, hold_s: float = 0.0) -> int:
    """Collect -> evaluate (the paper rules, sized on real cash) -> order on an ACT that has held
    `hold_s` seconds (0 = on the first ACT). Ctrl-C/SIGTERM to stop."""
    rules = trader.rules
    hold = Hold(hold_s, interval, log)
    state = EvalState.from_decisions(read_jsonl(decisions_path))
    errors = loops = 0
    last_logged = last_beat = 0.0
    window, window_ticker = [], None  # this window's [time, model chance, YES bid, YES ask] for the dashboard
    trader.refresh_shard()
    while max_loops is None or loops < max_loops:
        loops += 1
        t0 = clock()
        trader.refresh_balance()
        try:
            trader.settle_due(t0)  # this bot's own results, for its daily loss limit
        except Exception as e:  # bookkeeping must never stop trading
            log(f"could not update results: {type(e).__name__}: {e}")
        snap = collector.collect()
        state.bankroll = float(trader.cash) if trader.cash is not None else None
        dec = hold.check(evaluate(snap, rules, state))
        state.record(dec)
        f = dec.get("features")
        if dec.get("market_ticker") != window_ticker:
            if window_ticker is not None and dec.get("market_ticker"):
                trader.refresh_shard()  # once a window, in case Kalshi moves the series to another shard
            window, window_ticker = [], dec.get("market_ticker")
        if f:
            window.append([round(dec["evaluated_at"], 1), round(f["p_fair"], 4), float(f["best_yes_bid"]), float(f["yes_ask"])])
            del window[:-500]
        try:
            trader.write_state(dec, window)
        except Exception as e:  # the dashboard file must never stop trading
            log(f"could not write the dashboard file: {type(e).__name__}: {e}")
        # Every look of a trigger is kept (ACTs and the looks spent waiting for the hold); other SKIPs are sampled.
        if dec["decision"] == "ACT" or dec.get("hold") or t0 - last_logged >= DECISION_SAMPLE_S:
            append_jsonl(decisions_path, dec)
            last_logged = t0
        if dec["decision"] == "ACT":
            i = dec["intent"]
            log(f"{dec['evaluated_at_iso'][11:19]}Z {dec['market_ticker']} ACT: {i['side']} x{i['contracts']} "
                f"at {i['limit_price']} (edge {i['edge_per_contract'] * 100:+.1f}c)"
                + (f", held {dec['hold']['held_s']:.0f}s" if dec.get("hold") else ""))
            trader.place(dec)
        if t0 - last_beat >= 60:
            log(trader.heartbeat(dec))
            last_beat = t0
        errors = errors + 1 if (dec["reason"] or "").startswith("api_error") else 0
        if errors >= MAX_CONSECUTIVE_API_ERRORS:
            log(f"stopping: {errors} consecutive market-data errors; check connectivity")
            return 2
        delay = interval if errors == 0 else min(60.0, interval * 2 ** errors)
        sleep(max(0.0, delay - (clock() - t0)))
    return 0
