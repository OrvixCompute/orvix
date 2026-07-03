"""SPL Token / Associated Token Account instruction builders, in pure solders.

We deliberately avoid solana-py (keeps the dependency surface to solders + raw
httpx RPC, matching solana_service.py). Instruction layouts here follow the
SPL Token and Associated Token Account program wire formats.
"""

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# SPL Token instruction discriminators.
_TRANSFER_CHECKED = 12
# Associated Token Account program: 1 = CreateIdempotent (no-op if ATA exists).
_CREATE_IDEMPOTENT = 1


def associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the ATA address for (owner, mint) — the canonical PDA."""
    address, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return address


def create_idempotent_ata_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    """CreateIdempotent instruction for the owner's ATA — safe to send if it exists."""
    ata = associated_token_address(owner, mint)
    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes([_CREATE_IDEMPOTENT]), accounts)


def transfer_checked_ix(
    *,
    source_ata: Pubkey,
    mint: Pubkey,
    dest_ata: Pubkey,
    owner: Pubkey,
    amount_raw: int,
    decimals: int,
) -> Instruction:
    """SPL transferChecked: move `amount_raw` base units of `mint` source->dest.

    transferChecked re-verifies mint + decimals on-chain, so a wrong-decimals or
    wrong-mint transfer fails instead of silently moving the wrong amount.
    """
    data = bytes([_TRANSFER_CHECKED]) + int(amount_raw).to_bytes(8, "little") + bytes([decimals])
    accounts = [
        AccountMeta(pubkey=source_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=dest_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
    ]
    return Instruction(TOKEN_PROGRAM_ID, data, accounts)
