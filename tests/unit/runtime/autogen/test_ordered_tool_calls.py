"""Ordered tool calls share a journal without retrying an uncertain transaction."""

import asyncio
import hashlib
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from autogen_core import CancellationToken

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.autogen.adapter import (
    GatewayCapabilityTool,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.runtime.autogen.test_draft_action_progress import action_context
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability


class OrderedHarness:
    def __init__(self, context: TaskContext, *, unknown_first: bool = False) -> None:
        self.context = context
        self.unknown_first = unknown_first
        self.calls: list[HarnessToolCallRequest] = []
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id
        assert request.run_id == self.context.run_id and request.actor == "implementer"
        assert user_id == self.context.actor_id and role == self.context.actor_role
        assert request.tool_name == "workspace.write_text"
        self.calls.append(request)
        first = len(self.calls) == 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if first:
                self.started.set()
                await self.proceed.wait()
                if self.unknown_first:
                    raise RuntimeError("test-only uncertain tool outcome")
            content = request.arguments["content"]
            assert isinstance(content, str)
            return HarnessToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name, status="succeeded",
                payload={"workspace_files": ({"path": request.arguments["path"],
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "size_bytes": len(content.encode())},)},
            )
        finally:
            self.active -= 1


class ReplayPolicy:
    def __init__(self, replay_safe: bool) -> None:
        self.replay_safe = replay_safe

    def is_replay_safe(self, name: str) -> bool:
        assert name == "workspace.write_text"
        return self.replay_safe


def context_for(durable: _DiscussionDurability) -> TaskContext:
    return action_context().model_copy(update={"run_id": durable._context_run_id})


def writer(
    durable: _DiscussionDurability, backend: OrderedHarness, *, replay_safe: bool = True,
) -> GatewayCapabilityTool:
    context = backend.context
    return GatewayCapabilityTool(
        cast(Any, ReplayPolicy(replay_safe)), tenant_id=context.tenant_id, run_id=context.run_id,
        actor="implementer", name="workspace.write_text", records=[], durability=durable,
        harness_tool_gateway=backend, user_id=context.actor_id, role=context.actor_role,
        server_context=context, sandbox_profile="workspace_write", serialize_tool_calls=True,
    )


def arguments(index: int) -> dict[str, JsonValue]:
    return {"path": f"src/file{index}.cjs", "content": f"module.exports = {index};\n"}


async def wait_for_lock_waiters(durable: _DiscussionDurability, count: int) -> None:
    async with asyncio.timeout(2):
        while len(durable._tool_lock._waiters or ()) < count:
            await asyncio.sleep(0)


def restored_journal(original: _DiscussionDurability) -> _DiscussionDurability:
    # Restore the real recorded rows/artifacts into a fresh lock/cursor, not fake successes.
    restored = _durability(artifacts=original.artifacts)
    restored._context_run_id = original._context_run_id
    restored.tool_entries = [dict(row) for row in original.tool_entries]
    return restored


@pytest.mark.parametrize("count", [2, 3])
async def test_shared_journal_serializes_multiple_facades_fifo_once(count: int) -> None:
    durable = _durability()
    backend = OrderedHarness(context_for(durable))
    tasks = [asyncio.create_task(writer(durable, backend).run_json(
        arguments(0), CancellationToken(), call_id="write-0"))]
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        for index in range(1, count):
            tasks.append(asyncio.create_task(writer(durable, backend).run_json(
                arguments(index), CancellationToken(), call_id=f"write-{index}")))
        await wait_for_lock_waiters(durable, count - 1)
        assert len(backend.calls) == 1 and durable._tool_lock.locked()
        backend.proceed.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert [call.arguments["path"] for call in backend.calls] == [
            arguments(index)["path"] for index in range(count)]
        assert backend.max_active == 1
        assert [row["position"] for row in durable.tool_entries] == list(range(count))
        assert all(row["status"] == "succeeded" for row in durable.tool_entries)
        assert len({call.idempotency_key for call in backend.calls}) == count
        assert len(durable.artifacts) == durable._tool_cursor == count
        assert not durable._tool_lock.locked()
    finally:
        backend.proceed.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("cancel_source", ["token", "task"])
