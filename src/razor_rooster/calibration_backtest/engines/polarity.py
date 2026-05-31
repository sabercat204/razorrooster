"""Polarity resolver: ``comparison_resolutions`` + mapping fallback (T-CB-015).

Implements the three-tier polarity resolution chain documented in
``CALIBRATION_BACKTEST_DESIGN.md`` §3.5 and refined by the 2026-05-31
scout amendment that landed in ``CALIBRATION_BACKTEST_TASKS.md``
T-CB-015. Given a prediction timestamp and a ``(condition_id,
class_id, venue)`` triple, the resolver returns the polarity that
should be applied to align the Polymarket outcome with the model
class so the per-prediction Brier contribution carries the correct
``observed`` bit (REQ-CB-REPLAY-003).

Resolution order:

* **Tier 1 — ``comparison_resolutions`` (preferred).** Joins
  ``comparison_resolutions`` to ``comparisons`` on ``comparison_id``
  to recover ``class_id`` (the resolutions table itself does not
  carry it, per the scout amendment), filters to the requested
  ``venue`` and ``class_id``/``condition_id``, drops rows where
  ``resolution_outcome = 'invalid'`` so void resolutions do not feed
  phantom polarities into a backtest, and orders by ``resolution_ts
  ASC`` so the **earliest** resolution after ``prediction_ts`` wins.
  ASC ordering is correctness-critical: a later resolution may have
  observed a different polarity if the operator-curated mapping was
  flipped after the prediction was made, and the backtest must score
  against the polarity that was in effect when the model produced
  the prediction.
* **Tier 2 — ``class_market_mappings`` fallback.** Reads the active
  (``removed_at IS NULL``) mapping row for the requested
  ``(class_id, condition_id, venue)``. The caller is expected to
  flag the resulting prediction with
  ``mapping_mismatch_warning=True`` so operators can audit replays
  that fell back to the current mapping rather than a
  contemporaneous comparison resolution.
* **Tier 3 — abort.** Raises
  :class:`~razor_rooster.calibration_backtest.errors.NoPolarityError`
  carrying structured context. The replay loop catches the
  exception and records the prediction with ``status='skipped'`` and
  ``skip_reason='no_polarity_resolution'``.

The resolver is intentionally read-only: it accepts a DuckDB
connection, runs two narrow ``SELECT`` queries, and returns either a
``(polarity_value, source)`` tuple or raises. It performs no schema
mutations, opens no transactions, and does not require a
calibration-backtest schema to be present — only the
mispricing-detector tables ``comparisons``, ``comparison_resolutions``
and ``class_market_mappings`` (see
``razor_rooster.mispricing_detector.persistence.schemas``). This
keeps the dependency arrow pointing into ``mispricing_detector`` and
preserves the no-back-edge invariant from design §3.15.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

import duckdb

from razor_rooster.calibration_backtest.errors import NoPolarityError

_TIER_1_QUERY: Final[str] = """
SELECT cr.polarity_at_comparison
FROM comparison_resolutions AS cr
JOIN comparisons AS c USING (comparison_id)
WHERE c.condition_id = ?
  AND c.class_id = ?
  AND cr.venue = ?
  AND cr.resolution_ts > ?
  AND cr.resolution_outcome != 'invalid'
ORDER BY cr.resolution_ts ASC
LIMIT 1
""".strip()


_TIER_2_QUERY: Final[str] = """
SELECT polarity
FROM class_market_mappings
WHERE class_id = ?
  AND condition_id = ?
  AND venue = ?
  AND removed_at IS NULL
LIMIT 1
""".strip()


SOURCE_COMPARISON_RESOLUTIONS: Final[str] = "comparison_resolutions"
"""Sentinel returned by :func:`resolve` when Tier 1 hits."""


SOURCE_CURRENT_MAPPING_FALLBACK: Final[str] = "current_mapping_fallback"
"""Sentinel returned by :func:`resolve` when Tier 2 hits."""


def resolve(
    conn: duckdb.DuckDBPyConnection,
    prediction_ts: datetime,
    condition_id: str,
    class_id: str,
    *,
    venue: str = "polymarket",
) -> tuple[str, str]:
    """Resolve the polarity for a single replayed prediction.

    Parameters
    ----------
    conn:
        DuckDB connection on which the ``comparisons``,
        ``comparison_resolutions`` and ``class_market_mappings``
        tables are visible (typically the unified runtime database).
    prediction_ts:
        The point-in-time at which the model produced the
        prediction. Tier 1 only considers comparison resolutions
        whose ``resolution_ts`` is strictly greater than
        ``prediction_ts`` so the polarity reflects an outcome that
        had not yet occurred.
    condition_id:
        The Polymarket condition identifier the prediction is
        scored against.
    class_id:
        The pattern-library class identifier the prediction targets.
    venue:
        Market venue, used to filter both tiers; defaults to
        ``"polymarket"`` to preserve compatibility with pre-Kalshi
        callers and the seed library.

    Returns
    -------
    tuple[str, str]
        ``(polarity_value, source)`` where ``source`` is one of
        :data:`SOURCE_COMPARISON_RESOLUTIONS` or
        :data:`SOURCE_CURRENT_MAPPING_FALLBACK`. The polarity value
        itself is the raw string stored in the underlying table
        (``"aligned"`` / ``"inverted"`` for the v1 schema; the
        resolver passes it through verbatim so future schema-level
        polarity additions need no code change here).

    Raises
    ------
    NoPolarityError
        When neither Tier 1 nor Tier 2 produces a row. The
        exception carries the original ``prediction_ts``,
        ``condition_id``, ``class_id`` and ``venue`` so the caller
        can record the skip with ``skip_reason='no_polarity_resolution'``.
    """
    tier_1_row = conn.execute(
        _TIER_1_QUERY,
        [condition_id, class_id, venue, prediction_ts],
    ).fetchone()
    if tier_1_row is not None:
        polarity_value: str = tier_1_row[0]
        return polarity_value, SOURCE_COMPARISON_RESOLUTIONS

    tier_2_row = conn.execute(
        _TIER_2_QUERY,
        [class_id, condition_id, venue],
    ).fetchone()
    if tier_2_row is not None:
        fallback_polarity: str = tier_2_row[0]
        return fallback_polarity, SOURCE_CURRENT_MAPPING_FALLBACK

    raise NoPolarityError(
        prediction_ts=prediction_ts,
        condition_id=condition_id,
        class_id=class_id,
        venue=venue,
    )


__all__ = [
    "SOURCE_COMPARISON_RESOLUTIONS",
    "SOURCE_CURRENT_MAPPING_FALLBACK",
    "resolve",
]
