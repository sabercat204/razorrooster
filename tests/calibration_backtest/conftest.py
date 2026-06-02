"""Shared fixtures and seed helpers for ``tests/calibration_backtest/`` (Phase 8).

Promotes the per-file ``_insert_resolution`` / ``_insert_mapping`` helpers
that several Phase 2/3/4 tests defined locally into a shared module so
Phase 8 acceptance tests (`test_e2e.py`, `test_performance.py`,
`test_properties.py`, `test_no_side_channels.py`) can seed full upstream
corpora without re-defining the same SQL inserts.

Helpers exposed:

* :func:`insert_resolution` — one ``polymarket_resolutions`` row.
* :func:`insert_mapping` — one ``class_market_mappings`` row.
* :func:`insert_comparison_resolution` — one ``comparison_resolutions``
  row (mispricing_detector tier 1 polarity source).
* :func:`insert_time_series` — one ``time_series`` row (canonical
  data_ingest schema; FRED-shaped).
* :func:`insert_event_stream` — one ``event_stream`` row (canonical
  data_ingest schema; ACLED/GDELT-shaped).
* :func:`insert_source` — one ``sources`` row (data_ingest operational
  schema).

Module-private helpers from ``test_replay_persistence.py`` continue to
exist as ``_insert_resolution`` / ``_insert_mapping`` aliases (re-exported
from this module) so those tests keep their original call shapes
without churn (deliberate zero-diff promotion).

Performance corpus fixture: :func:`seed_synthetic_corpus` is a
parameterized fixture builder used by ``test_performance.py``
(REQ-CB-PERF-001, REQ-CB-PERF-002). It accepts ``prediction_count``,
``class_count``, and ``window_days`` and seeds a deterministic synthetic
corpus shaped to v1's seed-library upper bound (8 classes, ~500
resolutions across 5 years, ~4000 prediction attempts).

All helpers are fully type-annotated so ``mypy --strict`` over
``tests/calibration_backtest`` remains a hard gate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
)
from razor_rooster.calibration_backtest.models import RunParameters
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.data_ingest.persistence.operational_schemas import sources_ddl
from razor_rooster.data_ingest.persistence.schemas import (
    SchemaType,
    canonical_table_ddl,
)
from razor_rooster.mispricing_detector.persistence.schemas import (
    CLASS_MARKET_MAPPINGS_DDL,
    COMPARISON_CYCLES_DDL,
    COMPARISON_RESOLUTIONS_DDL,
    COMPARISONS_DDL,
)
from razor_rooster.polymarket_connector.persistence.schemas import (
    POLYMARKET_RESOLUTIONS_DDL,
)

if TYPE_CHECKING:
    from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore

# ---------------------------------------------------------------------------
# Default values for the synthetic perf corpus
# ---------------------------------------------------------------------------

DEFAULT_PERF_PREDICTION_COUNT: Final[int] = 4000
DEFAULT_PERF_CLASS_COUNT: Final[int] = 8
DEFAULT_PERF_WINDOW_DAYS: Final[int] = 365 * 5


# ---------------------------------------------------------------------------
# Upstream seed helpers (zero-churn promotion from test_replay_persistence.py)
# ---------------------------------------------------------------------------


def insert_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
    winning_outcome_label: str | None = "yes",
    invalidated: bool = False,
    record_id: str | None = None,
) -> None:
    """Insert one ``polymarket_resolutions`` row with the provenance prefix.

    Mirrors the ``_insert_resolution`` helper that lived in
    ``test_replay_persistence.py`` and ``test_replay.py``; the call
    shape is preserved so callers can swap the import line without any
    other diff.
    """
    conn.execute(
        "INSERT INTO polymarket_resolutions ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, winning_outcome_token_id, winning_outcome_label, "
        "resolution_ts, resolution_source, resolution_metadata, "
        "final_yes_price, final_no_price, total_volume_at_resolution, "
        "invalidated"
        ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, 'polymarket', "
        "NULL, NULL, NULL, NULL, ?)",
        [
            "polymarket",
            record_id or condition_id,
            resolution_ts,
            resolution_ts,
            "v1.0.0",
            "{}",
            condition_id,
            winning_outcome_label,
            resolution_ts,
            invalidated,
        ],
    )


def insert_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity_value: str = "aligned",
    venue: str = "polymarket",
    removed_at: datetime | None = None,
) -> None:
    """Insert one ``class_market_mappings`` row.

    Mirrors the ``_insert_mapping`` helper from
    ``test_replay_persistence.py``; default ``polarity_value='aligned'``
    matches the historical default so calls migrate one-for-one.
    """
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, 'direct', 'high', ?, 'op', ?, ?, NULL, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            polarity_value,
            datetime(2025, 1, 1, tzinfo=UTC),
            removed_at,
            venue,
        ],
    )


def insert_comparison_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    condition_id: str,
    resolution_ts: datetime,
    polarity_at_comparison: str,
    resolution_outcome: str = "yes",
    outcome_observed: int = 1,
    model_probability_at_comparison: float = 0.5,
    market_probability_at_comparison: float | None = 0.5,
    venue: str = "polymarket",
) -> None:
    """Insert one ``comparison_resolutions`` row (mispricing_detector tier 1).

    The freezer's polarity-resolution path prefers
    ``comparison_resolutions`` (tier 1) over a current
    ``class_market_mappings`` row (tier 2 fallback) when both are
    available; T-CB-053's e2e seeding exercises both tiers.
    """
    conn.execute(
        "INSERT INTO comparison_resolutions ("
        "comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at, venue"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            condition_id,
            resolution_outcome,
            resolution_ts,
            model_probability_at_comparison,
            market_probability_at_comparison,
            polarity_at_comparison,
            outcome_observed,
            resolution_ts,
            venue,
        ],
    )


def insert_time_series(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    series_id: str,
    observation_ts: datetime,
    value: float | None,
    source_publication_ts: datetime | None = None,
    fetch_ts: datetime | None = None,
    connector_version: str = "v1.0.0",
    record_id: str | None = None,
    unit: str | None = None,
    frequency: str | None = None,
) -> None:
    """Insert one ``time_series`` row (canonical data_ingest schema).

    The canonical primary key is ``(source_id, source_record_id,
    fetch_ts)``; ``record_id`` defaults to ``f"{series_id}@{observation_ts}"``
    so callers can omit it for unique-row inserts. ``source_publication_ts``
    and ``fetch_ts`` default to ``observation_ts`` so the freezer's
    point-in-time guard (REQ-CB-FREEZE-001) sees a self-consistent row.
    """
    publication_ts = source_publication_ts if source_publication_ts is not None else observation_ts
    actual_fetch_ts = fetch_ts if fetch_ts is not None else observation_ts
    actual_record_id = (
        record_id if record_id is not None else f"{series_id}@{observation_ts.isoformat()}"
    )
    conn.execute(
        "INSERT INTO time_series ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, superseded_at, source_payload_json, "
        "series_id, observation_ts, value, unit, frequency"
        ") VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            source_id,
            actual_record_id,
            publication_ts,
            actual_fetch_ts,
            connector_version,
            "{}",
            series_id,
            observation_ts,
            value,
            unit,
            frequency,
        ],
    )


def insert_event_stream(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    event_ts: datetime,
    source_publication_ts: datetime | None = None,
    fetch_ts: datetime | None = None,
    connector_version: str = "v1.0.0",
    record_id: str | None = None,
    country_iso3: str | None = None,
    actor_primary: str | None = None,
    actor_secondary: str | None = None,
    event_class: str | None = None,
    description: str | None = None,
) -> None:
    """Insert one ``event_stream`` row (canonical data_ingest schema).

    The canonical primary key is ``(source_id, source_record_id,
    fetch_ts)``; ``record_id`` defaults to ``f"event@{event_ts}"`` for
    unique-row inserts. Provenance ``source_publication_ts`` /
    ``fetch_ts`` default to ``event_ts`` so the freezer's point-in-time
    guard sees a self-consistent row.
    """
    publication_ts = source_publication_ts if source_publication_ts is not None else event_ts
    actual_fetch_ts = fetch_ts if fetch_ts is not None else event_ts
    actual_record_id = record_id if record_id is not None else f"event@{event_ts.isoformat()}"
    conn.execute(
        "INSERT INTO event_stream ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, superseded_at, source_payload_json, "
        "event_ts, country_iso3, actor_primary, actor_secondary, "
        "event_class, description"
        ") VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        [
            source_id,
            actual_record_id,
            publication_ts,
            actual_fetch_ts,
            connector_version,
            "{}",
            event_ts,
            country_iso3,
            actor_primary,
            actor_secondary,
            event_class,
            description,
        ],
    )


def insert_source(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    source_type: str = "time_series",
    cadence: str = "daily",
    freshness_threshold_seconds: int = 86_400,
    license_value: str = "public_domain",
    registered_at: datetime | None = None,
    last_successful_fetch: datetime | None = None,
    license_noncommercial_required: bool = False,
    commercial_use_recorded_grant: bool = False,
    acknowledged_posture: str | None = None,
) -> None:
    """Insert one ``sources`` row (data_ingest operational schema).

    The freezer's ``registered_sources`` set comes from this table; the
    e2e seed must register every source whose canonical row is loaded
    so the freezer's point-in-time guard sees the source as known.
    Inclusive-language note: the column name is ``license`` (not the
    Python keyword ``license`` itself in argument-naming style) — the
    parameter is exposed as ``license_value`` to dodge the shadowing
    warning.
    """
    actual_registered_at = (
        registered_at if registered_at is not None else datetime(2024, 1, 1, tzinfo=UTC)
    )
    conn.execute(
        "INSERT INTO sources ("
        "source_id, source_type, cadence, freshness_threshold_seconds, "
        "last_successful_fetch, last_failed_fetch, last_failure_summary, "
        "license, license_terms_hash, license_acknowledged_at, "
        "license_noncommercial_required, commercial_use_recorded_grant, "
        "acknowledged_posture, registered_at, notes"
        ") VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, ?, ?, ?, ?, NULL)",
        [
            source_id,
            source_type,
            cadence,
            freshness_threshold_seconds,
            last_successful_fetch,
            license_value,
            license_noncommercial_required,
            commercial_use_recorded_grant,
            acknowledged_posture,
            actual_registered_at,
        ],
    )


# ---------------------------------------------------------------------------
# Synthetic perf corpus fixture (T-CB-052)
# ---------------------------------------------------------------------------


_PINNED_NOW: datetime = datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)
"""Far-future wall clock so any reasonable ``until_ts`` clears the
30-day recent-window guard (``DEFAULT_RECENT_WINDOW_DAYS``). Pinning
``now`` keeps the synthesised :class:`RunParameters` deterministic and
independent of when the test runs."""


@dataclass(frozen=True, slots=True)
class SyntheticCorpus:
    """Container returned by the ``seed_synthetic_corpus`` factory.

    Holds the in-memory DuckDB connection (with upstream + persistence
    schemas applied and seeded), a sentinel :class:`DuckDBStore`, the
    canonical :class:`RunParameters` covering the seeded window, and the
    pinned ``now`` timestamp for ``run_backtest``'s recent-window guard.
    """

    conn: duckdb.DuckDBPyConnection
    store: DuckDBStore
    params: RunParameters
    now: datetime
    num_resolutions: int
    num_classes: int


SeedSyntheticCorpus = Callable[[int, int, int], SyntheticCorpus]
"""Factory protocol for the ``seed_synthetic_corpus`` fixture."""


def _apply_perf_schemas(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply upstream + calibration_backtest DDL on ``conn`` for perf seeding."""
    conn.execute(POLYMARKET_RESOLUTIONS_DDL)
    conn.execute(CLASS_MARKET_MAPPINGS_DDL)
    conn.execute(COMPARISON_CYCLES_DDL)
    conn.execute(COMPARISONS_DDL)
    conn.execute(COMPARISON_RESOLUTIONS_DDL)
    run_pending_calibration_backtest_migrations(conn)


