"""Opt-in evidence and exact pressure; portable units, never native acceptance."""
from __future__ import annotations

import copy
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox import python_project_cache as cache
from agent_hub.skills.sandbox import python_project_protocol as protocol
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project_contract_broker import _Observed
from tests.unit.skills.sandbox.test_python_project_cache import invocation


def evidence_payload() -> dict[str, object]:
    item = invocation()
    return {
        'schema_version': 1, 'storage': 'ephemeral_current_execution',
        'scope': {key: value for key, value in item.model_dump().items()
                  if key in {'tenant_id', 'run_id', 'project_id', 'session_id', 'original_reference'}},
        'execution_id': item.execution_id, 'recipe_sha256': item.recipe_sha256,
        'environment_input_sha256': item.environment_input_sha256,
        'environment_sha256': 'e' * 64, 'source_sha256': 'b' * 64,
        'interpreter_sha256': 'f' * 64, 'environment_bytes': 96 * 1024**2,
        'unit_quiet': True, 'environment_removed': True, 'cache_written': False,
    }


def test_opt_in_evidence_is_not_a_persistent_cache_reference() -> None:
    evidence = protocol.PythonEphemeralEnvironmentEvidence.model_validate(evidence_payload())
    evidence.require_for(invocation())
    with pytest.raises(ValueError):
        protocol.PythonEnvironmentReference.model_validate(evidence.model_dump())


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', 2), ('unit_quiet', 1),
    ('unit_quiet', False), ('environment_removed', False), ('environment_removed', 1),
    ('cache_written', True), ('cache_written', 0), ('environment_bytes', True),
    ('environment_bytes', 0), ('environment_bytes', -1), ('source_sha256', 'bad'),
    ('storage', 'persistent_cache'), ('host_path', '/tmp/environment'),
])
def test_evidence_rejects_unproven_cleanup_or_untyped_controls(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        protocol.PythonEphemeralEnvironmentEvidence.model_validate({**evidence_payload(), field: value})


@pytest.mark.parametrize('change', [
    {'execution_id': 'other'}, {'recipe_sha256': '1' * 64},
    {'environment_input_sha256': '2' * 64}, {'project_id': 'foreign'},
    {'session_id': 'foreign'}, {'workspace_limit_bytes': 1}, {'install_allowed': False},
])
def test_evidence_requires_same_authorized_execution(change: dict[str, object]) -> None:
    evidence = protocol.PythonEphemeralEnvironmentEvidence.model_validate(evidence_payload())
    with pytest.raises(ValueError):
        evidence.require_for(invocation(**change))


@pytest.mark.parametrize('field,value', [('unit_quiet', False), ('environment_removed', False),
                                        ('cache_written', True), ('schema_version', True)])
def test_constructed_evidence_cannot_bypass_require_for(field: str, value: object) -> None:
    evidence = protocol.PythonEphemeralEnvironmentEvidence.model_validate(evidence_payload())
    with pytest.raises(ValueError):
        evidence.model_copy(update={field: value}).require_for(invocation())


@pytest.mark.parametrize('value', [True, False, 0, 2, '1', 1.0])
def test_ephemeral_request_version_does_not_coerce(value: object) -> None:
    with pytest.raises(ValueError):
        protocol.PythonProjectBrokerRequest.model_validate({
            'invocation': invocation().model_dump(), 'ephemeral_environment_version': value})


def test_original_request_retains_persistent_default() -> None:
    old = protocol.PythonProjectBrokerRequest(invocation=invocation())
    assert old.ephemeral_environment_version is None


def test_response_requires_opt_in_at_actual_request_boundary() -> None:
    evidence = protocol.PythonEphemeralEnvironmentEvidence.model_validate(evidence_payload())
    response = protocol.PythonProjectBrokerResponse(action='run_python_project', execution_id='original',
        ok=True, result=SkillResult(exit_code=0, stdout='', stderr='', timed_out=False),
        ephemeral_environment=evidence)
    with pytest.raises(ValueError):
        response.require_for(protocol.PythonProjectBrokerRequest(invocation=invocation()))
    response.require_for(protocol.PythonProjectBrokerRequest(invocation=invocation(),
        ephemeral_environment_version=1))


@pytest.mark.parametrize('action,ok', [
    ('probe_python_project', True), ('inspect_python_project_environment', True),
    ('release_python_project_environment', True), ('release_expired_python_project_environment', True),
    ('maintain_expired_python_failure', True), ('run_python_project', False),
])
def test_non_run_and_failures_cannot_issue_ephemeral_evidence(action: str, ok: bool) -> None:
    with pytest.raises(ValueError):
        protocol.PythonProjectBrokerResponse.model_validate({
            'action': action, 'execution_id': 'original', 'ok': ok,
            'error': None if ok else 'python_project_environment_quota',
            'ephemeral_environment': evidence_payload()})


def test_mixed_persistent_and_ephemeral_proof_is_rejected() -> None:
    from tests.unit.skills.sandbox.test_python_project_client import reference

    with pytest.raises(ValueError):
        protocol.PythonProjectBrokerResponse.model_validate({
            'action': 'run_python_project', 'execution_id': 'original', 'ok': True,
            'result': {'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False},
            'environment_reference': reference().model_dump(), 'ephemeral_environment': evidence_payload()})


def test_only_full_slots_with_other_capacity_available_has_precise_pressure() -> None:
    policy = cache.PythonProjectCachePolicy(1024**2, 8, 1024**2, 3600)
    with pytest.raises(cache.PythonProjectCacheSlotsFull):
        policy.require_capacity(used_bytes=100, entries=8, next_bytes=100, free_bytes=4 * 1024**2)


@pytest.mark.parametrize('dimension', ['bytes', 'disk', 'invalid_entries'])
def test_slot_full_does_not_hide_other_capacity_refusals(dimension: str) -> None:
    policy = cache.PythonProjectCachePolicy(1024**2, 8, 1024**2, 3600)
    values = {'used_bytes': 100, 'entries': 8, 'next_bytes': 100, 'free_bytes': 4 * 1024**2}
    values.update({'bytes': {'used_bytes': 1024**2 - 1}, 'disk': {'free_bytes': 1024**2},
                   'invalid_entries': {'entries': 9}}[dimension])
    with pytest.raises(cache.PythonProjectCacheError) as error:
        policy.require_capacity(**values)
    assert not isinstance(error.value, cache.PythonProjectCacheSlotsFull)


def test_plan_ephemeral_storage_cannot_reuse_cache() -> None:
    from agent_hub.skills.sandbox.python_project_runner import PythonProjectRunnerPlan
    from tests.unit.skills.sandbox.test_python_project_runner import plan_payload
    selected: Literal['ephemeral_current_execution'] = 'ephemeral_current_execution'
    plan = PythonProjectRunnerPlan.model_validate({**plan_payload(), 'environment_storage': selected})
    assert plan.environment_storage == selected
    with pytest.raises(ValueError):
        PythonProjectRunnerPlan.model_validate({**plan_payload(reuse=True), 'environment_storage': selected})


def metadata_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[
    cache.PythonProjectEnvironmentCache, dict[int, dict[str, dict[str, object]]],
]:
    """Only directory IO is doubled; real reservation/reference validators remain."""
    api = cache.PythonProjectEnvironmentCache(tmp_path,
        cache.PythonProjectCachePolicy(1024**3, 8, 1024**3, 3600))
    controls: dict[int, dict[str, dict[str, object]]] = {}
    keys: dict[str, int] = {}
    for index in range(8):
        item = invocation(run_id=UUID(int=index + 10), execution_id=f'old_{index}')
        key = cache.python_project_cache_key(item)
        scope = protocol._scope_payload(item)
        reference = protocol.PythonEnvironmentReference(scope=protocol.PythonProjectScope.model_validate(scope),
            recipe_sha256=item.recipe_sha256, environment_input_sha256=item.environment_input_sha256,
            environment_sha256='e' * 64, security_manifest_sha256='f' * 64,
            origin_execution_id=item.execution_id)
        keys[key] = index + 2
        controls[index + 2] = {
            'reservation.json': {'schema_version': 1, 'scope': reference.scope.model_dump(mode='json'),
                'recipe_sha256': item.recipe_sha256, 'environment_input_sha256': item.environment_input_sha256,
                'origin_execution_id': item.execution_id, 'expires_at': 1, 'environment_limit_bytes': 1024**2},
            'reference.json': reference.model_dump(mode='json'),
        }

    @contextmanager
    def locked(deadline: float, *, create: bool = True) -> Iterator[
        tuple[int, ExitStack, list[_Observed]] | None,
    ]:
        assert create is False
        with ExitStack() as stack:
            yield 1, stack, []

    def directory(parent: int, name: str, owner: int, stack: ExitStack,
                  pins: list[_Observed], *, mode: int) -> int:
        assert parent == 1 and owner == 0 and mode == 0o700
        return keys[name]

    def read(parent: int, name: str, pins: list[_Observed]) -> dict[str, object]:
        return controls[parent][name]

    def no_write(*args: object, **kwargs: object) -> None:
        pytest.fail('ephemeral admission wrote the persistent cache')

    def ready(parent: int, path: Path, reservation: dict[str, object], limits: ScanLimits,
              stack: ExitStack, pins: list[_Observed], deadline: float) -> cache._Ready:
        # Deep environment scan is a privileged IO boundary, validated by native fixtures.
        if cache._entry_members(parent, reservation, pins, deadline) != cache._READY:
            raise cache.PythonProjectCacheError('cache_incomplete')
        reference = cache._reference(read(parent, 'reference.json', pins))
        cache._bind_reference(reference, reservation)
        return cache._Ready(reference, 0, {})

    monkeypatch.setattr(api, '_locked', locked)
    monkeypatch.setattr(api, '_catalog', lambda parent, deadline: set(keys))
    monkeypatch.setattr(api, '_capacity', lambda parent, deadline: (800 * 1024**2, 4 * 1024**3))
    monkeypatch.setattr(cache, '_open_directory', directory)
    monkeypatch.setattr(cache, '_read', read)
    monkeypatch.setattr(cache, '_ready', ready)
    monkeypatch.setattr(cache, '_names', lambda parent, maximum, deadline: set(cache._READY))
    monkeypatch.setattr(os, 'mkdir', no_write)
    monkeypatch.setattr(cache, '_write', no_write)
    return api, controls


def test_ephemeral_locked_admission_keeps_all_eight_entries_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controls = metadata_cache(tmp_path, monkeypatch)
    before = copy.deepcopy(controls)
    with api.ephemeral(invocation(), next_bytes=192 * 1024**2,
                       minimum_bytes=92 * 1024**2, deadline=time.monotonic() + 5) as limits:
        assert limits.max_bytes == 192 * 1024**2
        assert controls == before
    assert controls == before


@pytest.mark.parametrize('defect', ['not_full', 'existing', 'not_approved', 'partial', 'bad_reference', 'bytes'])
def test_ephemeral_admission_requires_only_slots_to_be_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    api, controls = metadata_cache(tmp_path, monkeypatch)
    item = invocation()
    if defect == 'not_full':
        monkeypatch.setattr(api, '_catalog', lambda parent, deadline: set())
    elif defect == 'existing':
        monkeypatch.setattr(api, '_catalog', lambda parent, deadline: {cache.python_project_cache_key(item)})
    elif defect == 'not_approved':
        item = invocation(install_allowed=False)
    elif defect == 'partial':
        monkeypatch.setattr(cache, '_names', lambda parent, maximum, deadline: {'reservation.json'})
    elif defect == 'bad_reference':
        controls[2]['reference.json']['environment_input_sha256'] = '0' * 64
    else:
        monkeypatch.setattr(api, '_capacity', lambda parent, deadline: (1024**3 - 100, 4 * 1024**3))
    with (pytest.raises(cache.PythonProjectCacheError),
          api.ephemeral(item, next_bytes=192 * 1024**2,
                        minimum_bytes=92 * 1024**2, deadline=time.monotonic() + 5)):
        pytest.fail('invalid cache pressure admitted a temporary execution')
