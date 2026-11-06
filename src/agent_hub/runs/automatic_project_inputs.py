"""Once-only selection of an existing managed project; never execution authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_contracts import ManagedProjectInput, _control, _path
from agent_hub.capabilities.project_stack_admission import require_supported_project_paths
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.domain.runs import RunStatus
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_manifest
from agent_hub.workspace_maintenance_contract import _v2_intent_text

VERSION = 'automatic_project_input_version'
STATE = 'automatic_project_input_selection'
ERROR = 'automatic project selection unavailable or changed'
_SCOPE = ('conversation_id', 'project_id', 'workspace_session_id', 'sandbox_profile',
          'execution_backend', 'permission_mode', 'requested_permissions', 'replace_workspace_files', VERSION)
_SELECTORS = ('project_input', 'project_input_contract_version', 'generated_project_contract_version')
DERIVED = frozenset((*_SELECTORS, STATE, 'project_delivery', 'artifact_strategy'))


def _need(value: object) -> None:
    if not value:
        raise RunConflict(ERROR)


def maintenance_intent(request: str) -> bool:
    """A leading command, optionally after a bounded existing-scope context."""
    text = _v2_intent_text(request).strip()
    first = re.split(r'[。；;\n!?！？]', text, maxsplit=1)[0].strip()
    if not first or re.search(r'[？?]|仅规划|只规划|先规划|只做计划|仅做计划|仅讨论|只讨论|先讨论|仅分析|只分析|只给方案|仅给方案|暂不执行|不要执行|禁止执行|planning only|\b(?:if|unless|maybe|perhaps|would|could|should)\b|如果|假如|倘若|如需', text[:len(first) + 1], re.IGNORECASE):
        return False
    command = (
        r'^(?:(?:请你?|麻烦|帮我|帮忙|现在|这次|继续|先)\s*|(?:please|kindly|now|continue to)\s+)*'
        r'(?:维护|修复|修改|修补|更新|调整|增加|新增|添加|加一个|加上|实现|支持|创建|编写|构建|开发|制作|搭建|'
        r'(?:fix|repair|modify|update|maintain|add|implement|support|build|create|develop|generate)\b)'
    )
    if re.match(command, first, re.IGNORECASE) is not None:
        return True
    # Consume only a grammatical leading scope, never arbitrary prose before an
    # action keyword. Labels are bounded identifiers, not selectors or authority;
    # the caller still binds the persisted conversation and eligible permissions.
    context = re.match(
        r'^(?:(?:请你?|麻烦|现在|这次|先)\s*)*'
        r'(?:(?:在|针对|对于|沿用|继续)\s*'
        r'(?:(?:当前|现有|既有|原有|同一|已有|这个|该|已经交付的|普通用户的|用户的)\s*)+'
        r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\s*)?(?:项目|工作区)'
        r'(?:\s*(?:和|及|与)(?:(?:当前|同一)\s*)?会话)?(?:中|内)?'
        r'|(?:in|for|within|using)\s+(?:the\s+)?(?:current|existing|same|retained)\s+'
        r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\s+)?(?:project|workspace)'
        r'(?:\s+and\s+(?:the\s+)?(?:conversation|session))?)', first, re.IGNORECASE,
    )
    if context is None or context.end() > 160:
        return False
    if re.search(
        r'如何|怎么|为什么|是否|能否|(?:不要|禁止|不得|无需|不必|暂不|请勿)\s*(?:修改|修复|修补|维护|更新|执行|动)|'
        r'\b(?:do\s+not|don.t|never|must\s+not|explain|analyse|analyze|why|whether)\b',
        first, re.IGNORECASE,
    ):
        return False
    following = first[context.end():].lstrip(' \t,，:：')
    # "Only repair" narrows the action. "Only analyse/plan" cannot match a command.
    following = re.sub(r'^(?:(?:请你?|先|只|仅)\s*)+', '', following)
    return re.match(command, following, re.IGNORECASE) is not None


def eligible(route: Mapping[str, object]) -> bool:
    permissions = route.get('requested_permissions')
    return (route.get('sandbox_profile') == 'workspace_write'
        and route.get('execution_backend', 'systemd') == 'systemd'
        and route.get('permission_mode') in ('default', 'auto_review')
        and route.get('replace_workspace_files') is not True
        and isinstance(permissions, (list, tuple))
        and all(type(p) is str for p in permissions)
        and {'workspace.read', 'workspace.write', 'command.run'}.issubset(permissions))


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _binding(context: TaskContext) -> str:
    return hashlib.sha256(_encoded({'tenant': str(context.tenant_id), 'run': str(context.run_id),
        'actor': str(context.actor_id), 'role': context.actor_role, 'mode': context.mode,
        'request': context.request, 'scope': {k: context.routing_decision.get(k) for k in _SCOPE}})).hexdigest()


def saved_selection(context: TaskContext, value: object) -> Mapping[str, JsonValue]:
    _need(isinstance(value, Mapping))
    assert isinstance(value, Mapping)
    state = cast(Mapping[str, JsonValue], value)
    _need(type(state.get('schema_version')) is int and state['schema_version'] == 1
          and state.get('binding_sha256') == _binding(context) and state.get('phase') == 'selected'
          and state.get('kind') in ('original', 'generated', 'blocked'))
    required = {'schema_version', 'binding_sha256', 'phase', 'kind', 'source_sha256'}
    if state['kind'] == 'original':
        required.add('editable_paths')
        ManagedProjectInput.from_payload({'editable_paths': state.get('editable_paths')})
    if state['kind'] == 'blocked':
        required.add('reason')
        _need(state.get('reason') in ('unsupported', 'scope_limit', 'no_implementation'))
    _need(set(state) == required and isinstance(state.get('source_sha256'), str)
          and re.fullmatch('[a-f0-9]{64}', cast(str, state['source_sha256'])) is not None)
    return state


def selected_route(context: TaskContext, state: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    state = saved_selection(context, state)
    _need(state['kind'] != 'blocked')
    route = dict(context.routing_decision)
    route[STATE] = cast(JsonValue, dict(state))
    if state['kind'] == 'original':
        selection = ManagedProjectInput.from_payload({'editable_paths': state['editable_paths']})
        route.pop('generated_project_contract_version', None)
        route.update(project_input_contract_version=1, project_input=cast(JsonValue, selection.to_payload()),
                     project_delivery='workspace', artifact_strategy='workspace_bundle')
    return route


def original_source(context: TaskContext) -> str | None:
    if VERSION not in context.routing_decision:
        return None
    state = saved_selection(context, context.routing_decision.get(STATE))
    _need(state['kind'] == 'original')
    return cast(str, state['source_sha256'])


def _implementation(path: str, python: bool) -> bool:
    parts = _path(path).split('/')
    name = parts[-1].casefold()
    if _control(path) or any(p.startswith('.') or p.casefold() in {
        'test', 'tests', 'fixtures', 'testdata', 'config', 'configs', 'profiles', 'deploy', 'scripts',
        'dist', 'build', 'node_modules', 'venv', '__pycache__'} for p in parts):
        return False
    if (name in {'__init__.py', '__main__.py', 'manifest.in'}
            or re.match(r'^(config|settings|profile)[_-]', name)
            or name.startswith(('config.', 'settings.', 'profile.', 'build.', 'jest.', 'vitest.',
                                'eslint.', 'prettier.', 'babel.', 'postcss.', 'tailwind.', 'webpack.', 'rollup.', 'vite.'))
            or re.search(
        r'(^test[_.-]|[_.-](test|spec|config|settings)[_.-]|^conftest\.|\.lock$)', name)):
        return False
    suffixes = {'.py'} if python else {'.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.css'}
    return PurePosixPath(path).suffix in suffixes


def _absent_path(path: str, paths: Sequence[str]) -> bool:
    candidate = path.casefold()
    return not any(candidate == p.casefold() or candidate.startswith(p.casefold() + '/')
                   or p.casefold().startswith(candidate + '/') for p in paths)


def _editables(
    paths: Sequence[str], request: str, run_id: UUID, python: bool, *, file_limit: int = 10_000,
) -> tuple[str, ...]:
    if type(file_limit) is not int or not 1 <= file_limit <= 100_000:
        raise ValueError('scope_limit')
    result = [p for p in paths if _implementation(p, python) or p == 'README.md']
    if _absent_path('README.md', paths):
        result.append('README.md')
    if not any(_implementation(p, python) for p in result):
        raise ValueError('no_implementation')
    if python:
        explicit = tuple(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9_/])tests/test_[A-Za-z0-9_]+\.py(?![A-Za-z0-9_./])', request)))
        absent = [p for p in explicit if p.casefold() not in {v.casefold() for v in paths}]
        _need(len(absent) <= 1)
        new_test = absent[0] if absent else f'tests/test_automatic_{run_id.hex}.py'
        _need(new_test.casefold() not in {v.casefold() for v in paths})
        _need(not any(new_test.startswith(p + '/') or p.startswith(new_test + '/') for p in paths))
        result.append(new_test)
    # Existing files plus precisely the absent README/test must fit the SAME native
    # file budget; do not coalesce exact files into directory write permissions.
    existing = set(paths)
    if len(paths) + sum(path not in existing for path in result) > file_limit:
        raise ValueError('scope_limit')
    try:
        return ManagedProjectInput(tuple(result)).editable_paths
    except ValueError:
        raise ValueError('scope_limit') from None


async def select_existing(
    repository: RunRepository, root: Path, context: TaskContext,
    *, generated_owned: Callable[[], Awaitable[bool]] | None = None,
) -> TaskContext:
    if VERSION not in context.routing_decision:
        return context
    context = context.validated_internal_clone()
    _need(type(context.routing_decision[VERSION]) is int and context.routing_decision[VERSION] == 1)

    async def state_io(expected: object = None, replacement: Mapping[str, JsonValue] | None = None,
                       *, reading: bool = False) -> Mapping[str, JsonValue] | None:
        async with await repository.run_transaction() as session, session.begin():
            row = await repository.get_for_update(session, context.run_id)
            record = RunRepository._record(row)
            route = _freeze_object(record.routing_decision or {}, name='automatic project route')
            _need(record.tenant_id == context.tenant_id and record.actor_id == context.actor_id
                and type(record.actor_id) is UUID and record.actor_role is context.actor_role
                and isinstance(record.actor_role, Role) and record.request == context.request
                and record.mode is context.mode and record.status is RunStatus.RUNNING and record.worker_id
                and type(context.execution_token) is UUID and record.worker_lease_token == context.execution_token
                and record.worker_lease_expires_at is not None and record.worker_lease_expires_at > datetime.now(UTC)
                and type(route.get(VERSION)) is int and route[VERSION] == 1
                and eligible(route) and maintenance_intent(record.request)
                and all(route.get(k) == context.routing_decision.get(k) for k in (*_SCOPE, *_SELECTORS)))
            authorized_workspace_scope(record, tenant_id=context.tenant_id, run_id=context.run_id)
            _need(type(route.get('conversation_id')) is str and bool(route['conversation_id']))
            state = route.get(STATE)
            if reading:
                _need(state is None or isinstance(state, Mapping))
                return cast(Mapping[str, JsonValue] | None, state)
            _need(state == expected)
            assert replacement is not None
            if replacement.get('phase') == 'selected':
                new_route = selected_route(context, replacement) if replacement.get('kind') != 'blocked' else {
                    **context.routing_decision, STATE: cast(JsonValue, dict(replacement))}
                route_delta = {k: v for k, v in new_route.items() if k in DERIVED}
                row.routing_decision = {k: v for k, v in (record.routing_decision or {}).items()
                                        if k not in DERIVED}
                row.routing_decision.update(json.loads(_encoded(route_delta)))
            else:
                row.routing_decision = {**(record.routing_decision or {}), STATE: json.loads(_encoded(replacement))}
            row.version += 1
            return replacement

    state = await state_io(reading=True)
    if state is not None:
        saved = saved_selection(context, state)  # interrupted selection is never repeated.
        if saved['kind'] == 'generated':
            project = cast(str, context.routing_decision['project_id'])
            session_id = cast(str, context.routing_decision['workspace_session_id'])
            items = await asyncio.to_thread(ProjectWorkspaceStore(root).list_files,
                                           context.tenant_id, project, session_id)
            current_manifest: dict[str, str] = {}
            if items:
                invocation = ProjectInvocation(execution_id='automatic_recheck_' + context.run_id.hex,
                    tenant_id=context.tenant_id, run_id=context.run_id, project_id=project,
                    session_id=session_id, sandbox_profile='workspace_write', timeout_seconds=1)
                current_manifest = await project_source_manifest(root, invocation)
            if hashlib.sha256(_encoded(current_manifest)).hexdigest() != saved['source_sha256']:
                _need(generated_owned is not None and await generated_owned() is True)
            _need(_freeze_object(await state_io(reading=True) or {}, name='rechecked selection')
                  == _freeze_object(saved, name='saved selection'))
        route = selected_route(context, saved)
        _need(_freeze_object({k: route[k] for k in _SELECTORS if k in route}, name='restored selectors')
              == _freeze_object({k: context.routing_decision[k] for k in _SELECTORS
                                 if k in context.routing_decision}, name='current selectors'))
        return context.model_copy(update={'routing_decision': route}).validated_internal_clone()
    _need(context.checkpoint is None and not any(k in context.routing_decision for k in (
        'project_input', 'project_input_contract_version', 'project_input_preparation', 'generated_project_preparation')))
    reserved: Mapping[str, JsonValue] = {'schema_version': 1, 'phase': 'selecting', 'binding_sha256': _binding(context)}
    await state_io(replacement=reserved)
    project = cast(str, context.routing_decision['project_id'])
    session_id = cast(str, context.routing_decision['workspace_session_id'])
    store = ProjectWorkspaceStore(root)
    items = await asyncio.to_thread(store.list_files, context.tenant_id, project, session_id)
    paths = tuple(item.path for item in items)
    manifest: dict[str, str] = {}
    kind, reason = 'generated', ''
    editable: tuple[str, ...] = ()
    if paths:
        invocation = ProjectInvocation(execution_id='automatic_' + context.run_id.hex,
            tenant_id=context.tenant_id, run_id=context.run_id, project_id=project,
            session_id=session_id, sandbox_profile='workspace_write', timeout_seconds=1)
        manifest = await project_source_manifest(root, invocation)
        _need(set(paths) == set(manifest))
        try:
            require_supported_project_paths(paths)
            if ('pyproject.toml' in paths) == ('package.json' in paths):
                raise ValueError('unsupported')
            editable = _editables(paths, context.request, context.run_id, 'pyproject.toml' in paths,
                                  file_limit=invocation.file_limit)
            kind = 'original'
        except ValueError as error:
            kind = 'blocked'
            reason = str(error) if str(error) in ('scope_limit', 'no_implementation') else 'unsupported'
        _need(manifest == await project_source_manifest(root, invocation))
    decision: dict[str, JsonValue] = {'schema_version': 1, 'phase': 'selected', 'kind': kind,
        'binding_sha256': _binding(context), 'source_sha256': hashlib.sha256(_encoded(manifest)).hexdigest()}
    if kind == 'original':
        decision['editable_paths'] = editable
        try:
            # Keep the existing JSON node/byte budgets, including the duplicate
            # persisted selection, user route and existing context artifacts.
            context.model_copy(update={'routing_decision': selected_route(context, decision)}).validated_internal_clone()
        except (ValueError, TypeError):
            decision.pop('editable_paths')
            decision.update(kind='blocked', reason='scope_limit')
    if kind == 'blocked':
        decision['reason'] = reason
    await state_io(expected=reserved, replacement=decision)
    return context.model_copy(update={'routing_decision': selected_route(context, decision)}).validated_internal_clone()
