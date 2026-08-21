"""Social intelligence for tokens — DexScreener + optional X/Twitter.

DexScreener (free, no API key) provides social links, volume, and trending
status.  Twitter/X API v2 (optional, needs X_BEARER_TOKEN) adds follower
counts, tweet volume, and basic sentiment.

Everything is fail-soft: a missing API key or network error yields null fields
rather than exceptions.  Results are cached via the shared two-tier cache
(in-memory + intel_scans DB table).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx
from supabase import Client

from app.config import settings
from app.logger import logger
from app.services import token_intel


# ---------------------------------------------------------------------------
# Social score weights
# ---------------------------------------------------------------------------
_TRENDING_POINTS = 30
_VOLUME_SPIKE_POINTS = 20
_TWITTER_FOLLOWERS_POINTS = 15
_TWITTER_ACTIVITY_POINTS = 15
_LINK_POINTS = 10  # per link, max 2 links = 20
_MAX_LINK_POINTS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------

async def _fetch_dexscreener(mint: str) -> Optional[dict]:
    """Fetch token data from DexScreener. Returns raw pair data or None."""
    url = f"{settings.DEXSCREENER_API_URL}/tokens/{mint}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DexScreener fetch failed for {}: {}", mint, exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Pick the pair with the highest liquidity.
    best = max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0))
    return best


def _parse_dexscreener(pair: dict) -> dict:
    """Extract social links and metrics from a DexScreener pair object."""
    info = pair.get("info") or {}
    links = info.get("links") or []

    social_links: dict[str, Optional[str]] = {
        "twitter": None,
        "website": None,
        "telegram": None,
        "discord": None,
    }
    for link in links:
        link_type = (link.get("type") or link.get("label") or "").lower()
        url = link.get("url")
        if not url:
            continue
        if "twitter" in link_type or "x.com" in link_type:
            social_links["twitter"] = url
        elif "website" in link_type or "web" in link_type:
            social_links["website"] = url
        elif "telegram" in link_type:
            social_links["telegram"] = url
        elif "discord" in link_type:
            social_links["discord"] = url

    # Also check the top-level website/twitter fields DexScreener sometimes puts
    # outside the links array.
    if not social_links["website"] and info.get("websites"):
        for w in info["websites"]:
            if w.get("url"):
                social_links["website"] = w["url"]
                break
    if not social_links["twitter"] and info.get("socials"):
        for s in info["socials"]:
            if (s.get("type") or "").lower() == "twitter" and s.get("url"):
                social_links["twitter"] = s["url"]
                break

    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}

    return {
        "social_links": social_links,
        "dex_trending": bool(pair.get("boosts", {}).get("active")),
        "dex_volume_24h": volume.get("h24"),
        "dex_price_change_24h": price_change.get("h24"),
    }


# ---------------------------------------------------------------------------
# Twitter/X API v2
# ---------------------------------------------------------------------------

async def _fetch_twitter(social_links: dict) -> dict:
    """Fetch Twitter metrics when X_BEARER_TOKEN is configured.

    Returns {"twitter_followers": int|None, "twitter_statuses_7d": int|None}.
    """
    token = settings.X_BEARER_TOKEN
    if not token:
        return {"twitter_followers": None, "twitter_statuses_7d": None}

    twitter_url = social_links.get("twitter")
    if not twitter_url:
        return {"twitter_followers": None, "twitter_statuses_7d": None}

    # Extract username from URL (e.g. https://x.com/username or https://twitter.com/username)
    username = twitter_url.rstrip("/").split("/")[-1]
    if not username:
        return {"twitter_followers": None, "twitter_statuses_7d": None}

    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api.twitter.com/2"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Look up user by username.
            user_resp = await client.get(
                f"{base}/users/by/username/{username}",
                headers=headers,
                params={"user.fields": "public_metrics"},
            )
            if user_resp.status_code != 200:
                logger.warning("Twitter user lookup failed for {}: HTTP {}", username, user_resp.status_code)
                return {"twitter_followers": None, "twitter_statuses_7d": None}

            user_data = user_resp.json().get("data") or {}
            metrics = user_data.get("public_metrics") or {}
            followers = metrics.get("followers_count")
            tweet_count = metrics.get("tweet_count")

            return {
                "twitter_followers": followers,
                "twitter_statuses_7d": tweet_count,  # API gives total; best-effort
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Twitter API failed for {}: {}", username, exc)
        return {"twitter_followers": None, "twitter_statuses_7d": None}


# ---------------------------------------------------------------------------
# Sentiment heuristic
# ---------------------------------------------------------------------------

def _estimate_sentiment(
    dex_trending: bool,
    volume_24h: Optional[float],
    price_change_24h: Optional[float],
    twitter_followers: Optional[int],
) -> Optional[str]:
    """Simple heuristic sentiment from available signals."""
    signals_positive = 0
    signals_negative = 0
    total = 0

    if dex_trending:
        signals_positive += 1
        total += 1

    if price_change_24h is not None:
        total += 1
        if price_change_24h > 5:
            signals_positive += 1
        elif price_change_24h < -5:
            signals_negative += 1

    if volume_24h is not None and volume_24h > 0:
        total += 1
        signals_positive += 0.5  # any volume is mildly positive

    if twitter_followers is not None:
        total += 1
        if twitter_followers > 1000:
            signals_positive += 1

    if total == 0:
        return None

    ratio = (signals_positive - signals_negative) / total
    if ratio > 0.3:
        return "positive"
    elif ratio < -0.2:
        return "negative"
    return "neutral"


# ---------------------------------------------------------------------------
# Social score
# ---------------------------------------------------------------------------

def compute_social_score(
    dex_trending: bool,
    volume_24h: Optional[float],
    twitter_followers: Optional[int],
    twitter_statuses_7d: Optional[int],
    social_links: dict[str, Optional[str]],
) -> int:
    """Composite 0-100 social activity score. Pure, unit-testable."""
    score = 0

    if dex_trending:
        score += _TRENDING_POINTS

    if volume_24h is not None and volume_24h > 0:
        score += _VOLUME_SPIKE_POINTS

    if twitter_followers is not None and twitter_followers > 1000:
        score += _TWITTER_FOLLOWERS_POINTS

    if twitter_statuses_7d is not None and twitter_statuses_7d > 10:
        score += _TWITTER_ACTIVITY_POINTS

    link_count = sum(1 for v in social_links.values() if v)
    score += min(link_count * _LINK_POINTS, _MAX_LINK_POINTS)

    return min(score, 100)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def analyze_social(
    db: Client, mint: str, *, bypass_cache: bool = False
) -> dict:
    """Full social analysis for a token mint.

    Returns {"mint", "social_links", "social_score", "metrics", "as_of"}.
    Degrades gracefully — missing data sources yield null fields.
    """
    if not bypass_cache:
        cached = token_intel._cache_get("social", mint)
        if cached is not None:
            return cached
        cached = token_intel._db_cache_get(db, "social", mint)
        if cached is not None:
            token_intel._cache_put("social", mint, cached)
            return cached

    # --- DexScreener ---
    dex_pair = await _fetch_dexscreener(mint)
    dex_data = _parse_dexscreener(dex_pair) if dex_pair else {
        "social_links": {"twitter": None, "website": None, "telegram": None, "discord": None},
        "dex_trending": False,
        "dex_volume_24h": None,
        "dex_price_change_24h": None,
    }

    social_links = dex_data["social_links"]

    # --- Twitter/X ---
    twitter_data = await _fetch_twitter(social_links)

    # --- Sentiment ---
    sentiment = _estimate_sentiment(
        dex_data["dex_trending"],
        dex_data["dex_volume_24h"],
        dex_data["dex_price_change_24h"],
        twitter_data["twitter_followers"],
    )

    # --- Score ---
    social_score = compute_social_score(
        dex_data["dex_trending"],
        dex_data["dex_volume_24h"],
        twitter_data["twitter_followers"],
        twitter_data["twitter_statuses_7d"],
        social_links,
    )

    payload = {
        "mint": mint,
        "social_links": social_links,
        "social_score": social_score,
        "metrics": {
            "dex_trending": dex_data["dex_trending"],
            "dex_volume_24h": dex_data["dex_volume_24h"],
            "dex_price_change_24h": dex_data["dex_price_change_24h"],
            "twitter_followers": twitter_data["twitter_followers"],
            "twitter_statuses_7d": twitter_data["twitter_statuses_7d"],
            "social_sentiment": sentiment,
        },
        "as_of": _now_iso(),
    }

    token_intel._cache_put("social", mint, payload)
    token_intel._db_cache_put(db, "social", mint, payload)
    return payload
