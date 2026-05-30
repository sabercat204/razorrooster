# REPORT_GENERATOR — Implementation Tasks

**Subsystem:** `report_generator`
**Codename:** The Crow
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `REPORT_GENERATOR.md` v0.1.0
- Design: `REPORT_GENERATOR_DESIGN.md` v0.1.0

**Hard prerequisites:**
- All upstream subsystems' implementation phases through their cycle-running tasks.
- `position_engine` T-PE-041 (linter) — shared, not duplicated.

Task IDs prefixed `T-RG-NNN`.

---

## Phase 0 — Bootstrap

### T-RG-001 — Module init
**Depends on:** monitor T-MON-001.
**References:** design §3.1.
**Deliverables:** module tree, CLI click group, `config/report.yaml`, `templates/calibration_verdicts.yaml`, `templates/disclaimer.txt`, `templates/section_headers.yaml`, test layout.
**Verification:** `--help` runs; pytest discovery clean.
**Out of scope:** logic.

## Phase 1 — Schemas

### T-RG-010 — Report log schema + migration
**Depends on:** T-RG-001, data_ingest T-013.
**References:** design §3.3.
**Deliverables:** DDL for `report_log`. m0001 migration.
**Verification:** schema applies; round-trip test.
**Out of scope:** persistence helpers.

### T-RG-011 — Persistence helpers
**Depends on:** T-RG-010, data_ingest T-014.
**References:** REQ-RG-PERSIST-001..002.
**Deliverables:** `persistence/operations.py` with: `query_last_report`, `persist_report`, `get_report`, `list_reports`.
**Verification:** unit tests per helper.
**Out of scope:** business logic.

## Phase 2 — Section Assemblers

### T-RG-020 — Header assembler
**Depends on:** T-RG-001, data_ingest T-015 (freshness query).
**References:** REQ-RG-SEC-001, design §3.5.
**Deliverables:** `engines/section_assemblers/header.py` returning the header content dict.
**Verification:** unit test against synthetic state.
**Out of scope:** rendering.

### T-RG-021 — System health assembler
**Depends on:** T-RG-001, data_ingest T-015.
**References:** REQ-RG-SEC-002, design §3.5.
**Deliverables:** `engines/section_assemblers/system_health.py` returning structured health dict (stale sources, errored cycles per subsystem, suppressed-comparison breakdown).
**Verification:** unit test with synthetic stale state confirms expected output.
**Out of scope:** rendering.

### T-RG-022 — Surfaced comparisons assembler
**Depends on:** T-RG-001, mispricing_detector T-MD-011, position_engine T-PE-011.
**References:** REQ-RG-SEC-003, design §3.5.
**Deliverables:** `engines/section_assemblers/surfaced.py` returning list of surfaced comparison content dicts. Each includes scan trace, comparison trace, optional position-engine analysis, warnings.
**Verification:** unit test against synthetic surfaced comparisons.
**Out of scope:** rendering.

### T-RG-023 — Watched assembler
**Depends on:** T-RG-001, monitor T-MON-011, position_engine T-PE-011.
**References:** REQ-RG-SEC-004, design §3.5.
**Deliverables:** `engines/section_assemblers/watched.py` returning list of follow-up content dicts ordered by alert tier.
**Verification:** unit test confirms ordering.
**Out of scope:** rendering.

### T-RG-024 — Calibration log assembler
**Depends on:** T-RG-001, mispricing_detector T-MD-011 (resolutions table).
**References:** REQ-RG-SEC-005, OQ-RG-001 resolution, OQ-RG-004 resolution, design §3.5.
**Deliverables:**
- `engines/section_assemblers/calibration.py` listing recent `comparison_resolutions` with calibration verdicts via template selection.
- Reads `templates/calibration_verdicts.yaml`.
- Selects band based on `(predicted_p_band, observed_outcome)`.
**Verification:**
- Unit test: each band/outcome combination produces expected verdict.
- Lookback test: first-run case looks back 30 days, subsequent runs look back to last report.
**Out of scope:** rendering.

### T-RG-025 — Watchlist assembler
**Depends on:** T-RG-001, signal_scanner T-SCAN-011, mispricing_detector T-MD-011.
**References:** REQ-RG-SEC-006, design §3.5.
**Deliverables:** `engines/section_assemblers/watchlist.py` listing scan candidates that did not produce a comparison this cycle, with reason annotations.
**Verification:** unit test against synthetic state with mixed candidates.
**Out of scope:** rendering.

