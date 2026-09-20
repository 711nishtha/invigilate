"""Health and readiness probes."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert body["server_time"]


def test_readiness_reports_database(client: TestClient) -> None:
    response = client.get("/api/health/ready")
    assert response.status_code in (200, 503)
    assert response.json()["database"] in ("ok", "unavailable")


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_openapi_available_outside_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
