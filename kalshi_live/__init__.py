"""Real orders on Kalshi (demo or live money) for the kalshi_paper rules.

Kept apart from kalshi_paper on purpose: kalshi_paper stays read-only (tests/test_safety.py
scans it), and this package uses its rules and evaluator unchanged, so every real order has
passed every paper rule at the moment it is sent. One thing differs: the paper bot acts on the
first look that triggers, and this package waits until the trigger has held for a few seconds
(`run --hold`, trader.Hold), so it trades fewer windows, and later in them. Setup: deploy/SERVER.md.

What this package can do on the exchange, and nothing else:
  - read the account's cash balance and its own orders
  - BUY YES or NO on a KXBTC15M market with an immediate-or-cancel limit order
  - (smoke test only) place a 1-contract order at 1 cent and cancel it
  - (`fund-shard`, only when you run it) move cash between exchange shards of your own account
There is no sell and no withdrawal code, and the trading loop never moves money.
"""
