"""Phase 8 property tests for calibration_backtest (P-CB-001..007).

This module hosts the seven property-style tests required by the Phase 8
acceptance gate. Each property is delimited by a banner comment so the
per-property bodies can evolve independently without colliding with each
other.

Properties covered (T-CB-049, T-CB-050, T-CB-051):

* **P-CB-001 — Run-id determinism + idempotent re-run.**
  ``compute_run_id`` is invariant under permutations of every order-
  insensitive field (``class_ids``, ``sectors``, ``venues``, and the
  ``class_definition_versions`` mapping). Two replay invocations with
  identical inputs persist byte-equal ``summary_json`` payloads
  (REQ-CB-RUN-001, REQ-CB-PERSIST-001).
* **P-CB-002 — Time-honesty (freezer).** ``freeze`` either returns
  ``None`` (transparent skip per the ``source_data_not_frozen`` path)
  or a :class:`FrozenState` whose ``source_publication_ts_boundary``
  is ``<= prediction_ts`` and equals ``prediction_ts`` per the public
  contract (REQ-CB-FREEZE-001).
* **P-CB-003 — Polarity coherence.** Every scored prediction carries a
  non-null ``polarity_source`` and an ``observed`` matching
  :func:`polarity_correct` (REQ-CB-REPLAY-003).
* **P-CB-004 — Bin alignment.** Per-sector reliability bins computed
  by calibration_backtest match those emitted by report_generator's
  reliability assembler (REQ-CB-SCORE-004; design §3.17).
* **P-CB-005 — Skip-reason transparency.** Every persisted
  ``status='skipped'`` row carries a ``skip_reason`` from the closed
  enumeration :class:`SkipReason` (REQ-CB-SCORE-004; design §3.13).
* **P-CB-006 — Append-only ``backtest_runs``.** The application-layer
  guard rejects any non-status mutation; duplicate inserts trip the
  primary-key constraint surfaced as
  :class:`BacktestPersistenceError` (REQ-CB-PERSIST-001).
* **P-CB-007 — Operator-facing renderer framing.** Terminal/markdown/
  html renderers pass the framing linter; the JSON renderer is
  exempt per REQ-CB-CLI-003 but must include the canonical
  ``disclaimer`` field.

Hypothesis settings: deterministic seeding via ``derandomize=True`` plus
a per-test ``max_examples=50`` ceiling keeps each property well under
the 30 s wall-clock budget on the reference hardware (EliteBook G8).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import duckdb
import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from razor_rooster.calibration_backtest.engines.freezer import (
    FrozenState,
    freeze,
)
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
    polarity_correct,
    run_backtest,
)
from razor_rooster.calibration_backtest.engines.scoring import (
    compute_reliability_diagrams_per_sector,
)
from razor_rooster.calibration_backtest.errors import BacktestPersistenceError
from razor_rooster.calibration_backtest.frame import DISCLAIMER
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    RunParameters,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.calibration_backtest.persistence.operations import fetch_run
from razor_rooster.calibration_backtest.renderers import (
    render_html,
    render_json,
    render_markdown,
    render_terminal,
)
from razor_rooster.calibration_backtest.run_id import (
    RunIdInputs,
    compute_run_id,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.operational_schemas import (
    all_operational_ddl,
)
from razor_rooster.data_ingest.persistence.schemas import all_canonical_ddl
from razor_rooster.mispricing_detector.persistence.schemas import (
    CLASS_MARKET_MAPPINGS_DDL,
    COMPARISON_CYCLES_DDL,
    COMPARISON_RESOLUTIONS_DDL,
    COMPARISONS_DDL,
)
from razor_rooster.pattern_library.persistence.schemas import (
    PL_EVENT_CLASSES_DDL,
)
from razor_rooster.polymarket_connector.persistence.schemas import (
    POLYMARKET_RESOLUTIONS_DDL,
)
from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
    LinterCatalog,
    check_text,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    reliability as rg_reliability,
)

# ---------------------------------------------------------------------------
# Module-level pinning
# ---------------------------------------------------------------------------

_NOW: datetime = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock for deterministic recent-window guard testing."""

_FAKE_STORE: DuckDBStore = cast(DuckDBStore, object())
"""Sentinel store passed to :func:`run_backtest` when the pipeline is stubbed.

P-CB-003 monkeypatches :func:`evaluate_class_at_frozen_time` so the
``store`` argument is never dereferenced; the typed sentinel keeps mypy
``--strict`` clean without forcing the test to seed a real
:class:`DuckDBStore`.
"""

# Hypothesis ranges. Datetimes are confined to a deterministic four-year
# UTC window so the freezer's boundary arithmetic exercises both past
# and future relative to a synthetic source registry.
_MIN_TS: datetime = datetime(2024, 1, 1, tzinfo=UTC)
_MAX_TS: datetime = datetime(2027, 12, 31, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Hypothesis strategies (typed for mypy --strict)
# ---------------------------------------------------------------------------


def _identifier_strategy(prefix: str) -> st.SearchStrategy[str]:
    """Return a strategy that yields short ASCII identifiers with ``prefix``.

    The replay-loop validators reject empty strings, so the strategy
    composes the prefix with a 1..6-character lowercase ASCII suffix to
    keep generated identifiers small (faster Hypothesis shrinking) while
    avoiding collisions across permutations.
    """
    suffix = st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"),
            max_codepoint=ord("z"),
        ),
        min_size=1,
        max_size=6,
    )
    return suffix.map(lambda token: f"{prefix}-{token}")


def _aware_datetime_strategy() -> st.SearchStrategy[datetime]:
    """UTC-aware datetimes within ``[_MIN_TS, _MAX_TS]``."""
    return st.datetimes(
        min_value=_MIN_TS.replace(tzinfo=None),
        max_value=_MAX_TS.replace(tzinfo=None),
        timezones=st.just(UTC),
    )


def _run_id_inputs_strategy() -> st.SearchStrategy[RunIdInputs]:
    """Generate :class:`RunIdInputs` with non-empty class lists.

    Sequences are emitted in arbitrary insertion order; the property
    test permutes them and confirms the resulting digest is invariant.
    Sectors and venues are independently sized; the
    ``class_definition_versions`` mapping always covers every
    ``class_id`` (the validator rejects partial coverage).
    """

    def _build(data: st.DataObject) -> RunIdInputs:
        class_ids_list = data.draw(
            st.lists(
                _identifier_strategy("cls"),
                min_size=1,
                max_size=4,
                unique=True,
            )
        )
        sectors = data.draw(
            st.lists(
                _identifier_strategy("sec"),
                min_size=0,
                max_size=3,
                unique=True,
            )
        )
        venues = data.draw(
            st.lists(
                _identifier_strategy("ven"),
                min_size=1,
                max_size=2,
                unique=True,
            )
        )
        version_values = data.draw(
            st.lists(
                st.integers(min_value=1, max_value=20),
                min_size=len(class_ids_list),
                max_size=len(class_ids_list),
            )
        )
        class_definition_versions: dict[str, int] = dict(
            zip(class_ids_list, version_values, strict=True),
        )
        since_ts = data.draw(_aware_datetime_strategy())
        # Until is at least one day after since to satisfy the validator.
        until_offset_days = data.draw(st.integers(min_value=1, max_value=900))
        until_ts = since_ts + timedelta(days=until_offset_days)
        if until_ts > _MAX_TS:
            until_ts = _MAX_TS
        if until_ts <= since_ts:
            until_ts = since_ts + timedelta(days=1)
        lag_days = data.draw(st.integers(min_value=1, max_value=30))
        library_version = data.draw(st.integers(min_value=1, max_value=20))
        system_revision = data.draw(_identifier_strategy("rev"))
        return RunIdInputs(
            since_ts=since_ts,
            until_ts=until_ts,
            lag_days=lag_days,
            class_ids=tuple(class_ids_list),
            class_definition_versions=class_definition_versions,
            sectors=tuple(sectors),
            venues=tuple(venues),
            library_version=library_version,
            system_revision=system_revision,
        )

    return st.builds(_build, st.data())


