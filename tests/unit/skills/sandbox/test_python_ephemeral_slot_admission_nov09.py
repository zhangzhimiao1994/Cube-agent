"""Exact slots pressure must not depend on a cache copy that will not be made.

Finite FD/cache doubles exercise real admission logic; no native-install claim.
"""
from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_hub.skills.sandbox import python_project_backend as backend
from agent_hub.skills.sandbox import python_project_cache as cache
from agent_hub.skills.sandbox.project_contract_broker import _Observed
from tests.unit.skills.sandbox.test_python_ephemeral_environment_nov08 import metadata_cache
from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload
from tests.unit.skills.sandbox.test_python_project_cache import invocation

FREE = 1_963_638_784
USED = 820_350_737
PRODUCER = 201_326_592
PRODUCT = 536_870_912
RESERVE = 1_073_741_824
MINIMUM = 92_684_832


def configuration() -> backend.RootConfig:
    payload = configuration_payload()
    payload['max_environment_bytes'] = PRODUCER
    payload['cache_policy'].update(max_bytes=RESERVE, max_entries=8,
        disk_reserve_bytes=RESERVE, retention_seconds=86400)
    return backend.RootConfig.from_payload(payload)


def admission_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, entries: int = 8,
    used: int = USED, free: int = FREE,
) -> tuple[cache.PythonProjectEnvironmentCache, backend._Roots, list[str]]:
    api = cache.PythonProjectEnvironmentCache(tmp_path, configuration().cache_policy)
    events: list[str] = []

    @contextmanager
    def locked(deadline: float, *, create: bool = True) -> Iterator[
        tuple[int, ExitStack, list[_Observed]] | None,
    ]:
        assert deadline > time.monotonic() and create is True
        events.append('lock')
        with ExitStack() as stack:
            try:
                yield 10, stack, []
            finally:
                events.append('unlock')

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail('admission refusal must not mutate or open a cache entry')

    monkeypatch.setattr(api, '_locked', locked)
    monkeypatch.setattr(api, '_catalog', lambda parent, deadline: {f'{i + 1:064x}' for i in range(entries)})
    monkeypatch.setattr(api, '_capacity', lambda parent, deadline: (used, free))
    monkeypatch.setattr(api, '_retired_scope', lambda parent, owner, deadline: False)
    monkeypatch.setattr(cache, '_open_directory', forbidden)
    monkeypatch.setattr(cache, '_write', forbidden)
    # Replace the module's os object, never process-global os.mkdir/fstat.
    monkeypatch.setattr(cache, 'os', SimpleNamespace(mkdir=forbidden))
    monkeypatch.setattr(backend, 'os', SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_dev=64769),
        fstatvfs=lambda fd: SimpleNamespace(f_bavail=free, f_frsize=1),
    ))
    return api, backend._Roots(stage_root_fd=9, stage_fd=10, cache_fd=11, pins=[]), events


def test_full_slots_reaches_precise_pressure_when_only_persistent_copy_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, roots, events = admission_fixture(tmp_path, monkeypatch)
    config = configuration()
    selected = backend._fresh_copy_budget(config, roots, PRODUCER, PRODUCER, product_bytes=PRODUCT)
    assert selected == 50_905_088 < MINIMUM
    backend._disk_budget(config, roots, PRODUCER, True, product_bytes=PRODUCT, cache_copy_bytes=0)

    def copy_budget(proposed: int) -> int:
        events.append('copy_budget')
        return backend._fresh_copy_budget(config, roots, PRODUCER, proposed, product_bytes=PRODUCT)

    with (pytest.raises(cache.PythonProjectCacheSlotsFull),
          api.reserve(invocation(), next_bytes=PRODUCER, adaptive=True,
            minimum_bytes=MINIMUM, fresh_budget=copy_budget, deadline=time.monotonic() + 3)):
        pytest.fail('full persistent cache granted a lease')
    assert events == ['lock', 'unlock']


@pytest.mark.parametrize('dimension', ['bytes', 'reserve', 'too_many_entries', 'no_install'])
def test_precise_slots_cannot_mask_other_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dimension: str,
) -> None:
    api, _, _ = admission_fixture(tmp_path, monkeypatch,
        used=RESERVE - MINIMUM if dimension == 'bytes' else USED,
        free=RESERVE + MINIMUM if dimension == 'reserve' else FREE,
        entries=9 if dimension == 'too_many_entries' else 8)
    item = invocation(install_allowed=dimension != 'no_install')
    with (pytest.raises(cache.PythonProjectCacheError) as failure,
          api.reserve(item, next_bytes=PRODUCER, adaptive=True,
            minimum_bytes=MINIMUM, deadline=time.monotonic() + 3)):
        pytest.fail('unsafe pressure granted a lease')
    assert type(failure.value) is not cache.PythonProjectCacheSlotsFull


