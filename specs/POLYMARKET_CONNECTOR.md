# POLYMARKET_CONNECTOR — Requirements

**Subsystem:** `polymarket_connector`
**Codename:** The Wire
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD (revised from FULL — see §3)
**Last updated:** 2026-05-14

---

## 1. Purpose

`polymarket_connector` is the read-only Polymarket data ingestion layer for Razor-Rooster. It is responsible for:

- Discovering the universe of currently active and recently resolved Polymarket markets and events.
- Pulling live and historical pricing for those markets.
- Pulling resolved-contract outcome data for backtesting and calibration.
- Persisting that data into the local DuckDB store maintained by `data_ingest`, conforming to its canonical schemas where possible and adding a small number of Polymarket-specific tables where the existing schemas don't fit.

Downstream consumers:
- `mispricing_detector` reads live market-implied probabilities to compare against model probabilities.
- `pattern_library` reads resolved contracts to calibrate base-rate priors and validate model outputs.
- `monitor` reads price movement on contracts mapped to active analyses, to detect material moves.

`polymarket_connector` does not place orders, hold positions, manage wallets, or sign transactions in v1. Trading is explicitly out of scope (see §3).

## 2. Scope

### In scope (v1)

- Read-only access to Polymarket's public APIs:
  - **Gamma API** (`gamma-api.polymarket.com`) for markets, events, and resolution metadata.
  - **CLOB public endpoints** for prices, order books, and trade history.
  - **Real-Time Data Socket (RTDS)** WebSocket for live price/orderbook updates *if* feasibility holds (see §8 OQ-PMC-002).
- Backfill of resolved-contract history for backtesting (calibration) — gated only by Polymarket's own retention and by our 100 GB global corpus cap.
- Mapping of Polymarket markets to event-class taxonomies used by `pattern_library` (some markets correspond to event classes the library tracks; many do not — both cases must be representable).
- Provenance tracking consistent with `data_ingest` (source-tagged, timestamped, deduplicated).
- Rate-limit-aware client respecting Polymarket's published per-firm caps.
- Geo-restriction awareness: refuse to start if the configured operating jurisdiction is on Polymarket's restricted list, or warn clearly if the system can't determine the jurisdiction.

### Out of scope (explicit)

- **Order placement, order cancellation, position management, balance management.** Trading is a v2+ concern; the v1 connector is read-only and never holds keys that could authorize transactions.
- **Wallet integration.** No Polygon wallet is configured, no EIP-712 signing, no funder address. The connector code path that would invoke L2 methods does not exist in v1.
- **L1 / L2 API credentials.** Not used. No `.env` variables for Polymarket-side authentication.
- **VPN-based access from restricted jurisdictions.** Polymarket's ToS prohibits this, and the connector will not facilitate it.
- **Off-platform liquidity** (e.g., other prediction markets like Kalshi, PredictIt). v1 is Polymarket-only. Adding alternate sources later is a separate spec.
- **User-facing market discovery / search UI.** This is a backend layer; market selection happens in downstream subsystems via configuration or programmatic queries.

## 3. Threat Context Reassessment

The LOOM v0.4.0 records `threat_context: FULL` for `polymarket_connector`, on the assumption that the connector would interact with wallets and authenticated APIs. With the v1 scope locked to read-only public data only:

- No credentials are stored.
- No transactions can be initiated.
- No on-chain identity is assumed by this code path.

The threat context for v1 is therefore **STANDARD**, matching `data_ingest`. If and when trading is added (v2+), the threat context for the affected code paths returns to FULL and a separate spec amendment captures the wallet-handling design at that time.

This downgrade is recorded in the LOOM as part of the evolution-log entry that accompanies this spec.

## 4. Stakeholders & Operating Assumptions

- **Operator:** single user, running on EliteBook G8 alongside the rest of the system.
- **Network:** intermittent residential connectivity. Pulls must tolerate transient failures and resume.
- **Cadence:** live pricing wants near-real-time updates; market metadata (existence, resolution status) updates daily; resolved-contract backfill runs once initially and then incrementally as new markets resolve.
- **Polymarket rate budget:** 100 requests/second firm-wide, averaged over a 1-minute window per Polymarket's published documentation. Razor-Rooster sits well inside this budget for any plausible workload, but the connector still respects it explicitly.
- **Jurisdiction:** the operator has confirmed they are in a jurisdiction where read access to Polymarket public data is permitted. Trading is out of scope so trading-jurisdiction questions don't arise in v1, but this assumption is recorded.

