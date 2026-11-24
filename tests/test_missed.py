"""Missed-trades report on SYNTHETIC live checks and backtest trades."""

import unittest
from dataclasses import replace
from unittest import mock

from kalshi_paper import missed
from kalshi_paper.rules import RULES

T0 = 1_893_999_600  # a window open, 2030


def check(t, reason="no_trend", ticker="W1", act=None):
    d = {"evaluated_at": float(t), "market_ticker": ticker, "decision": "SKIP", "reason": reason}
    if act:
        d.update(decision="ACT", reason="all_conditions_met",
                 intent={"side": act, "limit_price": "0.6000", "fee_per_contract": "0.017", "contracts": 10})
    return d


def window(i, result="yes"):
    o = T0 + 900 * i
    return {"ticker": f"W{i}", "open_ts": o, "close_ts": o + 900, "strike": 80000.0, "result": result}


class OfflinePeriodTests(unittest.TestCase):
    def test_gaps_and_the_current_one(self):
        times = [0, 2, 4, 300, 302]
        self.assertEqual(missed.offline_periods(times, now=303), [[4, 300]])
        self.assertEqual(missed.offline_periods(times, now=1000), [[4, 300], [302, 1000]])

    def test_live_state(self):
        times = [100, 102, 400, 402]
        reasons = ["no_trend", "no_trend", "insufficient_basis_history", "insufficient_basis_history"]
        self.assertEqual(missed._live_state(times, reasons, 130), "online")
        self.assertEqual(missed._live_state(times, reasons, 250), "offline")
        self.assertEqual(missed._live_state(times, reasons, 420), "warming_up")


class ReportTests(unittest.TestCase):
    def test_splits_backtest_trades_by_what_the_bot_was_doing(self):
        # Bot runs through window 1, sleeps through window 2, wakes up warming up in window 3.
        live = [check(T0 + 900 + s, ticker="W1") for s in range(0, 900, 2)]
        live[50] = check(live[50]["evaluated_at"], ticker="W1", act="YES")
        live += [check(T0 + 2700 + s, reason="insufficient_basis_history", ticker="W3") for s in range(0, 900, 2)]
        windows = [window(0), window(1), window(2, "no"), window(3)]
        bt = [{"ticker": "W1", "minute": 2, "side": "YES", "price": 0.6, "won": True, "pnl": 3.83},
              {"ticker": "W2", "minute": 3, "side": "YES", "price": 0.5, "won": False, "pnl": -5.18},
              {"ticker": "W3", "minute": 4, "side": "NO", "price": 0.7, "won": False, "pnl": -7.15}]
        with mock.patch.object(missed.study, "observations", return_value=[]), \
                mock.patch.object(missed.study, "paper_trades", return_value=bt):
            # $200 at 5 per $100 = 10 contracts, the minimum; the P&L comes from the sizing, not the input
            r = missed.report(live, windows, {}, replace(RULES, contracts_per_100usd=5.0), now=T0 + 3600, start_usd=200.0)
        self.assertEqual([t["live"] for t in r["trades"]], ["online", "offline", "warming_up"])
        self.assertEqual(r["trades"][0]["live_traded"], "YES")
        self.assertEqual([t["contracts"] for t in r["trades"]], [10, 10, 10])
        self.assertEqual(r["backtest"]["offline"], {"trades": 1, "won": 0, "pnl": -5.18})
        self.assertEqual(r["backtest"]["all"]["pnl"], round(3.83 - 5.18 - 7.15, 2))
        self.assertEqual(r["live"], {"trades": 1, "won": 1, "pnl": 3.83})   # 10 x (1 - 0.60 - 0.017)
        self.assertEqual(r["coverage"], {"watched": 1, "partly": 0, "missed": 2})
        self.assertEqual(r["offline_count"], 1)
        self.assertIn("Missed trades", missed.format_report(r))

    def test_no_live_decisions(self):
        self.assertIsNone(missed.report([], [], {}, RULES)["since"])


if __name__ == "__main__":
    unittest.main()
