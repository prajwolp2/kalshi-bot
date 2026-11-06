"""Math for the rules: fees, volatility, random-walk fair value, settlement-index gap, momentum."""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, Decimal
from statistics import NormalDist, median, stdev

CENT = Decimal("0.01")
_N = NormalDist()


def taker_fee_per_contract(price: Decimal, contracts: int, rate: Decimal = Decimal("0.07")) -> Decimal:
    """Kalshi taker fee: rate x C x P x (1-P) for the whole order, rounded UP to the next cent.

    Rounding the order total up to a cent is the conservative reading of Kalshi's fee
    schedule (members with sub-cent balance precision can pay slightly less).
    Returned per contract.
    """
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    total = (rate * contracts * price * (Decimal(1) - price)).quantize(CENT, rounding=ROUND_CEILING)
    return total / contracts


def sigma_per_second(closes: list[float]) -> float:
    """Stdev of consecutive 1-minute log returns, scaled to one second (random-walk scaling)."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 2:
        return float("nan")
    return stdev(rets) / math.sqrt(60.0)


def fair_probability(spot: float, strike: float, sigma_s: float, seconds_left: float,
                     avg_window_s: float = 60.0) -> tuple[float, float, float]:
    """P(final 60-second average >= strike) if BTC is a driftless random walk.

    Settlement uses the average over the last `avg_window_s` seconds. The variance of
    that average, seen from now, is sigma^2 * (seconds_left - 2/3 * avg_window_s), so
    with a 60-second average the effective horizon is seconds_left - 40.
    Requires seconds_left > avg_window_s (the rules skip the final 90 s anyway).
    """
    tau = seconds_left - 2.0 * avg_window_s / 3.0
    if tau <= 0 or not sigma_s > 0:
        raise ValueError("horizon and volatility must be positive")
    z = math.log(spot / strike) / (sigma_s * math.sqrt(tau))
    return _N.cdf(z), z, tau


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def settlement_basis(basis_log, now: float, rules) -> tuple[float, int] | None:
    """Recent average gap between Kalshi's settlement index and our own spot price.

    Rows are [open_ts, ticker, strike, our_average, n_points]: at each window open the
    strike is BRTI's average over the preceding minute, and our_average is the median of
    our three exchanges averaged over the same minute. Returns (mean of strike minus
    our_average over up to `basis_windows` recent usable rows, rows used), or None when
    fewer than `min_basis_windows` qualify.
    """
    usable = {}
    for r in basis_log if isinstance(basis_log, list) else []:
        if not isinstance(r, (list, tuple)) or len(r) < 5:
            continue
        o, strike, ours, n = r[0], r[2], r[3], r[4]
        if not all(_num(v) for v in (o, strike, ours, n)):
            continue
        if n >= rules.basis_min_points and strike > 0 and ours > 0 and 0 <= now - o <= rules.basis_max_age_s:
            usable[o] = strike - ours
    recent = [usable[o] for o in sorted(usable)[-rules.basis_windows:]]
    if len(recent) < rules.min_basis_windows:
        return None
    return sum(recent) / len(recent), len(recent)


def lookback(history: list, now: float, minutes: float, tolerance_s: float):
    """Latest history point at or before now - minutes, no more than tolerance_s earlier.

    History rows are [t, coinbase_mid, kraken_mid, bitstamp_mid]; rows missing any
    venue are ignored. Returns (t, median_mid) or None.
    """
    target = now - 60.0 * minutes
    best = None
    for row in history:
        t, mids = row[0], row[1:]
        if target - tolerance_s <= t <= target and len(mids) == 3 and all(m is not None for m in mids):
            if best is None or t > best[0]:
                best = (t, median(mids))
    return best


def momentum(history: list, now: float, spot: float, sigma_s: float, rules) -> dict | None:
    """Momentum features, or None if the spot history is too short (warming up)."""
    ks = (rules.confirm_short_min, rules.momentum_lookback_min, rules.confirm_long_min)
    out: dict = {}
    for k in ks:
        point = lookback(history, now, k, rules.history_tolerance_s)
        if point is None:
            return None
        t, ref = point
        out[f"ref_{k}m"] = ref
        out[f"elapsed_{k}m_s"] = now - t
        out[f"ret_{k}m"] = math.log(spot / ref)
    k = rules.momentum_lookback_min
    z = out[f"ret_{k}m"] / (sigma_s * math.sqrt(out[f"elapsed_{k}m_s"]))
    short, long_ = out[f"ret_{rules.confirm_short_min}m"], out[f"ret_{rules.confirm_long_min}m"]
    if z >= rules.momentum_z and short > 0 and long_ > 0:
        trend = "UP"
    elif z <= -rules.momentum_z and short < 0 and long_ < 0:
        trend = "DOWN"
    else:
        trend = None
    out["z"] = z
    out["trend"] = trend
    return out
