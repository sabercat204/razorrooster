"""``position_engine`` — paper-analysis sizing layer (The Spur).

v1 scope (POSITION_ENGINE.md): for each surfaced
``mispricing_detector`` comparison, produce a *sizing analysis* —
a structured document with Kelly fractions, half-Kelly bounds,
expected-value figures, bankroll-survival diagnostics, and
invalidation criteria. The subsystem produces analyses, not
directives.

The position_engine is recommendation-only by design (per OT-004
v1 resolution): no order placement, no real-capital tracking, no
wallet integration. The threat context is STANDARD (downgraded
from FULL) for v1; if and when v2+ adds execution, those code
paths get FULL-context handling and a separate spec amendment.

Every analysis output uses conditional language ("if the operator
chose to act"). The renderer linter enforces this — it refuses to
ship output containing forbidden imperative phrases like "you
should buy" or "I recommend." See ``frame/linter.py`` and
``config/forbidden_phrases.yaml``.
"""
