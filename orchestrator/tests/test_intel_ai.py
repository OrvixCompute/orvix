"""Tests for the AI intelligence layer (services/intel_ai.py)."""

import json

import pytest
from solders.pubkey import Pubkey

from app.services import intel_ai, token_intel
from app.services.intel_ai import _parse_json


class FakeResult:
    def __init__(self, content=None, status="completed", error=None):
        self.status = status
        self.error = error
        self.prompt_tokens = 120
        self.completion_tokens = 80
        self.result = {"choices": [{"message": {"content": content}}]} if content else {}


class FakeNode:
    def __init__(self):
        self.node_id = "node-1"
        self.provider_id = "prov-1"


class FakeNodeManager:
    def __init__(self, node=None, result=None, exc=None):
        self._node = node
        self._result = result
        self._exc = exc
        self.jobs = []

    def select_node(self, model, tier):
        return self._node

    async def dispatch_job(self, node, job):
        if self._exc:
            raise self._exc
        self.jobs.append(job)
        return self._result


class FakeSol:
    async def get_token_supply(self, mint):
        return {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}

    async def get_account_info(self, address, encoding="base64"):
        return None

    async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
        return []


@pytest.fixture
def ctx(monkeypatch):
    token_intel.reset_scan_cache()
    yield
    token_intel.reset_scan_cache()


def _empty_db():
    class _Q:
        def __init__(self, rows=None):
            self._rows = rows or []

        def select(self, *a, **k):
            return self

        def eq(self, c, v):
            return self

        def limit(self, n):
            return self

        def execute(self):
            return type("R", (), {"data": self._rows})()

        def insert(self, values):
            self._inserted = values
            return self

        def upsert(self, values, on_conflict=None):
            self._upserted = values
            return self

    class _Db:
        def __init__(self):
            self.rows = {}

        def table(self, name):
            return _Q(self.rows.get(name, []))

    return _Db()


