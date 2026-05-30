"""T-CB-005 — deterministic run_id derivation tests.

Covers the contract of
:mod:`razor_rooster.calibration_backtest.run_id`:

* Determinism: identical inputs -> identical 64-char SHA-256 hex digest.
* Order-invariance for sequence inputs (``class_ids``, ``sectors``,
  ``venues``).
* REQ-CB-FREEZE-003 propagation: any single class's
  ``definition_version`` bump produces a fresh digest.
* Single-field mutation invalidation across all hashed fields
  (``library_version``, ``system_revision``, ``lag_days``, ``since_ts``,
  ``until_ts``).
* :class:`RunIdInputs` validators: missing-class mapping, lag floor,
  inverted window, naive datetimes are all rejected with
  :class:`BacktestConfigError`.
* Canonical JSON shape: compact (no spaces) and key-sorted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from razor_rooster.calibration_backtest import (
    RunIdInputs,
    canonicalize,
    compute_run_id,
)
from razor_rooster.calibration_backtest.errors import BacktestConfigError

# -- helpers ---------------------------------------------------------------


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)


def _valid_inputs(**overrides: Any) -> RunIdInputs:
    base: dict[str, Any] = {
        "since_ts": _SINCE,
        "until_ts": _UNTIL,
        "lag_days": 7,
        "class_ids": ("currency_crisis", "armed_conflict", "election_upset"),
        "class_definition_versions": {
            "armed_conflict": 1,
            "currency_crisis": 2,
            "election_upset": 3,
        },
        "sectors": ("geopolitical", "macro"),
        "venues": ("polymarket", "kalshi"),
        "library_version": 1,
        "system_revision": "abc123def456",
    }
    base.update(overrides)
    class_ids = base["class_ids"]
    versions = base["class_definition_versions"]
    if isinstance(class_ids, Sequence) and isinstance(versions, Mapping):
        # No-op: just ensure types are right; assignment performed via overrides.
        pass
    return RunIdInputs(**base)


# -- determinism + format --------------------------------------------------


def test_compute_run_id_is_deterministic() -> None:
    """Same inputs produce the same digest twice."""
    a = compute_run_id(_valid_inputs())
    b = compute_run_id(_valid_inputs())
    assert a == b


def test_compute_run_id_is_64_hex_chars() -> None:
    """SHA-256 hex digest is 64 lowercase hex characters."""
    digest = compute_run_id(_valid_inputs())
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# -- order invariance ------------------------------------------------------


def test_class_ids_sorted_invariant() -> None:
    """``class_ids`` order at the call site does not affect the digest."""
    a = compute_run_id(
        _valid_inputs(
            class_ids=("armed_conflict", "currency_crisis", "election_upset"),
        )
    )
    b = compute_run_id(
        _valid_inputs(
            class_ids=("election_upset", "armed_conflict", "currency_crisis"),
        )
    )
    c = compute_run_id(
        _valid_inputs(
            class_ids=("currency_crisis", "election_upset", "armed_conflict"),
        )
    )
    assert a == b == c


def test_sectors_sorted_invariant() -> None:
    """``sectors`` order at the call site does not affect the digest."""
    a = compute_run_id(_valid_inputs(sectors=("geopolitical", "macro")))
    b = compute_run_id(_valid_inputs(sectors=("macro", "geopolitical")))
    assert a == b


def test_venues_sorted_invariant() -> None:
    """``venues`` order at the call site does not affect the digest."""
    a = compute_run_id(_valid_inputs(venues=("polymarket", "kalshi")))
    b = compute_run_id(_valid_inputs(venues=("kalshi", "polymarket")))
    assert a == b


def test_class_definition_versions_key_order_invariant() -> None:
    """Insertion order of ``class_definition_versions`` does not change the digest."""
    a = compute_run_id(
        _valid_inputs(
            class_definition_versions={
                "armed_conflict": 1,
                "currency_crisis": 2,
                "election_upset": 3,
            }
        )
    )
    b = compute_run_id(
        _valid_inputs(
            class_definition_versions={
                "election_upset": 3,
                "armed_conflict": 1,
                "currency_crisis": 2,
            }
        )
    )
    assert a == b


# -- field mutation invalidation -------------------------------------------


def test_definition_version_change_invalidates() -> None:
    """REQ-CB-FREEZE-003: bumping any class's ``definition_version`` changes the digest."""
    base = compute_run_id(_valid_inputs())
    bumped = compute_run_id(
        _valid_inputs(
            class_definition_versions={
                "armed_conflict": 2,  # bumped 1 -> 2
                "currency_crisis": 2,
                "election_upset": 3,
            }
        )
    )
    assert base != bumped


def test_library_version_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs())
    bumped = compute_run_id(_valid_inputs(library_version=2))
    assert base != bumped


def test_system_revision_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs())
    bumped = compute_run_id(_valid_inputs(system_revision="zzz999"))
    assert base != bumped


def test_lag_days_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs(lag_days=7))
    bumped = compute_run_id(_valid_inputs(lag_days=8))
    assert base != bumped


def test_since_ts_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs())
    bumped = compute_run_id(_valid_inputs(since_ts=_SINCE + timedelta(days=1)))
    assert base != bumped


def test_until_ts_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs())
    bumped = compute_run_id(_valid_inputs(until_ts=_UNTIL + timedelta(days=1)))
    assert base != bumped


def test_class_ids_membership_change_invalidates() -> None:
    """Removing a class_id must change the digest, even if its definition_version stays."""
    base = compute_run_id(_valid_inputs())
    smaller = compute_run_id(
        _valid_inputs(
            class_ids=("currency_crisis", "armed_conflict"),
            class_definition_versions={
                "armed_conflict": 1,
                "currency_crisis": 2,
            },
        )
    )
    assert base != smaller


