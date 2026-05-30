"""``monitor`` — active-observation layer for watched analyses (The Comb).

v1 scope (MONITOR.md): daily cycle that evaluates every watched and
acted-on ``position_engine`` analysis against current upstream state,
classifies change across four dimensions (model probability, market
probability, precursor variables, time decay), evaluates each
analysis's invalidation criteria, detects market resolution, and
surfaces ranked alerts to ``report_generator``.

The monitor does not recompute analyses (that is ``position_engine``'s
job), does not change watch states automatically beyond
resolution-triggered expiration, and does not place orders. It
observes and surfaces.
"""
