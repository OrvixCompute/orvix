# Verifiable inference receipts (signed verdicts)

Every request served by a real node gets a **signed receipt** — a cryptographic
proof of *who served the request* and *what usage was billed*. This is the
first concrete step from the README's "provider trust is currently
operational, not enforced" toward something a customer can check themselves.

## Where the receipt appears

| Endpoint | Header | Contents |
|---|---|---|
| `POST /v1/chat/completions` (non-streaming) | `X-Orvix-Receipt` | one receipt object (JSON) |
| `POST /v1/chat/completions` (streaming) | — | a final SSE event `data: {"receipt": {...}}` after `[DONE]` |
| `POST /v1/images/generations` | `X-Orvix-Receipts` | JSON array, one receipt per image |

Receipts are only emitted when the operator has configured a signing key
(`RECEIPT_SIGNING_KEY`). Without it the headers are simply absent — inference
never fails because of receipts.

## What a receipt contains

```json
{
  "payload": {
    "request_id": "…",
    "node_id": "…",
    "provider_id": "…",
    "prompt_tokens": 42,
    "completion_tokens": 17,
    "model": "qwen-2.5-7b",
    "served_at": "2026-08-27T12:00:00+00:00"
  },
  "signature": "<base64 ed25519 signature>",
  "public_key": "<base64 raw ed25519 public key>",
  "algorithm": "ed25519"
}
```

The signature covers the canonical JSON of `payload` (sorted keys, compact
separators), so it can be recomputed by any Ed25519 library.

## Verifying a receipt

**Online** — POST the decoded receipt object back:

```bash
curl -X POST https://orvix.network/v1/verify/receipt \
  -H "Content-Type: application/json" \
  -d @receipt.json
# → {"valid": true}
```

**Offline** — fetch the advertised public key once and verify locally:

```bash
curl https://orvix.network/v1/verify/public-key
# → {"algorithm": "ed25519", "public_key": "<base64>"}
```

```python
import base64, hashlib, json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

def verify(receipt: dict) -> bool:
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(receipt["public_key"])
    )
    try:
        pub.verify(base64.b64decode(receipt["signature"]), canonical(receipt["payload"]))
        return True
    except Exception:
        return False
```

The public key is self-advertised by the receipt *and* by `/v1/verify/public-key`;
the two should match. A mismatched key is a warning sign, not a verification.

## What this does and does not prove

**Proves:** the response was produced by Orvix's orchestrator for the named
node/provider, and the token usage in the receipt is exactly what was billed
(`X-Orvix-Cost` is derived from these same counts).

**Does not prove:** that the node itself behaved honestly — output
verification, slashing, and dispute resolution are still on the testnet
roadmap (see the README's alpha disclosures).

## Operator setup

```bash
# generate once
python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
# → e.g. "x7Kf…=="
```

Set `RECEIPT_SIGNING_KEY=<that value>` in the orchestrator's environment. Keep
it secret — anyone holding it can forge receipts. Rotating it invalidates all
previously issued receipts, so treat it as long-lived.

## Related

- [API keys](./api-keys.md) — using the API, including the receipt headers
- [Provider guide](./provider-guide.md) — node onboarding
- [OpenCovenant integration](./covenant.md) — wallet reputation attestation
  at node registration (`covenant_reputation` / `covenant_verify` MCP tools)
