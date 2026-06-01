"""JSON renderer for ``calibration-backtest run`` output (T-CB-030).

Bypasses the framing linter because the consumer is a tool, not an
operator (REQ-CB-CLI-003). The canonical disclaimer text is
nevertheless surfaced as a top-level ``disclaimer`` field so any
downstream renderer that re-stringifies the payload still carries the
framing.

The output is generated via ``json.dumps(..., indent=2,
sort_keys=True)`` so identical inputs produce byte-identical output —
the determinism gate (T-CB-027) and the JSON round-trip test in
:mod:`tests.calibration_backtest.test_renderers` rely on this.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from razor_rooster.calibration_backtest.frame import DISCLAIMER
from razor_rooster.calibration_backtest.models import BacktestRun


def render_json(run: BacktestRun) -> str:
    """Render ``run`` as a deterministic JSON document.

    The returned string is sorted-key, 2-space indented, and ends with
    no trailing newline (matching :func:`json.dumps` defaults). Callers
    that want a newline-terminated payload should append ``"\\n"``.

    Args:
        run: The :class:`BacktestRun` row to render.

    Returns:
        Pretty-printed JSON document carrying every persisted run
        attribute plus the canonical disclaimer string.
    """

    payload: dict[str, Any] = {
        "run_id": run.run_id,
        "since_ts": _iso(run.since_ts),
        "until_ts": _iso(run.until_ts),
        "lag_days": run.lag_days,
        "library_version": run.library_version,
        "system_revision": run.system_revision,
        "started_at": _iso(run.started_at),
        "completed_at": _iso_or_none(run.completed_at),
        "status": run.status.value,
        "predictions_total": run.predictions_total,
        "predictions_scored": run.predictions_scored,
        "predictions_skipped": run.predictions_skipped,
        "overall_brier": run.overall_brier,
        "fallback_polarity_count": run.fallback_polarity_count,
        "fallback_polarity_rate": _fallback_polarity_rate(run),
        "bin_count_global": run.bin_count_global,
        "bin_count_per_sector": _coerce_mapping_int(run.bin_count_per_sector),
        "summary_json": _coerce_summary(run.summary_json),
        "disclaimer": DISCLAIMER,
        "allow_recent": run.allow_recent,
        "disclaimer_version": run.disclaimer_version,
        "class_ids": list(run.class_ids),
        "sectors": list(run.sectors),
        "venues": list(run.venues),
        "error_summary": run.error_summary,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _iso_or_none(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _fallback_polarity_rate(run: BacktestRun) -> float | None:
    summary = run.summary_json or {}
    if isinstance(summary, dict):
        raw = summary.get("fallback_polarity_rate")
        if isinstance(raw, (int, float)):
            return float(raw)
    if run.predictions_scored <= 0:
        return None
    return run.fallback_polarity_count / run.predictions_scored


def _coerce_mapping_int(mapping: Mapping[str, int]) -> dict[str, int]:
    """Return a plain dict so :func:`json.dumps` accepts it without coercion."""

    return {str(k): int(v) for k, v in mapping.items()}


def _coerce_summary(summary: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Coerce the persisted summary into JSON-serialisable shape.

    The persisted shape is already a plain dict produced by
    :meth:`ScoreSummary.as_mapping`; we coerce any :class:`Mapping`
    descendant to ``dict`` and pass through scalar values so
    :func:`json.dumps` does not choke on a custom mapping type. The
    function recurses through nested mappings/lists so the full
    summary travels intact.
    """

    if summary is None:
        return None
    coerced: dict[str, Any] = {str(k): _coerce_value(v) for k, v in summary.items()}
    return coerced


def _coerce_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _coerce_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_value(v) for v in value]
    return value


__all__ = ["render_json"]
