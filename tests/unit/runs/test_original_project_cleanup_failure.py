"""Original native cleanup uncertainty must survive the real pre-model chain."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.skills.sandbox import project
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerResponse
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.runs.test_project_input_lifecycle import ObservingRuntime, route
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository

CLEANUP_CODE = 'project_termination_cleanup_incomplete'


async def test_systemd_cleanup_response_preserves_typed_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    request = AsyncMock(return_value=BrokerResponse(ok=False, error=CLEANUP_CODE))
    monkeypatch.setattr(project, '_systemd_supported', lambda: True)
    monkeypatch.setattr(project, 'request_project_broker', request)
    invocation = project.ProjectInvocation(execution_id='original_cleanup', tenant_id=uuid4(), run_id=uuid4(),
        project_id='project-a', session_id='session-a', sandbox_profile='workspace_write', timeout_seconds=30)
    with pytest.raises(ProcessCleanupIncomplete, match=f'^{CLEANUP_CODE}$'):
        await project.ProjectSystemdSandbox().run(invocation)
    assert request.await_count == 1  # No retry or native process; the broker boundary is a fixture.


@pytest.mark.parametrize('failure', [
    'cleanup_response', 'cleanup_exception', 'broker_rejection', 'prefixed_cleanup_text',
    'suffixed_cleanup_text', 'runtime_error_text', 'completed_process_failure',
])
async def test_original_cleanup_chain_only_retains_lease_for_typed_uncertainty(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    repository = ExecutableFakeRepository(routing_decision=route())
    calls: list[str] = []
    errors: list[Exception] = []
    files = {path: 'a' * 64 for path in ('package.json', 'src/index.js', 'tests/original.js')}
    source = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    verifier = ProjectVerificationService(root=Path('unused-original-source'), repository=repository,
                                          executor=project.ProjectSystemdSandbox())
    cleanup = ProcessCleanupIncomplete(CLEANUP_CODE)
    request = AsyncMock(return_value=BrokerResponse(ok=False, error=CLEANUP_CODE))
    if failure == 'cleanup_exception':
        request.side_effect = cleanup
    elif failure == 'broker_rejection':
        request.return_value = BrokerResponse(ok=False, error='project execution unavailable')
    elif failure == 'prefixed_cleanup_text':
        request.return_value = BrokerResponse(ok=False, error='untrusted ' + CLEANUP_CODE)
    elif failure == 'suffixed_cleanup_text':
        request.return_value = BrokerResponse(ok=False, error=CLEANUP_CODE + ' untrusted')
    elif failure == 'runtime_error_text':
        request.side_effect = RuntimeError(CLEANUP_CODE)
    elif failure == 'completed_process_failure':
        request.return_value = BrokerResponse(ok=True,
            result=SkillResult(exit_code=1, timed_out=False, stdout='', stderr='fixture failed'))
    # Only I/O boundaries are substituted; sandbox, verifier, service and lease branches are real.
    monkeypatch.setattr(project, '_systemd_supported', lambda: True)
    monkeypatch.setattr(project, 'request_project_broker', request)
    monkeypatch.setattr(project, 'project_source_manifest', AsyncMock(return_value=files))
    monkeypatch.setattr(project, 'project_source_fingerprint', AsyncMock(return_value=source))

    async def prepare(context: TaskContext) -> TaskContext:
        contract = build_project_check_contract(tenant_id=context.tenant_id, run_id=context.run_id,
            project_id='project-a', session_id='session-a', input_source_sha256=source,
            editable_paths=('src',), source_files=files)
        reference = ProjectBaselineReference('b' * 64, source, len(files))
        stored = StoredProjectBaseline(reference, contract, Path('unused-original-source'), files)
        invocation = project.ProjectInvocation(execution_id='original_cleanup', tenant_id=context.tenant_id,
            run_id=context.run_id, project_id='project-a', session_id='session-a',
            sandbox_profile='workspace_write', install_allowed=True, timeout_seconds=30)
        assert repository.row.routing_decision is not None
        repository.row.routing_decision['project_input_preparation'] = {
            'schema_version': 1, 'phase': 'executing', 'reference': reference.to_payload(),
            'execution_id': invocation.execution_id,
        }
        try:
            await verifier._run_original_contract(invocation, stored, original=True)
        except Exception as error:
            errors.append(error)
            raise
        raise AssertionError('native failure cannot produce a prepared context')

    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, calls),)),
        router=None, task_queue=cast(TaskQueue, object()), project_input_preparer=prepare)
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.FAILED and calls == []
    assert request.await_count == 1 and len(errors) == 1
    assert verifier._receipts == {} and verifier._original_receipts == {}
    assert repository.row.routing_decision is not None
    state = repository.row.routing_decision['project_input_preparation']
    assert isinstance(state, Mapping) and state.get('phase') == 'executing'
    if failure in {'cleanup_response', 'cleanup_exception'}:
        assert repository.row.worker_id is not None
        assert repository.row.worker_lease_token is not None
        assert repository.row.worker_lease_expires_at is not None
        assert repository.row.worker_heartbeat_at is None
        assert isinstance(errors[0], ProcessCleanupIncomplete)
        if failure == 'cleanup_exception':
            assert errors[0] is cleanup
    else:
        assert isinstance(errors[0], ProjectVerificationError)
        assert repository.row.worker_id is None
        assert repository.row.worker_lease_token is None
        assert repository.row.worker_lease_expires_at is None
        assert repository.row.worker_heartbeat_at is None
