"""Finite adaptive admission logic; mocked FD boundaries are not Linux native proof."""
from __future__ import annotations

import hashlib
import importlib
import json
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.project import _Execution
from agent_hub.skills.sandbox.python_project_copy import PythonProjectCopyError
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    base_python_invocation,
)
from tests.unit.skills.sandbox.test_python_project_cache import (
    failed_outcome,
    failed_reservation,
    invocation,
    policy,
)

USED = 409311207
REQUESTED = 201326592
EFFECTIVE = 127494169


@pytest.fixture
def api() -> Any:
    return importlib.import_module('agent_hub.skills.sandbox.python_project_cache')


def logical_cache(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                  state: str = 'fresh') -> tuple[Any, dict[str, Any], list[str]]:
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api, max_bytes=536870912))
    files: dict[str, Any] = {}
    events: list[str] = []
    key = api.python_project_cache_key(invocation())
    names = {str(i) * 64 for i in range(1, 6)}
    if state != 'fresh':
        names.remove('5' * 64)
        names.add(key)
        files['reservation.json'] = failed_reservation() | {'environment_limit_bytes': REQUESTED}
        if state == 'failed':
            files['failed.json'] = failed_outcome(files['reservation.json'])
        elif state == 'ready':
            files.update({'reference.json': {}, 'environment': {}})

    @contextmanager
    def locked(*args: Any, **kwargs: Any) -> Any:
        events.append('lock')
        with ExitStack() as stack:
            yield 1, stack, []
        events.append('unlock')

    def capacity(*args: Any, **kwargs: Any) -> tuple[int, int]:
        assert events.count('lock') == events.count('unlock') + 1
        return USED, 7644811264

    def write(parent: int, name: str, value: Any) -> None:
        assert name not in files
        files[name] = value
        events.append('write:' + name)

    def rename(src: str, dst: str, **kwargs: Any) -> None:
        assert dst not in files
        files[dst] = files.pop(src)

    def measured(*args: Any) -> Any:
        events.append('measured')
        reservation = files['reservation.json']
        payload = {'schema_version': 1, 'scope': reservation['scope'],
            'recipe_sha256': reservation['recipe_sha256'],
            'environment_input_sha256': reservation['environment_input_sha256'],
            'environment_sha256': 'e' * 64, 'security_manifest_sha256': 'f' * 64,
            'origin_execution_id': reservation['origin_execution_id']}
        return SimpleNamespace(reference=PythonEnvironmentReference.model_validate_json(json.dumps(payload)))

    monkeypatch.setattr(cache, '_locked', locked)
    monkeypatch.setattr(cache, '_catalog', lambda *args: names)
    monkeypatch.setattr(cache, '_capacity', capacity)
    monkeypatch.setattr(api, '_open_directory', lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, '_read', lambda parent, name, pins: files[name])
    monkeypatch.setattr(api, '_write', write)
    monkeypatch.setattr(api, '_names', lambda *args: set(files))
    monkeypatch.setattr(api, '_ready', measured)
    def mkdir(name: str, *args: Any, **kwargs: Any) -> None:
        assert name not in names
        names.add(name)
        events.append('mkdir')
    monkeypatch.setattr(api.os, 'mkdir', mkdir)
    monkeypatch.setattr(api.os, 'fsync', lambda *args: None)
    monkeypatch.setattr(api.os, 'rename', rename)
    return cache, files, events


@pytest.mark.parametrize('state', ['fresh', 'failed'])
def test_adaptive_measured_headroom_is_frozen_under_lock(api: Any, monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path, state: str) -> None:
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, state)
    before = dict(files)
    item = invocation(execution_id='new-attempt')
    with cache.reserve(item, next_bytes=REQUESTED, minimum_bytes=1024,
                       adaptive=True, deadline=time.monotonic() + 2) as lease:
        assert lease.limits.max_bytes == EFFECTIVE
        assert lease.reservation['environment_limit_bytes'] == EFFECTIVE
        assert lease.current(deadline=time.monotonic() + 2) is None
    assert events[0] == 'lock' and events[-1] == 'unlock'
    if state == 'failed':
        assert files['failed-original.reservation.json'] == before['reservation.json']
        assert files['failed-original.outcome.json'] == before['failed.json']


