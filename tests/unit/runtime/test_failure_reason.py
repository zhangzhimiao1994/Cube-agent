from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import (
    GatewayNegotiatedRejectedOutput,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
    ModelGatewayError,
)
from agent_hub.models.litellm_client import ModelResponseError, ModelTransportError
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import RejectedOutputEvidence, TokenUsage
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import (
    MAX_FAILURE_REASON_LENGTH,
    ORCHESTRATION_CHECKPOINT_FAILURE_REASON,
    runtime_failure_diagnostic_from_reason,
    safe_model_gateway_failure_reason,
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
        (
            ModelGatewayError("model response text is empty"),
            "model gateway failed: model response text is empty",
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


def test_runtime_failure_diagnostic_classifies_orchestration_checkpoint_failures() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(ORCHESTRATION_CHECKPOINT_FAILURE_REASON)

    assert diagnostic["error_stage"] == "runtime_recovery"
    assert diagnostic["error_category"] == "orchestration_checkpoint_incompatible"
    assert diagnostic["error_code"] == "runtime.orchestration_checkpoint_incompatible"
    assert diagnostic["retryable"] is False
    assert diagnostic["orchestration_recovery_hint"] == "retry_blocked_contract_chain"


def test_runtime_failure_diagnostic_from_reason_extracts_status_code() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        "model gateway failed: model response failed (status=502)"
    )

    assert diagnostic["error_stage"] == "model_provider"
    assert diagnostic["error_category"] == "upstream_unavailable"
    assert diagnostic["error_code"] == "model.provider_unavailable"
    assert diagnostic["retryable"] is True
    assert diagnostic["status_code"] == 502


@pytest.mark.parametrize(
    "reason",
    [
        "model response text is empty",
        "model gateway failed: model response text is empty",
        "hybrid dispatch failed: model gateway failed: model response text is empty",
    ],
)
def test_runtime_failure_diagnostic_classifies_empty_model_response_as_retryable(
    reason: str,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)

    assert diagnostic["error_stage"] == "model_response"
    assert diagnostic["error_category"] == "empty_response"
    assert diagnostic["error_code"] == "model.empty_response"
    assert diagnostic["retryable"] is True
    suggested_action = cast(str, diagnostic["suggested_action"])
    assert "压缩输入" in suggested_action
    assert "fallback" in suggested_action
    if reason.startswith("hybrid "):
        assert diagnostic["hybrid_child_mode"] == "dispatch"


@pytest.mark.parametrize("mode", ["direct", "dispatch", "discuss"])
def test_runtime_failure_diagnostic_classifies_generic_hybrid_child_failure(mode: str) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        f"hybrid {mode} failed: task execution plan is invalid"
    )

    assert diagnostic["error_stage"] == f"hybrid_{mode}"
    assert diagnostic["error_category"] == "child_runtime_failed"
    assert diagnostic["error_code"] == "runtime.hybrid_child_failed"
    assert diagnostic["retryable"] is True
    assert diagnostic["hybrid_child_mode"] == mode


@pytest.mark.parametrize(
    ("reason", "stage", "category", "code"),
    [
        (
            "dispatch execution failed",
            "dispatch_runtime",
            "child_runtime_failed",
            "runtime.dispatch_failed",
        ),
        ("step execution failed", "crew_step", "step_failed", "crew.step_failed"),
        ("discussion_failed", "discussion_runtime", "discussion_failed", "runtime.discussion_failed"),
    ],
)
def test_runtime_failure_diagnostic_classifies_legacy_safe_runtime_fallbacks(
    reason: str,
    stage: str,
    category: str,
    code: str,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)

    assert diagnostic["error_stage"] == stage
    assert diagnostic["error_category"] == category
    assert diagnostic["error_code"] == code
    assert diagnostic["retryable"] is True


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


