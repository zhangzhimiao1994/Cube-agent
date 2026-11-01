"""Actual Crew stream closure must let its single owner finish cancellation."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.harness.review_typed_fixture import UnusedGateway
from tests.unit.runtime.crew.test_execution_diagnostic_continuation import context


@pytest.mark.parametrize("concurrent_cancels", [1, 2])
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_cancel_allows_owner_to_close_after_a_queued_terminal(
    monkeypatch: pytest.MonkeyPatch,
    concurrent_cancels: int,
    cleanup_failure: bool,
) -> None:
    runtime = adapter.CrewDispatchRuntime(
        UnusedGateway(),
        DispatchPlan(
            agents=(AgentSpec(id="writer", role="Writer", goal="Reply", logical_model="primary"),),
            steps=(DispatchStep(id="reply", agent="writer", task="Reply", final_synthesizer=True),),
        ),
    )
    started, stopping, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release, cleaned, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def coordinate(
        ctx: TaskContext,
        queue: asyncio.Queue[RunEvent],
        terminal: asyncio.Future[adapter._Terminal],
        state: adapter._RunState,
    ) -> None:
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            # The real coordinator can queue cancellation/checkpoint events
            # while its awaited cleanup is still draining.
            queue.put_nowait(RunEvent(
                kind=EventKind.RUNTIME_CANCELLED, sequence=1, run_id=ctx.run_id,
            ))
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            cleaned.set()
            if cleanup_failure:
                state.cleanup_error = adapter.RuntimeExecutionError("artifact rollback failed")
            terminal.set_result(adapter._Terminal(state.cleanup_error or asyncio.CancelledError()))

    monkeypatch.setattr(runtime, "_coordinate", coordinate)
    stream = runtime.run(context())
    assert isinstance(stream, adapter.CrewRunStream)
    before = asyncio.all_tasks()

    async def consume_and_close() -> None:
        event = await anext(stream)
        assert event.kind is EventKind.RUNTIME_CANCELLED
        closing.set()
        await stream.aclose()

    owner = asyncio.create_task(consume_and_close())
    cancellers: list[asyncio.Task[None]] = []
    try:
        await asyncio.wait_for(started.wait(), 1)
        cancellers = [asyncio.create_task(runtime.cancel()) for _ in range(concurrent_cancels)]
        await asyncio.wait_for(stopping.wait(), 1)
        await asyncio.wait_for(closing.wait(), 1)
        await asyncio.sleep(0)
        assert not owner.done() and all(not task.done() for task in cancellers)
        assert not cleaned.is_set() and not interrupted.is_set()
        release.set()
        _, pending = await asyncio.wait((owner, *cancellers), timeout=1)
        assert not pending, "cancel held the state lock needed by the stream owner"
        for task in (owner, *cancellers):
            if cleanup_failure:
                with pytest.raises(adapter.RuntimeExecutionError, match="^artifact rollback failed$"):
                    task.result()
            else:
                task.result()
        assert calls == 1 and cleaned.is_set() and not interrupted.is_set()
        assert runtime._active_task is None and runtime._active_stream is None
        assert runtime._active_done is None and stream._closed
    finally:
        release.set()
        for task in (owner, *cancellers):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, *cancellers, return_exceptions=True)
        with suppress(adapter.RuntimeExecutionError):
            await stream.aclose()
    assert not (asyncio.all_tasks() - before)
