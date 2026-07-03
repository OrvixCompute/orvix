"""ORVX holder verification with a short DB cache.

`get_holder_status(db, wallet)` returns (is_holder, orvx_balance). Results are
cached in the holder_status table for HOLDER_CACHE_TTL_MINUTES to avoid hitting
Solana RPC on every request. When ORVX_MINT_ADDRESS is unset (alpha), the balance
is 0 and nobody is a holder — the image grace period is handled by quota_service.
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from app.config import settings
from app.logger import logger
from app.services.solana_service import get_solana_service


class HolderService:
    async def get_holder_status(self, db: Client, wallet: str) -> tuple[bool, float]:
        cached = (
            db.table("holder_status")
            .select("*")
            .eq("wallet_address", wallet)
            .limit(1)
            .execute()
        )
        if cached.data and self._is_fresh(cached.data[0].get("last_checked_at")):
            row = cached.data[0]
            return bool(row["is_holder"]), float(row["orvx_balance"])

        balance = await self._query_orvx_balance(wallet)
        is_holder = balance >= settings.ORVX_HOLDER_THRESHOLD
        self._write_cache(db, wallet, balance, is_holder, exists=bool(cached.data))
        return is_holder, balance

    def _is_fresh(self, last_checked_at) -> bool:
        if not last_checked_at:
            return False
        try:
            ts = datetime.fromisoformat(str(last_checked_at))
        except (TypeError, ValueError):
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < settings.HOLDER_CACHE_TTL_MINUTES * 60

    def _write_cache(
        self, db: Client, wallet: str, balance: float, is_holder: bool, exists: bool
    ) -> None:
        payload = {
            "wallet_address": wallet,
            "orvx_balance": balance,
            "is_holder": is_holder,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if exists:
                db.table("holder_status").update(payload).eq(
                    "wallet_address", wallet
                ).execute()
            else:
                db.table("holder_status").insert(payload).execute()
        except Exception as exc:  # noqa: BLE001 — cache write is best-effort
            logger.warning("Failed to cache holder status for {}: {}", wallet, exc)

    async def _query_orvx_balance(self, wallet: str) -> float:
        """Sum the wallet's ORVX token-account balances. 0.0 if the mint is unset."""
        mint = settings.ORVX_MINT_ADDRESS
        if not mint:
            return 0.0
        try:
            accounts = await get_solana_service().get_token_accounts_by_owner(wallet, mint)
        except Exception as exc:  # noqa: BLE001 — treat RPC failure as non-holder
            logger.warning("ORVX balance query failed for {}: {}", wallet, exc)
            return 0.0
        total = 0.0
        for acc in accounts:
            try:
                amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                total += float(amount or 0)
            except (KeyError, TypeError, ValueError):
                continue
        return total


holder_service = HolderService()
