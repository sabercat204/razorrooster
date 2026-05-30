"""Tests for auto-prune in report generation (T-RG-COMPAT-AUTOPRUNE-001 v0.43.0).

Covers:
- AutoPruneConfig defaults and config loader parsing.
- Bad-value fallback in the loader.
- Generator integration: a successful cycle prunes when enabled,
  doesn't prune when disabled, and survives prune failures.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.report_generator.config.loader import (
    AutoPruneConfig,
    ReportConfig,
    load_config,
)
from razor_rooster.report_generator.engines.generator import generate
from razor_rooster.report_generator.engines.measurements import compute_distribution
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    list_threshold_measurements,
    persist_threshold_measurement,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "auto_prune.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    with store.connection() as c:
        yield c


def _write_config(tmp_path: Path, payload: dict[str, object]) -> Path:
    file_path = tmp_path / "report.yaml"
    file_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return file_path


# -- defaults --------------------------------------------------------------


def test_auto_prune_default_is_disabled() -> None:
    cfg = ReportConfig()
    assert cfg.auto_prune.enabled is False
    assert cfg.auto_prune.older_than_days == 365
    assert cfg.auto_prune.keep_last is None


def test_auto_prune_dataclass_is_frozen() -> None:
    config = AutoPruneConfig()
    with pytest.raises(AttributeError):
        config.enabled = True  # type: ignore[misc]


# -- config loader ---------------------------------------------------------


def test_load_auto_prune_block_with_explicit_values(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "auto_prune": {
                "enabled": True,
                "older_than_days": 180,
                "keep_last": 100,
            }
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.auto_prune.enabled is True
    assert cfg.auto_prune.older_than_days == 180
    assert cfg.auto_prune.keep_last == 100


def test_load_auto_prune_only_keep_last(tmp_path: Path) -> None:
    """older_than_days can be explicitly null; keep_last alone works."""
    cfg_path = _write_config(
        tmp_path,
        {
            "auto_prune": {
                "enabled": True,
                "older_than_days": None,
                "keep_last": 50,
            }
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.auto_prune.older_than_days is None
    assert cfg.auto_prune.keep_last == 50


def test_load_auto_prune_block_missing_returns_defaults(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, {"thresholds": {}})
    cfg = load_config(cfg_path)
    assert cfg.auto_prune == AutoPruneConfig()


def test_load_auto_prune_invalid_block_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(tmp_path, {"auto_prune": "not a dict"})
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    assert cfg.auto_prune == AutoPruneConfig()
    assert any("auto_prune" in record.message for record in caplog.records)


def test_load_auto_prune_out_of_range_values_fall_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "auto_prune": {
                "enabled": True,
                "older_than_days": 0,  # below [1, 36500]
                "keep_last": 99_999_999,  # above [0, 1_000_000]
            }
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    # Both fall back to their defaults.
    assert cfg.auto_prune.older_than_days == 365
    assert cfg.auto_prune.keep_last == 365


# -- generator integration --------------------------------------------------


def test_generator_does_not_prune_when_disabled(conn: duckdb.DuckDBPyConnection) -> None:
    """Default (auto_prune.enabled = False) leaves all rows intact."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    # Pre-seed 5 old measurements that auto-prune would delete if enabled.
    for i in range(5):
        persist_threshold_measurement(
            conn,
            report_id=f"pre-{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base - timedelta(days=400 + i),
            distribution=compute_distribution([100.0], threshold=500.0),
        )
    # Run a generate cycle; default config has auto_prune disabled.
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    store = DuckDBStore(db_path)
    cfg = ReportConfig(enabled_sections=("cross_venue",))
    generate(
        store,
        since=base - timedelta(days=1),
        config=cfg,
        quiet=True,
        now=base,
    )
    rows = list_threshold_measurements(conn, measurement_kind="cross_venue_spread_bps")
    # 5 pre-seeded + 1 from this cycle.
    assert len(rows) == 6


