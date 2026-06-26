"""Provider staking: intents, unstaking, status, and public transparency data.

Staking is custodial in this alpha: the user sends ORVX (with a memo) to the
treasury, the payment listener credits ``users.staked_orvx`` atomically via the
``stake_orvx`` RPC, and tier/eligibility are derived from that column.

Unstaking debits the stake atomically (``unstake_orvx`` enforces the provider
minimum) and queues an ORVX payout. The on-chain ORVX send is NOT wired through
the USDC payout worker — it is recorded as a manual-approval withdrawal tagged
``asset: ORVX`` so the USDC stub worker leaves it alone. A dedicated ORVX payout
path is future work.
"""

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from solders.pubkey import Pubkey
from supabase import Client

from app.config import settings
from app.exceptions import InsufficientBalanceError, ValidationError
from app.logger import logger
from app.services import tier_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StakingService:
    def __init__(self, db: Client) -> None:
        self.db = db

    # --- intents -----------------------------------------------------------
    def create_stake_intent(self, user_id: str, amount: Decimal) -> dict:
        memo = f"orvix_stake_{secrets.token_hex(6)}"  # 12 hex chars
        expires_at = _now() + timedelta(minutes=settings.STAKE_INTENT_TTL_MINUTES)

        row = {
            "user_id": user_id,
            "memo": memo,
            "expected_amount": float(amount),
            "status": "pending",
            "expires_at": expires_at.isoformat(),
        }
        intent = self.db.table("staking_intents").insert(row).execute().data[0]

        treasury = settings.TREASURY_WALLET_ADDRESS
        qr = f"solana:{treasury}?spl-token={settings.ORVX_MINT_ADDRESS}&amount={amount}&memo={memo}"
        return {
            "intent_id": str(intent["id"]),
            "treasury_address": treasury,
            "memo": memo,
            "amount": amount,
            "expires_at": expires_at,
            "qr_data": qr,
        }

    # --- unstaking ---------------------------------------------------------
    def _validate_wallet(self, wallet: str) -> None:
        try:
            Pubkey.from_string(wallet)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("Invalid Solana destination wallet") from exc

    def unstake(self, user: dict, amount: Decimal, destination_wallet: str | None) -> dict:
        destination = destination_wallet or user["wallet_address"]
        self._validate_wallet(destination)

        staked = Decimal(str(user.get("staked_orvx", 0)))
        if amount > staked:
            raise InsufficientBalanceError(
                "Unstake amount exceeds staked balance",
                details={"staked": format(staked, "f"), "requested": format(amount, "f")},
            )

        minimum = Decimal(settings.PROVIDER_MIN_STAKE_ORVX)
        if user.get("is_provider") and (staked - amount) < minimum:
            raise ValidationError(
                f"Providers must keep at least {settings.PROVIDER_MIN_STAKE_ORVX} ORVX staked. "
                "Deregister as a provider before unstaking below the minimum.",
                error_code="provider_minimum_stake",
                details={
                    "staked": format(staked, "f"),
                    "requested": format(amount, "f"),
                    "minimum": format(minimum, "f"),
                },
            )

        # Atomic debit + provider-floor enforcement (source of truth).
        ok = self.db.rpc(
            "unstake_orvx",
            {
                "p_user_id": user["id"],
                "p_amount": float(amount),
                "p_solana_sig": None,
                "p_reason": "unstake withdrawal",
            },
        ).execute()
        if not ok.data:
            # Lost a race against another mutation, or the floor check changed.
            raise ValidationError(
                "Unstake could not be completed; balance changed. Please retry.",
                error_code="unstake_failed",
            )

        # Queue an ORVX payout. Tagged manual so the USDC stub worker skips it.
        withdrawal = (
            self.db.table("withdrawals")
            .insert(
                {
                    "user_id": user["id"],
                    "amount": float(amount),
                    "destination_wallet": destination,
                    "status": "queued",
                    "metadata": {
                        "asset": "ORVX",
                        "kind": "unstake",
                        "manual_approval_required": True,
                    },
                }
            )
            .execute()
            .data[0]
        )
        logger.info(
            "Queued ORVX unstake withdrawal {} for user {} ({} ORVX -> {})",
            withdrawal["id"],
            user["id"],
            amount,
            destination,
        )
        return withdrawal

    # --- status ------------------------------------------------------------
    def get_status(self, user_id: str) -> dict:
        urow = (
            self.db.table("users")
            .select("staked_orvx, stake_locked_until")
            .eq("id", user_id)
            .limit(1)
            .execute()
            .data
        )
        urow = urow[0] if urow else {}
        staked = Decimal(str(urow.get("staked_orvx", 0)))

        rows = (
            self.db.table("stakes")
            .select("type, amount, reason, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )
        history = [
            {
                "type": r["type"],
                "amount": str(r.get("amount")),
                "reason": r.get("reason"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

        return {
            "staked_orvx": format(staked, "f"),
            "stake_locked_until": urow.get("stake_locked_until"),
            "tier": tier_service.tier_for_stake(staked),
            "next_tier": tier_service.next_tier_info(staked),
            "history": history,
        }

    # --- public transparency ----------------------------------------------
    def buyback_history(self, limit: int) -> list[dict]:
        return (
            self.db.table("buyback_events")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    def burn_history(self, limit: int) -> list[dict]:
        return (
            self.db.table("burn_events")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    def network_stats(self) -> dict:
        acct = (
            self.db.table("global_accounting").select("*").eq("id", 1).limit(1).execute().data
        )
        acct = acct[0] if acct else {}

        providers = (
            self.db.table("users")
            .select("id", count="exact")
            .eq("is_provider", True)
            .execute()
        )

        # Sum staked ORVX across all users.
        staked_rows = self.db.table("users").select("staked_orvx").execute().data or []
        total_staked = sum(Decimal(str(r.get("staked_orvx", 0) or 0)) for r in staked_rows)

        last_buyback = self.buyback_history(1)
        last_burn = self.burn_history(1)

        return {
            "total_staked": format(total_staked, "f"),
            "total_providers": providers.count or 0,
            "buyback_budget_usdc": str(acct.get("buyback_budget_usdc", 0) or 0),
            "orvx_held_for_burn": str(acct.get("orvx_held_for_burn", 0) or 0),
            "total_orvx_burned": str(acct.get("total_orvx_burned", 0) or 0),
            "total_orvx_bought": str(acct.get("total_orvx_bought", 0) or 0),
            "last_buyback_at": last_buyback[0]["created_at"] if last_buyback else None,
            "last_burn_at": last_burn[0]["created_at"] if last_burn else None,
        }
