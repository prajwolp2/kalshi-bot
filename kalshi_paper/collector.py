"""Read-only collector: one snapshot = everything needed to reproduce one decision.

Per tick it GETs, in parallel:
  - Kalshi open markets for KXBTC15M   (identity, target price, times, contract text)
  - Coinbase, Kraken, Bitstamp tickers  (BTC spot, a stand-in for CF Benchmarks' BRTI)
  - Coinbase 1-minute candles           (volatility; refreshed at most once a minute)
then the Kalshi order book for the open market (quotes; fetched last, so freshest).

Every source records its URL, HTTP status, send/receive time, HTTP Date header,
CDN cache age and any error. The snapshot also embeds the recent spot history the
momentum rule needs, and the gap between each recent window's strike and our own
price over the minute before it opened, so a decision can be replayed from its
snapshot alone.

The collector applies no trading rules. All judgement happens in evaluator.py.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from statistics import median
from urllib.parse import quote

from . import SYNTHETIC_LABEL, __version__
from .http import FetchError, get
from .timeutil import http_date_to_epoch, iso_utc, parse_iso_utc

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"

URL_MARKET_LIST = f"{KALSHI_BASE}/markets?series_ticker={SERIES}&status=open&limit=10"
URL_ORDERBOOK = KALSHI_BASE + "/markets/{ticker}/orderbook?depth=5"
URL_COINBASE_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
URL_KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
URL_BITSTAMP_TICKER = "https://www.bitstamp.net/api/v2/ticker/btcusd/"
URL_COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"

VENUES = ("coinbase", "kraken", "bitstamp")
CANDLES_KEEP = 75           # completed 1-minute candles stored per snapshot
CANDLE_REFRESH_S = 60.0     # candles change once a minute; don't poll them faster
HISTORY_KEEP_S = 420.0      # spot history embedded in each snapshot (7 minutes)
HISTORY_SPACING_S = 10.0    # at most one history point per 10 seconds
BASIS_KEEP = 12             # strike-vs-our-price measurements embedded in each snapshot (3 hours)

MARKET_FIELDS = (
    "ticker", "event_ticker", "title", "market_type", "strike_type", "status",
    "open_time", "close_time", "floor_strike", "yes_sub_title", "rules_primary",
)


class ParseError(Exception):
    pass


# ---------------------------------------------------------------- parsers

def _json(resp):
    try:
        return json.loads(resp.body)
    except (ValueError, UnicodeDecodeError) as e:
        raise ParseError(f"invalid JSON: {e}") from e


def _positive(x, name):
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ParseError(f"{name} not numeric: {x!r}")
    if not math.isfinite(v) or v <= 0:
        raise ParseError(f"{name} not a positive number: {x!r}")
    return v


def parse_market_list(resp):
    d = _json(resp)
    markets = d.get("markets")
    if not isinstance(markets, list):
        raise ParseError("missing 'markets' list")
    market = {k: markets[0].get(k) for k in MARKET_FIELDS} if len(markets) == 1 else None
    return {
        "n_open_markets": len(markets),
        "open_tickers": [m.get("ticker") for m in markets],
        "market": market,
    }


def parse_orderbook(resp):
    """Kalshi lists bids only. YES ask = 1 - best NO bid; NO ask = 1 - best YES bid."""
    d = _json(resp)
    ob = d.get("orderbook_fp")
    if not isinstance(ob, dict):
        raise ParseError("missing 'orderbook_fp'")

    def side(key):
        out = []
        for lvl in ob.get(key) or []:
            if not (isinstance(lvl, list) and len(lvl) == 2):
                raise ParseError(f"bad level in {key}: {lvl!r}")
            out.append([str(lvl[0]), str(lvl[1])])  # [price_dollars, size_contracts]
        return out

    return {"yes_bids": side("yes_dollars"), "no_bids": side("no_dollars")}


def parse_coinbase_ticker(resp):
    d = _json(resp)
    try:
        ts = parse_iso_utc(d["time"])
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f"bad 'time': {e}")
    return {"bid": _positive(d.get("bid"), "bid"), "ask": _positive(d.get("ask"), "ask"),
            "source_ts": ts, "source_ts_kind": "last_trade_time"}


def parse_kraken_ticker(resp):
    d = _json(resp)
    if d.get("error"):
        raise ParseError(f"kraken error: {d['error']}")
    result = d.get("result") or {}
    if len(result) != 1:
        raise ParseError(f"expected one pair, got {list(result)}")
    ((pair, t),) = result.items()
    ts = http_date_to_epoch(resp.headers.get("date"))
    if ts is None:
        raise ParseError("no Date header (Kraken's ticker has no timestamp of its own)")
    return {"bid": _positive(t["b"][0], "bid"), "ask": _positive(t["a"][0], "ask"),
            "source_ts": ts, "source_ts_kind": "http_date_header", "pair": pair}


def parse_bitstamp_ticker(resp):
    d = _json(resp)
    return {"bid": _positive(d.get("bid"), "bid"), "ask": _positive(d.get("ask"), "ask"),
            "source_ts": _positive(d.get("timestamp"), "timestamp"), "source_ts_kind": "ticker_timestamp"}


def parse_coinbase_candles(resp):
    """Coinbase rows: [start, low, high, open, close, volume], newest first. Keep completed ones."""
    d = _json(resp)
    if not isinstance(d, list):
        raise ParseError("expected a list of candles")
    items = sorted([int(c[0]), float(c[4])] for c in d if int(c[0]) + 60 <= resp.received_at)
    if not items:
        raise ParseError("no completed candles")
    return items[-CANDLES_KEEP:]


# ---------------------------------------------------------------- fetching

def _blank_record(name, url, error=None):
    return {"name": name, "url": url, "ok": False, "status": None, "sent_at": None,
            "received_at": None, "http_date": None, "cache_age_s": None, "error": error}


def fetch_source(name, url, transport, parser, timeout):
    """Fetch and parse one source. Never raises for network/HTTP/parse problems: records them."""
    rec = _blank_record(name, url)
    try:
        resp = get(url, transport, timeout)
    except FetchError as e:
        rec["error"] = f"fetch_failed: {e}"
        return rec, None
    rec.update(status=resp.status, sent_at=resp.sent_at, received_at=resp.received_at,
               http_date=http_date_to_epoch(resp.headers.get("date")))
    age = resp.headers.get("age")
    if age is not None:
        try:
            rec["cache_age_s"] = float(age)
        except ValueError:
            pass
    if resp.status != 200:
        rec["error"] = f"http_{resp.status}"
        return rec, None
    try:
        parsed = parser(resp)
    except (ParseError, KeyError, TypeError, IndexError, ValueError) as e:
        rec["error"] = f"parse_error: {e}"
        return rec, None
    rec["ok"] = True
    return rec, parsed


class Collector:
    def __init__(self, transport, clock=time.time, synthetic=False, id_factory=None,
                 seed_history=None, seed_basis=None, timeout=4.0):
        self.transport = transport
        self.clock = clock
        self.synthetic = synthetic
        self.id_factory = id_factory or (lambda ts: f"{iso_utc(ts)}-{uuid.uuid4().hex[:8]}")
        self.history = deque([list(p) for p in (seed_history or [])])
        self.basis = deque([list(r) for r in (seed_basis or []) if isinstance(r, (list, tuple))][-BASIS_KEEP:])
        self.timeout = timeout
        self._candles = None  # (record, items)
        self._pool = ThreadPoolExecutor(max_workers=5)

    def close(self):
        self._pool.shutdown(wait=True)

    def _candles_due(self, now):
        if self._candles is None:
            return True
        rec, _ = self._candles
        return (not rec["ok"]) or now - rec["received_at"] >= CANDLE_REFRESH_S

    def collect(self) -> dict:
        now = self.clock()
        jobs = {
            "kalshi_market_list": (URL_MARKET_LIST, parse_market_list),
            "coinbase_ticker": (URL_COINBASE_TICKER, parse_coinbase_ticker),
            "kraken_ticker": (URL_KRAKEN_TICKER, parse_kraken_ticker),
            "bitstamp_ticker": (URL_BITSTAMP_TICKER, parse_bitstamp_ticker),
        }
        if self._candles_due(now):
            jobs["coinbase_candles"] = (URL_COINBASE_CANDLES, parse_coinbase_candles)
        futures = {name: self._pool.submit(fetch_source, name, url, self.transport, parser, self.timeout)
                   for name, (url, parser) in jobs.items()}
        results = {name: f.result() for name, f in futures.items()}
        if "coinbase_candles" in results:
            self._candles = results.pop("coinbase_candles")

        sources = {name: rec for name, (rec, _) in results.items()}
        cand_rec, cand_items = self._candles
        sources["coinbase_candles"] = cand_rec

        ml = results["kalshi_market_list"][1]
        market = ml["market"] if ml else None
        book = None
        if market and market.get("ticker"):
            url = URL_ORDERBOOK.format(ticker=quote(str(market["ticker"]), safe=""))
            ob_rec, book = fetch_source("kalshi_orderbook", url, self.transport, parse_orderbook, self.timeout)
        else:
            ob_rec = _blank_record("kalshi_orderbook", None, "not_fetched: no single open market")
        sources["kalshi_orderbook"] = ob_rec

        spot = {v: results[f"{v}_ticker"][1] for v in VENUES}
        collected_at = self.clock()
        self._measure_basis(market, collected_at)
        snap = {
            "schema": "snapshot/2",
            "snapshot_id": self.id_factory(collected_at),
            "synthetic": self.synthetic,
            "collector_version": __version__,
            "collected_at": collected_at,
            "collected_at_iso": iso_utc(collected_at),
            "sources": sources,
            "n_open_markets": ml["n_open_markets"] if ml else None,
            "open_tickers": ml["open_tickers"] if ml else None,
            "market": market,
            "book": book,
            "spot": spot,
            "candles": {"fetched_at": cand_rec["received_at"], "items": cand_items} if cand_items else None,
            "spot_history": [list(p) for p in self.history],
            "basis_log": [list(r) for r in self.basis],
        }
        if self.synthetic:
            snap["label"] = SYNTHETIC_LABEL
        self._remember(collected_at, spot)
        return snap

    def _measure_basis(self, market, now):
        """Once per window, record [open_ts, ticker, strike, our average, points] for the minute before open.

        The strike is BRTI's average over that minute; ours is the median of our three
        exchanges, averaged over the history points in the same minute. Measured on the
        first tick after the open, while the history still covers that minute.
        """
        if not isinstance(market, dict):
            return
        ticker, strike = market.get("ticker"), market.get("floor_strike")
        if not ticker or isinstance(strike, bool) or not isinstance(strike, (int, float)) or not strike > 0:
            return
        try:
            open_ts = parse_iso_utc(market.get("open_time") or "")
        except ValueError:
            return
        if not (open_ts < now <= open_ts + HISTORY_KEEP_S - 60) or any(r[1:2] == [ticker] for r in self.basis):
            return
        pts = [median(p[1:4]) for p in self.history
               if open_ts - 60 <= p[0] <= open_ts and len(p) >= 4 and all(m is not None for m in p[1:4])]
        ours = sum(pts) / len(pts) if pts else None
        self.basis.append([open_ts, ticker, float(strike), ours, len(pts)])
        while len(self.basis) > BASIS_KEEP:
            self.basis.popleft()

    def _remember(self, t, spot):
        """Append this tick's venue mids to the rolling history used by later snapshots."""
        mids = [((s["bid"] + s["ask"]) / 2) if s else None for s in (spot[v] for v in VENUES)]
        if all(m is None for m in mids):
            return
        if self.history and t - self.history[-1][0] < HISTORY_SPACING_S:
            return
        self.history.append([t, *mids])
        while self.history and t - self.history[0][0] > HISTORY_KEEP_S:
            self.history.popleft()


def market_close_ts(snapshot) -> float | None:
    try:
        return parse_iso_utc((snapshot.get("market") or {}).get("close_time") or "")
    except ValueError:
        return None
