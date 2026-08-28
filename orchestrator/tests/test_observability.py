"""Tests for observability wiring: intel scan logging and Sentry intel tags."""

import logging

import pytest
from loguru import logger as loguru_logger
from solders.pubkey import Pubkey

from app.exceptions import capture_intel_error
from app.logger import log_intel_scan
from app.services import token_intel


@pytest.fixture
def loguru_capture():
    """Capture loguru INFO+ lines (caplog does not see loguru records)."""
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage().strip())

    logger = logging.getLogger("loguru")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _Sink()
    logger.addHandler(handler)

    def interop(message: str) -> None:
        record = logger.makeRecord("loguru", logging.INFO, "", 0, message, None, None)
        logger.handle(record)

    sink_id = loguru_logger.add(interop, level="INFO", format="{message}")
    try:
        yield records
    finally:
        loguru_logger.remove(sink_id)
        logger.removeHandler(handler)


def _intel_lines(records: list[str]) -> list[str]:
    return [r for r in records if "intel_scan" in r]


# --- intel scan structured logging -----------------------------------------

def test_log_intel_scan_emits_structured_fields(loguru_capture):
    log_intel_scan("token", "mint-abc", cache_hit=False, duration_ms=12.3)
    lines = _intel_lines(loguru_capture)
    assert len(lines) == 1
    msg = lines[0]
    assert "scan_type=token" in msg
    assert "target=mint-abc" in msg
    assert "cache_hit=False" in msg
    assert "duration_ms=12.3" in msg


def test_log_intel_scan_strips_wallet_mint_suffix(loguru_capture):
    log_intel_scan("wallet", "wallet-x:mint-y", cache_hit=True, duration_ms=1.0)
    msg = _intel_lines(loguru_capture)[0]
    assert "scan_type=wallet" in msg
    assert "target=wallet-x" in msg
    assert "cache_hit=True" in msg
    assert "mint-y" not in msg.split("target=")[1].split(" ")[0]


def test_log_intel_scan_plain_wallet_key_unchanged(loguru_capture):
    log_intel_scan("wallet", "wallet-x", cache_hit=False, duration_ms=2.0)
    msg = _intel_lines(loguru_capture)[0]
    assert "scan_type=wallet" in msg
    assert "target=wallet-x" in msg


def test_log_intel_scan_keeps_other_scan_types_verbatim(loguru_capture):
    log_intel_scan("top_holders", "mint-abc", cache_hit=False, duration_ms=3.0)
    msg = _intel_lines(loguru_capture)[0]
    assert "scan_type=top_holders" in msg
    assert "target=mint-abc" in msg


# --- scan entry points emit a line -----------------------------------------

class _FakeSol:
    async def get_token_supply(self, mint):
        return {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}

    async def get_account_info(self, address, encoding="base64"):
        return None

    async def get_token_largest_accounts(self, mint):
        return []

    async def get_token_account_owner(self, account):
        return None

    async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
        return []

    async def get_token_accounts_by_owner(self, owner, mint):
        return []


@pytest.mark.asyncio
async def test_scan_token_logs_cache_hit_and_miss(monkeypatch, loguru_capture):
    from tests.fakes import FakeSupabase

    db = FakeSupabase()
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: _FakeSol())

    async def _no_price(mint):
        return None

    monkeypatch.setattr(token_intel, "get_token_price_usdc", _no_price)
    token_intel.reset_scan_cache()
    ca = str(Pubkey.new_unique())

    await token_intel.scan_token(db, ca)  # miss
    await token_intel.scan_token(db, ca)  # in-memory hit
    await token_intel.scan_token(db, ca)  # hit again

    lines = _intel_lines(loguru_capture)
    token_lines = [line for line in lines if "scan_type=token " in line]
    assert len(token_lines) == 3
    assert all(f"target={ca}" in line for line in token_lines)
    assert token_lines[0].startswith("intel_scan scan_type=token") and "cache_hit=False" in token_lines[0]
    assert all("cache_hit=True" in line for line in token_lines[1:])


@pytest.mark.asyncio
async def test_compute_accumulation_logs_bypass_cache(monkeypatch, loguru_capture):
    from tests.fakes import FakeSupabase

    db = FakeSupabase()
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: _FakeSol())
    token_intel.reset_scan_cache()
    ca = str(Pubkey.new_unique())

    await token_intel.compute_accumulation(db, ca, bypass_cache=True)

    lines = _intel_lines(loguru_capture)
    assert len(lines) == 1
    assert "scan_type=accumulation" in lines[0]
    assert "cache_hit=False" in lines[0]


# --- Sentry intel error tags ------------------------------------------------

def test_capture_intel_error_sets_sentry_tags(monkeypatch):
    """capture_intel_error logs a warning and tags the Sentry scope."""
    captured = {}

    class _FakeScope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tag(self, k, v):
            captured[k] = v

    monkeypatch.setattr("app.exceptions.sentry_sdk.new_scope", lambda: _FakeScope())
    captured_exc = []

    def _fake_capture(exc):
        captured_exc.append(exc)

    monkeypatch.setattr("app.exceptions.sentry_sdk.capture_exception", _fake_capture)

    exc = ValueError("rpc down")
    capture_intel_error("token", "mint-abc", exc)
    assert captured.get("scan_type") == "token"
    assert captured.get("target") == "mint-abc"
    assert captured_exc == [exc]


def test_report_to_sentry_is_callable():
    """The request-level reporter stays importable (unchanged behaviour)."""
    from app.exceptions import _report_to_sentry

    assert callable(_report_to_sentry)
