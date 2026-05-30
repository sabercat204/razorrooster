# Kalshi connector — engine reference

Read-only Kalshi market-data ingestion. Sibling to
`polymarket_connector`; shares the same DuckDB store, scheduler,
provenance helpers, and credential-redaction filter from
`data_ingest`. v1 ships `kalshi_connector` as a second prediction-
market venue alongside Polymarket.

This doc covers the engine internals — table layout, cutoff routing,
per-endpoint cost map, cross-venue mapping, post-T-KSI-072 measurement
guidance. For the operator-facing CLI surface see
[user_guide.md](user_guide.md) section 5b. For the requirements +
design + tasks triple see
[../specs/KALSHI_CONNECTOR.md](../specs/KALSHI_CONNECTOR.md),
[../specs/KALSHI_CONNECTOR_DESIGN.md](../specs/KALSHI_CONNECTOR_DESIGN.md),
[../specs/KALSHI_CONNECTOR_TASKS.md](../specs/KALSHI_CONNECTOR_TASKS.md).

## Read-only and unsigned by design

The v1 connector is non-trading. The `KALSHI_CONNECTOR.md` STANDARD
threat context applies because the connector reads only public
market-data endpoints. There is no API key, no RSA-PSS signing, no
order placement. v2 trading work is reserved for a future major
revision and will return the threat context to FULL.

Forbidden imports (asserted at acceptance time —
`tests/kalshi_connector/test_t_ksi_070_end_to_end.py::
test_kalshi_connector_forbidden_imports_absent`):

- `cryptography.hazmat.primitives.asymmetric.padding` — RSA-PSS
  signing, used only for authenticated trading endpoints.
- `websockets`, `from websockets ...` — the Kalshi WebSocket feed is
  reserved for v2.
- `aiohttp.WSMsgType` — same.

Code review at PR time and a runtime acceptance test both enforce
absence.

## Two non-bypassable startup gates

Every CLI subcommand runs both gates before any Kalshi network call.

### Eligibility allow-list (`gates/eligibility.py`)

