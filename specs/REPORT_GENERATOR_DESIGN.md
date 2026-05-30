# REPORT_GENERATOR — Design

**Subsystem:** `report_generator`
**Codename:** The Crow
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** MINIMAL_EXPOSURE
**Last updated:** 2026-05-14
**Companion spec:** `REPORT_GENERATOR.md` (Requirements v0.1.0)

---

## 1. Overview

`report_generator` is the operator-facing surface. Architecturally it's the smallest subsystem: read upstream tables, render text via templates, lint, persist. The discipline that distinguishes it is the framing constraints — every section template enforces the educational disposition, and the imperative-language linter (shared with `position_engine`) blocks anything imperative from shipping.

Discipline rules:

1. **Source-native preservation** — reads but does not transform upstream artifacts.
2. **Failure isolation** — bad section data produces "section unavailable," not crash.
3. **No silent generation** — every report stamps version, freshness, completeness.
4. **Conditional language only** — same linter as `position_engine`.
5. **Local-only** — no network access at any point.

## 2. Resolved Open Questions

### OQ-RG-001 — Calibration verdict text

**Resolution:** Template-driven. Catalog of phrases keyed by `(predicted_p_band, observed_outcome)` pairs.

**Reasoning:** Free-form text invites the renderer (or its templates) to express more confidence than the data supports. A bounded catalog like:
- (predicted high, observed YES) → "Model said {p:.2f} → resolved YES; in line with predicted likelihood."
- (predicted high, observed NO) → "Model said {p:.2f} → resolved NO; this counts against the model's calibration."
- (predicted mid, observed YES) → "Model said {p:.2f} → resolved YES; consistent with mid-confidence prediction."
- (predicted mid, observed NO) → "Model said {p:.2f} → resolved NO; consistent with mid-confidence prediction."
- (predicted low, observed YES) → "Model said {p:.2f} → resolved YES; the model assigned low probability, but tail outcomes happen."
- (predicted low, observed NO) → "Model said {p:.2f} → resolved NO; in line with predicted likelihood."

**Design implications:**
- `templates/calibration_verdicts.yaml` keyed by band ranges (`high`: ≥0.7, `mid`: 0.3–0.7, `low`: <0.3) and outcome.

### OQ-RG-002 — Markdown export structure

**Resolution:** Hierarchical headers (`##` for sections, `###` for subsection blocks per analysis), code blocks for traces, horizontal rules between sections.

**Design implications:**
- Renderer has two modes: `terminal` (uses ASCII rules and basic indentation) and `markdown` (uses GitHub-Flavored Markdown).

### OQ-RG-003 — Length policy

**Resolution:** Render in full. No truncation. Operator can use `--quiet` to skip terminal output and read markdown file at their convenience.

### OQ-RG-004 — Calibration log lookback

**Resolution:** Since prior report timestamp by default. First run looks back 30 days. Configurable via `--since`.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      report_generator/
        __init__.py
        cli.py
        engines/
          __init__.py
          generator.py                     # main orchestration
          section_assemblers/
            __init__.py
            header.py
            system_health.py
            surfaced.py
            watched.py
            calibration.py
            watchlist.py
            footer.py
        renderer/
          __init__.py
          terminal.py                      # plain-text renderer
          markdown.py                      # markdown renderer
          shared.py                        # common helpers (disclaimer block, etc.)
        templates/
          calibration_verdicts.yaml
          disclaimer.txt
          section_headers.yaml
        persistence/
          __init__.py
          schemas.py
          migrations/
            m0001_report_generator_initial.py
        config/
          report.yaml
        tests/
          fixtures/

### 3.2 Reuse from Other Subsystems

- All upstream subsystem tables (read-only).
- The `position_engine` linter (`frame/linter.py`) and `forbidden_phrases.yaml` catalog — shared, not duplicated. Verifies in tests that the catalog file is the same on disk.
- The `data_ingest` `freshness` view for system health.

### 3.3 Tables

