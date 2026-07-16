"""Health-check endpoint tests."""

from fastapi.testclient import TestClient

from arufa.main import app


def test_health_returns_ok() -> None:
    """GET /health returns 200 with a simple status payload."""
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
