# Architecture

This document is a deep technical overview of how Orvix works internally. It assumes you're
technically comfortable (FastAPI, asyncio, WebSockets) but new to this codebase.

## 1. Overview

Orvix connects developers who need inference with a distributed pool of GPU providers.
A central **orchestrator** exposes an OpenAI-compatible HTTP API, authenticates requests, and
routes each job to a connected **node**. Nodes run on provider machines, execute the job on a
local inference backend, and stream the result back over a persistent WebSocket.

On top of the inference path sits the **token-intelligence layer**: on-chain token/wallet
scans, accumulation scoring, holder analysis, and user-deployed monitoring agents. It runs
against plain Solana JSON-RPC (plus the Jupiter quote API for prices) and reuses the same
node network to generate AI narratives on GPU nodes.

The system is a **monorepo** with two independently deployable packages — `orchestrator/` and
`orvix-node/` — a shared `packages/protocol/` (Pydantic wire models) and a planned frontend.
Keeping them together simplifies dependency management, keeps the shared protocol in sync, and
lets a single CI cover both.

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
- Supabase (PostgreSQL): user accounts, API keys, nodes, job history, intel cache
- Solana JSON-RPC: on-chain reads (balances, signatures, parsed transactions)
- Jupiter quote API: USDC price estimation for tokens
- DexScreener / X API v2: social signals for tokens (optional)
- Sentry: error tracking (opt-in)
```

## 3. Components

### 3.1 Orchestrator

- **Location:** `orchestrator/`
- **Role:** API gateway, authentication, job routing, node management.
- **Stack:** FastAPI, async Python 3.11+.
- **Key modules:**
  - `app/routes/` — HTTP endpoints (inference, images, videos, tokens, wallets, agents, provider, admin, …)
  - `app/services/` — business logic (billing, node manager, token-intel, monitor worker, payouts)
  - `app/services/node_manager.py` — node registry, selection, and job dispatch
  - `app/services/token_intel.py` — token/wallet scans, accumulation, two-tier cache
  - `app/services/monitor_service.py` — background evaluation of user monitors + webhook delivery
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
- **Shared package:** the wire models live once in `packages/protocol/` and are
  imported by both the orchestrator and the node — no duplicated protocol files
  to drift. CI verifies imports resolve on both sides.

## 6. Database Schema

Main tables (Supabase / PostgreSQL):

- `users` — accounts, tiers
- `api_keys` — sha256-hashed API keys for developer auth
- `nodes` — registered provider nodes and their capabilities
- `jobs` / `image_jobs` / `video_jobs` — inference history (mock jobs excluded from stats)
- `intel_scans` — two-tier cache backing the token-intel endpoints (scan_type + target key)
- `monitors` / `alert_events` / `alert_webhooks` — user monitoring agents, their alerts, and the webhook outbox
- `auth_challenges` — wallet-signature challenge nonces (survive restarts)
- `topup_intents`, `withdrawals`, treasury/buyback/burn accounting tables

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
- Sent as `Authorization: Bearer <key>` to `/v1/chat/completions` and the
  token-intel endpoints. The read-only scan/status endpoints accept either
  scheme (`get_current_user_flexible`).

## 8. Node Selection

```
def select_node(model, user_tier):
    candidates = nodes.filter(
        status == 'ready',
        model in models_supported,
        current_jobs < max_concurrent_jobs,
        last_heartbeat within 60s,
        not draining,
    )
    if user_tier in ('gold', 'diamond'):
        prefer the least-loaded node (lowest current_jobs)
    else:
        any available node
    return candidates.first() or None
