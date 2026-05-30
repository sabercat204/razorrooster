# KALSHI_CONNECTOR — Design

**Subsystem:** `kalshi_connector`
**Codename:** The Stamp
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD (v1 read-only)
**Last updated:** 2026-05-15
**Companion spec:** `KALSHI_CONNECTOR.md` (Requirements v0.1.0)

---

## 1. Overview

This document specifies the technical design for `kalshi_connector` v1. It maps the requirements in `KALSHI_CONNECTOR.md` to a concrete architecture: API client, table layout, sync orchestration, sector mapping, rate limiting, the operational gates (eligibility allow-list, ToS acknowledgement), and the cross-subsystem changes that introduce a `venue` discriminator into `mispricing_detector` so a single mapping table can carry both Polymarket and Kalshi rows.

The connector is the second prediction-market venue in the system. The architecture deliberately keeps each venue as a sibling subsystem rather than refactoring an abstract `prediction_market_base` interface — with N=2, the abstraction is premature. If a third venue lands (Manifold, PredictIt, etc.), that's the time to extract.

The five discipline rules from `polymarket_connector` carry over verbatim:

1. **Source-native preservation.** Source payloads stored verbatim alongside normalized columns.
2. **Failure isolation.** Kalshi-side failure cannot corrupt or block `data_ingest`, `polymarket_connector`, or any downstream subsystem.
3. **No silent ingestion.** Cycle reports show what synced, what failed, what was skipped.
4. **No auth code paths in v1.** The codebase contains no RSA-PSS signing infrastructure, no API key loaders, no private-key path handling for Kalshi. v2 trading work will reintroduce these in a separate spec amendment with FULL threat context.
5. **Eligibility and ToS gates are non-bypassable.** The startup-time gates fail closed: misconfiguration prevents the connector from running, and the failure is loud.

A sixth rule is specific to this connector:

6. **Live/historical routing is automatic and unannounced.** The 3-month cutoff is a Kalshi internal partitioning detail; downstream code should not have to know it exists. The connector reads `/historical/cutoff` once per cycle, persists it, and routes settlement and trade backfills transparently.

## 2. Resolved Open Questions

### OQ-KSI-001 — Series → sector mapping

**Resolution:** Implement a translation layer mirroring `polymarket_sector_mapping`, with one Kalshi-specific addition: a `razor_sector = 'out_of_scope'` enum value for markets that fall entirely outside Razor's interest (Sports, Entertainment, etc.). Markets tagged out-of-scope are persisted but excluded from `signal_scanner` / `mispricing_detector` consumption.

**Reasoning:** Kalshi's catalog includes large families of markets — sports outcomes, daily entertainment, sub-day weather questions — that have no analogue in Razor's six-sector model and that the system has no reason to surface. Filtering these at ingest would lose recoverability if the operator later wants them; tagging them keeps the option open while keeping downstream surfaces clean.

**Design implications:**
- Table `kalshi_sector_mapping` (`ticker`, `razor_sector`, `secondary_sectors`, `confidence`, `mapped_at`, `mapped_by`).
- `razor_sector` values: the six existing sectors (`public_health`, `geopolitical`, `regulatory`, `commodity`, `climate`, `infrastructure_energy`, `macroeconomic`, `cross_cutting`) plus `out_of_scope`.
- The heuristic mapper consults the market's title, subtitle, category, series category, and series tags. Sports-related categories (`Sports`, sub-categories matching known sports leagues) auto-classify as `out_of_scope`.
- Operator can override via `razor-rooster kalshi map <ticker> <sector>`.
- A market mapped `out_of_scope` is **not** a candidate for downstream processing; the signal scanner skips it, the mispricing detector skips it.

### OQ-KSI-002 — WebSocket inclusion

**Resolution:** Defer to v2 with the auth layer.

**Reasoning:** Kalshi's WebSocket connections require authentication during the handshake, even for public market-data channels. Adding WebSocket support pulls the auth layer into v1, which is the line the read-only posture is holding. v1's 30-minute REST cadence is sufficient for downstream consumers (which run daily). When v2 trading lands, WebSocket comes with it as a single coherent change.

**Design implications:**
- `kalshi_price_snapshots.snapshot_source` column stays in the schema (`'rest'` for v1, `'ws'` reserved for v2+) — same forward-compatibility pattern as `polymarket_price_snapshots`.
- No `websocket` module created; `client/` is HTTP-only.
- The acceptance-test forbidden-imports check enumerates not just `cryptography.hazmat.primitives.asymmetric.padding` but also `websockets` and `aiohttp.WSMsgType` to catch accidental WebSocket pulls.

### OQ-KSI-003 — Market-type handling (binary vs scalar / categorical / strike-variants)

**Resolution:** Persist all four types faithfully. Surface only `binary` markets to downstream consumers in v1. The trigger to widen the surface in v1.2 is: an operator points a mapping at a non-binary market.

**Reasoning:** Kalshi's market hierarchy is more structured than Polymarket's. A series can produce many markets per event (a CPI series produces a `KXCPI-26AUG-T2.5`, `KXCPI-26AUG-T3.0`, etc., one per strike). Treating all four types as first-class in v1 would require schema and consumer changes throughout the pipeline. The operator's mapping intent is the cleanest signal of when the pipeline needs to widen.

**Design implications:**
- `kalshi_markets.market_type` column captures `'binary'`, `'scalar'`, `'categorical'`.
- `kalshi_markets.strike_type` captures `'above'`, `'below'`, `'between'`, `'unstructured'` for binary markets.
- The v1 sync logic persists every market regardless of type.
- The downstream-facing query helpers (those `mispricing_detector` calls into) filter `WHERE market_type = 'binary'` by default. A `--include-non-binary` operator flag is reserved for v1.2.
- When an operator runs `razor-rooster mispricing map <class_id> <ticker>` and the ticker resolves to a non-binary market, the command refuses with a clear "non-binary markets not yet supported in v1; widen the surface in v1.2" error and a reference to this design section.

### OQ-KSI-004 — Cutoff routing

**Resolution:** Snapshot `/historical/cutoff` once at the start of each cycle and persist to `kalshi_historical_cutoff`. Route all backfill paginations against that snapshot. Re-fetching mid-cycle is not done; if the cutoff advances during a backfill, the next cycle's snapshot picks up the change and routes correctly.

