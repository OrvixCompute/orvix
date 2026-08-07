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


def _connected(node_id: str, models: list[str], status: str = "ready"):
    from app.services.node_manager import NodeConnection

    return NodeConnection(
        node_id=node_id,
        provider_id="prov-1",
        websocket=None,
        model=models[0],
        gpu_info={},
        max_concurrent_jobs=4,
        status=status,
        models_supported=models,
        engines=["chat", "image"],
    )


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
    # Registration facts come from the database rows.
    assert nodes["registered"] == 3
    assert nodes["chat_capable"] == 3
    assert nodes["image_capable"] == 1
    assert float(nodes["total_vram_gb"]) == 128.0
    # Per-status counts describe live connections, and none of the seeded rows
    # is connected — so all three read as offline regardless of the status the
    # database last recorded for them.
    assert nodes["ready"] == 0
    assert nodes["busy"] == 0
    assert nodes["offline"] == 3

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

    node_manager.connected_nodes["node-a"] = _connected("node-a", ["qwen-2.5-7b"])
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



def test_model_counts_separate_catalog_from_actually_served(ctx):
    """The catalog advertises more than the network runs; say so.

    Reporting only the catalog size told the dashboard three chat models were
    on offer when one node served one of them, and a client picking any other
    got a 503 with no warning.
    """
    client, db = ctx
    _seed(db)
    node_manager.connected_nodes["n1"] = _connected("n1", ["qwen-2.5-7b", "orvix-image-1"])

    models = client.get("/v1/network/stats").json()["models"]
    assert models["chat"] >= 3 and models["image"] >= 2
    assert models["chat_available"] == 1
    assert models["image_available"] == 1


def test_node_status_counts_stay_consistent_with_online(ctx):
    """Live and cached fields must never contradict each other.

    Overlaying only `online` on a stale snapshot published online=1 next to
    ready=0 and offline=1 when a node reconnected inside the cache window.
    """
    client, db = ctx
    _seed(db)  # snapshot has 3 registered nodes, none connected

    first = client.get("/v1/network/stats").json()["nodes"]
    assert first["online"] == 0
    assert first["ready"] == 0
    assert first["offline"] == first["registered"]

    # Reconnect inside the cache window — the snapshot is stale on purpose.
    node_manager.connected_nodes["n1"] = _connected("n1", ["qwen-2.5-7b"], status="ready")
    nodes = client.get("/v1/network/stats").json()["nodes"]

    assert nodes["online"] == 1
    assert nodes["ready"] == 1
    assert nodes["offline"] == nodes["registered"] - 1
    assert nodes["ready"] + nodes["busy"] + nodes["draining"] == nodes["online"]


def test_models_endpoint_marks_what_is_actually_served(ctx):
    client, _db = ctx
    node_manager.connected_nodes["n1"] = _connected("n1", ["qwen-2.5-7b"])

    data = client.get("/v1/models").json()["data"]
    by_id = {m["id"]: m for m in data}

    assert by_id["qwen-2.5-7b"]["available"] is True
    assert by_id["mistral-7b"]["available"] is False
    # Still an OpenAI-shaped list; the hint is additive.
    assert by_id["qwen-2.5-7b"]["object"] == "model"
