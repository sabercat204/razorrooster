"""T-032 verification — source registry.

Verifies:
- ``register`` accepts a connector class and returns it unchanged.
- ``register`` is idempotent on the same class (re-import safe).
- Two different classes with the same source_id raise DuplicateSourceId.
- ``get`` returns the registered class.
- ``get`` raises UnknownSourceId for unregistered ids.
- ``get_all`` returns classes in source_id order.
- ``known_source_ids`` is alphabetically sorted.
- ``is_registered`` reflects state correctly.
- Decorator form works on a class definition.
- Test isolation helpers (_unregister_for_tests, _clear_for_tests) work.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.connectors.base import Connector, License
from razor_rooster.data_ingest.normalization.base import NormalizedRecord, RawRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import (
    DuplicateSourceId,
    RegistryError,
    UnknownSourceId,
    _clear_for_tests,
    _unregister_for_tests,
    get,
    get_all,
    is_registered,
    known_source_ids,
    register,
)


@pytest.fixture(autouse=True)
def isolate_registry() -> Iterator[None]:
    """Snapshot the registry, clear it, run the test, restore.

    This ensures tests in this file don't leave entries that pollute other
    test files, and that the registrations made by connector modules at
    import time (FRED, World Bank) are not lost when these tests clear the
    registry.
    """
    from razor_rooster.data_ingest.registry import _REGISTRY

    snapshot = dict(_REGISTRY)
    _clear_for_tests()
    try:
        yield
    finally:
        _clear_for_tests()
        _REGISTRY.update(snapshot)


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    return DuckDBStore(tmp_path / "test.duckdb")


def _make_connector_class(source_id: str) -> type[Connector]:
    """Produce a concrete Connector subclass with the given source_id."""

    class _C(Connector):
        title = f"{source_id} title"
        canonical_schema = SchemaType.TIME_SERIES
        license = License.PUBLIC_DOMAIN
        cadence_default = "daily"
        backfill_supported = False
        connector_version = f"{source_id}@0.1.0"

        def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
            return iter(())

        def normalize(self, raw: RawRecord) -> NormalizedRecord:
            raise NotImplementedError

    _C.source_id = source_id
    _C.__name__ = f"_C_{source_id}"
    return _C


def test_register_returns_class_unchanged() -> None:
    cls = _make_connector_class("test_src")
    returned = register(cls)
    assert returned is cls


def test_register_makes_class_retrievable() -> None:
    cls = _make_connector_class("test_src")
    register(cls)
    assert get("test_src") is cls


def test_register_is_idempotent_on_same_class() -> None:
    cls = _make_connector_class("test_src")
    register(cls)
    register(cls)
    assert is_registered("test_src")
    assert len(get_all()) == 1


def test_register_rejects_duplicate_source_id_with_different_class() -> None:
    cls_a = _make_connector_class("test_src")
    cls_b = _make_connector_class("test_src")
    register(cls_a)
    with pytest.raises(DuplicateSourceId, match="test_src"):
        register(cls_b)


def test_register_rejects_class_without_source_id() -> None:
    class _NoId:
        source_id = ""

    with pytest.raises(RegistryError, match="no source_id"):
        register(_NoId)  # type: ignore[arg-type]


def test_get_raises_for_unknown_source_id() -> None:
    with pytest.raises(UnknownSourceId, match="not_registered"):
        get("not_registered")


def test_get_all_returns_classes_in_source_id_order() -> None:
    cls_b = _make_connector_class("b_src")
    cls_a = _make_connector_class("a_src")
    cls_c = _make_connector_class("c_src")
    register(cls_b)
    register(cls_a)
    register(cls_c)
    assert get_all() == (cls_a, cls_b, cls_c)


def test_known_source_ids_returns_alphabetical() -> None:
    register(_make_connector_class("zebra"))
    register(_make_connector_class("apple"))
    register(_make_connector_class("mango"))
    assert known_source_ids() == ("apple", "mango", "zebra")


def test_is_registered_reflects_state() -> None:
    assert is_registered("xyz") is False
    register(_make_connector_class("xyz"))
    assert is_registered("xyz") is True


def test_decorator_form_works() -> None:
    @register
    class _DecoratorConnector(Connector):
        source_id = "decorator_test"
        title = "Decorator Test"
        canonical_schema = SchemaType.TIME_SERIES
        license = License.PUBLIC_DOMAIN
        cadence_default = "daily"
        backfill_supported = False
        connector_version = "decorator_test@0.1.0"

        def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
            return iter(())

        def normalize(self, raw: RawRecord) -> NormalizedRecord:
            raise NotImplementedError

    assert is_registered("decorator_test")
    assert get("decorator_test") is _DecoratorConnector


def test_unregister_for_tests_removes_entry() -> None:
    register(_make_connector_class("removable"))
    assert is_registered("removable")
    _unregister_for_tests("removable")
    assert is_registered("removable") is False


def test_clear_for_tests_empties_registry() -> None:
    register(_make_connector_class("a"))
    register(_make_connector_class("b"))
    register(_make_connector_class("c"))
    assert len(get_all()) == 3
    _clear_for_tests()
    assert get_all() == ()


def test_registered_class_can_be_instantiated(store: DuckDBStore) -> None:
    """The registry stores classes; instantiation happens elsewhere."""
    cls = _make_connector_class("instantiable")
    register(cls)
    instance = get("instantiable")(store)
    assert instance.source_id == "instantiable"
    assert instance.store is store
