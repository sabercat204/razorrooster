"""T-CB-048 — determinism re-run gate.

REQ-CB-FREEZE-003 propagation gate. The Phase 7 upgrade bumped the
``polymarket_resolution_calibration`` meta-class's ``definition_version``
from ``1`` to ``2`` so cached :class:`BacktestRun` rows produced during
the stub era (when ``_occurrences`` returned an empty DataFrame) cannot
silently reuse under the upgraded canonical three-table join.

This test proves the propagation by computing two ``run_id`` digests
that differ ONLY in the ``class_definition_versions`` mapping for the
meta-class — the digests must differ. It then asserts the meta-class's
on-disk ``definition_version`` is the post-upgrade value, so the
mapping passed to :func:`compute_run_id_for_params` in production
(sourced from the registry at replay time) actually carries the new
version forward.
"""

from __future__ import annotations

from datetime import UTC, datetime

from razor_rooster.calibration_backtest.models import RunParameters
from razor_rooster.calibration_backtest.run_id import compute_run_id_for_params
from razor_rooster.pattern_library.classes import (
    polymarket_resolution_calibration as pl_meta,
)


def _params() -> RunParameters:
    """Build a fixed :class:`RunParameters` whose only varying input
    across the two digest computations is the per-class definition
    version mapping passed to :func:`compute_run_id_for_params`.

    The replay window, lag, sectors, and venues are constant; the
    sole class is the meta-class itself. Bin counts are deliberately
    omitted because they are display-only (design §3.4) and do not
    enter the canonical hash.
    """
    return RunParameters(
        since_ts=datetime(2025, 1, 1, tzinfo=UTC),
        until_ts=datetime(2025, 6, 1, tzinfo=UTC),
        lag_days=7,
        class_ids=("polymarket_resolution_calibration",),
        sectors=(),
        venues=("polymarket",),
        allow_recent=False,
    )


def test_definition_version_bump_changes_run_id() -> None:
    """Bumping ``definition_version`` from ``1`` to ``2`` for the meta-
    class produces a different ``run_id`` (REQ-CB-FREEZE-003).

    Two calls to :func:`compute_run_id_for_params` differ only in the
    ``class_definition_versions`` mapping. The library_version,
    system_revision, and ``RunParameters`` instance are otherwise
    identical. The digests MUST differ — otherwise stub-era cached
    runs could silently reuse under the upgraded query.
    """
    params = _params()
    run_id_a = compute_run_id_for_params(
        params,
        library_version=1,
        system_revision="x",
        class_definition_versions={"polymarket_resolution_calibration": 1},
    )
    run_id_b = compute_run_id_for_params(
        params,
        library_version=1,
        system_revision="x",
        class_definition_versions={"polymarket_resolution_calibration": 2},
    )
    assert run_id_a != run_id_b, (
        "compute_run_id_for_params produced identical digests for "
        "definition_version 1 vs 2 — REQ-CB-FREEZE-003 propagation broken; "
        "stub-era cached BacktestRun rows could silently reuse."
    )


def test_meta_class_definition_version_is_post_upgrade() -> None:
    """The meta-class's on-disk ``definition_version`` is ``2``.

    Phase 7 (T-CB-042 amendment) bumped ``definition_version`` from
    ``1`` to ``2`` in the :class:`EventClass` literal so the canonical
    three-table join replaces the empty-frame stub. If this assertion
    fails the prior test still passes — but production replays would
    pass the OLD version through to the hash, which is the bug this
    gate prevents.
    """
    assert pl_meta.polymarket_resolution_calibration.definition_version == 2, (
        "polymarket_resolution_calibration.definition_version must be 2 "
        "post-Phase 7 upgrade (T-CB-042). REQ-CB-FREEZE-003 propagation "
        "depends on the registry sourcing this value when building the "
        "class_definition_versions mapping at replay time."
    )
