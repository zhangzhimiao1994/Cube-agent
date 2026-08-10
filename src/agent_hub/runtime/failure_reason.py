"""Shared redacted runtime failure diagnostics."""

from __future__ import annotations

import re

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import ModelGatewayError
from agent_hub.models.litellm_client import ModelResponseError, ModelTransportError
from agent_hub.models.registry import NoCapableDeployment

MAX_FAILURE_REASON_LENGTH = 240
SENSITIVE_FAILURE_REASON = re.compile(
    r"(api[_-]?key|authorization|bearer|credential|password|secret|(?:access|refresh|session)[_-]?token|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)
GENERIC_MODEL_GATEWAY_FAILURE = "model gateway failed"
LEGACY_GENERIC_FAILURES = frozenset(
    {
        "model gateway failed",
        "discussion_failed",
        "dispatch execution failed",
        "step execution failed",
        "runtime_failed",
    }
)
SAFE_MODEL_GATEWAY_FAILURES = frozenset(
    {
        "model capacity unavailable",
        "model transport failed",
        "model outcome recording failed",
        "model capacity release failed",
        "model gateway completed without a response",
    }
)


def safe_model_gateway_failure_reason(error: Exception) -> str | None:
    """Return a stable model-gateway diagnostic that is safe to show in the UI."""

    if isinstance(error, ModelTransportError):
        base = (
            "model response failed"
            if isinstance(error, ModelResponseError)
            else "model transport failed"
        )
        if error.status_code is not None:
            return f"{GENERIC_MODEL_GATEWAY_FAILURE}: {base} (status={error.status_code})"
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: {base}"
    if isinstance(error, NoCapableDeployment):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: no capable deployment"
    if isinstance(error, CapacityQueueFull):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity queue is full"
    if isinstance(error, CapacityWaitTimeout):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity queue timeout"
    if isinstance(error, CapacityUnavailable):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity unavailable"
    if isinstance(error, CapacityConfigurationError):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity configuration failed"
    if isinstance(error, CapacityBackendError):
        return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model capacity backend failed"
    if isinstance(error, ModelGatewayError):
        message = normalize_failure_reason(str(error))
        if message == "model credential resolution failed":
            return f"{GENERIC_MODEL_GATEWAY_FAILURE}: model configuration failed"
        if message in SAFE_MODEL_GATEWAY_FAILURES:
            return f"{GENERIC_MODEL_GATEWAY_FAILURE}: {message}"
    return None


def safe_runtime_failure_reason(error: Exception, *, fallback: str = "runtime_failed") -> str:
    gateway_reason = safe_model_gateway_failure_reason(error)
    if gateway_reason is not None:
        return gateway_reason
    reason = normalize_failure_reason(str(error))
    if not is_safe_failure_reason(reason):
        return fallback
    return reason[:MAX_FAILURE_REASON_LENGTH]


def normalize_failure_reason(reason: str) -> str:
    return " ".join(reason.strip().split())


def is_safe_failure_reason(reason: str) -> bool:
    return bool(reason) and SENSITIVE_FAILURE_REASON.search(reason) is None


def is_legacy_generic_failure_reason(reason: str | None) -> bool:
    if reason is None:
        return False
    return normalize_failure_reason(reason) in LEGACY_GENERIC_FAILURES


__all__ = [
    "GENERIC_MODEL_GATEWAY_FAILURE",
    "MAX_FAILURE_REASON_LENGTH",
    "SENSITIVE_FAILURE_REASON",
    "is_legacy_generic_failure_reason",
    "is_safe_failure_reason",
    "normalize_failure_reason",
    "safe_model_gateway_failure_reason",
    "safe_runtime_failure_reason",
]
