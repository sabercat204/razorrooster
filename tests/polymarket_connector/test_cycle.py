"""T-PMC-061 — Polymarket cycle integration tests."""

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
from razor_rooster.polymarket_connector.client.clob_public import (
    CLOB_BASE_URL,
    ClobPublicClient,
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
    PolymarketConfig,
    SectorKeywordsConfig,
)
from razor_rooster.polymarket_connector.cycle import (
    POLYMARKET_CYCLE_SOURCE_ID,
    PolymarketCycleReport,
    cycle_report_to_connector_outcome,
    run_polymarket_cycle,
    stage_summary_lines,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_cycle.duckdb"
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
            "geopolitical": ["election", "war"],
            "regulatory": ["FDA", "Congress"],
            "commodity": ["oil", "OPEC"],
            "climate": ["hurricane"],
            "infrastructure_energy": ["grid"],
        },
    )


@pytest.fixture
def config(tmp_path: Path) -> PolymarketConfig:
    """A minimal config with no watched markets and a non-existent keywords file.

    The cycle's keywords-load failure path is exercised by another test;
    here we want the success path so we pass keywords explicitly.
    """
    return PolymarketConfig.model_validate(
        {
            "version": 1,
            "sync": {
                "markets": {"cadence": "daily", "time_of_day": "08:30"},
                "prices": {
                    "default_cadence": "hourly",
                    "minimum_interval_seconds": 60,
                    "watched_markets": [],
                },
                "resolutions": {"cadence": "daily", "time_of_day": "08:45"},
                "trades": {"cadence": "daily", "time_of_day": "09:00"},
            },
            "rate_limit": {
                "bucket_capacity": 50,
                "refill_per_second": 50.0,
                "backoff_base_seconds": 1.0,
                "backoff_max_seconds": 60.0,
                "max_retries": 5,
            },
            "freshness": {
                "markets_threshold_seconds": 172_800,
                "prices_threshold_seconds": 21_600,
                "resolutions_threshold_seconds": 172_800,
            },
            "sector_mapping": {
                "heuristic_version": 1,
                "keywords_file": str(tmp_path / "missing-keywords.yaml"),
            },
        }
    )


def _market_payload(condition_id: str, *, question: str = "Will it happen?") -> dict[str, Any]:
    return {
        "conditionId": condition_id,
        "slug": f"market-{condition_id[2:6]}",
        "question": question,
        "active": True,
        "closed": False,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [f"{condition_id}-yes", f"{condition_id}-no"],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-05-14T08:00:00Z",
    }


def _gamma_handler(
    *,
    active: list[dict[str, Any]] | None = None,
    closed: list[dict[str, Any]] | None = None,
    fail: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    a = active or []
    c = closed or []

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, text="boom")
        if request.url.path != "/markets":
            return httpx.Response(404)
        active_param = request.url.params.get("active")
        closed_param = request.url.params.get("closed")
        if active_param == "true" and closed_param == "false":
            return httpx.Response(200, json=a)
        if closed_param == "true":
            return httpx.Response(200, json=c)
        return httpx.Response(200, json=[])

    return handler


def _clob_handler(*, fail: bool = False) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, text="clob boom")
        if request.url.path == "/book":
            token = request.url.params.get("token_id", "")
            return httpx.Response(
                200,
                json={
                    "market": "0xany",
                    "asset_id": token,
                    "timestamp": "1700000000",
                    "bids": [{"price": "0.45", "size": "100"}],
                    "asks": [{"price": "0.46", "size": "150"}],
                    "neg_risk": False,
                    "last_trade_price": "0.45",
                    "tick_size": "0.01",
                    "min_order_size": "1",
                },
            )
        if request.url.path == "/trades":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return handler


