"""Public read-only API facade for pattern_library (T-PL-060; design §5.4).

Downstream subsystems (signal_scanner, mispricing_detector, etc.)
import this module to read library outputs. The facade is the only
sanctioned read interface — direct queries against ``pl_*`` tables are
discouraged and may break across library versions.

Every function reads from persisted tables (no on-the-fly computation)
and returns versioned dataclasses tagged with ``library_version``.
Consumers compare returned versions against
:func:`current_version` to detect mismatches.

The facade does not own a DuckDB connection; callers pass in a
:class:`DuckDBStore` instance (typically the same one their subsystem
already holds) and the facade acquires connections from its pool.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.pattern_library.engines.analogues import find_analogues
from razor_rooster.pattern_library.models.analogue import (
    AnalogueResults,
)
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.calibration import (
    CalibrationOutput,
    ReliabilityBin,
)
from razor_rooster.pattern_library.models.event_class import Sector
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.pattern_library.persistence.operations import (
    query_latest_base_rate,
    query_signatures,
)
from razor_rooster.pattern_library.version import current_version as _current_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventClassSummary:
    """Lightweight projection of ``pl_event_classes`` for the list_classes API."""

    class_id: str
    title: str
    description: str
    domain_sector: Sector
    secondary_sectors: tuple[Sector, ...]
    definition_version: int
    library_version_at_last_eval: int | None
    last_evaluated_at: datetime | None
    removed_at: datetime | None


def current_version() -> int:
    """Return the live library version. Identical to
    :func:`pattern_library.version.current_version`; re-exported here so
    consumers only need to import the facade module.
    """
    return _current_version()


def list_classes(
    store: DuckDBStore,
    *,
    sector: Sector | None = None,
    include_removed: bool = False,
) -> tuple[EventClassSummary, ...]:
    """Return summaries for every registered class.

    Args:
        store: DuckDB store with the pattern_library schemas applied.
        sector: Optional filter by primary domain sector.
        include_removed: When True, also return classes whose
            ``removed_at`` is set. Default False keeps the result list
            tight.
    """
    query = (
        "SELECT class_id, title, description, domain_sector, secondary_sectors, "
        "definition_version, library_version_at_last_eval, last_evaluated_at, removed_at "
        "FROM pl_event_classes"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if not include_removed:
        conditions.append("removed_at IS NULL")
    if sector is not None:
        conditions.append("domain_sector = ?")
        params.append(sector.value)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY class_id"

    with store.connection() as conn:
        rows = conn.execute(query, params).fetchall()

    summaries: list[EventClassSummary] = []
    for row in rows:
        secondary_sectors_raw = row[4]
        secondary_sectors: tuple[Sector, ...] = ()
        if isinstance(secondary_sectors_raw, str) and secondary_sectors_raw:
            try:
                decoded = json.loads(secondary_sectors_raw)
                if isinstance(decoded, list):
                    secondary_sectors = tuple(Sector(s) for s in decoded if s)
            except (json.JSONDecodeError, ValueError):
                secondary_sectors = ()
        summaries.append(
            EventClassSummary(
                class_id=str(row[0]),
                title=str(row[1]),
                description=str(row[2]),
                domain_sector=Sector(row[3]),
                secondary_sectors=secondary_sectors,
                definition_version=int(row[5]),
                library_version_at_last_eval=(int(row[6]) if row[6] is not None else None),
                last_evaluated_at=row[7],
                removed_at=row[8],
            )
        )
    return tuple(summaries)


def base_rate(
    store: DuckDBStore,
    class_id: str,
    *,
    library_version: int | None = None,
) -> BaseRateResult | None:
    """Return the latest persisted base rate for a class.

    When ``library_version`` is omitted, the most recent computation is
    returned regardless of which library version produced it. Callers
    that care about version coherence should compare the returned
    value's ``library_version`` against :func:`current_version`.
    """
    with store.connection() as conn:
        return query_latest_base_rate(conn, class_id, library_version=library_version)


def signature(
    store: DuckDBStore,
    class_id: str,
    *,
    library_version: int | None = None,
) -> tuple[SignatureResult, ...]:
    """Return all persisted signatures for a class.

    When ``library_version`` is omitted, returns signatures from the
    most recent library version that touched the class. This is the
    common case for callers that just want "the latest signatures."
    """
    target_version = library_version
    if target_version is None:
        with store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(library_version) FROM pl_precursor_signatures WHERE class_id = ?",
                [class_id],
            ).fetchone()
        if row is None or row[0] is None:
            return ()
        target_version = int(row[0])

    with store.connection() as conn:
        return query_signatures(conn, class_id, library_version=target_version)


def find_analogues_by_class_id(
    store: DuckDBStore,
    *,
    class_id: str,
    current_features: dict[str, float],
    library_version: int | None = None,
    feature_weights: dict[str, float] | None = None,
    k: int = 10,
    query_timestamp: datetime | None = None,
) -> AnalogueResults:
    """Return the top-k analogues for a class given the operator's current features.

    Read-only wrapper around :func:`engines.analogues.find_analogues`
    that defaults ``library_version`` to the latest version with
    persisted analogue features for the class.
    """
    target_version = library_version
    if target_version is None:
        with store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(library_version) FROM pl_analogue_features WHERE class_id = ?",
                [class_id],
            ).fetchone()
        if row is None or row[0] is None:
            return AnalogueResults(
                class_id=class_id,
                library_version=current_version(),
                definition_version=1,
                query_timestamp=query_timestamp or datetime.now(tz=UTC),
                matches=(),
            )
        target_version = int(row[0])

    # Pull the class's stored definition_version so the result row tags
    # match what was persisted.
    with store.connection() as conn:
        def_row = conn.execute(
            "SELECT MAX(definition_version) FROM pl_analogue_features "
            "WHERE class_id = ? AND library_version = ?",
            [class_id, target_version],
        ).fetchone()
    target_definition = int(def_row[0]) if def_row and def_row[0] is not None else 1

    with store.connection() as conn:
        return find_analogues(
            conn,
            class_id=class_id,
            current_features=current_features,
            library_version=target_version,
            definition_version=target_definition,
            feature_weights=feature_weights,
            k=k,
            query_timestamp=query_timestamp,
        )


def calibration(
    store: DuckDBStore,
    class_id: str,
    *,
    library_version: int | None = None,
) -> CalibrationOutput | None:
    """Return the latest calibration result for a class, if any.

    Returns ``None`` when no calibration has been persisted for the
    class — distinct from the "computed but skipped" path
    (``method='insufficient_data'`` with ``brier_score=None``).
    """
    target_version = library_version
    base_query = (
        "SELECT class_id, library_version, definition_version, method, "
        "brier_score, reliability_bins, prediction_trace_path, computed_at, notes "
        "FROM pl_calibration WHERE class_id = ?"
    )
    if target_version is not None:
        query = base_query + " AND library_version = ? ORDER BY computed_at DESC LIMIT 1"
        params: tuple[Any, ...] = (class_id, target_version)
    else:
        query = base_query + " ORDER BY computed_at DESC LIMIT 1"
        params = (class_id,)

    with store.connection() as conn:
        row = conn.execute(query, list(params)).fetchone()
    if row is None:
        return None

    bins_payload = row[5]
    bins: tuple[ReliabilityBin, ...] = ()
    if isinstance(bins_payload, str) and bins_payload:
        try:
            decoded = json.loads(bins_payload)
            if isinstance(decoded, list):
                bins = tuple(
                    ReliabilityBin(
                        bin_low=float(b["bin_low"]),
                        bin_high=float(b["bin_high"]),
                        predicted_mean=float(b["predicted_mean"]),
                        observed_freq=float(b["observed_freq"]),
                        count=int(b["count"]),
                    )
                    for b in decoded
                    if isinstance(b, dict)
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            bins = ()

    return CalibrationOutput(
        class_id=str(row[0]),
        library_version=int(row[1]),
        definition_version=int(row[2]),
        method=str(row[3]),
        brier_score=float(row[4]) if row[4] is not None else None,
        reliability_bins=bins,
        prediction_trace_path=str(row[6]),
        computed_at=row[7],
        notes=str(row[8]) if row[8] is not None else None,
    )


# Public re-exports so consumers can write
# ``from razor_rooster.pattern_library import library; library.base_rate(...)``.
__all__ = [
    "EventClassSummary",
    "base_rate",
    "calibration",
    "current_version",
    "find_analogues_by_class_id",
    "list_classes",
    "signature",
]


# Reserved imports — exposed for type-hint consumers.
_RESERVED: tuple[Any, ...] = (np, Sequence)
