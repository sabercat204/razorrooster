# DATA_INGEST — Implementation Tasks

**Subsystem:** `data_ingest`
**Codename:** The Trough
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `DATA_INGEST.md` v0.1.0
- Design: `DATA_INGEST_DESIGN.md` v0.1.0

---

## How to Read This Document

Tasks are organized into phases. Within a phase, tasks should be completed in listed order — later tasks depend on earlier ones. Tasks across phases can sometimes be parallelized; explicit dependencies are marked.

Each task has:
- **ID** — stable identifier (T-NNN), referenced from commits and PRs.
- **Title** — short description.
- **Depends on** — prior task IDs that must be complete.
- **References** — requirement IDs (REQ-*), design section numbers, or open-question/deferral IDs.
- **Deliverables** — concrete artifacts that exist when the task is done.
- **Verification** — how the operator confirms the task is complete.
- **Out of scope** — what the task explicitly does *not* cover (to prevent scope creep).

A task is "done" only when its verification passes. No exceptions for "almost works."

## Phase 0 — Project Bootstrap

These tasks establish the package, tooling, and CI-equivalent local checks before any subsystem code is written. They are infrastructure for all subsequent work.

### T-001 — Initialize Python package and tooling
**Depends on:** none
**References:** LOOM project metadata (Python 3.11+); design §3.1 module layout.
**Deliverables:**
- `pyproject.toml` configured for Python 3.11+, with `razor_rooster` as the package name.
- Dependency groups: `runtime` (httpx, pandas, pyarrow, duckdb, jinja2, pyyaml, python-dotenv, click), `dev` (pytest, pytest-cov, pytest-recording, ruff, mypy).
- `ruff.toml` and `mypy.ini` configured. Ruff in lint+format mode; mypy in `--strict` for the `razor_rooster.data_ingest` package only (other packages can opt in later).
- `.gitignore` covers `.env`, `data/*.duckdb*`, `logs/`, `__pycache__/`, `.venv/`, `.pytest_cache/`.
- A `Makefile` (or `justfile`) with targets: `install`, `test`, `lint`, `typecheck`, `smoke`.
**Verification:** `make install && make lint && make typecheck` runs clean on an empty package.
**Out of scope:** any actual data-ingest code, any source connectors.

### T-002 — Bootstrap module skeleton
**Depends on:** T-001
**References:** design §3.1.
**Deliverables:**
- Empty `__init__.py` files for every module path listed in design §3.1.
- `data_ingest/cli.py` with a single `click` command `razor-rooster ingest` that prints `not yet implemented` and exits 0.
- `tests/data_ingest/` mirror layout with empty `test_*.py` placeholders for each module.
**Verification:** `pip install -e .` succeeds; `razor-rooster ingest --help` runs; `pytest` discovers and runs zero tests cleanly.
**Out of scope:** any logic — these are stubs only.

## Phase 1 — Persistence and Schemas

The persistence layer is the foundation. Connectors write through it; downstream subsystems read from it. Build it first, prove it works in isolation, then build connectors against it.

### T-010 — Canonical schemas as code
**Depends on:** T-002
**References:** REQ-NORM-001, REQ-NORM-002, REQ-NORM-003, design §4.1–§4.4.
**Deliverables:**
- `persistence/schemas.py` defining `SchemaType` enum and four DDL strings: `event_stream`, `time_series`, `document_docket`, `geospatial_indicator`.
- Each schema includes the provenance prefix from design §4 and the type-specific columns.
- `NormalizedRecord` tagged-union dataclasses in `normalization/base.py` matching the schemas.
**Verification:** schemas can be applied to an in-memory DuckDB; round-trip test inserts a synthetic record per schema, queries it back, and confirms all provenance columns are preserved.
**Out of scope:** the operational tables (`sources`, `backfill_state`, etc. — done in T-011); migration framework (T-013).

### T-011 — Operational tables
**Depends on:** T-010
**References:** REQ-PERSIST-002, design §4.5, REQ-PROV-002, design §4.6.
**Deliverables:**
- DDL for `sources`, `backfill_state`, `ingest_anomalies`, `cycle_log`, `schema_migrations`.
- The `freshness` view from design §4.6.
- Each operational table has a `__test__` row inserted in fixtures so query patterns can be confirmed.
**Verification:** query against the `freshness` view returns expected staleness flags for synthetic source rows with varied `last_successful_fetch` values.
**Out of scope:** populating these tables from real data — connectors do that later.

