from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|authorization|cookie|api[_-]?key|credential)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


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


@dataclass(slots=True)
class SecureLogger:
    name: str
    base_context: dict[str, object] = field(default_factory=dict)

    def bind(self, **context: object) -> SecureLogger:
        return SecureLogger(self.name, {**self.base_context, **context})

    def info(self, event: str, **fields: object) -> None:
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: object) -> None:
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._log(logging.ERROR, event, fields)

    def _log(self, level: int, event: str, fields: dict[str, object]) -> None:
        payload: dict[str, Any] = {
            "event": event,
            **self.base_context,
            **fields,
        }
        logging.getLogger(self.name).log(
            level,
            json.dumps(redact(payload), sort_keys=True, separators=(",", ":")),
        )


def get_secure_logger(name: str, **context: object) -> SecureLogger:
    return SecureLogger(name, context)


__all__ = ["REDACTED", "SecureLogger", "get_secure_logger", "redact"]
