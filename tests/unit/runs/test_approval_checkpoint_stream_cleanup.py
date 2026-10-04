from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, ExecutionRuntime, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, _RunState, _Terminal
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.defaults import (
    ConfigBackedDispatchRuntime,
    ConfigBackedHybridRuntime,
    UnavailableRuntime,
)
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_approval_checkpoint_service import (
    APPROVAL_ID,
    ApprovalRepository,
    crew_checkpoint,
)
from tests.unit.runs.test_terminal_hooks import TENANT_ID

ConfigRuntime = ConfigBackedDispatchRuntime | ConfigBackedHybridRuntime


class ControlledCrew(CrewDispatchRuntime):
    """Keep the real stream/producer ownership; replace only model/tool work."""

    def __init__(self, repository: ApprovalRepository | None = None) -> None:
        plan = DispatchPlan(
            agents=(AgentSpec(
                id="implementer", role="Implementer", goal="Finish", logical_model="general",
                allowed_tools=(),
            ),),
            steps=(DispatchStep(
                id="final", agent="implementer", task="Finish", tools=(),
                final_synthesizer=True, token_budget=100,
            ),),
            allowed_tools=(), total_token_budget=100,
        )
        super().__init__(cast(Any, object()), plan, crew_factory=cast(Any, object()))
        self.repository = repository
        self.proceed = asyncio.Event()
        self.stopped = asyncio.Event()
        self.producer: asyncio.Task[None] | None = None
        self.producer_state: _RunState | None = None

    async def _coordinate(
        self,
        context: TaskContext,
        queue: asyncio.Queue[RunEvent],
        terminal_future: asyncio.Future[_Terminal],
        state: _RunState,
    ) -> None:
        del terminal_future
        self.producer = asyncio.current_task()
        self.producer_state = state
        try:
            if self.repository is not None:
                self.repository.begin_approval()
            await queue.put(RunEvent(
                kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=context.run_id,
                checkpoint=crew_checkpoint(context.run_id),
            ))
            await self.proceed.wait()
            await queue.put(RunEvent(
                kind=EventKind.TOOL_STARTED, sequence=2, run_id=context.run_id,
                actor="implementer", tool_call_id="still-alive", tool_name="workspace.write_text",
            ))
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


def _runtime_chain(crew: ControlledCrew, *, hybrid: bool) -> ExecutionRuntime:
    if not hybrid:
        return crew
    return HybridRuntime(
        crew,
        UnavailableRuntime(TaskMode.DISCUSS),
        UnavailableRuntime(TaskMode.DIRECT),
    )


def _wrapper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hybrid: bool,
    factory: Callable[[TaskContext], Awaitable[ExecutionRuntime]],
) -> ConfigRuntime:
    runtime_class = ConfigBackedHybridRuntime if hybrid else ConfigBackedDispatchRuntime
    runtime = runtime_class(
        config_service=cast(Any, object()), secret_service=cast(Any, object()),
        capacity_factory=cast(Any, object()), transport=cast(Any, object()),
    )
    monkeypatch.setattr(runtime, "_runtime_for", factory)
    return runtime


async def _close_stream(stream: AsyncIterator[RunEvent]) -> None:
    await cast(AsyncGenerator[RunEvent, None], stream).aclose()


