"""Parsing-edge-case tests for SolanaService.extract_memo / extract_spl_transfers.

These are pure static parsers over jsonParsed transactions — no RPC/network — and
are the attribution backbone of the payment listener, so the edge cases (missing /
malformed / wrong-shape memo, mint filtering, plain vs transferChecked) matter.
"""

from app.services.solana_service import SolanaService

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


def _tx(instructions, *, meta=None):
    return {
        "meta": meta or {"postTokenBalances": [], "preTokenBalances": []},
        "transaction": {"message": {"instructions": instructions}},
    }


# --- extract_memo -----------------------------------------------------------

def test_extract_memo_plain_string():
    tx = _tx([{"program": "spl-memo", "parsed": "orvx_abc123def456"}])
    assert SolanaService.extract_memo(tx) == "orvx_abc123def456"


def test_extract_memo_by_program_id_when_no_program_name():
    tx = _tx([{"programId": MEMO_PROGRAM, "parsed": "orvx_xyz"}])
    assert SolanaService.extract_memo(tx) == "orvx_xyz"


def test_extract_memo_dict_memo_key():
    tx = _tx([{"program": "spl-memo", "parsed": {"memo": "orvx_dict"}}])
    assert SolanaService.extract_memo(tx) == "orvx_dict"


def test_extract_memo_dict_info_key():
    tx = _tx([{"program": "spl-memo", "parsed": {"info": "orvx_info"}}])
    assert SolanaService.extract_memo(tx) == "orvx_info"


def test_extract_memo_absent_returns_none():
    tx = _tx([{"program": "spl-token", "parsed": {"type": "transfer", "info": {}}}])
    assert SolanaService.extract_memo(tx) is None


def test_extract_memo_empty_instruction_list():
    assert SolanaService.extract_memo(_tx([])) is None


def test_extract_memo_malformed_tx_returns_none():
    # Missing transaction/message/instructions entirely.
    assert SolanaService.extract_memo({}) is None
    assert SolanaService.extract_memo({"transaction": {}}) is None


# --- extract_spl_transfers --------------------------------------------------

def _checked(amount_ui, dest, *, mint=MINT, source="src", amt_str=None):
    return {
        "program": "spl-token",
        "parsed": {
            "type": "transferChecked",
            "info": {
                "mint": mint,
                "tokenAmount": {"uiAmount": amount_ui, "uiAmountString": amt_str or str(amount_ui)},
                "source": source,
                "destination": dest,
            },
        },
    }


def test_transfer_checked_matching_mint():
    tx = _tx([_checked(50.0, "treasury_ata")])
    out = SolanaService.extract_spl_transfers(tx, MINT, "owner")
    assert len(out) == 1
    assert out[0]["amount"] == "50.0"
    assert out[0]["destination"] == "treasury_ata"


def test_transfer_checked_other_mint_filtered_out():
    tx = _tx([_checked(50.0, "treasury_ata", mint="SoME0therMint1111111111111111111111111111111")])
    assert SolanaService.extract_spl_transfers(tx, MINT, "owner") == []


def test_plain_transfer_resolves_decimals_from_balances():
    # Plain transfer carries base-unit amount + no mint; decimals come from balances.
    tx = _tx(
        [{
            "program": "spl-token",
            "parsed": {"type": "transfer", "info": {"amount": "50000000", "source": "s", "destination": "d"}},
        }],
        meta={"postTokenBalances": [{"mint": MINT, "uiTokenAmount": {"decimals": 6}}]},
    )
    out = SolanaService.extract_spl_transfers(tx, MINT, "owner")
    assert len(out) == 1
    assert out[0]["amount"] == "50"  # 50_000_000 / 10**6
    assert out[0]["destination"] == "d"


def test_plain_transfer_without_decimals_is_skipped():
    # No matching mint in balances -> decimals unknown -> cannot attribute -> skip.
    tx = _tx(
        [{"program": "spl-token", "parsed": {"type": "transfer", "info": {"amount": "50000000", "destination": "d"}}}],
        meta={"postTokenBalances": []},
    )
    assert SolanaService.extract_spl_transfers(tx, MINT, "owner") == []


def test_non_transfer_instruction_ignored():
    tx = _tx([{"program": "spl-token", "parsed": {"type": "approve", "info": {}}}])
    assert SolanaService.extract_spl_transfers(tx, MINT, "owner") == []


def test_malformed_tx_returns_empty():
    assert SolanaService.extract_spl_transfers({}, MINT, "owner") == []
