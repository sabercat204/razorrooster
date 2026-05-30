"""Public Kalshi REST client (T-KSI-033; design §3.1).

Wraps the public Kalshi REST endpoints behind typed methods that
return the dataclasses in :mod:`.models`. The client does NOT touch
authenticated endpoints — there is no signing, no API key, no order
placement. The only headers it sends are ``User-Agent`` and
``Content-Type``.

Cross-cutting concerns:

- :mod:`.rate_limit` charges per-endpoint cost before each request.
- :mod:`.retry` wraps each request with jittered exponential backoff
  on retryable status codes and transport errors.
- :mod:`.user_agent` builds the underlying ``httpx.Client`` and the
  User-Agent string.

The client deliberately uses ``httpx.Client`` (sync) rather than the
async variant: the v1 connector calls REST endpoints from synchronous
sync code, and adopting async at this layer would force the rest of
the connector into the async world without a payoff. If a future round
needs concurrency, the limiter's thread safety lets multiple workers
share the same client.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx

from razor_rooster.kalshi_connector.client.models import (
    KalshiEvent,
    KalshiHistoricalCutoff,
    KalshiMarket,
    KalshiMarketType,
    KalshiOrderbook,
    KalshiOrderbookLevel,
    KalshiPaginatedResponse,
    KalshiSeries,
    KalshiStrikeType,
    KalshiTrade,
)
from razor_rooster.kalshi_connector.client.rate_limit import (
    DEFAULT_BUCKET_CAPACITY,
    TokenBucket,
)
from razor_rooster.kalshi_connector.client.retry import (
    DEFAULT_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_SECONDS,
    retry_with_backoff,
)
from razor_rooster.kalshi_connector.client.user_agent import (
    DEFAULT_TIMEOUT_SECONDS,
    build_httpx_client,
)
from razor_rooster.kalshi_connector.config.loader import KalshiConfig

logger = logging.getLogger(__name__)


_DEFAULT_BASE_URL: Final[str] = "https://external-api.kalshi.com/trade-api/v2"
_DEFAULT_PAGE_LIMIT: Final[int] = 100
# Hard ceiling on auto-pagination loops to prevent a buggy or hostile
# server from keeping us looping forever.
_MAX_PAGES: Final[int] = 10_000


class KalshiAPIError(RuntimeError):
    """Raised when a Kalshi REST call returns a non-OK response we cannot retry past."""

    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(f"Kalshi API error {status_code}: {message}")
        self.status_code = status_code


class KalshiRESTClient:
    """Synchronous public-REST client.

    All list endpoints support cursor-based pagination. The default
    page-fetch helpers return one page; pass ``paginate=True`` to
    accumulate every page into a single list.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        contact: str | None = None,
        bucket: TokenBucket | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_MAX_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"base_url must be http(s), got {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._bucket = (
            bucket
            if bucket is not None
            else TokenBucket(
                capacity=DEFAULT_BUCKET_CAPACITY,
                refill_per_second=DEFAULT_BUCKET_CAPACITY,
            )
        )
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else build_httpx_client(
                timeout_seconds=timeout_seconds,
                contact=contact,
            )
        )

    @classmethod
    def from_config(
        cls,
        config: KalshiConfig,
        *,
        bucket: TokenBucket | None = None,
        contact: str | None = None,
        client: httpx.Client | None = None,
    ) -> KalshiRESTClient:
        """Build a client from ``KalshiConfig``.

        The bucket is sourced from :meth:`TokenBucket.from_config` if not
        supplied; the retry budget is taken from the config's
        ``rate_limit`` section.
        """
        local_bucket = bucket if bucket is not None else TokenBucket.from_config(config)
        return cls(
            base_url=config.base_url,
            bucket=local_bucket,
            max_retries=config.rate_limit.max_retries,
            backoff_base_seconds=config.rate_limit.backoff_base_seconds,
            backoff_max_seconds=config.rate_limit.backoff_max_seconds,
            contact=contact,
            client=client,
        )

    # -- public series endpoints -------------------------------------------

    def list_series(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        category: str | None = None,
        paginate: bool = False,
    ) -> KalshiPaginatedResponse:
        """List series. Set ``paginate=True`` to accumulate all pages."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if category is not None:
            params["category"] = category
        if paginate:
            return self._paginate("/series", params, "series", _parse_series)
        payload = self._get("/series", params)
        items = [_parse_series(s) for s in payload.get("series", []) if isinstance(s, dict)]
        return KalshiPaginatedResponse(items=items, cursor=_extract_cursor(payload))

    def get_series(self, series_ticker: str) -> KalshiSeries:
        """Fetch a single series by ticker."""
        payload = self._get(f"/series/{series_ticker}", {})
        body = payload.get("series", payload)
        if not isinstance(body, dict):
            raise KalshiAPIError(
                status_code=502,
                message=f"unexpected /series/{series_ticker} body shape",
            )
        return _parse_series(body)

    # -- public events endpoints -------------------------------------------

    def list_events(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        paginate: bool = False,
    ) -> KalshiPaginatedResponse:
        """List events optionally filtered by series + status."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if series_ticker is not None:
            params["series_ticker"] = series_ticker
        if status is not None:
            params["status"] = status
        if paginate:
            return self._paginate("/events", params, "events", _parse_event)
        payload = self._get("/events", params)
        items = [_parse_event(e) for e in payload.get("events", []) if isinstance(e, dict)]
        return KalshiPaginatedResponse(items=items, cursor=_extract_cursor(payload))

    def get_event(self, event_ticker: str) -> KalshiEvent:
        payload = self._get(f"/events/{event_ticker}", {})
        body = payload.get("event", payload)
        if not isinstance(body, dict):
            raise KalshiAPIError(
                status_code=502,
                message=f"unexpected /events/{event_ticker} body shape",
            )
        return _parse_event(body)

    # -- public markets endpoints ------------------------------------------

    def list_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = "open",
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        paginate: bool = False,
    ) -> KalshiPaginatedResponse:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if series_ticker is not None:
            params["series_ticker"] = series_ticker
        if event_ticker is not None:
            params["event_ticker"] = event_ticker
        if status is not None:
            params["status"] = status
        if paginate:
            return self._paginate("/markets", params, "markets", _parse_market)
        payload = self._get("/markets", params)
        items = [_parse_market(m) for m in payload.get("markets", []) if isinstance(m, dict)]
        return KalshiPaginatedResponse(items=items, cursor=_extract_cursor(payload))

    def get_market(self, ticker: str) -> KalshiMarket:
        payload = self._get(f"/markets/{ticker}", {})
        body = payload.get("market", payload)
        if not isinstance(body, dict):
            raise KalshiAPIError(
                status_code=502,
                message=f"unexpected /markets/{ticker} body shape",
            )
        return _parse_market(body)

    def get_orderbook(
        self,
        ticker: str,
        *,
        depth: int = 10,
    ) -> KalshiOrderbook:
        """Fetch the YES-side orderbook and derive the NO side.

        Kalshi returns only YES-side depth. We build NO-side levels by
        mirroring around 1.00: ``no_bid = 1 - yes_ask`` and
        ``no_ask = 1 - yes_bid``.
        """
        payload = self._get(
            f"/markets/{ticker}/orderbook",
            {"depth": depth},
        )
        body = payload.get("orderbook", payload)
        if not isinstance(body, dict):
            raise KalshiAPIError(
                status_code=502,
                message=f"unexpected /markets/{ticker}/orderbook body shape",
            )
        snapshot_ts = _parse_iso_datetime(payload.get("snapshot_ts")) or datetime.now(tz=UTC)
        yes_levels = _parse_orderbook_side(body.get("yes"))
        no_levels = _derive_no_side(yes_levels)
        return KalshiOrderbook(
            ticker=ticker,
            snapshot_ts=snapshot_ts,
            yes_levels=yes_levels,
            no_levels=no_levels,
        )

    def get_market_trades(
        self,
        ticker: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        paginate: bool = False,
    ) -> KalshiPaginatedResponse:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker is not None:
            params["ticker"] = ticker
        if paginate:
            return self._paginate("/markets/trades", params, "trades", _parse_trade)
        payload = self._get("/markets/trades", params)
        items = [_parse_trade(t) for t in payload.get("trades", []) if isinstance(t, dict)]
        return KalshiPaginatedResponse(items=items, cursor=_extract_cursor(payload))

    # -- historical endpoints ----------------------------------------------

    def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        payload = self._get("/historical/cutoff", {})
        return KalshiHistoricalCutoff(
            market_settled_ts=_require_iso(payload, "market_settled_ts"),
            trades_created_ts=_require_iso(payload, "trades_created_ts"),
            orders_updated_ts=_require_iso(payload, "orders_updated_ts"),
            fetched_at=datetime.now(tz=UTC),
        )

    def get_historical_markets(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        paginate: bool = False,
    ) -> KalshiPaginatedResponse:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if paginate:
            return self._paginate("/historical/markets", params, "markets", _parse_market)
        payload = self._get("/historical/markets", params)
        items = [_parse_market(m) for m in payload.get("markets", []) if isinstance(m, dict)]
        return KalshiPaginatedResponse(items=items, cursor=_extract_cursor(payload))

    def get_historical_market(self, ticker: str) -> KalshiMarket:
        payload = self._get(f"/historical/markets/{ticker}", {})
        body = payload.get("market", payload)
        if not isinstance(body, dict):
            raise KalshiAPIError(
                status_code=502,
                message=f"unexpected /historical/markets/{ticker} body shape",
            )
        return _parse_market(body)

    def get_historical_trades(
        self,
        *,
        ticker: str | None = None,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        paginate: bool = False,
    ) -> KalshiPaginatedResponse:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker is not None:
            params["ticker"] = ticker
        if paginate:
            return self._paginate("/historical/trades", params, "trades", _parse_trade)
        payload = self._get("/historical/trades", params)
        items = [_parse_trade(t) for t in payload.get("trades", []) if isinstance(t, dict)]
        return KalshiPaginatedResponse(items=items, cursor=_extract_cursor(payload))

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue one GET. Charges the limiter, applies retry, parses JSON."""
        url = self._base_url + path

        def _do() -> httpx.Response:
            self._bucket.acquire_for_endpoint(path)
            return self._client.get(url, params=params)

        response = retry_with_backoff(
            _do,
            max_retries=self._max_retries,
            base_seconds=self._backoff_base_seconds,
            max_seconds=self._backoff_max_seconds,
            bucket=self._bucket,
        )

        if response.status_code >= 400:
            raise KalshiAPIError(
                status_code=response.status_code,
                message=response.text or "(empty body)",
            )
        try:
            decoded = response.json()
        except ValueError as exc:
            raise KalshiAPIError(
                status_code=502,
                message=f"could not decode JSON from {path}: {exc}",
            ) from exc
        if not isinstance(decoded, dict):
            raise KalshiAPIError(
                status_code=502,
                message=f"unexpected non-object response from {path}",
            )
        return cast("dict[str, Any]", decoded)

    def _paginate(
        self,
        path: str,
        params: dict[str, Any],
        items_key: str,
        parse_one: Any,
    ) -> KalshiPaginatedResponse:
        """Drain pagination cursor until exhausted."""
        all_items: list[Any] = []
        cursor: str | None = params.get("cursor")
        page_count = 0
        while True:
            page_params = dict(params)
            if cursor is not None:
                page_params["cursor"] = cursor
            payload = self._get(path, page_params)
            raw_list = payload.get(items_key)
            if isinstance(raw_list, list):
                for entry in raw_list:
                    if isinstance(entry, dict):
                        all_items.append(parse_one(entry))
            cursor = _extract_cursor(payload)
            page_count += 1
            if cursor is None or page_count >= _MAX_PAGES:
                break
        return KalshiPaginatedResponse(items=all_items, cursor=None)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx client if we own it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> KalshiRESTClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[Any]:
        # Defensive: prevent callers from accidentally iterating the client.
        raise TypeError("KalshiRESTClient is not iterable; call list_* methods explicitly")


# -- parsing helpers --------------------------------------------------------


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _require_iso(payload: dict[str, Any], key: str) -> datetime:
    value = _parse_iso_datetime(payload.get(key))
    if value is None:
        raise KalshiAPIError(
            status_code=502,
            message=f"required ISO datetime field {key!r} missing or unparseable",
        )
    return value


def _extract_cursor(payload: dict[str, Any]) -> str | None:
    cursor = payload.get("cursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        if value.strip().lower() in ("true", "1", "yes"):
            return True
        if value.strip().lower() in ("false", "0", "no"):
            return False
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _parse_series(payload: dict[str, Any]) -> KalshiSeries:
    raw_tags = payload.get("tags") or []
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, list):
        tags = tuple(str(t) for t in raw_tags if isinstance(t, str | int | float))
    return KalshiSeries(
        series_ticker=str(payload.get("ticker") or payload.get("series_ticker") or ""),
        title=str(payload.get("title") or ""),
        category=_coerce_str(payload.get("category")),
        frequency=_coerce_str(payload.get("frequency")),
        tags=tags,
        settlement_source=_coerce_str(payload.get("settlement_source")),
        contract_url=_coerce_str(payload.get("contract_url")),
        created_at=_parse_iso_datetime(payload.get("created_at")),
        last_updated_at=_parse_iso_datetime(payload.get("last_updated_at")),
    )


def _parse_event(payload: dict[str, Any]) -> KalshiEvent:
    return KalshiEvent(
        event_ticker=str(payload.get("event_ticker") or payload.get("ticker") or ""),
        series_ticker=str(payload.get("series_ticker") or ""),
        title=str(payload.get("title") or ""),
        sub_title=_coerce_str(payload.get("sub_title")),
        category=_coerce_str(payload.get("category")),
        mutually_exclusive=bool(payload.get("mutually_exclusive") or False),
        expected_expiration_time=_parse_iso_datetime(payload.get("expected_expiration_time")),
        strike_period=_coerce_str(payload.get("strike_period")),
        status=str(payload.get("status") or "unknown"),
        created_at=_parse_iso_datetime(payload.get("created_at")),
        last_updated_at=_parse_iso_datetime(payload.get("last_updated_at")),
    )


def _parse_market(payload: dict[str, Any]) -> KalshiMarket:
    market_type_raw = str(payload.get("market_type") or "binary").lower()
    if market_type_raw not in ("binary", "scalar", "categorical"):
        # Fall back to binary so unknown future variants still persist
        # (downstream filtering can elide them via this typing).
        market_type_raw = "binary"
    market_type = cast("KalshiMarketType", market_type_raw)

    strike_type_raw = payload.get("strike_type")
    strike_type: KalshiStrikeType | None = None
    if isinstance(strike_type_raw, str):
        normalized = strike_type_raw.strip().lower()
        if normalized in ("above", "below", "between", "structured", "unstructured"):
            strike_type = cast("KalshiStrikeType", normalized)

    return KalshiMarket(
        ticker=str(payload.get("ticker") or ""),
        event_ticker=str(payload.get("event_ticker") or ""),
        series_ticker=str(payload.get("series_ticker") or ""),
        title=str(payload.get("title") or ""),
        sub_title=_coerce_str(payload.get("sub_title")),
        market_type=market_type,
        strike_type=strike_type,
        floor_strike=_coerce_float(payload.get("floor_strike")),
        cap_strike=_coerce_float(payload.get("cap_strike")),
        open_time=_parse_iso_datetime(payload.get("open_time")),
        close_time=_parse_iso_datetime(payload.get("close_time")),
        expiration_time=_parse_iso_datetime(payload.get("expiration_time")),
        expected_expiration_time=_parse_iso_datetime(payload.get("expected_expiration_time")),
        latest_expiration_time=_parse_iso_datetime(payload.get("latest_expiration_time")),
        settlement_timer_seconds=_coerce_int(payload.get("settlement_timer_seconds")),
        status=str(payload.get("status") or "unknown"),
        yes_sub_title=_coerce_str(payload.get("yes_sub_title")),
        no_sub_title=_coerce_str(payload.get("no_sub_title")),
        result=_coerce_str(payload.get("result")),
        can_close_early=_coerce_bool(payload.get("can_close_early")),
        expiration_value=_coerce_float(payload.get("expiration_value")),
        category=_coerce_str(payload.get("category")),
        risk_limit_cents=_coerce_int(payload.get("risk_limit_cents")),
        notional_value=_coerce_float(payload.get("notional_value")),
        tick_size=_coerce_float(payload.get("tick_size")),
        last_price_dollars=_coerce_float(payload.get("last_price")),
        previous_yes_bid_dollars=_coerce_float(payload.get("previous_yes_bid")),
        previous_yes_ask_dollars=_coerce_float(payload.get("previous_yes_ask")),
        previous_price_dollars=_coerce_float(payload.get("previous_price")),
        volume_24h=_coerce_float(payload.get("volume_24h")),
        volume=_coerce_float(payload.get("volume")),
        liquidity=_coerce_float(payload.get("liquidity")),
        open_interest=_coerce_float(payload.get("open_interest")),
        created_at=_parse_iso_datetime(payload.get("created_at")),
        last_updated_at=_parse_iso_datetime(payload.get("last_updated_at")),
        raw=dict(payload),
    )


def _parse_trade(payload: dict[str, Any]) -> KalshiTrade:
    yes_price = _coerce_float(payload.get("yes_price")) or 0.0
    no_price = _coerce_float(payload.get("no_price"))
    if no_price is None:
        no_price = max(0.0, 1.0 - yes_price)
    return KalshiTrade(
        trade_id=str(payload.get("trade_id") or ""),
        ticker=str(payload.get("ticker") or ""),
        created_time=_parse_iso_datetime(payload.get("created_time")) or datetime.now(tz=UTC),
        yes_price_dollars=yes_price,
        no_price_dollars=no_price,
        count=_coerce_float(payload.get("count")) or 0.0,
        taker_side=_coerce_str(payload.get("taker_side")),
    )


def _parse_orderbook_side(raw: Any) -> tuple[KalshiOrderbookLevel, ...]:
    """Parse an orderbook-side list of [price, count] tuples or dicts."""
    if not isinstance(raw, list):
        return ()
    levels: list[KalshiOrderbookLevel] = []
    for entry in raw:
        if isinstance(entry, list | tuple) and len(entry) >= 2:
            price = _coerce_float(entry[0])
            count = _coerce_float(entry[1])
            if price is not None and count is not None:
                levels.append(KalshiOrderbookLevel(price_dollars=price, count=count))
        elif isinstance(entry, dict):
            price = _coerce_float(entry.get("price"))
            count = _coerce_float(entry.get("count"))
            if price is not None and count is not None:
                levels.append(KalshiOrderbookLevel(price_dollars=price, count=count))
    return tuple(levels)


def _derive_no_side(
    yes_levels: tuple[KalshiOrderbookLevel, ...],
) -> tuple[KalshiOrderbookLevel, ...]:
    """Mirror YES asks to NO bids: ``no_bid_price = 1 - yes_ask_price``.

    Kalshi's REST returns YES depth only. The raw YES list mixes bids
    and asks; this connector treats every entry as an ask-side level
    and mirrors them. Sync code that needs both sides re-shapes
    explicitly.
    """
    return tuple(
        KalshiOrderbookLevel(
            price_dollars=max(0.0, 1.0 - level.price_dollars),
            count=level.count,
        )
        for level in yes_levels
    )


__all__ = [
    "KalshiAPIError",
    "KalshiRESTClient",
]