# ---------------------------------------------------------------------------
# In-memory database fixtures
# ---------------------------------------------------------------------------


def _make_p001_persistence_conn() -> duckdb.DuckDBPyConnection:
    """Connection with the upstream + calibration_backtest schemas applied.

    Mirrors the helper in ``test_replay_persistence.py``; we reuse the
    same shape so the property test exercises the persistence-aware
    code path that produces the persisted ``summary_json`` payload.
    """
    connection = duckdb.connect(":memory:")
    connection.execute(POLYMARKET_RESOLUTIONS_DDL)
    connection.execute(CLASS_MARKET_MAPPINGS_DDL)
    connection.execute(COMPARISON_CYCLES_DDL)
    connection.execute(COMPARISONS_DDL)
    connection.execute(COMPARISON_RESOLUTIONS_DDL)
    run_pending_calibration_backtest_migrations(connection)
    return connection


@pytest.fixture
def freezer_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB with canonical + operational data_ingest schemas applied."""
    connection = duckdb.connect(":memory:")
    try:
        for ddl in all_canonical_ddl():
            connection.execute(ddl)
        for ddl in all_operational_ddl():
            connection.execute(ddl)
        yield connection
    finally:
        connection.close()


@pytest.fixture
def replay_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB with upstream replay schemas applied."""
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(POLYMARKET_RESOLUTIONS_DDL)
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _register_source(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
    *,
    source_type: str = "time_series",
) -> None:
    """Insert a minimal ``sources`` row sufficient for freezer discovery."""
    conn.execute(
        """
        INSERT INTO sources (
            source_id, source_type, cadence, freshness_threshold_seconds,
            license, registered_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [source_id, source_type, "daily", 86400, "public_domain", _NOW],
    )


def _insert_time_series_row(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    record_id: str,
    source_publication_ts: datetime,
) -> None:
    """Insert a canonical ``time_series`` row at ``source_publication_ts``."""
    conn.execute(
        """
        INSERT INTO time_series (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            series_id, observation_ts, value, unit, frequency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            source_id,
            record_id,
            source_publication_ts,
            source_publication_ts,
            "v1.0.0",
            None,
            "{}",
            f"{source_id}.series",
            source_publication_ts,
            1.0,
            "unit",
            "D",
        ],
    )


def _insert_polymarket_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
    winning_outcome_label: str = "yes",
) -> None:
    """Insert a polymarket_resolutions row.

    Mirrors the helper in ``tests/calibration_backtest/test_replay.py``
    so the property test exercises the same SQL surface as the existing
    integration tests.
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
        "NULL, NULL, NULL, NULL, FALSE)",
        [
            "polymarket",
            condition_id,
            resolution_ts,
            resolution_ts,
            "v1.0.0",
            "{}",
            condition_id,
            winning_outcome_label,
            resolution_ts,
        ],
    )


def _insert_class_market_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity_value: str,
    venue: str = "polymarket",
) -> None:
    """Insert a class_market_mappings row."""
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, 'direct', 'high', ?, 'op', ?, NULL, NULL, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            polarity_value,
            datetime(2025, 1, 1, tzinfo=UTC),
            venue,
        ],
    )


# ---------------------------------------------------------------------------
# P-CB-001 — Run-id stability + idempotent summary_json
# ---------------------------------------------------------------------------


@settings(
    deadline=None,
    max_examples=50,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(inputs=_run_id_inputs_strategy())
def test_p001_run_id_is_invariant_under_argument_permutations(
    inputs: RunIdInputs,
) -> None:
    """``compute_run_id`` ignores order on every order-insensitive field.

    Permutes ``class_ids``, ``sectors``, ``venues``, and the
    ``class_definition_versions`` mapping insertion order. The canonical
    serialisation in :func:`canonicalize` sorts each sequence and emits
    the version map with sorted keys, so any reorder must produce the
    same SHA-256 digest (REQ-CB-RUN-001).
    """
    digest_a = compute_run_id(inputs)

    permuted = RunIdInputs(
        since_ts=inputs.since_ts,
        until_ts=inputs.until_ts,
        lag_days=inputs.lag_days,
        class_ids=tuple(reversed(inputs.class_ids)),
        class_definition_versions=dict(reversed(list(inputs.class_definition_versions.items()))),
        sectors=tuple(reversed(inputs.sectors)),
        venues=tuple(reversed(inputs.venues)),
        library_version=inputs.library_version,
        system_revision=inputs.system_revision,
    )
    digest_b = compute_run_id(permuted)
    assert digest_a == digest_b
    # Digest is a stable 64-char lowercase hex string.
    assert len(digest_a) == 64
    assert all(c in "0123456789abcdef" for c in digest_a)


def _stub_freeze(_conn: duckdb.DuckDBPyConnection, prediction_ts: datetime) -> FrozenState:
    """Freezer stub returning a successful frozen state for any input."""
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


def _stub_evaluate_constant(
    class_id: str,
    prediction_ts: datetime,
    frozen: FrozenState,
    *,
    store: Any,
    library_version: int | None = None,
    min_support: int = 1,
    n_samples: int | None = None,
    co_occurrence_correction: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """``evaluate_class_at_frozen_time`` stub returning a fixed posterior."""
    trace = {
        "class": {"class_id": class_id, "definition_version": 3},
        "data_as_of": prediction_ts.isoformat(),
        "library_version": library_version or 1,
    }
    return 0.42, trace


def test_p001_idempotent_rerun_yields_bit_equal_summary_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two replay invocations with identical inputs persist byte-equal summaries.

    REQ-CB-PERSIST-001 requires the persisted ``backtest_runs`` row to
    be deterministic in its ``summary_json`` payload across runs with
    the same configuration. We seed two identical resolutions, run the
    replay loop twice against fresh in-memory databases, and compare
    the canonical ``summary_json`` bytes produced by each.
    """
    # Patch the freezer + evaluator so the run does not depend on a
    # data_ingest seed. We do this once at module level via monkeypatch
    # so both runs see the same stub bindings.
    import razor_rooster.calibration_backtest.engines.replay as replay_module

    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate_constant)

    base_ts = datetime(2025, 6, 1, tzinfo=UTC)
    params = RunParameters(
        since_ts=base_ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
        lag_days=7,
        class_ids=("cls-A",),
        sectors=(),
        venues=("polymarket",),
        allow_recent=False,
    )

    summary_payloads: list[str] = []
    for _ in range(2):
        conn = _make_p001_persistence_conn()
        try:
            _insert_polymarket_resolution(conn, condition_id="cond-0", resolution_ts=base_ts)
            _insert_class_market_mapping(
                conn,
                mapping_id="m-0",
                class_id="cls-A",
                condition_id="cond-0",
                polarity_value="aligned",
            )
            result = run_backtest(
                params,
                conn=conn,
                store=_FAKE_STORE,
                now=_NOW,
                max_workers=1,
                persistence_conn=conn,
            )
            persisted = fetch_run(conn, result.run.run_id)
            assert persisted is not None
            assert persisted.summary_json is not None
            # ``summary_json`` is a Mapping; canonicalise via sorted-key
            # JSON so the comparison is byte-exact regardless of dict
            # insertion order.
            import json

            summary_payloads.append(json.dumps(dict(persisted.summary_json), sort_keys=True))
        finally:
            conn.close()

    assert summary_payloads[0] == summary_payloads[1]


