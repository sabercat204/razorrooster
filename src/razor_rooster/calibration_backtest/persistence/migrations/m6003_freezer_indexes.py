"""Calibration-backtest m6003 — freezer-supporting indexes (T-CB-014).

Adds covering indexes on the four canonical ``data_ingest`` tables so the
freezer's hot-path query::

    SELECT ...
    FROM <canonical_table>
    WHERE source_id IN (:registered_sources)
      AND source_publication_ts <= :prediction_ts
      AND superseded_at IS NULL

stays within the design §3.5 / §6 latency budget on multi-million-row
corpora. The leading column is ``source_publication_ts DESC`` so the
``<=`` predicate can be answered via a backwards range scan without
touching rows newer than ``prediction_ts``; ``source_id`` is the trailing
column so the ``IN (...)`` filter is satisfied without a second seek.

Tables indexed (mirror :data:`razor_rooster.calibration_backtest.engines.freezer.CANONICAL_TABLES`):

* ``time_series`` → ``idx_time_series_source_publication_ts``
* ``event_stream`` → ``idx_event_stream_source_publication_ts``
* ``document_docket`` → ``idx_document_docket_source_publication_ts``
* ``geospatial_indicator`` → ``idx_geospatial_indicator_source_publication_ts``

The migration is **forward-tolerant**: each ``CREATE INDEX IF NOT EXISTS``
is wrapped so a missing canonical table (e.g. the database has not yet
applied data_ingest's m1001) is treated as a no-op rather than a hard
failure. This preserves the calibration_backtest invariant that its
migrations are runnable on any database that has at least applied m6001;
the freezer itself surfaces the missing-column / missing-table case at
runtime by returning ``None`` and logging ``source_data_not_frozen``
(see ``engines/freezer.py``).

Down-migration drops the four indexes in reverse-creation order. The
underlying tables are owned by ``data_ingest`` so we do not touch them.
"""

from __future__ import annotations

import contextlib
from typing import Final

import duckdb

from razor_rooster.calibration_backtest.persistence.schemas import VERSION_6003

MIGRATION_ID: Final[int] = VERSION_6003
"""Version recorded in ``schema_migrations`` when ``up()`` succeeds."""

DESCRIPTION: Final[str] = (
    "Add (source_publication_ts DESC, source_id) indexes on the four canonical "
    "data_ingest tables to support the calibration_backtest freezer hot-path query"
)
"""Human-readable summary surfaced via ``schema_migrations.description``."""


# Tuple of ``(table_name, index_name)`` pairs in apply order. Kept as a
# module-level constant so tests and operational tooling can introspect the
# index set without re-parsing DDL strings.
INDEX_SPECS: Final[tuple[tuple[str, str], ...]] = (
    ("time_series", "idx_time_series_source_publication_ts"),
    ("event_stream", "idx_event_stream_source_publication_ts"),
    ("document_docket", "idx_document_docket_source_publication_ts"),
    ("geospatial_indicator", "idx_geospatial_indicator_source_publication_ts"),
)


def _create_index_ddl(table: str, index_name: str) -> str:
    """Build the ``CREATE INDEX IF NOT EXISTS`` statement for one canonical table.

    Column order is correctness-critical: ``source_publication_ts`` leads so
    the ``<= prediction_ts`` range predicate drives the scan; ``source_id``
    trails so the ``IN (...)`` predicate filters within each timestamp
    bucket. ``DESC`` on the timestamp matches the freezer's "show me the
    most recent admissible row" access pattern (used by signal_scanner's
    precursor evaluator at ``as_of_ts=prediction_ts``).
    """
    return (
        f"CREATE INDEX IF NOT EXISTS {index_name} "
        f"ON {table} (source_publication_ts DESC, source_id)"
    )


def _drop_index_ddl(index_name: str) -> str:
    """Build the rollback DDL for one freezer index."""
    return f"DROP INDEX IF EXISTS {index_name}"


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the freezer-supporting indexes.

    Wraps each ``CREATE INDEX`` in :func:`contextlib.suppress` against
    :class:`duckdb.CatalogException` so a database that has not yet
    applied the data_ingest canonical-table migrations is left in a clean
    state — the index will be created the next time this migration runs
    against a fully-populated schema. ``IF NOT EXISTS`` makes the
    re-application path a clean no-op.
    """
    for table, index_name in INDEX_SPECS:
        with contextlib.suppress(duckdb.CatalogException):
            conn.execute(_create_index_ddl(table, index_name))


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop the freezer-supporting indexes.

    Indexes are dropped in reverse creation order for symmetry with
    :func:`up`. ``IF EXISTS`` makes the rollback forgiving when only a
    partial set of indexes was created (e.g. the up-migration ran against
    a database that lacked some canonical tables). ``contextlib.suppress``
    mirrors the project's other rollback idiom (see ``m6002``).
    """
    for _, index_name in reversed(INDEX_SPECS):
        with contextlib.suppress(duckdb.Error):
            conn.execute(_drop_index_ddl(index_name))


__all__ = [
    "DESCRIPTION",
    "INDEX_SPECS",
    "MIGRATION_ID",
    "down",
    "up",
]
