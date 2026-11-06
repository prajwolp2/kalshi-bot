"""Public price history for studying the rules.

Fetches settled KXBTC15M windows (strike and result), Kalshi's 1-minute YES bid/ask
candles for each window, and Coinbase's 1-minute BTC-USD candles. Read-only GETs
through http.get (allow-listed hosts only), paced at about five requests per second
with bounded, backed-off retries (Kalshi Developer Agreement 4.3).

Everything is cached under data/history/, so a re-run only fetches what is missing.
Like data/live/, this data is for your own trading research only (Developer
Agreement 3.1), and the folder is git-ignored.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import quote

from .collector import KALSHI_BASE, URL_COINBASE_CANDLES
from .http import FetchError, get
from .storage import append_jsonl, read_jsonl
from .timeutil import parse_iso_utc

PAUSE_S = 0.1              # between requests: well under Kalshi's 20-per-second public read limit
MAX_TRIES = 4              # bounded retries, doubling the wait each time
SPOT_CHUNK_MIN = 300       # Coinbase returns at most 300 candles per request
SPOT_WARMUP_MIN = 66       # volatility uses the 60 minutes before a window opens, plus slack
SPOT_FILE = "coinbase-btcusd-1m.jsonl"


class HistoryError(Exception):
    """A history request kept failing after bounded retries."""


def windows_file(series: str) -> str:
    return f"settled-{series}.jsonl"


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def get_json(url: str, transport, sleep=time.sleep, timeout: float = 15.0):
    """GET and decode JSON, retrying network errors, non-200s and bad JSON a bounded number of times."""
    delay, last = 1.0, None
    for attempt in range(MAX_TRIES):
        try:
            r = get(url, transport, timeout=timeout)
        except FetchError as e:
            last = str(e)
        else:
            if r.status == 200:
                try:
                    return json.loads(r.body)
                except ValueError as e:
                    last = f"unreadable JSON: {e}"
            else:
                last = f"HTTP {r.status}"
        if attempt < MAX_TRIES - 1:
            sleep(delay)
            delay *= 2
    raise HistoryError(f"{url}: {last}")


def parse_settled(m: dict) -> dict | None:
    """One settled window as {ticker, open_ts, close_ts, strike, result}, or None if unusable."""
    try:
        o, c = int(parse_iso_utc(m["open_time"])), int(parse_iso_utc(m["close_time"]))
        strike = float(m["floor_strike"])
    except (KeyError, TypeError, ValueError):
        return None
    if (m.get("result") not in ("yes", "no") or c - o != 900 or strike <= 0
            or m.get("strike_type", "greater_or_equal") != "greater_or_equal"):
        return None
    return {"ticker": m["ticker"], "open_ts": o, "close_ts": c, "strike": strike, "result": m["result"]}


def parse_market_candles(d: dict) -> list:
    """[[minute_end_ts, yes_bid, yes_ask], ...] from Kalshi's candlesticks response, oldest first."""
    out = []
    for c in (d.get("candlesticks") or []) if isinstance(d, dict) else []:
        try:
            bid, ask = c["yes_bid"], c["yes_ask"]
            # Live endpoints say "close_dollars"; Kalshi's archive says "close".
            out.append([int(c["end_period_ts"]), float(bid.get("close_dollars", bid.get("close"))),
                        float(ask.get("close_dollars", ask.get("close")))])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def parse_spot_candles(rows) -> dict:
    """{minute_start_ts: close} from Coinbase rows [start, low, high, open, close, volume]."""
    out = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            out[int(r[0])] = float(r[4])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def fetch_settled(series: str, start_ts: int, end_ts: int, transport, sleep=time.sleep) -> list:
    """Settled windows that closed between start_ts and end_ts, oldest first."""
    out, cursor = {}, ""
    while True:
        url = (f"{KALSHI_BASE}/markets?series_ticker={series}&status=settled"
               f"&min_close_ts={start_ts}&max_close_ts={end_ts}&limit=1000")
        if cursor:
            url += "&cursor=" + quote(cursor, safe="")
        d = get_json(url, transport, sleep)
        markets = d.get("markets") or []
        for m in markets:
            w = parse_settled(m)
            if w and w["ticker"].startswith(series + "-"):
                out[w["ticker"]] = w
        cursor = d.get("cursor") or ""
        sleep(PAUSE_S)
        if not cursor or not markets:
            break
    return sorted(out.values(), key=lambda w: w["close_ts"])


