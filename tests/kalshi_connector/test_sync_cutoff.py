"""T-KSI-040 — cutoff snapshot acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.client.models import KalshiHistoricalCutoff
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.sync.cutoff import read_cutoff, snapshot_cutoff


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_cutoff.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
    yield s
    s.close()


class _FakeCutoffClient:
    """Minimal stand-in returning a fixed cutoff."""

    def __init__(self, cutoff: KalshiHistoricalCutoff) -> None:
        self._cutoff = cutoff
        self.calls = 0

    def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        self.calls += 1
        return self._cutoff


def _make_cutoff(
    *,
    market_settled_ts: datetime,
    trades_created_ts: datetime,
    orders_updated_ts: datetime,
) -> KalshiHistoricalCutoff:
    return KalshiHistoricalCutoff(
        market_settled_ts=market_settled_ts,
        trades_created_ts=trades_created_ts,
        orders_updated_ts=orders_updated_ts,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_snapshot_cutoff_writes_single_row(store: DuckDBStore) -> None:
    when = datetime(2026, 5, 16, 12, tzinfo=UTC)
    cutoff = _make_cutoff(
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 2, 15, tzinfo=UTC),
    )
    client = _FakeCutoffClient(cutoff)
    result = snapshot_cutoff(store, client=client, now=when)  # type: ignore[arg-type]
    assert result.fetched_at == when
    with store.connection() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM kalshi_historical_cutoff").fetchone()
    assert rows is not None and rows[0] == 1


def test_snapshot_cutoff_replaces_prior_row(store: DuckDBStore) -> None:
    cutoff_v1 = _make_cutoff(
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 2, 15, tzinfo=UTC),
    )
    cutoff_v2 = _make_cutoff(
        market_settled_ts=datetime(2026, 3, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 3, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 3, 15, tzinfo=UTC),
    )
    snapshot_cutoff(store, client=_FakeCutoffClient(cutoff_v1))  # type: ignore[arg-type]
    snapshot_cutoff(store, client=_FakeCutoffClient(cutoff_v2))  # type: ignore[arg-type]
    persisted = read_cutoff(store)
    assert persisted is not None
    assert persisted.market_settled_ts == datetime(2026, 3, 15, tzinfo=UTC)


def test_read_cutoff_returns_none_when_no_snapshot(store: DuckDBStore) -> None:
    assert read_cutoff(store) is None


def test_snapshot_cutoff_returns_parsed_cutoff(store: DuckDBStore) -> None:
    cutoff = _make_cutoff(
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 2, 15, tzinfo=UTC),
    )
    client = _FakeCutoffClient(cutoff)
    result = snapshot_cutoff(store, client=client)  # type: ignore[arg-type]
    assert result.market_settled_ts == cutoff.market_settled_ts
    assert client.calls == 1