def _seed_resolutions_and_mappings(
    conn: duckdb.DuckDBPyConnection,
    *,
    num_resolutions: int,
    num_classes: int,
    day_span_days: int,
    window_start: datetime,
) -> None:
    """Seed ``num_resolutions`` rows across ``num_classes`` classes.

    Resolutions are timestamped uniformly across ``day_span_days``
    starting at ``window_start``. Each resolution is mapped to exactly
    one class via ``class_market_mappings`` (round-robin assignment).
    All rows resolve ``"yes"`` with ``invalidated=False`` so the replay
    loop's scored path is exercised end to end.

    Inserts are batched via ``executemany`` so seeding 4000 rows
    completes in well under one second (DuckDB's row-by-row INSERT path
    is the dominant cost at this size).
    """
    if num_resolutions <= 0:
        raise ValueError(f"num_resolutions must be positive, got {num_resolutions!r}")
    if num_classes <= 0:
        raise ValueError(f"num_classes must be positive, got {num_classes!r}")
    if day_span_days <= 0:
        raise ValueError(f"day_span_days must be positive, got {day_span_days!r}")

    # Spread resolutions uniformly across the window. We use seconds-per-row
    # so two resolutions never collide on the same instant; this matters
    # because ``iter_mapped_resolutions`` orders by ``resolution_ts ASC``
    # and the deterministic ordering downstream of ties is undefined.
    span_seconds = day_span_days * 24 * 60 * 60
    step_seconds = max(1, span_seconds // num_resolutions)

    resolution_rows: list[tuple[object, ...]] = []
    mapping_rows: list[tuple[object, ...]] = []
    mapping_mapped_at = window_start - timedelta(days=1)

    for index in range(num_resolutions):
        condition_id = f"perf-cond-{index:06d}"
        resolution_ts = window_start + timedelta(seconds=index * step_seconds)
        class_id = f"perf-cls-{index % num_classes:02d}"
        mapping_id = f"perf-map-{index:06d}"

        resolution_rows.append(
            (
                "polymarket",
                condition_id,
                resolution_ts,
                resolution_ts,
                "v1.0.0",
                "{}",
                None,  # superseded_at
                condition_id,
                None,  # winning_outcome_token_id
                "yes",
                resolution_ts,
                "polymarket",
                None,  # resolution_metadata
                None,  # final_yes_price
                None,  # final_no_price
                None,  # total_volume_at_resolution
                False,  # invalidated
            ),
        )
        mapping_rows.append(
            (
                mapping_id,
                class_id,
                condition_id,
                "direct",
                "high",
                "aligned",
                "perf-fixture",
                mapping_mapped_at,
                None,  # removed_at
                None,  # notes
                "polymarket",
            ),
        )

    conn.executemany(
        "INSERT INTO polymarket_resolutions ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, winning_outcome_token_id, winning_outcome_label, "
        "resolution_ts, resolution_source, resolution_metadata, "
        "final_yes_price, final_no_price, total_volume_at_resolution, "
        "invalidated"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        resolution_rows,
    )
    conn.executemany(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        mapping_rows,
    )


@pytest.fixture
def seed_synthetic_corpus() -> Iterator[SeedSyntheticCorpus]:
    """Factory fixture: seeds a synthetic corpus on demand.

    Usage::

        def test_something(seed_synthetic_corpus):
            corpus = seed_synthetic_corpus(4000, 8, 365 * 5)
            run_backtest(corpus.params, conn=corpus.conn, store=corpus.store, ...)

    The fixture owns the lifetime of every connection it creates and
    closes them on teardown so DuckDB's in-memory pages are reclaimed
    promptly. Multiple invocations within the same test return distinct
    corpora — useful for tests that need to compare runs across two
    independent datasets.

    Default sizing covers v1's seed-library upper bound: 4000 prediction
    attempts across 8 classes, 365*5-day window
    (:data:`DEFAULT_PERF_PREDICTION_COUNT`,
    :data:`DEFAULT_PERF_CLASS_COUNT`,
    :data:`DEFAULT_PERF_WINDOW_DAYS`).
    """
    created: list[duckdb.DuckDBPyConnection] = []

    def _factory(
        num_resolutions: int,
        num_classes: int,
        day_span_days: int,
    ) -> SyntheticCorpus:
        connection = duckdb.connect(":memory:")
        created.append(connection)
        _apply_perf_schemas(connection)

        # Place the seeded window comfortably before ``_PINNED_NOW`` so
        # ``until_ts`` clears the 30-day recent-window guard.
        window_end = _PINNED_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1)
        window_start = window_end - timedelta(days=day_span_days)

        _seed_resolutions_and_mappings(
            connection,
            num_resolutions=num_resolutions,
            num_classes=num_classes,
            day_span_days=day_span_days,
            window_start=window_start,
        )

        class_ids = tuple(f"perf-cls-{index:02d}" for index in range(num_classes))
        params = RunParameters(
            since_ts=window_start - timedelta(days=1),
            until_ts=window_end,
            lag_days=1,
            class_ids=class_ids,
            sectors=(),
            venues=("polymarket",),
            allow_recent=False,
        )

        # The performance and end-to-end smoke tests stub
        # ``evaluate_class_at_frozen_time`` so the store argument is
        # never dereferenced. Mirror the ``_FAKE_STORE`` sentinel idiom
        # already in use in ``test_replay_persistence.py``.
        store_sentinel = cast("DuckDBStore", object())
        return SyntheticCorpus(
            conn=connection,
            store=store_sentinel,
            params=params,
            now=_PINNED_NOW,
            num_resolutions=num_resolutions,
            num_classes=num_classes,
        )

    try:
        yield _factory
    finally:
        for connection in created:
            connection.close()


