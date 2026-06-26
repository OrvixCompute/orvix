"""Staking endpoints.

Authenticated (JWT):
    POST /v1/staking/stake-intent   create a memo'd intent for an ORVX deposit
    POST /v1/staking/unstake        debit stake and queue an ORVX payout
    GET  /v1/staking/status         current stake, tier, and history

Public (no auth) transparency:
    GET  /v1/staking/buyback-history
    GET  /v1/staking/burn-history
    GET  /v1/staking/network-stats
"""

from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.models.staking import (
    BurnEventInfo,
    BuybackEventInfo,
    NetworkStats,
    StakeIntentRequest,
    StakeIntentResponse,
    StakingStatusResponse,
    UnstakeRequest,
    UnstakeResponse,
)
from app.services.staking_service import StakingService

router = APIRouter(prefix="/v1/staking", tags=["staking"])


@router.post("/stake-intent", response_model=StakeIntentResponse)
async def create_stake_intent(
    body: StakeIntentRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Create a staking intent with a unique memo the user attaches to their ORVX transfer."""
    info = StakingService(db).create_stake_intent(current_user["id"], body.amount)
    return StakeIntentResponse(**info)


@router.post("/unstake", response_model=UnstakeResponse)
async def unstake(
    body: UnstakeRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Unstake ORVX (subject to the provider minimum) and queue a payout."""
    w = StakingService(db).unstake(current_user, body.amount, body.destination_wallet)
    return UnstakeResponse(withdrawal_id=str(w["id"]), status="queued", amount=body.amount)


@router.get("/status", response_model=StakingStatusResponse)
async def staking_status(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    return StakingStatusResponse(**StakingService(db).get_status(current_user["id"]))


@router.get("/buyback-history", response_model=list[BuybackEventInfo])
async def buyback_history(
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_supabase),
):
    return [BuybackEventInfo.from_row(r) for r in StakingService(db).buyback_history(limit)]


@router.get("/burn-history", response_model=list[BurnEventInfo])
async def burn_history(
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_supabase),
):
    return [BurnEventInfo.from_row(r) for r in StakingService(db).burn_history(limit)]


@router.get("/network-stats", response_model=NetworkStats)
async def network_stats(db: Client = Depends(get_supabase)):
    return NetworkStats(**StakingService(db).network_stats())
