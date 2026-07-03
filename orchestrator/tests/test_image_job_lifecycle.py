"""NodeManager image dispatch/correlation: complete, failure, and timeout."""

import asyncio

import pytest

from app.config import settings
from app.models.protocol import (
    ImageJobCompleteMessage,
    ImageJobDispatchMessage,
    ImageJobFailedMessage,
)
from app.services.node_manager import NodeConnection, NodeManager, NodeTimeoutError


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


def _conn() -> NodeConnection:
    return NodeConnection(
        node_id="n1",
        provider_id="prov",
        websocket=FakeWS(),
        model="flux-schnell",
        gpu_info={},
        max_concurrent_jobs=2,
        engines=["image"],
        models_supported=["flux-schnell"],
    )


def _dispatch() -> ImageJobDispatchMessage:
    return ImageJobDispatchMessage(
        job_id="j1", model="flux-schnell", prompt="x", binary_token="tok"
    )


async def test_dispatch_resolves_on_complete():
    mgr = NodeManager()
    conn = _conn()
    mgr.connected_nodes["n1"] = conn

    task = asyncio.create_task(mgr.dispatch_image_job(conn, _dispatch()))
    await asyncio.sleep(0.02)
    assert conn.current_jobs == 1  # in flight
    mgr.handle_image_result(
        "n1", ImageJobCompleteMessage(job_id="j1", image_id="i1", binary_url="u")
    )
    result = await asyncio.wait_for(task, timeout=1)
    assert result.image_id == "i1"
    assert conn.current_jobs == 0  # released


async def test_dispatch_raises_on_failure():
    mgr = NodeManager()
    conn = _conn()
    mgr.connected_nodes["n1"] = conn

    task = asyncio.create_task(mgr.dispatch_image_job(conn, _dispatch()))
    await asyncio.sleep(0.02)
    mgr.handle_image_result("n1", ImageJobFailedMessage(job_id="j1", error="boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(task, timeout=1)
    assert conn.current_jobs == 0


async def test_dispatch_times_out(monkeypatch):
    monkeypatch.setattr(settings, "IMAGE_JOB_TIMEOUT", 0.05)
    mgr = NodeManager()
    conn = _conn()
    mgr.connected_nodes["n1"] = conn
    with pytest.raises(NodeTimeoutError):
        await mgr.dispatch_image_job(conn, _dispatch())  # never resolved
    assert conn.current_jobs == 0
