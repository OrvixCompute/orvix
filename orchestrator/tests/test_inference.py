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


def test_job_accrues_buyback_budget(client_and_db):
    """A completed job splits its platform fee into the buyback budget."""
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
    acct = next(r for r in db._table("global_accounting").rows if r["id"] == 1)
    # Platform fee = 30% of cost; buyback gets 50% of that. Both > 0.
    assert float(acct["buyback_budget_usdc"]) > 0
    assert float(acct["treasury_balance_usdc"]) > 0
    assert float(acct["operations_balance_usdc"]) > 0


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
