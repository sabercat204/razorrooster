"""Contract tests for the shared diagram-hydration helpers (T-CB-037 prereq).

The helpers in
:mod:`razor_rooster.calibration_backtest.renderers._diagram_hydrate`
are consumed by both the Phase 5 HTML renderer and the Phase 6 GUI
detail view. These tests pin the hydration contract so a future change
to the persisted summary shape surfaces here, not in either consumer.

Coverage:

* Round-trip from a :class:`ScoreSummary` payload through
  :func:`reliability_diagrams_from_run` reproduces the typed
  :class:`ReliabilityDiagram` byte-for-byte.
* Degenerate empty-bin entries (``count == 0`` with ``None`` for
  ``mean_predicted_p`` / ``empirical_rate``) hydrate cleanly.
* Validation: ``bin_count < 2`` raises :class:`BacktestConfigError`
  via the :class:`ReliabilityDiagram` constructor; the helper catches
  the error and returns ``None`` rather than propagating.
* AST gate: the Phase 5 HTML renderer imports the hydration helpers
  from the shared module (so a future drift back to a private copy
  fails CI immediately).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    BacktestStatus,
    ReliabilityBin,
    ReliabilityDiagram,
)
from razor_rooster.calibration_backtest.renderers import _diagram_hydrate
from razor_rooster.calibration_backtest.renderers._diagram_hydrate import (
    hydrate_diagram,
    reliability_diagrams_from_run,
)

_HTML_RENDERER_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "razor_rooster"
    / "calibration_backtest"
    / "renderers"
    / "html.py"
)


def _make_summary(*, sector: str = "public_health") -> dict[str, Any]:
    """Build the persisted summary shape produced by ``ScoreSummary.as_mapping``."""

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


def _make_run(*, summary: dict[str, Any] | None = None) -> BacktestRun:
    """Build a deterministic :class:`BacktestRun` carrying ``summary``."""

    return BacktestRun(
        run_id="abc123def456ghi789",
        since_ts=datetime(2024, 1, 1, tzinfo=UTC),
        until_ts=datetime(2024, 6, 1, tzinfo=UTC),
        lag_days=7,
        class_ids=("flu_h2h",),
        sectors=("public_health",),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeefcafef00d1122",
        started_at=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
        completed_at=datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC),
        status=BacktestStatus.COMPLETE,
        error_summary=None,
        predictions_total=10,
        predictions_scored=8,
        predictions_skipped=2,
        overall_brier=0.21,
        summary_json=summary if summary is not None else _make_summary(),
        bin_count_global=10,
        bin_count_per_sector={"public_health": 5},
        fallback_polarity_count=1,
        allow_recent=False,
        disclaimer_version="v1",
    )


# ---------------------------------------------------------------------------
# reliability_diagrams_from_run — round-trip and edge cases
# ---------------------------------------------------------------------------


def test_reliability_diagrams_from_run_round_trips_summary() -> None:
    """A populated summary hydrates back to typed bin/diagram instances."""

    run = _make_run()
    diagrams = reliability_diagrams_from_run(run)
    assert set(diagrams) == {"public_health"}
    diagram = diagrams["public_health"]
    assert isinstance(diagram, ReliabilityDiagram)
    assert diagram.bin_count == 2
    assert len(diagram.bins) == 2
    populated, empty = diagram.bins
    assert populated == ReliabilityBin(
        lower_p=0.0,
        upper_p=0.5,
        count=3,
        mean_predicted_p=0.25,
        empirical_rate=0.33,
    )
    assert empty == ReliabilityBin(
        lower_p=0.5,
        upper_p=1.0,
        count=0,
        mean_predicted_p=None,
        empirical_rate=None,
    )


def test_reliability_diagrams_from_run_returns_empty_when_summary_missing() -> None:
    """A run with ``summary_json=None`` hydrates to an empty mapping."""

    run = _make_run(summary={})
    out = reliability_diagrams_from_run(run)
    assert out == {}


def test_reliability_diagrams_from_run_skips_non_dict_payload() -> None:
    """A non-dict ``reliability_diagrams`` value yields an empty mapping."""

    summary = _make_summary()
    summary["reliability_diagrams"] = "not a dict"
    run = _make_run(summary=summary)
    assert reliability_diagrams_from_run(run) == {}


def test_reliability_diagrams_from_run_drops_malformed_sectors() -> None:
    """A malformed per-sector entry is silently skipped, not propagated."""

    summary = _make_summary()
    # Inject a malformed sector alongside the well-formed one.
    summary["reliability_diagrams"]["broken"] = {"bin_count": 2, "bins": "not a list"}
    run = _make_run(summary=summary)
    out = reliability_diagrams_from_run(run)
    assert set(out) == {"public_health"}


# ---------------------------------------------------------------------------
# hydrate_diagram — empty bins, validation, malformed payloads
# ---------------------------------------------------------------------------


def test_hydrate_diagram_handles_all_empty_bins() -> None:
    """A diagram whose every bin has ``count=0`` hydrates cleanly."""

    payload = {
        "bin_count": 2,
        "bins": [
            {
                "count": 0,
                "empirical_rate": None,
                "lower_p": 0.0,
                "mean_predicted_p": None,
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
    diagram = hydrate_diagram(payload)
    assert diagram is not None
    assert diagram.bin_count == 2
    assert all(b.count == 0 for b in diagram.bins)


def test_hydrate_diagram_returns_none_when_bin_count_below_two() -> None:
    """``bin_count < 2`` would raise BacktestConfigError; helper returns None."""

    payload = {
        "bin_count": 1,
        "bins": [
            {
                "count": 0,
                "empirical_rate": None,
                "lower_p": 0.0,
                "mean_predicted_p": None,
                "upper_p": 1.0,
            },
        ],
    }
    # Sanity-check: the typed constructor truly raises on bin_count < 2.
    with pytest.raises(BacktestConfigError, match="bin_count"):
        ReliabilityDiagram(
            bin_count=1,
            bins=(
                ReliabilityBin(
                    lower_p=0.0,
                    upper_p=1.0,
                    count=0,
                    mean_predicted_p=None,
                    empirical_rate=None,
                ),
            ),
        )
    # The helper catches that and returns None instead of propagating.
    assert hydrate_diagram(payload) is None


def test_hydrate_diagram_returns_none_for_non_dict_payload() -> None:
    """Anything that isn't a dict yields ``None`` (defensive contract)."""

    assert hydrate_diagram("not a dict") is None
    assert hydrate_diagram(None) is None
    assert hydrate_diagram(42) is None


