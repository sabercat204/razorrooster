"""ANSI SGR-to-HTML translator (v0.46.0 follow-on).

The terminal renderer in ``report_generator.renderer.terminal``
currently emits plain text without ANSI escape sequences, so the
existing ``compare --html`` side-by-side panel passes
``rendered_terminal_text`` straight into a ``<pre>`` block. That
works today.

The future (and the third-party renderer case) is more
defensive: an operator might paste in terminal text that includes
ANSI SGR (Select Graphic Rendition) sequences, or a future
renderer change might start emitting them. Without translation
those sequences appear as literal ``ESC[31m`` glyphs in the HTML
output and break alignment.

This module provides:

- :func:`strip_ansi` — remove every ANSI CSI/SGR sequence and
  return plain text (the safe fallback for renderers that just
  want the text content).
- :func:`ansi_to_html` — translate the most common SGR sequences
  (foreground colors, bold, dim, italic, underline) into inline
  ``<span>`` elements with semantic CSS class names. The output
  is HTML-escaped first; class names are fixed strings, so no
  user-controlled CSS can be injected.

Both functions are pure. The class-name palette is intentionally
small — eight foreground colors plus four typographical states.
The compare-HTML CSS block declares matching styles.

The shared imperative-language linter still runs over the final
compare-HTML payload, so any inappropriate phrasing inside an
ANSI-styled span gets caught by the existing check.
"""

from __future__ import annotations

import html as _html_module
import logging
import re
from collections.abc import Iterator

logger = logging.getLogger(__name__)


# Matches ANSI CSI sequences: ESC [ <params> <final-byte>.
# The final byte is in the range 0x40-0x7e; SGR uses "m".
_ANSI_CSI_RE = re.compile(
    r"\x1b\[(?P<params>[\x30-\x3f]*)(?P<intermediates>[\x20-\x2f]*)(?P<final>[\x40-\x7e])"
)


# SGR foreground color codes (30-37 standard, 90-97 bright).
_FG_CLASSES: dict[int, str] = {
    30: "ansi-fg-black",
    31: "ansi-fg-red",
    32: "ansi-fg-green",
    33: "ansi-fg-yellow",
    34: "ansi-fg-blue",
    35: "ansi-fg-magenta",
    36: "ansi-fg-cyan",
    37: "ansi-fg-white",
    90: "ansi-fg-bright-black",
    91: "ansi-fg-bright-red",
    92: "ansi-fg-bright-green",
    93: "ansi-fg-bright-yellow",
    94: "ansi-fg-bright-blue",
    95: "ansi-fg-bright-magenta",
    96: "ansi-fg-bright-cyan",
    97: "ansi-fg-bright-white",
}

# Typographical attribute SGR codes.
_ATTR_CLASSES: dict[int, str] = {
    1: "ansi-bold",
    2: "ansi-dim",
    3: "ansi-italic",
    4: "ansi-underline",
}

# Reset codes that close out a span.
_RESET_FG_CODES = {39}  # default foreground
_RESET_ALL_CODES = {0}  # full reset


# CSS used by :func:`ansi_to_html`. Compare-HTML can paste this
# block into its inline ``<style>`` so the spans render correctly.
ANSI_INLINE_CSS = """
.ansi-bold { font-weight: 600; }
.ansi-dim { opacity: 0.65; }
.ansi-italic { font-style: italic; }
.ansi-underline { text-decoration: underline; }
.ansi-fg-black { color: #1a1a1a; }
.ansi-fg-red { color: #c0392b; }
.ansi-fg-green { color: #27ae60; }
.ansi-fg-yellow { color: #b58900; }
.ansi-fg-blue { color: #2980b9; }
.ansi-fg-magenta { color: #8e44ad; }
.ansi-fg-cyan { color: #16a085; }
.ansi-fg-white { color: #888888; }
.ansi-fg-bright-black { color: #555555; }
.ansi-fg-bright-red { color: #e74c3c; }
.ansi-fg-bright-green { color: #2ecc71; }
.ansi-fg-bright-yellow { color: #f1c40f; }
.ansi-fg-bright-blue { color: #3498db; }
.ansi-fg-bright-magenta { color: #9b59b6; }
.ansi-fg-bright-cyan { color: #1abc9c; }
.ansi-fg-bright-white { color: #ecf0f1; }
""".strip()


