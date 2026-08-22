"""End-to-end intel pipeline: scan → accumulation → social → AI intelligence → monitor alert.

Exercises the full token-intelligence flow through the API endpoints with mocked
Solana RPC, DexScreener, and GPU node dispatch. Verifies that data flows correctly
between services and that the monitor worker fires alerts when conditions are met.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from solders.pubkey import Pubkey

from app.database import get_supabase
from app.dependencies import get_current_user_flexible
from app.main import app
from app.services import (
    holder_intel,
    intel_ai,
    monitor_service,
    rate_limit_service,
    social_intel,
    token_intel,
)
from app.services.node_manager import node_manager
from tests.fakes import FakeSupabase


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSolana:
    """Controllable Solana RPC stub for the full pipeline."""

    def __init__(self):
        self.supply = {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}
        self.metadata_account = None
        self.accounts_by_owner = []
        self.signatures = []
        self.parsed_txs = {}
        self.token_balances = {}
        self.largest = []
        self.owners = {}
        self.sigs_by_wallet = {}
        self.token_accounts_by_owner = {}

    async def get_token_supply(self, mint):
        return self.supply

    async def get_account_info(self, address, encoding="base64"):
        return self.metadata_account

    async def get_token_accounts_by_owner(self, owner, mint=None):
        if mint:
            return self.accounts_by_owner
        return self.token_accounts_by_owner.get(owner, [])

    async def get_token_balance(self, owner, mint):
        return self.token_balances.get((owner, mint), Decimal(0))

    async def get_token_largest_accounts(self, mint):
        return self.largest

    async def get_token_account_owner(self, account):
        return self.owners.get(account)

    async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
        return self.sigs_by_wallet.get(address, self.signatures)

    async def get_parsed_transaction(self, signature):
        return self.parsed_txs.get(signature)

    @staticmethod
    def extract_memo(parsed_tx):
        return None


class FakeNode:
    def __init__(self):
        self.node_id = "node-pipeline-1"
        self.provider_id = "prov-pipeline-1"


class FakeNodeManager:
    def __init__(self, node=None, result=None):
        self._node = node
        self._result = result
        self.jobs = []

    def select_node(self, model, tier):
        return self._node

    async def dispatch_job(self, node, job):
        self.jobs.append(job)
        return self._result


class FakeJobResult:
    def __init__(self, content):
        self.status = "completed"
        self.error = None
        self.prompt_tokens = 150
        self.completion_tokens = 100
        self.result = {"choices": [{"message": {"content": content}}]}


class FakeHttpResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeHttpClient:
    """Context-manager mock for httpx.AsyncClient."""

    def __init__(self, responses=None):
        self._responses = responses or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        for pattern, resp in self._responses.items():
            if pattern in url:
                return resp
        return FakeHttpResponse(status_code=404)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dex_response(trending=True, volume=25000.0, price_change=12.5, twitter_url=None):
    links = []
    if twitter_url:
        links.append({"type": "twitter", "url": twitter_url})
    return FakeHttpResponse({
        "pairs": [{
            "liquidity": {"usd": 50000},
            "volume": {"h24": volume},
            "priceChange": {"h24": price_change},
            "info": {
                "links": links,
                "socials": [{"type": "twitter", "url": twitter_url}] if twitter_url else [],
                "websites": [],
            },
            "boosts": {"active": trending},
        }]
    })


def _make_twitter_response(followers=3000, tweet_count=50):
    return FakeHttpResponse({
        "data": {
            "id": "12345",
            "username": "testtoken",
            "public_metrics": {
                "followers_count": followers,
                "tweet_count": tweet_count,
            },
        }
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pipeline_env(monkeypatch):
    """Full pipeline environment: DB, client, user, mocked externals."""
    db = FakeSupabase()
    user = db.add_user()

    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user_flexible] = lambda: user

    token_intel.reset_scan_cache()
    rate_limit_service.reset()

    sol = FakeSolana()
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    async def _price(mint):
        return Decimal("1.50")

    monkeypatch.setattr(token_intel, "get_token_price_usdc", _price)

    client = TestClient(app)
    yield client, db, user, sol, monkeypatch

    app.dependency_overrides.clear()
    token_intel.reset_scan_cache()
    rate_limit_service.reset()


def _auth_headers(user):
    """Build auth headers using the flexible auth dependency override."""
    return {}


# ---------------------------------------------------------------------------
# Pipeline tests
# ---------------------------------------------------------------------------

class TestIntelPipeline:
    """Full intel pipeline: scan → accumulation → social → intelligence → monitor."""

    def test_step1_token_scan(self, pipeline_env):
        """Step 1: Token scan returns metadata, supply, price, liquidity, risk."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        resp = client.get(f"/v1/tokens/{mint}", headers=_auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()

        assert body["mint"] == mint
        assert body["supply"]["decimals"] == 6
        assert body["price_usdc"] == 1.5
        assert body["liquidity"]["pool_count"] == 0
        assert isinstance(body["risk"]["warnings"], list)
        assert body["holders"]["top_holders"] == []

    def test_step2_accumulation(self, pipeline_env):
        """Step 2: Accumulation score is computed from scan data."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        resp = client.get(f"/v1/tokens/{mint}/accumulation", headers=_auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()

        assert 0 <= body["score"] <= 100
        assert body["label"] in ("distribution", "weak", "moderate", "strong")
        assert "metrics" in body

    def test_step3_social_analysis(self, pipeline_env):
        """Step 3: Social analysis returns DexScreener + Twitter data."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        dex_resp = _make_dex_response(
            trending=True, volume=25000.0, price_change=12.5,
            twitter_url="https://x.com/testtoken",
        )
        twitter_resp = _make_twitter_response(followers=3000, tweet_count=50)

        def _fake_client(**kwargs):
            return FakeHttpClient({
                "dexscreener.com": dex_resp,
                "api.twitter.com": twitter_resp,
            })

        monkeypatch.setattr(social_intel, "httpx", type("M", (), {"AsyncClient": _fake_client}))
        monkeypatch.setattr(social_intel.settings, "X_BEARER_TOKEN", "test-bearer")

        resp = client.get(f"/v1/tokens/{mint}/social", headers=_auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()

        assert body["social_score"] > 0
        assert body["social_links"]["twitter"] == "https://x.com/testtoken"
        assert body["metrics"]["dex_trending"] is True
        assert body["metrics"]["dex_volume_24h"] == 25000.0
        assert body["metrics"]["twitter_followers"] == 3000
        assert body["metrics"]["social_sentiment"] == "positive"

    def test_step4_holders_and_clusters(self, pipeline_env):
        """Step 4: Holder enumeration + wallet clustering."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())
        w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
        funder = str(Pubkey.new_unique())

        # Setup holders
        sol.largest = [
            {"address": "acc1", "uiAmountString": "600.0"},
            {"address": "acc2", "uiAmountString": "400.0"},
        ]
        sol.owners = {"acc1": w_a, "acc2": w_b}

        # Setup clustering: shared funding
        sol.sigs_by_wallet[w_a] = [{"signature": "sig-a", "blockTime": 100, "err": None}]
        sol.sigs_by_wallet[w_b] = [{"signature": "sig-b", "blockTime": 100, "err": None}]
        sol.parsed_txs["sig-a"] = {"transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {"source": funder, "destination": w_a, "lamports": "10000000"}}},
        ]}}}
        sol.parsed_txs["sig-b"] = {"transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {"source": funder, "destination": w_b, "lamports": "10000000"}}},
        ]}}}
        sol.token_accounts_by_owner = {w_a: [], w_b: []}

        # Holders endpoint
        resp = client.get(f"/v1/tokens/{mint}/holders", headers=_auth_headers(user))
        assert resp.status_code == 200
        holders = resp.json()
        assert holders["total_holders"] == 2
        assert holders["top_holders"][0]["wallet"] == w_a
        assert holders["top10_share"] == 1.0

        # Clusters endpoint
        resp = client.get(f"/v1/tokens/{mint}/clusters", headers=_auth_headers(user))
        assert resp.status_code == 200
        clusters = resp.json()
        assert clusters["total_wallets_analyzed"] == 2
        assert len(clusters["clusters"]) == 1
        assert "shared_funding" in clusters["clusters"][0]["signals"]

    def test_step5_ai_intelligence(self, pipeline_env):
        """Step 5: AI intelligence dispatches to GPU node and returns analysis."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        ai_content = json.dumps({
            "narrative": "Strong accumulation by coordinated wallets.",
            "risk_flags": ["high concentration"],
            "watch_next": "Monitor top holder movements",
            "verdict": "hold",
            "reasons": ["High top-10 share", "Moderate accumulation", "No social presence"],
        })
        result = FakeJobResult(content=ai_content)
        nm = FakeNodeManager(node=FakeNode(), result=result)
        monkeypatch.setattr(intel_ai, "node_manager", nm)

        resp = client.get(f"/v1/tokens/{mint}/intelligence", headers=_auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()

        assert body["analysis"]["narrative"] == "Strong accumulation by coordinated wallets."
        assert body["analysis"]["verdict"] == "hold"
        assert body["analysis"]["risk_flags"] == ["high concentration"]
        assert body["model"] == "qwen-2.5-7b"
        assert body["node_id"] == "node-pipeline-1"
        # GPU job was dispatched — the flywheel.
        assert len(nm.jobs) == 1

    def test_step6_intelligence_503_without_node(self, pipeline_env):
        """Step 6: Intelligence returns 503 when no GPU node is available."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        nm = FakeNodeManager(node=None)
        monkeypatch.setattr(intel_ai, "node_manager", nm)
        monkeypatch.setattr(node_manager, "unavailable_reason", lambda model, engine=None: "no_node")

        resp = client.get(f"/v1/tokens/{mint}/intelligence", headers=_auth_headers(user))
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "no_chat_provider"

    @pytest.mark.asyncio
    async def test_step7_monitor_fires_alert(self, pipeline_env):
        """Step 7: Monitor evaluation fires alert when accumulation threshold met."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        # Setup accumulation to return high score
        async def fake_accumulation(_db, target, **kwargs):
            return {"score": 85, "label": "strong", "metrics": {"inflow_7d": 50.0}}

        fake_ti = type("T", (), {"compute_accumulation": staticmethod(fake_accumulation)})()
        monkeypatch.setattr(monitor_service, "token_intel", fake_ti)

        # Skip AI analysis for this test
        async def fake_intel(_db, target):
            return None

        monkeypatch.setattr(monitor_service, "intel_ai", type("I", (), {"generate_token_intelligence": staticmethod(fake_intel)})())

        # Create a monitor via the service directly
        monitor_db = _MonitorDb()
        monitor = {
            "id": "m-pipeline-1",
            "user_id": user["id"],
            "name": "pipeline test monitor",
            "target_type": "token",
            "target_address": mint,
            "conditions": [{"type": "accumulation_score", "gte": 70}],
            "webhook_url": "https://example.com/hook",
            "is_active": True,
            "interval_minutes": 30,
            "baseline_price_usdc": None,
            "last_checked_at": None,
            "last_cursor": None,
        }
        monitor_db.monitors.append(monitor)

        svc = monitor_service.monitor_service
        await svc._evaluate_monitor(monitor_db, monitor)

        assert len(monitor_db.alert_events) == 1
        event = monitor_db.alert_events[0]
        assert event["condition_type"] == "accumulation_score"
        assert event["monitor_id"] == "m-pipeline-1"
        assert event["dedup_key"].startswith("acc:")

        # Webhook enqueued
        assert len(monitor_db.alert_webhooks) == 1
        assert monitor_db.alert_webhooks[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_step8_monitor_dedup(self, pipeline_env):
        """Step 8: Duplicate alerts are suppressed by dedup_key."""
        client, db, user, sol, monkeypatch = pipeline_env

        monitor_db = _MonitorDb()
        monitor = {
            "id": "m-dedup",
            "user_id": user["id"],
            "name": "dedup test",
            "target_type": "token",
            "target_address": str(Pubkey.new_unique()),
            "conditions": [{"type": "accumulation_score", "gte": 70}],
            "webhook_url": None,
            "is_active": True,
            "interval_minutes": 30,
            "baseline_price_usdc": None,
            "last_checked_at": None,
            "last_cursor": None,
        }

        svc = monitor_service.monitor_service
        # Fire same alert twice with same dedup key
        await svc._emit_alert(monitor_db, monitor, "accumulation_score", "msg1", "acc:2026-08-22", {"s": 85})
        await svc._emit_alert(monitor_db, monitor, "accumulation_score", "msg2", "acc:2026-08-22", {"s": 90})

        assert len(monitor_db.alert_events) == 1

    def test_full_pipeline_caching(self, pipeline_env):
        """Full pipeline: repeated scans hit cache, not RPC."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        # First scan hits RPC
        resp1 = client.get(f"/v1/tokens/{mint}", headers=_auth_headers(user))
        assert resp1.status_code == 200

        # Second scan should hit in-memory cache
        resp2 = client.get(f"/v1/tokens/{mint}", headers=_auth_headers(user))
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()

    def test_social_graceful_degradation(self, pipeline_env):
        """Social analysis degrades gracefully when DexScreener is down."""
        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        def _broken_client(**kwargs):
            return FakeHttpClient({})  # all 404

        monkeypatch.setattr(social_intel, "httpx", type("M", (), {"AsyncClient": _broken_client}))

        resp = client.get(f"/v1/tokens/{mint}/social", headers=_auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["social_score"] == 0
        assert body["metrics"]["dex_trending"] is False

    def test_invalid_address_returns_400(self, pipeline_env):
        """Invalid Solana address returns 400 on all intel endpoints."""
        client, db, user, sol, monkeypatch = pipeline_env

        for path in [
            "/v1/tokens/not-an-address",
            "/v1/tokens/not-an-address/accumulation",
            "/v1/tokens/not-an-address/intelligence",
            "/v1/tokens/not-an-address/holders",
            "/v1/tokens/not-an-address/social",
            "/v1/tokens/not-an-address/clusters",
        ]:
            resp = client.get(path, headers=_auth_headers(user))
            assert resp.status_code == 400, f"Expected 400 for {path}"

    def test_rate_limit_across_scan_endpoints(self, pipeline_env):
        """Scan endpoints share the 'intel' rate-limit bucket."""
        from app.services import tier_service

        client, db, user, sol, monkeypatch = pipeline_env
        mint = str(Pubkey.new_unique())

        # Shrink rate limit so the test trips quickly
        monkeypatch.setitem(tier_service.TIER_RATE_LIMITS, "bronze", 2)

        assert client.get(f"/v1/tokens/{mint}", headers=_auth_headers(user)).status_code == 200
        assert client.get(f"/v1/tokens/{mint}", headers=_auth_headers(user)).status_code == 200
        # Third request in the same bucket is throttled
        resp = client.get(f"/v1/tokens/{mint}/accumulation", headers=_auth_headers(user))
        assert resp.status_code == 429
        assert resp.json()["error"]["bucket"] == "intel"


# ---------------------------------------------------------------------------
# Helpers for monitor tests
# ---------------------------------------------------------------------------

class _MonitorDb:
    """Minimal in-memory DB for monitor evaluation tests."""

    def __init__(self):
        self.monitors = []
        self.alert_events = []
        self.alert_webhooks = []

    def table(self, name):
        return _MonitorTable(name, self)


class _MonitorTable:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self._filters = []
        self._op = None
        self._values = None

    def _rows(self):
        return getattr(self.owner, self.name)

    def select(self, *cols, **kw):
        return self

    def eq(self, c, v):
        self._filters.append((c, "eq", v))
        return self

    def in_(self, c, values):
        self._filters.append((c, "in", values))
        return self

    def limit(self, n):
        return self

    def order(self, c, desc=False):
        return self

    def insert(self, values):
        self._op = "insert"
        self._values = values
        return self

    def update(self, values):
        self._op = "update"
        self._values = values
        return self

    def delete(self):
        self._op = "delete"
        return self

    def _match(self, row):
        for c, op, v in self._filters:
            if op == "eq" and row.get(c) != v:
                return False
            if op == "in" and row.get(c) not in v:
                return False
        return True

    def execute(self):
        rows = self._rows()
        if self._op == "insert":
            row = dict(self._values)
            row.setdefault("id", f"{self.name}-{len(rows) + 1}")
            row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            rows.append(row)
            return type("R", (), {"data": [row]})()
        if self._op == "update":
            for r in rows:
                if self._match(r):
                    r.update(self._values)
            return type("R", (), {"data": []})()
        if self._op == "delete":
            for r in list(rows):
                if self._match(r):
                    rows.remove(r)
            return type("R", (), {"data": []})()
        matched = [r for r in rows if self._match(r)]
        return type("R", (), {"data": matched})()
