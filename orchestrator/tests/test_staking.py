"""Tests for the staking endpoints, service, and stake-deposit handling."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_supabase
from app.dependencies import get_current_user
from app.main import app
from app.services import tier_service
from app.services.payment_listener import PaymentListener
from tests.fakes import FakeSupabase

VALID_WALLET = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


@pytest.fixture
def ctx():
    db = FakeSupabase()
    user = db.add_user(tier="gold", staked_orvx=50000.0, is_provider=True)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    yield client, db, user
    app.dependency_overrides.clear()


# --- tier thresholds -------------------------------------------------------
@pytest.mark.parametrize(
    "staked,expected",
    [
        (0, "bronze"),
        (9999, "bronze"),
        (10000, "silver"),
        (49999, "silver"),
        (50000, "gold"),
        (249999, "gold"),
        (250000, "diamond"),
        (1_000_000, "diamond"),
    ],
)
def test_tier_for_stake_boundaries(staked, expected):
    assert tier_service.tier_for_stake(staked) == expected


def test_next_tier_info_top_is_none():
    assert tier_service.next_tier_info(250000) is None
    nxt = tier_service.next_tier_info(50000)
    assert nxt["name"] == "diamond"
    assert nxt["additional_needed"] == "200000"


# --- stake intent ----------------------------------------------------------
def test_stake_intent_creation(ctx):
    client, db, user = ctx
    resp = client.post("/v1/staking/stake-intent", json={"amount": 30000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["memo"].startswith("orvix_stake_")
    assert body["intent_id"]
    # Persisted as a pending intent.
    rows = db._table("staking_intents").rows
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["memo"] == body["memo"]


def test_stake_intent_rejects_non_positive(ctx):
    client, db, user = ctx
    assert client.post("/v1/staking/stake-intent", json={"amount": 0}).status_code == 422


# --- status ----------------------------------------------------------------
def test_status_reports_tier_and_history(ctx):
    client, db, user = ctx
    db._table("stakes").insert_row(
        {"user_id": user["id"], "type": "stake", "amount": 50000.0, "reason": "init"}
    )
    body = client.get("/v1/staking/status").json()
    assert body["tier"] == "gold"
    assert Decimal(body["staked_orvx"]) == Decimal("50000")
    assert body["next_tier"]["name"] == "diamond"
    assert len(body["history"]) == 1


# --- unstaking -------------------------------------------------------------
def test_unstake_with_sufficient_balance(ctx):
    client, db, user = ctx
    # Stays above the 25K provider floor: 50K -> 25K.
    resp = client.post(
        "/v1/staking/unstake", json={"amount": 25000, "destination_wallet": VALID_WALLET}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["withdrawal_id"]
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert float(row["staked_orvx"]) == pytest.approx(25000.0)
    # An ORVX-tagged withdrawal was queued (not a USDC payout).
    w = db._table("withdrawals").rows[0]
    assert w["metadata"]["asset"] == "ORVX"
    assert w["metadata"]["manual_approval_required"] is True


def test_unstake_rejected_below_provider_minimum(ctx):
    client, db, user = ctx
    # 50K - 30K = 20K < 25K minimum.
    resp = client.post(
        "/v1/staking/unstake", json={"amount": 30000, "destination_wallet": VALID_WALLET}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "provider_minimum_stake"
    # Stake unchanged.
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert float(row["staked_orvx"]) == pytest.approx(50000.0)


def test_unstake_rejected_when_exceeds_balance(ctx):
    client, db, user = ctx
    resp = client.post(
        "/v1/staking/unstake", json={"amount": 999999, "destination_wallet": VALID_WALLET}
    )
    assert resp.status_code == 402


def test_non_provider_can_unstake_to_zero():
    db = FakeSupabase()
    user = db.add_user(tier="silver", staked_orvx=10000.0, is_provider=False)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        resp = client.post(
            "/v1/staking/unstake", json={"amount": 10000, "destination_wallet": VALID_WALLET}
        )
        assert resp.status_code == 200
        row = next(r for r in db._table("users").rows if r["id"] == user["id"])
        assert float(row["staked_orvx"]) == pytest.approx(0.0)
    finally:
        app.dependency_overrides.clear()


# --- stake deposit handling (payment listener) -----------------------------
async def test_apply_stake_credits_and_fulfills():
    db = FakeSupabase()
    user = db.add_user(staked_orvx=0.0, is_provider=False)
    intent = db._table("staking_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvix_stake_abc123",
            "expected_amount": 25000.0,
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )
    listener = PaymentListener()
    await listener._apply_stake(db, intent, "stake-sig-1", Decimal("25000"))

    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert float(row["staked_orvx"]) == pytest.approx(25000.0)
    assert db._table("staking_intents").rows[0]["status"] == "fulfilled"
    # A stakes audit row was written.
    assert any(s["solana_signature"] == "stake-sig-1" for s in db._table("stakes").rows)


async def test_apply_stake_is_idempotent_on_signature():
    db = FakeSupabase()
    user = db.add_user(staked_orvx=0.0, is_provider=False)
    intent = db._table("staking_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvix_stake_dup",
            "expected_amount": 25000.0,
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )
    listener = PaymentListener()
    await listener._apply_stake(db, intent, "stake-dup", Decimal("25000"))
    await listener._apply_stake(db, intent, "stake-dup", Decimal("25000"))

    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert float(row["staked_orvx"]) == pytest.approx(25000.0)  # credited once


# --- network stats / transparency -----------------------------------------
def test_network_stats_public(ctx):
    client, db, user = ctx
    db._table("global_accounting").insert_row(
        {
            "id": 1,
            "buyback_budget_usdc": 100.0,
            "orvx_held_for_burn": 5000.0,
            "total_orvx_burned": 0.0,
            "total_orvx_bought": 5000.0,
        }
    )
    body = client.get("/v1/staking/network-stats").json()
    assert body["total_providers"] == 1
    assert Decimal(body["total_staked"]) == Decimal("50000")
    assert Decimal(body["buyback_budget_usdc"]) == Decimal("100")


def test_buyback_history_public(ctx):
    client, db, user = ctx
    db._table("buyback_events").insert_row(
        {
            "usdc_spent": 100.0,
            "orvx_received": 5000.0,
            "execution_price_usdc_per_orvx": 0.02,
            "solana_signature": "sig-buyback-1",
            "executed_by": "admin",
        }
    )
    resp = client.get("/v1/staking/buyback-history")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["solana_signature"] == "sig-buyback-1"
    assert Decimal(body[0]["orvx_received"]) == Decimal("5000")


def test_burn_history_public(ctx):
    client, db, user = ctx
    db._table("burn_events").insert_row(
        {
            "orvx_burned": 5000.0,
            "solana_signature": "sig-burn-1",
            "period_start": "2026-05-01T00:00:00+00:00",
            "period_end": "2026-06-01T00:00:00+00:00",
            "executed_by": "admin",
        }
    )
    resp = client.get("/v1/staking/burn-history")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["solana_signature"] == "sig-burn-1"
    assert Decimal(body[0]["orvx_burned"]) == Decimal("5000")