Kalshi is a CFTC-regulated US designated contract market. The v1
connector enforces an **allow-list** posture (the inverse of
Polymarket's deny-list).

The gate reads `OPERATOR_JURISDICTION` (env var with
`config/operator.yaml` jurisdiction-field fallback) and refuses with
`EligibilityRefusal` when the value is **not** on the allow-list in
`config/kalshi_allowed_jurisdictions.yaml`. The seed list is
`["US"]`. Operator edits the file when Kalshi extends access.

Cross-connector consistency: `OPERATOR_JURISDICTION=US` lets Kalshi
through but trips the Polymarket geo deny-list (US is on it).
`OPERATOR_JURISDICTION=DE` is the inverse — Polymarket through,
Kalshi refused. This is the design contract; both connectors enforce
their own posture from the same operator declaration.

### ToS acknowledgement with posture (`gates/tos.py`)

Mirrors the Polymarket pattern with two Kalshi-specific changes:

1. The acknowledgement record carries an explicit
   `acknowledged_posture='read_only'` so v2 trading work cannot
   accidentally inherit it. A future v2 acknowledgement under
   `posture='trading'` would be a separate flow, and the v1 gate
   refuses if it sees that posture (`ToSPostureMismatch`).
2. The ToS URL is operator-updateable via `config/kalshi.yaml.tos_url`
   (Polymarket hard-codes the URL). The default is
   `https://kalshi.com/docs/kalshi-terms-of-service`.

The hash is SHA-256 over the canonical ToS body. Each successful live
fetch records the hash in `kalshi_tos_version_history`; if a later
fetch fails, the gate falls back to the most recent hash. If both
the live fetch and the fallback fail, the gate raises
`ToSHashUnavailable` rather than letting the connector run with no
known reference.

## Token-bucket rate limiter

Live in `client/rate_limit.py`. Tier-aware: the bucket capacity and
refill rate scale to `headroom_pct * tier_budget_tokens_per_sec[tier]`.
Defaults: 50% of Basic-tier 200 read tokens/sec → 100 tokens
capacity, 100 tokens/sec refill. Switching `tier: Advanced` in
`config/kalshi.yaml` reconfigures the bucket on next startup
(150 tokens at 50% headroom).

### Per-endpoint cost map

Kalshi charges 10 tokens per request by default. The cost map in
`client/endpoint_costs.py` lists every v1 endpoint with its cost so
future drift is a config edit rather than code change. Concrete paths
match before placeholder paths so `/markets/trades` doesn't get
swallowed by `/markets/{ticker}`.

`acquire_for_endpoint(path)` consults the map automatically. Unknown
paths fall back to the documented default (10) and emit a structured
log telling the operator to update the map.

### 429 handling without `Retry-After`

Kalshi 429 responses do **not** include `Retry-After` or
`X-RateLimit-*` headers per Kalshi documentation. The retry helper in
`client/retry.py` uses jittered exponential backoff entirely and
ignores any header that might appear (a regression test asserts this
explicitly). On 429, if the helper has been given the bucket, it
drains the bucket fully so the next attempt does not race against a
stale rate budget.

## Tables

All Kalshi-namespaced tables share the data_ingest provenance prefix
(`source_id`, `source_record_id`, `source_publication_ts`,
`fetch_ts`, `connector_version`, `source_payload_json`,
`superseded_at`) where applicable. Schema-migration version space is
**8001+** and shares the central `schema_migrations` registry with
the other subsystems.

| Table | Purpose | Primary key | Notes |
| - | - | - | - |
| `kalshi_series` | Series catalogue. | `source_id, source_record_id` | Soft-delete via `removed_at`. |
| `kalshi_events` | Events per series. | `source_id, source_record_id` | Soft-delete via `removed_at`. |
| `kalshi_markets` | Markets per event. All four market types (binary, scalar, categorical) round-trip. | `source_id, source_record_id` | `market_type`, `strike_type`, `floor_strike`, `cap_strike` for binary scalar variants. |
| `kalshi_price_snapshots` | 30-min top-of-book snapshots. | `(ticker, snapshot_ts)` | YES quotes only on the wire; NO computed at read time. NULL-preserving. |
| `kalshi_orderbook_snapshots` | On-demand orderbook depth. | `(ticker, snapshot_ts, side, level)` | Persists both YES and derived NO levels. |
| `kalshi_trades` | Watched-markets trade history. | `(trade_id)` | Idempotent re-pulls via watermark. |
| `kalshi_settlements` | Settled-market history (live + historical merged). | `source_id, source_record_id` | `voided=true` for invalidated markets. |
| `kalshi_historical_cutoff` | Single-row state — most recent `/historical/cutoff` snapshot. | implicit | Overwritten at every cycle's start. |
| `kalshi_sector_mapping` | Razor-sector classification per ticker. | `ticker` | `'out_of_scope'` enum value reserved for sports / entertainment markets. |
| `kalshi_tos_version_history` | Hashes of every ToS the gate has observed. | `tos_version_hash` | Network-failure fallback for the gate. |

## Cutoff routing (OQ-KSI-004 resolution)

Kalshi partitions its market universe into "live" and "historical"
slabs. The boundary is exposed via `/historical/cutoff` and advances
on a Kalshi-internal schedule. The connector snapshots the cutoff
once at the start of each cycle and uses the snapshot for all
routing decisions during that cycle. Re-fetching mid-cycle is not
done; the next cycle picks up any advance.

- For a settlement at or after `cutoff.market_settled_ts`: pull from
  `/markets?status=settled`.
- For a settlement before the cutoff: pull from
  `/historical/markets`.
- For trade pulls of a watched ticker: if the local watermark
  (`MAX(kalshi_trades.created_time WHERE ticker=?)`) is at or after
  `cutoff.trades_created_ts`, route to `/markets/trades`; otherwise
  route to `/historical/trades`.

The connector's idempotent upserts make double-queries safe; if a
market straddles the cutoff during one cycle, it might appear in
both queries and the staging-merge handles the overlap cleanly.

## Sector mapping

`mapping/sector_heuristic.py` mirrors the Polymarket pattern with two
Kalshi-specific differences:

1. The mapper consults the market title, sub-title, category, yes/no
   sub-titles, and the parent series' title, category, and tags.
   Kalshi's series-level metadata is richer than Polymarket's, so
   the mapper takes advantage.
2. The output sector enum includes `'out_of_scope'`. This bucket
   captures Sports / Entertainment / daily-life markets that have no
   Razor-sector analogue. They are persisted faithfully in
   `kalshi_markets` but excluded from downstream
   `signal_scanner` / `mispricing_detector` consumption.

Three-pass logic:

- **Pass 1 — category auto-classification.** Markets or series whose
  category matches `_OUT_OF_SCOPE_CATEGORIES` (`"sports"`,
  `"entertainment"`, `"pop culture"`, `"daily life"`,
  `"sports - daily"`) auto-classify as `out_of_scope` without
  consulting the keyword scan.
- **Pass 2 — keyword scan.** For each sector in
  `config/kalshi_sector_keywords.yaml`, count distinct keyword
  matches in the corpus. Word-boundary regex matching so "oil"
  doesn't match "foil"; multi-word keywords ("rate hike") match as
  flexible-whitespace phrases.
- **Pass 3 — tie handling.** Top score with multiple sectors →
  `razor_sector=None` with both top sectors as secondaries.
  All-zero scores → `razor_sector=None` with empty secondaries.
  Operator review queue picks these up.

`mapping/sector_overrides.py` carries the persistence + override +
triage helpers. `upsert_inferred_mapping()` preserves manual
overrides (`confidence='manual'` rows are never clobbered).
`set_override()` writes manual rows; the Kalshi-specific
`'out_of_scope'` value is allowed for explicit operator marking.
`needs_review()` returns inferred-null rows; explicit-null operator
decisions are excluded.

## Cross-venue mapping

The shared `mispricing_detector.engines.comparator` reads market
state from one of two readers based on `mapping.venue`:

- `venue='polymarket'` → `_read_polymarket_market_context()` reads
  `polymarket_markets` + `polymarket_price_snapshots`.
- `venue='kalshi'` → `_read_kalshi_market_context()` reads
  `kalshi_markets` + `kalshi_price_snapshots`.

Both readers return the same `_MarketContext` shape so the rest of
the comparator stays venue-agnostic. The `Comparison`,
`ClassMarketMapping`, `ComparisonResolution` dataclasses all carry a
`venue: Literal["polymarket", "kalshi"]` discriminator.

Operator workflow:

```bash
# Polymarket mapping (default; backward-compatible).
razor-rooster mispricing map cpi_above_target 0xCONDITION_ID

# Kalshi mapping (explicit --venue).
razor-rooster mispricing map cpi_above_target KX-CPI --venue kalshi
```

The `mispricing run` cycle now writes one comparison row per
(class, venue) pair when both venues map. Downstream subsystems
(`position_engine`, `monitor`, `report_generator`) all carry the
`venue` discriminator end-to-end and render `(<venue>)` after the
market identifier in operator-facing output.

Non-binary Kalshi markets are deferred to v1.2 per OQ-KSI-003.
`mispricing map --venue kalshi` refuses with a clear error if the
ticker resolves to a `scalar` or `categorical` market.

## Cycle integration

`cycle.py` exposes `run_kalshi_cycle()` mirroring
`polymarket_connector.cycle.run_polymarket_cycle()`. Stages run
sequentially with per-stage failure isolation:

1. `snapshot_cutoff` — anchors live/historical routing for the cycle.
2. `sync_series` — series catalogue.
3. `sync_events` — events per active series.
4. `sync_markets` — markets per active event; runs the sector
   heuristic when `sector_keywords` is supplied.
5. `snapshot_prices` — 30-min snapshots for active binary markets.
6. `sync_settlements` — live + historical settlement reconcile.
7. `sync_trades` — watched-markets-only trade pull (skipped when
   `watched_markets` is empty).

Each stage is guarded by try/except. Failures are recorded in
`report.errors` with the stage name; subsequent stages still run.
`cycle_report_to_connector_outcome()` projects to the data_ingest
`ConnectorOutcome` shape with `'ok'` / `'partial'` / `'failed'`
status so the cycle report composes Polymarket + Kalshi sections
identically.

Slot ordering in the data_ingest cycle: Kalshi sync runs after
Polymarket sync. Both connectors are virtual sources within the
shared scheduler; failure isolation in the scheduler protects every
other source from a Kalshi outage.

## Post-T-KSI-072 measurement

After the first operator-driven settlement backfill, update this doc
with the measured numbers:

- **Backfill duration.** Live + historical paths separately.
- **Settlement count.** Live (`/markets?status=settled`) vs.
  historical (`/historical/markets`). The split tells us how
  recently Kalshi expanded coverage.
- **Disk footprint.** Bytes added to `data/trough.duckdb` after the
  backfill. Used to validate NFR-KSI-PERF-003 (5 GB target after one
  week of steady-state). DEFER-KSI-003 placeholder lives here until
  measurement.
- **Cutoff-advance behavior.** During the backfill, did the cutoff
  advance? How often? The connector's snapshot-once-per-cycle
  routing means a mid-cycle advance produces bounded re-queries
  rather than missing rows; record what was observed for future
  tuning.
- **Token-spend logs.** Confirm the limiter's 50% headroom is real.
  Look for sustained periods at the bucket cap; if they appear
  routinely, the operator should consider raising the tier or
  lowering the snapshot cadence.

## See also

- [README.md](../README.md) — quick-start, daily cadence summary,
  Kalshi first-run flow.
- [user_guide.md](user_guide.md) section 5b —
  `razor-rooster kalshi` CLI reference.
- [sources.md](sources.md) — `kalshi` source row entry.
- [../specs/KALSHI_CONNECTOR.md](../specs/KALSHI_CONNECTOR.md) — v1.1
  requirements (EARS, with stable IDs).
- [../specs/KALSHI_CONNECTOR_DESIGN.md](../specs/KALSHI_CONNECTOR_DESIGN.md)
  — design with all open-question resolutions (OQ-KSI-001..007).
- [../specs/KALSHI_CONNECTOR_TASKS.md](../specs/KALSHI_CONNECTOR_TASKS.md)
  — task tracking, dependency graph, cross-subsystem migration plan.
