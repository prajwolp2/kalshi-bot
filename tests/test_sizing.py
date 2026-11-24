"""Bankroll-scaled order size, on SYNTHETIC trades."""

import unittest
from dataclasses import replace

from kalshi_paper import sizing
from kalshi_paper.rules import RULES

R = replace(RULES, paper_contracts=10, contracts_per_100usd=5.0, max_contracts=1000)


def trade(i, won, price=0.5):
    return {"at": 1000.0 + 900 * i, "close_ts": 1500.0 + 900 * i, "side": "YES", "price": price, "won": won}


class ContractsForTests(unittest.TestCase):
    def test_grows_with_the_bankroll_between_floor_and_cap(self):
        self.assertEqual(sizing.contracts_for(300, 0.6, R), 15)
        self.assertEqual(sizing.contracts_for(600, 0.6, R), 30)
        self.assertEqual(sizing.contracts_for(100, 0.6, R), 10)        # floor
        self.assertEqual(sizing.contracts_for(50_000, 0.6, R), 1000)   # cap

    def test_never_more_than_the_cash_pays_for(self):
        rich = replace(R, contracts_per_100usd=500.0)
        self.assertEqual(sizing.contracts_for(100, 0.9, rich), 111)

    def test_no_bankroll_or_no_scaling_means_the_base_size(self):
        self.assertEqual(sizing.contracts_for(None, 0.6, R), 10)
        self.assertEqual(sizing.contracts_for(5000, 0.6, replace(R, contracts_per_100usd=None)), 10)


class SimulateTests(unittest.TestCase):
    def test_wins_grow_the_next_order_only_after_settling(self):
        s = sizing.simulate([trade(0, True), trade(1, True)], 400.0, R)
        # $400 -> 20 contracts at 0.50 + fee 0.07 x 20 x 0.25 = $0.35 (1.75c each); wins $9.65
        self.assertEqual(s["taken"][0]["contracts"], 20)
        self.assertEqual(round(s["taken"][0]["pnl"], 2), 9.65)
        self.assertEqual(s["taken"][1]["contracts"], 20)               # $409.65 -> still 20
        same_time = [trade(0, True), dict(trade(0, True), at=1001.0)]  # second order before the first settles
        self.assertEqual(sizing.simulate(same_time, 400.0, R)["taken"][1]["contracts"], 19)  # $389.65 cash -> 19

    def test_stops_once_the_cash_cannot_cover_the_minimum_order(self):
        s = sizing.simulate([trade(i, False) for i in range(100)], 100.0, R)
        self.assertIsNotNone(s["stopped_at"])
        self.assertLess(s["end"], 9.26)

    def test_rolling_counts_busts(self):
        trades = [trade(i, False) for i in range(2000)]   # all losers over ~20 days
        r = sizing.rolling(trades, 300.0, R, days=5, step_days=5)
        self.assertEqual(r["bust_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
