import asyncio
import importlib
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.gateway import ModelGateway as LeasedModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CapabilityGateway,
    CapabilityOutcomeUncertain,
    CrewAgentDefinition,
    CrewAIObjectFactory,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewRunStream,
    CrewTaskDefinition,
    RuntimeBusy,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.adapter import (
    ModelGateway as RuntimeModelGateway,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep

RUN_ID = UUID("00000000-0000-4000-8000-000000000021")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000022")


class FakeGateway:
    def __init__(self, *, barrier: int = 1, reviews: list[str] | None = None) -> None:
        self.requests: list[ModelRequest] = []
        self.active = 0
        self.maximum_observed_concurrency = 0
        self.barrier = barrier
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.reviews = list(reviews or [])
        if barrier == 1:
            self.release.set()

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        self.active += 1
        self.maximum_observed_concurrency = max(self.maximum_observed_concurrency, self.active)
        if self.active >= self.barrier:
            self.started.set()
            self.release.set()
        try:
            await self.release.wait()
            prompt = " ".join(cast(str, message.content) for message in request.messages)
            if "REVIEWER" in prompt:
                text = self.reviews.pop(0) if self.reviews else '{"verdict":"approve"}'
            else:
                text = f"safe result {len(self.requests)}"
            return GatewayCompletion(
                response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
            )
        finally:
            self.active -= 1


class ToolGateway(FakeGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web.search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
        )


class CostGateway(FakeGateway):
    def __init__(self, cost_usd: Decimal, *, reviews: list[str] | None = None) -> None:
        super().__init__(reviews=reviews)
        self.cost_usd = cost_usd

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        completion = await super().complete_with_context(request)
        return GatewayCompletion(
            response=completion.response,
            deployment_id=completion.deployment_id,
            logical_model=completion.logical_model,
            provider_id=completion.provider_id,
            provider_model=completion.provider_model,
            cost_usd=self.cost_usd,
        )


class FakeCapabilities:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def execute(  # type: ignore[no-untyped-def]
        self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
    ):
        del tenant_id, run_id, arguments, idempotency_key
        self.calls.append((actor, name))
        return {"items": ["safe result"]}

    def is_replay_safe(self, name: str) -> bool:
        return name == "web.search"


class FastGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, agent_id, storage_scope
        return await bridge.complete([{"role": "system", "content": prompt}])


class FastFactory:
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FastGeneration:
        del agents, tasks
        assert share_crew is False
        assert telemetry_disabled is True
        return FastGeneration()


def plan(*, review: bool = False, max_parallelism: int = 2) -> DispatchPlan:
    agents = (
        AgentSpec(id="researcher", role="researcher", goal="Research", logical_model="general"),
        AgentSpec(id="critic", role="critic", goal="Review", logical_model="general"),
        AgentSpec(id="writer", role="writer", goal="Synthesize", logical_model="general"),
    )
    return DispatchPlan(
        agents=agents,
        steps=(
            DispatchStep(
                id="left",
                agent="researcher",
                task="Research left",
                reviewer="critic" if review else None,
                reviewer_retries=1 if review else 0,
                token_budget=100,
            ),
            DispatchStep(id="right", agent="researcher", task="Research right", token_budget=100),
            DispatchStep(
                id="final",
                agent="writer",
                task="Synthesize",
                depends_on=("left", "right"),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        max_parallelism=max_parallelism,
        total_token_budget=1000,
    )


def context(*, checkpoint=None, artifacts=()) -> TaskContext:  # type: ignore[no-untyped-def]
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Do bounded research",
        checkpoint=checkpoint,
        artifacts=artifacts,
        token_budget=1000,
    )


async def collect(runtime: CrewDispatchRuntime, ctx: TaskContext) -> list[RunEvent]:
    return [event async for event in runtime.run(ctx)]


def make_runtime(
    gateway: RuntimeModelGateway,
    dispatch_plan: DispatchPlan | None = None,
    *,
    capability_gateway: CapabilityGateway | None = None,
    crew_factory: CrewObjectFactory | None = None,
) -> CrewDispatchRuntime:
    return CrewDispatchRuntime(
        gateway,
        dispatch_plan or plan(),
        capability_gateway=capability_gateway,
        crew_factory=crew_factory or FastFactory(),
    )


async def test_ready_steps_execute_in_parallel_and_emit_framework_neutral_events() -> None:
    gateway = FakeGateway(barrier=2)
    events = await collect(make_runtime(gateway), context())

    assert gateway.maximum_observed_concurrency == 2
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len([event for event in events if event.kind is EventKind.STEP_COMPLETED]) == 3
    artifacts = [event.artifact for event in events if event.kind is EventKind.ARTIFACT_CREATED]
    final = artifacts[-1]
    assert final is not None and len(final.source_ids) == 2
    assert all("crewai" not in type(event).__module__ for event in events)


async def test_plan_parallelism_caps_fanout_even_when_gateway_has_capacity() -> None:
    gateway = FakeGateway()
    await collect(make_runtime(gateway, plan(max_parallelism=1)), context())
    assert gateway.maximum_observed_concurrency == 1


async def test_real_model_gateway_queues_agents_sharing_one_quota_scope() -> None:
    class SharedCapacity:
        def __init__(self) -> None:
            self.semaphore = asyncio.Semaphore(1)
            self.active = 0
            self.maximum_active = 0
            self.next_candidate = 0
            self.scopes: list[str] = []

        async def initialize(self) -> None:
            return None

        def validate_configuration(self, deployments):  # type: ignore[no-untyped-def]
            assert {item.quota_scope_id for item in deployments} == {"shared-deepseek"}

        async def acquire(  # type: ignore[no-untyped-def]
            self, candidates, wait_timeout, *, estimated_tokens
        ):
            del wait_timeout, estimated_tokens
            await self.semaphore.acquire()
            selected = candidates[self.next_candidate % len(candidates)]
            self.next_candidate += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.scopes.append(selected.quota_scope_id)
            return CapacityLease(
                id=str(uuid4()),
                deployment_id=selected.id,
                quota_scope_id=selected.quota_scope_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=60),
                renew_after_seconds=10,
            )

        async def release(self, lease: CapacityLease) -> bool:
            del lease
            self.active -= 1
            self.semaphore.release()
            return True

        async def renew(self, lease: CapacityLease) -> CapacityLease:
            return lease

        async def record_outcome(self, quota_scope_id, **kwargs):  # type: ignore[no-untyped-def]
            del quota_scope_id, kwargs

    class Secrets:
        async def resolve(self, secret_ref: str) -> str:
            del secret_ref
            return "test-key"

    class Transport:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0

        async def complete(self, deployment, request, api_key):  # type: ignore[no-untyped-def]
            del deployment, request, api_key
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return ModelResponse(text="leased result", usage=TokenUsage(1, 1, 2))

    deployments = tuple(
        Deployment(
            id=f"deepseek-{index}",
            logical_model="general",
            provider_model="deepseek/chat",
            secret_ref=f"secret://deepseek-{index}",
            quota_scope_id="shared-deepseek",
        )
        for index in range(2)
    )
    capacity = SharedCapacity()
    transport = Transport()
    gateway = LeasedModelGateway(ModelRegistry(deployments), capacity, Secrets(), transport)

    events = await collect(make_runtime(gateway), context())

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert capacity.maximum_active == 1
    assert transport.maximum_active == 1
    assert set(capacity.scopes) == {"shared-deepseek"}


async def test_reviewer_feedback_causes_only_bounded_retry() -> None:
    gateway = FakeGateway(
        reviews=['{"verdict":"revise","feedback":"fix it"}', '{"verdict":"approve"}']
    )
    events = await collect(make_runtime(gateway, plan(review=True)), context())
    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == 1
    assert any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    review_index = next(
        index for index, event in enumerate(events) if event.kind is EventKind.REVIEW_COMPLETED
    )
    assert events[review_index + 1].kind is EventKind.CHECKPOINT_SAVED
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_reviewer_feedback_artifact_survives_crash_resume() -> None:
    class CrashOnRetryGeneration(FastGeneration):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.prompts: list[str] = []

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            self.prompts.append(prompt)
            if "untrusted_reviewer_feedback" in prompt:
                self.started.set()
                await asyncio.Event().wait()
            return await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )

    generation = CrashOnRetryGeneration()

    class Factory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return generation

    first = make_runtime(
        FakeGateway(reviews=['{"verdict":"revise","feedback":"persist me"}']),
        plan(review=True),
        crew_factory=Factory(),
    )
    first_events: list[RunEvent] = []

    async def consume() -> None:
        async for event in first.run(context()):
            first_events.append(event)

    task = asyncio.create_task(consume())
    await generation.started.wait()
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    checkpoint = next(
        event.checkpoint
        for event in reversed(first_events)
        if event.kind is EventKind.CHECKPOINT_SAVED
        and event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
    )
    assert "persist me" not in json.dumps(checkpoint.to_payload())

    referenced_ids = {
        reference["id"]
        for field in ("artifact_refs", "review_refs")
        for reference in cast(Mapping[str, Mapping[str, str]], checkpoint.state[field]).values()
    }
    artifacts = tuple(
        event.artifact
        for event in first_events
        if event.artifact is not None and str(event.artifact.id) in referenced_ids
    )

    class CapturingGeneration(FastGeneration):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            self.prompts.append(prompt)
            return await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )

    resumed_generation = CapturingGeneration()

    class ResumedFactory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return resumed_generation

    resumed = make_runtime(
        FakeGateway(reviews=['{"verdict":"approve"}']),
        plan(review=True),
        crew_factory=ResumedFactory(),
    )
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(resumed, context(checkpoint=checkpoint, artifacts=artifacts))

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any("persist me" in prompt for prompt in resumed_generation.prompts)