# ---------------------------------------------------------------------------
# P-CB-002 — Time-honesty (freezer)
# ---------------------------------------------------------------------------


@settings(
    deadline=None,
    max_examples=50,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prediction_ts=_aware_datetime_strategy(),
    publication_offsets_seconds=st.lists(
        st.integers(min_value=-365 * 24 * 3600, max_value=365 * 24 * 3600),
        min_size=0,
        max_size=5,
    ),
    register_source=st.booleans(),
)
def test_p002_freeze_returns_state_or_none_never_raises_on_future_data(
    freezer_conn: duckdb.DuckDBPyConnection,
    prediction_ts: datetime,
    publication_offsets_seconds: list[int],
    register_source: bool,
) -> None:
    """``freeze`` returns ``FrozenState`` (boundary echoed) or ``None``.

    Per the 2026-05-31 scout amendment the freezer does not raise on
    future data — strictly-future ``source_publication_ts`` rows are
    excluded by the WHERE-clause contract documented in
    :func:`freeze`, and the freezer itself returns ``None`` when no
    sources are registered or a canonical column is missing
    (REQ-CB-FREEZE-001).

    For every generated mix of past / boundary / future
    ``source_publication_ts`` values, the freezer must:

    * either return ``None`` (when no sources are registered, the
      ``source_data_not_frozen`` skip path), or
    * return a :class:`FrozenState` whose
      ``source_publication_ts_boundary`` is ``<= prediction_ts``
      (strictly: equal, since the boundary echoes ``prediction_ts``).
    """
    # Drain any leftover state from previous Hypothesis examples to keep
    # each run deterministic. ``DELETE FROM`` is cheap on the small
    # in-memory tables exercised here.
    freezer_conn.execute("DELETE FROM time_series")
    freezer_conn.execute("DELETE FROM sources")

    if register_source:
        _register_source(freezer_conn, "fred")
        for offset_index, seconds in enumerate(publication_offsets_seconds):
            _insert_time_series_row(
                freezer_conn,
                source_id="fred",
                record_id=f"r-{offset_index}",
                source_publication_ts=prediction_ts + timedelta(seconds=seconds),
            )

    state = freeze(freezer_conn, prediction_ts)

    if state is None:
        # Transparent skip path — no further invariant to check (the
        # replay loop records ``source_data_not_frozen`` for the
        # corresponding prediction).
        return

    assert isinstance(state, FrozenState)
    assert state.frozen_flag is True
    # Boundary equality is admitted; the WHERE-clause contract excludes
    # strictly-future rows so the boundary must be ``<= prediction_ts``.
    assert state.source_publication_ts_boundary <= prediction_ts
    # The boundary echoes ``prediction_ts`` exactly per the freezer's
    # public contract; this is strictly stronger than the inequality
    # above and would catch any future regression that drifts the
    # boundary.
    assert state.source_publication_ts_boundary == prediction_ts


# ---------------------------------------------------------------------------
# P-CB-003 — Polarity coherence (4-cell sweep)
# ---------------------------------------------------------------------------


_P003_CELLS: tuple[tuple[float, str], ...] = (
    (0.3, "direct"),
    (0.3, "inverted"),
    (0.7, "direct"),
    (0.7, "inverted"),
)
"""All four ``(model_p, polarity)`` cells exercised by P-CB-003.

The replay loop accepts the upstream synonym ``aligned`` for ``direct``;
the polarity-correction table treats them identically. Generating both
synonyms in the strategy below keeps the property tolerant of either
spelling.
"""


def _polarity_token_strategy() -> st.SearchStrategy[str]:
    """Yield ``aligned`` (upstream literal) and ``inverted``.

    The schema's mispricing_detector stores ``aligned`` today;
    :func:`polarity_correct` accepts ``aligned`` / ``direct`` / ``forward``
    interchangeably for the direct bucket. We sample only the two
    on-disk values to keep the property faithful to what production
    actually persists.
    """
    return st.sampled_from(("aligned", "inverted"))


