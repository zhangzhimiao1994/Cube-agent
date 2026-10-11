from __future__ import annotations

import json
from enum import StrEnum
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason

REASON = "runtime recovery blocked: non-replayable event after checkpoint"
INCOMPLETE_REASON = "model response incomplete"
GATEWAY_SCOPE = {
    "gateway_scope_phase": "transport",
    "gateway_scope_reason": "rejected_output",
    "gateway_scope_transport_entered_count": 1,
    "gateway_scope_failure_attempt_count": 1,
}


class CompressionTrigger(StrEnum):
    INCOMPLETE = "incomplete_response"


def incomplete_diagnostic(*, with_scope: bool) -> dict[str, object]:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(INCOMPLETE_REASON))
    if with_scope:
        payload.update(GATEWAY_SCOPE)
    return payload


@pytest.mark.parametrize("entry", ["constructor", "payload", "json"])
@pytest.mark.parametrize("with_scope", [False, True])
@pytest.mark.parametrize("prefix", ["", "hybrid dispatch failed: "])
def test_incomplete_producer_diagnostic_survives_runtime_contract_and_replay(
    entry: str, with_scope: bool, prefix: str,
) -> None:
    reason = prefix + INCOMPLETE_REASON
    diagnostic: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(reason))
    if with_scope:
        diagnostic.update(GATEWAY_SCOPE)
    values: dict[str, object] = {
        "kind": EventKind.RUNTIME_FAILED.value, "sequence": 132, "run_id": str(uuid4()),
        "reason": reason, "payload": diagnostic,
    }
    if entry == "constructor":
        event = RunEvent(
            kind=EventKind.RUNTIME_FAILED, sequence=132, run_id=uuid4(), reason=reason,
            payload=cast(dict[str, JsonValue], diagnostic),
        )
    elif entry == "payload":
        event = RunEvent.from_payload(values)
    else:
        event = RunEvent.model_validate_json(json.dumps(values), strict=True)
    restored = RunEvent.from_payload(event.to_payload())
    assert restored == event
    assert restored.reason == reason
    assert dict(restored.payload) == diagnostic
    assert restored.payload["compression_trigger"] == "incomplete_response"
    assert restored.payload["error_code"] == "model.incomplete_response"
    assert restored.payload["error_stage"] == "model_response"
    assert restored.payload["error_category"] == "incomplete_response"


def test_tool_failure_accepts_the_same_exact_incomplete_diagnostic() -> None:
    payload = incomplete_diagnostic(with_scope=False)
    event = tool_failure_event(payload)
    assert RunEvent.from_payload(event.to_payload()) == event
    assert event.payload["compression_trigger"] == "incomplete_response"


@pytest.mark.parametrize("with_scope", [False, True])
@pytest.mark.parametrize(("key", "value"), [
    ("compression_trigger", None), ("compression_trigger", False),
    ("compression_trigger", True), ("compression_trigger", 1),
    ("compression_trigger", ""), ("compression_trigger", "structured_output_invalid"),
    ("compression_trigger", "INCOMPLETE_RESPONSE"),
    ("compression_trigger", "incomplete_response "),
    ("compression_trigger", CompressionTrigger.INCOMPLETE),
    ("compression_trigger", {"value": "incomplete_response"}),
    ("error_code", "model.structured_output_invalid"),
    ("error_stage", "runtime"), ("error_category", "invalid_response"),
    ("error_code", None), ("error_stage", False), ("error_category", True),
])
def test_incomplete_marker_rejects_bad_values_and_unrelated_classification(
    key: str, value: object, with_scope: bool,
) -> None:
    payload = incomplete_diagnostic(with_scope=with_scope)
    payload[key] = value
    with pytest.raises((TypeError, ValueError)):
        diagnostic_event(payload)


@pytest.mark.parametrize("with_scope", [False, True])
@pytest.mark.parametrize("missing", [
    "error_code", "error_stage", "error_category", "error_summary", "retryable", "suggested_action",
])
def test_incomplete_marker_does_not_relax_required_fields(missing: str, with_scope: bool) -> None:
    payload = incomplete_diagnostic(with_scope=with_scope)
    del payload[missing]
    with pytest.raises(ValueError, match="runtime failure diagnostic fields are invalid"):
        diagnostic_event(payload)


@pytest.mark.parametrize("with_scope", [False, True])
def test_incomplete_marker_does_not_allow_unknown_fields(with_scope: bool) -> None:
    payload = incomplete_diagnostic(with_scope=with_scope)
    payload["uncontrolled_extension"] = "private"
    with pytest.raises(ValueError, match="runtime failure diagnostic fields are invalid"):
        diagnostic_event(payload)


@pytest.mark.parametrize(("key", "value"), [
    ("gateway_scope_phase", "unknown"), ("gateway_scope_phase", None),
    ("gateway_scope_phase", False), ("gateway_scope_reason", "unknown"),
    ("gateway_scope_reason", None), ("gateway_scope_reason", True),
    ("gateway_scope_transport_entered_count", None),
    ("gateway_scope_transport_entered_count", True),
    ("gateway_scope_transport_entered_count", -1),
    ("gateway_scope_failure_attempt_count", None),
    ("gateway_scope_failure_attempt_count", False),
    ("gateway_scope_failure_attempt_count", 2),
])
def test_incomplete_marker_preserves_gateway_value_validation(key: str, value: object) -> None:
    payload = incomplete_diagnostic(with_scope=True)
    payload[key] = value
    with pytest.raises(ValueError, match="gateway scope diagnostic values are invalid"):
        diagnostic_event(payload)


@pytest.mark.parametrize("missing", tuple(GATEWAY_SCOPE))
def test_incomplete_marker_requires_all_four_gateway_fields_if_any_are_present(missing: str) -> None:
    payload = incomplete_diagnostic(with_scope=True)
    del payload[missing]
    with pytest.raises(ValueError, match="gateway scope diagnostic fields are invalid"):
        diagnostic_event(payload)


