# Architecture

This document is a deep technical overview of how Orvix works internally. It assumes you're
technically comfortable (FastAPI, asyncio, WebSockets) but new to this codebase.

## 1. Overview

Orvix connects developers who need inference with a distributed pool of GPU providers.
A central **orchestrator** exposes an OpenAI-compatible HTTP API, authenticates requests, and
routes each job to a connected **node**. Nodes run on provider machines, execute the job on a
local inference backend, and stream the result back over a persistent WebSocket.

The system is a **monorepo** with two independently deployable packages — `orchestrator/` and
`orvix-node/` — plus a planned frontend. Keeping them together simplifies dependency
management, keeps the shared protocol in sync, and lets a single CI cover both.

## 2. High-Level Architecture

```
┌─────────────┐      OpenAI-compatible       ┌──────────────┐
│  Developer  │ ───────────  HTTPS  ───────▶ │ Orchestrator │
│  (curl /    │                              │   (FastAPI)  │
│   OpenAI    │ ◀──────── inference ──────── │              │
│   client)   │                              │              │
└─────────────┘                              └──────┬───────┘
                                                    │
                                               WebSocket
                                                    │
                                                    ▼
                                             ┌──────────────┐
                                             │   Node(s)    │
                                             │  (provider   │
                                             │  GPU + vLLM) │
                                             └──────────────┘

External:
- Supabase (PostgreSQL): user accounts, API keys, nodes, job history
```

## 3. Components

### 3.1 Orchestrator

- **Location:** `orchestrator/`
- **Role:** API gateway, authentication, job routing, node management.
- **Stack:** FastAPI, async Python 3.11+.
- **Key modules:**
  - `app/routes/` — HTTP endpoints
  - `app/services/` — business logic
  - `app/services/node_manager.py` — node registry, selection, and job dispatch
  - `app/dependencies.py` — auth dependencies (JWT and API key)

### 3.2 Node

- **Location:** `orvix-node/`
- **Role:** GPU agent — connects to the orchestrator and executes inference jobs.
- **Stack:** Python 3.11+, asyncio, `websockets`, `pynvml`, vLLM (when a GPU is present).
- **Key modules:**
  - `orvix_node/client.py` — WebSocket client with auth, heartbeat, and reconnect logic
  - `orvix_node/executor.py` — job execution with concurrency limiting
  - `orvix_node/inference/` — pluggable backends (`mock`, `vllm`)
  - `orvix_node/gpu.py` — GPU detection with a stub mode

## 4. Data Flow

### 4.1 Inference Request

```
1. Developer sends POST /v1/chat/completions with an API key.
2. Orchestrator validates the API key and resolves the user.
3. NodeManager selects an available node (by model support and tier priority).
4. The job is dispatched to the node over WebSocket.
5. The node runs inference (vLLM or mock).
6. The node returns the result over WebSocket (chunked if streaming).
7. Orchestrator returns an OpenAI-format response to the developer.
8. The job is recorded in the jobs table.
```

If no node is available, the orchestrator falls back to a mock response during alpha so the
full path stays exercisable.

## 5. Protocol (Orchestrator ↔ Node)

- **Transport:** WebSocket (`wss://` in production).
- **Handshake:** the node presents its `provider_id` and a node secret on connect.
- **Message types:** `register`, `register_ack`, `heartbeat`, `job`, `job_chunk`,
  `job_result`, `ping`, `shutdown`.
- **Encoding:** a discriminated union via Pydantic (`Field(discriminator="type")`).
- **Important:** `protocol.py` is duplicated in both packages and **must stay in sync**.
  The body below the file header is byte-identical and is verified in CI.

## 6. Database Schema

Main tables (Supabase / PostgreSQL):

- `users` — accounts, tiers
- `api_keys` — sha256-hashed API keys for developer auth
- `nodes` — registered provider nodes and their capabilities
- `jobs` — inference history

Migrations live in `orchestrator/migrations/` and are applied in numeric order
(`001`, `002`, …). Each file is idempotent and safe to re-run.

## 7. Authentication

Two distinct schemes:

### 7.1 Wallet Auth (dashboard)

- A wallet (e.g. Phantom) signs a server-issued challenge message.
- The server verifies the ed25519 signature locally via `solders`.
- On success the server issues a JWT (24h expiry) used for dashboard endpoints.

### 7.2 API Key Auth (inference)

- Format: `orvx_sk_<32-char urlsafe>`.
- Stored as a sha256 hash; the plaintext is shown once at creation.
- Sent as `Authorization: Bearer <key>` to `/v1/chat/completions`.

## 8. Node Selection

```
def select_node(model, user_tier):
    candidates = nodes.filter(
        status == 'ready',
        model in models_supported,
        current_jobs < max_concurrent_jobs,
        last_heartbeat within 60s,
    )
    if user_tier in ('gold', 'diamond'):
        prefer the least-loaded node (lowest current_jobs)
    else:
        any available node
    return candidates.first() or None
```

If no node qualifies, the request falls back to a mock response during alpha.

## 9. Stub Modes (Development)

Several components support stub modes so the whole system runs without special hardware:

- `ORVIX_NODE_STUB_GPU=true` — fake GPU detection.
- Mock inference backend (the node default).

Together these let the full developer → orchestrator → node → response path run end-to-end on
any machine, no GPU required.

## 10. Future Architecture

Planned but not yet implemented:

- Real vLLM backend (needs a GPU).
- DAO governance (v2).
- Frontend (Next.js) — a separate phase.
- Agent SDK (v3).

## 11. Testing

- **Unit tests:** `pytest` in each package (hermetic — no live DB or network).
- **Integration:** a cross-process flow runs the node against the orchestrator under uvicorn.
- **Coverage:** tracked, not yet enforced (target 80%).
- **CI:** GitHub Actions on every push and PR (see `.github/workflows/`).

## 12. Operational Notes

- **Process management:** systemd (Linux) or PM2.
- **Reverse proxy:** Caddy (auto-SSL) or Nginx.
- **Logs:** stdout in development, structured output via `loguru`.
- **Monitoring:** Sentry for errors, Grafana for metrics (planned).

## 13. Decision Records

- **Why a monorepo:** simpler dependency management, easy protocol sync, single CI.
- **Why Python everywhere:** developer productivity, and vLLM is Python-native.
- **Why Supabase:** managed PostgreSQL with auth and RLS, without the ops burden.
- **Why a WebSocket protocol:** persistent, bidirectional job dispatch and streaming with low
  per-message overhead.
