from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_prerequisites import validate_project_prerequisite_report
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import _event_with_failure_diagnostic, _public_event_payload
from agent_hub.runs.service import _runtime_failure_reason
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_project_prerequisites import raw_report
from tests.unit.runtime.test_direct_project_verification import Model, batch, context, runner
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, UnusedRuntime, final_zip_artifact


async def direct_blocked_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> tuple[TaskContext, RuntimeExecutionError]:
    ctx, model = context(), Model(batch({"main.py": "print('ready')\n"}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    execute = backend.execute

    async def blocked(*, name: str, **kwargs: Any) -> Any:
        if name != "project.verify":
            return await execute(name=name, **kwargs)
        backend.calls.append(name)
        source = await project_source_fingerprint(backend.workspace_root, ProjectInvocation(
            execution_id="fixture", tenant_id=ctx.tenant_id, run_id=ctx.run_id,
            project_id="direct-verification", session_id="same-task",
            sandbox_profile="workspace_write", timeout_seconds=60,
        ))
        raw = raw_report(reason=reason)
        raw["source_sha256"] = source
        raise RuntimeCapabilityError(
            "project verification blocked by execution prerequisites",
            project_prerequisite=validate_project_prerequisite_report(raw, source),
        )

    monkeypatch.setattr(backend, "execute", blocked)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    payload = getattr(caught.value, "project_prerequisite_payload", None)
    assert isinstance(payload, Mapping) and payload["reason"] == reason
    assert payload["status"] == "blocked"
    assert not hasattr(caught.value, "result")
    assert len(model.requests) == len(shared.requests) == 1
    assert backend.calls.count("project.verify") == 1
    assert "workspace.bundle" not in backend.calls
    assert not backend.process_results
    return ctx, caught.value


@pytest.mark.parametrize("reason", [
    "node_runtime_missing", "systemd_authorization_required", "install_authorization_required",
])
async def test_direct_blocked_reaches_public_run_failure_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    ctx, error = await direct_blocked_error(tmp_path, monkeypatch, reason)
    event = _event_with_failure_diagnostic(RunEvent(
        kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=ctx.run_id,
        reason=_runtime_failure_reason(error),
    ))

    assert event.payload["error_code"] == "project.prerequisite." + reason
    assert event.reason == "project prerequisite blocked: " + reason
    assert event.payload["error_stage"] == "project_prerequisite"
    assert event.payload["retryable"] is False
    assert "verification_receipt" not in event.payload
    assert_public_prerequisite(event, reason)


def assert_public_prerequisite(event: RunEvent, reason: str) -> None:
    public = _public_event_payload(event.to_payload())
    assert public["reason"] == event.reason
    assert isinstance(public["reason"], str)
    assert public["reason"].endswith("project prerequisite blocked: " + reason)
    diagnostic = public["payload"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["error_code"] == "project.prerequisite." + reason
    assert diagnostic["error_summary"] == event.reason
    assert diagnostic["error_stage"] == "project_prerequisite"
    assert diagnostic["retryable"] is False


class AttachmentThenBlockedRuntime(MultiArtifactRuntime):
    def __init__(self, error: RuntimeExecutionError) -> None:
        super().__init__(TaskMode.DISPATCH, (final_zip_artifact(),))
        self.error = error

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        yield RunEvent(
            kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=context.run_id,
            artifact=self.outputs[0],
        )
        raise self.error


@pytest.mark.parametrize("reason", [
    "node_runtime_missing", "systemd_authorization_required", "install_authorization_required",
])
async def test_hybrid_attachment_cannot_finalize_after_real_direct_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    ctx, error = await direct_blocked_error(tmp_path, monkeypatch, reason)
    child = AttachmentThenBlockedRuntime(error)
    runtime = HybridRuntime(
        child, UnusedRuntime(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
    )
    events = [event async for event in runtime.run(
        ctx.model_copy(update={"mode": TaskMode.HYBRID}),
    )]

    assert child.contexts
    assert any(event.artifact == child.outputs[0] for event in events)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(
        event.checkpoint is not None and event.checkpoint.state.get("terminal") is True
        for event in events
    )
    failure = _event_with_failure_diagnostic(events[-1])
    assert failure.kind is EventKind.RUNTIME_FAILED
    assert failure.payload["error_code"] == "project.prerequisite." + reason
    assert failure.payload["error_stage"] == "project_prerequisite"
    assert failure.payload["retryable"] is False
    assert_public_prerequisite(failure, reason)


@pytest.mark.parametrize("text", [
    "project prerequisite blocked: systemd_authorization_required extra",
    "project.prerequisite.install_authorization_required bearer private-value",
    "project prerequisite blocked: unknown_authorization_required",
    "authorization=Bearer private-value",
])
def test_public_prerequisite_exception_never_preserves_sensitive_free_text(text: str) -> None:
    public = _public_event_payload({
        "reason": text, "payload": {"error_code": text, "authorization": "private-value"},
    })
    assert public["reason"] == "[redacted]"
    assert public["payload"] == {"error_code": "[redacted]"}
