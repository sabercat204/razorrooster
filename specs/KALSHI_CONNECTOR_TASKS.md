# KALSHI_CONNECTOR — Implementation Tasks

**Subsystem:** `kalshi_connector`
**Codename:** The Stamp
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-15
**Companion specs:**
- Requirements: `KALSHI_CONNECTOR.md` v0.1.0
- Design: `KALSHI_CONNECTOR_DESIGN.md` v0.1.0

**Hard prerequisites:**

- All v1 subsystems PRODUCTION_READY (LOOM v0.35.x). The Kalshi connector
  is the v1.1 effort that lands after v1 ships.
- `data_ingest` Phase 0–3 (T-001 through T-035) — the connector reuses
  `DuckDBStore`, the migrations framework, staging-merge, scheduler,
  structured logging, and credential-redaction filter.
- `polymarket_connector` Phase 0–6 — the Kalshi connector mirrors
  Polymarket's structural decisions (geo gate pattern, ToS gate
  pattern, rate limiter shape, sector heuristic, opt-in trades + 
  orderbook). The Polymarket implementation is the working reference;
  the Kalshi work imports the same patterns rather than re-deriving.
- `mispricing_detector`, `position_engine`, `monitor`,
  `report_generator` Phase 0+ (already PRODUCTION_READY at v1) — the
  cross-subsystem `venue` discriminator migrations land in their
  existing migration version ranges.

---

## How to Read This Document

Same conventions as `POLYMARKET_CONNECTOR_TASKS.md`: each task has
stable ID, dependencies, references back to requirement and design
IDs, deliverables, verification, and out-of-scope guards.

Task IDs are prefixed `T-KSI-NNN` to distinguish from
`polymarket_connector` (`T-PMC-`), `data_ingest` (`T-`), and the
other subsystems. Cross-subsystem migration tasks use the affected
subsystem's prefix (`T-MD-NNN`, `T-PE-NNN`, `T-MON-NNN`,
`T-RG-NNN`) to make the migration trace visible from each
subsystem's tracking section.

## Phase 0 — Module Bootstrap

### T-KSI-001 — Initialize kalshi_connector module
**Depends on:** polymarket_connector T-PMC-001 (module skeleton convention established).
**References:** design §3.1 module layout.
**Deliverables:**
- Create the `razor_rooster/kalshi_connector/` directory tree per design §3.1.
- Empty `__init__.py` in each subdirectory.
- `cli.py` with a single `click` group `razor-rooster kalshi` that prints the available subcommands and exits.
- Mirror test layout under `tests/kalshi_connector/`.
- mypy strict override added in `pyproject.toml` for `razor_rooster.kalshi_connector.*`.
**Verification:** `razor-rooster kalshi --help` shows the command group; `pytest` discovers and runs zero tests cleanly; mypy strict clean on the empty module.
**Out of scope:** any logic.

### T-KSI-002 — Kalshi-specific configuration files
**Depends on:** T-KSI-001, data_ingest T-022 (config loader pattern established).
**References:** design §4 configuration, REQ-KSI-PRICE-003, REQ-KSI-RATE-001, REQ-KSI-RATE-003, REQ-KSI-ELIG-001.
**Deliverables:**
- `config/kalshi.yaml` populated with the structure from design §4: base_url, tier, sync cadences, rate-limit tier-budget map, freshness thresholds, sector-mapping config.
- `config/kalshi_sector_keywords.yaml` with a small curated initial keyword set per sector plus the `out_of_scope` bucket per OQ-KSI-001 (DEFER-KSI-001 acknowledges this will expand).
- `config/kalshi_allowed_jurisdictions.yaml` with `allowed: ["US"]` seed list.
- Pydantic config models for the Kalshi config tree (separate from Polymarket's models so the two don't drift).
**Verification:** unit test confirms valid configs parse, invalid configs (unknown fields, missing required, unknown tier) fail with informative errors. Sector-keyword config rejects unknown sector names.
**Out of scope:** runtime config reloading; Kalshi sector-keywords sharing with Polymarket (kept separate per design §3.5).

## Phase 1 — Schemas and Migrations

