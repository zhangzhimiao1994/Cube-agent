"""Real admission/cache logic with finite FD doubles; no native execution claim."""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.skills.sandbox.test_python_project_adaptive_capacity_oct28 import logical_cache
from tests.unit.skills.sandbox.test_python_project_backend import (
    configuration_payload,
    expected_reference,
    report,
    stubbed_context,
)
from tests.unit.skills.sandbox.test_python_project_cache import invocation

FREE = 2084413440
PRODUCER = 201326592
WORKSPACE = 536870912
RESERVE = 1073741824
MINIMUM = 92684832
COPY = 171679744  # free - reserve - workspace - 96MiB/64KiB - producer - 64KiB


def configured(backend: Any) -> Any:
    payload = configuration_payload()
    payload['max_environment_bytes'] = PRODUCER
    payload['cache_policy'].update(max_bytes=RESERVE, max_entries=8,
                                   disk_reserve_bytes=RESERVE, retention_seconds=86400)
    return backend.RootConfig.from_payload(payload)


def disk_boundary(backend: Any, monkeypatch: pytest.MonkeyPatch, *,
                  same: bool = True, stage: int = FREE, cache: int = FREE) -> tuple[Any, dict[int, int]]:
    free = {10: stage, 11: cache}
    monkeypatch.setattr(backend, 'os', SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_dev=1 if same or fd == 10 else 2),
        fstatvfs=lambda fd: SimpleNamespace(f_bavail=free[fd], f_frsize=1),
    ))
    return SimpleNamespace(stage_fd=10, cache_fd=11, pins=[]), free


@pytest.mark.parametrize('available,want', [(FREE, COPY), (2083880960, 171147264)])
def test_measured_full_producer_two_copy_gate_rejects_but_smaller_copy_fits(
    monkeypatch: pytest.MonkeyPatch, available: int, want: int,
) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    roots, free = disk_boundary(backend, monkeypatch, stage=available, cache=available)
    config = configured(backend)
    with pytest.raises(backend.PythonProjectBackendError, match='python_project_environment_quota'):
        backend._disk_budget(config, roots, PRODUCER, True, product_bytes=WORKSPACE)
    selected = backend._fresh_copy_budget(config, roots, PRODUCER, PRODUCER,
                                           product_bytes=WORKSPACE)
    assert selected == want and selected >= MINIMUM
    # Reservation/control writes happen after selection. The full producer ceiling
    # must still fit alongside the bounded cache copy after those writes.
    free[10] -= 65536
    free[11] -= 65536
    backend._disk_budget(config, roots, PRODUCER, True, product_bytes=WORKSPACE,
                         cache_copy_bytes=selected)
    free[10] -= 1
    with pytest.raises(backend.PythonProjectBackendError, match='python_project_environment_quota'):
        backend._disk_budget(config, roots, PRODUCER, True, product_bytes=WORKSPACE,
                             cache_copy_bytes=selected)


@pytest.mark.parametrize('stage,cache,want', [
    (FREE, FREE, PRODUCER),
    (FREE, RESERVE + MINIMUM + 131072, MINIMUM),
])
def test_separate_filesystems_do_not_charge_cache_copy_to_stage(
    monkeypatch: pytest.MonkeyPatch, stage: int, cache: int, want: int,
) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    roots, free = disk_boundary(backend, monkeypatch, same=False, stage=stage, cache=cache)
    config = configured(backend)
    assert backend._fresh_copy_budget(config, roots, PRODUCER, PRODUCER,
                                      product_bytes=WORKSPACE) == want
    free[11] -= 65536
    backend._disk_budget(config, roots, PRODUCER, True, product_bytes=WORKSPACE,
                         cache_copy_bytes=want)


def test_separate_stage_cannot_shrink_producer_to_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    roots, _ = disk_boundary(backend, monkeypatch, same=False,
        stage=RESERVE + WORKSPACE + 100728832 + PRODUCER - 1)
    with pytest.raises(backend.PythonProjectBackendError, match='python_project_environment_quota'):
        backend._fresh_copy_budget(configured(backend), roots, PRODUCER, MINIMUM,
                                   product_bytes=WORKSPACE)


@pytest.mark.parametrize('state', ['fresh', 'failed', 'ready'])
def test_callback_is_under_lock_before_mutation_and_skips_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str,
) -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, state)
    before = dict(files)

    def select(proposed: int) -> int:
        assert events.count('lock') == events.count('unlock') + 1
        assert files == before and 'mkdir' not in events
        assert proposed == 127494169  # original cache headroom clamp remains first
        return MINIMUM

    def check() -> None:
        assert files == before and 'mkdir' not in events

    with cache.reserve(invocation(execution_id='new-attempt'), next_bytes=PRODUCER,
                       adaptive=True, minimum_bytes=MINIMUM, fresh_budget=select,
                       fresh_check=check, deadline=time.monotonic() + 2) as lease:
        assert lease.limits.max_bytes == (PRODUCER if state == 'ready' else MINIMUM)
    if state == 'ready':
        assert files == before and 'mkdir' not in events
    else:
        assert files['reservation.json']['environment_limit_bytes'] == MINIMUM


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '100', None, MINIMUM - 1, 127494170])
def test_invalid_fresh_budget_never_creates_or_archives_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: Any,
) -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, 'failed')
    before = dict(files)
    with (pytest.raises(api.PythonProjectCacheError, match='cache_quota'),
          cache.reserve(invocation(execution_id='new-attempt'), next_bytes=PRODUCER,
                        adaptive=True, minimum_bytes=MINIMUM, fresh_budget=lambda _: value,
                        deadline=time.monotonic() + 2)):
        pytest.fail('invalid callback granted a lease')
    assert files == before and 'mkdir' not in events


