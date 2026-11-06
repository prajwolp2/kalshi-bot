# Kalshi 15-minute BTC paper bot (research prototype)

**Paper only.** This reads public market data and logs what the current rules (v0.3.0) *would* buy (ACT)
or why they wouldn't (SKIP). It has no order code, no API key, no authenticated client and no live mode. An ACT is a
paper intent, not an order and not a fill.

Standard library only (no installs). Tested on Python 3.15.

## Commands

```bash
python3 -m kalshi_paper demo                 # SYNTHETIC scenarios through the real code, no network
python3 -m unittest -v                       # 69 tests, no network
python3 -m kalshi_paper run --interval 2     # live public data every 2 s until Ctrl-C
python3 -m kalshi_paper run --minutes 60     # ...or for a fixed time
python3 -m kalshi_paper run --bankroll 100   # ...or until a $100 PAPER bankroll can't cover another trade
python3 -m kalshi_paper run --bankroll 100 --kelly 100   # ...plus a quarter-Kelly PAPER tracker alongside
python3 -m kalshi_paper dashboard            # live dashboard at http://127.0.0.1:8765 (this computer only)
python3 -m kalshi_paper score                # fetch settlements, score paper intents + calibration
python3 -m kalshi_paper replay               # re-evaluate this version's snapshots; must match the log exactly
python3 -m kalshi_paper history              # cache the last 7 days of public 1-minute history (~3 min)
python3 -m kalshi_paper study                # check the rules on that history: last day vs the days before
python3 -m kalshi_paper backtest             # both strategies month by month on all cached history (~1 min)
python3 -m kalshi_paper slippage --contracts 30   # reprice every paper trade as a real market or limit order
```

Warm-up after a fresh start:
- **Settlement-index gap.** The first 15–30 minutes log `insufficient_basis_history`. The rules need two
  window opens measured against the strike (see Rules v0.3.0). A restart within two hours reuses the
  last run's measurements.
- **Momentum.** The first ~5 minutes also log `insufficient_history`, because the momentum rule needs 5
  minutes of the bot's own spot history. The same reason appears for about a minute after any data gap,
  such as an exchange timeout.

## Live results so far

**v0.1.0, first run (2026-11-04, 05:04–05:17 UTC, 143 snapshots):**
- 0 ACT, 143 SKIP. The main reasons were `insufficient_history` ×64 (warm-up, plus a gap after exchange
  timeouts), `no_trend` ×37, `edge_below_threshold` ×19 and `stale_data:coinbase_candles` ×11.
- Replay: all 143 decisions reproduced exactly.
- One window settled (NO). The shadow without the trend filter would have bought YES at 35¢ and lost
  $3.66 on paper. Brier score: model 0.186 vs market mid 0.180. One window proves nothing.

**v0.1.1, six-hour run (2026-11-04, 05:18–11:18 UTC = 12:18–6:18 AM ET, 4,259 snapshots, 24 settled
windows):**

| | Rules v0.1.1 (with trend filter) | Shadow (no trend filter) |
|---|---|---|
| Paper trades, 10 contracts each | 17 | 24 |
| Won | 9 (53%) | 12 (50%) |
| Average price paid (the win rate the market implied) | 61¢ | 44¢ |
| Edge the model predicted at entry, after fees | +4.2¢ per contract | +4.0¢ per contract |
| Paper P&L | −$15.93 (−9.4¢ per contract) | +$11.19 (+4.7¢ per contract) |
| Standard deviations from zero edge | −0.76 | +0.65 |

- **Neither result can be told apart from luck.** If every price paid had been exactly right, the expected
  P&L would be minus the fees ($2.33 and $3.91), with one standard deviation of about $18 and $23. Telling
  a 2¢-per-contract edge from zero takes roughly 2,000–2,500 windows: three to four weeks of
  round-the-clock running.
- **The model knew no more than the market.** Its probability beat Kalshi's mid price in 11 of 24 windows.
  Brier score: model 0.1681 vs market mid 0.1657 (lower is better). The per-window difference was
  +0.0027 ± 0.0036 (model minus market, ± 1 standard error).
- **Don't switch to the no-filter variant on this evidence.** Its lead is within chance, and choosing it
  now would turn this run into tuning data. The shadow is logged on every run, so running v0.1.1 longer
  collects fresh evidence on both.
