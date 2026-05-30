"""HTTP client layer for Kalshi public REST endpoints (T-KSI-030..T-KSI-033).

- ``rate_limit`` — token-bucket limiter with per-endpoint cost map and
  tier-aware bucket sizing.
- ``retry`` — exponential backoff with jitter. Kalshi 429 responses do
  not include ``Retry-After``, so the helper relies entirely on its
  own backoff schedule.
- ``user_agent`` — User-Agent header construction (NFR-KSI-TOS-001).
- ``endpoint_costs`` — operator-curated per-endpoint token cost map.
- ``rest`` — typed REST client for ``/series``, ``/events``,
  ``/markets``, ``/markets/{ticker}/orderbook``, ``/markets/trades``,
  ``/historical/cutoff``, ``/historical/markets``, and
  ``/historical/trades``. No authenticated endpoints.
"""