## 5. Data Surface Inventory

The connector ingests these data classes from Polymarket:

| Data class | Source endpoint family | Cadence | Schema target |
|---|---|---|---|
| Active markets metadata | Gamma `/markets`, `/events` | Daily | `polymarket_markets` (new) |
| Live mid-price + spread | CLOB public price endpoints | Hourly (configurable; faster opt-in) | `polymarket_price_snapshots` (new, time-series-shaped) |
| Order book depth | CLOB public order book endpoint | On-demand for analyses | `polymarket_orderbook_snapshots` (new) |
| Trade history | CLOB public trades endpoint | Daily | `polymarket_trades` (new, event-stream-shaped) |
| Resolved contracts | Gamma resolved markets | Backfill once + daily delta | `polymarket_resolutions` (new) |
| Real-time price stream (optional) | RTDS WebSocket | Continuous when running | Same `polymarket_price_snapshots` table |

Polymarket data does not map cleanly onto `data_ingest`'s four canonical schemas (`event_stream`, `time_series`, `document_docket`, `geospatial_indicator`) because contracts have a token-pair structure (YES/NO outcome tokens with mirror prices) that none of those schemas natively represent. The connector therefore introduces five new Polymarket-namespaced tables. This is a deliberate exception to `data_ingest`'s REQ-EXT-002 (which makes adding a fifth canonical schema deliberate); these are *source-specific* tables, not new canonical schemas, and live in their own namespace.

## 6. Functional Requirements

Requirements use EARS-style phrasing with stable IDs and verification notes. PMC = `polymarket_connector`.

### 6.1 Market discovery and metadata

**REQ-PMC-MARKET-001: Active market enumeration**
The connector **shall** provide a `list_active_markets()` operation returning all currently active (not closed, not resolved) Polymarket markets with their metadata (question text, outcome tokens, end date, category, slug, condition ID, CLOB token IDs).
*Verification:* fixture-based unit test confirms parsing of a representative Gamma API response. Integration smoke test confirms a non-empty list against the live API.

**REQ-PMC-MARKET-002: Market metadata persistence**
Active market metadata **shall** be persisted to a `polymarket_markets` table with at minimum: `condition_id` (primary), `slug`, `question`, `category`, `subcategory`, `event_id`, `outcome_tokens` (JSON), `end_date`, `active`, `closed`, `resolved`, `volume`, `created_at`, `last_updated`.
*Verification:* schema migration produces the table; round-trip test stores and queries a representative market.

**REQ-PMC-MARKET-003: Daily market sync**
The connector **shall** support a daily sync that reconciles the local `polymarket_markets` table with Polymarket's current state: insert new markets, update changed metadata (end date extensions, status changes), mark missing markets as `removed_at = <timestamp>` rather than deleting rows.
*Verification:* integration test runs a sync against a mock API state, alters the mock state, runs a second sync, confirms expected diffs and that no historical rows are destroyed.

### 6.2 Pricing

**REQ-PMC-PRICE-001: Mid-price snapshot pull**
The connector **shall** provide a `snapshot_prices(market_ids)` operation that returns mid-price, best bid, best ask, and last trade price for each requested market's outcome tokens at the moment of call.
*Verification:* fixture test confirms parsing; smoke test against a small set of live markets returns plausible values (0 ≤ price ≤ 1, bid ≤ ask).

**REQ-PMC-PRICE-002: Price snapshot persistence**
Price snapshots **shall** be persisted to `polymarket_price_snapshots` with: `condition_id`, `outcome_token_id`, `snapshot_ts`, `mid_price`, `best_bid`, `best_ask`, `last_trade_price`, `last_trade_ts`, `volume_24h`, `source` (`rest` | `rtds`).
*Verification:* schema migration; round-trip test.

**REQ-PMC-PRICE-003: Configurable snapshot cadence**
The connector **shall** support configurable per-market snapshot cadence with a sensible default (hourly) and an upper bound (60-second minimum interval) to prevent rate-budget exhaustion. A small set of "watched markets" mapped to active analyses **may** be configured for higher-frequency snapshots, separately from the default.
*Verification:* config-driven test confirms cadence applied per market; rate-budget test confirms total request rate stays below 50% of Polymarket's published cap with default config.

