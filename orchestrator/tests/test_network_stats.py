"""Tests for GET /v1/network/stats (public network dashboard feed)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import get_supabase
from app.main import app
from app.services import network_stats_service
from app.services.node_manager import node_manager
from tests.fakes import FakeSupabase


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def ctx():
    db = FakeSupabase()
    app.dependency_overrides[get_supabase] = lambda: db
    network_stats_service.reset_cache()
    client = TestClient(app)
    yield client, db
    app.dependency_overrides.clear()
    network_stats_service.reset_cache()
    node_manager.connected_nodes.clear()


def _seed(db: FakeSupabase) -> None:
    provider = db.add_user(is_provider=True, staked_orvx=50000.0)
    db.add_user(is_provider=False, staked_orvx=0.0)

    db._table("nodes").insert_row(
        {
            "provider_id": provider["id"],
            "status": "ready",
            "gpu_model": "RTX 4090",
            "engines": ["chat", "image"],
            "vram_gb": 24.0,
        }
    )
    db._table("nodes").insert_row(
        {
            "provider_id": provider["id"],
            "status": "busy",
            "gpu_model": "RTX 4090",
            "engines": ["chat"],
            "vram_gb": 24.0,
        }
    )
    db._table("nodes").insert_row(
        {
            "provider_id": provider["id"],
            "status": "offline",
            "gpu_model": "A100",
            "engines": ["chat"],
            "vram_gb": 80.0,
        }
    )

    # Two real jobs inside the window, one outside it, one mock, one failed.
    db._table("jobs").insert_row(
        {
            "user_id": provider["id"],
            "status": "completed",
            "is_mock": False,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "latency_ms": 800,
            "created_at": _iso(1),
        }
    )
    db._table("jobs").insert_row(
        {
            "user_id": provider["id"],
            "status": "completed",
            "is_mock": False,
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "latency_ms": 1200,
            "created_at": _iso(2),
        }
    )
    db._table("jobs").insert_row(
        {
            "user_id": provider["id"],
            "status": "completed",
            "is_mock": False,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "latency_ms": 400,
            "created_at": _iso(72),
        }
    )
    db._table("jobs").insert_row(
        {
            "user_id": provider["id"],
            "status": "completed",
            "is_mock": True,
            "prompt_tokens": 999,
            "completion_tokens": 999,
            "latency_ms": 10,
            "created_at": _iso(1),
        }
    )
    db._table("jobs").insert_row(
        {
            "user_id": provider["id"],
            "status": "failed",
            "is_mock": False,
            "prompt_tokens": 777,
            "completion_tokens": 0,
            "created_at": _iso(1),
        }
    )

    db._table("image_jobs").insert_row({"model": "flux-schnell", "created_at": _iso(3)})
    db._table("image_jobs").insert_row({"model": "flux-schnell", "created_at": _iso(100)})


def test_stats_is_public_and_shaped(ctx):
    client, db = ctx
    _seed(db)

    resp = client.get("/v1/network/stats")  # no auth header
    assert resp.status_code == 200
    body = resp.json()

    assert body["window_hours"] == 24
    assert set(body) >= {"nodes", "gpus", "providers", "chat", "images", "models"}
    assert body["generated_at"]


def test_node_and_provider_counts(ctx):
    client, db = ctx
    _seed(db)

    nodes = client.get("/v1/network/stats").json()["nodes"]
    assert nodes["registered"] == 3
    assert nodes["ready"] == 1
    assert nodes["busy"] == 1
    assert nodes["offline"] == 1
    assert nodes["chat_capable"] == 3
    assert nodes["image_capable"] == 1
    assert float(nodes["total_vram_gb"]) == 128.0

    providers = client.get("/v1/network/stats").json()["providers"]
    assert providers["total"] == 1
    assert providers["staked"] == 1


def test_gpu_breakdown_is_ranked(ctx):
    client, db = ctx
    _seed(db)

    gpus = client.get("/v1/network/stats").json()["gpus"]
    assert gpus == [{"gpu_model": "RTX 4090", "count": 2}, {"gpu_model": "A100", "count": 1}]


def test_mock_and_failed_jobs_are_excluded(ctx):
    client, db = ctx
    _seed(db)

    chat = client.get("/v1/network/stats").json()["chat"]
    # 3 completed real jobs total, 2 of them inside the 24h window.
    assert chat["requests_total"] == 3
    assert chat["requests_window"] == 2
    assert chat["tokens_total"] == 465  # 150 + 300 + 15
    assert chat["tokens_window"] == 450  # 150 + 300
    assert chat["avg_latency_ms"] == 1000  # (800 + 1200) / 2


def test_image_and_model_counts(ctx):
    client, db = ctx
    _seed(db)

    body = client.get("/v1/network/stats").json()
    assert body["images"] == {"generated_total": 2, "generated_window": 1}
    assert body["models"]["chat"] >= 1
    assert body["models"]["image"] >= 1


def test_empty_network_returns_zeros(ctx):
    client, _ = ctx

    body = client.get("/v1/network/stats").json()
    assert body["nodes"]["registered"] == 0
    assert body["nodes"]["online"] == 0
    assert body["chat"]["requests_total"] == 0
    assert body["chat"]["avg_latency_ms"] is None
    assert body["gpus"] == []


def test_online_count_tracks_live_connections(ctx):
    client, db = ctx
    _seed(db)

    assert client.get("/v1/network/stats").json()["nodes"]["online"] == 0

    node_manager.connected_nodes["node-a"] = object()
    # Served from cache, but the live count must still update.
    assert client.get("/v1/network/stats").json()["nodes"]["online"] == 1

    node_manager.connected_nodes.clear()
    assert client.get("/v1/network/stats").json()["nodes"]["online"] == 0


def test_snapshot_is_cached(ctx, monkeypatch):
    client, db = ctx
    _seed(db)

    calls = {"n": 0}
    real_rpc = db.rpc

    def counting_rpc(fn, params):
        if fn == "network_stats":
            calls["n"] += 1
        return real_rpc(fn, params)

    monkeypatch.setattr(db, "rpc", counting_rpc)

    client.get("/v1/network/stats")
    client.get("/v1/network/stats")
    assert calls["n"] == 1

    network_stats_service.reset_cache()
    client.get("/v1/network/stats")
    assert calls["n"] == 2
