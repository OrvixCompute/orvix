# Orvix Orchestrator

FastAPI backend for **Orvix**, a decentralized AI compute network on Solana. It
handles wallet authentication, API keys, billing, an OpenAI-compatible
inference API (chat / images / videos / embeddings routed to real GPU nodes),
and a token-intelligence layer (scans, accumulation, monitoring agents).

Everything runs locally against Supabase (cloud Postgres) and Solana RPC.

- **Stack:** Python 3.11, FastAPI, Supabase, `solders`, `tiktoken`, `httpx`
- **Auth:** wallet signature (Phantom → ed25519 → JWT) + `orvx_sk_` API keys
- **Token:** USDC (SPL, 6 decimals — no custom smart contracts)

---

## 1. Install

```bash
cd orchestrator
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS/Linux:           source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
```

Fill in at least `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and `JWT_SECRET`.
Generate a secret with `openssl rand -hex 32`. The Solana/Helius vars are only
needed for the payment listener (leave `ENABLE_PAYMENT_LISTENER=false` until a
treasury wallet is configured).

## 3. Set up the database

Open the Supabase **SQL Editor**, paste the contents of
`migrations/001_initial_schema.sql`, and run it. This creates all tables,
indexes, triggers, RLS policies, the atomic balance functions, and seeds a test
user + API key. Run the remaining migrations in numeric order (`002`, `003`,
…); each is idempotent and safe to re-run.

Seeded test credentials (local dev only):

- **Wallet:** `5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9` (tier `gold`, 1000 USDC)
- **API key:** `orvx_sk_testkey0testkey0testkey0testkey0`

## 4. Run

```bash
uvicorn app.main:app --reload
```

Interactive docs at <http://localhost:8000/docs>.

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0","db":"connected"}
```

---

