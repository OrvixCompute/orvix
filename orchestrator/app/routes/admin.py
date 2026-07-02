"""Admin-only endpoints for buyback and burn, gated by the X-Admin-Key header.

These are an HTTP alternative to the CLI scripts (scripts/buyback.py,
scripts/burn.py) and share the same service logic and guardrails.
"""

from fastapi import APIRouter, Depends
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.dependencies import require_admin
from app.models.admin import (
    BurnExecuteRequest,
    BurnExecuteResponse,
    BuybackExecuteRequest,
    BuybackExecuteResponse,
)
from app.services import storage_service
from app.services.burn_service import BurnService
from app.services.buyback_service import BuybackService

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _executor() -> str:
    return settings.TREASURY_WALLET_ADDRESS or "admin-api"


@router.get("/feature-flags")
async def feature_flags():
    """Current runtime feature-flag state (admin-only)."""
    return {
        "require_stake_for_provider": settings.REQUIRE_STAKE_FOR_PROVIDER,
        "provider_min_stake_orvx": settings.PROVIDER_MIN_STAKE_ORVX,
        "buyback_stub": settings.BUYBACK_STUB,
        "burn_stub": settings.BURN_STUB,
        "payout_stub": settings.PAYOUT_STUB,
        "enable_payment_listener": settings.ENABLE_PAYMENT_LISTENER,
        "enable_payout_worker": settings.ENABLE_PAYOUT_WORKER,
        "orvx_mint_configured": bool(settings.ORVX_MINT_ADDRESS),
        "admin_api_key_set": bool(settings.ADMIN_API_KEY),
    }


@router.get("/buyback/status")
async def buyback_status(db: Client = Depends(get_supabase)):
    return BuybackService(db).status()


@router.post("/buyback/execute", response_model=BuybackExecuteResponse)
async def buyback_execute(
    body: BuybackExecuteRequest,
    db: Client = Depends(get_supabase),
):
    result = await BuybackService(db).execute(body.amount_usdc, body.slippage_bps, _executor())
    return BuybackExecuteResponse(**result)


@router.get("/burn/status")
async def burn_status(db: Client = Depends(get_supabase)):
    return BurnService(db).status()


@router.post("/burn/execute", response_model=BurnExecuteResponse)
async def burn_execute(
    body: BurnExecuteRequest,
    db: Client = Depends(get_supabase),
):
    result = await BurnService(db).execute(
        body.amount, body.period_start, body.period_end, _executor()
    )
    return BurnExecuteResponse(**result)


@router.get("/storage/stats")
async def storage_stats():
    """Image storage usage for disk monitoring (admin-only)."""
    data = storage_service.stats()
    data["cleanup_schedule"] = "hourly (orvix-image-cleanup.timer)"
    return data
