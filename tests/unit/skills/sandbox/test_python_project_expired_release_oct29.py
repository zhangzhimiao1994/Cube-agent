"""Scoped expiry contracts; portable mocks are not Linux/native deletion proof."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox import python_project_backend as backend
from agent_hub.skills.sandbox import python_project_cache as cache_api
from agent_hub.skills.sandbox import python_project_client as client
from agent_hub.skills.sandbox import python_project_protocol as protocol
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker
from agent_hub.skills.sandbox.project_contract_broker import _encoded
from agent_hub.skills.sandbox.python_project_copy import PythonProjectCopyError
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox.test_python_project_broker import api as api  # noqa: PLC0414
from tests.unit.skills.sandbox.test_python_project_broker import (
    configured as configured,  # noqa: PLC0414
)
from tests.unit.skills.sandbox.test_python_project_client import invocation, reference


def request() -> Any:
    cls = getattr(protocol, 'PythonProjectReleaseExpiredEnvironmentRequest', None)
    assert cls is not None, 'scoped expiry request missing'
    return cls(invocation=invocation())


def test_expired_release_is_scoped_internal_and_never_carries_a_path_or_grant() -> None:
    item = request()
    assert item.version == 4 and item.action == 'release_expired_python_project_environment'
    assert set(item.model_dump()) == {'version', 'action', 'invocation'}
    for field in ('host_path', 'protected_runs', 'terminal', 'expired', 'expected_environment'):
        with pytest.raises(ValidationError):
            type(item).model_validate(item.model_dump() | {field: True}, strict=True)
    with pytest.raises(ValidationError):
        type(item)(invocation=invocation().model_copy(update={'install_allowed': True}))
    assert backend._request(item.model_dump_json().encode()) == item
    assert client._validated_request(item) == item


@pytest.mark.parametrize('released', [False, True])
def test_expired_response_is_an_exclusive_strict_bool_bound_to_action_and_execution(released: bool) -> None:
    item = request()
    response = protocol.PythonProjectBrokerResponse(
        action=item.action, execution_id=item.invocation.execution_id, ok=True,
        expired_environment_released=released,
    )
    response.require_for(item)
    for change in ({'expired_environment_released': None}, {'expired_environment_released': 1},
                   {'released_environment': reference()}, {'environment_reference': reference()},
                   {'release_outcome': 'released'}, {'error': 'python_project_environment_changed'}):
        with pytest.raises(ValidationError):
            type(response).model_validate(response.model_dump() | change, strict=True)
    with pytest.raises(ValueError):
        response.model_copy(update={'execution_id': 'foreign'}).require_for(item)
    with pytest.raises(ValidationError):
        protocol.PythonProjectBrokerResponse(action='run_python_project',
            execution_id=item.invocation.execution_id, ok=True, expired_environment_released=released)


@pytest.mark.parametrize('released', [False, True])
async def test_client_returns_only_root_expiry_bool(monkeypatch: pytest.MonkeyPatch, released: bool) -> None:
    seen = []

    async def exchange(self: Any, item: Any) -> Any:
        seen.append(item)
        return protocol.PythonProjectBrokerResponse(action=item.action,
            execution_id=item.invocation.execution_id, ok=True, expired_environment_released=released)

    monkeypatch.setattr(client.PythonProjectSystemdSandbox, '_exchange', exchange)
    method = getattr(client.PythonProjectSystemdSandbox(), 'release_expired', None)
    assert callable(method), 'internal expiry client missing'
    assert await method(invocation()) is released
    assert seen == [request()]


@pytest.fixture
def cache_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Real cache methods, replacing only POSIX descriptor/measurement boundaries."""
    policy = cache_api.PythonProjectCachePolicy(2**30, 8, 2**20, 86400)
    cache = cache_api.PythonProjectEnvironmentCache(tmp_path, policy)
    inv = invocation()
    key = cache_api.python_project_cache_key(inv)
    reservation = {'environment_limit_bytes': 1024, 'expires_at': 1}
    erased: list[str] = []
    locked: list[bool] = []

    @contextmanager
    def lock(self: Any, deadline: float, *, create: bool = True) -> Any:
        assert create is False
        locked.append(True)
        pins = [SimpleNamespace(check=lambda: None)]
        with ExitStack() as stack:
            try:
                yield 42, stack, pins
                for observed in pins:
                    observed.check()
            finally:
                locked.pop()

    def ready(*args: Any, **kwargs: Any) -> Any:
        assert locked == [True]
        return SimpleNamespace(reference=reference())

    def erase(parent: Any, actual: str, *args: Any) -> None:
        assert locked == [True] and actual == key
        erased.append(actual)

    def directory(parent: Any, name: str, owner: int, stack: Any, pins: Any, **kwargs: Any) -> int:
        def check() -> None:
            if erased:
                raise cache_api.PythonProjectCacheError('cache_changed')
        pins.append(SimpleNamespace(check=check))
        return 43

    monkeypatch.setattr(cache_api.PythonProjectEnvironmentCache, '_locked', lock)
    monkeypatch.setattr(cache_api.PythonProjectEnvironmentCache, '_catalog', lambda *args: {key, 'f' * 64})
    monkeypatch.setattr(cache_api, '_open_directory', directory)
    monkeypatch.setattr(cache_api, '_reservation', lambda *args: dict(reservation))
    monkeypatch.setattr(cache_api, '_ready', ready)
    monkeypatch.setattr(cache_api, '_erase_ready', erase)
    return SimpleNamespace(cache=cache, inv=inv, key=key, reservation=reservation,
                           erased=erased, locked=locked)


