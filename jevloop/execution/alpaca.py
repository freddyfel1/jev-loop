"""Alpaca paper execution and market data, crypto or US equities. Paper
by default, on purpose.

Refuses to run against anything but the paper trading base URL, unless
ALL THREE live-trading gates are satisfied at once: see
resolve_trading_base_url() and LiveTradingRefused below. Paper is what
every default in this file, and every call site in the rest of the
skill, resolves to unless a caller explicitly asks for live and proves
it three separate ways. Refuses to place an equity order while the
market is closed, live or paper.
"""

from __future__ import annotations

import collections
import os
import time

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: uv pip install requests"
    ) from exc

from ..assets import AssetSpec, resolve_symbol

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_BASE_URL = "https://api.alpaca.markets"

# All three of these are required, together, before a single live order
# can be placed. Any one missing refuses outright rather than quietly
# trading paper instead, so a half-finished attempt to go live is never
# mistaken for a successful paper run.
LIVE_ALLOW_ENV_VAR = "JEV_LOOP_ALLOW_LIVE"
LIVE_ALLOW_ENV_VALUE = "i-understand-the-risk"
LIVE_CONFIRMATION_PHRASE = "I understand this trades real money"


class AlpacaConfigError(Exception):
    pass


class AlpacaAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


class MarketClosedError(Exception):
    """Raised when an order is attempted on a closed equity market. This
    class exists so the loop can tell "the venue said no" (AlpacaAPIError)
    apart from "we refused to even ask" (MarketClosedError)."""


class LiveTradingRefused(Exception):
    """Raised when live trading was requested but not all three required
    gates were satisfied: the --live flag, JEV_LOOP_ALLOW_LIVE set to
    exactly 'i-understand-the-risk' in the environment, and a typed
    confirmation matching LIVE_CONFIRMATION_PHRASE exactly. This is never
    raised for a plain paper run: paper needs none of these three."""


def assert_paper_url(base_url: str) -> None:
    """Hard guard for the ordinary, ungated construction path: this will
    not accept anything but the Alpaca paper trading endpoint. Live
    trading only ever reaches AlpacaPaperClient through client_from_env()
    after resolve_trading_base_url() has already checked all three gates;
    this function is not part of that path and still refuses the live URL
    on its own, by design."""
    if base_url.rstrip("/") != PAPER_TRADING_BASE_URL:
        raise AlpacaConfigError(
            f"refusing to run: trading base URL must be {PAPER_TRADING_BASE_URL}, got {base_url!r}. "
            "This skill only ever trades paper here."
        )


def resolve_trading_base_url(
    live: bool,
    allow_live_env: str | None,
    confirmation: str | None,
) -> str:
    """Paper unless all three gates are satisfied.

    live=False (the default everywhere): always returns the paper URL.
    The other two arguments are not even inspected, so a plain run is
    never affected by a stray environment variable.

    live=True: requires JEV_LOOP_ALLOW_LIVE=i-understand-the-risk in the
    environment AND a typed confirmation exactly matching
    LIVE_CONFIRMATION_PHRASE. Either one missing or wrong raises
    LiveTradingRefused; it never falls back to paper silently, because
    silently trading paper when the user explicitly asked for live and
    got a detail wrong is its own kind of dangerous surprise.
    """
    if not live:
        return PAPER_TRADING_BASE_URL

    if allow_live_env != LIVE_ALLOW_ENV_VALUE:
        raise LiveTradingRefused(
            f"refusing: --live was passed but {LIVE_ALLOW_ENV_VAR} is not set to "
            f"{LIVE_ALLOW_ENV_VALUE!r} in the environment. All three gates "
            "(--live, the environment variable, and a typed confirmation) are "
            "required together."
        )
    if confirmation != LIVE_CONFIRMATION_PHRASE:
        raise LiveTradingRefused(
            "refusing: the typed confirmation did not match "
            f"{LIVE_CONFIRMATION_PHRASE!r} exactly. All three gates (--live, "
            f"{LIVE_ALLOW_ENV_VAR}, and a typed confirmation) are required "
            "together."
        )
    return LIVE_TRADING_BASE_URL


