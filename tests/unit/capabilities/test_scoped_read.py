from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.capabilities import scoped_read
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.runtime.contracts import JsonValue

TENANT = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
OTHER = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
RUN = UUID('11111111-1111-4111-8111-111111111111')
OTHER_RUN = UUID('22222222-2222-4222-8222-222222222222')
ATTACHMENT = 'att_' + 'a' * 32
UNAUTHORIZED = 'att_' + 'b' * 32
ALIASES = ('workspace_read', 'workspace.read', 'read_context')


@dataclass(frozen=True)
class StoredRun:
    tenant_id: UUID
    id: UUID
    routing_decision: Mapping[str, object] | None


def stored_run(
    *, tenant: UUID = TENANT, run: UUID = RUN, session: str = 'session-a', **updates: object
) -> StoredRun:
    routing: dict[str, object] = {
        'project_id': 'project-a',
        'workspace_session_id': session,
        'sandbox_profile': 'workspace_write',
        'requested_permissions': ['workspace.read', 'workspace.write'],
        'attachment_ids': [ATTACHMENT],
    }
    routing.update(updates)
    return StoredRun(tenant, run, routing)


class FakeRunRepository:
    def __init__(self, *records: StoredRun) -> None:
        self.records = {(record.tenant_id, record.id): record for record in records}

    async def get(self, tenant_id: UUID, run_id: UUID) -> StoredRun:
        await asyncio.sleep(0)
        return self.records[(tenant_id, run_id)]


def write_file(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
    return target


def project_path(tenant: UUID = TENANT, session: str = 'session-a') -> str:
    return f'{tenant}/projects/project-a/sessions/{session}'


def gateway(root: Path, repository: object) -> RuntimeCapabilityGateway:
    return build_runtime_capability_stack(
        tenant_id=TENANT,
        run_repository=repository,
        skill_store_dir=root / 'skills',
        workspace_root=root / 'attachments',
        project_workspace_dir=root / 'projects',
    ).runtime_gateway


async def read(
    backend: RuntimeCapabilityGateway, path: str, *, name: str = 'workspace_read',
    tenant: UUID = TENANT, run: UUID = RUN, **extra: JsonValue,
) -> Mapping[str, JsonValue]:
    return await backend.execute(
        tenant_id=tenant, run_id=run, actor='reader', name=name,
        arguments={'path': path, **extra}, idempotency_key='scoped-read-test',
    )


@pytest.mark.parametrize('name', ALIASES)
async def test_missing_repository_never_falls_back_to_global_root(tmp_path: Path, name: str) -> None:
    write_file(tmp_path, 'note.txt', 'GLOBAL-CONTENT')
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills', workspace_root=tmp_path)
    with pytest.raises(RuntimeCapabilityError):
        await read(backend, 'note.txt', name=name)


@pytest.mark.parametrize('name', ALIASES)
@pytest.mark.parametrize('profile', ['read_only', 'restricted', 'workspace_write'])
async def test_current_project_file_is_read_from_persisted_session(
    tmp_path: Path, name: str, profile: str
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/AGENTS.md', 'CURRENT-RULES')
    write_file(tmp_path / 'attachments', 'AGENTS.md', 'WRONG-GLOBAL-RULES')
    repository = FakeRunRepository(stored_run(
        sandbox_profile=profile, requested_permissions=['workspace.read']
    ))
    result = await read(gateway(tmp_path, repository), 'AGENTS.md', name=name)
    assert result == {'path': 'AGENTS.md', 'text': 'CURRENT-RULES', 'truncated': False}


@pytest.mark.parametrize('name', ALIASES)
@pytest.mark.parametrize('member', [f'{ATTACHMENT}.bin', f'{ATTACHMENT}.json',
                                    f'{ATTACHMENT}.manifest.json', f'{ATTACHMENT}/src/main.ts'])
async def test_only_run_authorized_attachment_paths_remain_readable(
    tmp_path: Path, name: str, member: str
) -> None:
    path = f'{TENANT}/{member}'
    write_file(tmp_path / 'attachments', path, 'AUTHORIZED-ATTACHMENT')
    result = await read(gateway(tmp_path, FakeRunRepository(stored_run())), path, name=name)
    assert result['text'] == 'AUTHORIZED-ATTACHMENT'
    assert result['path'] == path


@pytest.mark.parametrize('name', ALIASES)
@pytest.mark.parametrize('path', [f'{OTHER}/{ATTACHMENT}.bin', f'{TENANT}/{UNAUTHORIZED}.bin',
                                  f'{TENANT}/{UNAUTHORIZED}/AGENTS.md', 'global.txt'])
async def test_other_tenant_or_unattached_files_are_denied(
    tmp_path: Path, name: str, path: str
) -> None:
    write_file(tmp_path / 'attachments', path, 'PRIVATE-CONTENT')
    with pytest.raises(RuntimeCapabilityError) as error:
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), path, name=name)
    assert 'PRIVATE-CONTENT' not in str(error.value)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize('name', ALIASES)
