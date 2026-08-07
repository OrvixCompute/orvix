"""Wallet-auth tests: challenge persistence, single use, and real ed25519 signatures.

These cover the two production failures migration 017 fixes — challenges dying
with the process, and a second /challenge silently invalidating the first.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair

from app.database import get_supabase
from app.exceptions import UnauthorizedError, ValidationError
from app.main import app
from app.services.auth_service import AuthService, auth_service
from tests.fakes import FakeSupabase


@pytest.fixture
def ctx():
    db = FakeSupabase()
    app.dependency_overrides[get_supabase] = lambda: db
    client = TestClient(app)
    yield client, db
    app.dependency_overrides.clear()


def _sign(kp: Keypair, message: str) -> str:
    """Base58 ed25519 signature, the same shape a wallet returns."""
    return str(kp.sign_message(message.encode("utf-8")))


def _challenge(db, wallet: str) -> str:
    return auth_service.create_challenge(db, wallet)["message"]


# --- happy path ------------------------------------------------------------
def test_challenge_is_persisted_and_verifies(ctx):
    _client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())

    message = _challenge(db, wallet)
    assert len(db._table("auth_challenges").rows) == 1

    auth_service.verify_signature(db, wallet, message, _sign(kp, message))
    # Single use: the row is consumed.
    assert db._table("auth_challenges").rows == []


def test_challenge_survives_a_restart(ctx):
    """A fresh service instance must still accept a challenge issued earlier.

    This is the production failure: the nonce lived in a dict on the service, so
    a restart between /challenge and /verify rejected the user with an opaque
    401 three seconds after issuing the challenge.
    """
    _client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())

    message = AuthService().create_challenge(db, wallet)["message"]
    # A different instance stands in for the process that came back up.
    AuthService().verify_signature(db, wallet, message, _sign(kp, message))


def test_older_challenge_still_valid_after_a_newer_one(ctx):
    """Issuing a second challenge must not invalidate the first.

    The old store was keyed by wallet and held one entry, so a client that asked
    for a challenge twice — or re-rendered — broke the signature the user had
    already approved in their wallet.
    """
    _client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())

    first = _challenge(db, wallet)
    second = _challenge(db, wallet)
    assert first != second
    assert len(db._table("auth_challenges").rows) == 2

    auth_service.verify_signature(db, wallet, first, _sign(kp, first))
    # Consuming the first leaves the second usable.
    assert len(db._table("auth_challenges").rows) == 1
    auth_service.verify_signature(db, wallet, second, _sign(kp, second))


# --- rejection paths -------------------------------------------------------
def test_nonce_cannot_be_replayed(ctx):
    _client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())
    message = _challenge(db, wallet)
    signature = _sign(kp, message)

    auth_service.verify_signature(db, wallet, message, signature)
    with pytest.raises(UnauthorizedError, match="already-used"):
        auth_service.verify_signature(db, wallet, message, signature)


def test_another_wallet_cannot_claim_a_nonce(ctx):
    """The nonce is looked up first, so it must still be bound to its wallet."""
    _client, db = ctx
    issued_to = Keypair()
    attacker = Keypair()
    message = _challenge(db, str(issued_to.pubkey()))

    with pytest.raises(UnauthorizedError, match="already-used"):
        auth_service.verify_signature(
            db, str(attacker.pubkey()), message, _sign(attacker, message)
        )


def test_expired_challenge_is_rejected_and_removed(ctx):
    _client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())
    message = _challenge(db, wallet)

    db._table("auth_challenges").rows[0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()

    with pytest.raises(UnauthorizedError, match="expired"):
        auth_service.verify_signature(db, wallet, message, _sign(kp, message))
    assert db._table("auth_challenges").rows == []


def test_wrong_signature_is_rejected(ctx):
    _client, db = ctx
    kp = Keypair()
    other = Keypair()
    wallet = str(kp.pubkey())
    message = _challenge(db, wallet)

    with pytest.raises(UnauthorizedError, match="Signature verification failed"):
        auth_service.verify_signature(db, wallet, message, _sign(other, message))


def test_message_without_the_orvix_prefix_is_rejected(ctx):
    _client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())
    _challenge(db, wallet)

    with pytest.raises(ValidationError, match="prefix"):
        auth_service.verify_signature(db, wallet, "Approve this transfer", "sig")


def test_expired_rows_are_swept_when_a_challenge_is_issued(ctx):
    _client, db = ctx
    db._table("auth_challenges").insert_row(
        {
            "nonce": "stale",
            "wallet": "whoever",
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
    )

    _challenge(db, str(Keypair().pubkey()))

    nonces = {r["nonce"] for r in db._table("auth_challenges").rows}
    assert "stale" not in nonces


# --- through the HTTP layer ------------------------------------------------
def test_challenge_endpoint_round_trips_to_a_jwt(ctx):
    client, db = ctx
    kp = Keypair()
    wallet = str(kp.pubkey())

    resp = client.get(f"/v1/auth/challenge?wallet={wallet}")
    assert resp.status_code == 200
    message = resp.json()["message"]

    resp = client.post(
        "/v1/auth/verify",
        json={"wallet": wallet, "message": message, "signature": _sign(kp, message)},
    )
    assert resp.status_code == 200
    assert resp.json()["token"]
    assert db._table("auth_challenges").rows == []
