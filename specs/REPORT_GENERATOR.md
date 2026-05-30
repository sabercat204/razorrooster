# REPORT_GENERATOR — Requirements

**Subsystem:** `report_generator`
**Codename:** The Crow
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** MINIMAL_EXPOSURE
**Last updated:** 2026-05-14

---

## 1. Purpose

`report_generator` is the operator-facing surface — the thing the operator actually reads on each cycle. It assembles outputs from the analytical subsystems into a single structured report that summarizes:

1. The system's current state of attention (newly surfaced comparisons, active watched analyses, recently resolved analyses).
2. Per-attention-item structured analysis (model probability, market probability, reasoning trace, sizing if relevant, monitoring status if watched).
3. Calibration log (resolutions of previously-stated probabilities and how those compared to outcomes).
4. System health (data freshness, stale-source warnings, library age, errors across the pipeline).

The report is the place where the educational framing is most visible. It is not a "trade ideas" sheet. It's a structured second opinion the operator reads alongside their own thinking.

The report is not interactive. It is a generated document (terminal text by default; markdown export optional) that the operator reads at whatever cadence they choose. Decisions, if any, happen outside the report — in the operator's head, with their hands typing into Polymarket's UI if they choose to act.

`report_generator` does not place orders, recommend trades, or communicate outside the local machine. It reads from local DuckDB, renders text, optionally writes a markdown file.

## 2. Scope

### In scope (v1)