async def test_review_retry_exhaustion_prevents_synthesis_and_redacts_output() -> None:
    sentinel = "model-secret-sentinel"
    gateway = FakeGateway(
        reviews=[
            f'{{"verdict":"revise","feedback":"{sentinel}"}}',
            f'{{"verdict":"revise","feedback":"{sentinel}"}}',
        ]
    )
    runtime = make_runtime(gateway, plan(review=True))
    with pytest.raises(RuntimeExecutionError) as caught:
        await collect(runtime, context())
    assert sentinel not in str(caught.value)
    assert not any(
        "SYNTHESIZER" in cast(str, request.messages[0].content) for request in gateway.requests
    )


async def test_checkpoint_can_resume_completed_run_without_model_calls() -> None:
    first = make_runtime(FakeGateway())
    events = await collect(first, context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.kind is EventKind.CHECKPOINT_SAVED
    )
    assert checkpoint is not None
    second_gateway = FakeGateway()
    second = make_runtime(second_gateway)
    await second.restore_checkpoint(checkpoint)
    stored_artifacts = tuple(
        event.artifact
        for event in events
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
    )
    resumed = await collect(second, context(checkpoint=checkpoint, artifacts=stored_artifacts))
    assert [event.kind for event in resumed] == [EventKind.RUNTIME_COMPLETED]
    assert not second_gateway.requests