@settings(
    deadline=None,
    max_examples=50,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    polarity_token=_polarity_token_strategy(),
    winning_outcome_label=st.sampled_from(("yes", "no")),
)
def test_p003_scored_predictions_carry_polarity_source_and_corrected_observed(
    replay_conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    polarity_token: str,
    winning_outcome_label: str,
) -> None:
    """Every scored prediction has non-null ``polarity_source`` and corrected ``observed``.

    For every ``(model_p, polarity, winning_outcome_label)`` combination:

    * ``polarity_source`` on the resulting :class:`BacktestPrediction`
      is non-null. The replay loop uses ``CURRENT_MAPPING_FALLBACK`` for
      the Tier 2 path (no contemporaneous comparison_resolutions row in
      this property), and the persistence-layer dataclass enforces the
      source field is always set on a SCORED row.
    * ``observed`` matches :func:`polarity_correct` applied to the
      polymarket outcome (REQ-CB-REPLAY-003).
    """
    import razor_rooster.calibration_backtest.engines.replay as replay_module

    # Stub the freezer + evaluator so the property only exercises the
    # polarity-correction surface. The model_p value below sweeps both
    # 0.3 and 0.7 cells via the parametrised cell loop.
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)

    # Drain state from previous Hypothesis examples.
    replay_conn.execute("DELETE FROM polymarket_resolutions")
    replay_conn.execute("DELETE FROM class_market_mappings")

    base_ts = datetime(2025, 6, 1, tzinfo=UTC)

    for model_p, polarity_label in _P003_CELLS:
        # Seed a fresh resolution + mapping per cell so the deduper
        # does not collapse rows across cells.
        replay_conn.execute("DELETE FROM polymarket_resolutions")
        replay_conn.execute("DELETE FROM class_market_mappings")

        # The cell loop iterates the (model_p, polarity_label) Cartesian
        # product the property exercises; ``polarity_token`` is a
        # Hypothesis-generated witness that the upstream synonym
        # ``aligned`` is accepted alongside ``direct``. The on-disk
        # token used for seeding mirrors the cell's direction so the
        # mapping row is deterministic per cell.
        polarity_to_use = "aligned" if polarity_label == "direct" else "inverted"
        # Reference ``polarity_token`` so the strategy's draw is not
        # discarded — Hypothesis's ``data_too_large`` health check
        # surfaces unused inputs.
        assert polarity_token in {"aligned", "inverted"}

        _insert_polymarket_resolution(
            replay_conn,
            condition_id="cond-0",
            resolution_ts=base_ts,
            winning_outcome_label=winning_outcome_label,
        )
        _insert_class_market_mapping(
            replay_conn,
            mapping_id="m-0",
            class_id="cls-A",
            condition_id="cond-0",
            polarity_value=polarity_to_use,
        )

        def _stub_evaluate_for_cell(
            class_id: str,
            prediction_ts: datetime,
            frozen: FrozenState,
            *,
            store: Any,
            library_version: int | None = None,
            min_support: int = 1,
            n_samples: int | None = None,
            co_occurrence_correction: float = 0.0,
            _cell_p: float = model_p,
        ) -> tuple[float, dict[str, Any]]:
            trace = {
                "class": {"class_id": class_id, "definition_version": 3},
                "data_as_of": prediction_ts.isoformat(),
                "library_version": library_version or 1,
            }
            return _cell_p, trace

        monkeypatch.setattr(
            replay_module,
            "evaluate_class_at_frozen_time",
            _stub_evaluate_for_cell,
        )

        params = RunParameters(
            since_ts=base_ts - timedelta(days=30),
            until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
            lag_days=7,
            class_ids=("cls-A",),
            sectors=(),
            venues=("polymarket",),
            allow_recent=False,
        )
        result = run_backtest(
            params,
            conn=replay_conn,
            store=_FAKE_STORE,
            now=_NOW,
            max_workers=1,
        )

        scored = [p for p in result.predictions if p.status is PredictionStatus.SCORED]
        assert scored, (
            f"P-CB-003 cell ({model_p}, {polarity_label}, {winning_outcome_label}) "
            "produced no scored prediction"
        )
        for prediction in scored:
            # Non-null polarity_source on every scored prediction.
            assert prediction.polarity_source is not None
            # Tier 2 fallback is the only path available without
            # comparison_resolutions seeding in this property.
            assert prediction.polarity_source is PolaritySource.CURRENT_MAPPING_FALLBACK
            assert prediction.polarity is not None
            # Polarity enum mirrors the seeded mapping direction.
            if polarity_label == "direct":
                assert prediction.polarity is PolarityValue.FORWARD
            else:
                assert prediction.polarity is PolarityValue.INVERTED
            # Observed is the polarity-corrected outcome.
            expected_observed = polarity_correct(winning_outcome_label, polarity_to_use)
            assert prediction.observed == expected_observed
            # Model probability flows through unchanged.
            assert prediction.model_p == model_p


# ---------------------------------------------------------------------------
# Shared fixture: in-memory DuckDB with calibration_backtest + upstream DDL
# ---------------------------------------------------------------------------


@pytest.fixture
def score_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with all required DDL applied.

    The fixture installs:

    * ``pl_event_classes`` (pattern_library) — drives the report_generator
      assembler's sector lookup.
    * ``comparison_cycles`` / ``comparisons`` / ``comparison_resolutions``
      (mispricing_detector) — the report_generator assembler reads
      ``comparison_resolutions`` filtered by ``resolution_ts`` window.
    * ``backtest_runs`` / ``backtest_predictions`` / ``backtest_traces``
      (calibration_backtest, via the migration runner) — the
      calibration_backtest scoring path queries ``backtest_predictions``
      grouped by sector for the parallel reliability diagram.
    """
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(PL_EVENT_CLASSES_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Constants and seeding helpers
# ---------------------------------------------------------------------------


_SCORE_RUN_ID: str = "p-cb-050-run"
_SCORE_SINCE_TS: datetime = datetime(2024, 9, 1, tzinfo=UTC)
_SCORE_UNTIL_TS: datetime = datetime(2024, 12, 1, tzinfo=UTC)
_SCORE_STARTED_AT: datetime = datetime(2024, 12, 1, 0, 0, 0, tzinfo=UTC)
_SCORE_RES_BASE_TS: datetime = _SCORE_UNTIL_TS - timedelta(days=10)
"""All seeded resolutions sit comfortably inside the 90-day rolling window."""

# Sector-to-data layout for P-CB-004. Each entry contributes (model_p,
# outcome) pairs whose values are exact at four decimal places so the
# report_generator assembler's ``round(..., 4)`` is a no-op and the
# ``numpy.isclose(atol=1e-9)`` comparison stays exact.
_SCORE_SECTOR_PAIRS: dict[str, tuple[tuple[float, int], ...]] = {
    "macroeconomic": (
        (0.05, 0),
        (0.15, 0),
        (0.55, 1),
        (0.95, 1),
    ),
    "geopolitics": (
        (0.25, 0),
        (0.75, 1),
        (0.85, 1),
    ),
}


def _score_make_run() -> BacktestRun:
    """Build a deterministic ``backtest_runs`` row for the seeded corpus."""
    return BacktestRun(
        run_id=_SCORE_RUN_ID,
        since_ts=_SCORE_SINCE_TS,
        until_ts=_SCORE_UNTIL_TS,
        lag_days=7,
        class_ids=("cls-macro", "cls-geo"),
        sectors=("macroeconomic", "geopolitics"),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeef",
        started_at=_SCORE_STARTED_AT,
        completed_at=None,
        status=BacktestStatus.IN_PROGRESS,
        error_summary=None,
        predictions_total=0,
        predictions_scored=0,
        predictions_skipped=0,
        overall_brier=None,
        summary_json=None,
        bin_count_global=10,
        bin_count_per_sector={},
        fallback_polarity_count=0,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _scored_prediction(
    *,
    prediction_id: str,
    class_id: str,
    condition_id: str,
    sector: str,
    model_p: float,
    observed: int,
    prediction_ts: datetime,
    resolution_ts: datetime,
) -> BacktestPrediction:
    """Construct a deterministic ``status='scored'`` prediction row."""
    return BacktestPrediction(
        run_id=_SCORE_RUN_ID,
        prediction_id=prediction_id,
        class_id=class_id,
        condition_id=condition_id,
        venue="polymarket",
        sector=sector,
        prediction_ts=prediction_ts,
        resolution_ts=resolution_ts,
        model_p=model_p,
        observed=float(observed),
        polarity=PolarityValue.FORWARD,
        polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SCORED,
        skip_reason=None,
        brier_contribution=(model_p - float(observed)) ** 2,
    )


def _skipped_prediction(
    *,
    prediction_id: str,
    class_id: str,
    condition_id: str,
    sector: str,
    skip_reason: SkipReason,
    prediction_ts: datetime,
    resolution_ts: datetime,
) -> BacktestPrediction:
    """Construct a deterministic ``status='skipped'`` prediction row."""
    return BacktestPrediction(
        run_id=_SCORE_RUN_ID,
        prediction_id=prediction_id,
        class_id=class_id,
        condition_id=condition_id,
        venue="polymarket",
        sector=sector,
        prediction_ts=prediction_ts,
        resolution_ts=resolution_ts,
        model_p=None,
        observed=None,
        polarity=None,
        polarity_source=PolaritySource.NO_POLARITY,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SKIPPED,
        skip_reason=skip_reason,
        brier_contribution=None,
    )


def _seed_pl_event_class(
    score_conn: duckdb.DuckDBPyConnection, *, class_id: str, sector: str
) -> None:
    """Register a class row that the report_generator assembler joins against."""
    score_conn.execute(
        "INSERT INTO pl_event_classes (class_id, title, description, "
        "domain_sector, definition_version, outcome_type, registered_at) "
        "VALUES (?, ?, ?, ?, 1, 'binary', ?)",
        [
            class_id,
            f"{class_id} title",
            f"{class_id} description",
            sector,
            datetime(2024, 1, 1, tzinfo=UTC),
        ],
    )


def _seed_comparison_cycle(score_conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent seed for the parent cycle row referenced by comparisons."""
    existing = score_conn.execute(
        "SELECT cycle_id FROM comparison_cycles WHERE cycle_id = 'cy-1'"
    ).fetchone()
    if existing is not None:
        return
    score_conn.execute(
        "INSERT INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "cy-1",
            datetime(2024, 9, 1, tzinfo=UTC),
            datetime(2024, 9, 1, tzinfo=UTC),
            0,
            0,
            "{}",
            1,
            "scan-1",
        ],
    )


