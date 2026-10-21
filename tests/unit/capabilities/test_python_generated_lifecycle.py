"""Generated terminal metadata tests: fake DB and release RPC, never native deletion."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.domain.runs import RunStatus
from tests.unit.capabilities.test_python_project_lifecycle import Rows, Session
from tests.unit.skills.sandbox.test_python_project_client import invocation, reference


def generated_session() -> Session:
    session = Session()
    route = session.row.routing_decision
    route.pop('project_input_preparation')
    route.update(generated_project_contract_version=1, generated_project_preparation={
        'schema_version': 1, 'phase': 'python_ready', 'stack': 'python',
        'reference': invocation().original_reference.model_dump(mode='json'),
        'acceptance_sha256': '1' * 64, 'request_sha256': '2' * 64,
        'user_id': str(UUID(int=3)), 'proposer_actor': 'engineer',
        'manifest_sha256': '3' * 64, 'manifest_raw_sha256': '4' * 64,
        'python_recipe': {'schema_version': 1, 'recipe_sha256': invocation().recipe_sha256,
            'environment_input_sha256': invocation().environment_input_sha256}})
    return session


def hook_for(session: Session, *, refs: Any = None, inv: Any = None, fail_once: bool = False) -> Any:
    calls: list[Any] = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class OriginalReader:
        async def python_original_context(self, *args: Any) -> Any:
            raise AssertionError('generated history must never select original context')

    class GeneratedReader:
        async def environment_context(self, tenant: UUID, run: UUID) -> Any:
            assert session.open
            assert (tenant, run) == (UUID(int=1), UUID(int=2))
            calls.append('generated-context')
            return SimpleNamespace(invocation=inv or invocation(),
                environment_references=(reference(),) if refs is None else refs)

    class Releaser:
        async def release_completed(self, item: Any, *, expected_environment: Any) -> Any:
            assert session.open and item.install_allowed is False
            assert item.execution_id != invocation().execution_id
            assert item.source_kind == 'candidate'
            assert expected_environment == reference()
            calls.append(item.execution_id)
            if fail_once and len([v for v in calls if v != 'generated-context']) == 1:
                raise OSError('reply lost')
            return expected_environment

    return PythonProjectTerminalHook(Repository(), OriginalReader(), Releaser(),
        generated_reader=GeneratedReader()), calls


async def invoke(hook: Any, *, status: RunStatus = RunStatus.COMPLETED) -> None:
    await hook(tenant_id=UUID(int=1), run_id=UUID(int=2), actor_id=None,
        status=status, mode=None, routing_decision={'terminal': True})


async def test_completed_generated_python_releases_exact_durable_reference_only() -> None:
    session = generated_session()
    hook, calls = hook_for(session)
    await invoke(hook)
    assert len(calls) == 2 and calls[0] == 'generated-context'
    assert calls[1].startswith('python_release_')
    assert session.row.routing_decision['python_project_cache_release']['environment_reference'] == reference().model_dump(mode='json')
    assert session.row.version == 2
    assert session.queries[0]._for_update_arg is not None


@pytest.mark.parametrize('status', list(RunStatus))
async def test_failed_cancelled_waiting_paused_and_other_noncompleted_generated_cache_is_retained(status: RunStatus) -> None:
    session = generated_session()
    session.row.status = status.value
    hook, calls = hook_for(session)
    await invoke(hook)
    assert bool(calls) is (status is RunStatus.COMPLETED)


@pytest.mark.parametrize('field', ['worker_id', 'worker_lease_token', 'worker_lease_expires_at', 'worker_heartbeat_at'])
async def test_generated_worker_not_yet_quiet_is_retained(field: str) -> None:
    session = generated_session()
    setattr(session.row, field, 'present')
    hook, calls = hook_for(session)
    await invoke(hook)
    assert calls == []


@pytest.mark.parametrize('defect', ['pending-approval', 'unknown', 'executing', 'inflight', 'partial', 'no-checkpoint',
    'invalid-checkpoint', 'captured', 'wrong-version', 'node', 'mixed-original', 'partial-descriptor',
    'no-refs', 'ambiguous', 'wrong-ref', 'wrong-recipe', 'wrong-project'])
async def test_generated_partial_uncertain_or_mismatched_evidence_cannot_release(defect: str) -> None:
    session = generated_session()
    state = session.row.routing_decision['generated_project_preparation']
    refs: tuple[Any, ...] | None = None
    if defect == 'pending-approval':
        session.approval = UUID(int=9)
    elif defect in {'unknown', 'executing', 'inflight', 'partial'}:
        session.checkpoints = [{'tools': {'status': defect.upper()}}]
    elif defect == 'no-checkpoint':
        session.checkpoints = []
    elif defect == 'invalid-checkpoint':
        session.checkpoints = [None]
    elif defect == 'captured':
        state['phase'] = 'captured'
        state.pop('python_recipe')
    elif defect == 'wrong-version':
        session.row.routing_decision['generated_project_contract_version'] = True
    elif defect == 'node':
        state['stack'] = 'node'
    elif defect == 'mixed-original':
        session.row.routing_decision['project_input_preparation'] = {'schema_version': 2, 'phase': 'observed'}
    elif defect == 'partial-descriptor':
        state.pop('reference')
    elif defect == 'no-refs':
        refs = ()
    elif defect == 'ambiguous':
        refs = (reference(), reference().model_copy(update={'environment_sha256': '9' * 64}))
    elif defect == 'wrong-ref':
        state['reference']['contract_sha256'] = '9' * 64
    elif defect == 'wrong-recipe':
        state['python_recipe']['recipe_sha256'] = '9' * 64
    else:
        session.row.routing_decision['project_id'] = 'different'
    hook, calls = hook_for(session, refs=refs)
    await invoke(hook)
    assert not [entry for entry in calls if entry != 'generated-context']
    assert 'python_project_cache_release' not in session.row.routing_decision


async def test_generated_retry_query_ors_new_route_and_reenters_locked_gate_after_lost_reply() -> None:
    class RetrySession(Session):
        async def scalar(self, query: Any) -> Any:
            self.queries.append(query)
            self._reads += 1
            return self.row if self._reads == 1 else self.approval

        async def scalars(self, query: Any) -> Any:
            if query.column_descriptions[0]['name'] != 'RunRow':
                return await super().scalars(query)
            params = query.compile().params
            assert {'project_input_preparation': {'schema_version': 2, 'phase': 'observed'}} in params.values()
            assert {'generated_project_contract_version': 1, 'generated_project_preparation': {
                'schema_version': 1, 'phase': 'python_ready', 'stack': 'python'}} in params.values()
            assert ' OR ' in str(query)
            if 'python_project_cache_release' in self.row.routing_decision or any(
                isinstance(value, UUID) and self.row.id <= value for value in params.values()
            ):
                return Rows([])
            return Rows([self.row])

    session = RetrySession()
    session.row = generated_session().row
    session.row.routing_decision['terminal_hook_notified'] = True
    hook, calls = hook_for(session, fail_once=True)
    assert await hook.retry_completed() == 1
    assert 'python_project_cache_release' not in session.row.routing_decision
    assert await hook.retry_completed() == 0
    assert await hook.retry_completed() == 1
    executions = [item for item in calls if item != 'generated-context']
    assert len(executions) == len(set(executions)) == 2
    assert session.row.version == 2
    assert await hook.retry_completed() == 0
