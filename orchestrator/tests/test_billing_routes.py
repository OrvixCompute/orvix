"""Tests for POST /v1/billing/topup-intent.

The endpoint hands the caller a deposit address and a `solana:` payment QR.
`TREASURY_WALLET_ADDRESS` is not gated by any feature flag, so an unconfigured
deploy would happily serve an empty address — these tests pin the refusal.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_current_user
from app.main import app
from tests.fakes import FakeSupabase

TREASURY = "DYCYqu53TestTreasuryAddressForUnitTestsOnly11"


@pytest.fixture
def client_and_db():
    db = FakeSupabase()
    user = db.add_user()
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app), db
    app.dependency_overrides.clear()


def test_topup_intent_refused_when_treasury_unconfigured(client_and_db, monkeypatch):
    """No treasury address must mean a 503, not an empty address."""
    client, db = client_and_db
    monkeypatch.setattr(settings, "TREASURY_WALLET_ADDRESS", "")

    resp = client.post("/v1/billing/topup-intent", json={})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "treasury_unconfigured"


def test_refusal_leaves_no_pending_intent_behind(client_and_db, monkeypatch):
    """The guard runs before the insert, so no row is created for a payment
    that cannot be made."""
    client, db = client_and_db
    monkeypatch.setattr(settings, "TREASURY_WALLET_ADDRESS", "")

    client.post("/v1/billing/topup-intent", json={})

    assert db._table("topup_intents").rows == []


def test_topup_intent_returns_address_and_qr_when_configured(client_and_db, monkeypatch):
    """The happy path is unchanged: address, memo, and a QR that carries both."""
    client, db = client_and_db
    monkeypatch.setattr(settings, "TREASURY_WALLET_ADDRESS", TREASURY)
    monkeypatch.setattr(settings, "USDC_MINT_ADDRESS", "UsdcMint1111")

    resp = client.post("/v1/billing/topup-intent", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["treasury_address"] == TREASURY
    assert body["memo"].startswith("orvx_")
    # The QR must point at the treasury and carry the memo, or the listener
    # cannot match the transfer back to this intent.
    assert body["qr_data"].startswith(f"solana:{TREASURY}")
    assert f"memo={body['memo']}" in body["qr_data"]
    assert len(db._table("topup_intents").rows) == 1
