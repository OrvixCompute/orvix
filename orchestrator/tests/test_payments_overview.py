"""Tests for the payment-flow monitoring: /v1/admin/payments/overview + service."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.database import get_supabase
from app.dependencies import require_admin
from app.main import app
from app.services.payments_overview import build_overview
from tests.fakes import FakeSupabase


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _seed(db: FakeSupabase) -> None:
    db._table("treasury_wallets").insert_row(
        {"wallet_role": "hot", "public_key": "H", "balance_usdc": 12.0}
    )
    # deposits: one inside 24h, one outside
    db._table("transactions").insert_row(
        {"type": "topup", "amount": 10.0, "status": "confirmed", "created_at": _iso(1)}
    )
    db._table("transactions").insert_row(
        {"type": "topup", "amount": 99.0, "status": "confirmed", "created_at": _iso(48)}
    )
    # withdrawals: one queued, one processing (needs review), one completed 24h
    db._table("withdrawals").insert_row(
        {"status": "queued", "amount": 5.0, "queued_at": _iso(2)}
    )
    db._table("withdrawals").insert_row(
        {
            "status": "processing",
            "amount": 7.0,
            "queued_at": _iso(3),
            "solana_signature": "sigX",
            "error_message": "broadcast but unconfirmed",
        }
    )
    db._table("withdrawals").insert_row(
        {"status": "completed", "amount": 3.0, "queued_at": _iso(5), "processed_at": _iso(4)}
    )


def test_build_overview_aggregates():
    db = FakeSupabase()
    _seed(db)
    data = build_overview(db)

    # only the in-window deposit counts
    assert data["deposits"]["count_24h"] == 1
    assert data["deposits"]["total_usdc_24h"] == 10.0
    # recent lists both deposits
    assert len(data["deposits"]["recent"]) == 2

    w = data["withdrawals"]
    assert w["queued"] == 1
    assert w["processing"] == 1
    assert w["completed_24h"] == 1
    assert len(w["needs_review"]) == 1
    assert w["needs_review"][0]["solana_signature"] == "sigX"

    assert data["treasury"][0]["wallet_role"] == "hot"
    assert "enable_payment_listener" in data["flags"]


def test_overview_endpoint():
    db = FakeSupabase()
    _seed(db)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[require_admin] = lambda: True
    try:
        r = TestClient(app).get("/v1/admin/payments/overview")
        assert r.status_code == 200
        body = r.json()
        assert body["withdrawals"]["queued"] == 1
        assert body["deposits"]["count_24h"] == 1
    finally:
        app.dependency_overrides.clear()
