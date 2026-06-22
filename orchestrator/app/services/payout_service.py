"""Provider withdrawals: queueing + a background worker that settles them.

The actual on-chain send is STUBBED by default (PAYOUT_STUB=true) — it simulates
success with a fake signature. This is the most security-sensitive component:
withdrawals are rate-limited per user per day, amounts above AUTO_APPROVE_MAX_USDC
require manual approval, and every step is logged.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from solders.pubkey import Pubkey

from app.config import settings
from app.database import get_supabase
from app.exceptions import InsufficientBalanceError, RateLimitError, ValidationError
from app.logger import logger


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PayoutService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --- queueing ----------------------------------------------------------
    def _validate_wallet(self, wallet: str) -> None:
        try:
            Pubkey.from_string(wallet)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("Invalid Solana destination wallet") from exc

    def _check_daily_limit(self, db, user_id: str) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        res = (
            db.table("withdrawals")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .gte("queued_at", since)
            .execute()
        )
        if (res.count or 0) >= settings.MAX_WITHDRAWALS_PER_DAY:
            raise RateLimitError(
                f"Daily withdrawal limit reached ({settings.MAX_WITHDRAWALS_PER_DAY}/day)"
            )

    def queue_withdrawal(self, user_id: str, amount: Decimal, destination_wallet: str) -> dict:
        db = get_supabase()
        self._validate_wallet(destination_wallet)

        if amount < Decimal(str(settings.MIN_WITHDRAW_AMOUNT_USDC)):
            raise ValidationError(
                f"Minimum withdrawal is {settings.MIN_WITHDRAW_AMOUNT_USDC} USDC"
            )
        self._check_daily_limit(db, user_id)

        # Atomically move available -> pending. Returns false if insufficient.
        locked = db.rpc(
            "lock_withdrawal", {"p_user_id": user_id, "p_amount": float(amount)}
        ).execute()
        if not locked.data:
            raise InsufficientBalanceError("Insufficient available balance to withdraw")

        requires_approval = float(amount) > settings.AUTO_APPROVE_MAX_USDC
        row = {
            "user_id": user_id,
            "amount": float(amount),
            "destination_wallet": destination_wallet,
            "status": "queued",
            "metadata": {"manual_approval_required": requires_approval},
        }
        inserted = db.table("withdrawals").insert(row).execute().data[0]

        # Ledger entry for the pending payout.
        db.table("transactions").insert(
            {
                "user_id": user_id,
                "type": "provider_payout",
                "amount": float(amount),
                "token": "USDC",
                "status": "pending",
                "metadata": {"withdrawal_id": str(inserted["id"])},
            }
        ).execute()

        logger.info(
            "Queued withdrawal {} for user {} ({} USDC, manual_approval={})",
            inserted["id"],
            user_id,
            amount,
            requires_approval,
        )
        return inserted

    # --- background worker -------------------------------------------------
    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="payout-worker")
        logger.info(
            "Payout worker started (stub={}, interval={}s)",
            settings.PAYOUT_STUB,
            settings.PAYOUT_INTERVAL_SECONDS,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=settings.PAYOUT_INTERVAL_SECONDS + 5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Payout worker stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_pending_withdrawals()
            except Exception as exc:  # noqa: BLE001
                logger.error("Payout cycle failed: {}", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.PAYOUT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def process_pending_withdrawals(self) -> None:
        db = get_supabase()
        res = (
            db.table("withdrawals")
            .select("*")
            .eq("status", "queued")
            .order("queued_at", desc=False)
            .limit(20)
            .execute()
        )
        for w in res.data or []:
            if (w.get("metadata") or {}).get("manual_approval_required"):
                logger.info("Withdrawal {} awaits manual approval — skipping", w["id"])
                continue
            await self._process_one(db, w)

    async def _process_one(self, db, w: dict) -> None:
        wid = w["id"]
        user_id = w["user_id"]
        amount = Decimal(str(w["amount"]))

        db.table("withdrawals").update({"status": "processing"}).eq("id", wid).execute()
        logger.info("Processing withdrawal {} ({} USDC)", wid, amount)

        try:
            signature = await self._send_payout(w)
        except Exception as exc:  # noqa: BLE001
            logger.error("Withdrawal {} failed: {} — refunding", wid, exc)
            db.rpc(
                "settle_withdrawal",
                {"p_user_id": user_id, "p_amount": float(amount), "p_refund": True},
            ).execute()
            db.table("withdrawals").update(
                {"status": "failed", "error_message": str(exc), "processed_at": _now_iso()}
            ).eq("id", wid).execute()
            return

        # Success: clear from pending (no refund), record signature.
        db.rpc(
            "settle_withdrawal",
            {"p_user_id": user_id, "p_amount": float(amount), "p_refund": False},
        ).execute()
        db.table("withdrawals").update(
            {"status": "completed", "solana_signature": signature, "processed_at": _now_iso()}
        ).eq("id", wid).execute()

        # Confirm the ledger transaction.
        db.table("transactions").update({"status": "confirmed", "solana_signature": signature}).eq(
            "type", "provider_payout"
        ).contains("metadata", {"withdrawal_id": str(wid)}).execute()

        logger.info("Withdrawal {} completed (sig={})", wid, signature)

    async def _send_payout(self, w: dict) -> str:
        """Send the USDC transfer. Stubbed unless PAYOUT_STUB=false."""
        if settings.PAYOUT_STUB:
            await asyncio.sleep(0.1)  # simulate network
            return "STUB" + uuid.uuid4().hex
        # TODO (production): build + sign an SPL transfer with the treasury
        # keypair (settings.TREASURY_KEYPAIR_PATH) and submit via Helius RPC,
        # then confirm. Kept unimplemented to avoid accidental real sends.
        raise NotImplementedError(
            "Real payouts not implemented. Set PAYOUT_STUB=true for development."
        )


payout_service = PayoutService()
