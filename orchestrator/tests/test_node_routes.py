"""WebSocket tests for the node-connect endpoint (register -> ack flow)."""

import pytest
from fastapi.testclient import TestClient

import app.services.node_manager as nm
from app.main import app
from app.models.protocol import (
    GPUInfo,
    HeartbeatMessage,
    GPUMetrics,
    RegisterMessage,
    parse_message,
    serialize,
)
from tests.fakes import FakeSupabase


@pytest.fixture
def ctx(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    nm.node_manager.connected_nodes.clear()
    yield db, user
    nm.node_manager.connected_nodes.clear()


def _register_msg(provider_id):
    return RegisterMessage(
        provider_id=provider_id,
        node_secret="secret",
        version="0.1.0",
        gpu_info=GPUInfo(model="RTX 4090", vram_total_mb=24576),
        models_supported=["qwen-2.5-7b"],
        max_concurrent_jobs=2,
    )


def test_register_accepted(ctx):
    db, user = ctx
    client = TestClient(app)
    with client.websocket_connect("/v1/node/connect") as ws:
        ws.send_text(serialize(_register_msg(user["id"])))
        ack = parse_message(ws.receive_text())
        assert ack.type == "register_ack"
        assert ack.accepted is True
        assert ack.node_id
        # Node is now tracked in the manager.
        assert ack.node_id in nm.node_manager.connected_nodes

        # A heartbeat should be accepted without error.
        ws.send_text(
            serialize(
                HeartbeatMessage(status="busy", current_jobs=1, gpu_metrics=GPUMetrics())
            )
        )


def test_register_rejected_unknown_provider(ctx):
    db, user = ctx
    client = TestClient(app)
    with client.websocket_connect("/v1/node/connect") as ws:
        ws.send_text(serialize(_register_msg("unknown-provider")))
        ack = parse_message(ws.receive_text())
        assert ack.type == "register_ack"
        assert ack.accepted is False
        assert "provider" in (ack.reason or "").lower()


def test_register_rejected_invalid_secret(ctx):
    db, user = ctx
    client = TestClient(app)
    msg = _register_msg(user["id"])
    msg.node_secret = "wrong-secret"
    with client.websocket_connect("/v1/node/connect") as ws:
        ws.send_text(serialize(msg))
        ack = parse_message(ws.receive_text())
        assert ack.type == "register_ack"
        assert ack.accepted is False
        assert "secret" in (ack.reason or "").lower()


def test_first_message_must_be_register(ctx):
    db, user = ctx
    client = TestClient(app)
    with client.websocket_connect("/v1/node/connect") as ws:
        ws.send_text(
            serialize(
                HeartbeatMessage(status="ready", current_jobs=0, gpu_metrics=GPUMetrics())
            )
        )
        ack = parse_message(ws.receive_text())
        assert ack.accepted is False
        assert "register" in (ack.reason or "").lower()