# ---------------------------------------------------------------------------
# Schema bootstrap helper for full upstream-stack DDL
# ---------------------------------------------------------------------------


def apply_full_upstream_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply every upstream + calibration_backtest table needed by Phase 8 e2e.

    Order matters only insofar as ``sources`` must exist before any
    canonical-schema row references it (the canonical-schema PK does
    not enforce the FK at the DuckDB level, but the freezer's
    registered-source guard reads ``sources`` so a missing row trips
    REQ-CB-FREEZE-001). Applying the calibration_backtest migrations
    last keeps the persistence-side DDL aligned with the migration
    runner's invariants (T-CB-009).
    """
    conn.execute(sources_ddl())
    conn.execute(canonical_table_ddl(SchemaType.TIME_SERIES))
    conn.execute(canonical_table_ddl(SchemaType.EVENT_STREAM))
    conn.execute(POLYMARKET_RESOLUTIONS_DDL)
    conn.execute(CLASS_MARKET_MAPPINGS_DDL)
    conn.execute(COMPARISON_CYCLES_DDL)
    conn.execute(COMPARISONS_DDL)
    conn.execute(COMPARISON_RESOLUTIONS_DDL)
    run_pending_calibration_backtest_migrations(conn)


__all__ = [
    "DEFAULT_PERF_CLASS_COUNT",
    "DEFAULT_PERF_PREDICTION_COUNT",
    "DEFAULT_PERF_WINDOW_DAYS",
    "SeedSyntheticCorpus",
    "SyntheticCorpus",
    "apply_full_upstream_schema",
    "insert_comparison_resolution",
    "insert_event_stream",
    "insert_mapping",
    "insert_resolution",
    "insert_source",
    "insert_time_series",
    "seed_synthetic_corpus",
]
