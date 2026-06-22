"""In-process end-to-end check with NO real database.

Patches the orchestrator's Supabase access with the test FakeSupabase, runs the
real ASGI app under uvicorn on a real port, connects the REAL node software over
a real WebSocket, and sends an inference request — verifying it routes to the
node (X-Orvix-Node = node uuid, not "mock").
"""

import asyncio
import os
import sys

# Make the orchestrator's tests.fakes importable and the node package available.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SUPABASE_URL", "https://test.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "k")
os.environ.setdefault("JWT_SECRET", "s")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("ORVIX_NODE_STUB_GPU", "true")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from tests.fakes import FakeSupabase  # noqa: E402

# One shared fake DB for the whole app.
DB = FakeSupabase()
PROVIDER = DB.add_user(tier="gold", balance_usdc=1000.0)
# Suffix after "orvx_sk_" must be exactly 32 chars (matches the auth regex).
API_KEY_PLAIN = "orvx_sk_e2ekeye2ekeye2ekeye2ekeye2ekeyAB"

import hashlib  # noqa: E402

DB._table("api_keys").insert_row(
    {
        "id": "key-e2e",
        "user_id": PROVIDER["id"],
        "key_hash": hashlib.sha256(API_KEY_PLAIN.encode()).hexdigest(),
        "key_prefix": API_KEY_PLAIN[:12],
        "name": "e2e",
        "is_active": True,
    }
)

# Patch direct get_supabase() callers (not dependency-injected ones).
import app.dependencies as deps  # noqa: E402
import app.services.node_manager as nm  # noqa: E402

nm.get_supabase = lambda: DB
deps.get_supabase = lambda: DB  # used by the last_used_at background task

from app.database import get_supabase  # noqa: E402
from app.main import app  # noqa: E402

# Dependency-injected get_supabase (Depends) — override for every route.
app.dependency_overrides[get_supabase] = lambda: DB

PORT = 8123


async def run_node(provider_id: str):
    from orvix_node.client import OrchestratorClient
    from orvix_node.config import NodeConfig
    from orvix_node.executor import JobExecutor
    from orvix_node.inference.mock import MockBackend

    cfg = NodeConfig(
        provider_id=provider_id,
        node_secret="secret",
        orchestrator_url=f"ws://127.0.0.1:{PORT}",
        model="qwen-2.5-7b",
        heartbeat_interval=2,
        health_port=9123,
    )
    executor = JobExecutor(MockBackend(provider_id), max_concurrent=2)
    await executor.initialize(cfg.model)
    client = OrchestratorClient(cfg)
    client.set_job_handler(
        lambda job: executor.execute(
            job, send_chunk=client.send_message, send_result=client.send_message
        )
    )
    task = asyncio.create_task(client.start())
    return client, task


async def main() -> int:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for the server to come up.
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)

    client, node_task = await run_node(PROVIDER["id"])

    # Wait until the node is registered with the orchestrator.
    for _ in range(50):
        if nm.node_manager.connected_nodes:
            break
        await asyncio.sleep(0.1)
    if not nm.node_manager.connected_nodes:
        print("FAIL: node never registered")
        return 1
    print(f"Node registered: {list(nm.node_manager.connected_nodes)}")

    # Send an inference request.
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"http://127.0.0.1:{PORT}/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY_PLAIN}"},
            json={
                "model": "qwen-2.5-7b",
                "messages": [{"role": "user", "content": "route me to a node"}],
                "max_tokens": 64,
            },
            timeout=30,
        )
    node_hdr = resp.headers.get("X-Orvix-Node")
    body = resp.json()
    print(f"status={resp.status_code} X-Orvix-Node={node_hdr}")
    if "choices" not in body:
        print("response body:", body)
        await client.stop()
        node_task.cancel()
        server.should_exit = True
        return 1
    print("content:", body["choices"][0]["message"]["content"])
    print("usage:", body["usage"])

    # Verify routing + billing side effects.
    jobs = DB._table("jobs").rows
    user_after = DB._table("users").rows[0]
    ok = (
        resp.status_code == 200
        and node_hdr not in (None, "mock")
        and jobs and jobs[0]["is_mock"] is False
        and float(user_after["balance_usdc"]) < 1000.0
        and float(user_after.get("available_usdc", 0)) > 0  # provider earned
    )

    # Shut down.
    await client.stop()
    node_task.cancel()
    server.should_exit = True
    await asyncio.sleep(0.2)
    server_task.cancel()

    if ok:
        print(
            f"\nPASS: routed to node, is_mock=False, dev billed "
            f"(balance={user_after['balance_usdc']}), provider earned "
            f"{user_after.get('available_usdc')}"
        )
        return 0
    print("\nFAIL: routing/billing assertions not met")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
