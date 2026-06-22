"""Background worker that watches the treasury wallet and credits USDC top-ups.

Polling design (Helius getSignaturesForAddress every N seconds). For each new
signature we fetch the parsed transaction, extract the memo and any USDC
transfer into the treasury's token account, match the memo against a pending
top-up intent, and credit the user — idempotent on the Solana signature.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from app.config import settings
from app.database import get_supabase
from app.logger import logger
from app.services.solana_service import get_solana_service


class PaymentListener:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_signature: str | None = None
        self._treasury_token_accounts: set[str] = set()

    async def start(self) -> None:
        """Spawn the polling loop as a background task."""
        if not settings.TREASURY_WALLET_ADDRESS or not settings.USDC_MINT_ADDRESS:
            logger.warning("Payment listener not started: treasury/mint not configured")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="payment-listener")
        logger.info(
            "Payment listener started (treasury={}, interval={}s)",
            settings.TREASURY_WALLET_ADDRESS,
            settings.POLLING_INTERVAL_SECONDS,
        )

    async def stop(self) -> None:
        """Signal the loop to stop and await its completion."""
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=settings.POLLING_INTERVAL_SECONDS + 5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Payment listener stopped")

    async def _resolve_treasury_token_accounts(self) -> None:
        """Cache the treasury's token account(s) for the USDC mint."""
        try:
            sol = get_solana_service()
            accounts = await sol.get_token_accounts_by_owner(
                settings.TREASURY_WALLET_ADDRESS, settings.USDC_MINT_ADDRESS
            )
            self._treasury_token_accounts = {a["pubkey"] for a in accounts}
            logger.info("Treasury token accounts: {}", self._treasury_token_accounts or "(none yet)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not resolve treasury token accounts: {}", exc)

    async def _run(self) -> None:
        await self._resolve_treasury_token_accounts()
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.error("Payment listener cycle failed (will retry): {}", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.POLLING_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass  # normal: timeout means "interval elapsed, poll again"

    async def _poll_once(self) -> None:
        sol = get_solana_service()
        sigs = await sol.get_signatures_for_address(
            settings.TREASURY_WALLET_ADDRESS, limit=25, until=self._last_signature
        )
        if not sigs:
            return

        # Process oldest-first so last_signature advances monotonically.
        for entry in reversed(sigs):
            signature = entry["signature"]
            if entry.get("err") is not None:
                continue  # failed tx
            try:
                await self._process_signature(signature)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed processing {}: {}", signature, exc)

        self._last_signature = sigs[0]["signature"]

    async def _process_signature(self, signature: str) -> None:
        db = get_supabase()

        # Idempotency: skip if we've already recorded this signature.
        existing = (
            db.table("transactions")
            .select("id")
            .eq("solana_signature", signature)
            .limit(1)
            .execute()
        )
        if existing.data:
            return

        sol = get_solana_service()
        parsed = await sol.get_parsed_transaction(signature)
        if not parsed:
            logger.warning("Unparseable transaction skipped: {}", signature)
            return

        memo = sol.extract_memo(parsed)
        transfers = sol.extract_spl_transfers(
            parsed, settings.USDC_MINT_ADDRESS, settings.TREASURY_WALLET_ADDRESS
        )

        # Keep only transfers into a treasury-owned token account.
        if self._treasury_token_accounts:
            transfers = [
                t for t in transfers if t.get("destination") in self._treasury_token_accounts
            ]
        if not transfers:
            return  # not a USDC deposit to the treasury

        total = sum(Decimal(str(t["amount"])) for t in transfers)

        if not memo:
            logger.warning("Unattributed deposit (no memo): sig={} amount={}", signature, total)
            return

        # Match the memo to a pending, non-expired intent.
        now_iso = datetime.now(timezone.utc).isoformat()
        intent_res = (
            db.table("topup_intents")
            .select("*")
            .eq("memo", memo)
            .eq("status", "pending")
            .gt("expires_at", now_iso)
            .limit(1)
            .execute()
        )
        if not intent_res.data:
            logger.warning(
                "Unattributed deposit (no matching intent): memo={} sig={} amount={}",
                memo,
                signature,
                total,
            )
            return
        intent = intent_res.data[0]

        await self._apply_topup(db, intent, signature, total)

    async def _apply_topup(self, db, intent: dict, signature: str, amount: Decimal) -> None:
        user_id = intent["user_id"]

        # Record the ledger row AND credit the balance in a single DB
        # transaction (see migrations/004_credit_topup.sql). The unique
        # constraint on solana_signature is the sole idempotency guard: if this
        # signature was already processed the function credits nothing and
        # returns NULL. Crediting and inserting atomically removes the
        # double-credit window that existed when we credited first and only
        # inserted the ledger row afterwards.
        res = db.rpc(
            "credit_topup",
            {
                "p_user_id": user_id,
                "p_amount": float(amount),
                "p_signature": signature,
                "p_memo": intent["memo"],
                "p_intent_id": str(intent["id"]),
            },
        ).execute()

        if res.data is None:
            logger.info("Deposit {} already credited — skipping", signature)
            return

        # Update intent status based on expected vs received.
        expected = intent.get("expected_amount_usdc")
        if expected is not None and amount < Decimal(str(expected)):
            new_status = "partial"
        else:
            new_status = "fulfilled"
        db.table("topup_intents").update(
            {"status": new_status, "fulfilled_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", intent["id"]).execute()

        logger.info(
            "Top-up applied: user={} amount={} status={} sig={}",
            user_id,
            amount,
            new_status,
            signature,
        )


payment_listener = PaymentListener()