@pytest.mark.parametrize('path', [
    '../session-b/AGENTS.md',
    'projects/project-a/sessions/session-b/AGENTS.md',
    f'{TENANT}/projects/project-a/sessions/session-b/AGENTS.md',
    'projects/project-b/sessions/session-a/AGENTS.md',
])
async def test_other_project_or_session_cannot_be_selected_by_path_or_arguments(
    tmp_path: Path, name: str, path: str
) -> None:
    write_file(tmp_path / 'projects', f'{project_path(session="session-b")}/AGENTS.md', 'OTHER-SESSION')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), path, name=name,
                   project_id='project-b', workspace_session_id='session-b', tenant_id=str(OTHER),
                   routing_decision={'sandbox_profile': 'workspace_write'})


@pytest.mark.parametrize('updates', [
    {'sandbox_profile': 'none'},
    {'sandbox_profile': None},
    {'sandbox_profile': 'unknown'},
    {'requested_permissions': []},
    {'requested_permissions': ['command.run']},
    {'requested_permissions': 'workspace.read'},
    {'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read', 'workspace.write']},
    {'project_id': '../other'},
    {'workspace_session_id': ''},
])
@pytest.mark.parametrize('name', ALIASES)
async def test_persisted_permissions_fail_closed_despite_caller_claims(
    tmp_path: Path, updates: dict[str, object], name: str
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/AGENTS.md', 'CURRENT-RULES')
    write_file(tmp_path / 'attachments', 'AGENTS.md', 'GLOBAL-RULES')
    record = stored_run()
    assert record.routing_decision is not None
    record = replace(record, routing_decision={**record.routing_decision, **updates})
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(record)), 'AGENTS.md', name=name,
                   sandbox_profile='workspace_write', requested_permissions=('workspace.read',))


@pytest.mark.parametrize('record', [None, stored_run(tenant=OTHER), stored_run(run=OTHER_RUN),
                                   replace(stored_run(), routing_decision=None)])
async def test_missing_or_mismatched_repository_record_is_denied(
    tmp_path: Path, record: StoredRun | None
) -> None:
    class WrongRepository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> StoredRun | None:
            return record

    write_file(tmp_path / 'attachments', 'note.txt', 'MUST-NOT-READ')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, WrongRepository()), 'note.txt')


async def test_repository_failure_is_redacted(tmp_path: Path) -> None:
    class BrokenRepository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> StoredRun:
            raise RuntimeError('private database detail')

    with pytest.raises(RuntimeCapabilityError) as error:
        await read(gateway(tmp_path, BrokenRepository()), 'AGENTS.md')
    assert 'private database detail' not in str(error.value)


@pytest.mark.parametrize('link_kind', ['file', 'session', 'tenant', 'attachment'])
async def test_symlink_cannot_redirect_scoped_reads(tmp_path: Path, link_kind: str) -> None:
    projects = tmp_path / 'projects'
    foreign = write_file(projects, f'{project_path(OTHER)}/AGENTS.md', 'FOREIGN')
    if link_kind == 'file':
        link = projects / project_path() / 'AGENTS.md'
        target = foreign
        path = 'AGENTS.md'
    elif link_kind == 'session':
        link = projects / project_path()
        target = foreign.parent
        path = 'AGENTS.md'
    elif link_kind == 'tenant':
        link = projects / str(TENANT)
        target = projects / str(OTHER)
        path = 'AGENTS.md'
    else:
        link = tmp_path / 'attachments' / str(TENANT) / f'{ATTACHMENT}.bin'
        target = foreign
        path = f'{TENANT}/{ATTACHMENT}.bin'
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        pytest.skip(f'symlinks unavailable: {error}')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), path)


