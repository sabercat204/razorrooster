# Monitor

The Comb — active-observation layer for watched analyses. v1
implementation per `specs/MONITOR.md` (Requirements v0.1.0) and
`specs/MONITOR_DESIGN.md` (Design v0.1.0).

## Purpose

The monitor watches analyses produced by `position_engine` after
they enter the `'watching'` or `'acted_on'` lifecycle state. It does
not recompute analyses (that is `position_engine`'s responsibility).
For each watched analysis, the monitor:

1. Reads current upstream state — latest scan record from
   `signal_scanner`, latest market price snapshot from
   `polymarket_connector`, market resolution from
   `polymarket_resolutions`.
2. Compares current state to the analysis-time snapshot embedded in
   the persisted analysis row.
3. Classifies the change across five alert tiers and surfaces a
   primary tier alongside the full priority-ordered list of all
   tiers that apply.
4. Builds a deterministic, template-driven reasoning text with the
   four bullet narrative: model shift, market shift, precursor
   movement, invalidation evaluation, and time-to-resolution.
5. Persists one `follow_ups` row per analysis plus one
   `monitor_cycles` row per cycle.
6. When a resolution is detected, triggers
   `position_engine.run_expiration_pass` so the watch state
   transitions to `'expired'` immediately.

## Cycle workflow

```bash
razor-rooster monitor run
```

The cycle is structured as:

1. **Stamp cycle row** — a fresh `monitor_cycles` row with
   `started_at` set; aggregates start at zero and are filled in
   when the cycle completes.
2. **Read watched + acted-on analyses** — calls
   `position_engine.persistence.operations.list_by_state` for both
   states and combines the analysis IDs.
3. **Per-analysis evaluation** — for each, fetch the analysis,
   call `evaluate_analysis`, persist the resulting follow-up.
   Per-analysis exception handling captures failures into the
   follow-up's `error` field; the cycle continues.
4. **Resolution-driven expiration** — if any resolutions were
   detected, run the expiration pass once at the end. Idempotent.
5. **Stamp cycle complete** — update `monitor_cycles` with
   aggregate counts (`follow_ups_total`,
   `follow_ups_with_alerts`, `alerts_by_tier`,
   `duration_seconds`).

## Alert tiers

The monitor surfaces alerts in five tiers, listed here in priority
order (highest first):

| Tier | Triggered when |
| - | - |
| `resolution` | The underlying market has resolved (yes / no / invalid). |
| `invalidation_triggered` | At least one stated invalidation criterion has fired against current data. |
| `material_shift` | Model or market probability has moved by a `material` or `major` band since the analysis. |
| `precursor_shift` | At least one underlying precursor variable has changed which side of its threshold it sits on since the analysis. |
| `time_decay` | Days-to-resolution is at or below `time_decay_alert_days` (default 7). |

A follow-up may match multiple tiers. `primary_alert_tier` is the
highest-priority match. `alert_tiers` is the list of all matching
tiers in priority order, persisted on the follow-up for trace
queries.

## Trajectory queries

```bash
razor-rooster monitor trajectory <analysis_id>
```

Returns all follow-ups for one analysis ordered chronologically —
useful for seeing how a watched analysis's situation has evolved
over multiple cycles. The output line per follow-up includes the
timestamp, current model probability, current market probability,
resolution status, and alert tier.

## Notes

```bash
razor-rooster monitor note <follow_up_id> "Reviewed; deciding to hold."
```

Notes are append-only retrospectives stored in `follow_up_notes`.
They appear in the `show` output below the reasoning text. Notes
do not affect cycle behavior or alert ranking — they are pure
operator commentary.

## Trace schema

Each follow-up row's reasoning text is built from a fixed template.
Same inputs always produce the same text. Structure:

```
Watched analysis for class '<class_id>' (mapped to market '<condition_id>').
Since the analysis (<N> days ago):
  - <model probability movement line, or unobservable note>
  - <market probability movement line, or unobservable note>
  - <one line per precursor in the analysis-time signature>
  - <invalidation summary line — triggered list, or "evaluated; N triggered">
  - <N days remaining to resolution> [optional below-window suffix]
<Review recommended ... | No review recommended at this time.>
```

When the market is resolved, the cycle short-circuits the per-line
narrative and produces only the resolution-alert framing.

## Configuration

`config/monitor.yaml`:

```yaml
shift_bands:
  default:
    minor_threshold: 0.01
    material_threshold: 0.05
    major_threshold: 0.15
  per_sector:
    monetary_policy:
      minor_threshold: 0.005
      material_threshold: 0.025
      major_threshold: 0.10

time_decay_alert_days: 7
material_shift_alert_threshold: 0.10
```

- `shift_bands.default` — global thresholds for `classify_band`.
  Magnitudes below `minor_threshold` are tagged `none` (no band),
  below `material_threshold` are `minor`, below `major_threshold`
  are `material`, otherwise `major`.
- `shift_bands.per_sector` — optional per-sector overrides keyed on
  `pl_event_classes.domain_sector`.
- `time_decay_alert_days` — global default for the
  `time_decay` tier trigger.
- `material_shift_alert_threshold` — informational; the alert
  ranker uses the `material`/`major` band instead.

## Failure isolation

If `evaluate_analysis` raises, the cycle:

1. Logs the exception traceback.
2. Persists a minimal "error follow-up" with `error` set to
   `"<TypeName>: <message>"` and `reasoning_text` set to the same.
3. Continues to the next analysis.

If even the error-follow-up write fails, the failure is recorded
on the cycle's `error_summary` JSON but the cycle still completes.

A `watch_states` row pointing at an analysis that no longer exists
is silently skipped (logged at WARNING level). This handles legacy
or orphaned watch state cleanly.

## Tables

- `monitor_cycles` — one row per cycle. Aggregate counts, alert
  breakdown, error summary.
- `follow_ups` — one row per (cycle, analysis). Embeds
  analysis-time and current-time snapshots, shifts, precursor
  snapshot, invalidation evaluations, resolution status, alert
  tier, reasoning text. Idempotent upsert keyed on
  `follow_up_id`.
- `follow_up_notes` — append-only operator notes keyed on
  `note_id`. Indexed by `(follow_up_id, set_at DESC)`.

Schema-migration version space: 6001+.

## CLI summary

```bash
razor-rooster monitor run                       # daily cadence
razor-rooster monitor evaluate <analysis_id>    # ad hoc one-off
razor-rooster monitor show <follow_up_id>       # full reasoning + notes
razor-rooster monitor list-alerts               # all alerts, tier-ordered
razor-rooster monitor list-alerts --tier material_shift
razor-rooster monitor list-alerts --since 2026-05-01
razor-rooster monitor trajectory <analysis_id>  # chronological history
razor-rooster monitor note <follow_up_id> "..." # append note
razor-rooster monitor version                   # schema namespace
```

## Post-T-MON-081 measurement guidance

The first month of operator-driven cycles tells us whether the
default magnitude bands are calibrated for the v1 corpus. The
acceptance test (`T-MON-081`) records:

- **Cycle duration distribution** — should stay sub-minute for
  v1 scale per NFR-MON-PERF-001. If the cycle exceeds a minute,
  profile the per-analysis SQL query plans first; the cycle issues
  ~5 SQL reads per analysis.
- **Empirical shift distribution** — for each analysis, the
  absolute model and market shift magnitudes per cycle. If the
  default minor/material/major boundaries (0.01 / 0.05 / 0.15) put
  too many follow-ups in `material` for ordinary day-to-day
  movement, raise the bands. If `none` and `minor` dominate while
  obvious moves slip past, lower them. Per-sector overrides can
  also tighten or loosen for specific markets.
- **Time-decay alert sensitivity** — the default 7-day window
  may be too eager (every long-running analysis hits time-decay)
  or too late (operator wants more lead time). Adjust per-class
  via the v1.1 `EventClass.time_decay_alert_days` override
  (planned; v1 uses the global default).
- **Disk usage** — per NFR-MON-DISK-001, follow-up rows should
  stay light (one row per analysis per cycle, ~1 KB JSON for
  precursor + invalidation payloads). Trajectory queries are
  fast because rows are not blob-heavy.

Threshold revisions and measurements are recorded in
`specs/MONITOR_TASKS.md` under the T-MON-081 entry.

## See also

- `specs/MONITOR.md` — Requirements v0.1.0
- `specs/MONITOR_DESIGN.md` — Design v0.1.0 (engine internals,
  threat model, test strategy)
- `specs/MONITOR_TASKS.md` — Task tracking and acceptance
  measurements
- `razorrooster.md` — LOOM
