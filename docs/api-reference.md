# API Reference

> 📝 Full interactive API docs are coming soon. The source of truth is the
> FastAPI auto-generated docs at `/docs` when the orchestrator is running
> (e.g. `http://localhost:8000/docs`). This page summarizes the current
> endpoints.

All endpoints are served under the `/v1` prefix. There are two kinds of
authentication:

- **JWT** — obtained by signing a wallet challenge. Used for account-level
  actions: API keys, billing, provider management, monitoring agents, and
  account status (`/v1/account/tier`, `/v1/account/quota`).
- **API key** — a `orvx_sk_...` bearer token. Used for inference requests
  (`/v1/chat/completions`, `/v1/images/generations`) and, like a JWT, for the
  token-intel endpoints (`/v1/tokens/*`, `/v1/wallets/*`).

> **Which scheme for which endpoint.** Most account/dashboard endpoints
> authenticate with a **JWT** (`get_current_user`); inference and token-intel
> endpoints accept an **API key** (`get_user_from_api_key` /
> `get_current_user_flexible`). Sending an `orvx_sk_` key to a JWT-only
> endpoint returns `401 "Not enough segments"` — the key is not a JWT.
> **Exception:** the read-only status endpoints `/v1/account/tier`,
> `/v1/account/quota`, and the token/wallet scan endpoints accept **either** a
> JWT or an API key.

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

Challenges are stored server-side, valid for **5 minutes**, and **single-use** —
verifying one consumes it. A wallet may hold **several outstanding at once**, so
requesting a new challenge does not invalidate one the user is still signing, and
a restart of the orchestrator does not drop challenges that are already issued.

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
| `capacity_exhausted` | Nodes serve this model but every one stayed busy for the whole wait | Retry — the body carries `retry_after_seconds` |
| `no_chat_provider` | No node on the network serves this model at all | Retrying will not help; pick a model from `/v1/models` that a node is actually running |

```json
{ "error": { "code": "capacity_exhausted", "retry_after_seconds": 3,
             "message": "All compute providers serving this model are busy. Retry shortly." } }
```

Chat requests do not give up the instant every node is busy: they wait up to
**3 seconds** for a slot and proceed as soon as one opens, since jobs typically
finish in about that time. `capacity_exhausted` means the whole window elapsed
with nothing free. A model no node serves still fails immediately — waiting
could not change that answer.

`POST /v1/images/generations` makes the same distinction, returning
`capacity_exhausted` or `no_image_provider`.

### `POST /v1/embeddings`
OpenAI-compatible text embeddings. **Auth: API key.**
Body: `{ "model": "orvix-embed-1", "input": "text" | ["a","b"], "encoding_format": "float" | "base64" }`.
Up to 256 inputs, 8192 characters each; pre-tokenized integer input is refused
rather than mishandled.

```json
{ "object": "list", "model": "orvix-embed-1",
  "data": [{ "object": "embedding", "index": 0, "embedding": [0.01, "..."] }],
  "usage": { "prompt_tokens": 8, "total_tokens": 8 } }
```

Vectors come back **in input order** — `index` is the position to pair them by —
and are **L2-normalized**, so cosine similarity is a dot product.
`503 no_embedding_provider` means nobody is serving the model (do not retry);
`503 capacity_exhausted` means they are busy (do retry).

**Free during the alpha**, rate-limited per API key in its own bucket so an
indexing run cannot spend your chat allowance. There is no embedding price yet;
when one exists this endpoint gains a quota gate like chat and images have.

### `POST /v1/videos/generations`
Text-to-video generation. **Auth: API key.**

```bash
curl -X POST https://orvix.network/v1/videos/generations \
  -H "Authorization: Bearer orvx_sk_..." \
  -H "Content-Type: application/json" \
  -d '{"model": "orvix-video-1", "prompt": "a cat walking through a neon city",
       "width": 704, "height": 480, "num_frames": 97, "fps": 24}'
```

Request: `model` (`orvix-video-1`), `prompt` (required), and the generation
knobs — `width` (256–1280, default 704), `height` (256–720, default 480),
`num_frames` (9–257, default 97), `fps` (8–60, default 24),
`num_inference_steps` (1–60, default 30), `guidance_scale` (0–20, default 3.0),
`negative_prompt`, `seed`. One clip per call.

```json
{ "created": ..., "data": [{ "url": "https://orvix.network/videos/<id>.mp4" }] }
```

Quota headers: `X-Orvix-Quota-Remaining`, `X-Orvix-Quota-Reset`.

