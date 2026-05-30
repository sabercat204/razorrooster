"""Report generator (T-RG-001+; design §1).

The Crow — operator-facing report renderer. Reads upstream subsystem
outputs since the previous report and assembles a structured
document with strict framing constraints (no imperative language,
educational disposition, equal-prominence "case for market"
sections, standard disclaimer block).

Discipline rules:

1. Source-native preservation — read upstream artifacts as-is.
2. Failure isolation — bad section data renders "section
   unavailable", not crash.
3. No silent generation — every report stamps version, freshness,
   completeness.
4. Conditional language only — shared linter with position_engine.
5. Local-only — no network access at any point.

See ``specs/REPORT_GENERATOR.md`` and ``specs/REPORT_GENERATOR_DESIGN.md``
for the full requirement and design v0.1.0.
"""
