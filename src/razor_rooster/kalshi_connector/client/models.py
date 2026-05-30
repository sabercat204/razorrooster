"""Typed dataclasses for Kalshi REST responses (T-KSI-033).

Each dataclass mirrors the columns the corresponding sync code persists.
The raw payload is preserved at the call site (sync code stamps it
into ``source_payload_json``) — these dataclasses are the *parsed*
projection the rest of the connector consumes.

Per OQ-KSI-003: all four market types (binary, scalar, categorical,
plus binary strike-variants) round-trip through this typing. The
``market_type`` and ``strike_type`` fields are the discriminators.

Per design §3.3: orderbook snapshots are YES-side-only on the wire.
:class:`KalshiOrderbook` keeps separate ``yes_levels`` / ``no_levels``
fields; the REST client populates ``no_levels`` by deriving from the
YES asks (``no_bid = 1 - yes_ask``) at parse time so the rest of the
codebase sees a symmetric snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

KalshiMarketType = Literal["binary", "scalar", "categorical"]
KalshiStrikeType = Literal["above", "below", "between", "structured", "unstructured"]
KalshiSide = Literal["yes", "no"]


@dataclass(frozen=True, slots=True)
class KalshiSeries:
    """One row of ``kalshi_series`` (REST: ``/series`` family)."""

    series_ticker: str
    title: str
    category: str | None = None
    frequency: str | None = None
    tags: tuple[str, ...] = ()
    settlement_source: str | None = None
    contract_url: str | None = None
    created_at: datetime | None = None
    last_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KalshiEvent:
    """One row of ``kalshi_events`` (REST: ``/events`` family)."""

    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str | None
    category: str | None
    mutually_exclusive: bool
    expected_expiration_time: datetime | None
    strike_period: str | None
    status: str
    created_at: datetime | None = None
    last_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KalshiMarket:
    """One row of ``kalshi_markets`` (REST: ``/markets`` family).

    All four market types round-trip through this dataclass.
    Strike-variant fields (``floor_strike``, ``cap_strike``,
    ``strike_type``) are populated for binary scalar markets and
    ``None`` otherwise.
    """

    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str | None
    market_type: KalshiMarketType
    strike_type: KalshiStrikeType | None
    floor_strike: float | None
    cap_strike: float | None
    open_time: datetime | None
    close_time: datetime | None
    expiration_time: datetime | None
    expected_expiration_time: datetime | None
    latest_expiration_time: datetime | None
    settlement_timer_seconds: int | None
    status: str
    yes_sub_title: str | None
    no_sub_title: str | None
    result: str | None
    can_close_early: bool | None
    expiration_value: float | None
    category: str | None
    risk_limit_cents: int | None
    notional_value: float | None
    tick_size: float | None
    last_price_dollars: float | None
    previous_yes_bid_dollars: float | None
    previous_yes_ask_dollars: float | None
    previous_price_dollars: float | None
    volume_24h: float | None
    volume: float | None
    liquidity: float | None
    open_interest: float | None
    created_at: datetime | None = None
    last_updated_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KalshiOrderbookLevel:
    """One depth level on one side of an orderbook."""

    price_dollars: float
    count: float


@dataclass(frozen=True, slots=True)
class KalshiOrderbook:
    """Parsed orderbook snapshot.

    Kalshi's API returns YES-side-only depth. The REST client derives
    NO-side levels by mirroring the YES asks: ``no_bid = 1 - yes_ask``,
    and ``no_ask = 1 - yes_bid``. The derivation is faithful within
    floating-point precision and avoids storing redundant data.
    """

    ticker: str
    snapshot_ts: datetime
    yes_levels: tuple[KalshiOrderbookLevel, ...] = ()
    no_levels: tuple[KalshiOrderbookLevel, ...] = ()


@dataclass(frozen=True, slots=True)
class KalshiTrade:
    """One row of ``kalshi_trades`` (REST: ``/markets/trades``)."""

    trade_id: str
    ticker: str
    created_time: datetime
    yes_price_dollars: float
    no_price_dollars: float
    count: float
    taker_side: str | None = None


@dataclass(frozen=True, slots=True)
class KalshiHistoricalCutoff:
    """Single-row state from ``/historical/cutoff``."""

    market_settled_ts: datetime
    trades_created_ts: datetime
    orders_updated_ts: datetime
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class KalshiPaginatedResponse:
    """Pagination envelope shared by all ``/list`` endpoints.

    Kalshi paginates with a string cursor returned in the response
    body. Callers either request a single page (the default) or pass
    ``paginate=True`` to the list helpers and get the accumulated
    ``items`` with ``cursor=None``.
    """

    items: Sequence[Any]
    cursor: str | None


__all__ = [
    "KalshiEvent",
    "KalshiHistoricalCutoff",
    "KalshiMarket",
    "KalshiMarketType",
    "KalshiOrderbook",
    "KalshiOrderbookLevel",
    "KalshiPaginatedResponse",
    "KalshiSeries",
    "KalshiSide",
    "KalshiStrikeType",
    "KalshiTrade",
]
