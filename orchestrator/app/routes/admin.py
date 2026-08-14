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
from app.services import treasury_health as treasury_health_service
from app.services.burn_service import BurnService
from app.services.buyback_service import BuybackService
from app.services.covenant_service import get_covenant_service
from app.services.hot_sweeper import hot_sweeper
from app.services.payments_overview import build_overview
from app.services.wallet import wallet_service

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _executor() -> str:
    return settings.TREASURY_WALLET_ADDRESS or "admin-api"


@router.get("/feature-flags")
async def feature_flags():
    """Current runtime feature-flag state (admin-only).

    Also reports the withdrawal economics. Those are plain settings rather than
    flags, but they are `.env`-only and read at request time, so without this
    there is no way to confirm from outside the box that an edit took effect —
    the alternative is SSHing in and importing `settings` by hand.
    """
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
        "min_withdraw_amount_usdc": settings.MIN_WITHDRAW_AMOUNT_USDC,
        "auto_approve_max_usdc": settings.AUTO_APPROVE_MAX_USDC,
        "max_withdrawals_per_day": settings.MAX_WITHDRAWALS_PER_DAY,
        "covenant_enable_attestation": settings.COVENANT_ENABLE_ATTESTATION,
        "covenant_min_reputation": settings.COVENANT_MIN_REPUTATION,
        "covenant_wallet_configured": bool(settings.COVENANT_PROVIDER_WALLET_ADDRESS),
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


# --- Treasury (cold/hot/payout) --------------------------------------------

@router.get("/treasury/balances")
async def treasury_balances(db: Client = Depends(get_supabase)):
    """Last-synced treasury balances from the DB (call /treasury/sync to refresh)."""
    res = db.table("treasury_wallets").select("*").execute()
    return {"wallets": res.data or []}


@router.get("/treasury/health")
async def treasury_health():
    """Live treasury balances checked against their floors.

    Reads the chain rather than the last-synced row: a monitor that can only
    see stale values cannot catch a balance falling while nobody is watching.
    """
    return await treasury_health_service.check()


@router.post("/treasury/sync")
async def treasury_sync(db: Client = Depends(get_supabase)):
    """Refresh on-chain balances into treasury_wallets and return them."""
    synced = await wallet_service.sync_balances(db)
    return {"synced": synced}


@router.post("/treasury/sweep-hot")
async def treasury_sweep_hot(db: Client = Depends(get_supabase)):
    """Manually trigger a hot->main sweep (stubbed unless TREASURY_SWEEP_STUB=false)."""
    return await hot_sweeper.run_once(db)


# --- Payment-flow monitoring -----------------------------------------------

@router.get("/payments/overview")
async def payments_overview(db: Client = Depends(get_supabase)):
    """Payment-flow snapshot: flags, treasury balances, deposits + withdrawals.

    Read-only aggregation for the activation dashboard. Treasury balances are the
    last-synced values — call POST /treasury/sync first to refresh from chain.
    """
    return build_overview(db)


# --- OpenCovenant trust (opt-in node attestation) ---------------------------

@router.get("/covenant/reputation")
async def covenant_reputation(wallet: str = ""):
    """Live OpenCovenant reputation check for a Solana wallet (admin-only).

    Read-only call to the Covenant Guard MCP (https://mcp.opencovenant.org/mcp).
    Returns the raw tool result; a failed check (network error, timeout, or an
    isError from the server) is reported as ``{"ok": false, "error": ...}`` and
    never raises — matching the fail-soft registration path.
    """
    target = wallet or settings.COVENANT_PROVIDER_WALLET_ADDRESS
    if not target:
        return {"ok": False, "error": "no wallet given and COVENANT_PROVIDER_WALLET_ADDRESS is empty"}
    result = await get_covenant_service().check_reputation(target)
    if not result.ok:
        return {"ok": False, "error": result.error, "wallet": target}
    rep = result.reputation
    return {
        "ok": True,
        "wallet": target,
        "score": rep.score,
        "tier": rep.tier,
        "settled_jobs": rep.settled_jobs,
        "distinct_counterparties": rep.distinct_counterparties,
        "volume_micro_usdc": rep.volume_micro_usdc,
        "source_fee_payer": rep.source_fee_payer,
    }
