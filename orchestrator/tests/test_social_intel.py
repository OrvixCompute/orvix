"""Tests for social intelligence (services/social_intel.py)."""

import pytest
from solders.pubkey import Pubkey

from app.services import social_intel, token_intel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeAsyncClient:
    """Context-manager mock for httpx.AsyncClient."""

    def __init__(self, responses=None):
        self._responses = responses or {}
        self._calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        self._calls.append(url)
        for pattern, resp in self._responses.items():
            if pattern in url:
                return resp
        return FakeResponse(status_code=404)


def _dex_pair(
    trending=False,
    volume_h24=1000.0,
    price_change_h24=5.0,
    twitter_url=None,
    website_url=None,
):
    links = []
    if twitter_url:
        links.append({"type": "twitter", "url": twitter_url})
    if website_url:
        links.append({"type": "website", "url": website_url})
    return {
        "liquidity": {"usd": 50000},
        "volume": {"h24": volume_h24},
        "priceChange": {"h24": price_change_h24},
        "info": {
            "links": links,
            "socials": [{"type": "twitter", "url": twitter_url}] if twitter_url else [],
            "websites": [{"url": website_url}] if website_url else [],
        },
        "boosts": {"active": trending},
    }


def _twitter_user_response(followers=500, tweet_count=20):
    return FakeResponse({
        "data": {
            "id": "12345",
            "username": "testtoken",
            "public_metrics": {
                "followers_count": followers,
                "tweet_count": tweet_count,
            },
        }
    })


@pytest.fixture
def ctx():
    token_intel.reset_scan_cache()
    yield
    token_intel.reset_scan_cache()


# ---------------------------------------------------------------------------
# Score calculation (pure function, no mocking needed)
# ---------------------------------------------------------------------------

def test_social_score_trending():
    score = social_intel.compute_social_score(
        dex_trending=True,
        volume_24h=5000.0,
        twitter_followers=2000,
        twitter_statuses_7d=20,
        social_links={"twitter": "https://x.com/t", "website": "https://t.io"},
    )
    assert score == 100  # 30 + 20 + 15 + 15 + 20


def test_social_score_minimal():
    score = social_intel.compute_social_score(
        dex_trending=False,
        volume_24h=None,
        twitter_followers=None,
        twitter_statuses_7d=None,
        social_links={},
    )
    assert score == 0


def test_social_score_partial():
    score = social_intel.compute_social_score(
        dex_trending=True,
        volume_24h=None,
        twitter_followers=None,
        twitter_statuses_7d=None,
        social_links={"twitter": "https://x.com/t"},
    )
    assert score == 40  # 30 trending + 10 one link


def test_social_score_capped_at_100():
    score = social_intel.compute_social_score(
        dex_trending=True,
        volume_24h=100.0,
        twitter_followers=5000,
        twitter_statuses_7d=100,
        social_links={"twitter": "x", "website": "w", "telegram": "t", "discord": "d"},
    )
    assert score == 100


# ---------------------------------------------------------------------------
# Sentiment heuristic
# ---------------------------------------------------------------------------

def test_sentiment_positive():
    assert social_intel._estimate_sentiment(True, 1000.0, 10.0, 5000) == "positive"


def test_sentiment_negative():
    assert social_intel._estimate_sentiment(False, 100.0, -20.0, None) == "negative"


def test_sentiment_neutral():
    assert social_intel._estimate_sentiment(False, None, 0.0, None) == "neutral"


def test_sentiment_none_when_no_data():
    assert social_intel._estimate_sentiment(False, None, None, None) is None


# ---------------------------------------------------------------------------
# Full analyze_social flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_social_analysis_returns_dexscreener_data(monkeypatch, ctx):
    pair = _dex_pair(
        trending=True,
        volume_h24=25000.0,
        price_change_h24=12.5,
        twitter_url="https://x.com/testtoken",
        website_url="https://testtoken.io",
    )
    dex_resp = FakeResponse({"pairs": [pair]})
    twitter_resp = _twitter_user_response(followers=3000, tweet_count=50)

    def _fake_client(**kwargs):
        return FakeAsyncClient({
            "dexscreener.com": dex_resp,
            "api.twitter.com": twitter_resp,
        })

    monkeypatch.setattr(social_intel, "httpx", type("M", (), {"AsyncClient": _fake_client}))
    monkeypatch.setattr(social_intel.settings, "X_BEARER_TOKEN", "test-bearer-token")

    result = await social_intel.analyze_social(None, str(Pubkey.new_unique()))
    assert result["social_score"] > 0
    assert result["social_links"]["twitter"] == "https://x.com/testtoken"
    assert result["social_links"]["website"] == "https://testtoken.io"
    assert result["metrics"]["dex_trending"] is True
    assert result["metrics"]["dex_volume_24h"] == 25000.0
    assert result["metrics"]["twitter_followers"] == 3000
    assert result["metrics"]["social_sentiment"] == "positive"


@pytest.mark.asyncio
async def test_social_analysis_no_x_token_returns_partial(monkeypatch, ctx):
    pair = _dex_pair(volume_h24=500.0, website_url="https://t.io")
    dex_resp = FakeResponse({"pairs": [pair]})

    def _fake_client(**kwargs):
        return FakeAsyncClient({"dexscreener.com": dex_resp})

    monkeypatch.setattr(social_intel, "httpx", type("M", (), {"AsyncClient": _fake_client}))
    monkeypatch.setattr(social_intel.settings, "X_BEARER_TOKEN", "")

    result = await social_intel.analyze_social(None, str(Pubkey.new_unique()))
    assert result["metrics"]["twitter_followers"] is None
    assert result["metrics"]["twitter_statuses_7d"] is None
    assert result["social_links"]["website"] == "https://t.io"
    assert result["social_score"] > 0  # at least volume + link points


@pytest.mark.asyncio
async def test_social_analysis_dexscreener_failure_graceful(monkeypatch, ctx):
    def _fake_client(**kwargs):
        return FakeAsyncClient({})  # all 404

    monkeypatch.setattr(social_intel, "httpx", type("M", (), {"AsyncClient": _fake_client}))

    result = await social_intel.analyze_social(None, str(Pubkey.new_unique()))
    assert result["social_score"] == 0
    assert result["metrics"]["dex_trending"] is False
    assert result["metrics"]["dex_volume_24h"] is None


@pytest.mark.asyncio
async def test_cached_social_avoids_fetch(monkeypatch, ctx):
    mint = str(Pubkey.new_unique())
    cached_payload = {
        "mint": mint,
        "social_links": {},
        "social_score": 42,
        "metrics": {"dex_trending": True},
        "as_of": "2025-01-01T00:00:00",
    }
    token_intel._cache_put("social", mint, cached_payload)

    # If httpx is called, the test should fail — so give it a broken client.
    def _broken_client(**kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(social_intel, "httpx", type("M", (), {"AsyncClient": _broken_client}))

    result = await social_intel.analyze_social(None, mint)
    assert result["social_score"] == 42
    assert result["metrics"]["dex_trending"] is True
