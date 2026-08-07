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


# --- RPC option shape -------------------------------------------------------


def test_send_raw_transaction_preflight_matches_blockhash_commitment():
    """Preflight must simulate at the same commitment the blockhash came from.

    get_latest_blockhash() asks for "confirmed". sendTransaction's preflight
    defaults to "finalized", which trails it by a slot or two, so the simulating
    node has not seen that blockhash yet and rejects the transaction with
    BlockhashNotFound — observed on mainnet while creating the treasury ATAs.
    Every signed transaction goes through here, payouts included.
    """
    import asyncio

    captured = {}

    class _Svc(SolanaService):
        def __init__(self):  # bypass the httpx client setup
            pass

        async def _rpc(self, method, params):
            captured["method"] = method
            captured["params"] = params
            return "sig"

    svc = _Svc()
    asyncio.run(svc.send_raw_transaction("dGVzdA=="))

    assert captured["method"] == "sendTransaction"
    opts = captured["params"][1]
    assert opts["preflightCommitment"] == "confirmed"

    # Pin the pairing itself: whatever commitment the blockhash is fetched at,
    # preflight has to match it, or this bug comes straight back.
    import inspect

    src = inspect.getsource(SolanaService.get_latest_blockhash)
    assert '"commitment": "confirmed"' in src


def test_get_parsed_transaction_uses_a_real_rpc_method():
    """`getParsedTransaction` is a web3.js client helper, not a JSON-RPC method.

    Sending it to a node returns -32601 Method not found, so the payment
    listener discovered every incoming deposit and then failed to read a single
    one — silently, as a per-signature warning. The RPC method is
    `getTransaction`; parsing is requested via the encoding option.
    """
    import asyncio

    captured = {}

    class _Svc(SolanaService):
        def __init__(self):  # bypass the httpx client setup
            pass

        async def _rpc(self, method, params):
            captured["method"] = method
            captured["params"] = params
            return {}

    asyncio.run(_Svc().get_parsed_transaction("sig123"))

    assert captured["method"] == "getTransaction"
    sig, opts = captured["params"]
    assert sig == "sig123"
    # Parsing and version support still have to be requested, or the listener
    # gets raw binary it cannot read, or chokes on versioned transactions.
    assert opts["encoding"] == "jsonParsed"
    assert opts["maxSupportedTransactionVersion"] == 0
