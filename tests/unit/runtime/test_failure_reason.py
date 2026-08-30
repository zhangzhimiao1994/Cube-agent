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
from agent_hub.runtime.failure_reason import (
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_diagnostic,
    safe_runtime_failure_reason,
)


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
        (
            CapacityQueueFull("model capacity queue is full"),
            "model gateway failed: model capacity queue is full",
        ),
        (
            CapacityWaitTimeout("model capacity queue timeout"),
            "model gateway failed: model capacity queue timeout",
        ),
        (
            CapacityUnavailable("model capacity unavailable"),
            "model gateway failed: model capacity unavailable",
        ),
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


def test_safe_runtime_failure_reason_keeps_non_secret_token_diagnostics() -> None:
    assert (
        safe_runtime_failure_reason(RuntimeError("max_tokens exceeded provider limit"))
        == "max_tokens exceeded provider limit"
    )


def test_safe_runtime_failure_diagnostic_classifies_provider_auth_failure() -> None:
    diagnostic = safe_runtime_failure_diagnostic(
        ModelTransportError("Authorization: Bearer sk-secret", status_code=401)
    )

    assert (
        diagnostic["error_summary"] == "model gateway failed: model transport failed (status=401)"
    )
    assert diagnostic["error_stage"] == "model_provider"
    assert diagnostic["error_category"] == "authentication"
    assert diagnostic["error_code"] == "model.provider_auth_failed"
    assert diagnostic["retryable"] is False
    assert diagnostic["status_code"] == 401
    assert "sk-secret" not in str(diagnostic)


def test_safe_runtime_failure_diagnostic_classifies_capacity_timeout() -> None:
    diagnostic = safe_runtime_failure_diagnostic(CapacityWaitTimeout("redis password leaked"))

    assert diagnostic["error_summary"] == "model gateway failed: model capacity queue timeout"
    assert diagnostic["error_stage"] == "model_capacity"
    assert diagnostic["error_category"] == "queue_timeout"
    assert diagnostic["error_code"] == "model.capacity_timeout"
    assert diagnostic["retryable"] is True
    assert "password" not in str(diagnostic)


def test_runtime_failure_diagnostic_from_reason_redacts_sensitive_unknown_reason() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason("Authorization: Bearer sk-secret failed")

    assert diagnostic["error_summary"] == "runtime_failed"
    assert diagnostic["error_code"] == "runtime.failed"
    assert "sk-secret" not in str(diagnostic)


def test_runtime_failure_diagnostic_from_reason_extracts_status_code() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        "model gateway failed: model response failed (status=502)"
    )

    assert diagnostic["error_stage"] == "model_provider"
    assert diagnostic["error_category"] == "upstream_unavailable"
    assert diagnostic["error_code"] == "model.provider_unavailable"
    assert diagnostic["retryable"] is True
    assert diagnostic["status_code"] == 502
