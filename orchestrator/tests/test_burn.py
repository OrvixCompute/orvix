"""Tests for the burn engine: defaults, guardrails, recording, and admin auth."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.routes.admin as admin_mod
from app.database import get_supabase
from app.exceptions import ValidationError
from app.main import app
from app.services.burn_service import BurnService, previous_month_period
from tests.fakes import FakeSupabase


def _db_with_held(held):
    db = FakeSupabase()
    db._table("global_accounting").insert_row({"id": 1, "orvx_held_for_burn": float(held)})
    return db


def test_previous_month_period():
    start, end = previous_month_period(datetime(2026, 6, 15, tzinfo=timezone.utc))
    assert start == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 1, tzinfo=timezone.utc)


async def test_execute_burns_all_held_by_default():
    db = _db_with_held(5000)
    result = await BurnService(db).execute(None, None, None, "admin")
    assert Decimal(result["orvx_burned"]) == Decimal("5000")
    assert result["solana_signature"].startswith("STUBBURN")
    acct = next(r for r in db._table("global_accounting").rows if r["id"] == 1)
    assert float(acct["orvx_held_for_burn"]) == pytest.approx(0.0)
    assert float(acct["total_orvx_burned"]) == pytest.approx(5000.0)
    assert len(db._table("burn_events").rows) == 1


async def test_execute_specific_amount():
    db = _db_with_held(5000)
    result = await BurnService(db).execute(Decimal("2000"), None, None, "admin")
    assert Decimal(result["orvx_burned"]) == Decimal("2000")
    acct = next(r for r in db._table("global_accounting").rows if r["id"] == 1)
    assert float(acct["orvx_held_for_burn"]) == pytest.approx(3000.0)


async def test_execute_rejects_over_held():
    db = _db_with_held(1000)
    with pytest.raises(ValidationError):
        await BurnService(db).execute(Decimal("2000"), None, None, "admin")


async def test_execute_rejects_zero_held():
    db = _db_with_held(0)
    with pytest.raises(ValidationError):
        await BurnService(db).execute(None, None, None, "admin")


async def test_execute_rejects_bad_period():
    db = _db_with_held(1000)
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)  # end before start
    with pytest.raises(ValidationError):
        await BurnService(db).execute(Decimal("100"), start, end, "admin")


async def test_execute_rejects_future_period():
    db = _db_with_held(1000)
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2099, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        await BurnService(db).execute(Decimal("100"), start, end, "admin")


# --- admin endpoint --------------------------------------------------------
def test_admin_burn_success(monkeypatch):
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "secret-admin-key")
    db = _db_with_held(5000)
    app.dependency_overrides[get_supabase] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post(
            "/v1/admin/burn/execute",
            json={"amount": 1000},
            headers={"X-Admin-Key": "secret-admin-key"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["orvx_burned"]) == Decimal("1000")
        assert body["period"]["period_start"] and body["period"]["period_end"]
    finally:
        app.dependency_overrides.clear()


def test_admin_burn_status(monkeypatch):
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "secret-admin-key")
    db = _db_with_held(5000)
    app.dependency_overrides[get_supabase] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/v1/admin/burn/status", headers={"X-Admin-Key": "secret-admin-key"})
        assert resp.status_code == 200
        body = resp.json()
        assert "orvx_held_for_burn" in body
        assert "total_orvx_burned" in body
    finally:
        app.dependency_overrides.clear()


def test_admin_burn_requires_key(monkeypatch):
    monkeypatch.setattr(admin_mod.settings, "ADMIN_API_KEY", "secret-admin-key")
    db = _db_with_held(5000)
    app.dependency_overrides[get_supabase] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/v1/admin/burn/execute", json={"amount": 1000})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
