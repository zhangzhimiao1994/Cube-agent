"""Offline retirement boundary tests; Linux-only fixtures use an isolated cache."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.skills.sandbox import python_project_cache as cache_api
from agent_hub.skills.sandbox.project_contract_broker import _encoded
from agent_hub.skills.sandbox.python_project_protocol import PythonFailedReservationSnapshot
from tests.unit.skills.sandbox.test_python_project_cache import (
    failed_outcome,
    failed_reservation,
    invocation,
    policy,
    reference_scope,
)


def snapshot(*, project: str = 'billing') -> PythonFailedReservationSnapshot:
    from agent_hub.skills.sandbox.python_project_protocol import PythonFailedReservationSnapshot
    reservation = failed_reservation()
    reservation['scope']['project_id'] = project
    reservation['expires_at'] = 1
    key = hashlib.sha256(_encoded({field: reservation[field]
        for field in ('scope', 'recipe_sha256', 'environment_input_sha256')})).hexdigest()
    raw, outcome = _encoded(reservation), _encoded(failed_outcome(reservation))
    return PythonFailedReservationSnapshot.model_validate_json(json.dumps({
        'schema_version': 1, 'cache_key': key, 'scope': reservation['scope'],
        'reservation': raw.decode(), 'outcome': outcome.decode(),
        'reservation_sha256': hashlib.sha256(raw).hexdigest(), 'outcome_sha256': hashlib.sha256(outcome).hexdigest(),
        'identities': [[1] * 9] * 3}), strict=True)


def retirement() -> dict[str, Any]:
    item = invocation()
    owner = {key: reference_scope(item)[key] for key in ("tenant_id", "run_id", "project_id", "session_id")}
    return {"schema_version": 1, "kind": "python_failed_owner_retired", "owner": owner,
        "cache_key": cache_api.python_project_cache_key(item), "origin_execution_id": item.execution_id,
        "reservation_sha256": "a" * 64, "outcome_sha256": "b" * 64,
        "archive_id": "00000000-0000-4000-8000-000000000009", "archive_sha256": "c" * 64}


def test_root_reserve_rejects_retired_owner_before_any_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_os: object = vars(cache_api)["os"]
    assert backend_os is os
    # Logical transition only: this does not claim Windows IO is native Linux proof.
    marker = retirement()
    name = "retired-" + hashlib.sha256(_encoded(marker["owner"])).hexdigest() + ".json"
    cache = cache_api.PythonProjectEnvironmentCache(tmp_path, policy(cache_api))

    @contextmanager
    def locked(*args: Any, **kwargs: Any) -> Any:
        with ExitStack() as stack:
            yield 1, stack, []

    monkeypatch.setattr(cache, "_locked", locked)
    monkeypatch.setattr(cache, "_catalog", lambda *args: set())
    monkeypatch.setattr(cache, "_capacity", lambda *args, **kwargs: (0, 2**40))
    monkeypatch.setattr(cache_api, "_names", lambda *args: {"cache.lock", name})
    monkeypatch.setattr(cache_api, "_read", lambda *args: marker)

    def no_allocate(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Root allocated a retired owner after its failed pair was removed")

    monkeypatch.setattr(os, "mkdir", no_allocate)
    with (pytest.raises(cache_api.PythonProjectCacheError, match="cache_incomplete"),
          cache.reserve(invocation(), next_bytes=100, deadline=10**20)):
        pytest.fail("retired scope was treated as a fresh installation")


@pytest.mark.parametrize('change', [{}, {'execution_id': 'different'}, {'recipe_sha256': 'e' * 64}])
def test_retired_owner_is_independent_of_execution_and_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
) -> None:
    marker = retirement()
    name = cache_api._retirement_name(marker['owner'])
    cache = cache_api.PythonProjectEnvironmentCache(tmp_path, policy(cache_api))
    monkeypatch.setattr(cache_api, '_names', lambda *args: {name, 'cache.lock'})
    monkeypatch.setattr(cache_api, '_read', lambda *args: marker)
    item = invocation(**change)
    assert cache._retired_scope(1, cache_api._owner(item), 10**20)
    assert not cache._retired_scope(1, cache_api._owner(invocation(run_id=UUID(int=3))), 10**20)


def test_snapshot_archive_is_deterministic_and_commit_never_accepts_a_grant() -> None:
    from agent_hub.skills.sandbox.python_project_protocol import (
        PythonFailedMaintenanceInvocation,
        PythonFailedMaintenanceRequest,
    )
    value = snapshot()
    archive = value.archive()
    assert archive.to_payload() == snapshot().archive().to_payload()
    for _ in range(2):
        item = PythonFailedMaintenanceInvocation(execution_id='python_release_expired_' + uuid4().hex,
            tenant_id=value.scope.tenant_id, run_id=value.scope.run_id,
            project_id=value.scope.project_id, session_id=value.scope.session_id)
        request = PythonFailedMaintenanceRequest(invocation=item, phase='commit', snapshot=value,
            archive_id=archive.id, archive_sha256=archive.content_sha256)
        assert request.snapshot is not None
        assert request.snapshot.archive().to_payload() == archive.to_payload()
        from agent_hub.skills.sandbox import python_project_backend as backend
        from agent_hub.skills.sandbox.python_project_client import _validated_request
        assert backend._request(request.model_dump_json().encode()) == request
        assert _validated_request(request) == request
    for change in ({'host_path': '/do/not/delete'}, {'authorized': True}, {'archive_sha256': 'f' * 64},
                   {'archive_id': uuid4()}, {'phase': 'inspect'}):
        with pytest.raises(ValueError):
            PythonFailedMaintenanceRequest.model_validate_json(json.dumps(
                request.model_dump(mode='json') | {key: str(v) if isinstance(v, UUID) else v
                    for key, v in change.items()}), strict=True)


@pytest.mark.parametrize('bad', [True, 0, 1, 'false'])
def test_maintenance_invocation_never_coerces_install_authority(bad: Any) -> None:
    from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceInvocation
    with pytest.raises(ValueError):
        PythonFailedMaintenanceInvocation(execution_id='python_release_expired_' + uuid4().hex,
            tenant_id=UUID(int=1), run_id=UUID(int=2), project_id='billing', session_id='one', install_allowed=bad)


async def test_same_archive_identity_with_changed_snapshot_metadata_is_conflict_not_overwrite() -> None:
    from agent_hub.runtime.artifacts import ArtifactRepositoryError, InMemoryArtifactRepository
    repository = InMemoryArtifactRepository()
    value = snapshot()
    original = value.archive()
    await repository.put(value.scope.tenant_id, value.scope.run_id, original)
    changed = value.model_copy(update={'identities': ((2,) * 9, *value.identities[1:])}).archive()
    assert changed.id == original.id and changed.content_sha256 != original.content_sha256
    with pytest.raises(ArtifactRepositoryError):
        await repository.put(value.scope.tenant_id, value.scope.run_id, changed)


@pytest.mark.parametrize(('job', 'change', 'populated', 'no_job', 'allowed'), [
    ('', {}, False, True, True),
    ('0', {}, False, True, True),
    (None, {}, False, True, False),
    ('7', {}, False, True, False),
    ('', {'MainPID': '123'}, False, True, False),
    ('0', {'ControlPID': '123'}, False, True, False),
    ('', {'TasksCurrent': '1'}, False, True, False),
    ('', {}, True, True, False),
    ('0', {}, True, True, False),
    (None, {}, False, False, True),
])
async def test_unit_admission_keeps_quiet_and_explicit_no_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job: str | None,
    change: dict[str, str], populated: bool, no_job: bool, allowed: bool,
) -> None:
    from agent_hub.skills.sandbox import project
    from agent_hub.skills.sandbox.base import SkillResult
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.project import _Execution
    from agent_hub.skills.sandbox.python_project_broker import _admit_unit
    seen: list[str] = []
    execution = _Execution(invocation(), None)
    properties = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead',
        'MainPID': '0', 'ControlPID': '0', 'ControlGroup': '', 'TasksCurrent': '[not set]'}
    if job is not None:
        properties['Job'] = job
    if populated:
        properties.update(LoadState='loaded', ControlGroup=f'/system.slice/{execution.unit}')
        group = tmp_path / 'system.slice' / execution.unit
        group.mkdir(parents=True)
        (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    properties.update(change)
    monkeypatch.setattr(project, '_CGROUP_ROOT', tmp_path)

    class Broker(project.ProjectExecutionBroker):
        def __init__(self) -> None:
            super().__init__(BrokerPolicy(systemctl='/usr/bin/systemctl'))
        # Keep the production quiet predicate; this is not a native Linux proof.
        _unit_quiet = staticmethod(project.ProjectExecutionBroker._unit_quiet)

        async def _helper(self, command: tuple[str, ...], timeout: float) -> SkillResult:
            seen.extend(command)
            return SkillResult(exit_code=0, stdout='\n'.join(f'{k}={v}' for k, v in properties.items()),
                               stderr='', timed_out=False)

    if allowed:
        await _admit_unit(Broker(), execution, no_job=no_job)
    else:
        with pytest.raises(ValueError, match='python_project_execution_unavailable'):
            await _admit_unit(Broker(), execution, no_job=no_job)
    assert any('Job' in item for item in seen) is no_job


@pytest.fixture
def cache_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Logical FD boundary only; parent Linux fixture must exercise real guards."""
    from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceInvocation
    value = snapshot()
    controls = {'reservation.json': json.loads(value.reservation), 'failed.json': json.loads(value.outcome)}
    root_controls: dict[str, Any] = {}
    mutations: list[str] = []
    active = {value.cache_key}
    cache = cache_api.PythonProjectEnvironmentCache(tmp_path, policy(cache_api))
    item = PythonFailedMaintenanceInvocation(execution_id='python_release_expired_' + uuid4().hex,
        tenant_id=value.scope.tenant_id, run_id=value.scope.run_id,
        project_id=value.scope.project_id, session_id=value.scope.session_id)

    @contextmanager
    def locked(*args: Any, **kwargs: Any) -> Any:
        with ExitStack() as stack:
            yield 1, stack, []

    info = SimpleNamespace(**{name: 1 for name in (
        'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')})
    original_fstat, original_stat = os.fstat, os.stat
    monkeypatch.setattr(cache, '_locked', locked)
    monkeypatch.setattr(cache, '_catalog', lambda *args: set(active))
    charged: list[int] = []

    def capacity(*args: Any, additional: int = 0, **kwargs: Any) -> tuple[int, int]:
        charged.append(additional)
        return 0, 2**40

    monkeypatch.setattr(cache, '_capacity', capacity)
    monkeypatch.setattr(cache_api, '_open_directory', lambda *args, **kwargs: 2)
    monkeypatch.setattr(cache_api, '_reservation', lambda *args: controls['reservation.json'])
    monkeypatch.setattr(cache_api, '_read', lambda fd, name, pins: (root_controls if fd == 1 else controls)[name])
    monkeypatch.setattr(cache_api, '_names', lambda fd, *args: (
        set(active) | set(root_controls) | {'cache.lock'} if fd == 1 else set(controls)))
    monkeypatch.setattr(os, 'fstat', lambda fd: info if fd == 2 else original_fstat(fd))
    monkeypatch.setattr(os, 'stat', lambda *args, **kwargs: (
        info if kwargs.get('dir_fd') == 2 else original_stat(*args, **kwargs)))
    monkeypatch.setattr(cache_api, '_same', lambda *args: None)
    monkeypatch.setattr(os, 'fsync', lambda *args: None)

    def write(fd: int, name: str, marker: Any) -> None:
        assert fd == 1 and name not in root_controls
        mutations.append('retirement')
        root_controls[name] = marker

    def unlink(name: str, **kwargs: Any) -> None:
        assert root_controls, 'once fence must persist before deleting consumed controls'
        mutations.append(name)
        controls.pop(name)

    def rmdir(name: str, **kwargs: Any) -> None:
        assert not controls
        mutations.append('rmdir')
        active.remove(name)

    monkeypatch.setattr(cache_api, '_write', write)
    monkeypatch.setattr(os, 'unlink', unlink)
    monkeypatch.setattr(os, 'rmdir', rmdir)
    return SimpleNamespace(cache=cache, item=item, controls=controls, root_controls=root_controls,
        active=active, mutations=mutations, charged=charged, snapshot=value)


