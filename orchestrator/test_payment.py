"""Exercise the top-up / payment flow (Prompt 6).

Two modes:

  1. Intent only (default) — authenticate, create a top-up intent, and print the
     memo + treasury address + QR string so you can pay manually from Phantom.

        python test_payment.py

  2. Simulate the on-chain send — actually transfer a test SPL token to the
     treasury with the intent's memo, then poll until the balance is credited.
     Requires `pip install solana spl-token` and a funded sender keypair.

        python test_payment.py --send \
            --sender-secret "<base58 secret key>" \
            --mint <TEST_MINT_ADDRESS> \
            --amount 10

NOTE: Since USDC itself lives on pump.fun, for local testing point
USDC_MINT_ADDRESS (in the server's .env) at a devnet/test SPL mint you control,
and pass that same mint here. The payment listener must be running
(ENABLE_PAYMENT_LISTENER=true).
"""

import argparse
import sys
import time

import httpx
from solders.keypair import Keypair

BASE_URL = "http://localhost:8000"


def authenticate(client: httpx.Client) -> tuple[str, str]:
    """Run the wallet auth flow with a throwaway keypair. Returns (jwt, wallet)."""
    kp = Keypair()
    wallet = str(kp.pubkey())
    r = client.get("/v1/auth/challenge", params={"wallet": wallet})
    r.raise_for_status()
    message = r.json()["message"]
    sig = str(kp.sign_message(message.encode("utf-8")))
    r = client.post(
        "/v1/auth/verify",
        json={"wallet": wallet, "message": message, "signature": sig},
    )
    r.raise_for_status()
    return r.json()["token"], wallet


def create_intent(client: httpx.Client, token: str, amount: float | None) -> dict:
    body = {"expected_amount": amount} if amount else {}
    r = client.post(
        "/v1/billing/topup-intent",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    r.raise_for_status()
    return r.json()


def get_balance(client: httpx.Client, token: str) -> str:
    r = client.get(
        "/v1/billing/balance", headers={"Authorization": f"Bearer {token}"}
    )
    r.raise_for_status()
    return r.json()["balance_usdc"]


def simulate_send(sender_secret: str, mint: str, treasury: str, amount: float, memo: str) -> str:
    """Build and submit an SPL transfer + memo to the treasury. Returns signature.

    Lazily imports solana-py so the main app doesn't depend on it.
    """
    try:
        from solana.rpc.api import Client as SolClient
        from solders.keypair import Keypair as SKeypair
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.message import Message
        from solders.transaction import Transaction
        from spl.token.instructions import (
            transfer_checked,
            TransferCheckedParams,
            get_associated_token_address,
        )
        from spl.token.constants import TOKEN_PROGRAM_ID
    except ImportError:
        print("This mode needs: pip install solana spl-token", file=sys.stderr)
        raise

    rpc = SolClient("https://api.devnet.solana.com")
    sender = SKeypair.from_base58_string(sender_secret)
    mint_pk = Pubkey.from_string(mint)
    treasury_pk = Pubkey.from_string(treasury)

    src_ata = get_associated_token_address(sender.pubkey(), mint_pk)
    dst_ata = get_associated_token_address(treasury_pk, mint_pk)

    # Resolve mint decimals.
    decimals = rpc.get_token_supply(mint_pk).value.decimals
    raw_amount = int(amount * (10 ** decimals))

    transfer_ix = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=src_ata,
            mint=mint_pk,
            dest=dst_ata,
            owner=sender.pubkey(),
            amount=raw_amount,
            decimals=decimals,
            signers=[],
        )
    )

    # SPL Memo program instruction.
    memo_program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    memo_ix = Instruction(
        program_id=memo_program,
        accounts=[AccountMeta(pubkey=sender.pubkey(), is_signer=True, is_writable=False)],
        data=memo.encode("utf-8"),
    )

    blockhash = rpc.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([transfer_ix, memo_ix], sender.pubkey(), blockhash)
    tx = Transaction([sender], msg, blockhash)
    sig = rpc.send_transaction(tx).value
    print(f"Sent transaction: {sig}")
    return str(sig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Orvix payment flow tester")
    parser.add_argument("--send", action="store_true", help="Actually submit an on-chain transfer")
    parser.add_argument("--sender-secret", help="base58 secret key of the funded sender")
    parser.add_argument("--mint", help="SPL mint address (test token)")
    parser.add_argument("--amount", type=float, default=10.0, help="Amount to send / expect")
    parser.add_argument("--poll-seconds", type=int, default=120, help="How long to poll for credit")
    args = parser.parse_args()

    with httpx.Client(base_url=BASE_URL, timeout=20.0) as client:
        token, wallet = authenticate(client)
        print(f"Authenticated as {wallet}")

        intent = create_intent(client, token, args.amount)
        print("\nTop-up intent created:")
        print(f"  memo:     {intent['memo']}")
        print(f"  treasury: {intent['treasury_address']}")
        print(f"  expires:  {intent['expires_at']}")
        print(f"  qr_data:  {intent['qr_data']}")

        start_balance = get_balance(client, token)
        print(f"\nStarting balance: {start_balance} USDC")

        if not args.send:
            print(
                "\nNow send the test token to the treasury WITH the memo above "
                "(Phantom: add memo when sending).\nRe-run with --send to automate it."
            )
            return 0

        if not (args.sender_secret and args.mint):
            print("--send requires --sender-secret and --mint", file=sys.stderr)
            return 2

        simulate_send(
            args.sender_secret, args.mint, intent["treasury_address"], args.amount, intent["memo"]
        )

        print("\nPolling for the listener to credit the balance...")
        deadline = time.time() + args.poll_seconds
        while time.time() < deadline:
            bal = get_balance(client, token)
            if bal != start_balance:
                print(f"\nBalance credited: {bal} USDC ✅")
                return 0
            print(".", end="", flush=True)
            time.sleep(5)
        print("\nTimed out waiting for credit. Check the server logs.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
