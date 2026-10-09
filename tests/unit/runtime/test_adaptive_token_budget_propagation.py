"""Offline RED probes for service-to-runtime adaptive token propagation."""

from __future__ import annotations

import asyncio
import json
import socket
import types
from collections.abc import AsyncIterator
from contextlib import nullcontext
from pathlib import Path
from typing import Protocol, cast

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.service import _adaptive_runtime_events
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    ExecutionRuntime,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter as adapter_module
from agent_hub.runtime.crew.adapter import (
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewObjectFactory,
    CrewStepGeneration,
    CrewTaskDefinition,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.plan import DispatchPlan
from agent_hub.runtime.defaults import (
    _dispatch_role_payload,
    _dispatch_step_payload,
    _PlannedRuntime,
)
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.streams import closing_runtime_events
from agent_hub.runtime.token_budget import (
    TokenBudgetSource,
    current_token_budget,
    token_budget_events,
    token_budget_scope,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import FastFactory
from tests.unit.runtime.crew.test_default_step_token_envelope import (
    UsageGateway,
    context,
    explicit_plan,
    generated_plan,
)
from tests.unit.runtime.test_direct_prompt import RecordingCapabilityGateway
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, UsageRecordingRuntime, artifact


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch, _function_scoped_runner: asyncio.Runner) -> None:
    _function_scoped_runner.get_loop()

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("owned propagation fixture cannot dispatch network traffic")

    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def adaptive_context(initial: int, mode: TaskMode) -> TaskContext:
    task = context(initial)
    decision = dict(task.routing_decision)
    decision.update(
        runtime_token_soft_base_tokens=1_000_000,
        runtime_token_absolute_tokens=10_000_000,
        critical_path_complexity_units=6,
    )
    return task.model_copy(update={"mode": mode, "routing_decision": decision})


async def consume(
    runtime: ExecutionRuntime, task: TaskContext, *, progress_granted: asyncio.Event | None = None,
) -> tuple[list[RunEvent], RuntimeExecutionError | None]:
    events: list[RunEvent] = []
    failure = None
    try:
        async for event in _adaptive_runtime_events(
            runtime,
            task,
            configured_tokens=1_000_000,
            routing_decision=task.routing_decision,
            initial_progress_units=0,
        ):
            events.append(event)
            if progress_granted is not None and task.token_budget > 1_500_003:
                progress_granted.set()
    except RuntimeExecutionError as error:
        failure = error
    return events, failure


class ProgressUsageGateway(UsageGateway):
    def __init__(self, task: TaskContext, progress_granted: asyncio.Event) -> None:
        super().__init__((2, 1_500_001, 2, 2))
        self.task = task
        self.progress_granted = progress_granted
        self.granted_at_calls: list[int] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.requests:
            await asyncio.wait_for(self.progress_granted.wait(), timeout=3)
        self.granted_at_calls.append(self.task.token_budget)
        return await super().complete_with_context(request)


@pytest.mark.parametrize("mode", (TaskMode.DISPATCH, TaskMode.HYBRID))
@pytest.mark.parametrize("initial", (1_500_000, 3_000_000))
async def test_service_progress_reaches_the_actual_crew_budget(
    mode: TaskMode, initial: int,
) -> None:
    task = adaptive_context(initial, mode)
    plan = generated_plan(task)
    progress_granted = asyncio.Event()
    gateway = ProgressUsageGateway(task, progress_granted)
    crew = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    child: ExecutionRuntime = crew
    if mode is TaskMode.HYBRID:
        child = HybridRuntime(
            crew,
            MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("discussion", "owned discussion"),)),
            MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "owned synthesis"),)),
        )
    runtime = _PlannedRuntime(
        child,
        mode=mode,
        main_agent_model="architect",
        roles=_dispatch_role_payload(plan),
        steps=_dispatch_step_payload(plan),
    )

    events, failure = await consume(runtime, task, progress_granted=progress_granted)
    checkpoint = await crew.save_checkpoint()
    print(
        f"OWN_PROPAGATION mode={mode.value} initial={initial} outer={task.token_budget} "
        f"before_second_receipt={gateway.granted_at_calls[1]} "
        f"calls={len(gateway.requests)} budget_terminal="
        f"{checkpoint.state['phase'] == 'budget_exhausted'} runtime_failed="
        f"{any(event.kind is EventKind.RUNTIME_FAILED for event in events)}"
    )
    assert task.token_budget > 1_500_003
    assert gateway.granted_at_calls[1] > 1_500_003
    assert failure is None, "service granted progress tokens but the Crew clone did not receive them"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    assert checkpoint.state["usage"] == {"tokens": 1_500_007, "cost_usd": "0"}


