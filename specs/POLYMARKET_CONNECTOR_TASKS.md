# POLYMARKET_CONNECTOR — Implementation Tasks

**Subsystem:** `polymarket_connector`
**Codename:** The Wire
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `POLYMARKET_CONNECTOR.md` v0.1.0
- Design: `POLYMARKET_CONNECTOR_DESIGN.md` v0.1.0

**Hard prerequisite:** `data_ingest` Phase 0–3 tasks (T-001 through T-035) must be DONE before any Polymarket task can start. The connector reuses DuckDBStore, staging-merge, scheduler, structured logging, and the credential-redaction filter. Without those, Polymarket tasks have nothing to plug into.

---

## How to Read This Document

Same conventions as `DATA_INGEST_TASKS.md`: each task has stable ID, dependencies, references back to requirement and design IDs, deliverables, verification, and out-of-scope guards.

Task IDs are prefixed `T-PMC-NNN` to distinguish from `data_ingest` tasks.

## Phase 0 — Module Bootstrap

### T-PMC-001 — Initialize polymarket_connector module
**Depends on:** data_ingest T-002 (module skeleton convention established).
**References:** design §3.1 module layout.
**Deliverables:**
- Create the `razor_rooster/polymarket_connector/` directory tree per design §3.1.
- Empty `__init__.py` in each subdirectory.
- `cli.py` with a single `click` group `razor-rooster polymarket` that prints the available subcommands and exits.
- Mirror test layout under `tests/polymarket_connector/`.
**Verification:** `razor-rooster polymarket --help` shows the command group; `pytest` discovers and runs zero tests cleanly.
**Out of scope:** any logic.

### T-PMC-002 — Polymarket-specific configuration files
**Depends on:** T-PMC-001, data_ingest T-022 (config loader pattern established).
**References:** design §4 configuration, REQ-PMC-PRICE-003, REQ-PMC-RATE-001.
**Deliverables:**
- `config/polymarket.yaml` populated with the structure from design §4.
- `config/sector_keywords.yaml` with a small curated initial keyword set per sector (DEFER-PMC-001 acknowledges this will expand).
- `config/restricted_jurisdictions.yaml` listing currently-blocked jurisdictions per Polymarket help docs.
- `config/loader.py` extended (or sibling loader added) to validate Polymarket config via Pydantic.
**Verification:** unit test confirms valid configs parse, invalid configs (unknown fields, missing required) fail with informative errors.
**Out of scope:** runtime config reloading.

## Phase 1 — Schemas and Migrations

### T-PMC-010 — Polymarket-namespaced table schemas
**Depends on:** T-PMC-001, data_ingest T-013 (migrations framework).
**References:** design §3.3 (all five tables + sector mapping), REQ-PMC-MARKET-002, REQ-PMC-PRICE-002, REQ-PMC-OB-002, REQ-PMC-RES-002, REQ-PMC-TRADE-002.
**Deliverables:**
- DDL strings for `polymarket_markets`, `polymarket_price_snapshots`, `polymarket_orderbook_snapshots`, `polymarket_trades`, `polymarket_resolutions`, `polymarket_sector_mapping`.
- DDL for the `tos_version_history` table referenced by gates (design §3.8).
- Each schema includes the provenance prefix from `data_ingest` design §4.
**Verification:** schemas applied to an in-memory DuckDB; round-trip test inserts a synthetic row per table and queries it back. Indexes confirmed via `EXPLAIN`.
**Out of scope:** the migration runner registration (T-PMC-011).

### T-PMC-011 — Polymarket migration registration
**Depends on:** T-PMC-010
**References:** design §3.2 (reuse from data_ingest).
**Deliverables:**
- `persistence/migrations/m0001_polymarket_initial.py` applying all DDL from T-PMC-010.
- The migration registers with the `data_ingest` migrations framework (no separate runner needed).
- Migration version recorded in `schema_migrations` table with description.
**Verification:** open a fresh store → m0001 + Polymarket m0001 both applied; reopen → no migration runs.
**Out of scope:** subsequent migrations.

