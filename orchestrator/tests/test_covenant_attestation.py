"""OpenCovenant attestation wiring: node registration + admin check endpoint.

These tests pin the fail-soft contract: attestation is opt-in, a failed check
never blocks registration, and the verdict lands on the nodes row only when
attestation is enabled and the provider wallet is configured.
"""

import pytest
from fastapi.testclient import TestClient

import app.services.node_manager as nm
from app.main import app
from app.models.protocol import GPUInfo, RegisterMessage
from app.services.covenant_service import CovenantCheckResult, CovenantReputation
from tests.fakes import FakeSupabase

REAL_WALLET = "7G73PLhKvAPBGTzG5ESAE4coE7QrVeTTKfhTxQZbyGgC"


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(s)

    async def close(self):
        pass


def _reg(provider_id):
    return RegisterMessage(
        provider_id=provider_id,
        node_secret="secret",
        version="0.1.0",
        gpu_info=GPUInfo(model="RTX 4090", vram_total_mb=24576),
        models_supported=["qwen-2.5-7b"],
        max_concurrent_jobs=2,
    )


def _enable(monkeypatch, **kw):
    """Turn attestation on with a configured wallet, resetting the other flags."""
    monkeypatch.setattr(nm.settings, "COVENANT_ENABLE_ATTESTATION", True)
    monkeypatch.setattr(nm.settings, "COVENANT_PROVIDER_WALLET_ADDRESS", REAL_WALLET)
    monkeypatch.setattr(nm.settings, "COVENANT_MIN_REPUTATION", 0)
    for k, v in kw.items():
        monkeypatch.setattr(nm.settings, k, v)


def _fake_ok(score=250, tier="silver"):
    async def check_reputation(self, wallet):
        return CovenantCheckResult(
            ok=True,
            tool="covenant_reputation",
            reputation=CovenantReputation(
                wallet=wallet,
                score=score,
                tier=tier,
                settled_jobs=3,
                distinct_counterparties=2,
                volume_micro_usdc=1_000_000,
            ),
        )

    return check_reputation


