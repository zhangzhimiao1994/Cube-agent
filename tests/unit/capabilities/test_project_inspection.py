from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.capabilities.tools.registry import create_builtin_tool_registry
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.defaults import _role_allowed_tools
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.capabilities.test_scoped_read import (
    OTHER,
    RUN,
    TENANT,
    FakeRunRepository,
    project_path,
    stored_run,
    write_file,
)
from tests.unit.harness.test_tool_gateway import FakePolicyGateway


def backend(root: Path, repository: object | None = None) -> RuntimeCapabilityGateway:
    return RuntimeCapabilityGateway(
        skill_store_dir=root / 'skills', project_workspace_dir=root / 'projects',
        run_repository=repository or FakeRunRepository(stored_run(execution_backend='docker')),
    )


async def inspect(
    gateway: RuntimeCapabilityGateway, arguments: Mapping[str, JsonValue] | None = None,
) -> Mapping[str, JsonValue]:
    return await gateway.execute(
        tenant_id=TENANT, run_id=RUN, actor='engineer', name='project.inspect',
        arguments=arguments or {}, idempotency_key='inspect-project',
    )


def observations(result: Mapping[str, JsonValue]) -> dict[str, Mapping[str, JsonValue]]:
    return {
        str(item['path']): item
        for item in cast(tuple[Mapping[str, JsonValue], ...], result['manifests'])
    }


async def test_inspection_reads_real_scoped_manifests_without_running_scripts(tmp_path: Path) -> None:
    manifest = {
        'engines': {'node': '>=22', 'npm': '>=10'},
        'dependencies': {'express': '^5.0.0'},
        'devDependencies': {'typescript': '~5.8'},
        'scripts': {'build': 'echo SECRET-script-body', 'test': 'exit 1'},
    }
    package = json.dumps(manifest)
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', package)
    write_file(tmp_path / 'projects', f'{project_path()}/package-lock.json', '{"lockfileVersion":3}')
    result = await inspect(backend(tmp_path))
    assert result['scope'] == {
        'tenant_id': str(TENANT), 'run_id': str(RUN),
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
    }
    assert result['environment'] == {'selected_backend': 'docker', 'readiness': 'not_probed'}
    assert result['operations'] == {'build': 'declared', 'test': 'declared'}
    assert result['runtimes'] == (
        {'name': 'node', 'constraint': '>=22'}, {'name': 'npm', 'constraint': '>=10'},
    )
    assert result['dependencies'] == (
        {'ecosystem': 'npm', 'name': 'express', 'group': 'dependencies',
         'source_kind': 'registry', 'constraint': '^5.0.0'},
        {'ecosystem': 'npm', 'name': 'typescript', 'group': 'devDependencies',
         'source_kind': 'registry', 'constraint': '~5.8'},
    )
    assert result['install_plan_state'] == 'requires_environment_probe'
    assert 'SECRET' not in repr(result)
    assert observations(result)['package-lock.json']['status'] == 'observed'


