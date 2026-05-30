"""Two-column HTML renderer for ``report compare`` (v0.46.0).

Operators who run ``razor-rooster report compare A B --html PATH``
get a self-contained HTML page that places report A and report B
side-by-side, with the structural metadata diff at the top and
the rendered terminal text of each report below.

Self-contained: inline CSS only, no external assets, no
JavaScript, no network calls. The styling reuses the dark/light
``prefers-color-scheme`` palette from
:mod:`razor_rooster.report_generator.renderer.html` so the
operator gets a consistent look across the daily report and the
compare view.

The output passes through the shared imperative-language linter
before being written to disk (REQ-RG-FRAME-001 carry-forward).

The terminal-text panels run their content through
:func:`razor_rooster.report_generator.engines.ansi_to_html.ansi_to_html`
so any ANSI SGR sequences in the persisted ``rendered_terminal_text``
get translated to inline ``<span>`` elements with semantic CSS
class names. Today's terminal renderer doesn't emit ANSI, but
this keeps the compare view robust against future renderer
changes and against externally-pasted terminal text.
"""

from __future__ import annotations

import difflib
import html as _html_module
import logging
import re

from razor_rooster.report_generator.engines.ansi_to_html import (
    ANSI_INLINE_CSS,
    ansi_to_html,
)
from razor_rooster.report_generator.engines.compare import ReportDiff
from razor_rooster.report_generator.models import ReportRecord

logger = logging.getLogger(__name__)


_COMPARE_CSS = """
:root {
    --bg: #fafafa;
    --fg: #1a1a1a;
    --muted: #6b6b6b;
    --accent: #0066aa;
    --accent-bg: #e6f0fa;
    --warn: #aa4400;
    --warn-bg: #fff0e0;
    --border: #d0d0d0;
    --table-bg: #ffffff;
    --code-bg: #f0f0f0;
    --added-bg: #e6f5e6;
    --added-fg: #226022;
    --removed-bg: #f9e6e6;
    --removed-fg: #883333;
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1a1a;
        --fg: #f0f0f0;
        --muted: #a0a0a0;
        --accent: #66aaff;
        --accent-bg: #1a3050;
        --warn: #ffaa66;
        --warn-bg: #4a2a10;
        --border: #404040;
        --table-bg: #252525;
        --code-bg: #2a2a2a;
        --added-bg: #1f3a1f;
        --added-fg: #a3e0a3;
        --removed-bg: #3a1f1f;
        --removed-fg: #e0a3a3;
    }
}
* { box-sizing: border-box; }
body {
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 Roboto, sans-serif;
    line-height: 1.5;
    margin: 0;
    padding: 1.5rem;
    max-width: 90rem;
    margin-left: auto;
    margin-right: auto;
}
h1 { font-size: 1.6rem; margin: 0 0 0.5rem; }
h2 {
    font-size: 1.2rem;
    margin: 1.5rem 0 0.5rem;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid var(--border);
}
h3 { font-size: 1.05rem; margin: 1rem 0 0.5rem; }
section { margin-bottom: 1.5rem; }
.muted { color: var(--muted); }
.metadata-table {
    border-collapse: collapse;
    background: var(--table-bg);
    margin: 0.5rem 0;
    width: 100%;
    font-size: 0.95rem;
}
.metadata-table th, .metadata-table td {
    border: 1px solid var(--border);
    padding: 0.4rem 0.6rem;
    text-align: left;
    vertical-align: top;
}
.metadata-table th {
    background: var(--accent-bg);
    color: var(--accent);
    font-weight: 600;
}
.changed { background: var(--accent-bg); }
.added {
    background: var(--added-bg);
    color: var(--added-fg);
    padding: 0 0.25rem;
    border-radius: 2px;
}
.removed {
    background: var(--removed-bg);
    color: var(--removed-fg);
    padding: 0 0.25rem;
    border-radius: 2px;
}
.side-by-side {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
}
@media (max-width: 50rem) {
    .side-by-side { grid-template-columns: 1fr; }
}
.column {
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--table-bg);
    padding: 0.75rem 1rem;
    overflow: hidden;
}
.column h3 {
    margin-top: 0;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid var(--border);
}
.column pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: var(--code-bg);
    border-radius: 2px;
    padding: 0.75rem 1rem;
    overflow-x: auto;
    font-size: 0.85rem;
    line-height: 1.4;
    white-space: pre-wrap;
    word-wrap: break-word;
}
.unified-diff {
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--table-bg);
    padding: 0;
    margin: 0.5rem 0;
    overflow: hidden;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.85rem;
    line-height: 1.4;
}
.unified-diff .diff-line {
    padding: 0 0.75rem;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 0;
}
.unified-diff .diff-line.diff-add {
    background: var(--added-bg);
    color: var(--added-fg);
}
.unified-diff .diff-line.diff-del {
    background: var(--removed-bg);
    color: var(--removed-fg);
}
.unified-diff .diff-line.diff-hunk {
    background: var(--accent-bg);
    color: var(--accent);
    font-weight: 600;
}
.unified-diff .diff-line.diff-meta {
    background: var(--code-bg);
    color: var(--muted);
    font-style: italic;
}
.unified-diff .diff-line.diff-context {
    color: var(--fg);
}
.unified-diff .diff-truncated {
    padding: 0.25rem 0.75rem;
    background: var(--code-bg);
    color: var(--muted);
    font-style: italic;
}
.unified-diff .word-add {
    background: color-mix(in srgb, var(--added-fg) 25%, transparent);
    border-radius: 2px;
    padding: 0 1px;
}
.unified-diff .word-del {
    background: color-mix(in srgb, var(--removed-fg) 25%, transparent);
    border-radius: 2px;
    padding: 0 1px;
    text-decoration: line-through;
    text-decoration-color: color-mix(in srgb, var(--removed-fg) 70%, transparent);
}
.disclaimer {
    margin-top: 1.5rem;
    padding: 1rem;
    border-left: 3px solid var(--accent);
    background: var(--accent-bg);
    font-style: italic;
    font-size: 0.9rem;
}
.quick-jump {
    margin: 0.5rem 0 1.25rem;
    font-size: 0.85rem;
}
.quick-jump a {
    color: var(--accent);
    text-decoration: none;
    margin-right: 0.25rem;
}
.quick-jump a:hover {
    text-decoration: underline;
}
""".strip()