def archive_cutoff(transport, sleep=time.sleep) -> int:
    """Markets that settled before this time live in Kalshi's archive (/historical/...)."""
    return int(parse_iso_utc(get_json(f"{KALSHI_BASE}/historical/cutoff", transport, sleep)["market_settled_ts"]))


def fetch_archived(series: str, start_ts: int, end_ts: int, transport, sleep=time.sleep) -> list:
    """Archived settled windows closing between start_ts and end_ts, oldest first.

    The archive ignores date filters, so this pages through the whole series and filters here.
    """
    out, cursor = {}, ""
    while True:
        url = f"{KALSHI_BASE}/historical/markets?series_ticker={series}&limit=1000"
        if cursor:
            url += "&cursor=" + quote(cursor, safe="")
        d = get_json(url, transport, sleep)
        markets = d.get("markets") or []
        for m in markets:
            w = parse_settled(m)
            if w and w["ticker"].startswith(series + "-") and start_ts <= w["close_ts"] <= end_ts:
                out[w["ticker"]] = w
        cursor = d.get("cursor") or ""
        sleep(PAUSE_S)
        if not cursor or not markets:
            break
    return sorted(out.values(), key=lambda w: w["close_ts"])


def update(data_dir: str, series: str, start_ts: int, end_ts: int, transport,
           sleep=time.sleep, log=print, cutoff: int | None = None) -> dict:
    """Bring the cache up to date for windows closing in [start_ts, end_ts], from the archive when needed."""
    d = Path(data_dir)
    wpath = d / windows_file(series)
    have = {w["ticker"] for w in read_jsonl(wpath)}
    cutoff = archive_cutoff(transport, sleep) if cutoff is None else cutoff
    windows = fetch_settled(series, max(start_ts, cutoff), end_ts, transport, sleep) if end_ts >= cutoff else []
    if start_ts < cutoff:
        archived = fetch_archived(series, start_ts, min(end_ts, cutoff), transport, sleep)
        seen = {w["ticker"] for w in windows}
        windows = [w for w in archived if w["ticker"] not in seen] + windows
    todo = [w for w in windows if w["ticker"] not in have]
    if log:
        log(f"{len(windows)} settled windows in range, {len(todo)} not cached yet")
    for i, w in enumerate(todo, 1):
        base = (f"{KALSHI_BASE}/historical/markets/{w['ticker']}" if w["close_ts"] < cutoff
                else f"{KALSHI_BASE}/series/{series}/markets/{w['ticker']}")
        url = f"{base}/candlesticks?start_ts={w['open_ts']}&end_ts={w['close_ts']}&period_interval=1"
        w["candles"] = parse_market_candles(get_json(url, transport, sleep))
        append_jsonl(wpath, w)
        sleep(PAUSE_S)
        if log and i % 500 == 0:
            log(f"  Kalshi candles: {i}/{len(todo)} windows")

    spath = d / SPOT_FILE
    spot = {r["t"]: r["close"] for r in read_jsonl(spath)}
    first = (start_ts - 900 - SPOT_WARMUP_MIN * 60) // 60 * 60
    last = end_ts // 60 * 60
    added, t = 0, first
    while t <= last:
        e = min(t + (SPOT_CHUNK_MIN - 1) * 60, last)
        missing = sum(1 for m in range(t, e + 60, 60) if m not in spot)
        if missing > 2:  # Coinbase skips minutes with no trades; don't refetch a chunk for a gap or two
            got = parse_spot_candles(get_json(
                f"{URL_COINBASE_CANDLES}&start={_iso(t)}&end={_iso(e)}", transport, sleep))
            for k in sorted(got):
                if t <= k <= e and k not in spot:
                    spot[k] = got[k]
                    append_jsonl(spath, {"t": k, "close": got[k]})
                    added += 1
            sleep(PAUSE_S)
        t = e + 60
    return {"windows_in_range": len(windows), "windows_fetched": len(todo), "spot_minutes_added": added}


def load(data_dir: str, series: str) -> tuple[list, dict]:
    """(windows oldest first, {minute_start_ts: close}) from the cache."""
    d = Path(data_dir)
    windows = {w["ticker"]: w for w in read_jsonl(d / windows_file(series))}
    spot = {int(r["t"]): float(r["close"]) for r in read_jsonl(d / SPOT_FILE)}
    return sorted(windows.values(), key=lambda w: w["close_ts"]), spot
