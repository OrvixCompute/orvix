"""Tests for the buyback engine: guardrails, recording, and admin auth.

Jupiter quotes are monkeypatched so nothing touches the network, and BUYBACK_STUB
(default true) means the swap is simulated.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.routes.admin as admin_mod
from app.database import get_supabase
from app.exceptions import RateLimitError, ValidationError
from app.main import app
from app.services.buyback_service import BuybackService
from tests.fakes import FakeSupabase


def _quote(out_orvx, impact_pct="0"):
    """Build a fake Jupiter quote yielding `out_orvx` ORVX (6 decimals)."""

    async def fake_quote(self, amount_usdc, slippage_bps):
        return {
            "outAmount": str(int(Decimal(str(out_orvx)) * (10**6))),
            "priceImpactPct": impact_pct,
        }

    return fake_quote


def _db_with_budget(budget):
    db = FakeSupabase()
    db._table("global_accounting").insert_row({"id": 1, "buyback_budget_usdc": float(budget)})
    return db


# --- service guardrails ----------------------------------------------------
async def test_execute_records_and_moves_budget(monkeypatch):
    monkeypatch.setattr(BuybackService, "quote", _quote(10000))
    db = _db_with_budget(100)
    svc = BuybackService(db)

    result = await svc.execute(Decimal("100"), 50, "admin")
    assert result["solana_signature"].startswith("STUBBUY")
    assert Decimal(result["orvx_received"]) == Decimal("10000")

    acct = next(r for r in db._table("global_accounting").rows if r["id"] == 1)
    assert float(acct["buyback_budget_usdc"]) == pytest.approx(0.0)
    assert float(acct["orvx_held_for_burn"]) == pytest.approx(10000.0)
    assert float(acct["total_usdc_spent_on_buyback"]) == pytest.approx(100.0)
    assert len(db._table("buyback_events").rows) == 1


async def test_execute_rejects_over_budget(monkeypatch):
    monkeypatch.setattr(BuybackService, "quote", _quote(10000))
    svc = BuybackService(_db_with_budget(50))
    with pytest.raises(ValidationError):
        await svc.execute(Decimal("100"), 50, "admin")


async def test_execute_aborts_on_high_slippage(monkeypatch):
    # 5% impact = 500 bps, above the default 100 bps max.
    monkeypatch.setattr(BuybackService, "quote", _quote(10000, impact_pct="0.05"))
    svc = BuybackService(_db_with_budget(100))
    with pytest.raises(ValidationError):
        await svc.execute(Decimal("100"), 50, "admin")
    # Nothing recorded.
    assert svc.db._table("buyback_events").rows == []


async def test_execute_rate_limited(monkeypatch):
    monkeypatch.setattr(BuybackService, "quote", _quote(10000))
    db = _db_with_budget(1000)
    db._table("buyback_events").insert_row(
        {"created_at": datetime.now(timezone.utc).isoformat(), "usdc_spent": 10.0}
    )
    with pytest.raises(RateLimitError):
        await BuybackService(db).execute(Decimal("100"), 50, "admin")


async def test_execute_allowed_after_interval(monkeypatch):
    monkeypatch.setattr(BuybackService, "quote", _quote(10000))
    db = _db_with_budget(1000)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db._table("buyback_events").insert_row({"created_at": old, "usdc_spent": 10.0})
    result = await BuybackService(db).execute(Decimal("100"), 50, "admin")
    assert result["buyback_id"]


# --- admin endpoint auth ---------------------------------------------------
@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setattr(BuybackService, "quote", _quote(10000))
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "secret-admin-key")
    db = _db_with_budget(1000)
    app.dependency_overrides[get_supabase] = lambda: db
    client = TestClient(app)
    yield client, db
    app.dependency_overrides.clear()


def test_admin_buyback_requires_key(admin_client):
    client, db = admin_client
    resp = client.post("/v1/admin/buyback/execute", json={"amount_usdc": 100})
    assert resp.status_code == 401


def test_admin_buyback_wrong_key(admin_client):
    client, db = admin_client
    resp = client.post(
        "/v1/admin/buyback/execute",
        json={"amount_usdc": 100},
        headers={"X-Admin-Key": "nope"},
    )
    assert resp.status_code == 401


def test_admin_buyback_success(admin_client):
    client, db = admin_client
    resp = client.post(
        "/v1/admin/buyback/execute",
        json={"amount_usdc": 100, "slippage_bps": 50},
        headers={"X-Admin-Key": "secret-admin-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert Decimal(body["orvx_received"]) == Decimal("10000")
    assert body["solana_signature"].startswith("STUBBUY")


def test_admin_buyback_status(admin_client):
    client, db = admin_client
    resp = client.get("/v1/admin/buyback/status", headers={"X-Admin-Key": "secret-admin-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert "buyback_budget_usdc" in body
    assert "orvx_held_for_burn" in body


def test_admin_feature_flags(monkeypatch):
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "secret-admin-key")
    monkeypatch.setattr(admin_mod.settings, "REQUIRE_STAKE_FOR_PROVIDER", False)
    client = TestClient(app)
    resp = client.get("/v1/admin/feature-flags", headers={"X-Admin-Key": "secret-admin-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["require_stake_for_provider"] is False
    assert body["buyback_stub"] is True
    assert "admin_api_key_set" in body


def test_admin_feature_flags_requires_key(monkeypatch):
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "secret-admin-key")
    client = TestClient(app)
    assert client.get("/v1/admin/feature-flags").status_code == 401


def test_admin_disabled_when_no_key(monkeypatch):
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "")
    db = _db_with_budget(1000)
    app.dependency_overrides[get_supabase] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(
            "/v1/admin/buyback/execute",
            json={"amount_usdc": 100},
            headers={"X-Admin-Key": "anything"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "admin_disabled"
    finally:
        app.dependency_overrides.clear()