def _build_clients(
    *,
    gamma_handler: Callable[[httpx.Request], httpx.Response],
    clob_handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[GammaClient, ClobPublicClient]:
    gamma_transport = httpx.MockTransport(gamma_handler)
    gamma_http = httpx.Client(transport=gamma_transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    gamma = GammaClient(http_client=gamma_http, bucket=bucket, max_retries=0)

    clob_transport = httpx.MockTransport(clob_handler)
    clob_http = httpx.Client(transport=clob_transport, base_url=CLOB_BASE_URL)
    clob = ClobPublicClient(http_client=clob_http, bucket=bucket, max_retries=0)
    return gamma, clob


# -------------------------------------------------------------------------
# happy path
# -------------------------------------------------------------------------
def test_run_cycle_executes_all_stages(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    assert isinstance(report, PolymarketCycleReport)
    assert report.markets is not None
    assert report.markets.markets_inserted == 1
    assert report.prices is not None
    assert report.prices.snapshots_inserted == 2
    assert report.resolutions is not None
    # No watched markets configured → trades stage skipped (None).
    assert report.trades is None
    assert report.errors == []


def test_run_cycle_skips_trades_when_no_watched_markets(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    assert report.trades is None


# -------------------------------------------------------------------------
# failure isolation
# -------------------------------------------------------------------------
def test_run_cycle_isolates_markets_failure(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    """Even when Gamma fails, the prices stage still runs (it depends on local store).

    With a fresh store and no markets persisted, prices has nothing to
    do but completes cleanly without errors.
    """
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(fail=True),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    # markets stage records its own internal errors and returns a report
    # (it doesn't raise) — see sync/markets.py contract. resolutions
    # also records its own errors. Cycle-level errors stay empty unless
    # an exception escapes.
    assert report.markets is not None
    assert report.markets.errors  # markets stage captured its own error
    # The cycle's top-level errors stay empty because the stages handle
    # their own failures internally.
    assert report.errors == []


def test_run_cycle_records_completion_timing(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(),
        clob_handler=_clob_handler(),
    )
    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
            now=when,
        )
    finally:
        gamma.close()
        clob.close()

    assert report.started_at == when
    assert report.completed_at is not None
    assert report.duration_seconds is not None


def test_run_cycle_handles_missing_keywords_file_gracefully(
    store: DuckDBStore,
    config: PolymarketConfig,
) -> None:
    """When keywords aren't supplied AND the config's keywords_file doesn't exist,
    the cycle records the load error and continues without sector mapping.
    """
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=None,  # force the load path
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    # The keywords load failure is recorded.
    assert any(err.startswith("keywords_load") for err in report.errors)
    # But the markets sync still ran — just without mapping.
    assert report.markets is not None
    assert report.markets.markets_inserted == 1


def test_run_cycle_with_watched_markets_pulls_trades(
    store: DuckDBStore,
    keywords: SectorKeywordsConfig,
) -> None:
    """Configured watched markets trigger the trades stage."""
    config_with_watched = PolymarketConfig.model_validate(
        {
            "version": 1,
            "sync": {
                "prices": {
                    "default_cadence": "hourly",
                    "minimum_interval_seconds": 60,
                    "watched_markets": ["0xabc"],
                },
            },
            "sector_mapping": {
                "heuristic_version": 1,
                "keywords_file": "config/sector_keywords.yaml",
            },
        }
    )

    # Seed the market so the trades stage has metadata.
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config_with_watched,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    assert report.trades is not None
    assert report.trades.markets_evaluated == 1


# -------------------------------------------------------------------------
# cycle_report_to_connector_outcome projection
# -------------------------------------------------------------------------
def test_outcome_projection_ok_status_when_no_errors(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    outcome = cycle_report_to_connector_outcome(report)
    assert outcome.source_id == POLYMARKET_CYCLE_SOURCE_ID
    assert outcome.status == "ok"
    # 1 market inserted + 2 price snapshots = 3 records.
    assert outcome.records_ingested == 3
    assert outcome.errors == []


def test_outcome_projection_failed_status_when_all_stages_fail(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    """When every stage records errors, the projection is 'failed'."""
    # Simulate a cycle where the keywords-load failed and we craft errors:
    report = PolymarketCycleReport(started_at=datetime.now(tz=UTC))
    report.errors.append("markets: ConnectionError: total network failure")
    outcome = cycle_report_to_connector_outcome(report)
    assert outcome.status == "failed"
    assert outcome.errors


def test_outcome_projection_partial_status(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    """When the cycle has both successes and errors, the projection is 'partial'."""
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=None,  # forces a keywords_load error
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    outcome = cycle_report_to_connector_outcome(report)
    # Markets sync succeeded; keywords_load failed → partial status.
    assert outcome.status == "partial"
    assert any("keywords_load" in str(e["message"]) for e in outcome.errors)


# -------------------------------------------------------------------------
# stage_summary_lines
# -------------------------------------------------------------------------
def test_stage_summary_lines_includes_all_active_stages(
    store: DuckDBStore,
    config: PolymarketConfig,
    keywords: SectorKeywordsConfig,
) -> None:
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=keywords,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    lines = list(stage_summary_lines(report))
    text = "\n".join(lines)
    assert "markets:" in text
    assert "prices:" in text
    assert "resolutions:" in text
    assert "trades:" in text  # the "no watched" placeholder line


def test_stage_summary_lines_renders_errors(
    store: DuckDBStore,
    config: PolymarketConfig,
) -> None:
    gamma, clob = _build_clients(
        gamma_handler=_gamma_handler(active=[_market_payload("0xabc")]),
        clob_handler=_clob_handler(),
    )
    try:
        report = run_polymarket_cycle(
            store,
            config=config,
            sector_keywords=None,
            gamma_client=gamma,
            clob_client=clob,
        )
    finally:
        gamma.close()
        clob.close()

    lines = list(stage_summary_lines(report))
    assert any(line.startswith("  error:") for line in lines)
