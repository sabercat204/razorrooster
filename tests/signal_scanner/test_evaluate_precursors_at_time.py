"""Contract tests for ``evaluate_precursors_at_time`` (T-CB-017).

The calibration backtest (`razor_rooster.calibration_backtest`) replays
historical resolutions and must evaluate each class's precursors as of
a frozen historical instant rather than "now". The public wrapper
:func:`signal_scanner.engines.scanner.evaluate_precursors_at_time`
exists so the backtest does not have to reach into private scanner
internals; the design (CALIBRATION_BACKTEST_DESIGN.md §T-CB-017)
requires the wrapper to remain bit-equivalent to the live scanner
path when ``as_of_ts`` equals the live ``scan_started_at``.

These tests lock that contract in:

* When ``as_of_ts`` matches the scanner's ``scan_started_at`` on a
  fixed corpus, the public wrapper's ``current_values`` dict equals
  the value the live scanner would produce.
* The public symbol is reachable from the design-doc-stated import
  path ``signal_scanner.engines.posterior.evaluate_precursors_at_time``
  as well as the canonical ``signal_scanner.engines.scanner`` path.
* The wrapper accepts both a tuple and a list of signatures (so
  callers do not have to coerce the type the pattern_library returns).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
)
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.signal_scanner.engines import posterior as posterior_module
from razor_rooster.signal_scanner.engines.scanner import (
    _evaluate_precursors,
    evaluate_precursors_at_time,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "scan.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _precursor_query_factory(
    observed_value: float,
) -> Callable[..., Any]:
    """Build a precursor query that emits a daily-indexed series.

    The returned callable matches the ``PrecursorVariable.query``
    contract: ``(conn, window_start, window_end) -> pd.Series`` with
    a tz-aware DatetimeIndex bounded by ``window_end``.
    """

    def _query(_conn: object, _start: datetime, end: datetime) -> pd.Series:
        idx = pd.date_range(end=end, periods=10, freq="D", tz="UTC")
        return pd.Series([observed_value] * 10, index=idx, dtype=float)

    return _query


def _precursor_empty(_conn: object, _start: datetime, _end: datetime) -> pd.Series:
    return pd.Series(dtype=float)


def _occurrences_empty(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _make_event_class(
    *,
    class_id: str = "test_evaluate_at_time",
    precursor_value: float = 8.0,
) -> EventClass:
    return EventClass(
        class_id=class_id,
        title="Test class for evaluate_precursors_at_time",
        description="Synthetic event class used by T-CB-017 contract tests.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences_empty,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="First precursor",
                query=_precursor_query_factory(precursor_value),
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )


def _make_signature(
    *,
    class_id: str,
    variable_id: str = "v1",
    threshold: float = 5.0,
) -> SignatureResult:
    return SignatureResult(
        class_id=class_id,
        variable_id=variable_id,
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=threshold,
        direction="high_signals_event",
        lead_time_window_days=180,
        pre_event_mean=8.0,
        pre_event_p25=6.0,
        pre_event_p50=8.0,
        pre_event_p75=10.0,
        baseline_mean=3.0,
        baseline_p25=1.5,
        baseline_p50=3.0,
        baseline_p75=4.5,
        hit_rate=0.7,
        false_positive_rate=0.2,
        sample_size_events=20,
        sample_size_baseline=200,
        confidence_score=0.8,
        low_confidence_warning=False,
        computed_at=datetime(2026, 5, 15, tzinfo=UTC),
    )


def test_wrapper_matches_private_path_on_fixed_corpus(store: DuckDBStore) -> None:
    """Anti-divergence: with as_of_ts == scan_started_at, the public
    wrapper produces the same ``current_values`` dict and the same
    ``source_stale`` flag as the live scanner's ``_evaluate_precursors``
    on the same fixture. This is the contract that lets the calibration
    backtest reuse the scanner without forking its precursor logic.
    """
    cls = _make_event_class(precursor_value=8.0)
    signatures: tuple[SignatureResult, ...] = (_make_signature(class_id=cls.class_id),)
    instant = datetime(2026, 5, 15, 8, tzinfo=UTC)

    live_values, live_stale = _evaluate_precursors(
        store=store,
        cls=cls,
        signatures=signatures,
        scan_started_at=instant,
    )
    frozen_values, frozen_stale = evaluate_precursors_at_time(
        store,
        cls,
        signatures,
        as_of_ts=instant,
    )

    assert frozen_values == live_values
    assert frozen_stale == live_stale
    assert frozen_values["v1"] == pytest.approx(8.0)
    assert frozen_stale is False


def test_wrapper_handles_list_signatures(store: DuckDBStore) -> None:
    """``library.signature`` returns a tuple, but downstream callers may
    hand in a list. The wrapper coerces internally so both shapes work.
    """
    cls = _make_event_class(precursor_value=4.2)
    sig_tuple = (_make_signature(class_id=cls.class_id),)
    instant = datetime(2026, 5, 15, 8, tzinfo=UTC)

    values_from_tuple, _ = evaluate_precursors_at_time(store, cls, sig_tuple, as_of_ts=instant)
    values_from_list, _ = evaluate_precursors_at_time(store, cls, list(sig_tuple), as_of_ts=instant)

    assert values_from_tuple == values_from_list


def test_wrapper_passes_through_lookback_days(store: DuckDBStore) -> None:
    """``lookback_days`` controls ``window_start``; the wrapper must
    forward it unchanged so backtest callers can override it.
    """
    captured_windows: list[tuple[datetime, datetime]] = []

    def _capturing_query(_conn: object, start: datetime, end: datetime) -> pd.Series:
        captured_windows.append((start, end))
        idx = pd.date_range(end=end, periods=3, freq="D", tz="UTC")
        return pd.Series([1.0, 2.0, 3.0], index=idx, dtype=float)

    cls = EventClass(
        class_id="test_lookback",
        title="Lookback passthrough",
        description="Captures the (start, end) tuple seen by the precursor.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences_empty,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="Capture",
                query=_capturing_query,
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )
    signatures = (_make_signature(class_id=cls.class_id),)
    instant = datetime(2026, 5, 15, 8, tzinfo=UTC)

    evaluate_precursors_at_time(store, cls, signatures, as_of_ts=instant, lookback_days=7)

    assert captured_windows == [(instant - timedelta(days=7), instant)]


def test_wrapper_returns_none_for_missing_data(store: DuckDBStore) -> None:
    """When the precursor query yields an empty series the wrapper
    reports ``None`` for that variable and flags ``source_stale``.
    """
    cls = EventClass(
        class_id="test_empty",
        title="Empty precursor",
        description="Precursor query returns an empty series.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences_empty,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="Empty",
                query=_precursor_empty,
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )
    signatures = (_make_signature(class_id=cls.class_id),)
    instant = datetime(2026, 5, 15, 8, tzinfo=UTC)

    values, stale = evaluate_precursors_at_time(store, cls, signatures, as_of_ts=instant)

    assert values == {"v1": None}
    assert stale is True


def test_public_function_reachable_via_posterior_module(store: DuckDBStore) -> None:
    """The design-doc-stated import path
    ``signal_scanner.engines.posterior.evaluate_precursors_at_time``
    must resolve to the same callable as the canonical scanner export.
    """
    via_posterior = posterior_module.evaluate_precursors_at_time
    assert via_posterior is evaluate_precursors_at_time

    cls = _make_event_class(precursor_value=8.0)
    signatures = (_make_signature(class_id=cls.class_id),)
    instant = datetime(2026, 5, 15, 8, tzinfo=UTC)

    direct, _ = evaluate_precursors_at_time(store, cls, signatures, as_of_ts=instant)
    via, _ = via_posterior(store, cls, signatures, as_of_ts=instant)
    assert direct == via


def test_public_function_listed_in_posterior_dunder_all() -> None:
    """``__all__`` advertises the wrapper from the posterior module so
    ``from ... import *`` consumers and static analysers see it.
    """
    assert "evaluate_precursors_at_time" in posterior_module.__all__
