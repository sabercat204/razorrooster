# DATA_INGEST — Requirements

**Subsystem:** `data_ingest`
**Codename:** The Trough
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14

---

## 1. Purpose

`data_ingest` is the public-data acquisition layer for Razor-Rooster. It is responsible for:

- Pulling raw data from a defined set of public sources across the six domain sectors (Public Health, Geopolitical Instability, Regulatory/Policy, Commodity/Supply Chain, Climate/Environmental, Infrastructure/Energy).
- Normalizing that data into a consistent, source-tagged, timestamped schema.
- Persisting it in a local DuckDB store that downstream subsystems (`pattern_library`, `signal_scanner`, `monitor`) can query without hitting the original APIs.
- Maintaining historical backfill depth sufficient for 50-year base-rate analysis where the source supports it.
- Operating without unattended financial risk — this is a read-only, public-data layer.

`data_ingest` does not interpret, score, or make claims about the data. It delivers clean inputs.

## 2. Scope

### In scope

- Scheduled and on-demand pulls from the data sources listed in section 4.
- Source-by-source rate-limit handling and retry logic.
- Schema normalization to a canonical form per data type (event-stream, time-series, document/docket, geospatial-indicator).
- Local persistence in DuckDB with deterministic table layout.
- Deduplication on re-fetch.
- Backfill orchestration (one-time historical pull) separate from incremental updates.
- Provenance tracking: every record stored carries source identifier, source-side ID where available, fetch timestamp, and source publication timestamp.
- Failure logging and a freshness/health view that downstream subsystems can read to know how stale each feed is.

### Out of scope (explicit)

- Pattern matching, scoring, or probability estimation. (`pattern_library`, `signal_scanner`)
- Polymarket API access. (`polymarket_connector` — different threat context, separate spec.)
- Any data sources that require paid licensing in v1. Free / open / public-tier only.
- Real-time streaming. The cadence is scheduled batch pulls; latency requirements are measured in hours, not seconds.
- Data correction or imputation. If a source publishes a value, we store it as-is. Downstream subsystems handle interpretation.

## 3. Stakeholders & Operating Assumptions

- **Operator:** single user (Daniel), running locally on EliteBook G8 (i7-8665U, 16GB DDR4, no GPU).
- **Compute envelope:** all ingest must fit within local memory and disk. Target: full 50-year multi-domain corpus under 100 GB on disk, working memory under 8 GB during normal operation.
- **Network:** intermittent residential connectivity assumed; pulls must tolerate transient failures and resume.
- **Cadence:** most sources update daily or weekly; ingest cadence per source is configurable but defaults to daily for fast-moving feeds and weekly for slow-moving ones.

## 4. Data Source Inventory (v1)

The following sources are in scope for v1. Each has an associated requirement section (REQ-SRC-*) below. Sources marked **TBC** require feasibility confirmation in the design phase before implementation.

| Source | Sector | Access | Cadence | Backfill |
|--------|--------|--------|---------|----------|
| FRED (Federal Reserve Economic Data) | Commodity, Infrastructure | Public API, registered key | Daily | 50+ years |
| World Bank Open Data | Commodity, Climate, Geopolitical | Public API | Weekly | 50+ years |
| WHO Disease Outbreak News (DON) | Public Health | Public RSS / scrape | Daily | ~30 years |
| ACLED (Armed Conflict Location & Event Data) — events + deleted | Geopolitical Instability | OAuth 2.0 password grant; user account required; per-source ToS acknowledgement | Daily | Bounded by ACLED account-tier; events 1997+ where account permits |
| GDELT 2.0 Event & GKG | Geopolitical Instability | Public bulk download | Daily | ~10–40 years |
| Federal Register API | Regulatory/Policy | Public API | Daily | 1994+ |
| NOAA Climate Data Online + ENSO indices | Climate/Environmental | Public API | Daily | 50+ years |
| USGS Mineral Commodity Summaries | Commodity/Supply Chain | Public download (annual) | Annual | 50+ years |
| EIA (U.S. Energy Information Admin) | Infrastructure/Energy | Public API | Daily/Weekly | 30+ years |
| NRC (Nuclear Regulatory Commission) ADAMS | Regulatory/Policy | Public search/document store | Weekly | 1999+ |
| EPA rulemaking dockets (regulations.gov) | Regulatory/Policy | Public API | Daily | 2003+ |
| OPEC Monthly Oil Market Report (MOMR) | Commodity | Public PDF (TBC: parsing complexity) | Monthly | ~25 years |
| Baltic Dry Index (BDI) via FRED proxy | Commodity/Supply Chain | Via FRED | Daily | 30+ years |

