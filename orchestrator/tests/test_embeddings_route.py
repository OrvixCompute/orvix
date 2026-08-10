"""Tests for POST /v1/embeddings."""

import base64
import struct

import pytest
from fastapi.testclient import TestClient

from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.main import app
from app.models.protocol import JobResultMessage
from app.services.node_manager import NodeTimeoutError, node_manager
from tests.fakes import FakeSupabase

DIM = 4


class _FakeNode:
    def __init__(self, node_id="node-1"):
        self.node_id = node_id
        self.status = "ready"
        self.engines = ["chat", "embedding"]
        self.models_supported = ["qwen-2.5-7b", "orvix-embed-1"]
        self.current_jobs = 0
        self.max_concurrent_jobs = 4


@pytest.fixture
def ctx(monkeypatch):
    db = FakeSupabase()
    user = db.add_user(tier="bronze")
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_user_from_api_key] = lambda: {
        "user": user,
        "api_key": {"id": "key-1"},
    }
    client = TestClient(app)
    yield client, db, user, monkeypatch
    app.dependency_overrides.clear()
    node_manager.connected_nodes.clear()


def _serve(monkeypatch, vectors, node=None, status="completed", error=None):
    node = node or _FakeNode()
    monkeypatch.setattr(node_manager, "select_embedding_node", lambda m: node)

    async def _dispatch(n, job):
        _dispatch.job = job
        return JobResultMessage(
            job_id=job.job_id,
            status=status,
            result={"embeddings": vectors} if vectors is not None else None,
            error=error,
        )

    monkeypatch.setattr(node_manager, "dispatch_embedding_job", _dispatch)
    return _dispatch


def test_single_string_input(ctx):
    client, _db, _user, monkeypatch = ctx
    _serve(monkeypatch, [[0.5] * DIM])

    r = client.post("/v1/embeddings", json={"model": "orvix-embed-1", "input": "hello"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["object"] == "list"
    assert b["model"] == "orvix-embed-1"
    assert len(b["data"]) == 1
    assert b["data"][0] == {"object": "embedding", "index": 0, "embedding": [0.5] * DIM}
    assert b["usage"]["total_tokens"] >= 1


def test_batch_preserves_order_and_indexes(ctx):
    """Index must follow input position — a vector store pairs them by it."""
    client, _db, _user, monkeypatch = ctx
    dispatch = _serve(monkeypatch, [[1.0] * DIM, [2.0] * DIM, [3.0] * DIM])

    r = client.post(
        "/v1/embeddings", json={"model": "orvix-embed-1", "input": ["a", "b", "c"]}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert [d["index"] for d in data] == [0, 1, 2]
    assert data[1]["embedding"] == [2.0] * DIM
    # The node received the inputs in the caller's order.
    assert dispatch.job.input == ["a", "b", "c"]


def test_base64_encoding_is_little_endian_float32(ctx):
    client, _db, _user, monkeypatch = ctx
    _serve(monkeypatch, [[1.0, 2.0, 3.0, 4.0]])

    r = client.post(
        "/v1/embeddings",
        json={"model": "orvix-embed-1", "input": "x", "encoding_format": "base64"},
    )
    assert r.status_code == 200
    raw = base64.b64decode(r.json()["data"][0]["embedding"])
    assert list(struct.unpack("<4f", raw)) == [1.0, 2.0, 3.0, 4.0]


def test_vector_count_mismatch_is_refused(ctx):
    """Fewer vectors than inputs would misalign every one of them, silently."""
    client, _db, _user, monkeypatch = ctx
    _serve(monkeypatch, [[1.0] * DIM])  # one vector for two inputs

    r = client.post("/v1/embeddings", json={"model": "orvix-embed-1", "input": ["a", "b"]})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "inference_failed"


def test_chat_model_is_refused(ctx):
    client, _db, _user, _mp = ctx
    r = client.post("/v1/embeddings", json={"model": "qwen-2.5-7b", "input": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "model_not_found"


def test_no_node_returns_no_embedding_provider(ctx):
    client, _db, _user, monkeypatch = ctx
    monkeypatch.setattr(node_manager, "select_embedding_node", lambda m: None)
    r = client.post("/v1/embeddings", json={"model": "orvix-embed-1", "input": "x"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "no_embedding_provider"


def test_busy_node_is_reported_as_retryable(ctx):
    """Busy and absent must not collapse into one code — only one is worth a retry."""
    client, _db, _user, monkeypatch = ctx
    busy = _FakeNode()
    busy.current_jobs = busy.max_concurrent_jobs  # present, but full
    node_manager.connected_nodes["n"] = busy
    monkeypatch.setattr(node_manager, "select_embedding_node", lambda m: None)

    r = client.post("/v1/embeddings", json={"model": "orvix-embed-1", "input": "x"})
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["code"] == "capacity_exhausted"
    assert body["retry_after_seconds"] > 0


def test_node_failure_surfaces_as_502(ctx):
    client, _db, _user, monkeypatch = ctx
    _serve(monkeypatch, None, status="failed", error="engine exploded")
    r = client.post("/v1/embeddings", json={"model": "orvix-embed-1", "input": "x"})
    assert r.status_code == 502
    assert "engine exploded" in r.json()["error"]["message"]


def test_node_timeout_surfaces_as_504(ctx):
    client, _db, _user, monkeypatch = ctx
    monkeypatch.setattr(node_manager, "select_embedding_node", lambda m: _FakeNode())

    async def _boom(n, job):
        raise NodeTimeoutError("too slow")

    monkeypatch.setattr(node_manager, "dispatch_embedding_job", _boom)
    r = client.post("/v1/embeddings", json={"model": "orvix-embed-1", "input": "x"})
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "node_timeout"


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "orvix-embed-1", "input": []},
        {"model": "orvix-embed-1", "input": [""]},
        {"model": "orvix-embed-1", "input": ["a"] * 257},
        {"model": "orvix-embed-1", "input": [[1, 2, 3]]},  # pre-tokenized
        {"model": "orvix-embed-1", "input": "x" * 8193},
    ],
)
def test_input_validation(ctx, payload):
    client, _db, _user, _mp = ctx
    r = client.post("/v1/embeddings", json=payload)
    assert r.status_code == 422, r.text


def test_embedding_model_is_in_the_public_catalog():
    """Clients size their vector store from this before ever calling."""
    client = TestClient(app)
    entries = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    assert "orvix-embed-1" in entries


def test_chat_and_image_models_are_untouched():
    """The catalog gained an entry; it must not have lost or changed one."""
    client = TestClient(app)
    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    for existing in ("qwen-2.5-7b", "mistral-7b", "llama-3.1-8b-quantized",
                     "flux-schnell", "orvix-image-1"):
        assert existing in ids
