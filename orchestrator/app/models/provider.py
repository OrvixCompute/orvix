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
    estimated_completion: str


class ProviderRegisterRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)


class SecretResponse(BaseModel):
    # `orvix-node join` needs BOTH values, but this response used to carry only
    # the secret — the id is `users.id`, handed back at login and nowhere else.
    # A provider following the docs got one of the two credentials and stalled.
    provider_id: str
    node_secret: str
