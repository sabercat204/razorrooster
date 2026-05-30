"""Polymarket source registration (T-PMC-012).

Registers the ``polymarket`` source row in the shared ``sources`` table
so the data_ingest freshness view, scheduler, and provenance helpers
treat Polymarket the same as any other source.

The Polymarket subsystem also tracks a ``polymarket_resolutions`` virtual
freshness entry so resolutions can have a different threshold than live
prices (per OQ-PMC-007: 6h prices, 48h resolutions). Both rows share the
same ``sources`` schema.
"""

from __future__ import annotations

from typing import Final

import duckdb

from razor_rooster.data_ingest.persistence.provenance import register_source

# The two source_id values Polymarket registers. The first is the
# "live data" source; the second covers resolutions which update on a
# different cadence and need a separate freshness threshold.
POLYMARKET_LIVE_SOURCE_ID: Final[str] = "polymarket"
POLYMARKET_RESOLUTIONS_SOURCE_ID: Final[str] = "polymarket_resolutions"

# Source type tag — Polymarket data straddles the canonical-schema model
# but its writes target the polymarket_* namespace, so the type column
# stays informational only. We use "polymarket_market" as a marker so
# operators querying the sources table can distinguish.
_SOURCE_TYPE: Final[str] = "polymarket_market"


def register_polymarket_sources(
    conn: duckdb.DuckDBPyConnection,
    *,
    prices_threshold_seconds: int = 21_600,  # 6h per OQ-PMC-007
    resolutions_threshold_seconds: int = 172_800,  # 48h per OQ-PMC-007
) -> None:
    """Register both Polymarket source rows. Idempotent.

    The Terms of Service hash and acknowledgement timestamp are written
    later by the ToS gate (T-PMC-021) via
    :func:`record_license_acknowledgement`. New registrations have no
    acknowledgement yet, so the corresponding columns are NULL and the
    gate refuses to start the connector until they are populated.
    """
    register_source(
        conn,
        source_id=POLYMARKET_LIVE_SOURCE_ID,
        source_type=_SOURCE_TYPE,
        cadence="hourly",
        freshness_threshold_seconds=prices_threshold_seconds,
        license="POLYMARKET_TERMS_VERSIONED",
        notes="Polymarket Gamma API + CLOB public REST. ToS-versioned.",
    )
    register_source(
        conn,
        source_id=POLYMARKET_RESOLUTIONS_SOURCE_ID,
        source_type=_SOURCE_TYPE,
        cadence="daily",
        freshness_threshold_seconds=resolutions_threshold_seconds,
        license="POLYMARKET_TERMS_VERSIONED",
        notes="Polymarket resolved-market history (Gamma API).",
    )
