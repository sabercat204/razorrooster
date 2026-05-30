"""T-PL-031 — pattern-library CLI tests (validate / list / show / sync)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.cli import pattern_library
from razor_rooster.pattern_library.models.event_class import (
    AnalogueFeature,
    EventClass,
    PrecursorVariable,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.registry import (
    _clear_for_tests,
    _set_discovered_for_tests,
    register,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    _clear_for_tests()
    _set_discovered_for_tests(True)
    yield
    _clear_for_tests()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "pl_cli.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    s.close()
    return db_path


def _stub_query(*_args: object, **_kwargs: object) -> object:
    return None


def _make_class(
    class_id: str = "test_a",
    *,
    sector: Sector = Sector.PUBLIC_HEALTH,
    with_precursor: bool = False,
    with_feature: bool = False,
) -> EventClass:
    precursors: tuple[PrecursorVariable, ...] = ()
    if with_precursor:
        precursors = (
            PrecursorVariable(
                variable_id="p1",
                title="Precursor 1",
                query=_stub_query,
                direction="high_signals_event",
            ),
        )
    features: tuple[AnalogueFeature, ...] = ()
    if with_feature:
        features = (AnalogueFeature(feature_id="f1", query=_stub_query, weight=2.0),)
    return EventClass(
        class_id=class_id,
        title=f"{class_id} title",
        description=f"{class_id} description",
        domain_sector=sector,
        occurrence_query=_stub_query,
        precursors=precursors,
        analogue_features=features,
    )


# -- group + version -----------------------------------------------------


def test_group_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["--help"])
    assert result.exit_code == 0
    for cmd in ("version", "list", "show", "validate", "sync-classes"):
        assert cmd in result.output


def test_version_prints_current() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "1"


# -- list ----------------------------------------------------------------


def test_list_empty() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["list"])
    assert result.exit_code == 0
    assert "no event classes registered" in result.output


def test_list_shows_registered() -> None:
    register(_make_class("c1"))
    register(_make_class("c2", sector=Sector.GEOPOLITICAL))
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["list"])
    assert result.exit_code == 0
    assert "c1" in result.output
    assert "c2" in result.output


def test_list_filters_by_sector() -> None:
    register(_make_class("c1", sector=Sector.PUBLIC_HEALTH))
    register(_make_class("c2", sector=Sector.GEOPOLITICAL))
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["list", "--sector", "geopolitical"])
    assert result.exit_code == 0
    assert "c2" in result.output
    assert "c1" not in result.output


def test_list_rejects_unknown_sector() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["list", "--sector", "not_a_sector"])
    assert result.exit_code == 2  # click reports invalid choice
    assert "Invalid value" in result.output or "invalid choice" in result.output.lower()


# -- show ----------------------------------------------------------------


def test_show_unknown_class_returns_error() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["show", "not_a_class"])
    assert result.exit_code == 1
    output = result.output + (result.stderr if result.stderr_bytes else "")
    assert "unknown class_id" in output


def test_show_renders_class_metadata() -> None:
    register(_make_class("c1", with_precursor=True, with_feature=True))
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["show", "c1"])
    assert result.exit_code == 0
    assert "c1" in result.output
    assert "public_health" in result.output
    assert "p1" in result.output
    assert "f1" in result.output
    assert "weight=2.0" in result.output


# -- validate ------------------------------------------------------------


def test_validate_reports_ok_for_valid_class() -> None:
    register(_make_class("c1"))
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["validate", "c1"])
    assert result.exit_code == 0
    assert "validates cleanly" in result.output


def test_validate_unknown_class_returns_error() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["validate", "missing"])
    assert result.exit_code == 1


# -- sync-classes --------------------------------------------------------


def test_sync_classes_reports_added(store_path: Path) -> None:
    register(_make_class("c1"))
    register(_make_class("c2"))
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["sync-classes", "--db", str(store_path)])
    assert result.exit_code == 0
    assert "added:               2" in result.output
    assert "+ c1" in result.output
    assert "+ c2" in result.output


def test_sync_classes_reports_removed(store_path: Path) -> None:
    register(_make_class("c1"))
    register(_make_class("c2"))
    runner = CliRunner()
    runner.invoke(pattern_library, ["sync-classes", "--db", str(store_path)])

    _clear_for_tests()
    _set_discovered_for_tests(True)
    register(_make_class("c1"))

    result = runner.invoke(pattern_library, ["sync-classes", "--db", str(store_path)])
    assert result.exit_code == 0
    assert "removed:             1" in result.output
    assert "- c2" in result.output


def test_sync_classes_with_missing_db_returns_error(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["sync-classes", "--db", str(tmp_path / "nope.duckdb")],
    )
    assert result.exit_code == 1