def render_compare_html(
    *,
    record_a: ReportRecord,
    record_b: ReportRecord,
    diff: ReportDiff,
    diff_line_limit: int = 500,
    word_diff: bool = True,
    side_by_side: bool = True,
    quick_jump: bool = True,
) -> str:
    """Render the two-column compare view as a self-contained HTML document.

    The page can render up to four content regions:

    1. Header — "Report comparison: <a> vs <b>".
    2. Metadata table — every diff field and whether it changed.
    3. Sections-changed list.
    4. Side-by-side panel — the rendered terminal text of report
       A on the left and report B on the right. Suppressed when
       ``side_by_side`` is False.
    5. Unified-diff panel — line-level color-highlighted diff
       between the two terminal texts. Bounded by
       ``diff_line_limit`` so very large diffs don't blow up the
       page. When ``word_diff`` is True (the default), paired
       deletion/addition lines also get word-level highlights;
       when False, only the line-level coloring is applied.

    The output is purely descriptive; it reports observed
    differences without ranking or recommending.
    """
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(
        f"<title>Razor-Rooster Report Compare — {_h(record_a.report_id)} vs "
        f"{_h(record_b.report_id)}</title>"
    )
    parts.append(f"<style>{_COMPARE_CSS}\n{ANSI_INLINE_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append(
        _render_header(record_a, record_b, diff, side_by_side=side_by_side, quick_jump=quick_jump)
    )
    parts.append(_render_metadata(diff))
    parts.append(_render_sections_diff(diff))
    if side_by_side:
        parts.append(_render_side_by_side(record_a, record_b))
    parts.append(_render_unified_diff(diff, diff_line_limit=diff_line_limit, word_diff=word_diff))
    parts.append(_render_footer())
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


