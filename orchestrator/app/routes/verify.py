"""Public verification endpoints for signed inference receipts.

Customers who receive an ``X-Orvix-Receipt`` header (chat) or
``X-Orvix-Receipts`` (images) can check it here, or offline against the
advertised public key. Nothing on this router is authenticated — the whole
point is that a third party can verify without Orvix's blessing.
"""

from fastapi import APIRouter

from app.services import receipt_signer

router = APIRouter(prefix="/v1/verify", tags=["verify"])


@router.get("/public-key")
async def public_key():
    """The Ed25519 public key used to sign inference receipts.

    Returns ``{"algorithm": "ed25519", "public_key": "<base64>"}`` when a
    signing key is configured, or ``{"algorithm": "ed25519", "public_key": null}``
    when receipts are disabled.
    """
    return {"algorithm": "ed25519", "public_key": receipt_signer.public_key_b64()}


@router.post("/receipt")
async def verify_receipt(body: dict):
    """Verify a signed receipt.

    POST the receipt object (as returned in the ``X-Orvix-Receipt`` header,
    after JSON-decoding) and get back ``{"valid": true}`` or ``{"valid": false}``.
    """
    return {"valid": receipt_signer.verify_receipt(body)}