> ⚠️ **Videos are auto-deleted after 24 hours.** Download and save anything you
> want to keep. Video is **free during the alpha**, limited by a daily per-account
> allowance (`GET /v1/account/quota` shows the `video` status). A clip takes
> minutes of GPU on the node, so the endpoint serializes per node — expect a slow
> response. Errors mirror the image path: `503 no_video_provider` means nobody is
> serving the model (do not retry), `503 capacity_exhausted` means nodes are busy
> (retry after `retry_after_seconds`), and `504 node_timeout` means the clip took
> longer than `VIDEO_JOB_TIMEOUT`.

---

## Models

### `GET /v1/models`
OpenAI-compatible model catalog. No auth required.

Each entry carries an Orvix-specific **`available`** flag: `true` when a
currently connected node runs that model, `false` when it is only in the
catalog. Requesting an unavailable model returns `503 no_chat_provider` /
`no_image_provider`, so check this first rather than discovering it from the
error. Extra fields are ignored by OpenAI clients.

```json
{ "object": "list", "data": [
  { "id": "qwen-2.5-7b", "object": "model", "owned_by": "orvix",
    "type": "chat", "available": true, "context_window": 32768 },
  { "id": "mistral-7b", "object": "model", "owned_by": "orvix",
    "type": "chat", "available": false, "context_window": 32768 }
] }
```

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
unstake below the 2,000,000 ORVX minimum (`400 provider_minimum_stake`). This floor
is **not** suspended during the alpha — `REQUIRE_STAKE_FOR_PROVIDER` governs
registration only, while this check keys off `is_provider` alone.

### `GET /v1/staking/status`
Your current stake, derived tier, next tier, and stake history. **Auth: JWT.**

### `GET /v1/staking/buyback-history`
Recent buybacks (public, no auth) — each with its Solana signature for verification.

### `GET /v1/staking/burn-history`
Recent burns (public, no auth) — each with its Solana signature.

### `GET /v1/staking/network-stats`
Public dashboard data: total staked, provider count, buyback budget, ORVX held for
burn, totals burned/bought, and last buyback/burn timestamps.

### `POST /v1/staking/user/stake` *(non-custodial, opt-in)*
Build an **unsigned** `stake` transaction for the user's wallet to sign.
**Auth: JWT.** Requires `USER_STAKING_PROGRAM_ID` configured; otherwise `404`.
Body: `{ "amount": <number>, "lock_days": 3|7|14 }`. The user signs the returned
`transaction` (hex) in their wallet and submits it; the ORVX moves from their ATA
into the program-owned vault.

### `POST /v1/staking/user/unstake` *(non-custodial, opt-in)*
Build an **unsigned** `unstake` transaction for the user's wallet to sign.
**Auth: JWT.** Body: `{ "amount": <number> }`. Only succeeds on-chain once the
lock period has passed; partial unstakes are allowed.

### `POST /v1/staking/user/submit` *(non-custodial, opt-in)*
Broadcast a user-signed transaction (hex, from `/stake` or `/unstake`) to the
network. **Auth: JWT.** Body: `{ "transaction": "<hex>" }`. Returns
`{ "signature": "<base58>" }` once accepted by the RPC.

### `GET /v1/staking/user/status` *(non-custodial, opt-in)*
Read the user's on-chain `StakeAccount` (staked ORVX, lock deadline) and the
derived tier. **Auth: JWT.**

---

## Token Intelligence

Token/CA scans, wallet analysis, accumulation detection, monitoring agents and
webhook alerts. Data comes from plain Solana JSON-RPC + the Jupiter quote API —
no third-party analytics providers. Because plain RPC cannot enumerate a mint's
holders or page a mint's transfer history, holder/whale analysis is anchored on
`TOKEN_WHALE_WATCHLIST_JSON` (tracked wallets) and on holder snapshots cached by
earlier scans. Fields the data sources cannot answer return `null`/`[]`.

Holder snapshots are refreshed automatically for monitored tokens by the
monitor worker (`INTEL_HOLDER_SNAPSHOT_TTL_SECONDS`, default 3600s) and can be
refreshed manually via `POST /v1/admin/intel/holder-snapshot?mint=<ca>`.

### `GET /v1/tokens/{ca}`
Full token profile for a Solana mint: metadata (Metaplex on-chain, when
present), supply, USDC price (Jupiter), liquidity estimate (only for pools
listed in `TOKEN_POOLS_JSON`), cached holder snapshot, and a risk summary.
**Auth: JWT or API key.** Results are cached for
`INTEL_SCAN_CACHE_TTL_SECONDS`. Scan endpoints share a per-account
per-minute rate limit (the `intel` bucket) because they spend external
RPC/Jupiter budget.