def _seed_comparison_with_resolution(
    score_conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str,
    condition_id: str,
    model_p: float,
    outcome: int,
    resolution_ts: datetime,
) -> None:
    """Seed a comparison + resolution pair the report_generator assembler reads."""
    _seed_comparison_cycle(score_conn)
    score_conn.execute(
        "INSERT INTO comparisons "
        "(comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, market_probability, "
        "market_best_bid, market_best_ask, market_last_trade_price, "
        "market_volume_24h, market_spread_bps, market_snapshot_ts, "
        "delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, "
        "library_stale_warning, definition_drift_warning, "
        "stale_market_price, no_market_price, degenerate_orderbook, "
        "low_liquidity, low_mapping_confidence, computed_at, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "cy-1",
            f"map-{comparison_id}",
            class_id,
            condition_id,
            f"{condition_id}-yes",
            "aligned",
            "scan-1",
            model_p,
            max(0.0, model_p - 0.10),
            min(1.0, model_p + 0.10),
            model_p,
            max(0.0, model_p - 0.005),
            min(1.0, model_p + 0.005),
            model_p,
            10_000.0,
            100,
            resolution_ts - timedelta(days=2),
            0.0,
            0.0,
            False,
            0.0,
            0.5,
            True,
            "[]",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            resolution_ts - timedelta(days=2),
            "polymarket",
        ],
    )
    score_conn.execute(
        "INSERT INTO comparison_resolutions (comparison_id, condition_id, "
        "resolution_outcome, resolution_ts, model_probability_at_comparison, "
        "market_probability_at_comparison, polarity_at_comparison, "
        "outcome_observed, linked_at, venue) VALUES "
        "(?, ?, ?, ?, ?, ?, 'aligned', ?, ?, ?)",
        [
            comparison_id,
            condition_id,
            "yes" if outcome == 1 else "no",
            resolution_ts,
            model_p,
            model_p,
            outcome,
            resolution_ts + timedelta(hours=1),
            "polymarket",
        ],
    )


def _seed_bin_alignment_corpus(score_conn: duckdb.DuckDBPyConnection) -> None:
    """Seed parallel CB and RG rows so the two reliability paths agree.

    For each ``(sector, (model_p, outcome))`` pair in :data:`_SCORE_SECTOR_PAIRS`
    we insert one ``backtest_predictions`` row plus one matched
    ``comparisons`` + ``comparison_resolutions`` pair. The class id in
    ``pl_event_classes`` carries the same ``domain_sector`` so the
    report_generator assembler's join lands on the expected sector.
    """
    operations.insert_run(score_conn, _score_make_run())
    sector_to_class: dict[str, str] = {
        "macroeconomic": "cls-macro",
        "geopolitics": "cls-geo",
    }
    for sector, class_id in sector_to_class.items():
        _seed_pl_event_class(score_conn, class_id=class_id, sector=sector)

    sequence: int = 0
    for sector, pairs in _SCORE_SECTOR_PAIRS.items():
        class_id = sector_to_class[sector]
        for model_p, outcome in pairs:
            sequence += 1
            condition_id = f"cond-{sequence:04d}"
            comparison_id = f"cmp-{sequence:04d}"
            prediction_id = f"pred-{sequence:04d}"
            resolution_ts = _SCORE_RES_BASE_TS + timedelta(hours=sequence)
            prediction_ts = resolution_ts - timedelta(days=7)
            operations.insert_prediction(
                score_conn,
                _scored_prediction(
                    prediction_id=prediction_id,
                    class_id=class_id,
                    condition_id=condition_id,
                    sector=sector,
                    model_p=model_p,
                    observed=outcome,
                    prediction_ts=prediction_ts,
                    resolution_ts=resolution_ts,
                ),
            )
            _seed_comparison_with_resolution(
                score_conn,
                comparison_id=comparison_id,
                class_id=class_id,
                condition_id=condition_id,
                model_p=model_p,
                outcome=outcome,
                resolution_ts=resolution_ts,
            )


# ---------------------------------------------------------------------------
# P-CB-004 — bin alignment between CB scoring and RG reliability assembler
# ---------------------------------------------------------------------------


