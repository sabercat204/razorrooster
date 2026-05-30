"""Polymarket cycle integration (T-PMC-061; design §3.2).

Wraps the four Phase 4 sync operations (markets, prices, resolutions
delta, watched trades) into a single ``run_polymarket_cycle`` entry
point. Each sync runs with failure isolation: an exception in one stage
is captured in the report's ``errors`` list and does not stop the
others. The aggregate result projects onto a typed
:class:`ConnectorOutcome` so the data_ingest cycle report (T-040) can
include a Polymarket section without scheduler refactoring.

This module is the seam the operator hits via
``razor-rooster ingest cycle`` (when the data_ingest cycle hooks Polymarket
in) or ``razor-rooster polymarket sync`` (the standalone path).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from razor_rooster.data_ingest.connectors.base import ConnectorOutcome
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.polymarket_connector.client.clob_public import (
    ClobPublicClient,
)
from razor_rooster.polymarket_connector.client.gamma import GammaClient
from razor_rooster.polymarket_connector.config.loader import (
    PolymarketConfig,
    SectorKeywordsConfig,
    load_sector_keywords,
)
from razor_rooster.polymarket_connector.sync.markets import (
    MarketSyncReport,
    sync_markets,
)
from razor_rooster.polymarket_connector.sync.prices import (
    PriceSnapshotReport,
    snapshot_prices,
)
from razor_rooster.polymarket_connector.sync.resolutions import (
    ResolutionDeltaReport,
    sync_recent_resolutions,
)
from razor_rooster.polymarket_connector.sync.trades import (
    TradePullReport,
    pull_watched_trades,
)

logger = logging.getLogger(__name__)


# Source id used in the data_ingest cycle report's connector section
# when Polymarket is integrated. Distinct from the per-source rows in
# the ``sources`` table (those use POLYMARKET_LIVE_SOURCE_ID and
# POLYMARKET_RESOLUTIONS_SOURCE_ID).
POLYMARKET_CYCLE_SOURCE_ID: Final[str] = "polymarket"


@dataclass(slots=True)
class PolymarketCycleReport:
    """Aggregate outcome of one full Polymarket cycle.

    Each per-stage report is None when that stage was skipped (either
    by configuration — empty watched markets — or because a prior stage
    failed and downstream stages are not safe to run).
    """

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    markets: MarketSyncReport | None = None
    prices: PriceSnapshotReport | None = None
    resolutions: ResolutionDeltaReport | None = None
    trades: TradePullReport | None = None
    errors: list[str] = field(default_factory=list)


def run_polymarket_cycle(
    store: DuckDBStore,
    *,
    config: PolymarketConfig,
    sector_keywords: SectorKeywordsConfig | None = None,
    gamma_client: GammaClient | None = None,
    clob_client: ClobPublicClient | None = None,
    now: datetime | None = None,
) -> PolymarketCycleReport:
    """Run a full Polymarket cycle with per-stage failure isolation.

    Stages run sequentially:

    1. ``sync_markets`` — upserts active and closed markets, runs the
       sector heuristic when ``sector_keywords`` is supplied.
    2. ``snapshot_prices`` — hourly orderbook snapshots for active
       binary markets.
    3. ``sync_recent_resolutions`` — daily delta of newly resolved
       markets.
    4. ``pull_watched_trades`` — opt-in trade history for watched
       markets (skipped when the watched list is empty).

    Each stage is guarded by a try/except. Failures are recorded in
    ``report.errors`` with the stage name and exception summary; the
    cycle continues so other stages still produce useful output.

    The caller may pass already-constructed clients to share httpx
    sessions across calls; otherwise this function creates and closes
    its own via the default factories.
    """
    started = now or datetime.now(tz=UTC)
    report = PolymarketCycleReport(started_at=started)

    keywords: SectorKeywordsConfig | None = sector_keywords
    if keywords is None:
        try:
            keywords = load_sector_keywords(config.sector_mapping.keywords_file)
        except Exception as exc:
            logger.warning(
                "Polymarket cycle: sector keywords unavailable; mapping skipped (%s)",
                exc,
            )
            report.errors.append(f"keywords_load: {type(exc).__name__}: {exc}")
            keywords = None

    owns_gamma = gamma_client is None
    owns_clob = clob_client is None
    gamma = gamma_client or GammaClient()
    clob = clob_client or ClobPublicClient()

    try:
        try:
            report.markets = sync_markets(
                store,
                client=gamma,
                sector_keywords=keywords,
                now=started,
            )
        except Exception as exc:
            logger.exception("Polymarket markets sync failed in cycle")
            report.errors.append(f"markets: {type(exc).__name__}: {exc}")

        try:
            report.prices = snapshot_prices(
                store,
                client=clob,
                now=started,
            )
        except Exception as exc:
            logger.exception("Polymarket price snapshots failed in cycle")
            report.errors.append(f"prices: {type(exc).__name__}: {exc}")

        try:
            report.resolutions = sync_recent_resolutions(
                store,
                client=gamma,
                now=started,
            )
        except Exception as exc:
            logger.exception("Polymarket resolutions delta failed in cycle")
            report.errors.append(f"resolutions: {type(exc).__name__}: {exc}")

        watched = list(config.sync.prices.watched_markets)
        if watched:
            try:
                report.trades = pull_watched_trades(
                    store,
                    client=clob,
                    watched_markets=watched,
                    now=started,
                )
            except Exception as exc:
                logger.exception("Polymarket trades pull failed in cycle")
                report.errors.append(f"trades: {type(exc).__name__}: {exc}")
    finally:
        if owns_gamma:
            gamma.close()
        if owns_clob:
            clob.close()

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "Polymarket cycle done in %.1fs (errors=%d)",
        report.duration_seconds or 0.0,
        len(report.errors),
    )
    return report


def cycle_report_to_connector_outcome(
    report: PolymarketCycleReport,
) -> ConnectorOutcome:
    """Project a :class:`PolymarketCycleReport` onto a ConnectorOutcome.

    The data_ingest cycle report (T-040) consumes ConnectorOutcome
    objects to produce its per-connector summary lines. The mapping:

    - ``records_ingested`` totals the markets inserted/updated, price
      snapshots, resolution deltas, and trade rows.
    - ``records_skipped_duplicate`` totals unchanged buckets.
    - ``status`` is 'failed' if any stage produced errors, 'partial'
      if some stages errored but others succeeded, else 'ok'.
    - ``errors`` carries the same per-stage error strings.
    """
    inserted = 0
    unchanged = 0
    if report.markets is not None:
        inserted += report.markets.markets_inserted + report.markets.markets_updated
        unchanged += report.markets.markets_unchanged
    if report.prices is not None:
        inserted += report.prices.snapshots_inserted
        unchanged += report.prices.snapshots_unchanged
    if report.resolutions is not None:
        inserted += report.resolutions.resolutions_inserted + report.resolutions.resolutions_updated
        unchanged += report.resolutions.resolutions_unchanged
    if report.trades is not None:
        inserted += report.trades.trades_inserted
        unchanged += report.trades.trades_unchanged

    stages_with_data = [
        s for s in (report.markets, report.prices, report.resolutions) if s is not None
    ]
    has_errors = bool(report.errors)
    has_successes = (
        bool(stages_with_data)
        and inserted >= 0
        and not all(bool(getattr(s, "errors", [])) for s in stages_with_data)
    )

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
        source_id=POLYMARKET_CYCLE_SOURCE_ID,
        status=status,
        records_ingested=inserted,
        records_skipped_duplicate=unchanged,
        duration_seconds=report.duration_seconds or 0.0,
        errors=structured_errors,
    )


def stage_summary_lines(report: PolymarketCycleReport) -> Iterable[str]:
    """Render a short per-stage summary suitable for stdout. Stable line order."""
    if report.markets is not None:
        yield (
            f"  markets:     seen={report.markets.markets_total_seen} "
            f"inserted={report.markets.markets_inserted} "
            f"removed={report.markets.markets_removed}"
        )
    if report.prices is not None:
        yield (
            f"  prices:      evaluated={report.prices.markets_evaluated} "
            f"snapshots={report.prices.snapshots_inserted}"
        )
    if report.resolutions is not None:
        yield (
            f"  resolutions: pages={report.resolutions.pages_fetched} "
            f"inserted={report.resolutions.resolutions_inserted}"
        )
    if report.trades is not None:
        yield (
            f"  trades:      evaluated={report.trades.markets_evaluated} "
            f"inserted={report.trades.trades_inserted}"
        )
    elif report.markets is not None:
        # Indicate trades intentionally skipped (no watched markets).
        yield "  trades:      (no watched markets configured)"
    if report.errors:
        for err in report.errors:
            yield f"  error:       {err}"
