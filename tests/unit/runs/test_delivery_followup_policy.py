"""Final-response guidance is pinned at fresh claim, not retrofitted on recovery."""

from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import RuntimeCheckpoint
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import (
    ContextRecordingRuntime,
    ExecutableFakeRepository,
    FakeRunRow,
    FakeTransaction,
)


@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
async def test_fresh_execution_pins_followup_guidance(mode: TaskMode) -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(cast(RunRepository, repository),
                         runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=cast(TaskQueue, object()))
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.COMPLETED
    assert runtime.contexts[0].routing_decision.get("delivery_followup_version") == 2
    assert repository.row.routing_decision is not None
    assert repository.row.routing_decision.get("delivery_followup_version") == 2
    assert (runtime.contexts[0].routing_decision.get("engineering_direct_report_version") == 1) is (
        mode is TaskMode.DIRECT
    )
    assert (runtime.contexts[0].routing_decision.get("engineering_dispatch_report_version") == 1) is (
        mode in (TaskMode.DISPATCH, TaskMode.HYBRID)
    )
    assert (runtime.contexts[0].routing_decision.get("engineering_hybrid_report_version") == 1) is (
        mode is TaskMode.HYBRID
    )
    assert (runtime.contexts[0].routing_decision.get("engineering_final_report_version") == 1) is (
        mode is TaskMode.DISCUSS
    )
    assert (runtime.contexts[0].routing_decision.get("engineering_tool_correction_version") == 1) is (
        mode is TaskMode.DISCUSS
    )
    assert (runtime.contexts[0].routing_decision.get("engineering_checkpoint_ack_version") == 1) is (
        mode is TaskMode.DISCUSS
    )


@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
@pytest.mark.parametrize("version", [None, 1, 2])
async def test_recovery_keeps_existing_followup_policy(
    mode: TaskMode, version: int | None,
) -> None:
    class CheckpointRepository(ExecutableFakeRepository):
        async def claim_for_execution(
            self,
            session: FakeTransaction,
            run_id: UUID,
            *,
            allow_running_recovery: bool,
            worker_id: str | None = None,
            worker_lease_token: UUID | None = None,
            worker_lease_expires_at: datetime | None = None,
        ) -> tuple[FakeRunRow, RuntimeCheckpoint]:
            claimed = await super().claim_for_execution(
                session, run_id,
                allow_running_recovery=allow_running_recovery,
                worker_id=worker_id,
                worker_lease_token=worker_lease_token,
                worker_lease_expires_at=worker_lease_expires_at,
            )
            assert not isinstance(claimed, RunRecord)
            row, _ = claimed
            return row, RuntimeCheckpoint(
                id=uuid4(), run_id=run_id, tenant_id=row.tenant_id, mode=mode,
                runtime_type="test_dispatch", runtime_version="1.0", state={"complete": False},
            )

    route: dict[str, object] = {"source": "manual"}
    if version is not None:
        route["delivery_followup_version"] = version
        route["engineering_tool_correction_version"] = version
    repository = CheckpointRepository(routing_decision=route)
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(cast(RunRepository, repository),
                         runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=cast(TaskQueue, object()))
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.COMPLETED
    assert runtime.contexts[0].routing_decision.get("delivery_followup_version") == version
    assert "engineering_final_report_version" not in runtime.contexts[0].routing_decision
    assert "engineering_direct_report_version" not in runtime.contexts[0].routing_decision
    assert "engineering_dispatch_report_version" not in runtime.contexts[0].routing_decision
    assert "engineering_hybrid_report_version" not in runtime.contexts[0].routing_decision
    assert runtime.contexts[0].routing_decision.get("engineering_tool_correction_version") == version
    assert "engineering_checkpoint_ack_version" not in runtime.contexts[0].routing_decision
