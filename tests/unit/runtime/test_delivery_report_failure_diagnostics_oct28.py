"""Cross-mode report diagnostics must stop at observation, never repair authority."""

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

# Literal expectations come from the actual Crew/AutoGen/Hybrid rejection sites.
# Aggregate text/model guards cannot establish json_only or empty_text.
REPORTS = [
    ("delivery report policy is invalid", "policy_invalid"),
    ("delivery report workspace writers are unordered", "binding_invalid"),
    ("delivery report requires current trusted verification and bundle", "evidence_unavailable"),
    ("delivery report tool prefix is unresolved or unproven", "unknown"),
    ("delivery report tool outcome changed or is unavailable", "unknown"),
    ("delivery report tool outcome is unavailable", "evidence_unavailable"),
    ("delivery report outcomes are missing", "evidence_unavailable"),
    ("delivery report outcome is invalid", "binding_invalid"),
    ("delivery report requires its single succeeded native model outcome", "unknown"),
    ("delivery report does not match current native evidence", "unknown"),
    ("delivery report text is unavailable", "unknown"),
    ("delivery report persisted binding is invalid", "binding_invalid"),
    ("delivery report scope or binding is invalid", "binding_invalid"),
    ("delivery report artifact registry is ambiguous", "binding_invalid"),
    ("delivery report current native artifact is unavailable", "evidence_unavailable"),
    ("delivery report native text is invalid", "unknown"),
    ("delivery report tool lineage is invalid", "binding_invalid"),
    ("delivery report tool evidence is invalid", "binding_invalid"),
    ("delivery report bundle verification is invalid", "binding_invalid"),
    ("engineering delivery report version is invalid", "policy_invalid"),
    ("delivery report requires a natural language final step", "policy_invalid"),
    ("delivery report framework text differs from native model output", "binding_invalid"),
    ("delivery report allows only its existing single native model call", "binding_invalid"),
    ("delivery report requires a no-tool native model request", "unknown"),
    ("delivery report evidence changed after checkpoint", "binding_invalid"),
    ("delivery report checkpoint policy is invalid", "policy_invalid"),
    ("delivery report checkpoint binding is missing or misplaced", "binding_invalid"),
    ("final report outcome reference is invalid", "binding_invalid"),
    ("final report requires a final succeeded bundle row", "evidence_unavailable"),
    ("final report tool prefix is invalid", "binding_invalid"),
    ("final report tail must contain only succeeded readonly outcomes", "unknown"),
    ("final report tool outcome is invalid", "binding_invalid"),
    ("final report artifact registry is ambiguous", "binding_invalid"),
    ("final report policy or text is invalid", "unknown"),
    ("final report phase binding is invalid", "binding_invalid"),
    ("final report requires a succeeded text-only model row", "unknown"),
    ("final report fallback provenance is inconsistent", "binding_invalid"),
    ("final report fallback provenance is invalid", "binding_invalid"),
    ("final report model outcome is invalid", "unknown"),
    ("final report cannot invoke tools", "tool_calls"),
    ("final report budget is unavailable", "budget_unavailable"),
    ("final report budget is exhausted", "budget_exhausted"),
    ("final report phase is unavailable", "evidence_unavailable"),
    ("final report evidence is unavailable", "evidence_unavailable"),
    ("final report evidence is invalid", "unknown"),
    ("final report configuration is invalid", "policy_invalid"),
    ("final_report_missing", "report_missing"),
    ("durable final report binding mismatch", "binding_invalid"),
    ("durable final report request mismatch", "binding_invalid"),
    ("durable final report outcome is invalid", "unknown"),
    ("durable final report phase is invalid", "binding_invalid"),
    ("hybrid final report checkpoint proof is invalid", "binding_invalid"),
    ("hybrid final report checkpoint policy changed", "policy_invalid"),
    ("hybrid final delivery report is missing", "report_missing"),
    ("hybrid final delivery inputs are ambiguous", "binding_invalid"),
    ("hybrid final delivery verification is unavailable", "evidence_unavailable"),
    ("hybrid final delivery reference is unavailable", "evidence_unavailable"),
    ("hybrid final delivery model lineage is unavailable", "evidence_unavailable"),
    ("hybrid final delivery frame is missing", "evidence_unavailable"),
    ("hybrid final delivery frame changed", "binding_invalid"),
    ("hybrid final delivery evidence is unavailable", "evidence_unavailable"),
]