def test_default_admission_stays_strict(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache, files, events = logical_cache(api, monkeypatch, tmp_path)
    with (pytest.raises(api.PythonProjectCacheError, match='cache_quota'), cache.reserve(invocation(), next_bytes=REQUESTED, deadline=time.monotonic() + 2)):
        pytest.fail('legacy allocation was clamped')
    assert files == {} and 'mkdir' not in events


@pytest.mark.parametrize('state', ['fresh', 'failed'])
def test_runtime_floor_rejects_before_any_mutation(api: Any, monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path, state: str) -> None:
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, state)
    before = dict(files)
    with (pytest.raises(api.PythonProjectCacheError, match='cache_quota'), cache.reserve(invocation(execution_id='new-attempt'), next_bytes=REQUESTED, adaptive=True, minimum_bytes=EFFECTIVE + 1, deadline=time.monotonic() + 2)):
        pytest.fail('insufficient runtime floor admitted')
    assert files == before and 'mkdir' not in events


def test_ready_is_measured_and_not_clamped_to_new_headroom(api: Any, monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, 'ready')
    before = dict(files)
    with cache.reserve(invocation(install_allowed=False), next_bytes=REQUESTED, adaptive=True,
                       minimum_bytes=1024, deadline=time.monotonic() + 2) as lease:
        assert 'measured' in events
        assert lease.limits.max_bytes == REQUESTED
    assert files == before and 'mkdir' not in events