### T-PMC-012 — Source registration and freshness participation
**Depends on:** T-PMC-011, data_ingest T-015 (provenance helpers), data_ingest T-032 (registry).
**References:** REQ-PMC-PROV-001, REQ-PMC-PROV-002, design §3.2, design §3.9.
**Deliverables:**
- Register `source_id = 'polymarket'` in the `sources` table on first connector startup.
- The `freshness` view automatically picks up Polymarket entries — no view changes needed since the view reads `sources` table generically.
- A `polymarket_resolutions_source` virtual entry tracks resolutions freshness separately from live-price freshness, since they have different thresholds.
**Verification:** integration test confirms `freshness` view returns Polymarket entry with correct staleness flag based on `last_successful_fetch`.
**Out of scope:** populating `last_successful_fetch` (sync code does that).

## Phase 2 — Gates (Geo and ToS)

These gates run before any Polymarket API call. Build them before the API client so the client can never be invoked from a misconfigured operator state.

### T-PMC-020 — Geo-restriction gate
**Depends on:** T-PMC-002
**References:** REQ-PMC-GEO-001, REQ-PMC-GEO-002, design §3.7.
**Deliverables:**
- `gates/geo.py` with `check_jurisdiction()` per design §3.7.
- `RESTRICTED_JURISDICTIONS` loaded from `config/restricted_jurisdictions.yaml`, not hard-coded.
- Refusal raises a typed `StartupRefusal` exception with a clear message.
- The gate is invoked by `cli.py` for every Polymarket subcommand and by the connector's sync entry points.
**Verification:**
- Unit test: missing config → refusal.
- Unit test: restricted jurisdiction → refusal.
- Unit test: permitted jurisdiction → pass.
- Integration test: `razor-rooster polymarket sync` with restricted jurisdiction fails fast with the documented error message.
**Out of scope:** auto-detection of jurisdiction (we explicitly do not do that — operator declares).

### T-PMC-021 — ToS acknowledgement gate
**Depends on:** T-PMC-012, data_ingest T-014 (staging-merge for atomic ack writes), T-PMC-020.
**References:** REQ-PMC-TOS-001, design §3.8.
**Deliverables:**
- `gates/tos.py` with `check_tos_acknowledged(store)` per design §3.8.
- `cli.py` gains the `ack-tos` subcommand: fetches current ToS hash, displays URL, prompts for confirmation, writes ack to `sources` table.
- ToS hash fetch with a fallback to last-known hash from `tos_version_history` if the live URL is unreachable.
- If both live fetch and last-known fail, refusal is raised.
**Verification:**
- Unit test: no prior ack → refusal.
- Unit test: matching hash → pass.
- Unit test: changed hash → refusal with re-prompt instructions.
- Unit test: live fetch fails, last-known matches → pass.
- Unit test: live fetch fails, no last-known → refusal.
- Integration test: full ack flow from fresh DB to acknowledged state.
**Out of scope:** automated re-acknowledgement; PDF or HTML rendering of ToS in-terminal.

## Phase 3 — HTTP Client Layer

### T-PMC-030 — Token-bucket rate limiter
**Depends on:** T-PMC-002
**References:** REQ-PMC-RATE-001, design §3.6.
**Deliverables:**
- `client/rate_limit.py` with a thread/async-safe `TokenBucket(capacity, refill_per_second)` class.
- `acquire(timeout=None)` blocks until a token is available or the timeout elapses.
- A module-level singleton bucket configured per `config/polymarket.yaml`.
**Verification:**
- Unit test: bucket drains under sustained load and refills correctly.
- Unit test: parallel acquirers do not exceed cap when measured over a 1-second window.
- Unit test: timeout behavior — `acquire(timeout=...)` raises a documented exception when bucket can't refill in time.
**Out of scope:** distributed limiting across processes (single-process only in v1).

### T-PMC-031 — Retry and backoff helpers
**Depends on:** T-PMC-030
**References:** REQ-PMC-RATE-002.
**Deliverables:**
- `client/retry.py` with `retry_with_backoff(callable, max_retries=5, base=1, max_seconds=60)`.
- Jittered exponential backoff.
- Detection of 429-class responses and other rate-limit signals.
- Structured log entry on each retry.
**Verification:**
- Unit test: synthetic 429 sequence triggers correct backoff timing (use a clock injection for deterministic test).
- Unit test: persistent failure exhausts retries and surfaces the original error.
**Out of scope:** circuit-breaker pattern (overkill for this scale).

