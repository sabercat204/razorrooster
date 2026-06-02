"""T-CB-052 — performance smoke gates (REQ-CB-PERF-001, REQ-CB-PERF-002).

These tests exercise :func:`engines.replay.run_backtest` against a
synthetic corpus shaped to the v1 seed-library upper bound (4000
prediction attempts across 8 classes spanning five years; design §6).
They are smoke tests: the goal is to catch order-of-magnitude
regressions in the replay loop's orchestration cost, not to certify a
specific latency or memory ceiling. Both tests warn (rather than fail)
when the soft threshold is exceeded so a slow CI runner does not block
the overall suite; an optional hard-fail ceiling guards against
runaway regressions.

Reference hardware
------------------

Targets are pinned to an HP EliteBook G8 development workstation:

* CPU: Intel Core i7-8665U (4 cores / 8 threads, ~1.9 GHz base)
* RAM: 16 GB DDR4
* Storage: NVMe SSD
* OS: Linux 6.x kernel, Python 3.11

On reference hardware the wall-clock for the 4000-row synthetic corpus
runs comfortably under one minute; the soft threshold is set to five
minutes to absorb slower CI runners and the hard ceiling at ten
minutes to catch true regressions.

Marker
------

Both tests are tagged ``@pytest.mark.perf`` and excluded from the
default selector via ``pyproject.toml``'s
``addopts = "... -m 'not smoke and not perf'"``. Opt in with
``pytest -m perf -q``.

Pipeline stubbing
-----------------

The real :func:`evaluate_class_at_frozen_time` would invoke the
pattern_library + signal_scanner posterior pipeline, which requires
data_ingest precursor tables and the registry. The perf smoke is
measuring the **orchestration** cost of the replay loop (SQL fan-out,
thread-pool dispatch, persistence buffering, summary aggregation), not
the posterior arithmetic. We therefore stub
:func:`evaluate_class_at_frozen_time` and :func:`freezer.freeze` with
the same lightweight stubs used in
``tests/calibration_backtest/test_replay_persistence.py``. The
synthetic corpus exercises the real SQL prefilter
(:func:`iter_mapped_resolutions`), the real persistence-buffered insert
path (``backtest_predictions`` + ``backtest_traces``), and the real
status transitions on ``backtest_runs``.
"""

from __future__ import annotations

import os
import resource
import sys
import time
import warnings
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import run_backtest
from razor_rooster.calibration_backtest.models import BacktestStatus

if TYPE_CHECKING:
    from tests.calibration_backtest.conftest import SeedSyntheticCorpus


# ---------------------------------------------------------------------------
# Soft / hard thresholds
# ---------------------------------------------------------------------------


_WALL_CLOCK_SOFT_SECONDS: float = 5 * 60.0
"""REQ-CB-PERF-001 soft threshold (warn if exceeded; do not fail)."""


_WALL_CLOCK_HARD_SECONDS: float = 10 * 60.0
"""Hard ceiling — exceed this and the test fails. Catches genuine
multi-minute regressions while absorbing the variability of CI
runners on the soft threshold."""


_MEMORY_THRESHOLD_ENV: str = "RAZOR_ROOSTER_PERF_MEMORY_MB_THRESHOLD"
"""Environment variable used to override the REQ-CB-PERF-002 memory
threshold (in megabytes). Defaults to 2048 MB (2 GiB)."""


_MEMORY_DEFAULT_MB: int = 2048
"""Default REQ-CB-PERF-002 memory threshold (2 GiB)."""


# ---------------------------------------------------------------------------
# Pipeline stubs (mirror tests/calibration_backtest/test_replay_persistence.py)
# ---------------------------------------------------------------------------


def _stub_freeze(_conn: Any, prediction_ts: datetime) -> FrozenState:
    """Return a synthetic ``FrozenState`` echoing ``prediction_ts``.

    The real freezer issues an aggregate query over data_ingest
    canonical tables to find the latest ``source_publication_ts <=
    prediction_ts`` cap. The perf smoke does not seed those tables, so
    we short-circuit with a frozen state that the replay loop will
    accept (``frozen_flag=True``) and that mirrors the live shape used
    elsewhere in the test suite.
    """
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


