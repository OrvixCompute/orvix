"""Tests for OrchestratorClient against an in-process mock WebSocket server."""

import asyncio

import pytest
import websockets

from orvix_node.client import OrchestratorClient
from orvix_node.config import NodeConfig
from orvix_node.exceptions import AuthError
from orvix_node.protocol import (
    JobMessage,
    RegisterAckMessage,
    parse_message,
    serialize,
)


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _cfg(port, **kw):
    return NodeConfig(
        provider_id="prov",
        node_secret="secret",
        orchestrator_url=f"ws://127.0.0.1:{port}",
        heartbeat_interval=kw.pop("heartbeat_interval", 1),
        **kw,
    )


async def _wait_until(predicate, timeout=5.0):
    async def loop():
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def test_register_success():
    async def handler(ws):
        reg = parse_message(await ws.recv())
        assert reg.type == "register"
        assert reg.provider_id == "prov"
        await ws.send(serialize(RegisterAckMessage(node_id="node-1", accepted=True)))
        async for _ in ws:  # keep the connection open
            pass

    server, port = await _serve(handler)
    client = OrchestratorClient(_cfg(port))
    task = asyncio.create_task(client.start())
    try:
        await _wait_until(lambda: client.is_connected)
        assert client.is_connected
    finally:
        await client.stop()
        task.cancel()
        server.close()
        await server.wait_closed()


async def test_register_rejected_raises_auth_error():
    async def handler(ws):
        await ws.recv()
        await ws.send(
            serialize(RegisterAckMessage(node_id="", accepted=False, reason="bad secret"))
        )
        await asyncio.sleep(0.1)

    server, port = await _serve(handler)
    client = OrchestratorClient(_cfg(port))
    try:
        with pytest.raises(AuthError):
            await asyncio.wait_for(client.start(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_heartbeats_sent_at_interval():
    heartbeats = []

    async def handler(ws):
        await ws.recv()
        await ws.send(serialize(RegisterAckMessage(node_id="n", accepted=True)))
        async for raw in ws:
            msg = parse_message(raw)
            if msg.type == "heartbeat":
                heartbeats.append(msg)

    server, port = await _serve(handler)
    # First heartbeat fires immediately, then every `heartbeat_interval` seconds.
    client = OrchestratorClient(_cfg(port, heartbeat_interval=1))
    task = asyncio.create_task(client.start())
    try:
        await _wait_until(lambda: len(heartbeats) >= 2, timeout=6)
        assert heartbeats[0].status in ("ready", "busy", "draining")
    finally:
        await client.stop()
        task.cancel()
        server.close()
        await server.wait_closed()


async def test_job_routed_to_handler():
    got_job = asyncio.Event()
    received = {}

    async def handler(ws):
        await ws.recv()
        await ws.send(serialize(RegisterAckMessage(node_id="n", accepted=True)))
        await ws.send(
            serialize(
                JobMessage(
                    job_id="job-xyz",
                    model="qwen-2.5-7b",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )
        )
        async for _ in ws:
            pass

    server, port = await _serve(handler)
    client = OrchestratorClient(_cfg(port))

    async def job_handler(job):
        received["job_id"] = job.job_id
        got_job.set()

    client.set_job_handler(job_handler)
    task = asyncio.create_task(client.start())
    try:
        await asyncio.wait_for(got_job.wait(), timeout=5)
        assert received["job_id"] == "job-xyz"
    finally:
        await client.stop()
        task.cancel()
        server.close()
        await server.wait_closed()
