# MONITOR — Requirements

**Subsystem:** `monitor`
**Codename:** The Comb
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14

---

## 1. Purpose

`monitor` is the active-observation layer for analyses the operator is paying attention to. Its job, on each cycle:

1. Identify all `position_engine` analyses with active watch states (`'watching'` or `'acted_on'`).
2. For each, evaluate whether anything has changed materially since the analysis was produced — has the model probability moved, has the market price moved, has a precursor variable shifted, is invalidation imminent, has the market resolved.
3. Produce *follow-up records* for each watched analysis describing what changed and what (if anything) the operator should reconsider.
4. Surface alerts to `report_generator` for the cycle report's "active situations" section.

Downstream consumers:
- `report_generator` reads follow-up records to populate the cycle report.
- The calibration backtest reads follow-up trajectories alongside resolutions for richer post-hoc analysis.

`monitor` does not place orders, change watch states automatically (except `'expired'` on resolution), or recompute the analysis from scratch — that is `position_engine`'s job. It observes and surfaces.

## 2. Scope

### In scope (v1)

- Daily evaluation of every analysis in `'watching'` or `'acted_on'` state.
- Per-analysis change detection across four dimensions: model probability shift, market price shift, precursor variable shift, time decay (proximity to resolution date).
- Invalidation-criteria evaluation: for each criterion in the original analysis, evaluate the current state and flag whether the criterion has triggered.
- Resolution detection: when the underlying market resolves, surface a final alert with the outcome and update the watch state to `'expired'` (interlocked with `position_engine`).
- Follow-up record persistence: every cycle produces a follow-up per active watched analysis, retained indefinitely.
- Alert ranking: surfaced alerts are ordered by importance (resolution > criterion-triggered > material-shift > minor-shift > no-change).
- Failure isolation: bad analysis input or missing market data does not stop the cycle.

### Out of scope (explicit)

- **Real-time monitoring.** v1 is daily-cadence batch. Real-time alerts are a v2 consideration.
- **Notifications outside the cycle report** (email, push, etc.). Output is structured records that `report_generator` consumes; no separate alerting channel.
- **Automatic state changes** beyond resolution-triggered expiration. Operator manages watch states themselves.
- **P&L tracking** for `'acted_on'` analyses. Out of v1 scope (recommendation-only system).
- **Cross-analysis correlation tracking** (e.g., if multiple watched analyses share a precursor that just shifted). Useful but adds complexity; deferred.

## 3. Operating Assumptions

- **Cadence:** runs daily after `position_engine` completes. Operator can run ad-hoc.
- **Data dependencies:** requires fresh `signal_scanner`, `polymarket_connector`, and `position_engine` data. If any is stale beyond a configurable threshold, follow-ups are flagged.
- **Watched-set scale:** v1 assumes the operator typically has 5–30 active watched analyses at any time. Larger scales are supported but performance NFRs assume this band.

## 4. Conceptual Model

### 4.1 Follow-up Record

A *follow-up record* is the output of one cycle's evaluation of one watched analysis:

- Reference to the original analysis and watch state.
- Snapshot of current state: latest `signal_scanner` model probability, latest `polymarket_connector` market price, latest precursor variable values.
- Per-dimension change indicators:
  - `model_probability_shift`: numeric change from analysis-time, with magnitude classification (`none`, `minor`, `material`, `major`).
  - `market_probability_shift`: same.
  - `precursor_shifts`: list per precursor of (current value, analysis-time value, threshold-crossed direction).
  - `time_decay`: days since analysis, days remaining to resolution.
- `invalidation_triggers`: list of original invalidation criteria with current evaluation (`triggered` / `not_triggered` / `cannot_evaluate`).
- `resolution_status`: `unresolved`, `resolved_yes`, `resolved_no`, `resolved_invalid`.
- `recommended_review`: derived flag indicating whether the operator should review this analysis (e.g. material shift in any dimension or a triggered invalidation criterion).
- Reasoning text describing the changes in human-readable form.
- Warning flags carry-through.

### 4.2 Alert

