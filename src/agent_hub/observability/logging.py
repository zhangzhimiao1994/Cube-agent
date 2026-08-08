from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TextIO

SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|authorization|cookie|api[_-]?key|credential)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"
_AGENT_HUB_HANDLER_MARKER = "_agent_hub_structured_handler"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if SENSITIVE_KEY_PATTERN.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_VALUE_PATTERN.sub(REDACTED, value)
    return value


class StructuredLogFormatter(logging.Formatter):
    """JSON formatter for both SecureLogger and ordinary stdlib logging."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        parsed = _parse_json_message(message)
        if parsed is None:
            payload["message"] = message
        else:
            payload.update(parsed)
            payload.setdefault("message", parsed.get("event", message))
        payload["level"] = record.levelname
        payload["logger"] = record.name
        return json.dumps(redact(payload), sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class SecureLogger:
    name: str
    base_context: dict[str, object] = field(default_factory=dict)

    def bind(self, **context: object) -> SecureLogger:
        return SecureLogger(self.name, {**self.base_context, **context})

    def debug(self, event: str, **fields: object) -> None:
        self._log(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: object) -> None:
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: object) -> None:
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._log(logging.ERROR, event, fields)

    def critical(self, event: str, **fields: object) -> None:
        self._log(logging.CRITICAL, event, fields)

    def _log(self, level: int, event: str, fields: dict[str, object]) -> None:
        level_name = logging.getLevelName(level)
        payload: dict[str, Any] = {
            "level": level_name if isinstance(level_name, str) else str(level),
            "logger": self.name,
            "event": event,
            **self.base_context,
            **fields,
        }
        logging.getLogger(self.name).log(
            level,
            json.dumps(redact(payload), sort_keys=True, separators=(",", ":")),
        )


def configure_logging(
    *,
    level: LogLevel | str = LogLevel.WARNING,
    stream: TextIO | None = None,
) -> None:
    """Configure process-wide JSON logs with a minimum severity level."""

    parsed_level = _normalize_level(level)
    handler = logging.StreamHandler(stream)
    setattr(handler, _AGENT_HUB_HANDLER_MARKER, True)
    handler.setFormatter(StructuredLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [
        item
        for item in root.handlers
        if not bool(getattr(item, _AGENT_HUB_HANDLER_MARKER, False))
    ]
    root.addHandler(handler)
    root.setLevel(parsed_level.value)


def get_secure_logger(name: str, **context: object) -> SecureLogger:
    return SecureLogger(name, context)


def _normalize_level(level: LogLevel | str) -> LogLevel:
    if isinstance(level, LogLevel):
        return level
    if type(level) is not str:
        raise ValueError("log level must be a string")
    try:
        return LogLevel(level.strip().upper())
    except ValueError:
        raise ValueError("log level is invalid") from None


def _parse_json_message(message: str) -> dict[str, object] | None:
    if not message.startswith("{"):
        return None
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}


__all__ = [
    "REDACTED",
    "LogLevel",
    "SecureLogger",
    "StructuredLogFormatter",
    "configure_logging",
    "get_secure_logger",
    "redact",
]
