"""Dashboard state built from SYNTHETIC files in a temp folder."""

import json
import tempfile
import unittest
from pathlib import Path

from kalshi_paper.dashboard import DecisionLog, state
from kalshi_paper.rules import RULES
from kalshi_paper.storage import append_jsonl


def dec(t, ticker="W1", decision="SKIP", reason="no_trend", intent=None):
    return {"evaluated_at": t, "market_ticker": ticker, "close_time": "2030-01-15T15:15:00Z", "decision": decision,
            "reason": reason, "intent": intent, "shadow_no_trend_filter": None,
            "features": {"p_fair": 0.6, "yes_mid": 0.62, "spot_composite": 80000.0, "target_price": 79990.0,
                         "basis_usd": 2.5, "best_yes_bid": "0.6100", "yes_ask": "0.6300", "momentum": {"z": 0.4}}}


class DashboardTests(unittest.TestCase):
    def test_state_reports_bankroll_trades_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / f"bankroll-{RULES.version}.json").write_text(json.dumps({"start_ts": 100.0, "start_usd": "100.00"}))
            log_path = d / f"decisions-{RULES.version}.jsonl"
            append_jsonl(log_path, dec(50.0))  # before the account started: not counted
            intent = {"side": "YES", "limit_price": "0.8300", "fee_per_contract": "0.010", "contracts": 10}
            append_jsonl(log_path, dec(200.0, decision="ACT", reason="all_conditions_met", intent=intent))
            append_jsonl(log_path, dec(205.0, reason="already_acted_this_window"))
            log = DecisionLog(log_path)

            s = state(log, d, RULES, now=210.0)
            self.assertTrue(s["running"])
            self.assertEqual(s["checks"], 2)
            self.assertEqual(s["bankroll"]["balance"], 91.6)       # $8.40 waiting to settle
            self.assertEqual(s["trades"][0]["status"], "open")
            self.assertEqual(s["current"]["gap"], 2.5)
            self.assertEqual(len(s["window_series"]), 3)

            append_jsonl(d / "settlements.jsonl", {"ticker": "W1", "result": "yes"})
            with open(log_path, "a") as f:
                f.write('{"evaluated_at": 21')  # half-written line: ignored until complete
            s = state(log, d, RULES, now=400.0)
            self.assertFalse(s["running"])
            self.assertEqual(s["bankroll"]["balance"], 101.6)
            self.assertEqual((s["trades"][0]["status"], s["trades"][0]["pnl"]), ("won", 1.6))
            self.assertEqual(s["balance_curve"][-1][1], 101.6)

            from kalshi_paper.slippage import Tracker
            s = state(log, d, RULES, now=400.0, slip=Tracker(d))
            self.assertEqual(set(s["slippage"]), {"10", "30"})
            self.assertEqual(s["slippage"]["30"]["trades"][0]["contracts"], 30)


class HistoryProgressTests(unittest.TestCase):
    def test_progress_speed_and_completion(self):
        from kalshi_paper.dashboard import HistoryProgress
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            cache = d / "settled-KXBTC15M.jsonl"
            for i in range(10):  # 10 windows already cached before this download
                append_jsonl(cache, {"ticker": f"T{i}", "close_ts": 1_893_999_600 + 900 * i})
            (d / "fetch.log").write_text("fetching public history...\n110 settled windows in range, 100 not cached yet\n")
            h = HistoryProgress(d, RULES)
            s = h.state(now=1000.0)
            self.assertEqual((s["cached"], s["fetched"], s["remaining"]), (10, 0, 100))
            self.assertIsNone(s["rate_per_s"])
            for i in range(10, 70):
                append_jsonl(cache, {"ticker": f"T{i}", "close_ts": 1_893_999_600 + 900 * i})
            s = h.state(now=1030.0)  # 60 more windows in 30 s
            self.assertEqual((s["fetched"], s["remaining"], s["rate_per_s"], s["eta_s"]), (60, 40, 2.0, 20))
            self.assertEqual(sum(n for _, n in s["months"]), 70)
            with open(d / "fetch.log", "a") as f:
                f.write("100 new window(s), 5 new Coinbase minute(s). Cache: 110 windows, 5 minutes.\n")
            self.assertTrue(h.state(now=1040.0)["done"])


class ConcurrentReadTests(unittest.TestCase):
    def test_parallel_refreshes_never_skip_or_duplicate_lines(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            log = DecisionLog(path)
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    log.refresh()

            threads = [threading.Thread(target=reader) for _ in range(6)]
            for t in threads:
                t.start()
            for i in range(3000):
                append_jsonl(path, {"evaluated_at": float(i), "n": i})
            stop.set()
            for t in threads:
                t.join()
            rows = log.refresh()
            self.assertEqual([r["n"] for r in rows], list(range(3000)))


if __name__ == "__main__":
    unittest.main()