async def test_python_requirements_are_declared_not_guessed_from_imports(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/pyproject.toml', '''
[project]
name = "sample"
requires-python = ">=3.12"
dependencies = ["pillow>=10", "requests[socks]>=2; python_version >= '3.12'"]
[build-system]
requires = ["hatchling>=1"]
build-backend = "hatchling.build"
''')
    write_file(tmp_path / 'projects', f'{project_path()}/main.py', 'import cv2\n')
    result = await inspect(backend(tmp_path))
    assert result['runtimes'] == ({'name': 'python', 'constraint': '>=3.12'},)
    names = [item['name'] for item in cast(tuple[Mapping[str, JsonValue], ...],
                                         result['dependencies'])]
    assert names == ['pillow', 'requests', 'hatchling']
    assert 'cv2' not in repr(result)
    assert result['operations'] == {'build': 'unknown', 'test': 'unknown'}


@pytest.mark.parametrize('arguments', [
    {'cwd': 'C:/secret'}, {'project_id': 'other'}, {'execution_backend': 'systemd'},
    {'operation': 'install'}, {'env': {'TOKEN': 'secret'}},
])
async def test_inspection_rejects_model_scope_or_execution_arguments(
    tmp_path: Path, arguments: dict[str, JsonValue],
) -> None:
    with pytest.raises(RuntimeCapabilityError, match='project.inspect takes no arguments'):
        await inspect(backend(tmp_path), arguments)


@pytest.mark.parametrize('record', [
    stored_run(tenant=OTHER), replace(stored_run(), id=OTHER),
    stored_run(sandbox_profile='none'), stored_run(requested_permissions=[]),
    stored_run(project_id='../escape'), stored_run(execution_backend={'bad': True}),
])
async def test_inspection_fails_closed_on_invalid_authoritative_scope(
    tmp_path: Path, record: object,
) -> None:
    class WrongRepository:
        async def get(self, tenant_id: object, run_id: object) -> object:
            return record

    with pytest.raises(RuntimeCapabilityError, match='project inspection scope unavailable'):
        await inspect(backend(tmp_path, WrongRepository()))


async def test_inspection_can_bootstrap_without_skill_or_target_runtime(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{"dependencies":{}}')
    gateway = backend(tmp_path)
    assert gateway.is_available(TENANT, 'project.inspect') is True
    assert gateway.is_replay_safe('project.inspect') is True
    result = await inspect(gateway)
    assert result['environment'] == {'selected_backend': 'docker', 'readiness': 'not_probed'}
    assert result['install_plan_state'] == 'requires_environment_probe'
    assert not (tmp_path / 'skills').exists()


async def test_inspection_handles_invalid_oversized_and_missing_manifests(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{not-json SECRET}')
    write_file(tmp_path / 'projects', f'{project_path()}/pyproject.toml', 'x' * 70_000)
    result = await inspect(backend(tmp_path))
    seen = observations(result)
    assert seen['package.json']['status'] == 'invalid'
    assert seen['pyproject.toml']['status'] == 'too_large'
    assert seen['pyproject.toml']['sha256'] is None
    assert seen['requirements.txt']['status'] == 'absent'
    assert result['install_plan_state'] == 'blocked'
    assert 'SECRET' not in repr(result)


@pytest.mark.parametrize('filename,content', [
    ('package.json', '{"dependencies":{"sample":"https://u:SECRET@example.com/x.tgz"}}'),
    ('requirements.txt', 'sample @ https://u:SECRET@example.com/a.whl\n'),
    ('requirements.txt', '--extra-index-url https://u:SECRET@example.com\n-r private.txt\n'),
    ('pyproject.toml', '[project]\ndynamic=["dependencies"]\n'),
    ('package.json', '{"dependencies":{"sample":"1"},"dependencies":{}}'),
])
async def test_unsafe_or_ambiguous_sources_block_plan_without_echoing_secrets(
    tmp_path: Path, filename: str, content: str,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/{filename}', content)
    result = await inspect(backend(tmp_path))
    assert result['install_plan_state'] == 'blocked'
    assert 'SECRET' not in repr(result)
    assert 'example.com' not in repr(result)


async def test_symlinks_hardlinks_and_special_files_do_not_leak_manifest(tmp_path: Path) -> None:
    secret = write_file(tmp_path, 'secret', '{"dependencies":{"secret-leak":"1"}}')
    package = write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{}')
    package.unlink()
    os.link(secret, package)
    result = await inspect(backend(tmp_path))
    assert observations(result)['package.json']['status'] == 'unavailable'
    assert result['install_plan_state'] == 'blocked'
    assert 'secret-leak' not in repr(result)


async def test_public_harness_path_inspects_frozen_arguments_and_file_read_policy(
    tmp_path: Path,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{"scripts":{}}')
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend(tmp_path), policy_gateway=policy)
    request = HarnessToolCallRequest(
        run_id=RUN, actor='engineer', approval_required=False,
        tool_name='project.inspect', arguments=MappingProxyType({}),
        sandbox='read_only', idempotency_key='inspect-through-harness',
    )
    result = await gateway.invoke(TENANT, request, user_id=OTHER, role=Role.OPERATOR)
    assert result.status == 'succeeded'
    assert result.payload['operations'] == {'build': 'absent', 'test': 'absent'}
    assert policy.requests[-1][0].capability == 'file'
    assert policy.requests[-1][0].operation == 'read'


def test_inspection_registry_and_missing_scope_availability(tmp_path: Path) -> None:
    assert 'project.inspect' in create_builtin_tool_registry().names()
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills')
    assert gateway.is_available(TENANT, 'project.inspect') is False
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / 'skills', project_workspace_dir=tmp_path,
    )
    assert gateway.is_available(TENANT, 'project.inspect') is False


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH,
                                 TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('configured', [True, False])
def test_shared_role_filter_exposes_inspection_only_with_scoped_reader(
    tmp_path: Path, mode: TaskMode, configured: bool,
) -> None:
    role = RoleAssignment(
        id='engineer', role='Engineer', purpose=RolePurpose.EXECUTE,
        mission='Build and verify project.', must_answer=('What passed?',),
        allowed_tools=('workspace.read',), forbidden_actions=('Do not exceed permission.',),
        skills=(), output_schema={'summary': 'string'}, model='main',
    )
    gateway = backend(tmp_path) if configured else RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / 'skills',
    )
    tools = _role_allowed_tools(
        role, TaskContext(run_id=RUN, tenant_id=TENANT, mode=mode, request='Build project.'),
        capability_gateway=gateway,
    )
    expected = ('workspace.read', 'project.inspect') if configured else ()
    if configured and gateway.is_available(TENANT, 'project.verify'):
        expected = (*expected, 'project.verify')
    assert tools == expected


@pytest.mark.parametrize('content', ['-\n' * 2000, '\n'.join(
    f'sample{i} @ https://u:SECRET@example.com/a.whl' for i in range(512)
)], ids=['repeated-options', 'direct-sources'])
async def test_many_invalid_requirements_preserve_blocker_through_public_gateway(
    tmp_path: Path, content: str,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/requirements.txt', content)
    gateway = HarnessToolGateway(backend(tmp_path))
    call = HarnessToolCallRequest(
        run_id=RUN, actor='engineer', tool_name='project.inspect', arguments={},
        approval_required=False, sandbox='read_only', idempotency_key='inspect-many-issues',
    )
    result = await gateway.invoke(TENANT, call)
    assert result.status == 'succeeded'
    assert result.payload['install_plan_state'] == 'blocked'
    assert result.payload['issues']
    assert len(cast(tuple[JsonValue, ...], result.payload['issues'])) < 10
    assert 'SECRET' not in repr(result)


@pytest.mark.parametrize('filename,content', [
    ('pyproject.toml', '[tool.poetry.dependencies]\nrequests="*"\n'),
    ('pyproject.toml', '[dependency-groups]\ndev=["pytest>=8"]\n'),
    ('pyproject.toml', ('[project]\ndependencies=["requests>=2"]\n'
     '[tool.uv.sources]\nrequests={path="../private"}\n')),
    ('package.json', ('{"dependencies":{"sample":"1"},"overrides":'
     '{"sample":"https://u:SECRET@example.com/a.tgz"}}')),
    ('package.json', '{"packageManager":"pnpm@9.0.0","dependencies":{"sample":"1"}}'),
    ('requirements.txt', 'sample-1.0-py3-none-any.whl\nsample-1.0.tar.gz\n'),
])
async def test_unresolved_declaration_or_override_blocks_installation_plan(
    tmp_path: Path, filename: str, content: str,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/{filename}', content)
    result = await inspect(backend(tmp_path))
    assert result['install_plan_state'] == 'blocked'
    assert result['issues']
    assert 'SECRET' not in repr(result)
    assert 'example.com' not in repr(result)
    dependencies = cast(tuple[Mapping[str, JsonValue], ...], result['dependencies'])
    assert not any(item['name'].endswith(('.whl', '.tar.gz')) for item in dependencies
                   if isinstance(item['name'], str))


async def test_long_dependency_list_never_loses_state_to_generic_payload_truncation(
    tmp_path: Path,
) -> None:
    entries = {f'package-{i:03d}-' + 'x' * 70: '>=1.0.0 <200.0.0' for i in range(500)}
    write_file(tmp_path / 'projects', f'{project_path()}/package.json',
               json.dumps({'dependencies': entries}))
    call = HarnessToolCallRequest(
        run_id=RUN, actor='engineer', tool_name='project.inspect', arguments={},
        approval_required=False, sandbox='read_only', idempotency_key='inspect-big-manifest',
    )
    result = await HarnessToolGateway(backend(tmp_path)).invoke(TENANT, call)
    assert 'install_plan_state' in result.payload
    assert result.payload['dependencies_truncated'] is True
    assert result.payload['install_plan_state'] == 'blocked'
    assert result.payload['dependency_count'] == 500


async def test_large_dependency_graph_is_not_misreported_as_invalid_json(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', json.dumps({
        'dependencies': {f'package{i}': '^1.0.0' for i in range(1000)},
    }))
    result = await inspect(backend(tmp_path))
    assert observations(result)['package.json']['status'] == 'observed'
    assert result['dependency_count'] == 1000
    assert result['dependencies_truncated'] is True
    assert result['install_plan_state'] == 'blocked'
