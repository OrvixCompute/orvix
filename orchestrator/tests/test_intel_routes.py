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

        async def get_token_largest_accounts(self, mint):
            return []

        async def get_token_account_owner(self, account):
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

        async def get_token_largest_accounts(self, mint):
            return []

        async def get_token_account_owner(self, account):
            return None

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


def test_intel_endpoints_are_rate_limited(ctx, monkeypatch):
    """Scan endpoints share an 'intel' bucket per user; exceeding it -> 429."""
    from app.services import rate_limit_service, tier_service

    client, db, user = ctx
    key = _seed_api_key(db, user)

    class FakeSol:
        async def get_token_supply(self, mint):
            return {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}

        async def get_account_info(self, address, encoding="base64"):
            return None

        async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
            return []

    async def _no_price(mint):
        return None

    monkeypatch.setattr(token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(token_intel, "get_token_price_usdc", _no_price)
    # Bronze normally allows 60/min; shrink it so the test trips quickly.
    monkeypatch.setitem(tier_service.TIER_RATE_LIMITS, "bronze", 2)
    rate_limit_service.reset()

    headers = {"Authorization": f"Bearer {key}"}
    mint = str(Pubkey.new_unique())
    # Two requests fit within the limit...
    assert client.get(f"/v1/tokens/{mint}", headers=headers).status_code == 200
    assert client.get(f"/v1/tokens/{mint}", headers=headers).status_code == 200
    # ...the third is throttled (bucket shared across scan endpoints).
    resp = client.get(f"/v1/tokens/{mint}/accumulation", headers=headers)
    assert resp.status_code == 429
    assert resp.json()["error"]["bucket"] == "intel"
    rate_limit_service.reset()


def test_intelligence_endpoint_503_without_node(ctx, monkeypatch):
    from app.services import intel_ai

    client, db, user = ctx
    key = _seed_api_key(db, user)

    async def _no_intel(_db, mint):
        return None

    monkeypatch.setattr(intel_ai, "generate_token_intelligence", _no_intel)
    # unavailable_reason reports no_node because the registry is empty.
    from app.services.node_manager import node_manager

    monkeypatch.setattr(node_manager, "unavailable_reason", lambda model, engine=None: "no_node")

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/intelligence",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_chat_provider"


def test_intelligence_endpoint_returns_analysis(ctx, monkeypatch):
    from app.services import intel_ai

    client, db, user = ctx
    key = _seed_api_key(db, user)

    async def _fake_intel(_db, mint):
        return {
            "mint": mint,
            "model": "qwen-2.5-7b",
            "analysis": {"narrative": "Bullish setup", "risk_flags": ["low liquidity"], "watch_next": "watch volume"},
            "generated_at": "2026-08-20T00:00:00Z",
            "latency_ms": 900,
            "node_id": "node-1",
        }

    monkeypatch.setattr(intel_ai, "generate_token_intelligence", _fake_intel)

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/intelligence",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis"]["narrative"] == "Bullish setup"
    assert body["model"] == "qwen-2.5-7b"
    assert body["node_id"] == "node-1"


def test_holders_endpoint(ctx, monkeypatch):
    from app.services import holder_intel

    client, db, user = ctx
    key = _seed_api_key(db, user)

    async def _fake_top(_db, mint):
        return {
            "total_holders": 1,
            "top_holders": [{"wallet": "wallet1", "token_account": "acc1", "balance": 900.0}],
            "top10_share": 0.9,
            "as_of": "2026-08-20T00:00:00Z",
            "source": "getTokenLargestAccounts",
        }

    monkeypatch.setattr(holder_intel, "top_holders", _fake_top)

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/holders",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_holders"] == 1
    assert body["top_holders"][0]["wallet"] == "wallet1"
    assert body["top10_share"] == 0.9


def test_early_buyers_endpoint(ctx, monkeypatch):
    from app.services import holder_intel

    client, db, user = ctx
    key = _seed_api_key(db, user)

    async def _fake_buyers(_db, mint):
        return [
            {"wallet": "wallet1", "amount": 100.0, "signature": "sig1", "block_time": 100},
            {"wallet": "wallet2", "amount": 50.0, "signature": "sig2", "block_time": 200},
        ]

    monkeypatch.setattr(holder_intel, "early_buyers", _fake_buyers)

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/early-buyers",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["signature"] == "sig1"
    assert body[1]["amount"] == 50.0


def test_social_endpoint(ctx, monkeypatch):
    from app.services import social_intel

    client, db, user = ctx
    key = _seed_api_key(db, user)

    async def _fake_social(_db, mint):
        return {
            "mint": mint,
            "social_links": {"twitter": "https://x.com/test", "website": None, "telegram": None, "discord": None},
            "social_score": 55,
            "metrics": {
                "dex_trending": True,
                "dex_volume_24h": 10000.0,
                "dex_price_change_24h": 5.0,
                "twitter_followers": 2000,
                "twitter_statuses_7d": 15,
                "social_sentiment": "positive",
            },
            "as_of": "2026-08-20T00:00:00Z",
        }

    monkeypatch.setattr(social_intel, "analyze_social", _fake_social)

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/social",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["social_score"] == 55
    assert body["social_links"]["twitter"] == "https://x.com/test"
    assert body["metrics"]["dex_trending"] is True
    assert body["metrics"]["twitter_followers"] == 2000


def test_clusters_endpoint(ctx, monkeypatch):
    from app.services import holder_intel

    client, db, user = ctx
    key = _seed_api_key(db, user)

    async def _fake_clusters(_db, mint):
        return {
            "mint": mint,
            "clusters": [
                {
                    "id": "abc123",
                    "wallets": ["w1", "w2"],
                    "signals": ["shared_funding", "coordinated_timing"],
                    "confidence": 0.67,
                }
            ],
            "total_wallets_analyzed": 5,
            "as_of": "2026-08-20T00:00:00Z",
        }

    monkeypatch.setattr(holder_intel, "detect_clusters", _fake_clusters)

    resp = client.get(
        f"/v1/tokens/{Pubkey.new_unique()}/clusters",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_wallets_analyzed"] == 5
    assert len(body["clusters"]) == 1
    assert body["clusters"][0]["confidence"] == 0.67
    assert "shared_funding" in body["clusters"][0]["signals"]