**REQ-PMC-PRICE-004: Price-stream tolerance for sparse markets**
For markets with low liquidity (no recent trades, wide spreads, missing best-bid or best-ask), the connector **shall** persist NULL values rather than synthesizing prices, and **shall** flag the snapshot with a `liquidity_warning` column when bid-ask spread exceeds a configurable threshold (default: 5 cents on $1 notional).
*Verification:* fixture test with a thin-orderbook response confirms NULLs and warning flag.

### 6.3 Order books

**REQ-PMC-OB-001: Order book depth on demand**
The connector **shall** provide a `fetch_orderbook(market_id, depth_levels=10)` operation returning the requested market's bid and ask sides to the requested depth.
*Verification:* fixture test confirms parsing of a representative orderbook response; smoke test against a live liquid market.

**REQ-PMC-OB-002: Order book snapshots are not auto-persisted by default**
Order book snapshots **shall not** be persisted on a schedule, only when an explicit `persist=True` flag is passed to `fetch_orderbook`. Default behavior returns the snapshot in-memory for analysis without writing to disk.
*Verification:* unit test confirms default behavior does not write; opt-in test confirms persistence path works.
*Rationale:* full orderbook history at scale would blow the corpus cap. Persist only when an analysis explicitly needs it.

### 6.4 Resolutions and historical outcomes

**REQ-PMC-RES-001: Resolved contracts enumeration**
The connector **shall** provide a `list_resolved_markets(since=None)` operation returning all markets resolved since the given timestamp, or all available resolved markets if `since` is `None`.
*Verification:* fixture test; smoke test with `since=<7 days ago>` returns a plausible list.

**REQ-PMC-RES-002: Resolution metadata persistence**
Resolved-market metadata **shall** be persisted to `polymarket_resolutions` with: `condition_id`, `winning_outcome` (token ID or NULL if invalid/refunded), `resolution_ts`, `resolution_source` (e.g. UMA oracle), `resolved_via` (free text describing the resolution mechanism), `final_yes_price`, `final_no_price`, `total_volume_at_resolution`.
*Verification:* schema migration; round-trip test.

**REQ-PMC-RES-003: Resolution backfill**
The connector **shall** support a one-time `backfill_resolutions()` operation that pulls the full history of resolved Polymarket contracts to the depth Polymarket exposes via Gamma. Subsequent daily syncs append new resolutions.
*Verification:* integration test against a mock with a known set of historical resolutions confirms full pull. Smoke test runs a real backfill on a fresh DuckDB and confirms a non-trivial number of resolutions land.

### 6.5 Trade history

**REQ-PMC-TRADE-001: Per-market trade history pull**
The connector **shall** provide a `pull_trades(market_id, since)` operation returning all trades on a given market since the given timestamp.
*Verification:* fixture test; smoke test.

**REQ-PMC-TRADE-002: Trade persistence**
Trades **shall** be persisted to `polymarket_trades` with: `condition_id`, `outcome_token_id`, `trade_ts`, `price`, `size`, `side` (buy/sell relative to YES outcome where determinable, else NULL), `tx_hash`.
*Verification:* schema migration; round-trip test.

**REQ-PMC-TRADE-003: Trade pull is not the default**
Trade history **shall not** be pulled on the daily cycle by default. It **shall** be pulled only for markets explicitly registered in a `watched_markets` config or invoked manually by the operator. *Rationale: high-volume markets generate substantial trade history; pulling all trades for all markets would dominate disk usage without proportional analytic value.*
*Verification:* config-driven test confirms default daily cycle does not pull trades unless markets are watched.

### 6.6 Real-time stream (optional, deferred decision)

**REQ-PMC-RTDS-001: WebSocket price stream support**
*Conditional requirement.* The connector **may** support subscribing to Polymarket's RTDS WebSocket for real-time price updates on a configurable list of watched markets. If implemented, RTDS-sourced snapshots **shall** be tagged `source = 'rtds'` in `polymarket_price_snapshots` and **shall not** mix indistinguishably with REST-pulled snapshots.
*Verification:* if implemented, an integration test confirms tag is set correctly; otherwise the requirement is deferred to v2.
*Note:* the implement-or-defer decision is settled in the design phase (OQ-PMC-002).

### 6.7 Rate limiting and resilience