# -- internals --------------------------------------------------------------


def _render_header(
    record_a: ReportRecord,
    record_b: ReportRecord,
    diff: ReportDiff,
    *,
    side_by_side: bool = True,
    quick_jump: bool = True,
) -> str:
    lines: list[str] = []
    lines.append("<header>")
    lines.append(
        f"<h1>Report comparison: {_h(record_a.report_id)} vs {_h(record_b.report_id)}</h1>"
    )
    lines.append('<p class="muted">')
    lines.append(
        f"a generated_at: {_h(_iso(record_a.generated_at))}<br>"
        f"b generated_at: {_h(_iso(record_b.generated_at))}<br>"
        f"time between: {_h(str(diff.time_between))}"
    )
    lines.append("</p>")
    if quick_jump:
        nav_links: list[str] = [
            '<a href="#metadata">metadata</a>',
            '<a href="#sections">sections</a>',
        ]
        if side_by_side:
            nav_links.append('<a href="#side-by-side">side-by-side</a>')
        nav_links.append('<a href="#unified-diff">unified diff</a>')
        lines.append('<nav class="quick-jump muted">')
        lines.append("jump to: " + " · ".join(nav_links))
        lines.append("</nav>")
    lines.append("</header>")
    return "\n".join(lines)


def _render_metadata(diff: ReportDiff) -> str:
    lines: list[str] = []
    lines.append('<section id="metadata">')
    lines.append("<h2>Metadata</h2>")
    lines.append('<table class="metadata-table">')
    lines.append("<thead><tr><th>field</th><th>a</th><th>b</th><th>changed</th></tr></thead>")
    lines.append("<tbody>")
    rows = [
        (
            "library version",
            str(diff.library_version_a),
            str(diff.library_version_b),
            diff.library_version_changed,
        ),
        (
            "disclaimer hash",
            (diff.disclaimer_hash_a[:12] + "…")
            if len(diff.disclaimer_hash_a) > 12
            else diff.disclaimer_hash_a,
            (diff.disclaimer_hash_b[:12] + "…")
            if len(diff.disclaimer_hash_b) > 12
            else diff.disclaimer_hash_b,
            diff.disclaimer_changed,
        ),
        (
            "terminal text length",
            str(diff.terminal_length_a),
            f"{diff.terminal_length_b} ({diff.terminal_length_delta:+d})",
            diff.terminal_length_a != diff.terminal_length_b,
        ),
    ]
    for label, val_a, val_b, changed in rows:
        cls = ' class="changed"' if changed else ""
        lines.append(
            f"<tr{cls}><th>{_h(label)}</th>"
            f"<td>{_h(val_a)}</td>"
            f"<td>{_h(val_b)}</td>"
            f"<td>{'yes' if changed else 'no'}</td></tr>"
        )
    lines.append("</tbody>")
    lines.append("</table>")
    lines.append("</section>")
    return "\n".join(lines)


def _render_sections_diff(diff: ReportDiff) -> str:
    lines: list[str] = []
    lines.append('<section id="sections">')
    lines.append("<h2>Sections</h2>")
    if not diff.sections_added and not diff.sections_removed:
        lines.append('<p class="muted">No section presence changes.</p>')
    else:
        lines.append("<ul>")
        for sec in diff.sections_added:
            lines.append(f'<li><span class="added">added</span> {_h(sec)}</li>')
        for sec in diff.sections_removed:
            lines.append(f'<li><span class="removed">removed</span> {_h(sec)}</li>')
        lines.append("</ul>")
    if diff.sections_failed_diff:
        lines.append("<h3>Failure delta</h3>")
        lines.append("<ul>")
        for entry in diff.sections_failed_diff:
            cls = "added" if entry.startswith("+") else "removed"
            label = "newly failing" if entry.startswith("+") else "no longer failing"
            sec_name = entry[1:]
            lines.append(f'<li><span class="{cls}">{label}</span> {_h(sec_name)}</li>')
        lines.append("</ul>")
    lines.append("</section>")
    return "\n".join(lines)