def selection(case: Any) -> Any:
    method = getattr(case.cache, 'inspect_expired', None)
    assert callable(method), 'native expired READY selection missing'
    return method(case.inv, deadline=time.monotonic() + 5)


def test_cache_selects_full_expired_ready_and_deletes_only_exact_key_under_lock(cache_boundary: Any) -> None:
    case = cache_boundary
    chosen = selection(case)
    assert chosen.reference == reference()
    assert chosen.reservation_sha256 == hashlib.sha256(_encoded(case.reservation)).hexdigest()
    assert case.cache.release_expired(case.inv, chosen, deadline=time.monotonic() + 5) is True
    assert case.erased == [case.key] and case.locked == []


@pytest.mark.parametrize('state', ['absent', 'unexpired', 'partial', 'changed-ref', 'changed-hash', 'changed-ttl'])
def test_cache_absence_unexpired_and_unsafe_changes_never_erase(
    cache_boundary: Any, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    case = cache_boundary
    if state == 'absent':
        monkeypatch.setattr(cache_api.PythonProjectEnvironmentCache, '_catalog', lambda *args: set())
        assert selection(case) is None
    elif state == 'unexpired':
        case.reservation['expires_at'] = int(time.time()) + 60
        assert selection(case) is None
    elif state == 'partial':
        def partial(*args: Any, **kwargs: Any) -> Any:
            raise cache_api.PythonProjectCacheError('cache_incomplete')
        monkeypatch.setattr(cache_api, '_ready', partial)
        with pytest.raises(cache_api.PythonProjectCacheError):
            selection(case)
    else:
        chosen = selection(case)
        if state == 'changed-ref':
            monkeypatch.setattr(cache_api, '_ready', lambda *args, **kwargs: SimpleNamespace(
                reference=reference().model_copy(update={'environment_sha256': '0' * 64})))
        elif state == 'changed-hash':
            case.reservation['environment_limit_bytes'] += 1
        else:
            case.reservation['expires_at'] = int(time.time()) + 60
        with pytest.raises(cache_api.PythonProjectCacheError):
            case.cache.release_expired(case.inv, chosen, deadline=time.monotonic() + 5)
    assert case.erased == []


@pytest.mark.parametrize('error', [OSError('fixed'), RuntimeError('fixed'), asyncio.CancelledError()])
def test_cache_mutation_started_failure_always_preserves_native_uncertainty(
    cache_boundary: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException,
) -> None:
    case = cache_boundary
    selected = selection(case)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        case.erased.append('mutation-started')
        raise error

    monkeypatch.setattr(cache_api, '_erase_ready', interrupted)
    with pytest.raises(ProcessCleanupIncomplete):
        case.cache.release_expired(case.inv, selected, deadline=time.monotonic() + 5)
    assert case.erased == ['mutation-started']


@pytest.mark.parametrize('barrier', ['quiet', 'unit', 'retained', 'scope', 'absent'])
def test_root_expiry_reuses_both_units_retained_and_whole_run_scope_before_cache_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, barrier: str,
) -> None:
    item = request()
    calls = []
    chosen = SimpleNamespace(reference=reference(), reservation_sha256='a' * 64)

    class Cache:
        def __init__(self, *args: Any) -> None:
            pass

        def inspect_expired(self, *args: Any, **kwargs: Any) -> Any:
            calls.append('select')
            return None if barrier == 'absent' else chosen

        def release_expired(self, inv: Any, candidate: Any, *, deadline: float) -> bool:
            assert inv == item.invocation and candidate is chosen
            calls.append('delete')
            return True

    async def admit(broker: Any, job: Any) -> None:
        calls.append(job.unit)
        if barrier == 'unit':
            raise ValueError('active')

    def scope(request: Any, *args: Any) -> None:
        assert request.expected_environment == reference()
        calls.append('scope')
        if barrier == 'scope':
            raise ProcessCleanupIncomplete('python_project_cleanup_incomplete')

    monkeypatch.setattr(backend, 'PythonProjectEnvironmentCache', Cache)
    monkeypatch.setattr(backend, '_admit_release_unit', admit)
    monkeypatch.setattr(backend, '_release_scope_guard', scope)
    monkeypatch.setattr(ProjectExecutionBroker, '_retained', lambda self, key: barrier == 'retained')
    config = SimpleNamespace(stage_root=tmp_path, environment_root=tmp_path, cache_policy=None)
    method = getattr(backend, '_release_expired', None)
    assert callable(method), 'root scoped expiry dispatch missing'
    if barrier in {'unit', 'retained', 'scope'}:
        with pytest.raises(backend.PythonProjectBackendError) as refused:
            method(item, BrokerPolicy(), config, time.monotonic() + 5, stage=tmp_path / 'current')
        assert refused.value.code == 'python_project_cleanup_incomplete'
        assert 'delete' not in calls
    else:
        response = method(item, BrokerPolicy(), config, time.monotonic() + 5, stage=tmp_path / 'current')
        assert response.expired_environment_released is (barrier != 'absent')
        if barrier == 'quiet':
            assert calls == ['select', 'agent-hub-python-broker-previous_run.service',
                             'agent-hub-project-previous_run.service', 'scope', 'delete']
        else:
            assert calls == ['select']


