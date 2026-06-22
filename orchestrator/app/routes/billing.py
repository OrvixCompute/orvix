"""Billing endpoints: top-up intents, balance, transactions (JWT-authenticated)."""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_current_user
from app.models.billing import (
    BalanceResponse,
    TopupIntentInfo,
    TopupIntentRequest,
    TopupIntentResponse,
    TransactionInfo,
)
from app.services.billing_service import BillingService

router = APIRouter(prefix="/v1/billing", tags=["billing"])

INTENT_TTL_MINUTES = 30


@router.post("/topup-intent", response_model=TopupIntentResponse)
async def create_topup_intent(
    body: TopupIntentRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Create a top-up intent with a unique memo the user attaches to their transfer."""
    memo = f"orvx_{secrets.token_hex(6)}"  # 12 hex chars
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=INTENT_TTL_MINUTES)

    row = {
        "user_id": current_user["id"],
        "memo": memo,
        "expected_amount_usdc": float(body.expected_amount) if body.expected_amount else None,
        "status": "pending",
        "expires_at": expires_at.isoformat(),
    }
    res = db.table("topup_intents").insert(row).execute()
    intent = res.data[0]

    treasury = settings.TREASURY_WALLET_ADDRESS
    amount = body.expected_amount
    qr = f"solana:{treasury}?spl-token={settings.USDC_MINT_ADDRESS}"
    if amount:
        qr += f"&amount={amount}"
    qr += f"&memo={memo}"

    return TopupIntentResponse(
        id=str(intent["id"]),
        treasury_address=treasury,
        memo=memo,
        expected_amount=body.expected_amount,
        expires_at=expires_at,
        qr_data=qr,
    )


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    billing = BillingService(db)
    return BalanceResponse(**billing.get_balance(current_user["id"]))


@router.get("/transactions", response_model=list[TransactionInfo])
async def list_transactions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    res = (
        db.table("transactions")
        .select("*")
        .eq("user_id", current_user["id"])
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return [TransactionInfo.from_row(r) for r in (res.data or [])]


@router.get("/topup-intents", response_model=list[TopupIntentInfo])
async def list_topup_intents(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Return the user's still-pending, non-expired intents."""
    now_iso = datetime.now(timezone.utc).isoformat()
    res = (
        db.table("topup_intents")
        .select("*")
        .eq("user_id", current_user["id"])
        .eq("status", "pending")
        .gt("expires_at", now_iso)
        .order("created_at", desc=True)
        .execute()
    )
    return [TopupIntentInfo.from_row(r) for r in (res.data or [])]
