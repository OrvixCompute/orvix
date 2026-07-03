"""Account endpoints: stake-based tier info (JWT-authenticated)."""

from decimal import Decimal

from fastapi import APIRouter, Depends
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.logger import logger
from app.models.billing import TierResponse
from app.services import quota_service, tier_service
from app.services.holder import holder_service

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


@router.get("/quota")
async def get_quota(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Current chat + image quota status for the authenticated wallet."""
    wallet = current_user["wallet_address"]
    is_holder, balance = await holder_service.get_holder_status(db, wallet)
    status = quota_service.quota_status(db, wallet, is_holder, balance)

    # Recent images (most recent first) so the user can see what they generated
    # before the 24h auto-delete removes them.
    try:
        recent = (
            db.table("image_jobs")
            .select("image_url,created_at,expires_at")
            .eq("user_id", current_user["id"])
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        status["image"]["generated_images_last_24h"] = [
            {"url": r["image_url"], "created_at": r["created_at"], "expires_at": r["expires_at"]}
            for r in (recent.data or [])
        ]
    except Exception as exc:  # noqa: BLE001 — recent-images list is best-effort
        logger.warning("Failed to load recent images for {}: {}", wallet, exc)
        status["image"]["generated_images_last_24h"] = []

    return status