Sources can be added or removed in later versions through a defined registration mechanism (see REQ-EXT-001).

## 5. Functional Requirements

Requirements use EARS-style phrasing. Each requirement has a stable ID and a verification note describing how compliance can be checked.

### 5.1 Source connectors

**REQ-SRC-001: Per-source connector module**
The system **shall** provide a separate connector module per data source listed in section 4, each implementing a common ingestion interface (fetch, normalize, persist).
*Verification:* unit tests per connector confirm interface conformance.

**REQ-SRC-002: Source-specific rate-limit handling**
Each connector **shall** respect the documented rate limits of its source and **shall** implement exponential backoff with jitter on 429 / 503 responses, capped at 5 retries before logging failure and proceeding.
*Verification:* unit test injects 429 responses and confirms backoff sequence; integration test confirms successful pull under rate-limited conditions.

**REQ-SRC-003: API key isolation**
When a source requires authentication, the connector **shall** read credentials from a local `.env` file via environment variables and **shall not** persist credentials in the DuckDB store, in logs, or in error messages.
*Verification:* code review confirms no credential paths into storage; log-scan test confirms credential strings never appear in log output.

**REQ-SRC-004: Failure isolation**
A failure in one connector **shall not** halt other connectors in the same ingest cycle. Each connector **shall** log its own failures and the cycle **shall** continue with remaining connectors.
*Verification:* integration test forces failure in one connector and confirms others complete.

### 5.2 Normalization

**REQ-NORM-001: Canonical schemas**
Ingested records **shall** be normalized into one of four canonical schemas based on data type:
- **event-stream** (point-in-time discrete events: ACLED incidents, GDELT events, WHO DON entries, Federal Register filings)
- **time-series** (numeric value at a timestamp: FRED indices, NOAA climate readings, EIA stock levels)
- **document/docket** (structured documents with metadata: NRC ADAMS, regulations.gov dockets, OPEC MOMR)
- **geospatial-indicator** (value indexed by geography and time: drought indices, ENSO state, wildfire risk by region)

Each canonical schema **shall** carry source identifier, source-side record ID, fetch timestamp, source publication timestamp, and a JSON payload of source-native fields preserved verbatim.
*Verification:* schema definitions checked into spec; round-trip test confirms source-native payload is recoverable from stored record.

**REQ-NORM-002: Timestamp normalization**
All timestamps **shall** be stored in UTC, ISO-8601 format. Source-native timezone information **shall** be preserved in the JSON payload.
*Verification:* unit test confirms all storage paths convert to UTC; payload inspection confirms original timezone is retained.

**REQ-NORM-003: No silent transformation**
Normalization **shall not** drop, infer, or correct values from the source. Missing values are stored as NULL with their source-native marker preserved in the payload.
*Verification:* code review and connector unit tests confirm no imputation logic.

### 5.3 Persistence

**REQ-PERSIST-001: DuckDB local store**
All normalized data **shall** persist in a single local DuckDB database file at a configurable path (default: `~/Projects/razor-rooster/data/trough.duckdb`).
*Verification:* integration test confirms records readable from DuckDB after ingest cycle.

**REQ-PERSIST-002: Deterministic table layout**
The store **shall** maintain one table per canonical schema (`event_stream`, `time_series`, `document_docket`, `geospatial_indicator`), with source identifier as a first-class indexed column. A separate `sources` table **shall** track per-source metadata (name, type, last successful fetch, last attempted fetch, freshness threshold).
*Verification:* schema migration test confirms layout; downstream subsystems can query a single table per type rather than per source.

**REQ-PERSIST-003: Idempotent writes**
A re-fetch of records the system has already stored **shall not** create duplicates. Deduplication **shall** use the composite key `(source_id, source_record_id)`.
*Verification:* integration test runs the same ingest cycle twice and confirms row counts are stable.

**REQ-PERSIST-004: Update semantics for revised records**
When a source publishes a corrected version of a previously fetched record (same `source_record_id`, different content), the system **shall** store the new version and retain the prior version with a `superseded_at` timestamp. No record is silently overwritten.
*Verification:* unit test simulates source revision and confirms both versions queryable.

### 5.4 Backfill vs incremental