- **Fills.** Every paper trade had at least 921 contracts displayed at its price, with a 1¢ spread, so 10
  contracts was small. But in 6 of 17 cases the price had moved against the trade by the next snapshot,
  about 5 s later. A real order that arrived late could have paid more or missed.
- **Skips.** `already_acted_this_window` ×1,532 (one trade per window), `no_trend` ×1,304,
  `too_close_to_settlement` ×410, `price_out_of_band` ×259, `edge_below_threshold` ×251. 11.4% of ticks
  were skipped for data or timing reasons: stale data ×174 (mostly Kraken ×97 and Bitstamp ×73), window
  turnover ×132 (the market list still shows the old market for up to ~45 s after each close), warm-up
  and post-gap history ×104, an empty book side ×48, API errors ×19, and exchanges
  disagreeing ×9. The longest gap between ticks was 36 s.
- **Replay.** All 4,259 decisions reproduced exactly. The first replay reported 2 mismatches because the
  replay tool also fed in the 143 snapshots from the v0.1.0 run, giving it one-trade-per-window memory the
  live run never had. Replay now re-evaluates only the snapshots its own rules version logged, and a test
  covers this.

To run it again, keep the Mac awake (plugged in, lid open) and start:

```bash
caffeinate -i python3 -m kalshi_paper run --minutes 360 >> data/live/run-$(date -u +%F).log 2>&1
```

Watch it, or stop it early:

```bash
tail -f data/live/run-$(date -u +%F).log
```
```bash
pkill -f "kalshi_paper run"
```

A new run under the same rules rebuilds its one-trade-per-window memory from the log, then warms up (see
above). `score` and `replay` cover every run under the current rules version.

**v0.2.0 has not run live yet.** Its rules were chosen on data up to 16:31 UTC on 2026-11-04, so `score`
judges it only on decisions after that.

## What the past week showed (the study behind v0.2.0)

`history` cached 664 settled windows, from 2026-10-28 16:30 to 2026-11-04 16:15 UTC. Each window came with
Kalshi's 1-minute YES bid and ask, and Coinbase's 1-minute prices. The study re-ran the rules at the end
of every minute, then compared the last day with the six days before it.

- **Results that looked good on the last day flipped on the six days before.** Under v0.1.1, the best
  variant on the last day was 10% wider volatility with the trend filter: +$17.26 over 25 paper trades.
  Over the six days before, the same variant lost $29.98 over 188. Picking rules by the last day's profit
  would have picked a loser. No variant beat zero edge by more than chance in either period.
- **The day's trend didn't carry into 15-minute windows.** BTC rose 3.8% over the last day, yet only 49%
  of its windows settled YES. Window-to-window streaks were noise again: after a YES window, the next
  was YES 46% of the time over the six days, against 48% after a NO.
- **Short-term trends didn't follow through beyond Kalshi's price.** After a confirmed 3-minute move,
  results matched what Kalshi's price already implied. Over the six days, the follow-through was −0.001
  across 1,570 looks. The last day showed +0.030, within the noise. The market already prices recent moves.
- **Bigger predicted edges didn't earn more.** Over the six days, trades with a predicted edge of 7¢ or
  more did worst: −3.1¢ per contract over 70 trades.
- **Strikes sit above our exchange prices.** Kalshi takes its strikes from CF Benchmarks' BRTI.
  - Coinbase traded $8.21 below the strike on average over the week: $3.93 on the last day and $8.93
    before it, with standard errors under $1.20.
  - The live bot's three-exchange median sat $2.54 below during the 6-hour run, drifting from about +$5 to
    $0 overnight.
  - Near the money, a $3–8 gap shifts the fair probability by 1–5 points, depending on time left. That's
    the size of the 2¢ edge threshold, so the gap was creating false edges.
  - Correcting it improved calibration over the six days (Brier 0.1683 → 0.1669). On the last day, when
    the gap was smaller, it made no difference (0.1744 → 0.1747).
- **Once the gap is corrected, the model and Kalshi agree.** In every probability bucket they are within
  about 2 points of each other. Kalshi already prices BTC's random walk, and the model isn't seeing
  anything the market misses.
- **The model was slightly overconfident.** Log loss was lowest with volatility 5–10% wider, in both
  periods and in the live run.

The study run under v0.2.0's own rules (`python3 -m kalshi_paper study`):