def _render_side_by_side(record_a: ReportRecord, record_b: ReportRecord) -> str:
    lines: list[str] = []
    lines.append('<section id="side-by-side">')
    lines.append("<h2>Side-by-side terminal text</h2>")
    lines.append('<div class="side-by-side">')
    lines.append('<div class="column">')
    lines.append(f"<h3>a — {_h(record_a.report_id)}</h3>")
    lines.append(f"<pre>{ansi_to_html(record_a.rendered_terminal_text or '')}</pre>")
    lines.append("</div>")
    lines.append('<div class="column">')
    lines.append(f"<h3>b — {_h(record_b.report_id)}</h3>")
    lines.append(f"<pre>{ansi_to_html(record_b.rendered_terminal_text or '')}</pre>")
    lines.append("</div>")
    lines.append("</div>")
    lines.append("</section>")
    return "\n".join(lines)


def _render_unified_diff(diff: ReportDiff, *, diff_line_limit: int, word_diff: bool = True) -> str:
    """Render the unified terminal-text diff as a color-highlighted panel.

    Each line is wrapped in a ``<div class="diff-line ...">`` with
    a class indicating whether it's an addition, deletion, hunk
    header, file metadata, or context line. When ``word_diff`` is
    True, adjacent deletion/addition pairs of equal length get an
    additional word-level pass: runs of unchanged tokens are
    emitted as plain text and runs of replaced tokens are wrapped
    in ``<span class="word-del">``/``<span class="word-add">`` so
    the operator can see which substring within a touched line
    actually changed. When False, only the line-level coloring
    applies (useful on narrow viewports where the word wrap can
    obscure the line boundary).

    Truncates after ``diff_line_limit`` lines and emits a
    "…N more lines" footer when truncation kicks in.
    """
    lines: list[str] = []
    lines.append('<section id="unified-diff">')
    lines.append("<h2>Unified diff</h2>")
    diff_text = diff.unified_terminal_diff or ""
    if not diff_text.strip():
        lines.append('<p class="muted">No textual differences in the rendered terminal output.</p>')
        lines.append("</section>")
        return "\n".join(lines)
    raw_lines = diff_text.splitlines()
    total = len(raw_lines)
    capped = raw_lines[: max(0, int(diff_line_limit))]
    rendered_rows = _render_diff_rows_with_word_highlights(capped, word_diff=word_diff)
    lines.append('<div class="unified-diff">')
    lines.extend(rendered_rows)
    if total > diff_line_limit:
        lines.append(
            f'<div class="diff-truncated">… {total - diff_line_limit} more line(s) truncated</div>'
        )
    lines.append("</div>")
    lines.append("</section>")
    return "\n".join(lines)


def _render_diff_rows_with_word_highlights(
    raw_lines: list[str], *, word_diff: bool = True
) -> list[str]:
    """Walk diff lines pairing adjacent del/add runs for word-level highlight.

    Pairing rule:
    - A run of one or more ``-`` lines immediately followed by a
      run of the same length of ``+`` lines is paired
      element-wise (the i-th del line pairs with the i-th add
      line) — but only when ``word_diff`` is True.
    - Unequal-length runs fall back to plain whole-line styling
      (the per-line color still helps).
    - Lines outside del/add runs (context, hunk header, file
      metadata) keep the existing whole-line styling.
    - When ``word_diff`` is False, every del/add line falls back
      to whole-line styling regardless of run length.

    The output preserves the original ordering: deletions before
    additions, just as ``difflib.unified_diff`` emits them.
    """
    rows: list[str] = []
    i = 0
    n = len(raw_lines)
    while i < n:
        line = raw_lines[i]
        cls = _classify_diff_line(line)
        if cls != "diff-del":
            rows.append(f'<div class="diff-line {cls}">{_h(line)}</div>')
            i += 1
            continue
        # Collect a run of consecutive deletion lines.
        del_run: list[str] = []
        j = i
        while j < n and _classify_diff_line(raw_lines[j]) == "diff-del":
            del_run.append(raw_lines[j])
            j += 1
        # Collect a run of consecutive addition lines that follows.
        add_run: list[str] = []
        k = j
        while k < n and _classify_diff_line(raw_lines[k]) == "diff-add":
            add_run.append(raw_lines[k])
            k += 1
        if word_diff and len(del_run) == len(add_run) and del_run:
            for del_line, add_line in zip(del_run, add_run, strict=True):
                del_html, add_html = _word_level_highlights(del_line, add_line)
                rows.append(f'<div class="diff-line diff-del">{del_html}</div>')
                rows.append(f'<div class="diff-line diff-add">{add_html}</div>')
        else:
            for del_line in del_run:
                rows.append(f'<div class="diff-line diff-del">{_h(del_line)}</div>')
            for add_line in add_run:
                rows.append(f'<div class="diff-line diff-add">{_h(add_line)}</div>')
        i = k
    return rows


