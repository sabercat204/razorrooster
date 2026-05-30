"""T-035 verification — cap enforcement during backfill.

Verifies:
- ``build_cap_check`` returns a callable.
- The check returns ``None`` when below all thresholds.
- The check returns ``GLOBAL_CAP_REACHED`` when the global corpus crosses
  the pause threshold.
- The check returns ``CAP_REACHED`` when a per-source byte cap is hit.
- The check ignores per-source caps for sources not in the config.
- ``estimate_source_bytes`` reflects row counts.
- Integration with run_backfill: the cap check pauses the backfill cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.backfill import (
    CapCheckResult,
    run_backfill,
)
from razor_rooster.data_ingest.cap_enforcement import (
    DEFAULT_AVERAGE_ROW_BYTES,
    build_cap_check,
    estimate_source_bytes,
    per_source_cap_for,
)
from razor_rooster.data_ingest.config.loader import (
    PerSourceCaps,
    SourceCapsConfig,
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
    db_path = tmp_path / "cap.duckdb"
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


def _make_caps(
    *,
    max_corpus_bytes: int = 10_000_000_000,  # 10 GB
    pause_at_pct: float = 95.0,
    warn_at_pct: float = 80.0,
    per_source: dict[str, PerSourceCaps] | None = None,
) -> SourceCapsConfig:
    return SourceCapsConfig.model_validate(
        {
            "version": 1,
            "global": {
                "max_corpus_bytes": max_corpus_bytes,
                "warn_at_pct": warn_at_pct,
                "pause_backfill_at_pct": pause_at_pct,
            },
            "per_source": {sid: caps.model_dump() for sid, caps in (per_source or {}).items()},
        }
    )


# --- Synthetic backfill connector for integration tests --------------------


class _Backfill(Connector):
    source_id = "synth_backfill"
    title = "Synthetic"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "annual"
    backfill_supported = True
    connector_version = "synth_backfill@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        return iter(())

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        start = int(resume_token.value) if resume_token is not None else 0
        for i in range(start, 200):
            yield (
                RawRecord(
                    source_id=self.source_id,
                    source_record_id=f"rec-{i}",
                    source_payload_json={"value": float(i)},
                    source_publication_ts=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                ResumeToken(value=str(i + 1)) if (i + 1) % 10 == 0 else None,
            )

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


# --- estimate_source_bytes -------------------------------------------------


def test_estimate_source_bytes_zero_when_empty(store: DuckDBStore) -> None:
    assert estimate_source_bytes(store, "nonexistent") == 0


def test_estimate_source_bytes_scales_with_row_count(store: DuckDBStore, tmp_path: Path) -> None:
    _register_source(store, "synth_backfill")
    connector = _Backfill(store)
    run_backfill(connector, batch_size=20)

    estimated = estimate_source_bytes(store, "synth_backfill")
    assert estimated == 200 * DEFAULT_AVERAGE_ROW_BYTES


def test_estimate_source_bytes_respects_average_override(store: DuckDBStore) -> None:
    _register_source(store, "synth_backfill")
    connector = _Backfill(store)
    run_backfill(connector, batch_size=20)

    estimated = estimate_source_bytes(store, "synth_backfill", average_row_bytes=10)
    assert estimated == 200 * 10


# --- build_cap_check -------------------------------------------------------


def test_check_returns_none_when_below_thresholds(store: DuckDBStore, tmp_path: Path) -> None:
    caps = _make_caps()
    check = build_cap_check(store, caps=caps, file_path=tmp_path / "cap.duckdb")
    assert check("any_source") is None


def test_check_returns_global_cap_when_pause_threshold_crossed(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """Configure a tiny global cap so the on-disk file blows past the pause %."""
    db_path = tmp_path / "cap.duckdb"
    actual_size = db_path.stat().st_size
    # Global cap at twice the file size with 10% pause threshold = always paused.
    caps = _make_caps(
        max_corpus_bytes=max(actual_size * 2, 1024),
        warn_at_pct=5.0,
        pause_at_pct=10.0,
    )
    check = build_cap_check(store, caps=caps, file_path=db_path)
    result = check("any_source")
    assert isinstance(result, CapCheckResult)
    assert result.status == "GLOBAL_CAP_REACHED"
    assert "global corpus" in result.reason


def test_check_returns_per_source_cap_when_byte_cap_hit(store: DuckDBStore, tmp_path: Path) -> None:
    _register_source(store, "synth_backfill")
    connector = _Backfill(store)
    run_backfill(connector, batch_size=20)

    estimated = estimate_source_bytes(store, "synth_backfill")
    caps = _make_caps(
        max_corpus_bytes=10 * 1024 * 1024 * 1024 * 1024,  # 10 TB so global never fires
        per_source={
            "synth_backfill": PerSourceCaps(max_bytes=estimated // 2),
        },
    )
    check = build_cap_check(store, caps=caps, file_path=tmp_path / "cap.duckdb")
    result = check("synth_backfill")
    assert isinstance(result, CapCheckResult)
    assert result.status == "CAP_REACHED"
    assert "synth_backfill" in result.reason


def test_check_ignores_per_source_when_under_byte_cap(store: DuckDBStore, tmp_path: Path) -> None:
    _register_source(store, "synth_backfill")
    connector = _Backfill(store)
    run_backfill(connector, batch_size=20)

    caps = _make_caps(
        max_corpus_bytes=10 * 1024 * 1024 * 1024 * 1024,
        per_source={
            "synth_backfill": PerSourceCaps(max_bytes=10 * 1024 * 1024),  # 10 MB
        },
    )
    check = build_cap_check(store, caps=caps, file_path=tmp_path / "cap.duckdb")
    assert check("synth_backfill") is None


def test_check_ignores_unconfigured_sources(store: DuckDBStore, tmp_path: Path) -> None:
    """A source not in per_source has no per-source cap to enforce."""
    caps = _make_caps()
    check = build_cap_check(store, caps=caps, file_path=tmp_path / "cap.duckdb")
    assert check("never_configured") is None


def test_check_ignores_per_source_without_max_bytes(store: DuckDBStore, tmp_path: Path) -> None:
    """A per_source row with only max_backfill_years isn't enforced as bytes."""
    caps = _make_caps(
        per_source={
            "synth_backfill": PerSourceCaps(max_backfill_years=10),  # no max_bytes
        },
    )
    check = build_cap_check(store, caps=caps, file_path=tmp_path / "cap.duckdb")
    assert check("synth_backfill") is None


