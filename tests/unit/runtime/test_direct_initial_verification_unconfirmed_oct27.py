"""Offline diagnostic REDs; never grant process-failure credit or resume old runs.

The post-execution cases use real generated/service/product/Harness boundaries;
only native execution and approval decisions are doubles. backend_error_code is
the proposed finite diagnostic interface, not recovery authority.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.capabilities import test_python_generated_service as generated
from tests.unit.capabilities.test_python_product_delivery import attach_product


class NoModel:
    async def complete_with_context(self, request: Any) -> Any:
        raise AssertionError("diagnostic reproduction must not call a model")


class Policy:
    def __init__(self, status: CapabilityStatus = CapabilityStatus.ALLOWED) -> None:
        self.status = status

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        assert role is Role.OPERATOR
        return CapabilityResult(status=self.status, run_id=request.run_id,
            approval_id="approval_local_only" if self.status is CapabilityStatus.WAITING_APPROVAL else None)


class RecordingHarness(HarnessToolGateway):
    def __init__(self, backend: Any, status: CapabilityStatus = CapabilityStatus.ALLOWED) -> None:
        super().__init__(backend, policy_gateway=Policy(status), require_actor_identity=True)
        self.results: list[HarnessToolCallResult] = []

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest,
                     **kwargs: Any) -> HarnessToolCallResult:
        result = await super().invoke(tenant_id, request, **kwargs)
        self.results.append(result)
        return result


def context(*, selected: bool = True) -> TaskContext:
    return TaskContext(tenant_id=uuid4(), run_id=uuid4(), actor_id=uuid4(),
        actor_role=Role.OPERATOR, mode=TaskMode.DIRECT, request="Build a ticket CSV CLI.",
        token_budget=50_000, timeout_seconds=60, routing_decision={
            "project_delivery": "workspace", "sandbox_profile": "workspace_write",
            "project_id": "diagnostic-only", "workspace_session_id": "diagnostic-only",
            **({"generated_project_contract_version": 1} if selected else {})})


async def reject(runtime: DirectRuntime, ctx: TaskContext, trace: Any = None) -> RuntimeExecutionError:
    with pytest.raises(RuntimeExecutionError) as caught:
        await runtime._verify_workspace(ctx, known_files={}, attempt=1,
            deadline=asyncio.get_running_loop().time() + 30, verification_trace=trace)
    assert not hasattr(caught.value, "result"), "no trusted process failure may be fabricated"
    assert runtime._active_capability_task is None
    return caught.value


def test_imports_are_from_this_worktree_not_editable_primary() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "agent_hub"
    assert Path(inspect.getfile(DirectRuntime)).resolve() == root / "runtime" / "direct.py"
    assert Path(inspect.getfile(HarnessToolGateway)).resolve() == root / "harness" / "tool_gateway.py"
    assert Path(inspect.getfile(RuntimeCapabilityGateway)).resolve() == root / "capabilities" / "runtime.py"


@pytest.mark.parametrize("selected", [False, True], ids=["legacy", "generated"])
@pytest.mark.parametrize(("status", "reason", "expected"), [
    (CapabilityStatus.DENIED, "capability denied", "capability_denied"),
    (CapabilityStatus.WAITING_APPROVAL, "capability requires approval", "approval_required"),
])
async def test_known_gate_rejection_keeps_category_without_execution_or_repair(
    selected: bool, status: CapabilityStatus, reason: str, expected: str,
) -> None:
    class NeverExecute:
        def is_available(self, tenant_id: UUID, name: str) -> bool:
            return True

        async def execute(self, **kwargs: Any) -> Mapping[str, JsonValue]:
            raise AssertionError("policy rejected before executor")

    backend = NeverExecute()
    harness = RecordingHarness(backend, status)
    runtime = DirectRuntime(cast(Any, NoModel()), logical_model="fixture",
        capability_gateway=cast(Any, backend), harness_tool_gateway=harness)
    error = await reject(runtime, context(selected=selected))
    assert len(harness.results) == 1
    assert harness.results[0].failure_reason == reason
    assert getattr(error, "failure_code", None) == expected


@pytest.mark.parametrize("defect", ["stdout", "missing_stream"])
async def test_post_execution_product_rejection_retains_finite_diagnostic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    api = importlib.import_module("agent_hub.capabilities.python_generated_service")
    service, _, repo, executor, _, prepared = await generated.configured(api, tmp_path)
    exact = await service.authorization_request(generated.request(actor="main_agent"), prepared)
    generated.approve(repo, exact)
    owner = service.owner
    owner._python_generated = service
    streams = attach_product(executor, monkeypatch, defect=defect)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills",
        project_workspace_dir=owner.root, run_repository=repo,
        project_executor=cast(Any, object()))
    backend._project_verification = owner
    route = {**repo.row.routing_decision, "project_delivery": "workspace",
        "artifact_strategy": "workspace_bundle"}
    ctx = TaskContext(tenant_id=repo.row.tenant_id, run_id=repo.row.id,
        actor_id=repo.row.actor_id, actor_role=repo.row.actor_role,
        request=repo.row.request, mode=TaskMode.DIRECT, routing_decision=route,
        token_budget=50_000, timeout_seconds=60)
    harness = RecordingHarness(backend)
    runtime = DirectRuntime(cast(Any, NoModel()), logical_model="fixture",
        capability_gateway=backend, harness_tool_gateway=harness)
    from agent_hub.runtime.direct import _WorkspaceVerificationTrace

    trace = _WorkspaceVerificationTrace(context=ctx)
    error = await reject(runtime, ctx, trace)
    assert len(executor.calls) == 1, "native double executes exactly once"
    assert repo.row.routing_decision["generated_project_preparation"]["phase"] == "python_ready"
    assert all(stream.closed for stream in streams)
    assert len(harness.results) == 1
    result = harness.results[0]
    assert result.status == "failed" and result.failure_reason == "python_project_product_unavailable"
    assert not {"execution_diagnostic", "python_verification_failure", "verification"} & result.payload.keys()
    assert not service.receipts and not owner._pending_python_products
    # A failed handoff cannot prove a failed build/test. Keep uncertainty, but
    # retain the exact finite backend category without raw stdout/paths/secrets.
    assert getattr(error, "failure_code", None) == "outcome_unconfirmed"
    assert getattr(error, "backend_error_code", None) == "python_project_product_unavailable"
    history = trace.to_payload()
    assert history["attempt_count"] == 1 and history["failed_attempt_count"] == 0
    assert history["final_receipt"] is None and not history["repair_observed"]
    entry = cast(Any, history["attempts"])[0]
    assert entry["status"] == "unconfirmed" and entry["call_id"] == result.call_id
    assert entry["failure_reason"] == "python_project_product_unavailable"
    assert "checks" not in entry and "failure_receipt_sha256" not in entry


@pytest.mark.parametrize("reason", ["UNKNOWN", "tool execution failed",
    "python_project_product_unavailable: PRIVATE_BACKEND_TEXT"])
async def test_unknown_and_unrecognized_backend_text_never_gain_failure_credit(reason: str) -> None:
    class FailedResult:
        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest,
                         **kwargs: Any) -> HarnessToolCallResult:
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="failed", failure_reason=reason, payload={})

    runtime = DirectRuntime(cast(Any, NoModel()), logical_model="fixture",
        harness_tool_gateway=cast(Any, FailedResult()))
    error = await reject(runtime, context())
    assert getattr(error, "failure_code", None) == "outcome_unconfirmed"
    assert getattr(error, "backend_error_code", None) is None
    assert "PRIVATE_BACKEND_TEXT" not in str(error)


@pytest.mark.parametrize("check", ["tool_event", "model_attribution"])
async def test_generated_engineering_failure_has_owned_safe_event_not_model_failure(
    tmp_path: Path, check: str,
) -> None:
    from agent_hub.runtime.contracts import EventKind
    from tests.unit.runtime.test_direct_generated_entry import Harness, Model, batch, response
    from tests.unit.runtime.test_generated_project_history import evidence

    ctx, verified, bundle = await evidence(tmp_path)
    ctx = ctx.model_copy(update={"token_budget": 50_000, "routing_decision": {
        **ctx.routing_decision, "sandbox_profile": "workspace_write"}})

    class FailedHarness(Harness):
        async def invoke(self, tenant: Any, request: Any, **kwargs: Any) -> HarnessToolCallResult:
            if request.tool_name != "project.verify":
                return await super().invoke(tenant, request, **kwargs)
            self.calls.append(request.tool_name)
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="failed", failure_reason="python_project_product_unavailable",
                payload={"stdout": "PRIVATE_BODY", "storage_key": "PRIVATE_KEY"})

    harness = FailedHarness(ctx, verified, bundle)
    model = Model(response(batch()))
    runtime = DirectRuntime(model, logical_model="fixture", capability_gateway=harness,
        harness_tool_gateway=cast(Any, harness))
    events = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(ctx):
            events.append(event)
    assert len(model.requests) == 1 and harness.calls.count("project.verify") == 1
    assert "workspace.bundle" not in harness.calls
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    if check == "model_attribution":
        assert not any(event.kind == "model.failure_receipt" for event in events)
    else:
        failures = [event for event in events if event.artifact is not None
            and event.artifact.content.get("tool_name") == "project.verify"]
        assert len(failures) == 1
        artifact = failures[0].artifact
        assert artifact is not None and artifact.content["status"] == "failed"
        result = cast(Any, artifact.content["result"])["verification_backend_failure"]
        assert result["failure_reason"] == "python_project_product_unavailable"
        assert result["process_failure_confirmed"] is False and result["retryable"] is False
        assert "PRIVATE_" not in repr(events)


@pytest.mark.parametrize("defect", ["call", "tool", "truncated"])
async def test_unowned_or_truncated_result_never_issues_backend_diagnostic(defect: str) -> None:
    class InvalidResult:
        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest,
                         **kwargs: Any) -> HarnessToolCallResult:
            return HarnessToolCallResult(call_id="foreign" if defect == "call" else request.call_id,
                tool_name="workspace.bundle" if defect == "tool" else request.tool_name,
                status="failed", failure_reason="python_project_product_unavailable",
                payload={}, truncated=defect == "truncated")

    runtime = DirectRuntime(cast(Any, NoModel()), logical_model="fixture",
        harness_tool_gateway=cast(Any, InvalidResult()))
    error = await reject(runtime, context())
    assert getattr(error, "failure_code", None) == ("truncated_response" if defect == "truncated" else "invalid_response")
    assert getattr(error, "backend_error_code", None) is None
    assert getattr(error, "tool_failure_artifact", None) is None


@pytest.mark.parametrize("code", ["PRIVATE_BACKEND_TEXT", "python_project_product_unavailable:PRIVATE",
    " python_project_product_unavailable", "python_project_unknown_private_key"])
def test_exception_constructor_cannot_publish_unrecognized_backend_code(code: str) -> None:
    from agent_hub.runtime.direct import _WorkspaceVerificationError

    error = _WorkspaceVerificationError("outcome_unconfirmed", backend_error_code=code)
    assert error.backend_error_code is None
    assert str(error) == "project verification failed: outcome unconfirmed"


@pytest.mark.parametrize("reason", ["python_project_execution_unavailable", "python_project_checks_failed",
    "input_supply_unknown", "python_dependency_draft_changed", "python_metadata.prepare.metadata_prepare_timeout"])
async def test_finite_protocol_causes_are_diagnostic_only_even_when_their_outcome_is_unknown(reason: str) -> None:
    class FiniteResult:
        calls = 0

        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest,
                         **kwargs: Any) -> HarnessToolCallResult:
            self.calls += 1
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="failed", failure_reason=reason, payload={"private": "PRIVATE_BODY"})

    gateway = FiniteResult()
    runtime = DirectRuntime(cast(Any, NoModel()), logical_model="fixture",
        harness_tool_gateway=cast(Any, gateway))
    error = await reject(runtime, context())
    assert gateway.calls == 1 and getattr(error, "failure_code", None) == "outcome_unconfirmed"
    assert getattr(error, "backend_error_code", None) == reason
    artifact = getattr(error, "tool_failure_artifact", None)
    assert artifact is not None
    diagnostic = artifact.content["result"]["verification_backend_failure"]
    assert diagnostic["retryable"] is False and diagnostic["process_failure_confirmed"] is False
    assert "PRIVATE_BODY" not in repr(artifact)
