"""structlog configuration — JSON in production, console renderer for humans."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from smtp_switch.config import LoggingConfig

_configured = False


def configure_logging(cfg: LoggingConfig) -> None:
    global _configured
    level = getattr(logging, cfg.level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    # Quiet a couple of chatty libraries (aiosmtpd logs the full SMTP dialogue
    # at INFO); only surface it when we are explicitly debugging.
    noisy_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    logging.getLogger("aiosmtpd").setLevel(noisy_level)
    logging.getLogger("mail.log").setLevel(noisy_level)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if cfg.json_format
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging(LoggingConfig())
    return structlog.get_logger(name)
