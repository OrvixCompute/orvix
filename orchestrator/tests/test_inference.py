"""Unit tests for the inference endpoint and its billing/cost logic."""

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.models.inference import ChatMessage
from app.services import inference_service
from app.services.holder import holder_service
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
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is module-global and every test here reuses one api_key_id,
    so without this the suite eventually trips its own 60/min limit."""
    from app.services import rate_limit_service

    rate_limit_service.reset()
    yield
    rate_limit_service.reset()


@pytest.fixture
def client_and_db(monkeypatch):
    db = FakeSupabase()
    api_key_id = "key-" + "0" * 8

    def fake_user_dep():
        # The user row must also exist in the DB for balance lookups.
        return {
            "user": db._table("users").rows[0],
            "api_key": {"id": api_key_id, "user_id": db._table("users").rows[0]["id"]},
        }

    # These tests exercise billing/tiers; treat the user as a holder so the chat
    # quota's free tier doesn't apply (free-tier behavior is covered in test_quota).
    async def fake_holder(db_, wallet):
        return True, 20000.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)

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
        db, user_id="u1", api_key_id="k1", node_id="node-1", provider_id="prov-1",
        model="qwen-2.5-7b",
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
        db2, user_id="u1", api_key_id="k1", node_id=None, provider_id=None,
        model="qwen-2.5-7b",
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


# --- Provider attribution --------------------------------------------------
def test_node_served_job_records_the_provider(client_and_db, monkeypatch):
    """The job stores provider_id itself rather than relying on the node row.

    Node rows are deleted routinely and (since migration 015) that nulls
    jobs.node_id, so inferring the provider through the node would silently
    lose the attribution for earnings already recorded against them.
    """
    from app.services import node_manager as nm_mod
    from app.services.node_manager import NodeConnection

    client, db = client_and_db
    _make_user(db)
    provider = db.add_user()

    node = NodeConnection(
        node_id="node-1",
        provider_id=provider["id"],
        websocket=None,
        model="qwen-2.5-7b",
        gpu_info={},
        max_concurrent_jobs=4,
        models_supported=["qwen-2.5-7b"],
    )
    monkeypatch.setattr(nm_mod.node_manager, "select_node", lambda model, tier: node)

    async def fake_dispatch(node_, job):
        from app.models.protocol import JobResultMessage

        return JobResultMessage(
            job_id=job.job_id,
            status="completed",
            result={
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ]
            },
            prompt_tokens=5,
            completion_tokens=3,
        )

    monkeypatch.setattr(nm_mod.node_manager, "dispatch_job", fake_dispatch)

    async def fake_settle(node_, cost):
        return Decimal("0")

    monkeypatch.setattr(nm_mod.node_manager, "settle_job", fake_settle)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text

    row = db._table("jobs").rows[0]
    assert row["provider_id"] == provider["id"]
    assert row["node_id"] == "node-1"
    assert row["is_mock"] is False


def test_mock_job_has_no_provider(client_and_db):
    # Nobody served it, so there is nobody to attribute it to.
    client, db = client_and_db
    _make_user(db)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    row = db._table("jobs").rows[0]
    assert row["is_mock"] is True
    assert row["provider_id"] is None


# --- refusing to fake it ---------------------------------------------------
def test_no_node_returns_503_when_mock_is_disabled(client_and_db, monkeypatch):
    """With no node and the mock off, chat must fail like images already do.

    A mock reply is indistinguishable from a real one apart from the
    X-Orvix-Node header, so serving it to real users misrepresents an empty
    network as a working one.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "ALLOW_MOCK_INFERENCE", False)
    client, db = client_and_db
    _make_user(db)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_chat_provider"
    # Nothing was served, so nothing was recorded.
    assert db._table("jobs").rows == []


def test_refusal_does_not_consume_the_free_quota(client_and_db, monkeypatch):
    """Availability is checked before the quota gate.

    The gate consumes one of the user's lifetime-free requests as a side
    effect, so checking it first would bill them for a request the network
    could never have served.
    """
    from app.config import settings
    from app.services import quota_service

    monkeypatch.setattr(settings, "ALLOW_MOCK_INFERENCE", False)
    client, db = client_and_db
    _make_user(db)

    calls = []
    real = quota_service.enforce_chat_quota
    monkeypatch.setattr(
        quota_service,
        "enforce_chat_quota",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 503
    assert calls == [], "quota was consumed for a request that was refused"


# --- tool calling ----------------------------------------------------------
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def _tool_node(monkeypatch, db, result_message, finish_reason="tool_calls"):
    """Wire a fake node that echoes back whatever message we hand it."""
    from app.services import node_manager as nm_mod
    from app.services.node_manager import NodeConnection

    provider = db.add_user()
    node = NodeConnection(
        node_id="node-tools",
        provider_id=provider["id"],
        websocket=None,
        model="qwen-2.5-7b",
        gpu_info={},
        max_concurrent_jobs=4,
        models_supported=["qwen-2.5-7b"],
    )
    monkeypatch.setattr(nm_mod.node_manager, "select_node", lambda model, tier: node)

    seen = {}

    async def fake_dispatch(node_, job):
        from app.models.protocol import JobResultMessage

        seen["job"] = job
        return JobResultMessage(
            job_id=job.job_id,
            status="completed",
            result={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "model": job.model,
                "choices": [
                    {"index": 0, "message": result_message, "finish_reason": finish_reason}
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
            },
            prompt_tokens=42,
            completion_tokens=17,
        )

    async def fake_settle(node_, cost):
        return Decimal("0")

    monkeypatch.setattr(nm_mod.node_manager, "dispatch_job", fake_dispatch)
    monkeypatch.setattr(nm_mod.node_manager, "settle_job", fake_settle)
    return seen


def test_tools_reach_the_node(client_and_db, monkeypatch):
    client, db = client_and_db
    _make_user(db)
    seen = _tool_node(monkeypatch, db, {"role": "assistant", "content": "hi"}, "stop")

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "weather in Jakarta?"}],
            "tools": _TOOLS,
            "tool_choice": "auto",
        },
    )

    assert resp.status_code == 200, resp.text
    job = seen["job"]
    assert job.tools == _TOOLS
    assert job.tool_choice == "auto"


