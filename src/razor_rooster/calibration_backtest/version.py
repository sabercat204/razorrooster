"""Subsystem version pin and system-revision capture (T-CB-002).

This module owns the calibration_backtest subsystem version constant and
the helper that resolves the running system's git revision. The version
constant is persisted alongside every backtest run so downstream readers
can detect schema or semantic drift; the system revision is persisted on
``backtest_runs.system_revision`` so forensic tracing can map a stored
result back to the codebase that produced it.

Capture strategy for ``get_system_revision``:

* Primary: ``git rev-parse HEAD`` against the working tree.
* Fallback: ``SYSTEM_REVISION_FALLBACK`` whenever git is unavailable
  (no binary, not a repo, command failure). The helper never raises;
  callers receive a deterministic string so they can persist it.

Subsequent tasks (T-CB-002 run-id derivation, T-CB-003 models, etc.)
will layer environment-variable and ``importlib.metadata`` fallbacks on
top of this primitive.
"""

from __future__ import annotations

import subprocess
from typing import Final

CALIBRATION_BACKTEST_VERSION: Final[str] = "1.0.0"

SYSTEM_REVISION_FALLBACK: Final[str] = "unknown"

# Backwards-compatible alias for the bootstrap stub. T-CB-002 keeps the
# old name available so existing imports do not break while downstream
# tasks migrate to the new constant.
SUBSYSTEM_VERSION: Final[str] = CALIBRATION_BACKTEST_VERSION


def get_system_revision() -> str:
    """Return the current git HEAD SHA, or :data:`SYSTEM_REVISION_FALLBACK`.

    Runs ``git rev-parse HEAD`` and captures stdout. Any failure
    (missing git binary, not a repo, non-zero exit, decode error) is
    swallowed and the helper returns :data:`SYSTEM_REVISION_FALLBACK`.
    The function never raises so callers can rely on a deterministic
    string for persistence.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return SYSTEM_REVISION_FALLBACK
    revision = completed.stdout.strip()
    if not revision:
        return SYSTEM_REVISION_FALLBACK
    return revision


# T-CB-026 / T-CB-002 alias: the design doc and Phase 4 review advisories
# refer to ``resolve_system_revision``; the bootstrap landed
# :func:`get_system_revision` first. We keep both names so existing
# imports continue working while the canonical run-id wiring uses the
# spec name.
def resolve_system_revision() -> str:
    """Return the current git HEAD SHA (alias for :func:`get_system_revision`).

    Provided so callers in the replay loop can reference the spec name
    (``version.resolve_system_revision()``) without spelling the alias.
    The behaviour is identical to :func:`get_system_revision`: any
    failure (missing git binary, non-repo, decode error) yields
    :data:`SYSTEM_REVISION_FALLBACK` and never raises.
    """
    return get_system_revision()


__all__ = [
    "CALIBRATION_BACKTEST_VERSION",
    "SUBSYSTEM_VERSION",
    "SYSTEM_REVISION_FALLBACK",
    "get_system_revision",
    "resolve_system_revision",
]
