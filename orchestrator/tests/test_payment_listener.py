"""Tests for PaymentListener._apply_topup: atomic credit + idempotency.

These lock in the fix for the double-credit bug — crediting and recording the
ledger row now happen in one atomic RPC (credit_topup), so a signature that has
already been processed credits nothing on a re-run.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.payment_listener import PaymentListener
from tests.fakes import FakeSupabase


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def listener():
    return PaymentListener()


def _make_intent(db, user, *, expected=None) -> dict:
    return db._table("topup_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvx_abc123def456",
            "expected_amount_usdc": expected,
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )


async def test_apply_topup_credits_and_records(db, listener):
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user)

    await listener._apply_topup(db, intent, "sig-aaa", Decimal("50"))

    # Balance credited.
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1050.0)
    # Exactly one confirmed top-up ledger row for this signature.
    txs = db._table("transactions").rows
    assert len(txs) == 1
    assert txs[0]["type"] == "topup"
    assert txs[0]["status"] == "confirmed"
    assert txs[0]["solana_signature"] == "sig-aaa"
    # Intent marked fulfilled.
    updated_intent = db._table("topup_intents").rows[0]
    assert updated_intent["status"] == "fulfilled"
    assert updated_intent["fulfilled_at"] is not None


async def test_apply_topup_is_idempotent_on_signature(db, listener):
    """A duplicate signature must credit the balance only once."""
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user)

    await listener._apply_topup(db, intent, "sig-dup", Decimal("50"))
    # Re-process the very same signature (e.g. after a crash/restart).
    await listener._apply_topup(db, intent, "sig-dup", Decimal("50"))

    # Credited once, not twice.
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1050.0)
    # Only one ledger row exists for that signature.
    txs = [t for t in db._table("transactions").rows if t["solana_signature"] == "sig-dup"]
    assert len(txs) == 1


async def test_apply_topup_partial_when_below_expected(db, listener):
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user, expected=100.0)

    await listener._apply_topup(db, intent, "sig-partial", Decimal("40"))

    # Received less than expected -> intent flagged partial, full amount credited.
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1040.0)
    assert db._table("topup_intents").rows[0]["status"] == "partial"


async def test_apply_topup_fulfilled_when_meets_expected(db, listener):
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user, expected=100.0)

    await listener._apply_topup(db, intent, "sig-full", Decimal("100"))

    assert db._table("topup_intents").rows[0]["status"] == "fulfilled"


# --- _process_signature routing --------------------------------------------


class _FakeSol:
    """Stands in for the Solana RPC service in _process_signature tests."""

    def __init__(self, memo, transfers):
        self._memo = memo
        self._transfers = transfers

    async def get_parsed_transaction(self, signature):
        return {"parsed": True}

    def extract_memo(self, parsed):
        return self._memo

    def extract_spl_transfers(self, parsed, mint, owner):
        return list(self._transfers)


def _patch_sol(monkeypatch, db, memo, transfers):
    # _process_signature fetches its own db via get_supabase() and the RPC service
    # via get_solana_service() — patch both to the fakes.
    monkeypatch.setattr("app.services.payment_listener.get_supabase", lambda: db)
    monkeypatch.setattr(
        "app.services.payment_listener.get_solana_service",
        lambda: _FakeSol(memo, transfers),
    )


async def test_process_signature_skips_already_seen(db, listener, monkeypatch):
    """A signature already in `transactions` must not be reprocessed or credited."""
    user = db.add_user(balance_usdc=1000.0)
    db._table("transactions").insert_row({"solana_signature": "sig-seen", "type": "topup"})
    _make_intent(db, user)  # a matching intent exists, but must be ignored
    _patch_sol(monkeypatch, db, "orvx_abc123def456", [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-seen")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_no_memo_no_credit(db, listener, monkeypatch):
    db.add_user(balance_usdc=1000.0)
    _patch_sol(monkeypatch, db, None, [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-nomemo")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_unmatched_intent_no_credit(db, listener, monkeypatch):
    db.add_user(balance_usdc=1000.0)
    # Memo has the right shape but no pending intent matches it.
    _patch_sol(monkeypatch, db, "orvx_doesnotexist", [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-nomatch")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_credits_on_matching_intent(db, listener, monkeypatch):
    user = db.add_user(balance_usdc=1000.0)
    _make_intent(db, user)  # memo "orvx_abc123def456"
    _patch_sol(monkeypatch, db, "orvx_abc123def456", [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-good")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1050.0)
    assert db._table("topup_intents").rows[0]["status"] == "fulfilled"


async def test_process_signature_ignores_transfer_to_non_treasury_account(db, listener, monkeypatch):
    """A USDC transfer whose destination is not a treasury token account is dropped."""
    user = db.add_user(balance_usdc=1000.0)
    _make_intent(db, user)
    _patch_sol(monkeypatch, db, "orvx_abc123def456", [{"amount": "50", "destination": "somebody_else"}])
    listener._treasury_token_accounts = {"tacc"}  # transfer dest is NOT in here

    await listener._process_signature("sig-elsewhere")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_routes_stake_memo(db, listener, monkeypatch):
    """A stake memo credits staked_orvx (not USDC balance) via the stake path."""
    from app.config import settings

    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "OrvxMint1111111111111111111111111111111111")
    user = db.add_user(balance_usdc=1000.0)
    db._table("staking_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvix_stake_zzz999",
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )
    _patch_sol(monkeypatch, db, "orvix_stake_zzz999", [{"amount": "25000", "destination": "orvx_ata"}])
    listener._treasury_orvx_token_accounts = {"orvx_ata"}

    await listener._process_signature("sig-stake")

    # ORVX staked, USDC balance untouched.
    assert float(db._table("users").rows[0]["staked_orvx"]) == pytest.approx(25000.0)
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)
    assert db._table("staking_intents").rows[0]["status"] == "fulfilled"
