"""Tests for Metaplex token-metadata parsing (services/token_metadata.py)."""

import base64
import struct

import pytest
from solders.pubkey import Pubkey

from app.services import token_metadata


def _metadata_bytes(name: str, symbol: str, uri: str) -> bytes:
    """Craft a MetadataV1 account payload (not including the 'key' byte)."""
    update_authority = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    data = bytearray()
    data.append(token_metadata.METADATA_KEY_V1)
    data += bytes(update_authority)
    data += bytes(mint)
    for field in (name, symbol, uri):
        encoded = field.encode("utf-8")
        data += struct.pack("<I", len(encoded))
        data += encoded
    return bytes(data), str(update_authority), str(mint)


def test_metadata_pda_derives_with_program_seed():
    mint = str(Pubkey.new_unique())
    pda = token_metadata.metadata_pda(mint)
    # The PDA must be a program-derived address of the metadata program.
    assert Pubkey.is_on_curve(pda) is False


def test_parse_valid_metadata_account():
    raw, update_auth, mint = _metadata_bytes("Orvix Token", "ORVX", "https://orvix.network/token.json")
    parsed = token_metadata.parse_metadata_account(base64.b64encode(raw).decode())
    assert parsed is not None
    assert parsed["name"] == "Orvix Token"
    assert parsed["symbol"] == "ORVX"
    assert parsed["uri"] == "https://orvix.network/token.json"
    assert parsed["update_authority"] == update_auth
    assert parsed["mint"] == mint


def test_parse_rejects_non_metadata_key():
    raw = bytearray(b"\x01") + bytearray(64)  # key=1 (MintV1), not MetadataV1
    assert token_metadata.parse_metadata_account(base64.b64encode(bytes(raw)).decode()) is None


def test_parse_rejects_invalid_base64():
    assert token_metadata.parse_metadata_account("!!!not-base64!!!") is None


def test_parse_rejects_truncated_account():
    raw, _a, _m = _metadata_bytes("Long Name Here", "ORVX", "https://orvix.network/token.json")
    truncated = raw[: len(raw) - 10]
    assert token_metadata.parse_metadata_account(base64.b64encode(truncated).decode()) is None


def test_parse_rejects_absurd_string_length():
    raw, _a, _m = _metadata_bytes("x", "y", "z")
    # Corrupt the name length to something huge.
    bad = bytearray(raw)
    bad[65:69] = struct.pack("<I", 2**31)
    assert token_metadata.parse_metadata_account(base64.b64encode(bytes(bad)).decode()) is None


@pytest.mark.asyncio
async def test_fetch_metadata_fail_soft_on_missing_account(monkeypatch):
    class FakeSol:
        async def get_account_info(self, address, encoding="base64"):
            return None

    assert await token_metadata.fetch_metadata(FakeSol(), str(Pubkey.new_unique())) is None


@pytest.mark.asyncio
async def test_fetch_metadata_parses_account(monkeypatch):
    raw, _a, _m = _metadata_bytes("Orvix Token", "ORVX", "uri")

    class FakeSol:
        async def get_account_info(self, address, encoding="base64"):
            return {"data": [base64.b64encode(raw).decode(), "base64"]}

    parsed = await token_metadata.fetch_metadata(FakeSol(), str(Pubkey.new_unique()))
    assert parsed is not None
    assert parsed["symbol"] == "ORVX"
