"""Contract checks against a REAL market captured from Kalshi's public API on 2026-11-04."""

import unittest
from datetime import datetime, timezone

from kalshi_paper.demo import market_json
from kalshi_paper.validate import check_market

# Real data (not synthetic): fields of KXBTC15M-26NOV032345-45 as returned by
# GET /trade-api/v2/markets?series_ticker=KXBTC15M&status=open
REAL_MARKET = {
    "ticker": "KXBTC15M-26NOV032345-45",
    "event_ticker": "KXBTC15M-26NOV032345",
    "title": "BTC price up in next 15 mins?",
    "market_type": "binary",
    "strike_type": "greater_or_equal",
    "status": "active",
    "open_time": "2026-11-04T04:30:00Z",
    "close_time": "2026-11-04T04:45:00Z",
    "floor_strike": 76670.93,
    "yes_sub_title": "Target Price: $76,670.93",
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' BRTI before 11:45 PM EST on "
        "Nov 3, 2026 is at least the simple average of the sixty seconds of CF Benchmarks' BRTI before "
        "11:30 PM EST on November 3, 2026, then the market resolves to Yes."
    ),
}


def check(**changes):
    return check_market({**REAL_MARKET, **changes}, "KXBTC15M", 900)


class RealMarketTests(unittest.TestCase):
    def test_real_market_is_accepted(self):
        self.assertIsNone(check())

    def test_ticker_must_match_close_time(self):
        self.assertEqual(check(ticker="KXBTC15M-26NOV032330-30", event_ticker="KXBTC15M-26NOV032330"),
                         "wrong_market:ticker_time_mismatch")

    def test_other_series_rejected(self):
        self.assertEqual(check(ticker="KXETH15M-26NOV032345-45"), "wrong_market:ticker")

    def test_window_must_be_15_minutes(self):
        self.assertEqual(check(open_time="2026-11-04T04:15:00Z"), "wrong_market:window_length")

    def test_contract_times_must_match_market_times(self):
        text = REAL_MARKET["rules_primary"].replace("11:45 PM", "11:50 PM")
        self.assertEqual(check(rules_primary=text), "wrong_market:rules_times")

    def test_displayed_target_must_match_floor_strike(self):
        self.assertEqual(check(yes_sub_title="Target Price: $76,999.00"), "wrong_market:target_mismatch")

    def test_inverted_contract_rejected(self):
        self.assertEqual(check(strike_type="less"), "wrong_market:strike_type")

    def test_settled_market_rejected(self):
        self.assertEqual(check(status="finalized"), "market_not_active:finalized")

    def test_summer_time_wording_accepted(self):
        o = datetime(2027, 7, 1, 14, 0, tzinfo=timezone.utc).timestamp()  # 10:00 AM EDT
        m = market_json(o, o + 900, 80000.0)
        self.assertIn("EDT", m["rules_primary"])
        self.assertIsNone(check_market(m, "KXBTC15M", 900))


if __name__ == "__main__":
    unittest.main()
