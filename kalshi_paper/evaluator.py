"""Paper evaluator: snapshot + rules + state -> ACT or SKIP, with the reason.

ACT means "these rules would have bought here". It is a PAPER INTENT: nothing is
sent anywhere and it is not a fill. The quoted ask is only what was displayed at
retrieval time.

Gates run in a fixed order and the first failure is the logged reason:
  1. duplicate snapshot            5. freshness                 9. consistency (spot agreement,
  2. any source failed (API)       6. timing (window, last 90s)    crossed book, volatility,
  3. market identity / contract    7. missing target / book        settlement-index gap)
  4. clock skew                    8. (features computed)      10. strategy gates
                                                                   (dedupe, band, spread,
                                                                    history, trend, extended
                                                                   move, edge, depth)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from statistics import median

from . import SYNTHETIC_LABEL
from .collector import VENUES
from .model import fair_probability, momentum, settlement_basis, sigma_per_second, taker_fee_per_contract
from .timeutil import iso_utc, parse_iso_utc, utc_date
from .validate import check_market

PAPER_NOTE = (
    "Paper intent only: no order was created or sent, and this is not a fill. The price is the "
    "top-of-book ask displayed at retrieval time; it may not be executable at this size, or at "
    "all, by the time a real order could arrive."
)

REQUIRED_SOURCES = ("kalshi_market_list", "coinbase_ticker", "kraken_ticker", "bitstamp_ticker",
                    "coinbase_candles")


class _Skip(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class EvalState:
    """Everything the evaluator remembers between snapshots. Rebuilt from the log on restart."""
    seen_snapshot_ids: set = field(default_factory=set)
    acts_by_window: dict = field(default_factory=dict)
    shadow_by_window: dict = field(default_factory=dict)
    acts_by_day: dict = field(default_factory=dict)

    def record(self, d: dict) -> None:
        if d.get("snapshot_id"):
            self.seen_snapshot_ids.add(d["snapshot_id"])
        ticker = d.get("market_ticker")
        if d.get("decision") == "ACT":
            self.acts_by_window[ticker] = self.acts_by_window.get(ticker, 0) + 1
            day = utc_date(d["evaluated_at"])
            self.acts_by_day[day] = self.acts_by_day.get(day, 0) + 1
        if (d.get("shadow_no_trend_filter") or {}).get("would_act"):
            self.shadow_by_window[ticker] = self.shadow_by_window.get(ticker, 0) + 1

    @classmethod
    def from_decisions(cls, decisions) -> "EvalState":
        state = cls()
        for d in decisions:
            state.record(d)
        return state


def evaluate(snap: dict, rules, state: EvalState) -> dict:
    """Pure function of (snapshot, rules, state). Call state.record(result) afterwards.

    Never raises: any unexpected problem becomes SKIP internal_error:<type> (fail closed),
    which the tests treat as a bug.
    """
    try:
        now = float(snap["collected_at"])
    except (KeyError, TypeError, ValueError):
        now = None
    market = snap.get("market") if isinstance(snap.get("market"), dict) else {}
    dec = {
        "schema": "decision/1",
        "rules_version": rules.version,
        "rules_hash": rules.hash(),
        "snapshot_id": snap.get("snapshot_id"),
        "synthetic": bool(snap.get("synthetic")),
        "evaluated_at": now,
        "evaluated_at_iso": iso_utc(now) if now is not None else None,
        "market_ticker": market.get("ticker"),
        "close_time": market.get("close_time"),
        "decision": "SKIP",
        "reason": None,
        "detail": None,
        "intent": None,
        "features": None,
        "shadow_no_trend_filter": None,
    }
    if snap.get("synthetic"):
        dec["label"] = SYNTHETIC_LABEL
    ctx = {"features": None, "shadow": None}
    try:
        if now is None:
            raise _Skip("missing_data:collected_at")
        dec["intent"] = _gates(snap, rules, state, now, ctx)
        dec["decision"] = "ACT"
        dec["reason"] = "all_conditions_met"
    except _Skip as s:
        dec["reason"], dec["detail"] = s.reason, s.detail
    except Exception as e:  # fail closed on anything unexpected, but make it visible
        dec["intent"] = None
        dec["reason"], dec["detail"] = f"internal_error:{type(e).__name__}", str(e)[:300]
    dec["features"] = ctx["features"]
    dec["shadow_no_trend_filter"] = ctx["shadow"]
    return dec


def _gates(snap, rules, state, now, ctx):
    # 1. Repeated event
    sid = snap.get("snapshot_id")
    if not sid:
        raise _Skip("missing_data:snapshot_id")
    if sid in state.seen_snapshot_ids:
        raise _Skip("duplicate_snapshot", "this snapshot was already evaluated")

    # 2. Every input must have been fetched and parsed
    src = snap.get("sources") or {}
    for name in REQUIRED_SOURCES:
        rec = src.get(name)
        if not rec or not rec.get("ok"):
            raise _Skip(f"api_error:{name}", (rec or {}).get("error", "source record missing"))

    # 3. Exactly one open market, and it is the contract we mean
    n_open = snap.get("n_open_markets")
    if n_open == 0:
        raise _Skip("no_open_market")
    if n_open != 1:
        raise _Skip("ambiguous_market", f"open markets: {snap.get('open_tickers')}")
    market = snap.get("market")
    bad = check_market(market, rules.series_ticker, rules.window_seconds)
    if bad:
        raise _Skip(bad)
    ob = src.get("kalshi_orderbook") or {}
    if not ob.get("ok"):
        raise _Skip("api_error:kalshi_orderbook", ob.get("error"))

    # 4. Our clock vs Kalshi's (Date header has 1 s resolution, so add 0.5 s)
    if ob.get("http_date") is None:
        raise _Skip("missing_data:kalshi_date_header")
    skew = ob["http_date"] + 0.5 - ob["received_at"]
    if abs(skew) > rules.max_clock_skew_s:
        raise _Skip("clock_skew", f"Kalshi clock minus ours = {skew:+.2f}s")

    # 5. Freshness
    for name in ("kalshi_market_list", "kalshi_orderbook"):
        age = now - src[name]["received_at"]
        if age > rules.max_kalshi_age_s:
            raise _Skip(f"stale_data:{name}", f"retrieved {age:.1f}s before evaluation")
    spot = snap.get("spot") or {}
    for venue in VENUES:
        age = now - src[f"{venue}_ticker"]["received_at"]
        if age > rules.max_spot_age_s:
            raise _Skip(f"stale_data:{venue}", f"retrieved {age:.1f}s before evaluation")
        s = spot.get(venue)
        if not s:
            raise _Skip(f"missing_data:{venue}")
        src_age = now - s["source_ts"]
        if src_age > rules.max_spot_age_s or src_age < -rules.max_clock_skew_s:
            raise _Skip(f"stale_data:{venue}", f"source timestamp is {src_age:+.1f}s old")
    candles = (snap.get("candles") or {}).get("items") or []
    need = rules.vol_lookback_minutes + 1
    if len(candles) < need:
        raise _Skip("missing_data:candles", f"{len(candles)} completed candles, need {need}")
    window = candles[-need:]
    if any(b[0] - a[0] != 60 for a, b in zip(window, window[1:])):
        raise _Skip("missing_data:candle_gap")
    lag = now - (window[-1][0] + 60)
    if lag > rules.max_candle_lag_s:
        raise _Skip("stale_data:coinbase_candles", f"last candle ended {lag:.0f}s ago")

    # 6. Timing
    open_ts, close_ts = parse_iso_utc(market["open_time"]), parse_iso_utc(market["close_time"])
    if not (open_ts <= now < close_ts):
        raise _Skip("outside_window")
    seconds_left = close_ts - now
    if seconds_left < rules.min_seconds_left:
        raise _Skip("too_close_to_settlement", f"{seconds_left:.0f}s left")

    # 7. Target price and order book present
    strike = market.get("floor_strike")
    if isinstance(strike, bool) or not isinstance(strike, (int, float)) or not strike > 0:
        raise _Skip("missing_data:target_price")
    book = snap.get("book") or {}
    yes_bids, no_bids = book.get("yes_bids") or [], book.get("no_bids") or []
    if not yes_bids or not no_bids:
        raise _Skip("missing_data:orderbook_side_empty")
    try:
        best_yes_bid, yes_bid_size = max(((Decimal(p), Decimal(q)) for p, q in yes_bids), key=lambda x: x[0])
        best_no_bid, no_bid_size = max(((Decimal(p), Decimal(q)) for p, q in no_bids), key=lambda x: x[0])
    except (InvalidOperation, ValueError, TypeError):
        raise _Skip("missing_data:orderbook_unparseable")

    # 9. Consistency
    mids = {v: (spot[v]["bid"] + spot[v]["ask"]) / 2 for v in VENUES}
    S = median(mids.values())
    disagreement = (max(mids.values()) - min(mids.values())) / S
    if disagreement > rules.max_spot_disagreement:
        raise _Skip("spot_disagreement", f"venues differ by {disagreement * 100:.3f}%")
    if not (Decimal(0) < best_yes_bid < 1 and Decimal(0) < best_no_bid < 1):
        raise _Skip("missing_data:orderbook_price_range")
    if best_yes_bid + best_no_bid >= 1:
        raise _Skip("crossed_book", f"yes bid {best_yes_bid} + no bid {best_no_bid} >= 1")
    sigma_s = sigma_per_second([c[1] for c in window])
    if not (sigma_s > 0 and math.isfinite(sigma_s)):
        raise _Skip("invalid_volatility")
    gap = settlement_basis(snap.get("basis_log"), now, rules)
    if gap is None:
        raise _Skip("insufficient_basis_history",
                    f"warming up: needs {rules.min_basis_windows} recent window opens measured against the strike")
    basis, basis_windows = gap
    if abs(basis) > rules.max_basis_fraction * S:
        raise _Skip("basis_out_of_range", f"strike minus our price at recent opens = ${basis:+.2f}")

    # 8. Features
    yes_ask, no_ask = Decimal(1) - best_no_bid, Decimal(1) - best_yes_bid
    p_fair, z, tau = fair_probability(S + basis, float(strike), sigma_s * rules.vol_multiplier,
                                      seconds_left, rules.settlement_avg_seconds)
    C = rules.paper_contracts
    rate = Decimal(str(rules.taker_fee_rate))
    fee_yes, fee_no = taker_fee_per_contract(yes_ask, C, rate), taker_fee_per_contract(no_ask, C, rate)
    edge_yes = p_fair - float(yes_ask) - float(fee_yes)
    edge_no = (1.0 - p_fair) - float(no_ask) - float(fee_no)
    yes_mid = float((best_yes_bid + yes_ask) / 2)
    spread = yes_ask - best_yes_bid
    mom = momentum(snap.get("spot_history") or [], now, S, sigma_s, rules)
    hour_move = math.log(window[-1][1] / window[0][1])  # same candles as the volatility
    ctx["features"] = {
        "target_price": strike,
        "seconds_left": seconds_left,
        "spot_mids": mids,
        "spot_composite": S,
        "spot_disagreement": disagreement,
        "basis_usd": basis,
        "basis_windows": basis_windows,
        "spot_adjusted": S + basis,
        "sigma_per_s": sigma_s,
        "sigma_1m": sigma_s * math.sqrt(60.0),
        "vol_multiplier": rules.vol_multiplier,
        "tau_eff_s": tau,
        "z_fair": z,
        "p_fair": p_fair,
        "best_yes_bid": str(best_yes_bid), "best_yes_bid_size": str(yes_bid_size),
        "best_no_bid": str(best_no_bid), "best_no_bid_size": str(no_bid_size),
        "yes_ask": str(yes_ask), "no_ask": str(no_ask),
        "yes_mid": yes_mid, "spread": str(spread),
        "fee_yes_per_contract": str(fee_yes), "fee_no_per_contract": str(fee_no),
        "edge_yes": edge_yes, "edge_no": edge_no,
        "momentum": mom,
        "hour_move": hour_move,
        "clock_skew_s": skew,
    }
    in_band = rules.min_yes_mid <= yes_mid <= rules.max_yes_mid
    spread_ok = spread <= Decimal(str(rules.max_spread))
    # side -> (ask, fee, edge, contracts displayed at that ask)
    sides = {"YES": (yes_ask, fee_yes, edge_yes, no_bid_size), "NO": (no_ask, fee_no, edge_no, yes_bid_size)}
    ticker = market["ticker"]
    ctx["shadow"] = _shadow(state, ticker, rules, in_band, spread_ok, sides)

    # 10. Strategy gates
    if state.acts_by_window.get(ticker, 0) >= rules.max_acts_per_window:
        raise _Skip("already_acted_this_window")
    if rules.daily_paper_cap is not None and state.acts_by_day.get(utc_date(now), 0) >= rules.daily_paper_cap:
        raise _Skip("daily_cap_reached")
    if not in_band:
        raise _Skip("price_out_of_band", f"yes mid {yes_mid:.3f}")
    if not spread_ok:
        raise _Skip("spread_too_wide", f"spread {spread}")
    if mom is None:
        raise _Skip("insufficient_history", "no complete spot point ~1/3/5 min ago (warming up or a recent data gap)")
    if mom["trend"] is None:
        raise _Skip("no_trend", f"momentum z {mom['z']:+.2f}")
    side = "YES" if mom["trend"] == "UP" else "NO"
    if move_already_extended(side, hour_move, rules):
        raise _Skip("move_already_extended", f"BTC {hour_move * 100:+.2f}% over the last hour, buying {side}")
    ask, fee, edge, depth = sides[side]
    if edge < rules.edge_threshold:
        raise _Skip("edge_below_threshold", f"{side} edge {edge * 100:+.2f}c after fees")
    if depth < C:
        raise _Skip("insufficient_depth", f"{depth} contracts displayed at {ask}")
    return {
        "type": "PAPER_INTENT",
        "is_fill": False,
        "action": "BUY",
        "side": side,
        "contracts": C,
        "limit_price": str(ask),
        "fee_per_contract": str(fee),
        "edge_per_contract": edge,
        "displayed_size_at_price": str(depth),
        "note": PAPER_NOTE,
    }


def move_already_extended(side: str, hour_move: float, rules) -> bool:
    """True if BTC already moved more than the limit over the last hour in the direction of this buy."""
    limit = rules.max_hour_move_with_trade
    if limit is None:
        return False
    return hour_move > limit if side == "YES" else hour_move < -limit


def _shadow(state, ticker, rules, in_band, spread_ok, sides):
    """Same rules minus the momentum filter, logged for comparison. Never an intent."""
    if state.shadow_by_window.get(ticker, 0) >= rules.max_acts_per_window:
        return {"would_act": False, "reason": "already_acted_this_window"}
    if not in_band:
        return {"would_act": False, "reason": "price_out_of_band"}
    if not spread_ok:
        return {"would_act": False, "reason": "spread_too_wide"}
    side = max(sides, key=lambda s: sides[s][2])
    ask, fee, edge, depth = sides[side]
    if edge < rules.edge_threshold:
        return {"would_act": False, "reason": "edge_below_threshold", "side": side, "edge": edge}
    if depth < rules.paper_contracts:
        return {"would_act": False, "reason": "insufficient_depth", "side": side, "edge": edge}
    return {"would_act": True, "reason": "edge_without_trend_filter", "side": side,
            "contracts": rules.paper_contracts, "limit_price": str(ask), "fee_per_contract": str(fee),
            "edge": edge}
