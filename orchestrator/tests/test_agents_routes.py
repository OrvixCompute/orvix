"""Route tests for /v1/agents (monitor CRUD, alerts, webhook test)."""

import pytest
from fastapi.testclient import TestClient
from solders.pubkey import Pubkey

from app.database import get_supabase
from app.main import app
from app.services import token_intel
from tests.fakes import FakeSupabase


@pytest.fixture
def ctx():
    db = FakeSupabase()
    app.dependency_overrides[get_supabase] = lambda: db
    token_intel.reset_scan_cache()
    client = TestClient(app)
    yield client, db
    app.dependency_overrides.clear()
    token_intel.reset_scan_cache()


def _jwt_for(user):
    """Build a valid HS256 JWT for the seeded user (mirrors auth_service)."""
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    from app.config import settings

    now = datetime.now(timezone.utc)
    claims = {
        "sub": user["id"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm="HS256")


def _auth_header(user):
    return {"Authorization": f"Bearer {_jwt_for(user)}"}


def test_create_monitor_requires_auth(ctx):
    client, _db = ctx
    resp = client.post("/v1/agents/monitors", json={"target_type": "token", "target_address": str(Pubkey.new_unique()), "conditions": [{"type": "accumulation_score", "gte": 70}]})
    assert resp.status_code == 401


def test_create_and_list_monitor(ctx, monkeypatch):
    client, db = ctx
    user = db.add_user()

    async def _no_price(mint):
        return None

    monkeypatch.setattr(token_intel, "get_token_price_usdc", _no_price)

    mint = str(Pubkey.new_unique())
    resp = client.post(
        "/v1/agents/monitors",
        headers=_auth_header(user),
        json={
            "name": "watch token",
            "target_type": "token",
            "target_address": mint,
            "conditions": [{"type": "accumulation_score", "gte": 70}],
            "interval_minutes": 30,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["target_address"] == mint
    assert body["conditions"][0]["type"] == "accumulation_score"

    listed = client.get("/v1/agents/monitors", headers=_auth_header(user))
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_create_monitor_validates_condition_target_type(ctx):
    client, db = ctx
    user = db.add_user()
    resp = client.post(
        "/v1/agents/monitors",
        headers=_auth_header(user),
        json={
            "target_type": "wallet",
            "target_address": str(Pubkey.new_unique()),
            # new_activity is valid for wallets; accumulation_score is not.
            "conditions": [{"type": "accumulation_score", "gte": 70}],
        },
    )
    assert resp.status_code == 400


def test_owner_scoping(ctx):
    client, db = ctx
    user_a = db.add_user()
    user_b = db.add_user()

    resp = client.post(
        "/v1/agents/monitors",
        headers=_auth_header(user_a),
        json={
            "target_type": "token",
            "target_address": str(Pubkey.new_unique()),
            "conditions": [{"type": "accumulation_score", "gte": 70}],
        },
    )
    monitor_id = resp.json()["id"]

    # User B cannot read, delete, or see alerts for A's monitor.
    assert client.get(f"/v1/agents/monitors/{monitor_id}", headers=_auth_header(user_b)).status_code == 404
    assert client.delete(f"/v1/agents/monitors/{monitor_id}", headers=_auth_header(user_b)).status_code == 404
    assert client.get(f"/v1/agents/monitors/{monitor_id}/alerts", headers=_auth_header(user_b)).status_code == 404

    # User A can.
    assert client.get(f"/v1/agents/monitors/{monitor_id}", headers=_auth_header(user_a)).status_code == 200
    assert client.get(f"/v1/agents/monitors/{monitor_id}/alerts", headers=_auth_header(user_a)).status_code == 200


def test_delete_monitor_owner(ctx):
    client, db = ctx
    user = db.add_user()
    resp = client.post(
        "/v1/agents/monitors",
        headers=_auth_header(user),
        json={
            "target_type": "token",
            "target_address": str(Pubkey.new_unique()),
            "conditions": [{"type": "accumulation_score", "gte": 70}],
        },
    )
    monitor_id = resp.json()["id"]
    assert client.delete(f"/v1/agents/monitors/{monitor_id}", headers=_auth_header(user)).status_code == 204
    assert client.get(f"/v1/agents/monitors/{monitor_id}", headers=_auth_header(user)).status_code == 404


def test_alerts_are_returned_for_monitor(ctx, monkeypatch):
    client, db = ctx
    user = db.add_user()
    resp = client.post(
        "/v1/agents/monitors",
        headers=_auth_header(user),
        json={
            "target_type": "token",
            "target_address": str(Pubkey.new_unique()),
            "conditions": [{"type": "accumulation_score", "gte": 70}],
        },
    )
    monitor_id = resp.json()["id"]

    # Seed an alert event directly.
    from datetime import datetime, timezone

    db.table("alert_events").insert(
        {
            "monitor_id": monitor_id,
            "user_id": user["id"],
            "condition_type": "accumulation_score",
            "message": "Accumulation score 85",
            "payload": {"score": 85},
            "dedup_key": "acc:2026-08-20",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()
    alerts = client.get(f"/v1/agents/monitors/{monitor_id}/alerts", headers=_auth_header(user))
    assert alerts.status_code == 200
    assert len(alerts.json()) == 1
    assert alerts.json()[0]["condition_type"] == "accumulation_score"


def test_webhook_test_requires_webhook_url(ctx, monkeypatch):
    client, db = ctx
    user = db.add_user()

    async def _no_price(mint):
        return None

    monkeypatch.setattr(token_intel, "get_token_price_usdc", _no_price)
    resp = client.post(
        "/v1/agents/monitors",
        headers=_auth_header(user),
        json={
            "target_type": "token",
            "target_address": str(Pubkey.new_unique()),
            "conditions": [{"type": "accumulation_score", "gte": 70}],
        },
    )
    monitor_id = resp.json()["id"]
    test_resp = client.post(f"/v1/agents/monitors/{monitor_id}/test", headers=_auth_header(user))
    assert test_resp.status_code == 400