**REQ-BACKFILL-001: One-time historical backfill**
Each connector **shall** support a backfill mode that pulls the maximum available history for the source (or a configurable cap), separate from the incremental daily/weekly cadence.
*Verification:* integration test runs backfill against a small sample source and confirms historical depth.

**REQ-BACKFILL-002: Resume on interruption**
Backfill **shall** be resumable. If interrupted, a subsequent backfill run **shall** resume from the last successfully persisted record rather than restarting from the beginning.
*Verification:* integration test interrupts a backfill mid-run and confirms resume completes the corpus without duplication and without restarting from scratch.

**REQ-BACKFILL-003: Storage cap respected**
Backfill **shall** respect a configurable per-source size cap and a global corpus cap. When the cap is approached, the connector **shall** log a warning and stop ingest for that source rather than silently exceeding disk budget.
*Verification:* integration test sets a small cap and confirms graceful stop.

### 5.5 Scheduling & orchestration

**REQ-SCHED-001: Cadence configuration**
Ingest cadence **shall** be configurable per source via a single config file (`config/ingest_schedule.yaml`). Defaults: daily for fast-moving sources, weekly for slow-moving.
*Verification:* config-driven test confirms cadence applied per source.

**REQ-SCHED-002: Cycle orchestration**
A single `ingest cycle` command **shall** run all connectors due for execution according to their cadence and **shall** report per-connector success, record-count, and duration.
*Verification:* CLI integration test runs a full cycle and confirms structured output.

**REQ-SCHED-003: Manual single-source pull**
The operator **shall** be able to invoke a single connector by name from the CLI for ad-hoc pulls and debugging.
*Verification:* CLI integration test invokes one connector and confirms isolated execution.

### 5.6 Provenance & freshness

**REQ-PROV-001: Per-record provenance**
Every stored record **shall** be queryable for: source identifier, source URL or endpoint, fetch timestamp, source publication timestamp, and the connector version that ingested it.
*Verification:* DuckDB query returns full provenance for any record.

**REQ-PROV-002: Freshness view**
The system **shall** expose a `freshness` view in DuckDB listing each source, its last successful fetch timestamp, time since last fetch, and a boolean flag indicating whether the source is past its freshness threshold (configurable per source, default 2× cadence).
*Verification:* downstream subsystem (e.g., `signal_scanner`) can query freshness and filter stale sources.

**REQ-PROV-003: Stale-source handling**
When a source has not produced a successful fetch within its freshness threshold, the system **shall** raise a warning in the next cycle output and continue. It **shall not** silently ingest stale data nor halt other connectors.
*Verification:* integration test simulates stale source and confirms warning surfaces in cycle report.

### 5.7 Logging & observability

**REQ-LOG-001: Structured per-cycle log**
Each ingest cycle **shall** emit a structured log entry (JSON) capturing start time, end time, per-connector outcome (success/failure/skipped), record counts, and any errors.
*Verification:* log inspection after a cycle confirms presence and structure.

**REQ-LOG-002: No PII or credential leakage in logs**
Logs **shall not** contain API keys, full request URLs with embedded keys, or any operator-identifying information beyond the local username.
*Verification:* log-scan test against synthetic credentials confirms redaction.

### 5.8 Extensibility

**REQ-EXT-001: New-source registration**
Adding a new data source **shall** require implementing the connector interface (fetch, normalize, persist) and registering it in the source registry. No changes to downstream subsystems **shall** be required to ingest new sources, provided the new source maps to one of the existing canonical schemas.
*Verification:* add a placeholder source via the registration mechanism in test and confirm it participates in a cycle.

**REQ-EXT-002: New canonical schema**
Adding a new canonical schema (beyond the four in REQ-NORM-001) **shall** be a deliberate, versioned operation requiring a spec amendment and migration script. It **shall not** happen implicitly through connector additions.
*Verification:* code review of schema-creation paths confirms no implicit schema creation.

### 5.9 Source-specific: ACLED (events + deleted reconciliation)

The ACLED connector is the only v1 source whose access model is OAuth-based and whose data require an active reconciliation step (deletions). These requirements amend the generic connector contract for ACLED specifically.

**REQ-ACLED-AUTH-001: OAuth 2.0 password grant**
The ACLED connector **shall** authenticate via OAuth 2.0 password grant against `https://acleddata.com/oauth/token` with the form-encoded fields `username`, `password`, `grant_type=password`, `client_id=acled`, `scope=authenticated`. Credentials are loaded from `.env` as `ACLED_USERNAME` and `ACLED_PASSWORD`.
*Verification:* fixture-based unit test against the documented OAuth response shape; smoke test exchanges credentials for a real token.

