"""Non-custodial user staking via the Orvix Anchor program.

Users lock ORVX in a program-owned vault on-chain; the orchestrator never
touches the tokens. The server builds *unsigned* transactions for the user to
sign in their wallet (matching the existing wallet-auth flow), and reads stake
state back from the program's per-user PDA.

This is opt-in: when USER_STAKING_PROGRAM_ID is empty the feature is disabled
and the routes return 404.
"""

from __future__ import annotations

from decimal import Decimal

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from app.config import settings
from app.exceptions import ValidationError
from app.services.solana_service import get_solana_service

# Anchor instruction discriminators = first 8 bytes of sha256("global:stake") /
# sha256("global:unstake"). Computed lazily below.
import hashlib


def _discriminator(namespace: str, name: str) -> bytes:
    return hashlib.sha256(f"{namespace}:{name}".encode()).digest()[:8]


STAKE_DISCRIMINATOR = _discriminator("global", "stake")
UNSTAKE_DISCRIMINATOR = _discriminator("global", "unstake")

# SPL Token program (legacy) and the associated-token program.
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Allowed lock periods (days), mirrored from the Anchor program.
ALLOWED_LOCK_DAYS = (3, 7, 14)


def _program_id() -> Pubkey:
    if not settings.USER_STAKING_PROGRAM_ID:
        raise ValidationError(
            "User staking is not configured on this deployment.",
            error_code="user_staking_not_configured",
        )
    return Pubkey.from_string(settings.USER_STAKING_PROGRAM_ID)


def _pda(seeds: list[bytes]) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address(seeds, _program_id())


def stake_vault_address() -> Pubkey:
    """Program-owned token account holding all staked ORVX."""
    addr, _ = _pda([settings.USER_STAKING_VAULT_SEED.encode()])
    return addr


def stake_vault_authority_address() -> Pubkey:
    """PDA that is the vault token account's authority (signs CPI transfers)."""
    # Same seed as the vault; used as a separate signer account in the program.
    addr, _ = _pda([settings.USER_STAKING_VAULT_SEED.encode()])
    return addr


def stake_account_address(wallet: str) -> Pubkey:
    """Per-user StakeAccount PDA: seeds [STAKE_SEED, owner]."""
    owner = Pubkey.from_string(wallet)
    addr, _ = _pda([settings.USER_STAKING_STAKE_SEED.encode(), bytes(owner)])
    return addr


def _ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    addr, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return addr


