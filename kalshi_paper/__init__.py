"""Read-only research prototype for Kalshi's 15-minute BTC markets (series KXBTC15M).

Collects public market data, evaluates the versioned paper rules, and logs ACT or
SKIP. It never trades: there is no order code, no API key, and no authenticated
client. Every network call is an unauthenticated HTTPS GET to an allow-listed host.
"""

__version__ = "0.1.0"

SYNTHETIC_LABEL = "SYNTHETIC - generated for testing, not market data"
