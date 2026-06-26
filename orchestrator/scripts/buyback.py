"""Admin CLI for the manual ORVX buyback engine.

Run on the VPS as the orchestrator service user (it reads the same .env).

Usage:
    python scripts/buyback.py status
    python scripts/buyback.py preview --amount-usdc 100 [--slippage-bps 50]
    python scripts/buyback.py execute --amount-usdc 100 [--slippage-bps 50] [--yes]

`execute` swaps USDC -> ORVX via Jupiter and records it (record_buyback RPC),
moving the spent budget into "held for burn". With BUYBACK_STUB=true (default)
the swap is simulated — flip BUYBACK_STUB=false only after devnet testing and
once the treasury keypair is configured.
"""

import argparse
import asyncio
import os
import sys
from decimal import Decimal

# Make the orchestrator package importable when run as `python scripts/buyback.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.services.buyback_service import BuybackService  # noqa: E402


def _print(d: dict) -> None:
    for k, v in d.items():
        print(f"  {k}: {v}")


async def _run(args: argparse.Namespace) -> int:
    svc = BuybackService()

    if args.command == "status":
        print("Buyback status:")
        _print(svc.status())
        return 0

    if args.command == "preview":
        print(f"Preview: spending {args.amount_usdc} USDC")
        _print(await svc.preview(Decimal(str(args.amount_usdc)), args.slippage_bps))
        return 0

    if args.command == "execute":
        amount = Decimal(str(args.amount_usdc))
        print(f"About to spend {amount} USDC on an ORVX buyback "
              f"(slippage {args.slippage_bps} bps, stub={settings.BUYBACK_STUB}).")
        if not args.yes:
            if input("Proceed? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1
        executor = settings.TREASURY_WALLET_ADDRESS or "admin-cli"
        result = await svc.execute(amount, args.slippage_bps, executor)
        print("Buyback complete:")
        _print(result)
        return 0

    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Orvix manual buyback engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show buyback budget, last buyback, ORVX held for burn")

    p_prev = sub.add_parser("preview", help="Preview a buyback quote (no execution)")
    p_prev.add_argument("--amount-usdc", required=True, type=float)
    p_prev.add_argument("--slippage-bps", type=int, default=50)

    p_exec = sub.add_parser("execute", help="Execute a buyback")
    p_exec.add_argument("--amount-usdc", required=True, type=float)
    p_exec.add_argument("--slippage-bps", type=int, default=50)
    p_exec.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
