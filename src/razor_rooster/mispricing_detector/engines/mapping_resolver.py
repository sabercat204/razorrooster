"""Mapping resolver (T-MD-022; design §3.5).

Combines two sources to produce the comparison-cycle's working set
of (class, market) pairs:

1. Operator-curated active mappings persisted in
   ``class_market_mappings`` (where ``removed_at IS NULL``).
2. Auto-derived mappings computed fresh per cycle via
   :func:`mapping.auto_heuristic.confidence` against the live
   Polymarket markets and sector-mapping table.

Auto mappings respect existing operator mappings and tombstoned
removals (a (class, condition) pair previously removed by the
operator does not get auto-resurrected).

Auto mappings are not persisted by default; they live only for the
duration of the cycle. Operators who want to "promote" an auto
mapping to a stable one issue an explicit ``mispricing map ...`` CLI
command.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.mispricing_detector.mapping.auto_heuristic import (
    HeuristicConfig,
    MarketSummary,
    confidence,
)
from razor_rooster.mispricing_detector.models import ClassMarketMapping
from razor_rooster.mispricing_detector.persistence.operations import query_mappings
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import EventClass

logger = logging.getLogger(__name__)


def resolve(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id_filter: str | None = None,
    heuristic_config: HeuristicConfig | None = None,
    when: datetime | None = None,
) -> tuple[ClassMarketMapping, ...]:
    """Return the mappings to evaluate this cycle.

    Args:
        conn: DuckDB connection with ``class_market_mappings`` and the
            ``polymarket_*`` tables in scope.
        class_id_filter: When set, restrict the result to this class.
        heuristic_config: Override the auto-heuristic knobs.
        when: Override "now" for testing.
    """
    operator = query_mappings(conn, class_id=class_id_filter)
    auto = derive_auto_mappings(
        conn,
        existing=operator,
        class_id_filter=class_id_filter,
        heuristic_config=heuristic_config,
        when=when,
    )
    return tuple(operator) + auto


def derive_auto_mappings(
    conn: duckdb.DuckDBPyConnection,
    *,
    existing: Iterable[ClassMarketMapping] = (),
    class_id_filter: str | None = None,
    heuristic_config: HeuristicConfig | None = None,
    when: datetime | None = None,
) -> tuple[ClassMarketMapping, ...]:
    """Compute auto mappings respecting existing + tombstoned pairs.

    Returns a tuple of in-memory :class:`ClassMarketMapping` rows. The
    rows are NOT persisted — callers that want to keep an auto mapping
    must call :func:`persistence.operations.register_mapping` with
    ``mapped_by='auto'`` and the right confidence.
    """
    ts = when or datetime.now(tz=UTC)
    cfg = heuristic_config or HeuristicConfig()

    # Pre-build the set of (class, condition) pairs we should never
    # auto-map: existing operator-active OR previously tombstoned.
    blocked_pairs = {(m.class_id, m.condition_id) for m in existing}
    blocked_pairs |= _tombstoned_pairs(conn)

    # Pull live registered classes.
    classes_iter = registry.get_all()
    if class_id_filter is not None:
        classes_iter = tuple(c for c in classes_iter if c.class_id == class_id_filter)

    if not classes_iter:
        return ()

    markets = _fetch_active_markets_with_sectors(conn)
    if not markets:
        return ()

    out: list[ClassMarketMapping] = []
    for cls in classes_iter:
        for mkt in markets:
            if (cls.class_id, mkt.condition_id) in blocked_pairs:
                continue
            level = confidence(cls, mkt, config=cfg)
            if level is None:
                continue
            out.append(
                ClassMarketMapping(
                    mapping_id=f"auto-{uuid.uuid4()}",
                    class_id=cls.class_id,
                    condition_id=mkt.condition_id,
                    mapping_type="proxy",
                    mapping_confidence=level,
                    polarity="aligned",
                    mapped_by="auto",
                    mapped_at=ts,
                )
            )
    return tuple(out)


# -- internals --------------------------------------------------------------


def _tombstoned_pairs(conn: duckdb.DuckDBPyConnection) -> set[tuple[str, str]]:
    """Return the set of (class_id, condition_id) pairs that were once
    operator-mapped and subsequently removed.

    These are excluded from auto-mapping so an operator who explicitly
    removed a mapping doesn't see the same pair re-appear from the
    heuristic.
    """
    rows = conn.execute(
        "SELECT DISTINCT class_id, condition_id FROM class_market_mappings "
        "WHERE removed_at IS NOT NULL"
    ).fetchall()
    return {(str(r[0]), str(r[1])) for r in rows}


def _fetch_active_markets_with_sectors(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[MarketSummary, ...]:
    """Pull active Polymarket markets joined to their razor_sector."""
    rows = conn.execute(
        "SELECT m.condition_id, sm.razor_sector, m.question, m.description "
        "FROM polymarket_markets m "
        "LEFT JOIN polymarket_sector_mapping sm ON m.condition_id = sm.condition_id "
        "WHERE m.active = TRUE AND m.closed = FALSE AND m.resolved = FALSE "
        "  AND m.superseded_at IS NULL "
        "  AND m.market_type = 'binary'"
    ).fetchall()
    out: list[MarketSummary] = []
    for r in rows:
        out.append(
            MarketSummary(
                condition_id=str(r[0]),
                razor_sector=(str(r[1]) if r[1] is not None else None),
                question=str(r[2]),
                description=(str(r[3]) if r[3] is not None else None),
            )
        )
    return tuple(out)


__all__ = ["derive_auto_mappings", "resolve"]


_RESERVED: tuple[Any, ...] = (EventClass,)