### T-RG-026 — Footer assembler
**Depends on:** T-RG-001.
**References:** REQ-RG-SEC-007, design §3.5.
**Deliverables:** `engines/section_assemblers/footer.py` returning footer dict with disclaimer text loaded from `templates/disclaimer.txt`.
**Verification:** unit test confirms exact disclaimer text.
**Out of scope:** rendering.

## Phase 3 — Renderers

### T-RG-030 — Shared rendering helpers
**Depends on:** T-RG-001.
**References:** REQ-RG-FRAME-001..004, design §3.6.
**Deliverables:** `renderer/shared.py` with: disclaimer block formatter, warnings block formatter, "case for model" / "case for market" equal-prominence formatter, section divider helpers.
**Verification:**
- Unit test: equal-prominence formatter produces visually balanced output (line count or word count comparable).
**Out of scope:** specific renderer implementations.

### T-RG-031 — Terminal renderer
**Depends on:** T-RG-030, all section assemblers (T-RG-020..T-RG-026).
**References:** REQ-RG-EXEC-002, design §3.6 terminal renderer.
**Deliverables:** `renderer/terminal.py` `render(sections, since, until, library_version) -> str`.
- ASCII section dividers, indentation, plain text.
- Width target 80–100 columns; no fixed truncation.
**Verification:**
- Unit test against synthetic content dicts produces expected text.
- Visual-inspection acceptance test on 80-col terminal.
**Out of scope:** colored output.

### T-RG-032 — Markdown renderer
**Depends on:** T-RG-030, all section assemblers.
**References:** REQ-RG-EXEC-002, OQ-RG-002 resolution, design §3.6 markdown renderer.
**Deliverables:** `renderer/markdown.py` `render(sections, since, until, library_version) -> str` producing GFM-valid output.
**Verification:**
- Unit test produces expected markdown.
- Validity test: rendered text passes a basic markdown parser without errors.
**Out of scope:** non-GFM dialects.

### T-RG-033 — Linter integration
**Depends on:** T-RG-031, T-RG-032, position_engine T-PE-041.
**References:** REQ-RG-FRAME-001, REQ-RG-FRAME-004, design §3.7.
**Deliverables:**
- Generator imports `position_engine.frame.linter` and applies it to both terminal and markdown outputs before persistence.
- Test verifies the linter's `forbidden_phrases.yaml` is the same file (no duplication).
**Verification:**
- Adversarial test: handcrafted output containing forbidden phrase triggers rejection in both renderers.
- Standard render passes.
**Out of scope:** alternate linter implementations.

## Phase 4 — Generator Orchestration

### T-RG-040 — Generator
**Depends on:** T-RG-033, T-RG-011.
**References:** REQ-RG-EXEC-001, REQ-RG-EXEC-003, REQ-RG-LOG-001, design §3.4.
**Deliverables:** `engines/generator.py` `generate(since=None, markdown_path=None, quiet=False)`.
- Per-section failure isolation.
- Linter check before persist.
- Persistence to `report_log` includes both terminal and markdown text where applicable.
- Structured cycle log.
**Verification:**
- Integration test: full generate against synthetic state.
- Section failure isolation test.
- Linter failure test prevents persistence.
- Quiet mode test confirms no terminal output.
**Out of scope:** CLI.

## Phase 5 — CLI

### T-RG-050 — Report CLI
**Depends on:** T-RG-040.
**References:** REQ-RG-EXEC-001, REQ-RG-EXEC-002, design §3.8.
**Deliverables:**
- `razor-rooster report generate [--since <iso>] [--markdown <path>] [--quiet]`.
- `razor-rooster report show <report_id>` — print stored terminal text.
- `razor-rooster report list [--since <iso>]`.
- `razor-rooster report latest`.
**Verification:** CLI integration tests for each subcommand.
**Out of scope:** TUI.

## Phase 6 — Acceptance

### T-RG-080 — End-to-end integration test
**Depends on:** T-RG-050.
**References:** acceptance criteria in REPORT_GENERATOR.md §8.
**Deliverables:**
- Integration test against synthetic upstream covering all sections.
- Empty-cycle test (no comparisons, no watched, no resolutions) renders "nothing to report" notes correctly.
- Section failure isolation across all sections.
- Markdown export round-trip: render → write → re-read → matches.
- Linter rejection scenario.
**Verification:** integration test passes in `make test`.
**Out of scope:** real network.

### T-RG-081 — Network-disabled smoke test
**Depends on:** T-RG-080.
**References:** NFR-RG-LOCAL-001, design §3.9.
**Deliverables:**
- Test environment with network blocked at the OS level (or via `unittest.mock` blocking httpx).
- `razor-rooster report generate` runs successfully.
**Verification:** test passes; no network activity logged.
**Out of scope:** sandbox tooling beyond this test.

