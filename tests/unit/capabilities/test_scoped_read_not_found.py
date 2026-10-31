from __future__ import annotations

import asyncio
import json
import os
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from itertools import count
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities import scoped_read
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runs.repository import RunRecord
from tests.unit.capabilities.test_scoped_read import (
    ALIASES,
    ATTACHMENT,
    OTHER,
    OTHER_RUN,
    RUN,
    TENANT,
    FakeRunRepository,
    gateway,
    project_path,
    read,
    stored_run,
    windows_access_error,
    write_file,
)


@pytest.mark.parametrize('name', ALIASES)
async def test_five_existing_reads_and_missing_core_return_independent_observations(
    tmp_path: Path, name: str,
) -> None:
    files = ('README.md', 'pyproject.toml', 'src/main.py', 'src/models.py', 'tests/test_app.py')
    for path in files:
        write_file(tmp_path / 'projects', f'{project_path()}/{path}', f'CONTENT:{path}')
    backend = gateway(tmp_path, FakeRunRepository(stored_run()))
    results = await asyncio.gather(*(read(backend, path, name=name) for path in (
        *files, r'src\core.py',
    )))
    assert results[:5] == [
        {'path': path, 'text': f'CONTENT:{path}', 'truncated': False} for path in files
    ]
    assert results[5] == {
        'path': 'src/core.py', 'status': 'not_found',
        'error_code': 'workspace.file_not_found', 'truncated': False,
    }
    assert not (tmp_path / 'projects' / project_path() / 'src/core.py').exists()


async def test_operator_harness_runtime_reads_five_files_and_missing_without_scope_escalation(
    tmp_path: Path,
) -> None:
    user = UUID('33333333-3333-4333-8333-333333333333')
    scope = {
        'project_id': 'project-a', 'workspace_session_id': 'session-a',
        'sandbox_profile': 'restricted', 'permission_mode': 'default',
        'requested_permissions': ['workspace.read'], 'attachment_ids': [],
    }
    persisted = tmp_path / 'stored-run.json'
    persisted.write_text(json.dumps({
        'id': str(RUN), 'tenant_id': str(TENANT), 'actor_id': str(user),
        'actor_role': 'operator', 'mode': 'dispatch', 'status': 'running',
        'version': 1, 'created_at': '2026-10-31T00:00:00+00:00',
        'request': 'Read the project source', 'routing_decision': scope,
    }), encoding='utf-8')
    original = persisted.read_bytes()
    observed_records: list[RunRecord] = []

    class DiskRunRepository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
            saved = json.loads(persisted.read_text(encoding='utf-8'))
            if (str(tenant_id), str(run_id)) != (saved['tenant_id'], saved['id']):
                raise KeyError('run unavailable')
            record = RunRecord(
                id=UUID(saved['id']), tenant_id=UUID(saved['tenant_id']),
                actor_id=UUID(saved['actor_id']), actor_role=Role(saved['actor_role']),
                request=saved['request'], mode=TaskMode(saved['mode']),
                status=RunStatus(saved['status']), version=saved['version'],
                created_at=datetime.fromisoformat(saved['created_at']),
                routing_decision=saved['routing_decision'],
            )
            observed_records.append(record)
            return record

    expected_scope = deepcopy(scope)
    repository = DiskRunRepository()
    projects = tmp_path / 'projects'
    files = ('README.md', 'pyproject.toml', 'src/main.py', 'src/models.py', 'tests/test_app.py')
    for path in files:
        write_file(projects, f'{project_path()}/{path}', f'CONTENT:{path}')
    for foreign_path in (
        f'{project_path(OTHER)}/src/core.py',
        f'{project_path(session="session-b")}/src/core.py',
        f'{TENANT}/projects/project-b/sessions/session-a/src/core.py', 'src/core.py',
    ):
        write_file(projects, foreign_path, 'PRIVATE-FOREIGN-CONTENT')
    foreign_attachment = f'{OTHER}/{ATTACHMENT}.bin'
    write_file(tmp_path / 'attachments', foreign_attachment, 'PRIVATE-FOREIGN-CONTENT')
    stack = build_runtime_capability_stack(
        tenant_id=TENANT, run_repository=repository,
        skill_store_dir=tmp_path / 'skills', project_workspace_dir=projects,
        workspace_root=tmp_path / 'attachments',
    )
    calls = count(1)

    async def invoke(path: str, *, write: bool = False) -> HarnessToolCallResult:
        return await stack.harness_tool_gateway.invoke(
            TENANT,
            HarnessToolCallRequest(
                run_id=RUN, actor='reader',
                tool_name='workspace.write_text' if write else 'workspace.read',
                arguments={'path': path, 'content': 'UNAUTHORIZED'} if write else {'path': path},
                approval_required=False, sandbox='workspace_write' if write else 'read_only',
                idempotency_key=f'operator-scoped-{next(calls)}',
            ),
            user_id=user, role=Role.OPERATOR,
        )

    results = [await invoke(path) for path in (*files, r'src\core.py')]
    assert [result.status for result in results] == ['succeeded'] * 6, [
        result.failure_reason for result in results
    ]
    assert [result.payload for result in results[:5]] == [
        {'path': path, 'text': f'CONTENT:{path}', 'truncated': False} for path in files
    ]
    assert results[5].failure_reason is None
    assert results[5].payload == {
        'path': 'src/core.py', 'status': 'not_found',
        'error_code': 'workspace.file_not_found', 'truncated': False,
    }
    assert persisted.read_bytes() == original
    for path in ('../session-b/src/core.py', foreign_attachment):
        denied = await invoke(path)
        assert denied.status == 'failed'
        assert 'PRIVATE-FOREIGN-CONTENT' not in str(denied)
    assert (await invoke('src/core.py', write=True)).status == 'failed'
    assert not (projects / project_path() / 'src/core.py').exists()
    again = await invoke('README.md')
    assert again.status == 'succeeded'
    assert again.payload == {'path': 'README.md', 'text': 'CONTENT:README.md', 'truncated': False}
    assert persisted.read_bytes() == original
    assert observed_records
    assert all(record.routing_decision == expected_scope for record in observed_records)
    assert all(record.actor_role is Role.OPERATOR for record in observed_records)


