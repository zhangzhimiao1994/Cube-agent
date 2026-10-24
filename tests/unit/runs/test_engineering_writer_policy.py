"""Only fresh generated Discuss executions opt into writer scheduling changes."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.autogen.adapter import _AutoGenCleanupIncomplete
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import (
    ContextRecordingRuntime,
    ExecutableFakeRepository,
    FakeRunRow,
    FakeTransaction,
)


@pytest.mark.parametrize("mode,generated,expected", [
    (TaskMode.DISCUSS, 1, True), (TaskMode.DISCUSS, True, False),
    (TaskMode.DISCUSS, None, False), (TaskMode.DISPATCH, 1, False),
])
async def test_fresh_generated_discuss_pins_continuation_without_changing_other_modes(
    mode: TaskMode, generated: int | None, expected: bool,
) -> None:
    route: dict[str, object] = {"source": "manual"}
    if generated is not None:
        route["generated_project_contract_version"] = generated
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(cast(RunRepository, repository),
                         runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=cast(TaskQueue, object()))
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    actual = runtime.contexts[0].routing_decision
    assert (actual.get("engineering_writer_continuation_version") == 1) is expected
    # All fresh Discuss engineering calls now need ACK before a correction can
    # enter the provider; writer-continuation remains generated-project only.
    assert (actual.get("engineering_checkpoint_ack_version") == 1) is (mode is TaskMode.DISCUSS)
    assert (actual.get("engineering_progress_budget_version") == 1) is (mode is TaskMode.DISCUSS)
    assert repository.row.routing_decision is not None
    assert (repository.row.routing_decision.get("engineering_writer_continuation_version") == 1) is expected


async def test_incomplete_discussion_cleanup_preserves_worker_execution_fence() -> None:
    class FailedCleanupRuntime(ContextRecordingRuntime):
        mode = TaskMode.DISCUSS

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            yield RunEvent(kind="runtime.cleanup_probe", sequence=1, run_id=context.run_id,
                           payload={"phase": "cleanup"})
            raise _AutoGenCleanupIncomplete("AutoGen runtime cleanup incomplete")

    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    repository.row.mode = TaskMode.DISCUSS.value
    service = RunService(cast(RunRepository, repository),
                         runtime_registry=RuntimeRegistry((FailedCleanupRuntime(),)),
                         router=None, task_queue=cast(TaskQueue, object()))
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.FAILED
    assert repository.row.worker_id is not None
    assert repository.row.worker_lease_token is not None
    assert repository.row.worker_lease_expires_at is not None
    assert repository.row.worker_heartbeat_at is None


class _CheckpointRepository(ExecutableFakeRepository):
    async def claim_for_execution(
        self,
        session: FakeTransaction,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
        worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
        worker_lease_expires_at: datetime | None = None,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        claimed = await super().claim_for_execution(
            session, run_id, allow_running_recovery=allow_running_recovery,
            worker_id=worker_id, worker_lease_token=worker_lease_token,
            worker_lease_expires_at=worker_lease_expires_at,
        )
        assert not isinstance(claimed, RunRecord)
        row, _ = claimed
        assert row.mode is not None
        return row, RuntimeCheckpoint(
            id=uuid4(), runtime_type="test", runtime_version="1",
            run_id=run_id, tenant_id=row.tenant_id, mode=TaskMode(row.mode), state={},
        )


async def _execute_policy(
    repository: ExecutableFakeRepository, *, allow_running_recovery: bool = False,
) -> TaskContext:
    assert repository.row.mode is not None
    runtime = ContextRecordingRuntime()
    runtime.mode = TaskMode(repository.row.mode)
    service = RunService(
        cast(RunRepository, repository), runtime_registry=RuntimeRegistry((runtime,)),
        router=None, task_queue=cast(TaskQueue, object()),
    )
    submitted = await service.execute(
        repository.run_id, allow_running_recovery=allow_running_recovery,
    )
    assert submitted.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    return runtime.contexts[0]


@pytest.mark.parametrize("generated", [None, 1])
async def test_fresh_discuss_project_soft_budget_pins_soft_admission(generated: int | None) -> None:
    route: dict[str, object] = {
        "source": "manual", "project_scale": "small", "project_delivery": "workspace",
        "runtime_timeout_source": "project_scale_soft_budget",
        "runtime_timeout_seconds": 300.0, "runtime_timeout_absolute_seconds": 3600.0,
    }
    if generated is not None:
        route["generated_project_contract_version"] = generated
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.mode = TaskMode.DISCUSS.value

    context = await _execute_policy(repository)

    assert repository.row.routing_decision is not None
    for decision in (repository.row.routing_decision, context.routing_decision):
        assert type(decision.get("engineering_soft_admission_version")) is int
        assert decision["engineering_soft_admission_version"] == 1
        assert decision["runtime_timeout_seconds"] == 300.0
        assert decision["runtime_timeout_absolute_seconds"] == 3600.0


@pytest.mark.parametrize("mode,source,generated", [
    (TaskMode.DISCUSS, "operator", 1),
    (TaskMode.DISCUSS, "user", 1),
    (TaskMode.DISCUSS, None, None),
    (TaskMode.DISCUSS, None, 1),
    (TaskMode.DIRECT, "project_scale_soft_budget", 1),
    (TaskMode.DISPATCH, "project_scale_soft_budget", 1),
    (TaskMode.HYBRID, "project_scale_soft_budget", 1),
])
async def test_soft_admission_excludes_explicit_timeout_chat_and_other_modes(
    mode: TaskMode, source: str | None, generated: int | None,
) -> None:
    route: dict[str, object] = {"source": "manual"}
    if source is not None:
        route.update({
            "project_scale": "small", "project_delivery": "workspace",
            "runtime_timeout_source": source, "runtime_timeout_seconds": 300.0,
        })
    if generated is not None:
        route["generated_project_contract_version"] = generated
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.mode = mode.value

    context = await _execute_policy(repository)

    assert "engineering_soft_admission_version" not in context.routing_decision
    assert repository.row.routing_decision is not None
    assert "engineering_soft_admission_version" not in repository.row.routing_decision


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("version", [None, True, "1", 2, 1])
@pytest.mark.parametrize("mode", [
    TaskMode.DISCUSS, TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.HYBRID,
])
async def test_soft_admission_preserves_historical_version_and_type(
    checkpoint: bool, version: object, mode: TaskMode,
) -> None:
    repository_type = _CheckpointRepository if checkpoint else ExecutableFakeRepository
    repository = repository_type(routing_decision={
        "source": "manual", "runtime_timeout_source": "project_scale_soft_budget",
        "engineering_soft_admission_version": version,
    })
    repository.row.mode = mode.value

    context = await _execute_policy(repository)

    assert repository.row.routing_decision is not None
    for decision in (repository.row.routing_decision, context.routing_decision):
        actual = decision["engineering_soft_admission_version"]
        assert type(actual) is type(version)
        assert actual == version


@pytest.mark.parametrize("checkpoint,recovery", [
    (True, False), (True, True), (False, True), (False, False),
])
async def test_existing_run_without_soft_admission_is_not_upgraded(
    checkpoint: bool, recovery: bool,
) -> None:
    repository_type = _CheckpointRepository if checkpoint else ExecutableFakeRepository
    repository = repository_type(routing_decision={
        "source": "manual", "runtime_timeout_source": "project_scale_soft_budget",
        "engineering_action_version": 1,
    })
    repository.row.mode = TaskMode.DISCUSS.value
    repository.row.status = RunStatus.RUNNING.value if recovery else RunStatus.RETRYING.value

    context = await _execute_policy(repository, allow_running_recovery=recovery)

    assert "engineering_soft_admission_version" not in context.routing_decision
    assert repository.row.routing_decision is not None
    assert "engineering_soft_admission_version" not in repository.row.routing_decision