### T-RG-082 — First report on operator hardware
**Depends on:** T-RG-081, monitor T-MON-081.
**References:** NFR-RG-PERF-001, NFR-RG-DISK-001, OQ-RG-002, DEFER-RG-001..002.
**Deliverables:**
- Operator generates first report against the populated system.
- Records: generation duration, report length, sections present.
- Visual review for "case for market" balance — adjust template if needed.
- Updates DEFER-RG-001..002.
**Verification:** measurements recorded under §X-Measurements.
**Out of scope:** automated layout tuning.

### T-RG-083 — Operator README
**Depends on:** T-RG-082.
**References:** design §5.
**Deliverables:**
- README updated with Reports section: daily-cadence setup, terminal vs markdown output, reviewing past reports, customizing sections.
- `docs/reports.md` (or similar) explaining the section structure and the framing constraints.
**Verification:** new operator can follow README.
**Out of scope:** developer docs.

## Dependency Summary (Critical Path)

    T-RG-001 → T-RG-010 → T-RG-011
                            ↓
    [T-RG-020..T-RG-026 in parallel]
                            ↓
                       T-RG-030
                            ↓
              [T-RG-031, T-RG-032]
                            ↓
                       T-RG-033 → T-RG-040 → T-RG-050 → T-RG-080 → T-RG-081 → T-RG-082 → T-RG-083

## Tracking

- **T-RG-001** — Module init — `DONE` — 2026-05-15 — `report cli + module bootstrap + templates + config`
- **T-RG-010** — Report log schema + migration — `DONE` — 2026-05-15 — `m7001_report_generator_initial`
- **T-RG-011** — Persistence helpers — `DONE` — 2026-05-15 — `report_generator.persistence.operations`
- **T-RG-020** — Header assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.header`
- **T-RG-021** — System health assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.system_health`
- **T-RG-022** — Surfaced comparisons assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.surfaced`
- **T-RG-023** — Watched assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.watched`
- **T-RG-024** — Calibration log assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.calibration` + `templates/calibration_verdicts.yaml`
- **T-RG-025** — Watchlist assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.watchlist`
- **T-RG-026** — Footer assembler — `DONE` — 2026-05-15 — `engines.section_assemblers.footer` + `templates/disclaimer.txt`
- **T-RG-030** — Shared rendering helpers — `DONE` — 2026-05-15 — `renderer.shared` (equal_prominence_blocks, disclaimer_block, dividers)
- **T-RG-031** — Terminal renderer — `DONE` — 2026-05-15 — `renderer.terminal`
- **T-RG-032** — Markdown renderer — `DONE` — 2026-05-15 — `renderer.markdown` (GFM)
- **T-RG-033** — Linter integration — `DONE` — 2026-05-15 — shared `position_engine.frame.linter.check_text` applied to both renderers
- **T-RG-040** — Generator orchestrator — `DONE` — 2026-05-15 — `engines.generator.generate`
- **T-RG-050** — Report CLI — `DONE` — 2026-05-15 — `report cli` (generate, show, list, latest, version)
- **T-RG-080** — End-to-end integration test — `DONE` — 2026-05-15 — `tests/report_generator/test_end_to_end_cycle.py`
- **T-RG-081** — Network-disabled smoke test — `DONE` — 2026-05-15 — `test_no_network_calls_during_generate`
- **T-RG-082** — First report on operator hardware — `OPERATOR_BLOCKED` — pending operator first run
- **T-RG-083** — Operator README — `DONE` — 2026-05-15 — `README.md` Reports section + `docs/reports.md`

## Phase 7 — Multi-venue calibration supplement (v0.38.0)

Compatible-subset extension to the v1 base report. See
`specs/REPORT_GENERATOR_SUPPLEMENT_MULTIVENUE.md` for the
supplemental requirements (REQ-RG-COMPAT-*) and acceptance.

- **T-RG-COMPAT-CV-001** — Cross-venue disagreement section — `DONE` — 2026-05-16 — `engines/section_assemblers/cross_venue.py`; 14 tests in `tests/report_generator/test_cross_venue.py`
- **T-RG-COMPAT-SV-001** — Single-venue dominance warning — `DONE` — 2026-05-16 — `engines/section_assemblers/surfaced.py` (`_compute_venue_volume_shares`, `_has_single_venue_dominance`); 10 tests in `tests/report_generator/test_single_venue_dominance.py`
- **T-RG-COMPAT-BRIER-001** — Per-sector Brier score — `DONE` — 2026-05-16 — `engines/section_assemblers/calibration.py` (`_compute_sector_brier_scores`); 13 tests in `tests/report_generator/test_sector_brier.py`
- **T-RG-COMPAT-CONS-001** — Liquidity-weighted consensus — `DONE` — 2026-05-16 — `engines/section_assemblers/cross_venue.py` (`_liquidity_weighted_consensus`); 9 tests in `tests/report_generator/test_cross_venue_consensus.py`