@pytest.mark.parametrize("reason,category", REPORTS)
def test_exact_current_report_rejection_has_finite_nonretryable_stage(reason, category):
    diagnostic = safe_runtime_failure_diagnostic(RuntimeError(reason))
    assert diagnostic == runtime_failure_diagnostic_from_reason(reason)
    assert diagnostic["error_stage"] == "delivery_report"
    assert diagnostic["error_category"] == category
    assert diagnostic["error_code"] == "delivery_report." + category
    assert diagnostic["error_summary"] == reason
    assert diagnostic["retryable"] is False
    assert safe_runtime_failure_reason(RuntimeError(reason)) == reason
    RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
             reason=reason, payload=diagnostic)


@pytest.mark.parametrize("reason,mode,wrapper", [
    ("delivery report does not match current native evidence", TaskMode.DISPATCH, ""),
    ("final report evidence is invalid", TaskMode.DISCUSS, ""),
    ("hybrid final delivery report is missing", TaskMode.HYBRID, ""),
    ("delivery report checkpoint policy is invalid", TaskMode.HYBRID, "hybrid dispatch failed: "),
    ("final_report_missing", TaskMode.HYBRID, "HYBRID DISCUSS FAILED: "),
    ("final report policy or text is invalid", TaskMode.HYBRID, "Hybrid Direct Failed: "),
])
async def test_real_terminal_consumer_records_only_no_approval_proposal_or_service_action(reason, mode, wrapper):
    run_id = uuid4()
    raw = wrapper + reason
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_stage"] == "delivery_report"
    if wrapper:
        assert diagnostic["hybrid_child_mode"] == wrapper.split()[1].lower()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id, reason=raw,
        payload={**runtime_failure_diagnostic_from_reason("model response text is empty"),
                 "suggested_action": "PRIVATE_RETRY_HINT"})
    # The production policy defaults to approval. A report observation must not
    # become an approval even under that policy or an auto-repair route.
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=mode,
        routing_decision={"source": "manual", "engineering_auto_repair_version": 1},
        events=(event,), policy=SelfRepairPolicy(requires_approval=True))
    assert decision is not None and decision.failure_category == "delivery_report"
    assert decision.kind == "repair.skipped" and decision.action == "manual_review"
    assert decision.requires_approval is False and decision.automatic_execution is False
    assert decision.recovery_strategy is None and decision.orchestration_recovery_hint is None
    assert decision.to_proposal(run_id=run_id) is None
    assert "PRIVATE_RETRY_HINT" not in json.dumps(decision.to_event(run_id=run_id, sequence=2).to_payload())

    class NoExecution:
        def __getattr__(self, name):
            pytest.fail("report classification attempted service work: " + name)

    assert await RunService._safe_project_repair_decision(
        NoExecution(), tenant_id=uuid4(), run_id=run_id, decision=decision) == decision
    assert await RunService._safe_auto_accept_self_repair_if_allowed(
        NoExecution(), tenant_id=uuid4(), run_id=run_id, decision=decision) is None


@pytest.mark.parametrize("raw", [
    "delivery report does not match current native evidence PRIVATE_PROVIDER_TEXT",
    "hybrid discuss failed: final report evidence is invalid PRIVATE_PROVIDER_TEXT",
    "HYBRID DISPATCH FAILED: delivery report text is unavailable (password=PRIVATE_KEY)",
    "final_report_missing: PRIVATE_PROVIDER_TEXT",
    "hybrid final delivery report is missing (reason=empty_text)",
    "durable final report outcome is invalid (reason=model_outcome_uncertain)",
    "final report invented failure",
    "delivery report invented failure",
    "hybrid direct failed: FINAL REPORT EVIDENCE IS INVALID",
])
def test_unknown_report_text_is_redacted_without_granting_report_credit_or_retry(raw):
    assert safe_runtime_failure_reason(RuntimeError(raw)) == "runtime_failed"
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_code"] == "runtime.failed"
    assert diagnostic["error_summary"] == "runtime_failed"
    assert diagnostic["retryable"] is False
    assert "hybrid_child_mode" not in diagnostic


