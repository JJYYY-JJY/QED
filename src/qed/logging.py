"""Structured process logging with conservative secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import TextIO, cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "auth_token",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "set_cookie",
        "token",
    }
)
_SECRET_SUFFIXES = ("_api_key", "_credential", "_password", "_secret", "_token")
_REDACTED = "[redacted]"
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def _secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


def _redact_value(value: object, *, key: str | None = None) -> object:
    if key is not None and _secret_key(key):
        return _REDACTED
    if isinstance(value, str):
        return _BEARER_VALUE.sub(f"Bearer {_REDACTED}", value)
    if isinstance(value, Mapping):
        return {
            str(nested_key): _redact_value(nested_value, key=str(nested_key))
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _redact_secrets(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    return {
        key: _redact_value(value, key=key)
        for key, value in event_dict.items()
    }


if not structlog.is_configured():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.format_exc_info,
            _redact_secrets,
            structlog.stdlib.render_to_log_kwargs,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog and stdlib logging as one redacting pipeline."""

    normalized_level = level.upper()
    numeric_level = logging.getLevelNamesMapping().get(normalized_level)
    if numeric_level is None:
        raise ValueError(f"unknown log level: {level}")

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_secrets,
    ]
    renderer: Processor
    if json_output:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared,
    )
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a logger that uses the configured structured pipeline."""

    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
