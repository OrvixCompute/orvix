"""Manual end-to-end integration test: orchestrator + a real node + an inference
request, verifying the job is routed to the node (not the mock fallback).

Prerequisites:
  1. The orchestrator is running on http://localhost:8000 with a configured DB.
  2. You have a provider registered (POST /v1/provider/register) and its
     provider_id + node_secret.
  3. You have a developer API key with some USDC balance.
  4. The `orvix-node` package is installed (pip install -e ../orvix-node).

Configure via env vars, then run:

    ORCHESTRATOR_HTTP=http://localhost:8000 \
    PROVIDER_ID=<uuid> NODE_SECRET=<secret> ORVIX_API_KEY=orvx_sk_... \
    python scripts/test_node_integration.py

What it does:
  - Starts a node (stub GPU, mock backend) pointing at the orchestrator.
  - Waits for the node to report orchestrator_connected=true.
  - Sends a chat completion and checks the X-Orvix-Node response header:
      * a node UUID  -> routed to the node (PASS)
      * "mock"       -> fell back to mock (FAIL — node not connected/selected)
"""

import os
import subprocess
import sys
import time

import httpx

ORCH = os.environ.get("ORCHESTRATOR_HTTP", "http://localhost:8000")
WS = ORCH.replace("http://", "ws://").replace("https://", "wss://")
PROVIDER_ID = os.environ.get("PROVIDER_ID")
NODE_SECRET = os.environ.get("NODE_SECRET")
API_KEY = os.environ.get("ORVIX_API_KEY")
MODEL = os.environ.get("MODEL", "qwen-2.5-7b")
NODE_HEALTH = "http://127.0.0.1:9000/health"


def _require(name, value):
    if not value:
        print(f"Missing required env var: {name}", file=sys.stderr)
        sys.exit(2)


def start_node() -> subprocess.Popen:
    env = {
        **os.environ,
        "ORVIX_NODE_STUB_GPU": "true",
        "ORVIX_NODE_BACKEND": "mock",
        "ORVIX_NODE_PROVIDER_ID": PROVIDER_ID,
        "ORVIX_NODE_NODE_SECRET": NODE_SECRET,
        "ORVIX_NODE_ORCHESTRATOR_URL": WS,
        "ORVIX_NODE_MODEL": MODEL,
    }
    print(f"Starting node -> {WS}/v1/node/connect (model={MODEL})")
    return subprocess.Popen([sys.executable, "-m", "orvix_node", "start"], env=env)


def wait_for_connection(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = httpx.get(NODE_HEALTH, timeout=2.0).json()
            if data.get("orchestrator_connected"):
                print(f"Node connected as {data.get('node_id')}")
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def send_inference() -> str:
    resp = httpx.post(
        f"{ORCH}/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Integration test ping"}],
            "max_tokens": 64,
        },
        timeout=90.0,
    )
    resp.raise_for_status()
    node = resp.headers.get("X-Orvix-Node", "?")
    content = resp.json()["choices"][0]["message"]["content"]
    print(f"Response routed via node='{node}'")
    print(f"Content: {content}")
    return node


def main() -> int:
    _require("PROVIDER_ID", PROVIDER_ID)
    _require("NODE_SECRET", NODE_SECRET)
    _require("ORVIX_API_KEY", API_KEY)

    node_proc = start_node()
    try:
        if not wait_for_connection():
            print("Node failed to connect within timeout.", file=sys.stderr)
            return 1
        node = send_inference()
        if node and node != "mock":
            print("\nPASS: request was routed to the node.")
            return 0
        print("\nFAIL: request fell back to the mock (no node selected).", file=sys.stderr)
        return 1
    finally:
        node_proc.terminate()
        try:
            node_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            node_proc.kill()


if __name__ == "__main__":
    sys.exit(main())
