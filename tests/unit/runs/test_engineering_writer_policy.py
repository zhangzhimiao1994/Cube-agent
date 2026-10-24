"""Only fresh generated Discuss executions opt into writer scheduling changes."""

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import RunService
from agent_hub.runtime.autogen.adapter import _AutoGenCleanupIncomplete
from agent_hub.runtime.contracts import RunEvent
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ContextRecordingRuntime, ExecutableFakeRepository


@pytest.mark.parametrize("mode,generated,expected", [
    (TaskMode.DISCUSS, 1, True), (TaskMode.DISCUSS, True, False),
    (TaskMode.DISCUSS, None, False), (TaskMode.DISPATCH, 1, False),
])
async def test_fresh_generated_discuss_pins_continuation_without_changing_other_modes(mode, generated, expected):
    route = {"source": "manual"}
    if generated is not None:
        route["generated_project_contract_version"] = generated
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)),
                         router=None, task_queue=object())
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    actual = runtime.contexts[0].routing_decision
    assert (actual.get("engineering_writer_continuation_version") == 1) is expected
    # All fresh Discuss engineering calls now need ACK before a correction can
    # enter the provider; writer-continuation remains generated-project only.
    assert (actual.get("engineering_checkpoint_ack_version") == 1) is (mode is TaskMode.DISCUSS)
    assert (repository.row.routing_decision.get("engineering_writer_continuation_version") == 1) is expected


async def test_incomplete_discussion_cleanup_preserves_worker_execution_fence():
    class FailedCleanupRuntime(ContextRecordingRuntime):
        mode = TaskMode.DISCUSS

        async def run(self, context):
            yield RunEvent(kind="runtime.cleanup_probe", sequence=1, run_id=context.run_id,
                           payload={"phase": "cleanup"})
            raise _AutoGenCleanupIncomplete("AutoGen runtime cleanup incomplete")

    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    repository.row.mode = TaskMode.DISCUSS.value
    service = RunService(repository, runtime_registry=RuntimeRegistry((FailedCleanupRuntime(),)),
                         router=None, task_queue=object())
    submitted = await service.execute(repository.run_id)
    assert submitted.status is RunStatus.FAILED
    assert repository.row.worker_id is not None
    assert repository.row.worker_lease_token is not None
    assert repository.row.worker_lease_expires_at is not None
    assert repository.row.worker_heartbeat_at is None
