"""T-CB-046 — integration tests for the polymarket_resolution_calibration meta-class.

Validates the three-table inner-join semantics of
``polymarket_resolution_calibration._occurrences`` against a synthetic
mispricing-detector + polymarket-connector corpus. Covers:

* **Occurrence count matches seed size** — N seeded comparisons +
  comparison_resolutions + polymarket_resolutions yield exactly N rows.
* **Downstream window filter exact split** — invoking
  ``base_rates._count_in_window`` on an even-N seed with an exact
  midpoint timestamp returns exactly N/2 rows (no ``±1`` slop, per the
  Phase 7 scout amendment that tightened the count assertion).
* **Invalidated rows excluded** — rows with ``invalidated=TRUE`` drop
  from the inner join's WHERE clause.
* **Superseded rows excluded** — rows with non-NULL ``superseded_at``
  drop too (provenance soft-delete).
* **Partial coverage** — a ``polymarket_resolutions`` row without a
  matching ``comparison_resolutions`` is excluded by the inner join,
  documenting the linkage-coverage caveat from T-CB-042.
* **Canonical-join cross-check** — ``_occurrences`` returns the same
  row set (modulo ordering) as the polarity.resolve-style three-table
  inner-join SQL, catching drift if either side mutates join semantics.

The fixture wires up data_ingest, polymarket_connector, pattern_library,
and mispricing_detector migrations so all three of the joined tables
exist in the synthetic store.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.classes.polymarket_resolution_calibration import (
    _occurrences,
)
from razor_rooster.pattern_library.engines.base_rates import _count_in_window
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)

# Even-N seed sized for the exact-midpoint split assertion. With ten
# evenly-spaced monthly timestamps Jan-Oct 2024 and ``t_mid`` set to
# 2024-06-01, the half-open window ``[t_start, t_mid)`` contains exactly
# the first five — Jan/Feb/Mar/Apr/May — and the remaining five fall in
# ``[t_mid, t_end)``. No off-by-one slop.
_N = 10
_RESOLUTION_TIMESTAMPS = tuple(datetime(2024, month, 1, tzinfo=UTC) for month in range(1, _N + 1))
_T_START = datetime(2024, 1, 1, tzinfo=UTC)
_T_MID = datetime(2024, 6, 1, tzinfo=UTC)
_T_END = datetime(2024, 11, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """A DuckDB store with all four subsystem schemas migrated."""
    db_path = tmp_path / "meta_class_integration.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_mispricing_migrations(conn)
    try:
        yield s
    finally:
        s.close()


# -- seeders --------------------------------------------------------------


def _seed_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    condition_id: str,
    class_id: str = "polymarket_resolution_calibration",
    polarity: str = "aligned",
    computed_at: datetime,
) -> None:
    """Insert minimal cycle + comparison rows so the inner join links."""
    cycle_id = f"cy-{comparison_id}"
    conn.execute(
        "INSERT INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            computed_at,
            computed_at,
            1,
            0,
            json.dumps({}),
            1,
            "scan-1",
        ],
    )
    conn.execute(
        "INSERT INTO comparisons "
        "(comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, ci_overlap, surfaced, computed_at, "
        "venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            cycle_id,
            "m-1",
            class_id,
            condition_id,
            "tok-yes",
            polarity,
            "scan-1",
            0.30,
            0.20,
            0.40,
            False,
            False,
            computed_at,
            "polymarket",
        ],
    )


def _seed_comparison_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    condition_id: str,
    resolution_ts: datetime,
    polarity_at_comparison: str = "aligned",
    outcome_observed: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO comparison_resolutions "
        "(comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            condition_id,
            "yes",
            resolution_ts,
            0.30,
            0.10,
            polarity_at_comparison,
            outcome_observed,
            resolution_ts,
            "polymarket",
        ],
    )


def _seed_polymarket_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
    winning_outcome_label: str = "Yes",
    invalidated: bool = False,
    superseded_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO polymarket_resolutions ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, winning_outcome_token_id, winning_outcome_label, "
        "resolution_ts, resolution_source, resolution_metadata, "
        "final_yes_price, final_no_price, total_volume_at_resolution, "
        "invalidated"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'gamma', NULL, ?, ?, ?, ?)",
        [
            "polymarket_resolutions",
            f"res-{condition_id}",
            resolution_ts,
            resolution_ts,
            "test@1",
            json.dumps({"raw": "synthetic"}),
            superseded_at,
            condition_id,
            "tok-yes" if winning_outcome_label == "Yes" else "tok-no",
            winning_outcome_label,
            resolution_ts,
            1.0 if winning_outcome_label == "Yes" else 0.0,
            0.0 if winning_outcome_label == "Yes" else 1.0,
            25000.0,
            invalidated,
        ],
    )


def _seed_full_chain(
    conn: duckdb.DuckDBPyConnection,
    *,
    index: int,
    resolution_ts: datetime,
    invalidated: bool = False,
    superseded_at: datetime | None = None,
) -> None:
    """Seed comparison + resolution + polymarket_resolution for one row."""
    comparison_id = f"cmp-{index:03d}"
    condition_id = f"cond-{index:03d}"
    _seed_comparison(
        conn,
        comparison_id=comparison_id,
        condition_id=condition_id,
        computed_at=resolution_ts,
    )
    _seed_comparison_resolution(
        conn,
        comparison_id=comparison_id,
        condition_id=condition_id,
        resolution_ts=resolution_ts,
    )
    _seed_polymarket_resolution(
        conn,
        condition_id=condition_id,
        resolution_ts=resolution_ts,
        invalidated=invalidated,
        superseded_at=superseded_at,
    )


# -- tests ----------------------------------------------------------------


def test_occurrence_count_matches_seed_size_integration(store: DuckDBStore) -> None:
    """N=10 seeded comparisons + resolutions yield exactly 10 rows."""
    with store.connection() as conn:
        for i, ts in enumerate(_RESOLUTION_TIMESTAMPS):
            _seed_full_chain(conn, index=i, resolution_ts=ts)
        df = _occurrences(conn)
    assert len(df) == _N


def test_downstream_window_filter_exact_split_integration(
    store: DuckDBStore,
) -> None:
    """Even-N seed + exact midpoint timestamp -> exactly N/2 in window.

    The half-open ``[t_start, t_mid)`` window catches the first five
    monthly timestamps (Jan-May 2024); the remaining five (Jun-Oct) fall
    on or after t_mid. No ``±1`` slop because the midpoint is one of the
    seeded timestamps and the window is half-open.
    """
    with store.connection() as conn:
        for i, ts in enumerate(_RESOLUTION_TIMESTAMPS):
            _seed_full_chain(conn, index=i, resolution_ts=ts)
        df = _occurrences(conn)
    assert len(df) == _N
    count_first_half = _count_in_window(df, _T_START, _T_MID)
    count_second_half = _count_in_window(df, _T_MID, _T_END)
    assert count_first_half == _N // 2
    assert count_second_half == _N // 2
    assert count_first_half + count_second_half == _N


def test_invalidated_rows_excluded_integration(store: DuckDBStore) -> None:
    """N valid + 1 invalidated rows -> _occurrences returns N (excludes invalidated)."""
    with store.connection() as conn:
        for i, ts in enumerate(_RESOLUTION_TIMESTAMPS):
            _seed_full_chain(conn, index=i, resolution_ts=ts)
        # One additional row flagged invalidated=TRUE.
        _seed_full_chain(
            conn,
            index=999,
            resolution_ts=datetime(2024, 5, 15, tzinfo=UTC),
            invalidated=True,
        )
        df = _occurrences(conn)
    assert len(df) == _N
    # Sanity-check the invalidated row was actually inserted in the
    # underlying table (so the test is exercising the WHERE filter, not
    # a missing row).
    with store.connection() as conn:
        invalidated_total = conn.execute(
            "SELECT COUNT(*) FROM polymarket_resolutions WHERE invalidated = TRUE"
        ).fetchone()
    assert invalidated_total is not None and invalidated_total[0] == 1


def test_superseded_rows_excluded_integration(store: DuckDBStore) -> None:
    """N valid + 1 superseded row -> _occurrences returns N (excludes superseded)."""
    superseded_at = datetime(2024, 6, 15, tzinfo=UTC)
    with store.connection() as conn:
        for i, ts in enumerate(_RESOLUTION_TIMESTAMPS):
            _seed_full_chain(conn, index=i, resolution_ts=ts)
        _seed_full_chain(
            conn,
            index=998,
            resolution_ts=datetime(2024, 5, 15, tzinfo=UTC),
            superseded_at=superseded_at,
        )
        df = _occurrences(conn)
    assert len(df) == _N
    with store.connection() as conn:
        superseded_total = conn.execute(
            "SELECT COUNT(*) FROM polymarket_resolutions WHERE superseded_at IS NOT NULL"
        ).fetchone()
    assert superseded_total is not None and superseded_total[0] == 1


def test_partial_coverage_unlinked_resolution_integration(
    store: DuckDBStore,
) -> None:
    """Polymarket resolution without a comparison_resolutions row is excluded.

    Documents the partial-coverage caveat from T-CB-042: comparisons that
    have not yet been linked are intentionally excluded by the three-table
    inner join. Adding an orphan ``polymarket_resolutions`` row exercises
    the inner-join filter — the meta-class only counts mature, linked
    resolutions.
    """
    with store.connection() as conn:
        for i, ts in enumerate(_RESOLUTION_TIMESTAMPS):
            _seed_full_chain(conn, index=i, resolution_ts=ts)
        # Orphan polymarket resolution: no matching comparison +
        # comparison_resolutions chain. The inner join should drop it.
        _seed_polymarket_resolution(
            conn,
            condition_id="cond-orphan",
            resolution_ts=datetime(2024, 4, 15, tzinfo=UTC),
        )
        df = _occurrences(conn)
    assert len(df) == _N
    # Orphan resolution is in the underlying table, just not in the
    # joined output.
    with store.connection() as conn:
        orphan_present = conn.execute(
            "SELECT COUNT(*) FROM polymarket_resolutions WHERE condition_id = 'cond-orphan'"
        ).fetchone()
    assert orphan_present is not None and orphan_present[0] == 1
    assert "cond-orphan" not in set(df["condition_id"].tolist())


# Mirrors the canonical three-table inner-join pattern from
# calibration_backtest/engines/polarity.py:62-72 — same JOIN topology,
# no time/venue/class filter (those happen downstream / are class-bound
# in the meta-class), and no resolution_outcome='invalid' filter (the
# meta-class scopes off ``pr.invalidated`` / ``pr.superseded_at``
# instead per the post-Phase 7 amendment). The query is intentionally
# duplicated here so a future drift in either side surfaces as a test
# failure rather than silent disagreement.
_CANONICAL_JOIN_SQL = """
SELECT pr.condition_id, pr.resolution_ts
FROM comparison_resolutions AS cr
JOIN comparisons AS c USING (comparison_id)
JOIN polymarket_resolutions AS pr ON pr.condition_id = c.condition_id
WHERE pr.invalidated = FALSE
  AND pr.superseded_at IS NULL
