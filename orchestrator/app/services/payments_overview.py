"""Aggregate a payment-flow snapshot for monitoring.

Shared by the admin endpoint (GET /v1/admin/payments/overview) and the CLI
dashboard (scripts/payment_status.py). Read-only: it only SELECTs; it never
touches the chain or mutates rows. Call /v1/admin/treasury/sync first if you
want fresh on-chain balances (this reads the last-synced values from the table).
"""

from datetime import datetime, timedelta, timezone

from app.config import settings


def _cutoff_24h() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


def _flags() -> dict:
    """Payment-relevant runtime flags (the levers Session 4 activation flips)."""
    return {
        "enable_payment_listener": settings.ENABLE_PAYMENT_LISTENER,
        "enable_payout_worker": settings.ENABLE_PAYOUT_WORKER,
        "payout_stub": settings.PAYOUT_STUB,
        "treasury_sweep_stub": settings.TREASURY_SWEEP_STUB,
        "enable_hot_sweeper": settings.ENABLE_HOT_SWEEPER,
        "min_withdraw_amount_usdc": settings.MIN_WITHDRAW_AMOUNT_USDC,
        "auto_approve_max_usdc": settings.AUTO_APPROVE_MAX_USDC,
        "max_withdrawals_per_day": settings.MAX_WITHDRAWALS_PER_DAY,
        "usdc_mint_configured": bool(settings.USDC_MINT_ADDRESS),
        "orvx_mint_configured": bool(settings.ORVX_MINT_ADDRESS),
    }


def _tx_view(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "user_id": r.get("user_id"),
        "type": r.get("type"),
        "amount": r.get("amount"),
        "token": r.get("token"),
        "status": r.get("status"),
        "solana_signature": r.get("solana_signature"),
        "created_at": r.get("created_at"),
    }


def _wd_view(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "user_id": r.get("user_id"),
        "amount": r.get("amount"),
        "destination_wallet": r.get("destination_wallet"),
        "status": r.get("status"),
        "solana_signature": r.get("solana_signature"),
        "error_message": r.get("error_message"),
        "queued_at": r.get("queued_at"),
        "processed_at": r.get("processed_at"),
    }


def _count(db, status: str, *, since_col: str | None = None, since: str | None = None) -> int:
    q = db.table("withdrawals").select("id", count="exact").eq("status", status)
    if since_col and since:
        q = q.gte(since_col, since)
    return q.execute().count or 0


def build_overview(db) -> dict:
    """Assemble the payment-flow monitoring snapshot from the DB."""
    cutoff = _cutoff_24h()

    treasury = db.table("treasury_wallets").select("*").execute().data or []

    # --- deposits (USDC top-ups credited by the payment listener) ----------
    recent_deposits = (
        db.table("transactions")
        .select("*")
        .eq("type", "topup")
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    dep_24h = (
        db.table("transactions")
        .select("amount")
        .eq("type", "topup")
        .gte("created_at", cutoff)
        .execute()
        .data
        or []
    )
    deposits = {
        "count_24h": len(dep_24h),
        "total_usdc_24h": round(sum(float(r.get("amount") or 0) for r in dep_24h), 6),
        "recent": [_tx_view(r) for r in recent_deposits],
    }

    # --- withdrawals (provider payouts) ------------------------------------
    # 'processing' rows that linger are the manual-review bucket: a payout was
    # broadcast but never confirmed (see PayoutService._process_one). The worker
    # never re-picks them, so an operator must reconcile the signature on-chain.
    needs_review = (
        db.table("withdrawals").select("*").eq("status", "processing").execute().data or []
    )
    recent_withdrawals = (
        db.table("withdrawals")
        .select("*")
        .order("queued_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    withdrawals = {
        "queued": _count(db, "queued"),
        "processing": _count(db, "processing"),
        "completed_24h": _count(db, "completed", since_col="processed_at", since=cutoff),
        "failed_24h": _count(db, "failed", since_col="processed_at", since=cutoff),
        "needs_review": [_wd_view(r) for r in needs_review],
        "recent": [_wd_view(r) for r in recent_withdrawals],
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flags": _flags(),
        "treasury": treasury,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "note": (
            "Balances are last-synced values; POST /v1/admin/treasury/sync to refresh. "
            "Unattributed deposits (no memo / no matching intent) are warn-logged only, "
            "not persisted — grep the service log for 'Unattributed deposit'."
        ),
    }
