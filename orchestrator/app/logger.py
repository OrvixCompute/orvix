"""Loguru configuration. Pretty output in dev, JSON in prod. Logs to stdout only."""

import sys

from loguru import logger

from app.config import settings


def configure_logging() -> None:
    """Set up loguru sinks. Call once on application startup."""
    logger.remove()  # drop the default handler

    if settings.is_prod:
        # Structured JSON for log aggregation in production. The enqueue sink
        # makes loguru thread-safe; serialize=True emits one JSON object per
        # line, including any contextualized `extra` fields (request_id etc).
        logger.add(
            sys.stdout,
            level=settings.LOG_LEVEL,
            serialize=True,
            backtrace=False,
            diagnose=False,
            enqueue=True,
        )
    else:
        # Human-friendly, colorized output for local development.
        logger.add(
            sys.stdout,
            level=settings.LOG_LEVEL,
            colorize=True,
            backtrace=True,
            diagnose=True,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{module}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
        )

    logger.debug("Logging configured (level={}, prod={})", settings.LOG_LEVEL, settings.is_prod)


def log_intel_scan(
    scan_type: str, target: str, *, cache_hit: bool, duration_ms: float
) -> None:
    """One structured line per token-intel scan.

    `scan_type`/`target` are the cache key parts so dashboards can group by
    type (token, wallet, accumulation, holders, ...) and see cache efficiency
    and per-type latency. Wallet scans cache under a composite key
    ("wallet" or "wallet:<mint>"); the mint suffix is stripped for the log.
    """
    if scan_type == "wallet":
        # "wallet" caches under "wallet" or "wallet:<mint>". Strip any mint
        # suffix from the *target* so the log groups by wallet; the plain key
        # stays intact. Note the parameter order: the cache key is the target.
        target = target.split(":", 1)[0]
    logger.info(
        "intel_scan scan_type={} target={} cache_hit={} duration_ms={:.1f}",
        scan_type,
        target,
        cache_hit,
        duration_ms,
    )


# Re-export so callers can `from app.logger import logger`.
__all__ = ["logger", "configure_logging", "log_intel_scan"]