### T-KSI-010 — Kalshi-namespaced table schemas
**Depends on:** T-KSI-001, data_ingest T-013 (migrations framework).
**References:** design §3.3 (all nine tables), REQ-KSI-MARKET-002, REQ-KSI-PRICE-002, REQ-KSI-OB-002, REQ-KSI-SETTLE-002, REQ-KSI-TRADE-002, REQ-KSI-SETTLE-004, REQ-KSI-SECTOR-002.
**Deliverables:**
- DDL strings for `kalshi_series`, `kalshi_events`, `kalshi_markets`, `kalshi_price_snapshots`, `kalshi_orderbook_snapshots`, `kalshi_trades`, `kalshi_settlements`, `kalshi_historical_cutoff` (single-row state table), `kalshi_sector_mapping`.
- All timestamp columns are `TIMESTAMPTZ` per `data_ingest` REQ-NORM-002.
- Each schema includes the provenance prefix from `data_ingest` design §4 where applicable (the cutoff and sector-mapping tables don't carry source-record provenance the same way).
- Schema-migration version space 8001+ allocated and documented.
**Verification:** schemas applied to an in-memory DuckDB; round-trip test inserts a synthetic row per table and queries it back. Indexes confirmed via `EXPLAIN`. The `kalshi_historical_cutoff` upsert pathway round-trips cleanly (single-row replace semantics).
**Out of scope:** the migration runner registration (T-KSI-011); cross-subsystem migrations (Phase 1.5).

### T-KSI-011 — Kalshi migration registration
**Depends on:** T-KSI-010
**References:** design §3.2 (reuse from data_ingest).
**Deliverables:**
- `persistence/migrations/__init__.py` with `run_pending_kalshi_migrations(conn)` that delegates to `data_ingest.persistence.migrations.run_pending_migrations(conn, package_name=__name__)`.
- `persistence/migrations/m8001_kalshi_initial.py` applying all DDL from T-KSI-010 with `up`/`down` functions.
- The migration version recorded in `schema_migrations` with description.
**Verification:** open a fresh store → m8001 applied; reopen → no migration runs (idempotent). `down` cleanly drops all tables in reverse-create order.
**Out of scope:** subsequent migrations.

### T-KSI-012 — Source registration and freshness participation
**Depends on:** T-KSI-011, data_ingest T-015 (provenance helpers), data_ingest T-032 (registry).
**References:** REQ-KSI-PROV-001, REQ-KSI-PROV-002, design §3.2, design §3.9.
**Deliverables:**
- Register `source_id = 'kalshi'` in the `sources` table on first connector startup.
- The `freshness` view picks up Kalshi entries via the existing generic source-row mechanism — no view changes needed.
- Separate freshness threshold tracking for prices (3h, tighter than Polymarket's 6h since cadence is 30min) vs. settlements (48h).
**Verification:** integration test confirms `freshness` view returns Kalshi entry with correct staleness flag based on `last_successful_fetch`.
**Out of scope:** populating `last_successful_fetch` (sync code does that).

## Phase 1.5 — Cross-Subsystem `venue` Discriminator Migrations

These migrations live in the **affected subsystems' migration directories**, not in `kalshi_connector`. They are additive: existing rows migrate in place with `venue = 'polymarket'` default, new Kalshi rows insert with `venue = 'kalshi'`. The migrations land **before** any Kalshi sync code so the schema is ready to accept the second venue.

Order matters: `data_ingest` (acknowledged_posture) → `mispricing_detector` (mappings + comparisons + resolutions) → `position_engine` (analyses) → `monitor` (follow_ups). `report_generator` schema is unchanged; its assemblers and renderer get code changes in Phase 6.

### T-DI-101 — Add `acknowledged_posture` column to `sources`
**Depends on:** none (data_ingest is the foundation).
**References:** design §3.8 (Kalshi ToS posture-aware acknowledgement), REQ-KSI-TOS-002.
**Deliverables:**
- Migration in `data_ingest/persistence/migrations/m0040_add_acknowledged_posture.py` (next available data_ingest migration version).
- DDL: `ALTER TABLE sources ADD COLUMN acknowledged_posture VARCHAR NULL`.
- Backfill: existing Polymarket acknowledgement rows get `acknowledged_posture = 'read_only'`.
- `data_ingest.persistence.provenance` write helpers updated to accept and persist the posture.
**Verification:**
- Round-trip test: write an ack with `posture='read_only'`, read back, confirm column.
- Backfill test: existing Polymarket ack rows have `acknowledged_posture = 'read_only'` after migration.
- Mypy strict clean.
- Existing data_ingest tests still pass.
**Out of scope:** v2 trading posture handling (`'trading'` is reserved but not yet exercised).

### T-MD-101 — Add `venue` discriminator to mispricing tables
**Depends on:** T-DI-101.
**References:** design §3.10 (Cross-Subsystem Schema Changes — mispricing_detector).
**Deliverables:**
- Migration `mispricing_detector/persistence/migrations/m4002_add_venue_discriminator.py`.
- DDL:
  - `ALTER TABLE class_market_mappings ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket'`.
  - Drop and recreate `idx_class_market_mappings_active` to include `venue`: `(class_id, venue, condition_id, polarity, removed_at)`.
  - `ALTER TABLE comparisons ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket'`.
  - `CREATE INDEX idx_comparisons_venue_class_computed ON comparisons (venue, class_id, computed_at)`.
  - `ALTER TABLE comparison_resolutions ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket'`.
- Update `mispricing_detector.models` dataclasses (`ClassMarketMapping`, `Comparison`, `ComparisonResolution`) to include the `venue` field with `Literal['polymarket', 'kalshi']` typing.
- Update `mispricing_detector.persistence.operations.register_mapping` to accept `venue` parameter and check uniqueness against `(class_id, venue, condition_id, polarity)` instead of `(class_id, condition_id, polarity)`.
- Update `mispricing_detector.persistence.operations.persist_comparison`, `query_comparisons`, `query_existing_resolution_links`, `query_comparisons_for_market` to round-trip the `venue` column.
- Add a schema-comment block (preserved as a docstring on the column constants in `schemas.py`) documenting that `condition_id` is now a venue-specific market identifier (Polymarket condition_id when `venue='polymarket'`; Kalshi ticker when `venue='kalshi'`).
**Verification:**
- Round-trip test: write a Polymarket mapping, write a Kalshi mapping for the same class, both persist independently.
- Uniqueness test: registering a duplicate (same class, venue, condition_id, polarity) raises `MappingExistsError`; registering across different venues does not.
- Backfill test: pre-existing rows have `venue = 'polymarket'` after migration.
- All existing mispricing_detector tests still pass (the default value preserves their semantics).
- Mypy strict clean.
**Out of scope:** updating the comparator engine to read both venues' price data — that's T-KSI-061.

### T-PE-101 — Add `venue` to position_engine analyses
**Depends on:** T-MD-101.
**References:** design §3.10 (Cross-Subsystem Schema Changes — position_engine).
**Deliverables:**
- Migration `position_engine/persistence/migrations/m5002_add_venue_to_analyses.py`.
- DDL:
  - `ALTER TABLE analyses ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket'`.
  - `CREATE INDEX idx_analyses_venue_computed ON analyses (venue, computed_at)`.
- Update `position_engine.models.Analysis` dataclass to include `venue: Literal['polymarket', 'kalshi']`.
- Update `position_engine.persistence.operations.persist_analysis`, `get_analysis`, `query_analyses`, `_analysis_from_row` to round-trip the column.
- Update `position_engine.engines.analyzer` so analyses copy `venue` from the source comparison.
- Update `position_engine.frame.renderer.render` to prefix the market identifier with `(<venue>)` so rendered output reads e.g. `Market 0xabc... (polymarket)` or `Market KXCPI-26AUG-T2.5 (kalshi)`.
**Verification:**
- Round-trip test for both venue values.
- Renderer test: rendered analysis includes the venue tag.
- Linter test: rendered analysis with venue tag still passes the imperative-language linter.
- All existing position_engine tests still pass.
- Mypy strict clean.
**Out of scope:** the analyzer reading Kalshi price data — that's T-KSI-061.

### T-MON-101 — Add `venue` to monitor follow_ups
**Depends on:** T-PE-101.
**References:** design §3.10 (Cross-Subsystem Schema Changes — monitor).
**Deliverables:**
- Migration `monitor/persistence/migrations/m6002_add_venue_to_follow_ups.py`.
- DDL: `ALTER TABLE follow_ups ADD COLUMN venue VARCHAR NOT NULL DEFAULT 'polymarket'`.
- Update `monitor.models.FollowUp` dataclass.
- Update `monitor.persistence.operations.persist_follow_up`, `get_follow_up`, `query_follow_ups`, `query_alerts`, `query_trajectory`, `_follow_up_from_row` to round-trip the column.
- Update `monitor.engines.comb.evaluate_analysis` so `venue` is copied from the source analysis into the follow-up.
- Update `monitor.engines.comb._query_resolution` to branch on venue: Polymarket reads from `polymarket_resolutions`, Kalshi reads from `kalshi_settlements` (added in T-KSI-040).
- Update `monitor.engines.reasoning.build_reasoning_text` to include the venue in the analysis-context line.
**Verification:**
- Round-trip test for both venue values.
- Resolution-detection test: synthetic Kalshi-resolved analysis triggers `resolution_status != 'unresolved'` via the kalshi_settlements path.
- All existing monitor tests still pass.
- Mypy strict clean.
**Out of scope:** the comb cycle iterating over Kalshi-venue analyses — already supported via `position_engine.list_by_state`, which is venue-agnostic by design.

### T-RG-101 — Render `venue` in report sections
**Depends on:** T-MON-101.
**References:** design §3.10 (Cross-Subsystem Schema Changes — report_generator).
**Deliverables:**
- Update `report_generator.engines.section_assemblers.surfaced` to include `venue` in each comparison content dict.
- Update `report_generator.engines.section_assemblers.watched` to include `venue` in each follow-up content dict.
- Update `report_generator.engines.section_assemblers.calibration` to include `venue` per resolution.
- Update `report_generator.engines.section_assemblers.watchlist` to surface unmapped candidates with a hint mentioning both venues ("Consider mapping to a Polymarket or Kalshi market").
- Update `report_generator.renderer.terminal` to render `(venue)` after each market identifier in surfaced and watched blocks.
- Update `report_generator.renderer.markdown` to add a `Venue` column to the calibration GFM table; update the surfaced and watched markdown subsections to include the venue.
- The `templates/calibration_verdicts.yaml` catalog is **not** changed — verdict text is venue-agnostic.
**Verification:**
- Renderer tests for terminal and markdown confirm venue is shown.
- Linter test: rendered output with venue tags still passes the imperative-language linter (no new forbidden phrases).
- All existing report_generator tests still pass (the default `venue='polymarket'` preserves their content).
- Mypy strict clean.
**Out of scope:** changing report ordering by venue (per design §3.10, ordering remains `confidence_weighted_score DESC` so the better-edge venue surfaces first regardless of which it is).

## Phase 2 — Gates (Eligibility and ToS)

These gates run before any Kalshi API call. Build them before the API client so the client cannot be invoked from a misconfigured operator state. Mirror the Polymarket gate structure; the differences are documented per task.

### T-KSI-020 — Eligibility allow-list gate
**Depends on:** T-KSI-002.
**References:** REQ-KSI-ELIG-001, REQ-KSI-ELIG-002, design §3.7.
**Deliverables:**
- `gates/eligibility.py` with `check_eligibility()` per design §3.7.
- Allow-list loaded from `config/kalshi_allowed_jurisdictions.yaml`, not hard-coded.
- The gate **inverts** Polymarket's deny-list: refusal on jurisdiction **not** in the allow-list, with a clear refusal message naming the file the operator must edit.
- Refusal raises a typed `EligibilityRefusal` exception.
- The gate is invoked by `cli.py` for every Kalshi subcommand and by the connector's sync entry points.
- Reuses the same `OPERATOR_JURISDICTION` env var that Polymarket consults (operator declares jurisdiction once).
**Verification:**
- Unit test: missing config → refusal.
- Unit test: jurisdiction not in allow-list → refusal.
- Unit test: jurisdiction in allow-list (e.g., `US`) → pass.
- Integration test: `razor-rooster kalshi sync` from a non-allowed jurisdiction fails fast.
- Cross-connector test: setting `OPERATOR_JURISDICTION=US` allows Kalshi but Polymarket refuses (US is on Polymarket's deny-list); setting `OPERATOR_JURISDICTION=DE` allows Polymarket but Kalshi refuses (DE not on Kalshi's allow-list). Confirms the operator's single declaration drives both gates correctly.
**Out of scope:** auto-detection of jurisdiction.

### T-KSI-021 — ToS acknowledgement gate with posture
**Depends on:** T-KSI-012, T-DI-101 (acknowledged_posture column), data_ingest T-014 (staging-merge for atomic ack writes), T-KSI-020.
**References:** REQ-KSI-TOS-001, REQ-KSI-TOS-002, design §3.8.
**Deliverables:**
- `gates/tos.py` with `check_tos_acknowledged(store)` per design §3.8.
- Reads `tos_version_hash`, `acknowledged_at`, `acknowledged_posture` from `sources` row for `kalshi`.
- Refuses with `ToSAcknowledgementRequired` if no ack or hash mismatch.
- Refuses with `ToSPostureMismatch` if hash matches but posture is not `'read_only'`.
- `cli.py` gains the `ack-tos` subcommand: fetches current ToS hash from `https://kalshi.com/docs/kalshi-terms-of-service` (URL configurable in `config/kalshi.yaml`), displays URL, prompts for confirmation with `--yes` for non-interactive use, writes ack to `sources` with `acknowledged_posture='read_only'`.
- ToS hash fetch with fallback to last-known hash from a `kalshi_tos_version_history` table (added in T-KSI-010 if not already there) if the live URL is unreachable.
- If both live fetch and last-known fail, refusal is raised.
**Verification:**
- Unit test: no prior ack → refusal.
- Unit test: matching hash + read_only posture → pass.
- Unit test: changed hash → refusal with re-prompt instructions.
- Unit test: matching hash but `posture='trading'` → posture-mismatch refusal.
- Unit test: live fetch fails, last-known matches → pass.
- Unit test: live fetch fails, no last-known → refusal.
- Integration test: full ack flow from fresh DB to acknowledged read-only state.
**Out of scope:** automated re-acknowledgement; v2 trading posture flow (acknowledged_posture column reserves the `'trading'` value but v1 never writes it).

## Phase 3 — HTTP Client Layer

### T-KSI-030 — Token-bucket rate limiter with per-endpoint cost map
**Depends on:** T-KSI-002.
**References:** REQ-KSI-RATE-001, REQ-KSI-RATE-003, design §3.6.
**Deliverables:**
- `client/rate_limit.py` with a thread/async-safe `TokenBucket(capacity, refill_per_second)` class (same shape as Polymarket's T-PMC-030 — consider extracting to `data_ingest` if the second copy raises maintenance concerns; for now duplicate is fine).
- `client/endpoint_costs.py` with the per-endpoint cost map per design §3.6.
- `acquire(endpoint_path, timeout=None)` consults the cost map and acquires `cost(endpoint)` tokens.
- Tier-aware initialization: `config/kalshi.yaml` records the operator's tier; the limiter's bucket capacity and refill rate scale to 50% of that tier's read budget.
- A module-level singleton bucket configured per `config/kalshi.yaml`.
**Verification:**
- Unit test: bucket drains under sustained load and refills correctly per declared tier.
- Unit test: parallel acquirers do not exceed cap when measured over a 1-second window.
- Unit test: timeout behavior — `acquire(timeout=...)` raises a documented exception when bucket can't refill in time.
- Unit test: cost-map consultation — confirm an endpoint with cost 5 (synthetic) drains 5 tokens, not 10.
- Tier-scaling test: switching from Basic (200 tokens/sec) to Advanced (300 tokens/sec) in config reconfigures the limiter on next startup.
**Out of scope:** distributed limiting across processes; per-endpoint cost discovery via API (cost map is operator-curated config).

### T-KSI-031 — Retry and backoff helpers
**Depends on:** T-KSI-030.
**References:** REQ-KSI-RATE-002.
**Deliverables:**
- `client/retry.py` with `retry_with_backoff(callable, max_retries=5, base=1, max_seconds=60)`.
- Jittered exponential backoff.
- 429-class detection. Important difference from Polymarket: Kalshi 429 responses do **not** include `Retry-After` or `X-RateLimit-*` headers, per Kalshi documentation. The retry helper relies entirely on its own backoff schedule.
- Structured log entry on each retry.
**Verification:**
- Unit test: synthetic 429 sequence triggers correct backoff timing (clock injection for deterministic test).
- Unit test: persistent failure exhausts retries and surfaces the original error.
- Unit test: confirms `Retry-After` header presence (synthetic) is **ignored** — Kalshi does not send it, and treating its absence as authoritative would create silent dependence.
**Out of scope:** circuit-breaker pattern.

### T-KSI-032 — User agent and HTTP client base
**Depends on:** T-KSI-030, T-KSI-031.
**References:** NFR-KSI-TOS-001, design §3.1.
**Deliverables:**
- `client/user_agent.py` building the User-Agent string per NFR-KSI-TOS-001 (e.g., `razor-rooster-kalshi/0.1 (research; +operator-contact)`).
- A shared `httpx.AsyncClient` factory wiring the limiter, retry decorator, User-Agent header, and timeout configuration.
- Base URL from `config/kalshi.yaml.base_url` (production-only in v1).
- All API client modules use this factory.
**Verification:** unit test confirms factory-built clients have the expected headers, timeout, and retry/limiter wiring.
**Out of scope:** HTTP/2 specifics; demo-environment switching.

### T-KSI-033 — Public REST client (markets / events / series)
**Depends on:** T-KSI-032.
**References:** REQ-KSI-SERIES-001, REQ-KSI-SERIES-002, REQ-KSI-EVENT-001, REQ-KSI-EVENT-002, REQ-KSI-MARKET-001, REQ-KSI-MARKET-002, REQ-KSI-MARKET-004, design §3.1.
**Deliverables:**
- `client/rest.py` with typed methods:
  - `list_series(cursor=None, limit=100)`.
  - `get_series(series_ticker)`.
  - `list_events(series_ticker=None, status=None, cursor=None, limit=100)`.
  - `get_event(event_ticker)`.
  - `list_markets(series_ticker=None, event_ticker=None, status='open', cursor=None, limit=100)`.
  - `get_market(ticker)`.
  - `get_orderbook(ticker, depth=10)` returning YES-side depth (NO derived in caller per design §3.3).
  - `get_market_trades(ticker, cursor=None, limit=100)`.
  - `get_historical_cutoff()`.
  - `get_historical_markets(cursor=None, limit=100)`.
  - `get_historical_market(ticker)`.
  - `get_historical_trades(ticker=None, cursor=None, limit=100)`.
- All methods return typed dataclasses (`KalshiSeries`, `KalshiEvent`, `KalshiMarket`, `KalshiOrderbook`, `KalshiTrade`, `KalshiHistoricalCutoff`); raw payload preserved.
- Cursor-based pagination handled internally for `list_*` methods (returns generator or accumulated list with `paginate=True` flag).
- All four market types (`binary`, `scalar`, `categorical`, plus binary strike-variants) round-trip through the dataclass typing per OQ-KSI-003.
**Verification:**
- Recorded-fixture unit tests for each endpoint family.
- Multi-type test: a fixture with `binary`, `scalar`, and `categorical` markets all parse correctly.
- Strike-variant test: `above`, `below`, `between`, `unstructured` all round-trip.
- NO-derivation test: orderbook fixture's YES asks correctly produce NO bids.
- Smoke test against the live Kalshi API pulls a small page from each endpoint family successfully.
**Out of scope:** any sync logic; any authenticated endpoints (acceptance test enforces absence).

## Phase 4 — Sync Operations

### T-KSI-040 — Cutoff snapshot
**Depends on:** T-KSI-033, T-KSI-011.
**References:** REQ-KSI-SETTLE-004, design §3.4 (Cutoff snapshot).
**Deliverables:**
- `sync/cutoff.py` with `snapshot_cutoff(store) -> KalshiHistoricalCutoff`.
- Single GET to `/historical/cutoff`, upsert into `kalshi_historical_cutoff` (single-row replace).
- Returns the snapshotted cutoff for use by sibling sync operations within the same cycle.
**Verification:**
- Unit test against a recorded cutoff response.
- Round-trip test: snapshot, query, confirm fields.
- Idempotency: re-running within same cycle replaces the row cleanly.
**Out of scope:** per-page cutoff re-fetching (deferred per OQ-KSI-004).

### T-KSI-041 — Daily series + events + markets sync
**Depends on:** T-KSI-021 (ToS gate), T-KSI-033 (REST client), T-KSI-040 (cutoff snapshot), T-KSI-011, data_ingest T-014.
**References:** REQ-KSI-SERIES-001..002, REQ-KSI-EVENT-001..002, REQ-KSI-MARKET-001..004, design §3.4 (Daily series + events + markets metadata sync).
**Deliverables:**
- `sync/series.py`, `sync/events.py`, `sync/markets.py` together implementing the three-stage daily sync per design §3.4.
- Diff logic identifies inserted, updated, removed rows per table.
- Removed rows get `removed_at = now()`; rows are never deleted.
- For each new or changed market, the sector heuristic mapper (T-KSI-050) is invoked and `kalshi_sector_mapping` upserted.
- Non-binary markets are persisted but tagged for downstream filtering per OQ-KSI-003.
**Verification:**
- Unit tests against three "snapshots in time" mock states confirm inserts/updates/removals across runs.
- Integration test: full sync against in-memory DuckDB and a comprehensive mock state.
- Idempotency: re-running a sync with identical state changes nothing.
- Multi-type integration test: a mock state with binary + scalar + categorical markets persists all three faithfully.
**Out of scope:** real-time market discovery.

### T-KSI-042 — 30-min price snapshot sync
**Depends on:** T-KSI-033, T-KSI-041 (markets must exist before snapshotting), T-KSI-011, data_ingest T-014.
**References:** REQ-KSI-PRICE-001..004, design §3.4 (30-min price snapshots).
**Deliverables:**
- `sync/prices.py` with `snapshot_prices(store, tickers=None, watched_only=False) -> PriceSnapshotReport`.
- Reads YES bid/ask, last trade, volume_24h, open_interest, liquidity from the markets snapshot fields (or from `/markets/{ticker}` for individual freshness).
- Computes `mid_price_dollars` when both YES bid and ask present, NULL otherwise.
- Computes `spread_bps` and `liquidity_warning` per REQ-KSI-PRICE-004 threshold.
- NULL preservation: missing one-sided quotes → NULLs, no synthetic prices.
- Filters to `market_type = 'binary'` per OQ-KSI-003 (non-binary markets are skipped, logged).
- Batches writes via staging-merge.
**Verification:**
- Unit test with thin-orderbook fixture: NULLs preserved, `liquidity_warning = TRUE`.
- Unit test with normal fixture: full row populated, NO derivation correct (downstream consumer test).
- Unit test with non-binary market: skipped, logged with reason.
- Rate-budget test: 500 mock binary markets snapshot in a synthetic run completes inside Basic-tier 50% headroom.
**Out of scope:** WebSocket source (deferred per OQ-KSI-002).

### T-KSI-043 — Settlement backfill (live + historical routing)
**Depends on:** T-KSI-033, T-KSI-040 (cutoff snapshot), T-KSI-011, data_ingest T-034 (backfill resume mechanism).
**References:** REQ-KSI-SETTLE-001..004, OQ-KSI-004 resolution, design §3.4 (Settlement backfill + daily delta).
**Deliverables:**
- `sync/settlements.py` with `backfill_settlements(store, until=None) -> BackfillReport`.
- Routing per design §3.4: settlements at or after the cycle's snapshotted cutoff route to `/markets?status=settled`; before the cutoff route to `/historical/markets`.
- Resumable per data_ingest's existing `backfill_state` machinery; cursor stored per route (live vs. historical).
- Each settled market also updates the corresponding `kalshi_markets` row (`status = 'settled'`).
- Idempotent upserts mean boundary-region markets that are double-queried during cutoff advances land exactly once.
**Verification:**
- Unit test against a paginated mock with synthetic settlements split across the cutoff.
- Resume test: kill mid-page on each route, re-run, confirm continuation without duplicates.
- Boundary test: simulate a cutoff advance during backfill; confirm bounded re-query produces no duplicate rows in `kalshi_settlements`.
- Smoke test: real backfill against live Kalshi; record duration and result count.
**Out of scope:** per-page cutoff re-fetching (deferred); third-party archival sources.

### T-KSI-044 — Daily settlement delta
**Depends on:** T-KSI-043.
**References:** REQ-KSI-SETTLE-001 (with `since` parameter), design §3.4.
**Deliverables:**
- `sync/settlements.py` `sync_recent_settlements(store) -> SyncReport` pulling settlements since `last_successful_fetch[kalshi_settlements]`.
- Same routing logic as backfill: live vs. historical based on snapshotted cutoff.
- Same upsert pathway.
**Verification:** integration test simulates two-day gap; sync pulls only the missing window and routes correctly.
**Out of scope:** intraday settlement sync.

### T-KSI-045 — Watched-markets trade pull
**Depends on:** T-KSI-033, T-KSI-040, T-KSI-011, T-KSI-041.
**References:** REQ-KSI-TRADE-001..003, design §3.4 (Trades pull).
**Deliverables:**
- `sync/trades.py` `pull_watched_trades(store) -> TradePullReport`.
- Reads watched markets from `config/kalshi.yaml` `sync.prices.watched_markets`; for each, pulls trades since last successful pull.
- Routing: trades created at or after `cutoff.trades_created_ts` go to `/markets/trades`; before go to `/historical/trades`.
- Trade-id-based dedup against re-pulls (Kalshi supplies a stable `trade_id`).
**Verification:**
- Unit test with mock trade history: deduped against a re-pull.
- Routing test: synthetic since-time spanning the cutoff produces correct two-route fetch.
- Empty-watched-markets case: completes immediately, logs "no watched markets."
**Out of scope:** unwatched-market trade pull on the daily cycle.

### T-KSI-046 — On-demand orderbook fetch
**Depends on:** T-KSI-033, T-KSI-011.
**References:** REQ-KSI-OB-001, REQ-KSI-OB-002, design §3.4 (Orderbook pull).
**Deliverables:**
- `sync/orderbook.py` `fetch_orderbook(ticker, depth=10, persist=False) -> KalshiOrderbook`.
- Returns in-memory `KalshiOrderbook` dataclass with YES bids and asks (NO derivation done by caller).
- Persists to `kalshi_orderbook_snapshots` only when `persist=True`.
- Default behavior verified to NOT write.
**Verification:**
- Unit test confirms default path does not write.
- Unit test confirms `persist=True` writes correctly with `side='yes_bid'` and `side='yes_ask'`.
- NO-derivation helper test: `derive_no_side(yes_orderbook)` returns expected NO bids/asks.
- Smoke test against live API returns plausible YES-side structure.
**Out of scope:** continuous orderbook tracking; persisting NO-side rows separately.

## Phase 5 — Sector Mapping

### T-KSI-050 — Sector heuristic mapper with out_of_scope
**Depends on:** T-KSI-002, T-KSI-011 (sector_mapping table).
**References:** OQ-KSI-001 resolution, design §3.5.
**Deliverables:**
- `mapping/sector_heuristic.py` implementing the three-pass mapper from design §3.5.
- Reads keywords from `config/kalshi_sector_keywords.yaml` (separate from Polymarket's keyword catalog per design §3.5).
- Returns `SectorMapping(razor_sector, secondary_sectors, confidence)` where `razor_sector` can be one of the six Razor sectors, `'cross_cutting'`, `'out_of_scope'`, or `None` for unmappable inputs.
- Sports / Entertainment / daily-life categories auto-classify as `'out_of_scope'` per OQ-KSI-001.
- Logs every classification with the inputs that drove the decision.
**Verification:**
- Unit tests across the six sectors with representative Kalshi market titles.
- Out-of-scope test: known sports markets (`KXNFL...`, `KXSUPERBOWL...`) classify as `out_of_scope`.
- Ambiguous-input test: returns `None` and logs the ambiguity.
- Confidence test: heuristic output is `'inferred'`.
**Out of scope:** ML-based mapping.

### T-KSI-051 — Sector mapping persistence and override CLI
**Depends on:** T-KSI-050, T-KSI-041.
**References:** OQ-KSI-001 resolution, design §3.5, REQ-KSI-SECTOR-001..003.
**Deliverables:**
- `mapping/sector_overrides.py` with `set_override(store, ticker, sector, secondary=None)`.
- `cli.py` gains:
  - `razor-rooster kalshi map <ticker> <sector> [--secondary ...]` writes a `confidence='manual'` row.
  - `razor-rooster kalshi needs-review [--limit N]` lists markets with `razor_sector IS NULL` or `confidence='low'`.
  - `razor-rooster kalshi mapping-stats` shows counts by sector and confidence (including `out_of_scope`).
- Markets sync (T-KSI-041) calls the heuristic mapper for new/changed markets and upserts; existing manual overrides are preserved.
- Downstream filter helper: `signal_scanner` and `mispricing_detector` query the table to skip `razor_sector='out_of_scope'` markets when iterating Kalshi tickers (purely additive — Polymarket markets don't hit this code path).
**Verification:**
- Integration test: heuristic produces `inferred` mapping, operator overrides to `manual`, subsequent sync does not overwrite.
- CLI test: `needs-review` returns expected list.
- Out-of-scope filter test: a ticker mapped `out_of_scope` is not a candidate for `signal_scanner` consumption.
**Out of scope:** bulk-override import; Polymarket sector-mapping cross-pollination.

## Phase 6 — CLI, Cycle Integration, and Cross-Subsystem Wiring

### T-KSI-060 — CLI subcommands
**Depends on:** T-KSI-041, T-KSI-042, T-KSI-043, T-KSI-044, T-KSI-045, T-KSI-046, T-KSI-051.
**References:** design §3.11, design §8.
**Deliverables:**
- `razor-rooster kalshi sync` — runs cutoff → series → events → markets → prices → settlements → trades in dependency order.
- `razor-rooster kalshi snapshot [--watched|--all]` — runs price snapshots only.
- `razor-rooster kalshi backfill-settlements [--restart] [--page-size N]` — initial historical pull.
- `razor-rooster kalshi watch <ticker>`, `kalshi unwatch <ticker>`, `kalshi list-watched`.
- `razor-rooster kalshi fetch-orderbook <ticker> [--persist]`.
- `razor-rooster kalshi map <ticker> <sector> [--secondary ...]`.
- `razor-rooster kalshi needs-review [--limit N]`.
- `razor-rooster kalshi mapping-stats`.
- `razor-rooster kalshi status` — print Kalshi source freshness and sync state.
- `razor-rooster kalshi version` — print `8001+` schema namespace.
- All commands invoke eligibility gate + ToS gate before any API call.
**Verification:**
- CLI test for each subcommand against a mock Kalshi state.
- Gate-bypass test: confirm no subcommand reaches API code paths if a gate raises.
- Top-level wiring: `razor-rooster --help` shows the `kalshi` group.
**Out of scope:** colored / TUI output beyond plain text.

### T-KSI-061 — Cross-subsystem comparator wiring
**Depends on:** T-KSI-060, T-MD-101 (venue column), T-KSI-042 (price snapshots exist).
**References:** design §3.10 (mispricing_detector cross-subsystem changes).
**Deliverables:**
- `mispricing_detector.engines.comparator` extended to read price + market data based on `venue` discriminator:
  - `venue='polymarket'`: existing path (reads `polymarket_price_snapshots`, `polymarket_markets`).
  - `venue='kalshi'`: new path (reads `kalshi_price_snapshots`, `kalshi_markets`; computes NO from YES).
- A new `mispricing_detector.engines.kalshi_market_state` helper (mirrors the existing `polymarket_market_state` helper) that returns the venue-agnostic `MarketState` dataclass the comparator already consumes.
- `mispricing map` CLI extended with `--venue (polymarket|kalshi)` option, default `polymarket` for backward compatibility. Kalshi mappings require explicit `--venue kalshi`.
- `mispricing map` refuses with a clear error when the supplied `<market_id>` resolves to a non-binary Kalshi market per OQ-KSI-003 widening trigger.
**Verification:**
- Cross-venue comparator test: register the same class against both Polymarket and Kalshi; `mispricing run` produces two distinct comparison rows with different `venue` values, both correctly populated.
- Non-binary-mapping refusal test: `razor-rooster mispricing map <class> <kalshi_scalar_ticker> --venue kalshi` fails with the documented error.
- Backward-compat test: existing Polymarket mappings continue to work without operator action.
**Out of scope:** the v1.2 work to widen the surface to non-binary Kalshi markets.

### T-KSI-062 — Cycle integration
**Depends on:** T-KSI-061, data_ingest T-033 (scheduler).
**References:** design §3.2, REQ-KSI-AVAIL-001 (failure isolation).
**Deliverables:**
- Kalshi sync registered with data_ingest's scheduler as a virtual source. Slot ordering: after Polymarket sync.
- Failure in Kalshi sync flows through the existing failure-isolation contract (REQ-SRC-004).
- Cycle report includes a Kalshi section per design §5.
**Verification:**
- Integration test: full cycle with Kalshi succeeding alongside Polymarket and other sources.
- Failure isolation test: Kalshi forced to 5xx; Polymarket and other sources complete; cycle report reflects partial success.
- Cross-failure test: Polymarket and Kalshi simultaneously failing → both marked failed, other sources still complete.
**Out of scope:** scheduler refactoring.

## Phase 7 — Acceptance and Operational Readiness

### T-KSI-070 — End-to-end integration test
**Depends on:** T-KSI-062, all prior tasks.
**References:** acceptance criteria in KALSHI_CONNECTOR.md §9.
**Deliverables:**
- Integration test covering: ToS ack with read_only posture, eligibility gate, daily sync (cutoff → series → events → markets → prices → settlements → trades), settlement backfill across the cutoff, watched-trade pull, on-demand orderbook with NO derivation, sector mapping including out_of_scope, removed-market handling, idempotency, cutoff-advance boundary case, cross-venue mapping with both Polymarket and Kalshi.
- Failure-injection scenarios: Kalshi 5xx, rate-limit 429 (no Retry-After), partial-page failure, ToS hash drift mid-run, posture-mismatch refusal.
- Forbidden-imports test: walks `kalshi_connector` package, asserts `cryptography.hazmat.primitives.asymmetric.padding`, `websockets`, `aiohttp.WSMsgType` are absent (REQ-KSI acceptance — codebase-level enforcement).
**Verification:** integration test passes as part of `make test`.
**Out of scope:** real-network testing.

### T-KSI-071 — Smoke test against live Kalshi
**Depends on:** T-KSI-070.
**References:** design §7.3.
**Deliverables:**
- `make smoke-kalshi` runs single-record fetches against each Kalshi public endpoint family.
- Skips cleanly when eligibility gate refuses (e.g., CI runner outside the allow-list).
- Uses a separate `data/trough_smoke.duckdb` so production data is not touched.
- Marked with `pytest.mark.smoke`, deselected from `make test` by default.
**Verification:** `make smoke-kalshi` completes inside 5 minutes locally from an allow-listed jurisdiction.
**Out of scope:** automated smoke runs.

### T-KSI-072 — First settlement backfill
**Depends on:** T-KSI-071.
**References:** NFR-KSI-PERF-002, DEFER-KSI-003.
**Deliverables:**
- Operator runs `razor-rooster kalshi backfill-settlements` on a fresh DuckDB.
- Records actual duration, settlement count (live + historical broken out), and disk footprint.
- Updates DEFER-KSI-003 with measured numbers.
- Records cutoff-advance behavior observed during the run (was a re-query needed? how often?) for future tuning.
**Verification:** measured numbers recorded in this document under a new §X-Measurements section.
**Out of scope:** ongoing backfill of newly settled markets (that's daily delta).

### T-KSI-073 — Steady-state cycle on operator hardware
**Depends on:** T-KSI-072.
**References:** NFR-KSI-PERF-001, NFR-KSI-PERF-003.
**Deliverables:**
- Three consecutive daily cycles complete inside NFR-KSI-PERF-001 (5 min for Kalshi portion).
- Disk footprint after one week of steady-state recorded against NFR-KSI-PERF-003 (5 GB target).
- Token-spend logs reviewed: confirm 50% headroom is real, not just declared.
- Cross-venue cycle report inspected: Polymarket and Kalshi sections both present, both within their respective NFRs.
**Verification:** logged cycle durations, disk usage, and token-spend in operator notes.
**Out of scope:** monitoring / alerting on cycle slowness.

### T-KSI-074 — Operator README and user-guide updates
**Depends on:** T-KSI-073.
**References:** design §8.
**Deliverables:**
- `README.md` updated with a Kalshi section: ToS ack first-run flow, eligibility allow-list config, watched-markets management, sector triage workflow, cross-venue mapping example.
- `docs/user_guide.md` updated with a `razor-rooster kalshi` section mirroring the Polymarket section's structure: ack-tos, status, sync, snapshot, backfill-settlements, watched-market management, fetch-orderbook, sector mapping triage. Configuration reference grows entries for `config/kalshi.yaml`, `config/kalshi_sector_keywords.yaml`, `config/kalshi_allowed_jurisdictions.yaml`. Common workflows section adds "First-time Kalshi setup" and "Mapping a class across both venues."
- `docs/sources.md` updated with `kalshi` entry: free public read endpoints, no v1 credentials, ToS-ack-required (read_only posture), eligibility-allow-list-aware, expected per-source disk footprint after T-KSI-072.
- `docs/kalshi_connector.md` (new): engine internals, table layout, cutoff routing, per-endpoint cost map, cross-venue mapping, post-T-KSI-072 measurement guidance — same structural pattern as `docs/monitor.md` / `docs/reports.md`.
**Verification:** a new operator could follow the README + user_guide from a clean machine to a steady-state Kalshi sync without code-reading. Cross-venue mapping example completes end-to-end.
**Out of scope:** developer architecture docs.

## Dependency Summary (Critical Path)

    T-KSI-001 → T-KSI-002 → T-KSI-010 → T-KSI-011 → T-KSI-012
                                              ↓
                              [T-DI-101] → T-MD-101 → T-PE-101 → T-MON-101 → T-RG-101
                              (Phase 1.5 cross-subsystem migrations land before Kalshi sync code)
                                              ↓
                              T-KSI-020 → T-KSI-021
                                              ↓
    T-KSI-030 → T-KSI-031 → T-KSI-032 → T-KSI-033 → T-KSI-040 → T-KSI-041 → T-KSI-042
                                                                                ↓
                                              T-KSI-050 → T-KSI-051     T-KSI-043 → T-KSI-044
                                                                                ↓
                                                                          T-KSI-060 → T-KSI-061 → T-KSI-062 → T-KSI-070 → T-KSI-072 → T-KSI-073

Phase 1.5 (cross-subsystem migrations) is the structural keystone — it must complete before any Kalshi sync code can wire in. T-DI-101 (sources.acknowledged_posture) must come first because T-KSI-021 depends on it. The four other migrations (T-MD-101, T-PE-101, T-MON-101, T-RG-101) can be sequenced in any order that respects the existing cross-subsystem reads, but T-RG-101 (rendering) is naturally last since it consumes data the other three produce.

Phases 0–2 must complete before Phase 3. Phase 4 tasks fan out from T-KSI-033/T-KSI-040 and converge at T-KSI-062. Sector-mapping tasks T-KSI-050/T-KSI-051 are on their own short branch hanging off T-KSI-041. T-KSI-061 (comparator wiring) closes the cross-subsystem loop. Phase 7 is the gate.

## Tracking

- **T-KSI-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

- **T-KSI-001** — Initialize kalshi_connector module — `DONE` — 2026-05-15 — `kalshi cli + module bootstrap + mypy strict override`
- **T-KSI-002** — Kalshi-specific configuration files — `DONE` — 2026-05-15 — `config/kalshi.yaml + config/kalshi_sector_keywords.yaml + config/kalshi_allowed_jurisdictions.yaml + Pydantic loader`
- **T-KSI-010** — Kalshi-namespaced table schemas — `DONE` — 2026-05-15 — `nine kalshi_* tables + kalshi_tos_version_history`
- **T-KSI-011** — Kalshi migration registration — `DONE` — 2026-05-15 — `m8001_kalshi_initial`
- **T-KSI-012** — Source registration and freshness participation — `DONE` — 2026-05-15 — `register_kalshi_sources (kalshi + kalshi_settlements)`
- **T-DI-101** — Add `acknowledged_posture` column to `sources` — `DONE` — 2026-05-16 — `m0002_add_acknowledged_posture`
- **T-MD-101** — Add `venue` discriminator to mispricing tables — `DONE` — 2026-05-16 — `m4002_add_venue_discriminator`
- **T-PE-101** — Add `venue` to position_engine analyses — `DONE` — 2026-05-16 — `m5002_add_venue_to_analyses + renderer venue tag`
- **T-MON-101** — Add `venue` to monitor follow_ups + branch resolution detection — `DONE` — 2026-05-16 — `m6002_add_venue_to_follow_ups + comb venue branch + reasoning venue line`
- **T-RG-101** — Render `venue` in report sections — `DONE` — 2026-05-16 — `surfaced/watched/calibration/watchlist + terminal/markdown renderer venue tags`
- **T-KSI-020** — Eligibility allow-list gate — `DONE` — 2026-05-16 — `gates/eligibility.py + 14 acceptance tests including cross-connector inversion`
- **T-KSI-021** — ToS acknowledgement gate with posture — `DONE` — 2026-05-16 — `gates/tos.py + ack-tos CLI subcommand + 10 acceptance tests`
- **T-KSI-030** — Token-bucket rate limiter with per-endpoint cost map — `DONE` — 2026-05-16 — `client/rate_limit.py + client/endpoint_costs.py + 40 acceptance tests`
- **T-KSI-031** — Retry and backoff helpers — `DONE` — 2026-05-16 — `client/retry.py + 16 acceptance tests including Retry-After-ignored invariant`
- **T-KSI-032** — User agent and HTTP client base — `DONE` — 2026-05-16 — `client/user_agent.py + 9 acceptance tests`
- **T-KSI-033** — Public REST client (markets / events / series) — `DONE` — 2026-05-16 — `client/rest.py + client/models.py + 23 acceptance tests covering all endpoints + multi-type markets + strike variants + NO-side derivation + pagination`
- **T-KSI-040** — Cutoff snapshot — `DONE` — 2026-05-16 — `sync/cutoff.py + 4 acceptance tests; single-row replace; read_cutoff helper`
- **T-KSI-041** — Daily series + events + markets sync — `DONE` — 2026-05-16 — `sync/series.py + sync/events.py + sync/markets.py + sync/_common.py + 11 acceptance tests; staging-merge with diff and removed_at`
- **T-KSI-042** — 30-min price snapshot sync — `DONE` — 2026-05-16 — `sync/prices.py + 5 acceptance tests; non-binary skipped; thin-book + null-preservation`
- **T-KSI-043** — Settlement backfill + daily delta — `DONE` — 2026-05-16 — `sync/settlements.py + 4 acceptance tests; cutoff routing live vs historical`
- **T-KSI-044** — Orderbook sampling — `DONE` — 2026-05-16 — `sync/orderbook.py + 3 acceptance tests; YES + derived NO levels persisted`
- **T-KSI-045** — Trades pull (watched markets) — `DONE` — 2026-05-16 — `sync/trades.py + 5 acceptance tests; cutoff-aware live vs historical routing; watermark idempotency`
- **T-KSI-050** — Sector heuristic mapper with `out_of_scope` enum — `DONE` — 2026-05-16 — `mapping/sector_heuristic.py + 22 acceptance tests; Pass-1 category match + Pass-2 keyword scan + Pass-3 tie handling`
- **T-KSI-051** — Sector mapping persistence + override + triage — `DONE` — 2026-05-16 — `mapping/sector_overrides.py + 13 acceptance tests + 3 markets-sync integration tests; manual override preserved across heuristic upserts`
- **T-KSI-060** — CLI subcommands — `DONE` — 2026-05-16 — `cli.py with 13 subcommands (version, ack-tos, status, sync, snapshot-prices, backfill-settlements, watch/unwatch/list-watched, fetch-orderbook, map, needs-review, mapping-stats) + 20 acceptance tests including gate-bypass invariant`
- **T-KSI-061** — Cross-subsystem comparator wiring — `DONE` — 2026-05-16 — `mispricing_detector.engines.comparator branches on venue; new _read_kalshi_market_context; mispricing map --venue option; non-binary Kalshi refusal; register_operator_mapping accepts venue; 11 acceptance tests`
- **T-KSI-062** — Cycle integration — `DONE` — 2026-05-16 — `cycle.py with run_kalshi_cycle, cycle_report_to_connector_outcome, stage_summary_lines; per-stage failure isolation mirrors polymarket_connector.cycle; 8 acceptance tests`
- **T-KSI-070** — End-to-end integration test — `DONE` — 2026-05-16 — `tests/kalshi_connector/test_t_ksi_070_end_to_end.py with 20 sub-scenarios: gates, daily-cycle happy path + idempotency, settlement backfill across cutoff, watched-trades scoping, orderbook NO-derivation, sector mapping with out_of_scope, removed-market handling, cross-venue mapping, 5xx + 429 + posture-mismatch failure injection, forbidden-imports acceptance check`
- **T-KSI-071** — Smoke test against live Kalshi — `OPERATOR_DRIVEN` — pending operator hardware first-run
- **T-KSI-072** — First settlement backfill — `OPERATOR_DRIVEN` — pending operator hardware first-run
- **T-KSI-073** — Steady-state cycle on operator hardware — `OPERATOR_DRIVEN` — pending operator hardware first-run
- **T-KSI-074** — Operator README and user-guide updates — `DONE` — 2026-05-16 — `docs/kalshi_connector.md (new), README.md Kalshi-connector section + status table + threat-context, docs/sources.md kalshi + kalshi_settlements rows + cross-venue note, docs/user_guide.md section 5b (Kalshi CLI reference) + section 12 config files (kalshi.yaml + kalshi_sector_keywords.yaml + kalshi_allowed_jurisdictions.yaml) + section 13 workflows (First-time Kalshi setup, Mapping a class across both venues)`

The cross-subsystem migration tasks (T-DI-101, T-MD-101, T-PE-101, T-MON-101, T-RG-101) also appear in the respective subsystem's TASKS.md tracking section so their progress is visible from each subsystem's perspective. Each subsystem's task tracker carries the same status.

## References

- Requirements: `KALSHI_CONNECTOR.md` v0.1.0
- Design: `KALSHI_CONNECTOR_DESIGN.md` v0.1.0
- Sibling subsystem: `POLYMARKET_CONNECTOR_TASKS.md` v0.1.0 (the working reference for connector structural decisions)
- LOOM: `razorrooster.md` v0.35.1 (current). v0.36.0 records the addition of `kalshi_connector` to the subsystem registry, the cross-subsystem `venue` discriminator migrations, the `data_ingest.sources.acknowledged_posture` column, and the lifecycle-stage advance to `IMPLEMENTATION_IN_PROGRESS` once T-KSI-001 starts.
- `data_ingest`, `mispricing_detector`, `position_engine`, `monitor`, `report_generator` specs (Requirements/Design/Tasks v0.1.0) — for shared infrastructure that Kalshi consumes and the cross-subsystem changes that ride alongside the connector landing.
