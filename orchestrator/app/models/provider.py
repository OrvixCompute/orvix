"""Pydantic request/response models for provider endpoints."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class RenameNodeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)


class WithdrawRequest(BaseModel):
    amount: Decimal = Field(..., gt=0)
    destination_wallet: Optional[str] = Field(
        None, description="Defaults to the user's wallet if omitted"
    )


class WithdrawResponse(BaseModel):
    withdrawal_id: str
    status: str
    # Was the hardcoded string "< 1 hour" on every response, which was false in
    # the one case that matters: a withdrawal above AUTO_APPROVE_MAX_USDC is
    # flagged for manual review, and no approval endpoint exists — so it sits
    # queued indefinitely while the caller was told to expect it within the hour.
    # Now describes the actual next step, derived from the worker's own interval.
    estimated_completion: str
    # Lets a client say "this needs a human" instead of showing a countdown that
    # will never run out.
    requires_manual_approval: bool = False


class ProviderRegisterRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)


class SecretResponse(BaseModel):
    # `orvix-node join` needs BOTH values, but this response used to carry only
    # the secret — the id is `users.id`, handed back at login and nowhere else.
    # A provider following the docs got one of the two credentials and stalled.
    provider_id: str
    node_secret: str
