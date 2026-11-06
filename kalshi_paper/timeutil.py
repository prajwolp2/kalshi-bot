from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|\+00:00)$")


def parse_iso_utc(value: str) -> float:
    """Parse '2026-11-04T04:50:58.592776585Z' (any number of fraction digits) to epoch seconds."""
    m = _ISO_RE.match(value or "")
    if not m:
        raise ValueError(f"unrecognized UTC timestamp: {value!r}")
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    frac = m.group(2) or ""
    micros = int((frac + "000000")[:6]) if frac else 0
    return base.timestamp() + micros / 1e6


def http_date_to_epoch(value: str | None) -> float | None:
    """HTTP Date header ('Wed, 04 Nov 2026 04:51:00 GMT', 1-second resolution) to epoch seconds."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