An *alert* is a follow-up record marked for surfacing in the cycle report. Alerts are ordered by importance:

1. `resolution_alert`: market resolved.
2. `invalidation_triggered_alert`: a stated invalidation criterion has fired.
3. `material_shift_alert`: model or market probability has shifted by ≥0.10 in absolute terms (configurable).
4. `precursor_shift_alert`: a precursor variable has crossed a meaningful threshold.
5. `time_decay_alert`: market resolves within a short window (default: ≤7 days).

Multiple alert reasons can apply to one follow-up; the strongest reason determines display order in the cycle report.

## 5. Functional Requirements

Requirements use EARS-style phrasing. MON = `monitor`.

### 5.1 Cycle execution

**REQ-MON-EXEC-001: Cycle command**
The monitor **shall** provide `razor-rooster monitor run` that evaluates every watched and acted-on analysis.
*Verification:* CLI integration test.

**REQ-MON-EXEC-002: Per-analysis scope**
The monitor **shall** support `razor-rooster monitor evaluate <analysis_id>` for ad-hoc evaluation.
*Verification:* CLI integration test.

**REQ-MON-EXEC-003: Failure isolation**
A failure evaluating one analysis **shall not** halt the cycle or corrupt other follow-up records. Failures are logged structured and surfaced in the cycle summary.
*Verification:* integration test with one analysis throwing; others complete.

### 5.2 Change detection

**REQ-MON-DETECT-001: Model probability shift**
The monitor **shall** compute the change in model probability between the analysis-time scan and the most recent scan for the same event class. Magnitude classification: `none` (≤0.01), `minor` (0.01–0.05), `material` (0.05–0.15), `major` (>0.15). Thresholds configurable.
*Verification:* unit test for each band.

**REQ-MON-DETECT-002: Market probability shift**
The monitor **shall** compute the change in market probability between the analysis-time price snapshot and the most recent price snapshot. Magnitude classification same as REQ-MON-DETECT-001.
*Verification:* unit test for each band.

**REQ-MON-DETECT-003: Precursor variable shifts**
For each precursor in the underlying class's signature, the monitor **shall** evaluate the current value and compare to the analysis-time value. Threshold-crossing (variable now past or below its signature threshold when it wasn't before, or vice versa) **shall** be tagged.
*Verification:* unit test for crossing in each direction.

**REQ-MON-DETECT-004: Time decay**
The monitor **shall** compute days since analysis and days remaining to resolution. Time-decay alert fires when days-to-resolution drops below a configurable threshold (default: 7).
*Verification:* unit test for threshold trigger.

**REQ-MON-DETECT-005: Invalidation criterion evaluation**
For each invalidation criterion stored on the original analysis, the monitor **shall** evaluate against current state. Outcomes: `triggered` (criterion is now true), `not_triggered`, or `cannot_evaluate` (data missing or stale).
*Verification:* unit tests across criterion types (precursor, market price, mapping confidence).

**REQ-MON-DETECT-006: Resolution detection**
The monitor **shall** check `polymarket_resolutions` for the analysis's market each cycle. If resolved, follow-up record's `resolution_status` set to the actual outcome. The monitor **shall** trigger watch state expiration via the same path `position_engine` uses.
*Verification:* simulated resolution flow: monitor surfaces resolution alert and watch state moves to `'expired'`.

### 5.3 Recommended review

**REQ-MON-REVIEW-001: Recommended review flag**
A follow-up **shall** have `recommended_review = TRUE` when any of the following applies:
- `resolution_status` is not `unresolved`.
- Any invalidation criterion is `triggered`.
- `model_probability_shift` or `market_probability_shift` is `material` or `major`.
- Days-to-resolution is below the time-decay threshold.

Otherwise `recommended_review = FALSE`.
*Verification:* unit tests for each trigger.

**REQ-MON-REVIEW-002: Reasoning text**
Each follow-up **shall** include human-readable text explaining what changed and why a review is recommended (or why not).
*Verification:* output inspection test.

### 5.4 Alert ranking

