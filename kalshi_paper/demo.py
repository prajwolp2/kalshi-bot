"""SYNTHETIC scenarios that drive the real collector, evaluator and scorer.

Every response here is invented. Nothing touches the network: a fake transport
answers each URL. Every snapshot, decision and settlement the demo writes carries
"synthetic": true and a SYNTHETIC label, and every file name starts with SYNTHETIC_.
Synthetic windows are dated January 2030 so they can't be mistaken for real ones.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path

from . import SYNTHETIC_LABEL
from .collector import Collector
from .evaluator import EvalState, evaluate
from .http import FetchError, Response
from .rules import RULES
from .scorer import fetch_settlement, format_report, score, tickers_to_settle
from .storage import write_jsonl
from .validate import ET, event_code

BASE_OPEN = datetime(2030, 1, 15, 15, 0, tzinfo=timezone.utc).timestamp()  # 10:00 AM EST
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]


def window(i: int) -> tuple[float, float]:
    o = BASE_OPEN + 900 * i
    return o, o + 900


def _iso(ts: float, micros: bool = False) -> str:
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if micros else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def _et_text(ts: float, long_month: bool) -> str:
    dt = datetime.fromtimestamp(ts, tz=ET)
    mon = (MONTH_FULL if long_month else MONTH_ABBR)[dt.month - 1]
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'} {dt.tzname()} on {mon} {dt.day}, {dt.year}"


def rules_text(open_ts: float, close_ts: float, comparator: str = "is at least") -> str:
    """Contract wording in the format Kalshi used on 2026-11-04."""
    return ("If the simple average of the sixty seconds of CF Benchmarks' BRTI before "
            f"{_et_text(close_ts, False)} {comparator} the simple average of the sixty seconds of "
            f"CF Benchmarks' BRTI before {_et_text(open_ts, True)}, then the market resolves to Yes.")


def market_json(open_ts, close_ts, strike, series="KXBTC15M", comparator="is at least"):
    ev = event_code(close_ts, series)
    return {
        "ticker": f"{ev}-{datetime.fromtimestamp(close_ts, tz=ET):%M}",
        "event_ticker": ev,
        "title": "BTC price up in next 15 mins?",
        "market_type": "binary",
        "strike_type": "greater_or_equal",
        "status": "active",
        "open_time": _iso(open_ts),
        "close_time": _iso(close_ts),
        "floor_strike": strike,
        "yes_sub_title": f"Target Price: ${strike:,.2f}" if strike else "Target Price: TBD",
        "rules_primary": rules_text(open_ts, close_ts, comparator),
    }


@dataclass
class Scenario:
    name: str
    simulates: str
    expect: tuple
    window: int
    seconds_in: float = 480.0              # 8 minutes into the window, 7 minutes left
    spot: float = 76800.0                  # BTC now (composite)
    trend_per_min: float = 50.0            # recent spot history slope, $/minute
    strike: float | None = 76700.0         # target price
    yes_bid: tuple = ("0.6500", "300.00")  # best YES bid, size  -> NO ask = 1 - 0.65
    no_bid: tuple = ("0.3400", "250.00")   # best NO bid, size   -> YES ask = 1 - 0.34
    series: str = "KXBTC15M"
    comparator: str = "is at least"
    coinbase_age_s: float = 0.4
    no_bids_empty: bool = False
    failure: dict = field(default_factory=dict)
    replay_of: str | None = None
    basis: float | None = 0.0              # strike minus our price at the last two opens; None = not measured yet
    hour_move: float = 0.0                 # extra BTC move over the hour of candles, e.g. 0.005 = +0.5%


SCENARIOS = [
    Scenario("trigger_buy_yes", "BTC trending up, YES ask well below fair value",
             ("ACT", "all_conditions_met"), window=0),
    Scenario("repeat_same_snapshot", "the same snapshot delivered a second time",
             ("SKIP", "duplicate_snapshot"), window=0, replay_of="trigger_buy_yes"),
    Scenario("repeat_trigger_same_window", "still triggering 5 s later, same window",
             ("SKIP", "already_acted_this_window"), window=0, seconds_in=485.0),
    Scenario("trigger_buy_no", "BTC trending down, NO ask well below fair value",
             ("ACT", "all_conditions_met"), window=1, spot=76600.0, trend_per_min=-50.0,
             yes_bid=("0.3400", "300.00"), no_bid=("0.6500", "250.00")),
    Scenario("non_trigger_no_trend", "same cheap YES ask, but BTC is flat",
             ("SKIP", "no_trend"), window=2, trend_per_min=0.0),
    Scenario("non_trigger_fair_price", "BTC trending up, YES ask at fair value",
             ("SKIP", "edge_below_threshold"), window=3,
             yes_bid=("0.7300", "300.00"), no_bid=("0.2600", "250.00")),
    Scenario("stale_spot", "Coinbase quote is 12 s old",
             ("SKIP", "stale_data:coinbase"), window=4, coinbase_age_s=12.0),
    Scenario("missing_target_price", "Kalshi hasn't published the target price",
             ("SKIP", "missing_data:target_price"), window=5, strike=None),
    Scenario("missing_book_side", "no NO bids, so there is no YES ask",
             ("SKIP", "missing_data:orderbook_side_empty"), window=6, no_bids_empty=True),
    Scenario("wrong_market_series", "the open market returned is ETH, not BTC",
             ("SKIP", "wrong_market:ticker"), window=7, series="KXETH15M"),
    Scenario("wrong_market_contract", "contract wording changed to 'is below'",
             ("SKIP", "wrong_market:rules_text"), window=8, comparator="is below"),
    Scenario("api_failure_kalshi_503", "Kalshi market list returns HTTP 503",
             ("SKIP", "api_error:kalshi_market_list"), window=9, failure={"kalshi_market_list": "http_503"}),
    Scenario("api_failure_timeout", "Kraken request times out",
             ("SKIP", "api_error:kraken_ticker"), window=10, failure={"kraken": "timeout"}),
    Scenario("api_failure_garbage", "Bitstamp returns an HTML error page",
             ("SKIP", "api_error:bitstamp_ticker"), window=11, failure={"bitstamp": "malformed"}),
    Scenario("near_settlement", "only 60 s left in the window",
             ("SKIP", "too_close_to_settlement"), window=12, seconds_in=840.0),
    Scenario("no_basis_yet", "just started: no window open measured yet",
             ("SKIP", "insufficient_basis_history"), window=13, basis=None),
    Scenario("basis_removes_false_edge", "cheap-looking NO; our price $35 under the index",
             ("SKIP", "edge_below_threshold"), window=14, spot=76600.0, trend_per_min=-50.0,
             yes_bid=("0.3400", "300.00"), no_bid=("0.6500", "250.00"), basis=35.0),
    Scenario("basis_out_of_range", "our price is $60 off the settlement index",
             ("SKIP", "basis_out_of_range"), window=15, basis=60.0),
    Scenario("move_already_extended", "trending up with a cheap YES, but BTC is already up 0.5% this hour",
             ("SKIP", "move_already_extended"), window=16, hour_move=0.005),
]

# Synthetic outcomes for the windows that produced valid evaluations.
SETTLEMENT_RESULTS = {0: "yes", 1: "yes", 2: "no", 3: "yes"}


def _history(sc: Scenario, now: float) -> list:
    """Spot history every 10 s for the last 7 minutes: a straight trend plus +/-$3 wiggle."""
    rows = []
    for k in range(42, 0, -1):
        p = sc.spot - sc.trend_per_min * (10 * k) / 60 + (3.0 if k % 2 else -3.0)
        rows.append([now - 10 * k, p, p + 1.0, p - 1.0])
    return rows


def _basis_log(sc: Scenario, market: dict, open_ts: float) -> list:
    """Two earlier measurements (this window's open and the one before) with the scenario's gap."""
    if sc.basis is None:
        return []
    k = sc.strike or 0.0
    return [[open_ts - 900, "SYNTHETIC-previous-window", k, k - sc.basis, 6],
            [open_ts, market["ticker"], k, k - sc.basis, 6]]


def _candles(sc: Scenario, now: float) -> list:
    """75 one-minute candles alternating +/-0.08% (sigma_1m ~ 0.08%), lagging 2 minutes like Coinbase."""
    last_start = math.floor(now / 60) * 60 - 120
    closes = [sc.spot - 100.0]
    for j in range(1, 75):
        closes.append(closes[-1] * math.exp((0.0008 if j % 2 else -0.0008) + sc.hour_move / 60))
    rows = [[last_start - 60 * (74 - j), c * 0.9995, c * 1.0005, c, c, 1.0] for j, c in enumerate(closes)]
    return rows[::-1]  # newest first, like Coinbase


class SyntheticTransport:
    def __init__(self, sc: Scenario, now: float, market: dict):
        self.sc, self.now, self.market = sc, now, market
        self.candles = _candles(sc, now)

    def _resp(self, url, body, status=200):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"date": formatdate(int(self.now), usegmt=True), "x-synthetic": "true"}
        return Response(url, status, headers, raw, self.now, self.now)

    def __call__(self, url: str, timeout: float) -> Response:
        sc, f = self.sc, self.sc.failure
        if "/markets?series_ticker=" in url:
            if f.get("kalshi_market_list") == "http_503":
                return self._resp(url, {"error": "service unavailable (SYNTHETIC)"}, 503)
            return self._resp(url, {"markets": [self.market], "cursor": ""})
        if "/orderbook" in url:
            def levels(best):
                lower = f"{float(best[0]) - 0.01:.4f}"
                return [[lower, "500.00"], [best[0], best[1]]]  # ascending, best last (like Kalshi)
            return self._resp(url, {"orderbook_fp": {
                "yes_dollars": levels(sc.yes_bid),
                "no_dollars": None if sc.no_bids_empty else levels(sc.no_bid)}})
        if "coinbase.com/products/BTC-USD/ticker" in url:
            m = sc.spot
            return self._resp(url, {"bid": f"{m - 0.01:.2f}", "ask": f"{m + 0.01:.2f}", "price": f"{m:.2f}",
                                    "time": _iso(self.now - sc.coinbase_age_s, micros=True)})
        if "coinbase.com/products/BTC-USD/candles" in url:
            return self._resp(url, self.candles)
        if "api.kraken.com" in url:
            if f.get("kraken") == "timeout":
                raise FetchError("TimeoutError: timed out (SYNTHETIC)")
            m = sc.spot + 1.0
            return self._resp(url, {"error": [], "result": {"XXBTZUSD": {
                "a": [f"{m + 0.5:.1f}", "1", "1.000"], "b": [f"{m - 0.5:.1f}", "1", "1.000"]}}})
        if "bitstamp.net" in url:
            if f.get("bitstamp") == "malformed":
                return self._resp(url, b"<html><body>502 Bad Gateway (SYNTHETIC)</body></html>")
            m = sc.spot - 1.0
            return self._resp(url, {"timestamp": str(int(self.now)), "bid": f"{m - 0.5:.2f}", "ask": f"{m + 0.5:.2f}"})
        raise FetchError(f"no synthetic route for {url}")


class SettlementTransport:
    """Answers GET /markets/{ticker} with SYNTHETIC results."""

    def __init__(self, now: float):
        self.now = now
        self.results = {market_json(*window(i), 76700.0)["ticker"]: r for i, r in SETTLEMENT_RESULTS.items()}

    def __call__(self, url, timeout):
        m = re.search(r"/markets/([^/?]+)$", url)
        if not m:
            raise FetchError(f"no synthetic route for {url}")
        ticker = m.group(1)
        result = self.results.get(ticker)
        body = {"market": {"ticker": ticker, "status": "finalized" if result else "active",
                           "result": result or "", "expiration_value": "SYNTHETIC",
                           "settlement_ts": _iso(self.now)}}
        headers = {"date": formatdate(int(self.now), usegmt=True), "x-synthetic": "true"}
        return Response(url, 200, headers, json.dumps(body).encode(), self.now, self.now)


def run_scenarios(rules=RULES) -> list:
    """Feed every scenario through Collector -> evaluate in order, sharing one EvalState."""
    state, out, snaps = EvalState(), [], {}
    for sc in SCENARIOS:
        o, c = window(sc.window)
        now = o + sc.seconds_in
        if sc.replay_of:
            snap = snaps[sc.replay_of]
        else:
            market = market_json(o, c, sc.strike, sc.series, sc.comparator)
            col = Collector(SyntheticTransport(sc, now, market), clock=lambda now=now: now, synthetic=True,
                            id_factory=lambda ts, n=sc.name: f"SYNTHETIC-{n}", seed_history=_history(sc, now),
                            seed_basis=_basis_log(sc, market, o))
            try:
                snap = col.collect()
            finally:
                col.close()
        dec = evaluate(snap, rules, state)
        state.record(dec)
        snaps[sc.name] = snap
        out.append((sc, snap, dec))
    return out


def run_scoring(results, rules=RULES):
    decisions = [d for _, _, d in results]
    now = window(max(sc.window for sc in SCENARIOS))[1] + 120
    transport = SettlementTransport(now)
    settlements = {}
    for t in tickers_to_settle(decisions, {}, now):
        s = fetch_settlement(t, transport, clock=lambda: now)
        if s:
            s.update(synthetic=True, label=SYNTHETIC_LABEL)
            settlements[t] = s
    return settlements, score(decisions, settlements, rules, now=now)


def main(out_dir: Path) -> int:
    results = run_scenarios()
    settlements, report = run_scoring(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "snapshots": out_dir / "SYNTHETIC_snapshots.jsonl",
        "decisions": out_dir / f"SYNTHETIC_decisions-{RULES.version}.jsonl",
        "settlements": out_dir / "SYNTHETIC_settlements.jsonl",
        "score": out_dir / f"SYNTHETIC_score-{RULES.version}.json",
    }
    write_jsonl(files["snapshots"], [s for _, s, _ in results])
    write_jsonl(files["decisions"], [d for _, _, d in results])
    write_jsonl(files["settlements"], settlements.values())
    files["score"].write_text(json.dumps(report, indent=2))

    print("SYNTHETIC DATA - every quote, price and API response below is invented to exercise the code.\n")
    print(f"{'#':>2}  {'scenario':<28} {'simulates':<47} {'result':<6} reason")
    bad = 0
    for n, (sc, _, d) in enumerate(results, 1):
        ok = (d["decision"], d["reason"]) == sc.expect
        bad += not ok
        extra = ""
        if d["decision"] == "ACT":
            i = d["intent"]
            extra = f"  -> PAPER BUY {i['side']} x{i['contracts']} @ {i['limit_price']}"
        flag = "" if ok else f"   <-- EXPECTED {sc.expect}"
        print(f"{n:>2}  {sc.name:<28} {sc.simulates:<47} {d['decision']:<6} {d['reason']}{extra}{flag}")
    print(f"\n{len(results) - bad}/{len(results)} scenarios gave the expected decision.\n")
    print(format_report(report))
    print("\nSaved (all SYNTHETIC):")
    for p in files.values():
        print(f"  {p}")
    return 1 if bad else 0
