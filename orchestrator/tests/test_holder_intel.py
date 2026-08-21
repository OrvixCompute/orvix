"""Tests for real holder enumeration + early-buyer tracing (holder_intel)."""

import pytest
from solders.pubkey import Pubkey

from app.services import holder_intel, token_intel


class FakeSol:
    def __init__(self):
        self.largest = []
        self.owners = {}
        self.supply = {"amount": "1000000000", "decimals": 6, "uiAmountString": "1000.0"}
        self.sigs_by_wallet = {}
        self.parsed_by_sig = {}
        self.token_accounts_by_owner = {}

    async def get_token_largest_accounts(self, mint):
        return self.largest

    async def get_token_account_owner(self, account):
        return self.owners.get(account)

    async def get_token_supply(self, mint):
        return self.supply

    async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
        return self.sigs_by_wallet.get(address, [])

    async def get_parsed_transaction(self, signature):
        return self.parsed_by_sig.get(signature)

    async def get_token_accounts_by_owner(self, owner):
        return self.token_accounts_by_owner.get(owner, [])


def _holder_payload(wallet, balance):
    return {"wallet": wallet, "token_account": "acc-" + wallet[:6], "balance": balance}


@pytest.fixture
def ctx():
    token_intel.reset_scan_cache()
    yield
    token_intel.reset_scan_cache()