@pytest.mark.parametrize('profile', ['read_only', 'restricted', 'workspace_write'])
@pytest.mark.parametrize('path', ['core.py', 'absent/nested/core.py'])
async def test_missing_child_is_observed_only_with_existing_authorized_session(
    tmp_path: Path, profile: str, path: str,
) -> None:
    (tmp_path / 'projects' / project_path()).mkdir(parents=True)
    result = await read(gateway(tmp_path, FakeRunRepository(stored_run(
        sandbox_profile=profile, requested_permissions=['workspace.read'],
    ))), path)
    assert result == {
        'path': path, 'status': 'not_found',
        'error_code': 'workspace.file_not_found', 'truncated': False,
    }


@pytest.mark.parametrize('include_metadata', [False, True])
async def test_low_level_missing_exception_carries_only_normalized_relative_path(
    tmp_path: Path, include_metadata: bool,
) -> None:
    (tmp_path / 'projects' / project_path()).mkdir(parents=True)
    with pytest.raises(scoped_read.ScopedReadError) as caught:
        await scoped_read.read_scoped_file(
            repository=FakeRunRepository(stored_run()), tenant_id=TENANT, run_id=RUN,
            project_root=tmp_path / 'projects', attachment_root=None,
            path=r'src\core.py', include_metadata=include_metadata,
        )
    assert isinstance(caught.value, scoped_read.ScopedFileNotFound)
    assert caught.value.relative_path == 'src/core.py'
    assert str(tmp_path) not in str(caught.value)
    assert str(TENANT) not in str(caught.value)


async def test_existing_empty_file_remains_a_text_read(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/core.py', '')
    assert await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'core.py') == {
        'path': 'core.py', 'text': '', 'truncated': False,
    }


