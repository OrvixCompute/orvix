"""FastAPI dependencies for the two authentication schemes.

- get_current_user: JWT bearer (used by dashboard/user endpoints)
- get_user_from_api_key: orvx_sk_ bearer (used by the inference API)
"""

import re
import secrets

from fastapi import BackgroundTasks, Depends, Header, Request
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.exceptions import UnauthorizedError
from app.logger import logger
from app.services.api_key_service import hash_key
from app.services.auth_service import auth_service

API_KEY_RE = re.compile(r"^orvx_sk_[A-Za-z0-9_-]{32}$")


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header")
    return auth[len("Bearer ") :].strip()


def _load_user(db: Client, user_id: str) -> dict:
    res = db.table("users").select("*").eq("id", user_id).limit(1).execute()
    if not res.data:
        raise UnauthorizedError("User not found")
    user = res.data[0]
    if not user.get("is_active", True):
        raise UnauthorizedError("User account is disabled")
    return user


async def get_current_user(
    request: Request, db: Client = Depends(get_supabase)
) -> dict:
    """Resolve the current user from a JWT bearer token."""
    token = _bearer_token(request)
    claims = auth_service.verify_jwt(token)
    user_id = claims.get("sub")
    if not user_id:
        raise UnauthorizedError("Token missing subject claim")
    return _load_user(db, user_id)


def _touch_last_used(api_key_id: str) -> None:
    """Best-effort update of last_used_at; runs in a background task."""
    try:
        from datetime import datetime, timezone

        db = get_supabase()
        db.table("api_keys").update(
            {"last_used_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", api_key_id).execute()
    except Exception as exc:  # noqa: BLE001 — never let bookkeeping break a request
        logger.warning("Failed to update last_used_at for {}: {}", api_key_id, exc)


async def get_user_from_api_key(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Client = Depends(get_supabase),
) -> dict:
    """Resolve the user from an `orvx_sk_` API key bearer token.

    Returns {"user": <user row>, "api_key": <api_key row>}.
    """
    token = _bearer_token(request)
    if not API_KEY_RE.match(token):
        raise UnauthorizedError("Malformed API key", error_code="invalid_api_key")

    res = (
        db.table("api_keys")
        .select("*")
        .eq("key_hash", hash_key(token))
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise UnauthorizedError("Invalid or revoked API key", error_code="invalid_api_key")
    api_key = res.data[0]

    user = _load_user(db, api_key["user_id"])

    # Update last_used_at without blocking the request.
    background_tasks.add_task(_touch_last_used, api_key["id"])

    return {"user": user, "api_key": api_key}


def require_admin(x_admin_key: str | None = Header(None)) -> bool:
    """Gate admin endpoints behind the ADMIN_API_KEY shared secret (X-Admin-Key)."""
    if not settings.ADMIN_API_KEY:
        raise UnauthorizedError(
            "Admin endpoints are disabled (ADMIN_API_KEY not set)",
            error_code="admin_disabled",
        )
    if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        raise UnauthorizedError("Invalid admin key", error_code="invalid_admin_key")
    return True
