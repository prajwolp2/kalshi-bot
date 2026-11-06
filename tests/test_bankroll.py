"""The paper bankroll: balance arithmetic and the stop. All decisions here are SYNTHETIC."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from kalshi_paper import bankroll
from kalshi_paper.cli import _bankroll_exhausted
from kalshi_paper.rules import RULES

ACCT = {"start_ts": 1000.0, "start_usd": "100.00"}


def act(ticker, side="YES", price="0.6600", fee="0.016", at=2000.0):
    return {"decision": "ACT", "market_ticker": ticker, "evaluated_at": at, "close_time": "2030-01-15T15:15:00Z",
            "features": {}, "intent": {"side": side, "limit_price": price, "fee_per_contract": fee, "contracts": 10}}


class BankrollTests(unittest.TestCase):
    def test_balance_counts_settled_pnl_and_open_cost(self):
        acts = [act("A"), act("B", side="NO"), act("C")]
        st = bankroll.status(acts, {"A": {"result": "yes"}, "B": {"result": "yes"}}, ACCT)
        # A won: +0.324 x 10; B lost: -0.676 x 10; C open: 0.676 x 10 tied up
        self.assertEqual(st["settled_pnl"], Decimal("-3.52"))
        self.assertEqual(st["open_cost"], Decimal("6.76"))
        self.assertEqual(st["balance"], Decimal("89.72"))
        self.assertEqual((st["trades"], st["settled"], st["wins"]), (3, 2, 1))

    def test_only_trades_since_the_account_started_count(self):
        decisions = [act("old", at=500.0), act("new"), {"decision": "SKIP"}]
        self.assertEqual([d["market_ticker"] for d in bankroll.counted(decisions, ACCT)], ["new"])

    def test_worst_case_trade_cost(self):
        # 10 contracts at 0.90 + 0.02 = 0.92, fee 0.07 x 10 x 0.92 x 0.08 = 0.0515 -> $0.06
        self.assertEqual(bankroll.max_trade_cost(RULES), Decimal("9.26"))

    def test_account_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bankroll-test.json"
            first = bankroll.start_or_load(path, 100, now=1000.0)
            again = bankroll.start_or_load(path, 500, now=9999.0)
            self.assertEqual(first, again)
            self.assertEqual(json.loads(path.read_text())["start_usd"], "100.00")

    def test_run_stops_before_the_balance_can_go_negative(self):
        losers = [act(f"L{i}", side="NO") for i in range(14)]  # 14 losses x $6.76 = $94.64
        bank = {"acct": ACCT, "acts": losers[:13], "settled": {f"L{i}": {"result": "yes"} for i in range(14)},
                "floor": bankroll.max_trade_cost(RULES), "checked": 0.0, "last": None, "data_dir": "unused"}
        quiet = lambda *a, **k: None  # noqa: E731
        import builtins
        real_print, builtins.print = builtins.print, quiet
        try:
            self.assertFalse(_bankroll_exhausted(bank, RULES, now=0.0))   # $12.12 left
            bank["acts"] = losers
            self.assertTrue(_bankroll_exhausted(bank, RULES, now=0.0))    # $5.36 left < $9.26
        finally:
            builtins.print = real_print


if __name__ == "__main__":
    unittest.main()