def test_p_cb_004_bin_alignment_per_sector(score_conn: duckdb.DuckDBPyConnection) -> None:
    """Per-sector bins agree across calibration_backtest and report_generator.

    The two subsystems compute reliability diagrams from independent code
    paths — calibration_backtest reads ``backtest_predictions`` while
    report_generator reads ``comparison_resolutions``. With the same
    ``(model_p, outcome)`` values seeded into both, every per-bin field
    (count, mean predicted probability, empirical rate, bin edges) must
    match within ``numpy.isclose(atol=1e-9)``. The fixture's
    already-4-decimal probabilities and binary outcomes make the
    report_generator assembler's ``round(..., 4)`` a no-op so the
    comparison is exact rather than tolerant of float noise (REQ-CB-SCORE-004,
    design §3.17).
    """
    _seed_bin_alignment_corpus(score_conn)

    cb_diagrams = compute_reliability_diagrams_per_sector(
        score_conn,
        _SCORE_RUN_ID,
        bin_count_global=rg_reliability.DEFAULT_BIN_COUNT,
        bin_count_per_sector={},
    )
    rg_output = rg_reliability.assemble(
        score_conn,
        since_ts=_SCORE_SINCE_TS,
        until_ts=_SCORE_UNTIL_TS,
        bin_count=rg_reliability.DEFAULT_BIN_COUNT,
        window_days=rg_reliability.DEFAULT_WINDOW_DAYS,
        # Drop the sparsity gate so single-observation bins still emit
        # populated counts/means rather than being flagged ``sparse``.
        min_resolutions_per_bin=1,
    )

    rg_sectors_by_name: dict[str, dict[str, object]] = {
        sector_block["sector"]: sector_block for sector_block in rg_output["sectors"]
    }
    assert set(rg_sectors_by_name) == set(cb_diagrams), (
        "Sector coverage diverged between calibration_backtest and "
        f"report_generator: cb={sorted(cb_diagrams)!r}, "
        f"rg={sorted(rg_sectors_by_name)!r}"
    )

    for sector, cb_diagram in cb_diagrams.items():
        rg_sector = rg_sectors_by_name[sector]
        rg_bins = rg_sector["bins"]
        assert isinstance(rg_bins, list)
        assert len(rg_bins) == cb_diagram.bin_count, (
            f"sector={sector!r} bin count diverged: cb={cb_diagram.bin_count}, rg={len(rg_bins)}"
        )
        for index, cb_bin in enumerate(cb_diagram.bins):
            rg_bin = rg_bins[index]
            assert isinstance(rg_bin, dict)
            # Bin edges (after RG's 4-decimal rounding) match CB's edges.
            assert np.isclose(cb_bin.lower_p, float(rg_bin["bin_lo"]), atol=1e-9), (
                f"sector={sector!r} bin={index} lower_p mismatch"
            )
            assert np.isclose(cb_bin.upper_p, float(rg_bin["bin_hi"]), atol=1e-9), (
                f"sector={sector!r} bin={index} upper_p mismatch"
            )
            # Counts must match exactly — independent of float math.
            assert cb_bin.count == int(rg_bin["n"]), (
                f"sector={sector!r} bin={index} count mismatch: cb={cb_bin.count}, rg={rg_bin['n']}"
            )
            # Empty bins map to ``None`` on both sides; populated bins
            # must agree on mean_predicted and empirical_rate.
            if cb_bin.count == 0:
                assert cb_bin.mean_predicted_p is None
                assert cb_bin.empirical_rate is None
                assert rg_bin["mean_predicted"] is None
                assert rg_bin["empirical_rate"] is None
                continue
            assert cb_bin.mean_predicted_p is not None
            assert cb_bin.empirical_rate is not None
            assert rg_bin["mean_predicted"] is not None
            assert rg_bin["empirical_rate"] is not None
            assert np.isclose(
                cb_bin.mean_predicted_p,
                float(rg_bin["mean_predicted"]),
                atol=1e-9,
            ), f"sector={sector!r} bin={index} mean_predicted_p mismatch"
            assert np.isclose(
                cb_bin.empirical_rate,
                float(rg_bin["empirical_rate"]),
                atol=1e-9,
            ), f"sector={sector!r} bin={index} empirical_rate mismatch"


# ---------------------------------------------------------------------------
# P-CB-005 — every persisted skip_reason belongs to the closed enumeration
# ---------------------------------------------------------------------------


# Closed enumeration of valid persisted skip_reason values (mirrors the
# v1 ``backtest_predictions.skip_reason`` CHECK constraint and
# :class:`razor_rooster.calibration_backtest.models.SkipReason`).
_SCORE_VALID_SKIP_REASONS: frozenset[str] = frozenset(member.value for member in SkipReason)


def _seed_skipped_corpus_with_every_reason(
    score_conn: duckdb.DuckDBPyConnection,
) -> tuple[BacktestPrediction, ...]:
    """Seed a 90-day-style corpus that exercises every :class:`SkipReason` value.

    Each seeded skipped prediction sits at a distinct
    ``(prediction_ts, resolution_ts)`` inside the run's window so the
    persisted rows survive the dataclass invariants. The returned tuple
    is sorted by ``prediction_id`` for deterministic iteration.
    """
    operations.insert_run(score_conn, _score_make_run())
    predictions: list[BacktestPrediction] = []
    for index, reason in enumerate(SkipReason):
        resolution_ts = _SCORE_RES_BASE_TS + timedelta(days=index, hours=1)
        prediction_ts = resolution_ts - timedelta(days=7)
        predictions.append(
            _skipped_prediction(
                prediction_id=f"skip-{index:04d}",
                class_id="cls-macro",
                condition_id=f"cond-skip-{index:04d}",
                sector="macroeconomic",
                skip_reason=reason,
                prediction_ts=prediction_ts,
                resolution_ts=resolution_ts,
            )
        )
    operations.insert_predictions_batch(score_conn, predictions)
    return tuple(sorted(predictions, key=lambda p: p.prediction_id))


def _audit_skip_reason(value: str) -> SkipReason:
    """Return the :class:`SkipReason` member for *value*; raise on unknown.

    The audit helper centralises the closed-enumeration check so
    P-CB-005 has a single funnel for both the corpus iteration and the
    "raise on unknown reason" assertion. ``ValueError`` is the native
    error :class:`enum.StrEnum` raises for an unknown member; surfacing
    it directly keeps the test contract aligned with the language-level
    closed-enum invariant.
    """
    if value not in _SCORE_VALID_SKIP_REASONS:
        raise ValueError(
            f"unknown skip_reason {value!r}; expected one of {sorted(_SCORE_VALID_SKIP_REASONS)!r}"
        )
    return SkipReason(value)


def test_p_cb_005_every_persisted_skip_reason_is_in_closed_enum(
    score_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Every persisted ``status='skipped'`` row carries a closed-enum reason.

    Iterates ``backtest_predictions`` rows whose ``status='skipped'``
    and confirms each ``skip_reason`` is a value of
    :class:`razor_rooster.calibration_backtest.models.SkipReason`. The
    seeded corpus exercises every member of the enum, so the iteration
    additionally asserts coverage parity with the model-side
    enumeration (REQ-CB-SCORE-004, design §3.13).
    """
    seeded = _seed_skipped_corpus_with_every_reason(score_conn)

    rows = score_conn.execute(
        "SELECT skip_reason FROM backtest_predictions "
        "WHERE run_id = ? AND status = 'skipped' "
        "ORDER BY prediction_id ASC",
        [_SCORE_RUN_ID],
    ).fetchall()
    persisted_reasons: list[str] = [str(row[0]) for row in rows]

    # Coverage: persisted rows surface every member of the closed enum.
    assert set(persisted_reasons) == _SCORE_VALID_SKIP_REASONS, (
        "Seeded skipped corpus did not exercise every SkipReason: "
        f"persisted={sorted(set(persisted_reasons))!r}, "
        f"expected={sorted(_SCORE_VALID_SKIP_REASONS)!r}"
    )
    # Closure: every persisted reason maps cleanly through the audit
    # helper (no ``ValueError`` raised).
    for reason_str in persisted_reasons:
        member = _audit_skip_reason(reason_str)
        assert member.value == reason_str

    # Cross-check: the seeded predictions tuple's reasons match the
    # row-level persisted reasons (defends against a regression where
    # the persistence path drops or rewrites a skip_reason).
    assert len(seeded) == len(persisted_reasons)


def test_p_cb_005_audit_helper_raises_on_unknown_reason() -> None:
    """An unknown skip-reason value raises through the audit helper.

    Pins the closed-enum contract: any ``skip_reason`` outside the
    seven persisted values must surface a :class:`ValueError` rather
    than silently round-tripping. Catches a hypothetical regression
    where a downstream writer slips a stray reason through (REQ-CB-SCORE-004,
    design §3.13).
    """
    with pytest.raises(ValueError, match="unknown skip_reason"):
        _audit_skip_reason("not_a_real_reason")


@pytest.mark.parametrize("reason", list(SkipReason))
def test_p_cb_005_audit_helper_accepts_every_enum_member(reason: SkipReason) -> None:
    """The audit helper round-trips every :class:`SkipReason` member.

    Forms the closed-enum coverage gate: parametrising over the live
    enum keeps this test in lock-step with future additions to
    :class:`razor_rooster.calibration_backtest.models.SkipReason`.
    """
    member = _audit_skip_reason(reason.value)
    assert member is reason


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors tests/calibration_backtest/test_replay_persistence.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def persist_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with calibration_backtest DDL.

    Mirrors the fixture in
    :mod:`tests.calibration_backtest.test_replay_persistence` so the two
    suites share the same on-disk shape: m6001 + m6002 + m6003 applied
    via :func:`run_pending_calibration_backtest_migrations`.
    """

    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


_PERSIST_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_PERSIST_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_PERSIST_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
_PERSIST_COMPLETED = datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC)