""".strip()


def test_canonical_join_matches_polarity_resolve_integration(
    store: DuckDBStore,
) -> None:
    """``_occurrences`` row set equals the polarity.resolve-style join output.

    Three-table inner join (``comparison_resolutions`` x ``comparisons``
    x ``polymarket_resolutions``). Both queries should agree on the set
    of (condition_id, resolution_ts) pairs they return.
    """
    with store.connection() as conn:
        for i, ts in enumerate(_RESOLUTION_TIMESTAMPS):
            _seed_full_chain(conn, index=i, resolution_ts=ts)
        # Mix in noise that should be filtered by both queries.
        _seed_full_chain(
            conn,
            index=900,
            resolution_ts=datetime(2024, 5, 15, tzinfo=UTC),
            invalidated=True,
        )
        _seed_polymarket_resolution(
            conn,
            condition_id="cond-orphan",
            resolution_ts=datetime(2024, 4, 15, tzinfo=UTC),
        )
        df = _occurrences(conn)
        canonical_rows = conn.execute(_CANONICAL_JOIN_SQL).fetchall()

    meta_set = {
        (cid, ts)
        for cid, ts in zip(
            df["condition_id"].tolist(),
            df["occurrence_ts"].tolist(),
            strict=True,
        )
    }
    canonical_set = {(cid, ts) for cid, ts in canonical_rows}
    # Normalize timezones — duckdb returns aware datetimes; the
    # DataFrame uses pandas Timestamps. Convert both sides to ISO
    # strings to side-step pandas-vs-stdlib equality quirks.
    meta_iso = {(cid, ts.isoformat()) for cid, ts in meta_set}
    canonical_iso = {(cid, ts.astimezone(UTC).isoformat()) for cid, ts in canonical_set}
    assert meta_iso == canonical_iso
