"""The SYNTHETIC demo scenarios, run through the real collector, evaluator and scorer."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from statistics import median

from kalshi_paper.cli import _last_snapshot
from kalshi_paper.cli import main as cli_main
from kalshi_paper.collector import Collector
from kalshi_paper.demo import SCENARIOS, Scenario, SyntheticTransport, _history, market_json, run_scenarios, run_scoring, window
from kalshi_paper.evaluator import EvalState, evaluate
from kalshi_paper.rules import RULES
from kalshi_paper.storage import write_jsonl


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = run_scenarios()

    def test_every_scenario_gives_the_expected_decision(self):
        self.assertEqual(len(self.results), len(SCENARIOS))
        for sc, _, d in self.results:
            with self.subTest(sc.name):
                self.assertEqual((d["decision"], d["reason"]), sc.expect)

    def test_everything_is_labeled_synthetic(self):
        for sc, snap, d in self.results:
            with self.subTest(sc.name):
                self.assertTrue(snap["synthetic"])
                self.assertIn("SYNTHETIC", snap["label"])
                self.assertTrue(d["synthetic"])
                self.assertIn("SYNTHETIC", d["label"])

    def test_act_is_a_paper_intent_not_a_fill(self):
        acts = [d for _, _, d in self.results if d["decision"] == "ACT"]
        self.assertEqual(len(acts), 2)
        for d in acts:
            self.assertEqual(d["intent"]["type"], "PAPER_INTENT")
            self.assertFalse(d["intent"]["is_fill"])
            self.assertIn("not a fill", d["intent"]["note"])

    def test_skips_never_carry_an_intent(self):
        for _, _, d in self.results:
            if d["decision"] == "SKIP":
                self.assertIsNone(d["intent"])

    def test_scoring_uses_settlements(self):
        settlements, report = run_scoring(self.results)
        self.assertEqual(report["data"], "SYNTHETIC")
        self.assertEqual(len(settlements), 4)
        b = report["strategy_with_trend_filter"]
        self.assertEqual((b["intents"], b["settled"], b["wins"]), (2, 2, 1))
        # YES @0.66 won: +0.324 x 10; NO @0.66 lost: -0.676 x 10 (fee 1.6c each)
        self.assertEqual(Decimal(b["paper_pnl_dollars"]), Decimal("-3.52"))
        # 3 in windows 0-3, plus move_already_extended: no trend filter, so no move filter either
        self.assertEqual(report["shadow_without_trend_filter"]["intents"], 4)


class FailClosedTests(unittest.TestCase):
    """Corrupt one field of a triggering snapshot: the evaluator must SKIP, never raise, never ACT."""

    MUTATIONS = {
        "target price null": lambda s: s["market"].update(floor_strike=None),
        "target price text": lambda s: s["market"].update(floor_strike="abc"),
        "target price negative": lambda s: s["market"].update(floor_strike=-5),
        "target price boolean": lambda s: s["market"].update(floor_strike=True),
        "close time garbage": lambda s: s["market"].update(close_time="garbage"),
        "contract text missing": lambda s: s["market"].update(rules_primary=None),
        "status closed": lambda s: s["market"].update(status="closed"),
        "market missing": lambda s: s.update(market=None),
        "two open markets": lambda s: s.update(n_open_markets=2),
        "yes bids missing": lambda s: s["book"].update(yes_bids=None),
        "yes bid unparseable": lambda s: s["book"].update(yes_bids=[["x", "1"]]),
        "crossed book": lambda s: s["book"].update(yes_bids=[["0.7000", "100"]]),
        "kraken quote missing": lambda s: s["spot"].update(kraken=None),
        "coinbase source failed": lambda s: s["sources"]["coinbase_ticker"].update(ok=False),
        "orderbook record missing": lambda s: s["sources"].pop("kalshi_orderbook"),
        "candles missing": lambda s: s.update(candles=None),
        "candle gap": lambda s: s["candles"]["items"].pop(-10),
        "spot history empty": lambda s: s.update(spot_history=[]),
        "venues disagree": lambda s: s["spot"]["bitstamp"].update(bid=70000.0, ask=70000.5),
        "snapshot id missing": lambda s: s.update(snapshot_id=None),
        "collected_at missing": lambda s: s.pop("collected_at"),
        "clock 10s off": lambda s: s["sources"]["kalshi_orderbook"].update(http_date=s["collected_at"] + 10),
        "gap log missing": lambda s: s.update(basis_log=None),
        "gap log not a list": lambda s: s.update(basis_log="abc"),
        "gap rows garbage": lambda s: s.update(basis_log=[["x", None, "abc", None, "n"], [1]]),
        "gap measured hours ago": lambda s: [r.__setitem__(0, r[0] - 3 * 3600) for r in s["basis_log"]],
        "gap implausibly large": lambda s: [r.__setitem__(3, r[2] - 500.0) for r in s["basis_log"]],
        "gap is not a number": lambda s: [r.__setitem__(3, float("nan")) for r in s["basis_log"]],
    }

    def test_corrupted_inputs_fail_closed(self):
        trigger = run_scenarios()[0][1]
        for name, mutate in self.MUTATIONS.items():
            with self.subTest(name):
                snap = copy.deepcopy(trigger)
                mutate(snap)
                d = evaluate(snap, RULES, EvalState())
                self.assertEqual(d["decision"], "SKIP")
                self.assertIsNone(d["intent"])
                self.assertFalse(d["reason"].startswith("internal_error"), d["reason"] + " " + str(d["detail"]))


class ExtendedMoveTests(unittest.TestCase):
    def test_filter_blocks_only_buys_in_the_direction_of_a_big_hourly_move(self):
        from dataclasses import replace
        from kalshi_paper.evaluator import move_already_extended
        lim = RULES.max_hour_move_with_trade
        self.assertTrue(move_already_extended("YES", lim * 1.01, RULES))
        self.assertTrue(move_already_extended("NO", -lim * 1.01, RULES))
        self.assertFalse(move_already_extended("YES", lim * 0.99, RULES))
        self.assertFalse(move_already_extended("YES", -0.02, RULES))   # buying against the move is allowed
        self.assertFalse(move_already_extended("NO", 0.02, RULES))
        self.assertFalse(move_already_extended("YES", 0.02, replace(RULES, max_hour_move_with_trade=None)))

    def test_hour_move_is_logged_with_every_evaluated_decision(self):
        for sc, _, d in run_scenarios():
            if d["features"]:
                with self.subTest(sc.name):
                    self.assertIsInstance(d["features"]["hour_move"], float)


class BasisMeasurementTests(unittest.TestCase):
    def test_collector_measures_the_gap_once_per_window(self):
        sc = Scenario("measure", "SYNTHETIC", ("SKIP", "any"), window=20, seconds_in=100.0)
        o, c = window(sc.window)
        now = o + sc.seconds_in
        market = market_json(o, c, sc.strike)
        hist = _history(sc, now)  # every 10 s for the last 7 minutes, so it covers the minute before open
        col = Collector(SyntheticTransport(sc, now, market), clock=lambda: now, synthetic=True, seed_history=hist)
        try:
            first, second = col.collect(), col.collect()
        finally:
            col.close()
        rows = [r for r in first["basis_log"] if r[1] == market["ticker"]]
        pts = [median(p[1:4]) for p in hist if o - 60 <= p[0] <= o]
        self.assertGreaterEqual(len(pts), RULES.basis_min_points)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], sc.strike)
        self.assertEqual(rows[0][4], len(pts))
        self.assertAlmostEqual(rows[0][3], sum(pts) / len(pts))
        self.assertEqual(sum(r[1] == market["ticker"] for r in second["basis_log"]), 1)


class CompressionTests(unittest.TestCase):
    def test_gzip_round_trip_and_original_removed(self):
        from kalshi_paper.storage import gzip_file, read_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshots-2030-01-14.jsonl"
            rows = [{"snapshot_id": f"s{i}", "x": i} for i in range(500)]
            write_jsonl(path, rows)
            out = gzip_file(path)
            self.assertFalse(path.exists())
            self.assertEqual(out.name, "snapshots-2030-01-14.jsonl.gz")
            self.assertEqual(read_jsonl(out), rows)

    def test_only_finished_days_are_compressed(self):
        from kalshi_paper.cli import _compress_finished_days
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for day in ("2030-01-13", "2030-01-14", "2030-01-15"):
                write_jsonl(d / f"snapshots-{day}.jsonl", [{"snapshot_id": day}])
            with contextlib.redirect_stdout(io.StringIO()):
                _compress_finished_days(d, "2030-01-15")
            self.assertEqual(sorted(f.name for f in d.iterdir()),
                             ["snapshots-2030-01-13.jsonl.gz", "snapshots-2030-01-14.jsonl.gz", "snapshots-2030-01-15.jsonl"])

    def test_replay_and_restart_read_compressed_days(self):
        from kalshi_paper.storage import gzip_file
        results = run_scenarios()
        earlier, ours = results[0][1], results[2][1]
        live = evaluate(json.loads(json.dumps(ours)), RULES, EvalState())
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_jsonl(d / "snapshots-2030-01-14.jsonl", [earlier, ours])
            gzip_file(d / "snapshots-2030-01-14.jsonl")
            write_jsonl(d / f"decisions-{RULES.version}.jsonl", [live])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli_main(["replay", "--data-dir", tmp]), 0, out.getvalue())
            self.assertIn("1 identical, 0 different", out.getvalue())
            self.assertEqual(_last_snapshot(d)["snapshot_id"], ours["snapshot_id"])


class ReplayTests(unittest.TestCase):
    def test_run_seeds_from_the_last_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_jsonl(d / "snapshots-2030-01-14.jsonl", [{"snapshot_id": "older day"}])
            write_jsonl(d / "snapshots-2030-01-15.jsonl", [{"snapshot_id": "a"}, {"snapshot_id": "b"}])
            with open(d / "snapshots-2030-01-15.jsonl", "a", encoding="utf-8") as f:
                f.write('{"snapshot_id": "cut off by a cra')
            self.assertEqual(_last_snapshot(d)["snapshot_id"], "b")
            self.assertIsNone(_last_snapshot(d / "nothing-here"))

    def test_decisions_reproduce_exactly_from_saved_snapshots(self):
        results = run_scenarios()
        state = EvalState()
        for sc, snap, logged in results:
            again = evaluate(json.loads(json.dumps(snap)), RULES, state)
            state.record(again)
            with self.subTest(sc.name):
                self.assertEqual(json.loads(json.dumps(again)), json.loads(json.dumps(logged)))

    def test_restart_rebuilds_dedupe_state_from_the_log(self):
        results = run_scenarios()
        logged = [json.loads(json.dumps(d)) for _, _, d in results[:1]]  # just the first ACT
        state = EvalState.from_decisions(logged)
        again = evaluate(results[2][1], RULES, state)  # same window, new snapshot
        self.assertEqual(again["reason"], "already_acted_this_window")

    def test_replay_command_ignores_snapshots_from_other_runs(self):
        # Runs under different rules versions share the daily snapshot files. A live run
        # starts from its own version's log, so an earlier run's ACT in the same window
        # must not give the replay one-per-window memory the live run never had.
        results = run_scenarios()
        earlier = results[0][1]  # trigger_buy_yes: ACT in window 0, logged under another version
        ours = results[2][1]     # same window, later snapshot, evaluated by a fresh run
        live = evaluate(json.loads(json.dumps(ours)), RULES, EvalState())
        self.assertEqual(live["decision"], "ACT")
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(Path(tmp) / "snapshots-2030-01-15.jsonl", [earlier, ours])
            write_jsonl(Path(tmp) / f"decisions-{RULES.version}.jsonl", [live])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(["replay", "--data-dir", tmp])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("1 identical, 0 different", out.getvalue())


if __name__ == "__main__":
    unittest.main()
