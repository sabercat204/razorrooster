"""``mispricing_detector`` — model-vs-market comparison layer (The Liver).

v1 scope (MISPRICING_DETECTOR.md): for each ``signal_scanner``
candidate situation, find Polymarket markets in the same event class,
compute the model-vs-market delta with credible-interval-overlap
analysis, and emit a structured comparison record with reasoning trace.

The phrase "mispricing detector" — the legacy name from the LOOM —
should be read in the educational-framing sense: the subsystem detects
*disagreements between model and market*, not "the market is wrong here."
Treating the market as default-correct is one of the system's stated
principles. Comparisons surface evidence; the operator decides what to
do with it. The reasoning trace presents the case for both views at
equal prominence per REQ-MD-TRACE-005.
"""
