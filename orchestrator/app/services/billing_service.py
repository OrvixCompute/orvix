"""Atomic balance operations: check-and-deduct for inference, credit for top-ups.

Balances are denominated in USDC (6 decimals). Atomicity is enforced in Postgres
via the `deduct_balance` / `credit_balance` RPC functions (see
migrations/001_initial_schema.sql). Doing the check and the decrement in a single
SQL statement avoids the read-modify-write race you'd get by fetching the balance
in Python and writing it back.
"""

from decimal import Decimal

from supabase import Client

from app.exceptions import InsufficientBalanceError
from app.logger import logger


class BillingService:
    def __init__(self, db: Client) -> None:
        self.db = db

    def get_balance(self, user_id: str) -> dict:
        res = (
            self.db.table("users")
            .select("balance_usdc, tier")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return {"balance_usdc": "0", "tier": "bronze"}
        row = res.data[0]
        return {
            "balance_usdc": str(row["balance_usdc"]),
            "tier": row["tier"],
        }

    def deduct_usdc(self, user_id: str, amount: Decimal) -> Decimal:
        """Atomically subtract `amount` USDC. Raises if balance is insufficient.

        Returns the new balance.
        """
        res = self.db.rpc(
            "deduct_balance",
            {"p_user_id": user_id, "p_amount": float(amount)},
        ).execute()

        # The function returns the new balance, or NULL when funds are insufficient.
        new_balance = res.data
        if new_balance is None:
            current = self.get_balance(user_id)["balance_usdc"]
            raise InsufficientBalanceError(
                "Insufficient USDC balance",
                details={
                    "current_balance": current,
                    "required": str(amount),
                },
            )
        logger.debug("Deducted {} USDC from {} (new balance {})", amount, user_id, new_balance)
        return Decimal(str(new_balance))

    def credit_usdc(self, user_id: str, amount: Decimal) -> Decimal:
        """Atomically add `amount` USDC. Returns the new balance."""
        res = self.db.rpc(
            "credit_balance",
            {"p_user_id": user_id, "p_amount": float(amount)},
        ).execute()
        new_balance = res.data
        logger.debug("Credited {} USDC to {} (new balance {})", amount, user_id, new_balance)
        return Decimal(str(new_balance))

    def has_sufficient_balance(self, user_id: str, amount: Decimal) -> bool:
        current = Decimal(self.get_balance(user_id)["balance_usdc"])
        return current >= amount
