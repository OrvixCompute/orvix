"""Tests for the accumulation score formula (pure functions in token_intel)."""

from decimal import Decimal

from app.services import token_intel


def test_zero_inflow_is_weak():
    metrics = {"inflow_7d": 0, "inflow_ratio": 0, "buy_tx_7d": 0, "top10_share": None}
    score, label = token_intel._accumulation_score(metrics)
    assert score == 10  # 0.5*0 + 0.3*0 + 0.2*50
    assert label == "weak"


def test_negative_inflow_is_distribution():
    metrics = {"inflow_7d": -5000, "inflow_ratio": -0.01, "buy_tx_7d": 0, "top10_share": None}
    score, label = token_intel._accumulation_score(metrics)
    assert label == "distribution"
    assert score <= 10


def test_full_inflow_scores_100():
    # inflow_ratio 2% of supply -> inflow_score 100.
    metrics = {
        "inflow_7d": 20000,
        "inflow_ratio": 0.02,
        "buy_tx_7d": 20,
        "top10_share": None,
    }
    score, label = token_intel._accumulation_score(metrics)
    # 0.5*100 + 0.3*100 + 0.2*50 = 90
    assert score == 90
    assert label == "strong"


def test_moderate_threshold():
    # inflow_ratio 0.5% -> inflow_score 25; 5 buy txs -> activity 25; no snapshot.
    metrics = {"inflow_7d": 500, "inflow_ratio": 0.005, "buy_tx_7d": 5, "top10_share": None}
    score, label = token_intel._accumulation_score(metrics)
    # 0.5*25 + 0.3*25 + 0.2*50 = 30
    assert score == 30
    assert label == "weak"


def test_moderate_label_above_40():
    metrics = {
        "inflow_7d": 1000,
        "inflow_ratio": 0.01,  # inflow_score 50
        "buy_tx_7d": 10,       # activity_score 50
        "top10_share": None,
    }
    score, label = token_intel._accumulation_score(metrics)
    # 0.5*50 + 0.3*50 + 0.2*50 = 50
    assert score == 50
    assert label == "moderate"


def test_concentrated_holders_reduce_score():
    # Same flows but top10 = 90% -> distribution_score 20.
    metrics = {
        "inflow_7d": 1000,
        "inflow_ratio": 0.01,
        "buy_tx_7d": 10,
        "top10_share": 0.9,
    }
    score, _label = token_intel._accumulation_score(metrics)
    # 0.5*50 + 0.3*50 + 0.2*20 = 44
    assert score == 44


def test_clamping():
    # inflow_ratio way above the full-score point clamps at 100.
    metrics = {
        "inflow_7d": 1_000_000,
        "inflow_ratio": 0.5,
        "buy_tx_7d": 200,
        "top10_share": None,
    }
    score, label = token_intel._accumulation_score(metrics)
    assert score == 90  # clamped: 0.5*100 + 0.3*100 + 0.2*50
    assert label == "strong"


def test_distribution_score_missing_snapshot_neutral():
    assert token_intel._distribution_score(None) == 50.0


def test_distribution_score_neutral_at_50pct():
    assert token_intel._distribution_score(Decimal("0.5")) == 100.0


def test_distribution_score_zero_at_100pct():
    assert token_intel._distribution_score(Decimal("1.0")) == 0.0


def test_inflow_score_scales_linearly():
    assert token_intel._inflow_score(Decimal("100"), Decimal("10000")) == 50.0
    assert token_intel._inflow_score(Decimal("200"), Decimal("10000")) == 100.0
    assert token_intel._inflow_score(Decimal("-50"), Decimal("10000")) == 0.0
    assert token_intel._inflow_score(Decimal("1"), None) == 0.0


def test_activity_score_scales_linearly():
    assert token_intel._activity_score(10) == 50.0
    assert token_intel._activity_score(20) == 100.0
    assert token_intel._activity_score(0) == 0.0