```

Selection is VRAM-aware (nodes with more free VRAM are preferred) and honors
drain mode (a drained node finishes its jobs and takes no new ones). If no node
qualifies, the request waits briefly for a slot (`capacity_exhausted`) or fails
fast with `no_chat_provider` / `no_image_provider` — it never falls back to a
mock response in production.

## 9. Token Intelligence & Monitoring

The intel layer answers "what is this token / wallet doing" from plain Solana
JSON-RPC + Jupiter quotes, with no third-party analytics providers.

- **Scans** (`GET /v1/tokens/{ca}`, `/v1/wallets/{wallet}`, accumulation,
  holders, early-buyers, social, clusters) resolve metadata, supply, USDC
  price, liquidity (configured pools), holder distribution
  (`getTokenLargestAccounts` + owner resolution), and social signals
  (DexScreener, optional X API v2). All lookups are fail-soft: a missing data
  source yields `null`/`[]`, never an error.
- **Cache** — results live in `intel_scans` (scan_type + target, TTL
  `INTEL_SCAN_CACHE_TTL_SECONDS`) with an in-memory front cache; repeat scans
  skip RPC work across restarts.
- **AI analysis** (`GET /v1/tokens/{ca}/intelligence`) dispatches the scan to a
  GPU node (`INTEL_AI_MODEL`) as a normal chat job — real compute demand on the
  network — and caches the narrative/risk summary. Fail-soft: no node → 503,
  never a degraded answer.
- **Monitoring agents** (`/v1/agents/*`) — the monitor worker
  (`ENABLE_MONITOR_WORKER`) evaluates user-defined conditions on a schedule,
  writes deduplicated `alert_events`, and delivers webhook alerts with
  exponential backoff (`alert_webhooks` outbox). Each cycle refreshes holder
  snapshots for monitored tokens so accumulation scoring stays current.

Plain-RPC constraints that shape this layer: mint addresses do not appear in
ordinary transfer transactions and plain RPC cannot enumerate a mint's holders,
so whale analysis is anchored on `TOKEN_WHALE_WATCHLIST_JSON` and cached holder
snapshots.

## 10. Stub Modes (Development)

Several components support stub modes so the whole system runs without special hardware:

- `ORVIX_NODE_STUB_GPU=true` — fake GPU detection.
- `ALLOW_MOCK_INFERENCE=true` — orchestrator serves canned replies when no node
  is connected (off by default; must stay off anywhere real users can reach).

Together these let the full developer → orchestrator → node → response path run end-to-end on
any machine, no GPU required.

## 11. Future Architecture

Planned but not yet implemented:

- DAO governance (v2).
- Frontend (Next.js) — a separate phase.
- Agent SDK (v3).
- Redis-backed rate limiting / challenge store (in-memory today — single-worker).

## 12. Testing

- **Unit tests:** `pytest` in each package (hermetic — no live DB or network).
- **Integration:** a cross-process flow runs the node against the orchestrator under uvicorn.
- **Coverage:** tracked, not yet enforced (target 80%).
- **CI:** GitHub Actions on every push and PR (see `.github/workflows/`).

## 13. Operational Notes

- **Process management:** systemd (Linux) or PM2.
- **Reverse proxy:** Caddy (auto-SSL) or Nginx.
- **Logs:** stdout in development, structured JSON via `loguru` in production;
  every request carries a `request_id` across all log lines (also returned as
  `X-Request-ID`). Intel scans and monitor cycles emit their own structured
  lines.
- **Monitoring:** Sentry for errors (opt-in via `SENTRY_DSN`, errors tagged
  with `request_id`/`path`; intel failures tagged with `scan_type`/`target`),
  Grafana for metrics (planned).

## 14. Decision Records

- **Why a monorepo:** simpler dependency management, easy protocol sync, single CI.
- **Why Python everywhere:** developer productivity, and vLLM is Python-native.
- **Why Supabase:** managed PostgreSQL with auth and RLS, without the ops burden.
- **Why a WebSocket protocol:** persistent, bidirectional job dispatch and streaming with low
  per-message overhead.

## 15. Image Generation & Storage Lifecycle

Image generation reuses the WebSocket job-dispatch model with an added binary
transfer channel:

1. **Dispatch** — `POST /v1/images/generations` selects an image-capable node
   (advertised via `engines` at registration) and sends `job.image.dispatch`
   over the WebSocket, carrying a per-job `binary_token`.
2. **Generate** — the node runs the default image engine (swapped into VRAM
   by the ModelManager, freeing the chat engine if needed, unless
   concurrent mode keeps both resident), writes the PNG to
   `/tmp/node-images/<id>.png`, and replies `job.image.complete` with a
   `binary_url`. (The earlier Flux Schnell engine remains in the codebase,
   unregistered, for a possible future gated-access re-enable.)
3. **Fetch** — the orchestrator GETs `binary_url` with the token as
   `X-Node-Secret`; the node streams the bytes and deletes its temp file.
4. **Store** — the orchestrator saves to `IMAGE_STORAGE_DIR`, records an
   `image_jobs` row with `expires_at = now + 24h`, and returns a public URL
   served by nginx.

**Lifecycle / retention:**
- Images auto-delete after 24h (hourly `orvix-image-cleanup` systemd timer →
  `scripts/cleanup_images.py`), which also sweeps orphan files and stale
  `holder_status` rows.
- `MAX_IMAGE_STORAGE_MB` caps the directory; over-cap requests get `503` before
  consuming quota.
- Nodes run a 10-min sweeper for un-fetched temp files.

**Access control:** holder-gated quota (`quota_service` + `holder.py`, ORVX
balance cached 15 min). Holders get `IMAGE_DAILY_LIMIT_HOLDER`/day; when
`ORVX_MINT_ADDRESS` is unset (alpha) everyone gets `IMAGE_DAILY_LIMIT_FALLBACK`
/day. Quota is consumed up front and refunded if generation fails.