async def test_nonterminal_checkpoint_resumes_sequence_without_rerunning_sibling() -> None:
    left_completed = asyncio.Event()

    class PartialGateway(FakeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            prompt = " ".join(cast(str, message.content) for message in request.messages)
            if "Research left" in prompt:
                left_completed.set()
            elif "Research right" in prompt:
                await left_completed.wait()
                raise RuntimeError("provider detail must be redacted")
            return await super().complete_with_context(request)

    first = make_runtime(PartialGateway())
    first_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in first.run(context()):
            first_events.append(event)
    checkpoint = await first.save_checkpoint()
    assert checkpoint.state["completed"] == ("left",)

    stored_artifacts = tuple(
        event.artifact
        for event in first_events
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
    )
    gateway = FakeGateway()
    resumed_runtime = make_runtime(gateway)
    await resumed_runtime.restore_checkpoint(checkpoint)
    resumed = await collect(
        resumed_runtime,
        context(checkpoint=checkpoint, artifacts=stored_artifacts),
    )

    prompts = [
        " ".join(cast(str, message.content) for message in request.messages)
        for request in gateway.requests
    ]
    assert not any("Research left" in prompt for prompt in prompts)
    assert resumed[0].sequence == max(event.sequence for event in first_events) + 1
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_cancel_and_early_close_clean_siblings_and_allow_reuse() -> None:
    gateway = FakeGateway(barrier=99)
    runtime = make_runtime(gateway)
    task = asyncio.create_task(collect(runtime, context()))
    while len(gateway.requests) < 2:
        await asyncio.sleep(0)
    await runtime.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    gateway.barrier = 1
    gateway.release.set()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED

    gateway.barrier = 99
    gateway.release.clear()
    stream = cast(CrewRunStream, runtime.run(context()))
    await anext(stream)
    await stream.aclose()
    gateway.barrier = 1
    gateway.release.set()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_noncooperative_generation_is_detached_and_runtime_cancelled_is_emitted() -> None:
    class NoncooperativeGeneration(FastGeneration):
        def __init__(self) -> None:
            self.block = True
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            if self.block:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                return "detached result"
            return await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )

    class Factory(FastFactory):
        def __init__(self, generation: NoncooperativeGeneration) -> None:
            self.generation = generation

        def build(
            self,
            agents: tuple[CrewAgentDefinition, ...],
            tasks: tuple[CrewTaskDefinition, ...],
            *,
            share_crew: bool,
            telemetry_disabled: bool,
        ) -> NoncooperativeGeneration:
            del agents, tasks, share_crew, telemetry_disabled
            return self.generation

    generation = NoncooperativeGeneration()
    runtime = make_runtime(FakeGateway(), crew_factory=Factory(generation))
    events: list[RunEvent] = []

    async def consume() -> None:
        async for event in runtime.run(context()):
            events.append(event)

    task = asyncio.create_task(consume())
    await generation.started.wait()
    await asyncio.wait_for(runtime.cancel(), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert EventKind.RUNTIME_CANCELLED in [event.kind for event in events]

    generation.block = False
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED
    terminal_checkpoint = await runtime.save_checkpoint()
    generation.release.set()
    await asyncio.sleep(0)
    assert (await runtime.save_checkpoint()).id == terminal_checkpoint.id


async def test_runtime_revalidates_constructed_plan_before_gateway() -> None:
    unsafe = plan().model_copy()
    object.__setattr__(unsafe.steps[0], "id", "../secret")
    gateway = FakeGateway()
    runtime = make_runtime(gateway, unsafe)
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime, context())
    assert not gateway.requests


