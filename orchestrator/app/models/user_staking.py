"""Pydantic models for non-custodial user staking endpoints."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class UserStakeRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="ORVX amount to stake (whole tokens)")
    lock_days: int = Field(7, ge=3, le=14, description="Lock period in days (3/7/14)")


class UserUnstakeRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="ORVX amount to unstake (whole tokens)")


class StakeTransactionResponse(BaseModel):
    """An unsigned transaction the user signs in their wallet."""

    transaction: str = Field(description="Hex-encoded unsigned serialized transaction")
    blockhash: str
    vault_address: str
    stake_account: str
    program_id: str


class SubmitTransactionRequest(BaseModel):
    transaction: str = Field(description="Hex-encoded signed transaction from the user's wallet")


class SubmitTransactionResponse(BaseModel):
    signature: str = Field(description="On-chain transaction signature after broadcast")


class UserStakeStatusResponse(BaseModel):
    wallet: str
    staked_orvx: str
    stake_locked_until: Optional[str] = None
    created_at: Optional[str] = None
    tier: str
    next_tier: Optional[dict] = None
