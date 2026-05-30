"""Library version pin (T-PL-001 / T-PL-020).

The ``LIBRARY_VERSION`` integer bumps in three cases (per design §3.6):

1. Code change — manually bumped here when computation engines or core
   models change. A pre-commit / CI check verifies the bump.
2. Class registry change — the refresh runner bumps automatically when
   a class is added or removed.
3. Class definition change — the refresh runner bumps automatically
   when a registered class's ``definition_version`` changes.

Outputs persisted by the library carry the version that produced them
(``library_version`` column on every output table). Downstream consumers
compare against ``current_version()`` to detect mismatches.

Detection of registry / definition changes lives in the refresh runner;
this module owns the constant and the bump-recording helper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import duckdb

from razor_rooster.pattern_library.persistence.operations import (
    record_library_version_bump,
)

logger = logging.getLogger(__name__)


# v1 — initial library. Bump on every breaking change.
LIBRARY_VERSION: Final[int] = 1


class BumpReason:
    """Constants for ``pl_library_versions.bump_reason`` values."""

    CODE_CHANGE: Final[str] = "code_change"
    CLASS_ADDED: Final[str] = "class_added"
    CLASS_MODIFIED: Final[str] = "class_modified"
    CLASS_REMOVED: Final[str] = "class_removed"


_VALID_BUMP_REASONS: Final[frozenset[str]] = frozenset(
    {
        BumpReason.CODE_CHANGE,
        BumpReason.CLASS_ADDED,
        BumpReason.CLASS_MODIFIED,
        BumpReason.CLASS_REMOVED,
    }
)


@dataclass(frozen=True, slots=True)
class VersionBump:
    """Outcome of a bump call. Records what was bumped and why."""

    library_version: int
    bump_reason: str
    affected_class_ids: tuple[str, ...]
    bumped_at: datetime


def current_version() -> int:
    """Return the live library version. Downstream consumers call this."""
    return LIBRARY_VERSION


def bump_for_reason(
    conn: duckdb.DuckDBPyConnection,
    *,
    reason: str,
    affected_class_ids: tuple[str, ...] = (),
    notes: str | None = None,
    when: datetime | None = None,
) -> VersionBump:
    """Record a library version bump.

    The constant ``LIBRARY_VERSION`` is the source of truth for the
    integer; this helper just records the bump in
    ``pl_library_versions`` so the refresh log and any downstream
    consumer can audit *why* the version is what it is.

    A rejected reason raises :class:`ValueError` with the allowed set.
    """
    if reason not in _VALID_BUMP_REASONS:
        raise ValueError(f"unknown bump_reason {reason!r}; allowed: {sorted(_VALID_BUMP_REASONS)}")
    ts = when or datetime.now(tz=UTC)
    version = current_version()
    record_library_version_bump(
        conn,
        library_version=version,
        bump_reason=reason,
        affected_class_ids=affected_class_ids,
        notes=notes,
        when=ts,
    )
    logger.info(
        "library version bump recorded: version=%d reason=%s affected=%s",
        version,
        reason,
        affected_class_ids,
    )
    return VersionBump(
        library_version=version,
        bump_reason=reason,
        affected_class_ids=affected_class_ids,
        bumped_at=ts,
    )
