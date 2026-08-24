"""Tests for provider REST endpoints via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

import app.routes.provider as provider_mod
import app.services.node_manager as nm
import app.services.payout_service as payout_mod
from app.database import get_supabase
from app.dependencies import get_current_user
from app.main import app
from app.services.node_manager import NodeConnection, node_manager
from tests.fakes import FakeSupabase

VALID_WALLET = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


@pytest.fixture
def ctx(monkeypatch):
    db = FakeSupabase()
    user = db.add_user(
        tier="gold",
        available_usdc=500.0,
        lifetime_earnings_usdc=500.0,
        staked_orvx=50000.0,
    )
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(payout_mod, "get_supabase", lambda: db)
    client = TestClient(app)
    yield client, db, user
    app.dependency_overrides.clear()


def test_register_returns_both_credentials(ctx):
    """`orvix-node join` needs the id as well as the secret, so both must ship.

    Returning only the secret stalled onboarding: the id is `users.id`, which
    the register response never carried.
    """
    client, db, user = ctx
    resp = client.post("/v1/provider/register", json={"display_name": "My Rig"})
    assert resp.status_code == 200
    assert resp.json()["node_secret"]
    assert resp.json()["provider_id"] == str(user["id"])
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert row["is_provider"] is True
    assert row["provider_secret_hash"]


def test_provider_register_with_stake_required_true_blocks_below_minimum(monkeypatch):
    """Named for the behaviour, not the number.

    This used to carry `25k` in its name and assert `required == "25000"`, so it
    broke the moment the policy moved — testing the setting rather than the gate.
    It now pins its own minimum and asserts the gate honours it.
    """
    monkeypatch.setattr(provider_mod.settings, "REQUIRE_STAKE_FOR_PROVIDER", True)
    monkeypatch.setattr(provider_mod.settings, "PROVIDER_MIN_STAKE_ORVX", 25000)
    db = FakeSupabase()
    # below the pinned minimum, and not yet a provider
    user = db.add_user(
        tier="bronze", staked_orvx=1000.0, is_provider=False, provider_secret_hash=None
    )
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        resp = client.post("/v1/provider/register", json={})
        assert resp.status_code == 400
        body = resp.json()["error"]
        assert body["code"] == "insufficient_stake"
        assert body["required"] == "25000"
        # User was not flipped to provider.
        row = next(r for r in db._table("users").rows if r["id"] == user["id"])
        assert row.get("is_provider") is False
        assert row.get("provider_secret_hash") is None
    finally:
        app.dependency_overrides.clear()


def test_provider_register_with_stake_required_false_allows_zero_stake(monkeypatch):
    # Default alpha behaviour: flag false -> no stake check, 0 stake allowed.
    monkeypatch.setattr(provider_mod.settings, "REQUIRE_STAKE_FOR_PROVIDER", False)
    db = FakeSupabase()
    user = db.add_user(
        tier="bronze", staked_orvx=0.0, is_provider=False, provider_secret_hash=None
    )
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        resp = client.post("/v1/provider/register", json={})
        assert resp.status_code == 200
        assert resp.json()["node_secret"]
        row = next(r for r in db._table("users").rows if r["id"] == user["id"])
        assert row["is_provider"] is True
        assert row["provider_secret_hash"]
    finally:
        app.dependency_overrides.clear()


def test_regenerate_secret_changes_hash(ctx):
    client, db, user = ctx
    client.post("/v1/provider/register", json={})
    first = next(r for r in db._table("users").rows if r["id"] == user["id"])["provider_secret_hash"]
    r = client.post("/v1/provider/regenerate-secret", json={})
    second = next(r2 for r2 in db._table("users").rows if r2["id"] == user["id"])["provider_secret_hash"]
    assert first != second
    # Rotating hands back a full credential pair too, so `join` works after a rotation.
    assert r.json()["provider_id"] == str(user["id"])


def test_list_and_rename_node(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row(
        {"id": "node-1", "provider_id": user["id"], "status": "ready", "name": "old"}
    )
    listed = client.get("/v1/provider/nodes").json()
    assert len(listed) == 1
    assert listed[0]["id"] == "node-1"
    assert listed[0]["is_connected"] is False

    r = client.post("/v1/provider/nodes/node-1/rename", json={"name": "new-name"})
    assert r.status_code == 200
    assert db._table("nodes").rows[0]["name"] == "new-name"


def test_rename_foreign_node_404(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row({"id": "other", "provider_id": "someone-else"})
    r = client.post("/v1/provider/nodes/other/rename", json={"name": "x"})
    assert r.status_code == 404


def test_earnings_aggregation(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row({"id": "node-1", "provider_id": user["id"]})
    for _ in range(3):
        db._table("jobs").insert_row(
            {"node_id": "node-1", "provider_earning_usdc": 1.5, "user_id": "dev"}
        )
    data = client.get("/v1/provider/earnings").json()
    assert data["available_to_withdraw"] == "500.0"
    assert data["total_lifetime_usdc"] == "500.0"
    assert len(data["earnings_by_day"]) == 1
    assert data["earnings_by_day"][0]["jobs_count"] == 3


def test_withdraw_below_minimum(ctx):
    client, db, user = ctx
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 10, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 400


def test_withdraw_valid(ctx):
    client, db, user = ctx
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 200, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["withdrawal_id"]
    # Funds moved available -> pending.
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert float(row["available_usdc"]) == pytest.approx(300.0)
    assert float(row["pending_withdrawal_usdc"]) == pytest.approx(200.0)


def test_withdraw_insufficient(ctx):
    client, db, user = ctx
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 9999, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 402


def test_withdraw_refused_for_non_provider(monkeypatch):
    """A funded non-provider must not reach the payout path.

    Funded deliberately: the old gate was `available_usdc`, so a zero-balance
    non-provider would be refused for the wrong reason and prove nothing.
    """
    db = FakeSupabase()
    user = db.add_user(available_usdc=500.0, is_provider=False, provider_secret_hash=None)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(payout_mod, "get_supabase", lambda: db)
    client = TestClient(app)
    try:
        r = client.post(
            "/v1/provider/withdraw",
            json={"amount": 200, "destination_wallet": VALID_WALLET},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "not_a_provider"
        # Nothing was locked: the balance never moved to pending.
        row = next(r for r in db._table("users").rows if r["id"] == user["id"])
        assert float(row["available_usdc"]) == pytest.approx(500.0)
        assert float(row.get("pending_withdrawal_usdc") or 0) == pytest.approx(0.0)
        assert db._table("withdrawals").rows == []
    finally:
        app.dependency_overrides.clear()


def test_withdraw_estimate_reflects_the_worker_interval(ctx, monkeypatch):
    """The estimate must come from the worker's own cadence, not a fixed string."""
    client, db, user = ctx
    monkeypatch.setattr(payout_mod.settings, "PAYOUT_INTERVAL_SECONDS", 600)
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 200, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requires_manual_approval"] is False
    assert "10 min" in body["estimated_completion"]