class UserStakingService:
    """Builds unsigned Anchor transactions and reads on-chain stake state."""

    def __init__(self) -> None:
        pass

    @property
    def sol(self):
        """Resolve lazily so tests can patch get_solana_service before use."""
        return get_solana_service()

    # --- transaction building ----------------------------------------------
    async def build_stake_transaction(
        self, wallet: str, amount: Decimal, lock_days: int
    ) -> dict:
        """Return an unsigned `stake` transaction the user signs and submits.

        The client sends the serialized transaction (or its signature) back;
        the server can also submit it on behalf of the user once signed.
        """
        if lock_days not in ALLOWED_LOCK_DAYS:
            raise ValidationError(
                f"lock_days must be one of {ALLOWED_LOCK_DAYS}",
                error_code="invalid_lock_period",
            )
        if amount <= 0:
            raise ValidationError("amount must be > 0", error_code="zero_amount")

        owner = Pubkey.from_string(wallet)
        mint = Pubkey.from_string(settings.ORVX_MINT_ADDRESS)
        user_ata = _ata(owner, mint)
        vault = stake_vault_address()
        vault_authority = stake_vault_authority_address()
        stake_pda = stake_account_address(wallet)

        amount_raw = int(amount * (Decimal(10) ** settings.ORVX_DECIMALS))

        # Account list must match the Anchor program's Stake struct order:
        # owner, stake_account, user_ata, vault, vault_authority, mint,
        # token_program, system_program.
        accounts = [
            AccountMeta(owner, is_signer=True, is_writable=True),
            AccountMeta(stake_pda, is_signer=False, is_writable=True),
            AccountMeta(user_ata, is_signer=False, is_writable=True),
            AccountMeta(vault, is_signer=False, is_writable=True),
            AccountMeta(vault_authority, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        # Anchor encodes args as borsh: u64 little-endian + i64 little-endian.
        data = STAKE_DISCRIMINATOR + amount_raw.to_bytes(8, "little") + lock_days.to_bytes(8, "little", signed=True)
        ix = Instruction(_program_id(), data, accounts)

        blockhash = await self.sol.get_latest_blockhash()
        msg = Message.new_with_blockhash([ix], owner, Hash.from_string(blockhash))
        tx = Transaction.new_unsigned(msg)
        return {
            "transaction": bytes(tx).hex(),
            "blockhash": blockhash,
            "vault_address": str(vault),
            "stake_account": str(stake_pda),
            "program_id": str(_program_id()),
        }

    async def build_unstake_transaction(self, wallet: str, amount: Decimal) -> dict:
        """Return an unsigned `unstake` transaction the user signs and submits."""
        if amount <= 0:
            raise ValidationError("amount must be > 0", error_code="zero_amount")

        owner = Pubkey.from_string(wallet)
        mint = Pubkey.from_string(settings.ORVX_MINT_ADDRESS)
        user_ata = _ata(owner, mint)
        vault = stake_vault_address()
        vault_authority = stake_vault_authority_address()
        stake_pda = stake_account_address(wallet)

        amount_raw = int(amount * (Decimal(10) ** settings.ORVX_DECIMALS))

        accounts = [
            AccountMeta(owner, is_signer=True, is_writable=True),
            AccountMeta(stake_pda, is_signer=False, is_writable=True),
            AccountMeta(user_ata, is_signer=False, is_writable=True),
            AccountMeta(vault, is_signer=False, is_writable=True),
            AccountMeta(vault_authority, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        data = UNSTAKE_DISCRIMINATOR + amount_raw.to_bytes(8, "little")
        ix = Instruction(_program_id(), data, accounts)

        blockhash = await self.sol.get_latest_blockhash()
        msg = Message.new_with_blockhash([ix], owner, Hash.from_string(blockhash))
        tx = Transaction.new_unsigned(msg)
        return {
            "transaction": bytes(tx).hex(),
            "blockhash": blockhash,
            "vault_address": str(vault),
            "stake_account": str(stake_pda),
            "program_id": str(_program_id()),
        }

    # --- submitting a signed transaction -----------------------------------
    async def submit_transaction(self, signed_tx_hex: str) -> dict:
        """Submit a user-signed transaction (hex) to the network.

        The user signs the unsigned transaction returned by build_stake_* /
        build_unstake_* in their wallet, then hands it back here to broadcast.
        Returns the transaction signature.
        """
        try:
            raw = bytes.fromhex(signed_tx_hex)
        except (TypeError, ValueError):
            raise ValidationError(
                "transaction must be hex-encoded", error_code="invalid_transaction"
            )
        if not raw:
            raise ValidationError(
                "transaction is empty", error_code="invalid_transaction"
            )
        import base64

        sig = await self.sol.send_raw_transaction(base64.b64encode(raw).decode())
        return {"signature": sig}

    # --- reading on-chain state ---------------------------------------------
    async def get_stake_status(self, wallet: str) -> dict:
        """Read the user's StakeAccount from the chain.

        Returns zeroed state when the account does not exist yet (never staked).
        """
        stake_pda = stake_account_address(wallet)
        # getAccountInfo via the RPC; parse the 8-byte discriminator + fields.
        result = await self.sol._rpc(
            "getAccountInfo",
            [str(stake_pda), {"encoding": "base64", "commitment": "confirmed"}],
        )
        value = result.get("value") if result else None
        if not value:
            return {
                "wallet": wallet,
                "staked_orvx": "0",
                "stake_locked_until": None,
                "created_at": None,
                "tier": "bronze",
                "next_tier": None,
            }

        data_b64 = value.get("data")
        if isinstance(data_b64, list):
            data_b64 = data_b64[0]
        raw = __import__("base64").b64decode(data_b64)

        # Layout: [8-byte discriminator][owner:32][amount:u64][locked_until:i64][created:i64]
        if len(raw) < 8 + 32 + 8 + 8 + 8:
            return {"wallet": wallet, "staked_orvx": "0", "stake_locked_until": None,
                    "created_at": None, "tier": "bronze", "next_tier": None}

        from datetime import datetime, timezone

        amount_raw = int.from_bytes(raw[40:48], "little")
        locked_until = int.from_bytes(raw[48:56], "little", signed=True)
        created_at = int.from_bytes(raw[56:64], "little", signed=True)

        amount = Decimal(amount_raw) / (Decimal(10) ** settings.ORVX_DECIMALS)

        from app.services import tier_service

        tier = tier_service.tier_for_stake(amount)
        next_tier = tier_service.next_tier_info(amount)

        return {
            "wallet": wallet,
            "staked_orvx": format(amount, "f"),
            "stake_locked_until": (
                datetime.fromtimestamp(locked_until, tz=timezone.utc).isoformat()
                if locked_until
                else None
            ),
            "created_at": (
                datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
                if created_at
                else None
            ),
            "tier": tier,
            "next_tier": next_tier,
        }


user_staking_service = UserStakingService()
