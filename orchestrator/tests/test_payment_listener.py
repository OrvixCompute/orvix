"""Tests for PaymentListener._apply_topup: atomic credit + idempotency.

These lock in the fix for the double-credit bug — crediting and recording the
ledger row now happen in one atomic RPC (credit_topup), so a signature that has
already been processed credits nothing on a re-run.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.payment_listener import PaymentListener
from tests.fakes import FakeSupabase


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def listener():
    return PaymentListener()


def _make_intent(db, user, *, expected=None) -> dict:
    return db._table("topup_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvx_abc123def456",
            "expected_amount_usdc": expected,
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )


async def test_apply_topup_credits_and_records(db, listener):
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user)

    await listener._apply_topup(db, intent, "sig-aaa", Decimal("50"))

    # Balance credited.
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1050.0)
    # Exactly one confirmed top-up ledger row for this signature.
    txs = db._table("transactions").rows
    assert len(txs) == 1
    assert txs[0]["type"] == "topup"
    assert txs[0]["status"] == "confirmed"
    assert txs[0]["solana_signature"] == "sig-aaa"
    # Intent marked fulfilled.
    updated_intent = db._table("topup_intents").rows[0]
    assert updated_intent["status"] == "fulfilled"
    assert updated_intent["fulfilled_at"] is not None


async def test_apply_topup_is_idempotent_on_signature(db, listener):
    """A duplicate signature must credit the balance only once."""
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user)

    await listener._apply_topup(db, intent, "sig-dup", Decimal("50"))
    # Re-process the very same signature (e.g. after a crash/restart).
    await listener._apply_topup(db, intent, "sig-dup", Decimal("50"))

    # Credited once, not twice.
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1050.0)
    # Only one ledger row exists for that signature.
    txs = [t for t in db._table("transactions").rows if t["solana_signature"] == "sig-dup"]
    assert len(txs) == 1


async def test_apply_topup_partial_when_below_expected(db, listener):
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user, expected=100.0)

    await listener._apply_topup(db, intent, "sig-partial", Decimal("40"))

    # Received less than expected -> intent flagged partial, full amount credited.
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1040.0)
    assert db._table("topup_intents").rows[0]["status"] == "partial"


async def test_apply_topup_fulfilled_when_meets_expected(db, listener):
    user = db.add_user(balance_usdc=1000.0)
    intent = _make_intent(db, user, expected=100.0)

    await listener._apply_topup(db, intent, "sig-full", Decimal("100"))

    assert db._table("topup_intents").rows[0]["status"] == "fulfilled"


# --- _process_signature routing --------------------------------------------


class _FakeSol:
    """Stands in for the Solana RPC service in _process_signature tests."""

    def __init__(self, memo, transfers):
        self._memo = memo
        self._transfers = transfers

    async def get_parsed_transaction(self, signature):
        return {"parsed": True}

    def extract_memo(self, parsed):
        return self._memo

    def extract_spl_transfers(self, parsed, mint, owner):
        return list(self._transfers)


def _patch_sol(monkeypatch, db, memo, transfers):
    # _process_signature fetches its own db via get_supabase() and the RPC service
    # via get_solana_service() — patch both to the fakes.
    monkeypatch.setattr("app.services.payment_listener.get_supabase", lambda: db)
    monkeypatch.setattr(
        "app.services.payment_listener.get_solana_service",
        lambda: _FakeSol(memo, transfers),
    )


async def test_process_signature_skips_already_seen(db, listener, monkeypatch):
    """A signature already in `transactions` must not be reprocessed or credited."""
    user = db.add_user(balance_usdc=1000.0)
    db._table("transactions").insert_row({"solana_signature": "sig-seen", "type": "topup"})
    _make_intent(db, user)  # a matching intent exists, but must be ignored
    _patch_sol(monkeypatch, db, "orvx_abc123def456", [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-seen")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_no_memo_no_credit(db, listener, monkeypatch):
    db.add_user(balance_usdc=1000.0)
    _patch_sol(monkeypatch, db, None, [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-nomemo")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_unmatched_intent_no_credit(db, listener, monkeypatch):
    db.add_user(balance_usdc=1000.0)
    # Memo has the right shape but no pending intent matches it.
    _patch_sol(monkeypatch, db, "orvx_doesnotexist", [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-nomatch")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_credits_on_matching_intent(db, listener, monkeypatch):
    user = db.add_user(balance_usdc=1000.0)
    _make_intent(db, user)  # memo "orvx_abc123def456"
    _patch_sol(monkeypatch, db, "orvx_abc123def456", [{"amount": "50", "destination": "tacc"}])
    listener._treasury_token_accounts = {"tacc"}

    await listener._process_signature("sig-good")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1050.0)
    assert db._table("topup_intents").rows[0]["status"] == "fulfilled"


async def test_process_signature_ignores_transfer_to_non_treasury_account(db, listener, monkeypatch):
    """A USDC transfer whose destination is not a treasury token account is dropped."""
    user = db.add_user(balance_usdc=1000.0)
    _make_intent(db, user)
    _patch_sol(monkeypatch, db, "orvx_abc123def456", [{"amount": "50", "destination": "somebody_else"}])
    listener._treasury_token_accounts = {"tacc"}  # transfer dest is NOT in here

    await listener._process_signature("sig-elsewhere")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)


async def test_process_signature_routes_stake_memo(db, listener, monkeypatch):
    """A stake memo credits staked_orvx (not USDC balance) via the stake path."""
    from app.config import settings

    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "OrvxMint1111111111111111111111111111111111")
    user = db.add_user(balance_usdc=1000.0)
    db._table("staking_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvix_stake_zzz999",
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )
    _patch_sol(monkeypatch, db, "orvix_stake_zzz999", [{"amount": "25000", "destination": "orvx_ata"}])
    listener._treasury_orvx_token_accounts = {"orvx_ata"}

    await listener._process_signature("sig-stake")

    # ORVX staked, USDC balance untouched.
    assert float(db._table("users").rows[0]["staked_orvx"]) == pytest.approx(25000.0)
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(1000.0)
    assert db._table("staking_intents").rows[0]["status"] == "fulfilled"


# --- attribution without a memo --------------------------------------------
#
# Before this, a deposit with no memo was logged and abandoned: the money sat in
# the treasury credited to nobody, and the depositor had no way to tell why.
# Users log in by signing with a Solana wallet, so a deposit sent from that same
# wallet identifies itself.


def _transfer(*, authority, amount="25", destination="treasury-ata"):
    return {
        "amount": amount,
        "ui_amount": float(amount),
        "source": "sender-ata",
        "destination": destination,
        "authority": authority,
    }


def test_signing_wallet_is_the_authority_not_the_token_account(listener):
    """`source` is the sender's ATA and matches no user; `authority` is the wallet."""
    wallet = listener._signing_wallet([_transfer(authority="WalletAbc")])
    assert wallet == "WalletAbc"


