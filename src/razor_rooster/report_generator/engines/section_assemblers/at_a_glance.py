"""At-a-glance report section (T-RG-COMPAT-GLANCE-001 v0.45.0).

A small section at the top of the report that lifts the top item
from each major section's *already-ordered* list and presents
them as a few "key: value" facts. Designed as a navigation aid,
not as a synthesis layer.

Strict framing rules:

- The assembler does NOT independently rank, score, or
  interpret. It pulls the first element out of each section's
  ordered list (cross_venue items already sorted by
  spread_bps descending, surfaced comparisons already sorted
  by confidence_weighted_score descending, etc.) and reports
  the data point.
- Output is structured key/value pairs. No prose.
- Every emitted string passes through the shared
  imperative-language linter and an extended editorial-phrase
  blocklist (in tests). Phrases like "particularly notable",
  "worth attention", "key takeaway", "important", "noteworthy"
  must never appear.
- Opt-in via ``enabled_sections``. Default workspace config
  does not enable it because most cycles' top items are
  already visible at the top of their own sections; the
  at-a-glance section is for operators who want a one-screen
  navigation view at the very top of the report.

Returns a content dict shaped like::

    {
      "type": "at_a_glance",
      "facts": [
        {
          "label": "top cross-venue spread",
          "value": "<class_title> at 1500 bps (polymarket vs kalshi)",
          "section": "cross_venue",
        },
        ...
      ],
    }

When no upstream section has data, ``facts`` is empty and the
renderer prints the standard empty-section message.

This assembler is special-cased by the generator: it runs
*after* the other body sections so it can read their content.
The generator passes a ``body_contents_by_name`` mapping into
``assemble`` instead of a connection.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def assemble(
    body_contents_by_name: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Build the at-a-glance content dict.

    ``body_contents_by_name`` maps each already-run section's name
    to its content dict (or None when the section failed). The
    assembler reads but does not mutate.

    Pure function. No DB access. No SQL. The content always
    derives from data already rendered elsewhere in the report.
    """
    facts: list[dict[str, Any]] = []

    cross_venue = body_contents_by_name.get("cross_venue")
    if cross_venue:
        cv_fact = _top_cross_venue_fact(cross_venue)
        if cv_fact is not None:
            facts.append(cv_fact)

    surfaced = body_contents_by_name.get("surfaced")
    if surfaced:
        surfaced_fact = _top_surfaced_fact(surfaced)
        if surfaced_fact is not None:
            facts.append(surfaced_fact)

    watched = body_contents_by_name.get("watched")
    if watched:
        watched_fact = _top_watched_fact(watched)
        if watched_fact is not None:
            facts.append(watched_fact)

    calibration = body_contents_by_name.get("calibration")
    if calibration:
        cal_fact = _top_calibration_fact(calibration)
        if cal_fact is not None:
            facts.append(cal_fact)

    return {"type": "at_a_glance", "facts": facts}


# -- per-section extractors ------------------------------------------------


def _top_cross_venue_fact(content: Mapping[str, Any]) -> dict[str, Any] | None:
    """Top cross-venue disagreement = first item (assembler sorts by spread desc)."""
    items = content.get("items") if isinstance(content, Mapping) else None
    if not isinstance(items, list) or not items:
        return None
    top = items[0]
    if not isinstance(top, Mapping):
        return None
    spread_bps = top.get("spread_bps")
    if spread_bps is None:
        return None
    venue_prices = top.get("venue_prices") or []
    venues = sorted({str(vp.get("venue")) for vp in venue_prices if isinstance(vp, Mapping)})
    venues_str = " vs ".join(venues) if venues else ""
    title = top.get("class_title") or top.get("class_id") or "?"
    value = f"{title} at {int(spread_bps)} bps"
    if venues_str:
        value += f" ({venues_str})"
    return {
        "label": "top cross-venue spread",
        "value": value,
        "section": "cross_venue",
    }


def _top_surfaced_fact(content: Mapping[str, Any]) -> dict[str, Any] | None:
    """Top surfaced comparison = first item (assembler sorts by score desc)."""
    comparisons = content.get("comparisons") if isinstance(content, Mapping) else None
    if not isinstance(comparisons, list) or not comparisons:
        return None
    top = comparisons[0]
    if not isinstance(top, Mapping):
        return None
    title = top.get("class_title") or top.get("class_id") or "?"
    delta = top.get("delta")
    venue = top.get("venue") or "?"
    if delta is None:
        return {
            "label": "top surfaced comparison",
            "value": f"{title} ({venue})",
            "section": "surfaced",
        }
    sign = "+" if float(delta) >= 0 else ""
    return {
        "label": "top surfaced comparison",
        "value": f"{title} ({venue}) delta {sign}{float(delta):.3f}",
        "section": "surfaced",
    }


def _top_watched_fact(content: Mapping[str, Any]) -> dict[str, Any] | None:
    """Top watched follow-up = first item (assembler sorts by alert tier)."""
    follow_ups = content.get("follow_ups") if isinstance(content, Mapping) else None
    if not isinstance(follow_ups, list) or not follow_ups:
        return None
    top = follow_ups[0]
    if not isinstance(top, Mapping):
        return None
    title = top.get("class_title") or top.get("class_id") or "?"
    tier = top.get("primary_alert_tier") or "(none)"
    return {
        "label": "top watched alert",
        "value": f"{title} at tier {tier}",
        "section": "watched",
    }


def _top_calibration_fact(content: Mapping[str, Any]) -> dict[str, Any] | None:
    """Top calibration item = first miscalibrated sector if any, else first resolution."""
    if not isinstance(content, Mapping):
        return None
    sector_brier = content.get("sector_brier_scores") or []
    if isinstance(sector_brier, list) and sector_brier:
        miscal = next(
            (
                s
                for s in sector_brier
                if isinstance(s, Mapping) and bool(s.get("miscalibrated", False))
            ),
            None,
        )
        if miscal is not None:
            sector = miscal.get("sector", "?")
            brier = miscal.get("brier_score")
            value = f"{sector} brier {brier}"
            return {
                "label": "top miscalibrated sector",
                "value": value,
                "section": "calibration",
            }
    resolutions = content.get("resolutions") or []
    if isinstance(resolutions, list) and resolutions:
        top = resolutions[0]
        if isinstance(top, Mapping):
            title = top.get("class_title") or top.get("class_id") or "?"
            outcome = top.get("resolution_outcome") or "?"
            return {
                "label": "most-recent resolution",
                "value": f"{title} → {str(outcome).upper()}",
                "section": "calibration",
            }
    return None


__all__ = ["assemble"]
