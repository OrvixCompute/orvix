"""Provider-facing endpoints: nodes, earnings, withdrawals (JWT-authenticated)."""

import hashlib
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.exceptions import NotFoundError
from app.models.protocol import ShutdownMessage
from app.models.provider import (
    ProviderRegisterRequest,
    RenameNodeRequest,
    SecretResponse,
    WithdrawRequest,
    WithdrawResponse,
)
from app.services.node_manager import node_manager
from app.services.payout_service import payout_service

router = APIRouter(prefix="/v1/provider", tags=["provider"])


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _user_node_ids(db: Client, user_id: str) -> list[str]:
    res = db.table("nodes").select("id").eq("provider_id", user_id).execute()
    return [r["id"] for r in (res.data or [])]


def _owned_node(db: Client, user_id: str, node_id: str) -> dict:
    res = (
        db.table("nodes")
        .select("*")
        .eq("id", node_id)
        .eq("provider_id", user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise NotFoundError("Node not found")
    return res.data[0]


# ---------------------------------------------------------------------------
# Registration / secret
# ---------------------------------------------------------------------------
@router.post("/register", response_model=SecretResponse)
async def register_provider(
    body: ProviderRegisterRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Opt the user in as a provider and issue a node secret (shown once)."""
    secret = secrets.token_urlsafe(24)
    update = {"is_provider": True, "provider_secret_hash": _hash_secret(secret)}
    if body.display_name:
        update["email"] = current_user.get("email")  # display_name kept client-side for now
    db.table("users").update(update).eq("id", current_user["id"]).execute()
    return SecretResponse(node_secret=secret)


@router.post("/regenerate-secret", response_model=SecretResponse)
async def regenerate_secret(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    secret = secrets.token_urlsafe(24)
    db.table("users").update({"provider_secret_hash": _hash_secret(secret)}).eq(
        "id", current_user["id"]
    ).execute()
    return SecretResponse(node_secret=secret)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@router.get("/nodes")
async def list_nodes(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    res = (
        db.table("nodes")
        .select("*")
        .eq("provider_id", current_user["id"])
        .order("created_at", desc=True)
        .execute()
    )
    out = []
    for n in res.data or []:
        out.append(
            {
                "id": n["id"],
                "name": n.get("name"),
                "status": n["status"],
                "gpu_model": n.get("gpu_model"),
                "vram_mb": n.get("vram_mb"),
                "models_supported": n.get("models_supported"),
                "total_jobs": n.get("total_jobs", 0),
                "total_earned_usdc": str(n.get("total_earned_usdc", 0)),
                "reputation_score": n.get("reputation_score", 100),
                "last_heartbeat": n.get("last_heartbeat"),
                "created_at": n.get("created_at"),
                "is_connected": n["id"] in node_manager.connected_nodes,
            }
        )
    return out


@router.get("/nodes/{node_id}")
async def node_detail(
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    n = _owned_node(db, current_user["id"], node_id)

    # Live metrics if connected.
    current_metrics = None
    conn = node_manager.connected_nodes.get(node_id)
    if conn is not None:
        current_metrics = {
            "current_jobs": conn.current_jobs,
            "status": conn.status,
            "gpu_info": conn.gpu_info,
        }

    recent = (
        db.table("jobs")
        .select("id, model, prompt_tokens, completion_tokens, cost_usdc, "
                "provider_earning_usdc, status, created_at")
        .eq("node_id", node_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    earnings_by_day = _aggregate_earnings([node_id], db)

    return {
        "id": n["id"],
        "name": n.get("name"),
        "status": n["status"],
        "gpu_model": n.get("gpu_model"),
        "vram_mb": n.get("vram_mb"),
        "models_supported": n.get("models_supported"),
        "total_jobs": n.get("total_jobs", 0),
        "total_earned_usdc": str(n.get("total_earned_usdc", 0)),
        "reputation_score": n.get("reputation_score", 100),
        "is_connected": conn is not None,
        "current_metrics": current_metrics,
        "recent_jobs": recent.data or [],
        "earnings_by_day": earnings_by_day,
    }


@router.post("/nodes/{node_id}/rename")
async def rename_node(
    node_id: str,
    body: RenameNodeRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    _owned_node(db, current_user["id"], node_id)
    db.table("nodes").update({"name": body.name}).eq("id", node_id).execute()
    return {"id": node_id, "name": body.name}


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    node_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    _owned_node(db, current_user["id"], node_id)
    db.table("nodes").update({"status": "offline"}).eq("id", node_id).execute()

    # If currently connected, ask it to shut down and drop it from routing.
    conn = node_manager.connected_nodes.get(node_id)
    if conn is not None:
        try:
            await conn.send(ShutdownMessage(reason="node removed by provider"))
        except Exception:  # noqa: BLE001
            pass
        await node_manager.unregister_node(node_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Earnings & withdrawals
# ---------------------------------------------------------------------------
def _aggregate_earnings(node_ids: list[str], db: Client) -> list[dict]:
    """Daily provider earnings over the last 30 days for the given nodes."""
    if not node_ids:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    res = (
        db.table("jobs")
        .select("provider_earning_usdc, created_at, node_id")
        .in_("node_id", node_ids)
        .gte("created_at", since)
        .execute()
    )
    by_day_amount: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_day_count: dict[str, int] = defaultdict(int)
    for j in res.data or []:
        day = str(j["created_at"])[:10]
        by_day_amount[day] += Decimal(str(j.get("provider_earning_usdc", 0)))
        by_day_count[day] += 1
    return [
        {"date": day, "amount": str(by_day_amount[day]), "jobs_count": by_day_count[day]}
        for day in sorted(by_day_amount)
    ]


@router.get("/earnings")
async def earnings(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    u = (
        db.table("users")
        .select("available_usdc, pending_withdrawal_usdc, lifetime_earnings_usdc")
        .eq("id", current_user["id"])
        .limit(1)
        .execute()
        .data
    )
    u = u[0] if u else {}

    last = (
        db.table("withdrawals")
        .select("processed_at")
        .eq("user_id", current_user["id"])
        .eq("status", "completed")
        .order("processed_at", desc=True)
        .limit(1)
        .execute()
    )
    last_payout = last.data[0]["processed_at"] if last.data else None

    return {
        "total_lifetime_usdc": str(u.get("lifetime_earnings_usdc", 0)),
        "available_to_withdraw": str(u.get("available_usdc", 0)),
        "pending_withdrawal": str(u.get("pending_withdrawal_usdc", 0)),
        "last_payout_at": last_payout,
        "earnings_by_day": _aggregate_earnings(_user_node_ids(db, current_user["id"]), db),
    }


@router.post("/withdraw", response_model=WithdrawResponse)
async def withdraw(
    body: WithdrawRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    destination = body.destination_wallet or current_user["wallet_address"]
    w = payout_service.queue_withdrawal(current_user["id"], body.amount, destination)
    return WithdrawResponse(
        withdrawal_id=str(w["id"]), status="queued", estimated_completion="< 1 hour"
    )


@router.get("/withdrawals")
async def list_withdrawals(
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    res = (
        db.table("withdrawals")
        .select("*")
        .eq("user_id", current_user["id"])
        .order("queued_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


@router.get("/jobs")
async def provider_jobs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    node_id: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    node_ids = [node_id] if node_id else _user_node_ids(db, current_user["id"])
    if node_id:
        _owned_node(db, current_user["id"], node_id)  # ownership check
    if not node_ids:
        return []
    res = (
        db.table("jobs")
        .select("*")
        .in_("node_id", node_ids)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return res.data or []