| | Last day (96 windows) | Six days before (566 windows) |
|---|---|---|
| Brier score, model / Kalshi mid (lower is better) | 0.1744 / 0.1765 | 0.1671 / 0.1660 |
| Model closer to the result than Kalshi's mid | 43 of 96 windows | 246 of 566 windows |
| Strategy, with trend filter | 33 paper trades, +$26.62 (+1.3 SD) | 179 paper trades, −$62.45 (−0.7 SD) |
| Shadow, no trend filter | 82 paper trades, +$14.33 (+0.7 SD) | 513 paper trades, −$118.91 (−0.4 SD) |

These numbers are in-sample, because v0.2.0 was chosen on this data. Minute bars also flatter the fills
(see the caveats `study` prints). The only fair test is live data from after the cutoff.

### Rule versions

| Version | Change | Why |
|---|---|---|
| v0.1.0 | Initial rules | Not fit to any data |
| v0.1.1 | `max_candle_lag_s` 300 → 600 | Coinbase's cached candles lagged 302–337 s on 11 of 143 ticks. This is a freshness fix, chosen without looking at prices, edges or outcomes. The scorer ignores anything at or before 05:17:23 UTC. |
| v0.2.0 | Fair value adds the measured gap between the strike and our price; volatility ×1.05 | The gap showed up in both periods and in the live run. Wider volatility had the better log loss in both periods and in the live run. Neither was chosen by paper P&L. The trend filter, edge threshold and everything else are unchanged, because nothing else held up. The scorer ignores anything at or before 16:31 UTC on 2026-11-04. |
| v0.3.0 | Skip a buy in the direction BTC already moved more than 0.30% over the last hour (`move_already_extended`). The quarter-Kelly tracker now sizes only the main rules' trades, at most 200 contracts. | See "The 11-month backtest" below. The scorer ignores anything at or before 16:15 UTC on 2026-11-06. |

## The 11-month backtest (the study behind v0.3.0)

`python3 -m kalshi_paper backtest` covers all 30,561 windows from 2025-12-10 to 2026-11-06. These are
minute bars, Coinbase only, with fills at the displayed price.

- **v0.2.0:** +$138 over 6,657 trades at 10 contracts, about +0.2¢ a contract. It lost $152 in April,
  $129 in August and $88 in October. Quarter Kelly (any 1¢ edge, no trend filter) took $100 to $2.33 by
  February.
- **What went wrong:**
  - In every month, the model's win chance on the side it bought was 3–8 points above how often that
    side actually won.
  - Kalshi's own price beat the model on log loss in 10 of 12 months, so most of the "edge" was the
    model being overconfident.
  - The worst losses came from buying in the direction of an hour-long run of 0.3% or more: −3.4¢ a
    contract in the good months and −7.5¢ in the bad ones. The market had already priced the
    continuation; the random-walk model ignores the run.
- **v0.3.0:** +$872 over 5,272 trades, about +1.7¢ a contract. April went from −$152 to −$61, August
  from −$129 to −$37, and October from −$88 to +$26. Quarter Kelly following those trades went from $100
  to about $7,200, which compounds an optimistic per-trade result and is the least reliable number here.
- **How honest that is:**
  - The 0.30% threshold was picked on all months. A walk-forward that picks it each month using only
    earlier months gave +$781, against +$138 for v0.2.0.
  - Every threshold from 0.15% to 0.75% beat v0.2.0 on Dec–Apr and on May–Nov.
  - The idea came from a diagnostic over all months, so this is still not a clean out-of-sample test.
  - With fills 1¢ worse than displayed, v0.3.0 would make +$344 (v0.2.0: −$528). At 2¢ worse it's
    −$183.
- **Rejected on the same walk-forward split:**
  - shrinking the model toward Kalshi's price (lost out of sample);
  - pausing after a run of recent losses (lost in both halves);
  - capping model–market disagreement (no better on Dec–Apr).
- **What would confirm it:** a live paper run after the cutoff.

## The market

Series `KXBTC15M`, "BTC price up in next 15 mins?". One window is open at a time.

- **Target price** (`floor_strike`): the average of CF Benchmarks' BRTI over the 60 seconds before the
  window opens.
- **YES wins** if the 60-second BRTI average before close is **at least** the target. A tie is YES.
- **Prices** are dollars per $1 contract, as strings (`"0.6500"` = 65¢). The tick is 0.1¢ below 10¢ and
  above 90¢, and 1¢ in between. Sizes are contracts and can be fractional.
