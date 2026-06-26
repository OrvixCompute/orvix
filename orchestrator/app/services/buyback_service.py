"""Manual buyback engine: swap treasury USDC -> ORVX via Jupiter, then record it.

Flow: validate budget + rate limit -> Jupiter quote -> slippage guard -> swap
(real or stubbed) -> confirm -> record_buyback RPC (moves budget into
orvx_held_for_burn) -> audit log.

The real on-chain swap is gated behind BUYBACK_STUB (default true), mirroring the
payout worker: nothing hits the chain until the treasury keypair is configured
and BUYBACK_STUB=false. Shared by the CLI (scripts/buyback.py) and the admin
endpoint so both paths behave identically.
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.config import settings
from app.database import get_supabase
from app.exceptions import OrvixException, RateLimitError, ValidationError
from app.logger import logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class BuybackService:
    def __init__(self, db=None) -> None:
        self.db = db or get_supabase()

    # --- read helpers ------------------------------------------------------
    def _accounting(self) -> dict:
        rows = (
            self.db.table("global_accounting").select("*").eq("id", 1).limit(1).execute().data
        )
        return rows[0] if rows else {}

    def _last_buyback(self) -> dict | None:
        rows = (
            self.db.table("buyback_events")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def status(self) -> dict:
        acct = self._accounting()
        last = self._last_buyback()
        return {
            "buyback_budget_usdc": str(acct.get("buyback_budget_usdc", 0) or 0),
            "orvx_held_for_burn": str(acct.get("orvx_held_for_burn", 0) or 0),
            "total_orvx_bought": str(acct.get("total_orvx_bought", 0) or 0),
            "total_usdc_spent_on_buyback": str(acct.get("total_usdc_spent_on_buyback", 0) or 0),
            "last_buyback_at": last["created_at"] if last else None,
        }

    # --- Jupiter -----------------------------------------------------------
    async def quote(self, amount_usdc: Decimal, slippage_bps: int) -> dict:
        if not settings.USDC_MINT_ADDRESS or not settings.ORVX_MINT_ADDRESS:
            raise ValidationError("USDC_MINT_ADDRESS and ORVX_MINT_ADDRESS must be configured")
        amount_base = int(
            (Decimal(str(amount_usdc)) * (Decimal(10) ** settings.USDC_DECIMALS)).to_integral_value()
        )
        params = {
            "inputMint": settings.USDC_MINT_ADDRESS,
            "outputMint": settings.ORVX_MINT_ADDRESS,
            "amount": amount_base,
            "slippageBps": slippage_bps,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{settings.JUPITER_QUOTE_API}/quote", params=params)
            resp.raise_for_status()
            return resp.json()

    def _orvx_out(self, quote: dict) -> Decimal:
        return Decimal(quote["outAmount"]) / (Decimal(10) ** settings.ORVX_DECIMALS)

    async def preview(self, amount_usdc: Decimal, slippage_bps: int) -> dict:
        amount_usdc = Decimal(str(amount_usdc))
        quote = await self.quote(amount_usdc, slippage_bps)
        orvx_out = self._orvx_out(quote)
        price = (amount_usdc / orvx_out) if orvx_out else Decimal(0)
        impact_pct = Decimal(str(quote.get("priceImpactPct", "0")))
        return {
            "amount_usdc": str(amount_usdc),
            "estimated_orvx": format(orvx_out, "f"),
            "price_usdc_per_orvx": format(price, "f"),
            "price_impact_pct": format(impact_pct * 100, "f"),
        }

    # --- guardrails --------------------------------------------------------
    def _validate(self, amount_usdc: Decimal) -> None:
        if amount_usdc <= 0:
            raise ValidationError("amount_usdc must be greater than 0")

        budget = Decimal(str(self._accounting().get("buyback_budget_usdc", 0) or 0))
        if amount_usdc > budget:
            raise ValidationError(
                "Buyback amount exceeds accumulated budget",
                details={"budget": format(budget, "f"), "requested": format(amount_usdc, "f")},
            )

        last = self._last_buyback()
        if last:
            elapsed = (_now() - _parse_ts(last["created_at"])).total_seconds()
            if elapsed < settings.BUYBACK_MIN_INTERVAL_SECONDS:
                raise RateLimitError(
                    f"Buyback rate limit: wait "
                    f"{int(settings.BUYBACK_MIN_INTERVAL_SECONDS - elapsed)}s before the next one."
                )

    # --- execution ---------------------------------------------------------
    async def execute(
        self, amount_usdc: Decimal, slippage_bps: int, executor: str
    ) -> dict:
        amount_usdc = Decimal(str(amount_usdc))
        self._validate(amount_usdc)

        quote = await self.quote(amount_usdc, slippage_bps)

        impact_bps = Decimal(str(quote.get("priceImpactPct", "0"))) * 10000
        if impact_bps > settings.BUYBACK_MAX_SLIPPAGE_BPS:
            raise ValidationError(
                "Aborting buyback: Jupiter price impact exceeds the configured maximum",
                details={
                    "price_impact_bps": format(impact_bps, "f"),
                    "max_bps": str(settings.BUYBACK_MAX_SLIPPAGE_BPS),
                },
            )

        orvx_received = self._orvx_out(quote)
        signature = await self._swap(quote)  # confirms on-chain (or stubs)

        # Record only after the swap is confirmed. If this fails, the swap
        # already happened — log loudly so it can be reconciled by hand.
        try:
            res = self.db.rpc(
                "record_buyback",
                {
                    "p_usdc_spent": float(amount_usdc),
                    "p_orvx_received": float(orvx_received),
                    "p_solana_sig": signature,
                    "p_executor": executor,
                    "p_notes": None,
                },
            ).execute()
            buyback_id = res.data
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "BUYBACK RECONCILIATION NEEDED: swap {} confirmed ({} USDC -> {} ORVX) but "
                "record_buyback failed: {}",
                signature,
                amount_usdc,
                orvx_received,
                exc,
            )
            raise OrvixException(
                "Swap confirmed but DB recording failed — manual reconciliation needed",
                error_code="buyback_record_failed",
                status_code=500,
            ) from exc

        result = {
            "buyback_id": str(buyback_id) if buyback_id else None,
            "usdc_spent": format(amount_usdc, "f"),
            "orvx_received": format(orvx_received, "f"),
            "solana_signature": signature,
        }
        self._audit(executor, slippage_bps, result)
        logger.info(
            "Buyback executed: {} USDC -> {} ORVX by {} (sig={})",
            amount_usdc,
            orvx_received,
            executor,
            signature,
        )
        return result

    async def _swap(self, quote: dict) -> str:
        """Build, sign, submit, and confirm the Jupiter swap. Stubbed by default."""
        if settings.BUYBACK_STUB:
            await asyncio.sleep(0.05)  # simulate network/confirmation
            return "STUBBUY" + uuid.uuid4().hex
        # TODO (production): POST {settings.JUPITER_QUOTE_API}/swap with the quote
        # and the treasury pubkey, deserialize the returned VersionedTransaction,
        # sign it with the treasury keypair (app.services.wallet.load_keypair),
        # submit via Helius, and confirm before returning the signature.
        raise NotImplementedError(
            "Real buyback swaps are not implemented. Keep BUYBACK_STUB=true until vetted."
        )

    def _audit(self, executor: str, slippage_bps: int, result: dict) -> None:
        """Best-effort append to a dated audit file (never breaks the operation)."""
        if not settings.AUDIT_LOG_DIR:
            return
        try:
            os.makedirs(settings.AUDIT_LOG_DIR, exist_ok=True)
            stamp = _now()
            path = os.path.join(settings.AUDIT_LOG_DIR, f"buyback-{stamp:%Y-%m}.log")
            line = (
                f"{stamp.isoformat()} executor={executor} slippage_bps={slippage_bps} "
                f"usdc={result['usdc_spent']} orvx={result['orvx_received']} "
                f"sig={result['solana_signature']} id={result['buyback_id']}\n"
            )
            with open(path, "a") as f:
                f.write(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not write buyback audit log: {}", exc)
