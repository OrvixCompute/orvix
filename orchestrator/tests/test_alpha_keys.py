"""Tests for alpha-partner API keys (orvx_alpha_ prefix + admin provisioning)."""

import pytest
from fastapi.testclient import TestClient

from app.database import get_supabase
from app.dependencies import (
    ALPHA_API_KEY_RE,
    API_KEY_RE,
    _is_api_key,
    _valid_api_key_shape,
)
from app.main import app
from app.services.api_key_service import (
    ALPHA_KEY_PREFIX,
    ApiKeyService,
    generate_alpha_key,
    generate_key,
    hash_key,
)
from tests.fakes import FakeSupabase


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def user(db):
    return db.add_user()


def test_generate_alpha_key_shape():
    key = generate_alpha_key()
    assert key.startswith("orvx_alpha_")
    assert len(key) == len("orvx_alpha_") + 32
    assert ALPHA_API_KEY_RE.match(key)
    assert not API_KEY_RE.match(key)


def test_alpha_key_created_with_kind(db, user):
    svc = ApiKeyService(db)
    created = svc.create(user["id"], "opencovenant", alpha=True)
    assert created["key"].startswith("orvx_alpha_")
    assert created["kind"] == "alpha"
    stored = db._table("api_keys").rows[0]
    assert stored["kind"] == "alpha"
    assert stored["key_hash"] == hash_key(created["key"])
    assert "key" not in stored


def test_standard_key_unchanged(db, user):
    svc = ApiKeyService(db)
    created = svc.create(user["id"], "app")
    assert created["key"].startswith("orvx_sk_")
    assert created["kind"] == "standard"


def test_shape_helpers():
    assert _is_api_key(generate_key())
    assert _is_api_key(generate_alpha_key())
    assert not _is_api_key("some-jwt-token")
    assert _valid_api_key_shape(generate_key())
    assert _valid_api_key_shape(generate_alpha_key())
    assert not _valid_api_key_shape("orvx_alpha_short")


# --- admin provisioning ------------------------------------------------------


@pytest.fixture
def admin_ctx(monkeypatch):
    db = FakeSupabase()
    user = db.add_user()
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    import app.config as config_mod

    monkeypatch.setattr(config_mod.settings, "ADMIN_API_KEY", "test-admin-key")
    app.dependency_overrides[get_supabase] = lambda: db
    yield db, user
    app.dependency_overrides.clear()


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key"}


def test_admin_issues_alpha_key(admin_ctx):
    db, user = admin_ctx
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/alpha/keys",
        json={"user_id": user["id"], "name": "opencovenant"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "alpha"
    assert body["key"].startswith(ALPHA_KEY_PREFIX)
    assert body["prefix"] == body["key"][:12]
    # The plaintext is stored only as a hash.
    stored = db._table("api_keys").rows[0]
    assert stored["key_hash"] == hash_key(body["key"])


def test_admin_alpha_key_requires_existing_user(admin_ctx):
    db, user = admin_ctx
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/alpha/keys",
        json={"user_id": "does-not-exist", "name": "x"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_admin_alpha_key_requires_admin_key(admin_ctx):
    db, user = admin_ctx
    client = TestClient(app)
    resp = client.post("/v1/admin/alpha/keys", json={"user_id": user["id"]})
    assert resp.status_code == 401