def test_hydrate_diagram_returns_none_when_bin_entry_malformed() -> None:
    """A non-dict bin entry aborts hydration for the whole diagram."""

    payload = {
        "bin_count": 2,
        "bins": [
            {
                "count": 0,
                "empirical_rate": None,
                "lower_p": 0.0,
                "mean_predicted_p": None,
                "upper_p": 0.5,
            },
            "not a dict",
        ],
    }
    assert hydrate_diagram(payload) is None


def test_hydrate_diagram_returns_none_when_required_key_missing() -> None:
    """Missing ``upper_p`` (or any required key) yields ``None``."""

    payload = {
        "bin_count": 2,
        "bins": [
            {
                "count": 0,
                "empirical_rate": None,
                "lower_p": 0.0,
                "mean_predicted_p": None,
                # upper_p missing
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
    assert hydrate_diagram(payload) is None


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_shared_module_exports_public_helpers() -> None:
    """The shared module exports both helpers under their public names."""

    assert "hydrate_diagram" in _diagram_hydrate.__all__
    assert "reliability_diagrams_from_run" in _diagram_hydrate.__all__


# ---------------------------------------------------------------------------
# AST gate — Phase 5 HTML renderer must consume the shared module
# ---------------------------------------------------------------------------


def _import_targets(source_path: Path) -> set[tuple[str, str]]:
    """Return ``(module, imported_name)`` pairs from a Python source file."""

    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                out.add((node.module, alias.name))
    return out


def test_html_renderer_imports_from_shared_diagram_hydrate_module() -> None:
    """``calibration_backtest/renderers/html.py`` consumes the shared helpers.

    The AST check pins the import path so a future revert to a local
    private copy of ``_reliability_diagrams_from_run`` is caught at
    test time, not by code review.
    """

    targets = _import_targets(_HTML_RENDERER_PATH)
    assert (
        "razor_rooster.calibration_backtest.renderers._diagram_hydrate",
        "reliability_diagrams_from_run",
    ) in targets
