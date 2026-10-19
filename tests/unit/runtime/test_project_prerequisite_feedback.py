from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_prerequisites import validate_project_prerequisite_report
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.autogen.adapter import _execution_failure_result as discuss_feedback
from agent_hub.runtime.crew.adapter import _execution_failure_result as dispatch_feedback
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.failure_reason import (
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_diagnostic,
    safe_runtime_failure_reason,
)
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_project_prerequisites import REASONS, SOURCE, raw_report
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
    public_reason = safe_runtime_failure_reason(caught.value)
    assert public_reason == "project prerequisite blocked: node_runtime_missing"
    assert runtime_failure_diagnostic_from_reason(public_reason)["error_code"] == diagnostic["error_code"]
    assert len(model.requests) == len(shared.requests) == 1
    assert backend.calls.count("project.verify") == 1 and "workspace.bundle" not in backend.calls
    assert not backend.process_results


@pytest.mark.parametrize(("category", "reason"), [
    (category, reason) for category, reasons in REASONS.items() for reason in reasons
])
def test_exact_prerequisite_reason_remains_diagnostic_not_install_authority(
    category: str, reason: str,
) -> None:
    public = runtime_failure_diagnostic_from_reason("project prerequisite blocked: " + reason)
    assert public["error_stage"] == "project_prerequisite"
    assert public["error_category"] == category
    assert public["error_code"] == "project.prerequisite." + reason
    assert public["retryable"] is False
    assert "approval_id" not in public and "install_allowed" not in public


@pytest.mark.parametrize("reason", [
    "project prerequisite blocked: not_an_issued_reason",
    "project prerequisite blocked: node_runtime_missing extra",
    "prefix project prerequisite blocked: node_runtime_missing",
    "project prerequisite blocked: NODE_RUNTIME_MISSING",
])
def test_near_match_prerequisite_text_is_not_typed_diagnostic(reason: str) -> None:
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    assert diagnostic["error_stage"] != "project_prerequisite"


@pytest.mark.parametrize("reason", [
    "node_runtime_missing", "systemd_authorization_required", "install_authorization_required",
])
@pytest.mark.parametrize("mode", ["direct", "dispatch", "discuss"])
def test_finite_hybrid_reason_survives_redaction_without_permission(
    reason: str, mode: str,
) -> None:
    text = f"hybrid {mode} failed: project prerequisite blocked: {reason}"
    assert safe_runtime_failure_reason(RuntimeError(text)) == text
    diagnostic = runtime_failure_diagnostic_from_reason(text)
    assert diagnostic["error_stage"] == "project_prerequisite"
    assert diagnostic["error_code"] == "project.prerequisite." + reason
    assert diagnostic["hybrid_child_mode"] == mode
    assert diagnostic["retryable"] is False
    assert "install_allowed" not in diagnostic and "approval_id" not in diagnostic


@pytest.mark.parametrize("suffix", [
    "authorization=private", "systemd_authorization_required secret=value",
    "install_authorization_required\npassword=value",
])
def test_no_sensitive_hybrid_free_text_bypass(suffix: str) -> None:
    text = "hybrid direct failed: project prerequisite blocked: " + suffix
    assert safe_runtime_failure_reason(RuntimeError(text)) == "runtime_failed"
    assert runtime_failure_diagnostic_from_reason(text)["error_stage"] != "project_prerequisite"


@pytest.mark.parametrize("reason", ["systemd_authorization_required", "install_authorization_required"])
@pytest.mark.parametrize("prefix", [
    "", "project.prerequisite.", "project prerequisite blocked: ",
    "hybrid direct failed: project prerequisite blocked: ",
])
def test_public_projection_preserves_only_finite_reason_values_not_secret_keys(
    reason: str, prefix: str,
) -> None:
    literal = prefix + reason
    projected = _public_event_payload({"payload": {
        "reason": literal, "nested": [{"error_code": literal}], "authorization": literal,
    }})
    assert projected == {"payload": {"reason": literal, "nested": [{"error_code": literal}]}}


@pytest.mark.parametrize("suffix", ["\n", " ", " secret=value", "\npassword=value"])
def test_public_projection_does_not_whitelist_reason_substrings(suffix: str) -> None:
    projected = _public_event_payload({"reason": "project.prerequisite.install_authorization_required" + suffix})
    assert projected == {"reason": "[redacted]"}
