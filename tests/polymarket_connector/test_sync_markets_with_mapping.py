"""T-PMC-051 integration — markets sync invokes the heuristic mapper."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.client.gamma import (
    GAMMA_BASE_URL,
    GammaClient,
)
from razor_rooster.polymarket_connector.client.rate_limit import (
    TokenBucket,
    reset_shared_bucket,
)
from razor_rooster.polymarket_connector.config.loader import (
    SectorKeywordsConfig,
)
from razor_rooster.polymarket_connector.mapping.sector_overrides import (
    MANUAL_CONFIDENCE,
    get_mapping,
    set_override,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)
from razor_rooster.polymarket_connector.sync.markets import sync_markets


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_sync_with_mapping.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def keywords() -> SectorKeywordsConfig:
    return SectorKeywordsConfig(
        version=1,
        sectors={
            "public_health": ["pandemic", "vaccine", "WHO"],
            "geopolitical": ["election", "war", "ceasefire"],
            "regulatory": ["FDA", "Congress", "Supreme Court"],
            "commodity": ["oil", "OPEC"],
            "climate": ["hurricane", "drought"],
            "infrastructure_energy": ["grid", "blackout"],
        },
    )


def _market_payload(
    condition_id: str,
    *,
    question: str,
    active: bool = True,
    closed: bool = False,
) -> dict[str, Any]:
    return {
        "conditionId": condition_id,
        "slug": f"market-{condition_id[2:6]}",
        "question": question,
        "active": active,
        "closed": closed,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [f"{condition_id}-yes", f"{condition_id}-no"],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-05-14T08:00:00Z",
    }


def _gamma_handler(
    payloads: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    p = payloads or []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/markets":
            return httpx.Response(404)
        active_param = request.url.params.get("active")
        if active_param == "true":
            return httpx.Response(200, json=p)
        return httpx.Response(200, json=[])

    return handler


def _build_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GammaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return GammaClient(http_client=http, bucket=bucket, max_retries=0)


def test_sync_markets_writes_inferred_mapping(
    store: DuckDBStore,
    keywords: SectorKeywordsConfig,
) -> None:
    payloads = [
        _market_payload("0xph", question="Will the WHO declare a pandemic?"),
        _market_payload("0xcom", question="Will OPEC cut oil output?"),
    ]
    handler = _gamma_handler(payloads)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client, sector_keywords=keywords)

    assert report.mappings_upserted == 2
    assert report.mappings_skipped_manual == 0

    with store.connection() as conn:
        ph = get_mapping(conn, "0xph")
        com = get_mapping(conn, "0xcom")

    assert ph is not None and ph.razor_sector == "public_health"
    assert com is not None and com.razor_sector == "commodity"


def test_sync_markets_preserves_manual_override(
    store: DuckDBStore,
    keywords: SectorKeywordsConfig,
) -> None:
    """An existing manual override is not overwritten by the heuristic."""
    with store.connection() as conn:
        set_override(conn, condition_id="0xabc", razor_sector="regulatory")

    payloads = [_market_payload("0xabc", question="Will the WHO declare a pandemic?")]
    handler = _gamma_handler(payloads)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client, sector_keywords=keywords)

    assert report.mappings_upserted == 0
    assert report.mappings_skipped_manual == 1

    with store.connection() as conn:
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.razor_sector == "regulatory"
    assert row.confidence == MANUAL_CONFIDENCE


def test_sync_markets_no_keywords_skips_mapping(store: DuckDBStore) -> None:
    """Without sector_keywords, the sync works exactly as before."""
    payloads = [_market_payload("0xph", question="Will the WHO declare a pandemic?")]
    handler = _gamma_handler(payloads)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.mappings_upserted == 0
    assert report.mappings_skipped_manual == 0

    with store.connection() as conn:
        row = get_mapping(conn, "0xph")
    assert row is None


def test_sync_markets_unmappable_market_records_null(
    store: DuckDBStore,
    keywords: SectorKeywordsConfig,
) -> None:
    """A market with no keyword hits gets a NULL mapping (pending review)."""
    payloads = [_market_payload("0xsports", question="Will the Lakers win?")]
    handler = _gamma_handler(payloads)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client, sector_keywords=keywords)

    assert report.mappings_upserted == 1

    with store.connection() as conn:
        row = get_mapping(conn, "0xsports")

    assert row is not None
    assert row.razor_sector is None
    assert row.confidence == "inferred"


def test_sync_markets_revision_refreshes_mapping(
    store: DuckDBStore,
    keywords: SectorKeywordsConfig,
) -> None:
    """When a market's question changes, the mapping is recomputed."""
    payload_v1 = _market_payload("0xabc", question="Will OPEC act?")
    payload_v2 = _market_payload("0xabc", question="Will the WHO act on the pandemic?")
    payload_v2["updatedAt"] = "2026-05-15T08:00:00Z"

    handler1 = _gamma_handler([payload_v1])
    when_1 = datetime(2026, 5, 14, tzinfo=UTC)
    when_2 = datetime(2026, 5, 15, tzinfo=UTC)

    with _build_client(handler1) as client:
        sync_markets(store, client=client, sector_keywords=keywords, now=when_1)

    handler2 = _gamma_handler([payload_v2])
    with _build_client(handler2) as client:
        sync_markets(store, client=client, sector_keywords=keywords, now=when_2)

    with store.connection() as conn:
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.razor_sector == "public_health"