@pytest.mark.asyncio
async def test_no_node_returns_none(monkeypatch):
    monkeypatch.setattr(intel_ai, "node_manager", FakeNodeManager(node=None))
    monkeypatch.setattr(intel_ai.token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(intel_ai.token_intel, "get_token_price_usdc", _price(None))

    result = await intel_ai.generate_token_intelligence(_empty_db(), str(Pubkey.new_unique()))
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_failure_is_fail_soft(monkeypatch):
    nm = FakeNodeManager(node=FakeNode(), exc=RuntimeError("node down"))
    monkeypatch.setattr(intel_ai, "node_manager", nm)
    monkeypatch.setattr(intel_ai.token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(intel_ai.token_intel, "get_token_price_usdc", _price(0.5))

    result = await intel_ai.generate_token_intelligence(_empty_db(), str(Pubkey.new_unique()))
    assert result is None


@pytest.mark.asyncio
async def test_node_failure_is_fail_soft(monkeypatch):
    nm = FakeNodeManager(node=FakeNode(), result=FakeResult(status="failed", error="oom"))
    monkeypatch.setattr(intel_ai, "node_manager", nm)
    monkeypatch.setattr(intel_ai.token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(intel_ai.token_intel, "get_token_price_usdc", _price(0.5))

    result = await intel_ai.generate_token_intelligence(_empty_db(), str(Pubkey.new_unique()))
    assert result is None


@pytest.mark.asyncio
async def test_successful_analysis_parsed(monkeypatch):
    content = json.dumps({"narrative": "Whales accumulating.", "risk_flags": ["illiquid"], "watch_next": "watch top holder"})
    nm = FakeNodeManager(node=FakeNode(), result=FakeResult(content=content))
    monkeypatch.setattr(intel_ai, "node_manager", nm)
    monkeypatch.setattr(intel_ai.token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(intel_ai.token_intel, "get_token_price_usdc", _price(0.5))

    db = _empty_db()
    result = await intel_ai.generate_token_intelligence(db, str(Pubkey.new_unique()))
    assert result is not None
    assert result["analysis"]["narrative"] == "Whales accumulating."
    assert result["analysis"]["risk_flags"] == ["illiquid"]
    assert result["node_id"] == "node-1"
    # Job was dispatched to the GPU network — the flywheel.
    assert len(nm.jobs) == 1
    assert nm.jobs[0].model == "qwen-2.5-7b"


@pytest.mark.asyncio
async def test_cached_analysis_avoids_dispatch(monkeypatch):
    mint = str(Pubkey.new_unique())
    token_intel._cache_put("intelligence", mint, {"mint": mint, "model": "m", "analysis": {"narrative": "cached"}, "generated_at": "x"})
    nm = FakeNodeManager(node=FakeNode(), result=FakeResult(content="{}"))
    monkeypatch.setattr(intel_ai, "node_manager", nm)

    result = await intel_ai.generate_token_intelligence(_empty_db(), mint)
    assert result["analysis"]["narrative"] == "cached"
    assert nm.jobs == []  # no dispatch on cache hit


def test_parse_json_handles_code_fence():
    raw = '```json\n{"narrative": "x", "risk_flags": []}\n```'
    parsed = _parse_json(raw)
    assert parsed is not None
    assert parsed["narrative"] == "x"


def test_parse_json_handles_plain():
    parsed = _parse_json('{"a": 1}')
    assert parsed == {"a": 1}


def test_parse_json_rejects_non_json():
    assert _parse_json("just prose, no json") is None


def _price(value):
    async def _p(mint):
        from decimal import Decimal

        return Decimal(str(value)) if value is not None else None

    return _p


# ---------------------------------------------------------------------------
# Social integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_intelligence_includes_social_data(monkeypatch, ctx):
    """Social data should be included in the prompt sent to the GPU node."""
    content = json.dumps({"narrative": "Trending on DexScreener.", "risk_flags": [], "watch_next": "watch volume"})
    nm = FakeNodeManager(node=FakeNode(), result=FakeResult(content=content))
    monkeypatch.setattr(intel_ai, "node_manager", nm)
    monkeypatch.setattr(intel_ai.token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(intel_ai.token_intel, "get_token_price_usdc", _price(0.5))

    social_data = {
        "mint": "test",
        "social_links": {"twitter": "https://x.com/t"},
        "social_score": 75,
        "metrics": {
            "dex_trending": True,
            "dex_volume_24h": 50000.0,
            "dex_price_change_24h": 15.0,
            "twitter_followers": 5000,
            "twitter_statuses_7d": 30,
            "social_sentiment": "positive",
        },
        "as_of": "2025-01-01T00:00:00",
    }
    monkeypatch.setattr(intel_ai.social_intel, "analyze_social", _social(social_data))

    db = _empty_db()
    result = await intel_ai.generate_token_intelligence(db, str(Pubkey.new_unique()))
    assert result is not None
    assert result["analysis"]["narrative"] == "Trending on DexScreener."
    # Verify the prompt included social data by checking the job messages.
    assert len(nm.jobs) == 1
    user_msg = nm.jobs[0].messages[1]["content"]
    assert "Social signals" in user_msg
    assert "75/100" in user_msg
    assert "5000" in user_msg


@pytest.mark.asyncio
async def test_intelligence_works_without_social(monkeypatch, ctx):
    """Intelligence should work fine when social analysis fails."""
    content = json.dumps({"narrative": "On-chain only.", "risk_flags": [], "watch_next": ""})
    nm = FakeNodeManager(node=FakeNode(), result=FakeResult(content=content))
    monkeypatch.setattr(intel_ai, "node_manager", nm)
    monkeypatch.setattr(intel_ai.token_intel, "get_solana_service", lambda: FakeSol())
    monkeypatch.setattr(intel_ai.token_intel, "get_token_price_usdc", _price(0.5))
    monkeypatch.setattr(intel_ai.social_intel, "analyze_social", _social(None))

    db = _empty_db()
    result = await intel_ai.generate_token_intelligence(db, str(Pubkey.new_unique()))
    assert result is not None
    assert result["analysis"]["narrative"] == "On-chain only."
    # Prompt should NOT contain social signals block.
    user_msg = nm.jobs[0].messages[1]["content"]
    assert "Social signals" not in user_msg


def _social(data):
    async def _s(db, mint):
        return data
    return _s
