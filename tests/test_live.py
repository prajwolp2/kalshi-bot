"""kalshi_live against a fake exchange: order mapping, limits, read-back checks, restarts. No network."""

import copy
import dataclasses
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from kalshi_live import api
from kalshi_live.api import ApiError, Client, NetworkError
from kalshi_live.cli import live_gate
from kalshi_live.smoke import smoke
from kalshi_live.trader import Hold, Limits, Store, Trader, run
from kalshi_paper import demo
from kalshi_paper.collector import Collector
from kalshi_paper.demo import run_scenarios
from kalshi_paper.rules import RULES
from kalshi_paper.storage import append_jsonl
from kalshi_paper.timeutil import parse_iso_utc, utc_date

PKG = Path(__file__).resolve().parent.parent


class FakeSigner:
    key_id = "test-key"

    def headers(self, method, path, ts_ms):
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
                "KALSHI-ACCESS-SIGNATURE": f"sig:{method}:{path}"}


class FakeExchange:
    """Answers like the Kalshi trade API. Records every request."""

    def __init__(self, cash_cents=20000, fill=True, record_side=None, status_for=None, list_orders=True):
        self.cash_cents, self.fill, self.record_side = cash_cents, fill, record_side
        self.status_for = status_for or {}
        self.list_orders = list_orders  # False: the order list never shows new orders either
        self.shards = None              # {exchange_index: "dollars"}: sent as balance_breakdown when set
        self.positions_cents = 0        # portfolio_value: every open position on the account (both bots)
        self.requests, self.orders = [], {}
        self.markets = []

    def __call__(self, method, url, headers, data, timeout):
        parts = urlsplit(url)
        path = parts.path.removeprefix("/trade-api/v2")
        body = json.loads(data) if data else None
        self.requests.append((method, path, body, headers))
        key = (method, path.split("/orders/")[0] + ("/orders/{id}" if "/orders/" in path else ""))
        if key in self.status_for:
            st = self.status_for[key]
            if st == "network":
                raise NetworkError("timed out")
            return st, '{"error": "nope"}'
        if path == "/portfolio/balance":
            r = {"balance": self.cash_cents, "portfolio_value": self.positions_cents, "updated_ts": 0}
            if self.shards is not None:
                r["balance_breakdown"] = [{"exchange_index": k, "balance": v} for k, v in self.shards.items()]
            return 200, json.dumps(r)
        if path == "/markets":
            return 200, json.dumps({"markets": self.markets})
        if method == "POST" and path == "/portfolio/events/orders":
            oid = f"ord-{len(self.orders) + 1}"
            yes_price = Decimal(body["price"])
            outcome = self.record_side or ("yes" if body["side"] == "bid" else "no")
            price = yes_price if outcome == "yes" else 1 - yes_price
            n = Decimal(body["count"])
            resting = body["time_in_force"] == "good_till_canceled"
            filled = Decimal(0) if (resting or not self.fill) else n
            fee = Decimal("0.02")
            self.orders[oid] = {"order_id": oid, "client_order_id": body.get("client_order_id"),
                                "ticker": body["ticker"], "outcome_side": outcome,
                                "book_side": body["side"], "status": "resting" if resting else "executed",
                                "yes_price_dollars": f"{1 - price if outcome == 'no' else price:.4f}",
                                "no_price_dollars": f"{price if outcome == 'no' else 1 - price:.4f}",
                                "fill_count_fp": f"{filled:.2f}", "initial_count_fp": f"{n:.2f}",
                                "taker_fill_cost_dollars": f"{filled * price:.4f}",
                                "taker_fees_dollars": f"{fee if filled else 0:.4f}"}
            self.cash_cents -= int((filled * price + (fee if filled else 0)) * 100)
            return 201, json.dumps({"order_id": oid, "fill_count": f"{filled:.2f}",
                                    "remaining_count": "0.00", "ts_ms": 0})
        if method == "POST" and path == "/portfolio/intra_exchange_instance_transfer":
            amt = Decimal(body["amount"]) / 10000
            src, dst = body["source_exchange_shard"], body["destination_exchange_shard"]
            if Decimal(self.shards.get(src, "0")) < amt:
                return 400, '{"error": {"code": "insufficient_balance"}}'
            self.shards[src] = str(Decimal(self.shards[src]) - amt)
            self.shards[dst] = str(Decimal(self.shards.get(dst, "0")) + amt)
            return 200, json.dumps({"transfer_id": "tr-1"})
        if method == "GET" and path == "/portfolio/orders":
            t = parse_qs(parts.query).get("ticker", [None])[0]
            found = [o for o in self.orders.values() if o["ticker"] == t] if self.list_orders else []
            return 200, json.dumps({"orders": found, "cursor": ""})
        if method == "GET" and path.startswith("/portfolio/orders/"):
            return 200, json.dumps({"order": self.orders[path.rsplit("/", 1)[1]]})
        if method == "DELETE" and path.startswith("/portfolio/events/orders/"):
            oid = path.rsplit("/", 1)[1]
            self.orders[oid]["status"] = "canceled"
            return 200, json.dumps({"order_id": oid, "reduced_by": "1.00", "ts_ms": 0})
        return 404, "{}"

    def posts(self):
        return [b for m, p, b, _ in self.requests if m == "POST"]


