"""Final-response guidance is pinned at fresh claim, not retrofitted on recovery."""

from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import RuntimeCheckpoint
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ContextRecordingRuntime, ExecutableFakeRepository


@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
async def test_fresh_execution_pins_followup_guidance(mode):
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=object())
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.COMPLETED
    assert runtime.contexts[0].routing_decision.get("delivery_followup_version") == 2
    assert repository.row.routing_decision.get("delivery_followup_version") == 2
    assert (runtime.contexts[0].routing_decision.get("engineering_final_report_version") == 1) is (
        mode is TaskMode.DISCUSS
    )


@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
@pytest.mark.parametrize("version", [None, 1, 2])
async def test_recovery_keeps_existing_followup_policy(mode, version):
    class CheckpointRepository(ExecutableFakeRepository):
        async def claim_for_execution(self, session, run_id, **kwargs):
            row, _ = await super().claim_for_execution(session, run_id, **kwargs)
            return row, RuntimeCheckpoint(
                id=uuid4(), run_id=run_id, tenant_id=row.tenant_id, mode=mode,
                runtime_type="test_dispatch", runtime_version="1.0", state={"complete": False},
            )

    route = {"source": "manual"}
    if version is not None:
        route["delivery_followup_version"] = version
    repository = CheckpointRepository(routing_decision=route)
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=object())
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.COMPLETED
    assert runtime.contexts[0].routing_decision.get("delivery_followup_version") == version
    assert "engineering_final_report_version" not in runtime.contexts[0].routing_decision
