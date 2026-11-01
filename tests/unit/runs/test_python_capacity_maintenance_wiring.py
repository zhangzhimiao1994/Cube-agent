"""Exercise real preparation/stack entry points with a finite cache-pressure executor."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities import defaults
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.runs.project_inputs import OriginalProjectPreparer
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerResponse
from tests.unit.runs.snapshot_approval_types_fixture import BoundMaintenanceHook
from tests.unit.runs.test_original_project_preparation import setup
from tests.unit.runs.test_python_original_preparation import configured


async def test_capacity_is_reclaimed_before_first_native_python_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, repository, context, executor, authorizer, *_ = await configured(tmp_path)
    authorizer.allowed = True
    available = False
    maintenance_calls = 0
    original = executor.run

    async def maintenance() -> int:
        nonlocal available, maintenance_calls
        assert repository.row.routing_decision['project_input_preparation']['phase'] == 'captured'
        assert not executor.calls and authorizer.calls == 1
        available = True
        maintenance_calls += 1
        return 1

    async def capacity_checked(invocation: Any) -> PythonProjectBrokerResponse:
        if not available:
            raise OSError('python_project_environment_quota')
        response: object = await original(invocation)
        assert isinstance(response, PythonProjectBrokerResponse)
        return response

    # Bootstrap supplies this private service callback, never request/model data.
    preparer.capacity_maintenance = maintenance
    monkeypatch.setattr(executor, 'run', capacity_checked)
    prepared = await preparer(context)
    assert maintenance_calls == 1 and len(executor.calls) == 1
    assert prepared.routing_decision['project_input_preparation']['phase'] == 'observed'
    await preparer(context.model_copy(update={'routing_decision': repository.row.routing_decision}))
    assert maintenance_calls == 1 and len(executor.calls) == 1


async def test_denied_action_does_not_enter_capacity_maintenance(tmp_path: Path) -> None:
    preparer, _, context, executor, _, *_ = await configured(tmp_path)
    calls = []

    async def maintenance() -> int:
        calls.append('unexpected')
        return 0

    preparer.capacity_maintenance = maintenance
    with pytest.raises(RuntimeError, match='authorization'):
        await preparer(context)
    assert not calls and not executor.calls


async def test_node_preparation_does_not_call_python_cache_maintenance(tmp_path: Path) -> None:
    preparer, _, context, observer, _, *_ = await setup(tmp_path)
    calls = []

    async def maintenance() -> int:
        calls.append('unexpected')
        return 0

    preparer.capacity_maintenance = maintenance
    await preparer(context)
    assert not calls and observer.count == 1


async def test_auxiliary_cleanup_failure_does_not_block_an_existing_usable_environment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    preparer, _, context, executor, authorizer, *_ = await configured(tmp_path)
    authorizer.allowed = True

    async def maintenance() -> int:
        raise RuntimeError('private-path-or-token-must-not-be-logged')

    preparer.capacity_maintenance = maintenance
    await preparer(context)
    assert len(executor.calls) == 1
    assert 'python_project_capacity_maintenance_failed' in caplog.text
    assert 'private-path-or-token' not in caplog.text


async def test_default_stack_binds_the_same_trusted_maintenance_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, repository, context, *_ = await configured(tmp_path)

    async def maintain_capacity() -> int:
        return 0

    hook = BoundMaintenanceHook(maintain_capacity)
    monkeypatch.setattr(hook, 'maintain_capacity', maintain_capacity)
    monkeypatch.setattr(defaults, 'build_python_project_terminal_hook', lambda *args: hook)
    monkeypatch.setattr(RuntimeCapabilityGateway, 'original_project_preparer',
                        lambda self, gateway: preparer)
    stack = defaults.build_runtime_capability_stack(tenant_id=context.tenant_id,
        run_repository=repository, skill_store_dir=tmp_path/'skills', workspace_root=None)
    assert stack.project_cache_cleanup_hook is hook
    assert isinstance(stack.project_input_preparer, OriginalProjectPreparer)
    assert stack.project_input_preparer is preparer
    assert preparer.capacity_maintenance is maintain_capacity


async def test_cancellation_during_maintenance_never_starts_original_execution(
    tmp_path: Path,
) -> None:
    preparer, repository, context, executor, authorizer, *_ = await configured(tmp_path)
    authorizer.allowed = True

    async def maintenance() -> int:
        raise asyncio.CancelledError

    preparer.capacity_maintenance = maintenance
    with pytest.raises(asyncio.CancelledError):
        await preparer(context)
    assert not executor.calls
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'captured'
