"""Smoke tests for the calibration-backtest GUI router (T-CB-035).

T-CB-036 / T-CB-037 fill in the route bodies; these smoke tests verify
the router stays registered and degrades cleanly when the seed store
carries no calibration-backtest rows. The list view renders the empty
placeholder; the detail view 404s on an unknown run id.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_route_smoke(client: TestClient) -> None:
    """GET /calibration-backtest renders the list view against an empty seed."""
    response = client.get("/calibration-backtest")
    assert response.status_code == 200


def test_detail_route_smoke(client: TestClient) -> None:
    """GET /calibration-backtest/{unknown_id} 404s with a structured detail."""
    response = client.get("/calibration-backtest/run-stub-123")
    assert response.status_code == 404
