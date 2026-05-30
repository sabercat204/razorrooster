# Razor-Rooster v1 Sources

Per-source reference for the 12 v1 ingest sources. License, ToS, free-tier limits, expected freshness, and expected per-source disk footprint after the T-072 backfill measurement.

The footprint column is a placeholder until T-072 measures the live backfill. Update this file with measured numbers once the operator has run the first full backfill.

| Source | Schema | Auth | License | ToS | Free-tier limits | Cadence | Freshness threshold | Backfill depth | Expected disk |
| - | - | - | - | - | - | - | - | - | - |
| `fred` | `time_series` | API key | public-domain | https://fred.stlouisfed.org/docs/api/terms_of_use.html | ~120 req/min | daily 08:00 UTC | 48h | 50 yr | TBD (T-072) |
| `worldbank` | `time_series` | none | CC BY-4.0 | https://datacatalog.worldbank.org/public-licenses | ~150 req/min | weekly Mon | 14d | 50 yr | TBD |
| `who_don` | `event_stream` | none | © WHO (terms permit non-commercial) | https://www.who.int/about/policies/publishing/copyright | RSS feed (light load) | daily 08:15 UTC | 48h | rolling window | TBD |
| `acled` | `event_stream` | OAuth password grant | ACLED Terms (hash-versioned in store) — non-commercial default | https://acleddata.com/terms-of-use/ | ~5 req/sec (conservative) | daily 09:00 UTC | 72h | ~30 yr | TBD |
| `gdelt_events` | `event_stream` | none | GDELT terms (events-only in v1; GKG deferred) | https://www.gdeltproject.org/about.html | 15-min event files; no rate limit on raw zips | daily 08:30 UTC | 24h | 5 yr (capped) | ~30 GB cap |
| `federal_register` | `document_docket` | none | public-domain (US Government) | https://www.federalregister.gov/reader-aids/developer-resources/rest-api-documentation | ~60 req/min advisory | daily 08:45 UTC | 48h | 1994+ (~31 yr) | TBD |
| `noaa` | `time_series` | API token | public-domain (US Government) | https://www.ncdc.noaa.gov/cdo-web/webservices/v2 | 1000 req/day, 5 req/sec | daily 09:15 UTC | 48h | 50 yr | TBD |
| `usgs_minerals` | `time_series` | none | public-domain (US Government) | https://www.usgs.gov/disclaimer | annual publication | annual | 1 yr | 50 yr | TBD |
| `eia` | `time_series` | API key | public-domain (US Government) | https://www.eia.gov/about/copyrights_reuse.php | 5000 req/hr | daily 09:30 UTC | 48h | 30 yr | TBD |
| `nrc_adams` | `document_docket` | API subscription key | public-domain (US Government) | https://www.nrc.gov/site-help/copyright.html | subscription tier | weekly Mon | 14d | 1999+ (~25 yr; PARS Library only) | TBD |
| `regulations_gov` | `document_docket` | API key | public-domain (US Government) | https://open.gsa.gov/api/regulationsgov/ | 1000 req/hr | daily 09:45 UTC | 48h | 2003+ (~22 yr; EPA dockets only) | TBD |
| `bdi` | `time_series` | (uses FRED key) | public-domain via FRED proxy series | https://fred.stlouisfed.org/docs/api/terms_of_use.html | shares FRED | daily 08:05 UTC | 48h | 50 yr | TBD |
| `polymarket` | (`polymarket_*` namespace; not a canonical schema) | none for read; ToS-versioned ack required | Polymarket TOS (operator records hash) | https://polymarket.com/tos | ~100 req/sec firm-wide (connector targets ≤50 req/sec) | sync daily; snapshots hourly | 6h prices / 48h resolutions | full Polymarket history (Polymarket has been live since 2020) | TBD |
| `polymarket_resolutions` | (`polymarket_resolutions`) | none | shares Polymarket TOS | shares Polymarket TOS link | shares Polymarket budget | daily | 48h | full historical resolutions via Gamma | TBD |
| `kalshi` | (`kalshi_*` namespace; not a canonical schema) | none for read; ToS-versioned ack required (read_only posture) | Kalshi TOS (operator records hash + posture) | https://kalshi.com/docs/kalshi-terms-of-service | tier-aware token bucket; default 50% of Basic tier (200 read tokens/sec) | sync daily; price snapshots every 30 min | 3h prices / 48h markets | full Kalshi history (subject to live/historical cutoff routing) | TBD |
| `kalshi_settlements` | (`kalshi_settlements`) | none | shares Kalshi TOS | shares Kalshi TOS link | shares Kalshi budget | daily | 48h | live + historical merged via cutoff routing | TBD |

