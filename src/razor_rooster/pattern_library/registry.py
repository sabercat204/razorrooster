"""Event-class registry (T-PL-030; design §3.3).

Each event class is a Python module under ``classes/`` exposing a
module-level ``CLASS = EventClass(...)``. The registry auto-discovers
those modules on first access via ``pkgutil.iter_modules`` and runs
validation on each registration.

Persistence sync writes the registered classes' metadata to
``pl_event_classes``; classes that were registered previously but are
no longer present get their ``removed_at`` column stamped (REQ-PL-VER-002
preserves history rather than deleting rows).

The module also exposes a small set of helpers used by the validate /
list / show CLI subcommands (T-PL-031).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import duckdb

from razor_rooster.pattern_library.models.event_class import EventClass, Sector
from razor_rooster.pattern_library.persistence.operations import (
    mark_event_class_removed,
    upsert_event_class,
)

logger = logging.getLogger(__name__)


_CLASSES_PACKAGE: Final[str] = "razor_rooster.pattern_library.classes"
_CLASS_ATTRIBUTE: Final[str] = "CLASS"


class ClassValidationError(RuntimeError):
    """Raised when a class definition fails registration-time validation."""


# ---------------------------------------------------------------------------
# Module-level registry. The state lives at module scope so importing the
# module twice (which Python caches) doesn't reset it.
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, EventClass] = {}
_DISCOVERED: bool = False


@dataclass(frozen=True, slots=True)
class ClassDelta:
    """The diff between previously-registered classes and the current set."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    definition_changed: tuple[str, ...]
    unchanged: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.definition_changed)


def register(cls: EventClass) -> None:
    """Register an event class. Validates and stores in the in-memory map.

    Raises :class:`ClassValidationError` if validation fails. Re-registering
    an EventClass with the same identity (``cls is`` the existing entry)
    is a no-op; re-registering with a different object but the same
    ``class_id`` is rejected.
    """
    if not isinstance(cls, EventClass):
        raise ClassValidationError(f"register() expected an EventClass, got {type(cls).__name__}")
    _validate(cls)
    existing = _REGISTRY.get(cls.class_id)
    if existing is not None and existing is not cls:
        raise ClassValidationError(
            f"class_id {cls.class_id!r} already registered with a different EventClass; "
            "re-registration of a different object is not allowed (use unregister or "
            "give the class a new class_id)"
        )
    _REGISTRY[cls.class_id] = cls


def get(class_id: str) -> EventClass:
    """Return the EventClass for ``class_id`` or raise KeyError."""
    _ensure_discovered()
    cls = _REGISTRY.get(class_id)
    if cls is None:
        raise KeyError(f"unknown class_id {class_id!r}")
    return cls


def get_all(*, sector: Sector | None = None) -> tuple[EventClass, ...]:
    """Return every registered class, optionally filtered by domain_sector."""
    _ensure_discovered()
    classes = list(_REGISTRY.values())
    if sector is not None:
        classes = [c for c in classes if c.domain_sector == sector]
    classes.sort(key=lambda c: c.class_id)
    return tuple(classes)


def known_class_ids() -> tuple[str, ...]:
    """Return the set of class_ids currently registered, sorted."""
    _ensure_discovered()
    return tuple(sorted(_REGISTRY.keys()))


def is_registered(class_id: str) -> bool:
    _ensure_discovered()
    return class_id in _REGISTRY


def discover() -> tuple[str, ...]:
    """Force the auto-discovery pass and return the class_ids found.

    Idempotent: a second call is a no-op once discovery has run. Tests
    that need a fresh registry call :func:`_clear_for_tests` first.
    """
    _ensure_discovered()
    return known_class_ids()