### T-PMC-032 — User agent and HTTP client base
**Depends on:** T-PMC-030, T-PMC-031
**References:** NFR-PMC-TOS-001, design §3.1.
**Deliverables:**
- `client/user_agent.py` building the User-Agent string per NFR-PMC-TOS-001.
- A shared `httpx.AsyncClient` factory that wires in the limiter (T-PMC-030), retry decorator (T-PMC-031), User-Agent header, and timeout configuration.
- All API client modules use this factory — no raw `httpx.Client` instantiation outside it.
**Verification:** unit test confirms factory-built clients have the expected headers, timeout, and retry/limiter wiring.
**Out of scope:** HTTP/2 specifics, connection-pool tuning beyond defaults.

### T-PMC-033 — Gamma API client
**Depends on:** T-PMC-032
**References:** REQ-PMC-MARKET-001, REQ-PMC-RES-001, REQ-PMC-RES-003, design §3.1.
**Deliverables:**
- `client/gamma.py` with typed methods: `list_markets(active=True, closed=False, limit, offset)`, `list_resolved(since=None, limit, offset)`, `get_market(condition_id)`, `get_event(event_id)`.
- Pagination handled internally where applicable.
- Raw response payload preserved per record (round-trip in `source_payload_json`).
**Verification:**
- Recorded-fixture unit tests for representative responses (markets list, resolved list, single market lookup).
- Smoke test against the live Gamma API pulls a small page successfully.
**Out of scope:** any sync logic — this is just the API client.

### T-PMC-034 — CLOB public client
**Depends on:** T-PMC-032
**References:** REQ-PMC-PRICE-001, REQ-PMC-OB-001, REQ-PMC-TRADE-001, design §3.1.
**Deliverables:**
- `client/clob_public.py` with: `get_price(token_id)`, `get_orderbook(token_id, depth)`, `get_trades(token_id, since, limit)`.
- All methods return typed dataclasses; raw payload preserved.
**Verification:**
- Recorded-fixture unit tests.
- Smoke test against live CLOB public endpoints.
**Out of scope:** any L1/L2/authenticated CLOB methods — those don't exist in v1 codebase.

## Phase 4 — Sync Operations

### T-PMC-040 — Daily markets sync
**Depends on:** T-PMC-021 (TOS gate), T-PMC-033 (Gamma client), T-PMC-011 (schemas), data_ingest T-014 (staging-merge).
**References:** REQ-PMC-MARKET-001..003, design §3.4 (Daily metadata sync).
**Deliverables:**
- `sync/markets.py` with `sync_markets(store) -> MarketSyncReport` per design §3.4.
- Diff logic identifies inserted, updated, removed markets.
- Removed markets get `removed_at = now()`; rows are never deleted.
- Each market triggers a sector-mapping run (T-PMC-050).
**Verification:**
- Unit test against mock Gamma responses across three "snapshots in time" — confirms inserts, updates, removals across runs.
- Integration test: full sync against in-memory DuckDB and a comprehensive mock state.
- Idempotency: re-running a sync with identical state changes nothing.
**Out of scope:** real-time market discovery (we only sync once per day).

### T-PMC-041 — Hourly price snapshot sync
**Depends on:** T-PMC-034 (CLOB public client), T-PMC-040 (markets must exist before snapshotting), T-PMC-011 (schemas), data_ingest T-014 (staging-merge).
**References:** REQ-PMC-PRICE-001..004, design §3.4 (Hourly price snapshots).
**Deliverables:**
- `sync/prices.py` with `snapshot_prices(store, market_ids=None, watched_only=False) -> PriceSnapshotReport`.
- Computes `spread_bps` and `liquidity_warning` per REQ-PMC-PRICE-004.
- NULL preservation per REQ-PMC-PRICE-004.
- Batches writes via staging-merge.
- Multi-outcome markets (>2 outcomes) are skipped per design §2 OQ-PMC-004 resolution.
**Verification:**
- Unit test with thin-orderbook fixture: NULLs preserved, `liquidity_warning = TRUE`.
- Unit test with normal-orderbook fixture: full row populated.
- Unit test with multi-outcome market: skipped, logged.
- Rate-budget test: 1,000 mock markets snapshot in a synthetic run completes inside the rate budget.
**Out of scope:** RTDS/WebSocket source (deferred per OQ-PMC-002).

