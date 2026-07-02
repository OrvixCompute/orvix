"""Chat + image quota accounting and enforcement.

Enforcement functions raise OrvixException (402/403/429) when a request must be
blocked, and otherwise return quota info for response headers. Counters use
read-modify-write (fine for the single-worker alpha; revisit with an atomic
upsert/RPC when scaling to multiple workers).

Reset: image quota is keyed by UTC date, so it resets at 00:00 UTC by rollover.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from supabase import Client

from app.config import settings
from app.exceptions import OrvixException
from app.logger import logger


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _next_midnight_iso() -> str:
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc).isoformat()


# --- chat ------------------------------------------------------------------
def _chat_used(db: Client, wallet: str) -> int:
    res = (
        db.table("chat_quota_usage")
        .select("lifetime_free_used")
        .eq("wallet_address", wallet)
        .limit(1)
        .execute()
    )
    return int(res.data[0]["lifetime_free_used"]) if res.data else 0


def _bump_chat(db: Client, wallet: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = (
        db.table("chat_quota_usage")
        .select("lifetime_free_used")
        .eq("wallet_address", wallet)
        .limit(1)
        .execute()
    )
    if existing.data:
        db.table("chat_quota_usage").update(
            {"lifetime_free_used": int(existing.data[0]["lifetime_free_used"]) + 1, "last_used_at": now}
        ).eq("wallet_address", wallet).execute()
    else:
        db.table("chat_quota_usage").insert(
            {"wallet_address": wallet, "lifetime_free_used": 1, "first_used_at": now, "last_used_at": now}
        ).execute()


def enforce_chat_quota(db: Client, wallet: str, is_holder: bool, balance_usdc) -> dict:
    """Gate a chat request. Returns {type, remaining, free}; raises 402 if blocked.

    - holder: unlimited access (billing still applies downstream).
    - non-holder under the free limit: allowed as a free request (not billed).
    - non-holder over the limit with balance: allowed on the paid flow.
    - non-holder over the limit with no balance: 402 quota_exceeded.
    """
    if is_holder:
        return {"type": "holder", "remaining": None, "free": False}

    used = _chat_used(db, wallet)
    limit = settings.CHAT_LIFETIME_FREE_LIMIT
    if used < limit:
        _bump_chat(db, wallet)
        return {"type": "free", "remaining": limit - used - 1, "free": True}

    if float(balance_usdc) > 0:
        return {"type": "paid", "remaining": 0, "free": False}

    raise OrvixException(
        f"Free chat quota ({limit} requests) exhausted. Hold "
        f"{settings.ORVX_HOLDER_THRESHOLD} ORVX for unlimited access, or top up USDC.",
        error_code="quota_exceeded",
        status_code=402,
        details={"upgrade_url": settings.UPGRADE_URL},
    )


# --- image -----------------------------------------------------------------
def _image_used(db: Client, wallet: str, day: str) -> int:
    res = (
        db.table("image_quota_usage")
        .select("count")
        .eq("wallet_address", wallet)
        .eq("usage_date", day)
        .limit(1)
        .execute()
    )
    return int(res.data[0]["count"]) if res.data else 0


def _bump_image(db: Client, wallet: str, day: str, units: int) -> None:
    existing = (
        db.table("image_quota_usage")
        .select("count")
        .eq("wallet_address", wallet)
        .eq("usage_date", day)
        .limit(1)
        .execute()
    )
    if existing.data:
        db.table("image_quota_usage").update(
            {"count": int(existing.data[0]["count"]) + units}
        ).eq("wallet_address", wallet).eq("usage_date", day).execute()
    else:
        db.table("image_quota_usage").insert(
            {"wallet_address": wallet, "usage_date": day, "count": units}
        ).execute()


def enforce_image_quota(
    db: Client, wallet: str, is_holder: bool, balance: float, units: int
) -> dict:
    """Gate an image request for `units` images. Returns {remaining, reset_at, limit}.

    Raises 403 not_holder (when ORVX_MINT_ADDRESS is set and the caller is not a
    holder) or 429 daily_quota_exceeded. When the mint address is unset, everyone
    gets the fallback grace allowance.
    """
    address_set = bool(settings.ORVX_MINT_ADDRESS)
    if address_set:
        if not is_holder:
            raise OrvixException(
                f"Image generation requires holding at least "
                f"{settings.ORVX_HOLDER_THRESHOLD} ORVX. Current balance: {balance:.0f} ORVX.",
                error_code="not_holder",
                status_code=403,
                details={"upgrade_url": settings.TOKENOMICS_URL},
            )
        daily_limit = settings.IMAGE_DAILY_LIMIT_HOLDER
    else:
        daily_limit = settings.IMAGE_DAILY_LIMIT_FALLBACK

    day = _today_iso()
    used = _image_used(db, wallet, day)
    if used + units > daily_limit:
        raise OrvixException(
            f"Daily image quota ({daily_limit}) exhausted. Resets at 00:00 UTC.",
            error_code="daily_quota_exceeded",
            status_code=429,
            details={"reset_at": _next_midnight_iso(), "used": used, "daily_limit": daily_limit},
        )
    _bump_image(db, wallet, day, units)
    return {"remaining": daily_limit - (used + units), "reset_at": _next_midnight_iso(), "limit": daily_limit}


def refund_image_quota(db: Client, wallet: str, units: int) -> None:
    """Give back `units` of today's image quota (e.g. after a generation failure)."""
    if units <= 0:
        return
    day = _today_iso()
    existing = (
        db.table("image_quota_usage")
        .select("count")
        .eq("wallet_address", wallet)
        .eq("usage_date", day)
        .limit(1)
        .execute()
    )
    if not existing.data:
        return
    new_count = max(0, int(existing.data[0]["count"]) - units)
    db.table("image_quota_usage").update({"count": new_count}).eq(
        "wallet_address", wallet
    ).eq("usage_date", day).execute()
    logger.info("Refunded {} image quota unit(s) to {}", units, wallet)


# --- status (for GET /v1/account/quota) ------------------------------------
def quota_status(db: Client, wallet: str, is_holder: bool, balance: float) -> dict:
    address_set = bool(settings.ORVX_MINT_ADDRESS)

    if is_holder:
        chat = {"type": "unlimited", "lifetime_free_used": None, "lifetime_free_limit": None}
    else:
        chat = {
            "type": "free_tier",
            "lifetime_free_used": _chat_used(db, wallet),
            "lifetime_free_limit": settings.CHAT_LIFETIME_FREE_LIMIT,
        }

    used_today = _image_used(db, wallet, _today_iso())
    if address_set and not is_holder:
        image = {"type": "locked", "used_today": used_today, "daily_limit": 0}
    elif address_set:
        image = {"type": "holder_daily", "used_today": used_today, "daily_limit": settings.IMAGE_DAILY_LIMIT_HOLDER}
    else:
        image = {"type": "grace_daily", "used_today": used_today, "daily_limit": settings.IMAGE_DAILY_LIMIT_FALLBACK}
    image["resets_at"] = _next_midnight_iso()

    return {
        "wallet": wallet,
        "is_holder": is_holder,
        "orvx_balance": balance,
        "orvx_holder_threshold": settings.ORVX_HOLDER_THRESHOLD,
        "chat": chat,
        "image": image,
    }
