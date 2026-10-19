from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
    _validate_report,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, FakeTransaction


class Authorizer:
    def __init__(self, calls: list[str], *, allowed: bool = True) -> None:
        self.calls, self.allowed = calls, allowed

    async def authorize(self, tenant_id: Any, request: Any, **kwargs: Any) -> HarnessToolCallResult:
        self.calls.append('authorize')
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='succeeded' if self.allowed else 'failed',
            payload={'authorized': self.allowed}, failure_reason=None if self.allowed else 'denied')


class Observer:
    def __init__(self, calls: list[str], root: Path, invocation: ProjectInvocation) -> None:
        self.calls, self.count = calls, 0
        self.root, self.invocation = root, invocation

    async def observe_original(self, **kwargs: Any) -> Mapping[str, JsonValue]:
        self.calls.append('execute-original')
        self.count += 1
        reference = kwargs['reference']
        loaded = ProjectBaselineStore(self.root).load(self.invocation, reference)
        try:
            _validate_report(json.dumps({'schema_version': 1, 'status': 'failed',
                'source_sha256': reference.input_source_sha256,
                'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'},
                'checks': [{'phase': phase, 'status': 'failed' if phase == 'test' else 'passed',
                            'exit_code': 1 if phase == 'test' else 0, 'timed_out': False,
                            'stdout': '', 'stderr': 'expected 12, got 10' if phase == 'test' else ''}
                           for phase in ('install', 'build', 'test')]}),
                expected_source=reference.input_source_sha256)
        except ProjectVerificationError as error:
            assert error.project_failure is not None and error.diagnostic is not None
            failure = error.project_failure.to_payload()
            diagnostic = error.diagnostic.to_payload()
        else:
            raise AssertionError('component fixture must fail its original test')
        return {'schema_version': 1, 'status': 'failed',
                'verification': failure, 'execution_diagnostic': diagnostic,
                'original_check_contract': {'schema_version': 1,
                    'contract_sha256': loaded.contract.protected_sha256,
                    'protected_sha256': loaded.contract.protected_sha256}}


async def setup(tmp_path: Path, *, allowed: bool = True):  # type: ignore[no-untyped-def]
    from agent_hub.runs.project_inputs import OriginalProjectPreparer

    root = tmp_path / 'workspaces'
    invocation, _ = source_tree(root)
    repository = ExecutableFakeRepository(routing_decision={
        'project_id': invocation.project_id, 'workspace_session_id': invocation.session_id,
        'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run'],
        'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']},
    })
    repository.run_id = repository.row.id = invocation.run_id
    repository.row.tenant_id = invocation.tenant_id
    repository.row.actor_role = Role.OPERATOR.value
    token = uuid4()
    await repository.claim_for_execution(FakeTransaction(), invocation.run_id, allow_running_recovery=False,
        worker_id='test-worker', worker_lease_token=token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    context = TaskContext(run_id=invocation.run_id, tenant_id=invocation.tenant_id,
        actor_id=repository.row.actor_id, actor_role=Role.OPERATOR, execution_token=token,
        mode=TaskMode.DISPATCH, request=repository.row.request,
        routing_decision=cast(Mapping[str, JsonValue], repository.row.routing_decision))
    calls: list[str] = []
    observer = Observer(calls, root, invocation)
    preparer = OriginalProjectPreparer(root=root, repository=cast(RunRepository, repository),
        authorizer=cast(HarnessToolGateway, Authorizer(calls, allowed=allowed)),
        verifier=cast(ProjectVerificationService, observer))
    return preparer, repository, context, observer, calls, root, invocation


async def test_preparation_captures_before_execution_and_reloads_without_replay(tmp_path: Path) -> None:
    preparer, repository, context, observer, calls, root, invocation = await setup(tmp_path)
    prepared = await preparer(context)
    state = repository.row.routing_decision['project_input_preparation']
    assert calls == ['authorize', 'execute-original']
    assert state['phase'] == 'observed'
    assert prepared.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert prepared.artifacts
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    loaded = ProjectBaselineStore(root).load(invocation, ProjectBaselineReference.from_payload(state['reference']))
    assert (loaded.snapshot / 'src/index.js').read_text() == 'module.exports = () => 0;\n'
    await preparer(context.model_copy(update={'routing_decision': repository.row.routing_decision}))
    assert observer.count == 1


async def test_missing_authority_does_not_execute_original_commands(tmp_path: Path) -> None:
    preparer, repository, context, observer, calls, _, _ = await setup(tmp_path, allowed=False)
    with pytest.raises(RuntimeError, match='authorization'):
        await preparer(context)
    assert calls == ['authorize']
    assert observer.count == 0
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'captured'


@pytest.mark.parametrize('changed', ['context_role', 'persisted_role', 'missing_worker'])
async def test_preparation_requires_current_persisted_identity_before_capture(tmp_path: Path, changed: str) -> None:
    from agent_hub.runs.repository import RunConflict

    preparer, repository, context, observer, calls, root, _ = await setup(tmp_path)
    if changed == 'context_role':
        context = context.model_copy(update={'actor_role': Role.SUPER_ADMIN})
    elif changed == 'persisted_role':
        repository.row.actor_role = Role.VIEWER.value
    else:
        repository.row.worker_id = None
    with pytest.raises(RunConflict, match='original project'):
        await preparer(context)
    assert not calls and observer.count == 0
    assert 'project_input_preparation' not in repository.row.routing_decision
    assert not (root / '.project-contracts').exists()


@pytest.mark.parametrize('phase', ['reserved', 'executing', 'unknown'])
async def test_consumed_or_uncertain_original_is_never_replayed(tmp_path: Path, phase: str) -> None:
    preparer, repository, context, observer, _, _, _ = await setup(tmp_path)
    repository.row.routing_decision['project_input_preparation'] = {'schema_version': 1, 'phase': phase}
    with pytest.raises(RuntimeError, match='original project'):
        await preparer(context.model_copy(update={'routing_decision': repository.row.routing_decision}))
    assert observer.count == 0