def test_tool_calls_are_returned_to_the_caller(client_and_db, monkeypatch):
    client, db = client_and_db
    _make_user(db)
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Jakarta"}'},
            }
        ],
    }
    _tool_node(monkeypatch, db, message)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "weather in Jakarta?"}],
            "tools": _TOOLS,
        },
    )

    assert resp.status_code == 200, resp.text
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Jakarta"}


def test_a_tool_result_message_round_trips(client_and_db, monkeypatch):
    """role="tool" with tool_call_id must be accepted and forwarded."""
    client, db = client_and_db
    _make_user(db)
    seen = _tool_node(monkeypatch, db, {"role": "assistant", "content": "It is 30C."}, "stop")

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [
                {"role": "user", "content": "weather in Jakarta?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Jakarta"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_abc123", "content": "30C, humid"},
            ],
            "tools": _TOOLS,
        },
    )

    assert resp.status_code == 200, resp.text
    roles = [m["role"] for m in seen["job"].messages]
    assert roles == ["user", "assistant", "tool"]
    assert seen["job"].messages[2]["tool_call_id"] == "call_abc123"


def test_streaming_with_tools_is_refused(client_and_db):
    """Better an explicit 400 than streaming prose and dropping the calls."""
    client, db = client_and_db
    _make_user(db)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={
            "model": "qwen-2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": _TOOLS,
            "stream": True,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "streaming_tools_unsupported"


def test_dispatched_messages_carry_no_null_tool_fields(client_and_db, monkeypatch):
    """Optional tool fields must not be serialised as explicit nulls.

    vLLM rejects a user message carrying tool_call_id=null with a wall of
    validation errors, so dumping every optional field would break *every*
    ordinary chat request, not just tool-calling ones.
    """
    client, db = client_and_db
    _make_user(db)
    seen = _tool_node(monkeypatch, db, {"role": "assistant", "content": "hi"}, "stop")

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200, resp.text
    sent = seen["job"].messages[0]
    assert sent == {"role": "user", "content": "hi"}
    assert all(v is not None for v in sent.values())


# --- capacity vs. nothing-serves-this-model --------------------------------
def _busy_node(provider_id: str, model: str = "qwen-2.5-7b"):
    """A connected node that serves `model` but has no free slot."""
    from app.services.node_manager import NodeConnection

    return NodeConnection(
        node_id="node-busy",
        provider_id=provider_id,
        websocket=None,
        model=model,
        gpu_info={},
        max_concurrent_jobs=1,
        status="busy",
        current_jobs=1,
        models_supported=[model],
        engines=["chat", "image"],
    )


def test_busy_nodes_report_capacity_not_absence(client_and_db, monkeypatch):
    """A saturated node must not be reported as "no providers available".

    Telling a user no provider exists while providers are in fact serving jobs
    is wrong in a way that matters: capacity is transient and worth retrying,
    an unserved model is not.
    """
    from app.config import settings
    from app.services import node_manager as nm_mod

    monkeypatch.setattr(settings, "ALLOW_MOCK_INFERENCE", False)
    client, db = client_and_db
    _make_user(db)
    provider = db.add_user()
    monkeypatch.setitem(
        nm_mod.node_manager.connected_nodes, "node-busy", _busy_node(provider["id"])
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "capacity_exhausted"
    # details are merged into the error body, matching RateLimitError's shape.
    assert body["retry_after_seconds"] > 0
    assert db._table("jobs").rows == []


def test_unserved_model_still_reports_absence(client_and_db, monkeypatch):
    """A node that serves a *different* model must not be read as capacity."""
    from app.config import settings
    from app.services import node_manager as nm_mod

    monkeypatch.setattr(settings, "ALLOW_MOCK_INFERENCE", False)
    client, db = client_and_db
    _make_user(db)
    provider = db.add_user()
    monkeypatch.setitem(
        nm_mod.node_manager.connected_nodes,
        "node-busy",
        _busy_node(provider["id"], model="qwen-2.5-7b"),
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "mistral-7b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_chat_provider"


def test_refusal_skips_the_balance_lookup(client_and_db, monkeypatch):
    """Availability is decided before any billing I/O.

    The registry can answer instantly; the balance lookup is a database round
    trip. Under load those round trips queued behind each other and turned a
    refusal into a multi-second wait, so the refusal path must not touch them.
    """
    from app.config import settings
    from app.services.billing_service import BillingService

    monkeypatch.setattr(settings, "ALLOW_MOCK_INFERENCE", False)
    client, db = client_and_db
    _make_user(db)

    def explode(self, user_id):  # noqa: ANN001
        raise AssertionError("balance was fetched on a request that cannot be served")

    monkeypatch.setattr(BillingService, "get_balance", explode)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"},
        json={"model": "qwen-2.5-7b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_chat_provider"