**Reasoning:** The cutoff advances on a Kalshi-internal schedule and may move during a long backfill. Two approaches:
1. Re-fetch the cutoff per page and re-route — adds complexity, adds API calls, and produces inconsistent behavior within a single backfill.
2. Snapshot once, accept that some markets near the boundary may be queried twice (once via live, once via historical) — costs are at most 2× the boundary-region call count, easily absorbed by the rate budget.

The second is operationally simpler and the cost is bounded. The connector's idempotent upserts make double-queries safe. Persistent cutoff snapshots also let downstream tools (calibration backtest) reproduce historical routing decisions.

**Design implications:**
- `kalshi_historical_cutoff` is a single-row table updated at every cycle's start.
- All routing decisions consult that row, not a fresh API call.
- Idempotent upserts on `kalshi_settlements` and `kalshi_trades` mean re-querying a market is a no-op rather than a duplicate write.

### OQ-KSI-005 — Settlement source recording

**Resolution:** Record the cited settlement source as free text. No archival hash, no follow-the-URL.

**Reasoning:** Kalshi cites a settlement source per market (e.g., "BLS CPI release Aug-2026"). Following the citation URL and capturing an archival hash would let a future calibration backtest verify that Kalshi resolved the market faithfully against its cited source. That's valuable but it's a calibration concern, not a connector concern; v1 records the source as text and a future calibration tool can do the verification work.

**Design implications:**
- `kalshi_settlements.settlement_source` is a `TEXT` column populated verbatim from Kalshi's response.
- No `archive.org` or `web.archive.org` integration in v1.
- A v1.x calibration backtest tool can later read `kalshi_settlements.settlement_source`, fetch the cited URL, and store an archive hash in a separate calibration-namespaced table.

### OQ-KSI-006 — Cross-venue duplicate detection

**Resolution:** Leave to `mispricing_detector` mapping. The connector's job is to surface markets faithfully; deduplication across venues belongs at the mapping layer where the operator decides whether one class maps to one venue, the other, or both.

**Reasoning:** The same underlying event (CPI print, Fed meeting, election outcome) often runs on both venues. The semantics of "duplicate" are operator-dependent — some operators want both venues' prices for arbitrage-style comparison, others want one consolidated view. The mapping layer is where operator intent is already captured.

**Design implications:**
- The connector does **not** flag duplicates.
- `class_market_mappings` (after the schema change in §3.10) supports multiple mappings per class — one for Polymarket, one for Kalshi, both active simultaneously. Each produces its own comparison row in the `mispricing_detector` cycle, and the report shows both side by side.
- A future operator-convenience CLI (`razor-rooster mispricing list-mappings --class <id>`) already enumerates all venues a class is mapped to.

### OQ-KSI-007 — Demo vs. production environment

**Resolution:** v1 reads only public production data. Demo (`external-api.demo.kalshi.co`) is unused in v1 — it's a v2 trading concern.

**Reasoning:** Public market-data endpoints are on production. The demo environment exists to let traders test order flow without risk; v1 has no order flow. Hitting demo for read-only public data would just produce a different snapshot of the universe than the operator cares about (demo markets are different from production markets).

**Design implications:**
- Configuration has only `production` base URL (`https://external-api.kalshi.com/trade-api/v2`).
- Smoke test hits production read endpoints with the same `User-Agent` discipline as Polymarket.
- v2 spec amendment will add demo configuration alongside the auth layer.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      kalshi_connector/
        __init__.py
        cli.py                          # ack-tos, status, sync, snapshot, backfill-settlements,
                                        # watch, unwatch, list-watched, fetch-orderbook,
                                        # map, needs-review, mapping-stats
        client/
          __init__.py
          rest.py                       # public REST client (no auth)
          rate_limit.py                 # token-bucket limiter with per-endpoint cost map
          retry.py                      # exponential backoff with jitter; no Retry-After header support
          user_agent.py                 # NFR-KSI-TOS-001
          endpoint_costs.py             # OQ-KSI per-endpoint token-cost map
        sync/
          __init__.py
          series.py                     # daily series enumeration
          events.py                     # daily event reconciliation
          markets.py                    # daily market metadata reconciliation
          prices.py                     # cadence-based price snapshots
          settlements.py                # daily settlement delta + initial backfill (live/historical routing)
          trades.py                     # opt-in per-market trade pull
          orderbook.py                  # on-demand orderbook pull (YES side; NO derived)
          cutoff.py                     # /historical/cutoff snapshotting
        mapping/
          __init__.py
          sector_heuristic.py           # OQ-KSI-001 heuristic mapper
          sector_overrides.py           # operator manual mappings
        gates/
          __init__.py
          eligibility.py                # REQ-KSI-ELIG-001 startup gate (allow-list)
          tos.py                        # REQ-KSI-TOS-001 startup gate
        persistence/
          __init__.py
          schemas.py                    # Kalshi-namespaced table DDL
          migrations/
            __init__.py
            m8001_kalshi_initial.py
            m8002_kalshi_indexes.py     # if needed
        config/
          kalshi.yaml                   # cadence, watched markets, rate-limit tier, freshness thresholds
          kalshi_allowed_jurisdictions.yaml
          kalshi_sector_keywords.yaml   # heuristic keyword sets (separate from polymarket's)
        tests/
          fixtures/
            series_response.json
            events_response.json
            markets_response.json
            orderbook_response.json
            settlements_live_response.json
            settlements_historical_response.json
            cutoff_response.json
            ...

### 3.2 Reuse from `data_ingest`

The connector consumes the same shared infrastructure as `polymarket_connector`:

- `DuckDBStore` and the connection-pool wrapper for persistence.
- The migrations framework for schema changes (version space 8001+).
- The staging-merge upsert pattern for batch writes.
- The provenance helpers for `last_successful_fetch` updates and freshness-view participation.
- The structured logging layer and credential-redaction filter.
- The schedule-cadence machinery — Kalshi sync is registered as a virtual "source" in the same scheduler.

Kalshi sync runs as part of the `data_ingest` cycle (operator runs `razor-rooster ingest cycle`, the cycle includes Kalshi sync after Polymarket sync). It does not run as a separate top-level command, though `razor-rooster kalshi <op>` exists for ad-hoc operations.

### 3.3 Tables (Kalshi namespace)

