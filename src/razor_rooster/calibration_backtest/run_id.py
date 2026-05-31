"""Deterministic run-id derivation for calibration_backtest (T-CB-005).

Implements REQ-CB-RUN-001 (deterministic run identifier) and
REQ-CB-FREEZE-003 (per-class definition_version capture). The run id is
the SHA-256 hex digest of a canonical JSON serialization of the
:class:`RunIdInputs` tuple. Canonicalization rules (design §3.4):

* Top-level keys are sorted alphabetically (``json.dumps(sort_keys=True)``).
* Sequence parameters (``class_ids``, ``sectors``, ``venues``) are sorted
  before serialization so call-site ordering does not change the digest.
* ``class_definition_versions`` is emitted as a key-sorted mapping so
  bumping any one class's ``definition_version`` propagates into the
  digest (REQ-CB-FREEZE-003).
* Datetimes are required to be timezone-aware and are serialized in ISO
  8601 form (``datetime.isoformat()``); naive datetimes raise
  :class:`razor_rooster.calibration_backtest.errors.BacktestConfigError`.
* Compact separators (``","``, ``":"``) suppress whitespace so the
  byte string fed into the hash is invariant under unrelated formatter
  changes.

The module is intentionally narrow: only :class:`RunIdInputs`,
:func:`canonicalize`, and :func:`compute_run_id` are exported. Wiring
into the replay loop (resolving live class definition versions, library
version capture, system revision capture) is layered on top by later
tasks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from razor_rooster.calibration_backtest.errors import BacktestConfigError

_RUN_ID_HEX_LENGTH: Final[int] = 64


@dataclass(frozen=True, slots=True)
class RunIdInputs:
    """Inputs that uniquely determine a backtest ``run_id`` (design §3.4).

    Bundles every value that, when changed, must invalidate the prior
    run_id: the replay window, lag policy, scoped class / sector / venue
    tuples, the per-class ``definition_version`` map (REQ-CB-FREEZE-003),
    the pattern-library ``library_version``, and the captured system
    revision. Sequences are accepted in any order; canonicalization
    sorts them. Mappings are accepted in any insertion order;
    canonicalization sorts by key.

    Validators in :meth:`__post_init__` reject configurations that the
    replay loop must never accept: lag below the floor, an empty or
    inverted replay window, naive datetimes, ``library_version`` below
    the floor, an empty ``system_revision``, and any ``class_id`` whose
    ``definition_version`` is missing from the supplied mapping.
    """

    since_ts: datetime
    until_ts: datetime
    lag_days: int
    class_ids: Sequence[str]
    class_definition_versions: Mapping[str, int]
    sectors: Sequence[str]
    venues: Sequence[str]
    library_version: int
    system_revision: str

    def __post_init__(self) -> None:
        if self.since_ts.tzinfo is None:
            raise BacktestConfigError(
                "RunIdInputs.since_ts must be timezone-aware, "
                f"got naive datetime {self.since_ts.isoformat()!r}"
            )
        if self.until_ts.tzinfo is None:
            raise BacktestConfigError(
                "RunIdInputs.until_ts must be timezone-aware, "
                f"got naive datetime {self.until_ts.isoformat()!r}"
            )
        if self.since_ts >= self.until_ts:
            raise BacktestConfigError(
                "RunIdInputs.since_ts must precede until_ts "
                f"(since_ts={self.since_ts.isoformat()}, "
                f"until_ts={self.until_ts.isoformat()})"
            )
        if self.lag_days < 1:
            raise BacktestConfigError(f"RunIdInputs.lag_days must be >= 1, got {self.lag_days!r}")
        if self.library_version < 1:
            raise BacktestConfigError(
                f"RunIdInputs.library_version must be >= 1, got {self.library_version!r}"
            )
        if not self.system_revision:
            raise BacktestConfigError("RunIdInputs.system_revision must be non-empty")
        for class_id in self.class_ids:
            if class_id not in self.class_definition_versions:
                raise BacktestConfigError(
                    f"RunIdInputs.class_definition_versions is missing class_id "
                    f"{class_id!r}; every class_id in class_ids must have a "
                    "pinned definition_version (REQ-CB-FREEZE-003)"
                )
        for class_id, version in self.class_definition_versions.items():
            if version < 1:
                raise BacktestConfigError(
                    f"RunIdInputs.class_definition_versions[{class_id!r}] must be >= 1, "
                    f"got {version!r}"
                )


def canonicalize(inputs: RunIdInputs) -> str:
    """Return the canonical JSON serialization of ``inputs``.

    Sequence fields are sorted; the ``class_definition_versions`` map is
    re-emitted with sorted keys. Datetimes are rendered via
    :meth:`datetime.isoformat`. The result is compact, key-sorted JSON
    suitable for SHA-256 hashing.
    """
    canonical: dict[str, Any] = {
        "since_ts": inputs.since_ts.isoformat(),
        "until_ts": inputs.until_ts.isoformat(),
        "lag_days": inputs.lag_days,
        "class_ids": sorted(inputs.class_ids),
        "class_definition_versions": dict(sorted(inputs.class_definition_versions.items())),
        "sectors": sorted(inputs.sectors),
        "venues": sorted(inputs.venues),
        "library_version": inputs.library_version,
        "system_revision": inputs.system_revision,
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def compute_run_id(inputs: RunIdInputs) -> str:
    """Return the SHA-256 hex digest of :func:`canonicalize` applied to ``inputs``.

    The digest is a 64-character lowercase hexadecimal string. Identical
    inputs produce identical digests across processes and hosts; any
    single-field mutation produces a different digest, including a bump
    to any class's ``definition_version`` (REQ-CB-FREEZE-003).
    """
    canonical = canonicalize(inputs)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Defensive: hashlib guarantees 64 hex chars for SHA-256, but assert
    # the contract so persistence-layer length constraints stay valid.
    if len(digest) != _RUN_ID_HEX_LENGTH:
        raise BacktestConfigError(
            f"compute_run_id produced digest of unexpected length {len(digest)!r}, "
            f"expected {_RUN_ID_HEX_LENGTH}"
        )
    return digest


def compute_run_id_for_params(
    params: Any,
    *,
    library_version: int,
    system_revision: str,
    class_definition_versions: Mapping[str, int],
) -> str:
    """Convenience wrapper: compute a canonical ``run_id`` from a :class:`RunParameters`.

    Builds a :class:`RunIdInputs` from the supplied
    :class:`razor_rooster.calibration_backtest.models.RunParameters`
    instance, the resolved pattern-library ``library_version``, the
    captured ``system_revision``, and the per-class
    ``definition_version`` mapping (REQ-CB-FREEZE-003), then forwards
    to :func:`compute_run_id`. The bin-count overrides on
    :class:`RunParameters` are deliberately not threaded into the hash —
    bin counts are display-only per design §3.4.

    The ``params`` argument is typed ``Any`` to avoid a circular import
    between :mod:`run_id` and :mod:`models`; in practice callers pass a
    :class:`razor_rooster.calibration_backtest.models.RunParameters`
    instance whose attribute surface mirrors :class:`RunIdInputs` minus
    the cross-cutting hash inputs (library_version, system_revision,
    class_definition_versions).
    """
    inputs = RunIdInputs(
        since_ts=params.since_ts,
        until_ts=params.until_ts,
        lag_days=params.lag_days,
        class_ids=tuple(params.class_ids),
        class_definition_versions=dict(class_definition_versions),
        sectors=tuple(params.sectors),
        venues=tuple(params.venues),
        library_version=library_version,
        system_revision=system_revision,
    )
    return compute_run_id(inputs)


__all__ = [
    "RunIdInputs",
    "canonicalize",
    "compute_run_id",
    "compute_run_id_for_params",
]
