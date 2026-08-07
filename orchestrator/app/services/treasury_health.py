"""Live treasury balance check with thresholds.

Unlike `payments_overview`, which reports the last values synced into
`treasury_wallets`, this reads the chain directly. A monitor that can only see
a stale row is not a monitor — the failure it exists to catch is the balance
falling while nobody is looking.

Both wallets are checked for SOL as well as tokens, because SOL is what
actually strands a payout: the payout signer pays the transaction fee AND the
destination's ATA rent, so it can run dry while still holding plenty of USDC.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import settings
from app.logger import logger
from app.services.solana_service import get_solana_service
from app.services.wallet import wallet_service

# Severity ordering, worst first — callers rank on this.
CRITICAL = "critical"
WARNING = "warning"


def _alert(severity: str, wallet: str, asset: str, balance: Decimal, threshold: float, note: str) -> dict:
    return {
        "severity": severity,
        "wallet": wallet,
        "asset": asset,
        "balance": float(balance),
        "threshold": threshold,
        "message": note,
    }


async def check() -> dict:
    """Read live balances and evaluate them against the configured floors.

    Never raises for a missing wallet: an unconfigured treasury is reported as
    an alert rather than an exception, so a monitoring timer keeps running and
    keeps saying what is wrong.
    """
    # Do NOT close this client. `get_solana_service()` hands back a process-wide
    # singleton shared with the payment listener and the payout worker. Closing
    # it here — the natural shape, and the one the one-shot scripts use — poisons
    # the httpx client for the whole orchestrator: the first call to this
    # endpoint returns 200 and every RPC after it dies with "Cannot send a
    # request, as the client has been closed". A monitoring endpoint that breaks
    # payouts on its first call is worse than no monitoring. The CLI script
    # exits after one check, so process teardown closes it there.
    sol = get_solana_service()
    wallets: dict[str, dict] = {}
    alerts: list[dict] = []

    for role in ("payout", "hot"):
        pub = wallet_service.public_key(role)
        if not pub:
            alerts.append(
                _alert(CRITICAL, role, "-", Decimal(0), 0.0, f"{role} wallet is not configured")
            )
            wallets[role] = {"public_key": None, "sol": None, "usdc": None}
            continue

        sol_balance = await sol.get_sol_balance(pub)
        usdc_balance = (
            await sol.get_token_balance(pub, settings.USDC_MINT_ADDRESS)
            if settings.USDC_MINT_ADDRESS
            else Decimal(0)
        )
        wallets[role] = {
            "public_key": pub,
            "sol": float(sol_balance),
            "usdc": float(usdc_balance),
        }

        min_sol = (
            settings.TREASURY_MIN_PAYOUT_SOL if role == "payout" else settings.TREASURY_MIN_HOT_SOL
        )
        if sol_balance < Decimal(str(min_sol)):
            # Out of SOL is worse than out of USDC: without it the payout cannot
            # even be broadcast, so every withdrawal fails and refunds rather
            # than queueing until funds arrive.
            alerts.append(
                _alert(
                    CRITICAL,
                    role,
                    "SOL",
                    sol_balance,
                    min_sol,
                    f"{role} SOL below floor — transactions from this wallet will fail",
                )
            )

        if role == "payout" and usdc_balance < Decimal(str(settings.TREASURY_MIN_PAYOUT_USDC)):
            alerts.append(
                _alert(
                    WARNING,
                    role,
                    "USDC",
                    usdc_balance,
                    settings.TREASURY_MIN_PAYOUT_USDC,
                    "payout float low — provider withdrawals will start failing on insufficient funds",
                )
            )

    alerts.sort(key=lambda a: 0 if a["severity"] == CRITICAL else 1)
    for a in alerts:
        logger.warning("treasury-health {}: {}", a["severity"], a["message"])

    return {
        "ok": not alerts,
        "wallets": wallets,
        "alerts": alerts,
        # Echoed so a reader can tell a quiet result from an unconfigured one.
        "thresholds": {
            "payout_min_sol": settings.TREASURY_MIN_PAYOUT_SOL,
            "payout_min_usdc": settings.TREASURY_MIN_PAYOUT_USDC,
            "hot_min_sol": settings.TREASURY_MIN_HOT_SOL,
        },
    }
