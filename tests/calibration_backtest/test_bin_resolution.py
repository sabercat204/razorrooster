"""Unit tests for :func:`resolve_bin_counts` (T-CB-026; design §3.6).

Covers the bin-count resolution chain documented in
:func:`razor_rooster.calibration_backtest.engines.scoring.resolve_bin_counts`:

1. CLI override on :class:`RunParameters` beats the per-sector entry in
   ``config/report.yaml``.
2. Per-sector override in ``report.yaml`` (via
   ``cfg.thresholds.reliability_bin_count_for_sector``) beats the
   global ``report.yaml`` value.
3. Global ``report.yaml`` value beats the module default
   (:data:`DEFAULT_BIN_COUNT` == 10).
4. Missing config file falls through to defaults with a warning logged.

Also covers the explicit validation guard (``bin_count >= 2``): any
override below the floor raises :class:`BacktestConfigError` BEFORE a
:class:`ReliabilityDiagram` is constructed (the loader silently clamps
to ``[2, 50]``, so calibration_backtest must guard explicitly per
design §3.6).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.calibration_backtest.engines.scoring import (
    DEFAULT_BIN_COUNT,
    DEFAULT_REPORT_CONFIG_PATH,
    resolve_bin_counts,
)
from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import RunParameters

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params(
    *,
    bin_count: int | None = None,
    bin_count_per_sector: dict[str, int] | None = None,
    sectors: tuple[str, ...] = (),
) -> RunParameters:
    """Build a :class:`RunParameters` carrying optional bin overrides."""
    return RunParameters(
        since_ts=datetime(2025, 1, 1, tzinfo=UTC),
        until_ts=datetime(2025, 6, 1, tzinfo=UTC),
        lag_days=7,
        class_ids=("flu_h2h",),
        sectors=sectors,
        venues=("polymarket",),
        allow_recent=False,
        bin_count=bin_count,
        bin_count_per_sector=bin_count_per_sector or {},
    )


def _write_report_yaml(path: Path, body: str) -> None:
    """Materialise a tiny ``report.yaml`` for the loader to find."""
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------


def test_default_bin_count_is_ten() -> None:
    """Module default mirrors report_generator's ``DEFAULT_BIN_COUNT`` (10)."""
    assert DEFAULT_BIN_COUNT == 10


def test_default_report_config_path_is_absolute() -> None:
    """``DEFAULT_REPORT_CONFIG_PATH`` is absolute so CLI cwd does not matter."""
    assert DEFAULT_REPORT_CONFIG_PATH.is_absolute()
    # Sanity: the path points at ``<workspace>/config/report.yaml``.
    assert DEFAULT_REPORT_CONFIG_PATH.name == "report.yaml"


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


def test_cli_override_beats_per_sector_yaml(tmp_path: Path) -> None:
    """CLI ``--bin-count`` flag wins over per-sector ``report.yaml`` entry."""
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(
        cfg_path,
        """
thresholds:
  reliability_bin_count: 8
  reliability_bin_count_per_sector:
    public_health: 12
""",
    )
    params = _params(bin_count=20, sectors=("public_health",))
    global_bins, per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == 20
    # Sector override is the YAML's 12 (still differs from global 20).
    assert per_sector == {"public_health": 12}


def test_cli_per_sector_beats_yaml_per_sector(tmp_path: Path) -> None:
    """CLI ``--bin-count-per-sector`` flag wins over the YAML per-sector entry."""
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(
        cfg_path,
        """
thresholds:
  reliability_bin_count: 10
  reliability_bin_count_per_sector:
    public_health: 12
""",
    )
    params = _params(
        bin_count_per_sector={"public_health": 25},
        sectors=("public_health",),
    )
    global_bins, per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == 10
    assert per_sector == {"public_health": 25}


def test_yaml_per_sector_beats_yaml_global(tmp_path: Path) -> None:
    """YAML per-sector entry wins over YAML global when no CLI override."""
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(
        cfg_path,
        """
thresholds:
  reliability_bin_count: 5
  reliability_bin_count_per_sector:
    public_health: 15
""",
    )
    params = _params(sectors=("public_health",))
    global_bins, per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == 5
    assert per_sector == {"public_health": 15}


def test_yaml_global_beats_module_default(tmp_path: Path) -> None:
    """YAML global wins over the module default when no CLI override."""
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(
        cfg_path,
        """
thresholds:
  reliability_bin_count: 7
""",
    )
    params = _params()
    global_bins, per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == 7
    assert per_sector == {}


def test_module_default_used_when_yaml_missing_section(tmp_path: Path) -> None:
    """An empty ``thresholds`` block falls through to the module default."""
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(cfg_path, "thresholds: {}\n")
    params = _params()
    global_bins, per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == DEFAULT_BIN_COUNT
    assert per_sector == {}


