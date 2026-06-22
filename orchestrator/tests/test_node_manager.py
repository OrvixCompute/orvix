"""Tests for NodeManager: registration, selection, dispatch/correlation, timeout."""

import asyncio
from decimal import Decimal

import pytest

import app.services.node_manager as nm
from app.models.protocol import (
    GPUInfo,
    JobMessage,
    JobResultMessage,
    RegisterMessage,
)
from app.services.node_manager import NodeConnection, NodeManager, NodeTimeoutError
from tests.fakes import FakeSupabase


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(s)

    async def close(self):
        pass


def _conn(node_id, model="qwen-2.5-7b", current_jobs=0, max_jobs=4, provider="prov"):
    return NodeConnection(
        node_id=node_id,
        provider_id=provider,
        websocket=FakeWS(),
        model=model,
        gpu_info={},
        max_concurrent_jobs=max_jobs,
        current_jobs=current_jobs,
        status="ready",
        models_supported=[model],
    )


async def test_register_node(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    mgr = NodeManager()

    msg = RegisterMessage(
        provider_id=user["id"],
        node_secret="secret",
        version="0.1.0",
        gpu_info=GPUInfo(model="RTX 4090", vram_total_mb=24576),
        models_supported=["qwen-2.5-7b"],
        max_concurrent_jobs=2,
    )
    conn = await mgr.register_node(FakeWS(), msg)
    assert conn.node_id in mgr.connected_nodes
    assert db._table("nodes").rows[0]["provider_id"] == user["id"]
    assert db._table("nodes").rows[0]["status"] == "ready"


async def test_register_unknown_provider_raises(monkeypatch):
    db = FakeSupabase()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    mgr = NodeManager()
    msg = RegisterMessage(
        provider_id="does-not-exist",
        node_secret="secret",
        version="0.1.0",
        gpu_info=GPUInfo(model="X", vram_total_mb=1),
        models_supported=["qwen-2.5-7b"],
        max_concurrent_jobs=1,
    )
    with pytest.raises(ValueError):
        await mgr.register_node(FakeWS(), msg)


def test_select_single_and_no_match():
    mgr = NodeManager()
    c = _conn("n1")
    mgr.connected_nodes = {"n1": c}
    assert mgr.select_node("qwen-2.5-7b", "bronze") is c
    assert mgr.select_node("mistral-7b", "bronze") is None


def test_select_tier_priority_picks_least_loaded():
    mgr = NodeManager()
    busy = _conn("busy", current_jobs=3)
    idle = _conn("idle", current_jobs=0)
    mgr.connected_nodes = {"busy": busy, "idle": idle}
    # Priority tier gets the least-loaded node.
    assert mgr.select_node("qwen-2.5-7b", "gold").node_id == "idle"
    # Non-priority just gets an available one.
    assert mgr.select_node("qwen-2.5-7b", "bronze") in (busy, idle)


def test_select_excludes_full_nodes():
    mgr = NodeManager()
    full = _conn("full", current_jobs=4, max_jobs=4)
    mgr.connected_nodes = {"full": full}
    assert mgr.select_node("qwen-2.5-7b", "bronze") is None


async def test_dispatch_blocking_correlates_result():
    mgr = NodeManager()
    conn = _conn("n1")
    mgr.connected_nodes = {"n1": conn}
    job = JobMessage(job_id="j1", model="qwen-2.5-7b", messages=[{"role": "user", "content": "hi"}])

    task = asyncio.create_task(mgr.dispatch_job(conn, job))
    await asyncio.sleep(0.02)
    assert any('"type":"job"' in s for s in conn.websocket.sent)
    assert conn.current_jobs == 1  # incremented during dispatch

    mgr.handle_job_result(
        "n1",
        JobResultMessage(
            job_id="j1", status="completed", result={"ok": True},
            prompt_tokens=5, completion_tokens=10,
        ),
    )
    res = await task
    assert res.status == "completed"
    assert res.completion_tokens == 10
    assert conn.current_jobs == 0  # released


async def test_dispatch_times_out(monkeypatch):
    monkeypatch.setattr(nm, "JOB_TIMEOUT_S", 0.1)
    mgr = NodeManager()
    conn = _conn("n1")
    mgr.connected_nodes = {"n1": conn}
    job = JobMessage(job_id="j1", model="qwen-2.5-7b", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(NodeTimeoutError):
        await mgr.dispatch_job(conn, job)
    assert conn.current_jobs == 0  # released even on timeout


async def test_streaming_dispatch_yields_chunks():
    mgr = NodeManager()
    conn = _conn("n1")
    mgr.connected_nodes = {"n1": conn}
    job = JobMessage(
        job_id="js", model="qwen-2.5-7b",
        messages=[{"role": "user", "content": "hi"}], stream=True,
    )
    gen = await mgr.dispatch_job(conn, job)

    async def feed():
        await asyncio.sleep(0.02)
        from app.models.protocol import JobChunkMessage

        mgr.handle_job_chunk("n1", JobChunkMessage(job_id="js", chunk={"a": 1}, is_final=False))
        mgr.handle_job_chunk("n1", JobChunkMessage(job_id="js", chunk={"b": 2}, is_final=True))

    asyncio.create_task(feed())
    chunks = [c async for c in gen]
    assert len(chunks) == 2
    assert chunks[-1].is_final is True


async def test_settle_job_credits_provider(monkeypatch):
    db = FakeSupabase()
    user = db.add_user(available_usdc=0.0, lifetime_earnings_usdc=0.0)
    db._table("nodes").insert_row(
        {"id": "n1", "provider_id": user["id"], "total_earned_usdc": 0, "total_jobs": 0}
    )
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    mgr = NodeManager()
    conn = _conn("n1", provider=user["id"])

    earning = await mgr.settle_job(conn, Decimal("1.0"))
    assert earning == Decimal("0.700000000")  # 70% reward
    assert float(db._table("users").rows[0]["available_usdc"]) == pytest.approx(0.7)
    assert db._table("nodes").rows[0]["total_jobs"] == 1
