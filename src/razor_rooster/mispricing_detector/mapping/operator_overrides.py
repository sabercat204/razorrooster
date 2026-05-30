"""Thin wrapper around persistence helpers for operator-curated mappings.

Centralizes the operator mapping flow so the CLI in
:mod:`mispricing_detector.cli` doesn't need to know about persistence
internals. Tests exercising the CLI mock this module rather than the
DuckDB store directly.
"""

from __future__ import annotations

import logging

import duckdb

from razor_rooster.mispricing_detector.models import (
    ClassMarketMapping,
    MappingConfidence,
    MappingType,
    Polarity,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    MappingExistsError,
    query_mappings,
    register_mapping,
    remove_mapping,
)

logger = logging.getLogger(__name__)


def register_operator_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    condition_id: str,
    mapping_type: MappingType = "direct",
    polarity: Polarity = "aligned",
    notes: str | None = None,
    venue: str = "polymarket",
) -> ClassMarketMapping:
    """Operator-driven mapping registration.

    Always sets ``mapped_by='operator'`` and ``mapping_confidence='exact'``.
    Raises :class:`MappingExistsError` on collision so the CLI can surface
    a clean message.

    The ``venue`` parameter (T-KSI-061) discriminates between the two
    supported markets. ``condition_id`` is interpreted per venue: a
    Polymarket condition_id when ``venue='polymarket'``; a Kalshi
    ticker when ``venue='kalshi'``.
    """
    return register_mapping(
        conn,
        class_id=class_id,
        condition_id=condition_id,
        mapping_type=mapping_type,
        mapping_confidence="exact",
        polarity=polarity,
        mapped_by="operator",
        notes=notes,
        venue=venue,  # type: ignore[arg-type]
    )


def remove_operator_mapping(conn: duckdb.DuckDBPyConnection, *, mapping_id: str) -> bool:
    """Soft-delete an operator-curated mapping. Returns True on success."""
    return remove_mapping(conn, mapping_id=mapping_id)


def list_active_operator_mappings(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str | None = None,
    confidence: MappingConfidence | None = None,
) -> tuple[ClassMarketMapping, ...]:
    """List active mappings (any source: operator or auto-derived)."""
    return query_mappings(conn, class_id=class_id, confidence=confidence)


__all__ = [
    "MappingExistsError",
    "list_active_operator_mappings",
    "register_operator_mapping",
    "remove_operator_mapping",
]
