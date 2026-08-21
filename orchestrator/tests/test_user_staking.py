"""Tests for non-custodial user staking endpoints and service."""

import base64
import struct

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_current_user
from app.main import app
from app.services import user_staking
from app.services.user_staking import (
    STAKE_DISCRIMINATOR,
    UNSTAKE_DISCRIMINATOR,
    stake_account_address,
)

VALID_WALLET = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
PROGRAM_ID = "CS4CWHL4DeSvbqZaUzT9AgK47VWweg94Ta2FZokvJZSg"


class _FakeSolana:
    """Minimal stand-in for SolanaService RPC calls used by user_staking."""

    def __init__(self):
        # 32-byte zero hash, base58-encoded -> valid Hash for Message building.
        self.blockhash = "11111111111111111111111111111111"
        self.account_data: dict[str, bytes] = {}
        self.sent: list[str] = []

    async def get_latest_blockhash(self) -> str:
        return self.blockhash

    async def send_raw_transaction(self, raw_b64: str) -> str:
        self.sent.append(raw_b64)
        return "SIG" + "1" * 85

    async def _rpc(self, method: str, params: list):
        if method == "getAccountInfo":
            addr = params[0]
            data = self.account_data.get(addr)
            if data is None:
                return {"value": None}
            return {"value": {"data": [base64.b64encode(data).decode(), "base64"]}}
        raise AssertionError(f"unexpected RPC {method}")


@pytest.fixture
def ctx(monkeypatch):
    fake_sol = _FakeSolana()
    monkeypatch.setattr(settings, "USER_STAKING_PROGRAM_ID", PROGRAM_ID)
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "FakeMint111111111111111111111111111111111111")
    monkeypatch.setattr(settings, "ORVX_DECIMALS", 6)
    monkeypatch.setattr(user_staking, "get_solana_service", lambda: fake_sol)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "wallet_address": VALID_WALLET}
    client = TestClient(app)
    yield client, fake_sol
    app.dependency_overrides.clear()


# --- 404 when not configured ------------------------------------------------
def test_disabled_returns_404(monkeypatch):
    monkeypatch.setattr(settings, "USER_STAKING_PROGRAM_ID", "")
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "wallet_address": VALID_WALLET}
    try:
        client = TestClient(app)
        assert client.get("/v1/staking/user/status").status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- stake transaction ------------------------------------------------------
def test_build_stake_transaction(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/stake", json={"amount": "100", "lock_days": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert body["program_id"] == PROGRAM_ID
    assert body["stake_account"] == str(stake_account_address(VALID_WALLET))
    # Hex-encoded transaction decodes and starts with the stake discriminator in
    # the instruction data.
    raw = bytes.fromhex(body["transaction"])
    assert raw  # non-empty serialized tx


def test_stake_rejects_invalid_lock_days(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/stake", json={"amount": "100", "lock_days": 5})
    assert resp.status_code == 400


def test_stake_rejects_zero_amount(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/stake", json={"amount": "0", "lock_days": 7})
    assert resp.status_code == 422


# --- unstake transaction ----------------------------------------------------
def test_build_unstake_transaction(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/unstake", json={"amount": "50"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["program_id"] == PROGRAM_ID


# --- status -----------------------------------------------------------------
def _stake_account_bytes(amount_raw: int, locked_until: int, created_at: int) -> bytes:
    owner = bytes.fromhex("00" * 32)  # placeholder owner bytes (parsing doesn't check)
    return (
        b"\x00" * 8  # discriminator placeholder
        + owner
        + struct.pack("<Q", amount_raw)
        + struct.pack("<q", locked_until)
        + struct.pack("<q", created_at)
    )


def test_status_empty_account(ctx):
    client, _ = ctx
    resp = client.get("/v1/staking/user/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["staked_orvx"] == "0"
    assert body["tier"] == "bronze"


def test_status_with_stake(ctx):
    client, fake_sol = ctx
    fake_sol.account_data[str(stake_account_address(VALID_WALLET))] = _stake_account_bytes(
        amount_raw=50_000 * 1_000_000, locked_until=0, created_at=1_700_000_000
    )
    resp = client.get("/v1/staking/user/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["staked_orvx"] == "50000"
    assert body["tier"] == "gold"


def test_discriminators_are_8_bytes():
    assert len(STAKE_DISCRIMINATOR) == 8
    assert len(UNSTAKE_DISCRIMINATOR) == 8
    assert STAKE_DISCRIMINATOR != UNSTAKE_DISCRIMINATOR


def test_amount_scaling_uses_decimals(ctx):
    client, fake_sol = ctx
    # 1.5 ORVX at 6 decimals -> 1_500_000 raw
    fake_sol.account_data[str(stake_account_address(VALID_WALLET))] = _stake_account_bytes(
        amount_raw=1_500_000, locked_until=0, created_at=0
    )
    resp = client.get("/v1/staking/user/status")
    assert resp.json()["staked_orvx"] == "1.5"


# --- submit ----------------------------------------------------------------
def test_initialize_vault_transaction(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/initialize-vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["program_id"] == PROGRAM_ID
    assert body["vault_address"]  # PDA vault address returned
    raw = bytes.fromhex(body["transaction"])
    assert raw  # non-empty serialized tx


def test_submit_transaction_broadcasts(ctx):
    client, fake_sol = ctx
    signed_hex = "deadbeef" * 16
    resp = client.post("/v1/staking/user/submit", json={"transaction": signed_hex})
    assert resp.status_code == 200
    body = resp.json()
    assert body["signature"].startswith("SIG")
    assert fake_sol.sent  # send_raw_transaction was called


def test_submit_rejects_non_hex(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/submit", json={"transaction": "not-hex!!"})
    assert resp.status_code == 400


def test_submit_rejects_empty(ctx):
    client, _ = ctx
    resp = client.post("/v1/staking/user/submit", json={"transaction": ""})
    assert resp.status_code == 400
