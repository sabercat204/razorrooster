"""Typed dataclasses for report_generator outputs (T-RG-010+)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SectionName = Literal[
    "system_health",
    "surfaced",
    "watched",
    "calibration",
    "watchlist",
]


@dataclass(frozen=True, slots=True)
class SectionContent:
    """One assembler's output before rendering.

    ``content`` is the structured dict the renderers consume.
    ``error`` is set when the assembler raised; renderers emit a
    'section unavailable' placeholder using ``error`` as the reason.
    """

    name: str
    content: Mapping[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class ReportRecord:
    """One row of ``report_log``."""

    report_id: str
    generated_at: datetime
    since_ts: datetime
    until_ts: datetime
    sections_enabled: Sequence[str]
    sections_rendered: Sequence[str]
    sections_failed: Sequence[Mapping[str, Any]]
    library_version: int
    disclaimer_version_hash: str
    rendered_terminal_text: str
    rendered_markdown_text: str | None = None
    markdown_path: str | None = None
    rendered_html_text: str | None = None
    html_path: str | None = None
    duration_seconds: float | None = None


@dataclass(slots=True)
class ReportResult:
    """In-memory result of one ``generate`` call."""

    report_id: str
    generated_at: datetime
    since_ts: datetime
    until_ts: datetime
    sections_enabled: tuple[str, ...]
    sections_rendered: tuple[str, ...]
    sections_failed: tuple[dict[str, Any], ...]
    rendered_terminal_text: str
    rendered_markdown_text: str | None = None
    markdown_path: str | None = None
    rendered_html_text: str | None = None
    html_path: str | None = None
    library_version: int = 0
    disclaimer_version_hash: str = ""
    duration_seconds: float | None = None
    section_contents: tuple[SectionContent, ...] = field(default_factory=tuple)