def test_unknown_is_not_adapted_or_replayed(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache, files, _ = logical_cache(api, monkeypatch, tmp_path, 'unknown')
    before = dict(files)
    with (pytest.raises(api.PythonProjectCacheError), cache.reserve(invocation(execution_id='new-attempt'), next_bytes=REQUESTED, adaptive=True, minimum_bytes=1024, deadline=time.monotonic() + 2)):
        pytest.fail('UNKNOWN reused')
    assert files == before


def unpublished(reservation: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps(reservation, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=True).encode()).hexdigest()
    return {'schema_version': 1, 'state': 'unpublished_capacity', 'phase': 'publish',
        'execution_id': reservation['origin_execution_id'], 'reservation_sha256': digest,
        'unit_quiet': True, 'cache_unpublished': True, 'export_disposition': 'broker_cleanup',
        'reason': 'native_export_limit'}


@pytest.mark.parametrize('fault', [None, 'quiet', 'reservation', 'phase', 'extra'])
def test_unpublished_capacity_is_distinct_from_install_failure(api: Any, fault: str | None) -> None:
    reservation = failed_reservation()
    value = unpublished(reservation)
    if fault == 'quiet': value['unit_quiet'] = False
    elif fault == 'reservation': value['reservation_sha256'] = '0' * 64
    elif fault == 'phase': value['phase'] = 'install'
    elif fault == 'extra': value['exit_code'] = 1
    if fault is None:
        api._validate_unpublished_outcome(value, reservation)
        with pytest.raises(api.PythonProjectCacheError):
            api._validate_failed_outcome(value, reservation)
    else:
        with pytest.raises(api.PythonProjectCacheError):
            api._validate_unpublished_outcome(value, reservation)


@pytest.mark.parametrize('quiet', [True, False])
def test_unpublished_marker_never_becomes_retryable_or_install_failure(api: Any, monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path, quiet: bool) -> None:
    cache, files, _ = logical_cache(api, monkeypatch, tmp_path)
    item = invocation(execution_id='new-attempt')
    monkeypatch.setattr(api, '_root', lambda *args, **kwargs: 3)
    with cache.reserve(item, next_bytes=REQUESTED, adaptive=True,
                       minimum_bytes=1024, deadline=time.monotonic() + 2) as lease:
        job = _Execution(base_python_invocation(item), None, stage=tmp_path, launched=True, quiet=quiet)
        if not quiet:
            with pytest.raises(api.PythonProjectCacheError):
                lease.record_unpublished_capacity(execution=job, deadline=time.monotonic() + 2)
            assert 'unpublished.json' not in files
            return
        lease.record_unpublished_capacity(execution=job, deadline=time.monotonic() + 2)
        assert files['unpublished.json'] == unpublished(lease.reservation)
        assert 'environment' not in files and 'reference.json' not in files and 'failed.json' not in files
    before = dict(files)
    with (pytest.raises(api.PythonProjectCacheError), cache.reserve(invocation(execution_id='later'), next_bytes=REQUESTED, adaptive=True, minimum_bytes=1024, deadline=time.monotonic() + 2)):
        pytest.fail('unpublished consumed outcome replayed')
    assert files == before


@pytest.mark.parametrize('oversize', [False, True, 'copy_started'])
def test_native_export_prescan_uses_effective_limit_before_cache_copy(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path, oversize: bool | str) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    from tests.unit.skills.sandbox.test_python_project_staging import fixture
    recipe, item, _, _ = fixture()
    events: list[str] = []
    lease = SimpleNamespace(limits=ScanLimits(EFFECTIVE))
    job = _Execution(base_python_invocation(item), None, stage=tmp_path, launched=True, quiet=True)

    def scan(*args: Any) -> dict[str, Any]:
        assert args[2] is lease.limits
        events.append('prescan')
        if oversize is True: raise PythonProjectCopyError('copy_limit')
        return {}

    @contextmanager
    def members(*args: Any) -> Any:
        yield iter([SimpleNamespace(name='ready' if args[0] == 2 else 'environment')])

    monkeypatch.setattr(backend, '_open_directory', lambda parent, name, *args, **kwargs:
                        {'export': 2, 'ready': 3, 'environment': 4}[name])
    monkeypatch.setattr(backend.os, 'scandir', members)
    monkeypatch.setattr(backend.os, 'stat', lambda *args, **kwargs: SimpleNamespace(st_uid=0))
    monkeypatch.setattr(backend, '_snapshot', scan)
    monkeypatch.setattr(backend, '_observe', lambda *args: SimpleNamespace(
        environment_sha256='a' * 64, security_manifest={}))
    monkeypatch.setattr(backend, '_recipe_entries', lambda *args: None)
    def publish(*args: Any, **kwargs: Any) -> str:
        events.append('copy')
        if oversize == 'copy_started':
            raise PythonProjectCopyError('copy_limit')
        return 'reference'
    lease.publish = publish
    lease.record_unpublished_capacity = lambda **kwargs: events.append('unpublished')
    args = (lease, 1, tmp_path / 'export', recipe, SimpleNamespace(runtime_root=tmp_path),
            REQUESTED, 'a' * 64, time.monotonic() + 2)
    if oversize == 'copy_started':
        with pytest.raises(PythonProjectCopyError):
            backend._publish_export(*args, execution=job)
        assert events == ['prescan', 'copy']
    elif oversize is True:
        with pytest.raises(backend.PythonProjectBackendError, match='python_project_environment_quota'):
            backend._publish_export(*args, execution=job)
        assert events == ['prescan', 'unpublished']
    else:
        assert backend._publish_export(*args, execution=job) == 'reference'
        assert events == ['prescan', 'copy']


@pytest.mark.parametrize('mismatch', [False, True])
def test_runtime_floor_uses_pinned_binary_not_model_budget(monkeypatch: pytest.MonkeyPatch, mismatch: bool) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    raw = b'canonical pinned native interpreter'
    config = SimpleNamespace(runtime_root=Path('/trusted/runtime'), runtime_max_bytes=1000,
        interpreter=SimpleNamespace(binary_sha256=('0' * 64 if mismatch else hashlib.sha256(raw).hexdigest())))
    monkeypatch.setattr(backend, '_read_no_links', lambda *args, **kwargs: raw)
    if mismatch:
        with pytest.raises(backend.PythonProjectBackendError, match='python_project_runtime_untrusted'):
            backend._minimum_environment_bytes(config, time.monotonic() + 2)
    else:
        assert backend._minimum_environment_bytes(config, time.monotonic() + 2) == 3 * len(raw)


@pytest.mark.parametrize('reuse', [False, True])
def test_real_backend_flow_uses_lease_budget_after_validation(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path, reuse: bool) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerRequest
    from tests.unit.skills.sandbox.test_python_project_backend import (
        configuration_payload,
        expected_reference,
        report,
        stubbed_context,
    )
    real_disk_budget = backend._disk_budget
    recipe, item, _, stage, events = stubbed_context(backend, monkeypatch, tmp_path)
    configured_payload = configuration_payload()
    configured_payload['max_environment_bytes'] = REQUESTED
    configured_payload['cache_policy'].update(max_bytes=536870912, max_entries=8,
        disk_reserve_bytes=1073741824, retention_seconds=86400)
    configured = backend.RootConfig.from_payload(configured_payload)
    if reuse:
        # Enough for initial source/product staging and reuse, not two new copies.
        available = (item.source_limit_bytes + item.workspace_limit_bytes
            + backend._STAGE_CONTROL_ALLOWANCE + configured.cache_policy.disk_reserve_bytes)
        real_fstat = backend.os.fstat
        monkeypatch.setattr(backend.os, 'fstat', lambda fd:
            SimpleNamespace(st_dev=64769) if fd in {10010, 10011} else real_fstat(fd))
        @contextmanager
        def roots_context(*args: Any, **kwargs: Any) -> Any:
            yield SimpleNamespace(stage_root_fd=9, stage_fd=10010, cache_fd=10011, pins=[])
        monkeypatch.setattr(backend, '_roots', roots_context)
        monkeypatch.setattr(backend.os, 'fstatvfs', lambda *args:
            SimpleNamespace(f_bavail=available, f_frsize=1), raising=False)
        roots = SimpleNamespace(stage_fd=10010, cache_fd=10011)
        real_disk_budget(configured, roots, REQUESTED, False, product_bytes=item.workspace_limit_bytes)
        with pytest.raises(backend.PythonProjectBackendError, match='python_project_environment_quota'):
            real_disk_budget(configured, roots, REQUESTED, True, product_bytes=item.workspace_limit_bytes)
    cached = expected_reference(item) if reuse else None
    selected = REQUESTED if reuse else EFFECTIVE
    lease = SimpleNamespace(path=tmp_path, limits=ScanLimits(selected))

    def current(**kwargs: Any) -> Any:
        events.append('validated-current')
        return cached
    lease.current = current

    class Cache:
        @contextmanager
        def reserve(self, actual: Any, **kwargs: Any) -> Any:
            assert kwargs['adaptive'] is True and kwargs['minimum_bytes'] == 300
            assert actual == item
            events.append('cache-lock')
            if not reuse:
                kwargs['fresh_check']()
            yield lease
            events.append('cache-unlock')

    def disk(config: Any, roots: Any, budget: int, copying: bool, **kwargs: Any) -> None:
        if copying:
            assert not reuse and 'cache-lock' in events and budget == REQUESTED
        if 'validated-current' in events:
            assert copying is (not reuse)
        if reuse:
            assert copying is False
            real_disk_budget(config, roots, budget, copying, **kwargs)

    def publish(*args: Any, **kwargs: Any) -> Any:
        assert args[5] == selected and kwargs['execution'].quiet is True
        events.append('published')
        return expected_reference(item)

    def quiet(policy: Any, job: Any) -> None:
        job.quiet = True

    payload = report(item, recipe, reused=reuse)
    monkeypatch.setattr(backend, 'PythonProjectEnvironmentCache', lambda *args: Cache())
    monkeypatch.setattr(backend, '_minimum_environment_bytes', lambda *args: 300)
    monkeypatch.setattr(backend, '_source_snapshot', lambda *args: recipe.original_reference.input_source_sha256)
    monkeypatch.setattr(backend, '_recheck_source', lambda *args: None)
    monkeypatch.setattr(backend, '_write_plan', lambda *args: stage / 'plan.json')
    monkeypatch.setattr(backend, '_disk_budget', disk)
    monkeypatch.setattr(backend, '_make_export', lambda *args: stage / 'export')
    monkeypatch.setattr(backend, '_export_empty', lambda *args: None)
    monkeypatch.setattr(backend, '_quiesce', quiet)
    monkeypatch.setattr(backend, '_publish_export', publish)
    monkeypatch.setattr(backend, 'run_python_phase', lambda *args, **kwargs:
        PythonPhaseProcessResult(0, False, json.dumps(payload), '', False, False, 1000, 0))
    response = backend.execute_python_project_request(PythonProjectBrokerRequest(invocation=item),
        policy=BrokerPolicy(), configuration=configured, stage=stage)
    assert response.ok and response.environment_reference is not None
    assert ('published' in events) is (not reuse)
    assert events[-1] == 'cache-unlock'


@pytest.mark.parametrize('state', ['fresh', 'failed', 'ready'])
def test_new_allocation_guard_runs_before_mutation_but_never_for_ready(api: Any,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str) -> None:
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, state)
    before = dict(files)
    def deny() -> None:
        assert 'mkdir' not in events and files == before
        raise api.PythonProjectCacheError('cache_quota')
    if state == 'ready':
        with cache.reserve(invocation(), next_bytes=REQUESTED, adaptive=True,
                           fresh_check=deny, deadline=time.monotonic() + 2) as lease:
            assert lease.limits.max_bytes == REQUESTED
        assert 'measured' in events
    else:
        with (pytest.raises(api.PythonProjectCacheError, match='cache_quota'),
              cache.reserve(invocation(execution_id='new-attempt'), next_bytes=REQUESTED,
                            adaptive=True, fresh_check=deny, deadline=time.monotonic() + 2)):
            pytest.fail('fresh storage admission bypassed')
    assert files == before and 'mkdir' not in events


