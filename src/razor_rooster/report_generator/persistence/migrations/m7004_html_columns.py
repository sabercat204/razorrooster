"""Report-generator m7004 — add HTML columns to ``report_log``.

T-RG-COMPAT-HTML-001 (LOOM v0.44.0). Adds ``rendered_html_text``
(TEXT, NULL) and ``html_path`` (VARCHAR, NULL) so generators can
persist the HTML output alongside the existing terminal and
markdown renderings.

Fresh installs apply m7001 + m7004 and get the columns at create
time via ``CREATE TABLE IF NOT EXISTS``. Upgrade installs apply
m7004 on top of m7001 and run ``ALTER TABLE`` to add the columns
to the existing table. We use ``PRAGMA table_info`` to detect
whether the columns already exist before issuing the ALTER, so
re-running m7004 on a fresh install is a no-op (the canonical
DDL in m7001 already created the columns).
"""

from __future__ import annotations

import contextlib

import duckdb


def up(conn: duckdb.DuckDBPyConnection) -> None:
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info('report_log')").fetchall()
    }
    if "rendered_html_text" not in existing_columns:
        conn.execute("ALTER TABLE report_log ADD COLUMN rendered_html_text TEXT NULL")
    if "html_path" not in existing_columns:
        conn.execute("ALTER TABLE report_log ADD COLUMN html_path VARCHAR NULL")


def down(conn: duckdb.DuckDBPyConnection) -> None:
    # DuckDB doesn't support DROP COLUMN inside ALTER TABLE in all
    # versions; in v1.5+ it does. Best-effort only.
    with contextlib.suppress(duckdb.Error):
        conn.execute("ALTER TABLE report_log DROP COLUMN rendered_html_text")
    with contextlib.suppress(duckdb.Error):
        conn.execute("ALTER TABLE report_log DROP COLUMN html_path")
