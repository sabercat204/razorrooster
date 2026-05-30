"""Shared rendering helpers (T-RG-030; design §3.6)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

# Width target for the terminal renderer. The markdown renderer
# ignores width and lets the markdown viewer wrap.
TERMINAL_WIDTH = 80


def disclaimer_block(text: str) -> str:
    """Wrap the disclaimer text with a header line."""
    return f"DISCLAIMER:\n\n{text.strip()}"


def equal_prominence_blocks(
    *,
    model_label: str,
    model_bullets: Sequence[str],
    market_label: str,
    market_bullets: Sequence[str],
    bullet_prefix: str = "  - ",
) -> str:
    """Render two bullet blocks at equal prominence (REQ-RG-FRAME-002).

    Both blocks render with identical headers, identical bullet
    prefixes, and the shorter list is padded so the visible block
    length matches.
    """
    target_len = max(len(model_bullets), len(market_bullets), 1)
    model_padded = _pad(model_bullets, target_len)
    market_padded = _pad(market_bullets, target_len)
    lines: list[str] = []
    lines.append(model_label)
    for bullet in model_padded:
        lines.append(f"{bullet_prefix}{bullet}")
    lines.append("")
    lines.append(market_label)
    for bullet in market_padded:
        lines.append(f"{bullet_prefix}{bullet}")
    return "\n".join(lines)


def divider(char: str = "=", width: int = TERMINAL_WIDTH) -> str:
    return char * width


def section_divider() -> str:
    return divider("=")


def thin_divider() -> str:
    return divider("-")


def disclaimer_version_hash(text: str) -> str:
    """SHA-256 hash of the disclaimer text used in the report log.

    Used for retrospective review to verify the disclaimer hasn't
    drifted across reports.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def warnings_block(warnings: Sequence[str]) -> str:
    if not warnings:
        return ""
    lines = ["Warnings:"]
    for w in warnings:
        lines.append(f"  - {w}")
    return "\n".join(lines)


# -- internals --------------------------------------------------------------


def _pad(items: Sequence[str], target_len: int) -> list[str]:
    out = list(items)
    while len(out) < target_len:
        out.append("(no specific items identified)")
    return out


__all__ = [
    "TERMINAL_WIDTH",
    "disclaimer_block",
    "disclaimer_version_hash",
    "divider",
    "equal_prominence_blocks",
    "section_divider",
    "thin_divider",
    "warnings_block",
]