def test_runtime_failure_diagnostic_classifies_capability_failure_without_raw_details() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        "capability failed: terminal command exited with private path C:/Users/me/token.txt"
    )

    assert diagnostic["error_summary"] == "capability failed"
    assert diagnostic["error_stage"] == "capability"
    assert diagnostic["error_category"] == "execution_failed"
    assert diagnostic["error_code"] == "capability.execution_failed"
    assert diagnostic["retryable"] is False
    assert "private path" not in str(diagnostic)
    assert "token.txt" not in str(diagnostic)


def test_runtime_failure_diagnostic_classifies_replay_safe_capability_failure_as_retryable() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason("capability transient execution failed")

    assert diagnostic["error_summary"] == "capability transient execution failed"
    assert diagnostic["error_stage"] == "capability"
    assert diagnostic["error_category"] == "transient_execution_failed"
    assert diagnostic["error_code"] == "capability.transient_execution_failed"
    assert diagnostic["retryable"] is True
    suggested_action = cast(str, diagnostic["suggested_action"])
    assert "replay-safe" in suggested_action
    assert "压缩输入" in suggested_action


def test_runtime_failure_diagnostic_classifies_planned_capability_unavailable() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason("planned capability is unavailable")

    assert diagnostic["error_summary"] == "planned capability is unavailable"
    assert diagnostic["error_stage"] == "capability"
    assert diagnostic["error_category"] == "planned_capability_unavailable"
    assert diagnostic["error_code"] == "capability.planned_unavailable"
    assert diagnostic["retryable"] is False
    suggested_action = cast(str, diagnostic["suggested_action"])
    assert "重新加载" in suggested_action
    assert "重新规划" in suggested_action


def test_runtime_failure_diagnostic_classifies_plugin_adapter_unavailable() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        "Plugin tool unavailable: plugin_package_adapter_unavailable"
    )

    assert diagnostic["error_summary"] == (
        "Plugin tool unavailable: plugin_package_adapter_unavailable"
    )
    assert diagnostic["error_stage"] == "plugin_runtime"
    assert diagnostic["error_category"] == "adapter_unavailable"
    assert diagnostic["error_code"] == "plugin.adapter_unavailable"
    assert diagnostic["retryable"] is False
    suggested_action = cast(str, diagnostic["suggested_action"])
    assert "插件适配器" in suggested_action
    assert "重新加载" in suggested_action


@pytest.mark.parametrize(
    ("reason", "error_category", "error_code", "retryable"),
    [
        (
            "plugin.adapter_unavailable",
            "adapter_unavailable",
            "plugin.adapter_unavailable",
            False,
        ),
        (
            "Plugin tool timed out",
            "timeout",
            "plugin.timeout",
            True,
        ),
        (
            "Plugin credential unavailable",
            "credential_unavailable",
            "plugin.credential_unavailable",
            False,
        ),
        (
            "Plugin tool unavailable: plugin_disabled",
            "disabled",
            "plugin.disabled",
            False,
        ),
        (
            "Plugin arguments do not match input schema: invalid type",
            "invalid_arguments",
            "plugin.invalid_arguments",
            False,
        ),
        (
            "Plugin result does not match output schema: invalid type",
            "invalid_result",
            "plugin.invalid_result",
            False,
        ),
        (
            "Plugin backend unavailable",
            "backend_unavailable",
            "plugin.backend_unavailable",
            True,
        ),
        (
            "Plugin endpoint unavailable",
            "endpoint_unavailable",
            "plugin.endpoint_unavailable",
            True,
        ),
        (
            "Plugin sandbox profile unsupported",
            "sandbox_unsupported",
            "plugin.sandbox_unsupported",
            False,
        ),
    ],
)
def test_runtime_failure_diagnostic_classifies_plugin_runtime_failures(
    reason: str,
    error_category: str,
    error_code: str,
    retryable: bool,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)

    assert diagnostic["error_stage"] == "plugin_runtime"
    assert diagnostic["error_category"] == error_category
    assert diagnostic["error_code"] == error_code
    assert diagnostic["retryable"] is retryable
    suggested_action = cast(str, diagnostic["suggested_action"])
    assert "插件" in suggested_action


