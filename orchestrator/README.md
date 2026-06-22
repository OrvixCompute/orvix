# Orvix Orchestrator

FastAPI backend for **Orvix**, a decentralized AI compute network on Solana. It
handles wallet authentication, API keys, billing, and an OpenAI-compatible
inference API (currently mock-backed — real GPU nodes come later).

Everything runs locally against Supabase (cloud Postgres) and Solana RPC
(Helius). No VPS or GPU required.

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
needed for the Prompt 6 payment listener (leave `ENABLE_PAYMENT_LISTENER=false`
until then).

## 3. Set up the database

Open the Supabase **SQL Editor**, paste the contents of
`migrations/001_initial_schema.sql`, and run it. This creates all tables,
indexes, triggers, RLS policies, the atomic balance functions, and seeds a test
user + API key.

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
| POST | `/v1/chat/completions` | API key | OpenAI-compatible inference (mock) |
| POST | `/v1/billing/topup-intent` | JWT | Create a deposit intent |
| GET  | `/v1/billing/balance` | JWT | Current balances |
| GET  | `/v1/billing/transactions` | JWT | Transaction history |
| GET  | `/v1/billing/topup-intents` | JWT | Pending intents |

---

## Test scripts

```bash
# End-to-end auth (generates a throwaway keypair, signs the challenge):
python test_auth.py

# Call the inference API like an OpenAI client:
python test_openai_client.py

# Simulate / inspect the payment flow (Prompt 6):
python test_payment.py --help
```

## Unit tests

```bash
pytest -q
```

`tests/` covers API-key management and the inference/billing logic. The
inference tests stub Supabase, so they run without a live database.

---

## Architecture notes

- **`app/config.py`** — single `Settings` object; all env access goes through it.
- **`app/dependencies.py`** — the two auth schemes: `get_current_user` (JWT) and
  `get_user_from_api_key` (`orvx_sk_`).
- **Atomic billing** — balance changes go through the `deduct_balance` /
  `credit_balance` Postgres functions so concurrent requests can't race.
- **Mock inference** — `inference_service` fakes generation but the cost,
  tier-discount, and balance math are real, so the whole billing flow is
  testable before any GPU exists.
- **Payment listener** — an asyncio background task polls Helius, matches memos
  to top-up intents, and credits balances idempotently (unique on the Solana
  signature).

---

## Node integration & job routing (Prompts 5–6)

GPU nodes (the `orvix-node` package) connect over WebSocket and the orchestrator
routes real inference jobs to them. If no node is connected, it falls back to the
in-process mock so development never blocks.

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
  `select_node` (tier-aware), `dispatch_job` (Future for blocking, Queue for
  streaming), result/chunk correlation, stale-node eviction.
- **`app/routes/node.py`** — `WS /v1/node/connect`: register → ack → receive loop.
- **`app/models/protocol.py`** — wire messages, **kept identical** with the node's
  `orvix_node/protocol.py` (verified byte-for-byte below the header).
- **`app/routes/inference.py`** — routes to a node when available, mock otherwise;
  bills on real token counts and settles the provider's share.

### Provider flow (`/v1/provider/*`)

1. `POST /v1/provider/register` → opt in, returns a `node_secret` (shown once).
2. Run a node with that `provider_id` + `node_secret`.
3. Jobs served by your nodes accrue earnings → `available_usdc` (70% of job cost).
4. `GET /v1/provider/earnings` to see lifetime/available/pending + daily breakdown.
5. `POST /v1/provider/withdraw` → queues a withdrawal; the **payout worker**
   (`app/services/payout_service.py`) settles it. On-chain sending is **stubbed**
   (`PAYOUT_STUB=true`) until you wire the treasury keypair. Withdrawals are
   rate-limited per day and amounts over `AUTO_APPROVE_MAX_USDC` need manual approval.

### Migrations

Run in order in the Supabase SQL editor: `001_initial_schema.sql` →
`002_nodes.sql` → `003_provider.sql` → `004_credit_topup.sql`. The schema is
USDC-native (6 decimals). For a database first created with the older ORVX
columns, run `005_orvx_to_usdc.sql` to migrate it in place (idempotent).

### Local end-to-end (no real DB)

`scripts/_local_e2e.py` runs the real app + the real node in one process against
the in-memory test fake and asserts a request routes to the node and bills both
sides. Run it from the orchestrator venv after `pip install -e ../orvix-node`.
The DB-backed manual version is `scripts/test_node_integration.py`.

## Roadmap

- Frontend (Next.js dashboard)
- Real vLLM inference on the node (orvix-node Prompt 7, needs a GPU)
- Move nonce store + rate limiter to Redis
- Real (non-stubbed) on-chain payouts with the treasury keypair
- Deployment to VPS
