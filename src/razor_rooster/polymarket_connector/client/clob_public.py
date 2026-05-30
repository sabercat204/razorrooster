"""Polymarket CLOB public client (T-PMC-034).

Wraps the public-only endpoints under ``https://clob.polymarket.com``:

- ``GET /book?token_id=<id>`` — full orderbook for one outcome token.
- ``GET /price?token_id=<id>&side=<buy|sell>`` — current price.
- ``GET /midpoint?token_id=<id>`` — midpoint of best bid/ask.
- ``GET /trades?market=<condition_id>&...`` — trade history for a market.
- ``GET /last-trade-price?token_id=<id>`` — last trade price.

The CLOB exposes more endpoints (L1/L2/builder); we deliberately do not
expose them here. v1 is read-only public data; trading paths return in
v2 with FULL threat context.

All requests go through the shared rate-limit token bucket and the
retry harness — same pattern as Gamma.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from razor_rooster.polymarket_connector.client.rate_limit import (
    TokenBucket,
    get_shared_bucket,
)
from razor_rooster.polymarket_connector.client.retry import (
    DEFAULT_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_SECONDS,
    retry_with_backoff,
)
from razor_rooster.polymarket_connector.client.user_agent import build_httpx_client

logger = logging.getLogger(__name__)


CLOB_BASE_URL: Final[str] = "https://clob.polymarket.com"

DEFAULT_TRADES_PAGE_SIZE: Final[int] = 100
_MAX_TRADE_PAGES: Final[int] = 10_000


Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class OrderbookLevel:
    """One side of one orderbook level."""

    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Orderbook:
    """Parsed orderbook for one outcome token.

    ``raw`` is the verbatim response so the connector's persistence layer
    can store it under ``source_payload_json``. ``best_bid`` /
    ``best_ask`` are convenience accessors over the top-of-book entries.
    """

    market: str
    asset_id: str
    timestamp: str
    bids: tuple[OrderbookLevel, ...]
    asks: tuple[OrderbookLevel, ...]
    last_trade_price: float | None
    tick_size: float | None
    min_order_size: float | None
    neg_risk: bool
    raw: dict[str, Any]

    @property
    def best_bid(self) -> OrderbookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderbookLevel | None:
        return self.asks[0] if self.asks else None


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """A side-resolved price quote for one token."""

    token_id: str
    side: Side
    price: float | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MidpointQuote:
    token_id: str
    midpoint: float | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Trade:
    """One trade record from the public trades endpoint."""

    tx_hash: str
    market: str
    asset_id: str
    price: float
    size: float
    side: str | None
    trade_ts_seconds: int | None
    raw: dict[str, Any]


class ClobClientError(RuntimeError):
    """Base class for CLOB client failures that survived retries."""


class ClobPublicClient:
    """Synchronous client for Polymarket's public CLOB REST endpoints."""

    def __init__(
        self,
        *,
        base_url: str = CLOB_BASE_URL,
        http_client: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        if http_client is not None:
            self._http: httpx.Client = http_client
            self._owns_client = False
        else:
            self._http = build_httpx_client()
            self._owns_client = True
        self._bucket: TokenBucket = bucket if bucket is not None else get_shared_bucket()
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds

    def __enter__(self) -> ClobPublicClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # -- public methods ----------------------------------------------------

    def get_orderbook(self, token_id: str) -> Orderbook | None:
        """Fetch the full orderbook for a token. Returns ``None`` for 404."""
        try:
            payload = self._get("/book", params={"token_id": token_id})
        except ClobClientError as exc:
            if "404" in str(exc):
                return None
            raise
        if not isinstance(payload, dict):
            raise ClobClientError(f"unexpected /book response shape: {type(payload).__name__}")
        return _parse_orderbook(payload)

    def get_price(self, token_id: str, side: Side) -> PriceQuote:
        """Fetch the current price for the given side of a token."""
        payload = self._get("/price", params={"token_id": token_id, "side": side})
        if not isinstance(payload, dict):
            raise ClobClientError(f"unexpected /price response shape: {type(payload).__name__}")
        price_value = payload.get("price")
        return PriceQuote(
            token_id=token_id,
            side=side,
            price=_safe_float(price_value),
            raw=payload,
        )

    def get_midpoint(self, token_id: str) -> MidpointQuote:
        """Fetch the midpoint price for a token."""
        payload = self._get("/midpoint", params={"token_id": token_id})
        if not isinstance(payload, dict):
            raise ClobClientError(f"unexpected /midpoint response shape: {type(payload).__name__}")
        mid_value = payload.get("mid") or payload.get("midpoint")
        return MidpointQuote(
            token_id=token_id,
            midpoint=_safe_float(mid_value),
            raw=payload,
        )

    def get_last_trade_price(self, token_id: str) -> float | None:
        """Fetch the last trade price for a token. ``None`` if the field is missing."""
        payload = self._get("/last-trade-price", params={"token_id": token_id})
        if not isinstance(payload, dict):
            raise ClobClientError(
                f"unexpected /last-trade-price response shape: {type(payload).__name__}"
            )
        return _safe_float(payload.get("price") or payload.get("last_trade_price"))

    def list_trades(
        self,
        *,
        market: str,
        limit: int = DEFAULT_TRADES_PAGE_SIZE,
        next_cursor: str | None = None,
    ) -> tuple[list[Trade], str | None]:
        """Return one page of trades plus the next-page cursor.

        The CLOB trades endpoint returns either a list of trades or an
        envelope dict with cursor. We support both shapes since
        Polymarket's docs and live behavior have varied across versions.
        """
        params: dict[str, Any] = {"market": market, "limit": limit}
        if next_cursor is not None:
            params["next_cursor"] = next_cursor
        payload = self._get("/trades", params=params)

        trades: list[dict[str, Any]]
        cursor: str | None = None
        if isinstance(payload, list):
            trades = [t for t in payload if isinstance(t, dict)]
        elif isinstance(payload, dict):
            data = payload.get("data") or payload.get("trades") or []
            trades = [t for t in data if isinstance(t, dict)]
            cursor_value = payload.get("next_cursor") or payload.get("nextCursor")
            if isinstance(cursor_value, str) and cursor_value:
                cursor = cursor_value
        else:
            raise ClobClientError(f"unexpected /trades response shape: {type(payload).__name__}")

        return [_parse_trade(t) for t in trades], cursor

    def iter_trades(
        self,
        *,
        market: str,
        page_size: int = DEFAULT_TRADES_PAGE_SIZE,
    ) -> Iterator[Trade]:
        """Iterate every trade for a market across paginated results."""
        cursor: str | None = None
        for _ in range(_MAX_TRADE_PAGES):
            page, cursor = self.list_trades(market=market, limit=page_size, next_cursor=cursor)
            yield from page
            if cursor is None:
                return
        raise ClobClientError(f"iter_trades: hit defensive page limit of {_MAX_TRADE_PAGES} pages")

    # -- internals ---------------------------------------------------------

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"

        def attempt() -> httpx.Response:
            self._bucket.acquire()
            return self._http.get(url, params=params)

        response = retry_with_backoff(
            attempt,
            max_retries=self._max_retries,
            base_seconds=self._backoff_base,
            max_seconds=self._backoff_max,
        )
        if response.status_code >= 400:
            raise ClobClientError(
                f"CLOB {path} returned HTTP {response.status_code}: {response.text[:200]!r}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ClobClientError(
                f"CLOB {path} returned non-JSON body: {response.text[:200]!r}"
            ) from exc


def _parse_orderbook(payload: dict[str, Any]) -> Orderbook:
    raw_bids = payload.get("bids") or []
    raw_asks = payload.get("asks") or []
    bids = tuple(_parse_level(b) for b in raw_bids if isinstance(b, dict))
    asks = tuple(_parse_level(a) for a in raw_asks if isinstance(a, dict))
    return Orderbook(
        market=str(payload.get("market") or ""),
        asset_id=str(payload.get("asset_id") or ""),
        timestamp=str(payload.get("timestamp") or ""),
        bids=bids,
        asks=asks,
        last_trade_price=_safe_float(payload.get("last_trade_price")),
        tick_size=_safe_float(payload.get("tick_size")),
        min_order_size=_safe_float(payload.get("min_order_size")),
        neg_risk=bool(payload.get("neg_risk", False)),
        raw=payload,
    )


def _parse_level(payload: dict[str, Any]) -> OrderbookLevel:
    return OrderbookLevel(
        price=_safe_float(payload.get("price")) or 0.0,
        size=_safe_float(payload.get("size")) or 0.0,
    )


def _parse_trade(payload: dict[str, Any]) -> Trade:
    side_value = payload.get("side")
    side: str | None = str(side_value) if side_value is not None else None
    timestamp_value = payload.get("trade_ts") or payload.get("timestamp") or payload.get("ts")
    trade_ts_seconds: int | None = None
    if timestamp_value is not None:
        try:
            trade_ts_seconds = int(timestamp_value)
        except (TypeError, ValueError):
            trade_ts_seconds = None
    return Trade(
        tx_hash=str(payload.get("tx_hash") or payload.get("transaction_hash") or ""),
        market=str(payload.get("market") or ""),
        asset_id=str(payload.get("asset_id") or payload.get("token_id") or ""),
        price=_safe_float(payload.get("price")) or 0.0,
        size=_safe_float(payload.get("size")) or 0.0,
        side=side,
        trade_ts_seconds=trade_ts_seconds,
        raw=payload,
    )


def _safe_float(value: object) -> float | None:
    """Best-effort float coercion. Returns ``None`` for non-numeric inputs."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
