# API Reference

> 📝 Full interactive API docs are coming soon. The source of truth is the
> FastAPI auto-generated docs at `/docs` when the orchestrator is running
> (e.g. `http://localhost:8000/docs`). This page summarizes the current
> endpoints.

All endpoints are served under the `/v1` prefix. There are two kinds of
authentication:

- **JWT** — obtained by signing a wallet challenge. Used for account-level
  actions: API keys, billing, provider management, and account status
  (`/v1/account/tier`, `/v1/account/quota`).
- **API key** — a `orvx_sk_...` bearer token. Used for inference requests
  (`/v1/chat/completions`, `/v1/images/generations`).

> **Which scheme for which endpoint.** Most account/dashboard endpoints
> authenticate with a **JWT** (`get_current_user`); inference endpoints use an
> **API key** (`get_user_from_api_key`). Sending an `orvx_sk_` key to a JWT-only
> endpoint returns `401 "Not enough segments"` — the key is not a JWT.
> **Exception:** the read-only status endpoints `/v1/account/tier` and
> `/v1/account/quota` accept **either** a JWT or an API key, so programmatic
> clients can check their own tier/quota before dispatching requests.

---

## Authentication

### `GET /v1/auth/challenge?wallet=<address>`
Get a challenge string to sign with your Solana wallet. No auth required.

```bash
curl "https://orvix.network/v1/auth/challenge?wallet=YOUR_WALLET_ADDRESS"
```

```json
{ "challenge": "Sign this message to authenticate with Orvix: <nonce>" }
```

### `POST /v1/auth/verify`
Verify the signed challenge and receive a JWT. No auth required.

```bash
curl -X POST https://orvix.network/v1/auth/verify \
  -H "Content-Type: application/json" \
  -d '{ "wallet": "YOUR_WALLET_ADDRESS", "signature": "BASE58_SIGNATURE" }'
```

```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

### `POST /v1/auth/me`
Return the current authenticated user. **Auth: JWT.**

```bash
curl -X POST https://orvix.network/v1/auth/me \
  -H "Authorization: Bearer <JWT>"
```

---

## API Keys

All require **Auth: JWT.**

### `POST /v1/api-keys`
Create a new API key. The full key is returned **once** — store it securely.

```bash
curl -X POST https://orvix.network/v1/api-keys \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{ "name": "my-app" }'
```

```json
{ "id": "uuid", "name": "my-app", "key": "orvx_sk_..." }
```

### `GET /v1/api-keys`
List your API keys (metadata only — never the secret).

### `DELETE /v1/api-keys/{key_id}`
Revoke an API key. Returns `204 No Content`.

### `POST /v1/api-keys/{key_id}/rotate`
Revoke the old secret and issue a new one for the same key record.

---

## Inference (OpenAI-compatible)

### `POST /v1/chat/completions`
Run a chat completion. **Auth: API key.**

```bash
curl https://orvix.network/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "Hello, Orvix!"}]
  }'
