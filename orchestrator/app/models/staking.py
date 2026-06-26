"""Pydantic request/response models for the staking endpoints."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


# --- Requests --------------------------------------------------------------
class StakeIntentRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="ORVX amount the user intends to stake")


class UnstakeRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="ORVX amount to unstake")
    destination_wallet: Optional[str] = Field(
        None, description="Where to send the unstaked ORVX (defaults to the user's wallet)"
    )


# --- Responses -------------------------------------------------------------
class StakeIntentResponse(BaseModel):
    intent_id: str
    treasury_address: str
    memo: str
    amount: Decimal
    expires_at: datetime
    qr_data: str


class UnstakeResponse(BaseModel):
    withdrawal_id: str
    status: str
    amount: Decimal


class NextTierInfo(BaseModel):
    name: str
    required_stake: str
    additional_needed: str


class StakeHistoryItem(BaseModel):
    type: str
    amount: str
    reason: Optional[str]
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "StakeHistoryItem":
        return cls(
            type=row["type"],
            amount=str(row["amount"]),
            reason=row.get("reason"),
            created_at=row["created_at"],
        )


class StakingStatusResponse(BaseModel):
    staked_orvx: str
    stake_locked_until: Optional[datetime]
    tier: str
    next_tier: Optional[NextTierInfo]
    history: list[StakeHistoryItem]


class BuybackEventInfo(BaseModel):
    usdc_spent: str
    orvx_received: str
    price: str
    solana_signature: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "BuybackEventInfo":
        return cls(
            usdc_spent=str(row["usdc_spent"]),
            orvx_received=str(row["orvx_received"]),
            price=str(row["execution_price_usdc_per_orvx"]),
            solana_signature=row["solana_signature"],
            created_at=row["created_at"],
        )


class BurnEventInfo(BaseModel):
    orvx_burned: str
    solana_signature: str
    period_start: datetime
    period_end: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "BurnEventInfo":
        return cls(
            orvx_burned=str(row["orvx_burned"]),
            solana_signature=row["solana_signature"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            created_at=row["created_at"],
        )


class NetworkStats(BaseModel):
    total_staked: str
    total_providers: int
    buyback_budget_usdc: str
    orvx_held_for_burn: str
    total_orvx_burned: str
    total_orvx_bought: str
    last_buyback_at: Optional[datetime]
    last_burn_at: Optional[datetime]