def test_signing_wallet_is_none_when_absent(listener):
    assert listener._signing_wallet([{"amount": "1", "source": "x"}]) is None
    assert listener._signing_wallet([]) is None


def test_user_lookup_by_wallet(db, listener):
    user = db.add_user(balance_usdc=0.0)
    wallet = user["wallet_address"]
    assert listener._user_id_for_wallet(db, wallet) == user["id"]
    assert listener._user_id_for_wallet(db, "NobodysWallet") is None


async def test_credit_without_an_intent_still_credits(db, listener):
    """The fallback path has no intent to point at; the ledger row takes a null."""
    user = db.add_user(balance_usdc=10.0)

    ok = await listener._credit(
        db, user["id"], "sig-no-memo", Decimal("25"), memo=None, intent=None
    )

    assert ok is True
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(35.0)
    tx = db._table("transactions").rows[0]
    assert tx["type"] == "topup"
    assert tx["solana_signature"] == "sig-no-memo"


async def test_credit_is_idempotent_without_an_intent(db, listener):
    """The signature constraint is the guard, not the intent."""
    user = db.add_user(balance_usdc=10.0)

    first = await listener._credit(
        db, user["id"], "sig-dup", Decimal("25"), memo=None, intent=None
    )
    second = await listener._credit(
        db, user["id"], "sig-dup", Decimal("25"), memo=None, intent=None
    )

    assert first is True
    assert second is False, "a re-run must credit nothing"
    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(35.0)


# --- _process_signature routing --------------------------------------------


class _FakeSol:
    """Stands in for the shared Solana client for one signature."""

    def __init__(self, memo, transfers):
        self._memo = memo
        self._transfers = transfers

    async def get_parsed_transaction(self, signature):
        return {"transaction": {"message": {"instructions": []}}, "meta": {}}

    def extract_memo(self, parsed):
        return self._memo

    def extract_spl_transfers(self, parsed, mint, owner):
        return self._transfers


@pytest.fixture
def wired(db, listener, monkeypatch):
    """Point the listener at the fake db and a stubbed chain."""
    import app.services.payment_listener as pl

    monkeypatch.setattr(pl, "get_supabase", lambda: db)
    monkeypatch.setattr(pl.settings, "USDC_MINT_ADDRESS", "UsdcMint111")
    monkeypatch.setattr(pl.settings, "TREASURY_WALLET_ADDRESS", "TreasuryWallet")

    def _wire(memo, transfers):
        monkeypatch.setattr(pl, "get_solana_service", lambda: _FakeSol(memo, transfers))
        # Matches the destination used by _transfer().
        listener._treasury_token_accounts = {"treasury-ata"}

    return _wire


