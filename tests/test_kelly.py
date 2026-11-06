"""Quarter-Kelly tracker on SYNTHETIC decisions."""

import unittest

from kalshi_paper.kelly import simulate
from kalshi_paper.rules import RULES
from kalshi_paper.timeutil import parse_iso_utc

ACCT = {"start_ts": 100.0, "start_usd": "100.00"}
CLOSE = "2030-01-15T15:15:00Z"


def dec(t, p=0.70, ask="0.6600", bid="0.6500", ticker="W1", close=CLOSE, size="500", decision="ACT", side="YES"):
    no_ask = f"{1 - float(bid):.4f}"
    return {"evaluated_at": t, "market_ticker": ticker, "close_time": close, "decision": decision,
            "intent": {"side": side} if decision == "ACT" else None,
            "features": {"p_fair": p, "yes_ask": ask, "no_ask": no_ask, "yes_mid": (float(ask) + float(bid)) / 2,
                         "spread": f"{float(ask) - float(bid):.4f}", "best_no_bid_size": size, "best_yes_bid_size": size}}


class KellyTests(unittest.TestCase):
    def test_sizes_by_risk(self):
        # all-in cost 0.66 + 1.6c fee = 0.676; edge 2.4c; Kelly 0.024/0.324 = 7.4%; quarter = 1.85% of $100
        k = simulate([dec(200.0)], {}, ACCT, RULES)
        tr = k["trades"][0]
        self.assertEqual((tr["side"], tr["contracts"]), ("YES", 2))
        self.assertEqual(tr["cost"], 1.36)          # 2 x (0.66 + 2c fee for a 2-contract order)
        self.assertEqual(tr["risk_pct"], 1.36)
        self.assertEqual((k["balance"], k["open_cost"]), (98.64, 1.36))

    def test_one_trade_per_window(self):
        k = simulate([dec(200.0), dec(205.0, p=0.9)], {}, ACCT, RULES)
        self.assertEqual(len(k["trades"]), 1)

    def test_follows_only_the_main_rules_trades_and_side(self):
        self.assertEqual(simulate([dec(200.0, decision="SKIP")], {}, ACCT, RULES)["trades"], [])
        # the model sees YES value, but the rules bought NO: the tracker won't switch sides
        self.assertEqual(simulate([dec(200.0, side="NO")], {}, ACCT, RULES)["trades"], [])
        from dataclasses import replace
        from kalshi_paper.kelly import PARAMS
        anything = replace(PARAMS, follow_rules=False)
        self.assertEqual(len(simulate([dec(200.0, decision="SKIP")], {}, ACCT, RULES, anything)["trades"]), 1)

    def test_never_more_than_max_contracts(self):
        rich = {"start_ts": 100.0, "start_usd": "1000000.00"}
        tr = simulate([dec(200.0, p=0.95)], {}, rich, RULES)["trades"][0]
        self.assertEqual(tr["contracts"], 200)

    def test_skips_small_edges_wide_spreads_and_extreme_prices(self):
        for d in (dec(200.0, p=0.68),                        # edge under 1c after fees
                  dec(200.0, ask="0.6800", bid="0.6500"),     # 3c spread
                  dec(200.0, p=0.99, ask="0.9500", bid="0.9400"),  # outside 10-90c
                  dec(50.0)):                                 # before the tracker started
            self.assertEqual(simulate([d], {}, ACCT, RULES)["trades"], [])

    def test_caps_risk_at_five_percent(self):
        tr = simulate([dec(200.0, p=0.95)], {}, ACCT, RULES)["trades"][0]
        self.assertLessEqual(tr["risk_pct"], 5.0)

    def test_results_count_only_after_the_window_settles(self):
        settled = {"W1": {"result": "yes"}}
        close_ts = parse_iso_utc(CLOSE)
        # 30 s after the close the result isn't counted yet: the second trade is sized on $98.64
        early = simulate([dec(200.0), dec(close_ts + 30, ticker="W2")], settled, ACCT, RULES)
        self.assertEqual(early["trades"][1]["risk_pct"], round(100 * early["trades"][1]["cost"] / 98.64, 2))
        # 90 s after, it is: sized on $100.64
        late = simulate([dec(200.0), dec(close_ts + 90, ticker="W2")], settled, ACCT, RULES)
        self.assertEqual(late["trades"][1]["risk_pct"], round(100 * late["trades"][1]["cost"] / 100.64, 2))
        k = simulate([dec(200.0)], settled, ACCT, RULES)
        self.assertEqual((k["trades"][0]["status"], k["trades"][0]["pnl"]), ("won", 0.64))
        self.assertEqual((k["balance"], k["settled_pnl"], k["wins"]), (100.64, 0.64, 1))


if __name__ == "__main__":
    unittest.main()
