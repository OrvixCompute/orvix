"""Monthly burn engine: send held ORVX to the incinerator, then record it.

Flow: resolve amount (default: all held) and period (default: previous calendar
month) -> validate -> transfer to the incinerator (real or stubbed) -> confirm
-> record_burn RPC (decrements orvx_held_for_burn) -> audit log.

The real SPL transfer is gated behind BURN_STUB (default true), mirroring the
buyback engine. Shared by the CLI (scripts/burn.py) and the admin endpoint.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import settings
from app.database import get_supabase
from app.exceptions import OrvixException, ValidationError
from app.logger import logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


def previous_month_period(ref: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month before `ref` (default: now), UTC."""
    ref = ref or _now()
    first_this = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (first_this - timedelta(days=1)).replace(day=1)
    return start, first_this


class BurnService:
    def __init__(self, db=None) -> None:
        self.db = db or get_supabase()

    # --- read helpers ------------------------------------------------------
    def _accounting(self) -> dict:
        rows = (
            self.db.table("global_accounting").select("*").eq("id", 1).limit(1).execute().data
        )
        return rows[0] if rows else {}

    def _last_burn(self) -> dict | None:
        rows = (
            self.db.table("burn_events")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def status(self) -> dict:
        acct = self._accounting()
        last = self._last_burn()
        return {
            "orvx_held_for_burn": str(acct.get("orvx_held_for_burn", 0) or 0),
            "total_orvx_burned": str(acct.get("total_orvx_burned", 0) or 0),
            "last_burn_at": last["created_at"] if last else None,
        }

    # --- execution ---------------------------------------------------------
    async def execute(
        self,
        amount: Decimal | None,
        period_start: datetime | None,
        period_end: datetime | None,
        executor: str,
    ) -> dict:
        held = Decimal(str(self._accounting().get("orvx_held_for_burn", 0) or 0))

        if amount is None:
            amount = held
        amount = Decimal(str(amount))

        if amount <= 0:
            raise ValidationError("Nothing to burn (amount is 0)")
        if amount > held:
            raise ValidationError(
                "Burn amount exceeds ORVX held for burn",
                details={"held": format(held, "f"), "requested": format(amount, "f")},
            )

        if period_start is None or period_end is None:
            period_start, period_end = previous_month_period()
        if period_end <= period_start:
            raise ValidationError("period_end must be after period_start")
        if period_end > _now():
            raise ValidationError("period_end cannot be in the future")

        signature = await self._burn(amount)

        try:
            res = self.db.rpc(
                "record_burn",
                {
                    "p_orvx_burned": float(amount),
                    "p_solana_sig": signature,
                    "p_period_start": period_start.isoformat(),
                    "p_period_end": period_end.isoformat(),
                    "p_executor": executor,
                    "p_notes": None,
                },
            ).execute()
            burn_id = res.data
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "BURN RECONCILIATION NEEDED: transfer {} confirmed ({} ORVX) but "
                "record_burn failed: {}",
                signature,
                amount,
                exc,
            )
            raise OrvixException(
                "Burn confirmed but DB recording failed — manual reconciliation needed",
                error_code="burn_record_failed",
                status_code=500,
            ) from exc

        result = {
            "burn_id": str(burn_id) if burn_id else None,
            "orvx_burned": format(amount, "f"),
            "solana_signature": signature,
            "period": {
                "period_start": period_start,
                "period_end": period_end,
            },
        }
        self._audit(executor, result)
        logger.info(
            "Burn executed: {} ORVX by {} (sig={}, period {}..{})",
            amount,
            executor,
            signature,
            period_start.date(),
            period_end.date(),
        )
        return result

    async def _burn(self, amount: Decimal) -> str:
        """SPL transfer of ORVX to the incinerator, confirmed. Stubbed by default."""
        if settings.BURN_STUB:
            await asyncio.sleep(0.05)
            return "STUBBURN" + uuid.uuid4().hex
        # TODO (production): build an SPL transfer of `amount` ORVX (raw =
        # amount * 10**ORVX_DECIMALS) from the treasury's ORVX ATA to the
        # incinerator's ORVX ATA (create if missing), sign with the treasury
        # keypair (app.services.wallet.load_keypair), submit via Helius, confirm.
        raise NotImplementedError(
            "Real burns are not implemented. Keep BURN_STUB=true until vetted."
        )

    def _audit(self, executor: str, result: dict) -> None:
        if not settings.AUDIT_LOG_DIR:
            return
        try:
            os.makedirs(settings.AUDIT_LOG_DIR, exist_ok=True)
            stamp = _now()
            path = os.path.join(settings.AUDIT_LOG_DIR, f"burn-{stamp:%Y-%m}.log")
            p = result["period"]
            line = (
                f"{stamp.isoformat()} executor={executor} orvx={result['orvx_burned']} "
                f"sig={result['solana_signature']} id={result['burn_id']} "
                f"period={p['period_start'].date()}..{p['period_end'].date()}\n"
            )
            with open(path, "a") as f:
                f.write(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not write burn audit log: {}", exc)
