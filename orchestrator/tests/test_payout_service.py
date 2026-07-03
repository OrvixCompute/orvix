"""Tests for PayoutService: queueing, locking, stub processing, refund-on-failure."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import app.services.payout_service as payout_mod
from app.exceptions import InsufficientBalanceError, RateLimitError, ValidationError
from app.services.payout_service import PayoutService
from tests.fakes import FakeSupabase

VALID_WALLET = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


@pytest.fixture
def db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(payout_mod, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def svc():
    return PayoutService()


def test_queue_locks_and_inserts(db, svc):
    user = db.add_user(available_usdc=500.0)
    w = svc.queue_withdrawal(user["id"], Decimal("200"), VALID_WALLET)

    assert w["status"] == "queued"
    row = db._table("users").rows[0]
    assert float(row["available_usdc"]) == pytest.approx(300.0)
    assert float(row["pending_withdrawal_usdc"]) == pytest.approx(200.0)
    # A pending ledger transaction was recorded.
    txs = db._table("transactions").rows
    assert any(t["type"] == "provider_payout" and t["status"] == "pending" for t in txs)


def test_queue_insufficient_balance_raises(db, svc):
    user = db.add_user(available_usdc=50.0)
    with pytest.raises(InsufficientBalanceError):
        svc.queue_withdrawal(user["id"], Decimal("200"), VALID_WALLET)


def test_queue_below_minimum_raises(db, svc):
    user = db.add_user(available_usdc=10000.0)
    with pytest.raises(ValidationError):
        svc.queue_withdrawal(user["id"], Decimal("10"), VALID_WALLET)  # below 100 min


def test_invalid_wallet_raises(db, svc):
    user = db.add_user(available_usdc=10000.0)
    with pytest.raises(ValidationError):
        svc.queue_withdrawal(user["id"], Decimal("200"), "not-a-wallet")


def test_daily_limit_enforced(db, svc, monkeypatch):
    monkeypatch.setattr(payout_mod.settings, "MAX_WITHDRAWALS_PER_DAY", 2)
    user = db.add_user(available_usdc=10000.0)
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(2):
        db._table("withdrawals").insert_row(
            {"user_id": user["id"], "amount": 100.0, "queued_at": now, "status": "completed"}
        )
    with pytest.raises(RateLimitError):
        svc.queue_withdrawal(user["id"], Decimal("200"), VALID_WALLET)


async def test_stub_processing_completes(db, svc, monkeypatch):
    monkeypatch.setattr(payout_mod.settings, "PAYOUT_STUB", True)
    user = db.add_user(available_usdc=0.0, pending_withdrawal_usdc=200.0)
    db._table("withdrawals").insert_row(
        {
            "user_id": user["id"],
            "amount": 200.0,
            "destination_wallet": VALID_WALLET,
            "status": "queued",
            "metadata": {"manual_approval_required": False},
        }
    )
    await svc.process_pending_withdrawals()

    updated = db._table("withdrawals").rows[0]
    assert updated["status"] == "completed"
    assert updated["solana_signature"].startswith("STUB")
    assert float(db._table("users").rows[0]["pending_withdrawal_usdc"]) == pytest.approx(0.0)
    # No refund — available stays at 0.
    assert float(db._table("users").rows[0]["available_usdc"]) == pytest.approx(0.0)


async def test_refund_on_failure(db, svc, monkeypatch):
    monkeypatch.setattr(payout_mod.settings, "PAYOUT_STUB", False)  # real send -> NotImplemented
    user = db.add_user(available_usdc=0.0, pending_withdrawal_usdc=200.0)
    db._table("withdrawals").insert_row(
        {
            "user_id": user["id"],
            "amount": 200.0,
            "destination_wallet": VALID_WALLET,
            "status": "queued",
            "metadata": {"manual_approval_required": False},
        }
    )
    await svc.process_pending_withdrawals()

    updated = db._table("withdrawals").rows[0]
    assert updated["status"] == "failed"
    # Amount refunded back to available.
    assert float(db._table("users").rows[0]["available_usdc"]) == pytest.approx(200.0)
    assert float(db._table("users").rows[0]["pending_withdrawal_usdc"]) == pytest.approx(0.0)


def _queue_row(db, user):
    db._table("withdrawals").insert_row(
        {
            "user_id": user["id"],
            "amount": 200.0,
            "destination_wallet": VALID_WALLET,
            "status": "queued",
            "metadata": {"manual_approval_required": False},
        }
    )


async def test_real_payout_confirmed_completes(db, svc, monkeypatch):
    monkeypatch.setattr(payout_mod.settings, "PAYOUT_STUB", False)
    user = db.add_user(available_usdc=0.0, pending_withdrawal_usdc=200.0)
    _queue_row(db, user)

    async def fake_send(w):
        return "REALSIG123"

    async def fake_confirm(sig):
        return True

    monkeypatch.setattr(svc, "_send_payout", fake_send)
    monkeypatch.setattr(svc, "_confirm", fake_confirm)

    await svc.process_pending_withdrawals()

    row = db._table("withdrawals").rows[0]
    assert row["status"] == "completed"
    assert row["solana_signature"] == "REALSIG123"
    u = db._table("users").rows[0]
    assert float(u["pending_withdrawal_usdc"]) == pytest.approx(0.0)
    assert float(u["available_usdc"]) == pytest.approx(0.0)  # settled, not refunded


async def test_broadcast_unconfirmed_stays_processing_no_refund(db, svc, monkeypatch):
    """A broadcast tx that never confirms must NOT be refunded (double-spend guard)."""
    monkeypatch.setattr(payout_mod.settings, "PAYOUT_STUB", False)
    user = db.add_user(available_usdc=0.0, pending_withdrawal_usdc=200.0)
    _queue_row(db, user)

    async def fake_send(w):
        return "SENTSIG"

    async def fake_confirm(sig):
        return False

    monkeypatch.setattr(svc, "_send_payout", fake_send)
    monkeypatch.setattr(svc, "_confirm", fake_confirm)

    await svc.process_pending_withdrawals()

    row = db._table("withdrawals").rows[0]
    assert row["status"] == "processing"  # left for manual review, NOT failed/completed
    assert row["solana_signature"] == "SENTSIG"
    assert "unconfirmed" in row["error_message"]
    u = db._table("users").rows[0]
    # No refund: balance stays locked in pending.
    assert float(u["available_usdc"]) == pytest.approx(0.0)
    assert float(u["pending_withdrawal_usdc"]) == pytest.approx(200.0)

    # A second worker pass must not re-send (row is 'processing', not 'queued').
    await svc.process_pending_withdrawals()
    assert db._table("withdrawals").rows[0]["status"] == "processing"


async def test_manual_approval_skipped(db, svc):
    user = db.add_user(available_usdc=0.0, pending_withdrawal_usdc=20000.0)
    db._table("withdrawals").insert_row(
        {
            "user_id": user["id"],
            "amount": 20000.0,
            "destination_wallet": VALID_WALLET,
            "status": "queued",
            "metadata": {"manual_approval_required": True},
        }
    )
    await svc.process_pending_withdrawals()
    assert db._table("withdrawals").rows[0]["status"] == "queued"  # untouched