## Notes

- **Polymarket geo + ToS gates**: Two non-bypassable startup gates run before every Polymarket subcommand. The geo gate requires `OPERATOR_JURISDICTION` and refuses on values matching `config/restricted_jurisdictions.yaml`. The ToS gate fetches the live Polymarket Terms, hashes the canonical text, and compares against the operator's recorded acknowledgement (`license_terms_hash` on the `polymarket` source row). See README.md "Polymarket connector" for the first-run flow.
- **Kalshi eligibility + ToS gates**: Two non-bypassable startup gates run before every `razor-rooster kalshi` subcommand. The eligibility gate **inverts** the Polymarket pattern — it requires `OPERATOR_JURISDICTION` to be **on** the allow-list in `config/kalshi_allowed_jurisdictions.yaml` (seed list: `["US"]`). The same env var drives both gates; one operator declaration produces opposite outcomes for the two venues by design (Kalshi is US-only; Polymarket is non-US). The Kalshi ToS gate also records an explicit `acknowledged_posture='read_only'` so v2 trading work cannot inherit a v1 acknowledgement. Default ToS URL: `https://kalshi.com/docs/kalshi-terms-of-service` (operator-updateable via `config/kalshi.yaml.tos_url`). See `docs/kalshi_connector.md` for the engine reference.
- **Polymarket data is in its own namespace**: seven Polymarket-namespace tables (`polymarket_markets`, `polymarket_price_snapshots`, `polymarket_orderbook_snapshots`, `polymarket_trades`, `polymarket_resolutions`, `polymarket_sector_mapping`, `polymarket_tos_version_history`) live alongside the four canonical schemas (`event_stream`, `time_series`, `document_docket`, `geospatial_indicator`) in the same DuckDB store. The provenance prefix is shared; the column shapes are Polymarket-specific.
- **Kalshi data is in its own namespace**: ten Kalshi-namespace tables (`kalshi_series`, `kalshi_events`, `kalshi_markets`, `kalshi_price_snapshots`, `kalshi_orderbook_snapshots`, `kalshi_trades`, `kalshi_settlements`, `kalshi_historical_cutoff`, `kalshi_sector_mapping`, `kalshi_tos_version_history`) live alongside the Polymarket and canonical schemas. Schema-migration version space 8001+. The provenance prefix is shared; the column shapes are Kalshi-specific. Orderbook snapshots persist YES + derived NO levels; the Kalshi REST API returns YES depth only.
- **Cross-venue mapping**: `mispricing_detector.class_market_mappings` carries a `venue` discriminator (`'polymarket'` or `'kalshi'`). One class can map to both venues simultaneously. The comparator reads venue-specific market state and emits one comparison row per (class, venue) pair; downstream subsystems carry the discriminator end-to-end and render `(<venue>)` after each market identifier.

- **Stored full-text bodies**: none. NRC ADAMS, regulations.gov, and Federal Register store the URI to the source-hosted body (`full_text_uri`) and not the body itself. Local-disk caching is deferred per design DEFER-004.
- **GDELT GKG**: deferred from v1 due to the ~166 GB compressed footprint. Only the events table is in scope.
- **OPEC MOMR**: deferred to v2 — PDF parsing risk too high for v1.
- **ACLED commercial use**: `commercial_use_recorded_grant` is FALSE by default. Operators must explicitly review ACLED's then-current Terms and record any commercial-use grant via the connector's license-acknowledgement gate. See README.md "Credentials → ACLED license gate".
- **GDELT cap**: 5-year backfill window is enforced in code (`config/source_caps.yaml: per_source.gdelt_events.max_backfill_years = 5`), not just by convention.
- **All timestamps stored UTC** (TIMESTAMPTZ) per REQ-NORM-002.

## Updating after T-072

After the first full backfill (T-072), update the "Expected disk" column with measured per-source byte counts. The measurement command:

```bash
razor-rooster ingest status                     # current freshness
.venv/bin/python -c '
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.disk_budget import per_source_row_counts
from pathlib import Path
store = DuckDBStore(Path("data/trough.duckdb"))
with store.connection() as conn:
    for source_id, row_count in sorted(per_source_row_counts(conn).items()):
        print(f"{source_id:<24} {row_count:>10} rows")
store.close()
'
```

Combine the row count with the database file size at `data/trough.duckdb` (use `ls -l` or `stat`) to derive average per-source bytes-per-row, then plug measurements into this table and into `config/source_caps.yaml` if any per-source cap needs tuning.