@pytest.mark.parametrize("depth", [2, 3, 4, 64, 1024])
@pytest.mark.parametrize("tail", ["final_report_missing", "delivery report policy is invalid PRIVATE_PROVIDER_TEXT"])
def test_nested_wrappers_fail_closed_at_any_depth(depth, tail):
    wrappers = ["hybrid direct failed: ", "HYBRID DISPATCH FAILED: ", "Hybrid Discuss Failed: "]
    raw = "".join(wrappers[index % 3] for index in range(depth)) + tail
    assert safe_runtime_failure_reason(RuntimeError(raw)) == "runtime_failed"
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_stage"] == "runtime"
    assert diagnostic["error_code"] == "runtime.failed" and diagnostic["retryable"] is False
    assert "PRIVATE_" not in json.dumps(diagnostic)
    assert "hybrid_child_mode" not in diagnostic


@pytest.mark.parametrize("reason,code", [
    ("model response text is empty", "model.empty_response"),
    ("model_outcome_uncertain", "runtime.model_outcome_uncertain"),
    ("tool_outcome_uncertain", "runtime.tool_outcome_uncertain"),
    ("model gateway failed: model transport failed (status=502)", "model.provider_unavailable"),
    ("hybrid discuss failed: model_outcome_uncertain", "runtime.model_outcome_uncertain"),
    ("final report cannot hide an uncertain model outcome", "runtime.model_outcome_uncertain"),
])
def test_model_or_uncertain_outcome_is_never_mislabeled_as_received_report(reason, code):
    diagnostic = safe_runtime_failure_diagnostic(RuntimeError(reason))
    assert diagnostic["error_code"] == code
    assert diagnostic["error_stage"] != "delivery_report"


def test_typed_transport_failure_wins_over_report_looking_exception_text():
    diagnostic = safe_runtime_failure_diagnostic(
        ModelTransportError("final report evidence is invalid", status_code=503))
    assert diagnostic["error_code"] == "model.provider_unavailable"
    assert diagnostic["error_stage"] == "model_provider"


@pytest.mark.parametrize("uncertainty", ["model_outcome_uncertain", "tool_outcome_uncertain", "payload"])
def test_existing_uncertain_evidence_wins_over_later_report_rejection(uncertainty):
    run_id = uuid4()
    first = (RunEvent(kind=EventKind.TOOL_FAILED, sequence=1, run_id=run_id,
        actor="main_agent", tool_call_id="call_1", tool_name="workspace_read",
        reason="capability execution failed", payload={"failure_kind": "uncertain"})
        if uncertainty == "payload" else RunEvent(kind=EventKind.RUNTIME_FAILED,
            sequence=1, run_id=run_id, reason=uncertainty))
    last = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=2, run_id=run_id,
        reason="final report evidence is invalid")
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DISCUSS,
        routing_decision={}, events=(first, last), policy=SelfRepairPolicy())
    assert decision is not None and decision.failure_category == "outcome_uncertain"
    assert decision.kind == "repair.skipped" and decision.automatic_execution is False
    assert decision.to_proposal(run_id=run_id) is None


def test_unrelated_report_wording_is_not_accepted_by_substring():
    diagnostic = runtime_failure_diagnostic_from_reason("provider says final_report_missing")
    assert diagnostic["error_stage"] != "delivery_report"


@pytest.mark.parametrize("requires_approval", [True, False])
def test_live_action_waiting_approval_contract_is_unchanged(requires_approval):
    run_id = uuid4()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id,
        reason="delivery report requires a no-tool native model request")
    assert classify_terminal_run(status=RunStatus.WAITING_APPROVAL, mode=TaskMode.DISPATCH,
        routing_decision={}, events=(event,),
        policy=SelfRepairPolicy(requires_approval=requires_approval)) is None