def test_nonfull_cache_still_checks_actual_copy_budget_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, roots, events = admission_fixture(tmp_path, monkeypatch, entries=7)

    def copy_budget(proposed: int) -> int:
        events.append('copy_budget')
        return backend._fresh_copy_budget(configuration(), roots, PRODUCER, proposed, product_bytes=PRODUCT)

    with (pytest.raises(cache.PythonProjectCacheError, match='cache_quota') as failure,
          api.reserve(invocation(), next_bytes=PRODUCER, adaptive=True,
            minimum_bytes=MINIMUM, fresh_budget=copy_budget, deadline=time.monotonic() + 3)):
        pytest.fail('insufficient persistent copy granted a lease')
    assert type(failure.value) is not cache.PythonProjectCacheSlotsFull
    assert events == ['lock', 'copy_budget', 'unlock']


def test_full_slots_preserves_independent_fresh_check_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, events = admission_fixture(tmp_path, monkeypatch)

    def deny() -> None:
        events.append('fresh_check')
        raise cache.PythonProjectCacheError('cache_changed')

    with (pytest.raises(cache.PythonProjectCacheError, match='cache_changed'),
          api.reserve(invocation(), next_bytes=PRODUCER, adaptive=True,
            minimum_bytes=MINIMUM, fresh_check=deny, deadline=time.monotonic() + 3)):
        pytest.fail('independent fresh check bypassed')
    assert events == ['lock', 'fresh_check', 'unlock']


def test_ephemeral_producer_and_reserve_still_refuse_actual_shortfall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    floor = RESERVE + PRODUCT + backend._STAGE_CONTROL_ALLOWANCE + PRODUCER
    _, roots, _ = admission_fixture(tmp_path, monkeypatch, free=floor - 1)
    with pytest.raises(backend.PythonProjectBackendError, match='python_project_environment_quota'):
        backend._disk_budget(configuration(), roots, PRODUCER, True,
            product_bytes=PRODUCT, cache_copy_bytes=0)


@pytest.mark.parametrize('defect', [None, 'partial', 'bad_reference'])
def test_real_reserve_to_ephemeral_rechecks_all_eight_ready_owners_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    api, controls = metadata_cache(tmp_path, monkeypatch)
    before = copy.deepcopy(controls)
    original_lock = api._locked

    @contextmanager
    def locked(deadline: float, *, create: bool = True) -> Iterator[
        tuple[int, ExitStack, list[_Observed]] | None,
    ]:
        with original_lock(deadline, create=False) as state:
            yield state

    monkeypatch.setattr(api, '_locked', locked)
    monkeypatch.setattr(api, '_capacity', lambda parent, deadline: (USED, FREE))
    roots = backend._Roots(stage_root_fd=9, stage_fd=10, cache_fd=11, pins=[])
    monkeypatch.setattr(backend, 'os', SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_dev=64769),
        fstatvfs=lambda fd: SimpleNamespace(f_bavail=FREE, f_frsize=1),
    ))
    def copy_budget(proposed: int) -> int:
        return backend._fresh_copy_budget(configuration(), roots, PRODUCER, proposed, product_bytes=PRODUCT)

    with (pytest.raises(cache.PythonProjectCacheSlotsFull),
          api.reserve(invocation(), next_bytes=PRODUCER, adaptive=True,
            minimum_bytes=MINIMUM, fresh_budget=copy_budget, deadline=time.monotonic() + 3)):
        pytest.fail('full cache granted persistent storage')
    assert controls == before
    if defect == 'partial':
        monkeypatch.setattr(cache, '_names', lambda parent, maximum, deadline: {'reservation.json'})
    elif defect == 'bad_reference':
        controls[2]['reference.json']['recipe_sha256'] = '0' * 64
    expected = copy.deepcopy(controls)
    if defect is not None:
        with (pytest.raises(cache.PythonProjectCacheError),
              api.ephemeral(invocation(), next_bytes=PRODUCER,
                minimum_bytes=MINIMUM, deadline=time.monotonic() + 3)):
            pytest.fail('unproven READY owner enabled ephemeral execution')
    else:
        with api.ephemeral(invocation(), next_bytes=PRODUCER,
                minimum_bytes=MINIMUM, deadline=time.monotonic() + 3) as limits:
            assert limits.max_bytes == PRODUCER
            backend._disk_budget(configuration(), roots, PRODUCER, True,
                product_bytes=PRODUCT, cache_copy_bytes=0)
    assert controls == expected