```

**Tool calling** is supported on the non-streaming path. Pass OpenAI-shaped
`tools` (and optionally `tool_choice`); when the model decides to call one, the
reply carries `finish_reason: "tool_calls"` and `message.tool_calls`, with
`message.content` null. Send the result back as a `role: "tool"` message
carrying the matching `tool_call_id`.

```bash
curl -X POST https://orvix.network/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "What is the weather in Jakarta?"}],
    "tools": [{"type": "function", "function": {
      "name": "get_weather",
      "description": "Current weather for a city",
      "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                     "required": ["city"]}}}]
  }'
```

`tools` together with `stream: true` returns `400 streaming_tools_unsupported`.
Streaming emits tool calls as indexed argument fragments that have to be
reassembled, which is not implemented yet — refusing is preferred over streaming
the prose and silently dropping the calls.

> Responses come from a real GPU node running the model. If no node can take the
> job the request returns 503 rather than a placeholder — the API never returns a
> fabricated answer. (A local `ALLOW_MOCK_INFERENCE` flag serves a canned reply
> for development against an empty network; it is off by default and must stay
> off anywhere real users can reach.)

Two different 503s, because they call for different responses:

| Code | Meaning | What to do |
|---|---|---|
| `capacity_exhausted` | Nodes serve this model but every one is busy | Retry — the body carries `retry_after_seconds` |
| `no_chat_provider` | No node on the network serves this model at all | Retrying will not help; pick a model from `/v1/models` that a node is actually running |

```json
{ "error": { "code": "capacity_exhausted", "retry_after_seconds": 3,
             "message": "All compute providers serving this model are busy. Retry shortly." } }
```

`POST /v1/images/generations` makes the same distinction, returning
`capacity_exhausted` or `no_image_provider`.

---

## Billing

All require **Auth: JWT.**

### `POST /v1/billing/topup-intent`
Create a top-up intent (returns the deposit details to fund your balance).

### `GET /v1/billing/balance`
Return your current balance.

### `GET /v1/billing/transactions`
Return your transaction history.

### `GET /v1/billing/topup-intents`
List pending top-up intents.

---

## Account

### `GET /v1/account/tier`
Return your **stake-based** tier, discount, and progress to the next tier.
**Auth: JWT or API key.**

```json
{
  "tier": "gold",
  "staked_orvx": "75000",
  "discount_pct": 15,
  "next_tier": { "name": "diamond", "required_stake": "250000", "additional_needed": "175000" }
}
```

### `GET /v1/account/quota`
Current chat + image quota status for the authenticated wallet, plus the images
generated in the last 24h (before auto-delete). **Auth: JWT or API key.**

```bash
curl https://orvix.network/v1/account/quota \
  -H "Authorization: Bearer <JWT>"
```

```json
{
  "is_holder": false,
  "orvx_balance": "0",
  "chat": { "type": "lifetime_free", "limit": 2, "used": 0 },
  "image": { "type": "grace_daily", "daily_limit": 1, "used_today": 0,
             "generated_images_last_24h": [] }
}
```

When `ORVX_MINT_ADDRESS` is unset (grace period) everyone gets `grace_daily`
(1/day). Once set, holders get `holder_daily` (5/day) and non-holders are blocked.

---

## Staking

### `POST /v1/staking/stake-intent`
Create a memo'd intent for an ORVX stake deposit. **Auth: JWT.**
Body: `{ "amount": <number> }`. Send ORVX with the returned memo to the treasury;
the listener credits your stake automatically.

### `POST /v1/staking/unstake`
Unstake ORVX and queue a payout. **Auth: JWT.**
Body: `{ "amount": <number>, "destination_wallet": <optional> }`. Providers cannot
unstake below the 25,000 ORVX minimum (`400 provider_minimum_stake`).

### `GET /v1/staking/status`
Your current stake, derived tier, next tier, and stake history. **Auth: JWT.**

### `GET /v1/staking/buyback-history`
Recent buybacks (public, no auth) — each with its Solana signature for verification.

### `GET /v1/staking/burn-history`
Recent burns (public, no auth) — each with its Solana signature.

### `GET /v1/staking/network-stats`
Public dashboard data: total staked, provider count, buyback budget, ORVX held for
burn, totals burned/bought, and last buyback/burn timestamps.

---

## Governance

### `GET /v1/governance/snapshot-url`
Return the Snapshot space slug and URL for off-chain voting. No auth required.

---

## Network

### `GET /v1/network/stats`
Public dashboard feed for the network's compute side — node/GPU capacity,
request and token volume, image count, provider count, and how many models are
served. No auth required.

`*_window` counters cover a rolling window (`NETWORK_STATS_WINDOW_HOURS`,
default 24h); the `*_total` counters are all-time. Mock jobs are excluded, so
the numbers reflect work that real nodes actually served.

```bash
curl https://orvix.network/v1/network/stats
```

```json
{
  "window_hours": 24,
  "nodes": {
    "registered": 3, "online": 2, "ready": 1, "busy": 1,
    "draining": 0, "offline": 1,
    "chat_capable": 3, "image_capable": 1, "total_vram_gb": "128.0"
  },
  "gpus": [
    { "gpu_model": "RTX 4090", "count": 2 },
    { "gpu_model": "A100", "count": 1 }
  ],
  "providers": { "total": 1, "staked": 1 },
  "chat": {
    "requests_total": 3, "requests_window": 2,
    "tokens_total": 465, "tokens_window": 450,
    "avg_latency_ms": 1000
  },
  "images": { "generated_total": 2, "generated_window": 1 },
  "models": { "chat": 3, "image": 2 },
  "generated_at": "2026-07-26T10:00:00Z"
}
```

`nodes.online` is the number of nodes holding a live websocket connection right
now and is read fresh on every call. Everything else is a database aggregate
cached for `NETWORK_STATS_CACHE_SECONDS` (default 30), so `generated_at` is the
snapshot time rather than the request time. `avg_latency_ms` is `null` when
there were no requests in the window.

For the token/treasury side of the dashboard, see
[`GET /v1/staking/network-stats`](#get-v1stakingnetwork-stats).

---

## Admin

Admin endpoints require the `X-Admin-Key` header (separate from JWT; set via
`ADMIN_API_KEY`). Disabled when `ADMIN_API_KEY` is unset.

### `GET /v1/admin/feature-flags`
Current runtime flag state, plus the withdrawal economics. Since these are `.env`-only
and read at request time, this is the way to confirm from outside the box that a config
edit took effect — remember the service must be restarted to pick one up.

Returns stub/worker flags (`buyback_stub`, `burn_stub`, `payout_stub`,
`enable_payment_listener`, `enable_payout_worker`), staking gate
(`require_stake_for_provider`, `provider_min_stake_orvx`), configuration presence
(`orvx_mint_configured`, `admin_api_key_set`), and withdrawal economics
(`min_withdraw_amount_usdc`, `auto_approve_max_usdc`, `max_withdrawals_per_day`).
See [payout operations](operations/payouts.md) for how to choose the floor.

### `POST /v1/admin/buyback/execute`
Execute a USDC→ORVX buyback via Jupiter.
Body: `{ "amount_usdc": <number>, "slippage_bps": <int, default 50> }`.

### `GET /v1/admin/buyback/status`
Buyback budget, last buyback, and ORVX held for burn.

### `POST /v1/admin/burn/execute`
Burn ORVX to the incinerator.
Body: `{ "amount": <optional, default all held>, "period_start": <iso>, "period_end": <iso> }`.

### `GET /v1/admin/burn/status`
ORVX held for burn, total burned, and last burn.

---

## Provider

All require **Auth: JWT.**

### `POST /v1/provider/register`
Register the current account as a provider. Returns a node secret used by the
node agent to authenticate. **Requires a stake of at least 25,000 ORVX** — returns
`400 insufficient_stake` otherwise. Stake first via `POST /v1/staking/stake-intent`.

### `POST /v1/provider/regenerate-secret`
Rotate the provider's node secret.

### `GET /v1/provider/nodes`
List the provider's nodes.

### `GET /v1/provider/nodes/{node_id}`
Get details for a single node.

### `POST /v1/provider/nodes/{node_id}/rename`
Rename a node.

### `DELETE /v1/provider/nodes/{node_id}`
Remove a node. Returns `204 No Content`.

### `GET /v1/provider/earnings`
Return an earnings summary.

### `POST /v1/provider/withdraw`
Request a withdrawal of accumulated earnings.

### `GET /v1/provider/withdrawals`
List withdrawal requests.

### `GET /v1/provider/jobs`
List jobs served by the provider's nodes.

---

## Health

### `GET /health`
Liveness probe. No auth required.

### `GET /v1`
API root / version info. No auth required.

## Image generation

`POST /v1/images/generations` — OpenAI DALL-E-compatible. Auth: `Authorization:
Bearer orvx_sk_...`.

```bash
curl -X POST https://orvix.network/v1/images/generations \
  -H "Authorization: Bearer orvx_sk_..." \
  -H "Content-Type: application/json" \
  -d '{"model": "orvix-image-1", "prompt": "a fox in snow", "size": "1024x1024", "n": 1}'
```

Request: `model` (`orvix-image-1`, default; `flux-schnell` also defined in
the catalog but not currently served by any node), `prompt`, `n` (1–4),
`size`, `response_format` (`url` | `b64_json`).

`size` must be one of `256x256`, `512x512`, `1024x1024` or `1536x1536`, and is
additionally capped by the model's `max_size` in `GET /v1/models` — so
`orvix-image-1` accepts up to `1024x1024` and `flux-schnell` up to `1536x1536`.
Anything larger is rejected with `400 invalid_size` listing the sizes that model
does accept.

Response: `{"created": ..., "data": [{"url": "https://orvix.network/images/<id>.png"}]}`.
Quota headers: `X-Orvix-Quota-Remaining`, `X-Orvix-Quota-Reset`.

> ⚠️ **Images are auto-deleted after 24 hours.** Download and save anything you
> want to keep. Quota rules: holders get 5 images/day; during alpha (no ORVX mint
> configured) everyone gets 1/day. See `GET /v1/account/quota` for current status.
