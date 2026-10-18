from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, Literal

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.runtime.test_direct_project_verification import Model, batch, context, runner


def process_diagnostic() -> ExecutionDiagnostic:
    return ExecutionDiagnostic(backend="fixture", result=SkillResult(
        exit_code=3, stdout="PRIVATE_PROJECT_OUTPUT", stderr="PRIVATE_PROJECT_ERROR",
        timed_out=False,
    ))


@pytest.mark.parametrize(("reason", "code"), [
    ("tool unavailable", "tool_unavailable"),
    ("capability denied", "capability_denied"),
    ("capability requires approval", "approval_required"),
    ("approval required", "approval_required"),
    ("project dependency installation approval required", "approval_required"),
    ("capability identity unavailable", "identity_unavailable"),
    ("tool permission mapping unavailable", "permission_mapping_unavailable"),
    ("project verification scope is not authorized", "scope_denied"),
    ("project execution backend is unavailable", "backend_unavailable"),
    ("project execution backend is unsupported", "backend_unsupported"),
    ("project source changed during verification", "source_changed"),
    ("project verification blocked by execution prerequisites", "blocked_prerequisites"),
    ("project execution report is invalid", "malformed_report"),
    ("project execution outcome is not confirmed", "outcome_unconfirmed"),
    ("tool execution failed", "tool_failed"),
    ("UNKNOWN", "failed"),
    ("tool unavailable: password=PRIVATE_SECRET", "failed"),
    ("project build failed: PRIVATE_PROJECT_OUTPUT", "failed"),
])
@pytest.mark.parametrize("with_diagnostic", [False, True])
async def test_direct_preserves_only_fixed_gate_categories_without_model_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, code: str,
    with_diagnostic: bool,
) -> None:
    ctx, model = context(), Model(batch({"main.py": "print('ready')\n"}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    execute = backend.execute

    async def fail_verification(*, name: str, **kwargs: Any) -> Any:
        if name == "project.verify":
            backend.calls.append(name)
            raise RuntimeCapabilityError(
                reason, execution_diagnostic=process_diagnostic() if with_diagnostic else None,
            )
        return await execute(name=name, **kwargs)

    monkeypatch.setattr(backend, "execute", fail_verification)
    events = []
    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(ctx):
            events.append(event)
    assert getattr(caught.value, "failure_code", None) == code
    assert str(caught.value) == (
        "project verification failed" if code == "failed"
        else "project verification failed: " + code.replace("_", " ")
    )
    assert not hasattr(caught.value, "result")
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(model.requests) == len(shared.requests) == 1
    assert backend.calls.count("project.verify") == 1
    assert "workspace.bundle" not in backend.calls
    assert not backend.process_results
    assert not any(event.kind in {EventKind.RUNTIME_COMPLETED, EventKind.CHECKPOINT_SAVED}
                   for event in events)
    public = str(caught.value) + repr(caught.value) + repr(events)
    assert "PRIVATE_" not in public
    with pytest.raises(RuntimeExecutionError, match="no completed runtime boundary"):
        await runtime.save_checkpoint()


@pytest.mark.parametrize(("case", "code"), [
    ("none", "invalid_response"),
    ("mapping", "invalid_response"),
    ("foreign_call", "invalid_response"),
    ("foreign_tool", "invalid_response"),
    ("truncated_failure", "truncated_response"),
    ("truncated_success", "truncated_response"),
    ("unknown_success", "not_passed"),
    ("missing_verification", "not_passed"),
    ("boolean_schema", "not_passed"),
    ("failed_without_diagnostic", "outcome_unconfirmed"),
    ("boolean_diagnostic_schema", "invalid_response"),
    ("boolean_exit_code", "invalid_response"),
    ("invalid_timeout", "invalid_response"),
    ("successful_diagnostic", "outcome_unconfirmed"),
])
async def test_direct_untrusted_verification_result_is_classified_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, code: str,
) -> None:
    ctx, model = context(), Model(batch({"main.py": "print('ready')\n"}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)

    async def respond(tenant_id: Any, request: HarnessToolCallRequest,
                      **kwargs: Any) -> Any:
        shared.requests.append(request)
        if case == "none":
            return None
        if case == "mapping":
            return {"status": "failed", "failure_reason": "tool unavailable"}
        diagnostic = dict(process_diagnostic().to_payload())
        payload: dict[str, Any] = {"execution_diagnostic": diagnostic}
        status: Literal["succeeded", "failed"] = "failed"
        reason: str | None = "project build failed"
        if case in {"truncated_failure", "foreign_call", "foreign_tool"}:
            reason = "tool unavailable"
        if case in {"truncated_success", "unknown_success", "missing_verification",
                    "boolean_schema"}:
            status, reason = "succeeded", None
            payload = {"verification": {
                "schema_version": True if case == "boolean_schema" else 1,
                "status": "UNKNOWN" if case == "unknown_success" else "passed",
                "source_sha256": "a" * 64,
                "checks": ({"phase": "fixture", "status": "passed"},),
            }}
        if case in {"missing_verification", "failed_without_diagnostic"}:
            payload = {}
        if case == "boolean_diagnostic_schema":
            diagnostic["schema_version"] = True
        if case == "boolean_exit_code":
            diagnostic["exit_code"] = True
        if case == "invalid_timeout":
            diagnostic["timed_out"] = "true"
        if case == "successful_diagnostic":
            diagnostic["exit_code"] = 0
        return HarnessToolCallResult(
            call_id="foreign" if case == "foreign_call" else request.call_id,
            tool_name="workspace.bundle" if case == "foreign_tool" else request.tool_name,
            status=status, payload=payload, failure_reason=reason,
            truncated=case.startswith("truncated"),
        )

    monkeypatch.setattr(shared, "invoke", respond)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    assert getattr(caught.value, "failure_code", None) == code
    assert not hasattr(caught.value, "result")
    assert len(model.requests) == len(shared.requests) == 1
    assert "workspace.bundle" not in backend.calls
    assert not backend.process_results and runtime._active_capability_task is None


@pytest.mark.parametrize(("error", "code"), [
    (RuntimeCapabilityError("project execution backend is unavailable"), "backend_unavailable"),
    (RuntimeCapabilityError("project execution report is invalid"), "malformed_report"),
    (RuntimeError("project execution backend is unavailable"), "failed"),
    (RuntimeError("password=PRIVATE_SECRET"), "failed"),
    (TimeoutError("password=PRIVATE_SECRET"), "timeout"),
])
async def test_direct_gateway_exceptions_use_types_and_never_expose_raw_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str,
) -> None:
    ctx, model = context(), Model(batch({"main.py": "print('ready')\n"}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(shared, "invoke", fail)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    assert getattr(caught.value, "failure_code", None) == code
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "PRIVATE_SECRET" not in "".join(traceback.format_exception(caught.value))
    assert len(model.requests) == 1 and "workspace.bundle" not in backend.calls
    assert runtime._active_capability_task is None
