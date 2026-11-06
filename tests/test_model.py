import math
import unittest
from decimal import Decimal

from kalshi_paper.model import fair_probability, settlement_basis, sigma_per_second, taker_fee_per_contract
from kalshi_paper.rules import RULES


class FeeTests(unittest.TestCase):
    def test_matches_kalshi_published_example(self):
        # Fee schedule example: 100 contracts at $0.55 cost $1.74 (0.07*100*0.55*0.45 = 1.7325, rounded up)
        self.assertEqual(taker_fee_per_contract(Decimal("0.55"), 100) * 100, Decimal("1.74"))

    def test_order_total_rounds_up_to_the_cent(self):
        # 10 x 0.07 x 0.5 x 0.5 = $0.175 -> $0.18 for the order -> 1.8 cents per contract
        self.assertEqual(taker_fee_per_contract(Decimal("0.50"), 10), Decimal("0.018"))

    def test_fee_is_smaller_near_the_extremes(self):
        self.assertLess(taker_fee_per_contract(Decimal("0.90"), 10), taker_fee_per_contract(Decimal("0.50"), 10))


class FairValueTests(unittest.TestCase):
    def test_at_the_target_it_is_a_coin_flip(self):
        p, _, tau = fair_probability(77000, 77000, 1e-4, 400)
        self.assertAlmostEqual(p, 0.5)
        self.assertEqual(tau, 360)  # 60-second settlement average -> horizon minus 40 s

    def test_up_and_down_are_symmetric(self):
        up, _, _ = fair_probability(77100, 77000, 1e-4, 400)
        down, _, _ = fair_probability(77000 ** 2 / 77100, 77000, 1e-4, 400)
        self.assertAlmostEqual(up + down, 1.0)

    def test_less_time_left_means_more_certain(self):
        near, _, _ = fair_probability(77100, 77000, 1e-4, 120)
        far, _, _ = fair_probability(77100, 77000, 1e-4, 800)
        self.assertGreater(near, far)

    def test_refuses_the_final_minute(self):
        with self.assertRaises(ValueError):
            fair_probability(77100, 77000, 1e-4, 30)

    def test_sigma_scales_minute_returns_to_seconds(self):
        closes = [100.0]
        for i in range(60):
            closes.append(closes[-1] * math.exp(0.001 if i % 2 else -0.001))
        expected = 0.001 * math.sqrt(60 / 59) / math.sqrt(60)
        self.assertAlmostEqual(sigma_per_second(closes), expected, places=12)


class SettlementBasisTests(unittest.TestCase):
    """Rows are [open_ts, ticker, strike, our average over the minute before open, points]."""

    NOW = 1_900_000_000.0

    def row(self, minutes_ago, gap, points=6, strike=80000.0):
        return [self.NOW - 60 * minutes_ago, f"T{minutes_ago}", strike, strike - gap, points]

    def test_needs_the_minimum_number_of_measurements(self):
        self.assertIsNone(settlement_basis([], self.NOW, RULES))
        self.assertIsNone(settlement_basis([self.row(5, 4.0)], self.NOW, RULES))  # rules want 2

    def test_averages_recent_measurements(self):
        self.assertEqual(settlement_basis([self.row(20, 2.0), self.row(5, 6.0)], self.NOW, RULES), (4.0, 2))

    def test_uses_only_the_most_recent_windows(self):
        rows = [self.row(5 + 15 * i, 100.0 if i >= RULES.basis_windows else 1.0) for i in range(RULES.basis_windows + 2)]
        rows = [r for r in rows if self.NOW - r[0] <= RULES.basis_max_age_s]
        self.assertEqual(settlement_basis(rows, self.NOW, RULES)[0], 1.0)

    def test_ignores_old_thin_future_and_malformed_rows(self):
        rows = [self.row(5, 3.0), self.row(20, 5.0),
                self.row(200, 500.0),                 # older than 2 hours
                self.row(35, 500.0, points=2),         # too few of our own points in that minute
                self.row(-15, 500.0),                  # measured "after now"
                [self.NOW - 600, "x", "80000", 79990.0, 6],  # strike as text
                [self.NOW - 600, "x", 80000.0, None, 0],     # no points at all
                [self.NOW - 600, "x", 80000.0, float("nan"), 6],
                "garbage", [1, 2]]
        self.assertEqual(settlement_basis(rows, self.NOW, RULES), (4.0, 2))
        self.assertIsNone(settlement_basis("not a list", self.NOW, RULES))


if __name__ == "__main__":
    unittest.main()
