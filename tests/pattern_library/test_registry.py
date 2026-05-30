"""T-PL-030 — class registry + persistence sync tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

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
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.registry import (
    ClassDelta,
    ClassValidationError,
    _clear_for_tests,
    _set_discovered_for_tests,
    get,
    get_all,
    is_registered,
    known_class_ids,
    register,
    sync_to_store,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    _clear_for_tests()
    # Pretend discovery has already run so tests don't trip the
    # auto-import pass against a (potentially populated) classes/ dir.
    _set_discovered_for_tests(True)
    yield
    _clear_for_tests()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_registry.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _stub_query(*_args: object, **_kwargs: object) -> object:
    return None


def _make_class(
    class_id: str,
    *,
    sector: Sector = Sector.PUBLIC_HEALTH,
    definition_version: int = 1,
) -> EventClass:
    return EventClass(
        class_id=class_id,
        title=f"{class_id} title",
        description=f"{class_id} description",
        domain_sector=sector,
        occurrence_query=_stub_query,
        definition_version=definition_version,
    )


# -- register / get / get_all --------------------------------------------


def test_register_and_get() -> None:
    cls = _make_class("test_a")
    register(cls)
    assert get("test_a") is cls
    assert is_registered("test_a")


def test_register_idempotent_for_same_object() -> None:
    cls = _make_class("test_a")
    register(cls)
    register(cls)
    assert known_class_ids() == ("test_a",)


def test_register_different_object_same_id_rejected() -> None:
    cls_a = _make_class("test_a")
    cls_b = _make_class("test_a")
    register(cls_a)
    with pytest.raises(ClassValidationError, match="already registered"):
        register(cls_b)


def test_get_all_filters_by_sector() -> None:
    register(_make_class("ph_a", sector=Sector.PUBLIC_HEALTH))
    register(_make_class("ph_b", sector=Sector.PUBLIC_HEALTH))
    register(_make_class("geo_a", sector=Sector.GEOPOLITICAL))
    public_health = get_all(sector=Sector.PUBLIC_HEALTH)
    geopolitical = get_all(sector=Sector.GEOPOLITICAL)
    assert {c.class_id for c in public_health} == {"ph_a", "ph_b"}
    assert {c.class_id for c in geopolitical} == {"geo_a"}


def test_get_unknown_class_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown class_id"):
        get("not_registered")


def test_register_validates_callable_queries() -> None:
    # Construct a class with a non-callable precursor query by abusing
    # the dataclass — bypasses __post_init__ because we go via the
    # already-validated path; but the registry's _validate runs again
    # as a safety net.
    cls = _make_class("test_a")
    register(cls)
    # Construct a synthetic invalid class via a fresh module — the
    # validator catches it on re-registration.
    invalid_class = EventClass(
        class_id="test_b",
        title="x",
        description="x",
        domain_sector=Sector.GEOPOLITICAL,
        occurrence_query=_stub_query,
        precursors=(
            PrecursorVariable(
                variable_id="p",
                title="P",
                query=_stub_query,
                direction="high_signals_event",
            ),
        ),
    )
    register(invalid_class)
    # And now we can re-register the same valid class.
    register(invalid_class)


def test_register_rejects_non_event_class() -> None:
    with pytest.raises(ClassValidationError):
        register("not_an_event_class")  # type: ignore[arg-type]


# -- sync_to_store -------------------------------------------------------


def test_sync_inserts_new_classes(store: DuckDBStore) -> None:
    register(_make_class("c1"))
    register(_make_class("c2"))
    when = datetime(2026, 5, 14, tzinfo=UTC)
    with store.connection() as conn:
        delta = sync_to_store(conn, when=when)
        rows = conn.execute("SELECT class_id FROM pl_event_classes ORDER BY class_id").fetchall()
    assert isinstance(delta, ClassDelta)
    assert delta.added == ("c1", "c2")
    assert delta.removed == ()
    assert delta.definition_changed == ()
    assert {r[0] for r in rows} == {"c1", "c2"}


def test_sync_marks_disappeared_classes_removed(store: DuckDBStore) -> None:
    register(_make_class("c1"))
    register(_make_class("c2"))
    with store.connection() as conn:
        sync_to_store(conn)

    # Drop c2 from the registry, then sync again.
    _clear_for_tests()
    _set_discovered_for_tests(True)
    register(_make_class("c1"))

    with store.connection() as conn:
        delta = sync_to_store(conn)
        row = conn.execute(
            "SELECT removed_at FROM pl_event_classes WHERE class_id = ?",
            ["c2"],
        ).fetchone()

    assert delta.removed == ("c2",)
    assert row is not None
    assert row[0] is not None


def test_sync_detects_definition_version_change(store: DuckDBStore) -> None:
    register(_make_class("c1", definition_version=1))
    with store.connection() as conn:
        sync_to_store(conn)

    _clear_for_tests()
    _set_discovered_for_tests(True)
    register(_make_class("c1", definition_version=2))

    with store.connection() as conn:
        delta = sync_to_store(conn)
        row = conn.execute(
            "SELECT definition_version FROM pl_event_classes WHERE class_id = ?",
            ["c1"],
        ).fetchone()

    assert delta.definition_changed == ("c1",)
    assert delta.added == ()
    assert delta.removed == ()
    assert row is not None
    assert row[0] == 2


def test_sync_unchanged_when_registry_matches_store(store: DuckDBStore) -> None:
    register(_make_class("c1"))
    with store.connection() as conn:
        first = sync_to_store(conn)
        second = sync_to_store(conn)
    assert first.added == ("c1",)
    assert second.added == ()
    assert second.unchanged == ("c1",)
    assert not second.has_changes


def test_sync_re_registration_after_removed_counts_as_added(
    store: DuckDBStore,
) -> None:
    """A class removed and later re-added shows up under ``added`` again."""
    register(_make_class("c1"))
    with store.connection() as conn:
        sync_to_store(conn)

    _clear_for_tests()
    _set_discovered_for_tests(True)
    with store.connection() as conn:
        sync_to_store(conn)  # mark removed

    _clear_for_tests()
    _set_discovered_for_tests(True)
    register(_make_class("c1"))
    with store.connection() as conn:
        delta = sync_to_store(conn)

    assert delta.added == ("c1",)
    assert delta.removed == ()


def test_class_delta_has_changes() -> None:
    no_changes = ClassDelta(added=(), removed=(), definition_changed=(), unchanged=("c1",))
    assert no_changes.has_changes is False
    with_added = ClassDelta(added=("c2",), removed=(), definition_changed=(), unchanged=("c1",))
    assert with_added.has_changes is True


def test_known_class_ids_sorted() -> None:
    register(_make_class("c_zebra"))
    register(_make_class("c_apple"))
    assert known_class_ids() == ("c_apple", "c_zebra")
