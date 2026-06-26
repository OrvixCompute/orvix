"""Test the governance snapshot-url endpoint (public, no auth)."""

from fastapi.testclient import TestClient

from app.main import app


def test_snapshot_url_is_public():
    client = TestClient(app)
    resp = client.get("/v1/governance/snapshot-url")
    assert resp.status_code == 200
    body = resp.json()
    assert "url" in body and "space" in body
    assert body["url"].startswith("http")