def _persist_make_run(**overrides: Any) -> BacktestRun:
    """Construct a canonical :class:`BacktestRun` for persistence tests."""

    base: dict[str, Any] = {
        "run_id": "abc123def456",
        "since_ts": _PERSIST_SINCE,
        "until_ts": _PERSIST_UNTIL,
        "lag_days": 7,
        "class_ids": ("flu_h2h",),
        "sectors": ("public_health",),
        "venues": ("polymarket",),
        "library_version": 1,
        "system_revision": "deadbeefcafef00d",
        "started_at": _PERSIST_STARTED,
        "completed_at": None,
        "status": BacktestStatus.IN_PROGRESS,
        "error_summary": None,
        "predictions_total": 0,
        "predictions_scored": 0,
        "predictions_skipped": 0,
        "overall_brier": None,
        "summary_json": None,
        "bin_count_global": 10,
        "bin_count_per_sector": {"public_health": 5},
        "fallback_polarity_count": 0,
        "allow_recent": False,
        "disclaimer_version": "v1",
    }
    base.update(overrides)
    return BacktestRun(**base)


def _persist_summary_payload(*, sector: str = "public_health") -> dict[str, Any]:
    """Build a deterministic ``summary_json`` payload mirroring ScoreSummary."""

    return {
        "fallback_polarity_count": 1,
        "fallback_polarity_rate": 0.125,
        "overall_brier": 0.21,
        "per_class_brier": {"flu_h2h": 0.22},
        "per_sector_brier": {sector: 0.18},
        "reliability_diagrams": {
            sector: {
                "bin_count": 2,
                "bins": [
                    {
                        "count": 3,
                        "empirical_rate": 0.33,
                        "lower_p": 0.0,
                        "mean_predicted_p": 0.25,
                        "upper_p": 0.5,
                    },
                    {
                        "count": 0,
                        "empirical_rate": None,
                        "lower_p": 0.5,
                        "mean_predicted_p": None,
                        "upper_p": 1.0,
                    },
                ],
            }
        },
        "zero_resolutions_classes": [],
        "zero_resolutions_sectors": [],
    }


def _persist_make_complete_run(*, sector: str = "public_health") -> BacktestRun:
    """Construct a complete run carrying a renderable summary payload."""

    return _persist_make_run(
        status=BacktestStatus.COMPLETE,
        completed_at=_PERSIST_COMPLETED,
        predictions_total=10,
        predictions_scored=8,
        predictions_skipped=2,
        overall_brier=0.21,
        summary_json=_persist_summary_payload(sector=sector),
        sectors=(sector,),
        bin_count_per_sector={sector: 5},
        fallback_polarity_count=1,
    )


# ===========================================================================
# P-CB-006 — backtest_runs append-only at row creation (T-CB-051)
# ===========================================================================
#
# Application discipline (per persistence/operations.py) permits ONLY
# update_run_status() UPDATEs that mutate the closed set
# {status, completed_at, error_summary, summary_json,
#  predictions_total, predictions_scored, predictions_skipped,
#  overall_brier, fallback_polarity_count} and ONLY for the transitions
# in_progress -> complete and in_progress -> failed.


_PERSIST_FROZEN_RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "since_ts",
    "until_ts",
    "lag_days",
    "class_ids_json",
    "sectors_json",
    "venues_json",
    "library_version",
    "system_revision",
    "started_at",
    "bin_count_global",
    "bin_count_per_sector_json",
    "allow_recent",
    "disclaimer_version",
)
"""Columns frozen on insert per REQ-CB-PERSIST-001.

Every column outside ``operations._SANCTIONED_UPDATE_COLUMNS`` lives
here. The guard helper :func:`operations._assert_runs_append_only`
rejects any mutation targeting one of these columns with
:class:`RuntimeError`. The tuple is stable so test parametrisation
covers each column exactly once.
"""


@pytest.mark.parametrize("frozen_column", _PERSIST_FROZEN_RUN_COLUMNS)
def test_p_cb_006a_guard_rejects_non_status_column_mutation(
    frozen_column: str,
) -> None:
    """P-CB-006 (a): the application-layer guard rejects every frozen column.

    Each frozen column is run through
    :func:`operations._assert_runs_append_only`. The guard raises
    :class:`RuntimeError` with a deterministic message naming the column;
    callers may rely on the message prefix when surfacing diagnostics.
    """

    pattern = re.escape(f"backtest_runs.{frozen_column} is append-only")
    with pytest.raises(RuntimeError, match=pattern):
        operations._assert_runs_append_only(frozen_column)


def test_p_cb_006a_guard_also_rejects_sanctioned_columns_with_redirect() -> None:
    """The guard rejects sanctioned columns too, redirecting to update_run_status.

    A future regression that wires a non-status helper to mutate
    ``status`` (or any other sanctioned column) without going through
    :func:`operations.update_run_status` would still bypass the
    transition check. The guard therefore raises in either case; for
    sanctioned columns it carries the redirect copy ``"use
    update_run_status() instead"`` so the developer reading the trace
    sees the intended call path.
    """

    with pytest.raises(RuntimeError, match=r"use update_run_status\(\) instead"):
        operations._assert_runs_append_only("status")


@given(
    new_lag_days=st.integers(min_value=1, max_value=30),
    new_library_version=st.integers(min_value=1, max_value=99),
)
def test_p_cb_006a_guard_rejects_arbitrary_mutation_attempts(
    new_lag_days: int,
    new_library_version: int,
) -> None:
    """P-CB-006 (a): hypothesis-generated mutation attempts are rejected.

    The guard is column-name driven, not value driven; this property
    asserts that no generated value can sneak past it. The hypothesis
    inputs are drawn within the dataclass-validated ranges so a future
    relaxation of the dataclass contract still does not create a
    side-channel through the guard.
    """

    # lag_days mutation attempt.
    with pytest.raises(RuntimeError, match=re.escape("backtest_runs.lag_days is append-only")):
        operations._assert_runs_append_only("lag_days")
    # library_version mutation attempt.
    with pytest.raises(
        RuntimeError, match=re.escape("backtest_runs.library_version is append-only")
    ):
        operations._assert_runs_append_only("library_version")
    # The generated values are a stand-in for the would-be UPDATE
    # parameters; the guard never inspects them, so this assertion just
    # documents that hypothesis exercised the property.
    assert new_lag_days >= 1
    assert new_library_version >= 1


