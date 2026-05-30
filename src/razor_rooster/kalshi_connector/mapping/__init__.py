"""Sector mapping for Kalshi markets (T-KSI-050..T-KSI-051).

- ``sector_heuristic`` — three-pass heuristic (category → keyword scan
  → tie-breaking) producing ``confidence='inferred'`` mappings or
  ``razor_sector=None`` for ambiguous inputs.
- ``sector_overrides`` — operator-curated ``confidence='manual'``
  rows persisted to ``kalshi_sector_mapping``.

Per OQ-KSI-001, the heuristic supports an ``out_of_scope`` enum value
for Sports / Entertainment / daily-life markets so they're persisted
faithfully but excluded from downstream processing.
"""
