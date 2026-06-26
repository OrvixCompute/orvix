"""Account endpoints: stake-based tier info (JWT-authenticated)."""

from decimal import Decimal

from fastapi import APIRouter, Depends
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.models.billing import TierResponse
from app.services import tier_service

router = APIRouter(prefix="/v1/account", tags=["account"])


@router.get("/tier", response_model=TierResponse)
async def get_tier(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Current tier, discount, and progress to the next tier — all stake-based."""
    staked = Decimal(str(current_user.get("staked_orvx", 0) or 0))
    tier = tier_service.tier_for_stake(staked)
    return TierResponse(
        tier=tier,
        staked_orvx=format(staked, "f"),
        discount_pct=tier_service.discount_pct_for_tier(tier),
        next_tier=tier_service.next_tier_info(staked),
    )