These nine tables live under the `kalshi_*` namespace in the same DuckDB file. They share the provenance prefix from `data_ingest` design §4 but their non-prefix columns are Kalshi-specific.

#### `kalshi_series`

    [provenance prefix from data_ingest §4]
    series_ticker             VARCHAR     PRIMARY KEY
    title                     TEXT        NOT NULL
    category                  VARCHAR     NULL              -- Kalshi-side (Economics, Climate, ...)
    frequency                 VARCHAR     NULL              -- e.g. 'daily', 'weekly', 'event-driven'
    tags                      JSON        NULL
    settlement_source         TEXT        NULL              -- e.g. "BLS CPI release"
    contract_url              VARCHAR     NULL
    created_at                TIMESTAMPTZ NULL
    last_updated_at           TIMESTAMPTZ NULL
    removed_at                TIMESTAMPTZ NULL

Index: `(category)`.

#### `kalshi_events`

    [provenance prefix]
    event_ticker              VARCHAR     PRIMARY KEY
    series_ticker             VARCHAR     NOT NULL
    title                     TEXT        NOT NULL
    sub_title                 TEXT        NULL
    category                  VARCHAR     NULL
    mutually_exclusive        BOOLEAN     NOT NULL DEFAULT FALSE
    expected_expiration_time  TIMESTAMPTZ NULL
    strike_period             VARCHAR     NULL
    status                    VARCHAR     NOT NULL          -- 'open' | 'closed' | 'settled'
    created_at                TIMESTAMPTZ NULL
    last_updated_at           TIMESTAMPTZ NULL
    removed_at                TIMESTAMPTZ NULL

Indexes: `(series_ticker)`, `(status)`, `(expected_expiration_time)`.

#### `kalshi_markets`

    [provenance prefix]
    ticker                    VARCHAR     PRIMARY KEY
    event_ticker              VARCHAR     NOT NULL
    series_ticker             VARCHAR     NOT NULL
    title                     TEXT        NOT NULL
    sub_title                 TEXT        NULL
    market_type               VARCHAR     NOT NULL          -- 'binary' | 'scalar' | 'categorical'
    strike_type               VARCHAR     NULL              -- 'above' | 'below' | 'between' | 'unstructured'
    floor_strike              DOUBLE      NULL
    cap_strike                DOUBLE      NULL
    open_time                 TIMESTAMPTZ NULL
    close_time                TIMESTAMPTZ NULL
    expiration_time           TIMESTAMPTZ NULL
    expected_expiration_time  TIMESTAMPTZ NULL
    latest_expiration_time    TIMESTAMPTZ NULL
    settlement_timer_seconds  INTEGER     NULL
    status                    VARCHAR     NOT NULL
    yes_sub_title             TEXT        NULL
    no_sub_title              TEXT        NULL
    result                    VARCHAR     NULL              -- 'yes' | 'no' | 'settled_above' | ...
    can_close_early           BOOLEAN     NULL
    expiration_value          DOUBLE      NULL
    category                  VARCHAR     NULL
    risk_limit_cents          INTEGER     NULL
    notional_value            DOUBLE      NULL
    tick_size                 DOUBLE      NULL
    last_price_dollars        DOUBLE      NULL
    previous_yes_bid_dollars  DOUBLE      NULL
    previous_yes_ask_dollars  DOUBLE      NULL
    previous_price_dollars    DOUBLE      NULL
    volume_24h                DOUBLE      NULL
    volume                    DOUBLE      NULL
    liquidity                 DOUBLE      NULL
    open_interest             DOUBLE      NULL
    created_at                TIMESTAMPTZ NULL
    last_updated_at           TIMESTAMPTZ NULL
    removed_at                TIMESTAMPTZ NULL

Indexes: `(event_ticker)`, `(series_ticker)`, `(market_type, status)`, `(expiration_time)`.

#### `kalshi_price_snapshots`

    [provenance prefix]
    ticker                    VARCHAR     NOT NULL
    snapshot_ts               TIMESTAMPTZ NOT NULL
    yes_bid_dollars           DOUBLE      NULL
    yes_ask_dollars           DOUBLE      NULL
    mid_price_dollars         DOUBLE      NULL              -- (yes_bid + yes_ask) / 2 when both present
    last_trade_price_dollars  DOUBLE      NULL
    last_trade_ts             TIMESTAMPTZ NULL
    volume_24h                DOUBLE      NULL
    volume_total              DOUBLE      NULL
    open_interest             DOUBLE      NULL
    liquidity                 DOUBLE      NULL
    liquidity_warning         BOOLEAN     NOT NULL DEFAULT FALSE
    spread_bps                INTEGER     NULL              -- (ask-bid)/mid in bps; NULL if either side missing
    snapshot_source           VARCHAR     NOT NULL          -- 'rest' (v1) | 'ws' (v2+)

Primary key: `(ticker, snapshot_ts)`.
Indexes: `(ticker, snapshot_ts DESC)`, `(snapshot_ts)`.

NO-side prices are not stored. Downstream consumers compute `no_bid_dollars = 1 - yes_ask_dollars`, `no_ask_dollars = 1 - yes_bid_dollars`. This avoids redundant data and synchronization drift.

#### `kalshi_orderbook_snapshots` (opt-in only)

    [provenance prefix]
    ticker                    VARCHAR     NOT NULL
    snapshot_ts               TIMESTAMPTZ NOT NULL
    side                      VARCHAR     NOT NULL          -- 'yes_bid' | 'yes_ask'
    level                     INTEGER     NOT NULL          -- 0 = best
    price_dollars             DOUBLE      NOT NULL
    count_fp                  DOUBLE      NOT NULL          -- contract count

Primary key: `(ticker, snapshot_ts, side, level)`.
Index: `(ticker, snapshot_ts DESC)`.

Per Kalshi's documented orderbook convention, the API returns YES-side data only; NO is derived.

#### `kalshi_trades` (opt-in via watched_markets only)

    [provenance prefix]
    trade_id                  VARCHAR     PRIMARY KEY       -- Kalshi-supplied
    ticker                    VARCHAR     NOT NULL
    created_time              TIMESTAMPTZ NOT NULL
    yes_price_dollars         DOUBLE      NOT NULL
    no_price_dollars          DOUBLE      NOT NULL          -- = 1 - yes_price_dollars
    count                     DOUBLE      NOT NULL
    taker_side                VARCHAR     NULL              -- 'yes' | 'no' if exposed by API; else NULL

