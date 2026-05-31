"""``source_publication_ts`` freezer + lag enforcement (T-CB-014; design §3.5).

The freezer enforces REQ-CB-FREEZE-001 (time honesty): no precursor row whose
``source_publication_ts`` exceeds ``prediction_ts`` may inform a replayed
posterior. Boundary equality is admitted — a row with
``source_publication_ts == prediction_ts`` is treated as having been published
at, and therefore knowable at, the simulated decision instant (design §3.5,
"prediction timestamp derivation"). Rows ``+1ns`` past ``prediction_ts`` are
excluded.

Architecture (scout amendment 2026-05-31)
-----------------------------------------

``data_ingest`` does **not** expose per-source observation tables (the legacy
design referenced ``bls_jolts_observations``, ``bls_ces_observations``,
``bea_personal_income_observations``, ``fred_observations``,
``census_retail_observations``; that naming is wrong). Instead, ingested data
lives in **four canonical tables** discriminated by ``source_id``:

* :data:`razor_rooster.data_ingest.persistence.schemas.SchemaType.TIME_SERIES`
  → ``time_series``
* :data:`razor_rooster.data_ingest.persistence.schemas.SchemaType.EVENT_STREAM`
  → ``event_stream``
* :data:`razor_rooster.data_ingest.persistence.schemas.SchemaType.DOCUMENT_DOCKET`
  → ``document_docket``
* :data:`razor_rooster.data_ingest.persistence.schemas.SchemaType.GEOSPATIAL_INDICATOR`
  → ``geospatial_indicator``

Every canonical table inherits the provenance prefix
``(source_id, source_record_id, source_publication_ts, fetch_ts,
connector_version, superseded_at, source_payload_json)``, so the
``source_publication_ts <= prediction_ts AND superseded_at IS NULL`` filter is
applied uniformly across all four tables. Source identities are discovered
**dynamically** from the ``sources`` operational table — the freezer never
hard-codes source names, so registering a new connector is a pure ingest-side
change with no freezer edit required.

Connector deferral
------------------

Of the design's named precursor sources, only ``fred`` has a registered
connector at the time of writing. ``bls_jolts``, ``bls_ces``,
``bea_personal_income``, and ``census_retail`` are deferred until those
connectors land in ``data_ingest`` (see ``DATA_INGEST.md`` v0.1.0
DEFER list). The freezer is correct as-of-today for ``fred`` and any other
canonical-schema source that registers in the ``sources`` table; tests
exercise the freeze logic with ``fred`` plus mocked ``source_id`` rows
inserted directly into the canonical tables.

Public surface
--------------

* :class:`FrozenState` — frozen dataclass carrying the boundary timestamp,
  the ``frozen_flag`` (always ``True`` on a successfully frozen state; the
  flag exists for symmetry with future degraded-mode states), and the
  ``frozenset`` of registered ``source_id`` values that were captured at
  freeze time.
* :func:`freeze` — given an open DuckDB connection and a ``prediction_ts``,
  returns a :class:`FrozenState` describing the time-bounded view, or
  ``None`` if any registered source lacks ``source_publication_ts``
  (logging a structured ``source_data_not_frozen`` event).
* :func:`registered_source_ids` — discovery helper used by :func:`freeze`;
  exposed for tests and downstream replay diagnostics.
* :func:`source_publication_ts_present` — column-presence check used by
  :func:`freeze` to enforce REQ-CB-FREEZE-001's "all sources are frozen or
  none are" invariant.

The freezer is read-only: no rows are mutated in either canonical or
operational tables. The performance index added by migration ``m6003`` keeps
``freeze()`` under the design §3.5 / §6 latency budget on multi-million-row
corpora; see ``persistence/migrations/m6003_freezer_indexes.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import duckdb

# Public table names — sourced from data_ingest's canonical schema enum so
# any future rename propagates automatically. Importing the enum (rather than
# the literal strings) gives mypy a single source of truth.
from razor_rooster.data_ingest.persistence.schemas import SchemaType

__all__ = [
    "CANONICAL_TABLES",
    "FrozenState",
    "freeze",
    "registered_source_ids",
    "source_publication_ts_present",
]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

CANONICAL_TABLES: Final[tuple[str, ...]] = (
    SchemaType.TIME_SERIES.value,
    SchemaType.EVENT_STREAM.value,
    SchemaType.DOCUMENT_DOCKET.value,
    SchemaType.GEOSPATIAL_INDICATOR.value,
)
"""The four canonical data_ingest tables the freezer queries.