@pytest.mark.parametrize('existing_depth', range(6))
async def test_missing_configured_root_or_scope_prefix_is_denied(
    tmp_path: Path, existing_depth: int,
) -> None:
    chain = ('projects', str(TENANT), 'projects', 'project-a', 'sessions', 'session-a')
    tmp_path.joinpath(*chain[:existing_depth]).mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeCapabilityError) as caught:
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'core.py')
    assert str(caught.value) == 'workspace read denied or scoped file unavailable'


@pytest.mark.parametrize('record', [
    stored_run(tenant=OTHER), stored_run(run=OTHER_RUN),
    stored_run(sandbox_profile='none'), stored_run(requested_permissions=[]),
    stored_run(project_id='../other'), stored_run(workspace_session_id=''),
    replace(stored_run(), routing_decision=None),
])
async def test_missing_file_does_not_bypass_persisted_identity_or_permissions(
    tmp_path: Path, record: object,
) -> None:
    class Repository:
        async def get(self, tenant_id: object, run_id: object) -> object:
            return record

    (tmp_path / 'projects' / project_path()).mkdir(parents=True)
    with pytest.raises(RuntimeCapabilityError) as caught:
        await read(gateway(tmp_path, Repository()), 'src/core.py')
    assert str(caught.value) == 'workspace read denied or scoped file unavailable'


async def test_foreign_or_global_file_never_substitutes_for_missing_session_child(
    tmp_path: Path,
) -> None:
    projects = tmp_path / 'projects'
    (projects / project_path()).mkdir(parents=True)
    for root, path in (
        (projects, f'{project_path(OTHER)}/src/core.py'),
        (projects, f'{project_path(session="session-b")}/src/core.py'),
        (projects, f'{TENANT}/projects/project-b/sessions/session-a/src/core.py'),
        (projects, 'src/core.py'), (tmp_path / 'attachments', 'src/core.py'),
    ):
        write_file(root, path, 'PRIVATE-FOREIGN-CONTENT')
    assert await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'src/core.py') == {
        'path': 'src/core.py', 'status': 'not_found',
        'error_code': 'workspace.file_not_found', 'truncated': False,
    }


@pytest.mark.parametrize('path', [
    '../session-b/core.py', r'src\..\core.py', 'C:core.py', 'NUL',
    f'{TENANT}/projects/project-a/sessions/session-b/core.py',
    f'{OTHER}/{ATTACHMENT}.bin',
])
async def test_unsafe_or_attachment_scope_path_stays_denied_when_missing(
    tmp_path: Path, path: str,
) -> None:
    (tmp_path / 'projects' / project_path()).mkdir(parents=True)
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), path)


@pytest.mark.parametrize('member', [f'{ATTACHMENT}.bin', f'{ATTACHMENT}/src/core.py'])
async def test_authorized_missing_attachment_remains_fail_closed(
    tmp_path: Path, member: str,
) -> None:
    (tmp_path / 'projects' / project_path()).mkdir(parents=True)
    (tmp_path / 'attachments' / str(TENANT) / ATTACHMENT).mkdir(parents=True)
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), f'{TENANT}/{member}')


@pytest.mark.parametrize('component', [0, 1, 2, 3, 4, 5, 6])
async def test_link_ancestor_of_missing_child_stays_denied(
    tmp_path: Path, component: int,
) -> None:
    projects = tmp_path / 'projects'
    parts = (*project_path().split('/'), 'src')
    link = projects.joinpath(*parts[:component])
    target = tmp_path / 'foreign'
    target.mkdir()
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'symlinks unavailable: {error}')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'src/core.py')


async def test_dangling_leaf_link_is_denied_instead_of_not_found(tmp_path: Path) -> None:
    link = tmp_path / 'projects' / project_path() / 'core.py'
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(tmp_path / 'missing-target')
    except OSError as error:
        pytest.skip(f'symlinks unavailable: {error}')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'core.py')


