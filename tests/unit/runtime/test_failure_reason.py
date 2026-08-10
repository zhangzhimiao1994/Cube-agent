from __future__ import annotations

import pytest

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
from agent_hub.runtime.failure_reason import safe_runtime_failure_reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            ModelTransportError("Authorization: Bearer sk-secret", status_code=401),
            "model gateway failed: model transport failed (status=401)",
        ),
        (
            ModelResponseError("bad provider shape", status_code=502),
            "model gateway failed: model response failed (status=502)",
        ),
        (
            ModelGatewayError("model credential resolution failed"),
            "model gateway failed: model configuration failed",
        ),
        (
            NoCapableDeployment("no capable deployment for logical model 'qwen_1'"),
            "model gateway failed: no capable deployment",
        ),
        (CapacityQueueFull("model capacity queue is full"), "model gateway failed: model capacity queue is full"),
        (
            CapacityWaitTimeout("model capacity queue timeout"),
            "model gateway failed: model capacity queue timeout",
        ),
        (CapacityUnavailable("model capacity unavailable"), "model gateway failed: model capacity unavailable"),
        (
            CapacityConfigurationError("raw redis secret"),
            "model gateway failed: model capacity configuration failed",
        ),
        (
            CapacityBackendError("redis password leaked"),
            "model gateway failed: model capacity backend failed",
        ),
    ],
)
def test_safe_runtime_failure_reason_preserves_diagnostic_without_secrets(
    error: Exception, reason: str
) -> None:
    assert safe_runtime_failure_reason(error) == reason


def test_safe_runtime_failure_reason_redacts_unknown_sensitive_errors() -> None:
    assert (
        safe_runtime_failure_reason(RuntimeError("Authorization: Bearer sk-secret failed"))
        == "runtime_failed"
    )
