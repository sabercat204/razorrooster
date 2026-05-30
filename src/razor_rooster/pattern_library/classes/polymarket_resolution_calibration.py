"""Seed class: Polymarket resolution calibration meta-class (T-PL-077).

The linchpin for OT-006 (calibration validation). When downstream
subsystems begin logging predictions, this class consumes that log
joined against ``polymarket_resolutions`` to verify calibration
empirically — not just on synthetic predictions but on what the
operator actually saw at the time of decision.

v1 scaffold: prediction logs don't exist yet (mispricing_detector and
report_generator add them later). The class returns an empty
occurrence list until those subsystems land. Documentation explicitly
notes this.

Class authors who later add the prediction-log table can swap the
empty-frame query for a real join without touching the rest of the
class definition or the refresh pipeline.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb
import pandas as pd

from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)


def _occurrences(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Empty by design until prediction-log table exists.

    When mispricing_detector and report_generator land their
    prediction-log tables, this query becomes a join against
    ``polymarket_resolutions`` returning resolved markets that had
    a prior model prediction logged.
    """
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


CLASS = EventClass(
    class_id="polymarket_resolution_calibration",
    title="Polymarket resolution calibration meta-class",
    description=(
        "Tracks calibration of the library's prior predictions against "
        "Polymarket-resolution ground truth. v1 scaffold returns empty "
        "until downstream subsystems (mispricing_detector, report_generator) "
        "begin logging predictions. Linchpin for OT-006 — full calibration "
        "backtest infrastructure."
    ),
    domain_sector=Sector.CROSS_CUTTING,
    occurrence_query=_occurrences,
    precursors=(),
    analogue_features=(),
    base_rate_window_default=timedelta(days=365 * 5),
    refractory_months=1,
    baseline_sample_size=200,
)
