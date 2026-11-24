# Kalshi 15-minute BTC trading bot

I built this bot to trade Kalshi's 15-minute Bitcoin contracts (`KXBTC15M`, "BTC price up in next 15
mins?"). It prices each contract with a random-walk model and buys only when short-term momentum agrees
and the contract is at least 2¢ cheaper than my fair value after fees. It has run 24/7 on a Linux server
with real money since November 8, 2026.

I built it with Claude Code as my AI pair programmer. I set the goals and the risk limits, decided which
experiments to run, read the results, chose every setting, and deploy and operate it myself.

There are two packages:

- **`kalshi_paper`**: data collection, the pricing model and rules, backtests, and a paper trader. It
  can't place orders: it has no order code, no API key and no signed requests, and
  `tests/test_safety.py` checks that.
- **`kalshi_live`**: runs the same rules unchanged and turns each signal into a real
  immediate-or-cancel buy behind hard limits and a HALT switch. My server setup is in
  [deploy/SERVER.md](deploy/SERVER.md).

It uses only the Python standard library, except that `kalshi_live` needs `cryptography` to sign
requests. There are 147 unit tests (`python3 -m unittest`), and none of them touch the network.

## Live results so far

As of November 23, 2026, under rules v0.4.2:

| | |
|---|---|
| Settled trades | 349 (214 won, 61%) |
| Net P&L after fees | +$144.85 |
| Fees paid | about $110 |
| Profit per contract | about +1.6¢ |
| Orders filled | 97% |
| Average fill vs the shown price | 0.2¢ better |

I don't count the edge as proven yet. When I resample the 16 trading days at random (a bootstrap
clustered by day, because trades on the same day move together), the 95% range is about −1.2¢ to +4.6¢
per contract. That range still includes zero. It came out positive in about 86% of resamples, and both
halves of the period earned about +1.5¢. I want 500–800 trades before calling it real.

## The market

- One 15-minute window is open at a time, 96 a day. A contract pays $1 if it wins.
- **Target price:** the average of CF Benchmarks' BRTI index over the 60 seconds before the window
  opens.
- **YES wins** if the BRTI average over the last 60 seconds before close is at or above the target.
- **Prices are probabilities**: a YES at 62¢ means the market sees about a 62% chance of up.
- **The book lists bids only:** YES ask = 1 − best NO bid, and NO ask = 1 − best YES bid.
- **Taker fee:** 7% × contracts × P × (1 − P), rounded up to the cent per order. That's 1.75¢ a contract
  at 50¢.
- **To break even, a side has to win as often as its all-in price.** Buying at 61¢ plus a 1.7¢ fee
  needs a 62.7% win rate.

## How I price a contract ([kalshi_paper/model.py](kalshi_paper/model.py))

I treat BTC as a driftless random walk over the window and compute the chance it finishes at or above the
target:

```
p_fair = Φ( ln((S + gap) / K) / (σ · √(T − 40)) )
```

- **S** is the median of the Coinbase, Kraken and Bitstamp mid prices. I skip if the three disagree by
  more than 0.05%.
- **K** is the target price, and **T** is the number of seconds left.
- **σ** is per-second volatility: the standard deviation of the last 60 one-minute Coinbase log returns,
  divided by √60 and widened by 5%. I chose the 1.05 because it gave the lowest log loss in every sample.
- **gap** corrects for the fact that I can't see BRTI itself. At each window open the target is BRTI's
  average over the previous minute, so the target minus my own average over that minute measures the
  gap. I average the last 8 of these (2 hours).
- **T − 40** accounts for settlement on a 60-second average. The variance of that average, seen from now,
  is σ²(T − ⅔·60).

**Edge** = p_fair − ask − fee for YES, and (1 − p_fair) − ask − fee for NO.

## When I buy ([kalshi_paper/evaluator.py](kalshi_paper/evaluator.py), [rules.py](kalshi_paper/rules.py))

The bot looks every 2 seconds and buys only when all three of these hold:

1. **Momentum.** z = (the ~3-minute log move) ÷ (σ√elapsed). The trend is UP when z ≥ 1 and the
   1- and 5-minute moves are also up; DOWN is the mirror image. UP buys YES and DOWN buys NO.
2. **No extended run.** I skip a buy in the direction BTC already moved more than 0.30% over the last
   hour. My backtest showed those trades lost 3.4–7.5¢ a contract, because the market had already priced
   the continuation.
3. **Edge of at least 2¢ a contract after fees.**

The checks run in a fixed order and the first failure is the logged reason, so every skip can be
explained. The bot fails closed: any missing or odd input means no trade. It skips when:

