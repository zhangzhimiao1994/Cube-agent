"""Real service lifecycle with in-memory persistence; no PostgreSQL lock proof."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import (
    ExecutionAuthorityError,
    get_verification_authority,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.project_verification_receipts import VerificationAuthority
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.self_repair import SelfRepairPolicy
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import (
    ExecutableFakeRepository,
    FakeRunRow,
    FakeTransaction,
    RuntimeCompletes,
)

MODES = (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID)


def authority(context: TaskContext) -> VerificationAuthority:
    assert context.actor_id is not None and context.actor_role is not None
    return get_verification_authority(
        tenant_id=context.tenant_id, run_id=context.run_id,
        user_id=context.actor_id, role=context.actor_role,
    )


def assert_unbound(context: TaskContext) -> None:
    with pytest.raises(ExecutionAuthorityError):
        get_verification_authority(
            tenant_id=context.tenant_id, run_id=context.run_id,
            user_id=context.actor_id or uuid4(), role=context.actor_role or Role.OPERATOR,
        )


def repository(mode: TaskMode = TaskMode.DIRECT) -> ExecutableFakeRepository:
    result = ExecutableFakeRepository(routing_decision={
        "project_id": "project-a", "workspace_session_id": "session-a",
        "sandbox_profile": "workspace_write", "execution_backend": "systemd",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
    })
    result.row.mode = mode.value
    result.row.actor_role = Role.OPERATOR.value
    return result


class ProbeRuntime(RuntimeCompletes):
    def __init__(
        self, mode: TaskMode, *, expected_binding: bool = True, ending: str = "complete",
        visit: Callable[[TaskContext], Awaitable[None]] | None = None,
        hold_close: bool = False,
    ) -> None:
        self.mode = mode
        self.expected_binding = expected_binding
        self.ending = ending
        self.visit = visit
        self.hold_close = hold_close
        self.context: TaskContext | None = None
        self.observed: list[VerificationAuthority] = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.closing = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_child_authority: VerificationAuthority | None = None
        self.release = asyncio.Event()
        self.late_release = asyncio.Event()
        self.late_child: asyncio.Task[None] | None = None
        self.cancel_calls = 0

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.context = context
        try:
            if self.expected_binding:
                self.observed.append(authority(context))

                async def live_child() -> VerificationAuthority:
                    await asyncio.sleep(0)
                    return authority(context)

                self.observed.append(await asyncio.create_task(live_child()))

                async def late_child() -> None:
                    await self.late_release.wait()
                    assert_unbound(context)

                self.late_child = asyncio.create_task(late_child())
            else:
                assert_unbound(context)
            if self.visit is not None:
                await self.visit(context)
            self.started.set()
            if self.ending == "raise":
                raise RuntimeError("controlled runtime failure")
            if self.ending == "cancel":
                raise asyncio.CancelledError
            if self.ending == "block":
                await self.release.wait()
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)
        finally:
            if self.expected_binding:
                self.observed.append(authority(context))

                async def close_child() -> VerificationAuthority:
                    await asyncio.sleep(0)
                    return authority(context)

                self.close_child_authority = await asyncio.create_task(close_child())
            else:
                assert_unbound(context)
            self.closing.set()
            if self.hold_close:
                await self.close_release.wait()
            self.closed.set()
            if self.ending == "close_raise":
                raise RuntimeError("controlled stream close failure")

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.release.set()


def service(
    repo: ExecutableFakeRepository, runtime: ProbeRuntime, *,
    project_input_preparer: Callable[[TaskContext], Awaitable[TaskContext]] | None = None,
) -> RunService:
    return RunService(
        cast(RunRepository, repo), runtime_registry=RuntimeRegistry((runtime,)),
        router=None, task_queue=cast(TaskQueue, object()), worker_id="trusted-worker",
        self_repair_policy=SelfRepairPolicy(enabled=False),
        project_input_preparer=project_input_preparer,
    )


async def finish_child(runtime: ProbeRuntime) -> None:
    runtime.late_release.set()
    if runtime.late_child is not None:
        await runtime.late_child


@pytest.mark.parametrize("mode", MODES)
async def test_execute_binds_claim_identity_through_stream_close(mode: TaskMode) -> None:
    repo = repository(mode)
    runtime = ProbeRuntime(mode)
    try:
        result = await service(repo, runtime).execute(repo.run_id)
        assert result.status is RunStatus.COMPLETED
        assert runtime.context is not None and runtime.closed.is_set()
        assert len(runtime.observed) == 3
        assert runtime.close_child_authority == runtime.observed[0]
        for observed in runtime.observed:
            assert observed.execution_token == runtime.context.execution_token
            assert observed.worker_id == "trusted-worker"
            assert observed.run_id == repo.run_id and observed.tenant_id == repo.row.tenant_id
            assert observed.actor_id == repo.row.actor_id
            assert observed.actor_role is Role.OPERATOR and observed.mode is mode
            assert observed.actor == "main_agent"
        assert_unbound(runtime.context)
    finally:
        await finish_child(runtime)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("legacy", ["missing_actor", "missing_role", "viewer"])
async def test_execute_legacy_streams_continue_without_authority(mode: TaskMode, legacy: str) -> None:
    repo = repository(mode)
    if legacy == "missing_actor":
        repo.row.actor_id = None
    else:
        repo.row.actor_role = None if legacy == "missing_role" else Role.VIEWER.value
    runtime = ProbeRuntime(mode, expected_binding=False)
    result = await service(repo, runtime).execute(repo.run_id)
    assert result.status is RunStatus.COMPLETED
    assert runtime.closed.is_set() and runtime.observed == []


@pytest.mark.parametrize("permission_mode", ["readonly", "unknown"])
async def test_execute_continues_when_permission_mode_denies_verification(
    permission_mode: str,
) -> None:
    repo = repository()
    repo.row.routing_decision = {**(repo.row.routing_decision or {}), "permission_mode": permission_mode}
    runtime = ProbeRuntime(TaskMode.DIRECT, expected_binding=False)
    result = await service(repo, runtime).execute(repo.run_id)
    assert result.status is RunStatus.COMPLETED
    assert runtime.closed.is_set() and runtime.observed == []


@pytest.mark.parametrize("ending", ["raise", "cancel", "close_raise"])
async def test_execute_revokes_after_runtime_exception_or_cancellation(ending: str) -> None:
    repo = repository()
    runtime = ProbeRuntime(TaskMode.DIRECT, ending=ending)
    try:
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await service(repo, runtime).execute(repo.run_id)
        else:
            result = await service(repo, runtime).execute(repo.run_id)
            assert result.status is RunStatus.FAILED
        assert runtime.closed.is_set() and len(runtime.observed) == 3
        assert runtime.context is not None
        assert_unbound(runtime.context)
    finally:
        await finish_child(runtime)


async def test_execute_task_cancellation_closes_stream_before_revocation() -> None:
    repo = repository()
    runtime = ProbeRuntime(TaskMode.DIRECT, ending="block")
    task = asyncio.create_task(service(repo, runtime).execute(repo.run_id))
    try:
        async with asyncio.timeout(2):
            await runtime.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert runtime.closed.is_set() and len(runtime.observed) == 3
        assert runtime.context is not None
        assert_unbound(runtime.context)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await finish_child(runtime)


async def test_execute_keeps_binding_until_async_stream_close_finishes() -> None:
    repo = repository()
    runtime = ProbeRuntime(TaskMode.DIRECT, hold_close=True)
    task = asyncio.create_task(service(repo, runtime).execute(repo.run_id))
    try:
        async with asyncio.timeout(2):
            await runtime.closing.wait()
            assert not task.done() and not runtime.closed.is_set()
            assert runtime.close_child_authority == runtime.observed[0]
            runtime.close_release.set()
            assert (await task).status is RunStatus.COMPLETED
        assert runtime.closed.is_set() and runtime.context is not None
        assert_unbound(runtime.context)
    finally:
        runtime.close_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await finish_child(runtime)


async def test_execute_binds_before_runtime_stream_factory_runs() -> None:
    class FactoryProbeRuntime(ProbeRuntime):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            assert authority(context).worker_id == "trusted-worker"
            return super().run(context)

    repo = repository()
    runtime = FactoryProbeRuntime(TaskMode.DIRECT)
    try:
        assert (await service(repo, runtime).execute(repo.run_id)).status is RunStatus.COMPLETED
    finally:
        await finish_child(runtime)


async def test_execute_parallel_runs_keep_independent_bindings() -> None:
    first_repo, second_repo = repository(), repository(TaskMode.DISPATCH)
    second_repo.row.actor_id = uuid4()
    first, second = ProbeRuntime(TaskMode.DIRECT, ending="block"), ProbeRuntime(
        TaskMode.DISPATCH, ending="block",
    )
    tasks = [asyncio.create_task(service(repo, runtime).execute(repo.run_id))
             for repo, runtime in ((first_repo, first), (second_repo, second))]
    try:
        async with asyncio.timeout(2):
            await first.started.wait()
            await second.started.wait()
            first.release.set()
            assert (await tasks[0]).status is RunStatus.COMPLETED
            await finish_child(first)
            assert not second.closed.is_set()
            second.release.set()
            assert (await tasks[1]).status is RunStatus.COMPLETED
        assert first.observed[0].execution_token != second.observed[0].execution_token
        assert first.observed[0].run_id != second.observed[0].run_id
        assert first.observed[0].actor_id != second.observed[0].actor_id
        for runtime in (first, second):
            assert all(item == runtime.observed[0] for item in runtime.observed)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await finish_child(first)
        await finish_child(second)


async def test_adaptive_stream_without_worker_id_acquires_no_authority() -> None:
    from agent_hub.runs.service import _adaptive_runtime_events

    repo = repository()
    context = TaskContext(
        tenant_id=repo.row.tenant_id, run_id=repo.run_id, actor_id=repo.row.actor_id,
        actor_role=Role.OPERATOR, execution_token=uuid4(), mode=TaskMode.DIRECT,
        request=repo.row.request, routing_decision=cast(dict[str, JsonValue], repo.row.routing_decision),
    )
    runtime = ProbeRuntime(TaskMode.DIRECT, expected_binding=False)
    events = [event async for event in _adaptive_runtime_events(
        runtime, context, configured_tokens=1000, routing_decision=context.routing_decision,
        initial_progress_units=0,
    )]
    assert [event.kind for event in events] == [EventKind.RUNTIME_COMPLETED]
    assert runtime.closed.is_set()
    assert_unbound(context)


async def test_execute_never_adopts_token_from_newer_database_record() -> None:
    new_token = uuid4()
    claimed_tokens: list[UUID] = []

    class ReplacedLeaseRepository(ExecutableFakeRepository):
        async def claim_for_execution(
            self, session: FakeTransaction, run_id: UUID, *, allow_running_recovery: bool,
            worker_id: str | None = None, worker_lease_token: UUID | None = None,
            worker_lease_expires_at: datetime | None = None,
        ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
            result = await super().claim_for_execution(
                session, run_id, allow_running_recovery=allow_running_recovery,
                worker_id=worker_id, worker_lease_token=worker_lease_token,
                worker_lease_expires_at=worker_lease_expires_at,
            )
            assert worker_lease_token is not None
            claimed_tokens.append(worker_lease_token)
            self.row.worker_id = "new-db-worker"
            self.row.worker_lease_token = new_token
            return result

    repo = ReplacedLeaseRepository(routing_decision=repository().row.routing_decision or {})
    repo.row.mode = TaskMode.DIRECT.value
    repo.row.actor_role = Role.OPERATOR.value

    async def inspect(context: TaskContext) -> None:
        current = await repo.get(context.tenant_id, context.run_id)
        assert context.actor_id is not None and context.actor_role is not None
        observed = get_verification_authority(
            tenant_id=context.tenant_id, run_id=context.run_id,
            user_id=context.actor_id, role=context.actor_role, current_record=current,
        )
        assert observed.worker_id == "trusted-worker"
        assert observed.execution_token == context.execution_token == claimed_tokens[0] != new_token
        # Restore the fake row only so this test can finish without simulating takeover recovery.
        repo.row.worker_id = "trusted-worker"
        repo.row.worker_lease_token = claimed_tokens[0]

    runtime = ProbeRuntime(TaskMode.DIRECT, visit=inspect)
    try:
        assert (await service(repo, runtime).execute(repo.run_id)).status is RunStatus.COMPLETED
    finally:
        await finish_child(runtime)


async def test_execute_heartbeat_cancellation_preserves_binding_until_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository()
    runtime = ProbeRuntime(TaskMode.DIRECT, ending="block")
    instance = service(repo, runtime)
    monkeypatch.setattr(instance, "_worker_lease_heartbeat_interval_seconds", lambda: 0.001)
    task = asyncio.create_task(instance.execute(repo.run_id))
    try:
        async with asyncio.timeout(2):
            await runtime.started.wait()
            repo.row.status = RunStatus.CANCELLED.value
            assert (await task).status is RunStatus.CANCELLED
        assert runtime.cancel_calls >= 1 and runtime.closed.is_set()
        assert runtime.context is not None and len(runtime.observed) == 3
        assert_unbound(runtime.context)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await finish_child(runtime)


async def test_execute_approval_drain_closes_bound_stream_after_lease_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
    from tests.unit.runs.test_approval_checkpoint_service import (
        APPROVAL_ID,
        ApprovalRepository,
        crew_checkpoint,
    )

    repo = ApprovalRepository()
    repo.row.routing_decision = repository().row.routing_decision
    repo.row.actor_role = Role.OPERATOR.value

    class ApprovalProbe(ProbeRuntime):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            self.context = context
            try:
                self.observed.append(authority(context))
                repo.begin_approval()
                await repo.failed_renewal.wait()
                assert repo.row.worker_lease_token is None
                self.observed.append(authority(context))
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=context.run_id,
                    checkpoint=crew_checkpoint(context.run_id),
                )
                pytest.fail("service consumed events after its approval checkpoint")
            finally:
                self.observed.append(authority(context))
                self.closed.set()

    runtime = ApprovalProbe(TaskMode.DISPATCH)
    instance = service(repo, runtime)
    monkeypatch.setattr(instance, "_worker_lease_heartbeat_interval_seconds", lambda: 0.001)
    async with asyncio.timeout(2):
        result = await instance.execute(repo.run_id)
    assert result.status is RunStatus.WAITING_APPROVAL
    assert checkpoint_waits_for_approval(repo.checkpoint, APPROVAL_ID)
    assert runtime.closed.is_set() and runtime.cancel_calls == 0
    assert len(runtime.observed) == 3
    assert all(item == runtime.observed[0] for item in runtime.observed)
    assert runtime.context is not None
    assert_unbound(runtime.context)


@pytest.mark.parametrize("mode", MODES)
async def test_execute_binds_context_only_after_original_preparation(mode: TaskMode) -> None:
    repo = repository(mode)
    repo.row.routing_decision = {
        **(repo.row.routing_decision or {}), "project_input_contract_version": 1,
        "project_input": {"schema_version": 1, "kind": "managed_workspace", "editable_paths": ["src"]},
    }
    prepared_contexts: list[TaskContext] = []

    async def prepare(context: TaskContext) -> TaskContext:
        assert_unbound(context)
        state: JsonValue = {"schema_version": 1, "phase": "observed"}
        repo.row.routing_decision = {
            **(repo.row.routing_decision or {}), "project_input_preparation": state,
        }
        prepared = context.model_copy(update={"routing_decision": {
            **context.routing_decision, "project_input_preparation": state,
        }})
        prepared_contexts.append(prepared)
        return prepared

    runtime = ProbeRuntime(mode)
    try:
        result = await service(repo, runtime, project_input_preparer=prepare).execute(repo.run_id)
        assert result.status is RunStatus.COMPLETED
        assert len(prepared_contexts) == 1 and runtime.context is not None
        observed = runtime.observed[0]
        expected = VerificationAuthority.from_context(
            prepared_contexts[0], worker_id="trusted-worker", actor="main_agent",
        )
        assert observed.scope_sha256 == expected.scope_sha256
        assert runtime.context.routing_decision["project_input_preparation"] == {
            "schema_version": 1, "phase": "observed",
        }
        assert_unbound(runtime.context)
    finally:
        await finish_child(runtime)
