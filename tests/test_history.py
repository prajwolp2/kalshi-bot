"""History fetching and caching against a fake transport. Every response here is SYNTHETIC (dated 2030)."""

import calendar
import json
import re
import tempfile
import time
import unittest

from kalshi_paper import history
from kalshi_paper.collector import KALSHI_BASE
from kalshi_paper.http import FetchError, Response

OPEN = 1_893_999_600  # a 15-minute boundary in January 2030 (synthetic)
LIST_URL = f"{KALSHI_BASE}/markets?series_ticker=KXBTC15M&status=settled"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def epoch(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def market(i, result="yes", length=900, strike=80000.0, strike_type="greater_or_equal"):
    o = OPEN + 900 * i
    return {"ticker": f"KXBTC15M-SYNTH{i:02d}", "open_time": iso(o), "close_time": iso(o + length),
            "floor_strike": strike, "result": result, "strike_type": strike_type, "status": "finalized"}


class FakeTransport:
    """Two pages of settled markets, Kalshi candles per window, Coinbase candles per minute."""

    def __init__(self, markets):
        self.markets, self.calls = markets, []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if "/historical/markets?series_ticker=" in url:  # archive: ignores dates, pages through everything
            page2 = "cursor=" in url
            body = {"markets": self.markets[2:] if page2 else self.markets[:2], "cursor": "" if page2 else "p2"}
        elif "/historical/markets/" in url:  # archive candles say "close", not "close_dollars"
            o = int(re.search(r"start_ts=(\d+)", url).group(1))
            body = {"candlesticks": [{"end_period_ts": o + 60 * m, "yes_bid": {"close": "0.4800"},
                                      "yes_ask": {"close": "0.5200"}} for m in range(1, 16)]}
        elif "/markets?series_ticker=" in url:
            page2 = "cursor=" in url
            body = {"markets": self.markets[2:] if page2 else self.markets[:2], "cursor": "" if page2 else "page 2"}
        elif "/candlesticks" in url:
            o = int(re.search(r"start_ts=(\d+)", url).group(1))
            body = {"candlesticks": [{"end_period_ts": o + 60 * m, "yes_bid": {"close_dollars": "0.4900"},
                                      "yes_ask": {"close_dollars": "0.5100"}} for m in range(1, 16)]}
        elif "coinbase.com" in url:
            start, end = (epoch(re.search(k + r"=([^&]+)", url).group(1)) for k in ("start", "end"))
            body = [[t, 1.0, 2.0, 1.5, 80000.0 + (t - OPEN) / 60, 3.0] for t in range(end, start - 60, -60)]
        else:
            raise FetchError(f"no route for {url}")
        return Response(url, 200, {}, json.dumps(body).encode(), 0.0, 0.0)


class ParserTests(unittest.TestCase):
    def test_settled_window(self):
        w = history.parse_settled(market(0))
        self.assertEqual(w, {"ticker": "KXBTC15M-SYNTH00", "open_ts": OPEN, "close_ts": OPEN + 900,
                             "strike": 80000.0, "result": "yes"})

    def test_unusable_windows_are_dropped(self):
        for bad in (market(0, result=""), market(0, length=600), market(0, strike=None),
                    market(0, strike_type="less"), {"ticker": "x"}):
            with self.subTest(bad=bad):
                self.assertIsNone(history.parse_settled(bad))

    def test_candles_skip_malformed_entries(self):
        d = {"candlesticks": [{"end_period_ts": 120, "yes_bid": {"close_dollars": "0.41"}, "yes_ask": {"close_dollars": "0.43"}},
                              {"end_period_ts": 60, "yes_bid": {"close_dollars": "0.40"}},  # no ask
                              {"end_period_ts": 60, "yes_bid": {"close_dollars": "0.40"}, "yes_ask": {"close_dollars": "0.42"}}]}
        self.assertEqual(history.parse_market_candles(d), [[60, 0.40, 0.42], [120, 0.41, 0.43]])
        self.assertEqual(history.parse_market_candles("not a dict"), [])

    def test_coinbase_rows(self):
        rows = [[120, 1, 2, 1.5, 80001.5, 3], ["bad"], [60, 1, 2, 1.5, 80000.0, 3]]
        self.assertEqual(history.parse_spot_candles(rows), {60: 80000.0, 120: 80001.5})


class FetchTests(unittest.TestCase):
    def test_retries_with_backoff(self):
        calls = []

        def flaky(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise FetchError("timed out")
            body, status = {2: (b"busy", 503), 3: (b"not json", 200)}.get(len(calls), (b'{"ok": true}', 200))
            return Response(url, status, {}, body, 0.0, 0.0)

        waits = []
        self.assertEqual(history.get_json(LIST_URL, flaky, sleep=waits.append), {"ok": True})
        self.assertEqual(waits, [1.0, 2.0, 4.0])

    def test_gives_up_after_bounded_retries(self):
        def down(url, timeout):
            raise FetchError("connection refused")

        waits = []
        with self.assertRaises(history.HistoryError):
            history.get_json(LIST_URL, down, sleep=waits.append)
        self.assertEqual(len(waits), history.MAX_TRIES - 1)

    def test_refuses_hosts_off_the_allow_list(self):
        def never(url, timeout):
            self.fail("transport must not be reached")

        with self.assertRaises(ValueError):
            history.get_json("https://example.com/markets", never, sleep=lambda s: None)


class CacheTests(unittest.TestCase):
    def test_update_fetches_once_and_caches(self):
        markets = [market(0), market(1), market(2, result=""), market(3, length=600)]
        t = FakeTransport(markets)
        with tempfile.TemporaryDirectory() as tmp:
            r = history.update(tmp, "KXBTC15M", OPEN, OPEN + 4 * 900, t, sleep=lambda s: None, log=None, cutoff=0)
            self.assertEqual((r["windows_in_range"], r["windows_fetched"]), (2, 2))
            self.assertGreater(r["spot_minutes_added"], 60)
            self.assertTrue(any("cursor=page%202" in u for u in t.calls))

            windows, spot = history.load(tmp, "KXBTC15M")
            self.assertEqual([w["ticker"] for w in windows], ["KXBTC15M-SYNTH00", "KXBTC15M-SYNTH01"])
            self.assertEqual(windows[0]["candles"][0], [OPEN + 60, 0.49, 0.51])
            self.assertEqual(spot[OPEN], 80000.0)

            before = len(t.calls)
            r2 = history.update(tmp, "KXBTC15M", OPEN, OPEN + 4 * 900, t, sleep=lambda s: None, log=None, cutoff=0)
            self.assertEqual((r2["windows_fetched"], r2["spot_minutes_added"]), (0, 0))
            new = t.calls[before:]
            self.assertFalse(any("/candlesticks" in u or "coinbase.com" in u for u in new), new)


class ArchiveTests(unittest.TestCase):
    def test_old_windows_come_from_the_archive_and_are_filtered_by_date(self):
        markets = [market(0), market(1), market(2), market(3)]
        t = FakeTransport(markets)
        cutoff = OPEN + 10 * 900  # everything here settled before the archive cutoff
        with tempfile.TemporaryDirectory() as tmp:
            # Ask only for windows 1 and 2: the archive returns all four, the fetcher keeps two.
            r = history.update(tmp, "KXBTC15M", OPEN + 2 * 900, OPEN + 3 * 900, t,
                               sleep=lambda s: None, log=None, cutoff=cutoff)
            self.assertEqual(r["windows_fetched"], 2)
            windows, _ = history.load(tmp, "KXBTC15M")
            self.assertEqual([w["ticker"] for w in windows], ["KXBTC15M-SYNTH01", "KXBTC15M-SYNTH02"])
            self.assertEqual(windows[0]["candles"][0], [OPEN + 900 + 60, 0.48, 0.52])
            self.assertTrue(any("cursor=p2" in u for u in t.calls))
            self.assertFalse(any("/series/KXBTC15M/markets/" in u for u in t.calls))


if __name__ == "__main__":
    unittest.main()
