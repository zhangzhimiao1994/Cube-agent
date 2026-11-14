from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.api.routers.admin import (
    FailureDiagnosticResponse,
    _admin_run_event,
    _runtime_or_step_failure_diagnostic,
)
from agent_hub.capabilities.python_resolution_diagnostics import (
    ResolutionPhase,
    ResolutionProgress,
    observe_resolution_timeout,
    resolution_timeout_payload,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.self_repair import (
    SelfRepairPolicy,
    classify_terminal_run,
    repair_context_from_proposal,
    repair_proposal_projection,
)
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from agent_hub.runtime.self_repair_context import self_repair_context_text

KEYS = (
    "resolution_phase", "resolution_progress", "resolution_progress_evidence",
    "original_deadline_expired",
)


def timeout_event(expired: bool | None = True) -> RunEvent:
    error = TimeoutError()
    deadline = None if expired is None else datetime.now(UTC) + timedelta(
        seconds=-30 if expired else 30,
    )
    observe_resolution_timeout(error, phase=ResolutionPhase.FREEZE,
        progress=ResolutionProgress.FREEZE_PENDING, original_deadline=deadline)
    return RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=3, run_id=uuid4(),
        reason="runtime_failed", payload=cast(dict[str, JsonValue], resolution_timeout_payload(error)))


@pytest.mark.parametrize("expired", [True, False, None])
def test_typed_timeout_is_not_a_generic_repair_even_with_empty_exception_reason(
    expired: bool | None,
) -> None:
    event = timeout_event(expired)
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DISPATCH,
        routing_decision={"engineering_auto_repair_version": 1}, events=(event,),
        policy=SelfRepairPolicy(requires_approval=False))
    assert decision is not None
    assert decision.kind == "repair.skipped"
    assert decision.failure_category == "project_verification"
    assert decision.skip_reason == "project_verification_no_safe_recovery"
    assert decision.automatic_execution is False
    assert decision.to_proposal(run_id=event.run_id) is None
    recorded = decision.with_source_sequence(4).to_event(run_id=event.run_id, sequence=5)
    assert {key: recorded.payload[key] for key in KEYS} == {
        key: event.payload[key] for key in KEYS
    }


@pytest.mark.parametrize("change", [
    {"resolution_progress_evidence": "none"},
    {"original_deadline_expired": 1},
    {"resolution_phase": "PRIVATE password/raw/path"},
    {"retryable": True},
])
def test_malformed_legacy_timeout_fails_closed_before_auto_repair(
    change: dict[str, object],
) -> None:
    event = timeout_event()
    # A legacy/corrupt stored event can bypass RunEvent's new constructor validation.
    event = event.model_copy(update={"payload": {**event.payload, **change}})
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DISPATCH,
        routing_decision={"engineering_auto_repair_version": 1}, events=(event,),
        policy=SelfRepairPolicy(requires_approval=False))
    assert decision is not None and decision.kind == "repair.skipped"
    assert decision.skip_reason == "side_effect_outcome_uncertain"
    assert decision.automatic_execution is False
    assert "PRIVATE" not in json.dumps(decision.to_event(run_id=event.run_id, sequence=4).to_payload())


@pytest.mark.parametrize("expired", [True, False, None])
def test_runtime_event_to_admin_response_preserves_finite_timeout_observation(
    expired: bool | None,
) -> None:
    event = timeout_event(expired)
    api_event = _admin_run_event(event.to_payload())
    assert {key: api_event.payload[key] for key in KEYS} == {
        key: event.payload[key] for key in KEYS
    }
    response = _runtime_or_step_failure_diagnostic(api_event).model_dump(mode="json")
    assert {key: response.get(key) for key in KEYS} == {
        key: event.payload[key] for key in KEYS
    }
    assert response["retryable"] is False


def test_approved_context_projects_observation_as_data_without_private_or_authority_fields() -> None:
    observation = {key: timeout_event().payload[key] for key in KEYS}
    # This is the existing explicit context path, not an automatically created timeout proposal.
    proposal = {"kind": "self_repair", "failure_kind": "runtime_failure",
        "source_run_id": "run_1", "source_event_sequence": 3,
        "requires_approval": True, "automatic_execution": False,
        "instruction": "仅核对既有回执，不重放。", **observation,
        "raw_stderr": "PRIVATE password/raw/path", "authority": "PRIVATE"}
    projected = repair_proposal_projection(proposal)
    assert projected is not None
    context = repair_context_from_proposal(projected)
    assert {key: context.get(key) for key in KEYS} == observation
    prompt = self_repair_context_text({"self_repair_context": context})
    match = re.search(r"<RESOLUTION_TIMEOUT_OBSERVATION>(.*?)</RESOLUTION_TIMEOUT_OBSERVATION>", prompt)
    assert match is not None
    data = json.loads(match[1])
    assert {key: data.get(key) for key in KEYS} == observation
    assert "PRIVATE" not in prompt
    assert context["automatic_execution"] is False


