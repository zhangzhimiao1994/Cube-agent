from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason

REASON = "runtime recovery blocked: non-replayable event after checkpoint"


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