async def test_constructed_context_failure_has_no_secret_exception_chain() -> None:
    sentinel = "raw-api-key-sentinel"
    unsafe = TaskContext.model_construct(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request=f"bad\x00{sentinel}",
        artifacts=(),
        checkpoint=None,
        timeout_seconds=60.0,
        token_budget=True,
    )
    with pytest.raises(RuntimeExecutionError) as caught:
        make_runtime(FakeGateway()).run(unsafe)
    assert sentinel not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


async def test_single_stream_consumer_and_mode_boundary() -> None:
    runtime = make_runtime(FakeGateway(barrier=99))
    stream = cast(CrewRunStream, runtime.run(context()))
    await anext(stream)

    async def consume_other() -> RunEvent:
        return await anext(stream)

    with pytest.raises(RuntimeBusy):
        await asyncio.create_task(consume_other())
    await stream.aclose()
    bad = context().model_copy(update={"mode": TaskMode.DIRECT})
    with pytest.raises(RuntimeExecutionError, match="mode"):
        runtime.run(bad)


async def test_unstarted_stream_close_releases_runtime_reservation() -> None:
    runtime = make_runtime(FakeGateway())
    stream = cast(CrewRunStream, runtime.run(context()))
    await stream.aclose()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_tool_calls_only_cross_the_capability_gateway() -> None:
    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    capabilities = FakeCapabilities()
    events = await collect(
        make_runtime(ToolGateway(), tool_plan, capability_gateway=capabilities),
        context(),
    )
    assert capabilities.calls == [("writer", "web.search")]
    assert [event.kind for event in events if str(event.kind).startswith("tool.")] == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
    ]
    tool_completed_index = next(
        index for index, event in enumerate(events) if event.kind is EventKind.TOOL_COMPLETED
    )
    assert events[tool_completed_index + 1].kind is EventKind.CHECKPOINT_SAVED