- A cycle command that reads all upstream subsystem outputs since the last report and assembles a structured report.
- Section ordering and content rules: warnings before analyses, "case for market" prominence, calibration log, system health.
- Output formats: terminal-rendered text (default), markdown file export (optional flag).
- Per-section configurability (e.g., operator can suppress sections they don't want).
- Persistence of generated reports for retrospective review.
- Failure isolation: missing upstream data produces "section unavailable" placeholders, not crashes.
- Strict framing constraints: no trading recommendations, no imperative language, no claim that the model is right.

### Out of scope (explicit)

- **Interactive UI.** Reports are generated text. Future versions could add a TUI; v1 does not.
- **Notifications outside local machine** (email, push, Slack, etc.).
- **Trading-actionable output formats** (e.g., orders ready to paste into a CLI).
- **Probability adjustment, recomputation, or smoothing** beyond what upstream subsystems already produced. Reports are descriptive.
- **Cross-cycle aggregations beyond the calibration log** (e.g., performance dashboards, drawdown curves). These are calibration-backtest concerns; if and when they exist, they're a separate render path.

## 3. Operating Assumptions

- **Cadence:** runs daily after `monitor` completes. Operator can run ad-hoc.
- **Audience:** the operator only. Reports are personal.
- **Length:** a typical daily report is 1–3 pages of terminal output. Reports with many active situations may be longer; the format does not artificially compress to a fixed length.
- **Local-only:** reports stay on the operator's machine. The renderer never makes network calls; markdown exports go to local disk.

## 4. Conceptual Model

### 4.1 Report

A *report* is a structured document for one cycle. It has a fixed top-to-bottom structure:

1. Header — date, cycle ID, library version, source freshness summary.
2. System health — warnings about stale sources, library age, errors in upstream subsystems.
3. Newly surfaced comparisons — `mispricing_detector` comparisons that crossed surfacing this cycle and have a `position_engine` analysis. Most prominent.
4. Active watched situations — `monitor` follow-ups for analyses in `'watching'` or `'acted_on'` state, ordered by alert tier.
5. Calibration log — recently resolved comparisons compared to predicted probabilities at comparison time.
6. Watchlist (developing) — `signal_scanner` candidates that did not surface in `mispricing_detector` due to no mapped market, low mapping confidence, etc. — situations the system thinks are interesting but cannot tie to a market.
7. Footer — disclaimer, system version stamp.

Each section can be present or absent based on availability of data; if no data, section header notes "nothing to report this cycle" rather than being silently omitted.

### 4.2 Section Templates

Each section has a strict template:

- **Surfaced comparison block**: class title, sector, model probability with CI, market probability with spread, delta, reasoning trace (rendered), sizing analysis if available (with disclaimer block), warnings.
- **Active watched block**: class title, watch state, days watching, follow-up reasoning text, primary alert tier if any, current vs analysis-time probabilities.
- **Calibration log row**: class, market, resolution outcome, probability at comparison, days from comparison to resolution, calibration error indicator (over/under-confident).
- **Watchlist row**: class, divergence from base rate, reason for non-surfacing, suggestion (e.g. "no mapped Polymarket market — operator might consider mapping if interested").

### 4.3 Disclaimer

Every report includes a footer-level disclaimer block:

    "This report is decision-support analysis. The system surfaces patterns,
     comparisons, and analyses; it does not place trades, recommend specific
     actions, or claim certainty about future events. The operator is
     responsible for any decisions taken based on this report. Polymarket
     prices represent the aggregate view of market participants, who often
     have information the model does not. When model and market disagree, the
     market is correct more often than not."

## 5. Functional Requirements

Requirements use EARS-style phrasing. RG = `report_generator`.

### 5.1 Cycle execution

**REQ-RG-EXEC-001: Generate command**
The generator **shall** provide `razor-rooster report generate [--since <iso>] [--markdown <path>]` that produces a report covering the period since the previous report (default: previous report timestamp from `report_log`, or 24 hours if no prior).
*Verification:* CLI integration test.

**REQ-RG-EXEC-002: Output formats**
The generator **shall** produce terminal-rendered plain text by default. Optional `--markdown <path>` writes a parallel markdown file. Optional `--quiet` suppresses terminal output (useful for automation that just wants the markdown file).
*Verification:* CLI tests for each combination.

**REQ-RG-EXEC-003: Failure isolation per section**
A failure rendering one section **shall not** halt the report. Failed sections render as "section error: <reason>" placeholders.
*Verification:* simulated upstream-data error per section confirms placeholder appears.

### 5.2 Section content

**REQ-RG-SEC-001: Header**
The header **shall** include: cycle date, report ID, pinned library version, source freshness summary (count of stale sources by sector), prior-report-since timestamp.
*Verification:* output inspection.

**REQ-RG-SEC-002: System health**
The system health section **shall** list: any source flagged stale per `data_ingest` freshness view, library refresh age, errored cycles in any subsystem since the prior report, count of suppressed comparisons by suppression reason.
*Verification:* output inspection with synthetic stale data confirms warnings appear.

**REQ-RG-SEC-003: Surfaced comparisons section**
This section **shall** list every `mispricing_detector` comparison with `surfaced = TRUE` since the prior report, ordered by `confidence_weighted_score` descending. For each, embed: scan reasoning trace, market context, position_engine analysis (if available), sizing disclaimer block.
*Verification:* synthetic surfaced comparison renders with all expected fields.

**REQ-RG-SEC-004: Active watched section**
This section **shall** list every `monitor` follow-up with `recommended_review = TRUE` since the prior report, ordered by primary alert tier (resolution > invalidation_triggered > material_shift > precursor_shift > time_decay). For each, embed: follow-up reasoning text, current vs. analysis-time probabilities, days watching, links/refs to original analysis.
*Verification:* synthetic follow-up renders correctly.

**REQ-RG-SEC-005: Calibration log section**
This section **shall** list every `comparison_resolutions` row created since the prior report. For each, embed: class, market question, resolution outcome (yes/no/invalid), model probability at comparison, market probability at comparison, days from comparison to resolution, calibration verdict (e.g., "Model said 0.18 → resolved YES; well-calibrated for a mid-range probability" or "Model said 0.85 → resolved NO; overconfident on YES side").
*Verification:* synthetic resolution renders with calibration verdict.

**REQ-RG-SEC-006: Watchlist section**
This section **shall** list `signal_scanner` candidates that did not produce a `mispricing_detector` comparison this cycle due to: no active mapping, all mappings `low_mapping_confidence`, or all mapped markets stale-priced. For each, brief context and a suggested action (e.g., "Consider mapping this class to a Polymarket market" — explicitly framed as suggestion, not directive).
*Verification:* synthetic unmapped candidate appears in watchlist.

**REQ-RG-SEC-007: Footer**
The footer **shall** include: standard disclaimer block (verbatim from §4.3), system version stamp, run identifier, completion timestamp.
*Verification:* output inspection confirms exact disclaimer text present.

### 5.3 Framing constraints

**REQ-RG-FRAME-001: Imperative-language linter**
The renderer **shall** apply the same imperative-language linter as `position_engine` (sharing `config/forbidden_phrases.yaml`). Output containing forbidden phrases is rejected; renderer raises and report is not written.
*Verification:* adversarial output triggers linter.

**REQ-RG-FRAME-002: "Case for market" prominence**
For every surfaced comparison, the rendered text **shall** include the "case for market" section from the comparison trace at equal prominence to the "case for model" section. Renderer enforces equal-line-count or equal-paragraph-prominence.
*Verification:* output inspection confirms equal prominence.

**REQ-RG-FRAME-003: Sizing-disclaimer block presence**
Every rendered position-engine analysis in the report **shall** include the standard disclaimer block from `position_engine`. Renderer fails if a sizing analysis is rendered without it.
*Verification:* render-without-disclaimer test triggers failure.

**REQ-RG-FRAME-004: No probability claims of certainty**
The renderer **shall** never produce phrases like "definitely will," "certainly," "guaranteed." Linter catalog covers these.
*Verification:* adversarial test.

### 5.4 Persistence

**REQ-RG-PERSIST-001: Report log**
The generator **shall** persist a `report_log` table with: `report_id`, `generated_at`, `since_ts`, `until_ts`, `sections_present`, `sections_failed`, `markdown_path` (NULL if not written), `library_version`, `disclaimer_version_hash`.
*Verification:* schema migration; round-trip test.

**REQ-RG-PERSIST-002: Rendered output retention**
The full rendered text of each report **shall** be persisted in `report_log.rendered_text` (TEXT column) for retrospective access via CLI.
*Verification:* repeated reports accumulate in DB; query returns historical reports.

### 5.5 Configuration

**REQ-RG-CONFIG-001: Section enable/disable**
The generator **shall** read `config/report.yaml` listing sections that are enabled. By default all are enabled. Disabled sections are omitted from the report (with a one-line note in the header that they were disabled).
*Verification:* config-driven test.

**REQ-RG-CONFIG-002: Verbosity**
Configuration **shall** support per-section verbosity (e.g., compact-watchlist that hides reasoning text, full-watchlist that includes it).
*Verification:* config-driven test with both modes.

### 5.6 Logging & observability

**REQ-RG-LOG-001: Structured generation log**
Each report generation **shall** emit a structured JSON log: report_id, generation duration, sections rendered, sections failed, lengths.
*Verification:* log inspection.

## 6. Non-Functional Requirements

**NFR-RG-PERF-001:** A daily report generation **shall** complete within 1 minute on the operator's hardware.

**NFR-RG-AVAIL-001:** Generation failures **shall** degrade gracefully — the operator sees a partial report rather than no report.

**NFR-RG-DISK-001:** `report_log` **shall** stay under 200 MB out of the 100 GB global cap given v1 scale and daily cadence over the first year (rendered text per day is small).

**NFR-RG-DETERMINISM-001:** A report against the same upstream snapshots **shall** produce identical text (excluding `report_id`, generation timestamps).

**NFR-RG-LOCAL-001:** The renderer **shall not** make network calls. Verified via code review and run-time network-disabled testing.

## 7. Open Questions (carry to design phase)

- **OQ-RG-001:** Calibration verdict text — free-form per resolution or template-driven? Default disposition: template-driven; varying the wording invites overconfidence in the descriptions.
- **OQ-RG-002:** Markdown export structure — should sections use `##`/`###` hierarchy, code-block embedding for traces, or plain prose with horizontal rules? Decide in design.
- **OQ-RG-003:** Maximum report length policy — when a report would be very long (many surfaced comparisons), should the generator truncate, summarize, or render in full? Default disposition: render in full; no truncation. Operator can `--quiet` to skip terminal rendering and read markdown.
- **OQ-RG-004:** Calibration log lookback window. Default disposition: list all resolutions since prior report; if first run, list all resolutions in the past 30 days.

## 8. Acceptance Criteria

The `report_generator` v1 is considered complete when:

- A daily generation runs end-to-end within NFR-RG-PERF-001.
- Every section renders or shows a "section unavailable" placeholder.
- Disclaimer block, "case for market" prominence, and sizing disclaimer all appear correctly.
- Imperative-language linter rejects adversarial output.
- Markdown export produces well-formed markdown.
- `report_log` retains historical reports.
- No network calls observable during generation.

## 9. References

- LOOM v0.10.0 — `razorrooster.md`, subsystem registry entry for `report_generator`.
- All upstream subsystem specs.
- `position_engine` Requirements/Design v0.1.0 — for the imperative-language linter and sizing disclaimer block (shared).
- System prompt v0.2 — `razorrooster-prompt.md.txt` (educational framing; reports as second opinions, not directives).
