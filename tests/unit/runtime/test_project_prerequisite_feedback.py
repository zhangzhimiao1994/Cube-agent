from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_prerequisites import validate_project_prerequisite_report
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.runtime.autogen.adapter import _execution_failure_result as discuss_feedback
from agent_hub.runtime.crew.adapter import _execution_failure_result as dispatch_feedback
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_project_prerequisites import SOURCE, raw_report
from tests.unit.runtime.test_direct_project_verification import Model, batch, context, runner


def blocked_payload() -> dict[str, Any]:
    report = validate_project_prerequisite_report(raw_report(), SOURCE).to_payload()
    return {"verification_blocked": report}


@pytest.mark.parametrize("feedback", [discuss_feedback, dispatch_feedback])
def test_native_blocked_observation_survives_mode_feedback_and_replay(
    feedback: Callable[..., Mapping[str, Any] | None],
) -> None:
    raw = blocked_payload()
    result = feedback(raw, tool_name="project.verify")
    assert result is not None
    assert result["status"] == "failed" and result["untrusted"] is True
    assert result["verification_blocked"] == raw["verification_blocked"]
    assert "execution_diagnostic" not in result and "verification_failure" not in result
    assert feedback(result, tool_name="project.verify") == result


@pytest.mark.parametrize("feedback", [discuss_feedback, dispatch_feedback])
@pytest.mark.parametrize("name", [None, "workspace.bundle", "docx", "project.inspect"])
def test_non_verify_cannot_publish_prerequisite(
    feedback: Callable[..., Mapping[str, Any] | None], name: object,
) -> None:
    assert feedback(blocked_payload(), tool_name=name) is None


@pytest.mark.parametrize("feedback", [discuss_feedback, dispatch_feedback])
@pytest.mark.parametrize("extra", [
    {"execution_diagnostic": {}}, {"verification_failure": {}}, {"approval_id": "injected"},
    {"unexpected": "value"}, {"status": "completed"}, {"untrusted": False},
])
def test_mixed_authority_or_failure_receipts_are_rejected(
    feedback: Callable[..., Mapping[str, Any] | None], extra: dict[str, Any],
) -> None:
    payload = {**blocked_payload(), **extra}
    assert feedback(payload, tool_name="project.verify") is None


@pytest.mark.parametrize("feedback", [discuss_feedback, dispatch_feedback])
def test_malformed_persisted_observation_is_not_model_feedback(
    feedback: Callable[..., Mapping[str, Any] | None],
) -> None:
    payload = deepcopy(raw_report())
    payload["category"] = "missing_runtime"
    payload["source_sha256"] = "model invented"
    assert feedback({"verification_blocked": payload}, tool_name="project.verify") is None


async def test_direct_preserves_native_blocked_reason_without_fake_code_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        raw = raw_report()
        raw["source_sha256"] = source
        raise RuntimeCapabilityError(
            "project verification blocked by execution prerequisites",
            project_prerequisite=validate_project_prerequisite_report(raw, source),
        )

    monkeypatch.setattr(backend, "execute", blocked)
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(ctx)]
    payload = getattr(caught.value, "project_prerequisite_payload", None)
    assert isinstance(payload, Mapping) and payload["reason"] == "node_runtime_missing"
    assert payload["status"] == "blocked"
    assert not hasattr(caught.value, "result")
    diagnostic = safe_runtime_failure_diagnostic(caught.value)
    assert diagnostic["error_code"] == "project.prerequisite.node_runtime_missing"
    assert diagnostic["error_stage"] == "project_prerequisite"
    assert diagnostic["retryable"] is False
    assert len(model.requests) == len(shared.requests) == 1
    assert backend.calls.count("project.verify") == 1 and "workspace.bundle" not in backend.calls
    assert not backend.process_results