class RateLimiter:
    """Simple token-bucket-by-timestamp limiter: keeps calls under N per
    rolling 60s window, sleeping just enough when the caller is about to
    exceed it."""

    def __init__(self, calls_per_minute: int):
        self.calls_per_minute = calls_per_minute
        self._timestamps: collections.deque[float] = collections.deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > 60.0:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.calls_per_minute:
            sleep_for = 60.0 - (now - self._timestamps[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._timestamps.append(time.monotonic())


class AlpacaPaperClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        spec: AssetSpec,
        base_url: str = PAPER_TRADING_BASE_URL,
        calls_per_minute: int = 90,
        _live_gate_passed: bool = False,
    ):
        if base_url.rstrip("/") == LIVE_TRADING_BASE_URL:
            if not _live_gate_passed:
                # Direct construction against the live URL, bypassing
                # client_from_env() and its three gates, is refused
                # unconditionally. There is no way to reach the live venue
                # from this class except through resolve_trading_base_url().
                raise AlpacaConfigError(
                    "refusing to run: the live trading base URL was passed directly, "
                    "bypassing the three live-trading gates in client_from_env(). "
                    "This skill only trades live via --live, JEV_LOOP_ALLOW_LIVE, "
                    "and a typed confirmation, all three, together."
                )
        else:
            assert_paper_url(base_url)
        self.base_url = base_url
        self.is_live = base_url.rstrip("/") == LIVE_TRADING_BASE_URL
        self.spec = spec
        self.symbol = spec.symbol
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._limiter = RateLimiter(calls_per_minute)
        self._clock_cache: tuple[float, dict] | None = None
        # One keep-alive session for every call: without it each request pays
        # a fresh TLS handshake, which is most of a tick's data-read time.
        self._session = requests.Session()

    # -- low-level HTTP -----------------------------------------------
    def _request(self, method: str, url: str, **kwargs) -> dict:
        self._limiter.wait()
        try:
            resp = self._session.request(
                method, url, headers=self._headers, timeout=10, **kwargs
            )
        except requests.RequestException as exc:
            # A network blip is an API error like any other: the loop counts
            # it toward max_api_errors and carries on, instead of crashing.
            raise AlpacaAPIError(0, f"network error: {exc}") from exc
        if resp.status_code == 401:
            raise AlpacaAPIError(
                401,
                "unauthorized. The key in your .env is either wrong or was copied from the LIVE "
                "dashboard instead of the PAPER dashboard. Generate a new key at "
                "https://app.alpaca.markets/paper/dashboard/overview (look for 'Generate New Keys' "
                "under Your API Keys) and update ALPACA_API_KEY / ALPACA_SECRET_KEY.",
            )
        if resp.status_code >= 400:
            raise AlpacaAPIError(resp.status_code, resp.text)
        if resp.text:
            return resp.json()
        return {}

    # -- account / orders -----------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", f"{self.base_url}/v2/account")

    def get_orders(self, status: str = "all", limit: int = 10) -> list:
        return self._request(
            "GET",
            f"{self.base_url}/v2/orders",
            params={"status": status, "limit": limit},
        )

    def get_clock(self, cache_s: float = 5.0) -> dict:
        """GET /v2/clock, cached briefly: this is checked every tick for an
        equity symbol and there is no reason to hit the API more than a
        few times a minute for a value that changes twice a day."""
        now = time.monotonic()
        if self._clock_cache and now - self._clock_cache[0] < cache_s:
            return self._clock_cache[1]
        data = self._request("GET", f"{self.base_url}/v2/clock")
        self._clock_cache = (now, data)
        return data

    def is_market_open(self) -> bool:
        if self.spec.is_24_7:
            return True
        return bool(self.get_clock().get("is_open"))

    def _qty_str(self, qty: float) -> str:
        return f"{qty:.{self.spec.qty_precision}f}"

    def submit_limit_order(
        self, side: str, qty: float, limit_price: float, tif: str = "gtc"
    ) -> dict:
        if not self.spec.is_24_7 and not self.is_market_open():
            raise MarketClosedError(
                f"{self.symbol} market is closed, refusing to submit an order"
            )
        body = {
            "symbol": self.symbol,
            "qty": self._qty_str(qty),
            "side": side,
            "type": "limit",
            "time_in_force": tif,
            "limit_price": f"{limit_price:.2f}",
        }
        return self._request("POST", f"{self.base_url}/v2/orders", json=body)

    def submit_market_order(self, side: str, qty: float) -> dict:
        if not self.spec.is_24_7 and not self.is_market_open():
            raise MarketClosedError(
                f"{self.symbol} market is closed, refusing to submit an order"
            )
        body = {
            "symbol": self.symbol,
            "qty": self._qty_str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "gtc" if self.spec.asset_class == "crypto" else "day",
        }
        return self._request("POST", f"{self.base_url}/v2/orders", json=body)

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", f"{self.base_url}/v2/orders/{order_id}")

    def cancel_all_orders(self) -> None:
        self._request("DELETE", f"{self.base_url}/v2/orders")

    # -- position (the broker is the source of truth for inventory) ------
    def _position_path(self) -> str:
        return f"{self.base_url}/v2/positions/{self.symbol.replace('/', '')}"

    def get_position(self) -> tuple[float, float]:
        """(signed qty, avg entry price) the broker actually holds for this
        symbol, (0.0, 0.0) when flat. Every fill lands here, from a market
        leg or a resting quote alike, which is why the loop reads inventory
        from this rather than counting its own orders."""
        try:
            data = self._request("GET", self._position_path())
        except AlpacaAPIError as exc:
            if exc.status_code == 404:
                return 0.0, 0.0
            raise
        qty = abs(float(data.get("qty") or 0.0))
        if data.get("side") == "short":
            qty = -qty
        return qty, float(data.get("avg_entry_price") or 0.0)

    def get_open_buy_notional_usd(self) -> float:
        """Dollar value still unfilled on this symbol's resting buy limit
        orders: exposure the position cap has to count, because any of them
        can fill between one tick and the next."""
        orders = self._request(
            "GET",
            f"{self.base_url}/v2/orders",
            params={"status": "open", "symbols": self.symbol, "limit": 500},
        )
        total = 0.0
        for o in orders:
            if o.get("side") != "buy" or not o.get("limit_price"):
                continue
            remaining = float(o.get("qty") or 0.0) - float(o.get("filled_qty") or 0.0)
            total += max(0.0, remaining) * float(o["limit_price"])
        return total

    def get_fills_since(self, after_iso: str, max_pages: int = 10) -> list[dict]:
        """Every fill (FILL account activity) on the account after
        `after_iso`, oldest first, across pages. Includes resting-quote
        fills, which no tick sees directly; the dashboard counts them."""
        fills: list[dict] = []
        params = {"after": after_iso, "direction": "asc", "page_size": 100}
        for _ in range(max_pages):
            page = self._request(
                "GET", f"{self.base_url}/v2/account/activities/FILL", params=params
            )
            if not page:
                break
            fills.extend(page)
            if len(page) < 100:
                break
            params = dict(params, page_token=page[-1]["id"])
        return fills

    def close_position(self) -> None:
        """Flatten this symbol at market. A 404 means it is already flat."""
        try:
            self._request("DELETE", self._position_path())
        except AlpacaAPIError as exc:
            if exc.status_code != 404:
                raise

    # -- market data -----------------------------------------------
    def get_orderbook(self) -> dict:
        """Real L2 depth on crypto. Returns {} on equities (has_depth is
        False there) rather than pretending a book exists."""
        if not self.spec.has_depth or self.spec.orderbook_url is None:
            return {}
        data = self._request(
            "GET", self.spec.orderbook_url, params={"symbols": self.symbol}
        )
        return data.get("orderbooks", {}).get(self.symbol, {})

    def get_latest_quote(self) -> dict:
        """Best bid/ask. The only depth source at all on equities."""
        data = self._request(
            "GET", self.spec.latest_quote_url, params={"symbols": self.symbol}
        )
        return data.get("quotes", {}).get(self.symbol, {})

    def get_latest_trade(self) -> dict:
        data = self._request(
            "GET", self.spec.latest_trade_url, params={"symbols": self.symbol}
        )
        return data.get("trades", {}).get(self.symbol, {})

    def get_recent_trades(self, limit: int = 100) -> list:
        data = self._request(
            "GET",
            self.spec.recent_trades_url,
            params={"symbols": self.symbol, "limit": limit},
        )
        return data.get("trades", {}).get(self.symbol, [])


def client_from_env(
    symbol: str = "BTC/USD",
    live: bool = False,
    confirmation: str | None = None,
    calls_per_minute: int = 90,
) -> AlpacaPaperClient:
    """Builds the execution client. Paper unless `live=True` AND
    JEV_LOOP_ALLOW_LIVE=i-understand-the-risk is set AND `confirmation`
    matches LIVE_CONFIRMATION_PHRASE exactly -- see
    resolve_trading_base_url(). `live` defaults to False everywhere it is
    called in this skill, so paper is always the default unless a caller
    goes out of its way to ask for live."""
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise AlpacaConfigError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")

    allow_live_env = os.environ.get(LIVE_ALLOW_ENV_VAR)
    base_url = resolve_trading_base_url(live, allow_live_env, confirmation)
    spec = resolve_symbol(symbol)

    if base_url == LIVE_TRADING_BASE_URL:
        print("\n" + "!" * 70)
        print("! LIVE TRADING ENABLED.")
        print("! Every order from here on spends real money in your real Alpaca")
        print("! account. There is no paper safety net under this run.")
        print("!" * 70 + "\n")
        return AlpacaPaperClient(
            api_key,
            secret_key,
            spec=spec,
            base_url=base_url,
            calls_per_minute=calls_per_minute,
            _live_gate_passed=True,
        )

    # Paper path: still allow ALPACA_BASE_URL to point at a stand-in
    # server for testing, exactly as before live trading existed at all.
    override_url = os.environ.get("ALPACA_BASE_URL", base_url)
    return AlpacaPaperClient(
        api_key,
        secret_key,
        spec=spec,
        base_url=override_url,
        calls_per_minute=calls_per_minute,
    )