Order matches the design §4 enumeration and is preserved when iterating so
structured-log output is deterministic across runs (REQ-CB-RUN-003).
"""

_PROVENANCE_COLUMN: Final[str] = "source_publication_ts"
"""Column the freezer's WHERE clause filters on (data_ingest design §4)."""

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrozenState:
    """Snapshot of the time-bounded data_ingest view used to evaluate one prediction.

    Returned by :func:`freeze` when every registered source carries
    ``source_publication_ts`` on its canonical table. Holds the **boundary
    timestamp** (``prediction_ts`` echoed back so callers do not have to
    thread it independently), the ``frozen_flag`` (``True`` when the state
    was assembled successfully), and the discovered set of source ids that
    were frozen — exposed as a ``frozenset`` so callers cannot mutate the
    captured registry.

    The state object intentionally does *not* hold a database cursor:
    ``freeze()`` is a *contract assertion* that the freezer's SQL
    invariants will be honoured by downstream queries (e.g.
    ``signal_scanner.engines.posterior.evaluate_precursors_at_time``),
    not a long-lived read transaction. Storing a cursor would force every
    consumer to participate in lifetime management; keeping ``FrozenState``
    immutable lets it travel through the replay loop and be persisted in
    structured logs without cleanup hazards.
    """

    source_publication_ts_boundary: datetime
    """The ``prediction_ts`` value enforced by downstream queries."""

    frozen_flag: bool
    """``True`` when the state was assembled with all sources frozen.

    Always ``True`` on a returned state in v1; reserved for future
    degraded-mode flows where partial freezing is acceptable for
    diagnostic-only runs. ``freeze()`` returns ``None`` rather than a
    ``frozen_flag=False`` state today.
    """

    registered_sources: frozenset[str]
    """The discovered ``source_id`` set captured at freeze time."""


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def registered_source_ids(conn: duckdb.DuckDBPyConnection) -> frozenset[str]:
    """Return the set of registered ``source_id`` values from the ``sources`` table.

    The ``sources`` operational table (data_ingest design §4.5) is the
    authoritative registry of ingestible sources; every connector that
    persists rows into a canonical table also writes a row here. The
    freezer relies on this dynamic discovery so a newly-registered source
    is automatically swept into the next backtest with no calibration_backtest
    code change required.

    Returns an empty :class:`frozenset` when the ``sources`` table is empty
    or missing — :func:`freeze` treats both cases as "no registered sources"
    and short-circuits with a structured-log entry.
    """
    try:
        rows = conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()
    except duckdb.CatalogException:
        # sources table not present — schema not initialised yet. Return an
        # empty registry; freeze() will log and return None.
        return frozenset()
    return frozenset(str(row[0]) for row in rows)


