"""T-PL-041 — base rate engine tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.data_ingest.persistence.provenance import register_source
from razor_rooster.pattern_library.engines.base_rates import (
    LOW_SAMPLE_THRESHOLD,
    compute_base_rate,
)
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_base_rates.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _make_class_with_occurrences(
    occurrences: list[datetime],
    *,
    class_id: str = "test_class",
    prior_alpha: float = 0.5,
    prior_beta: float = 0.5,
    base_rate_window_default: timedelta = timedelta(days=365 * 10),
) -> EventClass:
    df = pd.DataFrame({"occurrence_ts": occurrences})

    def query(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
        return df

    return EventClass(
        class_id=class_id,
        title="Test Class",
        description="A test class.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=query,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
        base_rate_window_default=base_rate_window_default,
    )


# -- happy paths -----------------------------------------------------------


def test_base_rate_with_known_count(store: DuckDBStore) -> None:
    """10 occurrences in 10 years → rate of 1.0/yr."""
    occurrences = [datetime(2020 + i, 6, 1, tzinfo=UTC) for i in range(10)]
    cls = _make_class_with_occurrences(occurrences)
    window = (
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2030, 1, 1, tzinfo=UTC),
    )
    with store.connection() as conn:
        result = compute_base_rate(conn, cls, window=window, library_version=1)
    assert result.occurrences == 10
    assert result.rate_per_year == pytest.approx(1.0, rel=0.01)
    assert result.low_sample_warning is False
    assert result.credible_interval_lower < result.credible_interval_upper


def test_base_rate_low_sample_warning_for_n_below_threshold(
    store: DuckDBStore,
) -> None:
    """n < 5 → low_sample_warning fires."""
    occurrences = [datetime(2024, 6, 1, tzinfo=UTC), datetime(2025, 6, 1, tzinfo=UTC)]
    cls = _make_class_with_occurrences(occurrences)
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    with store.connection() as conn:
        result = compute_base_rate(conn, cls, window=window, library_version=1)
    assert result.occurrences == 2
    assert result.low_sample_warning is True


def test_base_rate_low_sample_threshold_constant() -> None:
    assert LOW_SAMPLE_THRESHOLD == 5


def test_base_rate_zero_occurrences_in_window(store: DuckDBStore) -> None:
    """Empty result still produces a rate (0.0) and credible interval."""
    cls = _make_class_with_occurrences([])
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn:
        result = compute_base_rate(conn, cls, window=window, library_version=1)
    assert result.occurrences == 0
    assert result.rate_per_year == 0.0
    # Jeffreys prior puts non-zero mass below the observed 0 → tiny but
    # nonzero lower bound.
    assert result.credible_interval_lower < 0.01
    assert result.credible_interval_upper > 0.0
    assert result.low_sample_warning is True


def test_base_rate_filters_outside_window(store: DuckDBStore) -> None:
    """Occurrences outside the window are not counted."""
    occurrences = [
        datetime(2010, 6, 1, tzinfo=UTC),  # before
        datetime(2020, 6, 1, tzinfo=UTC),
        datetime(2025, 6, 1, tzinfo=UTC),
        datetime(2030, 6, 1, tzinfo=UTC),  # after
    ]
    cls = _make_class_with_occurrences(occurrences)
    window = (
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2030, 1, 1, tzinfo=UTC),
    )
    with store.connection() as conn:
        result = compute_base_rate(conn, cls, window=window, library_version=1)
    assert result.occurrences == 2


def test_base_rate_default_window_uses_class_default(store: DuckDBStore) -> None:
    """When window=None, the engine derives it from cls.base_rate_window_default."""
    now = datetime(2026, 5, 14, tzinfo=UTC)
    one_year_ago = datetime(2025, 5, 14, tzinfo=UTC)
    occurrences = [
        datetime(2024, 6, 1, tzinfo=UTC),  # outside default 365-day window
        datetime(2025, 11, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    ]
    cls = _make_class_with_occurrences(
        occurrences,
        base_rate_window_default=timedelta(days=365),
    )
    with store.connection() as conn:
        result = compute_base_rate(
            conn,
            cls,
            window=None,
            library_version=1,
            now=now,
        )
    assert result.window_end == now
    assert result.window_start == one_year_ago
    assert result.occurrences == 2  # only the two within the trailing year


def test_base_rate_credible_interval_widens_with_low_n(store: DuckDBStore) -> None:
    """Smaller sample → wider relative credible interval at the same observed rate.

    Both classes have an observed rate near 0.5/yr; the difference is
    the sample size (n=2 vs n=50). At similar central rates the
    n-dependence on CI width is dominant, so the rare class's
    interval should be wider in absolute terms than the common
    class's.
    """
    # Window of 4 years means: rare class with 2 events → 0.5/yr;
    # common class with 50 events would need a longer window. Tune so
    # both observe rates around 0.5/yr.
    window_rare = (datetime(2022, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    window_common = (datetime(1926, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    rare = _make_class_with_occurrences(
        [datetime(2022, 6, 1, tzinfo=UTC), datetime(2024, 6, 1, tzinfo=UTC)],
        class_id="rare",
    )
    common_occurrences = [datetime(1926 + (i * 2), 6, 1, tzinfo=UTC) for i in range(50)]
    common = _make_class_with_occurrences(common_occurrences, class_id="common")

    with store.connection() as conn:
        rare_result = compute_base_rate(conn, rare, window=window_rare, library_version=1)
        common_result = compute_base_rate(conn, common, window=window_common, library_version=1)

    rare_width = rare_result.credible_interval_upper - rare_result.credible_interval_lower
    common_width = common_result.credible_interval_upper - common_result.credible_interval_lower
    # rare has n=2, common has n=50 — rare's CI width should be larger.
    assert rare_width > common_width


def test_base_rate_with_custom_prior_logged(
    store: DuckDBStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-default prior emits an info log entry."""
    cls = _make_class_with_occurrences(
        [datetime(2025, 6, 1, tzinfo=UTC)], prior_alpha=2.0, prior_beta=2.0
    )
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn, caplog.at_level("INFO"):
        result = compute_base_rate(conn, cls, window=window, library_version=1)
    assert result.prior_alpha == 2.0
    assert result.prior_beta == 2.0
    assert any("non-default prior" in r.message for r in caplog.records)


