"""Inline CSS for the GUI.

Stored as a Python constant rather than a static-files mount so the
no-external-assets guarantee is preserved at the framework level —
there is no ``app.mount("/static", ...)`` and no chance of an
operator inadvertently exposing a directory.

The palette mirrors the report HTML renderer's
``prefers-color-scheme`` palette so the GUI feels consistent with
the rest of the operator surface.
"""

INLINE_CSS = """
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
    padding: 0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
}
header.topbar {
    background: var(--accent-bg);
    color: var(--accent);
    padding: 0.6rem 1.5rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 1.5rem;
    flex-wrap: wrap;
}
header.topbar h1 {
    font-size: 1rem;
    margin: 0;
    font-weight: 600;
}
header.topbar nav { display: flex; gap: 1rem; flex-wrap: wrap; }
header.topbar nav a {
    color: var(--accent);
    text-decoration: none;
    padding: 0.25rem 0.5rem;
    border-radius: 3px;
}
header.topbar nav a:hover { background: rgba(0, 0, 0, 0.05); }
header.topbar nav a.active {
    background: var(--accent);
    color: var(--bg);
    font-weight: 600;
}
header.topbar .meta {
    margin-left: auto;
    font-size: 0.85rem;
    color: var(--muted);
}
main {
    flex: 1;
    padding: 1.5rem;
    max-width: 80rem;
    margin: 0 auto;
    width: 100%;
}
h2 {
    font-size: 1.3rem;
    margin: 0 0 1rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid var(--border);
}
h3 { font-size: 1.05rem; margin: 1rem 0 0.5rem; }
p { margin: 0.5rem 0; }
.muted { color: var(--muted); font-size: 0.9rem; }
.empty {
    color: var(--muted);
    font-style: italic;
    padding: 1rem;
    border: 1px dashed var(--border);
    border-radius: 4px;
    text-align: center;
}
.summary-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.5rem;
}
.summary-card {
    background: var(--table-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem 1rem;
}
.summary-card .label {
    font-size: 0.8rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.summary-card .value {
    font-size: 1.5rem;
    font-weight: 600;
    margin-top: 0.25rem;
}
.summary-card.warn .value { color: var(--warn); }
table {
    width: 100%;
    border-collapse: collapse;
    background: var(--table-bg);
    font-size: 0.95rem;
    margin: 0.5rem 0 1.5rem;
}
th, td {
    border: 1px solid var(--border);
    padding: 0.4rem 0.6rem;
    text-align: left;
    vertical-align: top;
}
th {
    background: var(--accent-bg);
    color: var(--accent);
    font-weight: 600;
}
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td a { color: var(--accent); text-decoration: none; }
td a:hover { text-decoration: underline; }
.row-link { display: block; padding: 0.25rem 0; }
form.filter-form {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    align-items: end;
    background: var(--table-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem 1rem;
    margin-bottom: 1rem;
}
form.filter-form label {
    display: flex;
    flex-direction: column;
    font-size: 0.85rem;
    color: var(--muted);
    gap: 0.25rem;
}
form.filter-form input,
form.filter-form select {
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 0.35rem 0.5rem;
    font-family: inherit;
    font-size: 0.95rem;
}
form.filter-form button {
    background: var(--accent);
    color: var(--bg);
    border: none;
    border-radius: 3px;
    padding: 0.4rem 0.9rem;
    font-weight: 600;
    cursor: pointer;
}
form.filter-form button:hover { filter: brightness(1.1); }
.pill {
    display: inline-block;
    padding: 0.05rem 0.5rem;
    border-radius: 999px;
    font-size: 0.8rem;
    background: var(--code-bg);
    color: var(--muted);
}
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.added { background: var(--added-bg); color: var(--added-fg); }
.pill.removed { background: var(--removed-bg); color: var(--removed-fg); }
pre {
    background: var(--code-bg);
    border-radius: 3px;
    padding: 0.75rem 1rem;
    overflow-x: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.85rem;
    line-height: 1.4;
    white-space: pre-wrap;
    word-wrap: break-word;
}
iframe.report-frame {
    width: 100%;
    min-height: 80vh;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--table-bg);
}
footer.disclaimer {
    margin: 2rem auto 1.5rem;
    padding: 0.75rem 1rem;
    border-left: 3px solid var(--accent);
    background: var(--accent-bg);
    font-style: italic;
    font-size: 0.85rem;
    max-width: 80rem;
    width: 100%;
}
""".strip()


__all__ = ["INLINE_CSS"]