**REQ-ACLED-AUTH-002: Token caching and refresh**
The connector **shall** cache the access token in-memory for the connector's process lifetime and refresh it before expiry using the `refresh_token` flow. The connector **shall** treat tokens as ephemeral and **shall not** persist them to disk in v1. On refresh failure, the connector **shall** fall back to a full password-grant exchange.
*Verification:* unit tests for: fresh token acquisition, refresh-before-expiry, refresh-failure fallback, token-expired-mid-call retry.

**REQ-ACLED-AUTH-003: No credential leakage in URLs**
The connector **shall not** include username, password, access token, or refresh token in any URL query string, log message, error message, or persisted record. Tokens travel only in the `Authorization: Bearer …` header.
*Verification:* synthetic-credential log-scan test with all four credential types confirms redaction.

**REQ-ACLED-EVENTS-001: Events endpoint**
The connector **shall** fetch event data from `https://acleddata.com/api/acled/read?_format=json` with explicit `fields=` selection scoped to the columns the canonical `event_stream` schema and downstream subsystems consume.
*Verification:* fixture test against a representative paginated response; integration test confirms only requested columns are stored in the source payload.

**REQ-ACLED-EVENTS-002: Pagination**
Backfill and incremental pulls **shall** use ACLED's `page=` pagination (default page size 5,000 rows). The connector terminates pagination when a page returns fewer rows than the limit. Each page is a separate batch committed independently so a mid-pagination interruption does not lose completed pages (see REQ-BACKFILL-002).
*Verification:* fixture test with three-page response confirms termination; resume test confirms continuation from last successful page.

**REQ-ACLED-EVENTS-003: Year-bounded backfill chunks**
For backfill, the connector **shall** chunk requests by `year_where=BETWEEN` rather than pulling the entire historical record in one paginated call. Default chunk size is one calendar year per chunk, configurable.
*Verification:* unit test confirms request URLs are year-chunked; integration test confirms full historical pull across multiple chunks dedupes correctly.

**REQ-ACLED-EVENTS-004: Total count tracking**
The connector **shall** request `with_total=true` on the first page of each chunk to obtain the expected row count for that chunk and **shall** report progress in the cycle log relative to that count.
*Verification:* fixture test confirms `with_total` parameter included; log inspection confirms progress reporting.

**REQ-ACLED-DELETED-001: Deleted events reconciliation**
The connector **shall** include a deleted-events reconciliation step that fetches `https://acleddata.com/api/deleted/read?_format=json` filtered by `deleted_timestamp_where=>=` against the connector's `last_deleted_reconciliation_ts` (a per-source state value). Events whose `event_id_cnty` appears in the deleted response **shall** be marked superseded in the local store with a `deletion_reason = 'acled_deleted_endpoint'` annotation, mirroring the source-revision semantics from REQ-PERSIST-004.
*Verification:* simulated deletion flow: ingest events → run reconciliation → confirm affected rows superseded with annotation; idempotency test confirms re-run produces no additional state changes.

**REQ-ACLED-DELETED-002: Reconciliation cadence**
The deleted-events reconciliation **shall** run on every ACLED ingest cycle, before the events-pull step. This minimizes the window during which the local store reflects events ACLED has retracted.
*Verification:* cycle-order test confirms reconciliation precedes events pull.

**REQ-ACLED-LICENSE-001: ACLED Terms acknowledgement gate**
On first run, the connector **shall** require the operator to acknowledge ACLED's published Terms and Conditions and **shall** record the acknowledgement timestamp and the SHA-256 hash of the canonical Terms text in the `sources` table. On subsequent runs, the connector **shall** check the recorded hash against the live Terms and re-prompt if changed. The ACLED `license` field on the source registration is `ACLED_TERMS_VERSIONED` rather than a specific Creative Commons variant — the canonical license language is whatever ACLED currently publishes, and downstream subsystems that export ACLED-derived data must consult the operator's recorded acknowledgement (and, transitively, the live ACLED Terms) for compliance scope.
*Verification:* first-run integration test confirms acknowledgement gate; second run with same hash proceeds; simulated hash change re-prompts.

