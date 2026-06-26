"""Admin CLI for the monthly ORVX burn.

Run on the VPS as the orchestrator service user (it reads the same .env).

Usage:
    python scripts/burn.py status
    python scripts/burn.py execute [--amount 5000]
        [--period-start YYYY-MM-DD] [--period-end YYYY-MM-DD] [--yes]

`execute` sends ORVX to the incinerator and records it (record_burn RPC),
decrementing "held for burn". No --amount burns ALL held ORVX; no period
defaults to the previous calendar month. With BURN_STUB=true (default) the
transfer is simulated — flip BURN_STUB=false only after devnet testing.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.services.burn_service import BurnService  # noqa: E402


def _print(d: dict) -> None:
    for k, v in d.items():
        print(f"  {k}: {v}")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


async def _run(args: argparse.Namespace) -> int:
    svc = BurnService()

    if args.command == "status":
        print("Burn status:")
        _print(svc.status())
        return 0

    if args.command == "execute":
        amount = Decimal(str(args.amount)) if args.amount is not None else None
        label = f"{amount} ORVX" if amount is not None else "ALL held ORVX"
        print(f"About to burn {label} (stub={settings.BURN_STUB}).")
        if not args.yes:
            if input("Proceed? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1
        executor = settings.TREASURY_WALLET_ADDRESS or "admin-cli"
        result = await svc.execute(
            amount, _parse_date(args.period_start), _parse_date(args.period_end), executor
        )
        print("Burn complete:")
        _print(result)
        sig = result["solana_signature"]
        print(f"  solscan: https://solscan.io/tx/{sig}")
        return 0

    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Orvix monthly burn")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show ORVX held for burn, last burn, total burned")

    p_exec = sub.add_parser("execute", help="Execute a burn")
    p_exec.add_argument("--amount", type=float, default=None, help="ORVX to burn (default: all held)")
    p_exec.add_argument("--period-start", type=str, default=None, help="YYYY-MM-DD")
    p_exec.add_argument("--period-end", type=str, default=None, help="YYYY-MM-DD")
    p_exec.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
