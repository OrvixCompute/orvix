"""Pydantic models for admin buyback/burn endpoints."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class BuybackExecuteRequest(BaseModel):
    amount_usdc: Decimal = Field(..., gt=0, description="USDC to spend from the buyback budget")
    slippage_bps: int = Field(50, ge=0, le=10000, description="Max Jupiter slippage, basis points")


class BuybackExecuteResponse(BaseModel):
    buyback_id: Optional[str]
    usdc_spent: str
    orvx_received: str
    solana_signature: str


class BurnExecuteRequest(BaseModel):
    amount: Optional[Decimal] = Field(
        None, gt=0, description="ORVX to burn; defaults to all ORVX held for burn"
    )
    period_start: Optional[datetime] = Field(None, description="Start of the period this burn covers")
    period_end: Optional[datetime] = Field(None, description="End of the period this burn covers")


class BurnPeriod(BaseModel):
    period_start: datetime
    period_end: datetime


class BurnExecuteResponse(BaseModel):
    burn_id: Optional[str]
    orvx_burned: str
    solana_signature: str
    period: BurnPeriod