```json
{
  "mint": "...",
  "metadata": { "name": "...", "symbol": "...", "uri": "...", "decimals": 9 },
  "supply": { "amount": "1000000000000", "decimals": 9, "uiAmountString": "1000.0" },
  "price_usdc": 0.0123,
  "liquidity": { "estimated_usdc": 45000.0, "pool_count": 2 },
  "holders": { "total_holders": 18, "top_holders": [{ "wallet": "...", "token_account": "...", "balance": 123.0 }], "top10_share": 0.62, "as_of": "2026-08-20T00:00:00Z" },
  "risk": { "warnings": ["No on-chain token metadata — the mint may be unaudited or unverified"] },
  "scanned_at": "2026-08-20T00:00:00Z"
}
```

### `GET /v1/tokens/{ca}/accumulation`
Accumulation score (0–100) + metrics for a mint over a 7-day window: net inflow
across watchlist wallets, distinct buy transfers, top-10 holder concentration,
and per-component scores. **Auth: JWT or API key.** Rate-limited like the scan
endpoint.

```json
{
  "mint": "...",
  "score": 71,
  "label": "strong",
  "metrics": {
    "watchlist_wallets": 3, "inflow_7d": 12000.0, "inflow_ratio": 0.012,
    "buy_tx_7d": 14, "top10_share": 0.62,
    "inflow_score": 60.0, "activity_score": 70.0, "distribution_score": 76.0
  },
  "computed_at": "2026-08-20T00:00:00Z"
}
```

### `GET /v1/tokens/{ca}/holders`
Real top-holder distribution for a mint, resolved to owner wallets via
`getTokenLargestAccounts` + per-account owner lookup: `{ total_holders,
top_holders: [{wallet, token_account, balance}], top10_share, as_of }`.
Falls back to the watchlist snapshot when RPC cannot resolve accounts.
**Auth: JWT or API key.** Rate-limited like the scan endpoint.

### `GET /v1/tokens/{ca}/early-buyers`
First-buy evidence for the current top holders — for each, the earliest
detected incoming transfer of the mint is found by paging its wallet history.
Sorted oldest-buy first. **Auth: JWT or API key.** Rate-limited.

```json
[
  { "wallet": "...", "amount": 250.0, "signature": "...", "block_time": 1750000000 },
  { "wallet": "...", "amount": 90.5, "signature": "...", "block_time": 1750000100 }
]
```

### `GET /v1/tokens/{ca}/social`
Social media analysis for a token: DexScreener data (social links, 24h volume,
trending status, price change) + optional Twitter/X API v2 metrics (followers,
tweet volume) combined into a 0–100 social score with sentiment heuristic.
**Auth: JWT or API key.** Rate-limited. Cached for `SOCIAL_CACHE_TTL_SECONDS`.

```json
{
  "mint": "...",
  "social_links": { "twitter": "https://x.com/...", "website": "https://...", "telegram": null, "discord": null },
  "social_score": 55,
  "metrics": {
    "dex_trending": true,
    "dex_volume_24h": 10000.0,
    "dex_price_change_24h": 5.0,
    "twitter_followers": 2000,
    "twitter_statuses_7d": 15,
    "social_sentiment": "positive"
  },
  "as_of": "2026-08-20T00:00:00Z"
}
```

Social score weights: DexScreener trending (+30), volume spike (+20),
Twitter followers >1000 (+15), Twitter activity >10 tweets/7d (+15),
social links present (+10 each, max +20). Capped at 100.

### `GET /v1/tokens/{ca}/clusters`
Detect coordinated wallet clusters among top holders using three signals:
- **Shared funding** — wallets funded by the same SOL source
- **Coordinated timing** — first buys within ±60 seconds
- **Overlapping holdings** — Jaccard similarity ≥ 0.5 on held mints

Signals are merged via union-find. Each cluster reports confidence (0–1) based
on how many signals matched. **Auth: JWT or API key.** Rate-limited.

```json
{
  "mint": "...",
  "clusters": [
    { "id": "abc123", "wallets": ["w1...", "w2..."],
      "signals": ["shared_funding", "coordinated_timing"], "confidence": 0.67 }
  ],
  "total_wallets_analyzed": 20,
  "as_of": "2026-08-20T00:00:00Z"
}
```