## Endpoints

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| GET  | `/health` | — | Liveness + DB check |
| GET  | `/v1` | — | API info |
| GET  | `/v1/auth/challenge?wallet=` | — | Get a message to sign |
| POST | `/v1/auth/verify` | — | Verify signature → JWT |
| POST | `/v1/auth/me` | JWT | Current user |
| POST | `/v1/api-keys` | JWT | Create API key (returned once) |
| GET  | `/v1/api-keys` | JWT | List keys |
| DELETE | `/v1/api-keys/{id}` | JWT | Revoke (soft delete) |
| POST | `/v1/api-keys/{id}/rotate` | JWT | Rotate key |
| POST | `/v1/chat/completions` | API key | OpenAI-compatible chat (GPU node) |
| POST | `/v1/embeddings` | API key | OpenAI-compatible embeddings |
| POST | `/v1/images/generations` | API key | Text-to-image |
| POST | `/v1/videos/generations` | API key | Text-to-video |
| GET  | `/v1/models` | — | Model catalog with `available` flags |
| POST | `/v1/billing/topup-intent` | JWT | Create a deposit intent |
| GET  | `/v1/billing/balance` | JWT | Current balances |
| GET  | `/v1/billing/transactions` | JWT | Transaction history |
| GET  | `/v1/billing/topup-intents` | JWT | Pending intents |
| GET  | `/v1/account/tier` | JWT or key | Stake-based tier + discount |
| GET  | `/v1/account/quota` | JWT or key | Chat/image/video quota status |
| GET  | `/v1/tokens/{ca}` | JWT or key | Token profile (metadata, supply, price, liquidity, risk) |
| GET  | `/v1/tokens/{ca}/accumulation` | JWT or key | 7-day accumulation score |
| GET  | `/v1/tokens/{ca}/holders` | JWT or key | Real top-holder distribution |
| GET  | `/v1/tokens/{ca}/early-buyers` | JWT or key | First-buy evidence |
| GET  | `/v1/tokens/{ca}/social` | JWT or key | DexScreener + Twitter social analysis |
| GET  | `/v1/tokens/{ca}/clusters` | JWT or key | Coordinated-wallet clusters |
| GET  | `/v1/tokens/{ca}/intelligence` | JWT or key | AI narrative/risk via GPU node |
| GET  | `/v1/wallets/{wallet}?mint=` | JWT or key | Holdings, activity, buy history |
| POST | `/v1/agents/monitors` | JWT | Create a monitoring agent |
| GET  | `/v1/agents/monitors` | JWT | List monitors |
| PATCH | `/v1/agents/monitors/{id}` | JWT | Update monitor |
| DELETE | `/v1/agents/monitors/{id}` | JWT | Delete monitor |
| GET  | `/v1/agents/alerts` | JWT | Alert events across monitors |
| GET  | `/v1/agents/monitors/{id}/alerts` | JWT | Alert events for one monitor |
| POST | `/v1/agents/monitors/{id}/test` | JWT | Test the webhook |
| POST | `/v1/staking/stake-intent` | JWT | Create a stake deposit intent |
| POST | `/v1/staking/unstake` | JWT | Unstake ORVX |
| GET  | `/v1/staking/status` | JWT | Stake + derived tier |
| GET  | `/v1/staking/buyback-history` | — | Recent buybacks |
| GET  | `/v1/staking/burn-history` | — | Recent burns |
| GET  | `/v1/staking/network-stats` | — | Token-side dashboard data |
| GET  | `/v1/governance/snapshot-url` | — | Snapshot space URL |
| GET  | `/v1/network/stats` | — | Compute-side dashboard data |
| POST | `/v1/provider/register` | JWT | Become a provider (node_secret) |
| GET  | `/v1/provider/nodes` | JWT | List the provider's nodes |
| GET  | `/v1/provider/nodes/{id}` | JWT | Node details |
| GET  | `/v1/provider/nodes/{id}/history` | JWT | Node job history |
| GET  | `/v1/provider/health` | JWT | Aggregated node health + alerts |
| POST | `/v1/provider/nodes/{id}/rename` | JWT | Rename a node |
| DELETE | `/v1/provider/nodes/{id}` | JWT | Remove a node |
| GET  | `/v1/provider/earnings` | JWT | Earnings summary |
| POST | `/v1/provider/withdraw` | JWT | Request a withdrawal |
| GET  | `/v1/provider/withdrawals` | JWT | List withdrawals |
| GET  | `/v1/provider/jobs` | JWT | Jobs served by the provider |
| POST | `/v1/verify/receipt` | API key | Verify a signed inference receipt |
| GET  | `/v1/verify/public-key` | — | Receipt signing public key |
| GET/POST | `/v1/admin/*` | X-Admin-Key | Buyback/burn/storage/intel admin ops |

The full surface, with request/response shapes, is in
[`docs/api-reference.md`](../docs/api-reference.md).

---

## Test scripts

```bash
# End-to-end auth (generates a throwaway keypair, signs the challenge):
python test_auth.py

# Call the inference API like an OpenAI client:
python test_openai_client.py

# Simulate / inspect the payment flow:
python test_payment.py --help
```

## Unit tests

```bash
pytest -q
```

`tests/` covers auth, billing, inference routing, node management, the whole
token-intel layer (scans, accumulation, holders, social, monitors, webhooks,
AI analysis), and observability. The tests stub Supabase/Solana, so they run
without a live database.

---

## Architecture notes

- **`app/config.py`** — single `Settings` object; all env access goes through it.
- **`app/dependencies.py`** — the auth schemes: `get_current_user` (JWT),
  `get_user_from_api_key` (`orvx_sk_`), `get_current_user_flexible` (either),
  `get_current_provider` (JWT + provider role).
- **Atomic billing** — balance changes go through the `deduct_balance` /
  `credit_balance` Postgres functions so concurrent requests can't race.
- **Node routing** — real inference is dispatched over WebSocket to GPU nodes;
  without a node the API returns 503 rather than fabricating an answer
  (`ALLOW_MOCK_INFERENCE` exists only for local development).
- **Token intelligence** — scans/accumulation/social analysis run against
  plain Solana JSON-RPC + the Jupiter quote API, cached in the `intel_scans`
  table (two-tier: in-memory + DB), and fail soft when a data source is down.
- **Monitoring agents** — the background monitor worker evaluates user
  monitors (`ENABLE_MONITOR_WORKER`), emits deduplicated `alert_events`, and
  delivers webhooks with exponential backoff.