def strip_ansi(text: str) -> str:
    """Remove every ANSI CSI sequence from ``text``.

    Safe for any input including non-SGR sequences (cursor moves,
    clear screen). The result is plain text suitable for embedding
    in a ``<pre>`` block after HTML-escaping.
    """
    return _ANSI_CSI_RE.sub("", text)


def ansi_to_html(text: str) -> str:
    """Translate ANSI SGR sequences to HTML ``<span>`` elements.

    Foreground colors and four typographical states (bold, dim,
    italic, underline) are translated to spans with semantic
    class names. Other SGR codes (background colors, 256-color
    sequences, RGB sequences) are silently dropped — they're rare
    in our domain and adding them would expand the attack surface
    for inline-style injection.

    Non-SGR CSI sequences (cursor moves, screen clears) are
    silently dropped.

    The text content is HTML-escaped before splicing in the spans.
    """
    out: list[str] = []
    open_classes: list[str] = []  # active span class names
    for kind, payload in _walk(text):
        if kind == "text":
            out.append(_html_module.escape(payload, quote=True))
            continue
        # ``payload`` is the parameter string of the SGR sequence.
        codes = _parse_codes(payload)
        for code in codes:
            new_class = _classify_code(code)
            if new_class == "__reset_all__":
                # Close every open span.
                while open_classes:
                    out.append("</span>")
                    open_classes.pop()
                continue
            if new_class == "__reset_fg__":
                # Close any open foreground-color span.
                _close_classes_matching(out, open_classes, prefix="ansi-fg-")
                continue
            if new_class is None:
                continue
            # If the same class is already open, leave it; otherwise open one.
            if new_class not in open_classes:
                out.append(f'<span class="{new_class}">')
                open_classes.append(new_class)
    while open_classes:
        out.append("</span>")
        open_classes.pop()
    return "".join(out)


# -- internals --------------------------------------------------------------


def _walk(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``("text", chunk)`` and ``("sgr", params)`` events.

    Non-SGR CSI sequences are dropped (yield nothing for them).
    """
    pos = 0
    for match in _ANSI_CSI_RE.finditer(text):
        if match.start() > pos:
            yield ("text", text[pos : match.start()])
        if match.group("final") == "m":
            yield ("sgr", match.group("params"))
        # else: non-SGR CSI; drop.
        pos = match.end()
    if pos < len(text):
        yield ("text", text[pos:])


def _parse_codes(params: str) -> list[int]:
    """Parse an SGR parameter string like ``"1;31"`` into ``[1, 31]``.

    An empty params string is treated as ``"0"`` (full reset)
    per the SGR spec.
    """
    if not params:
        return [0]
    out: list[int] = []
    for part in params.split(";"):
        if not part:
            # Treat empty between semicolons (e.g. ";31") as 0.
            out.append(0)
            continue
        try:
            out.append(int(part))
        except ValueError:
            # Skip malformed codes.
            continue
    return out


def _classify_code(code: int) -> str | None:
    """Map an SGR code to a CSS class name or a sentinel."""
    if code in _RESET_ALL_CODES:
        return "__reset_all__"
    if code in _RESET_FG_CODES:
        return "__reset_fg__"
    if code in _ATTR_CLASSES:
        return _ATTR_CLASSES[code]
    if code in _FG_CLASSES:
        return _FG_CLASSES[code]
    return None


def _close_classes_matching(out: list[str], open_classes: list[str], *, prefix: str) -> None:
    """Close every open span whose class starts with ``prefix``.

    Closes from the top of the stack so the HTML stays
    well-nested. If a non-matching class is on top, leaves it
    alone and stops (because closing it would unbalance nesting).
    """
    while open_classes and open_classes[-1].startswith(prefix):
        out.append("</span>")
        open_classes.pop()


__all__ = ["ANSI_INLINE_CSS", "ansi_to_html", "strip_ansi"]
