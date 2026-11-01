from uuid import uuid4

import pytest

from agent_hub.api.routers.runs import SubmittedRunResponse
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import PermissionMode
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.capabilities.permission_modes_typed_fixture import (
    PermissionSubmissionRepository,
    present,
    submit_untrusted_permissions,
)
from tests.unit.runs.test_conversation_mode import RecordingQueue


@pytest.mark.parametrize("mode", ["default", "auto_review", "read_only", None])
async def test_submission_persists_and_projects_permission_mode(
    mode: PermissionMode | None,
) -> None:
    repository = PermissionSubmissionRepository(None)
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    )
    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="inspect notes",
        mode=TaskMode.DIRECT,
        permission_mode=mode,
        channel_context={"permission_mode": "auto_review"},
    )
    route = present(present(repository.record).routing_decision)
    if mode is None:
        assert "permission_mode" not in route
    else:
        assert route["permission_mode"] == mode
    assert SubmittedRunResponse.from_submitted(submitted).permission_mode == mode
    if mode == "read_only":
        assert route["sandbox_profile"] == "read_only"
        assert route["requested_permissions"] == ["workspace.read"]


@pytest.mark.parametrize(
    "mode,profile,permissions",
    [
        ("full_access", "none", ()),
        ("read_only", "workspace_write", ()),
        ("read_only", "read_only", ("command.run",)),
    ],
)
async def test_service_rejects_invalid_modes_before_creating_run(
    mode: str,
    profile: str,
    permissions: tuple[str, ...],
) -> None:
    repository = PermissionSubmissionRepository(None)
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    )
    with pytest.raises(ValueError):
        await submit_untrusted_permissions(
            service,
            tenant_id=uuid4(),
            actor_id=uuid4(),
            message="inspect notes",
            mode=TaskMode.DIRECT,
            permission_mode=mode,
            sandbox_profile=profile,
            requested_permissions=permissions,
        )
    assert repository.created == []
