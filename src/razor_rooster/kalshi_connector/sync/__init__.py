"""Sync operations for Kalshi (T-KSI-040..T-KSI-046).

- ``cutoff`` — snapshot ``/historical/cutoff`` once per cycle for
  routing decisions (REQ-KSI-SETTLE-004).
- ``series`` — daily series enumeration.
- ``events`` — daily event reconciliation.
- ``markets`` — daily market metadata reconciliation.
- ``prices`` — 30-min snapshot cadence; binary markets only in v1.
- ``settlements`` — initial backfill + daily delta with live/historical
  cutoff routing per OQ-KSI-004.
- ``trades`` — opt-in per-watched-market trade pull, also routed
  across the cutoff.
- ``orderbook`` — on-demand depth fetch; YES side from API, NO side
  derived per design §3.3.
"""