**REQ-MON-ALERT-001: Alert tier assignment**
Each follow-up with `recommended_review = TRUE` **shall** be tagged with one or more alert tiers per §4.2. The strongest applicable tier is the primary alert level.
*Verification:* unit tests across alert combinations.

**REQ-MON-ALERT-002: Alert ordering for report**
The monitor **shall** expose follow-ups ordered by alert tier for `report_generator` consumption.
*Verification:* query test confirms ordering.

### 5.5 Persistence

**REQ-MON-PERSIST-001: Follow-up tables**
The monitor **shall** persist outputs to `follow_ups` (one row per (cycle, analysis) pair) and `monitor_cycles` (one row per cycle execution).
*Verification:* schema migration; round-trip test.

**REQ-MON-PERSIST-002: Time-series retention**
Historical follow-ups **shall** be retained indefinitely.
*Verification:* repeated cycles accumulate rows.

**REQ-MON-PERSIST-003: Trajectory queryability**
The persisted follow-ups for a single analysis over time **shall** form a queryable trajectory (model and market probabilities over time, precursor evolution, alert history). This is the data the calibration backtest uses for richer analysis beyond at-resolution snapshots.
*Verification:* DuckDB query produces trajectory time-series for a representative analysis.

### 5.6 Logging & observability

**REQ-MON-LOG-001: Structured cycle log**
Each cycle **shall** emit a structured JSON log: cycle_id, follow-ups produced, alerts by tier, durations, warnings.
*Verification:* log inspection.

**REQ-MON-LOG-002: Per-alert log**
Every alert (any tier) **shall** be logged at INFO with analysis_id, tier, primary reason. Non-alerts (no review recommended) at DEBUG.
*Verification:* log inspection after representative cycle.

## 6. Non-Functional Requirements

**NFR-MON-PERF-001:** A daily cycle (5–30 watched analyses) **shall** complete within 2 minutes on the operator's hardware.

**NFR-MON-AVAIL-001:** Monitor failures **shall** degrade gracefully — `report_generator` sees absent or stale follow-ups, not crashes.

**NFR-MON-DISK-001:** Monitor tables **shall** stay under 100 MB out of the 100 GB global cap, given v1 scale and daily cadence over the first year.

**NFR-MON-DETERMINISM-001:** A cycle against the same upstream snapshots **shall** produce identical follow-ups (excluding `cycle_id` and timestamps).

## 7. Open Questions (carry to design phase)

- **OQ-MON-001:** Magnitude classification thresholds (0.01, 0.05, 0.15) — validate against empirical distributions of probability shifts on watched analyses after first month of operation.
- **OQ-MON-002:** Time-decay threshold default (7 days). Per-class override useful?
- **OQ-MON-003:** Whether to compute trajectory-derived metrics in the follow-up itself (e.g., 7-day moving average, trend slope) or leave to downstream consumers. Default: leave to downstream.
- **OQ-MON-004:** Operator notes on follow-ups — should the operator be able to attach a note to a follow-up explaining their interpretation? Useful for retrospective review. Default: implement as a separate `follow_up_notes` table accessed via CLI.

## 8. Acceptance Criteria

The `monitor` v1 is considered complete when:

- A daily cycle runs end-to-end within NFR-MON-PERF-001.
- Every watched and acted-on analysis produces a follow-up record per cycle.
- Change detection works across all four dimensions.
- Invalidation criteria are correctly evaluated.
- Resolution detection fires and triggers watch state expiration.
- Alert ranking is consistent and queryable.
- Failure isolation works.

## 9. References

- LOOM v0.9.0 — `razorrooster.md`, subsystem registry entry for `monitor`.
- `position_engine` Requirements/Design/Tasks v0.1.0 — for analyses and watch states.
- `signal_scanner` Requirements/Design/Tasks v0.1.0 — for current scan records.
- `polymarket_connector` Requirements/Design/Tasks v0.1.0 — for market prices and resolutions.
- `mispricing_detector` Requirements/Design/Tasks v0.1.0 — for source comparisons.
