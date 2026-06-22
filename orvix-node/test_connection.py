"""Standalone smoke test for OrchestratorClient against a mock WebSocket server.

Spins up an in-process server that accepts a node, ACKs registration, sends a
job, and prints the result the node returns. Verifies the client end-to-end
without a real orchestrator.

    ORVIX_NODE_STUB_GPU=true python test_connection.py
"""

import asyncio
import os

import websockets

from orvix_node.config import NodeConfig
from orvix_node.protocol import (
    JobMessage,
    RegisterAckMessage,
    parse_message,
    serialize,
)

os.environ.setdefault("ORVIX_NODE_STUB_GPU", "true")

HOST, PORT = "127.0.0.1", 8799


async def mock_orchestrator(stop: asyncio.Event):
    received_result = asyncio.Event()

    async def handler(ws):
        # 1. Expect register.
        reg = parse_message(await ws.recv())
        assert reg.type == "register", reg.type
        print(f"[server] register from provider={reg.provider_id} gpu={reg.gpu_info.model}")
        await ws.send(serialize(RegisterAckMessage(node_id="node-test-1", accepted=True)))

        # 2. Send a job.
        await ws.send(
            serialize(
                JobMessage(
                    job_id="job-1",
                    model="qwen-2.5-7b",
                    messages=[{"role": "user", "content": "Hello from the mock server"}],
                    max_tokens=64,
                    stream=False,
                )
            )
        )

        # 3. Read messages until we get the job result.
        async for raw in ws:
            msg = parse_message(raw)
            if msg.type == "heartbeat":
                print(f"[server] heartbeat status={msg.status} jobs={msg.current_jobs}")
            elif msg.type == "job_result":
                print(f"[server] job_result status={msg.status}")
                print(f"[server] content: {msg.result['choices'][0]['message']['content']}")
                received_result.set()
                break

    async with websockets.serve(handler, HOST, PORT):
        await asyncio.wait_for(received_result.wait(), timeout=15)
        stop.set()


async def main():
    from orvix_node.client import OrchestratorClient
    from orvix_node.executor import JobExecutor
    from orvix_node.inference.mock import MockBackend
    from orvix_node.logger import configure_logging

    configure_logging("INFO", None, False)

    cfg = NodeConfig(
        provider_id="test-provider",
        node_secret="test-secret",
        orchestrator_url=f"ws://{HOST}:{PORT}",
        model="qwen-2.5-7b",
        heartbeat_interval=2,
    )

    executor = JobExecutor(MockBackend(provider_id="test-provider"), max_concurrent=2)
    await executor.initialize(cfg.model)

    client = OrchestratorClient(cfg)
    client.set_job_handler(
        lambda job: executor.execute(
            job, send_chunk=client.send_message, send_result=client.send_message
        )
    )

    stop = asyncio.Event()
    server_task = asyncio.create_task(mock_orchestrator(stop))
    client_task = asyncio.create_task(client.start())

    await stop.wait()
    await client.stop()
    client_task.cancel()
    await asyncio.gather(server_task, return_exceptions=True)
    print("\nSmoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
