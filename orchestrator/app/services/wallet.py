"""Treasury keypair loading + the multi-wallet (cold/hot/payout) service.

Keypair files are JSON byte arrays (the `solana-keygen` / Phantom-export format),
readable only by the service user (chmod 600). Loaded lazily so importing this
module never touches disk.

Roles:
- main   — cold storage; PUBLIC key only on the server (private key stays offline)
- hot    — receives deposits; keypair = TREASURY_KEYPAIR_PATH, address = TREASURY_WALLET_ADDRESS
- payout — provider payout signer; keypair = PAYOUT_KEYPAIR_PATH
"""

import base64
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from app.config import settings
from app.logger import logger
from app.services import spl
from app.services.solana_service import get_solana_service

USDC_DECIMALS = 6
ROLES = ("main", "hot", "payout")


def load_keypair(path: str) -> Keypair:
    """Load a Solana keypair from a JSON byte-array file."""
    if not path:
        raise ValueError("No keypair path configured (set TREASURY_KEYPAIR_PATH)")
    with open(path) as f:
        secret = json.load(f)
    return Keypair.from_bytes(bytes(secret))


class WalletService:
    """Loads treasury wallets by role and performs balance sync + USDC sends."""

    def public_key(self, role: str) -> str:
        """Configured public key for a role ('' if unset)."""
        return {
            "main": settings.TREASURY_MAIN_PUBLIC,
            "hot": settings.TREASURY_WALLET_ADDRESS,
            "payout": settings.PAYOUT_WALLET_PUBLIC,
        }[role]

    def _keypair_path(self, role: str) -> str:
        if role == "hot":
            return settings.TREASURY_KEYPAIR_PATH
        if role == "payout":
            return settings.PAYOUT_KEYPAIR_PATH
        raise ValueError(f"Role {role!r} has no signing key on the server (main is offline)")

    def get_keypair(self, role: str) -> Keypair:
        """Load the signing keypair for a role. Raises for 'main' (offline)."""
        path = self._keypair_path(role)
        if not path:
            raise ValueError(f"No keypair path configured for role {role!r}")
        return load_keypair(path)

    async def get_usdc_balance(self, role: str) -> Decimal:
        pub = self.public_key(role)
        if not pub or not settings.USDC_MINT_ADDRESS:
            return Decimal(0)
        return await get_solana_service().get_token_balance(pub, settings.USDC_MINT_ADDRESS)

    async def _orvx_balance(self, pub: str) -> Decimal | None:
        if not settings.ORVX_MINT_ADDRESS:
            return None
        return await get_solana_service().get_token_balance(pub, settings.ORVX_MINT_ADDRESS)

    async def sync_balances(self, db) -> list[dict]:
        """Refresh on-chain balances into the treasury_wallets table."""
        now = datetime.now(timezone.utc).isoformat()
        out: list[dict] = []
        for role in ROLES:
            pub = self.public_key(role)
            if not pub:
                continue
            usdc = await self.get_usdc_balance(role)
            orvx = await self._orvx_balance(pub)
            update = {
                "public_key": pub,
                "balance_usdc": float(usdc),
                "balance_last_synced_at": now,
            }
            if orvx is not None:
                update["balance_orvx"] = float(orvx)
            db.table("treasury_wallets").update(update).eq("wallet_role", role).execute()
            out.append({"role": role, "public_key": pub, "balance_usdc": float(usdc)})
        return out

    async def send_usdc(self, from_role: str, to_owner: str, amount: Decimal, *, stub: bool) -> str:
        """Send `amount` USDC from a role wallet to `to_owner`. Returns the signature.

        Stubbed by default (TREASURY_SWEEP_STUB / PAYOUT_STUB) — returns a fake
        signature without touching the chain. The real path builds an SPL
        transferChecked, signs with the role keypair, and submits via RPC.
        """
        if stub:
            fake = "STUB_SEND_" + uuid.uuid4().hex[:16]
            logger.info("[STUB] send_usdc {} {} -> {} (sig={})", amount, from_role, to_owner, fake)
            return fake

        if not settings.USDC_MINT_ADDRESS:
            raise ValueError("USDC_MINT_ADDRESS not configured")

        kp = self.get_keypair(from_role)
        mint = Pubkey.from_string(settings.USDC_MINT_ADDRESS)
        source_ata = spl.associated_token_address(kp.pubkey(), mint)
        dest_ata = spl.associated_token_address(Pubkey.from_string(to_owner), mint)
        amount_raw = int((Decimal(amount) * (Decimal(10) ** USDC_DECIMALS)).to_integral_value())

        ix = spl.transfer_checked_ix(
            source_ata=source_ata,
            mint=mint,
            dest_ata=dest_ata,
            owner=kp.pubkey(),
            amount_raw=amount_raw,
            decimals=USDC_DECIMALS,
        )

        sol = get_solana_service()
        blockhash = Hash.from_string(await sol.get_latest_blockhash())
        tx = Transaction.new_signed_with_payer([ix], kp.pubkey(), [kp], blockhash)
        sig = await sol.send_raw_transaction(base64.b64encode(bytes(tx)).decode())
        logger.info("send_usdc {} {} -> {} submitted (sig={})", amount, from_role, to_owner, sig)
        return sig


wallet_service = WalletService()