Indexes: `(ticker, created_time DESC)`, `(created_time)`.

#### `kalshi_settlements`

    [provenance prefix]
    ticker                    VARCHAR     PRIMARY KEY
    event_ticker              VARCHAR     NOT NULL
    series_ticker             VARCHAR     NOT NULL
    result                    VARCHAR     NOT NULL          -- 'yes' | 'no' | 'settled_above' | 'settled_below' | 'void'
    settled_value             DOUBLE      NULL
    settlement_ts             TIMESTAMPTZ NOT NULL
    settlement_source         TEXT        NULL              -- free text
    final_yes_price           DOUBLE      NULL
    final_no_price            DOUBLE      NULL
    total_volume_at_settlement DOUBLE     NULL
    voided                    BOOLEAN     NOT NULL DEFAULT FALSE

Indexes: `(settlement_ts)`, `(series_ticker, settlement_ts)`.

#### `kalshi_historical_cutoff` (single-row state)

    market_settled_ts         TIMESTAMPTZ NOT NULL
    trades_created_ts         TIMESTAMPTZ NOT NULL
    orders_updated_ts         TIMESTAMPTZ NOT NULL
    fetched_at                TIMESTAMPTZ NOT NULL

Single row, upserted on every cycle's start (PK is implicit — there is at most one row).

#### `kalshi_sector_mapping`

    ticker                    VARCHAR     PRIMARY KEY
    razor_sector              VARCHAR     NULL              -- one of the 6 sectors, 'cross_cutting', 'out_of_scope', or NULL if unmapped
    secondary_sectors         JSON        NULL
    confidence                VARCHAR     NOT NULL          -- 'exact' | 'inferred' | 'low'
    mapped_at                 TIMESTAMPTZ NOT NULL
    mapped_by                 VARCHAR     NOT NULL          -- 'heuristic_v<n>' | 'operator'

Index: `(razor_sector)`.

### 3.4 Sync Operations

#### Cutoff snapshot (every cycle, first)

    1. GET /historical/cutoff
    2. Upsert into kalshi_historical_cutoff (single-row replace).
    3. Subsequent operations consult this row, not a fresh fetch.

#### Daily series + events + markets metadata sync (REQ-KSI-MARKET-003)

    1. Series:
       a. GET /series with cursor pagination.
       b. Upsert into kalshi_series; mark missing with removed_at.
    2. Events:
       a. For each active series, GET /events?series_ticker=...&status=open.
       b. Also pull status=closed and status=settled for events whose expected_expiration_time is within the live cutoff window.
       c. Upsert into kalshi_events; mark missing with removed_at.
    3. Markets:
       a. For each active event, GET /markets?event_ticker=... or paginate /markets?status=open.
       b. Upsert into kalshi_markets via staging-merge.
       c. Mark missing with removed_at.
    4. For each new or changed market, run the sector heuristic and upsert kalshi_sector_mapping.
    5. Update sources.last_successful_fetch for kalshi.

#### 30-min price snapshots (REQ-KSI-PRICE-001..004)

    1. Determine markets due: active binary markets per cadence config (default 30min), plus any
       watched_markets with overrides. Non-binary markets are skipped per OQ-KSI-003.
    2. For each ticker:
       a. Read snapshot fields directly from /markets?status=open response, or call /markets/{ticker}
          for individual freshness.
       b. Compute mid_price_dollars when both yes_bid and yes_ask present.
       c. Compute spread_bps and liquidity_warning.
       d. NULL-preserve missing fields.
    3. Batch upsert into kalshi_price_snapshots via staging-merge.
    4. Rate limiter applied across the entire batch.

#### Settlement backfill + daily delta (REQ-KSI-SETTLE-001..004)

The cutoff snapshot persisted at the start of the cycle dictates routing.

    Backfill (one-time):
    1. Read kalshi_historical_cutoff.market_settled_ts from this cycle's snapshot.
    2. For settlements at or after the cutoff:
       a. GET /markets?status=settled with cursor pagination.
       b. Upsert into kalshi_settlements and kalshi_markets.
    3. For settlements before the cutoff:
       a. GET /historical/markets with cursor pagination.
       b. Upsert into kalshi_settlements and kalshi_markets.
    4. Save resume cursor between pages.

    Daily delta:
    1. Pull from /markets?status=settled where settlement_ts >= sources.last_successful_fetch[kalshi_settlements].
    2. Upsert.
    3. If a market that was 'open' at last cycle is now in /historical (cutoff advanced past it),
       /markets won't include it. The delta misses it on this cycle but the next cycle's snapshot
       will route it through /historical and the resume cursor catches up.

#### Trades pull (REQ-KSI-TRADE-001..003)

Triggered only for watched_markets.

    1. For each ticker in watched_markets:
       a. Determine since: the latest kalshi_trades.created_time for that ticker.
       b. If since >= cutoff.trades_created_ts: GET /markets/trades?ticker=...&min_ts=...
          else: GET /historical/trades?ticker=...&min_ts=...
       c. Upsert into kalshi_trades keyed by trade_id (idempotent; re-pulling is a no-op).
    2. Skip markets with no new trades quickly.

#### Orderbook pull (REQ-KSI-OB-001..002)

On-demand only, invoked from CLI or by `mispricing_detector`. Returns in-memory result; persists only if `persist=True`.

### 3.5 Sector Heuristic Mapper

`mapping/sector_heuristic.py` mirrors the Polymarket implementation but with a Kalshi-specific keyword catalog and the additional `out_of_scope` enum value:

    def map_sector(market: KalshiMarket, series: KalshiSeries) -> SectorMapping:
        # Pass 1: exact category-name match.
        # Sports / Entertainment / Daily-life categories auto-classify as 'out_of_scope'.
        # Pass 2: keyword scan over title, sub_title, series.title against per-sector keyword sets.
        # Pass 3: tie-breaking; on tie, return None (operator review).
        ...

The keyword sets live in `config/kalshi_sector_keywords.yaml`. Kalshi-specific keyword examples:

- `macroeconomic`: ["CPI", "PPI", "Fed", "FOMC", "GDP", "unemployment", "jobs report", "rate cut", "rate hike"]
- `regulatory`: ["FDA", "EPA", "Congress", "Senate", "House", "executive order", "Supreme Court ruling"]
- `climate`: ["hurricane", "storm", "temperature", "snowfall", "drought", "El Niño", "La Niña"]
- `geopolitical`: ["election", "presidential", "approval rating", "war", "ceasefire"]
- `out_of_scope`: ["Super Bowl", "World Cup", "NFL", "NBA", "MLB", "NHL", "Oscar", "Emmy", "Grammy"]

(The Kalshi catalog is concrete-event-heavy compared to Polymarket; the keyword overlap with macro / regulatory / climate is tighter.)

### 3.6 Rate Limiting

Token-bucket limiter (`client/rate_limit.py`) with per-endpoint cost map.

- Default bucket capacity: 100 tokens (50% of Basic-tier 200 read tokens/sec).
- Refill rate: 100 tokens/sec.
- Per-endpoint cost map (`client/endpoint_costs.py`):

      ENDPOINT_COSTS = {
          "/series": 10,
          "/series/{series_ticker}": 10,
          "/events": 10,
          "/events/{event_ticker}": 10,
          "/markets": 10,
          "/markets/{ticker}": 10,
          "/markets/{ticker}/orderbook": 10,
          "/markets/{ticker}/candlesticks": 10,
          "/markets/trades": 10,
          "/historical/cutoff": 10,
          "/historical/markets": 10,
          "/historical/markets/{ticker}": 10,
          "/historical/markets/{ticker}/candlesticks": 10,
          "/historical/trades": 10,
      }

  The default 10-token cost matches Kalshi's documented default. The map's existence protects against future drift — when Kalshi changes a cost, it's a config edit, not a code change.

- All HTTP clients in `client/` go through a shared limiter instance. Workers acquire `cost(endpoint)` tokens before each request.
- On 429 response: drain the bucket fully (since Kalshi does not return `Retry-After`) and apply `client/retry.py` exponential backoff with jitter. Capped at 5 retries.
- Tier-aware: `config/kalshi.yaml` records the operator's tier. Operators on Advanced (300 read tokens/sec) or higher edit the tier; the limiter's bucket capacity and refill rate scale to 50% of the tier budget.

### 3.7 Eligibility Allow-list Gate

`gates/eligibility.py` runs at every connector startup:

    def check_eligibility() -> None:
        jurisdiction = (
            os.environ.get("OPERATOR_JURISDICTION")
            or load_yaml("config/operator.yaml").get("jurisdiction")
        )
        if jurisdiction is None:
            raise EligibilityRefusal(
                "OPERATOR_JURISDICTION not configured. Set the env var or "
                "config/operator.yaml jurisdiction field. kalshi_connector "
                "refuses to run without explicit jurisdiction declaration."
            )
        allowed = load_yaml("config/kalshi_allowed_jurisdictions.yaml")["allowed"]
        if jurisdiction.upper() not in {a.upper() for a in allowed}:
            raise EligibilityRefusal(
                f"Jurisdiction '{jurisdiction}' is not on the Kalshi allow-list. "
                "Kalshi is a CFTC-regulated US designated contract market and the "
                "v1 connector enforces an allow-list posture. To enable a new "
                "jurisdiction, edit config/kalshi_allowed_jurisdictions.yaml after "
                "verifying Kalshi permits participation from there."
            )