**REQ-PMC-RATE-001: Token-bucket rate limiter**
The connector **shall** apply a token-bucket rate limiter calibrated to stay below 50% of Polymarket's published per-firm cap (i.e., target ≤50 req/sec averaged over 1 minute) by default. The 50% headroom is configurable but the default is conservative to allow other consumers of the same shared firm cap (if any) to coexist.
*Verification:* unit test confirms the limiter blocks requests when the bucket is empty and refills correctly; integration test under simulated heavy load confirms request rate stays under cap.

**REQ-PMC-RATE-002: Rate-limit response handling**
On any 429 or rate-limit-related error response from Polymarket, the connector **shall** apply exponential backoff with jitter, capped at 5 retries, and **shall** log structured warnings on each retry. Persistent rate-limit failures after retries **shall** be surfaced to the cycle report.
*Verification:* fixture-based test injects 429 responses and confirms backoff behavior.

**REQ-PMC-RES-001a: Failure isolation from data_ingest**
A failure in `polymarket_connector` **shall not** halt or corrupt `data_ingest`. Both subsystems use the same DuckDB store but operate on disjoint table sets and do not hold cross-subsystem locks beyond DuckDB's normal transaction semantics.
*Verification:* integration test forces a Polymarket-side failure and confirms `data_ingest` cycles continue and persist correctly.

### 6.8 Geo-restriction and ToS compliance