- a data source failed to fetch or parse;
- there isn't exactly one open `KXBTC15M` market with the expected ticker, times and contract wording;
- my clock is more than 3 s off Kalshi's;
- Kalshi data is over 5 s old, or spot data over 3 s;
- the target or a side of the book is missing, or the book is crossed;
- less than 90 s is left in the window;
- the exchanges disagree, or the gap measurement is missing or out of range;
- the YES mid is outside 10–90¢, or the spread is over 2¢;
- there's no trend, the move is already extended, the edge is too small, or there's too little size at
  the ask;
- I already traded this window.

Every decision also logs a **shadow** result without the momentum filter, so I can measure what the
filter is worth. `evaluate()` is a pure function of the snapshot, the rules and the state, so any
decision can be replayed exactly.

## Live execution ([kalshi_live/trader.py](kalshi_live/trader.py))

- **4-second trigger hold.** In my paper logs about half the triggers were gone by the next look 2 s
  later. Now a signal must repeat, same market and same side, on every look for 4 seconds before it
  becomes an order. The order is sized and priced from the final look.
- **Immediate-or-cancel orders.** Each order is one limit buy at the shown ask plus up to 1¢, sent only
  if the trade still has edge at that price. Anything that doesn't fill at once is cancelled, so nothing
  rests on the book. The bot never sells; it holds every contract to settlement.
- **Read-back.** After each order I fetch it back from Kalshi. If the market, side, price or cost
  differs from what I sent, the bot writes a HALT file and stops.
- **The NO-side quirk.** Kalshi's Create Order V2 records buying NO as selling YES at 1 − price. A smoke
  test places 1¢ orders on both sides, checks what Kalshi recorded, and cancels them before real trading
  is allowed.
- **Signing.** Each request is signed with RSA-PSS/SHA-256 (or Ed25519) over timestamp + method + path.
  The bot refuses a key file other users can read. The client has no sell, withdraw or transfer-out
  method.
- **Restarts.** Every order carries a client order ID, and the bot rebuilds its state from append-only
  logs, so a restart can't double an order.

**Sizing and limits.** These are checked before every order, and the first that fails blocks it:

| Control | Setting |
|---|---|
| Size | 5 contracts per $100 of cash, at least 10 and at most 1,000 (about 3% of the account per trade) |
| Order cap | 25% of equity per order, fees included |
| Daily stop | stop for the UTC day once this bot's own trades lose 30% of the day's starting equity |
| Floor | HALT below $100 of equity; only a manual resume restarts it |
| Orders per day | 60 at most |
| One per window | also across restarts |

I share the Kalshi account with another bot of mine (gold contracts). The daily stop counts only this
bot's own trades, and equity includes every open position, so one bot's open trades don't look like a
loss to the other.

## How I tested and chose the rules

- **Data.** My backtests use all public history: 30,561 settled windows from December 10, 2025 to
  November 6, 2026, with Coinbase 1-minute bars. They look once a minute and assume fills at the shown
  price.
- **I calibrate the model by log loss, not P&L.** P&L rankings of rule variants flipped between
  periods, so P&L is too noisy to tune on.
- **Walk-forward testing.** For the 0.30% extended-move filter, I let the threshold be picked each month
  using only earlier months. That gave +$781, against +$138 for the rules before it. Every threshold from
  0.15% to 0.75% beat the old rules in both halves of the year. The idea itself came from looking at all
  months, so this isn't a perfectly clean out-of-sample result.
- **Ideas I rejected the same way:**
  - shrinking the model toward Kalshi's price;
  - pausing after losses;
  - capping how far the model and the market can disagree.
- **I chose the bet size from many runs, not one.** I replayed dozens of 90- and 180-day runs, started
  every 7 days. From $450 over 90 days:

  | Setting | Median end | Worst 10% of runs | Runs that lost money | Runs that hit the $100 floor |
  |---|---|---|---|---|
  | 5 per $100 with percentage limits (current) | $922 | $362 | 14% | 0% |
  | 7.5 per $100, $100 per order, $150 daily stop (before) | $792 | $214 | 29% | 9% |

- **Slippage is the biggest risk.** One cent worse on every fill takes a year's backtest from a profit to
  about break-even. That's why I track live fills against the shown price.
- **Small live samples can mislead.** In live trading, 85 trades under 35¢ lost $74, so I built a 35¢
  minimum. The full-year backtest disagreed: cheap trades made +2.75¢ a contract, and the 90-day median
  fell from $922 to $683 with the minimum. The 95% range on the live number was −15¢ to +9¢, so I
  reverted it the same day.
