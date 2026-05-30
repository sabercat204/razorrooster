"""Calibration log assembler (T-RG-024; design §3.5; OQ-RG-001 + OQ-RG-004).

Returns a content dict shaped like::

    {
      "type": "calibration",
      "resolutions": [
        {
          "comparison_id": "...",
          "class_id": "...",
          "class_title": "...",
          "condition_id": "...",
          "resolution_outcome": "yes" | "no" | "invalid",
          "model_probability": float,
          "market_probability": float | None,
          "polarity": "aligned" | "inverted",
          "outcome_observed": int,
          "days_to_resolution": int,
          "verdict_text": str,
          "predicted_band": "high" | "mid" | "low",
        },
        ...
      ],
      "sector_brier_scores": [
        {
          "sector": str,
          "n_resolutions": int,
          "brier_score": float,
          "miscalibrated": bool,
          "window_days": int,
        },
        ...
      ],
    }

Per-sector Brier scores aggregate every resolution in the last
``brier_window_days`` (default 90) by ``pl_event_classes.domain_sector``
and surface a ``miscalibrated`` flag when the rolling Brier exceeds
``miscalibration_threshold`` (default 0.25 per supplement §3).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import duckdb
import yaml

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "calibration_verdicts.yaml"
)

PredictedBand = Literal["high", "mid", "low"]


# Supplement §3 defaults: 90-day rolling window, miscalibration
# threshold of Brier > 0.25. Operators can override per call.
DEFAULT_BRIER_WINDOW_DAYS: Final[int] = 90
DEFAULT_MISCALIBRATION_THRESHOLD: Final[float] = 0.25


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    template_path: Path | None = None,
    brier_window_days: int = DEFAULT_BRIER_WINDOW_DAYS,
    miscalibration_threshold: float = DEFAULT_MISCALIBRATION_THRESHOLD,
    brier_window_days_per_sector: Mapping[str, int] | None = None,
    miscalibration_threshold_per_sector: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    verdicts_template = _load_verdict_templates(template_path)
    rows = conn.execute(
        "SELECT r.comparison_id, r.condition_id, r.resolution_outcome, "
        "r.resolution_ts, r.model_probability_at_comparison, "
        "r.market_probability_at_comparison, r.polarity_at_comparison, "
        "r.outcome_observed, r.linked_at, c.class_id, c.computed_at, r.venue "
        "FROM comparison_resolutions r "
        "LEFT JOIN comparisons c ON r.comparison_id = c.comparison_id "
        "WHERE r.linked_at >= ? AND r.linked_at <= ? "
        "ORDER BY r.resolution_ts DESC",
        [since_ts, until_ts],
    ).fetchall()
    resolutions: list[dict[str, Any]] = []
    for row in rows:
        comparison_id = str(row[0])
        outcome = str(row[2])
        model_p = float(row[4])
        polarity = str(row[6]) if row[6] is not None else "aligned"
        class_id = str(row[9]) if row[9] is not None else "?"
        class_title = _query_class_title(conn, class_id=class_id) or class_id
        days_to_resolution = _days_between(row[10] if row[10] is not None else row[8], row[3])
        band = predicted_band(model_p)
        verdict_text = _build_verdict(
            verdicts_template, band=band, outcome=outcome, model_p=model_p
        )
        resolutions.append(
            {
                "comparison_id": comparison_id,
                "class_id": class_id,
                "class_title": class_title,
                "condition_id": str(row[1]),
                "venue": str(row[11]) if row[11] is not None else "polymarket",
                "resolution_outcome": outcome,
                "model_probability": model_p,
                "market_probability": (float(row[5]) if row[5] is not None else None),
                "polarity": polarity,
                "outcome_observed": int(row[7]),
                "days_to_resolution": days_to_resolution,
                "predicted_band": band,
                "verdict_text": verdict_text,
            }
        )
    sector_brier_scores = _compute_sector_brier_scores(
        conn,
        until_ts=until_ts,
        window_days=brier_window_days,
        miscalibration_threshold=miscalibration_threshold,
        window_days_per_sector=brier_window_days_per_sector,
        miscalibration_threshold_per_sector=miscalibration_threshold_per_sector,
    )
    return {
        "type": "calibration",
        "resolutions": resolutions,
        "sector_brier_scores": sector_brier_scores,
    }


# -- helpers exposed for tests ---------------------------------------------


def predicted_band(p: float) -> PredictedBand:
    """Map a probability to a predicted band per OQ-RG-001."""
    if p >= 0.7:
        return "high"
    if p >= 0.3:
        return "mid"
    return "low"


# -- internals --------------------------------------------------------------


def _load_verdict_templates(path: Path | None = None) -> dict[str, str]:
    target = path or DEFAULT_TEMPLATE_PATH
    if not target.exists():
        return _FALLBACK_TEMPLATES
    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return _FALLBACK_TEMPLATES
    if not isinstance(payload, dict):
        return _FALLBACK_TEMPLATES
    raw = payload.get("verdicts") or {}
    if not isinstance(raw, dict):
        return _FALLBACK_TEMPLATES
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out or _FALLBACK_TEMPLATES


def _build_verdict(
    templates: dict[str, str],
    *,
    band: PredictedBand,
    outcome: str,
    model_p: float,
) -> str:
    key = f"{band}_{outcome}"
    template = (
        templates.get(key)
        or _FALLBACK_TEMPLATES.get(key)
        or "Model said {p}; resolution outcome recorded."
    )
    return template.format(p=f"{model_p:.2f}")


def _query_class_title(conn: duckdb.DuckDBPyConnection, *, class_id: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT title FROM pl_event_classes WHERE class_id = ?",
            [class_id],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _days_between(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return max(0, (end - start).days)


def _compute_sector_brier_scores(
    conn: duckdb.DuckDBPyConnection,
    *,
    until_ts: datetime,
    window_days: int,
    miscalibration_threshold: float,
    window_days_per_sector: Mapping[str, int] | None = None,
    miscalibration_threshold_per_sector: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Compute per-sector Brier scores over a rolling window.

    The Brier score is the mean squared error between the model's
    probability and the observed outcome (0 or 1, polarity-adjusted
    so the model's stated event matches the observed binary). Lower
    is better; 0.25 is the random-guesser baseline at p=0.5.

    Skips invalidated markets (``resolution_outcome='invalid'``) since
    they have no defined outcome to score against.

    Per-sector overrides shadow the global ``window_days`` and
    ``miscalibration_threshold`` per ``domain_sector``. We query the
    broadest possible window (the max across sectors) and filter in
    Python so the SQL stays one statement; for v1 scale this is
    cheaper than running one SQL per sector.

    Returns a list of per-sector summary dicts, sorted alphabetically
    by sector. ``miscalibrated=True`` when the rolling Brier exceeds
    the applicable threshold. Sectors with zero scoreable
    resolutions in their window are omitted.
    """
    per_sector_window = dict(window_days_per_sector or {})
    per_sector_threshold = dict(miscalibration_threshold_per_sector or {})

    # Use the broadest window across the global default and any
    # per-sector overrides so we read all the rows we might need in
    # a single SQL pass; per-sector filtering happens in Python.
    broadest_window = max([window_days, *per_sector_window.values()])
    window_start = until_ts - timedelta(days=broadest_window)
    rows = conn.execute(
        "SELECT c.class_id, ec.domain_sector, "
        "r.model_probability_at_comparison, r.outcome_observed, "
        "r.resolution_outcome, r.resolution_ts "
        "FROM comparison_resolutions r "
        "LEFT JOIN comparisons c ON r.comparison_id = c.comparison_id "
        "LEFT JOIN pl_event_classes ec ON c.class_id = ec.class_id "
        "WHERE r.resolution_ts >= ? AND r.resolution_ts <= ? "
        "AND r.resolution_outcome != 'invalid'",
        [window_start, until_ts],
    ).fetchall()

    by_sector: dict[str, list[float]] = defaultdict(list)
    for _class_id, sector, model_p, outcome_observed, _outcome, resolution_ts in rows:
        if sector is None or model_p is None or outcome_observed is None or resolution_ts is None:
            continue
        sector_str = str(sector)
        # Apply the sector-specific window: skip rows that are in the
        # broadest window but outside this sector's narrower one.
        applicable_window = per_sector_window.get(sector_str, window_days)
        sector_window_start = until_ts - timedelta(days=applicable_window)
        if resolution_ts < sector_window_start:
            continue
        squared_err = (float(model_p) - float(outcome_observed)) ** 2
        by_sector[sector_str].append(squared_err)

    summary: list[dict[str, Any]] = []
    for sector_name in sorted(by_sector.keys()):
        squared_errors = by_sector[sector_name]
        if not squared_errors:
            continue
        brier = sum(squared_errors) / len(squared_errors)
        applicable_window = per_sector_window.get(sector_name, window_days)
        applicable_threshold = per_sector_threshold.get(sector_name, miscalibration_threshold)
        summary.append(
            {
                "sector": sector_name,
                "n_resolutions": len(squared_errors),
                "brier_score": round(brier, 4),
                "miscalibrated": brier > applicable_threshold,
                "window_days": applicable_window,
                "miscalibration_threshold": applicable_threshold,
            }
        )
    return summary


_FALLBACK_TEMPLATES: dict[str, str] = {
    "high_yes": "Model said {p} → resolved YES; in line with predicted likelihood.",
    "high_no": "Model said {p} → resolved NO; this counts against the model's calibration.",
    "mid_yes": "Model said {p} → resolved YES; consistent with mid-confidence prediction.",
    "mid_no": "Model said {p} → resolved NO; consistent with mid-confidence prediction.",
    "low_yes": "Model said {p} → resolved YES; the model assigned low probability, but tail outcomes happen.",
    "low_no": "Model said {p} → resolved NO; in line with predicted likelihood.",
    "high_invalid": "Model said {p}; market was invalidated, outcome is undefined for calibration.",
    "mid_invalid": "Model said {p}; market was invalidated, outcome is undefined for calibration.",
    "low_invalid": "Model said {p}; market was invalidated, outcome is undefined for calibration.",
}


__all__ = ["assemble", "predicted_band"]
