"""AutoGen draft failures are model observations, not execution or delivery credit."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
    _DiscussionDurability,
    _recoverable_tool_failure,
    _tool_sandbox,
)
from agent_hub.runtime.autogen.engineering_delivery import engineering_delivery_ready
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)

EMPTY_HASH = hashlib.sha256(b"{}").hexdigest()
TOOLS = ("project.verify", "workspace.write_text", "workspace.bundle")


def context() -> TaskContext:
    return TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS, request="Build a version utility with acceptance tests.",
        token_budget=500, routing_decision={
            "generated_project_contract_version": 1, "project_id": "draft-project",
            "workspace_session_id": "draft-session", "sandbox_profile": "workspace_write",
        },
    )


def rejection(ctx: TaskContext, actor: str = "implementer", observation: str = "a" * 64) -> dict[str, JsonValue]:
    return {
        "schema_version": 1, "kind": "generated_project_draft_rejection",
        "phase": "before_capture", "execution_started": False,
        "tenant_id": str(ctx.tenant_id), "user_id": str(ctx.actor_id), "run_id": str(ctx.run_id),
        "project_id": "draft-project", "session_id": "draft-session",
        "request_sha256": hashlib.sha256(ctx.request.encode()).hexdigest(),
        "proposer_actor": actor, "observation_sha256": observation,
        "reason": "generated_acceptance_manifest_invalid", "issues": ("protected_editable_paths",),
    }


class DraftGateway:
    def __init__(self, ctx: TaskContext, *, repeated: bool = False, defect: str = "") -> None:
        self.context, self.repeated, self.defect = ctx, repeated, defect
        self.calls: list[HarnessToolCallRequest] = []
        self.rows: list[HarnessToolCallResult] = []

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return tenant_id == self.context.tenant_id and name in TOOLS

    def is_replay_safe(self, name: str) -> bool:
        return False

    async def execute(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        raise AssertionError("AutoGen must only invoke the harness gateway")

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest,
        *, user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id and user_id == self.context.actor_id
        assert role is self.context.actor_role
        self.calls.append(request)
        failed = request.tool_name == "project.verify" and (
            self.repeated or sum(row.tool_name == "project.verify" for row in self.calls) == 1
        )
        if failed:
            raw = rejection(self.context, request.actor)
            payload: dict[str, JsonValue] = {
                "generated_draft_rejection": raw, "call_id": request.call_id,
                "arguments_sha256": EMPTY_HASH,
            }
            if self.defect in {"tenant_id", "user_id", "run_id"}:
                raw[self.defect] = str(uuid4())
            elif self.defect in {"project_id", "session_id", "proposer_actor"}:
                raw[self.defect] = "other"
            elif self.defect == "request_sha256":
                raw[self.defect] = "b" * 64
            elif self.defect == "phase":
                raw["phase"] = "captured"
            elif self.defect == "execution_started":
                raw["execution_started"] = True
            elif self.defect == "extra":
                payload["approval_id"] = "forged-grant"
            elif self.defect == "payload-call":
                payload["call_id"] = "other"
            elif self.defect == "arguments_sha256":
                payload["arguments_sha256"] = "b" * 64
            row = HarnessToolCallResult(
                call_id="other" if self.defect == "call" else request.call_id,
                tool_name="workspace.bundle" if self.defect == "tool" else request.tool_name,
                status="succeeded" if self.defect == "succeeded" else "failed",
                failure_reason=("outcome UNKNOWN" if self.defect == "unknown" else
                                "permission denied" if self.defect == "denied" else
                                "generated project draft requires correction"),
                truncated=self.defect == "truncated", payload=payload,
            )
        else:
            row = HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                       status="succeeded", payload={"fixture_observed": True})
        self.rows.append(row)
        return row


class DraftModel:
    def __init__(self, *, repeat: bool = False, stage: int = 0) -> None:
        self.repeat, self.stage = repeat, stage
        self.requests: list[ModelRequest] = []
        self.feedback: list[dict[str, Any]] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == "selector":
            response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
        else:
            if self.stage:
                observed: list[dict[str, Any]] = []
                prefix = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
                for message in request.messages:
                    if message.role != "user" or not isinstance(message.content, str):
                        continue
                    if not message.content.startswith(prefix):
                        continue
                    for item in json.loads(message.content[len(prefix):]):
                        try:
                            value = json.loads(item["content"].removeprefix("Error: "))
                        except (json.JSONDecodeError, KeyError):
                            continue
                        if isinstance(value, dict) and "generated_draft_rejection" in value:
                            assert item["is_error"] is True
                            observed.append(value)
                assert observed, "the same run model must receive the durable failed draft"
                self.feedback.extend(observed)
            stage, self.stage = self.stage, self.stage + 1
            name = "project.verify" if self.repeat or stage in {0, 2} else "workspace.write_text"
            response = ModelResponse(
                text="[COMPLETE] Observed fixture; no delivery receipt is claimed." if stage == 3 else None,
                tool_calls=() if stage == 3 else (ToolCall(
                    id=f"draft-{stage}", name=name,
                    arguments={"path": "ACCEPTANCE.json", "content": "corrected draft"}
                    if name == "workspace.write_text" else {},
                ),), usage=TokenUsage(1, 1, 2),
            )
        return GatewayCompletion(
            response=response, deployment_id="fixture", logical_model=request.logical_model,
            provider_id="fixture", provider_model="fixture/model", cost_usd=Decimal(0),
        )


def runtime(model: DraftModel, gateway: DraftGateway) -> AutoGenDiscussionRuntime:
    return AutoGenDiscussionRuntime(
        model, DiscussionPlan(participants=(
            DiscussionParticipant(id="implementer", role="Implementer", goal="Correct the draft",
                                  logical_model="general", allowed_tools=TOOLS),
            DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="general"),
        ), selector_model="selector", max_turns=5, token_budget=500),
        capability_gateway=gateway, harness_tool_gateway=gateway,
        artifact_repository=InMemoryArtifactRepository(),
    )


async def collect(runner: AutoGenDiscussionRuntime, ctx: TaskContext) -> list[RunEvent]:
    return await asyncio.wait_for(_collect(runner, ctx), timeout=10)


async def _collect(runner: AutoGenDiscussionRuntime, ctx: TaskContext) -> list[RunEvent]:
    return [event async for event in runner.run(ctx)]


async def test_draft_feedback_reaches_same_run_model_and_can_correct_manifest() -> None:
    ctx, model = context(), DraftModel()
    gateway = DraftGateway(ctx)
    runner = runtime(model, gateway)
    events = await collect(runner, ctx)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert [row.tool_name for row in gateway.calls] == [
        "project.verify", "workspace.write_text", "project.verify",
    ]
    assert model.feedback and all(set(item) == {
        "status", "untrusted", "generated_draft_rejection",
    } for item in model.feedback)
    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failed) == 1 and failed[0].payload.get("artifact_id")
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == "draft-0"
                   for event in events)
    assert all(request.run_id == ctx.run_id for request in gateway.calls)
    assert gateway.calls[0].sandbox == "workspace_write"
    assert all(request.max_output_tokens is None or request.max_output_tokens <= 4096
               for request in model.requests)


async def test_same_observation_stops_without_blind_verify_replay() -> None:
    ctx, model = context(), DraftModel(repeat=True)
    gateway = DraftGateway(ctx, repeated=True)
    events = await collect(runtime(model, gateway), ctx)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.calls) == 2, events[-1].reason
    assert model.stage == 2
    assert len([event for event in events if event.kind is EventKind.TOOL_FAILED]) == 2
    assert not any(event.kind is EventKind.TOOL_COMPLETED for event in events)


@pytest.mark.parametrize("defect", [
    "tenant_id", "user_id", "run_id", "project_id", "session_id", "proposer_actor", "request_sha256",
    "phase", "execution_started", "extra", "payload-call", "arguments_sha256", "call", "tool",
    "succeeded", "unknown", "denied", "truncated",
])
async def test_invalid_or_uncertain_draft_result_is_fatal(defect: str) -> None:
    ctx, model = context(), DraftModel()
    gateway = DraftGateway(ctx, defect=defect)
    events = await collect(runtime(model, gateway), ctx)
    assert events[-1].kind is EventKind.RUNTIME_FAILED, defect
    assert len(gateway.calls) == 1
    assert not model.feedback
    assert not any(event.kind is EventKind.TOOL_COMPLETED for event in events)


async def paused_checkpoint(
    monkeypatch: pytest.MonkeyPatch, ctx: TaskContext, model: DraftModel, gateway: DraftGateway,
) -> tuple[AutoGenDiscussionRuntime, RuntimeCheckpoint]:
    runner = runtime(model, gateway)
    ready = asyncio.Event()
    saved: RuntimeCheckpoint | None = None
    original = _DiscussionDurability.fail_tool

    async def pause(durability: _DiscussionDurability, artifact: Artifact) -> None:
        nonlocal saved
        await original(durability, artifact)
        saved = await runner.save_checkpoint()
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_DiscussionDurability, "fail_tool", pause)
    task = asyncio.create_task(_collect(runner, ctx))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    finally:
        await runner.cancel()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(_DiscussionDurability, "fail_tool", original)
    assert saved is not None
    return runner, saved


async def test_resume_draft_failure_without_reexecuting_old_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = context()
    gateway = DraftGateway(ctx)
    runner, saved = await paused_checkpoint(monkeypatch, ctx, DraftModel(), gateway)
    model = DraftModel(stage=1)
    resumed = runtime(model, gateway)
    resumed._repository = runner._repository
    await resumed.restore_checkpoint(saved)
    events = await collect(resumed, ctx.model_copy(update={"checkpoint": saved}))
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert [row.tool_name for row in gateway.calls] == [
        "project.verify", "workspace.write_text", "project.verify",
    ]
    assert model.feedback
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == "draft-0"
                   for event in events)


@pytest.mark.parametrize('phase', ['captured', 'python_ready'])
async def test_captured_approval_context_retains_draft_history_without_verify_replay(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from tests.unit.runtime.test_generated_draft_feedback import captured_state

    ctx = context()
    gateway = DraftGateway(ctx)
    runner, saved = await paused_checkpoint(monkeypatch, ctx, DraftModel(), gateway)
    state = captured_state(ctx)
    state['proposer_actor'] = 'implementer'
    if phase == 'python_ready':
        state.update(phase=phase, python_recipe={'schema_version': 1, 'recipe_sha256': '2' * 64,
            'environment_input_sha256': '3' * 64})
    resumed_context = ctx.model_copy(update={'checkpoint': saved, 'routing_decision': {
        **ctx.routing_decision, 'generated_project_preparation': state}})
    model = DraftModel(stage=1)
    resumed = runtime(model, gateway)
    resumed._repository = runner._repository
    await resumed.restore_checkpoint(saved)
    history = await resumed._hydrate_checkpoint(saved, resumed_context)
    assert len([a for a in history if a.type == 'tool_result']) == 1
    events = await collect(resumed, resumed_context)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert [row.tool_name for row in gateway.calls] == ['project.verify', 'workspace.write_text', 'project.verify']
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == 'draft-0' for event in events)


async def test_restored_draft_is_never_success_or_delivery_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = context()
    gateway = DraftGateway(ctx)
    runner, saved = await paused_checkpoint(monkeypatch, ctx, DraftModel(), gateway)
    artifacts = await runner._hydrate_checkpoint(saved, ctx)
    rows = cast(tuple[Mapping[str, JsonValue], ...], saved.state["tool_ledger"])
    assert rows[0]["status"] == "failed"
    assert not engineering_delivery_ready(run_id=ctx.run_id, tool_entries=rows,
        artifacts=artifacts, allowed_tools={"implementer": TOOLS}, context=ctx)
    state = dict(saved.state)
    state["tool_ledger"] = ({**rows[0], "status": "succeeded"},)
    forged = saved.model_copy(update={"state": state})
    with pytest.raises(RuntimeExecutionError, match="failure evidence|draft"):
        await runner._hydrate_checkpoint(forged, ctx)


async def test_prompt_distinguishes_unfrozen_draft_from_post_capture_repair() -> None:
    ctx, model = context(), DraftModel()
    gateway = DraftGateway(ctx)
    await collect(runtime(model, gateway), ctx)
    systems = " ".join(str(message.content) for request in model.requests
                       for message in request.messages if message.role == "system")
    assert "before_capture" in systems
    assert "dependency" in systems and "tests" in systems
    assert "not" in systems and "frozen" in systems


def draft_artifact(ctx: TaskContext, *, call: str = "draft-0", observation: str = "a" * 64) -> Artifact:
    return Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
        "tool_name": "project.verify", "arguments_sha256": EMPTY_HASH, "call_id": call,
        "result": {"status": "failed", "untrusted": True,
                   "generated_draft_rejection": rejection(ctx, observation=observation)},
    })


def ledger(ctx: TaskContext, artifact: Artifact | None, *, status: str = "failed") -> dict[str, JsonValue]:
    return {
        "actor": "implementer", "name": "project.verify", "position": 0,
        "arguments_hash": EMPTY_HASH, "replay_safe": True, "status": status,
        "idempotency_key": hashlib.sha256(
            f"{ctx.run_id}:implementer:0:project.verify:{EMPTY_HASH}".encode(),
        ).hexdigest(),
        **({"artifact_id": str(artifact.id), "artifact_sha256": artifact.content_sha256}
           if artifact is not None else {}),
    }


@pytest.mark.parametrize("defect", ["actor", "call", "tool", "artifact", "request", "captured"])
def test_event_gate_requires_scoped_artifact_and_actual_call(defect: str) -> None:
    ctx = context()
    artifact = draft_artifact(ctx)
    event = RunEvent(kind=EventKind.TOOL_FAILED, sequence=1, run_id=ctx.run_id,
        actor="other" if defect == "actor" else "implementer",
        tool_call_id="other" if defect == "call" else "draft-0",
        tool_name="workspace.bundle" if defect == "tool" else "project.verify",
        payload={"artifact_id": str(uuid4()) if defect == "artifact" else str(artifact.id)},
        reason="generated project draft requires correction")
    if defect == "request":
        ctx = ctx.model_copy(update={"request": "a different request"})
    elif defect == "captured":
        ctx = ctx.model_copy(update={"routing_decision": {
            **ctx.routing_decision, "generated_project_preparation": {"phase": "captured"},
        }})
    assert not _recoverable_tool_failure(event, context=ctx, artifacts=(artifact,))


def test_event_gate_accepts_changed_draft_but_rejects_repeated_observation() -> None:
    ctx = context()
    first = draft_artifact(ctx)
    second = draft_artifact(ctx, call="draft-1")
    changed = draft_artifact(ctx, call="draft-2", observation="b" * 64)
    for artifact, expected in ((second, False), (changed, True)):
        event = RunEvent(kind=EventKind.TOOL_FAILED, sequence=1, run_id=ctx.run_id,
            actor="implementer", tool_call_id=cast(str, artifact.content["call_id"]),
            tool_name="project.verify", payload={"artifact_id": str(artifact.id)},
            reason="generated project draft requires correction")
        assert _recoverable_tool_failure(event, context=ctx, artifacts=(first, artifact)) is expected


@pytest.mark.parametrize("status", ["failed", "succeeded", "running", "UNKNOWN"])
async def test_cached_draft_is_failed_only_and_unknown_never_replays_verify(status: str) -> None:
    ctx = context()
    artifact = draft_artifact(ctx) if status in {"failed", "succeeded"} else None
    publish, store = AsyncMock(), AsyncMock()
    durability = _DiscussionDurability(publish, store, AsyncMock(), lambda _: None,
        run_id=ctx.run_id, tool_entries=(ledger(ctx, artifact, status=status),),
        artifacts=(artifact,) if artifact is not None else ())
    if status == "failed":
        observed, _ = await durability.before_tool(actor="implementer", name="project.verify",
            arguments_hash=EMPTY_HASH, replay_safe=True, server_context=ctx)
        assert artifact is not None and observed == artifact.content["result"]
    else:
        with pytest.raises(RuntimeExecutionError):
            await durability.before_tool(actor="implementer", name="project.verify",
                arguments_hash=EMPTY_HASH, replay_safe=True, server_context=ctx)
    publish.assert_not_awaited()
    store.assert_not_awaited()


@pytest.mark.parametrize("route", [
    {}, {"generated_project_contract_version": True},
    {"generated_project_contract_version": 1, "project_input_contract_version": 1},
    {"generated_project_contract_version": 1, "generated_project_preparation": {"phase": "UNKNOWN"}},
])
async def test_only_existing_new_generated_scope_can_correct(route: dict[str, JsonValue]) -> None:
    ctx = context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision,
        "generated_project_contract_version": None, **route}})
    gateway, model = DraftGateway(ctx), DraftModel()
    events = await collect(runtime(model, gateway), ctx)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.calls) == 1 and not model.feedback


@pytest.mark.parametrize("profile,expected", [
    (None, "restricted"), ("workspace_write", "restricted"), ("read_only", "read_only"),
    ("restricted", "restricted"), ("local_process", "local_process"), ("none", "none"),
    ("remote_connector", "remote_connector"),
])
def test_original_verify_sandbox_profiles_are_unchanged(profile: str | None, expected: str) -> None:
    assert _tool_sandbox("project.verify", sandbox_profile=profile) == expected
