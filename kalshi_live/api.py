"""Signed Kalshi Trade API client. Buys only: there is no sell, withdrawal or transfer method.

Orders use Create Order V2 (POST /portfolio/events/orders). In V2 `side` is the YES leg's book
side and `price` is a YES-leg price: buying YES is a bid at the YES price; buying NO is an ask
(selling YES) at 1 - the NO price. `smoke` checks this mapping on the exchange before any real
trading (the order Kalshi records must say outcome_side "no" and no_price = our NO price).
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from decimal import Decimal
from urllib.parse import quote, urlencode, urlsplit

BASE = {"demo": "https://external-api.demo.kalshi.co/trade-api/v2",
        "live": "https://external-api.kalshi.com/trade-api/v2"}
ALLOWED_HOSTS = frozenset(urlsplit(u).hostname for u in BASE.values())
SERIES = "KXBTC15M"
USER_AGENT = "kalshi-bot/0.1"
MIN_PRICE, MAX_PRICE = Decimal("0.01"), Decimal("0.99")


class ApiError(Exception):
    def __init__(self, status: int, body: str, method: str, path: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class NetworkError(Exception):
    """No HTTP response at all (timeout, DNS, reset). For an order this means: unknown if it was placed."""


def urllib_send(method: str, url: str, headers: dict, data: bytes | None, timeout: float):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing URL outside the Kalshi trade API: {url}")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return e.code, body
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise NetworkError(f"{type(e).__name__}: {e}") from e


def yes_leg_price(side: str, price: Decimal) -> Decimal:
    """The V2 order price for buying `side` at `price` (a price for that side)."""
    return price if side == "YES" else Decimal(1) - price


class Client:
    def __init__(self, env: str, signer, send=urllib_send, clock=time.time, sleep=time.sleep):
        if env not in BASE:
            raise ValueError(f"env must be one of {sorted(BASE)}")
        self.env, self.base, self.signer = env, BASE[env], signer
        self.send, self.clock, self.sleep = send, clock, sleep

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
              retries: int = 0) -> dict:
        url = self.base + path + ("?" + urlencode(query) if query else "")
        sign_path = urlsplit(self.base + path).path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(retries + 1):
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT,
                       **self.signer.headers(method, sign_path, int(self.clock() * 1000))}
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, text = self.send(method, url, headers, data, 5.0)
            if status == 429 and attempt < retries:
                self.sleep(0.5 * 2 ** attempt)  # rate limited: back off and try again
                continue
            break
        if status >= 400:
            raise ApiError(status, text, method, path)
        try:
            return json.loads(text) if text else {}
        except ValueError as e:
            raise ApiError(status, f"unparseable response: {text[:200]}", method, path) from e

    # -- reads -------------------------------------------------------------------------------
    def balance(self) -> Decimal:
        """Available cash in dollars across all exchange shards (money in open positions not included)."""
        return self.balances()[0]

    def balances(self) -> tuple[Decimal, dict]:
        """(total cash, {exchange_index: cash}) in dollars.

        Kalshi holds cash per exchange shard, and an API order can only use the cash on its
        market's shard (crypto is shard 2); the website moves money between shards for you,
        the API does not. The breakdown is empty if Kalshi doesn't send one."""
        total, shards, _ = self.account()
        return total, shards

    def account(self) -> tuple[Decimal, dict, Decimal | None]:
        """(total cash, {exchange_index: cash}, value of all open positions) in dollars.

        The positions value is Kalshi's portfolio_value: every open position on the account, so
        it includes other bots trading the same account (the gold bot). None if it isn't sent."""
        r = self._call("GET", "/portfolio/balance", retries=2)
        shards = {}
        for b in r.get("balance_breakdown") or []:
            try:
                shards[int(b["exchange_index"])] = Decimal(str(b["balance"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        pv = r.get("portfolio_value")
        return Decimal(int(r["balance"])) / 100, shards, None if pv is None else Decimal(int(pv)) / 100

    def market_shard(self, series: str = SERIES) -> int | None:
        """The exchange shard the series' open market trades on (None if Kalshi doesn't say)."""
        for m in self.open_markets(series):
            if m.get("exchange_index") is not None:
                return int(m["exchange_index"])
        return None

    def open_markets(self, series: str = SERIES) -> list:
        r = self._call("GET", "/markets", {"series_ticker": series, "status": "open"}, retries=2)
        return r.get("markets") or []

    def get_order(self, order_id: str) -> dict:
        r = self._call("GET", f"/portfolio/orders/{quote(order_id, safe='')}", retries=2)
        return r.get("order", r)

    def find_order(self, order_id: str, ticker: str, client_order_id: str | None = None, tries: int = 4) -> dict:
        """The order Kalshi recorded, right after placing it.

        On the live exchange GET /portfolio/orders/{id} answered 404 for V2 orders placed a moment
        earlier, so on a 404 this also searches the market's order list (all shards) by order ID
        or client order ID, retrying with short waits (0.5, 1, 2 s)."""
        for k in range(tries):
            try:
                return self.get_order(order_id)
            except ApiError as e:
                if e.status != 404:
                    raise
            r = self._call("GET", "/portfolio/orders", {"ticker": ticker, "limit": 200}, retries=2)
            for o in r.get("orders") or []:
                if o.get("order_id") == order_id or (client_order_id and o.get("client_order_id") == client_order_id):
                    return o
            if k < tries - 1:
                self.sleep(0.5 * 2 ** k)
        raise ApiError(404, f"order {order_id} not found by ID or in {ticker}'s order list", "GET", "/portfolio/orders")

    # -- writes ------------------------------------------------------------------------------
    def buy(self, ticker: str, side: str, price: Decimal, count: int, client_order_id: str,
            time_in_force: str = "immediate_or_cancel", expiration_ts: int | None = None) -> dict:
        """Buy `count` contracts of `side` paying at most `price` each (a price for that side).

        Never retried here: if it fails, the caller decides (a retry could double the order)."""
        if side not in ("YES", "NO"):
            raise ValueError(f"side must be YES or NO, not {side!r}")
        if not str(ticker).startswith(SERIES + "-"):
            raise ValueError(f"refusing to trade {ticker!r}: only {SERIES} markets")
        price = Decimal(price)
        if not MIN_PRICE <= price <= MAX_PRICE:
            raise ValueError(f"price {price} outside {MIN_PRICE}-{MAX_PRICE}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(f"count must be a whole number of contracts >= 1, not {count!r}")
        if time_in_force not in ("immediate_or_cancel", "good_till_canceled"):
            raise ValueError(f"unsupported time_in_force {time_in_force!r}")
        body = {"ticker": ticker, "side": "bid" if side == "YES" else "ask", "count": str(count),
                "price": f"{yes_leg_price(side, price):.4f}", "time_in_force": time_in_force,
                "self_trade_prevention_type": "taker_at_cross", "client_order_id": client_order_id}
        if expiration_ts is not None:
            body["expiration_time"] = int(expiration_ts)
        return self._call("POST", "/portfolio/events/orders", body=body)

    def move_between_shards(self, amount: Decimal, from_shard: int, to_shard: int, client_transfer_id: str) -> dict:
        """Move cash between exchange shards inside this account (Kalshi's intra-account transfer).

        Only ever called by the `fund-shard` command you run yourself; the trading loop never
        moves money. Source and destination are both the event-contract exchange of the primary
        account, so the money stays in your Kalshi account. Kalshi processes it asynchronously."""
        amount = Decimal(amount)
        if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError(f"amount must be positive whole cents, not {amount}")
        for s in (from_shard, to_shard):
            if not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 100:
                raise ValueError(f"shard must be a whole number 0-100, not {s!r}")
        if from_shard == to_shard:
            raise ValueError("source and destination shard are the same")
        body = {"client_transfer_id": client_transfer_id, "source": "event_contract", "destination": "event_contract",
                "amount": int(amount * 10000),  # Kalshi wants centicents: $1 = 10,000
                "source_exchange_shard": from_shard, "destination_exchange_shard": to_shard}
        return self._call("POST", "/portfolio/intra_exchange_instance_transfer", body=body)

    def cancel(self, order_id: str, ticker: str) -> dict:
        return self._call("DELETE", f"/portfolio/events/orders/{quote(order_id, safe='')}",
                          {"market_ticker": ticker}, retries=2)
