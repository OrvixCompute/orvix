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
