# API Reference

> 📝 Full interactive API docs are coming soon. The source of truth is the
> FastAPI auto-generated docs at `/docs` when the orchestrator is running
> (e.g. `http://localhost:8000/docs`). This page summarizes the current
> endpoints.

All endpoints are served under the `/v1` prefix. There are two kinds of
authentication:

- **JWT** — obtained by signing a wallet challenge. Used for account-level
  actions (API keys, billing, provider management).
- **API key** — a `orvx_sk_...` bearer token. Used for inference requests.

---

## Authentication

### `GET /v1/auth/challenge?wallet=<address>`
Get a challenge string to sign with your Solana wallet. No auth required.

```bash
curl "https://api.orvix.xyz/v1/auth/challenge?wallet=YOUR_WALLET_ADDRESS"
```

```json
{ "challenge": "Sign this message to authenticate with Orvix: <nonce>" }
```

### `POST /v1/auth/verify`
Verify the signed challenge and receive a JWT. No auth required.

```bash
curl -X POST https://api.orvix.xyz/v1/auth/verify \
  -H "Content-Type: application/json" \
  -d '{ "wallet": "YOUR_WALLET_ADDRESS", "signature": "BASE58_SIGNATURE" }'
```

```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

### `POST /v1/auth/me`
Return the current authenticated user. **Auth: JWT.**

```bash
curl -X POST https://api.orvix.xyz/v1/auth/me \
  -H "Authorization: Bearer <JWT>"
```

---

## API Keys

All require **Auth: JWT.**

### `POST /v1/api-keys`
Create a new API key. The full key is returned **once** — store it securely.

```bash
curl -X POST https://api.orvix.xyz/v1/api-keys \
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
curl https://api.orvix.xyz/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "Hello, Orvix!"}]
  }'
```

> Note: inference currently returns a mock response. Real GPU-backed inference
> (vLLM) is the next milestone — see [CHANGELOG.md](../CHANGELOG.md).

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

## Provider

All require **Auth: JWT.**

### `POST /v1/provider/register`
Register the current account as a provider. Returns a node secret used by the
node agent to authenticate.

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