@pytest.mark.asyncio
async def test_top_holders_ranked_with_share(monkeypatch, ctx):
    sol = FakeSol()
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sol.largest = [
        {"address": "acc1", "uiAmountString": "600.0"},
        {"address": "acc2", "uiAmountString": "400.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}
    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    result = await holder_intel.top_holders(None, str(Pubkey.new_unique()))
    assert result["total_holders"] == 2
    assert result["top_holders"][0]["wallet"] == w_a
    assert result["top_holders"][0]["balance"] == 600.0
    assert result["top10_share"] == 1.0
    assert result["source"] == "getTokenLargestAccounts"


@pytest.mark.asyncio
async def test_top_holders_falls_back_to_watchlist(monkeypatch, ctx):
    sol = FakeSol()  # no largest accounts
    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)
    monkeypatch.setattr(
        holder_intel.token_intel,
        "_load_holder_snapshot",
        _snapshot({"total_holders": 1, "top_holders": [{"wallet": "w", "balance": 5.0}]}),
    )

    result = await holder_intel.top_holders(None, str(Pubkey.new_unique()))
    assert result["source"] == "watchlist-fallback"
    assert result["total_holders"] == 1


@pytest.mark.asyncio
async def test_early_buyers_oldest_first(monkeypatch, ctx):
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sol.largest = [
        {"address": "acc1", "uiAmountString": "500.0"},
        {"address": "acc2", "uiAmountString": "500.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}
    # w_a bought at t=100, w_b at t=200 (signatures newest-first).
    sig_a1, sig_a2 = "a-new", "a-old"
    sig_b1 = "b-new"
    sol.sigs_by_wallet[w_a] = [
        {"signature": sig_a1, "blockTime": 300, "err": None},
        {"signature": sig_a2, "blockTime": 100, "err": None},
    ]
    sol.sigs_by_wallet[w_b] = [{"signature": sig_b1, "blockTime": 200, "err": None}]
    sol.parsed_by_sig[sig_a1] = _parsed_tx([])  # new tx has no buy
    sol.parsed_by_sig[sig_a2] = _parsed_tx(_inflow(w_a, "500.0", mint))
    sol.parsed_by_sig[sig_b1] = _parsed_tx(_inflow(w_b, "500.0", mint))
    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    buyers = await holder_intel.early_buyers(None, mint)
    assert len(buyers) == 2
    # Oldest buy first.
    assert buyers[0]["wallet"] == w_a
    assert buyers[0]["signature"] == sig_a2
    assert buyers[1]["wallet"] == w_b


@pytest.mark.asyncio
async def test_early_buyers_skip_holder_without_buy_history(monkeypatch, ctx):
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sol.largest = [
        {"address": "acc1", "uiAmountString": "500.0"},
        {"address": "acc2", "uiAmountString": "500.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}
    sig_a = "a-1"
    sol.sigs_by_wallet[w_a] = [{"signature": sig_a, "blockTime": 100, "err": None}]
    sol.sigs_by_wallet[w_b] = []
    sol.parsed_by_sig[sig_a] = _parsed_tx(_inflow(w_a, "100.0", mint))
    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    buyers = await holder_intel.early_buyers(None, mint)
    assert len(buyers) == 1
    assert buyers[0]["wallet"] == w_a


def _snapshot(payload):
    async def _snap(db, mint):
        return payload

    return _snap


def _inflow(wallet, amount, mint):
    return {
        "type": "transferChecked",
        "info": {
            "mint": mint,
            "source": "src",
            "destination": wallet,
            "tokenAmount": {"uiAmount": float(amount), "uiAmountString": amount},
        },
    }


def _parsed_tx(ix_parsed_list):
    if not isinstance(ix_parsed_list, list):
        ix_parsed_list = [ix_parsed_list]
    return {
        "transaction": {
            "message": {
                "instructions": [
                    {"program": "spl-token", "parsed": p} for p in ix_parsed_list
                ]
            }
        }
    }


def _system_transfer(source, destination, lamports="10000000"):
    return {
        "program": "system",
        "parsed": {
            "type": "transfer",
            "info": {"source": source, "destination": destination, "lamports": lamports},
        },
    }


def _token_account(mint, amount="100.0"):
    return {
        "account": {
            "data": {
                "parsed": {
                    "info": {"mint": mint, "tokenAmount": {"uiAmountString": amount}},
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Clustering tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_clusters_shared_funding(monkeypatch, ctx):
    """Two wallets funded by the same source should cluster."""
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    funder = str(Pubkey.new_unique())
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())

    # Top holders setup
    sol.largest = [
        {"address": "acc1", "uiAmountString": "500.0"},
        {"address": "acc2", "uiAmountString": "500.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}

    # Both wallets funded by the same funder
    sol.sigs_by_wallet[w_a] = [{"signature": "sig-a", "blockTime": 100, "err": None}]
    sol.sigs_by_wallet[w_b] = [{"signature": "sig-b", "blockTime": 100, "err": None}]
    sol.parsed_by_sig["sig-a"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder, w_a),
    ]}}}
    sol.parsed_by_sig["sig-b"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder, w_b),
    ]}}}

    # No token holdings
    sol.token_accounts_by_owner = {w_a: [], w_b: []}

    # No early buy history (not needed for funding signal)
    sol.sigs_by_wallet[w_a] = [{"signature": "sig-a", "blockTime": 100, "err": None}]
    sol.sigs_by_wallet[w_b] = [{"signature": "sig-b", "blockTime": 100, "err": None}]

    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    result = await holder_intel.detect_clusters(None, mint)
    assert result["total_wallets_analyzed"] == 2
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert set(cluster["wallets"]) == {w_a, w_b}
    assert "shared_funding" in cluster["signals"]
    assert cluster["confidence"] > 0


@pytest.mark.asyncio
async def test_detect_clusters_coordinated_timing(monkeypatch, ctx):
    """Two wallets that bought within ±60s should cluster."""
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())

    sol.largest = [
        {"address": "acc1", "uiAmountString": "500.0"},
        {"address": "acc2", "uiAmountString": "500.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}

    # Different funders (no shared_funding signal)
    funder_a, funder_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sol.sigs_by_wallet[w_a] = [
        {"signature": "sig-a-fund", "blockTime": 50, "err": None},
        {"signature": "sig-a-buy", "blockTime": 100, "err": None},
    ]
    sol.sigs_by_wallet[w_b] = [
        {"signature": "sig-b-fund", "blockTime": 50, "err": None},
        {"signature": "sig-b-buy", "blockTime": 130, "err": None},  # 30s apart
    ]
    sol.parsed_by_sig["sig-a-fund"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder_a, w_a),
    ]}}}
    sol.parsed_by_sig["sig-b-fund"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder_b, w_b),
    ]}}}
    sol.parsed_by_sig["sig-a-buy"] = _parsed_tx(_inflow(w_a, "500.0", mint))
    sol.parsed_by_sig["sig-b-buy"] = _parsed_tx(_inflow(w_b, "500.0", mint))

    sol.token_accounts_by_owner = {w_a: [], w_b: []}

    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    result = await holder_intel.detect_clusters(None, mint)
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert "coordinated_timing" in cluster["signals"]


