import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.engineering_admission import (
    EngineeringAdmissionRuntime,
    with_engineering_admission,
)
from tests.unit.runtime.test_engineering_admission import Authorizer, Child, context


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISCUSS, TaskMode.DISPATCH, TaskMode.HYBRID))
@pytest.mark.parametrize("permission_mode", ("default", "auto_review"))
async def test_selected_permissions_do_not_bypass_legacy_admission(
    mode: TaskMode, permission_mode: str,
) -> None:
    task = context(mode)
    task = task.model_copy(update={"routing_decision": {
        **task.routing_decision, "permission_mode": permission_mode,
    }})
    child, authorizer = Child(mode), Authorizer()
    runtime = with_engineering_admission(child, authorizer, task)
    events = [event async for event in runtime.run(task)]
    assert isinstance(runtime, EngineeringAdmissionRuntime)
    assert child.calls == [] and len(authorizer.calls) == 1
    assert len(events) == 1 and events[0].checkpoint is not None
    assert events[0].checkpoint.runtime_type == "engineering_admission"


async def test_old_pending_admission_checkpoint_is_not_silently_skipped() -> None:
    task = context(TaskMode.DIRECT)
    child, authorizer = Child(task.mode), Authorizer()
    old = EngineeringAdmissionRuntime(child, authorizer)
    events = [event async for event in old.run(task)]
    task = task.model_copy(update={"checkpoint": events[0].checkpoint, "routing_decision": {
        **task.routing_decision, "permission_mode": "default",
    }})
    assert isinstance(with_engineering_admission(child, authorizer, task), EngineeringAdmissionRuntime)
