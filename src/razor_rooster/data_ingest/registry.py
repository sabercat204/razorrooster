"""Source registry (T-032).

The registry is the single source of truth for "which connectors does this
build know about" (REQ-EXT-001). Connector modules self-register at import
time by calling :func:`register` (typically as a decorator on the connector
class).

The registry stores connector *classes*, not instances. Instances are
constructed by the cycle scheduler (T-033) when the connector is due,
because each cycle wants a fresh connector instance bound to a current
:class:`DuckDBStore` and the latest credentials.

Re-registration of an already-known source raises :class:`DuplicateSourceId`
to catch accidental double-imports. Unregistration is supported but
intentionally non-public — only tests should call it.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from razor_rooster.data_ingest.connectors.base import Connector

logger = logging.getLogger(__name__)


class RegistryError(ValueError):
    """Base class for registry errors."""


class DuplicateSourceId(RegistryError):
    """Raised when two connector classes claim the same ``source_id``."""


class UnknownSourceId(RegistryError):
    """Raised when looking up a source_id that wasn't registered."""


# The registry is a process-wide dict mapping source_id → connector class.
_REGISTRY: dict[str, type[Connector]] = {}


_C = TypeVar("_C", bound=type[Connector])


def register(connector_class: _C) -> _C:
    """Register a connector class. Usable as a decorator.

    Returns the class unchanged so it can be used in two ways:

    .. code-block:: python

        @register
        class FredConnector(Connector):
            ...

    or, equivalently, as a function call after class definition::

        register(FredConnector)
    """
    source_id = getattr(connector_class, "source_id", "")
    if not source_id:
        raise RegistryError(f"{connector_class.__name__} has no source_id; cannot register")
    if source_id in _REGISTRY:
        existing = _REGISTRY[source_id]
        if existing is connector_class:
            # Idempotent on identity — re-importing the same module is fine.
            return connector_class
        raise DuplicateSourceId(
            f"source_id {source_id!r} is already registered by "
            f"{existing.__module__}.{existing.__name__}; "
            f"cannot register {connector_class.__module__}.{connector_class.__name__}"
        )
    _REGISTRY[source_id] = connector_class
    logger.debug(
        "registered connector source_id=%s class=%s",
        source_id,
        connector_class.__name__,
        extra={"source_id": source_id},
    )
    return connector_class


def get(source_id: str) -> type[Connector]:
    """Look up a registered connector class by source_id.

    Raises :class:`UnknownSourceId` if the source isn't registered.
    """
    cls = _REGISTRY.get(source_id)
    if cls is None:
        raise UnknownSourceId(f"no connector registered for source_id {source_id!r}")
    return cls


def get_all() -> tuple[type[Connector], ...]:
    """Return all registered connector classes in source_id order."""
    return tuple(_REGISTRY[sid] for sid in sorted(_REGISTRY))


def known_source_ids() -> tuple[str, ...]:
    """Return all registered source_ids in alphabetical order."""
    return tuple(sorted(_REGISTRY))


def is_registered(source_id: str) -> bool:
    """Return whether the given source_id has a registered connector."""
    return source_id in _REGISTRY


def _unregister_for_tests(source_id: str) -> None:
    """Remove a source from the registry. Tests only.

    The leading underscore signals "internal API"; production code should
    never call this.
    """
    _REGISTRY.pop(source_id, None)


def _clear_for_tests() -> None:
    """Clear the entire registry. Tests only."""
    _REGISTRY.clear()
