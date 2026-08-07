"""Per-API-key request rate limiting, shared by the chat and image routes.

Lived inside the chat route until the image route needed it too. Importing a
private helper across routes would have worked and been quietly wrong: the two
paths would drift, and the counters would sit in whichever module happened to be
imported first.

TODO: replace the in-memory window with Redis. Limits currently reset on restart
and do not hold across processes, which is fine for a single uvicorn worker and
wrong the moment there are two.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings
from app.exceptions import RateLimitError
from app.services import tier_service

RATE_WINDOW = 60.0  # seconds

# Keyed by API key id, then by bucket, so chat and image ceilings are counted
# separately. A burst of images must not spend the caller's chat allowance —
# they are different resources with very different costs per request.
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def reset() -> None:
    """Drop all counters. For tests; never called in request handling."""
    _hits.clear()


def check(api_key_id: str, tier: str, *, bucket: str = "chat") -> None:
    """Enforce the caller's per-minute ceiling for `bucket`, set by their tier.

    Raises RateLimitError with the tier, the limit and where to upgrade, so a
    429 tells the caller what they have rather than only that they exceeded it.
    """
    limit = tier_service.rate_limit_for_tier(tier)
    now = time.monotonic()
    q = _hits[(api_key_id, bucket)]
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