## Phase 8 — Threshold knobs + per-sector overrides + reliability (v0.39.0)

Resolves the three DEFER items from the multi-venue supplement.

- **T-RG-COMPAT-CFG-001** — Wire all four multi-venue thresholds into `config/report.yaml` (DEFER-RG-COMPAT-001) — `DONE` — 2026-05-16 — `report_generator/config/loader.py` (`ReportThresholds`); 12 tests in `tests/report_generator/test_config_loader.py`
- **T-RG-COMPAT-CFG-002** — Per-sector overrides for the four thresholds (DEFER-RG-COMPAT-002) — `DONE` — 2026-05-16 — `report_generator/config/loader.py` (`<knob>_per_sector` fields + lookup helpers); 8 tests in `tests/report_generator/test_config_loader.py`
- **T-RG-COMPAT-REL-001** — Reliability-diagram section, opt-in (DEFER-RG-COMPAT-003) — `DONE` — 2026-05-16 — new module `engines/section_assemblers/reliability.py`; 14 tests in `tests/report_generator/test_reliability.py` plus 4 config-knob tests in `tests/report_generator/test_config_loader.py`

## Phase 9 — Distribution measurements + per-sector reliability + chart (v0.40.0)

Three follow-on items resolving v0.39.0 candidate next-moves.

- **T-RG-COMPAT-MEAS-001** — Per-cycle threshold-distribution measurement helper — `DONE` — 2026-05-16 — `engines/measurements.py`, m7002 migration, `report_threshold_measurements` table, `persist_threshold_measurement` / `list_threshold_measurements`, `razor-rooster report measurements` CLI subcommand; 20 tests in `tests/report_generator/test_threshold_measurements.py`
- **T-RG-COMPAT-REL-002** — Per-sector reliability overrides (`bin_count`, `min_resolutions_per_bin`) — `DONE` — 2026-05-16 — `report_generator/config/loader.py` (two new `_per_sector` fields + lookup helpers); 6 tests across `test_reliability.py` and `test_config_loader.py`
- **T-RG-COMPAT-CHART-001** — ASCII calibration-curve overlay in reliability section (terminal + markdown) — `DONE` — 2026-05-16 — new module `renderer/calibration_chart.py`; 12 tests in `tests/report_generator/test_calibration_chart.py`

## Phase 10 — Additional measurement kinds + explain + suggest (v0.41.0)

Three follow-on items resolving v0.40.0 candidate next-moves.

- **T-RG-COMPAT-MEAS-MULTI-001** — `single_venue_dominance_share` and `brier_per_sector` measurement kinds — `DONE` — 2026-05-16 — `engines/measurements.py` extractors + `engines/generator.py` records all three kinds per cycle; 9 tests in `tests/report_generator/test_threshold_measurements.py`
- **T-RG-COMPAT-EXPL-001** — `razor-rooster report explain-thresholds` CLI — `DONE` — 2026-05-16 — `engines/measurements.py::threshold_percentile_rank` helper + new CLI subcommand; 11 tests in `tests/report_generator/test_threshold_measurements.py`
- **T-RG-COMPAT-SUGG-001** — Threshold-suggestion engine + `razor-rooster report suggest-thresholds` CLI — `DONE` — 2026-05-16 — new module `engines/suggestions.py` + new CLI subcommand; 17 tests in `tests/report_generator/test_threshold_suggestions.py`

## Phase 11 — Reversible apply + retention + stability (v0.42.0)

Three follow-on items resolving v0.41.0 candidate next-moves.

- **T-RG-COMPAT-SUGG-002** — `suggest-thresholds --apply` reversible config write path — `DONE` — 2026-05-16 — `engines/suggestions.py` (`apply_threshold_suggestion`, `ApplyError`, `ApplyResult`, `KIND_TO_CONFIG_KNOB`, `INTEGER_VALUED_KNOBS`); CLI flags `--apply`, `--yes`, `--config`; 16 tests in `tests/report_generator/test_threshold_suggestions.py`
- **T-RG-COMPAT-PRUNE-001** — Threshold-measurement retention/prune CLI — `DONE` — 2026-05-16 — `persistence/operations.py` (`prune_threshold_measurements`, `PruneConfirmationError`); new `razor-rooster report prune-measurements` CLI subcommand; 16 tests in `tests/report_generator/test_prune_measurements.py`
- **T-RG-COMPAT-SUGG-003** — Stability metric on suggestion engine — `DONE` — 2026-05-16 — `engines/suggestions.py` (`stability_cv`, `unstable`, `DEFAULT_STABILITY_CV_THRESHOLD`); CLI prints stability line per kind and warns in `--apply` prompt when unstable; 10 tests in `tests/report_generator/test_threshold_suggestions.py`

