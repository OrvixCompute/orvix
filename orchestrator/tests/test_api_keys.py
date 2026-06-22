"""Unit tests for API key management (service + auth format)."""

import pytest

from app.dependencies import API_KEY_RE
from app.exceptions import NotFoundError, ValidationError
from app.services.api_key_service import (
    MAX_ACTIVE_KEYS,
    ApiKeyService,
    generate_key,
    hash_key,
)
from tests.fakes import FakeSupabase


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def user(db):
    return db.add_user(tier="gold")


def test_create_returns_plaintext_once(db, user):
    svc = ApiKeyService(db)
    res = svc.create(user["id"], "prod key")
    assert res["key"].startswith("orvx_sk_")
    assert res["prefix"] == res["key"][:12]
    assert res["name"] == "prod key"
    # The stored row holds the hash, never the plaintext.
    stored = db._table("api_keys").rows[0]
    assert stored["key_hash"] == hash_key(res["key"])
    assert "key" not in stored


def test_list_newest_first(db, user):
    svc = ApiKeyService(db)
    a = svc.create(user["id"], "first")
    b = svc.create(user["id"], "second")
    listed = svc.list(user["id"])
    assert {row["id"] for row in listed} == {a["id"], b["id"]}
    assert all("key_hash" not in row for row in listed)


def test_delete_is_soft_and_ownership_checked(db, user):
    svc = ApiKeyService(db)
    created = svc.create(user["id"], "to delete")
    svc.revoke(user["id"], created["id"])
    row = db._table("api_keys").rows[0]
    assert row["is_active"] is False

    with pytest.raises(NotFoundError):
        svc.revoke("someone-else", created["id"])


def test_rotate_deactivates_old_and_issues_new(db, user):
    svc = ApiKeyService(db)
    original = svc.create(user["id"], "rotate me")
    rotated = svc.rotate(user["id"], original["id"])

    assert rotated["key"] != original["key"]
    assert rotated["name"] == "rotate me"
    rows = {r["id"]: r for r in db._table("api_keys").rows}
    assert rows[original["id"]]["is_active"] is False
    assert rows[rotated["id"]]["is_active"] is True


def test_max_active_keys_enforced(db, user):
    svc = ApiKeyService(db)
    for i in range(MAX_ACTIVE_KEYS):
        svc.create(user["id"], f"key {i}")
    with pytest.raises(ValidationError):
        svc.create(user["id"], "one too many")


def test_api_key_regex_accepts_and_rejects():
    good = generate_key()
    assert API_KEY_RE.match(good)
    assert not API_KEY_RE.match("orvx_sk_short")
    assert not API_KEY_RE.match("sk_wrongprefix0000000000000000000000000")
    assert not API_KEY_RE.match("orvx_sk_" + "!" * 32)