def source_publication_ts_present(
    conn: duckdb.DuckDBPyConnection,
    table: str,
) -> bool:
    """Return ``True`` when ``table`` exposes ``source_publication_ts``.

    Canonical-schema tables inherit the provenance prefix unconditionally,
    so for ``time_series`` / ``event_stream`` / ``document_docket`` /
    ``geospatial_indicator`` this check is implicitly satisfied at install
    time. The check still runs at freeze-time so any hand-rolled or legacy
    table that gets registered (e.g. via a future custom-connector pathway)
    is detected before its rows leak into a posterior. A missing column
    returns ``False`` and forces :func:`freeze` to emit
    ``source_data_not_frozen`` and decline to assemble a state.

    The implementation uses ``PRAGMA table_info`` because DuckDB exposes a
    stable and inexpensive interface for column introspection across
    versions; ``information_schema.columns`` would also work but the
    ``PRAGMA`` form mirrors the project's other persistence introspection
    code (see ``test_migration_m6001._table_columns``).
    """
    try:
        rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    except duckdb.CatalogException:
        # Table missing entirely — column trivially absent.
        return False
    return any(str(row[1]) == _PROVENANCE_COLUMN for row in rows)


# ---------------------------------------------------------------------------
# freeze()
# ---------------------------------------------------------------------------


def freeze(
    conn: duckdb.DuckDBPyConnection,
    prediction_ts: datetime,
) -> FrozenState | None:
    """Assemble a :class:`FrozenState` for ``prediction_ts``, or ``None`` on failure.

    Steps (REQ-CB-FREEZE-001, design §3.5):

    1. Discover registered ``source_id`` values from the ``sources`` table.
    2. For every canonical table that any registered source feeds into,
       confirm the ``source_publication_ts`` column is present. Canonical
       tables always satisfy this; the check guards against future custom
       schemas.
    3. If any registered source's canonical-schema metadata indicates a
       missing ``source_publication_ts`` column, log
       ``source_data_not_frozen`` (structured) and return ``None`` — the
       caller skips the prediction with ``reason='source_data_not_frozen'``.
    4. Otherwise return :class:`FrozenState` carrying the boundary
       timestamp and the captured registry.

    Note on WHERE-clause enforcement: this function does not itself execute
    the per-table SELECTs that downstream signal_scanner / pattern_library
    queries run — it returns the *contract* (the ``FrozenState``) that those
    queries must honour. Concretely, every caller that selects rows from a
    canonical table must include the equivalent of::

        SELECT ...
        FROM <canonical_table>
        WHERE source_id IN (:registered_sources)
          AND source_publication_ts <= :prediction_ts
          AND superseded_at IS NULL

    The replay loop (T-CB-018, T-CB-019) wires these clauses through the
    public scanner entry point (T-CB-017
    ``evaluate_precursors_at_time(... as_of_ts=prediction_ts)``).

    Returns
    -------
    FrozenState | None
        A frozen state when every registered source's canonical-schema
        metadata exposes ``source_publication_ts``; ``None`` otherwise.

    Notes
    -----
    Boundary equality is admitted: rows with
    ``source_publication_ts == prediction_ts`` are inside the frozen
    window. The strict-inequality case (``> prediction_ts``) is rejected
    by the WHERE-clause contract above. See design §3.5 for the rationale
    (``prediction_ts`` is the *simulated decision instant*, not the
    timestamp of the most recent admissible data point).
    """
    sources = registered_source_ids(conn)
    if not sources:
        # No registered sources at all — treat as not-frozen so the caller
        # records a transparent skip rather than silently scoring with no
        # precursor evidence (REQ-CB-FREEZE-001 + design §3.13's
        # ``source_data_not_frozen`` skip reason).
        _LOGGER.info(
            "source_data_not_frozen",
            extra={
                "event": "source_data_not_frozen",
                "reason": "no_registered_sources",
                "prediction_ts": prediction_ts.isoformat(),
            },
        )
        return None

    missing_columns: list[str] = [
        table for table in CANONICAL_TABLES if not source_publication_ts_present(conn, table)
    ]
    if missing_columns:
        _LOGGER.info(
            "source_data_not_frozen",
            extra={
                "event": "source_data_not_frozen",
                "reason": "missing_source_publication_ts",
                "prediction_ts": prediction_ts.isoformat(),
                "tables_missing_column": tuple(missing_columns),
                "registered_sources": tuple(sorted(sources)),
            },
        )
        return None

    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=sources,
    )
