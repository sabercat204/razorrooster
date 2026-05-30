"""Report-to-report diff engine (T-RG-COMPAT-COMPARE-001 v0.45.0).

Pure-read helper that takes two ``ReportRecord`` rows and produces
a structured diff: metadata changes, section presence/absence,
length deltas in the rendered terminal text, and a unified-diff
preview of the terminal renderings.

Used by the ``razor-rooster report compare <a> <b>`` CLI
subcommand to answer "what changed since last week?". Strictly
descriptive — the output reports observed differences, never
ranks or recommends.

The engine never modifies state. It can run on read-only stores.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from datetime import timedelta

from razor_rooster.report_generator.models import ReportRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReportDiff:
    """Result of ``compare_reports``."""

    report_id_a: str
    report_id_b: str
    generated_at_a: object
    generated_at_b: object
    time_between: timedelta
    sections_added: tuple[str, ...]
    sections_removed: tuple[str, ...]
    sections_failed_diff: tuple[str, ...]
    library_version_a: int
    library_version_b: int
    library_version_changed: bool
    disclaimer_hash_a: str
    disclaimer_hash_b: str
    disclaimer_changed: bool
    terminal_length_a: int
    terminal_length_b: int
    terminal_length_delta: int
    unified_terminal_diff: str


def compare_reports(record_a: ReportRecord, record_b: ReportRecord) -> ReportDiff:
    """Diff two report records.

    The convention is ``a`` = older, ``b`` = newer. The function
    doesn't enforce that ordering — passing them in either order
    returns a valid diff with the time delta absolute-valued and
    the section/length deltas oriented as ``b - a``.

    The unified-terminal-diff field is bounded to a few hundred
    lines to keep CLI output readable; the caller can render the
    full thing by reading ``rendered_terminal_text`` from each
    record directly.
    """
    sections_a = set(record_a.sections_rendered)
    sections_b = set(record_b.sections_rendered)
    sections_added = tuple(sorted(sections_b - sections_a))
    sections_removed = tuple(sorted(sections_a - sections_b))

    failed_a = {
        str(f.get("section"))
        for f in record_a.sections_failed
        if isinstance(f, dict) and f.get("section") is not None
    }
    failed_b = {
        str(f.get("section"))
        for f in record_b.sections_failed
        if isinstance(f, dict) and f.get("section") is not None
    }
    sections_failed_diff: list[str] = []
    for sec in sorted(failed_b - failed_a):
        sections_failed_diff.append(f"+{sec}")
    for sec in sorted(failed_a - failed_b):
        sections_failed_diff.append(f"-{sec}")

    if record_a.generated_at and record_b.generated_at:
        delta = abs(record_b.generated_at - record_a.generated_at)
    else:
        delta = timedelta(0)

    text_a = record_a.rendered_terminal_text or ""
    text_b = record_b.rendered_terminal_text or ""

    unified = "\n".join(
        difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            fromfile=f"report:{record_a.report_id}",
            tofile=f"report:{record_b.report_id}",
            n=2,
            lineterm="",
        )
    )

    return ReportDiff(
        report_id_a=record_a.report_id,
        report_id_b=record_b.report_id,
        generated_at_a=record_a.generated_at,
        generated_at_b=record_b.generated_at,
        time_between=delta,
        sections_added=sections_added,
        sections_removed=sections_removed,
        sections_failed_diff=tuple(sections_failed_diff),
        library_version_a=record_a.library_version,
        library_version_b=record_b.library_version,
        library_version_changed=(record_a.library_version != record_b.library_version),
        disclaimer_hash_a=record_a.disclaimer_version_hash,
        disclaimer_hash_b=record_b.disclaimer_version_hash,
        disclaimer_changed=(record_a.disclaimer_version_hash != record_b.disclaimer_version_hash),
        terminal_length_a=len(text_a),
        terminal_length_b=len(text_b),
        terminal_length_delta=len(text_b) - len(text_a),
        unified_terminal_diff=unified,
    )


__all__ = ["ReportDiff", "compare_reports"]
