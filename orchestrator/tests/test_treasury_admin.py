"""Tests for the admin treasury endpoints (balances / sync / sweep-hot)."""

from fastapi.testclient import TestClient

from app.database import get_supabase
from app.dependencies import require_admin
from app.main import app
from tests.fakes import FakeSupabase


def _client(db):
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[require_admin] = lambda: True
    return TestClient(app)


def test_treasury_balances_reads_table():
    db = FakeSupabase()
    db._table("treasury_wallets").insert_row({"wallet_role": "hot", "balance_usdc": 12.0})
    try:
        r = _client(db).get("/v1/admin/treasury/balances")
        assert r.status_code == 200
        assert r.json()["wallets"][0]["wallet_role"] == "hot"
    finally:
        app.dependency_overrides.clear()


def test_treasury_sync_endpoint(monkeypatch):
    async def fake_sync(d):
        return [{"role": "hot", "public_key": "H", "balance_usdc": 5.0}]

    monkeypatch.setattr("app.routes.admin.wallet_service.sync_balances", fake_sync)
    try:
        r = _client(FakeSupabase()).post("/v1/admin/treasury/sync")
        assert r.status_code == 200
        assert r.json()["synced"][0]["role"] == "hot"
    finally:
        app.dependency_overrides.clear()


def test_treasury_sweep_hot_endpoint(monkeypatch):
    async def fake_sweep(d):
        return {"swept": False, "reason": "below_threshold"}

    monkeypatch.setattr("app.routes.admin.hot_sweeper.run_once", fake_sweep)
    try:
        r = _client(FakeSupabase()).post("/v1/admin/treasury/sweep-hot")
        assert r.status_code == 200
        assert r.json()["reason"] == "below_threshold"
    finally:
        app.dependency_overrides.clear()