async def test_succeeded_tool_result_resumes_without_repeating_side_effect() -> None:
    class FailingAfterToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if self.requests:
                raise RuntimeError("provider detail")
            return await super().complete_with_context(request)

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    first_capabilities = FakeCapabilities()
    first = make_runtime(
        FailingAfterToolGateway(), tool_plan, capability_gateway=first_capabilities
    )
    first_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in first.run(context()):
            first_events.append(event)
    checkpoint = await first.save_checkpoint()
    tool_artifacts = tuple(
        event.artifact
        for event in first_events
        if event.kind is EventKind.TOOL_COMPLETED and event.artifact is not None
    )
    assert len(tool_artifacts) == 1

    second_capabilities = FakeCapabilities()
    resumed = make_runtime(ToolGateway(), tool_plan, capability_gateway=second_capabilities)
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(
        resumed,
        context(checkpoint=checkpoint, artifacts=tool_artifacts),
    )

    assert second_capabilities.calls == []
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_replay_safe_running_tool_reuses_stable_idempotency_key() -> None:
    class RecordingCapabilities(FakeCapabilities):
        def __init__(self, *, block: bool) -> None:
            super().__init__()
            self.block = block
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.keys: list[str] = []

        async def execute(  # type: ignore[no-untyped-def]
            self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
        ):
            del tenant_id, run_id, arguments
            self.calls.append((actor, name))
            self.keys.append(idempotency_key)
            self.started.set()
            if self.block:
                await self.release.wait()
            return {"items": ["safe result"]}

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    first_capabilities = RecordingCapabilities(block=True)
    first = make_runtime(ToolGateway(), tool_plan, capability_gateway=first_capabilities)
    task = asyncio.create_task(collect(first, context()))
    await first_capabilities.started.wait()
    checkpoint = await first.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    tool_state = next(iter(tool_states.values()))
    assert isinstance(tool_state, Mapping)
    assert tool_state["status"] == "running"
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    second_capabilities = RecordingCapabilities(block=False)
    resumed = make_runtime(ToolGateway(), tool_plan, capability_gateway=second_capabilities)
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(resumed, context(checkpoint=checkpoint))

    assert second_capabilities.keys == first_capabilities.keys
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_detached_old_tool_cannot_overwrite_new_run_checkpoint() -> None:
    class LateToolCapabilities(FakeCapabilities):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.returned = asyncio.Event()

        async def execute(  # type: ignore[no-untyped-def]
            self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
        ):
            del tenant_id, run_id, arguments, idempotency_key
            self.calls.append((actor, name))
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            self.returned.set()
            return {"items": ["late result"]}

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    capabilities = LateToolCapabilities()
    runtime = make_runtime(ToolGateway(), tool_plan, capability_gateway=capabilities)
    old_task = asyncio.create_task(collect(runtime, context()))
    await capabilities.started.wait()
    await runtime.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task

    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED
    current_checkpoint = await runtime.save_checkpoint()
    assert current_checkpoint.state["phase"] == "completed"

    capabilities.release.set()
    await capabilities.returned.wait()
    for _ in range(10):
        await asyncio.sleep(0)

    assert (await runtime.save_checkpoint()).id == current_checkpoint.id


async def test_uncertain_tool_checkpoint_fails_closed_without_replay() -> None:
    class UncertainCapabilities(FakeCapabilities):
        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            raise RuntimeError("commit status unavailable")

        def is_replay_safe(self, name: str) -> bool:
            del name
            return False

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("account.update",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("account.update",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("account.update",),
        total_token_budget=100,
    )

    class RestrictedToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if completion.response.tool_calls:
                response = ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-call",
                            name="account.update",
                            arguments={"value": "safe"},
                        ),
                    ),
                    usage=completion.response.usage,
                )
                return GatewayCompletion(
                    response=response,
                    deployment_id=completion.deployment_id,
                    logical_model=completion.logical_model,
                    provider_id=completion.provider_id,
                    provider_model=completion.provider_model,
                )
            return completion

    first = make_runtime(
        RestrictedToolGateway(),
        tool_plan,
        capability_gateway=UncertainCapabilities(),
    )
    with pytest.raises(CapabilityOutcomeUncertain):
        await collect(first, context())
    checkpoint = await first.save_checkpoint()
    second_capabilities = UncertainCapabilities()
    resumed = make_runtime(
        RestrictedToolGateway(),
        tool_plan,
        capability_gateway=second_capabilities,
    )
    await resumed.restore_checkpoint(checkpoint)

    with pytest.raises(CapabilityOutcomeUncertain):
        await collect(resumed, context(checkpoint=checkpoint))


