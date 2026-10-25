"""Context preflight errors have safe, durable diagnostics rather than capacity advice."""
from uuid import uuid4

import pytest

from agent_hub.models import gateway as gateway_module
from agent_hub.models.capacity import CapacityQueueFull, CapacityWaitTimeout
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import (
    gateway_scope_failure_payload,
    runtime_failure_diagnostic_from_reason,
    safe_model_gateway_failure_reason,
    safe_runtime_failure_diagnostic,
    safe_runtime_failure_reason,
)


def test_context_exception_does_not_expose_private_args() -> None:
    error_type = getattr(gateway_module, "ModelContextWindowExceeded", None)
    assert error_type is not None
    error = error_type("Authorization: PRIVATE prompt and tool output")
    assert safe_model_gateway_failure_reason(error) == "model gateway failed: model context window exceeded"
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["error_code"] == "model.context_window_exceeded"
    assert diagnostic["error_stage"] == "model_context"
    assert diagnostic["error_category"] == "context_window_exceeded"
    assert diagnostic["retryable"] is False
    assert "PRIVATE" not in str(diagnostic) and "Authorization" not in str(diagnostic)


def test_context_reason_roundtrips_as_valid_runtime_event() -> None:
    reason = "model gateway failed: model context window exceeded"
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    assert diagnostic["error_code"] == "model.context_window_exceeded"
    assert diagnostic["retryable"] is False
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                     reason=reason, payload=diagnostic)
    assert RunEvent.from_payload(event.to_payload()) == event


@pytest.mark.parametrize("kind,code", [(CapacityQueueFull, "model.capacity_queue_full"),
                                      (CapacityWaitTimeout, "model.capacity_timeout")])
def test_legacy_capacity_text_with_specific_type_keeps_precise_code(kind: type[Exception], code: str) -> None:
    diagnostic = safe_runtime_failure_diagnostic(kind("model capacity unavailable"))
    assert diagnostic["error_code"] == code
    assert diagnostic["retryable"] is True


@pytest.mark.parametrize("mode", [None, "direct", "dispatch", "discuss"])
def test_context_reason_survives_runtime_string_and_hybrid_boundaries(mode: str | None) -> None:
    inner = "model gateway failed: model context window exceeded"
    reason = inner if mode is None else f"hybrid {mode} failed: {inner}"
    error = RuntimeError(reason)
    assert safe_runtime_failure_reason(error) == reason
    diagnostic = safe_runtime_failure_diagnostic(error)
    assert diagnostic["error_code"] == "model.context_window_exceeded"
    assert diagnostic["retryable"] is False
    if mode is not None:
        assert diagnostic["hybrid_child_mode"] == mode
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                     reason=reason, payload=diagnostic)
    assert RunEvent.from_payload(event.to_payload()) == event


def test_context_literal_survives_base_gateway_error_boundary() -> None:
    error = gateway_module.ModelGatewayError("model context window exceeded")
    reason = "model gateway failed: model context window exceeded"
    assert safe_model_gateway_failure_reason(error) == reason
    assert safe_runtime_failure_reason(error) == reason
    assert safe_runtime_failure_diagnostic(error)["error_code"] == "model.context_window_exceeded"


@pytest.mark.parametrize("kind", [EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED])
async def test_issued_context_scope_roundtrips_through_event_contract(kind: EventKind) -> None:
    from agent_hub.models.registry import ModelRegistry
    from tests.unit.models.test_gateway import (
        CapacityStub,
        SecretStub,
        TransportStub,
        deployment,
        request,
    )

    capacity = CapacityStub([])
    gateway = gateway_module.ModelGateway(ModelRegistry([deployment("primary", context_window_tokens=1)]),
        capacity, SecretStub(capacity.events), TransportStub(capacity.events))
    with pytest.raises(gateway_module.ModelContextWindowExceeded) as caught:
        await gateway.complete_with_context(request())
    scope = gateway_scope_failure_payload(caught.value, include_noncapacity=True)
    assert scope == {
        "gateway_scope_phase": "pretransport_context",
        "gateway_scope_reason": "context_window_exceeded",
        "gateway_scope_transport_entered_count": 0,
        "gateway_scope_failure_attempt_count": 0,
    }
    event = RunEvent(kind=kind, sequence=1, run_id=uuid4(),
        step_id="build" if kind is EventKind.STEP_FAILED else None,
        actor="implementer" if kind is EventKind.STEP_FAILED else None,
        reason=safe_runtime_failure_reason(caught.value),
        payload={**safe_runtime_failure_diagnostic(caught.value), **scope})
    assert RunEvent.from_payload(event.to_payload()) == event
    assert not capacity.events


@pytest.mark.parametrize("reason", [
    "model context window exceeded Authorization: PRIVATE",
    "hybrid discuss failed: model gateway failed: model context window exceeded secret=PRIVATE",
])
def test_context_string_does_not_whitelist_appended_private_text(reason: str) -> None:
    error = gateway_module.ModelGatewayError(reason)
    assert safe_model_gateway_failure_reason(error) is None
    assert safe_runtime_failure_reason(error) == "runtime_failed"
    assert "PRIVATE" not in str(safe_runtime_failure_diagnostic(error))
