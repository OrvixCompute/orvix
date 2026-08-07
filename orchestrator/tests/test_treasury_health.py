"""Tests for the treasury balance monitor.

The failure this guards against is a payout wallet that still shows a healthy
USDC float while its SOL has run dry — it cannot broadcast, so every withdrawal
fails and refunds instead of waiting for funds.
"""

import pytest

import app.services.treasury_health as th
from app.services.treasury_health import CRITICAL, WARNING, check


class _FakeSolana:
    def __init__(self, sol: dict, usdc: dict):
        self._sol = sol
        self._usdc = usdc
        self.closed = False

    async def get_sol_balance(self, address):
        from decimal import Decimal

        return Decimal(str(self._sol.get(address, 0)))

    async def get_token_balance(self, address, mint):
        from decimal import Decimal

        return Decimal(str(self._usdc.get(address, 0)))

    async def close(self):
        self.closed = True


PAYOUT = "PayoutPubkey11111111111111111111111111111111"
HOT = "HotPubkey1111111111111111111111111111111111"


@pytest.fixture
def wired(monkeypatch):
    """Point the service at fake wallets and a fake chain."""

    def _wire(*, payout_sol, payout_usdc, hot_sol, hot_usdc=0):
        monkeypatch.setattr(
            th.wallet_service,
            "public_key",
            lambda role: {"payout": PAYOUT, "hot": HOT}.get(role, ""),
        )
        fake = _FakeSolana(
            {PAYOUT: payout_sol, HOT: hot_sol}, {PAYOUT: payout_usdc, HOT: hot_usdc}
        )
        monkeypatch.setattr(th, "get_solana_service", lambda: fake)
        monkeypatch.setattr(th.settings, "USDC_MINT_ADDRESS", "UsdcMint111")
        monkeypatch.setattr(th.settings, "TREASURY_MIN_PAYOUT_SOL", 0.02)
        monkeypatch.setattr(th.settings, "TREASURY_MIN_PAYOUT_USDC", 5.0)
        monkeypatch.setattr(th.settings, "TREASURY_MIN_HOT_SOL", 0.005)
        return fake

    return _wire


async def test_healthy_treasury_reports_ok(wired):
    wired(payout_sol=0.1, payout_usdc=50, hot_sol=0.01)
    result = await check()
    assert result["ok"] is True
    assert result["alerts"] == []
    assert result["wallets"]["payout"]["sol"] == pytest.approx(0.1)


async def test_payout_out_of_sol_is_critical_even_with_plenty_of_usdc(wired):
    """The case the monitor exists for: rich in USDC, unable to send any of it."""
    wired(payout_sol=0.001, payout_usdc=500, hot_sol=0.01)
    result = await check()

    assert result["ok"] is False
    sol_alerts = [a for a in result["alerts"] if a["wallet"] == "payout" and a["asset"] == "SOL"]
    assert len(sol_alerts) == 1
    assert sol_alerts[0]["severity"] == CRITICAL
    # A healthy USDC float must not mask it.
    assert not [a for a in result["alerts"] if a["asset"] == "USDC"]


async def test_low_payout_float_warns_but_is_not_critical(wired):
    wired(payout_sol=0.1, payout_usdc=1.0, hot_sol=0.01)
    result = await check()

    usdc_alerts = [a for a in result["alerts"] if a["asset"] == "USDC"]
    assert len(usdc_alerts) == 1
    assert usdc_alerts[0]["severity"] == WARNING


async def test_critical_alerts_sort_before_warnings(wired):
    wired(payout_sol=0.001, payout_usdc=1.0, hot_sol=0.0001)
    result = await check()
    severities = [a["severity"] for a in result["alerts"]]
    assert severities == sorted(severities, key=lambda s: 0 if s == CRITICAL else 1)
    assert severities[0] == CRITICAL


async def test_unconfigured_wallet_alerts_instead_of_raising(monkeypatch):
    """A monitor that crashes on a missing wallet stops reporting the problem."""
    monkeypatch.setattr(th.wallet_service, "public_key", lambda role: "")
    fake = _FakeSolana({}, {})
    monkeypatch.setattr(th, "get_solana_service", lambda: fake)

    result = await check()

    assert result["ok"] is False
    assert {a["wallet"] for a in result["alerts"]} == {"payout", "hot"}
    assert all(a["severity"] == CRITICAL for a in result["alerts"])


async def test_rpc_client_is_closed_even_when_alerting(wired):
    fake = wired(payout_sol=0.0, payout_usdc=0.0, hot_sol=0.0)
    await check()
    assert fake.closed is True
