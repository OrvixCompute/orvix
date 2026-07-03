"""Wire-format tests for the pure-solders SPL instruction builders."""

from solders.pubkey import Pubkey

from app.services import spl

MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
OWNER = Pubkey.from_string("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9")


def test_associated_token_address_deterministic():
    a = spl.associated_token_address(OWNER, MINT)
    b = spl.associated_token_address(OWNER, MINT)
    assert isinstance(a, Pubkey)
    assert a == b
    # Different owner -> different ATA.
    assert spl.associated_token_address(MINT, MINT) != a


def test_create_idempotent_ata_ix_shape():
    payer = OWNER
    ix = spl.create_idempotent_ata_ix(payer, OWNER, MINT)
    assert ix.program_id == spl.ASSOCIATED_TOKEN_PROGRAM_ID
    assert bytes(ix.data) == bytes([1])  # CreateIdempotent discriminator
    assert len(ix.accounts) == 6
    # payer is signer+writable; ata (index 1) is writable.
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.accounts[1].pubkey == spl.associated_token_address(OWNER, MINT)
    assert ix.accounts[1].is_writable
    # program refs at the tail.
    assert ix.accounts[4].pubkey == spl.SYSTEM_PROGRAM_ID
    assert ix.accounts[5].pubkey == spl.TOKEN_PROGRAM_ID


def test_transfer_checked_ix_encoding():
    source = spl.associated_token_address(OWNER, MINT)
    dest = spl.associated_token_address(MINT, MINT)
    ix = spl.transfer_checked_ix(
        source_ata=source, mint=MINT, dest_ata=dest, owner=OWNER,
        amount_raw=50_000_000, decimals=6,
    )
    assert ix.program_id == spl.TOKEN_PROGRAM_ID
    data = bytes(ix.data)
    assert data[0] == 12  # transferChecked discriminator
    assert int.from_bytes(data[1:9], "little") == 50_000_000
    assert data[9] == 6
    assert len(data) == 10
    # accounts: source(w), mint(ro), dest(w), owner(signer)
    assert [m.is_writable for m in ix.accounts] == [True, False, True, False]
    assert ix.accounts[3].pubkey == OWNER
    assert ix.accounts[3].is_signer