### T-PMC-042 — Resolution backfill
**Depends on:** T-PMC-033, T-PMC-011, data_ingest T-034 (backfill resume mechanism).
**References:** REQ-PMC-RES-001..003, design §3.4 (Resolution backfill + daily delta).
**Deliverables:**
- `sync/resolutions.py` with `backfill_resolutions(store, until=None) -> BackfillReport`.
- Resumable per data_ingest's existing backfill machinery — uses `backfill_state` table.
- Each resolved market also updates the corresponding `polymarket_markets` row (`resolved = TRUE`, `closed = TRUE`).
**Verification:**
- Unit test against a paginated mock with 1,000 synthetic resolutions.
- Resume test: kill mid-page, re-run, confirm continuation without duplicates.
- Smoke test: real backfill against live Gamma; record duration and result count.
**Out of scope:** third-party archival sources for missing history.

### T-PMC-043 — Daily resolution delta
**Depends on:** T-PMC-042
**References:** REQ-PMC-RES-001 (with `since` parameter), design §3.4.
**Deliverables:**
- `sync/resolutions.py` `sync_recent_resolutions(store) -> SyncReport` pulling resolutions since `last_successful_fetch`.
- Same upsert pathway as backfill.
**Verification:** integration test simulates two-day gap; sync pulls only the missing window.
**Out of scope:** intraday resolution sync.

### T-PMC-044 — Watched-markets trade pull
**Depends on:** T-PMC-034, T-PMC-011, T-PMC-040.
**References:** REQ-PMC-TRADE-001..003, design §3.4 (Trades pull).
**Deliverables:**
- `sync/trades.py` `pull_watched_trades(store) -> TradePullReport`.
- Reads watched_markets from config; for each, pulls trades since last successful pull.
- Tx-hash-based dedup per design §3.3 (`PRIMARY KEY (tx_hash, outcome_token_id)`).
**Verification:**
- Unit test with mock trade history: deduped against a re-pull.
- Empty-watched-markets case: completes immediately, logs "no watched markets."
**Out of scope:** unwatched-market trade pull on the daily cycle.

### T-PMC-045 — On-demand orderbook fetch
**Depends on:** T-PMC-034, T-PMC-011.
**References:** REQ-PMC-OB-001, REQ-PMC-OB-002, design §3.4 (Orderbook pull).
**Deliverables:**
- `sync/orderbook.py` `fetch_orderbook(condition_id, outcome_token_id, depth=10, persist=False) -> Orderbook`.
- Returns in-memory `Orderbook` dataclass.
- Persists to `polymarket_orderbook_snapshots` only when `persist=True`.
- Default behavior verified to NOT write.
**Verification:**
- Unit test confirms default path does not write to store.
- Unit test confirms `persist=True` writes correctly.
- Smoke test against live API returns plausible bid/ask structure.
**Out of scope:** continuous orderbook tracking.

## Phase 5 — Sector Mapping

### T-PMC-050 — Sector heuristic mapper
**Depends on:** T-PMC-002, T-PMC-011 (sector_mapping table).
**References:** OQ-PMC-001 resolution, design §3.5.
**Deliverables:**
- `mapping/sector_heuristic.py` implementing the three-pass mapper from design §3.5.
- Reads keywords from `config/sector_keywords.yaml`.
- Returns `SectorMapping(razor_sector, secondary_sectors, confidence)` with `razor_sector=None` for unmappable inputs.
- Logs every classification with the inputs that drove the decision.
**Verification:**
- Unit tests across the six sectors with representative market questions.
- Unit test for ambiguous input: returns `None` and logs the ambiguity.
- Unit test confirms `confidence='inferred'` on heuristic output.
**Out of scope:** ML-based mapping (heuristic only in v1).

### T-PMC-051 — Sector mapping persistence and override CLI
**Depends on:** T-PMC-050, T-PMC-040.
**References:** OQ-PMC-001 resolution, OQ-PMC-006 resolution, design §3.5.
**Deliverables:**
- `mapping/sector_overrides.py` with `set_override(store, condition_id, sector, secondary=None)`.
- `cli.py` gains:
  - `razor-rooster polymarket map <condition_id> <sector> [--secondary ...]` writes a `confidence='manual'` row.
  - `razor-rooster polymarket needs-review` lists markets with `razor_sector IS NULL`.
  - `razor-rooster polymarket mapping-stats` shows counts by sector and confidence.
- Markets sync (T-PMC-040) calls the heuristic mapper for new/changed markets and upserts the result; existing `manual` overrides are preserved.
**Verification:**
- Integration test: heuristic produces inferred mapping, operator overrides to manual, subsequent sync does not overwrite the manual entry.
- CLI test: `needs-review` returns the expected list for a populated DB.
**Out of scope:** bulk-override import.