def test_withdraw_above_auto_approve_promises_no_eta(ctx, monkeypatch):
    """A withdrawal needing a human must not be given a countdown.

    Nothing drains this case — it is flagged for manual review and no approval
    endpoint exists — so the old fixed "< 1 hour" was telling the provider to
    expect money that never moves.
    """
    client, db, user = ctx
    monkeypatch.setattr(payout_mod.settings, "AUTO_APPROVE_MAX_USDC", 100.0)
    r = client.post(
        "/v1/provider/withdraw",
        json={"amount": 200, "destination_wallet": VALID_WALLET},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requires_manual_approval"] is True
    assert "manual review" in body["estimated_completion"]
    # The promise the old response made must not survive anywhere in this branch.
    assert "hour" not in body["estimated_completion"]
    assert "min" not in body["estimated_completion"]


def test_regenerate_secret_refused_for_non_provider(monkeypatch):
    """Minting a node secret must not be a way around registration.

    The hash this writes is the credential a node authenticates with, so a
    non-provider who could rotate one would hold a working provider credential
    without ever passing through register — the step that records consent and
    carries the stake gate.
    """
    db = FakeSupabase()
    user = db.add_user(is_provider=False, provider_secret_hash=None)
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(payout_mod, "get_supabase", lambda: db)
    client = TestClient(app)
    try:
        r = client.post("/v1/provider/regenerate-secret", json={})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "not_a_provider"
        # No credential was written.
        row = next(x for x in db._table("users").rows if x["id"] == user["id"])
        assert row.get("provider_secret_hash") is None
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 3.1 — Onboard endpoint
# ---------------------------------------------------------------------------
def test_onboard_new_provider(ctx):
    """Onboard a non-provider: registers + returns join command."""
    client, db, user = ctx
    # Ensure user is not yet a provider.
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    row["is_provider"] = False
    row["provider_secret_hash"] = None

    resp = client.post("/v1/provider/onboard", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_id"] == str(user["id"])
    assert body["node_secret"]
    assert body["join_command"].startswith("orvix-node join --provider-id")
    assert body["provider_id"] in body["join_command"]
    assert body["node_secret"] in body["join_command"]
    # User is now a provider.
    row = next(r for r in db._table("users").rows if r["id"] == user["id"])
    assert row["is_provider"] is True
    assert row["provider_secret_hash"]


def test_onboard_existing_provider_rotates_secret(ctx):
    """Onboarding an existing provider rotates the secret."""
    client, db, user = ctx
    # First onboard.
    r1 = client.post("/v1/provider/onboard", json={})
    secret1 = r1.json()["node_secret"]
    # Second onboard — should rotate.
    r2 = client.post("/v1/provider/onboard", json={})
    secret2 = r2.json()["node_secret"]
    assert secret1 != secret2
    assert r2.json()["join_command"]


def test_onboard_with_stake_gate_blocks_below_minimum(monkeypatch):
    monkeypatch.setattr(provider_mod.settings, "REQUIRE_STAKE_FOR_PROVIDER", True)
    monkeypatch.setattr(provider_mod.settings, "PROVIDER_MIN_STAKE_ORVX", 25000)
    db = FakeSupabase()
    user = db.add_user(
        tier="bronze", staked_orvx=1000.0, is_provider=False, provider_secret_hash=None
    )
    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        resp = client.post("/v1/provider/onboard", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "insufficient_stake"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 3.2 — Health dashboard
# ---------------------------------------------------------------------------
class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(s)

    async def close(self):
        pass


def _make_conn(node_id, provider_id, **kw):
    defaults = dict(
        node_id=node_id,
        provider_id=provider_id,
        websocket=FakeWS(),
        model="qwen-2.5-7b",
        gpu_info={"model": "RTX 4090", "vram_total_mb": 24576, "memory_used_mb": 4000, "memory_total_mb": 24576},
        max_concurrent_jobs=4,
        status="ready",
        models_supported=["qwen-2.5-7b"],
        engines=["chat"],
        vram_gb=24.0,
    )
    defaults.update(kw)
    return NodeConnection(**defaults)


def test_health_dashboard_empty(ctx):
    client, db, user = ctx
    resp = client.get("/v1/provider/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_nodes"] == 0
    assert body["online_nodes"] == 0
    assert body["nodes"] == []


def test_health_dashboard_with_nodes(ctx, monkeypatch):
    client, db, user = ctx
    db._table("nodes").insert_row(
        {"id": "n1", "provider_id": user["id"], "status": "ready", "name": "rig-1",
         "gpu_model": "RTX 4090", "vram_mb": 24576, "engines": ["chat"], "vram_gb": 24.0}
    )
    db._table("nodes").insert_row(
        {"id": "n2", "provider_id": user["id"], "status": "offline", "name": "rig-2",
         "gpu_model": "RTX 3090", "vram_mb": 24576, "engines": ["chat"], "vram_gb": 24.0}
    )
    # Simulate n1 connected.
    conn = _make_conn("n1", user["id"])
    node_manager.connected_nodes["n1"] = conn
    try:
        resp = client.get("/v1/provider/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_nodes"] == 2
        assert body["online_nodes"] == 1
        n1 = next(n for n in body["nodes"] if n["node_id"] == "n1")
        assert n1["is_connected"] is True
        assert n1["engines"] == ["chat"]
        n2 = next(n for n in body["nodes"] if n["node_id"] == "n2")
        assert n2["is_connected"] is False
    finally:
        node_manager.connected_nodes.pop("n1", None)


# ---------------------------------------------------------------------------
# 3.3 — Node job history
# ---------------------------------------------------------------------------
def test_node_job_history(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row(
        {"id": "n1", "provider_id": user["id"], "status": "ready"}
    )
    for i in range(3):
        db._table("jobs").insert_row(
            {"node_id": "n1", "model": "qwen-2.5-7b", "status": "completed",
             "provider_earning_usdc": 0.5, "user_id": "dev"}
        )
    resp = client.get("/v1/provider/nodes/n1/history")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_node_job_history_foreign_node_404(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row({"id": "other", "provider_id": "someone-else"})
    resp = client.get("/v1/provider/nodes/other/history")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3.4 — Drain endpoint
# ---------------------------------------------------------------------------
def test_drain_node(ctx, monkeypatch):
    client, db, user = ctx
    db._table("nodes").insert_row(
        {"id": "n1", "provider_id": user["id"], "status": "ready"}
    )
    conn = _make_conn("n1", user["id"])
    node_manager.connected_nodes["n1"] = conn
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    try:
        resp = client.post("/v1/provider/nodes/n1/drain")
        assert resp.status_code == 200
        assert resp.json()["status"] == "draining"
        assert conn.status == "draining"
    finally:
        node_manager.connected_nodes.pop("n1", None)


def test_drain_disconnected_node_404(ctx):
    client, db, user = ctx
    db._table("nodes").insert_row(
        {"id": "n1", "provider_id": user["id"], "status": "ready"}
    )
    resp = client.post("/v1/provider/nodes/n1/drain")
    assert resp.status_code == 404
