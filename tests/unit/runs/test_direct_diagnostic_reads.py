from __future__ import annotations

from typing import Any

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import RunService
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ContextRecordingRuntime, ExecutableFakeRepository


@pytest.mark.parametrize('marker', [1, None, True, '1', 2])
@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.HYBRID])
@pytest.mark.parametrize('recovery', [False, True])
async def test_only_fresh_direct_submission_selects_diagnostic_reads(
    marker: Any, mode: TaskMode, recovery: bool,
) -> None:
    route: dict[str, Any] = {
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
        'project_scale': 'small', 'sandbox_profile': 'workspace_write',
    }
    if marker is not None:
        route['engineering_diagnostic_reads_submission_version'] = marker
    repository = ExecutableFakeRepository(routing_decision=route)
    repository.row.mode = mode.value
    runtime = ContextRecordingRuntime()
    runtime.mode = mode
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
                         router=None, task_queue=object())  # type: ignore[arg-type]
    result = await service.execute(repository.run_id, allow_running_recovery=recovery)
    assert result.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    selected = runtime.contexts[0].routing_decision.get('engineering_diagnostic_reads_version')
    expected = mode is TaskMode.DIRECT and type(marker) is int and marker == 1 and not recovery
    assert selected == (1 if expected else None)
    assert repository.row.routing_decision is not None
    assert repository.row.routing_decision.get('engineering_diagnostic_reads_version') == selected


async def test_existing_direct_route_is_not_silently_upgraded() -> None:
    repository = ExecutableFakeRepository(routing_decision={
        'engineering_diagnostic_reads_submission_version': 1, 'engineering_action_version': 1,
    })
    repository.row.mode = TaskMode.DIRECT.value
    runtime = ContextRecordingRuntime()
    runtime.mode = TaskMode.DIRECT
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
                         router=None, task_queue=object())  # type: ignore[arg-type]
    await service.execute(repository.run_id)
    assert 'engineering_diagnostic_reads_version' not in runtime.contexts[0].routing_decision


async def test_previous_submission_marker_does_not_upgrade_old_queued_runs() -> None:
    repository = ExecutableFakeRepository(routing_decision={
        'engineering_diagnostic_submission_version': 1,
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
        'project_scale': 'small', 'sandbox_profile': 'workspace_write',
    })
    repository.row.mode = TaskMode.DIRECT.value
    runtime = ContextRecordingRuntime()
    runtime.mode = TaskMode.DIRECT
    service = RunService(repository, runtime_registry=RuntimeRegistry((runtime,)),  # type: ignore[arg-type]
                         router=None, task_queue=object())  # type: ignore[arg-type]
    await service.execute(repository.run_id)
    assert 'engineering_diagnostic_reads_version' not in runtime.contexts[0].routing_decision