@pytest.mark.parametrize(
    ("reason", "error_category", "error_code", "retryable"),
    [
        ("mcp.tool_unavailable", "tool_unavailable", "mcp.tool_unavailable", False),
        ("MCP tool unavailable", "tool_unavailable", "mcp.tool_unavailable", False),
        ("mcp.timeout", "timeout", "mcp.timeout", True),
        ("MCP tool timed out", "timeout", "mcp.timeout", True),
        ("mcp.server_not_discovered", "server_not_discovered", "mcp.server_not_discovered", False),
        ("mcp_server_not_discovered", "server_not_discovered", "mcp.server_not_discovered", False),
        ("mcp.server_timeout", "server_timeout", "mcp.server_timeout", True),
        ("mcp_server_timeout", "server_timeout", "mcp.server_timeout", True),
        ("mcp.server_failed", "server_failed", "mcp.server_failed", True),
        ("mcp_server_failed", "server_failed", "mcp.server_failed", True),
        ("MCP tool unavailable: mcp_server_failed", "server_failed", "mcp.server_failed", True),
        ("mcp.server_unavailable", "server_unavailable", "mcp.server_unavailable", True),
        ("mcp_server_unavailable", "server_unavailable", "mcp.server_unavailable", True),
        (
            "MCP tool unavailable: mcp_server_unavailable",
            "server_unavailable",
            "mcp.server_unavailable",
            True,
        ),
    ],
)
def test_runtime_failure_diagnostic_classifies_mcp_runtime_failures(
    reason: str,
    error_category: str,
    error_code: str,
    retryable: bool,
) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)

    assert diagnostic["error_stage"] == "mcp_runtime"
    assert diagnostic["error_category"] == error_category
    assert diagnostic["error_code"] == error_code
    assert diagnostic["retryable"] is retryable
    suggested_action = cast(str, diagnostic["suggested_action"])
    assert "MCP" in suggested_action


def test_runtime_failure_diagnostic_classifies_uncertain_capability_outcome_as_non_retryable() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason("capability outcome requires confirmation")

    assert diagnostic["error_stage"] == "capability"
    assert diagnostic["error_category"] == "outcome_uncertain"
    assert diagnostic["error_code"] == "capability.outcome_uncertain"
    assert diagnostic["retryable"] is False


def test_runtime_failure_diagnostic_classifies_recovery_replay_block_as_non_retryable() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        "runtime recovery blocked: non-replayable event after checkpoint"
    )

    assert (
        diagnostic["error_summary"]
        == "runtime recovery blocked: non-replayable event after checkpoint"
    )
    assert diagnostic["error_stage"] == "runtime_recovery"
    assert diagnostic["error_category"] == "non_replayable_event_after_checkpoint"
    assert diagnostic["error_code"] == "runtime.recovery_blocked"
    assert diagnostic["retryable"] is False


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


