"""Tests for signed inference receipts (receipt_signer.py + /v1/verify)."""

import base64

import pytest
from fastapi.testclient import TestClient

import app.services.receipt_signer as rs
from app.main import app
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture(autouse=True)
def _reset_signer_state():
    """Reset the module-level signing cache before and after every test."""
    rs._loaded = False
    yield
    rs._loaded = False


@pytest.fixture
def signing_key(monkeypatch):
    """Enable signing with a fresh random key, isolated per test."""
    seed = base64.b64encode(b"0" * 32).decode()
    monkeypatch.setattr(rs.settings, "RECEIPT_SIGNING_KEY", seed)
    rs._loaded = False  # force reload so the new key takes effect
    yield seed
    rs._loaded = False


def _make_receipt(**overrides):
    base = {
        "request_id": "req-1",
        "node_id": "node-1",
        "provider_id": "prov-1",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "model": "qwen-2.5-7b",
    }
    base.update(overrides)
    return rs.build_receipt(**base)


def test_signing_disabled_by_default(monkeypatch):
    monkeypatch.setattr(rs.settings, "RECEIPT_SIGNING_KEY", "")
    rs._loaded = False
    assert rs.signing_enabled() is False
    assert rs.public_key_b64() is None
    assert _make_receipt() is None


def test_build_receipt_signs_canonical_payload(signing_key):
    receipt = _make_receipt()
    assert receipt is not None
    assert receipt["algorithm"] == "ed25519"
    assert receipt["public_key"] == rs.public_key_b64()
    # Signature verifies against the canonical payload.
    assert rs.verify_receipt(receipt) is True
    # The payload carries the exact usage that gets billed.
    assert receipt["payload"]["prompt_tokens"] == 10
    assert receipt["payload"]["completion_tokens"] == 5
    assert receipt["payload"]["node_id"] == "node-1"


def test_tampered_payload_fails_verification(signing_key):
    receipt = _make_receipt()
    receipt["payload"]["completion_tokens"] = 999
    assert rs.verify_receipt(receipt) is False


def test_wrong_public_key_fails_verification(signing_key):
    receipt = _make_receipt()
    other = Ed25519PrivateKey.generate()
    receipt["public_key"] = base64.b64encode(
        other.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    assert rs.verify_receipt(receipt) is False


def test_malformed_receipt_fails_verification():
    assert rs.verify_receipt({}) is False
    assert rs.verify_receipt({"payload": {}, "signature": "x", "public_key": "y"}) is False


def test_receipt_digest_is_stable(signing_key):
    # The digest covers only the payload fields, and served_at differs per
    # receipt — so pin the payload to prove the digest is deterministic.
    a = _make_receipt()
    b = _make_receipt()
    a["payload"]["served_at"] = "2026-01-01T00:00:00+00:00"
    b["payload"]["served_at"] = "2026-01-01T00:00:00+00:00"
    assert rs.receipt_digest(a) == rs.receipt_digest(b)
    assert len(rs.receipt_digest(a)) == 16


# --- public endpoints --------------------------------------------------------


def test_public_key_endpoint_reports_disabled(monkeypatch):
    monkeypatch.setattr(rs.settings, "RECEIPT_SIGNING_KEY", "")
    rs._loaded = False
    client = TestClient(app)
    resp = client.get("/v1/verify/public-key")
    assert resp.status_code == 200
    assert resp.json() == {"algorithm": "ed25519", "public_key": None}


def test_public_key_endpoint_reports_enabled(signing_key):
    client = TestClient(app)
    resp = client.get("/v1/verify/public-key")
    assert resp.status_code == 200
    assert resp.json()["public_key"] == rs.public_key_b64()


def test_verify_receipt_endpoint(signing_key):
    receipt = _make_receipt()
    client = TestClient(app)
    ok = client.post("/v1/verify/receipt", json=receipt)
    assert ok.status_code == 200
    assert ok.json() == {"valid": True}
    receipt["payload"]["prompt_tokens"] = 999
    bad = client.post("/v1/verify/receipt", json=receipt)
    assert bad.status_code == 200
    assert bad.json() == {"valid": False}