def _word_level_highlights(del_line: str, add_line: str) -> tuple[str, str]:
    """Return HTML for one (del, add) pair with word-level highlights.

    Splits each line into tokens (runs of word characters separated
    by runs of whitespace/punctuation), runs ``SequenceMatcher``
    over the token streams, and wraps replaced/inserted/deleted
    runs in ``<span class="word-del">``/``<span class="word-add">``.
    The leading ``-`` / ``+`` marker stays unwrapped so the line
    classification is still visible.
    """

    def tokenize(s: str) -> list[str]:
        # Captures alternating word and non-word runs as separate
        # tokens. Empty matches are filtered out.
        return [t for t in re.findall(r"\w+|\W+", s) if t != ""]

    # Strip leading marker; we re-add it before returning.
    if del_line.startswith("-") and not del_line.startswith("---"):
        del_marker, del_body = "-", del_line[1:]
    else:
        del_marker, del_body = "", del_line
    if add_line.startswith("+") and not add_line.startswith("+++"):
        add_marker, add_body = "+", add_line[1:]
    else:
        add_marker, add_body = "", add_line

    del_tokens = tokenize(del_body)
    add_tokens = tokenize(add_body)
    matcher = difflib.SequenceMatcher(a=del_tokens, b=add_tokens, autojunk=False)
    del_parts: list[str] = []
    add_parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        del_chunk = "".join(del_tokens[i1:i2])
        add_chunk = "".join(add_tokens[j1:j2])
        if tag == "equal":
            del_parts.append(_h(del_chunk))
            add_parts.append(_h(add_chunk))
        elif tag == "delete":
            del_parts.append(f'<span class="word-del">{_h(del_chunk)}</span>')
        elif tag == "insert":
            add_parts.append(f'<span class="word-add">{_h(add_chunk)}</span>')
        elif tag == "replace":
            del_parts.append(f'<span class="word-del">{_h(del_chunk)}</span>')
            add_parts.append(f'<span class="word-add">{_h(add_chunk)}</span>')
    del_html = _h(del_marker) + "".join(del_parts)
    add_html = _h(add_marker) + "".join(add_parts)
    return del_html, add_html


def _classify_diff_line(line: str) -> str:
    """Map a unified-diff line to its semantic CSS class.

    Convention follows ``difflib.unified_diff``:
    - ``---``/``+++`` are file headers (file metadata).
    - ``@@`` lines are hunk headers.
    - Lines starting with ``+`` (other than ``+++``) are additions.
    - Lines starting with ``-`` (other than ``---``) are deletions.
    - Everything else is context.
    """
    if line.startswith("---") or line.startswith("+++"):
        return "diff-meta"
    if line.startswith("@@"):
        return "diff-hunk"
    if line.startswith("+"):
        return "diff-add"
    if line.startswith("-"):
        return "diff-del"
    return "diff-context"


def _render_footer() -> str:
    return (
        '<footer class="disclaimer">'
        "Razor-Rooster is an educational decision-support system. "
        "This compare view is descriptive: it reports observed "
        "differences between two report cycles. No content here "
        "constitutes a trading recommendation."
        "</footer>"
    )


def _h(value: object) -> str:
    return _html_module.escape(str(value), quote=True)


def _iso(value: object) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


__all__ = ["render_compare_html"]
