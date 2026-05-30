"""Kalshi cycle integration (T-KSI-062; design §3.2).

Wraps the Phase 4 sync operations (cutoff snapshot, series, events,
markets, prices, settlements, trades) into a single
``run_kalshi_cycle`` entry point. Each stage runs with failure
isolation: an exception in one stage is captured in the report's
``errors`` list and does not stop the others. The aggregate result
projects onto a typed :class:`ConnectorOutcome` so the data_ingest
cycle report (T-040) can include a Kalshi section without scheduler
refactoring.

This module mirrors :mod:`razor_rooster.polymarket_connector.cycle`
in shape so the two connectors compose into the same cycle report
identically.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from razor_rooster.data_ingest.connectors.base import ConnectorOutcome
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.config.loader import (
    KalshiConfig,
    KalshiSectorKeywordsConfig,
    load_kalshi_sector_keywords,
)
from razor_rooster.kalshi_connector.sync.cutoff import snapshot_cutoff
from razor_rooster.kalshi_connector.sync.events import (
    EventsSyncReport,
    sync_events,
)
from razor_rooster.kalshi_connector.sync.markets import (
    MarketsSyncReport,
    sync_markets,
)
from razor_rooster.kalshi_connector.sync.prices import (
    PriceSnapshotReport,
    snapshot_prices,
)
from razor_rooster.kalshi_connector.sync.series import (
    SeriesSyncReport,
    sync_series,
)
from razor_rooster.kalshi_connector.sync.settlements import (
    SettlementsSyncReport,
    sync_settlements,
)
from razor_rooster.kalshi_connector.sync.trades import (
    TradesSyncReport,
    sync_trades,
)

logger = logging.getLogger(__name__)


# Source id used in the data_ingest cycle report's connector section
# when Kalshi is integrated. Distinct from the per-source rows in the
# ``sources`` table (those use KALSHI_LIVE_SOURCE_ID and
# KALSHI_SETTLEMENTS_SOURCE_ID).
KALSHI_CYCLE_SOURCE_ID: Final[str] = "kalshi"


@dataclass(slots=True)
class KalshiCycleReport:
    """Aggregate outcome of one full Kalshi cycle.

    Each per-stage report is None when that stage was skipped (because
    a prior stage failed) or because configuration disabled it.
    """

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    series: SeriesSyncReport | None = None
    events: EventsSyncReport | None = None
    markets: MarketsSyncReport | None = None
    prices: PriceSnapshotReport | None = None
    settlements: SettlementsSyncReport | None = None
    trades: TradesSyncReport | None = None
    errors: list[str] = field(default_factory=list)


def run_kalshi_cycle(
    store: DuckDBStore,
    *,
    config: KalshiConfig,
    sector_keywords: KalshiSectorKeywordsConfig | None = None,
    rest_client: KalshiRESTClient | None = None,
    skip_trades: bool = False,
    now: datetime | None = None,
) -> KalshiCycleReport:
    """Run a full Kalshi cycle with per-stage failure isolation.

    Stages run sequentially:

    1. ``snapshot_cutoff`` — anchors live/historical routing for the
       cycle.
    2. ``sync_series`` — series catalogue.
    3. ``sync_events`` — events per active series.
    4. ``sync_markets`` — markets per active event; runs the sector
       heuristic when ``sector_keywords`` is supplied.
    5. ``snapshot_prices`` — 30-min snapshots for active binary
       markets.
    6. ``sync_settlements`` — live + historical settlement reconcile.
    7. ``sync_trades`` — watched-markets-only trade pull (skipped when
       ``skip_trades=True`` or ``watched_markets`` is empty).

    Each stage is guarded by try/except. Failures are recorded in
    ``report.errors`` with the stage name and exception summary; the
    cycle continues so other stages still produce useful output.

    The caller may pass an already-constructed REST client to share
    its rate-limit bucket and httpx session across cycles; otherwise
    this function builds and closes its own.
    """
    started = now or datetime.now(tz=UTC)
    report = KalshiCycleReport(started_at=started)

    keywords: KalshiSectorKeywordsConfig | None = sector_keywords
    if keywords is None:
        try:
            keywords = load_kalshi_sector_keywords(config.sector_mapping.keywords_file)
        except Exception as exc:
            logger.warning(
                "Kalshi cycle: sector keywords unavailable; mapping skipped (%s)",
                exc,
            )
            report.errors.append(f"keywords_load: {type(exc).__name__}: {exc}")
            keywords = None

    owns_client = rest_client is None
    client = rest_client or KalshiRESTClient.from_config(config)

    try:
        try:
            snapshot_cutoff(store, client=client, now=started)
        except Exception as exc:
            logger.exception("Kalshi cutoff snapshot failed in cycle")
            report.errors.append(f"cutoff: {type(exc).__name__}: {exc}")

        try:
            report.series = sync_series(store, client=client, now=started)
        except Exception as exc:
            logger.exception("Kalshi series sync failed in cycle")
            report.errors.append(f"series: {type(exc).__name__}: {exc}")

        try:
            report.events = sync_events(store, client=client, now=started)
        except Exception as exc:
            logger.exception("Kalshi events sync failed in cycle")
            report.errors.append(f"events: {type(exc).__name__}: {exc}")

        try:
            report.markets = sync_markets(
                store,
                client=client,
                sector_keywords=keywords,
                now=started,
            )
        except Exception as exc:
            logger.exception("Kalshi markets sync failed in cycle")
            report.errors.append(f"markets: {type(exc).__name__}: {exc}")

        try:
            report.prices = snapshot_prices(store, client=client, now=started)
        except Exception as exc:
            logger.exception("Kalshi price snapshots failed in cycle")
            report.errors.append(f"prices: {type(exc).__name__}: {exc}")

        try:
            report.settlements = sync_settlements(store, client=client, now=started)
        except Exception as exc:
            logger.exception("Kalshi settlements sync failed in cycle")
            report.errors.append(f"settlements: {type(exc).__name__}: {exc}")

        watched = list(config.sync.prices.watched_markets)
        if not skip_trades and watched:
            try:
                report.trades = sync_trades(
                    store,
                    client=client,
                    watched_markets=watched,
                    now=started,
                )
            except Exception as exc:
                logger.exception("Kalshi trades sync failed in cycle")
                report.errors.append(f"trades: {type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            client.close()

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "Kalshi cycle done in %.1fs (errors=%d)",
        report.duration_seconds or 0.0,
        len(report.errors),
    )
    return report


def cycle_report_to_connector_outcome(
    report: KalshiCycleReport,
) -> ConnectorOutcome:
    """Project a :class:`KalshiCycleReport` onto a ``ConnectorOutcome``.

    Mirrors :func:`razor_rooster.polymarket_connector.cycle.cycle_report_to_connector_outcome`.
    """
    inserted = 0
    unchanged = 0
    if report.series is not None:
        inserted += report.series.series_inserted + report.series.series_updated
        unchanged += report.series.series_unchanged
    if report.events is not None:
        inserted += report.events.events_inserted + report.events.events_updated
        unchanged += report.events.events_unchanged
    if report.markets is not None:
        inserted += report.markets.markets_inserted + report.markets.markets_updated
        unchanged += report.markets.markets_unchanged
    if report.prices is not None:
        inserted += report.prices.snapshots_inserted
        unchanged += report.prices.snapshots_unchanged
    if report.settlements is not None:
        inserted += report.settlements.settlements_inserted
        unchanged += report.settlements.settlements_unchanged
    if report.trades is not None:
        inserted += report.trades.trades_inserted
        unchanged += report.trades.trades_unchanged

    has_errors = bool(report.errors)
    stages_with_data = [
        s
        for s in (
            report.series,
            report.events,
            report.markets,
            report.prices,
            report.settlements,
        )
        if s is not None
    ]
    has_successes = bool(stages_with_data)

    if has_errors and has_successes:
        status = "partial"
    elif has_errors:
        status = "failed"
    else:
        status = "ok"

    structured_errors: list[dict[str, object]] = [
        {"message": err, "type": "stage_failure"} for err in report.errors
    ]

    return ConnectorOutcome(
        source_id=KALSHI_CYCLE_SOURCE_ID,
        status=status,
        records_ingested=inserted,
        records_skipped_duplicate=unchanged,
        duration_seconds=report.duration_seconds or 0.0,
        errors=structured_errors,
    )


def stage_summary_lines(report: KalshiCycleReport) -> Iterable[str]:
    """Render a short per-stage summary suitable for stdout. Stable line order."""
    if report.series is not None:
        yield (
            f"  series:      seen={report.series.series_total_seen} "
            f"inserted={report.series.series_inserted} "
            f"removed={report.series.series_removed}"
        )
    if report.events is not None:
        yield (
            f"  events:      seen={report.events.events_total_seen} "
            f"inserted={report.events.events_inserted} "
            f"removed={report.events.events_removed}"
        )
    if report.markets is not None:
        yield (
            f"  markets:     seen={report.markets.markets_total_seen} "
            f"inserted={report.markets.markets_inserted} "
            f"removed={report.markets.markets_removed} "
            f"mappings_upserted={report.markets.mappings_upserted}"
        )
    if report.prices is not None:
        yield (
            f"  prices:      evaluated={report.prices.markets_evaluated} "
            f"inserted={report.prices.snapshots_inserted}"
        )
    if report.settlements is not None:
        yield (
            f"  settlements: seen={report.settlements.settlements_seen} "
            f"inserted={report.settlements.settlements_inserted}"
        )
    if report.trades is not None:
        yield (
            f"  trades:      tickers={report.trades.tickers_evaluated} "
            f"inserted={report.trades.trades_inserted}"
        )
    elif report.markets is not None:
        yield "  trades:      (no watched markets configured)"
    if report.errors:
        for err in report.errors:
            yield f"  error:       {err}"


__all__ = [
    "KALSHI_CYCLE_SOURCE_ID",
    "KalshiCycleReport",
    "cycle_report_to_connector_outcome",
    "run_kalshi_cycle",
    "stage_summary_lines",
]