def test_insufficient_shared_disk_rejects_before_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    cache, files, events = logical_cache(api, monkeypatch, tmp_path)
    roots, _ = disk_boundary(backend, monkeypatch,
        stage=RESERVE + WORKSPACE + 100728832 + PRODUCER + MINIMUM + 65535)
    with (pytest.raises(api.PythonProjectCacheError, match='cache_quota'),
          cache.reserve(invocation(), next_bytes=PRODUCER, adaptive=True,
            minimum_bytes=MINIMUM, fresh_budget=lambda proposed: backend._fresh_copy_budget(
                configured(backend), roots, PRODUCER, proposed, product_bytes=WORKSPACE),
            deadline=time.monotonic() + 2)):
        pytest.fail('below interpreter floor admitted')
    assert files == {} and 'mkdir' not in events


@pytest.mark.parametrize('state', ['fresh', 'failed'])
def test_callback_exception_preserves_consumed_files_and_prevents_new_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str,
) -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, state)
    before = dict(files)

    def unavailable(_: int) -> int:
        raise api.PythonProjectCacheError('cache_timeout')

    with (pytest.raises(api.PythonProjectCacheError, match='cache_timeout'),
          cache.reserve(invocation(execution_id='new-attempt'), next_bytes=PRODUCER,
                        adaptive=True, fresh_budget=unavailable, deadline=time.monotonic() + 2)):
        pytest.fail('callback failure ignored')
    assert files == before and 'mkdir' not in events


def test_unknown_never_calls_budget_or_legacy_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    cache, files, events = logical_cache(api, monkeypatch, tmp_path, 'unknown')
    before = dict(files)

    def forbidden(*args: Any) -> Any:
        pytest.fail('UNKNOWN reached fresh admission callback')

    with (pytest.raises(api.PythonProjectCacheError, match='cache_incomplete'),
          cache.reserve(invocation(execution_id='new-attempt'), next_bytes=PRODUCER,
                        adaptive=True, fresh_budget=forbidden, fresh_check=forbidden,
                        deadline=time.monotonic() + 2)):
        pytest.fail('UNKNOWN reused')
    assert files == before and 'mkdir' not in events


@pytest.mark.parametrize('bad', [True, 12, 'callback'])
def test_noncallable_budget_rejected_before_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: Any,
) -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    cache, files, events = logical_cache(api, monkeypatch, tmp_path)
    with (pytest.raises(api.PythonProjectCacheError, match='cache_invalid'),
          cache.reserve(invocation(), next_bytes=PRODUCER, fresh_budget=bad,
                        deadline=time.monotonic() + 2)):
        pytest.fail('noncallable accepted')
    assert files == {} and events == []


def test_real_run_and_reserve_keep_producer_and_publish_with_measured_copy_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_cache')
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerRequest

    original_budget = backend._disk_budget
    recipe, item, _, stage, _ = stubbed_context(backend, monkeypatch, tmp_path)
    item = item.model_copy(update={'workspace_limit_bytes': WORKSPACE})
    cache, files, _ = logical_cache(api, monkeypatch, tmp_path)
    cache.policy = configured(backend).cache_policy
    monkeypatch.setattr(cache, '_capacity', lambda *args: (717446944, FREE))
    roots, free = disk_boundary(backend, monkeypatch)
    monkeypatch.setattr(backend, '_disk_budget', original_budget)
    monkeypatch.setattr(backend, 'PythonProjectEnvironmentCache', lambda *args: cache)
    monkeypatch.setattr(backend, '_minimum_environment_bytes', lambda *args: MINIMUM)
    monkeypatch.setattr(backend, '_source_snapshot', lambda *args:
                        recipe.original_reference.input_source_sha256)
    monkeypatch.setattr(backend, '_recheck_source', lambda *args: None)
    monkeypatch.setattr(backend, '_write_plan', lambda *args: stage / 'plan.json')
    monkeypatch.setattr(backend, '_make_export', lambda *args: stage / 'export')
    monkeypatch.setattr(backend, '_quiesce', lambda _, job: setattr(job, 'quiet', True))
    monkeypatch.setattr(backend, 'run_python_phase', lambda *args, **kwargs:
        PythonPhaseProcessResult(0, False, json.dumps(report(item, recipe)), '', False, False, 1000, 0))
    published = []

    def publish(lease: Any, *args: Any, **kwargs: Any) -> Any:
        assert lease.limits.max_bytes == COPY
        assert args[4] == COPY and kwargs['execution'].quiet is True
        published.append(True)
        return expected_reference(item)

    # The real reserve writes its reservation before yielding to the real _run.
    write = api._write

    def reservation_write(*args: Any) -> None:
        write(*args)
        free[10] -= 4096
        free[11] -= 4096

    monkeypatch.setattr(api, '_write', reservation_write)
    monkeypatch.setattr(backend, '_publish_export', publish)
    response = backend._run(PythonProjectBrokerRequest(invocation=item), BrokerPolicy(),
                            configured(backend), stage, roots, time.monotonic() + 5)
    assert response.ok and published == [True]
    assert files['reservation.json']['environment_limit_bytes'] == COPY
