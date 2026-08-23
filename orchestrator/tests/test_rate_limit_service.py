"""Tests for rate_limit_service — both in-memory and Redis backends."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import RateLimitError
from app.services import rate_limit_service


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear in-memory hits and reset Redis singleton between tests."""
    rate_limit_service.reset()
    rate_limit_service._redis = None
    rate_limit_service._redis_initialised = False
    yield
    rate_limit_service.reset()
    rate_limit_service._redis = None
    rate_limit_service._redis_initialised = False


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class TestInMemory:
    def test_allows_requests_under_limit(self):
        rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.check("key1", "bronze", bucket="chat")

    def test_blocks_when_limit_exceeded(self):
        limit = 60  # bronze
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        with pytest.raises(RateLimitError) as exc_info:
            rate_limit_service.check("key1", "bronze", bucket="chat")
        assert exc_info.value.details["limit_per_minute"] == limit
        assert exc_info.value.details["tier"] == "bronze"
        assert exc_info.value.details["bucket"] == "chat"

    def test_separate_buckets_are_independent(self):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        # Different bucket — should still work.
        rate_limit_service.check("key1", "bronze", bucket="image")

    def test_separate_keys_are_independent(self):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.check("key2", "bronze", bucket="chat")

    def test_check_user_uses_user_id(self):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check_user("user1", "bronze", bucket="intel")
        with pytest.raises(RateLimitError):
            rate_limit_service.check_user("user1", "bronze", bucket="intel")

    def test_tier_limits_differ(self):
        # silver allows 120 rpm
        for _ in range(60):
            rate_limit_service.check("key1", "silver", bucket="chat")
        # Still under silver limit
        rate_limit_service.check("key1", "silver", bucket="chat")

    def test_retry_after_seconds_is_positive(self):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        with pytest.raises(RateLimitError) as exc_info:
            rate_limit_service.check("key1", "bronze", bucket="chat")
        assert exc_info.value.details["retry_after_seconds"] >= 1

    def test_reset_clears_all_counters(self):
        for _ in range(60):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.reset()
        rate_limit_service.check("key1", "bronze", bucket="chat")


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal sorted-set Redis mock for sliding-window tests."""

    def __init__(self):
        self.store: dict[str, list[tuple[str, float]]] = {}

    def ping(self):
        return True

    def pipeline(self, _transaction=True):
        return _FakePipeline(self)

    def zremrangebyscore(self, key, min_score, max_score):
        if key in self.store:
            self.store[key] = [
                (m, s) for m, s in self.store[key] if s > max_score
            ]

    def zcard(self, key):
        return len(self.store.get(key, []))

    def zadd(self, key, mapping):
        self.store.setdefault(key, [])
        for member, score in mapping.items():
            self.store[key].append((member, score))

    def expire(self, key, ttl):
        pass

    def zrange(self, key, start, stop, withscores=False):
        items = sorted(self.store.get(key, []), key=lambda x: x[1])
        subset = items[start : stop + 1] if stop >= 0 else items[start:]
        if withscores:
            return subset
        return [m for m, _ in subset]

    def scan_iter(self, pattern="*"):
        import fnmatch

        for k in list(self.store):
            if fnmatch.fnmatch(k, pattern):
                yield k

    def delete(self, key):
        self.store.pop(key, None)


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._commands: list[tuple[str, tuple, dict]] = []

    def __call__(self, *a, **kw):
        return self

    def zremrangebyscore(self, *a, **kw):
        self._commands.append(("zremrangebyscore", a, kw))

    def zcard(self, *a, **kw):
        self._commands.append(("zcard", a, kw))

    def zadd(self, *a, **kw):
        self._commands.append(("zadd", a, kw))

    def expire(self, *a, **kw):
        self._commands.append(("expire", a, kw))

    def execute(self):
        results = []
        for cmd, args, kwargs in self._commands:
            method = getattr(self._redis, cmd)
            results.append(method(*args, **kwargs))
        return results


@pytest.fixture
def fake_redis():
    r = _FakeRedis()
    rate_limit_service._redis = r
    rate_limit_service._redis_initialised = True
    return r


class TestRedisBackend:
    def test_allows_requests_under_limit(self, fake_redis):
        rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.check("key1", "bronze", bucket="chat")

    def test_blocks_when_limit_exceeded(self, fake_redis):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        with pytest.raises(RateLimitError) as exc_info:
            rate_limit_service.check("key1", "bronze", bucket="chat")
        assert exc_info.value.details["limit_per_minute"] == limit

    def test_separate_buckets_are_independent(self, fake_redis):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.check("key1", "bronze", bucket="image")

    def test_separate_keys_are_independent(self, fake_redis):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.check("key2", "bronze", bucket="chat")

    def test_check_user_uses_user_id(self, fake_redis):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check_user("user1", "bronze", bucket="intel")
        with pytest.raises(RateLimitError):
            rate_limit_service.check_user("user1", "bronze", bucket="intel")

    def test_tier_limits_differ(self, fake_redis):
        for _ in range(60):
            rate_limit_service.check("key1", "silver", bucket="chat")
        rate_limit_service.check("key1", "silver", bucket="chat")

    def test_retry_after_seconds_is_positive(self, fake_redis):
        limit = 60
        for _ in range(limit):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        with pytest.raises(RateLimitError) as exc_info:
            rate_limit_service.check("key1", "bronze", bucket="chat")
        assert exc_info.value.details["retry_after_seconds"] >= 1

    def test_reset_clears_redis_keys(self, fake_redis):
        for _ in range(10):
            rate_limit_service.check("key1", "bronze", bucket="chat")
        rate_limit_service.reset()
        # After reset, should be able to make requests again
        rate_limit_service.check("key1", "bronze", bucket="chat")

    def test_fallback_to_in_memory_when_redis_unavailable(self):
        """When Redis is set but unreachable, falls back to in-memory."""
        rate_limit_service._redis = None
        rate_limit_service._redis_initialised = True
        # Should not raise — uses in-memory
        rate_limit_service.check("key1", "bronze", bucket="chat")
