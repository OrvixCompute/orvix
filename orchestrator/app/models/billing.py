"""Pydantic models for billing: top-up intents, balance, transactions."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class TopupIntentRequest(BaseModel):
    expected_amount: Optional[Decimal] = Field(
        None, gt=0, description="Optional expected USDC amount for this top-up"
    )


class TopupIntentResponse(BaseModel):
    id: str
    treasury_address: str
    memo: str
    expected_amount: Optional[Decimal]
    expires_at: datetime
    qr_data: str


class TopupIntentInfo(BaseModel):
    id: str
    memo: str
    expected_amount: Optional[Decimal]
    status: str
    expires_at: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "TopupIntentInfo":
        amt = row.get("expected_amount_usdc")
        return cls(
            id=str(row["id"]),
            memo=row["memo"],
            expected_amount=Decimal(str(amt)) if amt is not None else None,
            status=row["status"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )


class BalanceResponse(BaseModel):
    balance_usdc: str
    tier: str


class TransactionInfo(BaseModel):
    id: str
    type: str
    amount: Decimal
    token: str
    solana_signature: Optional[str]
    status: str
    metadata: dict
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "TransactionInfo":
        return cls(
            id=str(row["id"]),
            type=row["type"],
            amount=Decimal(str(row["amount"])),
            token=row["token"],
            solana_signature=row.get("solana_signature"),
            status=row["status"],
            metadata=row.get("metadata") or {},
            created_at=row["created_at"],
        )
