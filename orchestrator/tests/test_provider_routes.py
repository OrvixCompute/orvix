"""Tests for provider REST endpoints via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

import app.services.payout_service as payout_mod
from app.database import get_supabase
from app.dependencies import get_current_user
from app.main import app
from tests.fakes import FakeSupabase

VALID_WALLET = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


@pytest.fixture
def ctx(monkeypatch):
    db = FakeSupabase()
    user = db.add_user(
        tier="gold",
        available_usdc=500.0,
        lifetime_earnings_usdc=500.0,
        staked_orvx=50000.0,  # above the 25K provider minimum
    )
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(payout_mod, "get_supabase", lambda: db)
    client = TestClient(app)
    yield client, db, user
    app.dependency_overrides.clear()


def test_register_returns_secret(ctx):
    client, db, user = ctx
    resp = client.post("/v1/provider/register", json={"display_name": "My Rig"})
    assert resp.status_code == 200
    assert resp.json()["node_secret"]
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert row["is_provider"] is True
    assert row["provider_secret_hash"]


def test_register_rejected_when_stake_below_minimum(monkeypatch):
    db = FakeSupabase()
    # below the 25K minimum, and not yet a provider
    user = db.add_user(
        tier="bronze", staked_orvx=1000.0, is_provider=False, provider_secret_hash=None
    )
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        resp = client.post("/v1/provider/register", json={})
        assert resp.status_code == 400
        body = resp.json()["error"]
        assert body["code"] == "insufficient_stake"
        assert body["required"] == "25000"
        # User was not flipped to provider.
        row = next(r for r in db._table("users").rows if r["id"] == user["id"])
        assert row.get("is_provider") is False
        assert row.get("provider_secret_hash") is None
    finally:
        app.dependency_overrides.clear()


def test_regenerate_secret_changes_hash(ctx):
    client, db, user = ctx
    client.post("/v1/provider/register", json={})
    first = next(r for r in db._table("users").rows if r["id"] == user["id"])["provider_secret_hash"]
    client.post("/v1/provider/regenerate-secret", json={})
    second = next(r for r in db._table("users").rows if r["id"] == user["id"])["provider_secret_hash"]
    assert first != second


def test_list_and_rename_node(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row(
        {"id": "node-1", "provider_id": user["id"], "status": "ready", "name": "old"}
    )
    listed = client.get("/v1/provider/nodes").json()
    assert len(listed) == 1
    assert listed[0]["id"] == "node-1"
    assert listed[0]["is_connected"] is False

    r = client.post("/v1/provider/nodes/node-1/rename", json={"name": "new-name"})
    assert r.status_code == 200
    assert db._table("nodes").rows[0]["name"] == "new-name"


def test_rename_foreign_node_404(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row({"id": "other", "provider_id": "someone-else"})
    r = client.post("/v1/provider/nodes/other/rename", json={"name": "x"})
    assert r.status_code == 404


def test_earnings_aggregation(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row({"id": "node-1", "provider_id": user["id"]})
    for _ in range(3):
        db._table("jobs").insert_row(
            {"node_id": "node-1", "provider_earning_usdc": 1.5, "user_id": "dev"}
        )
    data = client.get("/v1/provider/earnings").json()
    assert data["available_to_withdraw"] == "500.0"
    assert data["total_lifetime_usdc"] == "500.0"
    assert len(data["earnings_by_day"]) == 1
    assert data["earnings_by_day"][0]["jobs_count"] == 3


def test_withdraw_below_minimum(ctx):
    client, db, user = ctx
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 10, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 400


def test_withdraw_valid(ctx):
    client, db, user = ctx
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 200, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["withdrawal_id"]
    # Funds moved available -> pending.
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert float(row["available_usdc"]) == pytest.approx(300.0)
    assert float(row["pending_withdrawal_usdc"]) == pytest.approx(200.0)


def test_withdraw_insufficient(ctx):
    client, db, user = ctx
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 9999, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 402