def test_sectors_membership_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs())
    smaller = compute_run_id(_valid_inputs(sectors=("geopolitical",)))
    assert base != smaller


def test_venues_membership_change_invalidates() -> None:
    base = compute_run_id(_valid_inputs())
    smaller = compute_run_id(_valid_inputs(venues=("polymarket",)))
    assert base != smaller


# -- validator coverage ----------------------------------------------------


def test_class_id_not_in_definition_versions_raises() -> None:
    """Every ``class_id`` must have a pinned ``definition_version``."""
    with pytest.raises(BacktestConfigError, match="class_definition_versions is missing"):
        RunIdInputs(
            since_ts=_SINCE,
            until_ts=_UNTIL,
            lag_days=7,
            class_ids=("armed_conflict", "missing_class"),
            class_definition_versions={"armed_conflict": 1},
            sectors=("geopolitical",),
            venues=("polymarket",),
            library_version=1,
            system_revision="abc123",
        )


def test_invalid_lag_days_raises() -> None:
    with pytest.raises(BacktestConfigError, match="lag_days must be >= 1"):
        _valid_inputs(lag_days=0)


def test_negative_lag_days_raises() -> None:
    with pytest.raises(BacktestConfigError, match="lag_days must be >= 1"):
        _valid_inputs(lag_days=-1)


def test_until_before_since_raises() -> None:
    with pytest.raises(BacktestConfigError, match="since_ts must precede until_ts"):
        _valid_inputs(since_ts=_UNTIL, until_ts=_SINCE)


def test_until_equals_since_raises() -> None:
    with pytest.raises(BacktestConfigError, match="since_ts must precede until_ts"):
        _valid_inputs(since_ts=_SINCE, until_ts=_SINCE)


def test_naive_since_ts_raises() -> None:
    """Naive datetimes are rejected so the canonical ISO form is unambiguous."""
    naive = datetime(2024, 1, 1)
    with pytest.raises(BacktestConfigError, match="since_ts must be timezone-aware"):
        _valid_inputs(since_ts=naive)


def test_naive_until_ts_raises() -> None:
    naive = datetime(2024, 6, 1)
    with pytest.raises(BacktestConfigError, match="until_ts must be timezone-aware"):
        _valid_inputs(until_ts=naive)


def test_invalid_library_version_raises() -> None:
    with pytest.raises(BacktestConfigError, match="library_version must be >= 1"):
        _valid_inputs(library_version=0)


def test_empty_system_revision_raises() -> None:
    with pytest.raises(BacktestConfigError, match="system_revision must be non-empty"):
        _valid_inputs(system_revision="")


def test_definition_version_below_floor_raises() -> None:
    with pytest.raises(BacktestConfigError, match="must be >= 1"):
        _valid_inputs(
            class_definition_versions={
                "armed_conflict": 0,
                "currency_crisis": 2,
                "election_upset": 3,
            }
        )


# -- canonical-form shape --------------------------------------------------


def test_canonicalize_is_compact_json() -> None:
    """Canonical JSON has no whitespace separators."""
    canonical = canonicalize(_valid_inputs())
    assert " " not in canonical
    assert "\n" not in canonical
    assert "\t" not in canonical


def test_canonicalize_keys_sorted() -> None:
    """Top-level JSON keys are emitted in sorted order."""
    canonical = canonicalize(_valid_inputs())
    parsed = json.loads(canonical)
    assert isinstance(parsed, dict)
    keys = list(parsed.keys())
    assert keys == sorted(keys)


def test_canonicalize_class_ids_sorted_in_output() -> None:
    """``class_ids`` appears sorted in the canonical JSON."""
    canonical = canonicalize(
        _valid_inputs(
            class_ids=("zzz", "aaa", "mmm"),
            class_definition_versions={"aaa": 1, "mmm": 2, "zzz": 3},
        )
    )
    parsed = json.loads(canonical)
    assert parsed["class_ids"] == ["aaa", "mmm", "zzz"]


def test_canonicalize_class_definition_versions_sorted_in_output() -> None:
    """``class_definition_versions`` is emitted with sorted keys."""
    canonical = canonicalize(
        _valid_inputs(
            class_definition_versions={
                "election_upset": 3,
                "armed_conflict": 1,
                "currency_crisis": 2,
            }
        )
    )
    parsed = json.loads(canonical)
    versions = parsed["class_definition_versions"]
    assert list(versions.keys()) == sorted(versions.keys())


def test_canonicalize_includes_all_fields() -> None:
    """Every :class:`RunIdInputs` field is present in the canonical JSON."""
    canonical = canonicalize(_valid_inputs())
    parsed = json.loads(canonical)
    expected_keys = {
        "since_ts",
        "until_ts",
        "lag_days",
        "class_ids",
        "class_definition_versions",
        "sectors",
        "venues",
        "library_version",
        "system_revision",
    }
    assert set(parsed.keys()) == expected_keys


def test_canonicalize_emits_iso_datetimes() -> None:
    canonical = canonicalize(_valid_inputs())
    parsed = json.loads(canonical)
    assert parsed["since_ts"] == _SINCE.isoformat()
    assert parsed["until_ts"] == _UNTIL.isoformat()


# -- public re-exports -----------------------------------------------------


def test_public_reexports_present() -> None:
    """``RunIdInputs``, ``canonicalize``, ``compute_run_id`` are re-exported."""
    import razor_rooster.calibration_backtest as pkg

    assert "RunIdInputs" in pkg.__all__
    assert "canonicalize" in pkg.__all__
    assert "compute_run_id" in pkg.__all__
    assert pkg.RunIdInputs is RunIdInputs
    assert pkg.canonicalize is canonicalize
    assert pkg.compute_run_id is compute_run_id