@pytest.mark.parametrize("status_code", [405, 410, 415, 422])
def test_unknown_provider_client_errors_are_not_network_retries(status_code: int) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(
        f"model gateway failed: model transport failed (status={status_code})"
    )
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
        (
            "dispatch budget exhausted",
            "runtime_accounting",
            "runtime.dispatch_budget_exhausted",
        ),
        (
            "dispatch usage unaccounted",
            "runtime_accounting",
            "runtime.dispatch_usage_unaccounted",
        ),
        (
            "dispatch accounting audit overflow",
            "runtime_accounting",
            "runtime.dispatch_accounting_audit_overflow",
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
            "dispatch budget exhausted",
            "runtime_accounting",
            "budget_exhausted",
            "runtime.dispatch_budget_exhausted",
        ),
        (
            "dispatch usage unaccounted",
            "runtime_accounting",
            "unaccounted_usage",
            "runtime.dispatch_usage_unaccounted",
        ),
        (
            "dispatch accounting audit overflow",
            "runtime_accounting",
            "audit_overflow",
            "runtime.dispatch_accounting_audit_overflow",
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


def test_runtime_failure_diagnostic_classifies_structured_output_invalid() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason("structured output invalid")

    assert diagnostic["error_summary"] == "structured output invalid"
    assert diagnostic["error_stage"] == "model_response"
    assert diagnostic["error_category"] == "structured_output_invalid"
    assert diagnostic["error_code"] == "model.structured_output_invalid"
    assert diagnostic["retryable"] is True
    assert "JSON" in str(diagnostic["suggested_action"])


@pytest.mark.parametrize("prefix", ["", "hybrid dispatch failed: "])
def test_known_incomplete_response_has_distinct_compact_recovery_diagnostic(prefix: str) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(prefix + "model response incomplete")

    assert diagnostic["error_summary"] == "model response incomplete"
    assert diagnostic["error_stage"] == "model_response"
    assert diagnostic["error_category"] == "incomplete_response"
    assert diagnostic["error_code"] == "model.incomplete_response"
    assert diagnostic["retryable"] is True
    assert diagnostic["compression_trigger"] == "incomplete_response"
    assert "\u672a\u5b8c\u6210" in str(diagnostic["suggested_action"])
    assert "\u7528\u91cf" in str(diagnostic["suggested_action"])
    assert "\u6709\u754c" in str(diagnostic["suggested_action"])


def test_arbitrary_incomplete_response_text_does_not_authorize_recovery() -> None:
    diagnostic = runtime_failure_diagnostic_from_reason("model response incomplete: arbitrary")

    assert diagnostic["error_code"] == "runtime.failed"
    assert diagnostic["retryable"] is False
    assert "compression_trigger" not in diagnostic


def _rejected_gateway_error(
    evidence: RejectedOutputEvidence | None,
    error_type: type[GatewayRejectedOutput] = GatewayRejectedOutput,
) -> GatewayRejectedOutput:
    error = error_type(evidence=evidence, deployment_id="PRIVATE_DEPLOYMENT",
                       logical_model="PRIVATE_LOGICAL_MODEL", provider_id="PRIVATE_PROVIDER",
                       provider_model="PRIVATE_PROVIDER_MODEL")
    error.args = ((
        'GatewayRejectedOutput model response rejected\n'
        'Traceback (most recent call last):\n  File "PRIVATE_PATH", line 1\n'
        'PRIVATE_RAW_EXCEPTION'
    ),)
    return error


@pytest.mark.parametrize("reason,status,usage_status", [
    ("output_limit", "incomplete", "known"),
    ("schema_mismatch", "completed", "known"),
    ("invalid_tool", "completed", "missing"),
    ("usage_invalid", "unknown", "invalid"),
    ("refusal", "refused", "missing"),
])
def test_typed_gateway_rejection_projects_only_finite_evidence(
    reason: str, status: str, usage_status: str,
) -> None:
    evidence = RejectedOutputEvidence(
        final_text="UNPUBLISHED_FINAL_TEXT" if status == "completed" else None,
        usage=TokenUsage(1, 2, 3) if usage_status == "known" else None,
        usage_status=usage_status, status=status, reason=reason,  # type: ignore[arg-type]
    )
    error = _rejected_gateway_error(evidence)
    expected = ("model gateway failed: model response rejected "
                f"(reason={reason}; status={status}; usage_status={usage_status})")
    assert safe_model_gateway_failure_reason(error) == expected
    assert safe_runtime_failure_reason(error) == expected
    assert len(expected) <= MAX_FAILURE_REASON_LENGTH
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["error_code"] == "model.response_rejected"
    assert diagnostic["error_stage"] == "model_response"
    assert diagnostic["retryable"] is False
    assert diagnostic["error_summary"] == expected
    assert runtime_failure_diagnostic_from_reason(expected) == diagnostic
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                     reason=expected, payload=diagnostic)
    assert event.payload["retryable"] is False
    assert "compression_trigger" not in diagnostic
    for private in ("UNPUBLISHED_FINAL_TEXT", "PRIVATE_", "Traceback", "24576"):
        assert private not in expected + str(diagnostic)


