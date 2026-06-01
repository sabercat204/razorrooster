"""Seed class: Polymarket resolution calibration meta-class (T-PL-077).

The linchpin for OT-006 (calibration validation). This class consumes
``mispricing_detector`` comparison resolutions joined against
``polymarket_resolutions`` to verify calibration empirically — not just
on synthetic predictions but on what the operator actually saw at the
time of decision.

Linkage-coverage caveat
-----------------------
Some ``comparisons`` rows may not yet have a matching
``comparison_resolutions`` row until the mispricing_detector linkage
pass matures. The three-table inner join below intentionally excludes
those unlinked predictions — partial coverage is **expected**, not a
defect (per T-CB-042 amendment).

Polarity-correction trap
------------------------
``comparison_resolutions.outcome_observed`` is **already** polarity-
adjusted at write time (see ``mispricing_detector/models.py:148-149``).
The meta-class therefore reads raw ``pr.winning_outcome_label`` and
``cr.polarity_at_comparison`` and re-derives observed downstream. It
**never** reads ``cr.outcome_observed`` — doing so would apply polarity
twice and silently corrupt calibration.

Reuse pattern (REQ-CB-PL-002, design §3.2)
------------------------------------------
The meta-class consumes only DuckDB tables already populated by the
upstream subsystems (``polymarket_connector``, ``mispricing_detector``).
It does NOT import from ``razor_rooster.calibration_backtest`` — keeps
the dependency graph acyclic across the seven canonical packages.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb
import pandas as pd

from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)

# REQ-CB-PL-002 + design §3.2 reuse pattern: query joins only DuckDB
# tables owned by upstream subsystems (polymarket_connector,
# mispricing_detector). No imports from calibration_backtest.
_OCCURRENCES_SQL = """
SELECT
    c.condition_id,
    c.class_id,
    cr.polarity_at_comparison,
    pr.winning_outcome_label,
    pr.resolution_ts AS occurrence_ts,
    pr.invalidated
FROM comparison_resolutions AS cr
JOIN comparisons AS c
  ON c.comparison_id = cr.comparison_id
JOIN polymarket_resolutions AS pr
  ON pr.condition_id = c.condition_id
WHERE pr.invalidated = FALSE
  AND pr.superseded_at IS NULL
""".strip()


def _occurrences(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Resolved Polymarket markets with a logged comparison.

    Three-table inner join across ``comparison_resolutions``,
    ``comparisons``, and ``polymarket_resolutions``. Returns ALL
    resolved+linked occurrences across history; ``refresh._count_in_window``
    applies time filtering downstream (single-arg ``OccurrenceQuery``
    protocol — no bind parameters).

    No defensive empty-frame fallback: the upstream test fixtures must
    run ``run_pending_mispricing_migrations`` so ``comparisons`` and
    ``comparison_resolutions`` exist (per T-CB-043 amendment). A
    ``CatalogException`` here would correctly surface a production
    schema bug rather than silently masking it.
    """
    rows = _conn.execute(_OCCURRENCES_SQL).fetchall()
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[4] for r in rows], utc=True),
            "condition_id": [r[0] for r in rows],
            "class_id": [r[1] for r in rows],
            "polarity_at_comparison": [r[2] for r in rows],
            "winning_outcome_label": [r[3] for r in rows],
            "invalidated": [r[5] for r in rows],
        }
    )


CLASS = EventClass(
    class_id="polymarket_resolution_calibration",
    title="Polymarket resolution calibration meta-class",
    description=(
        "Tracks calibration of the library's prior predictions against "
        "Polymarket-resolution ground truth. Joins mispricing_detector's "
        "comparison_resolutions against polymarket_resolutions; partial "
        "coverage is expected until linkage matures. Linchpin for OT-006 — "
        "full calibration backtest infrastructure."
    ),
    domain_sector=Sector.CROSS_CUTTING,
    occurrence_query=_occurrences,
    precursors=(),
    analogue_features=(),
    base_rate_window_default=timedelta(days=365 * 5),
    refractory_months=1,
    baseline_sample_size=200,
    definition_version=2,
)

# Module-name alias for callers that reference the meta-class via the
# module's public symbol (e.g. ``from ...polymarket_resolution_calibration
# import polymarket_resolution_calibration``). The registry continues to
# discover the class via the canonical ``CLASS`` attribute.
polymarket_resolution_calibration = CLASS
