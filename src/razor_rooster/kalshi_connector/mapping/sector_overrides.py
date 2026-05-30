"""Kalshi sector mapping persistence + operator overrides (T-KSI-051; design §3.5).

Three responsibilities:

1. Persist heuristic mappings produced by :mod:`mapping.sector_heuristic`
   into ``kalshi_sector_mapping``. The persistence rule preserves any
   existing operator override (``confidence='manual'``); a new
   inferred mapping never clobbers a manual one.
2. Allow operators to record manual overrides via :func:`set_override`.
3. Surface the "needs review" list — markets where the heuristic
   produced no mapping (``razor_sector IS NULL`` with
   ``confidence='inferred'``) — and a mapping-stats query for the CLI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb

from razor_rooster.kalshi_connector.mapping.sector_heuristic import (
    HEURISTIC_TAG,
    INFERRED_CONFIDENCE,
    MANUAL_CONFIDENCE,
    SectorMapping,
)

logger = logging.getLogger(__name__)


_OPERATOR_TAG: str = "operator"


@dataclass(frozen=True, slots=True)
class SectorMappingRow:
    """Snapshot of one row from ``kalshi_sector_mapping``."""

    ticker: str
    razor_sector: str | None
    secondary_sectors: tuple[str, ...]
    confidence: str
    mapped_at: datetime
    mapped_by: str


@dataclass(frozen=True, slots=True)
class MappingStats:
    """Aggregate counts surfaced via the CLI mapping-stats subcommand."""

    by_sector: dict[str, int]
    by_confidence: dict[str, int]
    unmapped: int


def upsert_inferred_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    mapping: SectorMapping,
    when: datetime | None = None,
) -> bool:
    """Upsert a heuristic mapping. Preserves existing manual overrides.

    Returns True iff a row was inserted or updated; False iff the call
    was a no-op because a manual override already exists for this
    ticker.
    """
    existing = _read_row(conn, ticker)
    if existing is not None and existing.confidence == MANUAL_CONFIDENCE:
        logger.debug(
            "preserving manual override for ticker %s; heuristic suggested %s",
            ticker,
            mapping.razor_sector,
        )
        return False

    ts = when or datetime.now(tz=UTC)
    secondary_json = json.dumps(list(mapping.secondary_sectors))

    if existing is None:
        conn.execute(
            "INSERT INTO kalshi_sector_mapping ("
            "ticker, razor_sector, secondary_sectors, confidence, "
            "mapped_at, mapped_by) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ticker,
                mapping.razor_sector,
                secondary_json,
                INFERRED_CONFIDENCE,
                ts,
                mapping.mapped_by or HEURISTIC_TAG,
            ],
        )
        return True

    conn.execute(
        "UPDATE kalshi_sector_mapping SET razor_sector = ?, "
        "secondary_sectors = ?, confidence = ?, mapped_at = ?, mapped_by = ? "
        "WHERE ticker = ?",
        [
            mapping.razor_sector,
            secondary_json,
            INFERRED_CONFIDENCE,
            ts,
            mapping.mapped_by or HEURISTIC_TAG,
            ticker,
        ],
    )
    return True


def set_override(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    razor_sector: str | None,
    secondary: list[str] | None = None,
    when: datetime | None = None,
    operator_tag: str = _OPERATOR_TAG,
) -> None:
    """Record a manual operator override.

    ``razor_sector=None`` is allowed and represents "operator confirmed
    that no Razor sector applies" — distinct from the heuristic's
    ambiguous output (``confidence='inferred'`` with
    ``razor_sector=None``).

    The Kalshi-specific ``'out_of_scope'`` value is allowed here too,
    in case the operator wants to manually mark a market that the
    heuristic couldn't auto-classify.
    """
    ts = when or datetime.now(tz=UTC)
    secondary_json = json.dumps(secondary or [])
    existing = _read_row(conn, ticker)
    if existing is None:
        conn.execute(
            "INSERT INTO kalshi_sector_mapping ("
            "ticker, razor_sector, secondary_sectors, confidence, "
            "mapped_at, mapped_by) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ticker,
                razor_sector,
                secondary_json,
                MANUAL_CONFIDENCE,
                ts,
                operator_tag,
            ],
        )
    else:
        conn.execute(
            "UPDATE kalshi_sector_mapping SET razor_sector = ?, "
            "secondary_sectors = ?, confidence = ?, mapped_at = ?, mapped_by = ? "
            "WHERE ticker = ?",
            [
                razor_sector,
                secondary_json,
                MANUAL_CONFIDENCE,
                ts,
                operator_tag,
                ticker,
            ],
        )


def get_mapping(conn: duckdb.DuckDBPyConnection, ticker: str) -> SectorMappingRow | None:
    return _read_row(conn, ticker)


def needs_review(
    conn: duckdb.DuckDBPyConnection,
    *,
    limit: int | None = None,
) -> list[SectorMappingRow]:
    """Return tickers where the heuristic produced no sector mapping.

    Filters to ``razor_sector IS NULL`` and ``confidence='inferred'``;
    operator-confirmed null mappings (``manual``) are excluded — those
    are explicit decisions, not pending reviews.
    """
    query = (
        "SELECT ticker, razor_sector, secondary_sectors, confidence, "
        "mapped_at, mapped_by FROM kalshi_sector_mapping "
        "WHERE razor_sector IS NULL AND confidence = 'inferred' "
        "ORDER BY mapped_at DESC"
    )
    params: list[object] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_dataclass(r) for r in rows]


def mapping_stats(conn: duckdb.DuckDBPyConnection) -> MappingStats:
    sector_rows = conn.execute(
        "SELECT razor_sector, COUNT(*) FROM kalshi_sector_mapping "
        "GROUP BY razor_sector ORDER BY razor_sector"
    ).fetchall()
    by_sector: dict[str, int] = {}
    unmapped = 0
    for sector, count in sector_rows:
        if sector is None:
            unmapped = int(count)
            continue
        by_sector[str(sector)] = int(count)

    confidence_rows = conn.execute(
        "SELECT confidence, COUNT(*) FROM kalshi_sector_mapping "
        "GROUP BY confidence ORDER BY confidence"
    ).fetchall()
    by_confidence: dict[str, int] = {str(r[0]): int(r[1]) for r in confidence_rows}

    return MappingStats(by_sector=by_sector, by_confidence=by_confidence, unmapped=unmapped)


# -- internals --------------------------------------------------------------


def _read_row(conn: duckdb.DuckDBPyConnection, ticker: str) -> SectorMappingRow | None:
    row = conn.execute(
        "SELECT ticker, razor_sector, secondary_sectors, confidence, "
        "mapped_at, mapped_by FROM kalshi_sector_mapping WHERE ticker = ?",
        [ticker],
    ).fetchone()
    if row is None:
        return None
    return _row_to_dataclass(row)


def _row_to_dataclass(row: tuple[object, ...]) -> SectorMappingRow:
    secondary_json = row[2]
    secondary: tuple[str, ...] = ()
    if isinstance(secondary_json, str) and secondary_json:
        try:
            decoded = json.loads(secondary_json)
            if isinstance(decoded, list):
                secondary = tuple(str(s) for s in decoded)
        except json.JSONDecodeError:
            secondary = ()
    mapped_at_value = row[4]
    if not isinstance(mapped_at_value, datetime):
        mapped_at_value = datetime.now(tz=UTC)
    return SectorMappingRow(
        ticker=str(row[0]),
        razor_sector=str(row[1]) if row[1] is not None else None,
        secondary_sectors=secondary,
        confidence=str(row[3]),
        mapped_at=mapped_at_value,
        mapped_by=str(row[5]),
    )


__all__ = [
    "MappingStats",
    "SectorMappingRow",
    "get_mapping",
    "mapping_stats",
    "needs_review",
    "set_override",
    "upsert_inferred_mapping",
]