@pytest.mark.asyncio
async def test_detect_clusters_overlapping_holdings(monkeypatch, ctx):
    """Two wallets holding the same tokens should cluster."""
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())

    sol.largest = [
        {"address": "acc1", "uiAmountString": "500.0"},
        {"address": "acc2", "uiAmountString": "500.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}

    # Different funders, no buy history
    funder_a, funder_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sol.sigs_by_wallet[w_a] = [{"signature": "sig-a", "blockTime": 100, "err": None}]
    sol.sigs_by_wallet[w_b] = [{"signature": "sig-b", "blockTime": 200, "err": None}]
    sol.parsed_by_sig["sig-a"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder_a, w_a),
    ]}}}
    sol.parsed_by_sig["sig-b"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder_b, w_b),
    ]}}}

    # Both hold the same 3 tokens (Jaccard = 1.0)
    shared_mints = [str(Pubkey.new_unique()) for _ in range(3)]
    sol.token_accounts_by_owner = {
        w_a: [_token_account(m) for m in shared_mints],
        w_b: [_token_account(m) for m in shared_mints],
    }

    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    result = await holder_intel.detect_clusters(None, mint)
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert "overlapping_holdings" in cluster["signals"]


@pytest.mark.asyncio
async def test_detect_clusters_no_signals(monkeypatch, ctx):
    """Wallets with nothing in common should not cluster."""
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    w_a, w_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())

    sol.largest = [
        {"address": "acc1", "uiAmountString": "500.0"},
        {"address": "acc2", "uiAmountString": "500.0"},
    ]
    sol.owners = {"acc1": w_a, "acc2": w_b}

    # Different funders, different timing, different holdings
    funder_a, funder_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sol.sigs_by_wallet[w_a] = [
        {"signature": "sig-a-fund", "blockTime": 50, "err": None},
        {"signature": "sig-a-buy", "blockTime": 100, "err": None},
    ]
    sol.sigs_by_wallet[w_b] = [
        {"signature": "sig-b-fund", "blockTime": 50, "err": None},
        {"signature": "sig-b-buy", "blockTime": 500, "err": None},  # 400s apart
    ]
    sol.parsed_by_sig["sig-a-fund"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder_a, w_a),
    ]}}}
    sol.parsed_by_sig["sig-b-fund"] = {"transaction": {"message": {"instructions": [
        _system_transfer(funder_b, w_b),
    ]}}}
    sol.parsed_by_sig["sig-a-buy"] = _parsed_tx(_inflow(w_a, "500.0", mint))
    sol.parsed_by_sig["sig-b-buy"] = _parsed_tx(_inflow(w_b, "500.0", mint))

    # Completely different holdings
    sol.token_accounts_by_owner = {
        w_a: [_token_account(str(Pubkey.new_unique()))],
        w_b: [_token_account(str(Pubkey.new_unique()))],
    }

    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    result = await holder_intel.detect_clusters(None, mint)
    assert result["clusters"] == []


@pytest.mark.asyncio
async def test_detect_clusters_single_holder_no_cluster(monkeypatch, ctx):
    """A single holder should not produce any clusters."""
    sol = FakeSol()
    mint = str(Pubkey.new_unique())
    w_a = str(Pubkey.new_unique())

    sol.largest = [{"address": "acc1", "uiAmountString": "1000.0"}]
    sol.owners = {"acc1": w_a}

    monkeypatch.setattr(holder_intel, "get_solana_service", lambda: sol)

    result = await holder_intel.detect_clusters(None, mint)
    assert result["total_wallets_analyzed"] == 1
    assert result["clusters"] == []
