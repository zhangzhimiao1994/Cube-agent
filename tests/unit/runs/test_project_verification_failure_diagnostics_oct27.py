"""Finite verification diagnostics cannot authorize a retry or platform repair."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.direct import (
    _VERIFICATION_BACKEND_REASONS,
    _VERIFICATION_FAILURE_CODES,
    _WorkspaceVerificationError,
)
from agent_hub.runtime.failure_reason import (
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_diagnostic,
    safe_runtime_failure_reason,
)

BASE = "project verification failed"
PRODUCT = "python_project_product_unavailable"


def reason(code="outcome_unconfirmed", backend=PRODUCT, prefix=""):
    return prefix + str(_WorkspaceVerificationError(code, backend_error_code=backend))


@pytest.mark.parametrize("prefix", ["", "hybrid direct failed: ", "hybrid dispatch failed: ", "hybrid discuss failed: "])
@pytest.mark.parametrize("backend", [PRODUCT, "python_project_execution_unavailable", "input_supply_unknown"])
def test_current_finite_backend_reason_is_engineering_not_model_or_internal(prefix, backend):
    raw = reason(backend=backend, prefix=prefix)
    diagnostic = safe_runtime_failure_diagnostic(RuntimeError(raw))
    assert diagnostic["error_stage"] == "project_verification"
    assert diagnostic["error_category"] == "backend"
    assert diagnostic["error_code"] == "project_verification." + backend
    assert diagnostic["retryable"] is False
    assert diagnostic["error_summary"] == reason(backend=backend)
    RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(), reason=raw, payload=diagnostic)
    assert safe_runtime_failure_reason(RuntimeError(raw)) == raw
    assert "inspect/reconcile existing execution" in diagnostic["suggested_action"]
    if prefix:
        assert diagnostic["hybrid_child_mode"] == prefix.split()[1]


def test_all_current_direct_literals_have_exact_central_coverage():
    for code in _VERIFICATION_FAILURE_CODES:
        raw = reason(code=code, backend=None)
        diagnostic = safe_runtime_failure_diagnostic(RuntimeError(raw))
        assert diagnostic["error_stage"] == "project_verification", raw
        assert diagnostic["error_code"] == "project_verification." + code
        assert diagnostic["error_summary"] == raw
        assert diagnostic["retryable"] is False
    for backend in _VERIFICATION_BACKEND_REASONS:
        raw = reason(backend=backend)
        diagnostic = safe_runtime_failure_diagnostic(RuntimeError(raw))
        assert diagnostic["error_code"] == "project_verification." + backend
        assert diagnostic["error_summary"] == raw
        assert safe_runtime_failure_reason(RuntimeError(raw)) == raw
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(), reason=raw, payload=diagnostic)


@pytest.mark.parametrize("backend", [None, PRODUCT, "python_project_cleanup_incomplete", "input_supply_unknown"])
@pytest.mark.parametrize("requires_approval", [True, False])
def test_verification_never_proposes_platform_repair_or_replays_existing_execution(backend, requires_approval):
    run_id = uuid4()
    raw = reason(backend=backend)
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=3, run_id=run_id, reason=raw,
        payload={**runtime_failure_diagnostic_from_reason("model response text is empty"),
            "suggested_action": "PRIVATE_RETRY_HINT"})
    observer = RunEvent(kind="observer.notice", sequence=4, run_id=run_id,
        payload={"recommendation": "compress_context_and_retry"})
    policy = SelfRepairPolicy(requires_approval=requires_approval)
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={"source": "manual", "engineering_auto_repair_version": 1},
        events=(event, observer), policy=policy)
    assert decision is not None and decision.failure_category == "project_verification"
    assert decision.kind == "repair.skipped" and decision.action == "manual_review"
    assert decision.skip_reason == "project_verification_no_safe_recovery"
    assert decision.requires_approval is False
    assert policy.requires_approval is requires_approval
    assert decision.automatic_execution is False and decision.pauses_run is False
    assert decision.recovery_strategy is None and decision.orchestration_recovery_hint is None
    assert decision.automatic_checkpoint_sha256 is None and decision.automatic_scope_sha256 is None
    assert decision.error_code == "project_verification." + (backend or "outcome_unconfirmed")
    assert decision.to_proposal(run_id=run_id) is None
    recorded = decision.to_event(run_id=run_id, sequence=5)
    assert recorded.payload["requires_approval"] is False
    assert recorded.payload["automatic_execution"] is False
    assert "PRIVATE_RETRY_HINT" not in json.dumps(recorded.to_payload())
    assert "inspect/reconcile existing execution" in decision.suggested_action
    assert classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event, recorded), policy=SelfRepairPolicy()) is None


@pytest.mark.parametrize("code", ["approval_required", "capability_denied", "identity_unavailable", "scope_denied"])
def test_permissions_stay_current_scoped_and_never_gain_auto_execution(code):
    raw = reason(code=code, backend=None)
    diagnostic = safe_runtime_failure_diagnostic(RuntimeError(raw))
    assert diagnostic["error_stage"] == "project_verification"
    assert diagnostic["error_category"] == "permission"
    assert "当前具体动作" in diagnostic["suggested_action"]
    assert "审批" in diagnostic["suggested_action"]
    run_id = uuid4()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id, reason=raw)
    # The terminal classifier must not take over a live current-action approval.
    assert classify_terminal_run(status=RunStatus.WAITING_APPROVAL, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event,), policy=SelfRepairPolicy()) is None
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event,), policy=SelfRepairPolicy(requires_approval=True))
    assert decision is not None and decision.requires_approval is False
    assert decision.to_event(run_id=run_id, sequence=2).payload["requires_approval"] is False
    assert decision.automatic_execution is False and decision.to_proposal(run_id=run_id) is None


@pytest.mark.parametrize("raw", [
    BASE + ": invented failure",
    BASE + ": outcome unconfirmed (backend=python_project_UNKNOWN)",
    BASE + ": outcome unconfirmed (backend=python_project_product_unavailable) PRIVATE_TEXT",
    BASE + ": outcome unconfirmed (backend=python_project_product_unavailable; password=PRIVATE_TEXT)",
    BASE + ": outcome unconfirmed (backend=python_project_product_unavailable/PRIVATE_PATH)",
    "hybrid direct failed: hybrid discuss failed: " + BASE + ": outcome unconfirmed",
])
def test_arbitrary_or_nested_failure_strings_do_not_gain_engineering_exception(raw):
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_stage"] != "project_verification"
    assert "verification_backend_code" not in diagnostic
    assert "PRIVATE_" not in json.dumps(diagnostic)
    assert "PRIVATE_" not in safe_runtime_failure_reason(RuntimeError(raw))


def test_model_transport_type_wins_over_project_looking_exception_body():
    diagnostic = safe_runtime_failure_diagnostic(ModelTransportError(reason(), status_code=503))
    assert diagnostic["error_stage"] == "model_provider"
    assert diagnostic["error_code"] == "model.provider_unavailable"
    assert "verification_backend_code" not in diagnostic


def test_old_aggregate_has_no_invented_backend_or_execution_evidence():
    diagnostic = runtime_failure_diagnostic_from_reason(BASE)
    assert diagnostic["error_stage"] == "project_verification"
    assert diagnostic["error_category"] == "unknown"
    assert "verification_backend_code" not in diagnostic
    assert diagnostic["retryable"] is False


@pytest.mark.parametrize("requires_approval", [True, False])
async def test_record_only_verification_never_enters_service_recovery_or_enqueue(requires_approval):
    run_id = uuid4()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=run_id,
        reason=reason(backend="python_project_cleanup_incomplete"))
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DIRECT,
        routing_decision={}, events=(event,), policy=SelfRepairPolicy(requires_approval=requires_approval))
    assert decision is not None and decision.kind == "repair.skipped"

    class NoExecution:
        def __getattr__(self, name):
            pytest.fail("verification diagnostic attempted service work: " + name)

    service = NoExecution()
    assert await RunService._safe_project_repair_decision(
        service, tenant_id=uuid4(), run_id=run_id, decision=decision) == decision
    assert await RunService._safe_auto_accept_self_repair_if_allowed(
        service, tenant_id=uuid4(), run_id=run_id, decision=decision) is None


@pytest.mark.parametrize("depth", [3, 4, 64, 1024])
@pytest.mark.parametrize("case", ["lower", "upper", "mixed"])
@pytest.mark.parametrize("tail", [
    BASE + ": invented failure PRIVATE_SUFFIX",
    BASE + ": outcome unconfirmed (backend=python_project_product_unavailable) password=PRIVATE_SECRET",
    BASE + ": outcome unconfirmed (backend=python_project_product_unavailable)",
])
def test_wrapper_chain_is_rejected_at_any_depth_without_suffix_leak_or_retry(depth, case, tail):
    wrappers = ["hybrid direct failed: ", "hybrid dispatch failed: ", "hybrid discuss failed: "]
    if case == "upper":
        wrappers = [wrapper.upper() for wrapper in wrappers]
    elif case == "mixed":
        wrappers = [wrapper.title() for wrapper in wrappers]
    raw = "".join(wrappers[index % len(wrappers)] for index in range(depth)) + tail
    assert safe_runtime_failure_reason(RuntimeError(raw)) == "runtime_failed"
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_summary"] == "runtime_failed"
    assert diagnostic["error_code"] == "runtime.failed"
    assert diagnostic["retryable"] is False
    assert diagnostic["error_stage"] != "project_verification"
    assert "hybrid_child_mode" not in diagnostic
    assert "PRIVATE_" not in json.dumps(diagnostic)
    assert "python_project_product_unavailable" not in json.dumps(diagnostic)


@pytest.mark.parametrize("wrapper", ["hybrid direct failed: ", "HYBRID DISPATCH FAILED: ", "Hybrid Discuss Failed: "])
def test_single_wrapper_case_guard_keeps_only_the_existing_finite_contract(wrapper):
    raw = wrapper + reason()
    diagnostic = runtime_failure_diagnostic_from_reason(raw)
    assert diagnostic["error_stage"] == "project_verification"
    assert diagnostic["error_code"] == "project_verification." + PRODUCT
    assert diagnostic["hybrid_child_mode"] == wrapper.split()[1].lower()
    assert diagnostic["retryable"] is False
    assert safe_runtime_failure_reason(RuntimeError(raw)) == "hybrid " + wrapper.split()[1].lower() + " failed: " + reason()
