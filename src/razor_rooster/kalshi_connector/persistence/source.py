"""Kalshi source registration (T-KSI-012).

Registers the ``kalshi`` source row in the shared ``sources`` table so
the data_ingest freshness view, scheduler, and provenance helpers
treat Kalshi the same as any other source. Mirrors
:mod:`razor_rooster.polymarket_connector.persistence.source`.

The Kalshi subsystem also tracks a ``kalshi_settlements`` virtual
freshness entry so settlements can have a different threshold than
live prices (per design §4: 3h prices, 48h settlements). Both rows
share the same ``sources`` schema.
"""

from __future__ import annotations

from typing import Final

import duckdb

from razor_rooster.data_ingest.persistence.provenance import register_source

# The two source_id values Kalshi registers.
KALSHI_LIVE_SOURCE_ID: Final[str] = "kalshi"
KALSHI_SETTLEMENTS_SOURCE_ID: Final[str] = "kalshi_settlements"

# Source type marker so operators querying the sources table can tell
# Kalshi rows apart from Polymarket rows.
_SOURCE_TYPE: Final[str] = "kalshi_market"


def register_kalshi_sources(
    conn: duckdb.DuckDBPyConnection,
    *,
    prices_threshold_seconds: int = 10_800,  # 3h per design §4
    settlements_threshold_seconds: int = 172_800,  # 48h per design §4
) -> None:
    """Register both Kalshi source rows. Idempotent.

    The Terms of Service hash and acknowledgement timestamp are written
    later by the ToS gate (T-KSI-021) via the data_ingest license
    acknowledgement helper. New registrations have no acknowledgement
    yet, so the corresponding columns are NULL and the gate refuses to
    start the connector until they are populated.
    """
    register_source(
        conn,
        source_id=KALSHI_LIVE_SOURCE_ID,
        source_type=_SOURCE_TYPE,
        cadence="every_30min",
        freshness_threshold_seconds=prices_threshold_seconds,
        license="KALSHI_TERMS_VERSIONED",
        notes="Kalshi public market data (REST). ToS-versioned, read_only posture.",
    )
    register_source(
        conn,
        source_id=KALSHI_SETTLEMENTS_SOURCE_ID,
        source_type=_SOURCE_TYPE,
        cadence="daily",
        freshness_threshold_seconds=settlements_threshold_seconds,
        license="KALSHI_TERMS_VERSIONED",
        notes="Kalshi settled-market history (live + /historical/markets).",
    )
