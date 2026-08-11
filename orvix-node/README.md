# Orvix Node Software

A Python agent that runs on a GPU provider's machine. It connects to the Orvix
Orchestrator over WebSocket, registers its GPU, receives inference jobs, runs
them, and returns results — earning USDC for the provider.

Inference is **mocked by default**, so the whole pipeline runs on a machine with
no GPU at all. Set `backend: "vllm"` to serve real traffic — that path is live,
and image generation runs alongside it on the same card.

## Hardware requirements

- For real inference: NVIDIA GPU, CUDA 11+, 8 GB+ VRAM (Linux).
- For development: anything — use `ORVIX_NODE_STUB_GPU=true` and the mock backend.

## Installation

**One-line (Linux providers):**
```bash
curl -sSL https://raw.githubusercontent.com/OrvixCompute/orvix/main/orvix-node/install.sh | bash
orvix-node join            # paste the credentials from the dashboard
orvix-node start
```

**Manual (development, any OS):**
```bash
cd orvix-node
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   Unix: source .venv/bin/activate
pip install -e .            # core only (mock backend)
# pip install -e .[nvml]    # + real GPU detection (no vLLM)
# pip install -e .[gpu]     # + vLLM for real inference (Linux/CUDA)
# pip install -e .[image]   # + diffusers stack for image generation
# pip install -e .[video]   # + diffusers/ffmpeg stack for text-to-video
# pip install -e .[embed]   # + sentence-transformers for embeddings (CPU is fine)
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

## Embeddings

Off by default; turn on with `enable_embedding_engine: true` and the `embed`
extra. Serves the catalog id `orvix-embed-1` (BAAI/bge-base-en-v1.5, 768 dims).

Unlike image and video, this one **runs on CPU by default and does not touch the
GPU** — which is the point. A node already busy serving chat can answer
embedding requests without competing for the card, so enabling it costs almost
nothing. Override with `ORVIX_NODE_EMBED_DEVICE=cuda` if you have headroom.

| Env | Default |
|---|---|
| `ORVIX_NODE_EMBED_MODEL` | `BAAI/bge-base-en-v1.5` |
| `ORVIX_NODE_EMBED_DEVICE` | `cpu` |
| `ORVIX_NODE_EMBED_CACHE_DIR` | `./models/orvix-embed` |

Vectors are L2-normalized before they leave the node, so callers can use a dot
product for cosine similarity. Output order always matches input order — the
engine refuses rather than returns a mismatched count, because a short result
would misalign every vector with its text in the caller's database.

## Video generation

Off by default. Turn it on with `enable_video_engine: true` (or
`ORVIX_NODE_ENABLE_VIDEO_ENGINE=true`) and install the `video` extra. The engine
serves the catalog id `orvix-video-1`, backed by LTX-Video through Diffusers;
both the repo and the pipeline class are configurable:

| Env | Default |
|---|---|
| `ORVIX_NODE_VIDEO_MODEL` | `Lightricks/LTX-Video` |
| `ORVIX_NODE_VIDEO_PIPELINE` | `LTXPipeline` |
| `ORVIX_NODE_VIDEO_CACHE_DIR` | `./models/orvix-video` |

**Enable this on a machine dedicated to video.** A clip takes minutes, and for
that whole time the node cannot serve anything else — turning it on for a box
that also carries chat changes what the machine is, it does not just add a
capability. `max_concurrent_video_jobs` defaults to 1; raising it without
measuring VRAM for two simultaneous clips is how a card that handles one fine
runs out of memory. Generated clips are written to `video_tmp_dir`
(default `/tmp/node-videos`) and served to the orchestrator over the node's
binary endpoint (`/v1/binary/video/<id>`), which deletes each MP4 after the
one-time fetch.

Requested frame counts are rounded up to the nearest 8k+1, because latent video
pipelines compress time by 8 and would otherwise silently alter the count. The
result metadata reports both `num_frames` (produced) and `requested_frames`, so
the duration it states is the real one.

`required_vram_gb` on the engine is a **placeholder** (20 GB), set high enough
that the ModelManager will not try to hold video resident beside a chat model.
Measure it on the target card before trusting it for scheduling.

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

## Releasing

Publishing is automated and tokenless — PyPI trusts this repository through
OIDC, so there is no API token to leak.

```bash
# 1. bump orvix_node/version.py (the single source; pyproject reads it)
# 2. merge that
git tag node-v0.2.1
git push origin node-v0.2.1
```

The `node-` prefix matters: this repository also tags its own releases as
`v0.2.0`, and the package has a separate version line. The prefix says which
artefact moved.

The workflow refuses to publish if the tag disagrees with `version.py`. PyPI
never lets a version be re-uploaded, so a mismatch is worth failing on rather
than discovering afterwards.

**Never move a tag that has already published.** Repointing `node-v0.2.2` at a
newer commit re-runs the workflow, which rebuilds the same version and dies on
`400 File already exists` — PyPI refuses a filename it has seen before, even
after a deletion, and that is deliberate. This happened twice on 2026-08-08:
the release at 08:58 succeeded and the two red runs after it were the same
0.2.2 being re-uploaded, not a broken release. New content means a new version:
bump `version.py`, merge, tag again. To re-attempt a release that genuinely
failed (a PyPI outage, say), use the workflow's `workflow_dispatch` trigger
rather than touching the tag.

**Providers install the last *published* version.** A fix merged to `main` does
not reach them until a release is cut — set `ORVIX_NODE_REF` to install from a
git ref if you need one before then.

## Architecture

| File | Responsibility |
| ---- | -------------- |
| `cli.py` | Click commands; wires config → GPU → backend → executor → client |
| `config.py` | Layered config (CLI/env/file/defaults), pydantic-validated |
| `gpu.py` | `GPUDetector` (pynvml) with stub mode |
| `protocol.py` | Wire messages — **kept identical with the orchestrator** |
| `client.py` | WebSocket connection, register, heartbeat, reconnect |
| `executor.py` | Concurrency-limited job execution + metrics |
| `inference/` | `base` interface, `mock` (default), `vllm` (real inference) |
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

- **`No GPU detected`** — `pip install --upgrade "orvix-node[nvml]"`, or set
  `ORVIX_NODE_STUB_GPU=true` for development.
- **`Refusing insecure ws://`** — only `ws://localhost` is allowed without TLS;
  use `wss://` for remote orchestrators.
- **Auth failed (exit 2)** — check `provider_id` / `node_secret` against the
  orchestrator's `/v1/provider/register`.

## Status

Job routing, provider earnings and USDC withdrawals are live, and vLLM inference
and image generation both run in production. Providers are paid a share of each
job they serve.

Staking is disabled during alpha, so the provider stake requirement is not
enforced yet. Expect breaking changes.
