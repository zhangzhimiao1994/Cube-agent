"""Real Root handler/report flow with privileged IO doubled, not native proof."""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox import python_project_backend as backend
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import _Execution
from agent_hub.skills.sandbox.python_project_cache import (
    PythonProjectCacheError,
    PythonProjectCacheSlotsFull,
)
from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEphemeralEnvironmentEvidence,
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
    PythonProjectScope,
    _scope_payload,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox.test_python_project_backend import (
    configuration_payload,
    report,
    stubbed_context,
)


@pytest.mark.parametrize('failure', [None, 'install', 'build', 'test', 'quiet', 'remove', 'bytes', 'old-client',
                                     'phase-timeout', 'phase-exitless', 'phase-malformed'])
def test_full_slot_execution_runs_once_without_publishing_and_never_forges_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    recipe, item, _, stage, events = stubbed_context(backend, monkeypatch, tmp_path)
    invocation = PythonProjectInvocation.model_validate(item.model_dump())
    config = backend.RootConfig.from_payload(configuration_payload())
    payload = report(invocation, recipe, failed='test' if failure in {'phase-timeout', 'phase-exitless', 'phase-malformed'}
                     else failure if failure in {'install', 'build', 'test'} else None)
    if failure == 'phase-timeout':
        payload['checks'][-1]['timed_out'] = True
    elif failure in {'phase-exitless', 'phase-malformed'}:
        payload['checks'][-1]['exit_code'] = None if failure == 'phase-exitless' else 0

    class Cache:
        @contextmanager
        def reserve(self, actual: PythonProjectInvocation, **kwargs: object) -> Iterator[None]:
            assert actual == invocation
            if failure == 'bytes':
                raise PythonProjectCacheError('cache_quota')
            raise PythonProjectCacheSlotsFull()
            yield

        @contextmanager
        def ephemeral(self, actual: PythonProjectInvocation, *, next_bytes: int,
                      minimum_bytes: int, deadline: float) -> Iterator[ScanLimits]:
            assert actual == invocation and next_bytes == config.max_environment_bytes
            assert failure not in {'bytes', 'old-client'}
            events.append('ephemeral_lock')
            yield ScanLimits(next_bytes)
            events.append('ephemeral_unlock')

    def quiet(policy: BrokerPolicy, job: _Execution) -> None:
        events.append('quiet')
        if failure == 'quiet':
            raise ProcessCleanupIncomplete('python_project_cleanup_incomplete')
        job.quiet = True

    def consume(parent: int, export: Path, inputs: object, configuration: backend.RootConfig,
                budget: int, reported_sha: str, deadline: float, *, execution: _Execution,
                source_sha256: str, product: bool = False) -> PythonEphemeralEnvironmentEvidence:
        assert execution.quiet and 'quiet' in events
        assert configuration == config and budget == config.max_environment_bytes
        events.append('remove_verified_export')
        if failure == 'remove':
            raise ProcessCleanupIncomplete('python_project_cleanup_incomplete')
        return PythonEphemeralEnvironmentEvidence(scope=PythonProjectScope.model_validate(_scope_payload(invocation)),
            execution_id=invocation.execution_id, recipe_sha256=invocation.recipe_sha256,
            environment_input_sha256=invocation.environment_input_sha256,
            environment_sha256=reported_sha, source_sha256=source_sha256,
            interpreter_sha256=config.interpreter.binary_sha256, environment_bytes=96 * 1024**2,
            unit_quiet=True, environment_removed=True, cache_written=False)

    def phase(*args: object, **kwargs: object) -> PythonPhaseProcessResult:
        events.append('launch')
        return PythonPhaseProcessResult(0, False, json.dumps(payload), '', False, False, 1000, 0)

    def no_publish(*args: object, **kwargs: object) -> None:
        pytest.fail('temporary execution attempted persistent cache publication')

    monkeypatch.setattr(backend, 'PythonProjectEnvironmentCache', lambda *args: Cache())
    monkeypatch.setattr(backend, '_source_snapshot', lambda *args: recipe.original_reference.input_source_sha256)
    monkeypatch.setattr(backend, '_recheck_source', lambda *args: None)
    monkeypatch.setattr(backend, '_write_plan', lambda *args: stage / 'plan.json')
    monkeypatch.setattr(backend, '_make_export', lambda *args: stage / 'export')
    monkeypatch.setattr(backend, '_quiesce', quiet)
    monkeypatch.setattr(backend, '_consume_ephemeral_export', consume)
    monkeypatch.setattr(backend, '_publish_export', no_publish)
    monkeypatch.setattr(backend, '_export_empty', lambda *args: events.append('empty_export'))
    monkeypatch.setattr(backend, 'run_python_phase', phase)
    request = PythonProjectBrokerRequest(invocation=invocation,
        ephemeral_environment_version=None if failure == 'old-client' else 1)
    if failure in {'quiet', 'remove'}:
        with pytest.raises(ProcessCleanupIncomplete):
            backend.execute_python_project_request(request, policy=BrokerPolicy(), configuration=config, stage=stage)
        return
    response = backend.execute_python_project_request(request, policy=BrokerPolicy(), configuration=config, stage=stage)
    assert isinstance(response, PythonProjectBrokerResponse)
    if failure in {'bytes', 'old-client'}:
        assert not response.ok and 'launch' not in events and 'ephemeral_lock' not in events
        return
    if failure in {'phase-timeout', 'phase-exitless', 'phase-malformed'}:
        assert not response.ok and response.ephemeral_environment is None
        assert 'remove_verified_export' not in events
        return
    assert response.ok and response.result is not None and response.environment_reference is None
    assert json.loads(response.result.stdout)['status'] == ('failed' if failure else 'passed')
    assert (response.ephemeral_environment is not None) == (failure != 'install')
    assert events.count('launch') == 1 and events.index('launch') < events.index('quiet')
    assert ('remove_verified_export' in events) == (failure != 'install')
    assert events[-1] == 'ephemeral_unlock'


@pytest.mark.parametrize('defect', ['quiet', 'not_launched', 'wrong_stage', 'source', 'budget', 'interpreter'])
def test_export_consumption_rejects_missing_native_authority_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation
    from tests.unit.skills.sandbox.test_python_project_staging import fixture

    inputs, item, _, _ = fixture()
    settings = configuration_payload()
    if defect == 'interpreter':
        settings['interpreter'] = {**settings['interpreter'], 'binary_sha256': '0' * 64}
    config = backend.RootConfig.from_payload(settings)
    job = _Execution(base_python_invocation(item), None, stage=tmp_path,
                     launched=defect != 'not_launched', quiet=defect != 'quiet',
                     source_sha256=inputs.original_reference.input_source_sha256)
    def no_io(*args: object, **kwargs: object) -> None:
        pytest.fail('unproven temporary environment reached filesystem')
    monkeypatch.setattr(backend, '_open_directory', no_io)
    with pytest.raises(backend.PythonProjectBackendError):
        backend._consume_ephemeral_export(1, tmp_path / ('elsewhere' if defect == 'wrong_stage' else 'export'),
            inputs, config, 0 if defect == 'budget' else config.max_environment_bytes, 'e' * 64, 9999999999.0,
            execution=job, source_sha256='0' * 64 if defect == 'source' else inputs.original_reference.input_source_sha256)
