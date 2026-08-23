"""Per-API-key request rate limiting, shared by the chat and image routes.

Supports two backends:
- **Redis** (when ``REDIS_URL`` is set): sliding-window counter stored in a
  sorted set, shared across all workers.
- **In-memory** (default): ``defaultdict(deque)`` keyed by ``(key, bucket)``.
  Limits reset on restart and do not hold across processes — fine for a single
  uvicorn worker, wrong the moment there are two.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings
from app.exceptions import RateLimitError
from app.services import tier_service

RATE_WINDOW = 60.0  # seconds

# ---------------------------------------------------------------------------
# In-memory backend (original behaviour)
# ---------------------------------------------------------------------------
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def _in_memory_check(key: str, limit: int, bucket: str, tier: str) -> None:
    now = time.monotonic()
    q = _hits[(key, bucket)]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= limit:
        raise RateLimitError(
            f"Rate limit exceeded: max {limit} {bucket} requests per minute on the {tier} tier",
            details={
                "retry_after_seconds": int(RATE_WINDOW - (now - q[0])) + 1,
                "tier": tier,
                "bucket": bucket,
                "limit_per_minute": limit,
                "upgrade_url": settings.UPGRADE_URL,
            },
        )
    q.append(now)


# ---------------------------------------------------------------------------
# Redis backend (sorted-set sliding window)
# ---------------------------------------------------------------------------
_redis = None
_redis_initialised = False


def _get_redis():
    """Lazy-initialise the Redis client. Returns None when REDIS_URL is empty."""
    global _redis, _redis_initialised
    if _redis_initialised:
        return _redis
    _redis_initialised = True
    url = settings.REDIS_URL
    if not url:
        return None
    try:
        import redis

        _redis = redis.from_url(url, decode_responses=True)
        _redis.ping()
    except Exception:
        _redis = None
    return _redis


def _redis_check(key: str, limit: int, bucket: str, tier: str) -> None:
    r = _get_redis()
    if r is None:
        return _in_memory_check(key, limit, bucket, tier)

    redis_key = f"rl:{key}:{bucket}"
    now = time.time()
    window_start = now - RATE_WINDOW

    pipe = r.pipeline(True)
    pipe.zremrangebyscore(redis_key, "-inf", window_start)
    pipe.zcard(redis_key)
    pipe.zadd(redis_key, {str(now): now})
    pipe.expire(redis_key, int(RATE_WINDOW) + 1)
    _, count, _, _ = pipe.execute()

    if count >= limit:
        oldest = r.zrange(redis_key, 0, 0, withscores=True)
        retry_after = int(RATE_WINDOW - (now - oldest[0][1])) + 1 if oldest else int(RATE_WINDOW)
        raise RateLimitError(
            f"Rate limit exceeded: max {limit} {bucket} requests per minute on the {tier} tier",
            details={
                "retry_after_seconds": retry_after,
                "tier": tier,
                "bucket": bucket,
                "limit_per_minute": limit,
                "upgrade_url": settings.UPGRADE_URL,
            },
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reset() -> None:
    """Drop all counters. For tests; never called in request handling."""
    _hits.clear()
    r = _get_redis()
    if r is not None:
        for k in r.scan_iter("rl:*"):
            r.delete(k)


def check(api_key_id: str, tier: str, *, bucket: str = "chat") -> None:
    """Enforce the caller's per-minute ceiling for ``bucket``, set by their tier."""
    limit = tier_service.rate_limit_for_tier(tier)
    _check_with_limit(api_key_id, tier, limit, bucket=bucket)


def check_user(user_id: str, tier: str, *, bucket: str) -> None:
    """Rate-limit a user id (JWT-authenticated routes) under a named bucket."""
    limit = tier_service.rate_limit_for_tier(tier)
    _check_with_limit(user_id, tier, limit, bucket=bucket)


def _check_with_limit(key: str, tier: str, limit: int, *, bucket: str) -> None:
    """Shared enforcement for both keyed forms (API key id or user id)."""
    if _get_redis() is not None:
        _redis_check(key, limit, bucket, tier)
    else:
        _in_memory_check(key, limit, bucket, tier)