async def test_concurrent_runs_do_not_share_mutable_reader_scope(tmp_path: Path) -> None:
    records = (stored_run(), stored_run(run=OTHER_RUN, session='session-b'),
               stored_run(tenant=OTHER))
    for record in records:
        assert record.routing_decision is not None
        session = str(record.routing_decision['workspace_session_id'])
        write_file(tmp_path / 'projects', f'{project_path(record.tenant_id, session)}/AGENTS.md',
                   f'{record.tenant_id}:{session}')
    backend = gateway(tmp_path, FakeRunRepository(*records))
    results = await asyncio.gather(*(
        read(backend, 'AGENTS.md', tenant=record.tenant_id, run=record.id)
        for record in records for _ in range(5)
    ))
    assert [result['text'] for result in results] == [
        f'{TENANT}:session-a',
    ] * 5 + [f'{TENANT}:session-b'] * 5 + [f'{OTHER}:session-a'] * 5


async def test_query_only_context_needs_no_repository_or_disk(tmp_path: Path) -> None:
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'absent')
    result = await backend.execute(tenant_id=TENANT, run_id=RUN, actor='reader', name='read_context',
                                   arguments={'query': '  context  '}, idempotency_key='query')
    assert result['query'] == 'context'
    assert result['matches'] == ()
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('name', ALIASES)
async def test_run_must_exist_and_its_permission_is_checked_on_every_read(
    tmp_path: Path, name: str,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/AGENTS.md', 'CURRENT')
    repository = FakeRunRepository(stored_run())
    backend = gateway(tmp_path, repository)
    with pytest.raises(RuntimeCapabilityError):
        await read(backend, 'AGENTS.md', name=name, run=OTHER_RUN)
    assert (await read(backend, 'AGENTS.md', name=name))['text'] == 'CURRENT'
    repository.records[(TENANT, RUN)] = stored_run(sandbox_profile='none')
    with pytest.raises(RuntimeCapabilityError):
        await read(backend, 'AGENTS.md', name=name)


@pytest.mark.parametrize('name', ALIASES)
async def test_attachment_read_requires_persisted_workspace_permission(
    tmp_path: Path, name: str,
) -> None:
    path = f'{TENANT}/{ATTACHMENT}.bin'
    write_file(tmp_path / 'attachments', path, 'ATTACHMENT')
    backend = gateway(tmp_path, FakeRunRepository(stored_run(requested_permissions=[])))
    with pytest.raises(RuntimeCapabilityError):
        await read(backend, path, name=name)


@pytest.mark.parametrize('component', [0, 1, 2, 3])
async def test_attachment_scope_ancestor_links_are_denied(tmp_path: Path, component: int) -> None:
    parts = (str(TENANT), ATTACHMENT, 'src')
    root = tmp_path / 'attachments'
    target_root = tmp_path / 'foreign'
    suffix = parts[component:]
    write_file(target_root, '/'.join((*suffix, 'AGENTS.md')), 'FOREIGN')
    link = root.joinpath(*parts[:component])
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'symlinks unavailable: {error}')
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), '/'.join((*parts, 'AGENTS.md')))


@pytest.mark.parametrize('path', ['', '../AGENTS.md', '/AGENTS.md', 'C:AGENTS.md',
                                   'AGENTS.md:secret', 'x/../../AGENTS.md', 'x\\..\\AGENTS.md',
                                   'AGENTS.md.', 'NUL', '//host/share/file'])
async def test_unsafe_path_forms_fail_closed(tmp_path: Path, path: str) -> None:
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), path)


async def test_scoped_read_still_bounds_file_content(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/large.txt', 'x' * 70_000)
    result = await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'large.txt')
    assert result['text'] == 'x' * 65_536
    assert result['truncated'] is True


async def test_cross_scope_hardlink_is_not_a_regular_authorized_file(tmp_path: Path) -> None:
    foreign = write_file(tmp_path / 'projects', f'{project_path(OTHER)}/AGENTS.md', 'FOREIGN')
    link = tmp_path / 'projects' / project_path() / 'AGENTS.md'
    link.parent.mkdir(parents=True)
    link.hardlink_to(foreign)
    with pytest.raises(RuntimeCapabilityError):
        await read(gateway(tmp_path, FakeRunRepository(stored_run())), 'AGENTS.md')


def windows_access_error(path: Path, access: int) -> int | None:
    if sys.platform != 'win32':
        raise RuntimeError('Windows test probe is unsupported on this platform')
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), access, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error()
    kernel.CloseHandle(handle)
    return None


