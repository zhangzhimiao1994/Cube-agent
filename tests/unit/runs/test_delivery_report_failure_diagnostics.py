"""Finite report failures are observations, not authority to repair or replay."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import (
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_diagnostic,
    safe_runtime_failure_reason,
)

BASE = "direct final report is invalid"
SHAPES = ["completion_missing", "tool_calls", "empty_text", "json_only", "binding_invalid", "unknown"]


def report_reason(shape: str, prefix: str = "") -> str:
    return prefix + BASE + ("" if shape == "unknown" else f" (reason={shape})")


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("prefix", ["", "hybrid direct failed: "])
def test_report_shape_is_finite_and_never_a_generic_empty_response(shape, prefix):
    reason = report_reason(shape, prefix)
    diagnostic = safe_runtime_failure_diagnostic(RuntimeError(reason))
    assert diagnostic["error_stage"] == "delivery_report"
    assert diagnostic["error_category"] == shape
    assert diagnostic["error_code"] == "delivery_report." + shape
    assert diagnostic["retryable"] is False
    assert diagnostic["error_summary"] == report_reason(shape)
    assert "压缩输入" not in diagnostic["suggested_action"]
    if prefix:
        assert diagnostic["hybrid_child_mode"] == "direct"
    else:
        assert "hybrid_child_mode" not in diagnostic
    assert safe_runtime_failure_reason(RuntimeError(reason)) == reason


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("prefix", ["", "hybrid direct failed: "])
@pytest.mark.parametrize("requires_approval", [True, False])
def test_report_failure_records_only_skipped_without_retry_or_approval(shape, prefix, requires_approval):
    run_id = uuid4()
    reason = report_reason(shape, prefix)
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=4, run_id=run_id,
                     reason=reason, payload=diagnostic)
    # An unrelated observer recommendation must not turn a report diagnostic
    # into a model/configuration repair or a new paid run.
    observer = RunEvent(kind="observer.notice", sequence=5, run_id=run_id,
                        payload={"recommendation": "compress_context_and_retry"})
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.HYBRID,
        routing_decision={"source": "manual", "engineering_auto_repair_version": 1},
        events=(event, observer), policy=SelfRepairPolicy(requires_approval=requires_approval))
    assert decision is not None
    assert decision.failure_category == "delivery_report"
    assert decision.error_code == "delivery_report." + shape
    assert decision.kind == "repair.skipped"
    assert decision.skip_reason == "delivery_report_no_safe_recovery"
    assert decision.action == "manual_review"
    assert decision.requires_approval is False
    assert decision.automatic_execution is False and decision.pauses_run is False
    assert decision.recovery_strategy is None and decision.orchestration_recovery_hint is None
    assert decision.automatic_checkpoint_sha256 is None and decision.automatic_scope_sha256 is None
    assert decision.to_proposal(run_id=run_id) is None
    recorded = decision.to_event(run_id=run_id, sequence=6)
    assert recorded.payload["error_code"] == "delivery_report." + shape
    assert recorded.payload["automatic_execution"] is False
    assert decision.with_source_sequence(7).to_proposal(run_id=run_id) is None
    assert classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.HYBRID,
        routing_decision={}, events=(event, recorded), policy=SelfRepairPolicy()) is None


@pytest.mark.parametrize("prefix", ["", "hybrid direct failed: "])
@pytest.mark.parametrize("suffix", [
    "(reason=json_only)",
    " (reason=private_provider_text)",
    " (reason=json_only; Authorization: Bearer sk-private-key)",
    " (reason=json_only) private_provider_text",
])
def test_report_suffix_only_projects_finite_allowlisted_fields(prefix, suffix):
    raw = prefix + BASE + suffix
    accepted = suffix == "(reason=json_only)"
    shape = "json_only" if accepted else "unknown"
    canonical = report_reason(shape, prefix)
    assert safe_runtime_failure_reason(RuntimeError(raw)) == canonical
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_stage"] == "delivery_report"
    assert diagnostic["error_category"] == shape
    assert diagnostic["error_code"] == "delivery_report." + shape
    assert diagnostic["error_summary"] == report_reason(shape)
    assert "private_provider_text" not in json.dumps(diagnostic)
    assert "sk-private-key" not in json.dumps(diagnostic)


def test_report_native_reason_cannot_be_replaced_by_retry_hint_payload():
    run_id = uuid4()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id,
        reason=report_reason("json_only"), payload={
            **runtime_failure_diagnostic_from_reason("model response text is empty"),
            "suggested_action": "private_provider_text sk-private-key",
        })
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event,), policy=SelfRepairPolicy(requires_approval=False))
    assert decision is not None
    assert decision.kind == "repair.skipped" and decision.failure_category == "delivery_report"
    assert decision.error_code == "delivery_report.json_only"
    payload = json.dumps(decision.to_event(run_id=run_id, sequence=2).to_payload())
    assert "private_provider_text" not in payload and "sk-private-key" not in payload


@pytest.mark.parametrize("uncertainty", ["payload", "model", "tool"])
def test_unknown_outcome_wins_over_report_format_and_never_replays(uncertainty):
    run_id = uuid4()
    events = [RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=2, run_id=run_id,
                      reason=report_reason("json_only"))]
    if uncertainty == "payload":
        events.insert(0, RunEvent(kind=EventKind.TOOL_FAILED, sequence=1, run_id=run_id,
            actor="main_agent", tool_call_id="call_1", tool_name="workspace_read",
            reason="capability execution failed", payload={"failure_kind": "uncertain"}))
    else:
        events.insert(0, RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id,
                                 reason=f"{uncertainty}_outcome_uncertain"))
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.HYBRID,
        routing_decision={}, events=events, policy=SelfRepairPolicy(requires_approval=False))
    assert decision is not None
    assert decision.failure_category == "outcome_uncertain"
    assert decision.kind == "repair.skipped"
    assert decision.skip_reason == "side_effect_outcome_uncertain"
    assert decision.automatic_execution is False and decision.to_proposal(run_id=run_id) is None


@pytest.mark.parametrize("reason,stage,code,category", [
    ("model response text is empty", "model_response", "model.empty_response", "empty_model_response"),
    ("model_outcome_uncertain", "runtime_accounting", "runtime.model_outcome_uncertain", "outcome_uncertain"),
    ("tool_outcome_uncertain", "runtime_accounting", "runtime.tool_outcome_uncertain", "outcome_uncertain"),
    ("model gateway failed: model transport failed (status=502)",
     "model_provider", "model.provider_unavailable", "capacity_pressure"),
])
def test_nonreport_failure_keeps_existing_classification(reason, stage, code, category):
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    assert diagnostic["error_stage"] == stage and diagnostic["error_code"] == code
    run_id = uuid4()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id,
                     reason=reason, payload=diagnostic)
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event,), policy=SelfRepairPolicy())
    assert decision is not None and decision.failure_category == category


def test_transport_exception_is_not_inferred_as_received_report_from_its_text():
    diagnostic = safe_runtime_failure_diagnostic(ModelTransportError(report_reason("json_only")))
    assert diagnostic["error_code"] != "delivery_report.json_only"
    assert diagnostic["error_stage"] == "model_provider"


@pytest.mark.parametrize("requires_approval", [True, False])
async def test_record_only_report_never_enters_service_enqueue_or_checkpoint_recovery(requires_approval):
    run_id = uuid4()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id,
                     reason=report_reason("json_only"))
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event,), policy=SelfRepairPolicy(requires_approval=requires_approval))
    assert decision is not None
    # The real service must return before it reads repository/checkpoint state,
    # creates an approval, accepts a repair, or enqueues another run.
    class NoExecution:
        def __getattr__(self, name):
            pytest.fail("report diagnostic attempted service work: " + name)

    service = NoExecution()
    assert await RunService._safe_project_repair_decision(
        service, tenant_id=uuid4(), run_id=run_id, decision=decision) == decision
    assert await RunService._safe_auto_accept_self_repair_if_allowed(
        service, tenant_id=uuid4(), run_id=run_id, decision=decision) is None