def diagnostic_event(payload: dict[str, object]) -> RunEvent:
    return RunEvent(
        kind=EventKind.RUNTIME_FAILED, sequence=10, run_id=uuid4(), reason=REASON,
        payload=cast(dict[str, JsonValue], payload),
    )


def test_runtime_failure_accepts_bounded_diagnostics_and_preserves_historical_text() -> None:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASON))
    payload["suggested_action"] = "Inspect the last checkpoint before any retry."
    event = diagnostic_event(payload)
    restored = RunEvent.from_payload(event.to_payload())
    assert restored.reason == REASON
    assert dict(restored.payload) == payload
    assert restored.payload["error_code"] == "runtime.recovery_blocked"
    assert restored.payload["retryable"] is False


@pytest.mark.parametrize(("key", "value"), [
    ("unknown", "extra"), ("error_code", {"nested": "private"}),
    ("error_code", "not a safe identifier"), ("error_stage", "x" * 129),
    ("error_category", 1), ("retryable", 1), ("retryable", "false"),
    ("error_summary", "x" * 241), ("error_summary", "unsafe\x00summary"),
    ("suggested_action", "x" * 1025), ("suggested_action", {"text": "private"}),
    ("status_code", True), ("status_code", "400"), ("status_code", 99), ("status_code", 600),
    ("hybrid_child_mode", "unknown"), ("orchestration_recovery_hint", "arbitrary_retry"),
    ("step_id", "bad step"), ("actor", "bad actor"),
])
def test_runtime_failure_rejects_unknown_or_unsafe_diagnostic_values(key: str, value: object) -> None:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASON))
    payload[key] = value
    with pytest.raises(ValueError):
        diagnostic_event(payload)


@pytest.mark.parametrize("missing", [
    "error_code", "error_stage", "error_category", "error_summary", "suggested_action", "retryable",
])
def test_nonempty_diagnostic_requires_all_core_fields(missing: str) -> None:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASON))
    del payload[missing]
    with pytest.raises(ValueError):
        diagnostic_event(payload)


def test_legacy_reason_only_failure_still_round_trips() -> None:
    event = diagnostic_event({})
    assert RunEvent.from_payload(event.to_payload()) == event


@pytest.mark.parametrize("extra", [{"actor": "worker"}, {"message": "not a diagnostic"}])
def test_runtime_failure_diagnostic_does_not_allow_unrelated_event_fields(extra: dict[str, object]) -> None:
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(), reason=REASON)
    payload = event.to_payload() | extra
    payload["payload"] = runtime_failure_diagnostic_from_reason(REASON)
    with pytest.raises(ValueError):
        RunEvent.from_payload(payload)


def tool_failure_event(payload: dict[str, object]) -> RunEvent:
    return RunEvent(
        kind=EventKind.TOOL_FAILED, sequence=10, run_id=uuid4(),
        actor="architect", tool_call_id="read-1", tool_name="workspace.read",
        reason="workspace read denied or scoped file unavailable",
        payload=cast(dict[str, JsonValue], payload),
    )


def test_tool_failure_accepts_validated_diagnostics_and_existing_receipt_fields() -> None:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASON))
    payload.update(status="rejected", failure_kind="invalid_arguments", replay_safe=True)
    event = tool_failure_event(payload)
    restored = RunEvent.from_payload(event.to_payload())
    assert restored == event
    assert restored.payload["status"] == "rejected"
    assert restored.payload["error_code"] == "runtime.recovery_blocked"


@pytest.mark.parametrize(("key", "value"), [
    ("unknown", "extra"), ("error_code", {"nested": "private"}),
    ("error_code", "not a safe identifier"), ("error_stage", "x" * 129),
    ("error_category", 1), ("retryable", 1), ("retryable", "false"),
    ("error_summary", "x" * 241), ("error_summary", "unsafe\x00summary"),
    ("suggested_action", "x" * 1025), ("suggested_action", {"text": "private"}),
    ("status_code", True), ("status_code", "400"), ("status_code", 99), ("status_code", 600),
    ("hybrid_child_mode", "unknown"), ("orchestration_recovery_hint", "arbitrary_retry"),
    ("step_id", "bad step"), ("actor", "bad actor"), ("password", "private"),
])
def test_tool_failure_rejects_unsafe_diagnostics(key: str, value: object) -> None:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASON))
    payload[key] = value
    with pytest.raises(ValueError):
        tool_failure_event(payload)


@pytest.mark.parametrize("missing", [
    "error_code", "error_stage", "error_category", "error_summary", "suggested_action", "retryable",
])
def test_tool_failure_diagnostic_requires_all_core_fields(missing: str) -> None:
    payload: dict[str, object] = dict(runtime_failure_diagnostic_from_reason(REASON))
    del payload[missing]
    with pytest.raises(ValueError):
        tool_failure_event(payload)


@pytest.mark.parametrize("kind", [
    EventKind.TOOL_REQUESTED, EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED,
])
def test_tool_diagnostics_are_only_permitted_for_failed_events(kind: EventKind) -> None:
    event = tool_failure_event({"status": "rejected"})
    payload = event.to_payload()
    payload.update(kind=kind.value, reason=None,
                   payload=dict(runtime_failure_diagnostic_from_reason(REASON)))
    with pytest.raises(ValueError):
        RunEvent.from_payload(payload)


def test_legacy_tool_failure_receipt_still_round_trips_without_diagnostics() -> None:
    event = tool_failure_event({"status": "rejected", "failure_kind": "invalid_arguments"})
    assert RunEvent.from_payload(event.to_payload()) == event