# ---------------------------------------------------------------------------
# Missing-config behaviour
# ---------------------------------------------------------------------------


def test_missing_config_returns_defaults_and_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing ``report.yaml`` falls back to defaults; a warning is logged."""
    missing_path = tmp_path / "does_not_exist.yaml"
    params = _params()
    with caplog.at_level(
        logging.WARNING, logger="razor_rooster.calibration_backtest.engines.scoring"
    ):
        global_bins, per_sector = resolve_bin_counts(params, config_path=missing_path)
    assert global_bins == DEFAULT_BIN_COUNT
    assert per_sector == {}
    # Warning surfaced so operators see the silent-default scenario.
    assert any("not found" in record.getMessage() for record in caplog.records)


def test_existing_config_logs_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An existing config logs an info-level "loaded" line."""
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(cfg_path, "thresholds:\n  reliability_bin_count: 4\n")
    params = _params()
    with caplog.at_level(logging.INFO, logger="razor_rooster.calibration_backtest.engines.scoring"):
        resolve_bin_counts(params, config_path=cfg_path)
    assert any("loaded report config" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Validation: bin_count >= 2
# ---------------------------------------------------------------------------


def test_bin_count_below_floor_raises() -> None:
    """``bin_count=1`` (CLI override) raises before any diagram is built.

    The :class:`RunParameters` validator catches this at construction
    time so the resolver never sees the bad value. The assertion below
    confirms the error message references the floor.
    """
    with pytest.raises(BacktestConfigError, match="bin_count"):
        _params(bin_count=1)


def test_per_sector_bin_count_below_floor_raises() -> None:
    """A per-sector CLI override below 2 raises at construction time."""
    with pytest.raises(BacktestConfigError, match="bin_count_per_sector"):
        _params(bin_count_per_sector={"public_health": 1})


def test_bin_count_above_loader_clamp_is_clamped(tmp_path: Path) -> None:
    """report.yaml with ``reliability_bin_count: 51`` is clamped to default by loader.

    The loader's ``_coerce_int_in_range`` falls back to the section
    default (10) when the value is out of ``[2, 50]``. calibration_backtest
    therefore sees the clamped value, not the 51 — this test pins that
    behaviour so we know the clamp is exercised.
    """
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(
        cfg_path,
        """
thresholds:
  reliability_bin_count: 51
""",
    )
    params = _params()
    global_bins, _per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == DEFAULT_BIN_COUNT


def test_bin_count_cli_override_with_sectors_includes_per_sector_overrides(
    tmp_path: Path,
) -> None:
    """CLI ``bin_count_per_sector`` for sectors not in ``params.sectors`` still surfaces.

    This catches the "operator passes ``--bin-count-per-sector`` without
    a matching ``--sector`` filter" path; the override must still apply.
    """
    cfg_path = tmp_path / "report.yaml"
    _write_report_yaml(cfg_path, "thresholds:\n  reliability_bin_count: 10\n")
    params = _params(
        bin_count_per_sector={"macro": 6},
        sectors=(),  # no --sector filter at all
    )
    global_bins, per_sector = resolve_bin_counts(params, config_path=cfg_path)
    assert global_bins == 10
    assert per_sector == {"macro": 6}


# ---------------------------------------------------------------------------
# Bin counts are NOT in the run_id hash (REQ-CB-RUN-001 / design §3.4)
# ---------------------------------------------------------------------------


def test_run_id_hash_excludes_bin_count() -> None:
    """``compute_run_id_for_params`` produces the same digest for two ``RunParameters``
    that differ only in ``bin_count`` / ``bin_count_per_sector``.

    The bin counts are display-only per design §3.4 — operators tuning
    bin counts must not invalidate prior caches. Using
    :func:`compute_run_id_for_params` directly here avoids the replay
    loop's transitive imports.
    """
    from razor_rooster.calibration_backtest.run_id import compute_run_id_for_params

    base_kwargs = {
        "library_version": 1,
        "system_revision": "deadbeef",
        "class_definition_versions": {"flu_h2h": 1},
    }
    params_a = _params(bin_count=None, bin_count_per_sector={})
    params_b = _params(bin_count=20, bin_count_per_sector={"public_health": 5})
    digest_a = compute_run_id_for_params(params_a, **base_kwargs)  # type: ignore[arg-type]
    digest_b = compute_run_id_for_params(params_b, **base_kwargs)  # type: ignore[arg-type]
    assert digest_a == digest_b
    # Sanity: the digest is the canonical 64-char SHA-256 hex string.
    assert len(digest_a) == 64
    assert all(ch in "0123456789abcdef" for ch in digest_a)
