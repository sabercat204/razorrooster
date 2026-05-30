"""``kalshi_connector`` — read-only Kalshi data access (The Stamp).

v1 scope (KALSHI_CONNECTOR.md): public market metadata, price snapshots,
settlement history, opt-in watched-market trades and on-demand
orderbooks. No authenticated endpoints, no RSA-PSS signing, no API key
loading, no WebSocket. Eligibility-allow-list and ToS-acknowledgement
gates are non-bypassable startup checks.

The connector is the second prediction-market venue in the system,
sibling to ``polymarket_connector``. It mirrors that subsystem's
structural decisions (gates → HTTP client → sync ops → mapping →
CLI → cycle integration → acceptance) but inverts the geo posture
(allow-list, US-only by default, since Kalshi is a CFTC-regulated US
DCM) and adds three Kalshi-specific concerns: per-endpoint token-cost
rate-limit math, live/historical cutoff routing for historical data,
and a ``venue`` discriminator that flows through every downstream
subsystem so a single class can be mapped to both Polymarket and
Kalshi simultaneously.
"""