- **The order book lists bids only.** YES ask = 1 − best NO bid; NO ask = 1 − best YES bid.
- **Fees** (`fee_type: quadratic`, multiplier 1): takers pay `0.07 × contracts × P × (1−P)`, rounded up.
  That's 1.75¢/contract at 50¢, about 1.5¢ at 30¢ or 70¢, and 0.63¢ at 90¢. No maker fee is listed.

## Rules v0.3.0 ([kalshi_paper/rules.py](kalshi_paper/rules.py))

Every 2 seconds (`--interval 2`):

1. **Fair value under a random walk.**
   `p_fair = Φ( ln((S + gap)/K) / (1.05·σ·√(seconds_left − 40)) )`
   - `S` is the median of the Coinbase, Kraken and Bitstamp mid prices.
   - `K` is the target price.
   - `gap` corrects for the bot not seeing BRTI itself. At each window open, the strike is BRTI's average
     over the previous minute. The bot compares it with its own average over that minute, and uses the
     mean of the last 8 such measurements (2 hours).
   - `σ` is per-second volatility from the last 60 one-minute Coinbase returns, widened 5%.
   - The `−40` accounts for settlement on a 60-second average.
2. **Momentum pattern.** `z` = the ~3-minute move ÷ (σ·√elapsed). A trend is UP when `z ≥ 1.0` and the
   1- and 5-minute moves are also up; DOWN is the mirror image. Momentum uses the bot's own 10-second
   spot history, because Coinbase's candles lag 3–6 minutes.
3. **Edge after fees.**
   - `edge_YES = p_fair − yes_ask − fee`
   - `edge_NO = (1 − p_fair) − no_ask − fee`
4. **Extended move (v0.3.0).** No buy in the direction BTC already moved more than 0.30% over the last
   hour (the same 60 Coinbase candles as σ).
5. **ACT:** paper BUY YES ×10 if the trend is UP and `edge_YES ≥ 2¢`. Paper BUY NO ×10 if the trend is
   DOWN and `edge_NO ≥ 2¢`. At most one per window, with no daily cap.

**SKIP (fail closed)** whenever any of these holds:
- An input failed to fetch or parse.
- It isn't exactly one open `KXBTC15M` market with the expected ticker, times, strike type and contract
  wording.
- Our clock is more than 3 s off Kalshi's.
- Kalshi data is more than 5 s old, or spot data is more than 3 s old.
- The target price or a book side is missing, or the book is crossed.
- Fewer than 90 s remain in the window.
- The three exchanges disagree by more than 0.05%.
- Fewer than two window opens in the last 2 hours were measured against the strike
  (`insufficient_basis_history`), or the measured gap is over 0.05% of the price (`basis_out_of_range`).
- The YES mid is outside 10–90¢, or the spread is wider than 2¢.
- There is no trend, the move is already extended (`move_already_extended`), the edge is too small, or fewer than 10 contracts are displayed at the ask.
- A paper trade was already logged in this window, or this snapshot was already seen.

Every decision also logs a **shadow** result: the same rules *without* the momentum filter. The scorer
compares the two, so you can see whether the pattern adds anything.

This is a hypothesis to test, not a known edge.

## Data sources, access and permitted use

| Source | Endpoint (all unauthenticated GET) | Used for | Timestamp saved |
|---|---|---|---|
| Kalshi | `/trade-api/v2/markets?series_ticker=KXBTC15M&status=open` | identity, target, times, contract text | HTTP `Date` + CDN `Age` (cached up to 15 s, so it is **not** used for prices) |
| Kalshi | `/trade-api/v2/markets/{ticker}/orderbook?depth=5` | quotes and displayed size (not cached) | HTTP `Date` (1-s resolution); quotes carry no timestamp of their own |
| Kalshi | `/trade-api/v2/markets/{ticker}` | settlement result (scorer only) | `settlement_ts` |
| Coinbase | `/products/BTC-USD/ticker` | spot bid/ask | last-trade `time` |
| Kraken | `/0/public/Ticker?pair=XBTUSD` | spot bid/ask | HTTP `Date` (the ticker has no timestamp) |
| Bitstamp | `/api/v2/ticker/btcusd/` | spot bid/ask | `timestamp` |
| Coinbase | `/products/BTC-USD/candles?granularity=60` | volatility; with `start`/`end`, history for `study` | candle start times (CDN-cached; lag 3–6 min) |
| Kalshi | `/trade-api/v2/markets?series_ticker=KXBTC15M&status=settled` | settled windows: strike and result (`history` only) | open and close times |
| Kalshi | `/trade-api/v2/series/KXBTC15M/markets/{ticker}/candlesticks?period_interval=1` | 1-minute YES bid/ask per window (`history` only) | candle end times |