@pytest.mark.parametrize('defect', [None, 'extra', 'unexpired', 'bad-receipt', 'changed-proof',
    'guard-blocked', 'retirement-conflict', 'quota', 'unlink-failure'])
def test_root_inspect_commit_keeps_exact_receipts_and_fences_before_removal(
    cache_boundary: Any, monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
    case = cache_boundary
    if defect == 'extra':
        case.controls['environment'] = {}
    elif defect == 'unexpired':
        case.controls['reservation.json']['expires_at'] = 2**62
        case.controls['failed.json'] = failed_outcome(case.controls['reservation.json'])
    elif defect == 'bad-receipt':
        case.controls['failed.json']['unit_quiet'] = False
    guards: list[str] = []

    def guard(origin: str) -> None:
        guards.append(origin)
        if defect == 'guard-blocked':
            raise ProcessCleanupIncomplete('python_project_cleanup_incomplete')

    if defect in {'bad-receipt', 'guard-blocked'}:
        with pytest.raises((cache_api.PythonProjectCacheError, ProcessCleanupIncomplete)):
            case.cache.maintain_failed(case.item, deadline=10**20, quiescence_check=guard)
        assert not case.mutations
        return
    value = case.cache.maintain_failed(case.item, deadline=10**20, quiescence_check=guard)
    assert not case.mutations
    if defect in {'extra', 'unexpired'}:
        assert value is None
        return
    assert value == case.snapshot and value.archive().to_payload() == case.snapshot.archive().to_payload()
    if defect == 'changed-proof':
        value = value.model_copy(update={'identities': ((2,) * 9, *value.identities[1:])})
    elif defect == 'retirement-conflict':
        marker = retirement()
        marker['archive_sha256'] = 'f' * 64
        case.root_controls[cache_api._retirement_name(marker['owner'])] = marker
    elif defect == 'quota':
        def quota(*args: Any, **kwargs: Any) -> Any:
            raise cache_api.PythonProjectCacheError('cache_quota')
        monkeypatch.setattr(case.cache, '_capacity', quota)
    elif defect == 'unlink-failure':
        def failed_unlink(*args: Any, **kwargs: Any) -> None:
            raise OSError('fixture deletion failure')
        monkeypatch.setattr(os, 'unlink', failed_unlink)
    if defect is None:
        assert case.cache.maintain_failed(case.item, deadline=10**20, quiescence_check=guard, expected=value) is True
        assert case.mutations == ['retirement', 'failed.json', 'reservation.json', 'rmdir']
        assert case.charged and case.charged[0] > 0 and not case.active
        assert case.cache._retired_scope(1, cache_api._owner(case.item), 10**20)
    else:
        with pytest.raises((cache_api.PythonProjectCacheError, ProcessCleanupIncomplete)):
            case.cache.maintain_failed(case.item, deadline=10**20, quiescence_check=guard, expected=value)
        assert case.mutations == (['retirement'] if defect == 'unlink-failure' else [])
        assert case.active
