"""
Structured logging configuration.

Outputs JSON in production (grep/splunk/datadog friendly),
human-readable colored output in development.

Usage:
    from app.core.logging import get_logger
    log = get_logger(__name__)
    log.info("reconciliation_complete", run_id=run_id, matched=245, unmatched=3)
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.types import EventDict, Processor


def add_app_context(
    logger: logging.Logger, method: str, event_dict: EventDict
) -> EventDict:
    """Inject static context into every log line."""
    from app.config import get_settings

    settings = get_settings()
    event_dict["app"] = settings.APP_NAME
    event_dict["env"] = settings.ENVIRONMENT
    event_dict["version"] = settings.APP_VERSION
    return event_dict


def drop_color_message_key(
    logger: logging.Logger, method: str, event_dict: EventDict
) -> EventDict:
    """Remove uvicorn's color_message to avoid duplicate log lines."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(log_level: str = "INFO", use_json: bool = True) -> None:
    """
    Configure structlog + stdlib logging to work together.
    Call once at application startup.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        add_app_context,
        drop_color_message_key,
    ]

    if use_json:
        # Production: machine-parseable JSON
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        # Development: colored, human-readable
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Quiet noisy libraries
    for noisy in ["uvicorn.access", "sqlalchemy.engine", "apscheduler"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named structured logger."""
    return structlog.get_logger(name)