## Phase 12 — Auto-prune + diff preview + tuning log (v0.43.0)

Three follow-on items resolving v0.42.0 candidate next-moves.

- **T-RG-COMPAT-AUTOPRUNE-001** — Auto-prune in report cycle — `DONE` — 2026-05-16 — `config/loader.py` (`AutoPruneConfig`); `engines/generator.py` (`_maybe_auto_prune_measurements`); `config/report.yaml` (commented `auto_prune:` block); 12 tests in `tests/report_generator/test_auto_prune.py`
- **T-RG-COMPAT-DIFF-001** — `--diff` flag for `suggest-thresholds --apply` — `DONE` — 2026-05-16 — `engines/suggestions.py::compute_apply_diff` + `_format_yaml_scalar`; 8 tests in `tests/report_generator/test_threshold_suggestions.py`
- **T-RG-COMPAT-TUNINGLOG-001** — `threshold_tuning_log` table + `tuning-log` CLI + `--note` flag — `DONE` — 2026-05-16 — m7003 migration; `persistence/operations.py` (`ThresholdTuningLogEntry`, `persist_tuning_log_entry`, `list_tuning_log_entries`); CLI `tuning-log` subcommand; CLI `--note TEXT` flag on `suggest-thresholds`; tuning-log write hooked into apply path with best-effort isolation; 13 tests in `tests/report_generator/test_tuning_log.py`

## Phase 13 — Undo + recent-tuning section + HTML mode (v0.44.0)

Three follow-on items resolving v0.43.0 candidate next-moves.

- **T-RG-COMPAT-UNDO-001** — `tuning-log-undo` CLI + helper — `DONE` — 2026-05-16 — `engines/suggestions.py::undo_tuning_log_entry` + `UndoResult`; `persistence/operations.py::get_tuning_log_entry`; CLI `tuning-log-undo` subcommand; backup-file timestamps now use microseconds; 7 tests in `tests/report_generator/test_tuning_log.py`
- **T-RG-COMPAT-RECENT-001** — Recent-tuning report section — `DONE` — 2026-05-16 — `engines/section_assemblers/recent_tuning.py`; ALL_SECTIONS ordering update; terminal + markdown renderers; HTML renderer (Step 3); 14 tests in `tests/report_generator/test_recent_tuning.py`
- **T-RG-COMPAT-HTML-001** — HTML render mode — `DONE` — 2026-05-16 — new module `renderer/html.py`; m7004 migration adding `rendered_html_text` + `html_path` columns to `report_log`; `models.py` `ReportRecord`/`ReportResult` extended; `engines/generator.py` rendering + persistence; CLI `--html PATH` flag; 16 tests in `tests/report_generator/test_html_renderer.py`

## Phase 14 — Compare + watch + at-a-glance (v0.45.0)

Three follow-on items resolving v0.44.0 candidate next-moves.

- **T-RG-COMPAT-COMPARE-001** — `report compare` CLI + engine — `DONE` — 2026-05-16 — new module `engines/compare.py` (`ReportDiff` + `compare_reports`); CLI `compare` subcommand with `--diff`/`--no-diff`/`--diff-lines` flags; 14 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-WATCH-001** — `report watch` CLI — `DONE` — 2026-05-16 — CLI `watch` subcommand with `--interval`/`--html`/`--markdown`/`--once`/`--max-cycles` flags; 9 tests in `tests/report_generator/test_watch.py`
- **T-RG-COMPAT-GLANCE-001** — At-a-glance section + extended editorial linter — `DONE` — 2026-05-16 — new module `engines/section_assemblers/at_a_glance.py`; ALL_SECTIONS gains the section at index 0; terminal/markdown/HTML renderers; nine new editorial phrases in `config/forbidden_phrases.yaml`; generator special-cases the section so it runs after the others and lifts their content; 17 tests in `tests/report_generator/test_at_a_glance.py`

## Phase 15 — Watch on-change + compare HTML + digest (v0.46.0)

Three follow-on items resolving v0.45.0 candidate next-moves.

