import asyncio
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeBusy, RuntimeExecutionError
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
            system = cast(str, request.messages[0].content)
            if "REVIEWER" in system:
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
                tool_calls=(ToolCall(id="provider-call", name="web.search", arguments={"q": "safe"}),),
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


class FakeCapabilities:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def execute(self, *, tenant_id, run_id, actor, name, arguments):  # type: ignore[no-untyped-def]
        del tenant_id, run_id, arguments
        self.calls.append((actor, name))
        return {"items": ["safe result"]}


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


async def test_ready_steps_execute_in_parallel_and_emit_framework_neutral_events() -> None:
    gateway = FakeGateway(barrier=2)
    events = await collect(CrewDispatchRuntime(gateway, plan()), context())

    assert gateway.maximum_observed_concurrency == 2
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len([event for event in events if event.kind is EventKind.STEP_COMPLETED]) == 3
    artifacts = [event.artifact for event in events if event.kind is EventKind.ARTIFACT_CREATED]
    final = artifacts[-1]
    assert final is not None and len(final.source_ids) == 2
    assert all("crewai" not in type(event).__module__ for event in events)


async def test_plan_parallelism_caps_fanout_even_when_gateway_has_capacity() -> None:
    gateway = FakeGateway()
    await collect(CrewDispatchRuntime(gateway, plan(max_parallelism=1)), context())
    assert gateway.maximum_observed_concurrency == 1


async def test_reviewer_feedback_causes_only_bounded_retry() -> None:
    gateway = FakeGateway(reviews=['{"verdict":"revise","feedback":"fix it"}', '{"verdict":"approve"}'])
    events = await collect(CrewDispatchRuntime(gateway, plan(review=True)), context())
    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == 1
    assert any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    review_index = next(
        index for index, event in enumerate(events) if event.kind is EventKind.REVIEW_COMPLETED
    )
    assert events[review_index + 1].kind is EventKind.CHECKPOINT_SAVED
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_review_retry_exhaustion_prevents_synthesis_and_redacts_output() -> None:
    sentinel = "model-secret-sentinel"
    gateway = FakeGateway(
        reviews=[
            f'{{"verdict":"revise","feedback":"{sentinel}"}}',
            f'{{"verdict":"revise","feedback":"{sentinel}"}}',
        ]
    )
    runtime = CrewDispatchRuntime(gateway, plan(review=True))
    with pytest.raises(RuntimeExecutionError) as caught:
        await collect(runtime, context())
    assert sentinel not in str(caught.value)
    assert not any("SYNTHESIZER" in cast(str, request.messages[0].content) for request in gateway.requests)


async def test_checkpoint_can_resume_completed_run_without_model_calls() -> None:
    first = CrewDispatchRuntime(FakeGateway(), plan())
    events = await collect(first, context())
    checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.kind is EventKind.CHECKPOINT_SAVED
    )
    assert checkpoint is not None
    second_gateway = FakeGateway()
    second = CrewDispatchRuntime(second_gateway, plan())
    await second.restore_checkpoint(checkpoint)
    stored_artifacts = tuple(
        event.artifact
        for event in events
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
    )
    resumed = await collect(
        second, context(checkpoint=checkpoint, artifacts=stored_artifacts)
    )
    assert [event.kind for event in resumed] == [EventKind.RUNTIME_COMPLETED]
    assert not second_gateway.requests


async def test_cancel_and_early_close_clean_siblings_and_allow_reuse() -> None:
    gateway = FakeGateway(barrier=99)
    runtime = CrewDispatchRuntime(gateway, plan())
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
    stream: AsyncIterator[RunEvent] = runtime.run(context())
    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]
    gateway.barrier = 1
    gateway.release.set()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_runtime_revalidates_constructed_plan_before_gateway() -> None:
    unsafe = plan().model_copy()
    object.__setattr__(unsafe.steps[0], "id", "../secret")
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, unsafe)
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
        CrewDispatchRuntime(FakeGateway(), plan()).run(unsafe)
    assert sentinel not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


async def test_single_stream_consumer_and_mode_boundary() -> None:
    runtime = CrewDispatchRuntime(FakeGateway(barrier=99), plan())
    stream = runtime.run(context())
    await anext(stream)

    async def consume_other() -> RunEvent:
        return await anext(stream)

    with pytest.raises(RuntimeBusy):
        await asyncio.create_task(consume_other())
    await stream.aclose()  # type: ignore[attr-defined]
    bad = context().model_copy(update={"mode": TaskMode.DIRECT})
    with pytest.raises(RuntimeExecutionError, match="mode"):
        runtime.run(bad)


async def test_unstarted_stream_close_releases_runtime_reservation() -> None:
    runtime = CrewDispatchRuntime(FakeGateway(), plan())
    stream = runtime.run(context())
    await stream.aclose()  # type: ignore[attr-defined]
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
        CrewDispatchRuntime(
            ToolGateway(), tool_plan, capability_gateway=capabilities
        ),
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
            return object()

    runtime = CrewDispatchRuntime(FakeGateway(), plan(), crew_factory=Factory())
    await collect(runtime, context())
    assert captured["share_crew"] is False
    assert captured["telemetry_disabled"] is True
    agents = captured["agents"]
    assert isinstance(agents, tuple)
    assert all(
        not item.allow_delegation and not item.memory and not item.code_execution
        for item in agents
    )
