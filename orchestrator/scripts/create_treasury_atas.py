"""Create the USDC associated token accounts for the hot + payout wallets.

Idempotent (uses the ATA program's CreateIdempotent) — safe to run repeatedly.
The HOT wallet pays the rent for both ATAs, so run it after the hot keypair is on
the VPS and .env has USDC_MINT_ADDRESS (+ PAYOUT_WALLET_PUBLIC).

Run (from /opt/orvix/orchestrator, .env in CWD):
  python scripts/create_treasury_atas.py
"""

import asyncio
import base64
import sys


def main() -> int:
    from solders.hash import Hash
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    from app.config import settings
    from app.services import spl
    from app.services.solana_service import get_solana_service
    from app.services.wallet import wallet_service

    if not settings.USDC_MINT_ADDRESS:
        print("USDC_MINT_ADDRESS not configured", file=sys.stderr)
        return 1

    hot_kp = wallet_service.get_keypair("hot")
    mint = Pubkey.from_string(settings.USDC_MINT_ADDRESS)

    owners = {"hot": hot_kp.pubkey()}
    if settings.PAYOUT_WALLET_PUBLIC:
        owners["payout"] = Pubkey.from_string(settings.PAYOUT_WALLET_PUBLIC)
    else:
        print("PAYOUT_WALLET_PUBLIC not set — creating the hot ATA only.", file=sys.stderr)

    ixs = [spl.create_idempotent_ata_ix(hot_kp.pubkey(), owner, mint) for owner in owners.values()]

    async def _run() -> str:
        sol = get_solana_service()
        try:
            blockhash = Hash.from_string(await sol.get_latest_blockhash())
            tx = Transaction.new_signed_with_payer(ixs, hot_kp.pubkey(), [hot_kp], blockhash)
            return await sol.send_raw_transaction(base64.b64encode(bytes(tx)).decode())
        finally:
            await sol.close()

    try:
        sig = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"ATA creation FAILED: {exc}", file=sys.stderr)
        return 1

    for role, owner in owners.items():
        print(f"{role:7} USDC ATA = {spl.associated_token_address(owner, mint)}")
    print(f"tx = {sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