### `GET /v1/tokens/{ca}/intelligence`
AI-written analysis of the token — the "ORVIX AI" layer. The scan +
accumulation results are dispatched as a chat job to an ORVX GPU node
(`INTEL_AI_MODEL`), which returns a JSON summary: `narrative` (emerging market
picture), `risk_flags`, and `watch_next`. Results are cached; the job row is
recorded (`is_mock=false`) so network stats reflect the real compute served.
**Auth: JWT or API key.** Returns `503 no_chat_provider` when no node serves
the model, `503 capacity_exhausted` when all are busy.

```json
{
  "mint": "...",
  "model": "qwen-2.5-7b",
  "analysis": {
    "narrative": "...", "risk_flags": ["..."], "watch_next": "...",
    "verdict": "hold", "reasons": ["...", "..."],
    "holder_count": 18, "top10_share": 0.62, "risk_score": 45
  },
  "generated_at": "2026-08-20T00:00:00Z",
  "latency_ms": 4321,
  "node_id": "..."
}
```

### `GET /v1/wallets/{wallet}?mint=<ca>`
Wallet analysis: token holdings (capped at `MAX_TOKEN_ACCOUNTS_PER_WALLET`),
recent activity (capped at `MAX_WALLET_TX_PARSE` parsed txs), and — when
`mint` is given — buy/inflow history for that mint. **Auth: JWT or API key.**

```json
{
  "wallet": "...",
  "holdings": [{ "mint": "...", "ui_amount": 12.5, "symbol": "ORVX", "name": "Orvix" }],
  "recent_activity": [
    { "signature": "...", "slot": 320000000, "timestamp": 1750000000,
      "memo": null, "transfers": [{ "mint": "...", "ui_amount": 1.0, "source": "...", "destination": "..." }] }
  ],
  "buy_history": [{ "signature": "...", "amount": 100.0, "timestamp": 1750000000, "counterparty": "..." }],
  "analyzed_at": "2026-08-20T00:00:00Z"
}
```

### `POST /v1/agents/monitors`
Create a monitoring agent. **Auth: JWT.**
Body: `{ "name", "target_type": "token"|"wallet", "target_address", "conditions":
[{...}], "webhook_url"?, "interval_minutes"?, "is_active"? }`.

Supported conditions (validated per target type):
- `token` — `{"type":"accumulation_score","gte":70}`, `{"type":"price_drop_pct","gte":10}`, `{"type":"large_transfer","min_ui_amount":1000}`
- `wallet` — `{"type":"new_activity"}`, `{"type":"large_inflow","min_ui_amount":5000}`

A `price_drop_pct` monitor snapshots the current price as its baseline at
creation. Evaluation runs in the background worker (`ENABLE_MONITOR_WORKER`).
Alerts are deduplicated per day (score/price conditions) or per on-chain
signature (transfer/activity conditions).

```json
{
  "id": "...", "name": "my-monitor", "target_type": "token",
  "target_address": "...",
  "conditions": [{ "type": "accumulation_score", "gte": 70 }],
  "webhook_url": "https://example.com/hook", "is_active": true,
  "interval_minutes": 30, "baseline_price_usdc": null,
  "last_checked_at": "2026-08-20T00:00:00Z", "created_at": "2026-08-20T00:00:00Z"
}
```

Alert event shape (`GET /v1/agents/alerts`, `GET /v1/agents/monitors/{id}/alerts`):

```json
{
  "id": "...", "monitor_id": "...", "condition_type": "accumulation_score",
  "message": "Accumulation score 85 ... (threshold 70)",
  "payload": { "score": 85, "label": "strong" },
  "occurred_at": "2026-08-20T00:00:00Z"
}
```

### `GET /v1/agents/monitors`
List the current user's monitors, newest first. **Auth: JWT.**