@pytest.mark.skipif(os.name != 'nt', reason='Windows handle sharing contract')
@pytest.mark.parametrize('attachment', [False, True])
async def test_windows_read_holds_directory_chain_and_file_until_read_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attachment: bool,
) -> None:
    root = tmp_path / ('attachments' if attachment else 'projects')
    relative = f'{TENANT}/{ATTACHMENT}/src/AGENTS.md' if attachment else f'{project_path()}/AGENTS.md'
    target = write_file(root, relative, 'AUTHORIZED')
    path = relative if attachment else 'AGENTS.md'
    paths = [target, *target.parents]
    paths = list(reversed(paths[:paths.index(tmp_path) + 1]))
    checked = False
    regular_file = scoped_read._regular_file

    def attempt_swaps(value: os.stat_result) -> None:
        nonlocal checked
        regular_file(value)
        for candidate in paths:
            for access in (0x40000000, 0x00010000):  # GENERIC_WRITE and DELETE
                error_code = windows_access_error(candidate, access)
                if error_code != 32:
                    pytest.fail(f'write/delete sharing not locked: {candidate.name}: {error_code}')
            moved = candidate.with_name(candidate.name + '-moved')
            try:
                candidate.rename(moved)
            except OSError as error:
                if getattr(error, 'winerror', None) not in (5, 32):
                    pytest.fail(f'unexpected rename error: {error}')
            else:
                moved.rename(candidate)
                pytest.fail(f'unlocked ancestor or file: {candidate.name}')
        with pytest.raises(PermissionError), target.open('r+b'):
            pass
        checked = True

    monkeypatch.setattr(scoped_read, '_regular_file', attempt_swaps)
    result = await read(gateway(tmp_path, FakeRunRepository(stored_run())), path)
    assert result['text'] == 'AUTHORIZED'
    assert checked, 'the file must be validated while its ancestor handles remain open'
    # Successful reads release every directory and file handle.
    for candidate in paths:
        for access in (0x40000000, 0x00010000):
            assert windows_access_error(candidate, access) is None
        moved = candidate.with_name(candidate.name + '-moved')
        candidate.rename(moved)
        moved.rename(candidate)
    with target.open('r+b') as handle:
        handle.write(b'OK')


@pytest.mark.skipif(os.name != 'nt', reason='Windows handle sharing contract')
async def test_windows_existing_writer_is_denied_and_handles_are_released(tmp_path: Path) -> None:
    root = tmp_path / 'projects'
    target = write_file(root, f'{project_path()}/AGENTS.md', 'AUTHORIZED')
    backend = gateway(tmp_path, FakeRunRepository(stored_run()))
    with target.open('r+b'), pytest.raises(RuntimeCapabilityError):
        await read(backend, 'AGENTS.md')
    moved = root.with_name('moved-projects')
    root.rename(moved)
    moved.rename(root)
    assert (await read(backend, 'AGENTS.md'))['text'] == 'AUTHORIZED'


@pytest.mark.skipif(os.name != 'nt', reason='Windows handle ownership contract')
@pytest.mark.parametrize('failure', ['validation', 'osfhandle', 'fdopen'])
async def test_windows_failure_releases_all_owned_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows only')
    import msvcrt

    root = tmp_path / 'projects'
    target = write_file(root, f'{project_path()}/AGENTS.md', 'AUTHORIZED')
    backend = gateway(tmp_path, FakeRunRepository(stored_run()))

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError('injected native handle ownership failure')

    with monkeypatch.context() as patch:
        if failure == 'validation':
            patch.setattr(scoped_read, '_regular_file', fail)
        elif failure == 'osfhandle':
            patch.setattr(msvcrt, 'open_osfhandle', fail)
        else:
            patch.setattr(os, 'fdopen', fail)
        with pytest.raises(RuntimeCapabilityError):
            await read(backend, 'AGENTS.md')
    for candidate in (root, target.parent, target):
        for access in (0x40000000, 0x00010000):
            assert windows_access_error(candidate, access) is None
    assert (await read(backend, 'AGENTS.md'))['text'] == 'AUTHORIZED'


@pytest.mark.skipif(os.name != 'nt', reason='Windows local filesystem scope')
def test_windows_network_namespace_fails_before_opening_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail('unsupported root must not open any native handle')

    monkeypatch.setattr(ctypes, 'WinDLL', unexpected)
    with pytest.raises(scoped_read.ScopedReadError):
        scoped_read._read_windows_locked(Path(r'\\server\share'), ('AGENTS.md',))
