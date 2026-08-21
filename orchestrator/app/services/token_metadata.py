"""SPL token-metadata (Metaplex) reading via the metadata PDA.

Derives the canonical metadata account for a mint:
    PDA = findProgramAddress(["metadata", PROGRAM_ID, mint], PROGRAM_ID)
and parses the v1 Metadata layout from the account's raw bytes:

    key (u8) | update_authority (32) | mint (32)
    | name (u32 len + utf8) | symbol (u32 len + utf8) | uri (u32 len + utf8)

Everything is fail-soft: any parse error or missing account returns None, so a
token without on-chain metadata reads as "unknown" rather than erroring.
"""

from __future__ import annotations

import base64
import struct
from typing import Optional

from solders.pubkey import Pubkey

from app.config import settings
from app.logger import logger
from app.services.solana_service import SolanaService

# Metadata key values: 4 = MetadataV1 (the account we read).
METADATA_KEY_V1 = 4


def metadata_program_id() -> Pubkey:
    return Pubkey.from_string(settings.TOKEN_METADATA_PROGRAM_ID)


def metadata_pda(mint: str) -> Pubkey:
    """Derive the Metaplex metadata PDA for a mint."""
    mint_pk = Pubkey.from_string(mint)
    program = metadata_program_id()
    seeds = [b"metadata", bytes(program), bytes(mint_pk)]
    pda, _bump = Pubkey.find_program_address(seeds, program)
    return pda


def _read_string(data: bytes, offset: int) -> tuple[Optional[str], int]:
    """Read a length-prefixed UTF-8 string at `offset`.

    Returns (value, new_offset); (None, offset) on truncation.
    """
    if offset + 4 > len(data):
        return None, offset
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    if length > 200:  # absurd length guard — metadata strings are short
        return None, offset
    if offset + length > len(data):
        return None, offset
    try:
        return data[offset : offset + length].decode("utf-8"), offset + length
    except UnicodeDecodeError:
        return None, offset


def parse_metadata_account(raw_b64: str) -> Optional[dict]:
    """Decode + parse a base64 metadata account into {name, symbol, uri, ...}.

    Returns None when the bytes are not a MetadataV1 account or fail to parse.
    """
    try:
        data = base64.b64decode(raw_b64)
    except (ValueError, TypeError) as exc:
        logger.warning("Metadata account is not valid base64: {}", exc)
        return None

    if len(data) < 1:
        return None
    key = data[0]
    if key != METADATA_KEY_V1:
        return None  # not a MetadataV1 account (e.g. a MintV1, or an empty account)

    if len(data) < 1 + 32 + 32:
        return None
    update_authority = str(Pubkey.from_bytes(data[1:33]))
    mint = str(Pubkey.from_bytes(data[33:65]))

    offset = 65
    name, offset = _read_string(data, offset)
    symbol, offset = _read_string(data, offset)
    uri, offset = _read_string(data, offset)

    if name is None or symbol is None or uri is None:
        return None

    return {
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "update_authority": update_authority,
        "mint": mint,
    }


async def fetch_metadata(sol: SolanaService, mint: str) -> Optional[dict]:
    """Fetch + parse on-chain metadata for a mint. Fail-soft -> None."""
    try:
        pda = metadata_pda(mint)
    except Exception as exc:  # noqa: BLE001 — invalid mint address
        logger.warning("Cannot derive metadata PDA for {}: {}", mint, exc)
        return None

    try:
        account = await sol.get_account_info(str(pda))
    except Exception as exc:  # noqa: BLE001 — RPC failure is not fatal
        logger.warning("Metadata RPC failed for {}: {}", mint, exc)
        return None

    if not account:
        return None
    data = account.get("data")
    if isinstance(data, list) and data:
        raw_b64 = data[0]
    elif isinstance(data, str):
        raw_b64 = data
    else:
        return None
    return parse_metadata_account(raw_b64)
