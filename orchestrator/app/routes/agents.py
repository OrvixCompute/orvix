"""Monitoring agents: create/list/delete monitors, read alerts, test webhooks.

Authenticated with a wallet JWT (get_current_user). A monitor targets a token
or wallet and fires alert events when its conditions are met; the background
MonitorService worker (ENABLE_MONITOR_WORKER) evaluates them. When a monitor
carries a webhook_url, alerts are POSTed there with retry.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from solders.pubkey import Pubkey
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.exceptions import ValidationError
from app.models.intel import (
    AlertEventResponse,
    MonitorCreateRequest,
    MonitorResponse,
    WebhookTestResponse,
)
from app.services import token_intel

router = APIRouter(prefix="/v1/agents", tags=["agents"])

# condition type -> allowed target types
_CONDITION_TARGETS: dict[str, set[str]] = {
    "accumulation_score": {"token"},
    "price_drop_pct": {"token"},
    "large_transfer": {"token"},
    "new_activity": {"wallet"},
    "large_inflow": {"wallet"},
}


def _validate_address(address: str, label: str) -> None:
    try:
        Pubkey.from_string(address)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(f"Invalid Solana address for {label}") from exc


def _validate_conditions(target_type: str, conditions: list) -> None:
    for cond in conditions:
        ctype = cond.get("type")
        allowed = _CONDITION_TARGETS.get(ctype)
        if allowed is None:
            raise ValidationError(f"Unknown condition type '{ctype}'")
        if target_type not in allowed:
            raise ValidationError(f"Condition '{ctype}' is not valid for a {target_type} monitor")
        if ctype in ("accumulation_score", "price_drop_pct") and not (
            isinstance(cond.get("gte"), (int, float))
        ):
            raise ValidationError(f"Condition '{ctype}' requires a numeric 'gte'")
        if ctype in ("large_transfer", "large_inflow") and not (
            isinstance(cond.get("min_ui_amount"), (int, float))
        ):
            raise ValidationError(f"Condition '{ctype}' requires a numeric 'min_ui_amount'")


def _get_owned_monitor(db: Client, monitor_id: str, user_id: str) -> dict:
    res = (
        db.table("monitors")
        .select("*")
        .eq("id", monitor_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return res.data[0]


def _monitor_response(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row.get("name", ""),
        "target_type": row.get("target_type"),
        "target_address": row.get("target_address"),
        "conditions": row.get("conditions") or [],
        "webhook_url": row.get("webhook_url"),
        "is_active": bool(row.get("is_active", True)),
        "interval_minutes": int(row.get("interval_minutes") or 30),
        "baseline_price_usdc": row.get("baseline_price_usdc"),
        "last_checked_at": row.get("last_checked_at"),
        "created_at": row.get("created_at"),
    }


@router.post("/monitors", response_model=MonitorResponse, status_code=201)
async def create_monitor(
    body: MonitorCreateRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Create a monitoring agent for a token or wallet."""
    _validate_address(body.target_address, "target_address")
    _validate_conditions(body.target_type, [c.model_dump() for c in body.conditions])

    baseline = None
    if any(c.type == "price_drop_pct" for c in body.conditions) and body.target_type == "token":
        baseline = await token_intel.get_token_price_usdc(body.target_address)
        if baseline is not None:
            baseline = float(baseline)

    row = {
        "user_id": current_user["id"],
        "name": body.name,
        "target_type": body.target_type,
        "target_address": body.target_address,
        "conditions": [c.model_dump() for c in body.conditions],
        "webhook_url": body.webhook_url,
        "is_active": body.is_active,
        "interval_minutes": body.interval_minutes,
        "baseline_price_usdc": baseline,
    }
    inserted = db.table("monitors").insert(row).execute().data[0]
    return _monitor_response(inserted)


@router.get("/monitors", response_model=list[MonitorResponse])
async def list_monitors(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """List the current user's monitors, newest first."""
    res = (
        db.table("monitors")
        .select("*")
        .eq("user_id", current_user["id"])
        .order("created_at", desc=True)
        .execute()
    )
    return [_monitor_response(r) for r in res.data or []]


@router.get("/monitors/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(
    monitor_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Get one monitor (owner only)."""
    return _monitor_response(_get_owned_monitor(db, monitor_id, current_user["id"]))


@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(
    monitor_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Delete a monitor (owner only). Alerts cascade."""
    _get_owned_monitor(db, monitor_id, current_user["id"])
    db.table("monitors").delete().eq("id", monitor_id).eq("user_id", current_user["id"]).execute()
    return None


@router.get("/monitors/{monitor_id}/alerts", response_model=list[AlertEventResponse])
async def list_monitor_alerts(
    monitor_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Alert events for one monitor, newest first (owner only)."""
    _get_owned_monitor(db, monitor_id, current_user["id"])
    res = (
        db.table("alert_events")
        .select("*")
        .eq("monitor_id", monitor_id)
        .order("occurred_at", desc=True)
        .limit(100)
        .execute()
    )
    return [
        {
            "id": str(r["id"]),
            "monitor_id": str(r["monitor_id"]),
            "condition_type": r.get("condition_type"),
            "message": r.get("message"),
            "payload": r.get("payload") or {},
            "occurred_at": r.get("occurred_at"),
        }
        for r in res.data or []
    ]


@router.post("/monitors/{monitor_id}/test", response_model=WebhookTestResponse)
async def test_monitor_webhook(
    monitor_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Send a sample alert payload to the monitor's webhook (no event row)."""
    monitor = _get_owned_monitor(db, monitor_id, current_user["id"])
    url = monitor.get("webhook_url")
    if not url:
        raise HTTPException(status_code=400, detail="Monitor has no webhook_url")
    payload = {
        "event_id": "test",
        "monitor_id": str(monitor["id"]),
        "condition_type": "test",
        "message": f"Test alert for monitor {monitor.get('name') or monitor['id']}",
        "payload": {"test": True},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        import httpx

        from app.config import settings

        async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
        return WebhookTestResponse(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
    except Exception as exc:  # noqa: BLE001
        return WebhookTestResponse(ok=False, error=str(exc))
