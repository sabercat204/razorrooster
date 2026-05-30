"""Footer assembler (T-RG-026; design §3.5).

Returns a content dict shaped like::

    {
      "type": "footer",
      "disclaimer_text": "...",
      "system_version": "...",
      "report_id": "...",
      "completed_at": datetime(...),
    }
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "disclaimer.txt"


def load_disclaimer_text(path: Path | None = None) -> str:
    """Load disclaimer text from the template file.

    Falls back to the verbatim text from REQ-RG-SEC-007 if the
    template is missing.
    """
    target = path or DEFAULT_TEMPLATE_PATH
    if target.exists():
        with target.open("r", encoding="utf-8") as handle:
            return handle.read().strip()
    return _FALLBACK_DISCLAIMER


def assemble(
    *,
    report_id: str,
    system_version: str,
    completed_at: datetime,
    disclaimer_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "type": "footer",
        "disclaimer_text": load_disclaimer_text(disclaimer_path),
        "system_version": system_version,
        "report_id": report_id,
        "completed_at": completed_at,
    }


_FALLBACK_DISCLAIMER = (
    "This report is decision-support analysis. The system surfaces "
    "patterns, comparisons, and analyses; it does not place trades, "
    "recommend specific actions, or claim certainty about future "
    "events. The operator is responsible for any decisions taken "
    "based on this report. Polymarket prices represent the aggregate "
    "view of market participants, who often have information the "
    "model does not. When model and market disagree, the market is "
    "correct more often than not."
)


__all__ = ["assemble", "load_disclaimer_text"]