# -- error paths -----------------------------------------------------------


def test_base_rate_inverted_window_rejected(store: DuckDBStore) -> None:
    cls = _make_class_with_occurrences([])
    bad_window = (
        datetime(2030, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 1, tzinfo=UTC),
    )
    with store.connection() as conn, pytest.raises(ValueError, match="window_start"):
        compute_base_rate(conn, cls, window=bad_window, library_version=1)


def test_base_rate_naive_window_rejected(store: DuckDBStore) -> None:
    cls = _make_class_with_occurrences([])
    naive_window = (datetime(2020, 1, 1), datetime(2030, 1, 1))
    with store.connection() as conn, pytest.raises(ValueError, match="timezone-aware"):
        compute_base_rate(
            conn,
            cls,
            window=naive_window,  # type: ignore[arg-type]
            library_version=1,
        )


def test_base_rate_bad_query_return_type_rejected(store: DuckDBStore) -> None:
    """occurrence_query must return a DataFrame; anything else fails fast."""

    def bad_query(_conn: duckdb.DuckDBPyConnection) -> object:
        return "not a dataframe"

    cls = EventClass(
        class_id="bad_query",
        title="x",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=bad_query,  # type: ignore[arg-type]
    )
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn, pytest.raises(TypeError, match="DataFrame"):
        compute_base_rate(conn, cls, window=window, library_version=1)


def test_base_rate_query_missing_column_rejected(store: DuckDBStore) -> None:
    def bad_query(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
        return pd.DataFrame({"some_other_column": [1, 2, 3]})

    cls = EventClass(
        class_id="missing_col",
        title="x",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=bad_query,
    )
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn, pytest.raises(ValueError, match="occurrence_ts"):
        compute_base_rate(conn, cls, window=window, library_version=1)


# -- source-staleness flag ------------------------------------------------


def test_source_stale_warning_propagates(store: DuckDBStore) -> None:
    """When a named source is stale, the result flag is True."""
    # Register a source with a long-past last_successful_fetch.
    with store.connection() as conn:
        register_source(
            conn,
            source_id="stale_source",
            source_type="event_stream",
            cadence="daily",
            freshness_threshold_seconds=3600,  # 1h threshold
            license="PUBLIC_DOMAIN",
        )
        # Mark its last_successful_fetch in the distant past.
        conn.execute(
            "UPDATE sources SET last_successful_fetch = ? WHERE source_id = ?",
            [datetime(2020, 1, 1, tzinfo=UTC), "stale_source"],
        )

    cls = _make_class_with_occurrences([datetime(2025, 6, 1, tzinfo=UTC)])
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn:
        result = compute_base_rate(
            conn,
            cls,
            window=window,
            library_version=1,
            source_ids_for_freshness=["stale_source"],
        )
    assert result.source_stale_warning is True


def test_source_stale_warning_unknown_source_treated_as_stale(
    store: DuckDBStore,
) -> None:
    cls = _make_class_with_occurrences([])
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn:
        result = compute_base_rate(
            conn,
            cls,
            window=window,
            library_version=1,
            source_ids_for_freshness=["never_registered"],
        )
    assert result.source_stale_warning is True


def test_source_stale_warning_skipped_when_no_sources_supplied(
    store: DuckDBStore,
) -> None:
    cls = _make_class_with_occurrences([])
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    with store.connection() as conn:
        result = compute_base_rate(
            conn,
            cls,
            window=window,
            library_version=1,
        )
    assert result.source_stale_warning is False