- **T-RG-COMPAT-WATCH-CHANGE-001** — `report watch --on-change` skip-when-unchanged — `DONE` — 2026-05-16 — new module `engines/change_detection.py` (`UpstreamFingerprint` + `compute_upstream_fingerprint`); fingerprint covers latest scan_id / comparison_id / follow_up_id / tuning_log log_id; CLI `watch_cmd` extended with `--on-change` flag, skip logic, and skip count in exit summary; 9 tests in `tests/report_generator/test_watch.py` covering first-cycle run, skip when unchanged, run when changed, max-cycles total counting, and engine-level fingerprint behaviors
- **T-RG-COMPAT-COMPARE-HTML-001** — `report compare --html PATH` two-column view — `DONE` — 2026-05-16 — new module `engines/compare_html.py` (`render_compare_html`); CLI `compare_cmd` extended with `--html PATH` flag, linter pass, and parent-directory creation; self-contained HTML with inline CSS, dark/light prefers-color-scheme palette, semantic `class="changed"`/`"added"`/`"removed"` styling, and HTML-escape of operator-supplied content; 6 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-DIGEST-001** — `report digest [--days N]` recent-reports listing — `DONE` — 2026-05-16 — new CLI `digest_cmd` between `latest` and `watch`; uses existing `list_reports(conn, since=cutoff)` operation; one line per report (generated_at, report_id, sections=R/E, failed=F, terminal_chars=L, optional [md]/[html] markers); --days range [1, 365]; 9 tests in `tests/report_generator/test_digest.py`

## Phase 16 — Compare-HTML diff panel + digest aggregation + watch resume + ANSI translator (v0.47.0)

Four follow-on rendering/ergonomic items resolving v0.46.0
candidate next-moves. All purely additive — no schema changes,
no existing behavior altered.

- **T-RG-COMPAT-COMPARE-HTML-DIFF-001** — Compare-HTML unified-diff panel — `DONE` — 2026-05-16 — `engines/compare_html.py` `render_compare_html` accepts optional `diff_line_limit: int = 500`; emits a fourth section with each diff line as `<div class="diff-line ...">`; semantic classes: `diff-add`/`diff-del`/`diff-hunk`/`diff-meta`/`diff-context`; truncation footer (`diff-truncated`) names the count of dropped lines; benign empty message when terminal text is identical; CLI `compare_cmd` passes `--diff-lines` through; 4 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-DIGEST-AGG-001** — Digest aggregation header — `DONE` — 2026-05-16 — `cli.py` `digest_cmd` emits aggregate stats above the per-row listing: total report count, cycles-with-failures count, cycles-with-markdown count, cycles-with-html count, average sections rendered (one decimal), average terminal-text length (rounded); 3 tests in `tests/report_generator/test_digest.py`
- **T-RG-COMPAT-WATCH-CHANGE-RESUME-001** — Watch on-change resume summary — `DONE` — 2026-05-16 — `cli.py` `watch_cmd` tracks `consecutive_skips`; when the loop transitions skip→run, the log line includes a parenthesized note `(resume after N skipped: <fields> changed)`; new helper `_diff_fingerprint_fields(prior, current)` enumerates which of the four fingerprint fields differ with short labels (`scan`/`comparison`/`follow_up`/`tuning_log`); 3 tests in `tests/report_generator/test_watch.py`
- **T-RG-COMPAT-ANSI-001** — ANSI SGR → HTML translator — `DONE` — 2026-05-16 — new module `engines/ansi_to_html.py` with `strip_ansi`, `ansi_to_html`, `ANSI_INLINE_CSS`; supports 8 standard + 8 bright foreground colors + bold/dim/italic/underline; HTML-escapes underlying text; well-nested span output; reset codes 0/39 close open spans correctly; unsupported SGR codes silently dropped; `engines/compare_html.py` routes side-by-side panel text through `ansi_to_html` and embeds the inline CSS palette; 24 tests in `tests/report_generator/test_ansi_to_html.py`

## Phase 17 — Word-level diff + digest --json/--since + watch summary block (v0.48.0)

Four follow-on rendering/ergonomic items resolving v0.47.0
candidate next-moves. All purely additive — no schema changes,
no existing behavior altered.