@pytest.mark.parametrize('kind', ['file_ancestor', 'directory_leaf', 'hardlink'])
async def test_nonregular_target_is_denied_instead_of_not_found(tmp_path: Path, kind: str) -> None:
    root = tmp_path / 'projects'
    session = root / project_path()
    session.mkdir(parents=True)
    if kind == 'file_ancestor':
        (session / 'src').write_text('not a directory')
        path = 'src/core.py'
    elif kind == 'directory_leaf':
        (session / 'core.py').mkdir()
        path = 'core.py'
    else:
        foreign = write_file(tmp_path / 'foreign', 'core.py', 'PRIVATE')
        (session / 'core.py').hardlink_to(foreign)
        path = 'core.py'
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), path)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX special file contract')
async def test_fifo_target_is_denied_without_blocking(tmp_path: Path) -> None:
    if sys.platform == 'win32':
        pytest.skip('POSIX special file contract')
    if os.name != 'posix':
        pytest.skip('POSIX special file contract')
    session = tmp_path / 'projects' / project_path()
    session.mkdir(parents=True)
    os.mkfifo(session / 'core.py')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'core.py')


@pytest.mark.parametrize('exception', [
    FileNotFoundError('private repository path'),
    scoped_read.ScopedFileNotFound('private/repository/path'),
    scoped_read._ScopedPathMissing(),
])
async def test_repository_missing_errors_cannot_impersonate_authorized_negative_read(
    tmp_path: Path, exception: Exception,
) -> None:
    class Repository:
        async def get(self, tenant_id: object, run_id: object) -> object:
            raise exception

    (tmp_path / 'projects' / project_path()).mkdir(parents=True)
    with pytest.raises(RuntimeCapabilityError) as caught:
        await read(gateway(tmp_path, Repository()), 'core.py')
    assert str(caught.value) == 'workspace read denied or scoped file unavailable'


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction contract')
@pytest.mark.parametrize('component', range(7))
async def test_junction_ancestor_of_missing_child_stays_denied(
    tmp_path: Path, component: int,
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows junction contract')
    import _winapi

    projects = tmp_path / 'projects'
    parts = (*project_path().split('/'), 'src')
    link = projects.joinpath(*parts[:component])
    target = tmp_path / 'foreign'
    target.mkdir()
    link.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'src/core.py')


@pytest.mark.skipif(os.name != 'nt', reason='Windows handle sharing contract')
@pytest.mark.parametrize('path', ['core.py', 'absent/nested/core.py'])
async def test_negative_read_holds_scope_chain_and_releases_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows handle sharing contract')
    import ctypes

    root = tmp_path / 'projects'
    session = root / project_path()
    session.mkdir(parents=True)
    paths = [session, *session.parents]
    paths = paths[:paths.index(tmp_path) + 1]
    original = ctypes.get_last_error
    checked = False

    def inspect_missing() -> int:
        nonlocal checked
        error = original()
        if not checked and error in (2, 3):
            checked = True
            for candidate in paths:
                for access in (0x40000000, 0x00010000):
                    assert windows_access_error(candidate, access) == 32
        return error

    monkeypatch.setattr(ctypes, 'get_last_error', inspect_missing)
    result = await read(gateway(tmp_path, FakeRunRepository(stored_run())), path)
    assert result['status'] == 'not_found'
    assert checked
    for candidate in paths:
        for access in (0x40000000, 0x00010000):
            assert windows_access_error(candidate, access) is None
    moved = root.with_name('moved-projects')
    root.rename(moved)
    moved.rename(root)


@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing permission contract')
async def test_missing_child_under_directory_with_existing_writer_stays_denied(
    tmp_path: Path,
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows sharing permission contract')
    import ctypes
    from ctypes import wintypes

    session = tmp_path / 'projects' / project_path()
    session.mkdir(parents=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    writer = kernel.CreateFileW(str(session), 0x40000000, 7, None, 3, 0x02000000, None)
    assert writer not in (None, ctypes.c_void_p(-1).value)
    backend = gateway(tmp_path, FakeRunRepository(stored_run()))
    try:
        with pytest.raises(RuntimeCapabilityError) as caught:
            await read(backend, 'core.py')
        assert str(caught.value) == 'workspace read denied or scoped file unavailable'
    finally:
        kernel.CloseHandle(writer)
    assert (await read(backend, 'core.py'))['status'] == 'not_found'
