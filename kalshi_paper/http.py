"""The only network code in this package: unauthenticated HTTPS GET to allow-listed hosts.

There is deliberately no way to send a request body, set a method, or attach
credentials. Kalshi's order endpoints require signed requests, which this module
cannot produce.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({
    "api.elections.kalshi.com",   # Kalshi public market data
    "api.exchange.coinbase.com",  # Coinbase Exchange public market data
    "api.kraken.com",             # Kraken public market data
    "www.bitstamp.net",           # Bitstamp public market data
})

# Generic client name. Coinbase rejects Python's default User-Agent with HTTP 403.
USER_AGENT = "kalshi-paper-research/0.1 (read-only)"


class FetchError(Exception):
    """Network-level failure: timeout, DNS, connection reset, TLS error."""


@dataclass
class Response:
    url: str
    status: int
    headers: dict  # lower-cased header names
    body: bytes
    sent_at: float
    received_at: float


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing non-HTTPS URL: {url}")
    if parts.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing host not on the read-only allow-list: {parts.hostname}")


class UrllibTransport:
    """Real network transport. Tests and the demo swap in a synthetic one."""

    def __call__(self, url: str, timeout: float) -> Response:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, method="GET"
        )
        sent = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return Response(url, r.status, _lower(r.headers), body, sent, time.time())
        except urllib.error.HTTPError as e:  # 4xx/5xx still carry headers and a body
            try:
                body = e.read()
            except OSError:
                body = b""
            return Response(url, e.code, _lower(e.headers), body, sent, time.time())
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e


def _lower(headers) -> dict:
    return {k.lower(): v for k, v in (headers.items() if headers else [])}


def get(url: str, transport, timeout: float = 4.0) -> Response:
    """GET `url` through `transport` after checking it against the allow-list."""
    check_url(url)
    return transport(url, timeout)
