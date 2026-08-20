"""Non-custodial user staking endpoints (Anchor program on Solana).

These build unsigned transactions for the user's wallet to sign, and read
stake state directly from the chain. The feature is opt-in: when
USER_STAKING_PROGRAM_ID is unset, all routes return 404.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.dependencies import get_current_user
from app.models.user_staking import (
    StakeTransactionResponse,
    SubmitTransactionRequest,
    SubmitTransactionResponse,
    UserStakeRequest,
    UserStakeStatusResponse,
    UserUnstakeRequest,
)
from app.services.user_staking import user_staking_service

router = APIRouter(prefix="/v1/staking/user", tags=["staking"])


def _require_enabled() -> None:
    if not settings.USER_STAKING_PROGRAM_ID:
        raise HTTPException(status_code=404, detail="user staking is not configured")


@router.post("/stake", response_model=StakeTransactionResponse)
async def user_stake(
    body: UserStakeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Build an unsigned `stake` transaction for the user's wallet to sign."""
    _require_enabled()
    wallet = current_user.get("wallet_address")
    if not wallet:
        raise HTTPException(status_code=400, detail="account has no wallet_address")
    return await user_staking_service.build_stake_transaction(
        wallet, body.amount, body.lock_days
    )


@router.post("/unstake", response_model=StakeTransactionResponse)
async def user_unstake(
    body: UserUnstakeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Build an unsigned `unstake` transaction for the user's wallet to sign."""
    _require_enabled()
    wallet = current_user.get("wallet_address")
    if not wallet:
        raise HTTPException(status_code=400, detail="account has no wallet_address")
    return await user_staking_service.build_unstake_transaction(wallet, body.amount)


@router.post("/submit", response_model=SubmitTransactionResponse)
async def user_submit_transaction(
    body: SubmitTransactionRequest,
    current_user: dict = Depends(get_current_user),
):
    """Broadcast a user-signed staking transaction to the network."""
    _require_enabled()
    return await user_staking_service.submit_transaction(body.transaction)


@router.get("/status", response_model=UserStakeStatusResponse)
async def user_staking_status(
    current_user: dict = Depends(get_current_user),
):
    """Read the user's on-chain stake state and derived tier."""
    _require_enabled()
    wallet = current_user.get("wallet_address")
    if not wallet:
        raise HTTPException(status_code=400, detail="account has no wallet_address")
    return await user_staking_service.get_stake_status(wallet)
