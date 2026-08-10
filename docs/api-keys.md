# Using an API key

Everything on this page was exercised against `https://orvix.network` — the
requests, the responses, and the errors are what the live network actually
returns.

## Two tokens, and they are not interchangeable

Orvix has two bearer tokens, and sending the wrong one is the most common first
mistake:

| Token | Looks like | Obtained by | Used for |
|---|---|---|---|
| **JWT** | a signed session token | signing a challenge with your Solana wallet | account actions — creating keys, billing, provider management |
| **API key** | `orvx_sk_…` | `POST /v1/api-keys`, with a JWT | inference — chat and image generation |

You cannot create an API key with an API key. That is deliberate: minting
credentials is an account action, so it stays behind the wallet.

A few read-only endpoints (`GET /v1/account/quota`, `GET /v1/account/tier`)
accept **either**, so a program can check its own allowance without the wallet
flow.

## 1. Create a key

```bash
curl -X POST https://orvix.network/v1/api-keys \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app"}'
```

`201 Created`:

```json
{ "id": "…", "key": "orvx_sk_…", "prefix": "orvx_sk_Wtd", "name": "my-app",
  "created_at": "…" }
```

**`key` is returned once and never again** — only a hash is stored. If you lose
it, rotate; there is no recovery path.

Managing keys:

| Call | Effect |
|---|---|
| `GET /v1/api-keys` | List keys — metadata only, never the secret |
| `POST /v1/api-keys/{id}/rotate` | New secret for the same key record; the old one stops working |
| `DELETE /v1/api-keys/{id}` | Revoke, `204 No Content` |

Prefer one key per application. Revoking then becomes surgical rather than an
outage for everything you run.

## 2. Call the API

```bash
curl https://orvix.network/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "Hello, Orvix!"}]
  }'
```

Image generation, OpenAI DALL·E-compatible:

```bash
curl -X POST https://orvix.network/v1/images/generations \
  -H "Authorization: Bearer orvx_sk_..." \
  -H "Content-Type: application/json" \
  -d '{"model": "orvix-image-1", "prompt": "a fox in snow",
       "size": "1024x1024", "n": 1}'
```

The response carries a `url`; fetch it to get the PNG.

Responses include an `X-Orvix-Node` header naming the GPU node that served the
request — useful when you are reporting a bad result.

## 3. Or point an OpenAI client at it

The API is OpenAI-compatible, so existing clients work by changing two settings.

```python
from openai import OpenAI

client = OpenAI(api_key="orvx_sk_...", base_url="https://orvix.network/v1")

resp = client.chat.completions.create(
    model="qwen-2.5-7b",
    messages=[{"role": "user", "content": "Hello, Orvix!"}],
)
print(resp.choices[0].message.content)
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.ORVIX_API_KEY,
  baseURL: "https://orvix.network/v1",
});
```

Streaming works — pass `stream: true` and read the SSE chunks as usual.

## Which models can you actually call

The catalog lists more models than are being served at any moment, because a
model is only available while some node has it loaded. **Check before you
choose** — `GET /v1/models` is public and needs no key:

```bash
curl https://orvix.network/v1/models
```

Each entry carries an `available` flag. Calling an unavailable model returns
`503 no_chat_provider`, and retrying will not help — pick one with
`available: true`.

## Embeddings

`POST /v1/embeddings` speaks OpenAI's shape, so RAG stacks that expect an
embeddings endpoint work against the same base URL:

```python
client.embeddings.create(model="orvix-embed-1", input=["chunk one", "chunk two"])
```

Vectors return in input order and are L2-normalized. Free during the alpha, in
its own rate-limit bucket. It still needs a provider running the embedding
engine — check `available` on `orvix-embed-1` in `GET /v1/models` first, exactly
as you would for a chat model.

## Allowances

| Limit | Value |
|---|---|
| Free chat requests | 1000 per account, **lifetime** (not per day) |
| Free images | 50 per day |
| Rate limit | 60 requests/minute on bronze; higher tiers get more |

Past the free allowance, chat is metered per token and images per area, settled
in USDC from your balance. Staking ORVX raises your tier, which discounts every
charge — see [Tokenomics](./tokenomics.md#premium-access-tiers).

Check where you stand with `GET /v1/account/quota` (accepts an API key):

```json
{ "chat":  { "type": "free_tier",   "lifetime_free_used": 12, "lifetime_free_limit": 1000 },
  "image": { "type": "grace_daily", "used_today": 3, "daily_limit": 50 } }
```

## Errors worth handling

Every error uses one envelope. Render `message`; log `request_id` — it is what
makes a report traceable.

```json
{ "error": { "code": "rate_limit_exceeded", "message": "…", "request_id": "…" } }
```

| Status / code | Meaning | Retry? |
|---|---|---|
| `401` | Missing, malformed, or revoked key | No — fix the key |
| `429 rate_limit_exceeded` | Over your per-minute ceiling | Yes, after `retry_after_seconds` |
| `402 insufficient_balance` | Free allowance spent and balance is empty | No — top up |
| `503 capacity_exhausted` | Nodes serve this model but all are busy | **Yes** — honour `retry_after_seconds` |
| `503 no_chat_provider` / `no_image_provider` | Nobody is serving that model | **No** — switch models |
| `400 streaming_tools_unsupported` | `tools` combined with `stream: true` | No — tool calling is non-streaming only |

The two 503s look alike and mean opposite things. Retrying `no_chat_provider` in
a loop will never succeed; retrying `capacity_exhausted` usually will. Branch on
the code, not the status.

## Tool / function calling

`tools` and `tool_choice` are accepted in OpenAI's shape on
`POST /v1/chat/completions`. A tool-calling turn returns
`finish_reason: "tool_calls"` with `message.tool_calls` and
`message.content: null`, and `role: "tool"` results round-trip with their
`tool_call_id`.

**Non-streaming only.** Combining `tools` with `stream: true` is refused with
`400 streaming_tools_unsupported` rather than quietly streaming prose and
dropping the calls.

## Keeping the key safe

- It is a bearer token: anyone holding it spends your balance. Keep it in an
  environment variable or a secret store, never in client-side code or a repo.
- Rotate on suspicion — `POST /v1/api-keys/{id}/rotate` invalidates the old
  secret immediately.
- `GET /v1/api-keys` shows `last_used_at` per key, which is the cheapest way to
  spot one that is being used when it should not be, or one you can safely
  delete.

## See also

- [API Reference](./api-reference.md) — every endpoint
- [Getting Started](./getting-started.md) — the two paths through Orvix
- [FAQ](./faq.md)
