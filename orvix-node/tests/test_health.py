"""Tests for the node health server, including the new /v1/status endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orvix_node.health import create_health_app
from orvix_node.inference.manager import ModelManager


def test_health_endpoint_ok():
    client = TestClient(create_health_app())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_endpoint_reports_manager():
    # status() only inspects the engine registry keys, so placeholder values are
    # fine here — we're testing the endpoint shape, not engine behavior.
    mgr = ModelManager({"chat": object(), "image": object()})
    client = TestClient(create_health_app(mgr))
    r = client.get("/v1/status")
    assert r.status_code == 200
    data = r.json()
    assert "uptime_seconds" in data
    assert data["manager"]["current_engine"] is None
    assert sorted(data["manager"]["engines"]) == ["chat", "image"]


def test_status_endpoint_without_manager():
    client = TestClient(create_health_app())
    r = client.get("/v1/status")
    assert r.status_code == 200
    assert r.json()["manager"] is None