@pytest.mark.parametrize('at', ['guard', 'delete', 'cancel'])
def test_expiry_known_predelete_refusal_is_not_native_uncertainty_but_delete_and_cancel_stay_uncertain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], at: str,
) -> None:
    item = request()
    removed = []
    selected = cache_api.PythonProjectExpiredEnvironment(reference(), 'a' * 64)

    def guard(*args: Any, **kwargs: Any) -> None:
        if at == 'guard':
            raise ProcessCleanupIncomplete('original UNKNOWN retained')
        if at == 'cancel':
            raise asyncio.CancelledError

    def delete(*args: Any, **kwargs: Any) -> bool:
        removed.append(True)
        raise ProcessCleanupIncomplete('mutation outcome uncertain')

    config = SimpleNamespace(environment_root=tmp_path, cache_policy=None)
    monkeypatch.setattr(backend, '_release_guard', guard)
    monkeypatch.setattr(backend, 'PythonProjectEnvironmentCache', lambda *args: SimpleNamespace(
        inspect_expired=lambda *a, **k: selected, release_expired=delete))
    monkeypatch.setattr(backend, '_native', lambda: None)
    monkeypatch.setattr(backend, '_path', lambda value: tmp_path)
    monkeypatch.setattr(backend, 'load_broker_policy', BrokerPolicy)
    monkeypatch.setattr(backend, 'load_root_configuration', lambda: config)
    monkeypatch.setattr(backend, '_root', lambda *args, **kwargs: 42)
    actual_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == 'request.json' and kwargs.get('dir_fd') == 42:
            return SimpleNamespace(st_size=len(item.model_dump_json().encode()))
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', stat)
    monkeypatch.setattr(backend, '_read_control', lambda *a, **k: item.model_dump_json().encode())
    monkeypatch.setattr(backend, 'execute_python_project_request', lambda actual, **kw:
        backend._release_expired(actual, kw['policy'], kw['configuration'],
                                 time.monotonic() + 5, stage=kw['stage']))
    if at == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            backend.main(['--stage', 'unused'])
    elif at == 'guard':
        assert backend.main(['--stage', 'unused']) == 0
        response = protocol.PythonProjectBrokerResponse.model_validate_json(capsys.readouterr().out)
        assert not response.ok and response.error == 'python_project_cleanup_incomplete'
    else:
        assert backend.main(['--stage', 'unused']) == protocol.PYTHON_PROJECT_NATIVE_UNCERTAIN_EXIT_CODE
    assert removed == ([True] if at == 'delete' else [])


