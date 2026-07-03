"""Tests for WalletService: role→key mapping, balance sync, USDC send (stub + real build)."""

from decimal import Decimal

import pytest

from app.config import settings
from app.services import wallet as wallet_module
from app.services.wallet import WalletService
from tests.fakes import FakeSupabase

TEST_OWNER = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


class _FakeSol:
    def __init__(self, balances=None, sig="SIG"):
        self._balances = balances or {}
        self._sig = sig
        self.sent = []

    async def get_token_balance(self, owner, mint):
        return self._balances.get(mint, Decimal(0))

    async def get_latest_blockhash(self):
        return "11111111111111111111111111111111"  # Hash.default() base58

    async def send_raw_transaction(self, raw_b64):
        self.sent.append(raw_b64)
        return self._sig


def test_public_key_roles(monkeypatch):
    monkeypatch.setattr(settings, "TREASURY_MAIN_PUBLIC", "MAINPUB")
    monkeypatch.setattr(settings, "TREASURY_WALLET_ADDRESS", "HOTPUB")
    monkeypatch.setattr(settings, "PAYOUT_WALLET_PUBLIC", "PAYPUB")
    ws = WalletService()
    assert ws.public_key("main") == "MAINPUB"
    assert ws.public_key("hot") == "HOTPUB"
    assert ws.public_key("payout") == "PAYPUB"


def test_get_keypair_main_is_offline():
    with pytest.raises(ValueError):
        WalletService().get_keypair("main")


def test_get_keypair_missing_path(monkeypatch):
    monkeypatch.setattr(settings, "PAYOUT_KEYPAIR_PATH", "")
    with pytest.raises(ValueError):
        WalletService().get_keypair("payout")


async def test_sync_balances_writes_table_and_skips_unset(monkeypatch):
    monkeypatch.setattr(settings, "TREASURY_MAIN_PUBLIC", "MAINPUB")
    monkeypatch.setattr(settings, "TREASURY_WALLET_ADDRESS", "HOTPUB")
    monkeypatch.setattr(settings, "PAYOUT_WALLET_PUBLIC", "")  # unset -> skipped
    monkeypatch.setattr(settings, "USDC_MINT_ADDRESS", "USDCMINT")
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "")
    monkeypatch.setattr(wallet_module, "get_solana_service", lambda: _FakeSol({"USDCMINT": Decimal("123.45")}))

    db = FakeSupabase()
    for role in ("main", "hot", "payout"):
        db._table("treasury_wallets").insert_row({"wallet_role": role})

    out = await WalletService().sync_balances(db)

    assert {r["role"] for r in out} == {"main", "hot"}  # payout skipped
    hot = next(r for r in db._table("treasury_wallets").rows if r["wallet_role"] == "hot")
    assert float(hot["balance_usdc"]) == pytest.approx(123.45)
    assert hot["public_key"] == "HOTPUB"


async def test_send_usdc_stub_returns_fake_sig():
    sig = await WalletService().send_usdc("hot", TEST_OWNER, Decimal("50"), stub=True)
    assert sig.startswith("STUB_SEND_")


async def test_send_usdc_real_path_builds_and_submits(monkeypatch):
    from solders.keypair import Keypair

    monkeypatch.setattr(settings, "USDC_MINT_ADDRESS", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    fake = _FakeSol(sig="REALSIG")
    monkeypatch.setattr(wallet_module, "get_solana_service", lambda: fake)
    ws = WalletService()
    monkeypatch.setattr(ws, "get_keypair", lambda role: Keypair())

    sig = await ws.send_usdc("hot", TEST_OWNER, Decimal("50"), stub=False)

    assert sig == "REALSIG"
    assert len(fake.sent) == 1  # exactly one serialized tx submitted