- **T-RG-COMPAT-COMPARE-HTML-WORD-001** — Compare-HTML word-level diff — `DONE` — 2026-05-16 — `engines/compare_html.py` `_render_unified_diff` rewritten to call `_render_diff_rows_with_word_highlights`; new `_word_level_highlights` helper tokenizes each (del, add) pair via `re.findall(r"\w+|\W+", body)` and runs `difflib.SequenceMatcher.get_opcodes()`; replaced/inserted/deleted runs wrapped in `<span class="word-del">` / `<span class="word-add">`; equal-length adjacent runs paired element-wise; unequal-length runs fall back to whole-line styling; new CSS rules use `color-mix(in srgb, ...)` for dark/light palette derivation; 5 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-DIGEST-JSON-001** — Digest --json output — `DONE` — 2026-05-16 — `cli.py` `digest_cmd` extended with `--json` flag; new `_emit_digest_json` helper emits one `{"kind": "report", ...}` object per line in newest-first order followed by a single `{"kind": "aggregate", ...}` object; jsonlines convention so each line parses standalone; empty windows emit aggregate-only output with null averages; 3 tests in `tests/report_generator/test_digest.py`
- **T-RG-COMPAT-WATCH-SUMMARY-001** — Watch-loop exit summary block — `DONE` — 2026-05-16 — `cli.py` `watch_cmd` extended with per-cycle duration tracking (`time.monotonic`), failed-cycle count, and distinct-fingerprint-fields set; new helper `_emit_watch_exit_summary` emits a multi-line summary block with avg cycle duration, cycles failed, fingerprint fields changed during loop, and total skip time; conditional lines for happy-path brevity; one ASCII compatibility fix (× → x to satisfy ruff RUF001); 5 tests in `tests/report_generator/test_watch.py`
- **T-RG-COMPAT-DIGEST-SINCE-001** — Digest --since window override — `DONE` — 2026-05-16 — `cli.py` `digest_cmd` extended with `--since ISO` option mutually exclusive with `--days`; naive timestamps interpreted as UTC; window label in both terminal output and JSON aggregate adapts (`"since 2026-05-14T00:00:00+00:00"`); mutually-exclusive validation runs before DB access; 5 tests in `tests/report_generator/test_digest.py`

## Phase 18 — Compare toggles + watch summary file + digest prefix filter (v0.49.0)

Four follow-on rendering/ergonomic items resolving v0.48.0
candidate next-moves. All purely additive — no schema changes,
no existing default behavior altered.

- **T-RG-COMPAT-COMPARE-HTML-NO-WORD-001** — Compare --no-word-diff toggle — `DONE` — 2026-05-16 — `engines/compare_html.py` `render_compare_html` accepts `word_diff: bool = True`; threaded through `_render_unified_diff` and `_render_diff_rows_with_word_highlights`; CLI `compare_cmd` exposes `--word-diff/--no-word-diff` flag (default --word-diff); 3 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-WATCH-SUMMARY-FILE-001** — Watch --summary-file flag — `DONE` — 2026-05-16 — `cli.py` `watch_cmd` accepts `--summary-file PATH`; `_emit_watch_exit_summary` buffers lines locally and dispatches on suffix (.json → single `{"kind": "watch_summary", ...}` JSON object; otherwise plain-text matching stdout format); parent directories created on demand; 4 tests in `tests/report_generator/test_watch.py`
- **T-RG-COMPAT-DIGEST-PREFIX-001** — Digest --report-id PREFIX filter — `DONE` — 2026-05-16 — `cli.py` `digest_cmd` accepts `--report-id PREFIX`; `str.startswith` filter applied after `list_reports`; combines cleanly with `--days`/`--since`/`--json`; populated header and empty message reflect the prefix; `_emit_digest_json` aggregate object gains `report_id_prefix` field (string or null); 6 tests in `tests/report_generator/test_digest.py`
- **T-RG-COMPAT-COMPARE-HTML-NO-SBS-001** — Compare --no-side-by-side toggle — `DONE` — 2026-05-16 — `engines/compare_html.py` `render_compare_html` accepts `side_by_side: bool = True`; when False, the two-column terminal-text panel is suppressed; CLI `compare_cmd` exposes `--side-by-side/--no-side-by-side` flag (default --side-by-side); pairs with --no-word-diff for the most compact view; 3 tests in `tests/report_generator/test_compare.py`

## Phase 19 — Watch summary rotation + digest sort + compare anchors + compare-latest (v0.50.0)

Four follow-on rendering/ergonomic items resolving v0.49.0
candidate next-moves. All purely additive — no schema changes,
no existing default behavior altered.