def test_actual_capacity_budget_reaches_root_export_and_real_lease_copy(api: Any,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Real reserve/publish methods; only native FD/scanner/copier boundaries are doubles."""
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    from tests.unit.skills.sandbox.test_python_project_staging import fixture

    recipe, item, _, _ = fixture()
    cache, files, _ = logical_cache(api, monkeypatch, tmp_path)
    observed_size = 102327500
    assert USED + REQUESTED + api._CONTROL_ALLOWANCE == 610703335 > cache.policy.max_bytes
    assert EFFECTIVE == cache.policy.max_bytes - USED - api._CONTROL_ALLOWANCE
    assert observed_size < EFFECTIVE and USED + observed_size + api._CONTROL_ALLOWANCE < cache.policy.max_bytes
    reached: list[tuple[str, int]] = []
    actual = SimpleNamespace(environment_sha256='a' * 64, security_manifest={'entries': ()})

    @contextmanager
    def members(*args: Any) -> Any:
        yield iter([SimpleNamespace(name='ready' if args[0] == 20 else 'environment')])

    def scan(fd: int, owner: int, limits: ScanLimits, deadline: float) -> dict[str, Any]:
        reached.append(('snapshot', limits.max_bytes))
        assert limits.max_bytes == EFFECTIVE
        return {}

    def observe(path: Path, input_sha: str, limits: ScanLimits, deadline: float) -> Any:
        assert input_sha == recipe.environment_input_sha256
        reached.append(('observe', limits.max_bytes))
        assert limits.max_bytes == EFFECTIVE
        return actual

    def copy(source: Path, destination: Path, **kwargs: Any) -> Any:
        reached.append(('copy', kwargs['limits'].max_bytes))
        assert kwargs['limits'].max_bytes == EFFECTIVE
        assert kwargs['environment_input_sha256'] == item.environment_input_sha256
        assert kwargs['expected_environment_sha256'] == actual.environment_sha256
        assert destination.name == 'environment' and 'environment' not in files
        files['environment'] = {'logical_size': observed_size}
        return actual

    def ready(*args: Any) -> Any:
        assert {'reservation.json', 'environment', 'reference.json'} <= files.keys()
        return SimpleNamespace(reference=PythonEnvironmentReference.model_validate_json(
            json.dumps(files['reference.json'])))

    monkeypatch.setattr(backend, '_open_directory', lambda parent, name, *args, **kwargs:
        {'export': 20, 'ready': 30, 'environment': 40}[name])
    monkeypatch.setattr(backend.os, 'scandir', members)
    monkeypatch.setattr(backend.os, 'stat', lambda *args, **kwargs: SimpleNamespace(st_uid=0))
    monkeypatch.setattr(backend, '_snapshot', scan)
    monkeypatch.setattr(backend, '_observe', observe)
    monkeypatch.setattr(backend, '_recipe_entries', lambda *args: None)
    monkeypatch.setattr(api, 'copy_python_project_environment', copy)
    monkeypatch.setattr(api, '_ready', ready)
    with cache.reserve(item, next_bytes=REQUESTED, adaptive=True,
                       minimum_bytes=1024, deadline=time.monotonic() + 2) as lease:
        assert lease.limits.max_bytes == EFFECTIVE
        job = _Execution(base_python_invocation(item), None, stage=tmp_path, launched=True, quiet=True)
        reference = backend._publish_export(lease, 1, tmp_path / 'export', recipe,
            SimpleNamespace(runtime_root=tmp_path), REQUESTED, 'a' * 64,
            time.monotonic() + 2, execution=job)
        assert reference.environment_sha256 == actual.environment_sha256
        assert files['reservation.json']['environment_limit_bytes'] == EFFECTIVE
    assert reached == [('snapshot', EFFECTIVE), ('observe', EFFECTIVE), ('copy', EFFECTIVE)]
    assert 'unpublished.json' not in files and 'failed.json' not in files