def acts():
    """The two synthetic ACT decisions (buy YES, buy NO), marked real so the trader may use them."""
    out = []
    for _, _, d in run_scenarios():
        if d["decision"] == "ACT":
            d = copy.deepcopy(d)
            d["synthetic"] = False
            d.pop("label", None)
            out.append(d)
    return out


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class ClientTests(unittest.TestCase):
    def client(self, ex):
        return Client("live", FakeSigner(), send=ex, clock=lambda: 1000.0, sleep=lambda s: None)

    def test_buy_yes_is_a_bid_at_the_yes_price(self):
        ex = FakeExchange()
        self.client(ex).buy("KXBTC15M-26NOV081615-15", "YES", Decimal("0.67"), 10, "c1")
        b = ex.posts()[0]
        self.assertEqual((b["side"], b["price"], b["count"], b["time_in_force"]),
                         ("bid", "0.6700", "10", "immediate_or_cancel"))

    def test_buy_no_is_an_ask_at_one_minus_the_no_price(self):
        ex = FakeExchange()
        self.client(ex).buy("KXBTC15M-26NOV081615-15", "NO", Decimal("0.35"), 12, "c1")
        b = ex.posts()[0]
        self.assertEqual((b["side"], b["price"], b["count"]), ("ask", "0.6500", "12"))

    def test_signs_the_path_without_query_and_never_retries_an_order(self):
        ex = FakeExchange(status_for={("POST", "/portfolio/events/orders"): 429})
        c = self.client(ex)
        with self.assertRaises(ApiError):
            c.buy("KXBTC15M-26NOV081615-15", "YES", Decimal("0.5"), 10, "c1")
        self.assertEqual(len(ex.posts()), 1)
        c.open_markets()
        _, path, _, headers = ex.requests[-1]
        self.assertEqual(headers["KALSHI-ACCESS-SIGNATURE"], "sig:GET:/trade-api/v2/markets")

    def test_reads_retry_when_rate_limited(self):
        ex = FakeExchange(status_for={("GET", "/portfolio/balance"): 429})
        with self.assertRaises(ApiError):
            self.client(ex).balance()
        self.assertEqual(sum(1 for m, p, _, _ in ex.requests if p == "/portfolio/balance"), 3)

    def test_refuses_bad_orders_before_sending(self):
        ex = FakeExchange()
        c = self.client(ex)
        for args in (("KXETH15M-26NOV081615-15", "YES", Decimal("0.5"), 10),
                     ("KXBTC15M-26NOV081615-15", "BUY", Decimal("0.5"), 10),
                     ("KXBTC15M-26NOV081615-15", "YES", Decimal("1.00"), 10),
                     ("KXBTC15M-26NOV081615-15", "YES", Decimal("0.00"), 10),
                     ("KXBTC15M-26NOV081615-15", "YES", Decimal("0.5"), 0),
                     ("KXBTC15M-26NOV081615-15", "YES", Decimal("0.5"), 2.5)):
            with self.subTest(args), self.assertRaises(ValueError):
                c.buy(*args, "c1")
        self.assertEqual(ex.requests, [])

    def test_only_buys_and_only_kalshi_hosts(self):
        names = [n for n in dir(Client) if not n.startswith("__")]
        self.assertFalse([n for n in names if any(w in n.lower() for w in ("sell", "withdraw", "transfer", "deposit"))])
        # The one money-moving call stays inside the account and only the fund-shard command uses it.
        for f in (PKG / "kalshi_live").glob("*.py"):
            if f.name not in ("api.py", "funding.py"):
                self.assertNotIn("move_between_shards", f.read_text(), f.name)
        text = (PKG / "kalshi_live" / "api.py").read_text()
        self.assertNotIn('"action"', text)
        for url in ("https://example.com/trade-api/v2/portfolio/balance",
                    "http://external-api.kalshi.com/trade-api/v2/portfolio/balance",
                    "https://external-api.kalshi.com.evil.example/x"):
            with self.subTest(url), self.assertRaises(ValueError):
                api.urllib_send("GET", url, {}, None, 1.0)


class TraderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.yes, cls.no = acts()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(self.yes["evaluated_at"])
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def trader(self, ex, limits=Limits(), dry_run=False, env="live"):
        client = Client(env, FakeSigner(), send=ex, clock=self.clock, sleep=lambda s: None)
        return Trader(client, Store(self.tmp.name, env), limits, RULES, dry_run=dry_run,
                      log=self.lines.append, clock=self.clock)

    def test_act_becomes_one_verified_ioc_buy(self):
        ex = FakeExchange()
        t = self.trader(ex)
        rec = t.place(self.yes)
        self.assertEqual(rec["status"], "filled", rec)
        self.assertTrue(rec["verified"])
        self.assertEqual(rec["contracts"], self.yes["intent"]["contracts"])
        b = ex.posts()[0]
        self.assertEqual(b["side"], "bid")
        self.assertEqual(Decimal(b["price"]), Decimal(self.yes["intent"]["limit_price"]) + Decimal("0.01"))
        self.assertEqual(b["time_in_force"], "immediate_or_cancel")
        self.assertIsNone(t.store.halted())

    def test_verifies_through_the_order_list_when_lookup_by_id_404s(self):
        ex = FakeExchange(status_for={("GET", "/portfolio/orders/{id}"): 404})
        rec = self.trader(ex).place(self.yes)
        self.assertEqual(rec["status"], "filled", rec)
        self.assertTrue(rec["verified"])
        self.assertIsNone(Store(self.tmp.name, "live").halted())

    def test_money_on_another_shard_is_a_funding_problem_not_a_loss(self):
        # What happened live: $200 in the account, only cents on the crypto shard (2), order rejected.
        ex = FakeExchange(cash_cents=20008)
        ex.shards = {0: "200.0000", 2: "0.0800"}
        ex.markets = [{"ticker": self.yes["market_ticker"], "exchange_index": 2}]
        t = self.trader(ex)
        t.refresh_shard()
        rec = t.place(self.yes)
        self.assertTrue(rec["reason"].startswith("fund_exchange_shard"), rec)
        self.assertIn("$0.08 of your $200.08", rec["reason"])
        self.assertIsNone(t.store.halted())  # not a loss: no HALT
        self.assertEqual(ex.posts(), [])

    def test_sizes_on_the_crypto_shard_cash(self):
        ex = FakeExchange(cash_cents=20008)
        ex.shards = {0: "0.0800", 2: "200.0000"}
        ex.markets = [{"ticker": self.yes["market_ticker"], "exchange_index": 2}]
        t = self.trader(ex)
        t.refresh_shard()
        rec = t.place(self.yes)
        self.assertEqual(rec["status"], "filled", rec)
        self.assertEqual(t.shard, 2)
        self.assertEqual(Decimal(rec["cash_usd"]), Decimal("200"))

    def test_no_side_maps_to_an_ask(self):
        ex = FakeExchange()
        rec = self.trader(ex).place(self.no)
        self.assertEqual(rec["status"], "filled", rec)
        b = ex.posts()[0]
        self.assertEqual(b["side"], "ask")
        self.assertEqual(Decimal(b["price"]), 1 - Decimal(rec["limit_price"]))

    def test_one_order_per_window_even_after_a_restart(self):
        ex = FakeExchange()
        self.trader(ex).place(self.yes)
        rec = self.trader(ex).place(self.yes)  # a fresh Trader reads the order log back
        self.assertEqual((rec["status"], rec["reason"]), ("blocked", "already_ordered_this_window"))
        self.assertEqual(len(ex.posts()), 1)

    def test_synthetic_and_other_rules_versions_never_trade(self):
        ex = FakeExchange()
        t = self.trader(ex)
        d = copy.deepcopy(self.yes)
        d["synthetic"] = True
        self.assertEqual(t.place(d)["reason"], "synthetic_snapshot")
        d = copy.deepcopy(self.yes)
        d["rules_version"] = "v0.0.1"
        self.assertEqual(t.place(d)["reason"], "rules_version_mismatch")
        self.assertEqual(ex.posts(), [])

    def test_halt_blocks_and_resume_allows(self):
        ex = FakeExchange()
        t = self.trader(ex)
        t.store.halt("by hand")
        self.assertEqual(t.place(self.yes)["reason"], "halted: by hand")
        self.assertTrue(t.store.resume())
        self.assertEqual(t.place(self.yes)["status"], "filled")

    def test_equity_below_floor_halts(self):
        ex = FakeExchange(cash_cents=9000)
        t = self.trader(ex)
        self.assertEqual(t.place(self.yes)["reason"], "below_floor")
        self.assertIn("below", t.store.halted())
        self.assertEqual(ex.posts(), [])

    def _lost_today(self, store, cost):
        """One of this bot's own orders earlier today that lost `cost` dollars."""
        now = self.clock()
        store.log({"id": "earlier", "at": now - 1800, "ticker": "KXBTC15M-EARLIER", "side": "YES", "status": "filled",
                   "filled": "50", "cost_usd": f"{cost:.2f}", "fees_usd": "0.00", "close_ts": now - 1200})
        append_jsonl(store.settlements_path, {"ticker": "KXBTC15M-EARLIER", "result": "no"})

    def test_daily_loss_limit_counts_this_bots_own_trades(self):
        ex = FakeExchange(cash_cents=20000)
        t = self.trader(ex)
        self._lost_today(t.store, 45.0)
        self.assertEqual(t.today_pnl(self.clock()), Decimal("-45.00"))
        self.assertTrue(t.place(self.yes)["reason"].startswith("daily_loss_limit"))
        self.assertIsNone(t.store.halted())  # a daily pause, not a halt
        self.assertEqual(ex.posts(), [])

    def test_a_loss_by_the_gold_bot_on_the_same_account_does_not_stop_it(self):
        ex = FakeExchange(cash_cents=20000)
        t = self.trader(ex)
        t.refresh_balance(force=True)
        t.store.day_start(utc_date(self.clock()), t.equity(self.clock()))
        ex.cash_cents = 15500  # the gold bot lost $45 today; this bot lost nothing
        self.assertEqual(t.place(self.yes)["status"], "filled")

    def test_the_gold_bots_open_positions_count_toward_the_floor(self):
        ex = FakeExchange(cash_cents=9000)
        ex.positions_cents = 5000  # $50 tied up in gold trades: the account still holds $140
        t = self.trader(ex)
        self.assertEqual(t.place(self.yes)["status"], "filled")
        self.assertIsNone(t.store.halted())

    def test_fetches_its_own_results_once_per_window(self):
        ex = FakeExchange(cash_cents=20000)
        calls = []
        client = Client("live", FakeSigner(), send=ex, clock=self.clock, sleep=lambda s: None)
        t = Trader(client, Store(self.tmp.name, "live"), Limits(), RULES, log=self.lines.append, clock=self.clock,
                   settle_fetch=lambda tk: calls.append(tk) or {"ticker": tk, "result": "yes"})
        rec = t.place(self.yes)
        self.assertEqual(t.settle_due(self.clock()), 0)            # the window hasn't closed yet
        self.clock.t = rec["close_ts"] + 61
        self.assertEqual(t.settle_due(self.clock()), 1)
        self.assertEqual(t.settle_due(self.clock() + 100), 0)      # cached: not fetched again
        self.assertEqual(calls, [rec["ticker"]])
        self.assertGreater(t.today_pnl(self.clock()), 0)

    def test_a_just_filled_order_is_not_counted_as_a_loss(self):
        ex = FakeExchange(cash_cents=20000)
        t = self.trader(ex)
        t.place(self.yes)
        t.refresh_balance(force=True)
        self.assertLess(t.cash, Decimal("200"))
        self.assertAlmostEqual(float(t.equity(self.clock())), 200.0, delta=0.05)

    def test_order_cost_cap_shrinks_the_order(self):
        ex = FakeExchange()
        rec = self.trader(ex, Limits(max_order_usd=Decimal("3"))).place(self.yes)
        self.assertLess(rec["contracts"], self.yes["intent"]["contracts"])
        self.assertLessEqual(Decimal(rec["max_cost_usd"]), Decimal("3"))
        self.assertEqual(Decimal(ex.posts()[0]["count"]), rec["contracts"])

    def test_limits_as_a_share_of_the_account(self):
        pct = Limits(max_order_usd=None, daily_loss_usd=None, max_order_pct=Decimal("0.25"), daily_loss_pct=Decimal("0.30"))
        self.assertEqual(pct.order_cap(Decimal(400)), Decimal(100))
        self.assertEqual(pct.loss_cap(Decimal(500)), Decimal(150))
        both = dataclasses.replace(pct, max_order_usd=Decimal(60), daily_loss_usd=Decimal(200))
        self.assertEqual(both.order_cap(Decimal(400)), Decimal(60))     # the tighter one applies
        self.assertEqual(both.loss_cap(Decimal(1000)), Decimal(200))
        none = Limits(max_order_usd=None, daily_loss_usd=None)
        self.assertEqual(none.order_cap(Decimal(400)), Decimal("Infinity"))
        self.assertEqual(pct.describe()["daily_loss_pct"], "0.30")
        self.assertIsNone(pct.describe()["daily_loss_usd"])

    def test_daily_loss_stop_as_a_share_of_the_days_starting_equity(self):
        lim = Limits(max_order_usd=None, daily_loss_usd=None, max_order_pct=Decimal("0.25"), daily_loss_pct=Decimal("0.30"))
        ex = FakeExchange(cash_cents=20000)
        t = self.trader(ex, lim)
        t.store.day_start(utc_date(self.clock()), Decimal(200))   # 30% of $200 = $60
        self._lost_today(t.store, 45.0)
        self.assertEqual(t.place(self.yes)["status"], "filled")    # down $45 < $60: still trading
        t2 = self.trader(FakeExchange(cash_cents=20000), lim)
        t2.store.day_path.write_text(json.dumps({"day": utc_date(self.clock()), "start_equity": "140"}))  # 30% = $42
        rec = t2.place(self.no)
        self.assertTrue(rec["reason"].startswith("daily_loss_limit"), rec)
        self.assertIn("the limit is $42.00", rec["reason"])

    def test_order_cap_as_a_share_of_equity_shrinks_the_order(self):
        ex = FakeExchange(cash_cents=2000)   # $20: 25% = $5 per order
        lim = Limits(max_order_usd=None, daily_loss_usd=None, max_order_pct=Decimal("0.25"), daily_loss_pct=Decimal("0.30"),
                     stop_below_usd=Decimal(0))
        rec = self.trader(ex, lim).place(self.yes)
        self.assertLessEqual(Decimal(rec["max_cost_usd"]), Decimal("5"))
        self.assertLess(rec["contracts"], self.yes["intent"]["contracts"])

    def test_run_flags_default_to_limits_that_scale_with_the_account(self):
        from kalshi_live import cli
        args = SimpleNamespace(max_order=None, daily_loss=None, max_order_pct=25.0, daily_loss_pct=30.0, stop_below=100.0,
                               max_orders_per_day=60, cushion=0.01)
        lim = cli.limits_from_args(args)
        self.assertEqual((lim.max_order_pct, lim.daily_loss_pct, lim.max_order_usd, lim.daily_loss_usd),
                         (Decimal("0.25"), Decimal("0.3"), None, None))
        text = (PKG / "kalshi_live" / "cli.py").read_text()
        self.assertIn('"--daily-loss-pct", type=float, default=30.0', text)
        self.assertIn('"--max-order-pct", type=float, default=25.0', text)
        self.assertIn('"--daily-loss", type=float, default=None', text)
        self.assertEqual(cli.describe_cap(None, Decimal("0.3"), "of x"), "30% of x")
        self.assertEqual(cli.describe_cap(Decimal(50), Decimal("0.3"), "of x"), "30% of x or $50 (whichever is less)")

    def test_wrong_side_recorded_by_the_exchange_halts(self):
        ex = FakeExchange(record_side="no")
        rec = self.trader(ex).place(self.yes)
        self.assertFalse(rec["verified"])
        self.assertIn("outcome_side no instead of yes", rec["halt_reason"])
        self.assertIn("order check failed", Store(self.tmp.name, "live").halted())

    def test_rejected_key_halts(self):
        ex = FakeExchange(status_for={("POST", "/portfolio/events/orders"): 401})
        rec = self.trader(ex).place(self.yes)
        self.assertEqual(rec["status"], "rejected")
        self.assertIn("API key", Store(self.tmp.name, "live").halted())

    def test_no_response_is_not_retried(self):
        ex = FakeExchange(status_for={("POST", "/portfolio/events/orders"): "network"})
        t = self.trader(ex)
        self.assertEqual(t.place(self.yes)["status"], "unknown")
        self.assertEqual(t.place(self.yes)["reason"], "already_ordered_this_window")

    def test_unfilled_ioc_is_logged_as_not_filled(self):
        ex = FakeExchange(fill=False)
        rec = self.trader(ex).place(self.yes)
        self.assertEqual(rec["status"], "not_filled")
        self.assertIsNone(Store(self.tmp.name, "live").halted())

    def test_dry_run_sends_nothing(self):
        ex = FakeExchange()
        rec = self.trader(ex, dry_run=True).place(self.yes)
        self.assertEqual(rec["status"], "dry_run")
        self.assertEqual(ex.posts(), [])

    def test_unexpected_error_fails_closed(self):
        ex = FakeExchange()
        t = self.trader(ex)
        d = copy.deepcopy(self.yes)
        d["intent"]["limit_price"] = "not-a-price"
        self.assertEqual(t.place(d)["status"], "error")
        self.assertIn("unexpected error", t.store.halted())

    def test_run_loop_orders_once_on_act(self):
        ex = FakeExchange()
        t = self.trader(ex)
        snap = next(s for sc, s, d in run_scenarios() if sc.name == "trigger_buy_yes")
        snap = copy.deepcopy(snap)
        snap["synthetic"] = False
        snap.pop("label", None)

        class OneSnap:
            def __init__(self):
                self.n = 0

            def collect(self_inner):
                self_inner.n += 1
                s = copy.deepcopy(snap)
                s["snapshot_id"] = f"s{self_inner.n}"
                return s

        rc = run(t, OneSnap(), Path(self.tmp.name) / "decisions.jsonl", 2.0, log=self.lines.append,
                 clock=self.clock, sleep=lambda s: None, max_loops=3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(ex.posts()), 1)  # 3 looks at the same window, one order
        self.assertTrue(any("ORDER FILLED" in line for line in self.lines), self.lines)