Every live source also records our send and receive times, the HTTP status and any error. `history`
paces itself at about 5 requests per second and gives up on a request after 4 tries with doubling waits.

**Kalshi Developer Agreement v1.1:**
- You must be a Kalshi member (§4.1).
- API use is limited to facilitating your own trading on Kalshi (§3).
- Stored data may be used only for that purpose and may not be shared with third parties (§3.1). Keep
  `data/live/` and `data/history/` to yourself; `.gitignore` excludes both.
- Retries must back off and be bounded (§4.3). The bot backs off 5→60 s on errors and stops after 30
  failures in a row.

**What's unavailable:**
- **BRTI in real time.** CF Benchmarks licenses it, so the three exchanges stand in for it. Kalshi's
  BTC-vs-gold contract names the same three as its fallback source. Since v0.2.0, the gap to BRTI is
  measured at every window open and corrected for.
- **Kalshi's WebSocket feed** (timestamped and sequenced) needs an API key, so this tool polls REST.
- **Exchange terms.** I could not find explicit terms for Coinbase's, Kraken's or Bitstamp's public market
  data. Treat it as personal, non-redistributed use, and read their terms before doing anything more.

## Files

| File | Contents |
|---|---|
| `data/live/snapshots-YYYY-MM-DD.jsonl` | One line per tick. Holds all source records and the parsed market, book, spot quotes, candles, the spot history used, and the gap measured at recent window opens (`basis_log`). About 7 KB per tick, or ~120 MB/day at 5 s. |
| `data/live/decisions-<version>.jsonl` | ACT/SKIP, reason, detail, the paper intent, every computed feature, and the shadow result. One file per rules version, each row stamped with the version and hash. |
| `data/live/settlements.jsonl` | Kalshi's result for each scored window. |
| `data/live/score-<version>.json` | Latest scorer report for that rules version. |
| `data/live/run-YYYY-MM-DD.log` | Console output of a background run, one line per tick. |
| `data/history/settled-KXBTC15M.jsonl` | One line per settled window: strike, result and Kalshi's 1-minute YES bid/ask. |
| `data/history/coinbase-btcusd-1m.jsonl` | Coinbase 1-minute closing prices. |
| `data/history/study-<version>.json` | Latest `study` report for that rules version. |
| `data/demo/SYNTHETIC_*` | Demo output. Every record carries `"synthetic": true` and a SYNTHETIC label. |

## What each check establishes, and what it doesn't

| Check | Establishes | Does not establish |
|---|---|---|
| Contract validation (`tests/test_validate.py`, uses a real market captured on 2026-11-04) | The bot only evaluates a window whose series, ticker, open/close times, strike type, target and exact contract wording match the BRTI up/down contract | That Kalshi won't change what the contract means without changing the wording |
| 18 synthetic scenarios (`demo`) | Each gate fires for its case: trigger YES/NO, no trend, fair price, stale, missing target, missing book side, wrong series, changed wording, HTTP 503, timeout, garbage response, near settlement, duplicate snapshot, second trigger in a window, no gap measured yet, a gap that removes a false edge, an implausible gap | How often these happen in reality |
| 28 corrupted-input cases | Bad, missing or contradictory inputs produce SKIP with a specific reason, never ACT and never a crash | Every possible malformed input |
| Gap measurement (`tests/test_scenarios.py`, `tests/test_model.py`) | The collector measures each window's gap once, from its own spot points in the minute before open; the rules average only recent, well-sampled measurements | That BRTI's gap to the exchanges stays stable within 2 hours |
| History and study (`tests/test_history.py`, `tests/test_study.py`) | Paging, caching, bounded retries and the allow-list for `history`; `study` rebuilds minutes with the live formulas, and its paper trades obey the live gates | That minute bars reflect what a 5-second bot would see or get filled at |
| Replay | Decisions are a pure function of the saved snapshot plus rules version, so any decision can be audited. The six-hour run reproduced 4,259 of 4,259 | That the inputs themselves were right |
| Safety scan (`tests/test_safety.py`) | No code references Kalshi's authenticated endpoints or signing headers. The only request builder is GET to four allow-listed HTTPS hosts | It's a static scan, not a proof |
| Live runs | Collection, validation and logging work end to end on real data, including six hours unattended with bounded retries | Anything about profitability. 24 windows is far too few |

