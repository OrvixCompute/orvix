"""Loguru configuration: stdout + a rotating file (10MB x 5)."""

import sys
from pathlib import Path

from loguru import logger

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{module}</cyan> | "
    "<level>{message}</level>"
)


def configure_logging(
    level: str = "INFO", log_file: Path | None = None, json_logs: bool = False
) -> None:
    """Set up sinks. Safe to call once at startup."""
    logger.remove()

    logger.add(
        sys.stdout,
        level=level,
        colorize=not json_logs,
        serialize=json_logs,
        format=_FORMAT,
    )

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_file),
            level=level,
            rotation="10 MB",
            retention=5,
            serialize=json_logs,
            format=_FORMAT,
            enqueue=True,  # safe across threads / async
        )

    logger.debug("Logging configured (level={}, file={})", level, log_file)


__all__ = ["logger", "configure_logging"]
