"""Provenance helpers for ``data_ingest`` (T-015).

Connectors and downstream subsystems use these helpers rather than writing
raw SQL against the operational tables. The helpers enforce a few
discipline rules:

- Every write to ``sources``, ``ingest_anomalies``, or ``cycle_log`` goes
  through a typed function with a clear contract.
- Freshness queries return a typed dataclass per source rather than a raw
  tuple, so callers can't accidentally pluck the wrong column.
- License-related writes go through dedicated helpers so the ACLED gate
  (REQ-ACLED-LICENSE-001) and the Polymarket ToS gate are uniform.

Concurrency: callers pass in a connection. The helpers do not acquire
connections from a store; that's the caller's responsibility (the
``DuckDBStore`` pool from T-012).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import duckdb

# Posture under which an operator acknowledged a source's Terms of Service.
# v1 only writes ``'read_only'`` (Polymarket and Kalshi connectors are
# both read-only). v2+ trading work introduces ``'trading'`` for source
# rows that authorize order placement, alongside its own gate logic.
AcknowledgedPosture = Literal["read_only", "trading"]


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FreshnessRow:
    """One row from the ``freshness`` view.

    ``seconds_since_fetch`` is ``None`` when the source has never been
    successfully fetched. ``is_stale`` is ``True`` whenever the source is
    past its freshness threshold *or* has never been fetched.
    """

    source_id: str
    last_successful_fetch: datetime | None
    last_failed_fetch: datetime | None
    freshness_threshold_seconds: int
    seconds_since_fetch: float | None
    is_stale: bool


@dataclass(frozen=True, slots=True)
class SourceLicensePosture:
    """The license-related state recorded for a source."""

    license: str
    license_terms_hash: str | None
    license_acknowledged_at: datetime | None
    license_noncommercial_required: bool
    commercial_use_recorded_grant: bool
    acknowledged_posture: AcknowledgedPosture | None = None


def register_source(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    source_type: str,
    cadence: str,
    freshness_threshold_seconds: int,
    license: str,
    license_noncommercial_required: bool = False,
    notes: str | None = None,
) -> None:
    """Register a new source or no-op if it already exists.

    The full license posture (hash, acknowledgement timestamp,
    commercial-use grant) is set later by per-source startup gates via
    :func:`record_license_acknowledgement`. New registrations have no
    acknowledgement yet, so the corresponding columns are NULL / FALSE.
    """
    existing = conn.execute("SELECT 1 FROM sources WHERE source_id = ?", [source_id]).fetchone()
    if existing is not None:
        return

    conn.execute(
        """
        INSERT INTO sources (
            source_id, source_type, cadence, freshness_threshold_seconds,
            last_successful_fetch, last_failed_fetch, last_failure_summary,
            license, license_terms_hash, license_acknowledged_at,
            license_noncommercial_required, commercial_use_recorded_grant,
            registered_at, notes
        ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, ?, FALSE, ?, ?)
        """,
        [
            source_id,
            source_type,
            cadence,
            freshness_threshold_seconds,
            license,
            license_noncommercial_required,
            datetime.now(tz=UTC),
            notes,
        ],
    )


def update_last_successful_fetch(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
    *,
    when: datetime | None = None,
) -> None:
    """Mark a source as having had a successful fetch.

    Clears ``last_failed_fetch`` and ``last_failure_summary`` so the
    freshness view reflects the current healthy state. ``when`` defaults
    to the current UTC time.
    """
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        "UPDATE sources SET last_successful_fetch = ?, "
        "last_failed_fetch = NULL, last_failure_summary = NULL "
        "WHERE source_id = ?",
        [ts, source_id],
    )


def update_last_failed_fetch(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
    *,
    error_summary: str,
    when: datetime | None = None,
) -> None:
    """Mark a source as having had a failed fetch.

    Does not clear ``last_successful_fetch`` — a stale-but-known-failing
    source still has its prior good timestamp visible for the freshness
    view's "how stale" calculation.
    """
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        "UPDATE sources SET last_failed_fetch = ?, last_failure_summary = ? WHERE source_id = ?",
        [ts, error_summary, source_id],
    )


def record_anomaly(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    anomaly_type: str,
    details: dict[str, Any],
    cycle_id: str | None = None,
    when: datetime | None = None,
) -> str:
    """Record an ingest-time anomaly. Returns the new ``anomaly_id``."""
    anomaly_id = str(uuid.uuid4())
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        """
        INSERT INTO ingest_anomalies (
            anomaly_id, source_id, cycle_id, anomaly_type, detected_at, details_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [anomaly_id, source_id, cycle_id, anomaly_type, ts, json.dumps(details)],
    )
    return anomaly_id


