"""Loguru configuration. Pretty output in dev, JSON in prod. Logs to stdout only."""

import sys

from loguru import logger

from app.config import settings


def configure_logging() -> None:
    """Set up loguru sinks. Call once on application startup."""
    logger.remove()  # drop the default handler

    if settings.is_prod:
        # Structured JSON for log aggregation in production.
        logger.add(
            sys.stdout,
            level=settings.LOG_LEVEL,
            serialize=True,
            backtrace=False,
            diagnose=False,
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


# Re-export so callers can `from app.logger import logger`.
__all__ = ["logger", "configure_logging"]