def sync_to_store(
    conn: duckdb.DuckDBPyConnection,
    *,
    when: datetime | None = None,
) -> ClassDelta:
    """Reconcile the live registry against ``pl_event_classes``.

    - Classes in the registry but absent from the table are inserted.
    - Classes in both with a changed definition_version trigger an
      update.
    - Classes in the table but absent from the registry get
      ``removed_at`` stamped.

    Returns a :class:`ClassDelta` describing the diff so the refresh
    runner can decide whether to bump the library version.
    """
    _ensure_discovered()
    ts = when or datetime.now(tz=UTC)
    rows = conn.execute(
        "SELECT class_id, definition_version, removed_at FROM pl_event_classes"
    ).fetchall()
    existing: dict[str, tuple[int, datetime | None]] = {str(r[0]): (int(r[1]), r[2]) for r in rows}

    current_ids = set(_REGISTRY.keys())
    existing_ids = {cid for cid, (_, removed_at) in existing.items() if removed_at is None}

    added: list[str] = []
    definition_changed: list[str] = []
    unchanged: list[str] = []
    for class_id, cls in _REGISTRY.items():
        prior = existing.get(class_id)
        if prior is None or prior[1] is not None:
            # Either brand-new or previously removed and now re-registered.
            added.append(class_id)
        elif prior[0] != cls.definition_version:
            definition_changed.append(class_id)
        else:
            unchanged.append(class_id)
        upsert_event_class(conn, cls, when=ts)

    removed: list[str] = []
    for class_id in sorted(existing_ids - current_ids):
        mark_event_class_removed(conn, class_id, when=ts)
        removed.append(class_id)

    return ClassDelta(
        added=tuple(sorted(added)),
        removed=tuple(removed),
        definition_changed=tuple(sorted(definition_changed)),
        unchanged=tuple(sorted(unchanged)),
    )


# ---------------------------------------------------------------------------
# Test-only helpers (leading underscores) — production code never touches.
# ---------------------------------------------------------------------------
def _clear_for_tests() -> None:
    """Reset the registry. Test fixtures call this in setup/teardown."""
    global _DISCOVERED
    _REGISTRY.clear()
    _DISCOVERED = False


def _set_discovered_for_tests(value: bool = True) -> None:
    """Force the discovery flag without running the import pass."""
    global _DISCOVERED
    _DISCOVERED = value


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _validate(cls: EventClass) -> None:
    """REQ-PL-CLASS-004 — registration-time validation.

    The :class:`EventClass.__post_init__` already enforces the structural
    invariants (non-empty fields, version positivity, etc.); this helper
    adds checks that need the live registry, primarily ensuring the
    occurrence_query and per-precursor / per-feature queries are
    callable. Type signatures aren't strictly checked because that
    requires running the queries, which we save for refresh time.
    """
    if not callable(cls.occurrence_query):
        raise ClassValidationError(f"class {cls.class_id!r}: occurrence_query must be callable")
    for p in cls.precursors:
        if not callable(p.query):
            raise ClassValidationError(
                f"class {cls.class_id!r} precursor {p.variable_id!r}: query must be callable"
            )
    for f in cls.analogue_features:
        if not callable(f.query):
            raise ClassValidationError(
                f"class {cls.class_id!r} analogue feature {f.feature_id!r}: query must be callable"
            )


def _ensure_discovered() -> None:
    """Run the auto-discovery pass if it hasn't already."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    _import_class_modules()


def _import_class_modules() -> None:
    """Walk ``classes/`` and import every module so its ``CLASS`` registers.

    Modules that don't expose ``CLASS`` are ignored (so package-level
    ``__init__.py`` and helper modules can live alongside).
    """
    try:
        package = importlib.import_module(_CLASSES_PACKAGE)
    except ImportError as exc:
        raise ClassValidationError(f"could not import {_CLASSES_PACKAGE}: {exc}") from exc
    if not hasattr(package, "__path__"):
        return
    for mod_info in pkgutil.iter_modules(package.__path__):
        if mod_info.name.startswith("_"):
            continue
        full_name = f"{_CLASSES_PACKAGE}.{mod_info.name}"
        module = importlib.import_module(full_name)
        cls = getattr(module, _CLASS_ATTRIBUTE, None)
        if cls is None:
            logger.debug("skipping %s — no module-level CLASS attribute", full_name)
            continue
        if not isinstance(cls, EventClass):
            raise ClassValidationError(
                f"{full_name}: module-level CLASS must be an EventClass, got {type(cls).__name__}"
            )
        register(cls)