**Still open:**
- **Does `p_fair` forecast better than Kalshi's own mid price?** Not so far. The live model was closer in
  11 of 24 windows, and over a week of minute data it was even with the mid or slightly worse.
- **Does the momentum filter add anything?** Not so far. Over a week of minute data, trends showed no
  follow-through beyond Kalshi's price.
- **Does the gap correction hold up over weeks?** The gap drifted from about $5 to $0 in one night.
- **Could a displayed ask actually be hit?**
- Behavior across the next DST change. EDT wording is only tested synthetically.

## Fees, spread, liquidity and fills (read before any historical simulation)

- **Fees** are largest at 50¢. A 2¢ edge threshold means `p_fair` must beat the ask by about 3.5–4¢
  before fees at mid-range prices.
- **Spread** was 1¢ when sampled. Buying at the ask gives up half the spread immediately.
- **Displayed size** was hundreds to thousands of contracts at the top level. 10 is small, but a 5-second
  snapshot says nothing about queue dynamics or how fast the level gets taken.
- **Fills.** The rule fires exactly when prices are moving and the quote looks stale. That is when quotes
  are most likely to have moved before an order arrives (adverse selection). In the six-hour run, 6 of 17
  paper prices had moved against the trade within ~5 s. Treat paper P&L as an upper bound.
- **Historical data** is limited to Kalshi's 1-minute candlesticks (YES bid/ask OHLC) plus settled
  outcomes, and Coinbase's 1-minute candles. A bar-level simulation can't see intra-minute quotes and
  would overstate fills.
  - `study`'s paper trades fill in full at each minute's closing ask. That's optimistic, so use them only
    to compare rule versions and periods.
  - A real execution simulation needs agreed assumptions first: fill at the ask plus N¢ of slippage, no
    fill if the next bar moved away, and a size cap.

## Versioning and keeping tuning data separate

- Rules are a frozen dataclass. Change any value and you must bump `version`. Decisions are stamped with
  the version and a parameter hash and written to a per-version file.
- `replay` re-evaluates the current version's own snapshots and checks them against its log. Code
  changes with each version, so an older version's decisions can't be replayed by newer code. v0.1.1's
  run was replay-checked (4,259 of 4,259) before v0.2.0 replaced it.
- When you tune a version on data through time D, set `tuned_on_data_through = D`. The scorer then
  ignores every decision at or before D, so the version is only judged on data collected after D.
- v0.1.0 was not fit to any data. Its only prior look at data was the streak check that ruled out
  window-to-window patterns.
- v0.2.0 was chosen on a week of public history plus the 6-hour live run, through 16:31 UTC on
  2026-11-04.
  - It changed only what showed up in both halves of the week: the price gap and the model's slight
    overconfidence.
  - It did not change anything chosen by paper P&L, because P&L rankings flipped between periods.
  - `study` checks any future version the same way: on recent days against the days before them.

## Possible future stages (not built; your call after reviewing)

1. **Longer paper run of v0.2.0.** Three to four weeks round the clock (2,000–2,500 windows) to tell a 2¢
   edge from zero. That needs a machine that doesn't sleep (a paper-only version of stage 7) and about
   4 GB of disk per month.
2. **Historical calibration study.** Built as `history` + `study`. Kalshi kept 1-minute candles for at
   least 14 days when checked, so `history --days 14` doubles the sample.
3. **Historical execution simulation.** Only after agreeing the fill and slippage assumptions above.
4. **v0.3 rules.** Only if live data or a longer study shows a pattern that holds in more than one period.
   The analysis behind v0.2.0 found none that makes money.
5. **Kalshi's demo environment.** An authenticated client against demo funds, using demo keys you create.
6. **Live trading.** Your own API key, small size, hard exposure limits and a kill switch, run by you.
7. **Deployment.** An always-on host with monitoring.