async def test_attestation_off_by_default_skips_check(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    # Flag + wallet off by default; the fake would raise if it were called.
    async def boom(self, wallet):
        raise AssertionError("covenant check must not run when disabled")

    monkeypatch.setattr(nm, "get_covenant_service", lambda: type("S", (), {"check_reputation": boom})())
    mgr = nm.NodeManager()

    conn = await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert conn.attestation is None
    assert db._table("nodes").rows[0].get("covenant_attestation") is None


async def test_attestation_enabled_stores_verdict_on_row(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    _enable(monkeypatch)

    fake_svc = type("S", (), {"check_reputation": _fake_ok(score=250)})
    monkeypatch.setattr(nm, "get_covenant_service", lambda: fake_svc())
    mgr = nm.NodeManager()

    conn = await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert conn.attestation is not None
    assert conn.attestation["attested"] is True
    assert conn.attestation["score"] == 250
    row = db._table("nodes").rows[0]
    assert row["covenant_attestation"]["attested"] is True
    assert row["covenant_attestation"]["score"] == 250


async def test_attestation_uses_provider_wallet_over_env_var(monkeypatch):
    """The provider's own wallet_address from the users row takes priority
    over the COVENANT_PROVIDER_WALLET_ADDRESS env var."""
    db = FakeSupabase()
    provider_wallet = "ProviderWallet11111111111111111111111111111"
    user = db.add_user(wallet_address=provider_wallet)
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    _enable(monkeypatch, COVENANT_PROVIDER_WALLET_ADDRESS="EnvVarWallet22222222222222222222222222222")

    captured = {}

    async def capture_wallet(self, wallet):
        captured["wallet"] = wallet
        return await _fake_ok(score=300)(self, wallet)

    fake_svc = type("S", (), {"check_reputation": capture_wallet})
    monkeypatch.setattr(nm, "get_covenant_service", lambda: fake_svc())
    mgr = nm.NodeManager()

    conn = await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert captured["wallet"] == provider_wallet
    assert conn.attestation is not None
    assert conn.attestation["wallet"] == provider_wallet


async def test_attestation_falls_back_to_env_var_when_user_wallet_empty(monkeypatch):
    """When the provider's wallet_address is empty, the env var is used."""
    db = FakeSupabase()
    env_wallet = "EnvVarWallet33333333333333333333333333333"
    user = db.add_user(wallet_address="")
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    _enable(monkeypatch, COVENANT_PROVIDER_WALLET_ADDRESS=env_wallet)

    captured = {}

    async def capture_wallet(self, wallet):
        captured["wallet"] = wallet
        return await _fake_ok(score=300)(self, wallet)

    fake_svc = type("S", (), {"check_reputation": capture_wallet})
    monkeypatch.setattr(nm, "get_covenant_service", lambda: fake_svc())
    mgr = nm.NodeManager()

    await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert captured["wallet"] == env_wallet


async def test_attestation_below_min_score_records_not_attested(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    _enable(monkeypatch, COVENANT_MIN_REPUTATION=500)

    fake_svc = type("S", (), {"check_reputation": _fake_ok(score=250)})
    monkeypatch.setattr(nm, "get_covenant_service", lambda: fake_svc())
    mgr = nm.NodeManager()

    conn = await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert conn.attestation["attested"] is False
    assert conn.attestation["reason"] == "score below threshold"
    # Fail-soft: registration still succeeded.
    assert conn.node_id in mgr.connected_nodes


async def test_attestation_failure_never_blocks_registration(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    _enable(monkeypatch)

    async def boom(self, wallet):
        return CovenantCheckResult(ok=False, tool="covenant_reputation", error="MCP transport error")

    fake_svc = type("S", (), {"check_reputation": boom})
    monkeypatch.setattr(nm, "get_covenant_service", lambda: fake_svc())
    mgr = nm.NodeManager()

    conn = await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert conn.attestation is not None
    assert conn.attestation["attested"] is False
    assert "transport" in (conn.attestation["reason"] or "")
    assert conn.node_id in mgr.connected_nodes


async def test_attestation_disabled_with_wallet_set_skips_check(monkeypatch):
    # Even with a wallet configured, the flag must gate the whole path.
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    monkeypatch.setattr(nm.settings, "COVENANT_ENABLE_ATTESTATION", False)
    monkeypatch.setattr(nm.settings, "COVENANT_PROVIDER_WALLET_ADDRESS", REAL_WALLET)

    async def boom(self, wallet):
        raise AssertionError("covenant check must not run when flag is off")

    monkeypatch.setattr(nm, "get_covenant_service", lambda: type("S", (), {"check_reputation": boom})())
    mgr = nm.NodeManager()

    conn = await mgr.register_node(FakeWS(), _reg(user["id"]))
    assert conn.attestation is None
    assert db._table("nodes").rows[0].get("covenant_attestation") is None


# --- admin route ------------------------------------------------------------

ADMIN_KEY = "test-admin-key"


@pytest.fixture
def admin_ctx(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setattr(nm, "get_supabase", lambda: db)
    nm.node_manager.connected_nodes.clear()
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    # The settings singleton is loaded at import time; force a refresh.
    import app.config as config_mod

    monkeypatch.setattr(config_mod.settings, "ADMIN_API_KEY", ADMIN_KEY)
    yield db, user
    nm.node_manager.connected_nodes.clear()


def _admin_headers():
    return {"X-Admin-Key": ADMIN_KEY}


def test_admin_covenant_reputation_live_check(monkeypatch, admin_ctx):
    import app.routes.admin as admin

    async def fake_check(self, wallet):
        return CovenantCheckResult(
            ok=True,
            tool="covenant_reputation",
            reputation=CovenantReputation(
                wallet=wallet, score=812, tier="diamond",
                settled_jobs=42, distinct_counterparties=7,
                volume_micro_usdc=55_000_000,
                source_fee_payer="2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4",
            ),
        )

    monkeypatch.setattr(admin, "get_covenant_service", lambda: type("S", (), {"check_reputation": fake_check})())
    client = TestClient(app)
    resp = client.get("/v1/admin/covenant/reputation", params={"wallet": REAL_WALLET}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["score"] == 812
    assert body["tier"] == "diamond"
    assert body["settled_jobs"] == 42


def test_admin_covenant_reputation_uses_configured_wallet_when_blank(monkeypatch, admin_ctx):
    import app.routes.admin as admin

    captured = {}

    async def fake_check(self, wallet):
        captured["wallet"] = wallet
        return CovenantCheckResult(ok=False, tool="covenant_reputation", error="boom")

    monkeypatch.setattr(admin, "get_covenant_service", lambda: type("S", (), {"check_reputation": fake_check})())
    monkeypatch.setattr(admin.settings, "COVENANT_PROVIDER_WALLET_ADDRESS", REAL_WALLET)
    client = TestClient(app)
    resp = client.get("/v1/admin/covenant/reputation", headers=_admin_headers())
    assert resp.status_code == 200
    assert captured["wallet"] == REAL_WALLET
    assert resp.json()["ok"] is False


def test_admin_covenant_reputation_empty_wallet_returns_error(monkeypatch, admin_ctx):
    import app.routes.admin as admin

    monkeypatch.setattr(admin.settings, "COVENANT_PROVIDER_WALLET_ADDRESS", "")
    client = TestClient(app)
    resp = client.get("/v1/admin/covenant/reputation", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "no wallet" in resp.json()["error"]


def test_admin_feature_flags_include_covenant(monkeypatch, admin_ctx):
    import app.routes.admin as admin

    monkeypatch.setattr(admin.settings, "COVENANT_ENABLE_ATTESTATION", True)
    monkeypatch.setattr(admin.settings, "COVENANT_MIN_REPUTATION", 100)
    client = TestClient(app)
    resp = client.get("/v1/admin/feature-flags", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["covenant_enable_attestation"] is True
    assert body["covenant_min_reputation"] == 100
