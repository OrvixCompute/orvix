"""Hot-wallet sweeper: move USDC above a threshold from the hot wallet to cold main.

Keeps the hot (deposit-receiver) wallet's balance small so a hot-key compromise
caps the loss. Run daily via the systemd timer (scripts/sweep_hot.py). The actual
transfer is stubbed unless TREASURY_SWEEP_STUB=false.
"""

from decimal import Decimal

from app.config import settings
from app.logger import logger
from app.services.wallet import wallet_service


class HotSweeper:
    async def run_once(self, db=None) -> dict:
        """Sweep hot->main if the hot balance is above the threshold. Idempotent-ish:
        safe to run repeatedly (each run only sweeps the current excess)."""
        hot = await wallet_service.get_usdc_balance("hot")
        threshold = Decimal(str(settings.HOT_SWEEP_THRESHOLD_USDC))
        keep = Decimal(str(settings.HOT_SWEEP_MIN_KEEP_USDC))

        if hot <= threshold:
            logger.info("Hot sweep skipped: balance {} <= threshold {}", hot, threshold)
            return {"swept": False, "reason": "below_threshold", "hot_balance_usdc": float(hot)}

        main_pub = wallet_service.public_key("main")
        if not main_pub:
            logger.warning("Hot sweep skipped: TREASURY_MAIN_PUBLIC not configured")
            return {"swept": False, "reason": "no_main_wallet", "hot_balance_usdc": float(hot)}

        amount = hot - keep
        if amount <= 0:
            return {"swept": False, "reason": "nothing_above_keep", "hot_balance_usdc": float(hot)}

        sig = await wallet_service.send_usdc(
            "hot", main_pub, amount, stub=settings.TREASURY_SWEEP_STUB
        )
        logger.info(
            "Hot sweep: {} USDC hot->main (stub={}, sig={})",
            amount,
            settings.TREASURY_SWEEP_STUB,
            sig,
        )

        if db is not None:
            try:
                await wallet_service.sync_balances(db)
            except Exception as exc:  # noqa: BLE001 — balance resync is best-effort
                logger.warning("Post-sweep balance sync failed: {}", exc)

        return {
            "swept": True,
            "amount_usdc": float(amount),
            "signature": sig,
            "stub": settings.TREASURY_SWEEP_STUB,
            "hot_balance_usdc": float(hot),
        }


hot_sweeper = HotSweeper()
