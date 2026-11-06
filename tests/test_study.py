"""The minute-bar study on SYNTHETIC history generated here (dated 2030)."""

import math
import random
import time
import unittest

from kalshi_paper import study
from kalshi_paper.rules import RULES

OPEN0 = 1_893_999_600  # a 15-minute boundary in January 2030 (synthetic)


def synthetic_history(n_windows=12, gap=10.0, seed=3):
    """Coinbase follows a random walk; the settlement index (and so every strike) sits `gap` dollars above it."""
    rnd = random.Random(seed)
    spot, p = {}, 80000.0
    for t in range(OPEN0 - 7200, OPEN0 + 900 * n_windows + 60, 60):
        p *= math.exp(rnd.gauss(0, 0.0005))
        spot[t] = round(p, 2)
    windows = []
    for i in range(n_windows):
        o = OPEN0 + 900 * i
        c = o + 900
        strike = (spot[o - 120] + spot[o - 60]) / 2 + gap
        final = (spot[c - 120] + spot[c - 60]) / 2 + gap
        candles = []
        for m in range(1, 16):
            t = o + 60 * m
            mid = 0.5 + max(-0.4, min(0.4, (spot[t - 60] + gap - strike) / 200))
            candles.append([t, round(mid - 0.005, 3), round(mid + 0.005, 3)])
        windows.append({"ticker": f"KXBTC15M-SYNTH{i:02d}", "open_ts": o, "close_ts": c, "strike": strike,
                        "result": "yes" if final >= strike else "no", "candles": candles})
    return windows, spot


def row(**kw):
    r = {"ticker": "A", "close_ts": 1, "minute": 1, "y": 1, "p": 0.80, "bid": 0.64, "ask": 0.66, "mid": 0.65,
         "basis": 0.0, "has_momentum": True, "trend": "UP", "ret_60m": 0.0}
    r.update(kw)
    return r


class ObservationTests(unittest.TestCase):
    def test_rebuilds_minutes_like_the_live_rules(self):
        windows, spot = synthetic_history()
        rows = study.observations(windows, spot, RULES)
        self.assertTrue(rows)
        # The first window has only one gap measurement so far; the rules want two.
        self.assertNotIn(windows[0]["ticker"], {r["ticker"] for r in rows})
        for r in rows:
            self.assertAlmostEqual(r["basis"], 10.0, places=6)
            self.assertTrue(0 < r["p"] < 1)
            self.assertLessEqual(r["minute"], 13)  # the final 90 s are skipped


class PaperTradeTests(unittest.TestCase):
    def test_trend_side_with_enough_edge_trades_once_per_window(self):
        trades = study.paper_trades([row(), row(minute=2)], RULES)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["side"], t["price"], t["won"]), ("YES", 0.66, True))
        self.assertAlmostEqual(t["pnl"], 10 * (1 - 0.66 - 0.016))

    def test_gates(self):
        cases = {
            "no trend": ([row(trend=None)], 0, 1),
            "trend points the other way": ([row(trend="DOWN")], 0, 1),
            "spread too wide": ([row(bid=0.63, ask=0.66)], 0, 0),
            "price out of band": ([row(bid=0.94, ask=0.95, mid=0.945, p=0.99)], 0, 0),
            "fair price": ([row(p=0.67)], 0, 0),
            "already up 0.5% this hour": ([row(ret_60m=0.005)], 0, 1),
            "down 0.5% this hour, buying NO": ([row(trend="DOWN", p=0.2, ret_60m=-0.005)], 0, 1),
        }
        for name, (rows, with_filter, shadow) in cases.items():
            with self.subTest(name):
                self.assertEqual(len(study.paper_trades(rows, RULES, True)), with_filter)
                self.assertEqual(len(study.paper_trades(rows, RULES, False)), shadow)


class ReportTests(unittest.TestCase):
    def test_recent_and_earlier_periods(self):
        windows, spot = synthetic_history()
        r = study.run(windows, spot, RULES, recent_days=1 / 24)
        self.assertEqual(r["rules_version"], RULES.version)
        self.assertEqual(r["recent"]["windows"], 4)
        self.assertEqual(r["earlier"]["windows"], 7)
        self.assertAlmostEqual(r["recent"]["basis_usd"], 10.0, places=2)
        text = study.format_report(r)
        for part in ("Recent", "Earlier", "Calibration", "Caveats"):
            self.assertIn(part, text)


class MonthlyTests(unittest.TestCase):
    def test_months_split_and_kelly_bankroll_carries_over(self):
        windows, spot = synthetic_history(n_windows=16)
        # Pretend each hour is a "month" so the 16 synthetic windows split into 4 groups.
        r = study.monthly(windows, spot, RULES, month_of=lambda ts: time.strftime("%H", time.gmtime(ts - 1)))
        self.assertEqual([m["windows"] for m in r["months"]], [4, 4, 4, 4])
        usable = [m for m in r["months"] if m.get("minute_looks")]
        for a, b in zip(usable, usable[1:]):
            self.assertEqual(b["kelly"]["start"], a["kelly"]["end"])
        self.assertEqual(r["kelly_end"], usable[-1]["kelly"]["end"])
        self.assertIn("Quarter Kelly", study.format_monthly(r))


if __name__ == "__main__":
    unittest.main()
