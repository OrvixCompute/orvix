"""Ed25519 signing of inference receipts (the "signed verdict" per request).

Every inference/image request that a node serves gets a signed receipt proving
who served it and what usage was billed. Customers can verify the signature
offline: fetch the public key from ``GET /v1/verify/public-key`` and check the
signature over the canonical payload with any Ed25519 library.

Design notes:
- The private key lives only in the environment (``RECEIPT_SIGNING_KEY``, a
  base64 32-byte seed). It is never exposed over HTTP.
- When no key is configured, signing is disabled: no header, no failures.
  Receipts are a trust feature, not a hard dependency.
- The signing key is loaded lazily so importing this module never fails and
  tests without a configured key keep working.

The claim being signed is the receipt itself (canonical JSON of the fields a
customer cares about: who served the request, what was billed, and when).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.config import settings
from app.logger import logger

# Canonical claim fields, in order. json.dumps(..., sort_keys=True) keeps the
# signature stable across Python versions.
_RECEIPT_FIELDS = (
    "request_id",
    "node_id",
    "provider_id",
    "prompt_tokens",
    "completion_tokens",
    "model",
    "served_at",
)


def _load_private_key() -> Optional[Ed25519PrivateKey]:
    """Decode RECEIPT_SIGNING_KEY (base64 32-byte seed) into an Ed25519 key."""
    raw = (settings.RECEIPT_SIGNING_KEY or "").strip()
    if not raw:
        return None
    try:
        seed = base64.b64decode(raw)
        if len(seed) != 32:
            logger.warning(
                "RECEIPT_SIGNING_KEY must be a base64 32-byte seed (got {} bytes); receipts disabled",
                len(seed),
            )
            return None
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:  # noqa: BLE001 — a bad key must never crash the app
        logger.warning("Failed to load RECEIPT_SIGNING_KEY: {}; receipts disabled", exc)
        return None


_private_key: Optional[Ed25519PrivateKey] = None
_public_key_b64: Optional[str] = None
_loaded = False


def _ensure_loaded() -> None:
    global _private_key, _public_key_b64, _loaded
    if _loaded:
        return
    _loaded = True
    key = _load_private_key()
    if key is None:
        # Re-loading with a disabled/cleared key must also clear any previously
        # cached public key, not just skip the load.
        _private_key = None
        _public_key_b64 = None
        return
    _private_key = key
    _public_key_b64 = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")


def signing_enabled() -> bool:
    """True when a signing key is configured and usable."""
    _ensure_loaded()
    return _private_key is not None


def public_key_b64() -> Optional[str]:
    """Base64 (raw 32-byte) Ed25519 public key, or None when signing is off."""
    _ensure_loaded()
    return _public_key_b64


def canonical_payload(fields: dict[str, Any]) -> bytes:
    """Serialize the receipt fields into the canonical bytes that get signed."""
    claim = {k: fields[k] for k in _RECEIPT_FIELDS if k in fields}
    return json.dumps(claim, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_receipt(
    *,
    request_id: str,
    node_id: str,
    provider_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
) -> Optional[dict]:
    """Build and sign a receipt for one served request.

    Returns None when signing is disabled. When signing is enabled this never
    raises: a signing failure is logged and treated as "no receipt" so a bad
    key cannot break inference.
    """
    _ensure_loaded()
    if _private_key is None:
        return None
    fields = {
        "request_id": request_id,
        "node_id": node_id,
        "provider_id": provider_id,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "model": model,
        "served_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        payload = canonical_payload(fields)
        signature = base64.b64encode(_private_key.sign(payload)).decode("ascii")
        return {
            "payload": fields,
            "signature": signature,
            "public_key": _public_key_b64,
            "algorithm": "ed25519",
        }
    except Exception as exc:  # noqa: BLE001 — signing must never break inference
        logger.warning("Failed to sign receipt for request {}: {}", request_id, exc)
        return None


def verify_receipt(receipt: dict) -> bool:
    """Verify a receipt's Ed25519 signature against its canonical payload.

    The public key is taken from the receipt itself, so this proves the
    receipt was signed by whoever holds the matching private key — the same
    key advertised by ``GET /v1/verify/public-key``.
    """
    payload = receipt.get("payload")
    signature_b64 = receipt.get("signature")
    pub_b64 = receipt.get("public_key")
    if not isinstance(payload, dict) or not signature_b64 or not pub_b64:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        signature = base64.b64decode(signature_b64)
        pub.verify(signature, canonical_payload(payload))
        return True
    except Exception:  # noqa: BLE001 — any failure means "not verified"
        return False


def receipt_digest(receipt: dict) -> str:
    """Stable sha256 digest of the receipt payload (for receipt IDs)."""
    payload = receipt.get("payload") or {}
    return hashlib.sha256(canonical_payload(payload)).hexdigest()[:16]