def snap_at(seconds_in, **changes):
    """A snapshot of synthetic window 0, `seconds_in` seconds after it opened, marked real.

    Without `changes` it is the buy-YES trigger; e.g. yes_bid/no_bid move Kalshi's prices.
    """
    sc = dataclasses.replace(next(s for s in demo.SCENARIOS if s.name == "trigger_buy_yes"),
                             seconds_in=seconds_in, **changes)
    o, c = demo.window(sc.window)
    now = o + seconds_in
    market = demo.market_json(o, c, sc.strike, sc.series, sc.comparator)
    col = Collector(demo.SyntheticTransport(sc, now, market), clock=lambda: now, synthetic=True,
                    id_factory=lambda ts: f"snap-{seconds_in:g}", seed_history=demo._history(sc, now),
                    seed_basis=demo._basis_log(sc, market, o))
    try:
        snap = col.collect()
    finally:
        col.close()
    snap["synthetic"] = False
    snap.pop("label", None)
    return snap


FAIR = {"yes_bid": ("0.7300", "300.00"), "no_bid": ("0.2600", "250.00")}  # YES at fair value: no trigger


class HoldTests(unittest.TestCase):
    """`run --hold`: a trigger must stay on for a few seconds before it becomes an order."""

    @classmethod
    def setUpClass(cls):
        cls.yes, cls.no = acts()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def look(self, dt, base=None, **changes):
        d = copy.deepcopy(base or self.yes)
        d["evaluated_at"] += dt
        d.update(changes)
        return d

    def skip(self, dt):
        return self.look(dt, decision="SKIP", reason="edge_below_threshold", intent=None)

    def test_orders_only_after_the_trigger_has_held(self):
        h = Hold(4.0, log=self.lines.append)
        first, second, third = (h.check(self.look(dt)) for dt in (0, 2.0, 4.1))
        for d, held in ((first, 0), (second, 2.0)):
            self.assertEqual((d["decision"], d["reason"], d["intent"]), ("SKIP", "trigger_not_held", None))
            self.assertEqual(d["hold"]["held_s"], held)
            self.assertEqual(d["waiting_intent"]["side"], "YES")  # kept for later study, never ordered
        self.assertEqual(third["decision"], "ACT")
        self.assertEqual(third["intent"], self.yes["intent"])
        self.assertEqual(third["hold"], {"side": "YES", "since": self.yes["evaluated_at"], "held_s": 4.1, "need_s": 4.0})
        self.assertEqual(len([l for l in self.lines if "ordering if it holds 4s" in l]), 1)

    def test_looks_a_little_under_two_seconds_apart_still_count(self):
        h = Hold(4.0, log=self.lines.append)
        self.assertEqual([h.check(self.look(dt))["decision"] for dt in (0, 1.8, 3.6)], ["SKIP", "SKIP", "ACT"])

    def test_a_look_without_the_trigger_starts_the_count_again(self):
        h = Hold(4.0, log=self.lines.append)
        seq = [self.look(0), self.look(2), self.skip(4), self.look(6), self.look(8), self.look(10)]
        self.assertEqual([h.check(d)["decision"] for d in seq], ["SKIP"] * 5 + ["ACT"])
        self.assertTrue(any("trigger gone after 2s: no order" in l for l in self.lines), self.lines)

    def test_the_other_side_a_new_window_or_a_long_pause_start_the_count_again(self):
        no = self.look(2, base=self.no, market_ticker=self.yes["market_ticker"])
        for name, second in (("other side", no),
                             ("new window", self.look(2, market_ticker="KXBTC15M-OTHER")),
                             ("long pause", self.look(4.5))):
            with self.subTest(name):
                h = Hold(4.0, log=self.lines.append)
                h.check(self.look(0))
                d = h.check(second)
                self.assertEqual((d["decision"], d["hold"]["held_s"]), ("SKIP", 0))
                later = h.check(self.look(2, base=second))  # 2 s after `second`: counted from there
                self.assertEqual((later["decision"], later["hold"]["held_s"]), ("SKIP", 2.0))

    def test_zero_turns_it_off(self):
        d = self.look(0)
        self.assertIs(Hold(0, log=self.lines.append).check(d), d)
        self.assertEqual(self.lines, [])

    def run_feed(self, snaps, hold_s=4.0):
        ex = FakeExchange()
        clock = Clock(snaps[0]["collected_at"])
        client = Client("live", FakeSigner(), send=ex, clock=clock, sleep=lambda s: None)
        t = Trader(client, Store(self.tmp.name, "live"), Limits(), RULES, log=self.lines.append, clock=clock)
        feed = list(snaps)

        class Feed:
            def collect(self_inner):
                s = feed.pop(0)
                clock.t = s["collected_at"]
                return s

        path = Path(self.tmp.name) / "decisions.jsonl"
        rc = run(t, Feed(), path, 2.0, log=self.lines.append, clock=clock, sleep=lambda s: None,
                 max_loops=len(snaps), hold_s=hold_s)
        self.assertEqual(rc, 0)
        logged = [json.loads(l) for l in path.read_text().splitlines()]
        return ex, t, logged

    def test_run_loop_sends_nothing_while_the_trigger_is_two_seconds_old(self):
        ex, t, logged = self.run_feed([snap_at(480), snap_at(482)])
        self.assertEqual(ex.posts(), [])
        self.assertEqual([d["reason"] for d in logged], ["trigger_not_held"] * 2)

    def test_run_loop_orders_on_the_look_that_completes_the_hold(self):
        ex, t, logged = self.run_feed([snap_at(s) for s in (480, 482, 484, 486, 488)])
        self.assertEqual(len(ex.posts()), 1)
        self.assertEqual([d["reason"] for d in logged if d["decision"] == "ACT" or d.get("hold")],
                         ["trigger_not_held", "trigger_not_held", "all_conditions_met"])
        act = next(d for d in logged if d["decision"] == "ACT")
        order = t.store.orders()[0]
        self.assertEqual((order["status"], order["held_s"], order["hold_s"]), ("filled", 4.0, 4.0))
        self.assertEqual(order["snapshot_id"], "snap-484")  # priced from the look that completed the hold
        self.assertEqual(order["shown_ask"], act["intent"]["limit_price"])
        self.assertTrue(any("ACT: YES" in l and "held 4s" in l for l in self.lines), self.lines)

    def test_run_loop_skips_a_trigger_that_is_gone_on_the_next_look(self):
        ex, t, logged = self.run_feed([snap_at(480), snap_at(482, **FAIR), snap_at(484), snap_at(486)])
        self.assertEqual(ex.posts(), [])
        self.assertEqual(t.store.orders(), [])
        self.assertTrue(any("trigger gone after 0s: no order" in l for l in self.lines), self.lines)

    def test_run_loop_without_a_hold_orders_on_the_first_look(self):
        ex, t, logged = self.run_feed([snap_at(480), snap_at(482)], hold_s=0.0)
        self.assertEqual(len(ex.posts()), 1)
        self.assertEqual(t.store.orders()[0]["snapshot_id"], "snap-480")

    def test_the_service_holds_four_seconds_unless_told_otherwise(self):
        text = (PKG / "kalshi_live" / "cli.py").read_text()
        self.assertIn('"--hold", type=float, default=4.0', text)
        self.assertIn("hold_s=args.hold", text)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.yes, self.no = acts()
        self.clock = Clock(self.yes["evaluated_at"])

    def tearDown(self):
        self.tmp.cleanup()

    def trader(self, ex):
        client = Client("live", FakeSigner(), send=ex, clock=self.clock, sleep=lambda s: None)
        return Trader(client, Store(self.tmp.name, "live"), Limits(), RULES, log=lambda s: None, clock=self.clock)

    def test_results_pnl_and_health(self):
        from kalshi_live import dashboard
        from kalshi_paper.http import Response
        t = self.trader(FakeExchange())
        y, n = t.place(self.yes), t.place(self.no)
        t.refresh_balance(force=True)
        t.write_state(self.yes, [[self.clock(), 0.7, 0.66, 0.67]])
        results = {y["ticker"]: "yes", n["ticker"]: "yes"}  # YES wins both windows: the YES buy won, the NO buy lost

        def transport(url, timeout):
            tk = url.rsplit("/", 1)[1]
            body = json.dumps({"market": {"ticker": tk, "status": "finalized", "result": results[tk]}}).encode()
            return Response(url, 200, {}, body, 0.0, 0.0)

        later = max(y["close_ts"], n["close_ts"]) + 120
        self.assertEqual(dashboard.settle_due(t.store, transport, now=later), 2)
        self.assertEqual(dashboard.settle_due(t.store, transport, now=later), 0)  # cached
        s = dashboard.state(t.store, now=self.clock() + 1)
        st = s["stats"]
        self.assertEqual((st["filled"], st["settled"], st["won"]), (2, 2, 1))
        byside = {o["side"]: o for o in s["orders"]}
        self.assertEqual(byside["YES"]["result"], "won")
        self.assertEqual(byside["NO"]["result"], "lost")
        exp_yes = byside["YES"]["filled"] - byside["YES"]["cost"]
        self.assertAlmostEqual(byside["YES"]["pnl"], exp_yes, places=6)
        self.assertAlmostEqual(byside["NO"]["pnl"], -byside["NO"]["cost"], places=6)
        self.assertAlmostEqual(st["pnl"], round(exp_yes - byside["NO"]["cost"], 2), places=2)
        self.assertEqual(len(s["curve"]), 2)
        self.assertEqual(s["health"]["state"], "running")
        self.assertEqual(s["live"]["window"][0][1], 0.7)
        self.assertEqual(dashboard.state(t.store, now=self.clock() + 60)["health"]["state"], "stale")
        t.store.halt("test")
        t.write_state(self.yes, [])
        self.assertEqual(dashboard.state(t.store, now=self.clock())["health"]["state"], "halted")

    def test_run_loop_writes_the_dashboard_file(self):
        t = self.trader(FakeExchange())
        snap = copy.deepcopy(next(s for sc, s, d in run_scenarios() if sc.name == "trigger_buy_yes"))
        snap["synthetic"] = False
        snap.pop("label", None)

        class Snaps:
            n = 0

            def collect(self_inner):
                self_inner.n += 1
                s = copy.deepcopy(snap)
                s["snapshot_id"] = f"s{self_inner.n}"
                return s

        run(t, Snaps(), Path(self.tmp.name) / "d.jsonl", 2.0, log=lambda s: None, clock=self.clock,
            sleep=lambda s: None, max_loops=2)
        live = t.store.read_live()
        self.assertEqual(live["decision"]["ticker"], self.yes["market_ticker"])
        self.assertEqual(len(live["window"]), 2)
        self.assertFalse(live["dry_run"])
        self.assertIsNotNone(live["cash"])

    @staticmethod
    def fake_trades():
        """Four settled trades over two Eastern-time days (2026-11-20 and 21), 10 contracts each."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        t0 = datetime(2026, 11, 20, 10, 0, tzinfo=et).timestamp()
        mk = lambda dt, side, paid, res, pnl: {"at": t0 + dt, "close_ts": t0 + dt + 600, "side": side, "paid": paid,
                                                "filled": 10.0, "result": res, "pnl": pnl, "fees": 0.1}
        return [mk(0, "YES", 0.80, "won", 1.9), mk(3600, "NO", 0.30, "lost", -3.1),
                mk(86400, "YES", 0.25, "won", 7.4), mk(86400 + 3600, "NO", 0.70, "lost", -7.1)]

    def test_summary_days_records_and_breakdown(self):
        from kalshi_live import dashboard
        tr = self.fake_trades()
        s = dashboard.summarize(tr, now=tr[-1]["close_ts"] + 60)
        self.assertEqual([d["day"] for d in s["days"]], ["2026-11-21", "2026-11-20"])  # newest first
        self.assertEqual([d["pnl"] for d in s["days"]], [0.3, -1.2])
        self.assertEqual(s["days"][0]["total"], -0.9)  # running total
        r = s["records"]
        self.assertEqual((r["today"], r["today_trades"], r["week"]), (0.3, 2, -0.9))
        self.assertEqual(r["best_day"]["day"], "2026-11-21")
        self.assertEqual((r["up_days"], r["down_days"], r["streak"]), (1, 1, -1))
        self.assertEqual(r["max_drawdown"], 7.1)  # +1.9, -1.2 (peak 1.9), +6.2 (peak 6.2), -0.9
        self.assertAlmostEqual(r["per_contract"], -0.9 / 40)
        side = {g["label"]: g for g in s["breakdown"]["side"]}
        self.assertEqual((side["bought YES"]["trades"], side["bought YES"]["pnl"]), (2, 9.3))
        self.assertEqual(sum(g["trades"] for g in s["breakdown"]["price"]), 4)
        self.assertEqual(sum(g["trades"] for g in s["breakdown"]["minute"]), 4)

    def test_what_if_replays_real_trades_with_fixed_size_and_limits(self):
        from kalshi_live import dashboard
        from kalshi_paper import sizing as sz
        tr = self.fake_trades()
        big = Limits(max_order_usd=Decimal(10**6), daily_loss_usd=Decimal(10**6), stop_below_usd=Decimal(0))
        r = dashboard.what_if(tr, 100.0, fixed=10, limits=big)
        exp = sum((10 if t["result"] == "won" else 0) - 10 * (t["paid"] + sz.fee_per_contract(t["paid"], 10, RULES)) for t in tr)
        self.assertAlmostEqual(r["profit"], round(exp, 2), places=2)
        self.assertEqual((r["trades"], r["won"], r["contracts_min"], r["contracts_max"]), (4, 2, 10, 10))
        # since: only the second day
        since = tr[2]["at"] - 1
        self.assertEqual(dashboard.what_if(tr, 100.0, since_ts=since, fixed=10, limits=big)["trades"], 2)
        # the most one order may cost shrinks the size: $5 at 80c+fee is 6 contracts
        capped = dashboard.what_if(tr[:1], 100.0, fixed=10, limits=Limits(max_order_usd=Decimal(5), stop_below_usd=Decimal(0)))
        self.assertEqual(capped["contracts_max"], 6)
        # the daily loss stop (UTC day) skips the rest of that day once it is down that much
        stop = dashboard.what_if(tr[1:2] * 3, 100.0, fixed=10, limits=Limits(daily_loss_usd=Decimal(3), stop_below_usd=Decimal(0)))
        self.assertEqual((stop["trades"], stop["skipped"]["limits"]), (1, 2))
        # below the floor: halted, nothing more
        halt = dashboard.what_if(tr[1:2] * 3, 5.0, fixed=10, limits=Limits(stop_below_usd=Decimal(4)))
        self.assertIsNotNone(halt["halted_at"])
        self.assertEqual(halt["trades"], 1)
        # balance-based limits: 25% of $100 = $25 per order (31 contracts at 80c), not the $1M cap
        share = Limits(max_order_usd=None, daily_loss_usd=None, max_order_pct=Decimal("0.25"), stop_below_usd=Decimal(0))
        self.assertEqual(dashboard.what_if(tr[:1], 100.0, fixed=1000, limits=share)["contracts_max"], 30)
        # the bot's rule grows with cash: $1,000 -> 50 contracts per trade (v0.4.2: 5 per $100)
        if RULES.contracts_per_100usd:
            grow = dashboard.what_if(tr[:1], 1000.0, limits=big)
            self.assertEqual(grow["contracts_max"], int(1000 / 100 * RULES.contracts_per_100usd))

    def test_what_if_query_checks_its_inputs(self):
        from kalshi_live import dashboard
        store = Store(self.tmp.name, "live")
        self.assertIn("error", dashboard.whatif_query(store, "start=abc"))
        self.assertIn("error", dashboard.whatif_query(store, "start=0"))
        self.assertIn("error", dashboard.whatif_query(store, "start=500&since=11/19"))
        self.assertIn("error", dashboard.whatif_query(store, "start=500&fixed=0"))
        ok = dashboard.whatif_query(store, "start=500&since=2026-11-19&fixed=10&limits=off")
        self.assertEqual((ok["trades"], ok["end"], ok["limits_applied"]), (0, 500.0, False))
        old = dashboard.whatif_query(store, "start=500&limits=old")
        self.assertEqual((old["limits"]["daily_loss_usd"], old["limits_mode"]), ("150", "old"))
        self.assertIn("7.5 contracts per $100", old["sizing"])
        self.assertIn("error", dashboard.whatif_query(store, "start=500&per100=0"))
        text = (PKG / "kalshi_live" / "dashboard.py").read_text()
        self.assertIn('self.path.startswith("/api/whatif")', text)

    def test_page_and_api_serve_on_localhost_only(self):
        text = (PKG / "kalshi_live" / "dashboard.py").read_text()
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", port)', text)
        self.assertNotIn("0.0.0.0", text)


class SmokeAndGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_smoke(self, ex):
        now = 2_000_000_000.0
        ex.markets = [{"ticker": "KXBTC15M-33MAY180345-45", "close_time": "2033-05-18T03:45:00Z"}]
        now = parse_iso_utc(ex.markets[0]["close_time"]) - 600
        client = Client("live", FakeSigner(), send=ex, clock=lambda: now, sleep=lambda s: None)
        store = Store(self.tmp.name, "live")
        return smoke(client, store, log=lambda s: None, clock=lambda: now), store, ex

    def test_smoke_passes_and_cancels_both_orders(self):
        ok, store, ex = self.run_smoke(FakeExchange())
        self.assertTrue(ok)
        self.assertTrue(store.smoke_passed("test-key"))
        self.assertFalse(store.smoke_passed("other-key"))
        self.assertEqual([b["side"] for b in ex.posts()], ["bid", "ask"])
        self.assertEqual([b["price"] for b in ex.posts()], ["0.0100", "0.9900"])
        self.assertTrue(all(o["status"] == "canceled" for o in ex.orders.values()))

    def test_smoke_finds_orders_in_the_list_when_lookup_by_id_404s(self):
        # What the live exchange did on the first real smoke run (GET /portfolio/orders/{id} -> 404).
        ok, store, ex = self.run_smoke(FakeExchange(status_for={("GET", "/portfolio/orders/{id}"): 404}))
        self.assertTrue(ok)
        self.assertTrue(all(o["status"] == "canceled" for o in ex.orders.values()))

    def test_smoke_cancels_its_orders_even_when_it_cannot_read_them_back(self):
        ok, store, ex = self.run_smoke(FakeExchange(status_for={("GET", "/portfolio/orders/{id}"): 404},
                                                    list_orders=False))
        self.assertFalse(ok)
        self.assertEqual(len(ex.orders), 2)
        self.assertTrue(all(o["status"] == "canceled" for o in ex.orders.values()))

    def test_smoke_fails_when_the_markets_shard_is_unfunded(self):
        ex = FakeExchange(cash_cents=20008)
        ex.shards = {0: "200.0000", 2: "0.0800"}
        now = parse_iso_utc("2033-05-18T03:45:00Z") - 600
        ex.markets = [{"ticker": "KXBTC15M-33MAY180345-45", "close_time": "2033-05-18T03:45:00Z", "exchange_index": 2}]
        client = Client("live", FakeSigner(), send=ex, clock=lambda: now, sleep=lambda s: None)
        lines = []
        ok = smoke(client, Store(self.tmp.name, "live"), log=lines.append, clock=lambda: now)
        self.assertFalse(ok)
        self.assertTrue(any("FAIL  cash on exchange shard 2" in l for l in lines), lines)

    def test_smoke_fails_if_the_exchange_reads_the_side_differently(self):
        ok, store, _ = self.run_smoke(FakeExchange(record_side="yes"))
        self.assertFalse(ok)
        self.assertFalse(store.smoke_passed("test-key"))

    def test_real_money_needs_the_flag_and_a_passed_smoke(self):
        store = Store(self.tmp.name, "live")
        args = SimpleNamespace(env="live", dry_run=False)
        self.assertIn("KALSHI_REAL_MONEY", live_gate(args, store, "k", environ={}))
        self.assertIn("smoke", live_gate(args, store, "k", environ={"KALSHI_REAL_MONEY": "yes"}))
        store.smoke_path.write_text(json.dumps({"passed": True, "key_id": "k"}))
        self.assertIsNone(live_gate(args, store, "k", environ={"KALSHI_REAL_MONEY": "yes"}))
        self.assertIsNone(live_gate(SimpleNamespace(env="live", dry_run=True), store, "x", environ={}))
        self.assertIsNone(live_gate(SimpleNamespace(env="demo", dry_run=False), store, "x", environ={}))


class FundShardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ex = FakeExchange(cash_cents=20008)
        self.ex.shards = {0: "200.0600", 1: "0.0100", 2: "0.0100"}  # the live account's split
        self.ex.markets = [{"ticker": "KXBTC15M-33MAY180345-45", "exchange_index": 2}]
        self.client = Client("live", FakeSigner(), send=self.ex, clock=lambda: 1000.0, sleep=lambda s: None)
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def fund(self, amount, answer="yes", **kw):
        from kalshi_live.funding import fund_shard
        return fund_shard(self.client, Store(self.tmp.name, "live"), amount, confirm=lambda p: answer,
                          log=self.lines.append, sleep=lambda s: None, clock=lambda: 1000.0, **kw)

    def test_moves_to_the_markets_shard_inside_the_account(self):
        self.assertTrue(self.fund("200"))
        b = self.ex.posts()[0]
        self.assertEqual((b["source"], b["destination"]), ("event_contract", "event_contract"))
        self.assertEqual((b["source_exchange_shard"], b["destination_exchange_shard"], b["amount"]), (0, 2, 2_000_000))
        self.assertEqual(Decimal(self.ex.shards[2]), Decimal("200.01"))
        self.assertTrue(any(l.startswith("done.") for l in self.lines), self.lines)
        log = json.loads((Path(self.tmp.name) / "transfers-live.jsonl").read_text())
        self.assertEqual((log["status"], log["amount_usd"], log["to_shard"]), ("accepted", "200", 2))

    def test_asks_first_and_does_nothing_without_yes(self):
        self.assertFalse(self.fund("200", answer="no"))
        self.assertEqual(self.ex.posts(), [])

    def test_never_more_than_the_source_shard_holds(self):
        self.assertFalse(self.fund("500"))
        self.assertEqual(self.ex.posts(), [])
        self.assertTrue(self.fund("all"))
        self.assertEqual(self.ex.posts()[0]["amount"], 2_000_600)

    def test_refuses_bad_amounts_and_shards(self):
        for args in ((Decimal("0"), 0, 2), (Decimal("-5"), 0, 2), (Decimal("1.005"), 0, 2), (Decimal("5"), 2, 2),
                     (Decimal("5"), 0, 101), (Decimal("5"), -1, 2)):
            with self.subTest(args), self.assertRaises(ValueError):
                self.client.move_between_shards(*args, "id")
        self.assertEqual(self.ex.posts(), [])


class SeparationTests(unittest.TestCase):
    def test_paper_package_never_imports_the_trading_package(self):
        for f in sorted((PKG / "kalshi_paper").glob("*.py")):
            with self.subTest(f.name):
                self.assertNotIn("kalshi_live", f.read_text())


try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ModuleNotFoundError:
    HAVE_CRYPTO = False


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed here (deploy/setup.sh installs it on the server)")
class SigningTests(unittest.TestCase):
    def test_rsa_and_ed25519_signatures_verify(self):
        import base64
        import os
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        from kalshi_live.signing import Signer, message
        for key in (rsa.generate_private_key(public_exponent=65537, key_size=2048), ed25519.Ed25519PrivateKey.generate()):
            with tempfile.TemporaryDirectory() as d, self.subTest(type(key).__name__):
                p = Path(d) / "k.key"
                p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
                os.chmod(p, 0o644)
                with self.assertRaises(PermissionError):
                    Signer("id", str(p))
                os.chmod(p, 0o600)
                h = Signer("id", str(p)).headers("POST", "/trade-api/v2/portfolio/events/orders?x=1", 1700000000000)
                sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
                msg = message(1700000000000, "POST", "/trade-api/v2/portfolio/events/orders")
                self.assertEqual(msg, b"1700000000000POST/trade-api/v2/portfolio/events/orders")
                if isinstance(key, rsa.RSAPrivateKey):
                    key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
                else:
                    key.public_key().verify(sig, msg)


if __name__ == "__main__":
    unittest.main()