#### `report_log`

    report_id                     VARCHAR     PRIMARY KEY
    generated_at                  TIMESTAMP   NOT NULL
    since_ts                      TIMESTAMP   NOT NULL
    until_ts                      TIMESTAMP   NOT NULL
    sections_enabled              JSON        NOT NULL    -- list of section names
    sections_rendered             JSON        NOT NULL
    sections_failed               JSON        NOT NULL    -- list of {section, error}
    library_version               INTEGER     NOT NULL
    disclaimer_version_hash       VARCHAR     NOT NULL
    rendered_terminal_text        TEXT        NOT NULL
    rendered_markdown_text        TEXT        NULL          -- present when --markdown was used
    markdown_path                 VARCHAR     NULL          -- output path if written

Index: `(generated_at DESC)` for "latest report" queries.

### 3.4 Generator Orchestration

`engines/generator.py`:

    def generate(since=None, markdown_path=None, quiet=False) -> Report:
        report_id = uuid()
        config = load_config('report.yaml')
        if since is None:
            last = query_last_report()
            since = last.generated_at if last else (utcnow() - timedelta(days=1))
        until = utcnow()

        sections = []
        for section_name in config.enabled_sections:
            try:
                content = section_assemblers.assemble(section_name, since, until)
                sections.append((section_name, content, None))
            except Exception as e:
                sections.append((section_name, None, str(e)))

        terminal_text = renderer.terminal.render(sections, since, until, library_version)
        linter.check(terminal_text)  # raises if forbidden phrases

        markdown_text = None
        if markdown_path:
            markdown_text = renderer.markdown.render(sections, since, until, library_version)
            linter.check(markdown_text)
            write_file(markdown_path, markdown_text)

        if not quiet:
            print(terminal_text)

        persist_report(report_id, since, until, sections, terminal_text, markdown_text, markdown_path)
        return Report(...)

### 3.5 Section Assemblers

Each section assembler returns a structured content dict that the renderers convert to text.

**Header (`section_assemblers/header.py`)**:

    {
      "type": "header",
      "report_id": "...",
      "cycle_date": "2026-05-14",
      "library_version": 1,
      "freshness_summary": {"stale_sources": [...], "library_age_days": 3},
      "since_ts": "...",
    }

**System health**:

    {
      "type": "system_health",
      "stale_sources": [{"source_id": "noaa", "last_successful_fetch": "...", "days_stale": 4}, ...],
      "errored_subsystems": [{"subsystem": "polymarket_connector", "cycle_id": "...", "error_count": 1}, ...],
      "suppressed_breakdown": {"low_mapping_confidence": 8, "stale_market_price": 2, ...},
    }

**Surfaced comparisons**:

    {
      "type": "surfaced",
      "comparisons": [
        {
          "comparison_id": "...",
          "class": {...},
          "scan_trace": {...},
          "comparison_trace": {...},
          "analysis": {...} | None,
          "warnings": [...],
        },
        ...
      ],
    }

**Watched**, **calibration**, **watchlist**, **footer** follow the same pattern.

### 3.6 Renderers

#### Terminal renderer

`renderer/terminal.py` produces plain text with ASCII section dividers:

    ═══════════════════════════════════════════════════════════════════
    RAZOR-ROOSTER REPORT
    Cycle 2026-05-14 (since 2026-05-13 09:30 UTC)
    Library version 1
    ═══════════════════════════════════════════════════════════════════

    SYSTEM HEALTH
    ───────────────────────────────────────────────────────────────────
    Stale sources: noaa (4 days), gdelt_events (2 days)
    Library refresh age: 3 days
    Errored subsystems: polymarket_connector (1 cycle errored)

    Suppressed comparisons this cycle:
      low_mapping_confidence: 8
      stale_market_price: 2

    ═══════════════════════════════════════════════════════════════════

    SURFACED COMPARISONS
    ───────────────────────────────────────────────────────────────────

    [for each surfaced comparison, render block...]

    ═══════════════════════════════════════════════════════════════════

    [...subsequent sections...]

    ═══════════════════════════════════════════════════════════════════

    DISCLAIMER

    This report is decision-support analysis. The system surfaces patterns,
    comparisons, and analyses; it does not place trades, recommend specific
    actions, or claim certainty about future events. The operator is
    responsible for any decisions taken based on this report. Polymarket
    prices represent the aggregate view of market participants, who often
    have information the model does not. When model and market disagree, the
    market is correct more often than not.

    Razor-Rooster v0.1.0 — generated 2026-05-14T09:31:42Z
    ═══════════════════════════════════════════════════════════════════

