from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.direct import RuntimeExecutionError
from tests.unit.runtime.test_direct_project_verification import FIXED, Model, batch, context, runner

REPORT = "已交付可运行的项目及 ZIP。\n\n验证：实际运行检查已通过。使用方法见 README；未验证部署。"


def report_context():
    ctx = context()
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "engineering_direct_report_version": 1,
    }})


async def test_direct_final_report_uses_post_verification_bundle_and_full_model_text(tmp_path: Path) -> None:
    ctx = report_context()
    model = Model(batch({"main.py": FIXED, "README.md": "Run main.py.\n"}), REPORT)
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2
    assert backend.calls[-2:] == ["project.verify", "workspace.bundle"]
    request = model.requests[-1]
    assert not request.tools and request.required_tool is None and request.response_schema is None
    assert request.timeout_seconds <= ctx.timeout_seconds
    assert request.max_output_tokens <= ctx.token_budget - model.usage
    inputs = "\n".join(str(message.content) for message in request.messages)
    assert "passed" in inputs
    assert ctx.request in inputs
    assert "Return only the next workspace_batch" not in inputs
    artifact = next(event.artifact for event in reversed(events) if event.artifact is not None
                    and event.artifact.producer == "main")
    assert artifact.content["text"] == REPORT
    supplied = json.loads(inputs.split("<UNTRUSTED_VERIFIED_DELIVERY_JSON>", 1)[1].split("</", 1)[0])
    assert supplied["filename"] == artifact.content["workspace_delivery"]["file"]["filename"]
    assert isinstance(artifact.content["delivery_report_binding"], Mapping)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize("reply", ["", '{"result": "done"}', '{"workspace_batch": {"files": {}}}'])
async def test_direct_final_report_cannot_fall_back_to_early_summary(tmp_path: Path, reply: str) -> None:
    ctx = report_context()
    model = Model(batch({"main.py": FIXED}), reply)
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert backend.calls[-1] == "workspace.bundle"
    assert len(model.requests) == 2
    assert list((tmp_path / "generated").rglob("*.zip"))


async def test_legacy_direct_keeps_existing_model_count(tmp_path: Path) -> None:
    ctx = context()
    model = Model(batch({"main.py": FIXED}))
    runtime, _, _, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 1


async def test_direct_final_report_failure_does_not_claim_completion(tmp_path: Path) -> None:
    class FailingReport(Model):
        async def complete_with_context(self, request):
            if self.requests:
                self.requests.append(request)
                raise OSError("private provider credential")
            return await super().complete_with_context(request)
    ctx = report_context()
    model = FailingReport(batch({"main.py": FIXED}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError) as error:
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2
    assert backend.calls[-1] == "workspace.bundle"
    assert "private provider" not in str(error.value)


async def test_direct_completed_report_recovery_never_repeats_model_or_build(tmp_path: Path) -> None:
    from agent_hub.runtime import direct_delivery_report
    from agent_hub.runtime.direct import DirectRuntime
    ctx = report_context()
    model = Model(batch({"main.py": FIXED}), REPORT)
    runtime, _, _, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    artifact = next(event.artifact for event in events if event.artifact is not None
                    and event.artifact.producer == "main")
    assert direct_delivery_report.valid(ctx, artifact)
    checkpoint = await runtime.save_checkpoint()
    unused = Model()
    restored = DirectRuntime(unused, logical_model="main")
    await restored.restore_checkpoint(checkpoint)
    resumed = [event async for event in restored.run(ctx.model_copy(update={
        "artifacts": (artifact,), "checkpoint": checkpoint,
    }))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not unused.requests
    assert not direct_delivery_report.valid(ctx.model_copy(update={"request": "A different task"}), artifact)
    weakened = DirectRuntime(Model(), logical_model="main")
    await weakened.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in weakened.run(ctx.model_copy(update={
            "artifacts": (artifact,), "checkpoint": checkpoint,
            "routing_decision": {**ctx.routing_decision, "engineering_direct_report_version": 0},
        }))]


async def test_direct_report_internal_binding_is_not_public_output(tmp_path: Path) -> None:
    from agent_hub.runs.repository import _public_artifact_payload
    ctx = report_context()
    runtime, _, _, _ = runner(tmp_path, ctx, Model(batch({"main.py": FIXED}), REPORT))
    events = [event async for event in runtime.run(ctx)]
    artifact = next(event.artifact for event in events if event.artifact is not None
                    and event.artifact.producer == "main")
    public = _public_artifact_payload(artifact.to_payload())
    assert "delivery_report_binding" not in json.dumps(public)
    assert public["content"]["text"] == REPORT


def test_direct_report_policy_is_part_of_action_recovery_identity() -> None:
    from agent_hub.runtime.action_direct import _binding
    ctx = report_context()
    changed = ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "engineering_direct_report_version": 0,
    }})
    assert _binding(ctx, "main") != _binding(changed, "main")


def test_large_project_final_explanation_does_not_resend_full_file_manifest() -> None:
    from agent_hub.runtime.direct_delivery_report import evidence
    files = [{"path": f"src/component_{index}.py", "sha256": "a" * 64, "size_bytes": 1000}
             for index in range(2000)]
    delivery = {"artifact_id": "bundle", "file": {"filename": "project.zip"},
                "verification": {"status": "passed", "source_sha256": "b" * 64},
                "workspace_files": files}
    view = evidence(report_context(), delivery)
    assert len(json.dumps(view)) < len(json.dumps(files)) // 10
    assert view["workspace_file_count"] == 2000
    assert len(view["delivery_sha256"]) == 64


async def test_final_report_usage_cannot_renew_the_consumed_project_budget(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_hub.models.types import ModelResponse, TokenUsage

    class ReportBudget(Model):
        async def complete_with_context(self, request):
            outcome = await super().complete_with_context(request)
            if len(self.requests) == 2:
                return replace(outcome, response=ModelResponse(text=REPORT,
                    usage=TokenUsage(49_950, 1, 49_951)))
            return outcome
    ctx = report_context()
    model = ReportBudget(batch({"main.py": FIXED}), REPORT)
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="budget exceeds runtime limit"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2 and backend.calls[-1] == "workspace.bundle"
