"""T-034 verification — backfill resume mechanism.

Verifies:
- A clean backfill run completes, persists records, marks status='completed'.
- An interrupted backfill (exception mid-stream) marks status='failed' and
  preserves the last good resume token so re-run continues from there.
- Re-running with the prior token in backfill_state resumes; doesn't restart.
- ``--restart`` ignores the prior token and starts from scratch.
- A connector without backfill support raises BackfillNotSupportedError.
- Cap check pauses backfill cleanly when triggered.
- Resume token is the last token from the most recent batch, not from the
  last record (when records emit None tokens between batches).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.backfill import (
    BackfillNotSupportedError,
    BackfillReport,
    CapCheckResult,
    get_backfill_state,
    run_backfill,
    upsert_backfill_state,
)
from razor_rooster.data_ingest.connectors.base import (
    Connector,
    License,
    ResumeToken,
)
from razor_rooster.data_ingest.normalization.base import (
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import register_source
from razor_rooster.data_ingest.persistence.schemas import SchemaType


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "backfill.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


def _register_source(store: DuckDBStore, source_id: str) -> None:
    with store.connection() as conn:
        register_source(
            conn,
            source_id=source_id,
            source_type="time_series",
            cadence="annual",
            freshness_threshold_seconds=31536000,
            license="PUBLIC_DOMAIN",
        )


# --- Synthetic backfill connectors ----------------------------------------


class _BackfillBase(Connector):
    title = "Backfill Base"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "annual"
    backfill_supported = True
    connector_version = "backfill_base@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        return iter(())

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        return TimeSeriesRecord(
            source_id=raw.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=raw.source_payload_json,
            series_id="X",
            observation_ts=raw.source_publication_ts,
            value=float(raw.source_payload_json["value"]),
        )


class _CleanBackfill(_BackfillBase):
    """Yields 100 records across pages of 10, with a per-page resume token."""

    source_id = "clean_backfill"
    title = "Clean Backfill"
    connector_version = "clean_backfill@0.1.0"

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: object | None = None,
        start_index: int = 0,
    ) -> None:
        super().__init__(store, credentials=credentials)  # type: ignore[arg-type]
        self.start_index = start_index

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        start = int(resume_token.value) if resume_token is not None else 0
        for i in range(start, 100):
            yield (
                RawRecord(
                    source_id=self.source_id,
                    source_record_id=f"rec-{i}",
                    source_payload_json={"value": float(i)},
                    source_publication_ts=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                ResumeToken(value=str(i + 1)) if (i + 1) % 10 == 0 else None,
            )


class _FailsMidway(_BackfillBase):
    """Yields 10 records, commits a token at 5, then raises."""

    source_id = "fails_midway"
    title = "Fails Midway"
    connector_version = "fails_midway@0.1.0"

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        start = int(resume_token.value) if resume_token is not None else 0
        for i in range(start, 100):
            if i >= 6 and i < 7:
                # After the first batch (5 records) committed, we raise.
                raise RuntimeError("simulated crash mid-backfill")
            yield (
                RawRecord(
                    source_id=self.source_id,
                    source_record_id=f"rec-{i}",
                    source_payload_json={"value": float(i)},
                    source_publication_ts=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                ResumeToken(value=str(i + 1)) if (i + 1) % 5 == 0 else None,
            )


class _NoBackfill(_BackfillBase):
    source_id = "no_backfill"
    title = "No Backfill"
    backfill_supported = False
    connector_version = "no_backfill@0.1.0"


# --- Tests ----------------------------------------------------------------


def test_clean_backfill_completes(store: DuckDBStore) -> None:
    _register_source(store, "clean_backfill")
    connector = _CleanBackfill(store)
    report = run_backfill(connector, batch_size=10)

    assert report.status == "completed"
    assert report.records_persisted == 100
    assert report.batches_committed == 10
    # Last token should be 100 (the next-record marker).
    assert report.last_resume_token == "100"

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM time_series WHERE source_id = 'clean_backfill'"
        ).fetchone()
    assert count == (100,)


def test_backfill_persists_state(store: DuckDBStore) -> None:
    _register_source(store, "clean_backfill")
    connector = _CleanBackfill(store)
    run_backfill(connector, batch_size=10)

    with store.connection() as conn:
        token, status = get_backfill_state(conn, "clean_backfill")
    assert status == "completed"
    assert token == "100"


def test_backfill_failure_preserves_last_good_token(store: DuckDBStore) -> None:
    _register_source(store, "fails_midway")
    connector = _FailsMidway(store)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_backfill(connector, batch_size=5)

    # The first batch (records 0-4 with token "5") should have committed
    # before the crash on record 6.
    with store.connection() as conn:
        token, status = get_backfill_state(conn, "fails_midway")
        records = conn.execute(
            "SELECT COUNT(*) FROM time_series WHERE source_id = 'fails_midway'"
        ).fetchone()
    assert status == "failed"
    assert token == "5"
    assert records == (5,)


def test_backfill_resumes_from_prior_token(store: DuckDBStore) -> None:
    """A second run after a crash picks up where the first left off."""
    _register_source(store, "clean_backfill")

    # First run: simulate a partial run by manually setting backfill state.
    with store.connection() as conn:
        upsert_backfill_state(
            conn,
            source_id="clean_backfill",
            started_at=datetime.now(tz=UTC),
            last_resume_token="50",
            records_persisted=50,
            status="failed",
        )

    connector = _CleanBackfill(store)
    report = run_backfill(connector, batch_size=10)

    assert report.status == "completed"
    # We resumed from token "50", so only 50 records were processed in this run.
    assert report.records_persisted == 50

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM time_series WHERE source_id = 'clean_backfill'"
        ).fetchone()
    assert count == (50,)  # only the second-half records were ingested


def test_backfill_restart_ignores_prior_token(store: DuckDBStore) -> None:
    """``restart=True`` starts from the beginning regardless of prior state."""
    _register_source(store, "clean_backfill")

    with store.connection() as conn:
        upsert_backfill_state(
            conn,
            source_id="clean_backfill",
            started_at=datetime.now(tz=UTC),
            last_resume_token="50",
            records_persisted=50,
            status="failed",
        )

    connector = _CleanBackfill(store)
    report = run_backfill(connector, batch_size=10, restart=True)

    assert report.records_persisted == 100


def test_backfill_unsupported_raises(store: DuckDBStore) -> None:
    _register_source(store, "no_backfill")
    connector = _NoBackfill(store)
    with pytest.raises(BackfillNotSupportedError):
        run_backfill(connector)


def test_backfill_cap_check_pauses(store: DuckDBStore) -> None:
    """When the cap check fires, backfill stops cleanly with status set."""
    _register_source(store, "clean_backfill")
    connector = _CleanBackfill(store)

    # Stop after the second batch (~20 records).
    state = {"call_count": 0}

    def cap_check(source_id: str) -> CapCheckResult | None:
        state["call_count"] += 1
        if state["call_count"] > 2:
            return CapCheckResult(status="CAP_REACHED", reason="test cap reached")
        return None

    report = run_backfill(connector, batch_size=10, cap_check=cap_check)

    assert report.status == "CAP_REACHED"
    # The cap check is consulted before each batch commit; the third call
    # blocks the third batch but the first two already committed.
    assert report.records_persisted == 20
    with store.connection() as conn:
        token, status = get_backfill_state(conn, "clean_backfill")
    assert status == "CAP_REACHED"
    assert token == "20"


def test_backfill_report_dataclass_default_construction() -> None:
    report = BackfillReport(
        source_id="x",
        started_at=datetime.now(tz=UTC),
    )
    assert report.records_persisted == 0
    assert report.batches_committed == 0
    assert report.last_resume_token is None
    assert report.status == "in_progress"


def test_get_backfill_state_returns_none_when_no_row(store: DuckDBStore) -> None:
    with store.connection() as conn:
        token, status = get_backfill_state(conn, "never_started")
    assert token is None
    assert status is None


def test_upsert_backfill_state_creates_then_updates(store: DuckDBStore) -> None:
    started = datetime.now(tz=UTC)
    with store.connection() as conn:
        upsert_backfill_state(
            conn,
            source_id="src",
            started_at=started,
            last_resume_token="1",
            records_persisted=10,
            status="in_progress",
        )
    with store.connection() as conn:
        upsert_backfill_state(
            conn,
            source_id="src",
            started_at=started,
            last_resume_token="2",
            records_persisted=20,
            status="completed",
        )
        token, status = get_backfill_state(conn, "src")
    assert token == "2"
    assert status == "completed"


def test_backfill_through_store_pool_concurrent_safe(store: DuckDBStore) -> None:
    """The persister and state writes share the store's pool without deadlocking."""
    _register_source(store, "clean_backfill")
    connector = _CleanBackfill(store)
    report = run_backfill(connector, batch_size=25)
    assert report.status == "completed"
    assert report.records_persisted == 100
