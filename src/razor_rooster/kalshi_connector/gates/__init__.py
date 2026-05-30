"""Startup-time gates for the Kalshi connector (T-KSI-020..T-KSI-021).

- ``eligibility`` — REQ-KSI-ELIG-001 allow-list gate. Inverts the
  Polymarket geo-restriction pattern: refusal on jurisdiction NOT in
  ``config/kalshi_allowed_jurisdictions.yaml``. Reuses the same
  ``OPERATOR_JURISDICTION`` env var Polymarket consults.
- ``tos`` — REQ-KSI-TOS-001 ToS hash + posture gate. v1 acknowledgements
  carry ``acknowledged_posture='read_only'``. v2 trading posture is
  reserved but not exercised in v1.

Both gates fail closed: missing config refuses with a clear,
actionable error message naming the file the operator must edit.
"""