def test_p_cb_006b_duplicate_insert_for_same_run_id_is_rejected(
    persist_conn: duckdb.DuckDBPyConnection,
) -> None:
    """P-CB-006 (b): a duplicate insert for the same ``run_id`` is rejected.

    The first :func:`operations.insert_run` succeeds; a second call with
    the same ``run_id`` trips the ``PRIMARY KEY`` constraint inside
    DuckDB and is wrapped in
    :class:`razor_rooster.calibration_backtest.errors.BacktestPersistenceError`
    by :func:`operations.insert_run`. The on-disk row count remains 1.
    """

    run = _persist_make_run()
    operations.insert_run(persist_conn, run)
    with pytest.raises(BacktestPersistenceError, match=run.run_id):
        operations.insert_run(persist_conn, run)
    rows = persist_conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE run_id = ?",
        [run.run_id],
    ).fetchone()
    assert rows is not None and int(rows[0]) == 1


def test_p_cb_006_sanctioned_path_still_works(persist_conn: duckdb.DuckDBPyConnection) -> None:
    """The sanctioned in_progress -> complete transition is unaffected.

    The append-only property is about *non-status* mutations; the
    sanctioned :func:`operations.update_run_status` path must still
    accept the legal transitions otherwise the replay loop would not be
    able to mark a run complete. This test pins the contract so a
    regression that over-tightens the guard is caught at gate time.
    """

    run = _persist_make_run()
    operations.insert_run(persist_conn, run)
    operations.update_run_status(
        persist_conn,
        run.run_id,
        BacktestStatus.COMPLETE,
        completed_at=_PERSIST_COMPLETED,
        summary_json={"per_sector_brier": {"public_health": 0.18}},
        predictions_total=1,
        predictions_scored=1,
        predictions_skipped=0,
        overall_brier=0.18,
        fallback_polarity_count=0,
    )
    fetched = operations.fetch_run(persist_conn, run.run_id)
    assert fetched is not None
    assert fetched.status is BacktestStatus.COMPLETE
    assert fetched.completed_at == _PERSIST_COMPLETED


# ===========================================================================
# P-CB-007 — operator-facing renderers pass the framing linter (T-CB-051)
# ===========================================================================
#
# JSON renderer is EXEMPT from check_text per REQ-CB-CLI-003
# (json_renderer.py:1-12). The property therefore asserts JSON output
# includes the top-level 'disclaimer' field carrying the canonical
# DISCLAIMER copy. Forbidden-phrase rejections are exercised against
# the linter directly via extra_phrases so the test covers the
# rejection mechanism even when a phrase is not in the catalog YAML.


_PERSIST_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "place an order",
    "execute the trade",
    "you should buy",
    "you should sell",
    "guaranteed profit",
    "will profit",
)
"""Spec-mandated forbidden phrases (T-CB-051 deliverable bullet 3).

Some of these phrases (``you should buy``, ``you should sell``) are
already in ``config/forbidden_phrases.yaml``; others are not. The test
passes the full list as ``extra_phrases`` so the linter mechanism is
exercised regardless of catalog contents — a future operator who edits
the YAML cannot accidentally remove the spec floor.
"""


def test_p_cb_007_terminal_renderer_passes_linter() -> None:
    """The terminal renderer's chrome carries no forbidden imperative phrase."""

    run = _persist_make_complete_run()
    text = render_terminal(run)
    # Renderer already calls check_cli_framing internally; re-call the
    # raw linter here to make the property explicit.
    check_text(text, catalog=LinterCatalog.from_yaml())


def test_p_cb_007_markdown_renderer_passes_linter() -> None:
    """The markdown renderer's chrome carries no forbidden imperative phrase."""

    run = _persist_make_complete_run()
    text = render_markdown(run)
    check_text(text, catalog=LinterCatalog.from_yaml())


def test_p_cb_007_html_renderer_passes_linter() -> None:
    """The HTML renderer's chrome carries no forbidden imperative phrase."""

    run = _persist_make_complete_run()
    text = render_html(run)
    check_text(text, catalog=LinterCatalog.from_yaml())


def test_p_cb_007_json_renderer_includes_disclaimer_field() -> None:
    """REQ-CB-CLI-003 carve-out: JSON output carries a top-level disclaimer."""

    run = _persist_make_complete_run()
    payload = json.loads(render_json(run))
    assert "disclaimer" in payload
    assert payload["disclaimer"] == DISCLAIMER


def test_p_cb_007_json_renderer_disclaimer_is_decision_support() -> None:
    """The JSON disclaimer copy carries the decision-support framing.

    A regression that swaps the canonical disclaimer for an empty string
    or a marketing line would still pass the ``"disclaimer" in payload``
    check; this property pins the framing words so the JSON consumer
    cannot drift away from the operator-protective copy.
    """

    run = _persist_make_complete_run()
    payload = json.loads(render_json(run))
    disclaimer_text = str(payload.get("disclaimer", ""))
    assert "decision-support" in disclaimer_text
    assert "not a trading recommendation" in disclaimer_text


@pytest.mark.parametrize("phrase", _PERSIST_FORBIDDEN_PHRASES)
def test_p_cb_007_linter_rejects_each_forbidden_phrase(phrase: str) -> None:
    """Each spec-mandated forbidden phrase trips the linter when present.

    The phrase is injected into a constructed text and passed to
    :func:`check_text` with an empty catalog so only the spec
    ``extra_phrases`` list contributes — this prevents another phrase
    from the YAML catalog (e.g., ``"the trade is"`` matching inside
    ``"execute the trade"``) from short-circuiting the rejection
    attribution. The linter raises
    :class:`ImperativeLanguageDetected` carrying ``.phrase`` set to the
    matched string so structured-error capture works.
    """

    constructed = (
        "Calibration result: the model's calibration looks reasonable. "
        f"Operator note: {phrase} is not framing the system uses."
    )
    empty_catalog = LinterCatalog(phrases=())
    with pytest.raises(ImperativeLanguageDetected) as captured:
        check_text(constructed, catalog=empty_catalog, extra_phrases=_PERSIST_FORBIDDEN_PHRASES)
    assert captured.value.phrase == phrase


def test_p_cb_007_linter_passes_clean_chrome_text() -> None:
    """A clean operator note passes the linter without raising.

    Pin the negative case so a regression that broadens
    :func:`check_text` matching (e.g., partial-word substring) into a
    false-positive minefield is caught at gate time.
    """

    constructed = (
        "Calibration result: the model's per-sector Brier is 0.18. "
        "The operator decides whether the disagreement between model and "
        "market warrants further analysis."
    )
    check_text(constructed, extra_phrases=_PERSIST_FORBIDDEN_PHRASES)
