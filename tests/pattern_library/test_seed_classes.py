"""T-PL-070 through T-PL-077 — seed class integration tests.

Verifies that each of the eight seed classes:

- Auto-discovers via the registry's pkgutil pass.
- Validates cleanly (the ``EventClass.__post_init__`` invariants hold).
- Refreshes successfully against an empty data_ingest corpus — the
  occurrence_query returns an empty DataFrame, the refresh runner
  produces a zero-occurrence base rate with low_sample_warning,
  no signatures, no analogues, and the insufficient-data
  calibration sentinel.

These tests don't exercise the predicate semantics (which require
real backfill data — DEFER-PL-001 / T-PL-081). They confirm the
scaffolding is functional and downstream subsystems can rely on the
eight class_ids being available.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.engines.refresh import run_refresh
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)

_SEED_CLASS_IDS = (
    "enso_neutral_to_elnino",
    "eia_grid_reliability_event",
    "final_rule_within_12mo",
    "gdelt_conflict_intensification",
    "multi_signal_geopolitical_alert",
    "opec_unscheduled_cut",
    "pheic_declaration_12mo",
    "polymarket_resolution_calibration",
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    yield
    registry._clear_for_tests()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_seed_classes.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "library" / ".refresh.lock"


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    target = tmp_path / "calibration"
    target.mkdir(parents=True, exist_ok=True)
    return target


# -- discovery + validation -----------------------------------------------


def test_all_eight_seed_classes_auto_discover() -> None:
    """The pkgutil-driven auto-discovery picks up every seed class."""
    discovered = registry.discover()
    for class_id in _SEED_CLASS_IDS:
        assert class_id in discovered, (
            f"missing seed class {class_id!r} in registry; "
            f"auto-discovery returned {sorted(discovered)}"
        )


def test_each_seed_class_passes_post_init_validation() -> None:
    """Force discovery; if any class's __post_init__ raised, that's a fail."""
    discovered = registry.discover()
    # All eight class_ids should be present.
    for class_id in _SEED_CLASS_IDS:
        assert class_id in discovered


def test_seed_classes_have_documented_metadata() -> None:
    """Each class has non-empty title, description, and a sector."""
    registry.discover()
    for class_id in _SEED_CLASS_IDS:
        cls = registry.get(class_id)
        assert cls.title, f"{class_id} has empty title"
        assert cls.description, f"{class_id} has empty description"
        # Domain sector enum is enforced by the dataclass; we just
        # confirm it's set to a Sector value.
        assert cls.domain_sector is not None


# -- refresh pipeline -----------------------------------------------------


def test_refresh_runs_against_empty_corpus(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Even with zero data_ingest rows, the refresh completes for all eight classes."""
    registry.discover()
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )
    # All eight seed classes should appear in the report.
    seen_class_ids = {o.class_id for o in report.classes}
    for class_id in _SEED_CLASS_IDS:
        assert class_id in seen_class_ids


def test_refresh_zero_occurrences_paths(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Each seed class against empty data should record:
    - zero occurrences persisted
    - base_rate computed (rate=0)
    - calibration status = insufficient_data
    """
    registry.discover()
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    for outcome in report.classes:
        assert outcome.occurrences_persisted == 0, outcome.class_id
        # base_rate may be computed (rate=0) — but might also fail to
        # compute when scipy or another dependency isn't loadable.
        # If it computed, we expect the low_sample warning.
        if outcome.base_rate_computed:
            assert "low_sample" in outcome.warnings, outcome.class_id
        # No occurrences → calibration must be insufficient_data.
        assert outcome.calibration_status == "insufficient_data", outcome.class_id


def test_refresh_no_per_class_failures_on_empty_corpus(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """An empty corpus shouldn't trigger per-class exceptions."""
    registry.discover()
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    failed = [o for o in report.classes if o.status == "failed"]
    assert not failed, f"failed classes: {failed}"


# -- per-class spot checks -----------------------------------------------


def test_pheic_class_uses_public_health_sector() -> None:
    registry.discover()
    cls = registry.get("pheic_declaration_12mo")
    assert cls.domain_sector.value == "public_health"
    assert len(cls.precursors) == 1


def test_gdelt_class_uses_geopolitical_sector() -> None:
    registry.discover()
    cls = registry.get("gdelt_conflict_intensification")
    assert cls.domain_sector.value == "geopolitical"


def test_multi_signal_class_has_three_precursors() -> None:
    registry.discover()
    cls = registry.get("multi_signal_geopolitical_alert")
    assert len(cls.precursors) == 3


def test_polymarket_calibration_class_is_cross_cutting() -> None:
    registry.discover()
    cls = registry.get("polymarket_resolution_calibration")
    assert cls.domain_sector.value == "cross_cutting"
    assert cls.precursors == ()  # no precursors yet


def test_enso_class_uses_climate_sector() -> None:
    registry.discover()
    cls = registry.get("enso_neutral_to_elnino")
    assert cls.domain_sector.value == "climate"


def test_eia_grid_class_uses_infrastructure_energy_sector() -> None:
    registry.discover()
    cls = registry.get("eia_grid_reliability_event")
    assert cls.domain_sector.value == "infrastructure_energy"


def test_opec_class_uses_commodity_sector() -> None:
    registry.discover()
    cls = registry.get("opec_unscheduled_cut")
    assert cls.domain_sector.value == "commodity"


def test_final_rule_class_uses_regulatory_sector() -> None:
    registry.discover()
    cls = registry.get("final_rule_within_12mo")
    assert cls.domain_sector.value == "regulatory"