- **Payment listener** — an asyncio background task polls Solana, matches memos
  to top-up intents, and credits balances idempotently (unique on the Solana
  signature).

---

## Node integration & job routing

GPU nodes (the `orvix-node` package) connect over WebSocket and the orchestrator
routes real inference jobs to them. If no node is connected, the API returns
503 (a local `ALLOW_MOCK_INFERENCE` flag serves canned replies for development
against an empty network; it is off by default and must stay off anywhere real
users can reach).

```
Developer                Orchestrator                         Node
   │  POST /v1/chat/completions │                               │
   │  (Bearer orvx_sk_…)        │                               │
   │ ──────────────────────────▶│ select_node(model, tier)      │
   │                            │ ── JobMessage (WS) ──────────▶ │ run inference
   │                            │ ◀── JobResult / JobChunk ───── │
   │ ◀── OpenAI response ────────│ bill dev, pay provider 70%    │
   │   X-Orvix-Node: <uuid>     │ record job (is_mock=False)     │
```

- **`app/services/node_manager.py`** — in-memory registry of connected nodes;
  `select_node` (tier-aware, VRAM-aware), `dispatch_job`, result/chunk
  correlation, stale-node eviction, drain mode.
- **`app/routes/node.py`** — `WS /v1/node/connect`: register → ack → receive loop.
- **`app/models/protocol.py`** — wire messages, imported from the shared
  `packages/protocol` package (kept in sync with `orvix_node/protocol.py`).
- **`app/routes/inference.py`** — routes to a node, bills on real token counts,
  settles the provider's share.

### Provider flow (`/v1/provider/*`)

1. `POST /v1/provider/register` (or the combined `POST /v1/provider/onboard`)
   → opt in, returns a `node_secret` (shown once).
2. Run a node with that `provider_id` + `node_secret`.
3. Jobs served by your nodes accrue earnings → `available_usdc` (70% of job cost).
4. `GET /v1/provider/earnings` to see lifetime/available/pending + daily breakdown.
5. `POST /v1/provider/withdraw` → queues a withdrawal; the **payout worker**
   (`app/services/payout_service.py`) settles it. On-chain sending is **stubbed**
   (`PAYOUT_STUB=true`) until you wire the treasury keypair. Withdrawals are
   rate-limited per day and amounts over `AUTO_APPROVE_MAX_USDC` need manual approval.

### Migrations

Run in order in the Supabase SQL editor: `001_initial_schema.sql` → `002_nodes.sql`
→ … → `025_*`. Each file is idempotent and safe to re-run. The schema is
USDC-native (6 decimals). For a database first created with the older ORVX
columns, run `005_orvx_to_usdc.sql` to migrate it in place.

### Local end-to-end (no real DB)

`scripts/_local_e2e.py` runs the real app + the real node in one process against
the in-memory test fake and asserts a request routes to the node and bills both
sides. Run it from the orchestrator venv after `pip install -e ../orvix-node`.
The DB-backed manual version is `scripts/test_node_integration.py`.

---

## Observability

- **Logs:** stdout only; human-readable in dev, JSON lines in prod. Every
  request gets a `request_id` (also returned as `X-Request-ID`) and it is
  attached to **every** log line emitted while serving the request. Intel
  scans log one `intel_scan` line each (scan_type, target, cache_hit,
  duration_ms); the monitor worker logs a `monitor_cycle` summary.
- **Sentry:** opt-in via `SENTRY_DSN` (see `.env.example`). Errors are tagged
  with `request_id`/`path`; fail-soft intel failures are captured as warnings
  tagged with `scan_type`/`target`.
- See [`docs/operations.md`](../docs/operations.md) for the runbook.

## Roadmap

Shipped since this list was written: real vLLM inference, image/video/embedding
generation on nodes, on-chain USDC payouts from the treasury, the payment
listener crediting top-ups, the VPS deployment, the token-intelligence layer,
and provider self-serve onboarding.

Still open:

- Frontend dashboard (separate repository)
- Redis-backed rate limiting / challenge store (in-memory today — single-worker)
- Re-enable the provider stake requirement, disabled during alpha