async def test_cancelled_waiter_does_not_execute_or_release_another_owner(
    cancel_source: str,
) -> None:
    durable = _durability()
    backend = OrderedHarness(context_for(durable))
    first = asyncio.create_task(writer(durable, backend).run_json(
        arguments(0), CancellationToken(), call_id="first"))
    token = CancellationToken()
    second: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        second = asyncio.create_task(writer(durable, backend).run_json(
            arguments(1), token, call_id="cancelled-waiter"))
        await wait_for_lock_waiters(durable, 1)
        if cancel_source == "token":
            token.cancel()
        else:
            second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, timeout=2)
        assert len(backend.calls) == 1 and durable._tool_lock.locked()
        assert len(durable.tool_entries) == 1 and durable.tool_entries[0]["status"] == "running"
        backend.proceed.set()
        await asyncio.wait_for(first, timeout=2)
        await asyncio.wait_for(writer(durable, backend).run_json(
            arguments(2), CancellationToken(), call_id="after-cancel"), timeout=2)
        assert [call.arguments["path"] for call in backend.calls] == [
            arguments(0)["path"], arguments(2)["path"]]
        assert durable._tool_cursor == 2 and not durable._tool_lock.locked()
    finally:
        backend.proceed.set()
        first.cancel()
        if second is not None:
            second.cancel()
        await asyncio.gather(first, *([] if second is None else [second]), return_exceptions=True)


@pytest.mark.parametrize("replay_safe", [False, True])
@pytest.mark.parametrize("same_arguments", [False, True])
async def test_unknown_first_transaction_blocks_queued_io(
    replay_safe: bool, same_arguments: bool,
) -> None:
    durable = _durability()
    backend = OrderedHarness(context_for(durable), unknown_first=True)
    first = asyncio.create_task(writer(durable, backend, replay_safe=replay_safe).run_json(
        arguments(0), CancellationToken(), call_id="unknown-first"))
    second: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        second = asyncio.create_task(writer(durable, backend, replay_safe=replay_safe).run_json(
            arguments(0 if same_arguments else 1), CancellationToken(), call_id="queued-after-unknown"))
        await wait_for_lock_waiters(durable, 1)
        backend.proceed.set()
        outcomes = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True),
                                          timeout=2)
        assert len(backend.calls) == 1, "queued calls must not retry an unconfirmed transaction"
        assert all(isinstance(outcome, RuntimeExecutionError) for outcome in outcomes)
        assert durable._tool_cursor == 0 and durable.tool_entries[0]["status"] == "running"
        assert not durable.artifacts and not durable._tool_lock.locked()
    finally:
        backend.proceed.set()
        first.cancel()
        if second is not None:
            second.cancel()
        await asyncio.gather(first, *([] if second is None else [second]), return_exceptions=True)


async def test_cancel_at_lock_grant_releases_without_starting_a_transaction() -> None:
    durable = _durability()
    backend = OrderedHarness(context_for(durable))
    backend.proceed.set()
    token = CancellationToken()

    class CancelAtGrantLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            acquired = await super().acquire()
            asyncio.get_running_loop().call_soon(token.cancel)
            return acquired

    durable._tool_lock = CancelAtGrantLock()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(writer(durable, backend).run_json(
            arguments(0), token, call_id="cancel-at-grant"), timeout=2)
    assert token.is_cancelled() and not durable._tool_lock.locked()
    assert not backend.calls and not durable.tool_entries and not durable.artifacts


async def test_completed_ordered_transactions_replay_from_fresh_journal_without_io() -> None:
    durable = _durability()
    backend = OrderedHarness(context_for(durable))
    backend.proceed.set()
    for index in range(3):
        await writer(durable, backend).run_json(arguments(index), CancellationToken(),
                                               call_id=f"original-{index}")
    before = [dict(row) for row in durable.tool_entries]
    restored = restored_journal(durable)
    for index in range(3):
        await writer(restored, backend).run_json(arguments(index), CancellationToken(),
                                                call_id=f"restored-{index}")
    assert len(backend.calls) == 3 and restored._tool_cursor == 3
    assert restored.tool_entries == before and not restored._tool_lock.locked()


@pytest.mark.parametrize("replay_safe", [False, True])
async def test_unknown_ordered_transaction_does_not_replay_from_fresh_journal(
    replay_safe: bool,
) -> None:
    durable = _durability()
    backend = OrderedHarness(context_for(durable), unknown_first=True)
    backend.proceed.set()
    with pytest.raises(RuntimeExecutionError, match="uncertain"):
        await writer(durable, backend, replay_safe=replay_safe).run_json(
            arguments(0), CancellationToken(), call_id="original-unknown")
    restored = restored_journal(durable)
    with pytest.raises(RuntimeExecutionError):
        await writer(restored, backend, replay_safe=replay_safe).run_json(
            arguments(0), CancellationToken(), call_id="restored-unknown")
    assert len(backend.calls) == 1 and restored._tool_cursor == 0
    assert restored.tool_entries[0]["status"] == "running" and not restored.artifacts
    assert not restored._tool_lock.locked()