async def test_deposit_without_a_memo_is_credited_to_the_sending_wallet(db, listener, wired):
    """The behaviour that was missing: no memo, money still lands."""
    user = db.add_user(balance_usdc=5.0)
    wired(None, [_transfer(authority=user["wallet_address"], amount="25")])

    await listener._process_signature("sig-sender-match")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(30.0)


async def test_expired_intent_falls_back_to_the_sending_wallet(db, listener, wired):
    """A memo whose 30-minute intent lapsed must not cost the depositor their money."""
    user = db.add_user(balance_usdc=5.0)
    db._table("topup_intents").insert_row(
        {
            "user_id": user["id"],
            "memo": "orvx_expired",
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
    )
    wired("orvx_expired", [_transfer(authority=user["wallet_address"], amount="25")])

    await listener._process_signature("sig-expired-intent")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(30.0)


async def test_memo_wins_over_the_sending_wallet(db, listener, wired):
    """An exchange withdrawal is signed by the exchange, so the memo must lead.

    Here the memo names one user and the signer is a different one; the memo's
    owner is the one credited.
    """
    payer = db.add_user(balance_usdc=0.0)
    signer = db.add_user(balance_usdc=0.0)
    _make_intent(db, payer)
    wired("orvx_abc123def456", [_transfer(authority=signer["wallet_address"], amount="25")])

    await listener._process_signature("sig-memo-wins")

    balances = {r["id"]: float(r["balance_usdc"]) for r in db._table("users").rows}
    assert balances[payer["id"]] == pytest.approx(25.0)
    assert balances[signer["id"]] == pytest.approx(0.0)


async def test_unknown_sender_without_a_memo_credits_nobody(db, listener, wired):
    """Still refuse to guess. An unattributable deposit must not land on someone."""
    db.add_user(balance_usdc=5.0)
    wired(None, [_transfer(authority="SomeStrangersWallet", amount="25")])

    await listener._process_signature("sig-stranger")

    assert float(db._table("users").rows[0]["balance_usdc"]) == pytest.approx(5.0)
    assert db._table("transactions").rows == []


# --- which addresses get polled --------------------------------------------
#
# Watching only the owner wallet is why no deposit was ever detected.
# `getSignaturesForAddress` returns transactions the address appears in, and an
# SPL transfer into an ATA names the ATA, the mint and the sender — never the
# ATA's owner. Confirmed against a real 0.11 USDC deposit that appeared under the
# treasury's USDC ATA and was completely absent from the wallet's history.


def test_token_accounts_are_watched_not_just_the_wallet(listener, monkeypatch):
    import app.services.payment_listener as pl

    monkeypatch.setattr(pl.settings, "TREASURY_WALLET_ADDRESS", "TreasuryWallet")
    listener._treasury_token_accounts = {"usdc-ata"}
    listener._treasury_orvx_token_accounts = {"orvx-ata"}

    watched = listener._watched_addresses()

    assert "usdc-ata" in watched, "the USDC ATA is where deposits actually land"
    assert "orvx-ata" in watched, "stake deposits land in the ORVX ATA"
    assert "TreasuryWallet" in watched, "still catches what the treasury itself signs"


def test_watched_addresses_are_deduped_and_skip_blanks(listener, monkeypatch):
    """Cursors are keyed by address, so a repeat would poll the same place twice."""
    import app.services.payment_listener as pl

    monkeypatch.setattr(pl.settings, "TREASURY_WALLET_ADDRESS", "same")
    listener._treasury_token_accounts = {"same"}
    listener._treasury_orvx_token_accounts = set()

    assert listener._watched_addresses() == ["same"]


async def test_each_address_keeps_its_own_cursor(db, listener, monkeypatch):
    """One shared cursor would let one address's newest signature hide another's."""
    import app.services.payment_listener as pl

    monkeypatch.setattr(pl, "get_supabase", lambda: db)
    monkeypatch.setattr(pl.settings, "TREASURY_WALLET_ADDRESS", "wallet")
    listener._treasury_token_accounts = {"ata"}

    seen = []

    class _Sol:
        async def get_signatures_for_address(self, address, limit=25, until=None):
            seen.append((address, until))
            return [{"signature": f"sig-{address}", "err": None}]

    monkeypatch.setattr(pl, "get_solana_service", lambda: _Sol())
    monkeypatch.setattr(listener, "_process_signature", lambda sig: _noop())

    await listener._poll_once()
    await listener._poll_once()

    # Second cycle must carry each address's own cursor, not a shared one.
    assert ("wallet", None) in seen and ("ata", None) in seen
    assert ("wallet", "sig-wallet") in seen
    assert ("ata", "sig-ata") in seen


async def _noop():
    return None
