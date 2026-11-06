"""Slippage tracker on SYNTHETIC decisions."""

import tempfile
import unittest
from pathlib import Path

from kalshi_paper.slippage import Tracker, format_report, reprice, summarize
from kalshi_paper.storage import append_jsonl


def check(t, yes_ask, size="5000", ticker="W1", act_side=None, contracts=10):
    no_ask = round(1 - yes_ask + 0.01, 4)  # 1c spread
    d = {"evaluated_at": t, "market_ticker": ticker, "close_time": "2030-01-15T15:15:00Z", "decision": "SKIP",
         "features": {"yes_ask": f"{yes_ask:.4f}", "no_ask": f"{no_ask:.4f}",
                      "best_no_bid_size": size, "best_yes_bid_size": size}}
    if act_side:
        price = yes_ask if act_side == "YES" else no_ask
        d.update(decision="ACT", intent={"side": act_side, "limit_price": f"{price:.4f}", "contracts": contracts,
                                         "displayed_size_at_price": size})
    return d


TRADE = {"at": 100.0, "ticker": "W1", "close_time": None, "version": "v", "side": "YES", "price": 0.60,
         "contracts": 10, "shown_size": 5000.0}


def quotes(*pairs, size=5000.0):
    return [(t, a, round(1 - a + 0.01, 4), size, size) for t, a in pairs]


class RepriceTests(unittest.TestCase):
    def test_price_ran_away_on_a_winner(self):
        r = reprice(TRADE, quotes((100.0, 0.60), (102.0, 0.63), (106.0, 0.66)), "yes")
        m = r["models"]
        self.assertEqual((r["next_ask"], r["next_after_s"], r["late_ask"]), (0.63, 2.0, 0.66))
        self.assertEqual(m["shown"]["price"], 0.60)
        self.assertEqual(m["market_2s"]["price"], 0.63)
        self.assertEqual(m["market_5s"]["price"], 0.66)
        self.assertFalse(m["limit"]["filled"])              # ask rose above the limit: missed the winner
        self.assertEqual(m["limit"]["pnl"], 0.0)
        self.assertFalse(m["limit_2c"]["filled"])           # 3c above: also missed
        # fee rounds up per order: $0.17 on 10 at 60c or 63c. 10 x (1 - 0.63) - 0.17 = +3.53 vs +3.83
        self.assertEqual((m["shown"]["pnl"], m["market_2s"]["pnl"]), (3.83, 3.53))

    def test_price_came_back_on_a_loser_and_the_limit_fills_cheaper(self):
        r = reprice(TRADE, quotes((102.0, 0.58), (105.5, 0.55)), "no")
        m = r["models"]
        self.assertTrue(m["limit"]["filled"])
        self.assertEqual(m["limit"]["price"], 0.58)          # a limit fills at the better ask
        self.assertEqual(m["limit"]["pnl"], -5.98)           # 10 x 0.58 + $0.18 fee
        self.assertEqual(m["shown"]["pnl"], -6.17)           # 10 x 0.60 + $0.17 fee

    def test_thin_book_prices_the_rest_one_tick_higher(self):
        r = reprice(TRADE, quotes((102.0, 0.60), size=12.0), "yes", contracts=30)
        # 12 at 60c + 18 at 61c = 60.6c average
        self.assertAlmostEqual(r["models"]["market_2s"]["price"], 0.606)
        self.assertEqual(r["contracts"], 30)

    def test_unmeasured_when_no_later_check(self):
        r = reprice(TRADE, quotes((100.0, 0.60), (130.0, 0.70)), "yes")
        self.assertFalse(r["models"]["market_2s"]["measured"])
        self.assertFalse(r["models"]["market_5s"]["measured"])
        self.assertEqual(summarize([r])["compared"], 0)

    def test_open_trades_have_no_pnl(self):
        r = reprice(TRADE, quotes((102.0, 0.61), (105.0, 0.61)), None)
        self.assertIsNone(r["models"]["market_2s"]["pnl"])
        self.assertEqual(summarize([r])["compared"], 0)


class SummaryTests(unittest.TestCase):
    def test_totals_and_moves(self):
        win = reprice(TRADE, quotes((102.0, 0.63), (105.0, 0.64)), "yes")
        lose = reprice({**TRADE, "ticker": "W2"}, quotes((102.0, 0.57), (105.0, 0.55)), "no")
        s = summarize([win, lose])
        self.assertEqual(s["compared"], 2)
        self.assertEqual(s["models"]["shown"]["pnl"], round(3.83 - 6.17, 2))
        self.assertEqual(s["models"]["market_2s"]["pnl"], round(3.53 - 5.88, 2))  # loser filled at 57c, $0.18 fee
        self.assertEqual((s["models"]["limit"]["filled"], s["models"]["limit"]["missed_winners"]), (1, 1))
        self.assertEqual((s["next_check_move_cents"]["winners"], s["next_check_move_cents"]["losers"]), (3.0, -3.0))
        self.assertIn("Market order, ~2 s later", format_report({"contracts": None, "summary": s}))


class TrackerTests(unittest.TestCase):
    def test_reads_every_version_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            append_jsonl(d / "decisions-v0.2.0.jsonl", check(100.0, 0.60, act_side="YES"))
            append_jsonl(d / "decisions-v0.2.0.jsonl", check(102.0, 0.62))
            tr = Tracker(d)
            rep = tr.report({})
            self.assertEqual(len(rep["trades"]), 1)
            self.assertEqual(rep["trades"][0]["next_ask"], 0.62)
            append_jsonl(d / "decisions-v0.3.0.jsonl", check(200.0, 0.40, ticker="W2", act_side="NO"))
            append_jsonl(d / "decisions-v0.3.0.jsonl", check(202.0, 0.42, ticker="W2"))
            append_jsonl(d / "decisions-v0.3.0.jsonl", check(206.0, 0.43, ticker="W2"))
            rep = tr.report({"W1": {"result": "yes"}, "W2": {"result": "no"}}, contracts=30)
            self.assertEqual([t["version"] for t in rep["trades"]], ["v0.2.0", "v0.3.0"])
            no = rep["trades"][1]
            self.assertEqual((no["price"], no["next_ask"], no["contracts"]), (0.61, 0.59, 30))  # NO got cheaper
            self.assertTrue(no["models"]["limit"]["filled"])


if __name__ == "__main__":
    unittest.main()