def test_generator_prunes_old_measurements_when_enabled(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """auto_prune.older_than_days deletes ancient rows after the cycle."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    # 3 old (>365d), 2 recent.
    for i in range(3):
        persist_threshold_measurement(
            conn,
            report_id=f"old-{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base - timedelta(days=400 + i),
            distribution=compute_distribution([100.0], threshold=500.0),
        )
    for i in range(2):
        persist_threshold_measurement(
            conn,
            report_id=f"new-{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base - timedelta(days=10 + i),
            distribution=compute_distribution([100.0], threshold=500.0),
        )
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    store = DuckDBStore(db_path)
    cfg = ReportConfig(
        enabled_sections=("cross_venue",),
        auto_prune=AutoPruneConfig(
            enabled=True,
            older_than_days=365,
            keep_last=None,
        ),
    )
    generate(
        store,
        since=base - timedelta(days=1),
        config=cfg,
        quiet=True,
        now=base,
    )
    rows = list_threshold_measurements(conn, measurement_kind="cross_venue_spread_bps")
    # 2 recent survivors + 1 from this cycle = 3 (3 old were pruned).
    assert len(rows) == 3
    surviving_ids = {r.report_id for r in rows}
    assert "old-0" not in surviving_ids
    assert "old-1" not in surviving_ids
    assert "old-2" not in surviving_ids


def test_generator_keeps_last_n_when_enabled(conn: duckdb.DuckDBPyConnection) -> None:
    """auto_prune.keep_last enforces a per-kind row cap."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    # 6 measurements; we want to keep only the newest 3.
    for i in range(6):
        persist_threshold_measurement(
            conn,
            report_id=f"r-{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base - timedelta(days=10 + i),
            distribution=compute_distribution([100.0], threshold=500.0),
        )
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    store = DuckDBStore(db_path)
    cfg = ReportConfig(
        enabled_sections=("cross_venue",),
        auto_prune=AutoPruneConfig(
            enabled=True,
            older_than_days=None,
            keep_last=3,
        ),
    )
    generate(
        store,
        since=base - timedelta(days=1),
        config=cfg,
        quiet=True,
        now=base,
    )
    rows = list_threshold_measurements(conn, measurement_kind="cross_venue_spread_bps")
    # keep_last=3 caps to 3 per kind. The cycle that just ran adds 1 row; that row is
    # newer than the seeded ones, so it survives. After the prune: 3 rows total.
    assert len(rows) == 3


def test_generator_auto_prune_no_op_when_no_strategy(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Enabled but no strategy set is a silent no-op."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    persist_threshold_measurement(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base - timedelta(days=400),
        distribution=compute_distribution([100.0], threshold=500.0),
    )
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    store = DuckDBStore(db_path)
    cfg = ReportConfig(
        enabled_sections=("cross_venue",),
        auto_prune=AutoPruneConfig(
            enabled=True,
            older_than_days=None,
            keep_last=None,
        ),
    )
    generate(
        store,
        since=base - timedelta(days=1),
        config=cfg,
        quiet=True,
        now=base,
    )
    rows = list_threshold_measurements(conn, measurement_kind="cross_venue_spread_bps")
    # Both pre-seeded and the new cycle's row survive — no strategy means no prune.
    assert len(rows) == 2


def test_generator_survives_auto_prune_failure(
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prune failure is logged and swallowed; the report still ships."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)

    def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("prune blew up")

    monkeypatch.setattr(
        "razor_rooster.report_generator.engines.generator.prune_threshold_measurements",
        boom,
    )
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    store = DuckDBStore(db_path)
    cfg = ReportConfig(
        enabled_sections=("cross_venue",),
        auto_prune=AutoPruneConfig(
            enabled=True,
            older_than_days=365,
        ),
    )
    with caplog.at_level(logging.ERROR, logger="razor_rooster.report_generator.engines.generator"):
        result = generate(
            store,
            since=base - timedelta(days=1),
            config=cfg,
            quiet=True,
            now=base,
        )
    # Report still produced.
    assert result.report_id is not None
    # The prune failure was logged.
    messages = " ".join(record.message for record in caplog.records)
    assert "auto-prune" in messages
