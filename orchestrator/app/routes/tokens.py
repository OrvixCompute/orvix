"""Token intelligence endpoints: token/CA scans + accumulation.

Authenticated with EITHER a wallet JWT or an `orvx_sk_` API key (scans spend
Solana RPC + Jupiter calls, so they are not public). Degrade gracefully: fields
the data sources cannot answer come back null/[].
"""

from fastapi import APIRouter, Depends
from solders.pubkey import Pubkey
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user_flexible
from app.exceptions import ValidationError
from app.models.intel import AccumulationResponse, TokenScanResponse
from app.services import rate_limit_service, tier_service, token_intel

router = APIRouter(prefix="/v1/tokens", tags=["tokens"])


def _validate_address(address: str, label: str) -> None:
    try:
        Pubkey.from_string(address)
    except Exception as exc:  # noqa: BLE001 — solders raises ValueError
        raise ValidationError(f"Invalid Solana address for {label}") from exc


def _rate_limit(current_user: dict) -> None:
    """Throttle scan endpoints: they spend external RPC/Jupiter budget."""
    tier = tier_service.tier_for_stake(current_user.get("staked_orvx"))
    rate_limit_service.check_user(current_user["id"], tier, bucket="intel")


@router.get("/{ca}", response_model=TokenScanResponse)
async def get_token_scan(
    ca: str,
    current_user: dict = Depends(get_current_user_flexible),
    db: Client = Depends(get_supabase),
):
    """Full profile for a token mint: metadata, supply, price, liquidity, risk."""
    _validate_address(ca, "token")
    _rate_limit(current_user)
    return await token_intel.scan_token(db, ca)


@router.get("/{ca}/accumulation", response_model=AccumulationResponse)
async def get_accumulation(
    ca: str,
    current_user: dict = Depends(get_current_user_flexible),
    db: Client = Depends(get_supabase),
):
    """Accumulation score (0-100) + metrics for a token mint."""
    _validate_address(ca, "token")
    _rate_limit(current_user)
    return await token_intel.compute_accumulation(db, ca)