### `GET /v1/agents/monitors/{id}`
Get one monitor (owner only; 404 for other users' monitors). **Auth: JWT.**

### `PATCH /v1/agents/monitors/{id}`
Update a monitor (owner only). All fields optional — only the provided ones
change: `{ "name"?, "conditions"?, "webhook_url"?, "is_active"?,
"interval_minutes"?, "reset_baseline"? }`. `reset_baseline: true` re-snapshots
the baseline price of a token monitor with a `price_drop_pct` condition to the
current market price. New conditions are validated against the monitor's target
type. **Auth: JWT.**

### `DELETE /v1/agents/monitors/{id}`
Delete a monitor; its alert events cascade. **Auth: JWT.** Returns `204`.

### `GET /v1/agents/alerts`
All of the current user's alert events across every monitor, newest first.
**Auth: JWT.** Query params: `limit` (default 50, max 200), `offset`.

### `GET /v1/agents/monitors/{id}/alerts`
Alert events for one monitor, newest first (owner only). **Auth: JWT.**
Paginated with `limit`/`offset` (defaults 50/0).

### `POST /v1/agents/monitors/{id}/test`
Send a sample alert payload to the monitor's webhook (no event row is written).
**Auth: JWT.** Returns `{ "ok", "status_code"?, "error"? }`.

When a monitor has a `webhook_url`, each alert is POSTed to it as JSON
(`{ event_id, monitor_id, condition_type, message, payload, occurred_at }`)
with exponential backoff (`WEBHOOK_RETRY_BASE_SECONDS`, capped at
`WEBHOOK_MAX_ATTEMPTS`). When `WEBHOOK_SIGNING_SECRET` is set, every delivery
carries an `X-Orvix-Signature` header — the hex HMAC-SHA256 of the raw JSON
body, sorted keys, compact separators — so receivers can authenticate the
sender.

### `POST /v1/admin/intel/holder-snapshot?mint=<ca>`
Manually refresh the holder snapshot for a mint from
`TOKEN_WHALE_WATCHLIST_JSON` balances (ranked top holders + `top10_share`),
merging it into the cached token scan so accumulation scoring picks it up
immediately. **Auth: `X-Admin-Key`.** Returns the snapshot
(`{ total_holders, top_holders: [{wallet, balance}], top10_share, as_of }`).
`400 invalid_request` when the watchlist is empty or the mint is invalid.

```bash
curl -X POST "https://orvix.network/v1/admin/intel/holder-snapshot?mint=<ca>" \
  -H "X-Admin-Key: <ADMIN_API_KEY>"
```

```json
{
  "total_holders": 5,
  "top_holders": [{ "wallet": "...", "balance": 123.0 }],
  "top10_share": 0.42,
  "as_of": "2026-08-20T00:00:00Z"
}
```

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
  "models": { "chat": 3, "image": 2, "chat_available": 1, "image_available": 1 },
  "generated_at": "2026-07-26T10:00:00Z"
}
```

Anything the live websocket registry can answer is read fresh on every call:
`nodes.online`, the per-status counts (`ready`/`busy`/`draining`/`offline`), and
`models.*_available`. The rest — registration totals, GPU breakdown, job and
token volume — is a database aggregate cached for `NETWORK_STATS_CACHE_SECONDS`
(default 30), so `generated_at` is the snapshot time rather than the request
time. `avg_latency_ms` is `null` when there were no requests in the window.

`models.chat`/`models.image` count the **catalog**; `chat_available` and
`image_available` count what a connected node is actually running. The two differ
whenever the catalog offers a model nobody serves, which is the case a client
needs to see before it picks one.

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

> `POST /v1/auth/me` returns the caller's user record, including
> **`is_provider`** — the flag the dashboard needs to decide whether to offer
> registration or the provider view. It cannot be probed any other way: the only
> endpoint gated by it queues a payout.

### `POST /v1/provider/register`
Register the current account as a provider. Returns `provider_id` and
`node_secret` — the pair `orvix-node join` asks for. The secret is shown once
and stored only as a hash, so a lost secret is rotated, not recovered.
**Requires a stake of at least 2,000,000 ORVX** — returns `400 insufficient_stake`
otherwise. Stake first via `POST /v1/staking/stake-intent`. The stake gate is
off during the alpha (`REQUIRE_STAKE_FOR_PROVIDER=false`).

```json
{ "provider_id": "…", "node_secret": "…" }
```

### `POST /v1/provider/regenerate-secret`
Rotate the provider's node secret. Returns the same pair. Requires a registered
provider — returns `403 not_a_provider` otherwise, since the hash it writes is
the credential a node authenticates with. The old secret stops working
immediately, so any node still running on it drops at its next reconnect and
needs `orvix-node join --force`.

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
Request a withdrawal of accumulated earnings. Requires a registered provider —
returns `403 not_a_provider` otherwise. Minimum `MIN_WITHDRAW_AMOUNT_USDC`
(1 USDC); `402 insufficient_balance` when it exceeds `available_to_withdraw`.

```json
{ "withdrawal_id": "…", "status": "queued",
  "estimated_completion": "picked up by the payout worker within ~5 min, then confirmed on-chain",
  "requires_manual_approval": false }
```

`estimated_completion` is descriptive, not a guarantee. Above
`AUTO_APPROVE_MAX_USDC` the request is flagged for manual review and
`requires_manual_approval` is `true` — **nothing drains that case
automatically**, so no ETA is given. A withdrawal queued while the payout wallet
is short of USDC fails before broadcast and the amount is refunded to
`available_to_withdraw`.

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