async def _cleanup_crews(*crews: ControlledCrew) -> None:
    async def cleanup(crew: ControlledCrew) -> None:
        try:
            async with asyncio.timeout(2):
                await crew.cancel()
        finally:
            producer = crew.producer
            if producer is not None:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    results = await asyncio.gather(*(cleanup(crew) for crew in crews), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


@pytest.mark.asyncio
@pytest.mark.parametrize("hybrid", [False, True], ids=["dispatch", "hybrid"])
async def test_service_checkpoint_stop_closes_real_crew_producer(
    hybrid: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ApprovalRepository()
    crew = ControlledCrew(repository)
    inner = _runtime_chain(crew, hybrid=hybrid)

    async def factory(context: TaskContext) -> ExecutionRuntime:
        assert context.run_id == repository.run_id
        return inner

    wrapper = _wrapper(monkeypatch, hybrid=hybrid, factory=factory)
    repository.row.mode = wrapper.mode.value
    service = RunService(
        cast(Any, repository), runtime_registry=RuntimeRegistry((wrapper,)),
        router=None, task_queue=cast(Any, object()),
    )
    try:
        async with asyncio.timeout(2):
            result = await service.execute(repository.run_id)
        assert result.status is RunStatus.WAITING_APPROVAL
        assert checkpoint_waits_for_approval(repository.checkpoint, APPROVAL_ID)
        assert crew.stopped.is_set(), "service returned before the old producer stopped"
        assert crew.producer_state is not None
        assert not crew.producer_state.artifact_writes_open
        assert crew._active_stream is None
        assert not wrapper._active
    finally:
        await _cleanup_crews(crew)


@pytest.mark.asyncio
@pytest.mark.parametrize("hybrid", [False, True], ids=["dispatch", "hybrid"])
async def test_closing_old_execution_preserves_new_owner_stream(
    hybrid: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, old_token, new_token = uuid4(), uuid4(), uuid4()
    old_crew, new_crew = ControlledCrew(), ControlledCrew()
    old_inner = _runtime_chain(old_crew, hybrid=hybrid)
    new_inner = _runtime_chain(new_crew, hybrid=hybrid)

    async def factory(context: TaskContext) -> ExecutionRuntime:
        assert context.run_id == run_id
        assert context.execution_token in {old_token, new_token}
        return old_inner if context.execution_token == old_token else new_inner

    wrapper = _wrapper(monkeypatch, hybrid=hybrid, factory=factory)

    def context(token: UUID) -> TaskContext:
        return TaskContext(
            run_id=run_id, tenant_id=TENANT_ID, mode=wrapper.mode,
            request="approval stream ownership", execution_token=token,
            token_budget=100, timeout_seconds=30,
        )

    old_stream = wrapper.run(context(old_token))
    new_stream = wrapper.run(context(new_token))
    new_started = asyncio.Event()
    keep_new_alive = asyncio.Event()
    new_event: asyncio.Future[RunEvent] = asyncio.get_running_loop().create_future()

    async def consume_new() -> None:
        try:
            assert (await anext(new_stream)).kind is EventKind.CHECKPOINT_SAVED
            new_started.set()
            new_event.set_result(await anext(new_stream))
            await keep_new_alive.wait()
        finally:
            await _close_stream(new_stream)

    new_consumer: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(2):
            assert (await anext(old_stream)).kind is EventKind.CHECKPOINT_SAVED
            new_consumer = asyncio.create_task(consume_new())
            await new_started.wait()
            # Both executions share run_id; cleanup must retain the execution-token fence.
            await _close_stream(old_stream)
            await wrapper.cancel_run_owned(run_id, old_token)
            assert not new_crew.stopped.is_set(), "old close cancelled the new owner"
            assert wrapper._active.get((run_id, new_token)) is new_inner
            new_crew.proceed.set()
            event = await new_event
            assert event.kind is EventKind.TOOL_STARTED
            assert event.tool_call_id == "still-alive"
            assert not new_consumer.done()
            assert old_crew.stopped.is_set(), "old wrapper close left its producer alive"
            assert old_crew.producer_state is not None
            assert not old_crew.producer_state.artifact_writes_open
            assert new_crew.producer_state is not None
            assert new_crew.producer_state.artifact_writes_open
    finally:
        try:
            if new_consumer is not None:
                keep_new_alive.set()
                if not new_consumer.done():
                    new_consumer.cancel()
                await asyncio.gather(new_consumer, return_exceptions=True)
            await _close_stream(old_stream)
        finally:
            await _cleanup_crews(old_crew, new_crew)
