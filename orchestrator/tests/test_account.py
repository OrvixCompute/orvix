"""Tests for the stake-based /v1/account/tier endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.database import get_supabase
from app.dependencies import get_current_user
from app.main import app
from tests.fakes import FakeSupabase


def _client(staked):
    db = FakeSupabase()
    user = db.add_user(tier="bronze", staked_orvx=staked)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize(
    "staked,tier,discount",
    [
        (0, "bronze", 0),
        (10000, "silver", 5),
        (75000, "gold", 15),
        (250000, "diamond", 25),
    ],
)
def test_account_tier(staked, tier, discount):
    client = _client(staked)
    try:
        body = client.get("/v1/account/tier").json()
        assert body["tier"] == tier
        assert body["discount_pct"] == discount
    finally:
        app.dependency_overrides.clear()


def test_account_tier_next_tier_progress():
    client = _client(75000)
    try:
        body = client.get("/v1/account/tier").json()
        assert body["next_tier"]["name"] == "diamond"
        assert body["next_tier"]["additional_needed"] == "175000"
    finally:
        app.dependency_overrides.clear()


def test_account_tier_diamond_has_no_next():
    client = _client(250000)
    try:
        assert client.get("/v1/account/tier").json()["next_tier"] is None
    finally:
        app.dependency_overrides.clear()
