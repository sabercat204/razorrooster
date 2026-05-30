"""Polymarket Gamma API client (T-PMC-033).

Wraps ``https://gamma-api.polymarket.com`` for market metadata, event
groupings, and resolution history. The Gamma API is public — no
authentication, no API key, no wallet required.

Endpoints used:

- ``GET /markets`` — paginated market discovery with active/closed filters
  (used for daily metadata sync and resolution backfill).
- ``GET /markets/slug/<slug>`` — single market lookup by slug.
- ``GET /events`` — event-grouped market discovery.
- ``GET /events/<event_id>`` — single event lookup by ID.

The client is HTTP-only and synchronous. Layering: every HTTP call goes
through the shared token bucket (T-PMC-030) to honour the 50% headroom
target; transport / retryable status codes go through
``retry_with_backoff`` (T-PMC-031); the User-Agent comes from the shared
factory (T-PMC-032).

Raw response payloads are preserved verbatim by the caller so downstream
sync logic can put them in ``source_payload_json``. This client returns
typed dataclasses that include the ``raw`` dict alongside the parsed
fields, so callers can choose either surface.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

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


GAMMA_BASE_URL: Final[str] = "https://gamma-api.polymarket.com"

# Default page size for paginated endpoints. 100 is the largest size
# Polymarket's docs explicitly demonstrate; some endpoints accept larger
# but the network round-trip cost is modest at 100 and the upper bound
# isn't documented uniformly.
DEFAULT_PAGE_SIZE: Final[int] = 100

# Hard pagination ceiling so a runaway loop can't drain the rate budget
# on a buggy server response. At 100 markets/page, 100k pages = 10M
# rows — far above any plausible Polymarket scale.
_MAX_PAGES: Final[int] = 100_000


@dataclass(frozen=True, slots=True)
class GammaMarket:
    """A single market record from the Gamma /markets endpoint.

    Only the fields the connector cares about are parsed out as named
    attributes; the verbatim payload is in ``raw`` for storage in
    ``source_payload_json``.
    """

    condition_id: str
    slug: str
    question: str
    active: bool
    closed: bool
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GammaEvent:
    """A single event record from the Gamma /events endpoint."""

    event_id: str
    slug: str
    title: str
    raw: dict[str, Any]


class GammaClientError(RuntimeError):
    """Base class for Gamma client failures that survived the retry budget."""


class GammaClient:
    """Synchronous client for Polymarket's Gamma API.

    Construct once per process; reuse across syncs. The client owns its
    httpx.Client lifecycle when constructed via the default factory; pass
    an explicit ``http_client`` to reuse an outer-scoped session.
    """

    def __init__(
        self,
        *,
        base_url: str = GAMMA_BASE_URL,
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

    def __enter__(self) -> GammaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying http client if we own it."""
        if self._owns_client:
            self._http.close()

    # -- public methods ----------------------------------------------------

    def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[GammaMarket]:
        """Return one page of markets.

        Use :meth:`iter_markets` for full discovery; this method is the
        single-page primitive.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = "true" if active else "false"
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        payload = self._get("/markets", params=params)
        if not isinstance(payload, list):
            raise GammaClientError(
                f"unexpected /markets response shape: expected list, got {type(payload).__name__}"
            )
        return [_parse_market(item) for item in payload if isinstance(item, dict)]

    def iter_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        start_offset: int = 0,
    ) -> Iterator[GammaMarket]:
        """Iterate every market across paginated results.

        Stops when a page returns fewer than ``page_size`` rows. The
        ``_MAX_PAGES`` guard exists as a defensive ceiling; under normal
        operation it is never reached.
        """
        offset = start_offset
        for _ in range(_MAX_PAGES):
            page = self.list_markets(
                active=active,
                closed=closed,
                limit=page_size,
                offset=offset,
            )
            yield from page
            if len(page) < page_size:
                return
            offset += page_size
        raise GammaClientError(
            f"iter_markets: hit defensive page limit of {_MAX_PAGES} pages; "
            "this almost certainly indicates a server-side pagination bug"
        )

    def list_resolved(
        self,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[GammaMarket]:
        """Return one page of resolved (closed) markets.

        Equivalent to ``list_markets(active=False, closed=True)`` but
        named explicitly so callers in the resolution-backfill path
        document their intent.
        """
        return self.list_markets(active=None, closed=True, limit=limit, offset=offset)

    def iter_resolved(
        self,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        start_offset: int = 0,
    ) -> Iterator[GammaMarket]:
        """Iterate every resolved market across paginated results."""
        offset = start_offset
        for _ in range(_MAX_PAGES):
            page = self.list_resolved(limit=page_size, offset=offset)
            yield from page
            if len(page) < page_size:
                return
            offset += page_size
        raise GammaClientError(f"iter_resolved: hit defensive page limit of {_MAX_PAGES} pages")

    def get_market_by_slug(self, slug: str) -> GammaMarket | None:
        """Fetch a single market by slug. Returns ``None`` if absent."""
        try:
            payload = self._get(f"/markets/slug/{slug}")
        except GammaClientError as exc:
            if "404" in str(exc):
                return None
            raise
        if isinstance(payload, dict):
            return _parse_market(payload)
        if isinstance(payload, list):
            if not payload:
                return None
            return _parse_market(payload[0])
        raise GammaClientError(f"unexpected /markets/slug response shape: {type(payload).__name__}")

    def list_events(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[GammaEvent]:
        """Return one page of events."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = "true" if active else "false"
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        payload = self._get("/events", params=params)
        if not isinstance(payload, list):
            raise GammaClientError(f"unexpected /events response shape: {type(payload).__name__}")
        return [_parse_event(item) for item in payload if isinstance(item, dict)]

    # -- internals ---------------------------------------------------------

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Acquire a token, GET the URL with retries, parse JSON."""
        url = f"{self._base_url}{path}"

        def attempt() -> httpx.Response:
            self._bucket.acquire()
            response = self._http.get(url, params=params)
            return response

        response = retry_with_backoff(
            attempt,
            max_retries=self._max_retries,
            base_seconds=self._backoff_base,
            max_seconds=self._backoff_max,
        )
        if response.status_code >= 400:
            raise GammaClientError(
                f"Gamma {path} returned HTTP {response.status_code}: {response.text[:200]!r}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GammaClientError(
                f"Gamma {path} returned non-JSON body: {response.text[:200]!r}"
            ) from exc


def _parse_market(payload: dict[str, Any]) -> GammaMarket:
    """Build a typed GammaMarket from a raw response dict.

    Missing fields default to safe values rather than raising; the
    connector's normalization layer is allowed to filter on
    ``raw`` whenever it needs richer fields.
    """
    return GammaMarket(
        condition_id=str(payload.get("conditionId") or payload.get("condition_id") or ""),
        slug=str(payload.get("slug") or ""),
        question=str(payload.get("question") or ""),
        active=bool(payload.get("active", False)),
        closed=bool(payload.get("closed", False)),
        raw=payload,
    )


def _parse_event(payload: dict[str, Any]) -> GammaEvent:
    return GammaEvent(
        event_id=str(payload.get("id") or payload.get("eventId") or ""),
        slug=str(payload.get("slug") or ""),
        title=str(payload.get("title") or payload.get("question") or ""),
        raw=payload,
    )