**REQ-PMC-GEO-001: Restricted-jurisdiction refusal**
The connector **shall** read a `OPERATOR_JURISDICTION` environment variable (or a `config/operator.yaml` entry) at startup. If the value is on a restricted-jurisdiction list (currently per Polymarket help docs, including but not limited to: countries explicitly blocked by Polymarket's geofence), the connector **shall** refuse to start and emit a clear error explaining the restriction.
*Verification:* unit test confirms refusal for a known-restricted value; passes for a known-permitted value.

**REQ-PMC-GEO-002: No VPN circumvention support**
The connector **shall not** include any logic to route requests through proxies, VPNs, or other endpoints intended to circumvent Polymarket's geographic restrictions. The connector exclusively uses direct HTTPS to Polymarket's documented API hosts.
*Verification:* code review confirms no proxy-configuration code paths exist.

**REQ-PMC-TOS-001: Terms-of-service acknowledgement on startup**
On first run, the connector **shall** require the operator to acknowledge Polymarket's terms of service (record acknowledgement timestamp and the ToS version hash in the `sources` table). On subsequent runs, the connector **shall** check the recorded version and re-prompt if the ToS version has changed.
*Verification:* first-run integration test confirms acknowledgement gate; second run with same ToS version proceeds; simulated version change re-prompts.

### 6.9 Provenance and freshness

**REQ-PMC-PROV-001: Per-record provenance**
Every Polymarket-sourced record **shall** carry: source identifier (`polymarket`), source-side ID (Polymarket's `condition_id` or token ID), fetch timestamp, source publication timestamp where the API exposes it, and connector version.
*Verification:* DuckDB query returns full provenance for any record.

**REQ-PMC-PROV-002: Polymarket entry in freshness view**
The `freshness` view defined by `data_ingest` **shall** include `polymarket` as a registered source with last-successful-fetch tracking and a freshness threshold (default: 6 hours for live data, 48 hours for resolutions).
*Verification:* freshness query returns Polymarket entry after a sync.

### 6.10 Logging and observability

**REQ-PMC-LOG-001: Structured per-operation logging**
Each connector operation (sync, snapshot, backfill) **shall** emit a structured JSON log entry consistent with `data_ingest` REQ-LOG-001 conventions, with operation type, market count touched, duration, and any errors.
*Verification:* log inspection confirms entry structure.

**REQ-PMC-LOG-002: No PII leakage**
Logs **shall not** contain wallet addresses, on-chain identities, or any operator-side identifiers beyond the local username. Polymarket's public market data does not contain operator PII; this requirement is a forward-looking guard for the v2+ trading expansion.
*Verification:* log-scan test against synthetic operator identifiers confirms redaction.

## 7. Non-Functional Requirements

**NFR-PMC-PERF-001:** A daily metadata sync of all active Polymarket markets **shall** complete within 5 minutes on the operator's hardware and network.

**NFR-PMC-PERF-002:** A full resolution backfill **shall** complete within 12 hours on the operator's hardware. (Polymarket's resolved-market history is bounded; this should be comfortable. If exceeded, the connector logs a warning and the design phase reconsiders pagination strategy.)

**NFR-PMC-PERF-003:** Steady-state Polymarket-side disk usage (markets + resolutions + hourly price snapshots for the active universe) **shall** stay under 5 GB out of the 100 GB global cap. Trade-history and orderbook persistence (opt-in, watched-markets-only) is excluded from this budget and managed via `data_ingest`'s per-source caps.

**NFR-PMC-AVAIL-001:** Polymarket-side connector failures **shall** degrade gracefully — `mispricing_detector` consumers see stale-but-flagged data (via the freshness view) rather than crashes.

**NFR-PMC-SEC-001:** No Polymarket-side credentials are configured in v1 (per scope). The connector code path that would load them does not exist. When v2+ adds trading, NFR-PMC-SEC-001 is amended to specify wallet-key handling at FULL threat context.

**NFR-PMC-TOS-001:** Each connector run that hits Polymarket APIs **shall** include a `User-Agent` header identifying the application (e.g., `razor-rooster/0.1 (research; +operator-contact)`), per the common convention for polite API use even when the source does not require it.

## 8. Open Questions (carry to design phase)

- **OQ-PMC-001:** Polymarket category/subcategory taxonomy — does it map cleanly to Razor-Rooster's six domain sectors, or do we need a translation layer? The design should specify the mapping table and a mechanism for handling Polymarket markets that don't fit any sector.
- **OQ-PMC-002:** RTDS WebSocket — implement in v1 or defer? Trade-off: adds real-time fidelity for live mispricing detection vs. adds long-running-connection complexity to a system that is otherwise batch-oriented. Default disposition: defer to v2 unless `mispricing_detector` requirements demand sub-hour freshness.
- **OQ-PMC-003:** Resolution backfill depth — Polymarket has been live since 2020. Confirm whether the Gamma API exposes the full historical record or only a recent window. If windowed, decide whether to supplement with off-API archival sources (e.g., third-party mirrors) or accept the window.
- **OQ-PMC-004:** Negative-risk markets and CTF token mechanics — Polymarket has multi-outcome markets with specialized token structures. Determine in design whether the v1 schema captures these adequately or whether multi-outcome markets are deferred.
- **OQ-PMC-005:** Polymarket US vs. main Polymarket — there are now two regulated tracks (the main international platform and the US-regulated platform). Determine whether the connector targets both, picks one based on operator jurisdiction, or treats them as two source identifiers.
- **OQ-PMC-006:** Sector-mapping of markets — for each active market, which sector(s) does it belong to? Some markets span multiple (e.g., a public-health market with regulatory implications). The design specifies whether mapping is one-to-one, one-to-many, or operator-curated.
- **OQ-PMC-007:** Freshness threshold for live price data — 6 hours is generous. If `mispricing_detector` operations expect tighter freshness (e.g., sub-hour for active markets), this changes the snapshot cadence and the rate-limit budget. Settle when `mispricing_detector` requirements are written.

## 9. Acceptance Criteria

The `polymarket_connector` v1 is considered complete when all the following are true:

- A daily sync produces an up-to-date `polymarket_markets` table covering all active markets.
- Hourly price snapshots run for the active universe under the rate-limit budget without throttle errors.
- A resolution backfill produces a queryable history of resolved contracts that `pattern_library` can read for calibration.
- Order book depth is fetchable on demand and can be persisted opt-in.
- Trade history is fetchable for watched markets.
- Geo-restriction refusal works as specified (REQ-PMC-GEO-001).
- ToS acknowledgement gate works (REQ-PMC-TOS-001).
- A simulated Polymarket outage degrades gracefully — `data_ingest` and other subsystems continue operating; freshness view reflects staleness.
- No credentials, wallet addresses, or trading code paths exist in the v1 codebase.

## 10. References

- LOOM v0.4.0 — `razorrooster.md`, subsystem registry entry for `polymarket_connector`.
- `data_ingest` Requirements v0.1.0 — for the freshness/provenance contract this connector hooks into.
- Polymarket public API documentation: `docs.polymarket.com` (Gamma API, CLOB public methods, RTDS overview, rate limits, geoblock notes).
- Open thread OT-001 — resolved by §3 of this document.
- Open thread OT-004 — this spec implements the recommendation-only/manual-execution disposition (no trading in v1).

Content drawn from external sources is paraphrased per licensing constraints.