## Phase 6 — CLI and Cycle Integration

### T-PMC-060 — CLI subcommands
**Depends on:** T-PMC-040, T-PMC-041, T-PMC-042, T-PMC-044, T-PMC-045, T-PMC-051.
**References:** design §8.1–§8.5.
**Deliverables:**
- `razor-rooster polymarket sync` — runs markets, prices, resolutions, trades in dependency order.
- `razor-rooster polymarket snapshot [--watched]` — runs price snapshots only.
- `razor-rooster polymarket backfill-resolutions` — initial historical pull.
- `razor-rooster polymarket watch <condition_id> [--cadence ...] [--orderbook] [--trades]`.
- `razor-rooster polymarket unwatch <condition_id>`.
- `razor-rooster polymarket list-watched`.
- `razor-rooster polymarket fetch-orderbook <condition_id>` (in-memory display only, no persist).
- All commands invoke geo gate + ToS gate before any API call.
**Verification:**
- CLI test for each subcommand against a mock Polymarket state.
- Gate-bypass test: confirm no subcommand reaches API code paths if a gate raises.
**Out of scope:** colored / TUI output beyond plain text.

### T-PMC-061 — Cycle integration
**Depends on:** T-PMC-060, data_ingest T-033 (scheduler).
**References:** design §3.2, REQ-PMC-RES-001a (failure isolation).
**Deliverables:**
- Polymarket sync registered with data_ingest's scheduler as a virtual source.
- Failure in Polymarket sync flows through the existing failure-isolation contract.
- Cycle report includes a Polymarket section per design §5.
**Verification:**
- Integration test: full cycle with Polymarket succeeding alongside other sources.
- Failure isolation test: Polymarket forced to 5xx; other sources complete; cycle report reflects partial success.
**Out of scope:** scheduler refactoring.

## Phase 7 — Acceptance and Operational Readiness

### T-PMC-070 — End-to-end integration test
**Depends on:** T-PMC-061, all prior tasks.
**References:** acceptance criteria in POLYMARKET_CONNECTOR.md §9.
**Deliverables:**
- Integration test covering: ToS ack, geo gate, daily sync, hourly snapshots, resolution backfill, watched-trade pull, on-demand orderbook, sector mapping, removed-market handling, idempotency.
- Failure-injection scenarios: Polymarket 5xx, rate-limit 429, partial-page failure, ToS hash drift mid-run.
**Verification:** integration test passes as part of `make test`.
**Out of scope:** real-network testing.

### T-PMC-071 — Smoke test against live Polymarket
**Depends on:** T-PMC-070.
**References:** design §7.3.
**Deliverables:**
- `make smoke-polymarket` runs single-record fetches against each Polymarket endpoint family.
- Skips cleanly when geo gate refuses (e.g., CI runner in restricted region).
- Uses a separate `data/trough_smoke.duckdb` so production data is not touched.
**Verification:** `make smoke-polymarket` completes inside 5 minutes locally.
**Out of scope:** automated smoke runs.