**REQ-ACLED-LICENSE-002: Conservative non-commercial assumption**
Until ACLED's Terms have been operator-reviewed and an explicit commercial-use grant recorded, downstream subsystems treating ACLED-derived data **shall** behave as if the source is non-commercial-use-only. This is a default-safe posture, not an assertion that the Terms forbid commercial use; it requires the operator to actively record a different posture if ACLED's Terms permit broader use.
*Verification:* downstream subsystems that consume ACLED-derived data check a per-source `commercial_use_recorded_grant` flag (default false) before exporting.

**REQ-ACLED-RATE-001: Conservative request rate**
The connector **shall** apply a token-bucket rate limiter with a conservative default (5 requests/second) since ACLED's documentation does not publish a specific per-account RPS. The rate **shall** be adapted upward only after observing successful sustained operation without 429 responses, and downward immediately on observing them. The OAuth refresh exchange does not consume the same bucket.
*Verification:* unit test of the limiter; integration test with simulated 429 response confirms backoff.

## 6. Non-Functional Requirements

**NFR-PERF-001:** A full daily ingest cycle (all v1 sources at incremental cadence) **shall** complete within 30 minutes on the operator's hardware, assuming no source-side outages.

**NFR-PERF-002:** A full 50-year backfill across all in-scope sources **shall** be feasible within 100 GB of disk and within 72 hours of wall-clock time on the operator's hardware. (If feasibility analysis in design phase shows this is unrealistic for any source, that source's backfill depth is reduced and the change is recorded as an evolution-log entry.)

**NFR-AVAIL-001:** Failure of any single source **shall not** prevent downstream subsystems from operating on data already persisted. The system degrades gracefully.

**NFR-PORT-001:** The subsystem **shall** run on macOS and Linux. Windows is not a target in v1.

**NFR-SEC-001:** API credentials **shall** be loaded from `.env` files, **shall not** be committed to source control (enforced via `.gitignore`), and **shall not** be transmitted to any host other than the corresponding source API.

**NFR-LEGAL-001:** Each connector **shall** respect the terms of service of its source. Sources whose ToS prohibit automated access or local storage are excluded from v1 regardless of technical feasibility. (Specific ToS review per source happens in the design phase.)

## 7. Acceptance Criteria

The `data_ingest` subsystem is considered complete when all the following are true:

- All connectors listed in section 4 (excluding any TBC sources excluded during design review) are implemented and pass their per-connector unit tests.
- A full daily incremental cycle completes successfully on the operator's hardware within NFR-PERF-001.
- A backfill run produces a corpus that downstream subsystems (`pattern_library`) can query for base-rate calculations across all six domain sectors.
- The freshness view correctly reflects the state of all sources after a cycle.
- A simulated source-failure scenario (one connector errors, others succeed) produces the expected partial-success cycle report and does not corrupt persisted data.
- A re-run of the same cycle produces no duplicate records (REQ-PERSIST-003).
- Backfill is resumable as specified in REQ-BACKFILL-002.
- No credentials appear in DuckDB, logs, or error output.

## 8. Open Questions (carry to design phase)

- **OQ-001:** ACLED free tier rate limits and historical depth — confirm whether 30-year backfill is achievable under free tier or requires reduced scope.
- **OQ-002:** GDELT bulk-download size at 10-year depth — measure actual disk footprint before committing to full backfill.
- **OQ-003:** OPEC MOMR is published as PDF. Parsing reliability across years is uncertain. Decide in design whether to include in v1 or defer.
- **OQ-004:** NRC ADAMS does not have a clean modern API. Confirm whether a scraping approach is permissible under the source's ToS and stable enough to maintain.
- **OQ-005:** DuckDB's behavior under repeated large-batch upserts at scale (REQ-PERSIST-003 / REQ-PERSIST-004) — benchmark before committing to the deduplication approach.
- **OQ-006:** Whether to normalize geographic identifiers (ISO country codes, GADM admin units) at ingest time or defer to a downstream geographic-normalization layer.
- **OQ-007:** Whether to include a `data_quality` table that tracks per-source anomalies (sudden gaps, schema changes) detected at ingest, or leave that responsibility to downstream subsystems.

## 9. References

- LOOM v0.3.0 — `razorrooster.md`, subsystem registry entry for `data_ingest`.
- Open thread OT-002 (historical backfill depth) — this spec partially addresses; final feasibility settled in design.
- System prompt — `razorrooster-prompt.md.txt` v0.2 (educational framing).