- **I keep tuning data separate.** Every rules version records the date its parameters were tuned
  through, and scoring ignores decisions before that date.

| Version | Date | Change |
|---|---|---|
| v0.4.2 | Nov 22, 2026 | Back to 5 contracts per $100; live limits as a share of equity |
| v0.4.1 | Nov 12, 2026 | 7.5 contracts per $100 |
| live | Nov 9, 2026 | 4-second trigger hold |
| v0.4.0 | Nov 8, 2026 | Size grows with the bankroll; real-money trading starts |
| v0.3.1 | Nov 7, 2026 | Fixed 30 contracts |
| v0.3.0 | Nov 6, 2026 | Extended-move filter, after the full-history backtest |
| v0.2.0 | Nov 4, 2026 | Settlement-gap correction and 1.05× volatility, fit by log loss |
| v0.1.0 | Nov 4, 2026 | First rules, set before I looked at any live data |

Each version's full reasoning is in the `provenance` field of [kalshi_paper/rules.py](kalshi_paper/rules.py).

## Commands

Paper side, on my Mac:

```bash
python3 -m unittest -v                     # 147 tests, no network
python3 -m kalshi_paper demo               # SYNTHETIC scenarios through the real code
python3 -m kalshi_paper run --interval 2   # paper-trade on live public data until Ctrl-C
python3 -m kalshi_paper dashboard          # paper dashboard at http://127.0.0.1:8765
python3 -m kalshi_paper score              # score paper decisions against settlements
python3 -m kalshi_paper replay             # re-evaluate saved snapshots; must match the log exactly
python3 -m kalshi_paper history            # cache public 1-minute history
python3 -m kalshi_paper study              # recent days vs the days before
python3 -m kalshi_paper backtest           # month by month over all cached history
python3 -m kalshi_paper slippage           # reprice paper trades as real orders
python3 -m kalshi_paper missed             # trades missed while offline
```

Live side, on the server (`deploy/kb` wraps these with the server's settings):

```bash
deploy/kb status        # cash, recent orders, HALT state
deploy/kb smoke live    # 1¢ order check of the side and price mapping
deploy/kb halt          # stop placing orders now
deploy/kb resume        # allow orders again
deploy/kb logs          # follow the service log
```

The live dashboard runs on the server as the `kalshi-dash` service and listens only on the server
itself. I view it through an SSH tunnel at http://localhost:8766. It shows P&L by day, records, what
made and lost money, and a what-if calculator that replays my real trades from any starting amount.

## Code layout

| File | What it does |
|---|---|
| `kalshi_paper/rules.py` | Every parameter, versioned and hashed, with the reason for each change |
| `kalshi_paper/model.py` | Fees, volatility, fair value, the settlement gap, momentum |
| `kalshi_paper/evaluator.py` | Snapshot + rules → ACT or SKIP, with the reason |
| `kalshi_paper/collector.py` | Read-only collection of one snapshot from Kalshi and three exchanges |
| `kalshi_paper/sizing.py` | Contracts per trade and compounding replays |
| `kalshi_paper/history.py`, `study.py` | Historical data and backtests |
| `kalshi_live/trader.py` | Trigger hold, limits, order placement, read-back, HALT |
| `kalshi_live/api.py`, `signing.py` | Signed Kalshi API client (buys only) |
| `kalshi_live/smoke.py` | The 1¢ order check |
| `kalshi_live/dashboard.py` | Live account dashboard |
| `deploy/` | systemd services, setup script, server guide |

## Data and keys

- My API key lives only on the server, in the git-ignored `secrets/` folder, with `chmod 600`.
- Kalshi's Developer Agreement (§3.1) limits API data to my own trading use, so `data/live/`,
  `data/history/` and the trading logs are git-ignored. Only `data/demo/SYNTHETIC_*` is in the repo.
- The bot polls public REST endpoints with bounded, backed-off retries. Real-time BRTI is licensed, so
  the three exchanges stand in for it, and the gap correction accounts for the difference.

## What's still open

- **Is the edge real?** That needs a few hundred more live trades.
- **Where does it come from?** My working idea is that Kalshi's quotes lag fast BTC moves, but I haven't
  measured that directly.
- **The model's limits.** It assumes constant volatility and no drift, and it ignores fat tails. In the
  backtest, Kalshi's own price beat it on log loss in 10 of 12 months, so the bot only trades the narrow
  situations where the backtest found an advantage.
- **What I'd try next:** better volatility estimates, measuring how fast Kalshi's quotes catch up to
  spot, and resting (maker) orders to avoid the taker fee.