The seed allow-list is `["US"]`. The list is operator-editable so that if Kalshi expands access (or if the operator's jurisdiction is somehow non-US but valid for read access), the gate updates without a code change. The same `OPERATOR_JURISDICTION` env var that Polymarket consults is reused — operator declares once, both connectors enforce their respective postures.

The gate fails closed: missing config = refuse. The refusal message names the file the operator must edit.

### 3.8 ToS Acknowledgement Gate

`gates/tos.py` mirrors the Polymarket pattern with two Kalshi-specific changes:

1. The acknowledgement record explicitly notes the **read-only-only posture** (per REQ-KSI-TOS-002). v2 trading will require a separate, distinct acknowledgement.
2. The ToS URL is `https://kalshi.com/docs/kalshi-terms-of-service` (or whatever value `config/kalshi.yaml` records — operator-updateable when Kalshi revises).

Implementation:

    def check_tos_acknowledged(store: DuckDBStore) -> None:
        current_hash = fetch_tos_version_hash()  # SHA-256 of canonical ToS text
        ack = store.query_one(
            "SELECT tos_version_hash, acknowledged_at, acknowledged_posture "
            "FROM sources WHERE source_id = 'kalshi'"
        )
        if ack is None or ack["tos_version_hash"] != current_hash:
            raise ToSAcknowledgementRequired(
                tos_version_hash=current_hash,
                tos_url=KALSHI_TOS_URL,
                cli_command="razor-rooster kalshi ack-tos",
            )
        if ack["acknowledged_posture"] != "read_only":
            raise ToSPostureMismatch(
                f"Kalshi ToS acknowledgement is for posture "
                f"{ack['acknowledged_posture']!r} but v1 is read_only. "
                "Re-acknowledge with --posture read_only or use the v2 trading code path."
            )

`razor-rooster kalshi ack-tos` displays the current ToS URL, prompts for confirmation, and on operator confirmation writes the hash, timestamp, and posture (`'read_only'` for v1) to `sources`.

`sources` table gets a new column `acknowledged_posture VARCHAR NULL` to distinguish read-only from trading acknowledgements. v1 only writes `'read_only'`; v2 will introduce `'trading'`. The Polymarket connector's existing acknowledgement is migrated to `'read_only'` on the next data_ingest migration so the column is uniformly populated.

### 3.9 Failure Isolation

Per design rule 2: a Kalshi-side failure cannot block `data_ingest` or any other subsystem.

Implementation mirrors `polymarket_connector` §3.9:
- Kalshi sync registers as a connector in `data_ingest`'s scheduler with `source_id = 'kalshi'`.
- The scheduler's existing failure-isolation contract applies.
- Kalshi-side reads do not hold cross-table locks; their writes are confined to `kalshi_*` tables and the `sources` row for `'kalshi'`.
- A long Kalshi outage causes freshness-view staleness for the `kalshi` source row, which downstream consumers check before producing stale-data analyses.

### 3.10 Cross-Subsystem Schema Changes

Adding Kalshi requires the following changes outside `kalshi_connector` itself. These are additive (no removals, no semantic changes to existing columns) and ship as new migrations in each affected subsystem's version range.

#### `mispricing_detector` — venue discriminator

The single biggest cross-subsystem change. `class_market_mappings` and `comparisons` add a `venue` column.

**Migration `m4002_add_venue_discriminator.py`** in `mispricing_detector/persistence/migrations/`:

    -- class_market_mappings
    ALTER TABLE class_market_mappings
        ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket';

    -- The existing index on (class_id, condition_id, polarity, removed_at)
    -- is dropped and recreated to include venue, since the active-mapping
    -- uniqueness invariant is now per-venue.
    DROP INDEX IF EXISTS idx_class_market_mappings_active;
    CREATE INDEX idx_class_market_mappings_active
        ON class_market_mappings (class_id, venue, condition_id, polarity, removed_at);

    -- comparisons
    ALTER TABLE comparisons
        ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket';

    CREATE INDEX idx_comparisons_venue_class_computed
        ON comparisons (venue, class_id, computed_at);

    -- comparison_resolutions
    ALTER TABLE comparison_resolutions
        ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket';

The default `'polymarket'` migrates existing rows in place. New Kalshi rows insert with `venue = 'kalshi'`. The `condition_id` column on `comparisons` and `comparison_resolutions` is repurposed semantically as "venue-specific market identifier" — it holds Polymarket condition_ids for `venue = 'polymarket'` rows and Kalshi tickers for `venue = 'kalshi'` rows. The column is not renamed (rename is destructive and backward-incompatible); a comment in the schema records the dual meaning.

The application-level uniqueness check in `register_mapping` is updated:

    def register_mapping(class_id, market_id, venue, polarity, ...):
        # Check for existing active mapping at the same (class_id, venue, market_id, polarity).
        # Pre-Kalshi: checked (class_id, condition_id, polarity).
        # Post-Kalshi: checks (class_id, venue, market_id, polarity).

The `mispricing_detector.engines.comparator.run_cycle` queries route price + market data based on the `venue` discriminator:

    def fetch_market_state(comparison: Comparison) -> MarketState:
        if comparison.venue == 'polymarket':
            return polymarket_market_state(comparison.condition_id, comparison.outcome_token_id)
        elif comparison.venue == 'kalshi':
            return kalshi_market_state(comparison.condition_id)  # condition_id holds Kalshi ticker
        else:
            raise UnknownVenueError(comparison.venue)

`outcome_token_id` is NULL for Kalshi rows (Kalshi doesn't have outcome-token-pair semantics; YES is the only side).

#### `position_engine` — venue carry-through

`analyses` table adds a `venue` column:

**Migration `m5002_add_venue_to_analyses.py`** in `position_engine/persistence/migrations/`:

    ALTER TABLE analyses
        ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket';

    CREATE INDEX idx_analyses_venue_computed
        ON analyses (venue, computed_at);

The analyzer copies `venue` from the source comparison. Rendered analyses prefix the market identifier with the venue (e.g., `Market 0xabc... (polymarket)`, `Market KXCPI-26AUG-T2.5 (kalshi)`).

#### `monitor` — resolution detection branches

`engines/comb.py` queries route resolution detection through the venue:

    def detect_resolution(analysis: Analysis) -> ResolutionStatus:
        if analysis.venue == 'polymarket':
            return polymarket_resolution(analysis.condition_id)
        elif analysis.venue == 'kalshi':
            return kalshi_resolution(analysis.condition_id)
        else:
            raise UnknownVenueError(analysis.venue)

`follow_ups` table adds a `venue` column:

**Migration `m6002_add_venue_to_follow_ups.py`**:

    ALTER TABLE follow_ups
        ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket';

#### `report_generator` — render the venue

The surfaced, watched, calibration, and watchlist sections all show `venue` alongside the market identifier:

- **Surfaced comparisons:** `pheic_declaration_12mo (polymarket)` and `pheic_declaration_12mo (kalshi)` render as separate adjacent blocks for the same class. The report orders by `confidence_weighted_score` so the better-edge venue surfaces first.
- **Watched analyses:** the row header includes `(venue)` after the class title.
- **Calibration log:** the GFM table gets a new `Venue` column.
- **Watchlist:** unmapped scan candidates are surfaced once with a hint about which venues might host the underlying event ("Consider mapping to a Polymarket or Kalshi market").

`templates/calibration_verdicts.yaml` is unchanged — verdict text is venue-agnostic.

### 3.11 Top-level CLI Wiring

`razor_rooster/cli.py` adds `kalshi` to the click group:

    main.add_command(ingest)
    main.add_command(polymarket)
    main.add_command(pattern_library)
    main.add_command(scan)
    main.add_command(mispricing)
    main.add_command(position_engine)
    main.add_command(monitor)
    main.add_command(kalshi)         # new
    main.add_command(report)

The `razor-rooster kalshi` group exposes:

    razor-rooster kalshi ack-tos
    razor-rooster kalshi status
    razor-rooster kalshi sync
    razor-rooster kalshi snapshot [--watched|--all]
    razor-rooster kalshi backfill-settlements [--restart] [--page-size N]
    razor-rooster kalshi watch <ticker>
    razor-rooster kalshi unwatch <ticker>
    razor-rooster kalshi list-watched
    razor-rooster kalshi fetch-orderbook <ticker> [--persist]
    razor-rooster kalshi map <ticker> <sector> [--secondary <sector> ...]
    razor-rooster kalshi needs-review [--limit N]
    razor-rooster kalshi mapping-stats
    razor-rooster kalshi version

`razor-rooster mispricing map` grows a `--venue` option:

    razor-rooster mispricing map <class_id> <market_id> --venue (polymarket|kalshi) --type ... [--polarity ...]

Default venue is `polymarket` for backward compatibility. New mappings against Kalshi require `--venue kalshi` explicitly.

## 4. Sync Cadence Configuration

`config/kalshi.yaml`:

    version: 1
    base_url: "https://external-api.kalshi.com/trade-api/v2"
    tier: "Basic"                              # Basic | Advanced | Premier | Paragon | Prime
    sync:
      cutoff:
        cadence: every_cycle                   # snapshotted at the start of every data_ingest cycle
      series:
        cadence: daily
        time_of_day: "08:30"
      events:
        cadence: daily
        time_of_day: "08:35"
      markets:
        cadence: daily
        time_of_day: "08:40"
      prices:
        default_cadence: every_30min
        minimum_interval_seconds: 60
        watched_markets: []                    # populated by mispricing_detector or operator
      settlements:
        cadence: daily
        time_of_day: "08:45"
      trades:
        cadence: daily                         # only runs against watched_markets
        time_of_day: "09:00"
    rate_limit:
      tier_budget_tokens_per_sec:
        Basic: 200
        Advanced: 300
        Premier: 1000
        Paragon: 2000
        Prime: 4000
      headroom_pct: 0.5                        # target 50% of tier budget
      backoff_base_seconds: 1.0
      backoff_max_seconds: 60.0
      max_retries: 5
    freshness:
      markets_threshold_seconds: 172800        # 48h
      prices_threshold_seconds: 10800          # 3h (tighter than Polymarket's 6h since cadence is 30min)
      settlements_threshold_seconds: 172800
    sector_mapping:
      heuristic_version: 1
      keywords_file: "config/kalshi_sector_keywords.yaml"

## 5. Logging

Every sync operation emits a structured JSON log entry:

    {
      "operation": "kalshi_sync_markets",
      "started_at": "...",
      "ended_at": "...",
      "duration_seconds": ...,
      "markets_total_seen": ...,
      "markets_inserted": ...,
      "markets_updated": ...,
      "markets_removed": ...,
      "non_binary_skipped": ...,
      "rate_limit_throttle_events": ...,
      "tokens_spent": ...,                     # new vs Polymarket: tracks token consumption
      "errors": [...]
    }

The credential-redaction filter from `data_ingest` applies — there are no Kalshi credentials in v1 to redact, but the filter stays in place as defense in depth (and to catch any accidental leak from operator-side identifiers).

## 6. Threat Model

Threat context: STANDARD (v1 read-only).

Principal risks for this subsystem:

1. **Eligibility violation.** Mitigation: REQ-KSI-ELIG-001, REQ-KSI-ELIG-002 + the gate in §3.7. Verification: gate test.
2. **ToS drift.** Mitigation: ToS hash check on every startup + posture check + version-history table. Verification: simulated version-change test re-prompts.
3. **Rate-budget exhaustion.** Mitigation: 50% headroom + token-bucket limiter + per-endpoint cost map + structured warnings on throttle events.
4. **Inadvertent auth code path.** Mitigation: code review checklist explicitly forbids importing `cryptography.hazmat.primitives.asymmetric`, `websockets`, `aiohttp.WSMsgType` in v1. The acceptance test walks the package and asserts these names are absent.
5. **Cross-venue data confusion.** Mitigation: the `venue` discriminator is non-NULL on every comparison, analysis, and follow-up row. Render paths always show the venue. Tests verify the venue propagates correctly through the whole pipeline.
6. **Untrusted source content.** Kalshi market titles and subtitles are exchange-curated (more structured than Polymarket's user-generated questions) but still untrusted. The connector stores; downstream consumers treat as untrusted data per `data_ingest` threat-model rule 5.
7. **Demo/production confusion.** Mitigation: only the production base URL is in `config/kalshi.yaml`. v2 will add demo configuration alongside the auth layer, not before.

When v2 adds trading, threat context for the affected paths returns to FULL. A separate spec amendment then specifies RSA private-key handling, signing, order-entry guardrails, and the regulated-broker-relationship implications.

## 7. Test Strategy

### 7.1 Unit Tests

Per-module tests using recorded fixtures:

- REST API parsing: series, events, markets, orderbook, trades, settlements, cutoff response shapes.
- Sector heuristic mapper: representative inputs across the six sectors plus `out_of_scope` plus unmappable cases.
- Rate limiter: bucket drain/refill behavior, blocking under empty bucket, recovery after 429.
- Per-endpoint cost map: confirm the limiter consults the map and adapts when a cost changes.
- Eligibility gate: refusal for non-allowed jurisdiction, pass for allowed, refusal for missing config.
- ToS gate: refusal without ack, pass after ack, re-prompt on hash change, posture-mismatch refusal.
- NULL preservation: thin-orderbook fixture confirms NULLs and `liquidity_warning = TRUE`.
- Cutoff routing: `since` < cutoff → historical endpoint; `since` >= cutoff → live endpoint.
- NO-side derivation: confirm `no_bid = 1 - yes_ask` when both sides present.

### 7.2 Integration Tests

Against in-memory DuckDB:

- Full daily sync against a mock Kalshi API state. Confirm `kalshi_series`, `kalshi_events`, `kalshi_markets`, `kalshi_price_snapshots`, `kalshi_settlements` populated correctly.
- Re-run idempotency (no duplicate snapshots; double-queried boundary markets land once).
- Market disappearance: market in DB but absent from response → `removed_at` set.
- Settlement: market becomes settled between cycles → `kalshi_settlements` row + `kalshi_markets.status = 'settled'`.
- Backfill resume: interrupt mid-pagination, resume completes.
- Cutoff advance during backfill: confirm bounded re-query rather than missing rows.
- Failure isolation: mock 5xx on Kalshi; confirm `data_ingest`, `polymarket_connector`, and the rest of the cycle continue, Kalshi marked failed in cycle report.
- Cross-venue mapping: register the same class against both Polymarket and Kalshi; confirm `mispricing run` produces two distinct comparison rows with different `venue` values.

### 7.3 Smoke Tests

`make smoke-kalshi` runs against the live Kalshi public API:

- Fetch /historical/cutoff.
- Fetch 5 active markets from a known-stable series (KXCPI or KXFEDFUNDS).
- Snapshot prices for those 5 markets.
- Fetch 1 settled market via /historical/markets.
- Fetch one orderbook (without persist).
- Confirm rate-limit headroom not exceeded.

Skipped in CI; run locally before any release that touches this subsystem.

### 7.4 Acceptance Test

On operator hardware:

- Fresh DuckDB → Kalshi migrations applied → ToS ack gate prompts → operator acks with read-only posture.
- Eligibility gate confirms allowed jurisdiction.
- Daily sync runs end-to-end inside NFR-KSI-PERF-001 (5 min).
- Settlement backfill (live + historical) runs inside NFR-KSI-PERF-002 (12 hr).
- Steady-state disk usage stays inside NFR-KSI-PERF-003 (5 GB).
- Simulated outage: disable network mid-cycle; confirm graceful degradation.
- Forbidden-imports test: walk `kalshi_connector` package, assert `cryptography.hazmat.primitives.asymmetric.padding`, `websockets`, `aiohttp.WSMsgType` are not imported.

## 8. Operational Model

### 8.1 First-run

    razor-rooster kalshi ack-tos                           # one-time gate
    razor-rooster kalshi sync                              # initial sync
    razor-rooster kalshi backfill-settlements              # initial historical pull
    razor-rooster ingest cycle                             # includes Kalshi sync from now on

### 8.2 Steady-state

Kalshi sync runs as part of the daily `razor-rooster ingest cycle`. No separate cron entry needed.

### 8.3 Watched markets

    razor-rooster kalshi watch <ticker>
    razor-rooster kalshi unwatch <ticker>
    razor-rooster kalshi list-watched

### 8.4 Sector mapping triage

    razor-rooster kalshi needs-review                      # lists unmapped or low-confidence markets
    razor-rooster kalshi map <ticker> <sector>             # operator override

### 8.5 ToS re-acknowledgement

When ToS hash changes, the next sync attempt fails with a clear message instructing the operator to re-run `razor-rooster kalshi ack-tos`. No automatic re-acknowledgement.

### 8.6 Cross-venue mapping

When the same underlying event is hosted on both venues, the operator can map a class to both:

    razor-rooster mispricing map cpi_above_target_aug26 0xabcdef --venue polymarket --type direct
    razor-rooster mispricing map cpi_above_target_aug26 KXCPI-26AUG-T2.5 --venue kalshi --type direct

The mispricing cycle then produces two comparisons per cycle, the position engine produces two analyses, and the report renders both side by side. The operator decides which (if either) is more attractive.

## 9. Performance Notes & Risks

- **Sync scale.** Kalshi has fewer active markets than Polymarket at any given moment but a higher event-to-market multiplier (CPI series produces ~10 markets per event). Daily metadata sync against the v1 universe is comfortably inside 5 minutes.
- **Snapshot scale.** At 30-min cadence and ~500 active binary markets, each snapshot pulls 500 rows. Inside the Basic-tier 50% headroom (100 read tokens/sec, 10 tokens/request → 10 req/sec), this completes in ~50 seconds plus latency.
- **Settlement backfill scale.** Kalshi's full settled-market history is shorter than Polymarket's (Kalshi's CFTC designation date is more recent), so backfill is bounded.
- **Cutoff awareness latency.** `/historical/cutoff` is a single cheap call at the start of every cycle. Negligible overhead.
- **Risk: tier downgrade for inactivity.** Kalshi documentation notes that tiers may be downgraded for prolonged inactivity. The connector's `tier` config setting is operator-declared; if the operator's actual tier is lower than declared, requests will hit 429s sooner than expected. Mitigation: the limiter's 50% headroom absorbs minor mismatches; gross mismatches surface as 429 patterns in the structured logs and the operator can reduce the declared tier.
- **Risk: API surface change.** Kalshi has migrated APIs in the past (v1 → v2) and may again. The connector's defensive parsing tolerates unknown fields (preserved verbatim in `source_payload_json`); a breaking change in the documented surface causes the connector to fail loud.
- **Risk: ToS terms changing the read-access posture.** If Kalshi changes its ToS to require authentication for currently-public read endpoints, the v1 connector breaks. The ToS-hash gate catches the version change before the connector keeps running on stale assumptions.
- **Risk: cross-venue mapping ambiguity.** When the same class is mapped to both venues with different polarities or strike levels, the operator may end up with comparisons that look superficially similar but are semantically different. Mitigation: the `venue` is always rendered in surfacing output, and the mapping CLI rejects same-(class, venue, condition_id, polarity) duplicates. Operator-error mappings are recoverable via `unmap`.

## 10. Deferred to Implementation

- **DEFER-KSI-001:** Initial sector keyword sets for Kalshi — start with a small curated list per sector (less than Polymarket's, since Kalshi's titles are more formal) and expand based on `needs-review` triage feedback. The keyword file is config, so this is iterative.
- **DEFER-KSI-002:** Watched-markets cadence floor — REQ-KSI-PRICE-003 sets a 60-second minimum; in practice, validate this stays inside rate budget when multiple watched markets sync at the same cadence under Basic tier. Adjust if needed.
- **DEFER-KSI-003:** Settlement backfill exact pagination strategy — Kalshi's cursor pagination is documented but boundary-case edge handling (settlements that cross the cutoff during a long backfill) needs empirical verification on operator hardware. The design above accepts double-queries; verify the cost is bounded.
- **DEFER-KSI-004:** Settlement-source archival — currently free text per OQ-KSI-005. If the calibration backtest shows that disagreements between Kalshi resolutions and the cited sources are common, a v1.x archival tool will be added.
- **DEFER-KSI-005:** Multi-outcome surface widening — currently binary-only per OQ-KSI-003. The trigger to widen is an operator mapping pointing at a non-binary market. The widening work is its own spec amendment when the trigger fires.

## 11. References

- Requirements: `KALSHI_CONNECTOR.md` v0.1.0
- Sibling subsystem: `POLYMARKET_CONNECTOR.md` v0.1.0 + `POLYMARKET_CONNECTOR_DESIGN.md` v0.1.0 (this design mirrors its structure)
- `data_ingest` Design v0.1.0 — for the freshness / provenance / staging-merge / scheduler contracts
- `mispricing_detector` Design v0.1.0 — for the existing mapping table that gains the `venue` discriminator
- `position_engine` Design v0.1.0 — for the analyses table that gains `venue`
- `monitor` Design v0.1.0 — for the follow_ups table that gains `venue`
- `report_generator` Design v0.1.0 — for the section assemblers that render `venue`
- Kalshi public documentation: docs.kalshi.com — Quick Start: Market Data, Quick Start: Authenticated Requests (read but not implemented in v1), Rate Limits and Tiers, Historical Data, Orderbook Responses
- LOOM v0.35.1 — `razorrooster.md`. v0.36.0 will record the addition of `kalshi_connector` to the subsystem registry, the cross-subsystem `venue` discriminator migration, and the data_ingest `acknowledged_posture` column.

Content drawn from external sources is paraphrased per licensing constraints.
