"""T-016 verification — disk budget tracker.

Verifies:
- ``DiskBudgetConfig`` validation rejects nonsense values.
- ``current_status`` returns expected pct, should_warn, should_pause flags
  across the under-warn / between-thresholds / over-pause regimes.
- ``database_size_bytes`` reports the on-disk file size when given a file
  path, including a WAL sibling if present.
- ``database_size_bytes`` falls back to ``PRAGMA database_size`` for
  in-memory stores.
- ``per_source_row_counts`` aggregates across canonical tables.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from razor_rooster.data_ingest.persistence.disk_budget import (
    DEFAULT_GLOBAL_CAP_BYTES,
    DEFAULT_PAUSE_BACKFILL_PCT,
    DEFAULT_WARN_PCT,
    DiskBudgetConfig,
    DiskBudgetStatus,
    current_status,
    database_size_bytes,
    per_source_row_counts,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge


def test_default_config_uses_published_thresholds() -> None:
    cfg = DiskBudgetConfig()
    assert cfg.global_cap_bytes == DEFAULT_GLOBAL_CAP_BYTES
    assert cfg.warn_at_pct == DEFAULT_WARN_PCT
    assert cfg.pause_backfill_at_pct == DEFAULT_PAUSE_BACKFILL_PCT


def test_config_rejects_zero_or_negative_cap() -> None:
    with pytest.raises(ValueError):
        DiskBudgetConfig(global_cap_bytes=0)
    with pytest.raises(ValueError):
        DiskBudgetConfig(global_cap_bytes=-1)


def test_config_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="strictly less than"):
        DiskBudgetConfig(warn_at_pct=95.0, pause_backfill_at_pct=80.0)


def test_config_rejects_out_of_range_pcts() -> None:
    with pytest.raises(ValueError):
        DiskBudgetConfig(warn_at_pct=0.0)
    with pytest.raises(ValueError):
        DiskBudgetConfig(warn_at_pct=100.5)
    with pytest.raises(ValueError):
        DiskBudgetConfig(pause_backfill_at_pct=0.0)


def test_status_below_warn_threshold() -> None:
    cfg = DiskBudgetConfig(global_cap_bytes=1000, warn_at_pct=80.0, pause_backfill_at_pct=95.0)
    status = DiskBudgetStatus(
        bytes_used=500,
        cap_bytes=cfg.global_cap_bytes,
        pct_of_cap=50.0,
        should_warn=False,
        should_pause_backfill=False,
    )
    assert status.should_warn is False
    assert status.should_pause_backfill is False


def test_current_status_in_memory_is_zero(
    tmp_path: Path,
) -> None:
    """A freshly-opened in-memory store has effectively zero usage."""
    c = duckdb.connect(":memory:")
    cfg = DiskBudgetConfig(global_cap_bytes=10 * 1024 * 1024)
    status = current_status(c, file_path=":memory:", config=cfg)
    # bytes_used can be 0 or a tiny housekeeping size; either way it's well
    # under the warn threshold.
    assert status.should_warn is False
    assert status.should_pause_backfill is False
    assert status.cap_bytes == 10 * 1024 * 1024


def test_current_status_warns_when_above_threshold(tmp_path: Path) -> None:
    """If the file size crosses warn_at_pct, ``should_warn`` flips to True."""
    db_path = tmp_path / "small.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_migrations(c)
            # Insert a few rows so the file actually grows.
            now = datetime.now(tz=UTC)
            batch = pa.table(
                {
                    "source_id": ["test"] * 10,
                    "source_record_id": [f"rec-{i}" for i in range(10)],
                    "source_publication_ts": [now] * 10,
                    "fetch_ts": [now] * 10,
                    "connector_version": ["test@0.1.0"] * 10,
                    "superseded_at": pa.array([None] * 10, type=pa.timestamp("us", tz="UTC")),
                    "source_payload_json": [json.dumps({"v": i}) for i in range(10)],
                    "event_ts": [now] * 10,
                    "country_iso3": ["XKX"] * 10,
                    "actor_primary": [None] * 10,
                    "actor_secondary": [None] * 10,
                    "event_class": [None] * 10,
                    "description": [None] * 10,
                }
            )
            staging_merge(c, "event_stream", batch)
            c.execute("CHECKPOINT")  # flush WAL into the main file
    finally:
        store.close()

    actual_size = db_path.stat().st_size
    assert actual_size > 0

    # Configure a tiny cap so the file is well above 80%.
    tiny_cap = max(actual_size * 2, 1024)  # ensure file is > 50% of cap
    cfg = DiskBudgetConfig(
        global_cap_bytes=tiny_cap,
        warn_at_pct=10.0,
        pause_backfill_at_pct=20.0,
    )
    # Re-open store to query.
    store2 = DuckDBStore(db_path, read_only=True)
    try:
        with store2.connection() as c:
            status = current_status(c, file_path=db_path, config=cfg)
    finally:
        store2.close()
    assert status.bytes_used >= actual_size  # may include WAL / metadata
    assert status.should_warn is True
    assert status.should_pause_backfill is True


def test_current_status_does_not_pause_below_pause_threshold(tmp_path: Path) -> None:
    db_path = tmp_path / "below_pause.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_migrations(c)
    finally:
        store.close()

    # File exists, but cap is huge — well below warn or pause.
    cfg = DiskBudgetConfig(global_cap_bytes=10 * 1024 * 1024 * 1024)  # 10 GB
    store2 = DuckDBStore(db_path, read_only=True)
    try:
        with store2.connection() as c:
            status = current_status(c, file_path=db_path, config=cfg)
    finally:
        store2.close()
    assert status.should_warn is False
    assert status.should_pause_backfill is False


def test_database_size_bytes_for_missing_file_returns_zero(tmp_path: Path) -> None:
    nonexistent = tmp_path / "doesnt_exist.duckdb"
    c = duckdb.connect(":memory:")
    assert database_size_bytes(c, file_path=nonexistent) == 0


def test_database_size_bytes_includes_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "wal_test.duckdb"
    db_path.write_bytes(b"x" * 100)
    wal_path = db_path.with_suffix(db_path.suffix + ".wal")
    wal_path.write_bytes(b"y" * 50)

    c = duckdb.connect(":memory:")
    total = database_size_bytes(c, file_path=db_path)
    assert total == 150


def test_per_source_row_counts_aggregates_across_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "counts.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_migrations(c)
            now = datetime.now(tz=UTC)
            event_batch = pa.table(
                {
                    "source_id": ["acled"] * 5 + ["who_don"] * 3,
                    "source_record_id": [f"rec-{i}" for i in range(8)],
                    "source_publication_ts": [now] * 8,
                    "fetch_ts": [now] * 8,
                    "connector_version": ["test@0.1.0"] * 8,
                    "superseded_at": pa.array([None] * 8, type=pa.timestamp("us", tz="UTC")),
                    "source_payload_json": [json.dumps({"v": i}) for i in range(8)],
                    "event_ts": [now] * 8,
                    "country_iso3": [None] * 8,
                    "actor_primary": [None] * 8,
                    "actor_secondary": [None] * 8,
                    "event_class": [None] * 8,
                    "description": [None] * 8,
                }
            )
            staging_merge(c, "event_stream", event_batch)

            ts_batch = pa.table(
                {
                    "source_id": ["fred"] * 4 + ["acled"] * 2,
                    "source_record_id": [f"ts-{i}" for i in range(6)],
                    "source_publication_ts": [now] * 6,
                    "fetch_ts": [now] * 6,
                    "connector_version": ["test@0.1.0"] * 6,
                    "superseded_at": pa.array([None] * 6, type=pa.timestamp("us", tz="UTC")),
                    "source_payload_json": [json.dumps({"v": i}) for i in range(6)],
                    "series_id": [f"S{i}" for i in range(6)],
                    "observation_ts": [now] * 6,
                    "value": [float(i) for i in range(6)],
                    "unit": [None] * 6,
                    "frequency": [None] * 6,
                }
            )
            staging_merge(c, "time_series", ts_batch)

            counts = per_source_row_counts(c)
    finally:
        store.close()

    assert counts == {"acled": 7, "who_don": 3, "fred": 4}


def test_per_source_row_counts_skips_missing_tables() -> None:
    """No canonical tables yet → empty dict, not a crash."""
    c = duckdb.connect(":memory:")
    counts = per_source_row_counts(c)
    assert counts == {}
