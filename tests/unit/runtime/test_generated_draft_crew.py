"""Crew draft feedback is failed, causal, resumable data, never verification credit."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    RuntimeExecutionError,
    _cached_generated_draft_feedback,
    _RunState,
)
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_sequence_context,
    _workspace_delivery_sequence_runtime,
    _workspace_delivery_write_response,
)

REASON = "generated project draft requires correction"
EMPTY_SHA = hashlib.sha256(b"{}").hexdigest()
TOOLS = ("project.verify", "workspace.write_text", "workspace.bundle")


def context() -> TaskContext:
    ctx = _workspace_delivery_sequence_context()
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "generated_project_contract_version": 1,
    }})


def verify(call_id: str = "verify") -> ModelResponse:
    return ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
        tool_calls=(ToolCall(call_id, "project.verify", {}),))


class DraftHarness(WorkspaceDeliverySequenceHarness):
    """Gateway boundary double; the real shared validator handles all draft payloads."""

    def __init__(self, ctx: TaskContext, *, defect: str | None = None,
                 observations: tuple[str, ...] = ("a" * 64,)) -> None:
        super().__init__()
        self.context = ctx
        self.defect = defect
        self.observations = observations
        self.verifications = 0

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id and user_id == self.context.actor_id
        assert role is Role.OPERATOR and request.run_id == self.context.run_id
        if request.tool_name == "project.inspect":
            self.calls.append(request)
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload={"scope": {"tenant_id": str(tenant_id), "run_id": str(request.run_id)},
                    "manifests": (), "operations": {}, "issues": ()})
        if request.tool_name != "project.verify":
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request)
        position = self.verifications
        self.verifications += 1
        if position >= len(self.observations):
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="failed", payload={}, failure_reason="capability denied")
        rejection: dict[str, Any] = {
            "schema_version": 1, "kind": "generated_project_draft_rejection",
            "phase": "before_capture", "execution_started": False,
            "tenant_id": str(tenant_id), "user_id": str(user_id), "run_id": str(request.run_id),
            "project_id": self.context.routing_decision["project_id"],
            "session_id": self.context.routing_decision["workspace_session_id"],
            "request_sha256": hashlib.sha256(self.context.request.encode()).hexdigest(),
            "proposer_actor": request.actor, "observation_sha256": self.observations[position],
            "reason": "generated_acceptance_manifest_invalid", "issues": ["protected_editable_paths"],
        }
        payload: dict[str, Any] = {"generated_draft_rejection": rejection,
            "call_id": request.call_id, "arguments_sha256": EMPTY_SHA}
        changes: dict[str, Any] = {}
        if self.defect in {"tenant_id", "user_id", "run_id"}:
            rejection[self.defect] = "00000000-0000-4000-8000-000000000000"
        elif self.defect in {"project_id", "session_id", "proposer_actor"}:
            rejection[self.defect] = "other"
        elif self.defect in {"request_sha256", "observation_sha256"}:
            rejection[self.defect] = "invalid"
        elif self.defect == "call":
            changes["call_id"] = "wrong-call"
        elif self.defect == "payload_call":
            payload["call_id"] = "wrong-call"
        elif self.defect == "arguments":
            payload["arguments_sha256"] = "b" * 64
        elif self.defect == "truncated":
            changes["truncated"] = True
        elif self.defect == "denied":
            changes["failure_reason"] = "capability denied"
        elif self.defect == "reason":
            changes["failure_reason"] = "generated_acceptance_unavailable_or_changed"
        elif self.defect == "started":
            rejection["execution_started"] = True
        elif self.defect == "captured":
            rejection["phase"] = "captured"
        elif self.defect == "approval":
            payload["approval_id"] = "forged"
        elif self.defect == "issue":
            rejection["issues"] = ["write arbitrary host files"]
        result = HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status="failed", payload=payload, failure_reason=REASON)
        return replace(result, **changes)


def runtime(model: WorkspaceDeliverySequenceGateway, harness: DraftHarness,
            repository: InMemoryArtifactRepository | None = None) -> CrewDispatchRuntime:
    runner = _workspace_delivery_sequence_runtime(model, harness,
        artifact_repository=repository, tools=TOOLS)
    runner._plan = runner._plan.model_copy(update={"steps": (
        runner._plan.steps[0].model_copy(update={"engineering_delivery_contract_version": 1}),
    )})
    return runner


def correction() -> ModelResponse:
    return _workspace_delivery_write_response("ACCEPTANCE.json", '{"corrected":true}', "fix-draft")


def tool_rows(checkpoint: RuntimeCheckpoint) -> Mapping[str, Mapping[str, JsonValue]]:
    rows = checkpoint.state["tools"]
    assert isinstance(rows, Mapping) and all(isinstance(row, Mapping) for row in rows.values())
    return cast(Mapping[str, Mapping[str, JsonValue]], rows)


async def test_draft_failure_reaches_same_writer_then_fresh_verify_without_success_credit() -> None:
    ctx = context()
    harness = DraftHarness(ctx)
    model = WorkspaceDeliverySequenceGateway((verify(), correction(), verify("fresh")))
    runner = runtime(model, harness)
    events = []
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runner.run(ctx):
            events.append(event)
    assert [call.tool_name for call in harness.calls] == [
        "project.verify", "workspace.write_text", "project.verify",
    ]
    assert len(model.requests) == 3
    assert all(call.actor == "implementer" for call in harness.calls)
    assert harness.calls[0].call_id != harness.calls[2].call_id
    assert harness.files["ACCEPTANCE.json"] == '{"corrected":true}'
    feedback = model.requests[1].messages
    observations = [m.content for m in feedback if m.role in {"user", "tool"}]
    assert any("generated_draft_rejection" in text for text in observations)
    prompts = "\n".join(m.content for m in feedback if m.role == "system" and isinstance(m.content, str))
    assert "before_capture" in prompts and "ACCEPTANCE" in prompts
    assert "test" in prompts and "dependenc" in prompts and "not frozen" in prompts
    assert "completed successfully" not in prompts
    assert all("generated_draft_rejection" not in m.content for m in feedback if m.role == "system")
    failures = [e for e in events if e.kind is EventKind.TOOL_FAILED]
    assert failures[0].payload.get("artifact_id")
    assert not any(e.kind is EventKind.TOOL_COMPLETED and e.tool_call_id == failures[0].tool_call_id
                   for e in events)
    assert not any(e.kind in {EventKind.STEP_COMPLETED, EventKind.RUNTIME_COMPLETED} for e in events)
    saved = await runner.save_checkpoint()
    failed = next(row for row in tool_rows(saved).values() if row.get("artifact_id") and row["status"] == "failed")
    artifact = next(a for a in runner._current_artifact_registry.values() if str(a.id) == failed["artifact_id"])
    assert artifact.producer == "implementer" and artifact.type == "tool_result"
    assert artifact.content["tool_name"] == "project.verify"
    assert artifact.content["arguments_sha256"] == EMPTY_SHA
    assert artifact.source_ids == (failed["trigger_model_artifact_id"],)
    assert artifact.content["call_id"] == harness.calls[0].call_id
    result = artifact.content["result"]
    assert isinstance(result, Mapping)
    assert set(result) == {"status", "untrusted", "generated_draft_rejection"}


@pytest.mark.parametrize("defect", ["tenant_id", "user_id", "run_id", "project_id", "session_id",
    "proposer_actor", "request_sha256", "observation_sha256", "call", "payload_call", "arguments",
    "truncated", "denied", "reason", "started", "captured", "approval", "issue"])
async def test_untrusted_draft_never_opens_correction(defect: str) -> None:
    ctx = context()
    harness = DraftHarness(ctx, defect=defect)
    model = WorkspaceDeliverySequenceGateway((verify(), correction()))
    with pytest.raises(RuntimeExecutionError):
        async for _ in runtime(model, harness).run(ctx):
            pass
    assert len(model.requests) == 1 and len(harness.calls) == 1
    assert not harness.files


@pytest.mark.parametrize("route_change", [{"generated_project_contract_version": 0},
    {"generated_project_contract_version": True}, {"project_input_contract_version": 1},
    {"generated_project_preparation": {"phase": "captured"}}])
async def test_non_draft_workflow_cannot_accept_feedback(route_change: dict[str, Any]) -> None:
    ctx = context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision, **route_change}})
    harness = DraftHarness(ctx)
    model = WorkspaceDeliverySequenceGateway((verify(), correction()))
    with pytest.raises(RuntimeExecutionError):
        async for _ in runtime(model, harness).run(ctx):
            pass
    assert len(model.requests) == 1 and not harness.files


@pytest.mark.parametrize("write_between", [False, True])
async def test_repeated_observation_stops_without_replaying_verify(write_between: bool) -> None:
    ctx = context()
    harness = DraftHarness(ctx, observations=("a" * 64, "a" * 64))
    responses = (verify(), *((correction(),) if write_between else ()), verify("repeat"), correction())
    model = WorkspaceDeliverySequenceGateway(responses)
    runner = runtime(model, harness)
    with pytest.raises(RuntimeExecutionError, match="draft.*unchanged|draft.*progress"):
        async for _ in runner.run(ctx):
            pass
    assert len(model.requests) == (3 if write_between else 2)
    assert harness.verifications == 2
    saved = await runner.save_checkpoint()
    assert sum(row["status"] == "failed" and row["artifact_id"] is not None
               for row in tool_rows(saved).values()) == 2


async def pause_at_failure(monkeypatch: pytest.MonkeyPatch, ctx: TaskContext | None = None) -> tuple[
    RuntimeCheckpoint, DraftHarness, InMemoryArtifactRepository, TaskContext,
]:
    ctx = ctx if ctx is not None else context()
    harness, repository = DraftHarness(ctx), InMemoryArtifactRepository()
    runner = runtime(WorkspaceDeliverySequenceGateway((verify(),)), harness, repository)
    saved: list[RuntimeCheckpoint] = []
    publish = runner._publish_checkpoint

    def stop(state: _RunState, checkpoint: RuntimeCheckpoint) -> None:
        publish(state, checkpoint)
        if any(row["status"] == "failed" and row.get("artifact_id")
               for row in tool_rows(checkpoint).values()):
            saved.append(checkpoint)
            raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_publish_checkpoint", stop)
    with pytest.raises(asyncio.CancelledError):
        async for _ in runner.run(ctx):
            pass
    assert saved
    return saved[0], harness, repository, ctx


async def test_failed_checkpoint_restores_feedback_without_reinvoking_rejected_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved, harness, repository, ctx = await pause_at_failure(monkeypatch)
    model = WorkspaceDeliverySequenceGateway((correction(), verify("fresh")))
    resumed = runtime(model, harness, repository)
    await resumed.restore_checkpoint(saved)
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in resumed.run(ctx.model_copy(update={"checkpoint": saved})):
            pass
    assert len(model.requests) == 2 and harness.verifications == 2
    assert "before_capture" in "\n".join(m.content for m in model.requests[0].messages if isinstance(m.content, str))
    assert [call.tool_name for call in harness.calls] == ["project.verify", "workspace.write_text", "project.verify"]


@pytest.mark.parametrize("status", ["uncertain", "running", "UNKNOWN"])
async def test_unknown_checkpoint_never_replays_draft_verify(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    saved, harness, repository, ctx = await pause_at_failure(monkeypatch)
    payload = saved.to_payload()
    state = cast(dict[str, Any], payload["state"])
    rows = state["tools"]
    for row in rows.values():
        row.update(status=status, replay_safe=False, artifact_id=None, sha256=None)
    changed = RuntimeCheckpoint(id=saved.id, runtime_type=saved.runtime_type,
        runtime_version=saved.runtime_version, run_id=saved.run_id, tenant_id=saved.tenant_id,
        mode=saved.mode, state=state)
    model = WorkspaceDeliverySequenceGateway(())
    resumed = runtime(model, harness, repository)
    with pytest.raises(RuntimeExecutionError):
        await resumed.restore_checkpoint(changed)
        async for _ in resumed.run(ctx.model_copy(update={"checkpoint": changed})):
            pass
    assert not model.requests and harness.verifications == 1


async def test_native_draft_failure_skips_batched_delivery_and_keeps_tool_result_error() -> None:
    class NativeModel(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            response = completion.response
            if response.tool_calls:
                response = replace(response, native_assistant=NativeAssistantMessage("primary", None,
                    tuple(NativeToolCall(c.id, c.name.replace(".", "_"), json.dumps(dict(c.arguments)))
                          for c in response.tool_calls)))
            return replace(completion, response=response)

    ctx = context()
    harness = DraftHarness(ctx)
    batched = replace(verify(), tool_calls=(*verify().tool_calls, ToolCall("not-run", "workspace.bundle", {})))
    model = NativeModel((batched, correction(), verify("fresh")))
    runner = runtime(model, harness)
    runner._provider_continuation_repository = InMemoryProviderContinuationRepository()
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in runner.run(ctx):
            pass
    assert "workspace.bundle" not in [call.tool_name for call in harness.calls]
    messages = model.requests[1].messages
    assert any(m.role == "tool" and "generated_draft_rejection" in m.content for m in messages)
    skipped = next(m for m in messages if m.tool_call_id == "not-run")
    assert isinstance(skipped.content, str)
    assert json.loads(skipped.content.split("=", 1)[1])[0]["result"] == {"status": "not_executed"}


async def test_mutated_artifact_with_stale_digest_cannot_restore_draft_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved, harness, repository, ctx = await pause_at_failure(monkeypatch)
    key, state = next(iter(tool_rows(saved).items()))
    original = repository._artifacts[(ctx.tenant_id, ctx.run_id, UUID(cast(str, state["artifact_id"])))]
    artifact = Artifact(id=original.id, type=original.type, producer=original.producer,
        content=original.content, source_ids=original.source_ids)
    result = cast(Mapping[str, JsonValue], artifact.content["result"])
    rejection = cast(Mapping[str, JsonValue], result["generated_draft_rejection"])
    content = {**artifact.content, "result": {**result, "generated_draft_rejection": {
        **rejection, "observation_sha256": "b" * 64,
    }}}
    object.__setattr__(artifact, "content", content)
    assert artifact.recompute_content_sha256() != artifact.content_sha256
    step = runtime(WorkspaceDeliverySequenceGateway(()), harness)._plan.steps[0]
    assert _cached_generated_draft_feedback(ctx, step, key, state, artifact) is None


async def test_engineering_action_mode_preserves_draft_correction_without_unknown_credit() -> None:
    ctx = context()
    harness = DraftHarness(ctx)
    inspect = ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
        tool_calls=(ToolCall("inspect", "project.inspect", {}),))
    initial = _workspace_delivery_write_response("main.py", "print('draft')", "initial")
    model = WorkspaceDeliverySequenceGateway((inspect, initial, verify(), correction(), verify("fresh")))
    runner = _workspace_delivery_sequence_runtime(model, harness, tools=(*TOOLS, "project.inspect"))
    runner._plan = runner._plan.model_copy(update={"steps": (
        runner._plan.steps[0].model_copy(update={"engineering_delivery_contract_version": 1,
            "engineering_action_version": 1}),
    )})
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in runner.run(ctx):
            pass
    assert len(model.requests) == 5 and harness.verifications == 2
    assert "before_capture" in "\n".join(m.content for m in model.requests[3].messages if isinstance(m.content, str))


async def test_changed_observation_allows_new_draft_within_existing_round_budget() -> None:
    ctx = context()
    harness = DraftHarness(ctx, observations=("a" * 64, "b" * 64))
    dependency = _workspace_delivery_write_response("requirements.lock", "packaging==26.0", "fix-lock")
    model = WorkspaceDeliverySequenceGateway((verify(), correction(), verify("changed"), dependency, verify("fresh")))
    runner = runtime(model, harness)
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in runner.run(ctx):
            pass
    assert len(model.requests) == 5 and harness.verifications == 3
    assert harness.files["requirements.lock"] == "packaging==26.0"
    saved = await runner.save_checkpoint()
    usage = saved.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 10


async def test_corrected_draft_capture_approval_restore_consumes_history_without_reopening_draft(
    tmp_path: Path,
) -> None:
    from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected
    from tests.unit.capabilities.test_generated_project_service import USER, manifest, setup
    from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file

    service, repo = setup(tmp_path)
    write_file(service.root, f"{project_path()}/ACCEPTANCE.json", "invalid draft")
    initial = context().model_copy(update={"tenant_id": TENANT, "run_id": RUN, "actor_id": USER,
        "request": repo.row.request, "routing_decision": {
            **repo.row.routing_decision, "project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
        }})

    class CaptureHarness(DraftHarness):
        approved = False

        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                         user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
            assert tenant_id == TENANT and request.run_id == RUN and user_id == USER and role is Role.OPERATOR
            self.calls.append(request)
            if request.tool_name == "workspace.write_text":
                path, body = cast(str, request.arguments["path"]), cast(str, request.arguments["content"])
                write_file(service.root, f"{project_path()}/{path}", body)
                self.files[path] = body
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status="succeeded", payload={"path": path, "content_bytes": len(body.encode()),
                        "content_sha256": hashlib.sha256(body.encode()).hexdigest()})
            assert request.tool_name == "project.verify"
            self.verifications += 1
            try:
                generated = await service.prepare(TENANT, RUN, request.actor, user_id=USER)
            except GeneratedProjectDraftRejected as error:
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status="failed", failure_reason=REASON,
                    payload={"generated_draft_rejection": cast(JsonValue, error.rejection.to_payload()),
                        "call_id": request.call_id, "arguments_sha256": EMPTY_SHA})
            assert generated is not None and generated.state["phase"] == "captured"
            if not self.approved:
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status="failed", failure_reason="capability requires approval", payload={"approval_id": "recipe-review"})
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload={"approval_resumed": True})

    harness = CaptureHarness(initial)
    repository = InMemoryArtifactRepository()
    fix = _workspace_delivery_write_response("ACCEPTANCE.json", manifest().decode(), "fix-draft")
    model = WorkspaceDeliverySequenceGateway((verify("old-draft"), fix, verify("capture")))
    runner = runtime(model, harness, repository)
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in runner.run(initial):
            pass
    saved = await runner.save_checkpoint()
    assert sorted(cast(str, row["status"]) for row in tool_rows(saved).values()) == ["failed", "succeeded", "waiting_approval"]
    assert repo.row.routing_decision["generated_project_preparation"]["phase"] == "captured"
    assert repo.updates == 2 and harness.verifications == 2
    old_call_id = harness.calls[0].call_id
    capture_call_id = harness.calls[2].call_id
    frozen_manifest = (service.root / project_path() / "ACCEPTANCE.json").read_bytes()
    harness.approved = True
    resumed_ctx = initial.model_copy(update={"routing_decision": {
        **initial.routing_decision, "generated_project_preparation": repo.row.routing_decision["generated_project_preparation"],
    }, "checkpoint": saved})
    # The next provider request must see history-only guidance; stop after its real receipt.
    resumed_model = WorkspaceDeliverySequenceGateway((ModelResponse(text="Not delivered", usage=TokenUsage(1, 1, 2)),))
    resumed = runtime(resumed_model, harness, repository)
    await resumed.restore_checkpoint(saved)
    events = []
    async for event in resumed.run(resumed_ctx):
        events.append(event)
        if resumed_model.requests:
            break
    await resumed.cancel()
    assert harness.verifications == 3 and repo.updates == 2
    assert sum(call.call_id == old_call_id for call in harness.calls) == 1
    assert sum(call.call_id == capture_call_id for call in harness.calls) == 2
    assert (service.root / project_path() / "ACCEPTANCE.json").read_bytes() == frozen_manifest
    prompts = "\n".join(m.content for m in resumed_model.requests[0].messages
        if m.role == "system" and isinstance(m.content, str))
    assert "GENERATED_PROJECT_DRAFT_CORRECTION:" not in prompts
    assert "not frozen" not in prompts and "historical" in prompts and "frozen" in prompts
    assert not any(e.kind is EventKind.TOOL_COMPLETED and e.tool_call_id == old_call_id for e in events)
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)


@pytest.mark.parametrize("phase", ["captured", "python_ready"])
async def test_real_python_state_restores_historical_draft_without_live_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from tests.unit.capabilities.test_python_generated_service import configured, request

    service, _, repo, _, _, generated = await configured(
        importlib.import_module("agent_hub.capabilities.python_generated_service"), tmp_path)
    if phase == "python_ready":
        await service.authorization_request(request(), generated)
    captured = repo.row.routing_decision["generated_project_preparation"]
    assert captured["phase"] == phase
    ctx = context().model_copy(update={"tenant_id": repo.row.tenant_id, "run_id": repo.row.id,
        "actor_id": repo.row.actor_id, "request": repo.row.request, "routing_decision": {
            **{key: value for key, value in repo.row.routing_decision.items() if key != "generated_project_preparation"},
            "project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
        }})
    saved, harness, repository, ctx = await pause_at_failure(monkeypatch, ctx)
    old_call_id = harness.calls[0].call_id
    harness.observations = ()  # New verify is denied, not another draft rejection or an execution receipt.
    model = WorkspaceDeliverySequenceGateway((verify("current"),))
    resumed = runtime(model, harness, repository)
    await resumed.restore_checkpoint(saved)
    resumed_ctx = ctx.model_copy(update={"checkpoint": saved, "routing_decision": {
        **ctx.routing_decision, "generated_project_preparation": captured,
    }})
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for _ in resumed.run(resumed_ctx):
            pass
    assert harness.verifications == 2 and len(model.requests) == 1 and not harness.files
    assert sum(call.call_id == old_call_id for call in harness.calls) == 1
    prompts = "\n".join(m.content for m in model.requests[0].messages
        if m.role == "system" and isinstance(m.content, str))
    assert "GENERATED_PROJECT_DRAFT_HISTORY:" in prompts and "not frozen" not in prompts
    assert "GENERATED_PROJECT_DRAFT_CORRECTION:" not in prompts


@pytest.mark.parametrize("phase", ["capturing", "UNKNOWN"])
async def test_unconfirmed_generated_state_cannot_restore_draft_history(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    saved, harness, repository, ctx = await pause_at_failure(monkeypatch)
    model = WorkspaceDeliverySequenceGateway(())
    resumed = runtime(model, harness, repository)
    await resumed.restore_checkpoint(saved)
    with pytest.raises(RuntimeExecutionError, match="draft failure evidence is invalid"):
        async for _ in resumed.run(ctx.model_copy(update={"checkpoint": saved, "routing_decision": {
            **ctx.routing_decision, "generated_project_preparation": {"schema_version": 1, "phase": phase},
        }})):
            pass
    assert not model.requests and harness.verifications == 1


@pytest.mark.parametrize("mixture", ["payload", "reason"])
async def test_valid_verification_success_mixed_with_draft_candidate_is_terminal(
    tmp_path: Path, mixture: str,
) -> None:
    from agent_hub.runtime.generated_project_history import generated_success_source
    from tests.unit.runtime.test_generated_project_history import evidence

    ctx, verified, _ = await evidence(tmp_path)
    ctx = ctx.model_copy(update={"mode": context().mode})
    assert generated_success_source(ctx, verified) is not None

    class MixedSuccessHarness(DraftHarness):
        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                         user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
            draft = await super().invoke(tenant_id, request, user_id=user_id, role=role)
            assert request.tool_name == "project.verify"
            payload = dict(verified)
            if mixture == "payload":
                payload["generated_draft_rejection"] = draft.payload["generated_draft_rejection"]
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload=payload, failure_reason=REASON if mixture == "reason" else None)

    harness = MixedSuccessHarness(ctx)
    model = WorkspaceDeliverySequenceGateway((verify(), correction()))
    runner = runtime(model, harness)
    events = []
    with pytest.raises(RuntimeExecutionError, match="draft failure evidence is invalid"):
        async for event in runner.run(ctx):
            events.append(event)
    assert len(model.requests) == 1 and harness.verifications == 1 and not harness.files
    assert not any(event.kind in {EventKind.TOOL_COMPLETED, EventKind.STEP_COMPLETED,
        EventKind.RUNTIME_COMPLETED} for event in events)
    saved = await runner.save_checkpoint()
    assert all(row["status"] == "failed" and row["artifact_id"] is None for row in tool_rows(saved).values())