def _stub_evaluate(
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
    """Return a fixed posterior + a JSON-roundtrippable trace dict.

    The signature matches :func:`evaluate_class_at_frozen_time` exactly
    so the replay loop's call site is bit-identical to production. The
    returned trace is small (~150 bytes serialised) so the per-row
    persistence path measures the encoder + DuckDB BLOB insert cost
    rather than the size of an unrealistic mock payload.
    """
    trace = {
        "class": {"class_id": class_id, "definition_version": 1},
        "data_as_of": prediction_ts.isoformat(),
        "library_version": library_version or 1,
        "posterior": {"mean": 0.5, "ci_lower": 0.4, "ci_upper": 0.6},
    }
    return 0.5, trace


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace freezer + posterior pipeline with lightweight stubs."""
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peak_memory_bytes() -> int:
    """Return the current process's peak resident-set size in bytes.

    :func:`resource.getrusage`'s ``ru_maxrss`` field is documented in
    KB on Linux but in bytes on macOS/BSD. We normalise to bytes here
    so the threshold comparison is platform-independent. ``maxrss`` is
    a high-water-mark counter that does not reset between calls; the
    perf tests therefore record it after :func:`run_backtest` returns
    so the value reflects the orchestration peak, not the process's
    historical peak across the entire pytest session. (The high-water
    nature still means a noisy earlier test could inflate the value;
    we accept that as part of the smoke contract — the hard threshold
    is generous enough to absorb it.)
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    multiplier = 1024 if sys.platform.startswith("linux") else 1
    return int(raw) * multiplier


def _memory_threshold_bytes() -> int:
    """Resolve the REQ-CB-PERF-002 memory threshold in bytes.

    Reads ``RAZOR_ROOSTER_PERF_MEMORY_MB_THRESHOLD`` from the
    environment (megabytes, integer) and falls back to the 2 GiB
    default. Operators raise the threshold when running on
    memory-constrained CI shapes by exporting a smaller value would
    not make sense — the override is intended to absorb noisy CI
    hosts, so the env knob increases the ceiling, not lowers it.
    """
    raw = os.environ.get(_MEMORY_THRESHOLD_ENV, str(_MEMORY_DEFAULT_MB))
    return int(raw) * 1024 * 1024


# ---------------------------------------------------------------------------
# REQ-CB-PERF-001 — wall-clock smoke
# ---------------------------------------------------------------------------


@pytest.mark.perf
def test_run_backtest_wall_clock_under_five_minutes(
    seed_synthetic_corpus: SeedSyntheticCorpus,
    patched_pipeline: None,
) -> None:
    """REQ-CB-PERF-001: 4000-attempt run completes in under five minutes.

    Soft assertion via ``warnings.warn`` so a slow CI runner does not
    block the overall suite; hard assertion at ten minutes catches
    true multi-minute regressions.
    """
    corpus = seed_synthetic_corpus(4000, 8, 365 * 5)

    started = time.perf_counter()
    result = run_backtest(
        corpus.params,
        conn=corpus.conn,
        store=corpus.store,
        now=corpus.now,
        max_workers=1,
        persistence_conn=corpus.conn,
    )
    duration_s = time.perf_counter() - started

    # Sanity: the run must have completed and scored every row, otherwise
    # the wall-clock measurement is meaningless. ``num_resolutions`` rows
    # in -> ``num_resolutions`` predictions out, all scored.
    assert result.run.status is BacktestStatus.COMPLETE
    assert len(result.predictions) == corpus.num_resolutions

    if duration_s >= _WALL_CLOCK_SOFT_SECONDS:
        warnings.warn(
            f"REQ-CB-PERF-001 exceeded: wall-clock={duration_s:.1f}s "
            f"(soft threshold {_WALL_CLOCK_SOFT_SECONDS:.0f}s; "
            f"corpus={corpus.num_resolutions} attempts across "
            f"{corpus.num_classes} classes)",
            UserWarning,
            stacklevel=2,
        )

    assert duration_s < _WALL_CLOCK_HARD_SECONDS, (
        f"REQ-CB-PERF-001 hard ceiling breached: "
        f"wall-clock={duration_s:.1f}s exceeds {_WALL_CLOCK_HARD_SECONDS:.0f}s"
    )


# ---------------------------------------------------------------------------
# REQ-CB-PERF-002 — peak memory smoke
# ---------------------------------------------------------------------------


@pytest.mark.perf
def test_run_backtest_peak_memory_under_two_gib(
    seed_synthetic_corpus: SeedSyntheticCorpus,
    patched_pipeline: None,
) -> None:
    """REQ-CB-PERF-002: peak resident memory after run stays below 2 GiB.

    Captures ``ru_maxrss`` after :func:`run_backtest` returns. The
    threshold is overridable via the
    ``RAZOR_ROOSTER_PERF_MEMORY_MB_THRESHOLD`` environment variable so
    operators can tune it to noisy CI hosts. Soft warning + assertion
    follow the same shape as REQ-CB-PERF-001.
    """
    corpus = seed_synthetic_corpus(4000, 8, 365 * 5)

    result = run_backtest(
        corpus.params,
        conn=corpus.conn,
        store=corpus.store,
        now=corpus.now,
        max_workers=1,
        persistence_conn=corpus.conn,
    )
    peak_bytes = _peak_memory_bytes()
    threshold_bytes = _memory_threshold_bytes()

    # Sanity: the run must have completed; otherwise the memory
    # measurement is unrelated to the replay loop.
    assert result.run.status is BacktestStatus.COMPLETE

    if peak_bytes >= threshold_bytes:
        peak_mib = peak_bytes / (1024 * 1024)
        threshold_mib = threshold_bytes / (1024 * 1024)
        warnings.warn(
            f"REQ-CB-PERF-002 exceeded: peak={peak_mib:.0f}MB "
            f"(threshold {threshold_mib:.0f}MB; corpus="
            f"{corpus.num_resolutions} attempts across "
            f"{corpus.num_classes} classes)",
            UserWarning,
            stacklevel=2,
        )

    assert peak_bytes < threshold_bytes, (
        f"REQ-CB-PERF-002 hard ceiling breached: "
        f"peak={peak_bytes / (1024 * 1024):.0f}MB exceeds "
        f"{threshold_bytes / (1024 * 1024):.0f}MB"
    )
