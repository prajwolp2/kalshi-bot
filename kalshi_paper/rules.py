"""Versioned paper-trading rules. Change a value -> bump `version`.

Decisions are logged per rules version (decisions-<version>.jsonl), each stamped
with the version and a hash of every parameter, so results from different rule
sets can never be mixed up.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Rules:
    version: str = "v0.4.2"

    # Which contract: "BTC price up in next 15 mins?" (YES if the 60-second BRTI
    # average before close >= the 60-second BRTI average before open).
    series_ticker: str = "KXBTC15M"
    window_seconds: int = 900
    settlement_avg_seconds: int = 60

    # Timing
    min_seconds_left: int = 90            # skip the final 90 s

    # Freshness / fail-closed limits
    max_kalshi_age_s: float = 5.0         # Kalshi data retrieved at most 5 s before evaluation
    max_spot_age_s: float = 3.0           # spot quotes no older than 3 s
    max_clock_skew_s: float = 3.0         # local clock vs exchange clocks
    max_candle_lag_s: float = 600.0       # 1-min candles (volatility only) may lag up to 10 min

    # Spot composite = median of Coinbase, Kraken, Bitstamp mids
    max_spot_disagreement: float = 0.0005  # skip if venues disagree by > 0.05%

    # Volatility: stdev of the last 60 one-minute log returns (Coinbase candles)
    vol_lookback_minutes: int = 60
    vol_multiplier: float = 1.05          # fair value uses 5% wider volatility (v0.2.0, see provenance)

    # Settlement-index gap (v0.2.0). Kalshi settles on CF Benchmarks' BRTI; the bot prices off
    # a median of three exchanges. Each window's strike is BRTI's average over the minute
    # before it opened, so strike minus our own average over that minute measures the gap.
    # Fair value adds the average gap from recent window opens to our price.
    basis_windows: int = 8                # average over up to the last 8 window opens (2 hours)
    basis_max_age_s: float = 7200.0       # ignore measurements older than 2 hours
    basis_min_points: int = 4             # our own 10-second spot points needed in that minute
    min_basis_windows: int = 2            # skip until two window opens have been measured
    max_basis_fraction: float = 0.0005    # a gap over 0.05% of the price means bad data: skip

    # Momentum pattern
    momentum_lookback_min: int = 3        # z-score of the ~3-minute move
    momentum_z: float = 1.0
    confirm_short_min: int = 1            # 1-minute move must agree in sign
    confirm_long_min: int = 5             # 5-minute move must agree in sign
    history_tolerance_s: float = 20.0     # lookback point must be within 20 s of target time

    # Extended-move filter (v0.3.0): don't buy in the direction BTC already moved more than this
    # over the last hour (log return over the 60-minute volatility window of Coinbase candles).
    # After a big hourly run the market already prices the continuation and our random-walk
    # model, which ignores the run, overstates the chance it continues. None turns it off.
    max_hour_move_with_trade: float | None = 0.003   # 0.30%

    # Market filters
    min_yes_mid: float = 0.10
    max_yes_mid: float = 0.90
    max_spread: float = 0.02

    # Edge, fees, size
    edge_threshold: float = 0.02          # dollars per contract, after fees
    taker_fee_rate: float = 0.07          # fee = rate x C x P x (1-P), rounded up to the cent
    paper_contracts: int = 10             # v0.4.0: the minimum size (v0.3.1: a fixed 30)
    # Bankroll-scaled size (see sizing.py): contracts = bankroll / $100 x contracts_per_100usd,
    # at least paper_contracts, at most max_contracts. None = always paper_contracts.
    contracts_per_100usd: float | None = 5.0   # v0.4.2: $450 -> 22, $1,000 -> 50, $20,000+ -> 1,000 (v0.4.1: 7.5)
    max_contracts: int = 1000             # live books showed >= 1,000 contracts at the best ask in 92% of looks
    max_acts_per_window: int = 1
    daily_paper_cap: int | None = None    # no daily cap (your choice)

    # Provenance: which data these parameters were tuned on. The scorer ignores
    # every decision at or before this UTC timestamp, so tuning data is never
    # reused as evidence.
    tuned_on_data_through: str | None = "2026-11-08T19:30:00Z"
    provenance: str = (
        "v0.1.0: defaults proposed before any live data was collected. Only prior look at "
        "data: a streak check of 6,000 settled outcomes (2026-09-01 to 2026-11-04) that ruled "
        "out window-to-window streak rules. No parameter was fit. "
        "v0.1.1: max_candle_lag_s 300 -> 600 after the first live run (2026-11-04 05:04-05:17 UTC) "
        "showed Coinbase's cached candles lagging 302-337 s on 11 of 143 ticks. An operational "
        "freshness fix, chosen without looking at prices, edges or outcomes. "
        "v0.2.0: after studying 664 settled windows of public 1-minute history (2026-10-28 16:15 "
        "to 2026-11-04 16:15 UTC) and the 6-hour live run. Strikes sat a measurable gap above our "
        "exchange prices (Coinbase about $8 on average over the week, $4 on the last day; the "
        "live 3-exchange median about $2.50, drifting from about $5 to $0 overnight), which is "
        "the size of the 2c edge threshold near the money, so fair value now adds the gap "
        "measured at recent window opens. Volatility x1.05 because log loss was lowest at "
        "1.05-1.10 in every sample (both periods and the live run). Fit by calibration "
        "(log loss), not by paper P&L: P&L rankings of rule variants flipped between the last "
        "day and the six days before, and no variant beat zero edge in either. Trend filter, "
        "edge threshold and everything else unchanged: momentum showed no follow-through beyond "
        "Kalshi's price, and larger predicted edges did not earn more. "
        "v0.3.0: after a backtest on all public history (30,561 windows, 2025-12-10 to "
        "2026-11-06; minute bars, Coinbase only). v0.2.0 made +$138 over 6,657 paper trades at "
        "10 contracts (+0.2c/contract), losing $152, $129 and $88 in April, August and October. "
        "Diagnosis: in every month the model's win chance on the side it bought was 3-8 points "
        "above how often it won, and Kalshi's price beat the model on log loss in 10 of 12 "
        "months. Losses concentrated in trades in the direction of a run of more than about "
        "0.3% over the past hour (-3.4c/contract in good months, -7.5c in bad ones), where the "
        "market already priced the continuation. Added max_hour_move_with_trade = 0.003. The "
        "threshold was chosen on all months; an expanding walk-forward that picks it each month "
        "only from earlier months gave +$781 vs +$138, and every threshold from 0.15% to 0.75% "
        "beat v0.2.0 on both Dec-Apr and May-Nov. Rejected, by the same walk-forward split: "
        "shrinking the model toward the market price (fewer trades, lost out of sample), a "
        "pause after recent losses (cut P&L in both halves), and a cap on model-market "
        "disagreement (no better on Dec-Apr). The idea for the move filter came from a "
        "diagnostic over all months, so it is not a clean out-of-sample result. "
        "v0.3.1: paper_contracts 10 -> 30 at the user's request (2026-11-07). Same signals; the fee "
        "is rounded per order, so per-contract fees at 30 are slightly lower and a few borderline "
        "edges now pass. Trades also need 30 contracts displayed at the ask. Not fit to any data. "
        "v0.4.0: order size grows with the paper bankroll, at the user's request (2026-11-08): 5 contracts "
        "per $100, at least 10, at most 1,000. Signals unchanged: the edge check still uses the fee at "
        "the base size, so the same trades are taken. Chosen on all public history (5,390 trades, "
        "2025-12-15 to 2026-11-08) by replaying runs from $300 started every 7 days (34 starts of "
        "90 days, 22 of 180), not by the single full-history run, which a lucky start dominated "
        "(it ranked 30-50 contracts per $100 best, but those went bust in 59-88% of 90-day starts). "
        "A fixed 30 contracts went bust in 23-24% of starts because $18 a trade is about 6% of $300, "
        "more than the full-Kelly bet for a 1.6c edge (about 4%). 5 per $100 with a floor of 10 never "
        "went bust in any start, ended 180 days at a median $1,029 (fixed 30: $1,201, but its worst "
        "10% ended near $19), and stayed above zero with fills 1c worse than shown; 6-7.5 per $100 had a "
        "slightly higher median but did worse with any slippage. max_contracts 1,000: live books showed "
        "at least that at the best ask in 92% of looks (31,432 looks, 2026-11-04 to 11-08). "
        "v0.4.1: 7.5 contracts per $100 instead of 5, at the user's request for more risk (2026-11-12). Same signals. "
        "Replaying 60-day runs of real backtest days from $400: median $856 at the shown price (5 per $100: $740), "
        "$597 at 0.5c worse ($585), but a 2% chance of reaching the $100 floor (0%) and a 68% chance of a 50% fall "
        "from a peak (23%). 10 per $100 and above did worse at 0.5c worse fills; 15 and above reached the floor in "
        "40-90% of runs. "
        "v0.4.2: back to 5 contracts per $100 (2026-11-22, the user's choice after a backtest of sizes and limits). "
        "Same signals. 35 runs of 90 days from $450 with the live bot's new limits (25% of equity per order, stop "
        "for the day at 30% of the day's start): median $922, worst 10% $362, 14% lost money, none reached the $100 "
        "floor; at 7.5 per $100 with $100/order and a $150 daily stop: median $792, worst 10% $214, 29% lost money, "
        "9% reached the floor. With fills 1c worse: $427 vs $258. 180-day runs: median $1,408 vs $882."
    )

    def to_dict(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


RULES = Rules()
