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

# One RPC page of signatures. Solana caps this at 1000; 25 keeps each cycle
# cheap on a quiet address, and the catch-up loop handles busy ones.
SIGNATURE_PAGE_SIZE = 25
# Ceiling on how far a single cycle will page back. 40 pages is 1000 signatures
# — far more than a poll interval can accumulate, and a hard stop against a
# cursor that has somehow fallen a long way behind.
MAX_CATCHUP_PAGES = 40


class PaymentListener:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Cursor per watched address, not one shared cursor: the addresses are
        # polled independently and a single cursor would let one address's
        # newest signature hide another's.
        self._last_signature: dict[str, str] = {}
        self._treasury_token_accounts: set[str] = set()
        self._treasury_orvx_token_accounts: set[str] = set()

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
            logger.info("Treasury USDC token accounts: {}", self._treasury_token_accounts or "(none yet)")

            if settings.ORVX_MINT_ADDRESS:
                orvx_accounts = await sol.get_token_accounts_by_owner(
                    settings.TREASURY_WALLET_ADDRESS, settings.ORVX_MINT_ADDRESS
                )
                self._treasury_orvx_token_accounts = {a["pubkey"] for a in orvx_accounts}
                logger.info(
                    "Treasury ORVX token accounts: {}",
                    self._treasury_orvx_token_accounts or "(none yet)",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not resolve treasury token accounts: {}", exc)

    # --- cursors -----------------------------------------------------------
    def _load_cursors(self) -> None:
        """Restore how far each address was read before the last shutdown.

        Failure is non-fatal: an empty cursor means the next poll starts from
        the newest page, which is where the listener always used to start. A
        listener that refuses to run because it cannot read its bookmark is
        worse than one that re-reads a page.
        """
        try:
            rows = get_supabase().table("listener_cursors").select("*").execute().data or []
            self._last_signature = {r["address"]: r["last_signature"] for r in rows}
            if self._last_signature:
                logger.info("Restored {} listener cursor(s)", len(self._last_signature))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load listener cursors (starting fresh): {}", exc)

    def _save_cursor(self, address: str, signature: str) -> None:
        """Persist progress for one address, after its signatures were handled.

        Written per address rather than in a batch at the end of a cycle: if the
        process dies mid-cycle, the addresses already finished keep their
        progress instead of all of them rewinding together.
        """
        self._last_signature[address] = signature
        try:
            get_supabase().table("listener_cursors").upsert(
                {
                    "address": address,
                    "last_signature": signature,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="address",
            ).execute()
        except Exception as exc:  # noqa: BLE001
            # In-memory cursor still advanced, so this cycle does not loop; the
            # cost of a failed write is re-reading one page after a restart.
            logger.warning("Could not persist listener cursor for {}: {}", address, exc)

    async def _fetch_new_signatures(self, sol, address: str) -> list[dict]:
        """Every signature on `address` newer than its cursor, oldest last.

        Pages backwards rather than taking a single window. `until` stops the
        RPC at the cursor but `limit` still caps the response, so a burst larger
        than one page used to leave the middle unread while the cursor jumped to
        the newest — the gap this exists to close.

        Bounded by MAX_CATCHUP_PAGES so a cursor that is somehow far in the past
        cannot spin the loop forever. Hitting the cap is logged, because it means
        older transactions were skipped and that must not pass silently.
        """
        cursor = self._last_signature.get(address)
        collected: list[dict] = []
        before: str | None = None

        for page in range(MAX_CATCHUP_PAGES):
            sigs = await sol.get_signatures_for_address(
                address, limit=SIGNATURE_PAGE_SIZE, until=cursor, before=before
            )
            if not sigs:
                break
            collected.extend(sigs)
            if len(sigs) < SIGNATURE_PAGE_SIZE:
                break  # reached the cursor (or the end of history)
            before = sigs[-1]["signature"]
            if page == MAX_CATCHUP_PAGES - 1:
                logger.error(
                    "Listener catch-up hit {} pages on {} — older transactions were "
                    "NOT read. Cursor was {}.",
                    MAX_CATCHUP_PAGES,
                    address,
                    cursor or "unset",
                )
        return collected

    async def _run(self) -> None:
        self._load_cursors()
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
        # Re-resolve the treasury's token accounts if we never got them (initial
        # RPC error, or no USDC ATA existed yet because no deposit had arrived).
        # Otherwise the destination safety-filter in _process_signature stays
        # disabled for the whole run, weakening deposit attribution.
        if not self._treasury_token_accounts:
            await self._resolve_treasury_token_accounts()

        sol = get_solana_service()
        for address in self._watched_addresses():
            sigs = await self._fetch_new_signatures(sol, address)
            if not sigs:
                continue

            # Process oldest-first so the cursor advances monotonically: if this
            # dies partway, the saved cursor is behind the failure rather than
            # past it, and the rest is retried next cycle.
            for entry in reversed(sigs):
                signature = entry["signature"]
                if entry.get("err") is not None:
                    continue  # failed tx
                try:
                    await self._process_signature(signature)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed processing {}: {}", signature, exc)

            self._save_cursor(address, sigs[0]["signature"])

    def _watched_addresses(self) -> list[str]:
        """Addresses to poll for incoming deposits.

        The token accounts are the ones that matter, and watching only the owner
        wallet is why no deposit was ever seen: `getSignaturesForAddress` returns
        transactions the address appears in, and an SPL transfer into an ATA
        names the ATA, the mint and the sender — not the ATA's owner. Confirmed
        against a real 0.11 USDC deposit, which appeared under the treasury's
        USDC ATA and was entirely absent from the wallet's own history.

        The wallet is still polled: it catches transactions the treasury itself
        signs, and dropping it would lose that for no gain.
        """
        addresses = [settings.TREASURY_WALLET_ADDRESS]
        addresses.extend(sorted(self._treasury_token_accounts))
        addresses.extend(sorted(self._treasury_orvx_token_accounts))
        # Dedupe while keeping order stable, so cursors stay attached to the
        # same address across cycles.
        seen: set[str] = set()
        return [a for a in addresses if a and not (a in seen or seen.add(a))]

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

        # Stake deposits use a distinct memo prefix and the ORVX mint, not USDC.
        if memo and memo.startswith("orvix_stake_"):
            await self._process_stake(db, parsed, signature, memo)
            return

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

        # Attribution, in order of confidence.
        #
        # 1. The memo. It is the only thing that works when the signer is not the
        #    depositor — an exchange withdrawal arrives signed by the exchange's
        #    hot wallet, so nothing about the sender identifies the customer.
        # 2. Failing that, the signing wallet. Users authenticate by signing with
        #    a Solana wallet, so a deposit sent from that same wallet identifies
        #    itself. This covers the case the memo path handles worst: someone
        #    who simply sent USDC to the address without one, or whose 30-minute
        #    intent expired before they got round to sending.
        #
        # Before this fallback existed a memo-less deposit was logged and
        # abandoned — the money sat in the treasury, credited to nobody, and the
        # depositor had no way to tell why.
        intent = None
        if memo:
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
            if intent_res.data:
                intent = intent_res.data[0]

        if intent is not None:
            await self._apply_topup(db, intent, signature, total)
            return

        sender = self._signing_wallet(transfers)
        user_id = self._user_id_for_wallet(db, sender) if sender else None
        if user_id:
            logger.info(
                "Deposit attributed by sender wallet (memo={}): sig={} wallet={} amount={}",
                memo or "none",
                signature,
                sender,
                total,
            )
            await self._credit(db, user_id, signature, total, memo=memo, intent=None)
            return

        logger.warning(
            "Unattributed deposit: sig={} amount={} memo={} sender={} — no matching "
            "intent and the sending wallet belongs to no account",
            signature,
            total,
            memo or "none",
            sender or "unknown",
        )

    @staticmethod
    def _signing_wallet(transfers: list[dict]) -> str | None:
        """The wallet that authorised the transfer, i.e. the depositor.

        `source` is the sender's token account, not their wallet — matching on it
        would never find a user. `authority` is the account that signed, which is
        what users register when they log in.
        """
        for t in transfers:
            authority = t.get("authority")
            if authority:
                return str(authority)
        return None

    @staticmethod
    def _user_id_for_wallet(db, wallet: str) -> str | None:
        res = (
            db.table("users").select("id").eq("wallet_address", wallet).limit(1).execute()
        )
        return res.data[0]["id"] if res.data else None

    async def _credit(
        self,
        db,
        user_id: str,
        signature: str,
        amount: Decimal,
        *,
        memo: str | None,
        intent: dict | None,
    ) -> bool:
        """Credit a deposit atomically. Returns False if already credited.

        Record the ledger row AND credit the balance in a single DB transaction
        (see migrations/004_credit_topup.sql). The unique constraint on
        solana_signature is the sole idempotency guard: if this signature was
        already processed the function credits nothing and returns NULL.
        Crediting and inserting atomically removes the double-credit window that
        existed when we credited first and only inserted the ledger row
        afterwards.

        `intent` is optional — a deposit attributed by its sending wallet has no
        intent to point at, and the function stores a null intent_id happily.
        """
        res = db.rpc(
            "credit_topup",
            {
                "p_user_id": user_id,
                "p_amount": float(amount),
                "p_signature": signature,
                "p_memo": memo,
                "p_intent_id": str(intent["id"]) if intent else None,
            },
        ).execute()

        if res.data is None:
            logger.info("Deposit {} already credited — skipping", signature)
            return False

        logger.info(
            "Top-up applied: user={} amount={} sig={} via={}",
            user_id,
            amount,
            signature,
            "intent" if intent else "sender-wallet",
        )
        return True

    async def _apply_topup(self, db, intent: dict, signature: str, amount: Decimal) -> None:
        user_id = intent["user_id"]

        if not await self._credit(
            db, user_id, signature, amount, memo=intent["memo"], intent=intent
        ):
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

        logger.info("Intent {} marked {} (sig={})", intent["id"], new_status, signature)

    # --- staking deposits --------------------------------------------------
    async def _process_stake(self, db, parsed: dict, signature: str, memo: str) -> None:
        if not settings.ORVX_MINT_ADDRESS:
            logger.warning(
                "Stake deposit seen but ORVX_MINT_ADDRESS not configured: sig={}", signature
            )
            return

        sol = get_solana_service()
        transfers = sol.extract_spl_transfers(
            parsed, settings.ORVX_MINT_ADDRESS, settings.TREASURY_WALLET_ADDRESS
        )
        if self._treasury_orvx_token_accounts:
            transfers = [
                t for t in transfers if t.get("destination") in self._treasury_orvx_token_accounts
            ]
        if not transfers:
            return  # not an ORVX deposit to the treasury

        total = sum(Decimal(str(t["amount"])) for t in transfers)

        now_iso = datetime.now(timezone.utc).isoformat()
        intent_res = (
            db.table("staking_intents")
            .select("*")
            .eq("memo", memo)
            .eq("status", "pending")
            .gt("expires_at", now_iso)
            .limit(1)
            .execute()
        )
        if not intent_res.data:
            logger.warning(
                "Unattributed stake deposit (no matching intent): memo={} sig={} amount={}",
                memo,
                signature,
                total,
            )
            return

        await self._apply_stake(db, intent_res.data[0], signature, total)

    async def _apply_stake(self, db, intent: dict, signature: str, amount: Decimal) -> None:
        user_id = intent["user_id"]

        # stake_orvx credits users.staked_orvx and logs the stakes row atomically.
        # It is idempotent on the on-chain signature: a duplicate returns false.
        res = db.rpc(
            "stake_orvx",
            {
                "p_user_id": user_id,
                "p_amount": float(amount),
                "p_solana_sig": signature,
                "p_reason": "stake deposit",
            },
        ).execute()

        if not res.data:
            logger.info("Stake {} already credited — skipping", signature)
            return

        db.table("staking_intents").update(
            {"status": "fulfilled", "fulfilled_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", intent["id"]).execute()

        logger.info("Stake applied: user={} amount={} ORVX sig={}", user_id, amount, signature)


payment_listener = PaymentListener()
