"""Tests for token/wallet scans via the token_intel service."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from solders.pubkey import Pubkey

from app.services import token_intel


@pytest.fixture
def ctx():
    token_intel.reset_scan_cache()
    yield
    token_intel.reset_scan_cache()


class FakeSolana:
    """Stubbed Solana service with controllable responses."""

    def __init__(self):
        self.supply = {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}
        self.metadata_account = None
        self.accounts_by_owner = []
        self.signatures = []
        self.parsed_txs = {}
        self.token_balances = {}
        self.calls = {"supply": 0, "account_info": 0, "signatures": 0}

    async def get_token_supply(self, mint):
        self.calls["supply"] += 1
        return self.supply

    async def get_account_info(self, address, encoding="base64"):
        self.calls["account_info"] += 1
        return self.metadata_account

    async def get_token_accounts_by_owner(self, owner, mint):
        return self.accounts_by_owner

    async def get_token_balance(self, owner, mint):
        return self.token_balances.get((owner, mint), Decimal(0))

    async def get_token_largest_accounts(self, mint):
        return []

    async def get_token_account_owner(self, account):
        return None

    async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
        self.calls["signatures"] += 1
        return self.signatures

    async def get_parsed_transaction(self, signature):
        return self.parsed_txs.get(signature)

    @staticmethod
    def extract_memo(parsed_tx):
        return None


@pytest.fixture
def sol():
    return FakeSolana()


@pytest.mark.asyncio
async def test_scan_token_returns_fields(monkeypatch, ctx, sol):
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel, "get_token_price_usdc", _price(1.23))

    result = await token_intel.scan_token(None, str(Pubkey.new_unique()))
    assert result["mint"]
    assert result["supply"]["decimals"] == 6
    assert result["price_usdc"] == 1.23
    assert result["liquidity"]["pool_count"] == 0
    assert result["liquidity"]["estimated_usdc"] is None
    assert result["holders"]["top_holders"] == []  # no accounts resolvable
    assert isinstance(result["risk"]["warnings"], list)


@pytest.mark.asyncio
async def test_scan_token_missing_metadata_is_fail_soft(monkeypatch, ctx, sol):
    sol.metadata_account = None
    sol.supply = None
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel, "get_token_price_usdc", _price(None))

    result = await token_intel.scan_token(None, str(Pubkey.new_unique()))
    assert result["metadata"] is None
    assert result["supply"] is None
    assert result["price_usdc"] is None


def _price(value):
    async def _p(mint):
        return Decimal(str(value)) if value is not None else None
    return _p


@pytest.mark.asyncio
async def test_liquidity_from_configured_pools(monkeypatch, ctx, sol):
    mint = str(Pubkey.new_unique())
    sol.token_balances = {("pool1", mint): Decimal("5000"), ("pool2", mint): Decimal("3000")}
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel, "get_token_price_usdc", _price(None))
    monkeypatch.setattr(
        token_intel.settings, "TOKEN_POOLS_JSON", f'{{"{mint}": ["pool1", "pool2"]}}'
    )

    result = await token_intel.scan_token(None, mint)
    assert result["liquidity"]["pool_count"] == 2
    assert result["liquidity"]["estimated_usdc"] == 8000.0


@pytest.mark.asyncio
async def test_scan_token_caches_in_memory(monkeypatch, ctx, sol):
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel, "get_token_price_usdc", _price(0.5))

    mint = str(Pubkey.new_unique())
    await token_intel.scan_token(None, mint)
    await token_intel.scan_token(None, mint)
    # Supply + account-info RPC calls happen once; the second read is cached.
    assert sol.calls["supply"] == 1


class FakeDb:
    """Minimal intel_scans table for cache tests (select + upsert)."""

    def __init__(self):
        self.rows = {}

    class _Q:
        def __init__(self, rows):
            self._rows = rows
            self._f = []
            self._lim = None
            self._op = None
            self._values = None
            self._on_conflict = None

        def select(self, *a, **k):
            return self

        def eq(self, c, v):
            self._f.append((c, v))
            return self

        def limit(self, n):
            self._lim = n
            return self

        def upsert(self, values, on_conflict=None):
            self._op = "upsert"
            self._values = values
            self._on_conflict = on_conflict
            return self

        def execute(self):
            if self._op == "upsert":
                row = dict(self._values)
                for existing in self._rows:
                    if existing.get("scan_type") == row.get("scan_type") and existing.get("target") == row.get("target"):
                        existing.update(row)
                        return type("R", (), {"data": [existing]})()
                self._rows.append(row)
                return type("R", (), {"data": [row]})()
            out = self._rows
            for c, v in self._f:
                out = [r for r in out if r.get(c) == v]
            return type("R", (), {"data": out[: self._lim] if self._lim else out})()

    def table(self, name):
        return self._Q(self.rows.setdefault(name, []))


@pytest.mark.asyncio
async def test_scan_token_db_cache_hit_avoids_rpc(monkeypatch, ctx, sol):
    mint = str(Pubkey.new_unique())
    db = FakeDb()
    db.rows["intel_scans"] = [
        {
            "scan_type": "token",
            "target": mint,
            "payload": {"mint": mint, "price_usdc": 9.99, "scanned_at": "x"},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)

    result = await token_intel.scan_token(db, mint)
    assert result["price_usdc"] == 9.99
    # No RPC calls made for a fresh DB-cached row.
    assert sol.calls["supply"] == 0
    assert sol.calls["account_info"] == 0


@pytest.mark.asyncio
async def test_analyze_wallet_holdings_and_activity(monkeypatch, ctx, sol):
    wallet = str(Pubkey.new_unique())
    mint_a = str(Pubkey.new_unique())
    sol.accounts_by_owner = [
        {
            "account": {
                "data": {
                    "parsed": {
                        "info": {"mint": mint_a, "tokenAmount": {"uiAmount": 42.5}}
                    }
                }
            }
        }
    ]
    sig = "sig1"
    sol.signatures = [{"signature": sig, "slot": 1, "blockTime": 1700000000}]
    sol.parsed_txs[sig] = {
        "transaction": {"message": {"instructions": []}},
        "meta": {},
    }
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)

    result = await token_intel.analyze_wallet(None, wallet, mint=mint_a)
    assert result["wallet"] == wallet
    assert result["holdings"][0]["ui_amount"] == 42.5
    assert result["recent_activity"][0]["signature"] == sig
    assert result["buy_history"] == []


@pytest.mark.asyncio
async def test_holdings_resolve_metadata_when_enabled(monkeypatch, ctx, sol):
    """RESOLVE_HOLDING_METADATA fills symbol/name per holding."""
    wallet = str(Pubkey.new_unique())
    mint_a = str(Pubkey.new_unique())
    sol.accounts_by_owner = [
        {
            "account": {
                "data": {
                    "parsed": {
                        "info": {"mint": mint_a, "tokenAmount": {"uiAmount": 7.0}}
                    }
                }
            }
        }
    ]
    sol.metadata_account = None  # FakeSolana default; override per mint below
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel.settings, "RESOLVE_HOLDING_METADATA", True)

    async def fake_fetch_metadata(_sol, mint):
        if mint == mint_a:
            return {"name": "Orvix Token", "symbol": "ORVX", "uri": None,
                    "update_authority": None, "mint": mint}
        return None

    monkeypatch.setattr(token_intel.token_metadata, "fetch_metadata", fake_fetch_metadata)

    result = await token_intel.analyze_wallet(None, wallet)
    assert result["holdings"][0]["symbol"] == "ORVX"
    assert result["holdings"][0]["name"] == "Orvix Token"


@pytest.mark.asyncio
async def test_holdings_skip_metadata_when_disabled(monkeypatch, ctx, sol):
    wallet = str(Pubkey.new_unique())
    mint_a = str(Pubkey.new_unique())
    sol.accounts_by_owner = [
        {
            "account": {
                "data": {
                    "parsed": {
                        "info": {"mint": mint_a, "tokenAmount": {"uiAmount": 7.0}}
                    }
                }
            }
        }
    ]
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel.settings, "RESOLVE_HOLDING_METADATA", False)

    called = {"n": 0}

    async def fake_fetch_metadata(_sol, mint):
        called["n"] += 1
        return None

    monkeypatch.setattr(token_intel.token_metadata, "fetch_metadata", fake_fetch_metadata)

    result = await token_intel.analyze_wallet(None, wallet)
    assert result["holdings"][0]["symbol"] is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_refresh_holder_snapshot_from_watchlist(monkeypatch, ctx, sol):
    """Refresh derives top holders from the watchlist and writes the cache."""
    mint = str(Pubkey.new_unique())
    wallet_a = str(Pubkey.new_unique())
    wallet_b = str(Pubkey.new_unique())
    sol.supply = {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}
    sol.token_balances = {
        (wallet_a, mint): Decimal("600"),
        (wallet_b, mint): Decimal("400"),
    }
    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(
        token_intel.settings,
        "TOKEN_WHALE_WATCHLIST_JSON",
        f'["{wallet_a}", "{wallet_b}"]',
    )
    db = FakeDb()

    snapshot = await token_intel.refresh_holder_snapshot(db, mint)
    assert snapshot["total_holders"] == 2
    assert snapshot["top_holders"][0]["wallet"] == wallet_a
    assert snapshot["top_holders"][0]["balance"] == 600.0
    assert snapshot["top10_share"] == 1.0  # both wallets = 100% of supply

    # Merged into the token-scan cache.
    assert db.rows["intel_scans"][0]["payload"]["holders"]["total_holders"] == 2


@pytest.mark.asyncio
async def test_refresh_holder_snapshot_requires_watchlist(monkeypatch, ctx, sol):
    from app.exceptions import ValidationError

    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(token_intel.settings, "TOKEN_WHALE_WATCHLIST_JSON", "")

    with pytest.raises(ValidationError):
        await token_intel.refresh_holder_snapshot(FakeDb(), str(Pubkey.new_unique()))


@pytest.mark.asyncio
async def test_refresh_holder_snapshot_rejects_invalid_mint(monkeypatch, ctx, sol):
    from app.exceptions import ValidationError

    monkeypatch.setattr(token_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(
        token_intel.settings, "TOKEN_WHALE_WATCHLIST_JSON", '["wallet1"]'
    )

    with pytest.raises(ValidationError):
        await token_intel.refresh_holder_snapshot(FakeDb(), "not-an-address")
