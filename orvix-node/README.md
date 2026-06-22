# Orvix Node Software

A Python agent that runs on a GPU provider's machine. It connects to the Orvix
Orchestrator over WebSocket, registers its GPU, receives inference jobs, runs
them, and returns results — earning USDC for the provider.

Inference is **mocked by default**, so the entire pipeline runs on any machine.
Real GPU inference (vLLM) is a one-file swap once you have a CUDA GPU (Prompt 7).

## Hardware requirements

- For real inference: NVIDIA GPU, CUDA 11+, 8 GB+ VRAM (Linux).
- For development: anything — use `ORVIX_NODE_STUB_GPU=true` and the mock backend.

## Installation

**One-line (Linux providers):**
```bash
curl -sSL https://get.orvix.xyz/node | bash
```

**Manual (development, any OS):**
```bash
cd orvix-node
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   Unix: source .venv/bin/activate
pip install -e .            # core only (mock backend)
# pip install -e .[nvml]    # + real GPU detection (no vLLM)
# pip install -e .[gpu]     # + vLLM for real inference (Linux/CUDA)
```

Verify:
```bash
orvix-node --version
```

## Configuration

Create the config file:
```bash
orvix-node config init        # writes ~/.orvix/config.yaml
orvix-node config show        # prints resolved config (secrets masked)
```

Precedence: **CLI flags > env vars (`ORVIX_NODE_*`) > config file > defaults.**
Required fields: `provider_id`, `node_secret` (get them from
`POST /v1/provider/register` on the orchestrator).

## Running

```bash
# Development without a GPU (mock everything):
ORVIX_NODE_STUB_GPU=true orvix-node start

# Check the GPU detector:
ORVIX_NODE_STUB_GPU=true orvix-node gpu
ORVIX_NODE_STUB_GPU=true orvix-node gpu --watch

# Run inference locally without the orchestrator:
orvix-node test-inference --prompt "Hello, world"
orvix-node test-inference --prompt "Stream this" --stream

# Live status (queries the local health endpoint):
orvix-node status

# Tail logs:
orvix-node logs --tail 100 --follow
```

The node exposes a local health server (default `:9000`):
- `GET /health` → status, uptime, current jobs, GPU health, orchestrator connection
- `GET /metrics` → counters + live GPU metrics

## Running as a systemd service

The installer can set this up, or do it manually:
```ini
# /etc/systemd/system/orvix-node.service
[Service]
ExecStart=%h/.local/bin/orvix-node start
Restart=always
```
```bash
sudo systemctl enable --now orvix-node
systemctl status orvix-node
```

## Connection flow

```
Node                                  Orchestrator
 │ ── WS connect /v1/node/connect ───────▶ │
 │ ── RegisterMessage ───────────────────▶ │  validate provider + secret
 │ ◀── RegisterAck(accepted, node_id) ──── │
 │                                          │
 │ ── Heartbeat (every 15s) ─────────────▶ │  status, current_jobs, GPU metrics
 │ ◀── JobMessage ──────────────────────── │  dispatched inference request
 │ ── JobResult / JobChunk(stream) ──────▶ │  result correlated to the job
 │ ◀── Ping / Shutdown ─────────────────── │
```

On disconnect the node reconnects with exponential backoff (1→2→4…→60s).
A rejected registration (`accepted=false`) is **not** retried.

## Architecture

| File | Responsibility |
| ---- | -------------- |
| `cli.py` | Click commands; wires config → GPU → backend → executor → client |
| `config.py` | Layered config (CLI/env/file/defaults), pydantic-validated |
| `gpu.py` | `GPUDetector` (pynvml) with stub mode |
| `protocol.py` | Wire messages — **kept identical with the orchestrator** |
| `client.py` | WebSocket connection, register, heartbeat, reconnect |
| `executor.py` | Concurrency-limited job execution + metrics |
| `inference/` | `base` interface, `mock` (now), `vllm` (Prompt 7) |
| `health.py` | Local FastAPI health/metrics server |
| `state.py` | Singleton runtime state |

## Local integration with the orchestrator

1. Run the orchestrator on `:8000`.
2. Point the node at it: `ORVIX_NODE_ORCHESTRATOR_URL=ws://localhost:8000`.
3. Start the node (`ORVIX_NODE_STUB_GPU=true orvix-node start`).
4. Send a request via the OpenAI client to the orchestrator — it routes to the node.

## Testing

```bash
pip install -e .[dev]
pytest -q

# Standalone client smoke test against an in-process mock server:
ORVIX_NODE_STUB_GPU=true python test_connection.py
```

## Troubleshooting

- **`No GPU detected`** — install `pip install orvix-node[nvml]`, or set
  `ORVIX_NODE_STUB_GPU=true` for development.
- **`Refusing insecure ws://`** — only `ws://localhost` is allowed without TLS;
  use `wss://` for remote orchestrators.
- **Auth failed (exit 2)** — check `provider_id` / `node_secret` against the
  orchestrator's `/v1/provider/register`.

## Roadmap

- Prompt 5–6: orchestrator routes real jobs to nodes; provider earnings/withdrawals.
- Prompt 7: real vLLM inference (replace the mock backend).