# --- per_source_cap_for ----------------------------------------------------


def test_per_source_cap_for_returns_none_for_unknown() -> None:
    caps = _make_caps()
    assert per_source_cap_for(caps, "unknown") is None


def test_per_source_cap_for_returns_configured() -> None:
    caps = _make_caps(
        per_source={
            "fred": PerSourceCaps(max_backfill_years=50, max_bytes=1024),
        },
    )
    cap = per_source_cap_for(caps, "fred")
    assert cap is not None
    assert cap.max_backfill_years == 50
    assert cap.max_bytes == 1024


# --- Integration with run_backfill -----------------------------------------


def test_backfill_pauses_when_per_source_cap_hit(store: DuckDBStore, tmp_path: Path) -> None:
    _register_source(store, "synth_backfill")

    # Set the per-source byte cap to roughly 50 records' worth so backfill
    # pauses partway through the 200-record stream.
    caps = _make_caps(
        max_corpus_bytes=10 * 1024 * 1024 * 1024 * 1024,
        per_source={
            "synth_backfill": PerSourceCaps(max_bytes=50 * DEFAULT_AVERAGE_ROW_BYTES),
        },
    )
    check = build_cap_check(store, caps=caps, file_path=tmp_path / "cap.duckdb")

    connector = _Backfill(store)
    report = run_backfill(connector, batch_size=10, cap_check=check)

    assert report.status == "CAP_REACHED"
    # Records persisted should be at least 50 (the cap) and at most 60
    # (one batch overshoots the boundary).
    assert 50 <= report.records_persisted <= 60
