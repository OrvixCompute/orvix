"""Holder verification + chat/image quota enforcement."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_current_user, get_user_from_api_key
from app.exceptions import OrvixException
from app.main import app
from app.services import quota_service
from app.services.holder import HolderService, holder_service
from tests.fakes import FakeSupabase

_KEY = {"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"}


# --- holder service --------------------------------------------------------
def _seed_holder(db, wallet, balance, is_holder, checked_at):
    db._table("holder_status").insert_row(
        {
            "wallet_address": wallet,
            "orvx_balance": balance,
            "is_holder": is_holder,
            "last_checked_at": checked_at.isoformat(),
        }
    )


async def test_holder_cache_miss_queries_and_caches(monkeypatch):
    db = FakeSupabase()
    hs = HolderService()
    calls = {"n": 0}

    async def fake_q(wallet):
        calls["n"] += 1
        return 20000.0

    monkeypatch.setattr(hs, "_query_orvx_balance", fake_q)
    is_holder, bal = await hs.get_holder_status(db, "w1")
    assert is_holder is True and bal == 20000.0
    assert calls["n"] == 1
    assert len(db._table("holder_status").rows) == 1


async def test_holder_fresh_cache_hit_skips_query(monkeypatch):
    db = FakeSupabase()
    _seed_holder(db, "w1", 15000.0, True, datetime.now(timezone.utc))
    hs = HolderService()

    async def boom(wallet):
        raise AssertionError("should not query on a fresh cache hit")

    monkeypatch.setattr(hs, "_query_orvx_balance", boom)
    is_holder, bal = await hs.get_holder_status(db, "w1")
    assert is_holder is True and bal == 15000.0


async def test_holder_stale_cache_requeries(monkeypatch):
    db = FakeSupabase()
    old = datetime.now(timezone.utc) - timedelta(minutes=settings.HOLDER_CACHE_TTL_MINUTES + 5)
    _seed_holder(db, "w1", 0.0, False, old)
    hs = HolderService()

    async def fake_q(wallet):
        return 20000.0

    monkeypatch.setattr(hs, "_query_orvx_balance", fake_q)
    is_holder, bal = await hs.get_holder_status(db, "w1")
    assert is_holder is True and bal == 20000.0


async def test_holder_no_mint_is_non_holder(monkeypatch):
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "")
    is_holder, bal = await HolderService().get_holder_status(FakeSupabase(), "w1")
    assert is_holder is False and bal == 0.0


# --- chat quota ------------------------------------------------------------
def test_chat_holder_unlimited():
    q = quota_service.enforce_chat_quota(FakeSupabase(), "w", True, 0)
    assert q["type"] == "holder" and q["free"] is False


def test_chat_free_twice_then_402():
    db = FakeSupabase()
    q1 = quota_service.enforce_chat_quota(db, "w", False, 0)
    assert q1 == {"type": "free", "remaining": 1, "free": True}
    q2 = quota_service.enforce_chat_quota(db, "w", False, 0)
    assert q2["remaining"] == 0
    with pytest.raises(OrvixException) as exc:
        quota_service.enforce_chat_quota(db, "w", False, 0)
    assert exc.value.status_code == 402 and exc.value.error_code == "quota_exceeded"


def test_chat_over_free_with_balance_is_paid():
    db = FakeSupabase()
    quota_service.enforce_chat_quota(db, "w", False, 0)
    quota_service.enforce_chat_quota(db, "w", False, 0)
    q = quota_service.enforce_chat_quota(db, "w", False, 5.0)
    assert q["type"] == "paid" and q["free"] is False


# --- image quota -----------------------------------------------------------
def test_image_grace_fallback_one_per_day(monkeypatch):
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "")
    db = FakeSupabase()
    q = quota_service.enforce_image_quota(db, "w", False, 0.0, units=1)
    assert q["limit"] == 1 and q["remaining"] == 0
    with pytest.raises(OrvixException) as exc:
        quota_service.enforce_image_quota(db, "w", False, 0.0, units=1)
    assert exc.value.status_code == 429 and exc.value.error_code == "daily_quota_exceeded"


def test_image_non_holder_403_when_mint_set(monkeypatch):
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT")
    with pytest.raises(OrvixException) as exc:
        quota_service.enforce_image_quota(FakeSupabase(), "w", False, 100.0, units=1)
    assert exc.value.status_code == 403 and exc.value.error_code == "not_holder"


def test_image_holder_five_per_day(monkeypatch):
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT")
    db = FakeSupabase()
    for _ in range(5):
        quota_service.enforce_image_quota(db, "w", True, 20000.0, units=1)
    with pytest.raises(OrvixException) as exc:
        quota_service.enforce_image_quota(db, "w", True, 20000.0, units=1)
    assert exc.value.status_code == 429


def test_image_n_consumes_n_units(monkeypatch):
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT")
    db = FakeSupabase()
    q = quota_service.enforce_image_quota(db, "w", True, 20000.0, units=3)
    assert q["remaining"] == 2  # 5 - 3
    with pytest.raises(OrvixException):  # 3 more → 6 > 5
        quota_service.enforce_image_quota(db, "w", True, 20000.0, units=3)


def test_image_daily_reset_at_utc_rollover(monkeypatch):
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "")
    db = FakeSupabase()
    monkeypatch.setattr(quota_service, "_today_iso", lambda: "2026-07-02")
    quota_service.enforce_image_quota(db, "w", False, 0.0, units=1)
    with pytest.raises(OrvixException):
        quota_service.enforce_image_quota(db, "w", False, 0.0, units=1)
    # New UTC day → counter resets.
    monkeypatch.setattr(quota_service, "_today_iso", lambda: "2026-07-03")
    q = quota_service.enforce_image_quota(db, "w", False, 0.0, units=1)
    assert q["remaining"] == 0


# --- endpoint integration --------------------------------------------------
def test_chat_endpoint_free_then_402(monkeypatch):
    db = FakeSupabase()
    db.add_user(balance_usdc=0.0)

    def dep():
        return {
            "user": db._table("users").rows[0],
            "api_key": {"id": "k-quota", "user_id": db._table("users").rows[0]["id"]},
        }

    async def fake_holder(d, w):
        return False, 0.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_user_from_api_key] = dep
    client = TestClient(app)
    payload = {"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    try:
        r1 = client.post("/v1/chat/completions", headers=_KEY, json=payload)
        assert r1.status_code == 200
        assert r1.headers["X-Orvix-Quota-Type"] == "free"
        # Free requests aren't billed → balance untouched at 0.
        assert db._table("users").rows[0]["balance_usdc"] == 0.0
        assert client.post("/v1/chat/completions", headers=_KEY, json=payload).status_code == 200
        r3 = client.post("/v1/chat/completions", headers=_KEY, json=payload)
        assert r3.status_code == 402
        assert r3.json()["error"]["code"] == "quota_exceeded"
    finally:
        app.dependency_overrides.clear()


def test_account_quota_endpoint(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "")

    async def fake_holder(d, w):
        return False, 0.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        r = client.get("/v1/account/quota", headers={"Authorization": "Bearer jwt"})
        assert r.status_code == 200
        body = r.json()
        assert body["is_holder"] is False
        assert body["chat"]["type"] == "free_tier"
        assert body["chat"]["lifetime_free_limit"] == settings.CHAT_LIFETIME_FREE_LIMIT
        assert body["image"]["type"] == "grace_daily"
        assert body["image"]["daily_limit"] == settings.IMAGE_DAILY_LIMIT_FALLBACK
    finally:
        app.dependency_overrides.clear()


def test_image_endpoint_non_holder_403(monkeypatch):
    db = FakeSupabase()
    db.add_user()
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT")

    def dep():
        return {
            "user": db._table("users").rows[0],
            "api_key": {"id": "k-img", "user_id": db._table("users").rows[0]["id"]},
        }

    async def fake_holder(d, w):
        return False, 50.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_user_from_api_key] = dep
    client = TestClient(app)
    try:
        r = client.post("/v1/images/generations", headers=_KEY, json={"prompt": "x"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "not_holder"
    finally:
        app.dependency_overrides.clear()
