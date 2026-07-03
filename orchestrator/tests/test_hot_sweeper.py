"""Tests for HotSweeper threshold + sweep-amount logic (send is mocked)."""

from decimal import Decimal

import pytest

from app.config import settings
from app.services.hot_sweeper import HotSweeper


def _set_balance(monkeypatch, value):
    async def fake_bal(role):
        return Decimal(str(value))

    monkeypatch.setattr("app.services.hot_sweeper.wallet_service.get_usdc_balance", fake_bal)


async def test_below_threshold_skips(monkeypatch):
    monkeypatch.setattr(settings, "HOT_SWEEP_THRESHOLD_USDC", 100.0)
    _set_balance(monkeypatch, 50)
    res = await HotSweeper().run_once()
    assert res == {"swept": False, "reason": "below_threshold", "hot_balance_usdc": 50.0}


async def test_no_main_wallet_skips(monkeypatch):
    monkeypatch.setattr(settings, "HOT_SWEEP_THRESHOLD_USDC", 100.0)
    monkeypatch.setattr(settings, "TREASURY_MAIN_PUBLIC", "")
    _set_balance(monkeypatch, 500)
    res = await HotSweeper().run_once()
    assert res["swept"] is False and res["reason"] == "no_main_wallet"


async def test_sweeps_amount_above_keep(monkeypatch):
    monkeypatch.setattr(settings, "HOT_SWEEP_THRESHOLD_USDC", 100.0)
    monkeypatch.setattr(settings, "HOT_SWEEP_MIN_KEEP_USDC", 10.0)
    monkeypatch.setattr(settings, "TREASURY_MAIN_PUBLIC", "MAINPUB")
    monkeypatch.setattr(settings, "TREASURY_SWEEP_STUB", True)
    _set_balance(monkeypatch, 500)

    captured = {}

    async def fake_send(from_role, to_owner, amount, *, stub):
        captured.update(from_role=from_role, to_owner=to_owner, amount=amount, stub=stub)
        return "STUB_SEND_x"

    monkeypatch.setattr("app.services.hot_sweeper.wallet_service.send_usdc", fake_send)

    res = await HotSweeper().run_once()

    assert res["swept"] is True
    assert res["amount_usdc"] == pytest.approx(490.0)  # 500 - 10 keep
    assert captured["from_role"] == "hot"
    assert captured["to_owner"] == "MAINPUB"
    assert captured["amount"] == Decimal("490")
    assert captured["stub"] is True
