"""Pydantic models for the wallet-authentication flow."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ChallengeRequest(BaseModel):
    """Query parameters for GET /v1/auth/challenge are passed directly, but this
    documents the expected shape."""

    wallet: str = Field(..., description="Solana wallet address (base58)")


class ChallengeResponse(BaseModel):
    message: str
    nonce: str
    expires_at: datetime


class VerifyRequest(BaseModel):
    wallet: str = Field(..., description="Solana wallet address (base58)")
    message: str = Field(..., description="The exact challenge message that was signed")
    signature: str = Field(..., description="base58-encoded ed25519 signature")


class User(BaseModel):
    """Public-facing representation of a user."""

    id: str
    wallet: str
    tier: str
    balance_usdc: Decimal
    # The dashboard has to know whether this account is already a provider in
    # order to decide what to show. It used to be underivable: the flag lived
    # only on the user row, and the one endpoint gated by it (POST /withdraw)
    # queues a payout, so it could not be used as a probe. The UI was left
    # inferring the answer from whether nodes or earnings happened to exist.
    is_provider: bool = False

    @classmethod
    def from_row(cls, row: dict) -> "User":
        return cls(
            id=str(row["id"]),
            wallet=row["wallet_address"],
            tier=row["tier"],
            balance_usdc=Decimal(str(row.get("balance_usdc", 0))),
            is_provider=bool(row.get("is_provider", False)),
        )


class VerifyResponse(BaseModel):
    token: str
    user: User