- **T-RG-COMPAT-WATCH-SUMMARY-ROTATE-001** — Watch --summary-file `{timestamp}` placeholder — `DONE` — 2026-05-16 — `cli.py` new helper `_resolve_summary_path(path)` substitutes `{timestamp}` with UTC ISO timestamp (colons replaced with hyphens for filesystem safety); paths without the placeholder pass through unchanged; CLI emits `summary written to: <resolved>` only when the path was rewritten; JSON suffix dispatch preserved; 4 tests in `tests/report_generator/test_watch.py`
- **T-RG-COMPAT-DIGEST-SORT-001** — Digest --sort-by/--sort-direction — `DONE` — 2026-05-16 — `cli.py` `digest_cmd` accepts `--sort-by FIELD` (`click.Choice` over generated_at / sections_failed / terminal_chars) and `--sort-direction {asc,desc}`; defaults preserve newest-first ordering; new helper `_sort_digest_reports` applies primary sort with secondary sort on `generated_at desc` for tie-breaking; sort applies to JSON output too; 6 tests in `tests/report_generator/test_digest.py`
- **T-RG-COMPAT-COMPARE-HTML-ANCHORS-001** — Compare-HTML deep-link anchors — `DONE` — 2026-05-16 — `engines/compare_html.py` adds `id="metadata"` / `id="sections"` / `id="side-by-side"` / `id="unified-diff"` to each `<section>`; new `<nav class="quick-jump muted">` block in header lists `href="#..."` links; nav omits the side-by-side anchor when `--no-side-by-side` is set; new `.quick-jump` CSS rules with dark/light palette consistency; 3 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-COMPARE-LATEST-001** — `report compare-latest` shortcut — `DONE` — 2026-05-16 — new CLI subcommand resolves the two newest report ids via `list_reports(conn, limit=2)` and forwards rendering flags to `compare_cmd` via `ctx.invoke`; same flag set as `report compare` (`--diff/--no-diff`, `--diff-lines`, `--html`, `--word-diff/--no-word-diff`, `--side-by-side/--no-side-by-side`, `--db`); pre-flight check refuses on fewer than 2 reports; echoes `comparing latest pair: a=<id>  b=<id>` before forwarding; 5 tests in `tests/report_generator/test_compare.py`

## Phase 20 — Compare nav toggle + compare-latest offset + watch retention + digest top (v0.51.0)

Four follow-on rendering/ergonomic items resolving v0.50.0
candidate next-moves. All purely additive — no schema changes,
no existing default behavior altered.

- **T-RG-COMPAT-COMPARE-HTML-NO-QJ-001** — Compare --no-quick-jump toggle — `DONE` — 2026-05-16 — `engines/compare_html.py` `render_compare_html` accepts `quick_jump: bool = True` (third toggle keyword); `_render_header` honors the flag by suppressing the `<nav class="quick-jump muted">` block; section ids preserved so deep linking still works; CLI `compare_cmd` and `compare_latest_cmd` expose `--quick-jump/--no-quick-jump`; 3 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-COMPARE-LATEST-OFFSET-001** — `report compare-latest --offset N` — `DONE` — 2026-05-16 — `cli.py` `compare_latest_cmd` accepts `--offset N` (default 0); store query becomes `list_reports(conn, limit=offset + 2)`; pre-flight check refuses with `Need at least {offset + 2} reports for compare-latest --offset {N}; found M.`; negative offsets rejected; flag combines with --html; 4 tests in `tests/report_generator/test_compare.py`
- **T-RG-COMPAT-WATCH-SUMMARY-RETAIN-001** — Watch --summary-retention DAYS — `DONE` — 2026-05-16 — `cli.py` `watch_cmd` accepts `--summary-retention DAYS`; range [1, 365]; requires `--summary-file` with `{timestamp}` placeholder; new helper `_prune_old_summaries(template, retention_days, keep_path)` matches files via the template's filename glob and prunes by mtime; never prunes the just-written file; pruning announced on stdout; errors during unlink logged via `logger.exception`; 6 tests in `tests/report_generator/test_watch.py`
- **T-RG-COMPAT-DIGEST-TOP-001** — Digest --top N — `DONE` — 2026-05-16 — `cli.py` `digest_cmd` accepts `--top N` with range [1, 1000]; slice applied after sorting; aggregate header still over the full unsliced window; terminal output shows `showing top X of Y` indicator when slice in effect; `_emit_digest_json` accepts `full_reports` param so the aggregate is computed correctly; aggregate gains `top_n` and `top_n_emitted` fields; 6 tests in `tests/report_generator/test_digest.py`

## References

- Requirements: `REPORT_GENERATOR.md` v0.1.0
- Design: `REPORT_GENERATOR_DESIGN.md` v0.1.0
- Supplement: `REPORT_GENERATOR_SUPPLEMENT_MULTIVENUE.md` v0.1.0
- LOOM: `razorrooster.md`
