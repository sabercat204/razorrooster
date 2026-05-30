# KALSHI_CONNECTOR — Requirements

**Subsystem:** `kalshi_connector`
**Codename:** The Stamp
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD (read-only v1; returns to FULL if v2+ adds trading)
**Last updated:** 2026-05-15

---

## 1. Purpose

`kalshi_connector` is the read-only Kalshi data ingestion layer for Razor-Rooster. It is responsible for:

- Discovering the universe of currently active and recently settled Kalshi markets, events, and series.
- Pulling live and historical pricing for those markets.
- Pulling settled-market resolution data for backtesting and calibration.
- Persisting that data into the local DuckDB store maintained by `data_ingest`, conforming to the same source-namespaced pattern `polymarket_connector` established (`kalshi_*` tables alongside the `polymarket_*` ones, neither encroaching on `data_ingest`'s four canonical schemas).

Downstream consumers — same as Polymarket:
- `mispricing_detector` reads live market-implied probabilities to compare against model probabilities. Class-to-market mappings grow a `venue` discriminator so a single class can map to a Polymarket market, a Kalshi market, or both.
- `pattern_library` reads settled markets to calibrate base-rate priors and validate model outputs.
- `monitor` reads price movement on Kalshi markets mapped to active analyses.
- `report_generator` surfaces venue alongside the market identifier in every section.

`kalshi_connector` does not place orders, hold positions, or sign authenticated requests in v1. Authentication, RSA-PSS signing, and the trading API surface are explicitly out of scope (see §3). Read-only public market data does not require authentication and is the entire v1 surface.

This is the second prediction-market venue in the system. The architecture deliberately keeps each venue as a sibling subsystem rather than refactoring an abstract `prediction_market_base` interface — with N=2, the abstraction is premature. If a third venue lands, that's the time to extract.

## 2. Scope

### In scope (v1)

- Read-only access to Kalshi's public REST endpoints at `https://external-api.kalshi.com/trade-api/v2`:
  - `/series` — enumerate the catalogue of recurring contract families.
  - `/events` — enumerate events within series, with their nested markets.
  - `/markets` — enumerate active and recently settled markets (subject to the live/historical cutoff).
  - `/markets/{ticker}` — fetch one market's full metadata.
  - `/markets/{ticker}/orderbook` — fetch order-book depth on demand.
  - `/markets/{ticker}/candlesticks` — fetch OHLC-shape historical price bars when needed.
  - `/markets/trades` — fetch recent trade history.
  - `/historical/cutoff` — read the live/historical boundary timestamps.
  - `/historical/markets`, `/historical/markets/{ticker}`, `/historical/markets/{ticker}/candlesticks`, `/historical/trades` — backfill data older than the live cutoff.
- Backfill of settled-market history for backtesting, gated only by Kalshi's own retention and by our 100 GB global corpus cap.
- Mapping of Kalshi markets to event-class taxonomies used by `pattern_library`. Many Kalshi markets correspond directly to event classes the library tracks (CPI prints, Fed meetings, weather thresholds); others won't fit.
- Provenance tracking consistent with `data_ingest` and matching the pattern `polymarket_connector` set.
- Token-bucket rate-limit-aware client respecting Kalshi's published Basic-tier read budget (200 tokens/sec, default 10 tokens/request → effective 20 read req/sec). Targets ≤50% of the budget by default.
- Eligibility gate: refuse to start if the operator's declared jurisdiction is outside the configurable allow-list (US-only by default, since Kalshi is a CFTC-regulated US DCM). Inverts the Polymarket geo-restriction pattern (deny-list).
- Terms-of-service acknowledgement gate matching the `polymarket_connector` pattern: hash the live ToS, persist on first-run acknowledgement, re-prompt on hash change.

### Out of scope (explicit)

- **Order placement, position management, balance management, fills, RFQ.** Trading and authenticated portfolio endpoints are v2+; the v1 connector is read-only and never holds an RSA private key.
- **RSA-PSS request signing.** No signing logic in v1. The cryptography library is not imported by `kalshi_connector` v1 source files (verified by acceptance test).
- **API-key configuration.** `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` env vars are not read in v1. The auth code path that would consume them does not exist.
- **WebSocket subscriptions.** Kalshi WebSocket connections require authentication during the handshake even for public market data channels. v1 polls REST. WebSocket is deferred to v2 alongside the auth layer.
- **FIX gateway.** Order entry over FIX requires authenticated sessions; out of scope for v1 by extension.
- **Sub-penny pricing rules and order-entry math.** v1 reads prices as published; the sub-penny-pricing semantics matter only for order placement.
- **Settlement-source verification.** The settlement source is recorded for context (calibration log) but the connector does not independently verify Kalshi's resolution against the cited source. That's a downstream calibration-backtest concern.
- **Off-platform liquidity.** v1 is Kalshi-and-Polymarket. Manifold, PredictIt, etc. are separate specs.

## 3. Threat Context Reassessment

The LOOM does not yet record `kalshi_connector` because it is being added in v1.1. With v1 scope locked to read-only public data only:

- No credentials are stored.
- No transactions can be initiated.
- No CFTC-registered trading account is exercised by this code path.

The threat context for v1 is therefore **STANDARD**, matching `data_ingest` and `polymarket_connector`. If and when trading is added (v2+), the threat context for the affected code paths returns to FULL and a separate spec amendment captures RSA private-key handling, signing, order-entry guardrails, and the regulated-broker-relationship implications at that time.

The downgrade rationale is identical to OT-001's resolution for Polymarket: the codebase contains no signing imports, no key paths, no balance reads, no order endpoints. The acceptance test enforces this by walking the package and asserting `cryptography.hazmat.primitives.asymmetric.padding` does not appear as an imported symbol.

This threat context is recorded in the LOOM in the v0.36.0 evolution-log entry that accompanies this spec.

## 4. Stakeholders & Operating Assumptions

- **Operator:** single user, US-resident (per the eligibility gate), running on EliteBook G8 alongside the rest of the system.
- **Network:** intermittent residential connectivity. Pulls must tolerate transient failures and resume.
- **Cadence:** live pricing wants near-real-time updates (REST polling every 15–60 minutes for the active universe; configurable per-market for watched markets). Market metadata (existence, settlement status) updates daily. Settled-market backfill runs once initially and then incrementally as new markets settle.
- **Kalshi rate budget:** Basic tier is 200 read tokens/sec at 10 tokens per default request → effective 20 read req/sec. Razor-Rooster's plausible workload sits well inside this; the connector targets ≤50% headroom by default.
- **Eligibility:** the operator has confirmed they are in a jurisdiction where Kalshi participation is permitted. v1 is read-only so the eligibility question is technically softer (publicly readable data is publicly readable), but the gate is enforced anyway because:
  1. It mirrors the Polymarket pattern (consistency for the operator).
  2. v2+ trading will require it, and building the gate now makes the v2 cutover smaller.
  3. The ToS acknowledgement (§6.8) reads as part of the same workflow.

## 5. Data Surface Inventory

The connector ingests these data classes from Kalshi:

| Data class | Source endpoint family | Cadence | Schema target |
|---|---|---|---|
| Series catalogue | `/series` | Daily | `kalshi_series` (new) |
| Active events | `/events` | Daily | `kalshi_events` (new) |
| Active markets metadata | `/markets`, `/markets/{ticker}` | Daily | `kalshi_markets` (new) |
| Live YES bid/ask + spread | `/markets` (snapshot fields) and `/markets/{ticker}/orderbook` | 30–60 min default; per-market overrides | `kalshi_price_snapshots` (new) |
| Order book depth | `/markets/{ticker}/orderbook` | On-demand for analyses | `kalshi_orderbook_snapshots` (new) |
| Trade history | `/markets/trades`, `/historical/trades` | Daily for watched markets | `kalshi_trades` (new) |
| Settled markets | `/markets` (status=settled) + `/historical/markets` | Backfill once + daily delta | `kalshi_settlements` (new) |
| Live/historical cutoff | `/historical/cutoff` | Each cycle (cheap) | `kalshi_historical_cutoff` (new, single-row state) |
| Candlesticks (optional) | `/markets/{ticker}/candlesticks` and `/historical/markets/{ticker}/candlesticks` | On-demand | `kalshi_candlesticks` (new) — opt-in like Polymarket trades |

Same posture as `polymarket_connector`: Kalshi data does not map onto `data_ingest`'s four canonical schemas because the series → event → market → ticker hierarchy and the YES-orderbook-only convention have no canonical analogue. The connector therefore introduces eight new Kalshi-namespaced tables, all source-specific (not new canonical schemas), all under schema-migration version space 8001+.

## 6. Functional Requirements

Requirements use EARS-style phrasing with stable IDs and verification notes. KSI = `kalshi_connector`.

### 6.1 Series and event discovery

**REQ-KSI-SERIES-001: Series enumeration**
The connector **shall** provide a `list_series()` operation returning all series the public `/series` endpoint exposes, with their tickers, titles, frequency, and category.
*Verification:* fixture-based unit test confirms parsing of a representative `/series` response. Smoke test against the live API returns a non-empty list including known series like `KXHIGHNY`.

**REQ-KSI-SERIES-002: Series persistence**
Series metadata **shall** be persisted to a `kalshi_series` table with at minimum: `series_ticker` (primary), `title`, `category`, `frequency`, `tags` (JSON), `settlement_source` (free text where the API exposes it), `contract_url`, `created_at`, `last_updated_at`.
*Verification:* schema migration produces the table; round-trip test stores and queries a representative series.

**REQ-KSI-EVENT-001: Event enumeration**
The connector **shall** provide a `list_events(series_ticker=None, status=None)` operation returning events within an optional series, optionally filtered by status (`open`, `closed`, `settled`).
*Verification:* fixture-based unit test; smoke test confirms a non-empty list against the live API.

**REQ-KSI-EVENT-002: Event persistence**
Event metadata **shall** be persisted to a `kalshi_events` table with at minimum: `event_ticker` (primary), `series_ticker`, `title`, `category`, `mutually_exclusive` (boolean), `expected_expiration_time`, `strike_period`, `status`, `created_at`, `last_updated_at`.
*Verification:* schema migration; round-trip test.

### 6.2 Market discovery and metadata

**REQ-KSI-MARKET-001: Active market enumeration**
The connector **shall** provide a `list_active_markets(series_ticker=None, event_ticker=None)` operation returning all currently active (status `open` or recently settled within the live cutoff) Kalshi markets with their metadata.
*Verification:* fixture test; smoke test returns a non-empty list.

**REQ-KSI-MARKET-002: Market metadata persistence**
Active market metadata **shall** be persisted to a `kalshi_markets` table with at minimum: `ticker` (primary), `event_ticker`, `series_ticker`, `title`, `subtitle`, `market_type` (`binary`, `scalar`, `categorical`), `strike_type` (`above` / `below` / `between` / `unstructured` for binary), `floor_strike`, `cap_strike`, `open_time`, `close_time`, `expiration_time`, `expected_expiration_time`, `latest_expiration_time`, `settlement_timer_seconds`, `status`, `yes_sub_title`, `no_sub_title`, `result`, `can_close_early`, `expiration_value`, `category`, `risk_limit_cents`, `notional_value`, `tick_size`, `last_price_dollars`, `previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `previous_price_dollars`, `volume_24h`, `volume`, `liquidity`, `open_interest`, `created_at`, `last_updated_at`, `removed_at`.
*Verification:* schema migration; round-trip test stores and queries a representative market across each `market_type`.

**REQ-KSI-MARKET-003: Daily market sync**
The connector **shall** support a daily sync that reconciles the local `kalshi_markets` table with Kalshi's current state across all sectors of interest: insert new markets, update changed metadata (close-time extensions, status changes), mark missing markets as `removed_at = <timestamp>` rather than deleting rows. Soft-delete only.
*Verification:* integration test runs a sync against a mock API state, alters the mock state, runs a second sync, confirms expected diffs and that no historical rows are destroyed.

**REQ-KSI-MARKET-004: Market-type representation**
The schema **shall** represent all four Kalshi market types — `binary` (YES/NO), `scalar` (price/threshold pairs), `categorical` (mutually-exclusive multi-outcome), and the `above`/`below`/`between` strike variants of binary — without lossy collapsing. Mappings, comparisons, and analyses in v1 are expected to consume binary markets only; non-binary markets are read and persisted but not surfaced to downstream consumers until v1.2.
*Verification:* schema-validation test confirms all four types round-trip; downstream-consumer test confirms `mispricing_detector` ignores non-binary markets in v1.

### 6.3 Pricing

**REQ-KSI-PRICE-001: Price snapshot pull**
The connector **shall** provide a `snapshot_prices(tickers)` operation that returns YES bid/ask, last trade price, mid-price (bid+ask/2), volume_24h, and open interest for each requested market.
*Verification:* fixture test; smoke test returns plausible values (0 ≤ bid ≤ ask ≤ 1, last trade in [bid, ask] when both present).

**REQ-KSI-PRICE-002: Price snapshot persistence**
Price snapshots **shall** be persisted to a `kalshi_price_snapshots` table with: `ticker`, `snapshot_ts`, `yes_bid_dollars`, `yes_ask_dollars`, `mid_price_dollars`, `last_trade_price_dollars`, `last_trade_ts`, `volume_24h`, `volume_total`, `open_interest`, `liquidity`, `liquidity_warning` (boolean — set when bid-ask spread exceeds the configurable threshold, default 5¢ on the $1 contract), `spread_bps`, `snapshot_source` (`rest` for REST polls; `ws` reserved for v2+).
*Verification:* schema migration; round-trip test.

**REQ-KSI-PRICE-003: Configurable snapshot cadence**
The connector **shall** support configurable per-market snapshot cadence. Default is 30 minutes for the active universe (Kalshi liquidity profiles often change slowly between events; tighter cadence wastes the rate budget). A small set of `watched_markets` mapped to active analyses **may** be configured for higher-frequency snapshots (down to a 60-second floor).
*Verification:* config-driven test; rate-budget test confirms default cadence keeps total request rate below 50% of Basic-tier read budget.

**REQ-KSI-PRICE-004: Sparse-orderbook tolerance**
For markets with one-sided or absent quotes (no YES bid, no YES ask, or both), the connector **shall** persist NULL values rather than synthesizing prices. `liquidity_warning` is set when either side is NULL or the spread exceeds the threshold. NO prices are derivable from YES (`no_bid = 1 - yes_ask`) but are not stored separately to avoid redundant data and synchronization drift; downstream consumers compute the NO side from the YES quote.
*Verification:* fixture test with a thin-orderbook response confirms NULLs and warning flag.

### 6.4 Order books

**REQ-KSI-OB-001: Order book depth on demand**
The connector **shall** provide a `fetch_orderbook(ticker, depth_levels=10)` operation returning the requested market's YES bid and YES ask sides to the requested depth. Per Kalshi's documented orderbook convention, only the YES side is returned by the API; NO bids are derivable from YES asks (`no_bid_price = 1 - yes_ask_price`, `no_bid_size = yes_ask_size`).
*Verification:* fixture test with the sample response from the docs confirms parsing; smoke test against a live liquid market returns non-empty depth.

**REQ-KSI-OB-002: Order book snapshots are not auto-persisted by default**
Same posture as Polymarket REQ-PMC-OB-002. Snapshots **shall not** be persisted on a schedule; only when an explicit `persist=True` flag is passed. Default returns the snapshot in-memory.
*Rationale:* full orderbook history at scale would blow the corpus cap.
*Verification:* unit test confirms default does not write; opt-in test confirms persistence path works.

### 6.5 Settlements and historical outcomes

**REQ-KSI-SETTLE-001: Settled market enumeration**
The connector **shall** provide a `list_settled_markets(since=None)` operation returning markets settled since the given timestamp. Below the live cutoff, the operation queries `/markets?status=settled`. Above the cutoff, it queries `/historical/markets`. The cutoff is read each cycle from `/historical/cutoff` and persisted to `kalshi_historical_cutoff` so the operation can route correctly.
*Verification:* fixture test exercises both routing branches; smoke test with `since=<7 days ago>` returns a plausible list.

**REQ-KSI-SETTLE-002: Settlement persistence**
Settled-market metadata **shall** be persisted to a `kalshi_settlements` table with: `ticker` (primary), `event_ticker`, `series_ticker`, `result` (`yes` / `no` / `settled_above` / `settled_below` / `void`), `settled_value`, `settlement_ts`, `settlement_source` (free text describing the cited authority — e.g., "BLS CPI release Aug-2026"), `final_yes_price`, `final_no_price`, `total_volume_at_settlement`, `voided` (boolean).
*Verification:* schema migration; round-trip test.

**REQ-KSI-SETTLE-003: Settlement backfill**
The connector **shall** support a one-time `backfill_settlements()` operation that pulls the full history of settled Kalshi markets to the depth Kalshi exposes via the historical endpoints. Subsequent daily syncs append new settlements via the live `/markets?status=settled` route until the cutoff advances past them.
*Verification:* integration test against a mock with a known set of historical settlements (split across the cutoff) confirms full pull. Smoke test runs a real backfill on a fresh DuckDB and confirms a non-trivial number of settlements land.

**REQ-KSI-SETTLE-004: Cutoff awareness**
The connector **shall** read `/historical/cutoff` at the start of each cycle and persist the current `market_settled_ts`, `trades_created_ts`, and `orders_updated_ts` values to `kalshi_historical_cutoff`. Routing decisions for settlement and trade backfills consult this row rather than re-fetching mid-cycle.
*Verification:* fixture test confirms cutoff is read once per cycle; routing-test confirms `since` < cutoff routes to historical endpoints, `since` ≥ cutoff stays on live endpoints.

### 6.6 Trade history

**REQ-KSI-TRADE-001: Per-market trade history pull**
The connector **shall** provide a `pull_trades(ticker, since)` operation returning all trades on a given market since the given timestamp. Below the cutoff, queries `/historical/trades`; above, `/markets/trades`.
*Verification:* fixture test for both branches; smoke test for a recent watched market.

**REQ-KSI-TRADE-002: Trade persistence**
Trades **shall** be persisted to a `kalshi_trades` table with: `trade_id` (primary, from Kalshi), `ticker`, `created_time`, `yes_price_dollars`, `no_price_dollars`, `count`, `taker_side` (`yes` or `no` where determinable, else NULL).
*Verification:* schema migration; round-trip test.

**REQ-KSI-TRADE-003: Trade pull is not the default**
Trade history **shall not** be pulled on the daily cycle by default. It **shall** be pulled only for markets explicitly registered in a `watched_markets` config or invoked manually by the operator. Same rationale as REQ-PMC-TRADE-003.
*Verification:* config-driven test confirms default daily cycle does not pull trades unless markets are watched.

### 6.7 Rate limiting and resilience

**REQ-KSI-RATE-001: Token-bucket rate limiter**
The connector **shall** apply a token-bucket rate limiter calibrated to stay below 50% of the operator's tier read budget. Default targets ≤100 read tokens/sec (50% of Basic). Configurable to higher tiers as the operator's account is upgraded; the connector reads the configured tier from `config/kalshi.yaml`.
*Verification:* unit test confirms the limiter blocks requests when the bucket is empty and refills correctly; integration test under simulated heavy load confirms request rate stays under budget.

**REQ-KSI-RATE-002: Rate-limit response handling**
On any 429 response from Kalshi, the connector **shall** apply exponential backoff with jitter, capped at 5 retries, and **shall** log structured warnings on each retry. Per Kalshi documentation, 429 responses do **not** include `Retry-After` or `X-RateLimit-*` headers; the connector relies entirely on its own backoff schedule. Persistent rate-limit failures after retries **shall** be surfaced to the cycle report.
*Verification:* fixture-based test injects 429 responses and confirms backoff behavior.

**REQ-KSI-RATE-003: Token-cost awareness per endpoint**
The connector **shall** maintain a per-endpoint token-cost map (sourced from Kalshi's published costs and updated as documentation changes), and **shall** debit the bucket by the endpoint's actual cost rather than always assuming the 10-token default. Endpoints with discounted costs (single-order reads, etc., per Kalshi docs) do not apply to v1's read-only surface, but the cost map's existence protects against future drift.
*Verification:* unit test confirms cost map is consulted; integration test simulates a cost change and confirms the limiter adapts.

**REQ-KSI-AVAIL-001: Failure isolation from data_ingest and polymarket_connector**
A failure in `kalshi_connector` **shall not** halt or corrupt `data_ingest` or `polymarket_connector`. All three subsystems share the DuckDB store but operate on disjoint table sets and do not hold cross-subsystem locks beyond DuckDB's normal transaction semantics.
*Verification:* integration test forces a Kalshi-side failure and confirms the other two subsystems' cycles continue and persist correctly.

### 6.8 Eligibility and ToS compliance

**REQ-KSI-ELIG-001: Allowed-jurisdiction gate**
The connector **shall** read the `OPERATOR_JURISDICTION` environment variable (or `config/operator.yaml` entry) at startup and compare against `config/kalshi_allowed_jurisdictions.yaml`. If the value is **not** in the allow-list, the connector **shall** refuse to start with a typed `EligibilityRefusal` error.
*Verification:* unit test confirms refusal for a non-allowed value; passes for an allowed value (e.g., `US`). The same `OPERATOR_JURISDICTION` env var that Polymarket reads is reused — the operator declares once, both connectors enforce their own posture.

**REQ-KSI-ELIG-002: No VPN circumvention support**
Same as Polymarket REQ-PMC-GEO-002. The connector **shall not** route through proxies. Direct HTTPS to `external-api.kalshi.com` only.
*Verification:* code review confirms no proxy-configuration code paths exist.

**REQ-KSI-TOS-001: Terms-of-service acknowledgement on startup**
On first run, the connector **shall** require the operator to acknowledge Kalshi's Terms of Service (record acknowledgement timestamp and the ToS version hash in the `sources` table). On subsequent runs, the connector **shall** check the recorded version and re-prompt if the ToS version has changed. The ToS URL is `https://kalshi.com/docs/kalshi-terms-of-service` (or the URL recorded in `config/kalshi.yaml` if Kalshi changes it).
*Verification:* first-run integration test confirms acknowledgement gate; second run with same ToS version proceeds; simulated version change re-prompts.

**REQ-KSI-TOS-002: Read-only ToS posture**
The acknowledgement text **shall** record explicitly that the v1 acknowledgement covers read-only data access only, not trading. v2+ trading will require a separate, distinct acknowledgement at FULL threat context.
*Verification:* acknowledgement record contains the read-only-posture marker; v2+ spec amendment is referenced in the record's `notes` field.

### 6.9 Provenance and freshness

**REQ-KSI-PROV-001: Per-record provenance**
Every Kalshi-sourced record **shall** carry: source identifier (`kalshi`), source-side ID (Kalshi's `ticker`, `event_ticker`, `series_ticker`, or `trade_id` as appropriate), fetch timestamp, source publication timestamp where the API exposes it (e.g., market `last_updated_at`), and connector version.
*Verification:* DuckDB query returns full provenance for any record.

**REQ-KSI-PROV-002: Kalshi entry in freshness view**
The `freshness` view defined by `data_ingest` **shall** include `kalshi` as a registered source with last-successful-fetch tracking and a freshness threshold (default: 6 hours for live data, 48 hours for settlements).
*Verification:* freshness query returns Kalshi entry after a sync; threshold checked.

### 6.10 Sector mapping

**REQ-KSI-SECTOR-001: Per-market sector classification**
The connector **shall** classify each Kalshi market into one of the six Razor sectors (Public Health, Energy & Climate, Conflict & Geopolitics, Macroeconomic, Regulatory & Legal, Climate Disasters) using a heuristic over the market's title, subtitle, category, and series category, with operator-curated overrides taking precedence.
*Verification:* fixture test confirms the heuristic classifies a known set of markets correctly; operator-override test confirms manual mapping wins.

**REQ-KSI-SECTOR-002: Sector mapping persistence**
Per-market sector classifications **shall** be persisted to a `kalshi_sector_mapping` table with: `ticker` (primary), `razor_sector`, `secondary_sectors` (JSON), `confidence` (`exact` / `inferred` / `low`), `mapped_at`, `mapped_by` (`operator` / `auto`).
*Verification:* schema migration; round-trip test.

**REQ-KSI-SECTOR-003: Operator review surface**
Markets the heuristic cannot classify (or classifies with `low` confidence) **shall** surface in a `razor-rooster kalshi needs-review` CLI listing for operator triage. The operator can record a manual mapping that is preserved across subsequent cycles.
*Verification:* CLI integration test against a synthetic state with mixed-confidence markets.

### 6.11 Logging and observability

**REQ-KSI-LOG-001: Structured per-operation logging**
Each connector operation (sync, snapshot, backfill) **shall** emit a structured JSON log entry consistent with `data_ingest` REQ-LOG-001 conventions, with operation type, market count touched, duration, token spend, and any errors.
*Verification:* log inspection confirms entry structure including the `tokens_spent` field.

**REQ-KSI-LOG-002: No PII leakage**
Logs **shall not** contain account identifiers, API key IDs, private-key paths, or any operator-side identifiers beyond the local username. Kalshi's public market data does not contain operator PII; this requirement is a forward-looking guard for the v2+ trading expansion.
*Verification:* log-scan test against synthetic operator identifiers confirms redaction.

## 7. Non-Functional Requirements

**NFR-KSI-PERF-001:** A daily metadata sync of all active Kalshi markets **shall** complete within 5 minutes on the operator's hardware and network. (Kalshi's active-market count is typically smaller than Polymarket's; this should be comfortable.)

**NFR-KSI-PERF-002:** A full settlement backfill (live + historical) **shall** complete within 12 hours. If exceeded, the connector logs a warning and the design phase reconsiders pagination strategy.

**NFR-KSI-PERF-003:** Steady-state Kalshi-side disk usage (markets + events + series + settlements + 30-min price snapshots for the active universe) **shall** stay under 5 GB out of the 100 GB global cap. Trade-history and orderbook persistence (opt-in, watched-markets-only) is excluded from this budget and managed via per-source caps.

**NFR-KSI-AVAIL-001:** Kalshi-side connector failures **shall** degrade gracefully — `mispricing_detector` consumers see stale-but-flagged data (via the freshness view) rather than crashes.

**NFR-KSI-SEC-001:** No Kalshi-side credentials are configured in v1 (per scope). The connector code path that would load them does not exist. When v2+ adds trading, NFR-KSI-SEC-001 is amended to specify private-key handling at FULL threat context.

**NFR-KSI-TOS-001:** Each connector run that hits Kalshi APIs **shall** include a `User-Agent` header identifying the application (e.g., `razor-rooster-kalshi/0.1 (research; +operator-contact)`), per the common convention for polite API use even when the source does not require it.

## 8. Open Questions (carry to design phase)

- **OQ-KSI-001:** Series → sector mapping — does Kalshi's category taxonomy map cleanly to Razor's six sectors, or does the heuristic need a translation table similar to Polymarket's? Some Kalshi categories (e.g., "Sports") fall outside Razor's interest entirely; the design specifies whether out-of-scope markets are filtered before persistence or persisted-and-tagged.
- **OQ-KSI-002:** WebSocket — defer to v2 with the auth layer, or stand up a minimal authenticated read-only client now to capture sub-minute price moves? Default disposition: defer to v2; the existing 30-minute REST cadence is sufficient for v1's downstream consumers (which themselves run daily).
- **OQ-KSI-003:** Market-type handling — binary markets are first-class in v1; scalar / categorical / strike-variant binary markets are persisted but not surfaced. When does v1.2 widen the surface? Default disposition: when an operator-curated mapping points at a non-binary market, treat that as the trigger for v1.2 work.
- **OQ-KSI-004:** Cutoff routing — Kalshi advances the live/historical cutoff over time. A backfill that starts before a cutoff advance and ends after will straddle. The design specifies the routing reconciliation: re-read the cutoff per page, or rely on the start-of-cycle snapshot.
- **OQ-KSI-005:** Settlement source recording — Kalshi cites a settlement source per market. Should the connector follow the citation URL and capture an archival hash for later verification? Default disposition: no, that's a calibration-backtest concern, not the connector's job. Record the cited source as free text.
- **OQ-KSI-006:** Cross-venue duplicates — both Kalshi and Polymarket may run markets on the same underlying event (e.g., a CPI print). Should the connector flag these for the operator? Default disposition: leave to `mispricing_detector` mapping; the connector's job is to surface markets faithfully, not to deduplicate across venues.
- **OQ-KSI-007:** Demo vs. production — Kalshi has separate demo (`external-api.demo.kalshi.co`) and production (`external-api.kalshi.com`) environments. v1 reads only public data, which is on production. The design specifies whether the demo environment is used at all (e.g., for the smoke test) or if the smoke test hits production read endpoints. Default disposition: smoke test hits production with the same `User-Agent` discipline; demo is a v2 trading concern.

## 9. Acceptance Criteria

The `kalshi_connector` v1 is considered complete when all the following are true:

- A daily sync produces an up-to-date `kalshi_markets`, `kalshi_events`, and `kalshi_series` set covering all active markets.
- 30-minute price snapshots run for the active universe under the rate-limit budget without 429 errors.
- A settlement backfill produces a queryable history of settled markets that `pattern_library` can read for calibration, correctly routed across the live/historical cutoff.
- Order book depth is fetchable on demand and can be persisted opt-in.
- Trade history is fetchable for watched markets, correctly routed across the cutoff.
- Eligibility refusal works as specified (REQ-KSI-ELIG-001).
- ToS acknowledgement gate works (REQ-KSI-TOS-001).
- A simulated Kalshi outage degrades gracefully — `data_ingest`, `polymarket_connector`, and other subsystems continue operating; freshness view reflects staleness.
- No credentials, RSA private keys, signing logic, or trading code paths exist in the v1 codebase. Acceptance test walks the package and asserts forbidden imports (`cryptography.hazmat.primitives.asymmetric.padding`, etc.) are absent.

## 10. References

- LOOM v0.35.0 — `razorrooster.md`. The v0.36.0 evolution-log entry that accompanies this spec adds `kalshi_connector` to the subsystem registry.
- `data_ingest` Requirements v0.1.0 — for the freshness/provenance contract this connector hooks into.
- `polymarket_connector` Requirements v0.1.0 (`POLYMARKET_CONNECTOR.md`) — sibling subsystem; this spec mirrors its structure deliberately.
- Kalshi API documentation: `docs.kalshi.com` — Quick Start: Market Data, Quick Start: Authenticated Requests, Rate Limits and Tiers, Historical Data, Orderbook Responses.
- Kalshi public Terms of Service URL recorded in `config/kalshi.yaml` (operator-updateable when Kalshi revises).

Content drawn from external sources is paraphrased per licensing constraints.