async def test_tool_idempotency_key_changes_when_canonical_arguments_change() -> None:
    class ArgumentGateway(ToolGateway):
        def __init__(self, query: str) -> None:
            super().__init__()
            self.query = query

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-call",
                            name="web.search",
                            arguments={"q": self.query},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
                if len(self.requests) == 1
                else ModelResponse(text="done", usage=TokenUsage(1, 1, 2))
            )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
            )

    class KeyCapabilities(FakeCapabilities):
        def __init__(self) -> None:
            super().__init__()
            self.keys: list[str] = []

        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            self.keys.append(kwargs["idempotency_key"])
            return await super().execute(**kwargs)  # type: ignore[no-untyped-call]

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    capabilities = KeyCapabilities()
    await collect(
        make_runtime(
            ArgumentGateway("first"),
            tool_plan,
            capability_gateway=capabilities,
        ),
        context(),
    )
    await collect(
        make_runtime(
            ArgumentGateway("second"),
            tool_plan,
            capability_gateway=capabilities,
        ),
        context(),
    )

    assert len(capabilities.keys) == 2
    assert capabilities.keys[0] != capabilities.keys[1]


@pytest.mark.parametrize("limit_source", ["step", "run"])
async def test_one_monotonic_deadline_bounds_cumulative_step_work(
    limit_source: str,
) -> None:
    class SlowGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            await asyncio.sleep(0.06)
            return await super().complete_with_context(request)

    class SlowCapabilities(FakeCapabilities):
        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.06)
            return await super().execute(**kwargs)  # type: ignore[no-untyped-call]

    deadline = 0.1
    step_timeout = deadline if limit_source == "step" else 0.5
    run_timeout = deadline if limit_source == "run" else 0.5
    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
                timeout_seconds=step_timeout,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
        total_timeout_seconds=0.5,
    )
    ctx = context().model_copy(update={"timeout_seconds": run_timeout})
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError):
        await collect(
            make_runtime(
                SlowGateway(),
                tool_plan,
                capability_gateway=SlowCapabilities(),
            ),
            ctx,
        )

    assert time.monotonic() - started < 0.2


async def test_trusted_cost_is_atomic_in_parallel_checkpoint_and_retry() -> None:
    priced_steps = tuple(
        step.model_copy(update={"cost_budget_usd": Decimal("0.10")})
        for step in plan(review=True).steps
    )
    priced_plan = plan(review=True).model_copy(
        update={
            "steps": priced_steps,
            "total_cost_usd": Decimal("1.00"),
        }
    )
    gateway = CostGateway(
        Decimal("0.01"),
        reviews=['{"verdict":"revise","feedback":"retry"}', '{"verdict":"approve"}'],
    )
    events = await collect(make_runtime(gateway, priced_plan), context())
    final_checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )

    cost_events = [event for event in events if event.kind is EventKind.COST_RECORDED]
    assert len(cost_events) == 6
    assert all(event.cost_usd == Decimal("0.01") for event in cost_events)
    assert final_checkpoint.state["usage"] == {
        "tokens": 12,
        "cost_usd": "0.06",
    }


async def test_trusted_cost_fails_closed_at_step_budget() -> None:
    base = plan(max_parallelism=1)
    priced_plan = base.model_copy(
        update={
            "steps": tuple(
                step.model_copy(update={"cost_budget_usd": Decimal("0.01")}) for step in base.steps
            ),
            "total_cost_usd": Decimal(1),
        }
    )
    with pytest.raises(RuntimeExecutionError):
        await collect(
            make_runtime(CostGateway(Decimal("0.02")), priced_plan),
            context(),
        )


