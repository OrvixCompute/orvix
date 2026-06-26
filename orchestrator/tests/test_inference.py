"""Unit tests for the inference endpoint and its billing/cost logic."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.models.inference import ChatMessage
from app.services import inference_service
from tests.fakes import FakeSupabase


# --- Pure cost-logic tests (no app needed) --------------------------------
def test_calculate_cost_bronze_no_discount():
    cost = inference_service.calculate_cost("qwen-2.5-7b", 1000, 1000, "bronze")
    # input 0.0001 + output 0.0002 = 0.0003 USDC
    assert cost == Decimal("0.000300")


def test_tier_discount_applied():
    bronze = inference_service.calculate_cost("qwen-2.5-7b", 1000, 1000, "bronze")
    diamond = inference_service.calculate_cost("qwen-2.5-7b", 1000, 1000, "diamond")
    # diamond gets 25% off
    assert diamond == inference_service.quantize_usdc(bronze * Decimal("0.75"))


def test_validate_model_rejects_unknown():
    from app.exceptions import ValidationError

    with pytest.raises(ValidationError) as exc:
        inference_service.validate_model("gpt-4")
    assert exc.value.error_code == "model_not_found"


def test_estimate_prompt_tokens_positive():
    msgs = [ChatMessage(role="user", content="hello world, this is a test")]
    assert inference_service.estimate_prompt_tokens(msgs) > 0


# --- Endpoint tests --------------------------------------------------------
@pytest.fixture
def client_and_db():
    db = FakeSupabase()
    api_key_id = "key-" + "0" * 8

    def fake_user_dep():
        # The user row must also exist in the DB for balance lookups.
        return {
            "user": db._table("users").rows[0],
            "api_key": {"id": api_key_id, "user_id": db._table("users").rows[0]["id"]},
        }

    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_user_from_api_key] = fake_user_dep
    # TestClient without a context manager skips lifespan (no DB connection probe).
    client = TestClient(app)
    yield client, db
    app.dependency_overrides.clear()


def _make_user(db, tier="gold", balance=1000.0):
    return db.add_user(tier=tier, balance_usdc=balance)


def test_happy_path_non_streaming(client_and_db):
    client, db = client_and_db
    _make_user(db)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"].startswith("This is a mock response")
    assert body["usage"]["total_tokens"] > 0
    assert "X-Orvix-Cost" in resp.headers
    # A job row was recorded and the balance dropped.
    assert len(db._table("jobs").rows) == 1
    assert db._table("users").rows[0]["balance_usdc"] < 1000.0


def test_tier_header_is_stake_based(client_and_db):
    """The served tier comes from staked_orvx, not the stored users.tier column."""
    client, db = client_and_db
    # Stored tier says bronze, but the stake puts them at diamond.
    _make_user(db, tier="bronze")
    db._table("users").rows[0]["staked_orvx"] = 250000.0
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["X-Orvix-Tier"] == "diamond"


def test_tier_header_bronze_when_unstaked(client_and_db):
    client, db = client_and_db
    _make_user(db, tier="gold")  # stored column ignored
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["X-Orvix-Tier"] == "bronze"


def test_mock_job_does_not_accrue_budget(client_and_db):
    """Mock-served jobs aren't billable revenue, so they must not touch accounting."""
    client, db = client_and_db
    _make_user(db)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
        },
    )
    assert resp.status_code == 200
    # The job was recorded as a mock...
    assert db._table("jobs").rows[0]["is_mock"] is True
    # ...and no revenue split ran, so no global_accounting row was created/touched.
    acct = [r for r in db._table("global_accounting").rows if r.get("id") == 1]
    assert acct == [] or float(acct[0]["buyback_budget_usdc"]) == 0


def test_record_job_real_accrues_budget_but_mock_does_not():
    """_record_job splits the fee only for real jobs (is_mock=False)."""
    from app.routes import inference as inference_route

    # Real job -> accounting accrues.
    db = FakeSupabase()
    inference_route._record_job(
        db, user_id="u1", api_key_id="k1", node_id="node-1", model="qwen-2.5-7b",
        prompt_tokens=1000, completion_tokens=1000, cost=Decimal("1.0"),
        latency_ms=5, is_mock=False,
    )
    acct = next(r for r in db._table("global_accounting").rows if r["id"] == 1)
    # Platform fee = cost - 70% provider = 0.30; buyback = 50% of fee = 0.15.
    assert float(acct["buyback_budget_usdc"]) == pytest.approx(0.15)
    assert float(acct["treasury_balance_usdc"]) == pytest.approx(0.09)
    assert float(acct["operations_balance_usdc"]) == pytest.approx(0.06)

    # Mock job -> no accounting row created at all.
    db2 = FakeSupabase()
    inference_route._record_job(
        db2, user_id="u1", api_key_id="k1", node_id=None, model="qwen-2.5-7b",
        prompt_tokens=1000, completion_tokens=1000, cost=Decimal("1.0"),
        latency_ms=5, is_mock=True,
    )
    assert len(db2._table("jobs").rows) == 1
    assert db2._table("jobs").rows[0]["is_mock"] is True
    assert [r for r in db2._table("global_accounting").rows if r.get("id") == 1] == []


def test_streaming_emits_done(client_and_db):
    client, db = client_and_db
    _make_user(db)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "mistral-7b",
            "messages": [{"role": "user", "content": "stream"}],
            "max_tokens": 64,
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "chat.completion.chunk" in text
    assert "data: [DONE]" in text


def test_insufficient_balance_returns_402(client_and_db):
    client, db = client_and_db
    _make_user(db, balance=0.0)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4096,
        },
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "insufficient_balance"


def test_invalid_model_returns_400(client_and_db):
    client, db = client_and_db
    _make_user(db)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_not_found"


def test_rate_limit_triggers(client_and_db):
    client, db = client_and_db
    _make_user(db, balance=1_000_000.0)
    headers = {"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"}
    payload = {
        "model": "qwen-2.5-7b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    statuses = []
    for _ in range(62):
        statuses.append(client.post("/v1/chat/completions", headers=headers, json=payload).status_code)
    assert 429 in statuses