def record_license_acknowledgement(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    terms_hash: str,
    when: datetime | None = None,
    commercial_use_recorded_grant: bool = False,
    acknowledged_posture: AcknowledgedPosture | None = None,
) -> None:
    """Record an operator acknowledgement of a source's license / Terms.

    Used by the ACLED Terms gate (T-060), the Polymarket ToS gate
    (T-PMC-021), and the Kalshi ToS gate (T-KSI-021). The hash is the
    SHA-256 of the canonical Terms text at acknowledgement time;
    subsequent runs compare and re-prompt if changed.

    ``commercial_use_recorded_grant`` defaults to FALSE — this is the
    conservative-non-commercial posture (REQ-ACLED-LICENSE-002). Operators
    record a TRUE only after explicitly reviewing the source's then-current
    Terms.

    ``acknowledged_posture`` distinguishes read-only acknowledgements (v1
    Polymarket, v1 Kalshi) from trading acknowledgements (reserved for
    v2+). Defaults to ``None`` for backward compatibility with sources
    that pre-date the column. When supplied, it is stored verbatim and
    later read by gates that need to refuse re-use under a different
    posture (e.g., the Kalshi gate refuses if a row is acknowledged as
    ``'trading'`` while v1 expects ``'read_only'``).
    """
    ts = when or datetime.now(tz=UTC)
    if acknowledged_posture is None:
        rows_affected = conn.execute(
            "UPDATE sources SET license_terms_hash = ?, license_acknowledged_at = ?, "
            "commercial_use_recorded_grant = ? WHERE source_id = ?",
            [terms_hash, ts, commercial_use_recorded_grant, source_id],
        ).fetchone()
    else:
        rows_affected = conn.execute(
            "UPDATE sources SET license_terms_hash = ?, license_acknowledged_at = ?, "
            "commercial_use_recorded_grant = ?, acknowledged_posture = ? "
            "WHERE source_id = ?",
            [terms_hash, ts, commercial_use_recorded_grant, acknowledged_posture, source_id],
        ).fetchone()
    # DuckDB's UPDATE in some versions returns rowcount via a separate channel;
    # we just check whether the row exists.
    del rows_affected
    existing = conn.execute("SELECT 1 FROM sources WHERE source_id = ?", [source_id]).fetchone()
    if existing is None:
        raise ValueError(f"source {source_id!r} is not registered")


def get_license_posture(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
) -> SourceLicensePosture | None:
    """Return the license posture for a source, or ``None`` if not registered."""
    row = conn.execute(
        "SELECT license, license_terms_hash, license_acknowledged_at, "
        "license_noncommercial_required, commercial_use_recorded_grant, "
        "acknowledged_posture "
        "FROM sources WHERE source_id = ?",
        [source_id],
    ).fetchone()
    if row is None:
        return None
    posture_raw = row[5]
    posture: AcknowledgedPosture | None
    if posture_raw is None:
        posture = None
    elif posture_raw in ("read_only", "trading"):
        posture = posture_raw
    else:
        # Unknown posture in the column — treat as missing and let the gate
        # refuse rather than silently coercing.
        posture = None
    return SourceLicensePosture(
        license=str(row[0]),
        license_terms_hash=str(row[1]) if row[1] is not None else None,
        license_acknowledged_at=row[2],
        license_noncommercial_required=bool(row[3]),
        commercial_use_recorded_grant=bool(row[4]),
        acknowledged_posture=posture,
    )


def query_freshness(
    conn: duckdb.DuckDBPyConnection,
    source_id: str | None = None,
) -> tuple[FreshnessRow, ...]:
    """Query the ``freshness`` view, optionally filtering to a single source."""
    if source_id is None:
        rows = conn.execute(
            "SELECT source_id, last_successful_fetch, last_failed_fetch, "
            "freshness_threshold_seconds, seconds_since_fetch, is_stale "
            "FROM freshness ORDER BY source_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source_id, last_successful_fetch, last_failed_fetch, "
            "freshness_threshold_seconds, seconds_since_fetch, is_stale "
            "FROM freshness WHERE source_id = ?",
            [source_id],
        ).fetchall()
    return tuple(
        FreshnessRow(
            source_id=str(r[0]),
            last_successful_fetch=r[1],
            last_failed_fetch=r[2],
            freshness_threshold_seconds=int(r[3]),
            seconds_since_fetch=float(r[4]) if r[4] is not None else None,
            is_stale=bool(r[5]),
        )
        for r in rows
    )


def freshness_for(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
) -> FreshnessRow | None:
    """Convenience: return the freshness row for one source, or ``None``."""
    rows = query_freshness(conn, source_id=source_id)
    if not rows:
        return None
    return rows[0]
