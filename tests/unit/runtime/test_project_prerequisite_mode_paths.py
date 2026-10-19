"""Exercise prerequisite observations through real mode runtimes and checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.project_prerequisites import validate_project_prerequisite_report
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.domain.runs import TaskMode
from agent_hub.harness import HarnessToolGateway
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.capabilities.test_project_prerequisites import SOURCE, raw_report
from tests.unit.runtime.crew.test_adapter_failure_reason import FastFactory
from tests.unit.runtime.crew.test_execution_diagnostic_continuation import Policy, context

TOOLS = ("project.verify", "workspace.read")
ENGINEERING_TOOLS = (*TOOLS, "project.inspect", "workspace.write_text", "workspace.bundle")


class BlockedBackend:
    def __init__(self, tools: tuple[str, ...] = TOOLS) -> None:
        self.calls: list[str] = []
        self.tools = tools
        self.observation = validate_project_prerequisite_report(raw_report(), SOURCE)

    def is_available(self, tenant_id: Any, name: str) -> bool:
        return name in self.tools

    def is_replay_safe(self, name: str) -> bool:
        return name == "workspace.read"

    async def execute(self, *, name: str, arguments: Mapping[str, Any], **kwargs: Any) -> Any:
        self.calls.append(name)
        if name == "project.verify":
            assert arguments == {}
            raise RuntimeCapabilityError(
                "project verification blocked by execution prerequisites",
                project_prerequisite=self.observation,
            )
        assert name == "workspace.read" and arguments == {"path": "package.json"}
        return {"path": "package.json", "text": '{"name":"fixture"}', "truncated": False}


class ObservationModel:
    def __init__(self, mode: TaskMode, *, pause: bool = False, stage: int = 0) -> None:
        self.mode, self.pause, self.stage = mode, pause, stage
        self.requests: list[ModelRequest] = []
        self.feedback: list[Mapping[str, Any]] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.pause and self.stage:
            await asyncio.Event().wait()
        self.requests.append(request)
        if request.logical_model == "selector":
            response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
        else:
            if self.stage:
                prefix = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
                observed = []
                for message in request.messages:
                    if message.role != "user" or not isinstance(message.content, str):
                        continue
                    if not message.content.startswith(prefix):
                        continue
                    for row in json.loads(message.content[len(prefix):]):
                        result = row.get("result")
                        if self.mode is TaskMode.DISCUSS:
                            try:
                                result = json.loads(row["content"].removeprefix("Error: "))
                            except json.JSONDecodeError:
                                continue
                        if isinstance(result, dict) and "verification_blocked" in result:
                            if self.mode is TaskMode.DISCUSS:
                                assert row["is_error"] is True
                            observed.append(result)
                assert observed, "the next model request must retain the failed observation"
                self.feedback.extend(observed)
                assert all(
                    "verification_blocked" not in message.content
                    for message in request.messages
                    if message.role == "system" and isinstance(message.content, str)
                )
            stage = self.stage
            self.stage += 1
            response = ModelResponse(
                text="[COMPLETE] Runtime prerequisites remain blocked; no installation or verification succeeded."
                if stage == 2 else None,
                tool_calls=() if stage == 2 else (ToolCall(
                    id=f"prerequisite-{stage}", name=TOOLS[stage],
                    arguments={} if stage == 0 else {"path": "package.json"},
                ),), usage=TokenUsage(1, 1, 2),
            )
        return GatewayCompletion(
            response=response, deployment_id="fixture", logical_model=request.logical_model,
            provider_id="fixture", provider_model="fixture/model", cost_usd=Decimal(0),
        )


def runtime(
    ctx: TaskContext, model: Any, backend: Any, policy: Policy,
    repository: InMemoryArtifactRepository,
) -> CrewDispatchRuntime | AutoGenDiscussionRuntime:
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    engineering = ctx.routing_decision.get("project_delivery") == "workspace"
    tools = ENGINEERING_TOOLS if engineering else TOOLS
    if ctx.mode is TaskMode.DISCUSS:
        return AutoGenDiscussionRuntime(
            model, DiscussionPlan(participants=(
                DiscussionParticipant(id="implementer", role="Implementer", goal="Observe",
                                      logical_model="general", allowed_tools=tools),
                DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review",
                                      logical_model="general"),
            ), selector_model="selector", max_turns=5),
            capability_gateway=backend, harness_tool_gateway=gateway, artifact_repository=repository,
        )
    return CrewDispatchRuntime(
        model, DispatchPlan(agents=(AgentSpec(
            id="implementer", role="Implementer", goal="Observe", logical_model="general",
            allowed_tools=tools,
        ),), steps=(DispatchStep(
            id="observe", agent="implementer", task="Observe prerequisites and read the manifest.",
            tools=tools, final_synthesizer=True, token_budget=10_000,
            engineering_delivery_contract_version=1 if engineering else 0,
        ),), allowed_tools=tools, total_token_budget=10_000),
        capability_gateway=backend, harness_tool_gateway=gateway,
        artifact_repository=repository, crew_factory=FastFactory(),
    )


def failed_rows(checkpoint: RuntimeCheckpoint) -> list[Any]:
    rows: Any = checkpoint.state.get("tools", checkpoint.state.get("tool_ledger", ()))
    return [row for row in (rows.values() if isinstance(rows, Mapping) else rows)
            if row.get("status") == "failed" and row.get("artifact_id") is not None]


async def assert_failed_artifact(
    checkpoint: RuntimeCheckpoint, repository: InMemoryArtifactRepository, ctx: TaskContext,
    backend: BlockedBackend,
) -> None:
    rows = failed_rows(checkpoint)
    assert len(rows) == 1
    row = rows[0]
    artifacts = await repository.get_many(ctx.tenant_id, ctx.run_id, (ArtifactReference(
        id=UUID(row["artifact_id"]), sha256=row.get("sha256", row.get("artifact_sha256")),
    ),))
    assert artifacts[0].content["tool_name"] == "project.verify"
    assert artifacts[0].content["result"] == {
        "status": "failed", "untrusted": True,
        "verification_blocked": backend.observation.to_payload(),
    }


@pytest.mark.parametrize("mode", [TaskMode.DISPATCH, TaskMode.DISCUSS])
async def test_real_mode_continues_with_blocked_observation_not_success(mode: TaskMode) -> None:
    ctx, backend, model = context(mode), BlockedBackend(), ObservationModel(mode)
    repository = InMemoryArtifactRepository()
    runner = runtime(ctx, model, backend, Policy(ctx), repository)
    events = await asyncio.wait_for(_collect(runner, ctx), timeout=10)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert backend.calls == ["project.verify", "workspace.read"]
    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failed) == 1 and failed[0].tool_name == "project.verify"
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_name == "project.verify"
                   for event in events)
    expected = {"status": "failed", "untrusted": True,
                "verification_blocked": backend.observation.to_payload()}
    serialized = json.loads(json.dumps(expected, default=dict))
    assert model.feedback and all(feedback == serialized for feedback in model.feedback)


async def _collect(
    runner: CrewDispatchRuntime | AutoGenDiscussionRuntime, ctx: TaskContext,
) -> list[RunEvent]:
    return [event async for event in runner.run(ctx)]


async def blocked_checkpoint(
    ctx: TaskContext, backend: BlockedBackend, repository: InMemoryArtifactRepository,
    policy: Policy, monkeypatch: pytest.MonkeyPatch,
) -> RuntimeCheckpoint:
    mode = ctx.mode
    model = ObservationModel(mode, pause=True)
    runner = runtime(ctx, model, backend, policy, repository)
    saved: RuntimeCheckpoint | None = None
    ready = asyncio.Event()
    original = _DiscussionDurability.fail_tool

    async def pause_after_checkpoint(durability: _DiscussionDurability, artifact: Artifact) -> None:
        nonlocal saved
        await original(durability, artifact)
        saved = await runner.save_checkpoint()
        ready.set()

    async def consume() -> None:
        nonlocal saved
        async for event in runner.run(ctx):
            if mode is TaskMode.DISPATCH and event.checkpoint is not None and failed_rows(event.checkpoint):
                saved = event.checkpoint
                ready.set()
                break

    if mode is TaskMode.DISCUSS:
        monkeypatch.setattr(_DiscussionDurability, "fail_tool", pause_after_checkpoint)
    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    finally:
        await runner.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        monkeypatch.setattr(_DiscussionDurability, "fail_tool", original)
    assert saved is not None
    assert backend.calls == ["project.verify"]
    await assert_failed_artifact(saved, repository, ctx, backend)
    return saved


@pytest.mark.parametrize("mode", [TaskMode.DISPATCH, TaskMode.DISCUSS])
async def test_real_mode_checkpoint_replays_blocked_observation_without_reexecuting_verify(
    mode: TaskMode, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, backend = context(mode), BlockedBackend()
    repository, policy = InMemoryArtifactRepository(), Policy(ctx)
    saved = await blocked_checkpoint(ctx, backend, repository, policy, monkeypatch)
    resumed_model = ObservationModel(mode, stage=1)
    resumed = runtime(ctx, resumed_model, backend, policy, repository)
    await resumed.restore_checkpoint(saved)
    events = await asyncio.wait_for(_collect(resumed, ctx.model_copy(update={"checkpoint": saved})), 10)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert backend.calls == ["project.verify", "workspace.read"], (
        events[-1].reason, [(request.logical_model, request.messages) for request in resumed_model.requests],
    )
    assert resumed_model.feedback
    expected = json.loads(json.dumps({"status": "failed", "untrusted": True,
                                     "verification_blocked": backend.observation.to_payload()}, default=dict))
    assert all(feedback == expected
               for feedback in resumed_model.feedback)


class PrematureCompletionModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.stage = 0

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == "selector":
            response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
        elif self.stage == 0:
            self.stage += 1
            response = ModelResponse(text=None, tool_calls=(ToolCall(
                id="blocked-verify", name="project.verify", arguments={},
            ),), usage=TokenUsage(1, 1, 2))
        else:
            response = ModelResponse(text="[COMPLETE] Installed and verified successfully.",
                                     usage=TokenUsage(1, 1, 2))
        return GatewayCompletion(
            response=response, deployment_id="fixture", logical_model=request.logical_model,
            provider_id="fixture", provider_model="fixture/model", cost_usd=Decimal(0),
        )


@pytest.mark.parametrize("mode", [TaskMode.DISPATCH, TaskMode.DISCUSS])
async def test_engineering_blocked_then_complete_claim_cannot_publish_delivery(mode: TaskMode) -> None:
    ctx = context(mode)
    ctx = ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
        "engineering_delivery_contract_version": 1,
    }})
    backend, model = BlockedBackend(ENGINEERING_TOOLS), PrematureCompletionModel()
    runner = runtime(ctx, model, backend, Policy(ctx), InMemoryArtifactRepository())
    events: list[RunEvent] = []

    async def consume() -> None:
        try:
            async for event in runner.run(ctx):
                events.append(event)
        except RuntimeExecutionError as error:
            assert "delivery" in str(error) or "bundle is missing" in str(error)

    await asyncio.wait_for(consume(), 10)
    assert backend.calls == ["project.verify"]
    assert any(event.kind is EventKind.TOOL_FAILED and event.tool_name == "project.verify"
               for event in events)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


    assert any("verification_blocked" in message.content
               for request in model.requests for message in request.messages
               if message.role == "user" and isinstance(message.content, str))
    assert len([request for request in model.requests if request.logical_model != "selector"]) >= 2


@pytest.mark.parametrize("mode", [TaskMode.DISPATCH, TaskMode.DISCUSS])
@pytest.mark.parametrize("defect", ["wrong_tool_name", "mixed_authority"])
async def test_resigned_checkpoint_rejects_wrong_tool_and_mixed_authority_observations(
    mode: TaskMode, defect: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, backend = context(mode), BlockedBackend()
    repository, policy = InMemoryArtifactRepository(), Policy(ctx)
    saved = await blocked_checkpoint(ctx, backend, repository, policy, monkeypatch)
    row = failed_rows(saved)[0]
    old_id = row["artifact_id"]
    old_sha = row.get("sha256", row.get("artifact_sha256"))
    original, = await repository.get_many(ctx.tenant_id, ctx.run_id, (
        ArtifactReference(id=UUID(old_id), sha256=old_sha),
    ))
    content = json.loads(json.dumps(dict(original.content), default=dict))
    if defect == "wrong_tool_name":
        content["tool_name"] = "project.inspect"
    else:
        content["result"]["execution_diagnostic"] = {}
    changed = Artifact(id=uuid4(), type=original.type, producer=original.producer,
                       source_ids=original.source_ids, content=content)
    await repository.put(ctx.tenant_id, ctx.run_id, changed)

    def rebind(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(changed.id) if key == old_id else key: rebind(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item) for item in value]
        return str(changed.id) if value == old_id else changed.content_sha256 if value == old_sha else value

    payload = rebind(saved.to_payload())
    payload.update(id=str(uuid4()), state_sha256="")
    resigned = RuntimeCheckpoint.from_payload(payload)
    assert resigned.state_sha256 == resigned.recompute_state_sha256()
    assert changed.content_sha256 == changed.recompute_content_sha256()
    new_row = failed_rows(resigned)[0]
    assert new_row["artifact_id"] == str(changed.id)
    assert new_row.get("sha256", new_row.get("artifact_sha256")) == changed.content_sha256
    unused = PrematureCompletionModel()
    unused.stage = 1
    resumed = runtime(ctx, unused, backend, policy, repository)
    events = []
    if mode is TaskMode.DISPATCH:
        with pytest.raises(RuntimeExecutionError, match="capability failure evidence is invalid"):
            await resumed.restore_checkpoint(resigned)
            events = await asyncio.wait_for(_collect(resumed, ctx.model_copy(update={"checkpoint": resigned})), 10)
    else:
        await resumed.restore_checkpoint(resigned)
        events = await asyncio.wait_for(_collect(resumed, ctx.model_copy(update={"checkpoint": resigned})), 10)
        assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert backend.calls == ["project.verify"]
    assert unused.requests == [], "invalid replay must fail before another provider request"
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


@pytest.mark.parametrize("defect", [
    "producer_binding", "arguments_binding", "unprivileged_actor", "wrong_ledger_tool",
    "malformed_report", "failed_tail", "terminal_shortcut",
    "missing_arguments_pair", "invalid_arguments_pair",
])
async def test_autogen_hydration_checks_every_failed_row_before_any_provider_request(
    defect: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, backend = context(TaskMode.DISCUSS), BlockedBackend()
    repository, policy = InMemoryArtifactRepository(), Policy(ctx)
    saved = await blocked_checkpoint(ctx, backend, repository, policy, monkeypatch)
    payload = saved.to_payload()
    state: Any = payload["state"]
    row = state["tool_ledger"][0]
    original, = await repository.get_many(ctx.tenant_id, ctx.run_id, (
        ArtifactReference(id=UUID(row["artifact_id"]), sha256=row["artifact_sha256"]),
    ))
    content = json.loads(json.dumps(dict(original.content), default=dict))
    producer = original.producer
    if defect == "producer_binding":
        producer = "reviewer"
    elif defect == "arguments_binding":
        content["arguments_sha256"] = "b" * 64
    elif defect == "unprivileged_actor":
        producer = row["actor"] = "reviewer"
    elif defect == "wrong_ledger_tool":
        content["tool_name"] = row["name"] = "workspace.read"
    elif defect == "malformed_report":
        content["result"]["verification_blocked"]["status"] = "passed"
    elif defect == "missing_arguments_pair":
        del content["arguments_sha256"]
        del row["arguments_hash"]
    elif defect == "invalid_arguments_pair":
        content["arguments_sha256"] = row["arguments_hash"] = "not-a-digest"
    else:
        content["result"]["execution_diagnostic"] = {}
    changed = Artifact(id=uuid4(), type=original.type, producer=producer,
                       source_ids=original.source_ids, content=content)
    await repository.put(ctx.tenant_id, ctx.run_id, changed)
    registry = state["artifact_registry"]
    if defect == "failed_tail":
        row = {**row, "position": 1}
        state["tool_ledger"].append(row)
    else:
        del registry[str(original.id)]
    row.update(artifact_id=str(changed.id), artifact_sha256=changed.content_sha256)
    row["idempotency_key"] = hashlib.sha256(
        f"{ctx.run_id}:{row['actor']}:{row['position']}:{row['name']}:{row.get('arguments_hash')}".encode(),
    ).hexdigest()
    registry[str(changed.id)] = changed.content_sha256
    if defect == "terminal_shortcut":
        state.update(terminal=True, reason="complete_marker")
    payload.update(id=str(uuid4()), state_sha256="")
    resigned = RuntimeCheckpoint.from_payload(payload)
    assert resigned.state_sha256 == resigned.recompute_state_sha256()
    hydrated = await repository.get_many(ctx.tenant_id, ctx.run_id, tuple(
        ArtifactReference(id=UUID(artifact_id), sha256=digest)
        for artifact_id, digest in registry.items()
    ))
    assert changed in hydrated
    assert all(artifact.content_sha256 == artifact.recompute_content_sha256() for artifact in hydrated)
    unused = PrematureCompletionModel()
    unused.stage = 1
    resumed = runtime(ctx, unused, backend, policy, repository)
    await resumed.restore_checkpoint(resigned)
    events = await asyncio.wait_for(_collect(resumed, ctx.model_copy(update={"checkpoint": resigned})), 10)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert unused.requests == [], "hydration must reject all failed rows before invoking any provider"
    assert backend.calls == ["project.verify"]
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