@pytest.mark.parametrize("initial", (1_500_000, 3_000_000))
async def test_hybrid_remaining_budget_tracks_service_progress_across_stages(initial: int) -> None:
    task = adaptive_context(initial, TaskMode.HYBRID)
    dispatch = UsageRecordingRuntime(
        TaskMode.DISPATCH, artifact("dispatch", "owned dispatch"), tokens_used=1_000_000,
    )
    discussion = UsageRecordingRuntime(
        TaskMode.DISCUSS, artifact("discussion", "owned discussion"), tokens_used=600_000,
    )
    synthesis = MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "owned synthesis"),))
    runtime = HybridRuntime(dispatch, discussion, synthesis)

    events, failure = await consume(runtime, task)
    print(
        f"OWN_HYBRID_SNAPSHOT initial={initial} outer={task.token_budget} "
        f"discussion_limit={discussion.contexts[0].token_budget} runtime_failed="
        f"{any(event.kind is EventKind.RUNTIME_FAILED for event in events)}"
    )
    assert task.token_budget > 1_600_000
    assert failure is None, "hybrid remaining_tokens stayed at the initial snapshot"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


class DirectGateway:
    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        index = len(self.requests)
        if index >= len(self.responses):
            raise AssertionError("unexpected additional direct fixture call")
        self.requests.append(request)
        return GatewayCompletion(
            response=self.responses[index],
            deployment_id="owned-deployment",
            logical_model=request.logical_model,
            provider_id="owned-provider",
            provider_model="owned-provider/owned-model",
        )


async def test_direct_single_response_is_not_a_false_positive_for_mid_run_progress() -> None:
    task = adaptive_context(1_500_000, TaskMode.DIRECT)
    gateway = DirectGateway((ModelResponse(text="owned answer", usage=TokenUsage(999_999, 1, 1_000_000)),))
    runtime = DirectRuntime(gateway, logical_model="owned-model")

    events, failure = await consume(runtime, task)

    assert failure is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 1


async def test_direct_workspace_batching_has_its_own_progress_extension() -> None:
    task = adaptive_context(1_500_000, TaskMode.DIRECT)
    decision = dict(task.routing_decision)
    decision.update(project_delivery="workspace", artifact_strategy="workspace_bundle")
    task = task.model_copy(update={"request": "Build an owned medium project.", "routing_decision": decision})
    responses = tuple(
        ModelResponse(
            text=json.dumps({
                "workspace_batch": {
                    "files": {path: "owned fixture\n"},
                    "complete": complete,
                    "continuation": "" if complete else "continue",
                },
            }),
            usage=TokenUsage(tokens - 1, 1, tokens),
        )
        for path, complete, tokens in (
            ("src/owned-first.txt", False, 1_000_000),
            ("src/owned-second.txt", True, 600_000),
        )
    )
    gateway = DirectGateway(responses)
    runtime = DirectRuntime(
        gateway, logical_model="owned-model", capability_gateway=RecordingCapabilityGateway(),
    )

    events, failure = await consume(runtime, task)

    assert failure is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2