def test_root_expiry_dispatch_holds_admission_without_project_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload

    held = []

    @contextmanager
    def roots(*args: Any) -> Any:
        yield SimpleNamespace(stage_root_fd=42)

    @contextmanager
    def admission(*args: Any) -> Any:
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    def handler(item: Any, *args: Any, **kwargs: Any) -> Any:
        assert held == [True] and kwargs['stage'] == tmp_path
        return protocol.PythonProjectBrokerResponse(action=item.action,
            execution_id=item.invocation.execution_id, ok=True, expired_environment_released=False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('expiry reached project source, execution or budget reservation')

    monkeypatch.setattr(backend, '_native', lambda: None)
    monkeypatch.setattr(backend, '_roots', roots)
    monkeypatch.setattr(backend, '_admission', admission)
    monkeypatch.setattr(backend, '_release_expired', handler, raising=False)
    for name in ('_prepared', '_run', '_probe', '_inspect', '_disk_budget'):
        monkeypatch.setattr(backend, name, forbidden)
    result = backend.execute_python_project_request(request(), policy=BrokerPolicy(), stage=tmp_path,
        configuration=backend.RootConfig.from_payload(configuration_payload()))
    assert result.ok and result.expired_environment_released is False and held == []


@pytest.mark.parametrize('cancelled', [False, True])
def test_actual_wire_dispatch_validates_expiry_and_preserves_typed_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    from agent_hub.skills.sandbox import broker as wire
    from tests.unit.skills.sandbox.test_broker_cold_dispatch_oct28 import serve

    item = request()
    broker = wire.SkillBroker(BrokerPolicy())
    seen = []

    async def handle(actual: Any, *, peer_uid: int) -> Any:
        assert peer_uid == BrokerPolicy().allowed_uid
        seen.append(actual)
        if cancelled:
            raise asyncio.CancelledError
        return protocol.PythonProjectBrokerResponse(action=actual.action,
            execution_id=actual.invocation.execution_id, ok=True, expired_environment_released=False)

    monkeypatch.setattr(broker, 'handle_python_project', handle)
    response = serve(monkeypatch, item.model_dump(mode='json'), execution=broker)
    assert seen == [item] and response['action'] == item.action
    if cancelled:
        assert response['ok'] is False and response['error'] == 'python_project_execution_unavailable'
    else:
        assert response['ok'] is True and response['expired_environment_released'] is False
    seen.clear()
    rejected = serve(monkeypatch, item.model_dump(mode='json') | {'host_path': 'DO-NOT-ECHO'}, execution=broker)
    assert not rejected['ok'] and seen == [] and 'DO-NOT-ECHO' not in str(rejected)


async def test_expiry_peer_authentication_once_and_unknown_fences_are_preserved(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.base import SkillResult

    item = request()
    broker = configured.broker
    wrong = await api.handle_python_project(broker, item, peer_uid=broker.policy.allowed_uid + 1)
    assert not wrong.ok and not broker.executions and configured.events == []

    async def subprocess(*args: Any, **kwargs: Any) -> SkillResult:
        configured.events.append('expiry-helper')
        kwargs['process_started'](SimpleNamespace(returncode=0))
        return SkillResult(exit_code=0, stderr='', timed_out=False,
            stdout=protocol.PythonProjectBrokerResponse(action=item.action,
                execution_id=item.invocation.execution_id, ok=True,
                expired_environment_released=True).model_dump_json())

    monkeypatch.setattr(api, 'run_subprocess_with_limits', subprocess)
    reply = await api.handle_python_project(broker, item, peer_uid=broker.policy.allowed_uid)
    assert reply.ok and reply.expired_environment_released is True
    replay = await api.handle_python_project(broker, item, peer_uid=broker.policy.allowed_uid)
    assert not replay.ok and configured.events.count('expiry-helper') == 1

    other = type(item)(invocation=item.invocation.model_copy(update={'execution_id': 'expiry_unknown'}))

    async def unknown(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs['process_started'](SimpleNamespace(returncode=3))
        return SkillResult(exit_code=3, stdout='', stderr='python_project_cleanup_incomplete', timed_out=False)

    monkeypatch.setattr(api, 'run_subprocess_with_limits', unknown)
    refused = await api.handle_python_project(broker, other, peer_uid=broker.policy.allowed_uid)
    assert not refused.ok and refused.error == 'python_project_cleanup_incomplete'
    job = broker.executions[other.invocation.execution_id]
    assert not job.quiet and job.stage.is_dir()
    broker.executions.clear()
    replay = await api.handle_python_project(broker, other, peer_uid=broker.policy.allowed_uid)
    assert not replay.ok


async def test_known_guard_refusal_removes_only_each_new_gc_stage_and_keeps_original_unknown(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.base import SkillResult
    from agent_hub.skills.sandbox.project import _Execution

    broker = configured.broker
    origin = 'original_89d8bbe047064b5abe355e30fc9088c1'
    old = configured.root / (origin + '-oldunknown')
    old.mkdir(mode=0o700)
    broker._metadata(_Execution(protocol.base_python_invocation(
        invocation().model_copy(update={'execution_id': origin})), None, stage=old), 'UNKNOWN')
    original = (old / 'execution.json').read_bytes()
    item = request()

    async def subprocess(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs['process_started'](SimpleNamespace(returncode=0))
        return SkillResult(exit_code=0, timed_out=False, stderr='',
            stdout=protocol.PythonProjectBrokerResponse(action=item.action,
                execution_id=item.invocation.execution_id, ok=False,
                error='python_project_cleanup_incomplete').model_dump_json())

    monkeypatch.setattr(api, 'run_subprocess_with_limits', subprocess)
    for i in range(2):
        item = type(item)(invocation=invocation().model_copy(update={'execution_id': 'expiry_refused_' + str(i)}))
        response = await api.handle_python_project(broker, item, peer_uid=broker.policy.allowed_uid)
        assert not response.ok and response.error == 'python_project_cleanup_incomplete'
        job = broker.executions[item.invocation.execution_id]
        assert job.quiet and job.finished.is_set() and not job.stage.exists()
        assert broker.slots._value == 8
        assert (old / 'execution.json').read_bytes() == original
        assert list(configured.root.iterdir()) == [old]


@pytest.mark.parametrize('defect', [None, 'partial', 'changed-tree', 'changed-ttl'])
def test_native_fd_expiry_measures_and_preserves_unsafe_or_changed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.skip('actual no-follow cache/observer/deletion requires Linux root')
    from tests.unit.skills.sandbox.test_python_project_cache import invocation as cache_invocation
    from tests.unit.skills.sandbox.test_python_project_cache import ready

    cache, native_ref = ready(cache_api, tmp_path)
    inv = cache_invocation(execution_id='expiry_fresh', install_allowed=False)
    key = cache_api.python_project_cache_key(inv)
    entry = cache.root / key
    wall = time.time() + cache.policy.retention_seconds + 1
    monkeypatch.setattr(time, 'time', lambda: wall)
    if defect == 'partial':
        (entry / 'reference.json').unlink()
        with pytest.raises(cache_api.PythonProjectCacheError):
            cache.inspect_expired(inv, deadline=time.monotonic() + 10)
        assert entry.is_dir()
        return
    selected = cache.inspect_expired(inv, deadline=time.monotonic() + 10)
    assert selected is not None and selected.reference == native_ref
    if defect == 'changed-tree':
        path = entry / 'environment/lib/module.py'
        path.chmod(0o600)
        path.write_bytes(b'changed; never execute')
    elif defect == 'changed-ttl':
        import json
        path = entry / 'reservation.json'
        reservation = json.loads(path.read_bytes())
        reservation['expires_at'] = int(wall + 60)
        path.write_bytes(_encoded(reservation))
    if defect is not None:
        with pytest.raises((cache_api.PythonProjectCacheError, PythonProjectCopyError)):
            cache.release_expired(inv, selected, deadline=time.monotonic() + 10)
        assert entry.is_dir()
    else:
        assert cache.release_expired(inv, selected, deadline=time.monotonic() + 10) is True
        assert not entry.exists()
