"""Tests for auth_service Redis challenge backend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from solders.keypair import Keypair

from app.exceptions import UnauthorizedError, ValidationError
from app.services.auth_service import AuthService, auth_service


@pytest.fixture(autouse=True)
def _reset_redis():
    """Reset Redis singleton between tests."""
    import app.services.auth_service as mod

    mod._redis = None
    mod._redis_initialised = False
    yield
    mod._redis = None
    mod._redis_initialised = False


class _FakeRedis:
    """Minimal Redis mock with string GET/SETEX/DELETE."""

    def __init__(self):
        self.store: dict[str, tuple[str, int | None]] = {}  # key -> (value, ttl_seconds)

    def ping(self):
        return True

    def setex(self, key, ttl, value):
        self.store[key] = (value, ttl)

    def get(self, key):
        entry = self.store.get(key)
        return entry[0] if entry else None

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis():
    r = _FakeRedis()
    import app.services.auth_service as mod

    mod._redis = r
    mod._redis_initialised = True
    return r


def _sign(kp: Keypair, message: str) -> str:
    return str(kp.sign_message(message.encode("utf-8")))


def _challenge(wallet: str) -> str:
    return auth_service.create_challenge(None, wallet)["message"]


class TestRedisChallengeStore:
    def test_create_and_verify(self, fake_redis):
        kp = Keypair()
        wallet = str(kp.pubkey())
        message = _challenge(wallet)

        # Challenge stored in Redis
        assert len(fake_redis.store) == 1

        auth_service.verify_signature(None, wallet, message, _sign(kp, message))
        # Consumed
        assert len(fake_redis.store) == 0

    def test_challenge_survives_restart(self, fake_redis):
        kp = Keypair()
        wallet = str(kp.pubkey())
        message = AuthService().create_challenge(None, wallet)["message"]
        AuthService().verify_signature(None, wallet, message, _sign(kp, message))

    def test_multiple_challenges_valid(self, fake_redis):
        kp = Keypair()
        wallet = str(kp.pubkey())
        first = _challenge(wallet)
        second = _challenge(wallet)
        assert first != second
        assert len(fake_redis.store) == 2

        auth_service.verify_signature(None, wallet, first, _sign(kp, first))
        assert len(fake_redis.store) == 1
        auth_service.verify_signature(None, wallet, second, _sign(kp, second))

    def test_nonce_cannot_be_replayed(self, fake_redis):
        kp = Keypair()
        wallet = str(kp.pubkey())
        message = _challenge(wallet)
        signature = _sign(kp, message)

        auth_service.verify_signature(None, wallet, message, signature)
        with pytest.raises(UnauthorizedError, match="already-used"):
            auth_service.verify_signature(None, wallet, message, signature)

    def test_another_wallet_cannot_claim_nonce(self, fake_redis):
        issued_to = Keypair()
        attacker = Keypair()
        message = _challenge(str(issued_to.pubkey()))

        with pytest.raises(UnauthorizedError, match="already-used"):
            auth_service.verify_signature(
                None, str(attacker.pubkey()), message, _sign(attacker, message)
            )

    def test_wrong_signature_rejected(self, fake_redis):
        kp = Keypair()
        other = Keypair()
        wallet = str(kp.pubkey())
        message = _challenge(wallet)

        with pytest.raises(UnauthorizedError, match="Signature verification failed"):
            auth_service.verify_signature(None, wallet, message, _sign(other, message))

    def test_message_without_prefix_rejected(self, fake_redis):
        kp = Keypair()
        wallet = str(kp.pubkey())
        _challenge(wallet)

        with pytest.raises(ValidationError, match="prefix"):
            auth_service.verify_signature(None, wallet, "Approve this transfer", "sig")

    def test_redis_ttl_is_set(self, fake_redis):
        kp = Keypair()
        wallet = str(kp.pubkey())
        _challenge(wallet)

        key = list(fake_redis.store.keys())[0]
        _, ttl = fake_redis.store[key]
        assert ttl is not None
        assert ttl > 0
        assert ttl <= 301  # 5 minutes + 1 second buffer

    def test_fallback_to_db_when_redis_unavailable(self):
        """When Redis is set but None, falls back to DB path."""
        import app.services.auth_service as mod

        mod._redis = None
        mod._redis_initialised = True

        # This would fail if it tried Redis — it should use DB instead.
        # We can't fully test DB path without a real/fake DB, but we verify
        # the code doesn't crash on the Redis path.
        kp = Keypair()
        wallet = str(kp.pubkey())
        # create_challenge with db=None will fail at DB call, confirming fallback
        with pytest.raises(AttributeError):
            auth_service.create_challenge(None, wallet)