@pytest.mark.parametrize("change", [
    {"original_deadline_expired": 1}, {"resolution_phase": "PRIVATE"},
    {"resolution_progress_evidence": "none"},
])
def test_prompt_drops_the_entire_malformed_observation(change: dict[str, object]) -> None:
    observation = {key: timeout_event().payload[key] for key in KEYS}
    context: dict[str, object] = {"source": "self_repair", **observation, **change}
    prompt = self_repair_context_text({"self_repair_context": context})
    assert all(f'"{key}"' not in prompt for key in KEYS)
    assert "PRIVATE" not in prompt


def test_missing_entire_observation_on_known_timeout_code_is_still_unknown() -> None:
    event = timeout_event()
    event = event.model_copy(update={"payload": {
        key: value for key, value in event.payload.items() if key not in KEYS
    }})
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DISPATCH,
        routing_decision=None, events=(event,), policy=SelfRepairPolicy(requires_approval=False))
    assert decision is not None and decision.skip_reason == "side_effect_outcome_uncertain"
    assert decision.to_proposal(run_id=event.run_id) is None


@pytest.mark.parametrize("change", [
    {"original_deadline_expired": 1}, {"resolution_phase": "PRIVATE"},
    {"resolution_progress_evidence": "none"}, {"resolution_progress": None},
])
def test_response_schema_rejects_partial_or_coerced_observation(change: dict[str, object]) -> None:
    diagnostic = _runtime_or_step_failure_diagnostic(_admin_run_event(timeout_event().to_payload()))
    with pytest.raises(ValueError):
        FailureDiagnosticResponse.model_validate({**diagnostic.model_dump(mode="json"), **change})


def test_observation_survives_prompt_budget_without_restoring_private_streams() -> None:
    observation = {key: timeout_event(None).payload[key] for key in KEYS}
    context = repair_context_from_proposal({"kind": "self_repair", "failure_kind": "runtime_failure",
        "source_run_id": "r" * 96, "instruction": "核对" * 120,
        "suggested_action": "保持禁止重放" * 60, **observation, "stdout": "PRIVATE"})
    prompt = self_repair_context_text({"self_repair_context": context})
    match = re.search(r"<RESOLUTION_TIMEOUT_OBSERVATION>(.*?)</RESOLUTION_TIMEOUT_OBSERVATION>", prompt)
    assert match is not None and json.loads(match[1]) == observation
    assert "not PASS, execution authority or permission to replay" in prompt
    assert "PRIVATE" not in prompt
    assert len(match[1].encode()) < 256


@pytest.mark.parametrize("variant", ["valid", "mixed", "bad_leaf", "conflicting_summary"])
def test_root_leaf_precedence_preserves_its_original_typed_inspect_only_contract(variant: str) -> None:
    from agent_hub.runtime.failure_reason import resolution_failure_payload
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionFailureDiagnostic

    leaf = ResolutionFailureDiagnostic.model_validate({"phase": "registry_metadata",
        "code": "resolution_timeout", "exit_code": 2, "timed_out": True}, strict=True)
    canonical = resolution_failure_payload(leaf)
    payload = dict(canonical)
    if variant == "mixed":
        payload.update({key: timeout_event().payload[key] for key in KEYS})
    elif variant == "bad_leaf":
        payload["resolution_failure"] = {"phase": "PRIVATE", "code": "resolution_timeout",
            "exit_code": 2, "timed_out": True}
    elif variant == "conflicting_summary":
        payload["error_summary"] = "PRIVATE conflicting summary"
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=3, run_id=uuid4(),
        reason="project verification failed: resolution resolution_timeout", payload=canonical)
    event = event.model_copy(update={"payload": payload})
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DISPATCH,
        routing_decision=None, events=(event,), policy=SelfRepairPolicy(requires_approval=False))
    assert decision is not None and decision.kind == "repair.skipped"
    assert decision.to_proposal(run_id=event.run_id) is None and not decision.automatic_execution
    if variant == "valid":
        assert decision.skip_reason == "project_verification_no_safe_recovery"
        assert decision.error_code == canonical["error_code"]
        assert decision.suggested_action == canonical["suggested_action"]
    else:
        assert decision.skip_reason == "side_effect_outcome_uncertain"
        assert decision.error_code is None and decision.suggested_action is None
    assert "PRIVATE" not in json.dumps(decision.to_event(run_id=event.run_id, sequence=4).to_payload())