### T-PMC-072 — First resolution backfill
**Depends on:** T-PMC-071.
**References:** NFR-PMC-PERF-002, DEFER-PMC-003.
**Deliverables:**
- Operator runs `razor-rooster polymarket backfill-resolutions` on a fresh DuckDB.
- Records actual duration, resolution count, and disk footprint.
- Updates DEFER-PMC-003 with measured numbers.
- If pagination edge cases discovered, captured as findings and addressed in a follow-up patch.
**Verification:** measured numbers recorded in this document under a new §X-Measurements section.
**Out of scope:** ongoing backfill of newly resolved markets (that's daily delta).

### T-PMC-073 — Steady-state cycle on operator hardware
**Depends on:** T-PMC-072.
**References:** NFR-PMC-PERF-001, NFR-PMC-PERF-003.
**Deliverables:**
- Three consecutive daily cycles complete inside NFR-PMC-PERF-001 (5 min for Polymarket portion).
- Disk footprint after one week of steady-state recorded against NFR-PMC-PERF-003 (5 GB target).
**Verification:** logged cycle durations and disk usage in operator notes.
**Out of scope:** monitoring / alerting on cycle slowness.

### T-PMC-074 — Operator README updates
**Depends on:** T-PMC-073.
**References:** design §8.
**Deliverables:**
- `README.md` updated with a Polymarket section: ToS ack first-run flow, geo gate config, watched-markets management, sector triage workflow.
- `docs/sources.md` updated with `polymarket` entry: free public APIs, no credentials, ToS-ack-required, geographic-restriction-aware, expected per-source disk footprint after T-PMC-072.
**Verification:** a new operator could follow the README from a clean machine to a steady-state Polymarket sync without code-reading.
**Out of scope:** developer architecture docs (the spec files cover that).

## Dependency Summary (Critical Path)

    T-PMC-001 → T-PMC-002 → T-PMC-010 → T-PMC-011 → T-PMC-012 → T-PMC-020 → T-PMC-021
                                                                              ↓
    T-PMC-030 → T-PMC-031 → T-PMC-032 → T-PMC-033 → T-PMC-040 → T-PMC-041 → T-PMC-061 → T-PMC-070 → T-PMC-072 → T-PMC-073

Sync tasks T-PMC-040..T-PMC-045 fan out from T-PMC-033/T-PMC-034 and converge at T-PMC-061. Sector-mapping tasks T-PMC-050/T-PMC-051 are on their own short branch hanging off T-PMC-040.

Phases 0–2 must complete before Phase 3. Phase 4 tasks can interleave with Phase 5. Phase 6 closes the loop. Phase 7 is the gate.

## Tracking

- **T-PMC-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.26.0):

- **T-PMC-001** — Initialize polymarket_connector module — `DONE` — 2026-05-15
- **T-PMC-002** — Polymarket-specific configuration files — `DONE` — 2026-05-15
- **T-PMC-010** — Polymarket-namespaced table schemas — `DONE` — 2026-05-15
- **T-PMC-011** — Polymarket migration registration — `DONE` — 2026-05-15
- **T-PMC-012** — Source registration and freshness participation — `DONE` — 2026-05-15
- **T-PMC-020** — Geo-restriction gate — `DONE` — 2026-05-15
- **T-PMC-021** — ToS acknowledgement gate — `DONE` — 2026-05-15
- **T-PMC-030** — Token-bucket rate limiter — `DONE` — 2026-05-15
- **T-PMC-031** — Retry and backoff helpers — `DONE` — 2026-05-15
- **T-PMC-032** — User agent and HTTP client base — `DONE` — 2026-05-15
- **T-PMC-033** — Gamma API client — `DONE` — 2026-05-15
- **T-PMC-034** — CLOB public client — `DONE` — 2026-05-15
- **T-PMC-040** — Daily markets sync — `DONE` — 2026-05-15
- **T-PMC-041** — Hourly price snapshot sync — `DONE` — 2026-05-15
- **T-PMC-042** — Resolution backfill — `DONE` — 2026-05-15
- **T-PMC-043** — Daily resolution delta — `DONE` — 2026-05-15
- **T-PMC-044** — Watched-markets trade pull — `DONE` — 2026-05-15
- **T-PMC-045** — On-demand orderbook fetch — `DONE` — 2026-05-15
- **T-PMC-050** — Sector heuristic mapper — `DONE` — 2026-05-15
- **T-PMC-051** — Sector mapping persistence + override CLI — `DONE` — 2026-05-15
- **T-PMC-060** — CLI subcommands — `DONE` — 2026-05-15
- **T-PMC-061** — Cycle integration — `DONE` — 2026-05-15
- **T-PMC-070** — End-to-end integration test — `DONE` — 2026-05-15
- **T-PMC-071** — Smoke test against live Polymarket — `DONE` — 2026-05-15
- **T-PMC-072** — First resolution backfill — `OPERATOR_BLOCKED` — pending operator-initiated live run
- **T-PMC-073** — Steady-state cycle on operator hardware — `OPERATOR_BLOCKED` — pending operator-initiated live run
- **T-PMC-074** — Operator README updates — `DONE` — 2026-05-15

Phases 0, 1, 2, 3, 4, 5, 6 fully complete. Phase 7 partially complete: T-PMC-070, T-PMC-071, T-PMC-074 done; T-PMC-072 and T-PMC-073 require live network and operator hardware and are deferred to operator-driven runs.

## References

- Requirements: `POLYMARKET_CONNECTOR.md` v0.1.0
- Design: `POLYMARKET_CONNECTOR_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md` v0.5.0
- `data_ingest` specs (Requirements/Design/Tasks v0.1.0) — for shared infrastructure that Polymarket consumes.