@pytest.mark.parametrize("defect", ["absent", "duck_type", "subclass", "reason", "status", "usage_status"])
def test_rejected_diagnostic_requires_exact_finite_evidence(defect: str) -> None:
    evidence = RejectedOutputEvidence(final_text=None, usage=None, usage_status="missing",
                                      status="unknown", reason="invalid_output")
    if defect == "absent":
        invalid: object = None
    elif defect == "duck_type":
        from types import SimpleNamespace
        invalid = SimpleNamespace(reason="output_limit", status="incomplete", usage_status="known")
    elif defect == "subclass":
        class DerivedEvidence(RejectedOutputEvidence):
            pass
        invalid = DerivedEvidence(final_text=None, usage=None, usage_status="missing",
                                  status="unknown", reason="invalid_output")
    else:
        object.__setattr__(evidence, defect, "PRIVATE_UNTRUSTED_FIELD")
        invalid = evidence
    error = _rejected_gateway_error(cast(RejectedOutputEvidence | None, invalid))
    expected = "model gateway failed: model response rejected"
    assert safe_model_gateway_failure_reason(error) == expected
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["error_summary"] == expected
    assert diagnostic["error_code"] == "model.response_rejected"
    assert diagnostic["retryable"] is False
    assert not any(key.startswith("rejected_") for key in diagnostic)
    assert "PRIVATE_" not in str(diagnostic)


def test_negotiated_rejection_retains_stop_boundary_and_safe_evidence() -> None:
    evidence = RejectedOutputEvidence(final_text=None, usage=TokenUsage(1, 2, 3),
        usage_status="known", status="incomplete", reason="output_limit")
    error = _rejected_gateway_error(evidence, GatewayNegotiatedRejectedOutput)
    expected = "model gateway failed: model parameter negotiation exhausted"
    assert safe_model_gateway_failure_reason(error) == expected
    assert safe_runtime_failure_reason(error) == expected
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["error_code"] == "model.request_negotiation_exhausted"
    assert diagnostic["retryable"] is False
    assert "reason=output_limit" in str(diagnostic["error_summary"])
    assert "status=incomplete" in str(diagnostic["error_summary"])
    assert "usage_status=known" in str(diagnostic["error_summary"])
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                     reason=expected, payload=diagnostic)
    assert event.payload["error_code"] == "model.request_negotiation_exhausted"
    assert "PRIVATE_" not in str(diagnostic)
    assert runtime_failure_diagnostic_from_reason(expected)["retryable"] is False


def test_rejected_cancellation_is_not_unwrapped_into_a_model_failure() -> None:
    error = GatewayResponseCancelled(receipt=_rejected_gateway_error(None))
    assert not isinstance(error, Exception)
    assert safe_model_gateway_failure_reason(error) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("wrapper", [RuntimeError, ModelGatewayError])
def test_untyped_framework_traceback_is_redacted_without_inventing_rejection(
    wrapper: type[Exception],
) -> None:
    error = wrapper('GatewayRejectedOutput model response rejected\n'
                    'Traceback (most recent call last):\n  File "LOCAL_PATH", line 2\n'
                    'UNPUBLISHED_MODEL_TEXT')
    error.__cause__ = _rejected_gateway_error(None)
    assert safe_model_gateway_failure_reason(error) is None
    assert safe_runtime_failure_reason(error, fallback="framework_failed") == "framework_failed"
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["error_summary"] == "runtime_failed"
    assert diagnostic["error_code"] == "runtime.failed"
    assert "Traceback" not in str(diagnostic)
    assert "LOCAL_PATH" not in str(diagnostic)
