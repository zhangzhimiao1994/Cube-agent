"""Fresh Action rollout is restricted to already supported worker execution scopes."""
from __future__ import annotations

import pytest

from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.direct import DirectRuntime
from tests.unit.runtime.test_action_direct_assembly import checkpoint, configured, stack, task


def supported() -> dict[str, JsonValue]:
    return {'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
            'permission_mode': 'default', 'requested_permissions': (
                'workspace.read', 'workspace.write', 'command.run', 'network.read')}


@pytest.mark.parametrize('override', [
    {'execution_backend': 'docker'}, {'execution_backend': 'ssh'},
    {'permission_mode': 'full_access'}, {'permission_mode': 'read_only'},
    {'sandbox_profile': 'read_only'}, {'sandbox_profile': 'none'},
    {'sandbox_profile': 'restricted'},
    {'requested_permissions': ('workspace.read', 'workspace.write')},
    {'requested_permissions': ('command.run',)},
    {'requested_permissions': None},
])
async def test_fresh_unsupported_scope_preserves_legacy_runtime(override: dict[str, JsonValue]) -> None:
    child = await configured(stack())._runtime_for(task(**{**supported(), **override}))
    assert type(child) is DirectRuntime


@pytest.mark.parametrize('permission_mode', ['default', 'auto_review'])
async def test_supported_fresh_scope_uses_action_runtime(permission_mode: str) -> None:
    child = await configured(stack())._runtime_for(task(**{**supported(), 'permission_mode': permission_mode}))
    assert type(child) is ActionApprovedDirectRuntime


async def test_implicit_default_backend_and_permission_mode_remain_supported() -> None:
    route = supported()
    del route['execution_backend'], route['permission_mode']
    child = await configured(stack())._runtime_for(task(**route))
    assert type(child) is ActionApprovedDirectRuntime


async def test_incompatible_scope_never_downgrades_existing_action_checkpoint() -> None:
    context = task(**{**supported(), 'execution_backend': 'docker', 'permission_mode': 'full_access'})
    context = context.model_copy(update={'checkpoint': checkpoint(context)})
    assert type(await configured(stack())._runtime_for(context)) is ActionApprovedDirectRuntime