### T-012 — DuckDB store wrapper
**Depends on:** T-010, T-011
**References:** REQ-PERSIST-001, design §3.1, design §11.
**Deliverables:**
- `persistence/duckdb_store.py` with a `DuckDBStore` class wrapping a single connection. Configurable path; default `~/Projects/razor-rooster/data/trough.duckdb`.
- Connection-pool wrapper for thread-bounded access (design §11 mentions the single-connection ceiling — implement now, document the ceiling, don't try to remove it in v1).
- Context-manager support so connections close cleanly on exceptions.
**Verification:** parallel-write test with 4 worker threads confirms no corruption, no deadlock, and serialized writes complete within an acceptable time bound (set a generous bound; this is a smoke check, not a benchmark).
**Out of scope:** the staging-merge pattern (T-014); cap tracking (T-016).

### T-013 — Schema migrations framework
**Depends on:** T-012
**References:** design §5.2, REQ-EXT-002.
**Deliverables:**
- `persistence/migrations/` directory with the migration discovery and runner.
- Migration `m0001_initial.py` that applies all DDL from T-010 and T-011.
- A migration runs at every store open if version mismatch is detected.
- `down()` is implemented but only callable explicitly via a CLI flag — never auto-runs.
**Verification:** open store on empty DuckDB → m0001 runs → version recorded. Open same store again → no migration runs. Manually downgrade and re-open → m0001 runs again.
**Out of scope:** any subsequent migrations; a real CI test of upgrade-from-old-version (we only have one version yet).

### T-014 — Staging-merge upsert pattern
**Depends on:** T-012, T-013
**References:** REQ-PERSIST-003, REQ-PERSIST-004, OQ-005, design §5.1.
**Deliverables:**
- `persistence/staging_merge.py` implementing the two-stage staging-merge pattern from design §5.1.
- Function `staging_merge(conn, table, batch: pa.Table, dedup_keys: list[str]) -> MergeResult` returning record counts: inserted, updated (superseded), unchanged.
- The merge is wrapped in a single transaction.
**Verification:**
- Insert 10,000 synthetic records via the staging-merge. Query confirms all present.
- Re-run the same insert. Query confirms zero new rows (REQ-PERSIST-003, idempotency).
- Modify payload for 100 records, re-run. Confirm those 100 have `superseded_at` set on the old rows and new rows inserted (REQ-PERSIST-004).
- Sort-key benchmark: insert sorted vs unsorted. Note relative timing in a comment in the test, not as a hard requirement.
**Out of scope:** per-source customization of dedup keys (handled by connector base class).

### T-015 — Provenance helpers
**Depends on:** T-014
**References:** REQ-PROV-001, REQ-PROV-002, REQ-PROV-003, design §4.6, design §6.
**Deliverables:**
- `persistence/provenance.py` with helpers: `update_last_successful_fetch(source_id)`, `update_last_failed_fetch(source_id, error_summary)`, `record_anomaly(source_id, anomaly_type, details)`, `query_freshness()`.
- `query_freshness()` returns a typed result with one entry per source.
**Verification:** unit tests cover each helper; freshness query results match design §4.6 specification.
**Out of scope:** integrating freshness into the cycle report (T-040).

### T-016 — Disk budget tracker
**Depends on:** T-012
**References:** design §5.3, REQ-BACKFILL-003, NFR-PERF-002.
**Deliverables:**
- `persistence/disk_budget.py` with `current_corpus_bytes()`, `corpus_pct_of_cap()`, `should_pause_backfill()`, `should_warn()`.
- Reads cap configuration from `config/source_caps.yaml` (T-022 creates the file; this task uses a synthetic config in tests).
- Per-source byte estimation: sum `pg_relation_size`-equivalent for rows tagged with that source. (DuckDB has `database_size()` and per-table approximations; pick the most accurate available.)
**Verification:** synthetic-load test inserts data toward the 80% threshold and confirms `should_warn()` flips. Inserts further toward 95% and confirms `should_pause_backfill()` flips.
**Out of scope:** actual pause behavior (T-035 wires this into backfill logic).

## Phase 2 — Configuration and Logging

These cross-cutting concerns are needed before any connector can run end-to-end. Build them ahead of connectors so the connector tasks can use them rather than mocking around them.

### T-020 — Environment-variable credential loader
**Depends on:** T-002
**References:** REQ-SRC-003, NFR-SEC-001, design §7.3.
**Deliverables:**
- `config/credentials.py` with `load_credentials_for(source_id) -> CredentialBundle | None`.
- Reads from `.env` via `python-dotenv` — no other source of credentials supported.
- Returns `None` if the source's required env vars are missing; caller decides whether to skip or fail.
- Sentinel test: any function in this module that ever returns a credential into a string interpolation must be marked with a comment and reviewed; no general-purpose "format credentials" helper exists.
**Verification:** unit test confirms credentials load correctly with `.env` present, return `None` cleanly when absent, and no credential value is ever logged at any log level.
**Out of scope:** secret management beyond `.env` files (e.g., keychain integration, vault) — explicitly deferred to later versions.

### T-021 — Structured JSON logging
**Depends on:** T-002
**References:** REQ-LOG-001, REQ-LOG-002, design §6.1, design §6.2.
**Deliverables:**
- `logging/structured.py` with a JSON-line logger writing to `logs/cycles/cycle-<iso8601>.jsonl`.
- A redaction filter (design §6.2) applied at the formatter level for both INFO and ERROR.
- A `cycle_logger(cycle_id)` context manager that accumulates per-connector results and writes the final JSON line on exit (success or failure).
**Verification:**
- Synthetic-key test: write a log entry that contains a fake API key; confirm it's redacted in the output file.
- URL test: log a URL with `?api_key=secret`; confirm query string is stripped or redacted.
- Header test: log an HTTP request including `Authorization: Bearer xyz`; confirm value redacted.
**Out of scope:** log rotation, log shipping — local files are the only target in v1.

### T-022 — Schedule and caps configuration files
**Depends on:** T-002
**References:** REQ-SCHED-001, design §7.1, design §7.2.
**Deliverables:**
- `config/ingest_schedule.yaml` with the structure from design §7.1, populated for the 12 v1 sources.
- `config/source_caps.yaml` with the structure from design §7.2.
- `config/loader.py` that reads, validates (via Pydantic or equivalent), and exposes typed access objects. Validation failures abort startup with a clear error.
**Verification:** unit tests confirm valid configs parse, invalid configs (missing fields, unknown sources, malformed cadence) fail with informative errors.
**Out of scope:** changing schedule/caps at runtime — config is read at startup only.

## Phase 3 — Connector Framework

Build the connector contract and the shared infrastructure (rate limiting, retries, normalization helpers) once, so the 12 individual connectors are thin and consistent.

### T-030 — Connector ABC and shared fetch infrastructure
**Depends on:** T-014, T-020, T-021
**References:** REQ-SRC-001, REQ-SRC-002, REQ-SRC-004, design §3.2.
**Deliverables:**
- `connectors/base.py` defining the `Connector` ABC from design §3.2.
- Default rate-limit handling: exponential backoff with jitter, capped at 5 retries. Inputs: HTTP response. Output: retry-or-give-up decision.
- `RawRecord` and `NormalizedRecord` dataclasses formalized.
- `License` and `SchemaType` enums.
- `ConnectorHealth` result type for `health_check()`.
- Failure isolation: a uniform `run_incremental(connector)` entry point that catches exceptions per connector and returns a structured outcome rather than propagating.
**Verification:** mock connector implementing the ABC succeeds, fails on a forced exception, and times out on a hung HTTP call — all without crashing the test runner. Backoff sequence on synthetic 429 responses matches expected pattern.
**Out of scope:** any specific source — that's T-050+.

### T-031 — Time and geo normalization helpers
**Depends on:** T-030
**References:** REQ-NORM-002, OQ-006, design §3.1, design §4.1, design §4.4.
**Deliverables:**
- `normalization/time.py` with `to_utc(value, hint_tz=None) -> datetime`. Preserves source-native timezone in the payload (caller's responsibility to keep it; this function returns UTC).
- `normalization/geo.py` with `to_iso3(country_value) -> str | None`. Handles common variants (full name, ISO-2, common misspellings via a small whitelist). Returns `None` and logs a warning for ambiguous input.
**Verification:** unit tests cover: ISO-2 input, ISO-3 input, full English name, common alternatives, ambiguous input. No imputation: ambiguous always returns `None` rather than a best guess.
**Out of scope:** sub-national normalization (deferred per OQ-006).

### T-032 — Source registry
**Depends on:** T-030
**References:** REQ-EXT-001, design §3.1.
**Deliverables:**
- `registry.py` with `register(connector_class)` and `get_all() -> list[Connector]`.
- A decorator-style registration so each connector module self-registers when imported.
- The registry is the single source of truth for "what connectors does this build know about."
**Verification:** unit test confirms a fresh registration, duplicate-registration rejection, and ordered iteration.
**Out of scope:** dynamic loading from external packages — registration is import-time only in v1.

### T-033 — Cycle scheduler
**Depends on:** T-022, T-032
**References:** REQ-SCHED-002, REQ-SCHED-003, design §3.3.
**Deliverables:**
- `scheduler.py` with `evaluate_due() -> list[(Connector, mode)]` reading from `ingest_schedule.yaml` and the `sources.last_successful_fetch` column.
- `run_cycle(mode='incremental') -> CycleReport` orchestrating the parallel run with `max_workers` from config.
- `run_single(source_id) -> ConnectorOutcome` for ad-hoc pulls.
**Verification:** unit test with three mock connectors confirms parallel execution bounded at `max_workers`, correct due-evaluation, and structured `CycleReport` output. Single-source path runs only the named connector.
**Out of scope:** the actual JSON cycle log file — that comes from T-021 wired in via T-040.

### T-034 — Backfill state and resume mechanism
**Depends on:** T-014, T-015, T-030
**References:** REQ-BACKFILL-001, REQ-BACKFILL-002, design §3.4.
**Deliverables:**
- `scheduler.run_backfill(source_id, until=None, cap=None) -> BackfillReport`.
- Resume token persisted to `backfill_state` after each successful batch commit.
- Restart picks up from the last committed token; never restarts from scratch unless `--restart` is explicitly passed.
**Verification:** integration test interrupts a backfill mid-run (kill signal between batches), then runs again. Confirms continuation from the last token, no duplicates, total record count matches the un-interrupted run.
**Out of scope:** cap enforcement (T-035).

### T-035 — Cap enforcement during backfill
**Depends on:** T-016, T-034
**References:** REQ-BACKFILL-003, design §5.3.
**Deliverables:**
- Backfill checks per-source cap before each batch commit. On exceedance, marks `backfill_state.status = 'CAP_REACHED'`, logs structured warning, exits cleanly.
- Backfill checks global corpus cap before each batch commit. On exceedance, pauses *all* backfills and emits a critical-level structured log entry.
- Cap raise + resume is operator-initiated, never automatic.
**Verification:** integration test sets a low cap, runs backfill, confirms graceful stop with status `CAP_REACHED` and no duplicate or partial-row corruption. Operator raises cap, re-runs backfill, confirms resume completes.
**Out of scope:** UI for raising caps — config-file edit + restart is the v1 mechanism.

## Phase 4 — Cycle Reporting

This is the operator-facing surface. It's small but critical; without it, the operator can't see what the system is doing.

### T-040 — Cycle report writer
**Depends on:** T-021, T-033
**References:** REQ-LOG-001, REQ-PROV-002, design §6.1.
**Deliverables:**
- `scheduler.write_cycle_report(report)` writes the structured JSON line per design §6.1.
- A short stdout summary printed at the end of every cycle: per-connector status, total records, stale-source warnings.
- A `cycle_log` table row inserted with a pointer to the JSONL file path.
**Verification:** end-to-end test runs a cycle with three mock connectors (one ok, one rate-limited-then-ok, one failing), confirms JSON file matches design §6.1 schema, and stdout summary contains expected lines.
**Out of scope:** human-friendly terminal formatting beyond plain-text summary; that's `report_generator`'s job.

## Phase 5 — Source Connectors (Public, Unauthenticated)

Build the public-API connectors first. They're easier (no credential plumbing), they validate the framework end-to-end, and they unblock pattern-library work that doesn't need authenticated sources.

### T-050 — FRED connector
**Depends on:** T-030, T-031, T-032, T-033
**References:** REQ-SRC-001, REQ-SRC-002, REQ-SRC-003.
**Deliverables:**
- `connectors/fred.py` implementing the `Connector` ABC for FRED's API.
- Pulls a configurable list of series IDs (initial set: a small handful of headline indicators; expandable via config).
- Maps to `time_series` schema.
- Backfill supported back to FRED's earliest data per series.
**Verification:** recorded-fixture unit tests for normal pull, empty pull, rate-limited pull, persistent-failure pull. Integration smoke test pulls one real series end-to-end and persists to DuckDB.
**Out of scope:** dynamic series discovery — series IDs are configuration in v1.

### T-051 — World Bank connector
**Depends on:** T-050 (for connector pattern reference, not a hard runtime dependency)
**References:** REQ-SRC-001.
**Deliverables:**
- `connectors/worldbank.py`, mapping to `time_series` schema.
- Pulls a configurable list of indicator codes.
- Cadence: weekly (per design §7.1).
**Verification:** same fixture-based test pattern as T-050; smoke test against the live free API.
**Out of scope:** projects API, climate API extensions — only the indicators API in v1.

### T-052 — GDELT events connector
**Depends on:** T-030
**References:** OQ-002, design §3.4 (backfill), design §11 (performance risk).
**Deliverables:**
- `connectors/gdelt_events.py`, mapping to `event_stream` schema.
- Incremental: pulls 15-minute event files since last successful fetch.
- Backfill: capped at 5 years per design §7.2; uses the GDELT 2.0 raw-data event-files convention.
- Optional event-code filter at ingest time, off by default (store all).
**Verification:** fixture test against a saved 15-minute event file. Integration smoke test pulls one recent 15-minute window, parses, persists. Disk-budget test confirms backfill stops at the 30 GB per-source cap from design §7.2.
**Out of scope:** GDELT GKG (deferred per OQ-002), GDELT mentions table.

### T-053 — Federal Register connector
**Depends on:** T-030, T-031
**References:** REQ-SRC-001.
**Deliverables:**
- `connectors/federal_register.py`. Maps to `document_docket` schema.
- Stores `full_text_uri` only, not the full text body (per design DEFER-004).
- Backfill from 1994.
**Verification:** fixture test for a representative rule, proposed rule, and notice. Smoke test against the live API.
**Out of scope:** full-text caching to local disk.

### T-054 — WHO Disease Outbreak News connector
**Depends on:** T-030, T-031
**References:** REQ-SRC-001.
**Deliverables:**
- `connectors/who_don.py`. Maps to `event_stream` schema.
- Source: WHO DON RSS or equivalent stable endpoint.
- ToS confirmation logged at startup; if the endpoint approach changes, the connector emits a clear error rather than silently scraping a different surface.
**Verification:** fixture test on a saved DON entry. Smoke test against live feed.
**Out of scope:** WHO IHR formal notifications — DON entries only in v1.

### T-055 — NOAA Climate Data connector
**Depends on:** T-030, T-031
**References:** REQ-SRC-001, design §11 (NOAA CDO rate limits).
**Deliverables:**
- `connectors/noaa.py`. Maps to `time_series` and `geospatial_indicator` (multi-schema connector — design §3.2 supports this; if not, this task adds support).
- Pulls a configurable list of station IDs and indices (ENSO, drought, etc.).
- Respects the 1,000-requests-per-day CDO free-tier limit (paces requests across the day).
**Verification:** fixture tests; smoke test for one station and one index. Pacing test confirms request distribution stays under the daily cap on a synthetic 25-hour window.
**Out of scope:** bulk-archive (NCEI direct download) — only the CDO API in this task; bulk paths can be added in a later task if pacing under CDO is too slow.

### T-056 — USGS Mineral Commodity Summaries connector
**Depends on:** T-030, T-031
**References:** REQ-SRC-001.
**Deliverables:**
- `connectors/usgs_minerals.py`. Maps to `time_series` schema.
- Annual cadence; pulls the most recent published summary plus historical archive.
**Verification:** fixture test on a saved summary; smoke test against live publication.
**Out of scope:** sub-annual reports.

### T-057 — Baltic Dry Index via FRED proxy
**Depends on:** T-050
**References:** REQ-SRC-001.
**Deliverables:**
- A configuration entry under FRED for BDI (no separate module needed if the FRED connector is generic enough). This task confirms the FRED connector handles BDI correctly and registers it in the schedule.
**Verification:** BDI series persists to `time_series` with correct provenance.
**Out of scope:** alternative BDI sources.

## Phase 6 — Source Connectors (Authenticated)

These require API keys. They follow the same pattern as Phase 5 but with credential-loading from `.env`.

### T-060 — ACLED connector (events + auth)
**Depends on:** T-020, T-030, T-031
**References:** OQ-001, REQ-SRC-003, REQ-ACLED-AUTH-001..003, REQ-ACLED-EVENTS-001..004, REQ-ACLED-LICENSE-001..002, REQ-ACLED-RATE-001, design §2 OQ-001 resolution.
**Deliverables:**
- `connectors/acled.py` implementing the events portion of the ACLED API:
  - OAuth 2.0 password grant token acquisition against `acleddata.com/oauth/token` with credentials loaded from `ACLED_USERNAME` / `ACLED_PASSWORD` (REQ-ACLED-AUTH-001).
  - In-process token cache with refresh-before-expiry and refresh-failure fallback to full password grant (REQ-ACLED-AUTH-002).
  - Bearer-header request execution; no credentials in URLs or logs (REQ-ACLED-AUTH-003).
  - Events fetch from `/api/acled/read` with explicit `fields=` selection.
  - Pagination via `page=` parameter, terminating when a page returns fewer rows than the limit (REQ-ACLED-EVENTS-002).
  - Year-bounded backfill via `year_where=BETWEEN` chunks, default one calendar year per chunk (REQ-ACLED-EVENTS-003).
  - `with_total=true` on first page of each chunk for progress reporting (REQ-ACLED-EVENTS-004).
  - Maps to `event_stream` schema.
- Terms acknowledgement gate writes `sources.license = 'ACLED_TERMS_VERSIONED'` plus `sources.license_terms_hash` and `sources.license_acknowledged_at`. Refuses to start without ack (REQ-ACLED-LICENSE-001).
- `LICENSE_NONCOMMERCIAL_REQUIRED = true` and `commercial_use_recorded_grant = false` set on source registration (REQ-ACLED-LICENSE-002).
- Conservative 5 rps default token-bucket limiter (REQ-ACLED-RATE-001).
**Verification:** fixture-based tests for OAuth happy path, refresh flow, refresh-failure fallback, expired-mid-call retry, paginated events response, year-chunked backfill, `with_total` progress reporting; smoke test against the live API exchanges credentials and fetches a small page; license acknowledgement gate test confirms refusal-then-pass cycle.
**Out of scope:** the deleted-events reconciliation pass (T-064 — separate task).

### T-064 — ACLED deleted-events reconciliation
**Depends on:** T-060
**References:** REQ-ACLED-DELETED-001, REQ-ACLED-DELETED-002, design §2 OQ-001 resolution (deleted-endpoint reconciliation).
**Deliverables:**
- Extend `connectors/acled.py` (or add a sibling sync helper) with a `reconcile_deleted()` step that:
  - Reads `last_deleted_reconciliation_ts` from the per-source state.
  - Fetches `/api/deleted/read?_format=json&deleted_timestamp_where=>=` with that timestamp.
  - Paginates the response.
  - For each returned `event_id_cnty`, marks the corresponding `event_stream` rows superseded with `deletion_reason = 'acled_deleted_endpoint'` (mirroring REQ-PERSIST-004 source-revision semantics; the `superseded_at` column is reused, with `deletion_reason` stored in the source payload metadata to distinguish a content revision from a retraction).
  - Updates `last_deleted_reconciliation_ts` to `max(deleted_timestamp)` from the response.
- The reconciliation step **shall** run before the events fetch step on every ACLED ingest cycle (REQ-ACLED-DELETED-002).
- Idempotent: re-running on an unchanged source state is a no-op.
**Verification:**
- Fixture test simulates deletion: ingest events → run reconciliation against synthetic deleted response → confirm affected rows superseded with the correct annotation.
- Idempotency test: re-run reconciliation, confirm no additional state changes.
- Order test: cycle integration confirms reconciliation precedes events fetch.
- Resume-after-interrupt test: reconciliation interrupted mid-pagination resumes via the `last_deleted_reconciliation_ts` value persisted before the interrupt (effectively the same resumability story as REQ-BACKFILL-002, but using a timestamp watermark rather than a resume token).
**Out of scope:** retroactive cleanup of historical deletions that pre-date the connector's first run — those are handled by the initial backfill pulling the current ACLED state, not the deleted endpoint.

### T-061 — EIA connector
**Depends on:** T-020, T-030, T-031
**References:** REQ-SRC-001.
**Deliverables:**
- `connectors/eia.py`. Maps to `time_series` schema.
- Pulls a configurable list of EIA series.
**Verification:** fixture test; smoke test.
**Out of scope:** EIA's open-data application API — only the time-series API in v1.

### T-062 — NRC ADAMS connector
**Depends on:** T-020, T-030, T-031
**References:** OQ-004, design §2 OQ-004 resolution.
**Deliverables:**
- `connectors/nrc_adams.py` using the official Public Search API (Azure-managed).
- Maps to `document_docket` schema.
- Backfill targets PARS Library (1999+).
- Stores `full_text_uri` only.
**Verification:** fixture test on a saved ADAMS document response. Smoke test against the live API. Confirm backfill correctly resumes via REQ-BACKFILL-002.
**Out of scope:** Public Legacy Library (pre-1999), fee-based document ordering.

### T-063 — regulations.gov (EPA dockets) connector
**Depends on:** T-020, T-030, T-031
**References:** REQ-SRC-001.
**Deliverables:**
- `connectors/regulations_gov.py`, scoped to EPA dockets in v1 (other agencies addable later via configuration).
- Maps to `document_docket` schema.
- Backfill from 2003.
**Verification:** fixture test; smoke test.
**Out of scope:** comment-content ingest at scale — comments are bulk and noisy; we store docket metadata and let downstream subsystems pull comments on demand.

## Phase 7 — Acceptance and Operational Readiness

After connectors are individually verified, prove the system works as a whole.

### T-070 — End-to-end cycle integration test
**Depends on:** T-040, all connector tasks (T-050..T-063).
**References:** acceptance criteria in DATA_INGEST.md §7.
**Deliverables:**
- Integration test: in-memory or disposable DuckDB; mock all 12 sources with realistic fixtures; run a full cycle; confirm cycle report matches expectations.
- Failure-isolation scenario: mock one connector to fail; confirm others still complete and the report reflects partial success (REQ-SRC-004).
- Idempotency scenario: run the same cycle twice; confirm no duplicates (REQ-PERSIST-003).
- Source-revision scenario: between two cycles, alter one record's payload at the mock source; confirm `superseded_at` set and new row inserted (REQ-PERSIST-004).
**Verification:** the integration test is part of `make test` and passes.
**Out of scope:** real-network testing — that's the smoke target.

### T-071 — Smoke test against real services
**Depends on:** T-070
**References:** design §9.3.
**Deliverables:**
- `make smoke` runs a single-record incremental fetch against each source for which credentials are present.
- Authenticated sources skip cleanly when credentials are absent.
- Smoke test does not write to the production DuckDB — uses a separate `data/trough_smoke.duckdb`.
**Verification:** `make smoke` completes in under 5 minutes on the operator's hardware against a recent network condition.
**Out of scope:** automated smoke runs — operator-initiated only.

### T-072 — First backfill run and disk-budget verification
**Depends on:** T-035, T-070
**References:** NFR-PERF-002, DEFER-001.
**Deliverables:**
- Operator runs `razor-rooster ingest backfill --all` on a fresh DuckDB.
- Records actual disk footprint per source after backfill, plus wall-clock duration.
- Updates `DEFER-001` resolution in this document with measured numbers.
- If footprint exceeds NFR-PERF-002, raises a finding and proposes resolution (reduce GDELT depth further, raise global cap, or defer a source).
**Verification:** measured footprint and duration recorded in this document under a new section §X-Measurements (added by the operator).
**Out of scope:** backfill of newly added sources after v1 — this task is the v1 baseline only.

### T-073 — Daily incremental cycle on operator hardware
**Depends on:** T-072
**References:** NFR-PERF-001.
**Deliverables:**
- Daily cycle runs end-to-end on the EliteBook G8 within NFR-PERF-001 (30 minutes).
- Wall-clock measurement recorded.
- `launchd` (macOS) or `cron` configuration documented in operator README, not implemented in code.
**Verification:** three consecutive daily cycles complete inside the 30-minute budget.
**Out of scope:** monitoring/alerting on cycle slowness — that's a future hook concern.

### T-074 — Operator README
**Depends on:** T-073
**References:** design §10.
**Deliverables:**
- `README.md` (workspace root) covering: prerequisites (Python 3.11+, free disk), `.env` setup with each required variable named (no example values), first-run sequence (`init` → per-source backfill → cycle), daily-cron setup, recovery procedure, and the license-non-commercial constraint for ACLED.
- A `docs/sources.md` listing the 12 v1 sources with: license, ToS link, free-tier limits, expected freshness threshold, expected per-source disk footprint after the backfill measured in T-072.
**Verification:** a new operator could (in theory) follow this README from a clean machine to a steady-state daily cycle without code-reading.
**Out of scope:** developer docs, architecture explainer beyond what's already in the spec files.

## Dependency Summary (Critical Path)

For planning purposes, the longest dependency chain is roughly:

    T-001 → T-002 → T-010 → T-011 → T-012 → T-014 → T-030 → T-033 → T-034 → T-035 → T-070 → T-072 → T-073

Connector tasks T-050..T-064 fan out from T-030/T-031/T-032/T-033 and converge at T-070. They can be implemented in parallel by a single developer (one source at a time) without blocking each other. T-064 (ACLED deleted-events reconciliation) depends specifically on T-060 since they share the same connector module.

Phase 0–4 should be completed sequentially. Phase 5 and Phase 6 can interleave. Phase 7 is the gate.

## Tracking

Tasks are tracked here. As each is completed, the operator updates this file with a status and a date:

- **T-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.22.0):

- **T-001** — Initialize Python package and tooling — `DONE` — 2026-05-14
- **T-002** — Bootstrap module skeleton — `DONE` — 2026-05-14
- **T-010** — Canonical schemas as code — `DONE` — 2026-05-14
- **T-011** — Operational tables — `DONE` — 2026-05-14
- **T-012** — DuckDB store wrapper — `DONE` — 2026-05-14
- **T-013** — Schema migrations framework — `DONE` — 2026-05-14
- **T-014** — Staging-merge upsert pattern — `DONE` — 2026-05-14
- **T-015** — Provenance helpers — `DONE` — 2026-05-14
- **T-016** — Disk budget tracker — `DONE` — 2026-05-14
- **T-020** — Environment-variable credential loader — `DONE` — 2026-05-14
- **T-021** — Structured JSON logging — `DONE` — 2026-05-14
- **T-022** — Schedule and caps configuration files — `DONE` — 2026-05-14
- **T-030** — Connector ABC + shared fetch infrastructure — `DONE` — 2026-05-14
- **T-031** — Time and geo normalization helpers — `DONE` — 2026-05-14
- **T-032** — Source registry — `DONE` — 2026-05-14
- **T-033** — Cycle scheduler — `DONE` — 2026-05-14
- **T-034** — Backfill resume mechanism — `DONE` — 2026-05-14
- **T-035** — Cap enforcement during backfill — `DONE` — 2026-05-14
- **T-040** — Cycle report writer — `DONE` — 2026-05-14
- **T-050** — FRED connector — `DONE` — 2026-05-14
- **T-051** — World Bank connector — `DONE` — 2026-05-14
- **T-052** — GDELT events connector — `DONE` — 2026-05-14
- **T-053** — Federal Register connector — `DONE` — 2026-05-14
- **T-054** — WHO DON connector — `DONE` — 2026-05-14
- **T-055** — NOAA CDO connector — `DONE` — 2026-05-14
- **T-056** — USGS Minerals connector — `DONE` — 2026-05-14
- **T-057** — BDI via FRED proxy — `DONE` — 2026-05-14
- **T-060** — ACLED connector (events + auth) — `DONE` — 2026-05-14
- **T-061** — EIA connector — `DONE` — 2026-05-14
- **T-062** — NRC ADAMS connector — `DONE` — 2026-05-14
- **T-063** — regulations.gov (EPA dockets) connector — `DONE` — 2026-05-14
- **T-064** — ACLED deleted-events reconciliation — `DONE` — 2026-05-14
- **T-070** — End-to-end cycle integration test — `DONE` — 2026-05-15
- **T-071** — Smoke test against real services — `DONE` — 2026-05-15
- **T-072** — First backfill run and disk-budget verification — `OPERATOR_BLOCKED` — pending operator-initiated live run
- **T-073** — Daily incremental cycle on operator hardware — `OPERATOR_BLOCKED` — pending operator-initiated live run
- **T-074** — Operator README — `DONE` — 2026-05-15

Phases 1-6 complete. Phase 7 is partially complete: T-070 (integration test), T-071 (smoke harness), and T-074 (operator README + docs/sources.md) DONE; T-072 and T-073 require live network and operator hardware and are deferred to operator-driven runs.

## References

- Requirements: `DATA_INGEST.md` v0.1.0
- Design: `DATA_INGEST_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md` v0.3.0
