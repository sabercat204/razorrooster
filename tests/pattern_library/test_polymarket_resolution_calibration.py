"""T-CB-043 — unit tests for the upgraded ``_occurrences`` query.

Exercises the post-T-CB-042 three-table inner join across
``comparison_resolutions``, ``comparisons``, and
``polymarket_resolutions``. Verifies:

- A seeded triple yields exactly one row with the expected fields.
- ``polymarket_resolutions.invalidated = TRUE`` rows are filtered out.
- ``polymarket_resolutions.superseded_at IS NOT NULL`` rows are
  filtered out.
- The empty-frame fallback path has been removed from production
  code (AST grep on ``polymarket_resolution_calibration.py`` returns
  zero matches for the stub-era literal). The fallback is only
  acceptable in test fixtures and elsewhere — never in the production
  meta-class itself.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.models import (
    Comparison,
    ComparisonCycle,
    ComparisonResolution,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    persist_comparison,
    write_cycle,
    write_resolution_link,
)
from razor_rooster.pattern_library.classes import (
    polymarket_resolution_calibration as pl_meta,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)

# -- fixtures ------------------------------------------------------------


@pytest.fixture
def populated_store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """Store with all four migrations run — mirrors the extended
    populated_store fixture from ``test_end_to_end_refresh.py``."""
    db_path = tmp_path / "pl_meta.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_mispricing_migrations(conn)
    try:
        yield store
    finally:
        store.close()


# -- seed helpers --------------------------------------------------------


_NOW = datetime(2026, 5, 20, tzinfo=UTC)
_RESOLUTION_TS = datetime(2026, 6, 1, tzinfo=UTC)


def _seed_comparison(
    store: DuckDBStore,
    *,
    comparison_id: str,
    condition_id: str,
    class_id: str = "polymarket_resolution_calibration",
) -> None:
    with store.connection() as conn:
        write_cycle(
            conn,
            ComparisonCycle(
                cycle_id=f"cy-{comparison_id}",
                started_at=_NOW,
                completed_at=_NOW,
                comparisons_total=1,
                surfaced_count=0,
                suppressed_breakdown={},
                library_version_at_cycle=1,
                scan_id_consumed="scan-1",
            ),
        )
        persist_comparison(
            conn,
            Comparison(
                comparison_id=comparison_id,
                cycle_id=f"cy-{comparison_id}",
                mapping_id="map-1",
                class_id=class_id,
                condition_id=condition_id,
                outcome_token_id="tok-yes",
                polarity="aligned",
                scan_id="scan-1",
                model_probability=0.30,
                model_ci_lower=0.20,
                model_ci_upper=0.40,
                market_probability=0.25,
                market_best_bid=None,
                market_best_ask=None,
                market_last_trade_price=None,
                market_volume_24h=None,
                market_spread_bps=None,
                market_snapshot_ts=None,
                delta=None,
                log_odds_delta=None,
                ci_overlap=False,
                expected_value=None,
                confidence_weighted_score=None,
                surfaced=False,
                computed_at=_NOW,
            ),
        )


def _seed_resolution_link(
    store: DuckDBStore,
    *,
    comparison_id: str,
    condition_id: str,
) -> None:
    with store.connection() as conn:
        write_resolution_link(
            conn,
            ComparisonResolution(
                comparison_id=comparison_id,
                condition_id=condition_id,
                resolution_outcome="yes",
                resolution_ts=_RESOLUTION_TS,
                model_probability_at_comparison=0.30,
                market_probability_at_comparison=0.25,
                polarity_at_comparison="aligned",
                outcome_observed=1,
                linked_at=_RESOLUTION_TS,
            ),
        )


def _seed_polymarket_resolution(
    store: DuckDBStore,
    *,
    condition_id: str,
    winning_outcome_label: str = "yes",
    invalidated: bool = False,
    superseded_at: datetime | None = None,
) -> None:
    with store.connection() as conn:
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
                _NOW,
                _NOW,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                superseded_at,
                condition_id,
                "tok-yes",
                winning_outcome_label,
                _RESOLUTION_TS,
                1.0 if winning_outcome_label == "yes" else 0.0,
                0.0 if winning_outcome_label == "yes" else 1.0,
                25000.0,
                invalidated,
            ],
        )


# -- tests ---------------------------------------------------------------


def test_occurrences_returns_seeded_row(populated_store: DuckDBStore) -> None:
    """A fully-linked triple of (comparison, resolution_link, polymarket_resolution)
    yields exactly one row from ``_occurrences`` with the expected fields."""
    comparison_id = "cmp-1"
    condition_id = "0xabc"
    _seed_comparison(populated_store, comparison_id=comparison_id, condition_id=condition_id)
    _seed_resolution_link(populated_store, comparison_id=comparison_id, condition_id=condition_id)
    _seed_polymarket_resolution(populated_store, condition_id=condition_id)

    with populated_store.connection() as conn:
        df = pl_meta.polymarket_resolution_calibration.occurrence_query(conn)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1, f"expected exactly 1 row, got {len(df)}: {df}"
    row = df.iloc[0]
    assert row["condition_id"] == condition_id
    assert row["class_id"] == "polymarket_resolution_calibration"
    assert row["polarity_at_comparison"] == "aligned"
    assert row["winning_outcome_label"] == "yes"
    assert row["invalidated"] is False or row["invalidated"] == 0
    # ``occurrence_ts`` is parsed to a UTC datetime.
    assert pd.Timestamp(row["occurrence_ts"]).to_pydatetime() == _RESOLUTION_TS


def test_occurrences_filters_invalidated(populated_store: DuckDBStore) -> None:
    """``invalidated = TRUE`` polymarket_resolutions rows are filtered out."""
    comparison_id = "cmp-inv"
    condition_id = "0xinv"
    _seed_comparison(populated_store, comparison_id=comparison_id, condition_id=condition_id)
    _seed_resolution_link(populated_store, comparison_id=comparison_id, condition_id=condition_id)
    _seed_polymarket_resolution(populated_store, condition_id=condition_id, invalidated=True)

    with populated_store.connection() as conn:
        df = pl_meta.polymarket_resolution_calibration.occurrence_query(conn)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0, f"expected empty DataFrame, got {df}"


def test_occurrences_filters_superseded(populated_store: DuckDBStore) -> None:
    """``superseded_at IS NOT NULL`` polymarket_resolutions rows are filtered out."""
    comparison_id = "cmp-sup"
    condition_id = "0xsup"
    _seed_comparison(populated_store, comparison_id=comparison_id, condition_id=condition_id)
    _seed_resolution_link(populated_store, comparison_id=comparison_id, condition_id=condition_id)
    _seed_polymarket_resolution(
        populated_store,
        condition_id=condition_id,
        superseded_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    with populated_store.connection() as conn:
        df = pl_meta.polymarket_resolution_calibration.occurrence_query(conn)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0, f"expected empty DataFrame, got {df}"


def test_no_empty_frame_fallback_in_production() -> None:
    """The empty-frame fallback path must NOT live in the production
    meta-class. Reading ``cr.outcome_observed`` and synthesizing an
    empty DataFrame on no-rows masked the missing-table catalog error
    that motivated the Phase 7 fixture extension; per the T-CB-043
    decision the fix is the fixture extension, not a defensive fallback.

    This test enforces that decision via AST grep against the
    production source file.
    """
    src_path = Path(pl_meta.__file__).resolve() if pl_meta.__file__ else None
    assert src_path is not None
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    fallback_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match ``pd.DataFrame({...})``.
        func = node.func
        is_pd_dataframe = (
            isinstance(func, ast.Attribute)
            and func.attr == "DataFrame"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pd"
        )
        if not is_pd_dataframe:
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        # Match the stub-era literal: keys include 'occurrence_ts' bound
        # to a ``pd.to_datetime([], utc=True)`` empty-series.
        keys = [
            k.value
            for k in node.args[0].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if "occurrence_ts" not in keys:
            continue
        # Look for ``pd.to_datetime([], utc=True)`` value bound to
        # ``occurrence_ts``.
        for k, v in zip(node.args[0].keys, node.args[0].values, strict=False):
            if not (isinstance(k, ast.Constant) and k.value == "occurrence_ts"):
                continue
            if not isinstance(v, ast.Call):
                continue
            v_func = v.func
            if (
                isinstance(v_func, ast.Attribute)
                and v_func.attr == "to_datetime"
                and isinstance(v_func.value, ast.Name)
                and v_func.value.id == "pd"
                and v.args
                and isinstance(v.args[0], ast.List)
                and len(v.args[0].elts) == 0
            ):
                fallback_calls.append(node)
                break

    assert not fallback_calls, (
        "production polymarket_resolution_calibration.py still contains "
        'the empty-frame fallback ``pd.DataFrame({"occurrence_ts": '
        "pd.to_datetime([], utc=True)})`` — Phase 7 (T-CB-043) requires "
        "removal. The fixture extension is the canonical fix; defensive "
        "fallbacks would mask production schema bugs."
    )
