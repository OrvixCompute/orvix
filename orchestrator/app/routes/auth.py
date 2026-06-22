"""Wallet authentication endpoints."""

from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.models.auth import (
    ChallengeResponse,
    User,
    VerifyRequest,
    VerifyResponse,
)
from app.services.auth_service import auth_service

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.get("/challenge", response_model=ChallengeResponse)
async def challenge(wallet: str = Query(..., description="Solana wallet address")):
    """Issue a nonce + message for the wallet to sign."""
    return auth_service.create_challenge(wallet)


@router.post("/verify", response_model=VerifyResponse)
async def verify(body: VerifyRequest, db: Client = Depends(get_supabase)):
    """Verify a signed challenge, upsert the user, and return a JWT."""
    auth_service.verify_signature(body.wallet, body.message, body.signature)

    # Upsert the user by wallet_address.
    existing = (
        db.table("users").select("*").eq("wallet_address", body.wallet).limit(1).execute()
    )
    if existing.data:
        user = existing.data[0]
    else:
        inserted = db.table("users").insert({"wallet_address": body.wallet}).execute()
        user = inserted.data[0]

    token = auth_service.create_jwt(user)
    return VerifyResponse(token=token, user=User.from_row(user))


@router.post("/me", response_model=User)
async def me(current_user: dict = Depends(get_current_user)):
    """Return the user identified by the bearer JWT."""
    return User.from_row(current_user)
