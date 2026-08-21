"""Wallet analysis endpoint.

Authenticated with EITHER a wallet JWT or an `orvx_sk_` API key. Returns token
holdings, recent activity, and (when ?mint= is given) buy/inflow history for
that mint.
"""

from fastapi import APIRouter, Depends, Query
from solders.pubkey import Pubkey
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user_flexible
from app.exceptions import ValidationError
from app.models.intel import WalletAnalysisResponse
from app.services import rate_limit_service, tier_service, token_intel

router = APIRouter(prefix="/v1/wallets", tags=["wallets"])


@router.get("/{wallet}", response_model=WalletAnalysisResponse)
async def get_wallet_analysis(
    wallet: str,
    mint: str | None = Query(None, description="Restrict buy history to this mint"),
    current_user: dict = Depends(get_current_user_flexible),
    db: Client = Depends(get_supabase),
):
    """Holdings, recent activity, and optional per-mint buy history."""
    try:
        Pubkey.from_string(wallet)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Invalid Solana wallet address") from exc
    if mint:
        try:
            Pubkey.from_string(mint)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("Invalid Solana mint address") from exc
    tier = tier_service.tier_for_stake(current_user.get("staked_orvx"))
    rate_limit_service.check_user(current_user["id"], tier, bucket="intel")
    return await token_intel.analyze_wallet(db, wallet, mint=mint)
