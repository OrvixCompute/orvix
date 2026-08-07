"""Tests for the /v1/account/tier + /quota endpoints and their flexible auth.

These endpoints accept EITHER a wallet JWT or an `orvx_sk_` API key
(get_current_user_flexible), so an API client can read its own tier/quota
without the wallet-signature flow.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks
from starlette.requests import Request

from app.database import get_supabase
from app.dependencies import get_current_user_flexible
from app.main import app
from app.services.api_key_service import hash_key
from app.services.auth_service import auth_service
from tests.fakes import FakeSupabase

API_KEY = "orvx_sk_" + "a" * 32


def _client(staked):
    db = FakeSupabase()
    user = db.add_user(tier="bronze", staked_orvx=staked)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user_flexible] = lambda: user
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


# --- flexible auth (JWT or API key) -----------------------------------------


def _request_with_token(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/account/tier",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


async def test_flexible_auth_api_key_returns_bare_user_row():
    """API-key path returns the user row directly, not the {"user","api_key"} wrapper."""
    db = FakeSupabase()
    user = db.add_user(staked_orvx=75000)
    db.table("api_keys").insert(
        {"key_hash": hash_key(API_KEY), "is_active": True, "user_id": user["id"]}
    ).execute()

    result = await get_current_user_flexible(_request_with_token(API_KEY), BackgroundTasks(), db)

    assert result["id"] == user["id"]
    assert result["wallet_address"] == user["wallet_address"]
    assert "api_key" not in result  # shape must match get_current_user, not the wrapper


async def test_flexible_auth_jwt_returns_bare_user_row():
    db = FakeSupabase()
    user = db.add_user(staked_orvx=10000)
    token = auth_service.create_jwt(user)

    result = await get_current_user_flexible(_request_with_token(token), BackgroundTasks(), db)

    assert result["id"] == user["id"]


async def test_flexible_auth_revoked_api_key_rejected():
    from app.exceptions import UnauthorizedError

    db = FakeSupabase()
    user = db.add_user()
    db.table("api_keys").insert(
        {"key_hash": hash_key(API_KEY), "is_active": False, "user_id": user["id"]}
    ).execute()

    with pytest.raises(UnauthorizedError):
        await get_current_user_flexible(_request_with_token(API_KEY), BackgroundTasks(), db)


def test_account_tier_via_api_key_end_to_end():
    """A real orvx_sk_ key resolves through get_current_user_flexible at the route."""
    db = FakeSupabase()
    user = db.add_user(tier="gold", staked_orvx=75000)
    db.table("api_keys").insert(
        {"key_hash": hash_key(API_KEY), "is_active": True, "user_id": user["id"]}
    ).execute()
    app.dependency_overrides[get_supabase] = lambda: db  # only override the DB, not auth
    try:
        client = TestClient(app)
        r = client.get("/v1/account/tier", headers={"Authorization": f"Bearer {API_KEY}"})
        assert r.status_code == 200
        assert r.json()["tier"] == "gold"
    finally:
        app.dependency_overrides.clear()


# --- tier-gated throughput -------------------------------------------------
def test_rate_limit_rises_with_tier():
    """Staking buys throughput, not only a discount.

    Every tier used to share one flat 60/min ceiling, which made the published
    "higher API limits" benefit untrue.
    """
    from app.services import tier_service

    limits = [tier_service.rate_limit_for_tier(t) for t in ("bronze", "silver", "gold", "diamond")]
    assert limits == sorted(limits)
    assert len(set(limits)) == 4
    # Bronze keeps the historical flat limit, so no existing caller gets tighter.
    assert tier_service.rate_limit_for_tier("bronze") == 60
    # An unknown tier must not accidentally grant more than the floor.
    assert tier_service.rate_limit_for_tier("titanium") == 60


def test_bronze_is_limited_where_diamond_is_not():
    """The ceiling actually applied is the caller's tier ceiling."""
    from app.exceptions import RateLimitError
    from app.services import rate_limit_service

    rate_limit_service.reset()

    for _ in range(60):
        rate_limit_service.check("key-bronze", "bronze")
    with pytest.raises(RateLimitError) as exc:
        rate_limit_service.check("key-bronze", "bronze")
    assert exc.value.details["tier"] == "bronze"
    assert exc.value.details["limit_per_minute"] == 60

    # A diamond key sails past the point where bronze was cut off.
    for _ in range(300):
        rate_limit_service.check("key-diamond", "diamond")


def test_chat_and_image_ceilings_are_counted_separately():
    """A burst of images must not spend the caller's chat allowance.

    They cost very different amounts of GPU time, so sharing one counter would
    let a cheap resource exhaust an expensive one's budget and vice versa.
    """
    from app.exceptions import RateLimitError
    from app.services import rate_limit_service

    rate_limit_service.reset()

    for _ in range(60):
        rate_limit_service.check("key-a", "bronze", bucket="image")
    with pytest.raises(RateLimitError):
        rate_limit_service.check("key-a", "bronze", bucket="image")

    # Chat is untouched by the image burst above.
    rate_limit_service.check("key-a", "bronze", bucket="chat")