For each surfaced comparison block, the layout enforces: warnings → comparison numbers → reasoning trace (with `case_for_model` and `case_for_market` adjacent and equal-prominence) → sizing analysis if available (with disclaimer block from `position_engine`) → invalidation criteria.

#### Markdown renderer

`renderer/markdown.py` uses `##` for sections, `###` for per-comparison subsections, code blocks for embedded traces, GFM tables for the calibration log:

    # Razor-Rooster Report — 2026-05-14

    ## System Health
    ...

    ## Surfaced Comparisons

    ### pheic_declaration_12mo (Public Health)
    ...

    | Class | Outcome | Predicted p | Days to Resolution | Verdict |
    |-------|---------|-------------|---------------------|---------|
    | ...   | YES     | 0.18        | 87                  | ...     |

### 3.7 Linter Integration

The shared linter from `position_engine.frame.linter` is imported and applied to both terminal and markdown outputs. If the linter rejects, the generator raises a typed error and no report is persisted (so the next run will re-attempt rather than skipping).

### 3.8 CLI

    razor-rooster report generate [--since <iso>] [--markdown <path>] [--quiet]
    razor-rooster report show <report_id>                # rendered text from DB
    razor-rooster report list [--since <iso>]            # list past reports
    razor-rooster report latest                          # show most recent

### 3.9 Threat Model

Threat context: MINIMAL_EXPOSURE.

Risks:
1. **Imperative language slipping in.** Mitigation: shared linter; report not persisted on failure.
2. **Section assembler errors crashing report.** Mitigation: per-section try/except; "section unavailable" placeholder.
3. **Network call leaking in.** Mitigation: code review checklist forbids `httpx`, `requests`, etc. imports anywhere in `report_generator/`. Run-time test verifies no network activity during a generate call.
4. **Markdown injection / file path traversal.** Mitigation: `markdown_path` validated to be a relative or absolute local path; rejected if URL-shaped.

## 4. Test Strategy

### 4.1 Unit Tests

- Per section assembler: synthetic upstream data → expected content dict.
- Terminal renderer: known content dict → expected text.
- Markdown renderer: known content dict → valid GFM.
- Linter integration: adversarial output rejected.
- Calibration verdict template selection per band.

### 4.2 Integration Tests

- Full generate against synthetic upstream system, all sections present. Output text contains expected fields, disclaimer, and equal-prominence "case for market" sections.
- Section failure isolation: synthetic missing data per section → placeholder.
- Empty-cycle case: no surfaced comparisons, no watched, no resolutions → "nothing to report" notes per section.
- Markdown export writes file and content matches stored `rendered_markdown_text`.

### 4.3 Acceptance Test

On operator hardware against real upstream system:
- Daily generation within NFR-RG-PERF-001.
- Output is readable on a normal terminal width without overflow issues.
- Markdown export renders cleanly in a markdown viewer.
- Network-disabled run completes successfully (NFR-RG-LOCAL-001).

## 5. Operational Model

### 5.1 First report

After all upstream subsystems have run their first cycle:

    razor-rooster report generate --markdown ~/Documents/razor-rooster-2026-05-14.md

### 5.2 Daily cadence

After `monitor` cycle: `razor-rooster report generate`. Operator reads at their own cadence.

### 5.3 Reviewing past reports

    razor-rooster report list --since 2026-05-01
    razor-rooster report show <report_id>
    razor-rooster report latest

### 5.4 Customizing sections

Edit `config/report.yaml` to disable sections the operator doesn't want.

## 6. Performance Notes

- Sub-minute on v1 scale. The dominant cost is rendering, not querying.

## 7. Deferred to Implementation

- **DEFER-RG-001:** Empirical "case for market" word-count parity validation — does the output truly look balanced? Adjust template if not.
- **DEFER-RG-002:** Calibration verdict catalog refinement — start with the seed list and revise as resolutions accumulate.

## 8. References

- Requirements: `REPORT_GENERATOR.md` v0.1.0
- All upstream subsystem specs.
- `position_engine` Design — for the linter and disclaimer block (shared).
- LOOM v0.10.0
- System prompt v0.2.
