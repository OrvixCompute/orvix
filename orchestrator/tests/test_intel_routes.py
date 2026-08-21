"""Route tests for /v1/tokens and /v1/wallets."""

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
    user = db.add_user()
    client = TestClient(app)
    yield client, db, user
    app.dependency_overrides.clear()
    token_intel.reset_scan_cache()


def _seed_api_key(db, user):
    from app.services.api_key_service import hash_key

    raw = "orvx_sk_" + "a" * 32
    db.table("api_keys").insert(
        {"user_id": user["id"], "key_hash": hash_key(raw), "is_active": True}
    ).execute()
    return raw


def test_token_scan_requires_auth(ctx):
    client, _db, _user = ctx
    resp = client.get(f"/v1/tokens/{Pubkey.new_unique()}")
    assert resp.status_code == 401


def test_token_scan_invalid_address_400(ctx):
    client, db, user = ctx
    key = _seed_api_key(db, user)
    resp = client.get("/v1/tokens/not-an-address", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 400


def test_token_scan_returns_200(ctx, monkeypatch):
    client, db, user = ctx
    key = _seed_api_key(db, user)

    class FakeSol:
        async def get_token_supply(self, mint):
            return {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}

        async def get_account_info(self, address, encoding="base64"):
            return None

    async def _no_price(mint):
        return None

    monkeypatch.setattr(token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(token_intel, "get_token_price_usdc", _no_price)

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mint"]
    assert body["supply"]["decimals"] == 6
    assert body["liquidity"]["pool_count"] == 0
    assert body["risk"]["warnings"]


def test_accumulation_endpoint_returns_score(ctx, monkeypatch):
    client, db, user = ctx
    key = _seed_api_key(db, user)

    class FakeSol:
        async def get_token_supply(self, mint):
            return {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}

        async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
            return []

    monkeypatch.setattr(token_intel, "get_solana_service", lambda: FakeSol())

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/accumulation",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert 0 <= body["score"] <= 100
    assert body["label"] in ("distribution", "weak", "moderate", "strong")


def test_wallet_analysis_endpoint(ctx, monkeypatch):
    client, db, user = ctx
    key = _seed_api_key(db, user)

    class FakeSol:
        def __init__(self):
            self.sigs = [{"signature": "s1", "slot": 1, "blockTime": 1700000000}]

        async def get_token_accounts_by_owner(self, owner, mint):
            return []

        async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
            return self.sigs

        async def get_parsed_transaction(self, signature):
            return {
                "transaction": {"message": {"instructions": []}},
                "meta": {},
            }

        @staticmethod
        def extract_memo(parsed_tx):
            return None

        async def _rpc(self, method, params):
            return {"value": []}

    monkeypatch.setattr(token_intel, "get_solana_service", lambda: FakeSol())

    resp = client.get(
        f"/v1/wallets/{Pubkey.new_unique()}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet"]
    assert body["holdings"] == []
    assert body["recent_activity"][0]["signature"] == "s1"
