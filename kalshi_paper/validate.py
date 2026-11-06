"""Checks that the open market is exactly the contract we intend: KXBTC15M, binary,
"BTC price up in next 15 mins?", settling on CF Benchmarks' BRTI 60-second averages.

Anything unexpected returns a reason string, and the evaluator SKIPs (fail closed).
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .timeutil import parse_iso_utc

ET = ZoneInfo("America/New_York")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# The exact contract wording Kalshi used on 2026-11-04 (straight or curly apostrophe).
RULES_RE = re.compile(
    r"^If the simple average of the sixty seconds of CF Benchmarks['’] BRTI before (?P<close>.+?) "
    r"is at least the simple average of the sixty seconds of CF Benchmarks['’] BRTI before (?P<open>.+?), "
    r"then the market resolves to Yes\.$"
)
# e.g. "11:45 PM EST on Nov 3, 2026" or "11:30 PM EST on November 3, 2026"
TIME_RE = re.compile(
    r"^(?P<h>\d{1,2}):(?P<m>\d{2}) (?P<ampm>AM|PM) (?P<tz>EST|EDT) on "
    r"(?P<mon>[A-Za-z]{3,9})\.? (?P<d>\d{1,2}), (?P<y>\d{4})$"
)
TARGET_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")


def _ticker_re(series):
    return re.compile(
        rf"^{re.escape(series)}-(?P<yy>\d{{2}})(?P<mon>[A-Z]{{3}})(?P<dd>\d{{2}})(?P<hhmm>\d{{4}})-(?P<mm>\d{{2}})$"
    )


def event_code(close_ts: float, series: str) -> str:
    """Kalshi event ticker for a window closing at close_ts, e.g. KXBTC15M-26NOV032345."""
    dt = datetime.fromtimestamp(close_ts, tz=ET)
    return f"{series}-{dt:%y}{MONTHS[dt.month - 1]}{dt:%d}{dt:%H%M}"


def _matches_et(text: str, ts: float) -> bool:
    m = TIME_RE.match(text.strip())
    if not m:
        return False
    dt = datetime.fromtimestamp(ts, tz=ET)
    return (
        int(m["h"]) == (dt.hour % 12 or 12)
        and int(m["m"]) == dt.minute
        and m["ampm"] == ("AM" if dt.hour < 12 else "PM")
        and m["tz"] == dt.tzname()
        and m["mon"][:3].upper() == MONTHS[dt.month - 1]
        and int(m["d"]) == dt.day
        and int(m["y"]) == dt.year
    )


def check_market(market, series: str, window_seconds: int) -> str | None:
    """None if `market` is the intended contract, else a SKIP reason."""
    if not isinstance(market, dict):
        return "wrong_market:no_market_object"
    ticker = market.get("ticker") or ""
    tm = _ticker_re(series).match(ticker)
    if not tm:
        return "wrong_market:ticker"
    if market.get("event_ticker") != ticker.rsplit("-", 1)[0]:
        return "wrong_market:event_ticker"
    if market.get("market_type") != "binary":
        return "wrong_market:market_type"
    if market.get("strike_type") != "greater_or_equal":
        return "wrong_market:strike_type"
    try:
        open_ts = parse_iso_utc(market.get("open_time") or "")
        close_ts = parse_iso_utc(market.get("close_time") or "")
    except ValueError:
        return "wrong_market:bad_times"
    if abs((close_ts - open_ts) - window_seconds) > 0.5:
        return "wrong_market:window_length"
    close_et = datetime.fromtimestamp(close_ts, tz=ET)
    if ticker.rsplit("-", 1)[0] != event_code(close_ts, series) or tm["mm"] != f"{close_et:%M}":
        return "wrong_market:ticker_time_mismatch"
    rules = RULES_RE.match((market.get("rules_primary") or "").strip())
    if not rules:
        return "wrong_market:rules_text"
    if not (_matches_et(rules["close"], close_ts) and _matches_et(rules["open"], open_ts)):
        return "wrong_market:rules_times"
    strike = market.get("floor_strike")
    shown = TARGET_RE.search(market.get("yes_sub_title") or "")
    if shown and isinstance(strike, (int, float)):
        if abs(float(shown.group(1).replace(",", "")) - float(strike)) > 0.005:
            return "wrong_market:target_mismatch"
    if market.get("status") != "active":
        return f"market_not_active:{market.get('status')}"
    return None