async def test_legacy_generated_plan_checkpoint_denies_new_envelope_without_model_calls() -> None:
    task = adaptive_context(3_000_000, TaskMode.DISPATCH)
    current_plan = generated_plan(task)
    legacy_payload = current_plan.to_payload()
    for step in cast(list[dict[str, object]], legacy_payload["steps"]):
        step["token_budget"] = 1_000_000
    legacy_plan = DispatchPlan.from_payload(legacy_payload)
    assert legacy_plan.digest != current_plan.digest
    repository = InMemoryArtifactRepository()
    legacy = CrewDispatchRuntime(
        UsageGateway((2, 2, 2, 2)), legacy_plan,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    paused = None
    async with closing_runtime_events(legacy.run(task)) as stream:
        async for event in stream:
            if (
                event.kind is EventKind.CHECKPOINT_SAVED
                and event.checkpoint is not None
                and event.checkpoint.state["phase"] == "running"
                and event.checkpoint.state["usage"] == {"tokens": 2, "cost_usd": "0"}
            ):
                paused = event.checkpoint
                break
    assert paused is not None
    checkpoint = RuntimeCheckpoint.from_payload(paused.to_payload())
    original_hash = checkpoint.state_sha256
    assert checkpoint.state["terminal"] is False
    assert checkpoint.state["plan_digest"] == legacy_plan.digest
    assert checkpoint.runtime_version == "11"

    retained_gateway = UsageGateway(())
    retained = CrewDispatchRuntime(
        retained_gateway, legacy_plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await retained.restore_checkpoint(checkpoint)
    assert retained_gateway.requests == []

    changed_gateway = UsageGateway(())
    changed = CrewDispatchRuntime(
        changed_gateway, current_plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError, match="runtime checkpoint is incompatible"):
        await changed.restore_checkpoint(checkpoint)
    assert changed_gateway.requests == []
    assert checkpoint.state_sha256 == original_hash
    assert checkpoint.recompute_state_sha256() == original_hash
    assert checkpoint.state["usage"] == {"tokens": 2, "cost_usd": "0"}


class ChangedBudgetGateway(UsageGateway):
    def __init__(self, task: TaskContext, value: object, *, tokens: int) -> None:
        super().__init__((tokens,))
        self.task = task
        self.value = value

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        object.__setattr__(self.task, "token_budget", self.value)
        return await super().complete_with_context(request)


@pytest.mark.parametrize("value", (80, True, 0, -1, 10_000_001, "6000", 1.5))
async def test_crew_observes_decreases_and_denies_invalid_live_token_limits(value: object) -> None:
    task = adaptive_context(5_000, TaskMode.DISPATCH)
    gateway = ChangedBudgetGateway(task, value, tokens=100)
    runtime = CrewDispatchRuntime(
        gateway, explicit_plan(step_tokens=10_000), crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        async for _ in runtime.run(task):
            pass
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "budget_exhausted"
    assert checkpoint.state["usage"] == {"tokens": 100, "cost_usd": "0"}
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("adaptive,absolute,plan_limit,tokens", (
    (False, 10_000_000, 10_000_000, 6_000),
    (True, 6_000, 10_000_000, 7_000),
    (True, 10_000_000, 6_000, 7_000),
))
async def test_live_source_cannot_expand_fixed_or_persisted_caps(
    adaptive: bool, absolute: int, plan_limit: int, tokens: int,
) -> None:
    task = adaptive_context(5_000, TaskMode.DISPATCH) if adaptive else context(5_000)
    decision = dict(task.routing_decision)
    decision.update(runtime_token_absolute_tokens=absolute, runtime_plan_token_budget=plan_limit)
    task = task.model_copy(update={"routing_decision": decision})
    gateway = ChangedBudgetGateway(task, 8_000, tokens=tokens)
    runtime = CrewDispatchRuntime(
        gateway, explicit_plan(step_tokens=min(10_000, plan_limit), total_tokens=plan_limit),
        crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        async for _ in runtime.run(task):
            pass
    assert (await runtime.save_checkpoint()).state["usage"] == {
        "tokens": tokens, "cost_usd": "0",
    }
    assert len(gateway.requests) == 1


async def test_hybrid_decrease_reaches_next_stage_without_repeated_credit() -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    dispatch = UsageRecordingRuntime(
        TaskMode.DISPATCH, artifact("dispatch", "owned dispatch"), tokens_used=400,
    )
    discussion = UsageRecordingRuntime(
        TaskMode.DISCUSS, artifact("discussion", "owned discussion"), tokens_used=200,
    )
    synthesis = MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "owned synthesis"),))
    runtime = HybridRuntime(dispatch, discussion, synthesis)
    events = []
    async for event in runtime.run(task):
        events.append(event)
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact == dispatch.outputs[0]:
            object.__setattr__(task, "token_budget", 500)
    assert discussion.contexts[0].token_budget == 100
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert synthesis.contexts == []


async def test_crew_restored_artifact_clone_keeps_live_budget_without_rebilling() -> None:
    task = adaptive_context(1_500_000, TaskMode.DISPATCH)
    plan = generated_plan(task)
    repository = InMemoryArtifactRepository()
    first = CrewDispatchRuntime(
        UsageGateway((2, 2, 2, 2)), plan,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    paused = None
    async with closing_runtime_events(first.run(task)) as stream:
        async for event in stream:
            if event.checkpoint is not None and event.checkpoint.state.get("usage") == {
                "tokens": 2, "cost_usd": "0",
            }:
                paused = event.checkpoint
                break
    assert paused is not None
    resumed_task = task.model_copy(update={"checkpoint": paused})
    granted = asyncio.Event()

    class RestoredGateway(UsageGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            await asyncio.wait_for(granted.wait(), timeout=3)
            return await super().complete_with_context(request)

    gateway = RestoredGateway((1_500_001, 2, 2))
    resumed = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    await resumed.restore_checkpoint(paused)
    events, failure = await consume(resumed, resumed_task, progress_granted=granted)
    assert failure is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    assert (await resumed.save_checkpoint()).state["usage"] == {
        "tokens": 1_500_007, "cost_usd": "0",
    }


@pytest.mark.parametrize("resume_limit", (2_000, 3_000))
async def test_hybrid_same_grant_and_restore_never_add_credit_twice(resume_limit: int) -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    repository = InMemoryArtifactRepository()

    def stages() -> tuple[UsageRecordingRuntime, UsageRecordingRuntime, UsageRecordingRuntime]:
        return (
            UsageRecordingRuntime(TaskMode.DISPATCH, artifact("dispatch", "owned stage"), tokens_used=400),
            UsageRecordingRuntime(TaskMode.DISCUSS, artifact("discussion", "owned stage"), tokens_used=300),
            UsageRecordingRuntime(TaskMode.DIRECT, artifact("synthesis", "owned stage"), tokens_used=200),
        )

    dispatch, discussion, synthesis = stages()
    runtime = HybridRuntime(dispatch, discussion, synthesis, artifact_repository=repository)
    paused = None
    async for event in runtime.run(task):
        if event.kind is EventKind.ARTIFACT_CREATED:
            object.__setattr__(task, "token_budget", 2_000)
        if (
            event.checkpoint is not None
            and event.checkpoint.state["next_stage"] == 1
            and event.checkpoint.state.get("child_checkpoint") is None
        ):
            paused = event.checkpoint
    assert paused is not None
    assert paused.state["remaining_token_budget"] == 1_600
    assert (await runtime.save_checkpoint()).state["remaining_token_budget"] == 1_100

    next_dispatch, next_discussion, next_synthesis = stages()
    resumed = HybridRuntime(
        next_dispatch, next_discussion, next_synthesis, artifact_repository=repository,
    )
    await resumed.restore_checkpoint(paused)
    resumed_task = task.model_copy(update={"checkpoint": paused, "token_budget": resume_limit})
    events = [event async for event in resumed.run(resumed_task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert next_dispatch.contexts == []
    assert next_discussion.contexts[0].token_budget == 1_600
    assert next_synthesis.contexts[0].token_budget == 1_300
    assert (await resumed.save_checkpoint()).state["remaining_token_budget"] == 1_100
    assert paused.state["remaining_token_budget"] == 1_600


@pytest.mark.parametrize("resume_limit", (600, 1_000, 1_500))
async def test_hybrid_restore_preserves_spent_after_source_decrease(resume_limit: int) -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    repository = InMemoryArtifactRepository()
    dispatch = UsageRecordingRuntime(
        TaskMode.DISPATCH, artifact("dispatch", "owned stage"), tokens_used=400,
    )
    runtime = HybridRuntime(
        dispatch,
        UsageRecordingRuntime(TaskMode.DISCUSS, artifact("discussion", "owned stage"), tokens_used=0),
        UsageRecordingRuntime(TaskMode.DIRECT, artifact("synthesis", "owned stage"), tokens_used=0),
        artifact_repository=repository,
    )
    paused = None
    async for event in runtime.run(task):
        if (event.checkpoint is not None and event.checkpoint.state["next_stage"] == 1
                and event.checkpoint.state.get("child_checkpoint") is None):
            paused = event.checkpoint
    assert paused is not None
    assert paused.state["remaining_token_budget"] == 600
    next_dispatch = UsageRecordingRuntime(
        TaskMode.DISPATCH, artifact("dispatch", "owned stage"), tokens_used=400,
    )
    discussion = UsageRecordingRuntime(
        TaskMode.DISCUSS, artifact("discussion", "owned stage"), tokens_used=500,
    )
    synthesis = UsageRecordingRuntime(
        TaskMode.DIRECT, artifact("synthesis", "owned stage"), tokens_used=0,
    )
    resumed = HybridRuntime(next_dispatch, discussion, synthesis, artifact_repository=repository)
    await resumed.restore_checkpoint(paused)
    resumed_task = task.model_copy(update={"checkpoint": paused, "token_budget": resume_limit})
    events = [event async for event in resumed.run(resumed_task)]
    assert next_dispatch.contexts == []
    assert discussion.contexts[0].token_budget == min(1_000, resume_limit) - 400
    if resume_limit == 600:
        assert events[-1].kind is EventKind.RUNTIME_FAILED
        assert synthesis.contexts == []
    else:
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert (await resumed.save_checkpoint()).state["remaining_token_budget"] == 100


class OwnedAsyncLLM(Protocol):
    async def acall(self, messages: object, **kwargs: object) -> str: ...


class OwnedAsyncFactory(CrewObjectFactory):
    def __init__(self, task: TaskContext, value: int, storage_root: Path) -> None:
        self.kickoffs = 0
        self.acalls = 0
        self.storage_root = storage_root
        fixture = self

        class BaseLLM:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class Agent:
            llm: OwnedAsyncLLM

            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class Task:
            description: str

            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class Crew:
            agents: list[Agent]
            tasks: list[Task]

            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

            async def akickoff(self, inputs: dict[str, object]) -> object:
                assert inputs == {}
                fixture.kickoffs += 1
                object.__setattr__(task, "token_budget", value)
                fixture.acalls += 1
                answer = await self.agents[0].llm.acall(
                    [{"role": "user", "content": self.tasks[0].description}], tools=None,
                )
                return types.SimpleNamespace(raw=answer)

        self.module = types.SimpleNamespace(
            BaseLLM=BaseLLM, Agent=Agent, Task=Task, Crew=Crew,
            Process=types.SimpleNamespace(sequential="sequential"),
        )

    def build(
        self, agents: tuple[CrewAgentDefinition, ...], tasks: tuple[CrewTaskDefinition, ...],
        *, share_crew: bool, telemetry_disabled: bool,
    ) -> CrewStepGeneration:
        assert not share_crew and telemetry_disabled
        return adapter_module._CrewAIGeneration(self.module, agents, tasks, self.storage_root)


@pytest.mark.parametrize("live_limit", (0, 6_000))
async def test_native_async_bridge_checks_live_budget_before_new_gateway_submission(
    live_limit: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    task = adaptive_context(5_000, TaskMode.DISPATCH)
    factory = OwnedAsyncFactory(task, live_limit, tmp_path)
    gateway = UsageGateway((2,))
    monkeypatch.setattr(adapter_module, "_active_crewai_scope", lambda path: nullcontext())
    runtime = CrewDispatchRuntime(
        gateway, explicit_plan(step_tokens=10_000), crew_factory=factory,
    )
    events, failure = await consume(runtime, task)
    assert factory.kickoffs == factory.acalls == 1
    if live_limit == 0:
        assert gateway.requests == []
        assert failure is not None
        assert events[-1].kind is EventKind.RUNTIME_FAILED
        assert not any(event.kind is EventKind.COST_RECORDED for event in events)
        with pytest.raises(RuntimeExecutionError, match="no completed checkpoint boundary"):
            await runtime.save_checkpoint()
    else:
        assert failure is None
        assert len(gateway.requests) == 1
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_budget_binding_is_exact_identity_and_clears_on_cancel() -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    budget = TokenBudgetSource.from_context(task)
    child = task.validated_internal_clone()
    forged = child.model_copy()
    object.__setattr__(forged, "token_budget_source", budget)
    object.__setattr__(task, "token_budget", 2_000)
    with token_budget_scope(child, budget):
        assert current_token_budget(child) == 2_000
        assert current_token_budget(forged) == 1_000
    assert current_token_budget(child) == 1_000

    entered = asyncio.Event()

    async def blocked() -> AsyncIterator[RunEvent]:
        entered.set()
        await asyncio.Event().wait()
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=task.run_id)

    async with closing_runtime_events(token_budget_events(blocked, child, budget)) as stream:
        async def next_event() -> RunEvent:
            return await anext(stream)

        pending = asyncio.create_task(next_event())
        await asyncio.wait_for(entered.wait(), timeout=3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert current_token_budget(child) == child.token_budget
    assert current_token_budget(forged) == 1_000


def test_live_caps_are_frozen_even_if_source_routing_or_identity_is_tampered() -> None:
    task = adaptive_context(5_000, TaskMode.DISPATCH)
    decision = dict(task.routing_decision)
    decision["runtime_token_absolute_tokens"] = 6_000
    task = task.model_copy(update={"routing_decision": decision})
    budget = TokenBudgetSource.from_context(task)
    object.__setattr__(task, "routing_decision", {"runtime_token_absolute_tokens": 10_000_000})
    object.__setattr__(task, "token_budget", 8_000)
    assert budget.limit == 6_000
    object.__setattr__(task, "token_budget", 80)
    assert budget.limit == 80
    object.__setattr__(task, "actor_id", task.run_id)
    assert budget.limit == 0


@pytest.mark.parametrize("value", (0, True, -1, 10_000_001))
async def test_zero_live_budget_never_calls_child_factory(value: object) -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    child = task.validated_internal_clone()
    budget = TokenBudgetSource.from_context(task)
    factory_calls: list[bool] = []
    advances: list[bool] = []

    async def events() -> AsyncIterator[RunEvent]:
        advances.append(True)
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=task.run_id)

    def factory() -> AsyncIterator[RunEvent]:
        factory_calls.append(True)
        return events()

    object.__setattr__(task, "token_budget", value)
    async with closing_runtime_events(token_budget_events(factory, child, budget)) as stream:
        with pytest.raises(ValueError, match="token budget exhausted"):
            await anext(stream)
    assert factory_calls == []
    assert advances == []


async def test_zero_live_budget_never_advances_existing_child() -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    child = task.validated_internal_clone()
    budget = TokenBudgetSource.from_context(task)
    advances: list[int] = []

    async def events() -> AsyncIterator[RunEvent]:
        for sequence in (1, 2):
            advances.append(sequence)
            yield RunEvent(
                kind=EventKind.STEP_STARTED, sequence=sequence, run_id=task.run_id,
                step_id="owned", actor="owned",
            )

    async with closing_runtime_events(token_budget_events(events, child, budget)) as stream:
        await anext(stream)
        object.__setattr__(task, "token_budget", 0)
        with pytest.raises(ValueError, match="token budget exhausted"):
            await anext(stream)
    assert advances == [1]


def test_checkpoint_baseline_never_grants_credit_after_live_parent_is_zero() -> None:
    task = adaptive_context(1_000, TaskMode.HYBRID)
    root = TokenBudgetSource.from_context(task)
    child = root.remaining(spent=200, checkpoint_baseline=100)
    assert child.limit == 900
    object.__setattr__(task, "token_budget", 0)
    assert child.limit == 0


def test_unrecognized_scale_policy_does_not_expand_explicit_context_limit() -> None:
    task = adaptive_context(1_000, TaskMode.DISPATCH)
    decision = dict(task.routing_decision)
    decision["project_scale"] = "unknown"
    task = task.model_copy(update={"routing_decision": decision})
    budget = TokenBudgetSource.from_context(task)
    object.__setattr__(task, "token_budget", 2_000)
    assert budget.limit == 1_000


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("complexity", (None, 0, -1, True, "invalid", 1.5, 10_000_001))
async def test_service_normalized_complexity_keeps_verified_progress_credit(
    scale: str, complexity: JsonValue,
) -> None:
    task = adaptive_context(1_000_000, TaskMode.DISPATCH)
    decision = dict(task.routing_decision)
    decision["project_scale"] = scale
    if complexity is None:
        decision.pop("critical_path_complexity_units")
    else:
        decision["critical_path_complexity_units"] = complexity
    task = task.model_copy(update={"routing_decision": decision})
    clone = task.validated_internal_clone()
    budget = TokenBudgetSource.from_context(task, validated=clone)
    runtime = MultiArtifactRuntime(
        TaskMode.DISPATCH, (artifact("dispatch", "owned progress"),),
    )

    events, failure = await consume(runtime, task)

    assert failure is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert task.token_budget > 1_000_000
    with token_budget_scope(clone, budget):
        assert current_token_budget(clone) == task.token_budget


@pytest.mark.parametrize("missing", ("project_scale", "runtime_token_soft_base_tokens"))
async def test_service_progress_without_scale_or_soft_marker_stays_fixed_hard(
    missing: str,
) -> None:
    task = adaptive_context(1_000_000, TaskMode.DISPATCH)
    decision = dict(task.routing_decision)
    decision.pop(missing)
    task = task.model_copy(update={"routing_decision": decision})
    clone = task.validated_internal_clone()
    budget = TokenBudgetSource.from_context(task, validated=clone)
    runtime = MultiArtifactRuntime(
        TaskMode.DISPATCH, (artifact("dispatch", "owned progress"),),
    )

    _, failure = await consume(runtime, task)

    assert failure is None
    with token_budget_scope(clone, budget):
        assert current_token_budget(clone) == 1_000_000


@pytest.mark.parametrize("phase", ("factory", "advance"))
async def test_restored_baseline_after_decrease_never_drives_additional_child_calls(
    phase: str,
) -> None:
    task = adaptive_context(2_000_000, TaskMode.HYBRID)
    child = task.validated_internal_clone()
    root = TokenBudgetSource.from_context(task)
    budget = root.remaining(spent=1_000_000, checkpoint_baseline=200_000)
    factory_calls: list[bool] = []
    advances: list[int] = []

    async def events() -> AsyncIterator[RunEvent]:
        for sequence in (1, 2):
            advances.append(sequence)
            yield RunEvent(
                kind=EventKind.STEP_STARTED, sequence=sequence, run_id=task.run_id,
                step_id="owned", actor="owned",
            )

    def factory() -> AsyncIterator[RunEvent]:
        factory_calls.append(True)
        return events()

    async with closing_runtime_events(token_budget_events(factory, child, budget)) as stream:
        if phase == "advance":
            await anext(stream)
            assert child.token_budget == 1_200_000
        object.__setattr__(task, "token_budget", 80)
        assert budget.limit == 0
        with pytest.raises(ValueError, match="token budget exhausted"):
            await anext(stream)
    assert len(factory_calls) == (0 if phase == "factory" else 1)
    assert advances == ([] if phase == "factory" else [1])