async def test_private_factory_receives_locked_down_framework_generation() -> None:
    captured: dict[str, object] = {}

    class Factory:
        def build(self, agents, tasks, *, share_crew, telemetry_disabled):  # type: ignore[no-untyped-def]
            captured.update(
                agents=agents,
                tasks=tasks,
                share_crew=share_crew,
                telemetry_disabled=telemetry_disabled,
            )
            return FastGeneration()

    runtime = make_runtime(FakeGateway(), crew_factory=Factory())
    await collect(runtime, context())
    assert captured["share_crew"] is False
    assert captured["telemetry_disabled"] is True
    agents = captured["agents"]
    assert isinstance(agents, tuple)
    assert all(
        not item.allow_delegation and not item.memory and not item.code_execution for item in agents
    )


async def test_real_crewai_akickoff_uses_only_model_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "true")
    storage_root = tmp_path / "crewai"
    scoped_storage = storage_root / "agent-hub" / str(TENANT_ID) / str(RUN_ID)
    scoped_storage.mkdir(parents=True)
    (scoped_storage / ".crewai_user.json").write_text(
        json.dumps({"first_execution_done": True, "trace_consent": True}),
        encoding="utf-8",
    )
    gateway = FakeGateway()
    events = await collect(
        make_runtime(
            gateway,
            crew_factory=CrewAIObjectFactory(storage_dir=storage_root),
        ),
        context(),
    )
    tracing_utils = importlib.import_module("crewai.events.listeners.tracing.utils")

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    assert tracing_utils.is_tracing_enabled_in_context() is False


async def test_real_crewai_storage_is_scoped_without_global_pollution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdirs_module = importlib.import_module("appdirs")
    forbidden_calls: list[tuple[object, ...]] = []

    def forbidden_default_storage(*args, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        forbidden_calls.append(args)
        raise AssertionError("default user storage must not be consulted")

    monkeypatch.setattr(appdirs_module, "user_data_dir", forbidden_default_storage)
    before_function = appdirs_module.user_data_dir
    before_environment = dict(os.environ)
    before_cwd = os.getcwd()
    storage_root = tmp_path / "controlled-crewai"

    events = await collect(
        make_runtime(
            FakeGateway(),
            crew_factory=CrewAIObjectFactory(storage_dir=storage_root),
        ),
        context(),
    )

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert forbidden_calls == []
    assert appdirs_module.user_data_dir is before_function
    assert dict(os.environ) == before_environment
    assert os.getcwd() == before_cwd
    files = tuple(path for path in storage_root.rglob("*") if path.is_file())
    assert files
    assert all(str(path).startswith(str(storage_root)) for path in files)


async def test_real_crewai_concurrent_runs_use_distinct_storage_scopes(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "concurrent-crewai"
    factory = CrewAIObjectFactory(storage_dir=storage_root)
    first_context = context().model_copy(update={"run_id": uuid4(), "tenant_id": uuid4()})
    second_context = context().model_copy(update={"run_id": uuid4(), "tenant_id": uuid4()})

    first_events, second_events = await asyncio.gather(
        collect(
            make_runtime(FakeGateway(), crew_factory=factory),
            first_context,
        ),
        collect(
            make_runtime(FakeGateway(), crew_factory=factory),
            second_context,
        ),
    )

    assert first_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert second_events[-1].kind is EventKind.RUNTIME_COMPLETED
    for ctx in (first_context, second_context):
        scope = storage_root / "agent-hub" / str(ctx.tenant_id) / str(ctx.run_id)
        assert scope.is_dir()
        assert any(path.is_file() for path in scope.rglob("*"))


def test_crewai_storage_router_preserves_external_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    sentinel = "external-crewai-storage"
    monkeypatch.setattr(
        adapter_module,
        "_CREWAI_DEFAULT_STORAGE_PATH",
        lambda: sentinel,
    )

    assert adapter_module._contextual_crewai_storage_path() == sentinel
