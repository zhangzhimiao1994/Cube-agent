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


def test_runtime_failure_diagnostic_classifies_crewai_step_timeout() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        "CrewAI step timed out: step=writer_step actor=writer"
    )

    assert diagnostic["error_summary"] == "CrewAI step timed out: step=writer_step actor=writer"
    assert diagnostic["error_stage"] == "crew_step"
    assert diagnostic["error_category"] == "step_timeout"
    assert diagnostic["error_code"] == "crew.step_timeout"
    assert diagnostic["retryable"] is True
    assert diagnostic["step_id"] == "writer_step"
    assert diagnostic["actor"] == "writer"


@pytest.mark.parametrize(
    ("status_code", "error_category", "error_code"),
    [
        (400, "bad_request", "model.provider_bad_request"),
        (402, "quota_or_billing", "model.provider_quota_or_billing_failed"),
        (404, "model_not_found", "model.provider_model_not_found"),
        (413, "payload_too_large", "model.provider_payload_too_large"),
    ],
)
def test_runtime_failure_diagnostic_classifies_common_provider_statuses(
    status_code: int,
    error_category: str,
    error_code: str,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        f"model gateway failed: model transport failed (status={status_code})"
    )

    assert diagnostic["error_stage"] == "model_provider"
    assert diagnostic["error_category"] == error_category
    assert diagnostic["error_code"] == error_code
    assert diagnostic["retryable"] is False
    assert diagnostic["status_code"] == status_code


@pytest.mark.parametrize(
    ("reason", "error_stage", "error_code"),
    [
        ("runtime_not_configured", "runtime_configuration", "runtime.not_configured"),
        (
            "dispatch accounting exhausted",
            "runtime_accounting",
            "runtime.dispatch_accounting_exhausted",
        ),
        ("artifact rollback failed", "artifact_storage", "artifact.rollback_failed"),
    ],
)
def test_runtime_failure_diagnostic_classifies_runtime_guardrails(
    reason: str,
    error_stage: str,
    error_code: str,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)

    assert diagnostic["error_summary"] == reason
    assert diagnostic["error_stage"] == error_stage
    assert diagnostic["error_code"] == error_code
    assert diagnostic["retryable"] is False


@pytest.mark.parametrize(
    ("status_code", "error_category", "error_code", "retryable"),
    [
        (400, "bad_request", "model.provider_bad_request", False),
        (402, "quota_or_billing", "model.provider_quota_or_billing_failed", False),
        (404, "model_not_found", "model.provider_model_not_found", False),
        (413, "payload_too_large", "model.provider_payload_too_large", False),
        (429, "rate_limited", "model.provider_rate_limited", True),
    ],
)
def test_runtime_failure_diagnostic_classifies_provider_status_codes(
    status_code: int,
    error_category: str,
    error_code: str,
    retryable: bool,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        f"model gateway failed: model response failed (status={status_code})"
    )

    assert diagnostic["error_category"] == error_category
    assert diagnostic["error_code"] == error_code
    assert diagnostic["retryable"] is retryable
    assert diagnostic["status_code"] == status_code


@pytest.mark.parametrize(
    ("reason", "error_stage", "error_category", "error_code"),
    [
        (
            "runtime_not_configured",
            "runtime_configuration",
            "missing_runtime",
            "runtime.not_configured",
        ),
        (
            "dispatch accounting exhausted",
            "runtime_accounting",
            "accounting_guardrail",
            "runtime.dispatch_accounting_exhausted",
        ),
        (
            "artifact rollback failed",
            "artifact_storage",
            "rollback_failed",
            "artifact.rollback_failed",
        ),
    ],
)
def test_runtime_failure_diagnostic_classifies_runtime_infrastructure_failures(
    reason: str,
    error_stage: str,
    error_category: str,
    error_code: str,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)

    assert diagnostic["error_stage"] == error_stage
    assert diagnostic["error_category"] == error_category
    assert diagnostic["error_code"] == error_code
    assert diagnostic["retryable"] is False
