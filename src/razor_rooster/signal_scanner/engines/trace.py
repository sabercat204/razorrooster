"""Reasoning trace builder and renderer (T-SCAN-021; design §3.6).

Two functions:

- :func:`build_trace` produces a JSON-serializable dict matching the
  schema in design §3.6.
- :func:`render_trace_text` produces a human-readable rendering for
  the report_generator and CLI ``scan show-trace``.

The trace is the operator-facing audit artifact: it records the
prior, every precursor's evaluation (current value, threshold,
direction, fire/no-fire, hit rate, false-positive rate, applied LR),
the optional co-occurrence correction, the posterior, the log-odds
shift, candidate flag/direction, warnings, and the CI propagation
method. All numeric fields are plain floats / ints so the JSON is
trivial to query downstream.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.event_class import EventClass
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.signal_scanner.engines.posterior import PosteriorResult


def build_trace(
    *,
    cls: EventClass,
    base_rate: BaseRateResult,
    signatures: Sequence[SignatureResult],
    current_values: Mapping[str, float | None],
    posterior: PosteriorResult,
    is_candidate: bool,
    candidate_direction: str | None,
    warnings: Sequence[str],
    no_update_applied: bool = False,
    no_update_reason: str | None = None,
    library_version: int,
    data_as_of: datetime,
) -> dict[str, Any]:
    """Assemble the trace JSON for one (scan, class) pair.

    The schema mirrors design §3.6 exactly. ``no_update_applied``
    short-circuits the precursor section: when set, the per-variable
    list is empty and the explanation is recorded under
    ``no_update_reason``.
    """
    precursors_payload: list[dict[str, Any]] = []
    if not no_update_applied:
        for sig, lr in zip(
            signatures, _zip_lrs(signatures, posterior.likelihood_ratios), strict=True
        ):
            current = current_values.get(sig.variable_id)
            fired = (
                _direction_fired(current, sig.threshold_value, sig.direction)
                if (current is not None and sig.threshold_value is not None)
                else False
            )
            precursors_payload.append(
                {
                    "variable_id": sig.variable_id,
                    "title": sig.variable_id,  # signatures don't carry title; class precursors do
                    "current_value": (float(current) if current is not None else None),
                    "threshold": sig.threshold_value,
                    "direction": sig.direction,
                    "fired": fired,
                    "hit_rate": sig.hit_rate,
                    "false_positive_rate": sig.false_positive_rate,
                    "likelihood_ratio_applied": lr,
                    "confidence_score": sig.confidence_score,
                    "low_confidence_warning": sig.low_confidence_warning,
                }
            )
        # Stitch in titles from the class definition where possible.
        precursor_titles = {p.variable_id: p.title for p in cls.precursors}
        for entry in precursors_payload:
            entry["title"] = precursor_titles.get(entry["variable_id"], entry["variable_id"])

    return {
        "class_id": cls.class_id,
        "class_definition_version": cls.definition_version,
        "library_version": library_version,
        "data_as_of": data_as_of.isoformat(),
        "prior": {
            "point": float(min(max(base_rate.rate_per_year, 0.0), 1.0)),
            "ci": [
                float(base_rate.credible_interval_lower),
                float(base_rate.credible_interval_upper),
            ],
        },
        "precursors": precursors_payload,
        "co_occurrence_correction": float(posterior.co_occurrence_correction),
        "posterior": {
            "point": float(posterior.posterior),
            "ci": [
                float(posterior.posterior_ci_lower),
                float(posterior.posterior_ci_upper),
            ],
        },
        "log_odds_shift": float(posterior.log_odds_shift),
        "is_candidate": bool(is_candidate),
        "candidate_direction": candidate_direction,
        "warnings": list(warnings),
        "no_update_applied": no_update_applied,
        "no_update_reason": no_update_reason,
        "ci_method": (
            f"monte_carlo_{posterior.n_samples}_samples"
            if posterior.n_samples > 0
            else "no_update_prior_passthrough"
        ),
    }


def render_trace_text(trace: Mapping[str, Any]) -> str:
    """Render a trace dict to human-readable text.

    The format is stable: ``report_generator`` consumes it as plain
    text. Lines are emitted verbatim from class-author content
    (``class_id``, precursor ids/titles); since the consumer of these
    fields is downstream subsystems and the operator's own terminal,
    no escaping is applied.
    """
    lines: list[str] = []
    lines.append(f"class:           {trace.get('class_id')}")
    lines.append(
        f"library_version: {trace.get('library_version')}  "
        f"definition_version: {trace.get('class_definition_version')}"
    )
    lines.append(f"data_as_of:      {trace.get('data_as_of')}")
    prior_payload = trace.get("prior") or {}
    posterior_payload = trace.get("posterior") or {}
    if prior_payload:
        prior_ci = prior_payload.get("ci") or [0.0, 0.0]
        lines.append(
            f"prior:           p={float(prior_payload.get('point', 0.0)):.4f} "
            f"CI=[{float(prior_ci[0]):.4f}, {float(prior_ci[1]):.4f}]"
        )
    if trace.get("no_update_applied"):
        lines.append(f"no_update:       {trace.get('no_update_reason') or '(unknown reason)'}")
    else:
        lines.append("precursors:")
        for entry in trace.get("precursors") or []:
            fired = "FIRED" if entry.get("fired") else "       "
            value_str = (
                f"{entry.get('current_value'):.3f}"
                if entry.get("current_value") is not None
                else "?"
            )
            threshold_str = (
                f"{entry.get('threshold'):.3f}" if entry.get("threshold") is not None else "?"
            )
            lr_value = entry.get("likelihood_ratio_applied")
            lr_str = f"{float(lr_value):.3f}" if lr_value is not None else "?"
            confidence_value = entry.get("confidence_score")
            conf_str = f"{float(confidence_value):.2f}" if confidence_value is not None else "?"
            lines.append(
                f"  {entry.get('variable_id'):<30} {fired} "
                f"value={value_str:>8} thr={threshold_str:>8} "
                f"hit={entry.get('hit_rate')} fpr={entry.get('false_positive_rate')} "
                f"LR={lr_str} conf={conf_str}"
            )
        if trace.get("co_occurrence_correction"):
            correction = float(trace.get("co_occurrence_correction") or 0.0)
            lines.append(f"co-occurrence correction: {correction:+.3f} (log-odds)")
    if posterior_payload:
        posterior_ci = posterior_payload.get("ci") or [0.0, 0.0]
        lines.append(
            f"posterior:       p={float(posterior_payload.get('point', 0.0)):.4f} "
            f"CI=[{float(posterior_ci[0]):.4f}, {float(posterior_ci[1]):.4f}]"
        )
    lines.append(f"log_odds_shift:  {float(trace.get('log_odds_shift') or 0.0):+.3f}")
    if trace.get("is_candidate"):
        lines.append(f"candidate:       YES ({trace.get('candidate_direction')})")
    else:
        lines.append("candidate:       no")
    if trace.get("warnings"):
        lines.append(f"warnings:        {', '.join(str(w) for w in trace['warnings'])}")
    lines.append(f"ci_method:       {trace.get('ci_method')}")
    return "\n".join(lines)


# -- internals --------------------------------------------------------------


def _direction_fired(value: float | None, threshold: float | None, direction: str) -> bool:
    if value is None or threshold is None:
        return False
    if direction == "high_signals_event":
        return value >= threshold
    if direction == "low_signals_event":
        return value <= threshold
    return False


def _zip_lrs(signatures: Sequence[SignatureResult], lrs: Sequence[float]) -> list[float | None]:
    """Pair signatures with their applied LRs; missing entries get None.

    The posterior engine skips signatures with missing rates or values,
    so the LR list is shorter than the signature list. We pad so the
    trace can show every declared precursor.
    """
    out: list[float | None] = []
    iterator = iter(lrs)
    for sig in signatures:
        if sig.hit_rate is None or sig.false_positive_rate is None:
            out.append(None)
            continue
        try:
            out.append(next(iterator))
        except StopIteration:
            out.append(None)
    return out
