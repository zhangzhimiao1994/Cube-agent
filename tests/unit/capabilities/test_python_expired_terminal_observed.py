"""Offline legacy READY selection: no native cache, deletion, model or server."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import RuntimeCheckpoint
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities import test_python_failed_hybrid_cleanup_oct28 as failed_hybrid
from tests.unit.capabilities.capability_lifecycle_typed_fixture import UnusedReleaser
from tests.unit.capabilities.test_python_expired_capacity_oct29 import Session
from tests.unit.capabilities.test_python_failed_hybrid_cleanup_oct28 import (
    EvidenceSession,
    rehash,
)
from tests.unit.capabilities.test_python_project_service import configured, observed, request

evidence = failed_hybrid.evidence


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=dict, sort_keys=True,
        ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()


async def prepared(tmp_path: Path, *, passed: bool = False) -> tuple[Any, Any, Any, Any]:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    if passed:
        executor.failure_index = None
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    repository.record = replace(repository.record, routing_decision=json.loads(
        json.dumps(repository.record.routing_decision, default=dict)))
    return verifier, repository, executor, invocation


class ObservedSession(Session):
    def __init__(self, record: Any) -> None:
        super().__init__()
        self.row.routing_decision = copy.deepcopy(record.routing_decision)
        self.row.actor_id, self.row.actor_role = record.actor_id, record.actor_role
        self.row.request = 'historical existing-project repair'
        self.row.mode, self.row.blocked_by_run_id = 'direct', None
        self.rows = [self.row]
        self.checkpoint: Any = None
        self.private_rows: list[Any] = []

    async def scalar(self, query: Any) -> Any:
        self.queries.append(query)
        entity = query.column_descriptions[0].get('entity')
        if entity.__name__ == 'RunRow':
            assert query._for_update_arg is not None and self.open
            values = query.compile().params.values()
            return next((r for r in self.rows if r.id in values), None)
        if entity.__tablename__ == self.block_table:
            return UUID(int=99)
        if entity.__name__ == 'RunCheckpointRow':
            return self.checkpoint
        return None

    async def scalars(self, query: Any) -> Any:
        self.queries.append(query)
        entity = query.column_descriptions[0].get('entity')
        if entity.__name__ == 'RunCheckpointRow':
            assert self.open
            return SimpleNamespace(all=lambda: [] if self.checkpoint is None else [self.checkpoint])
        if entity.__name__ == 'RuntimeArtifactRow':
            assert self.open
            return SimpleNamespace(all=lambda: self.private_rows)
        params = query.compile().params
        includes_observed = any(isinstance(v, dict) and
            v.get('project_input_preparation', {}).get('phase') == 'observed'
            for v in params.values())
        after = [v for v in params.values() if isinstance(v, UUID)]
        rows = [r for r in self.rows if includes_observed and (not after or r.id > max(after))]
        return SimpleNamespace(all=lambda: rows[:8])


async def exercise(
    session: ObservedSession, verifier: Any, *, change: dict[str, object] | None = None,
    root_result: object = True,
) -> tuple[int, list[str], PythonProjectTerminalHook]:
    calls: list[str] = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader:
        async def python_original_context(self, *args: Any) -> Any:
            pytest.fail('must not load a live project or original execution context')

        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            assert session.open
            calls.append('metadata')
            result = await verifier.python_expired_environment_invocation(tenant, run)
            if change and result is not None:
                result = result.model_copy(update=change)
            return result

    class Root(UnusedReleaser):
        async def release_expired(self, invocation: PythonProjectInvocation) -> Any:
            assert session.open and invocation.install_allowed is False
            assert invocation.source_kind == 'original' and invocation.timeout_seconds == 30
            calls.append('root-expired-ready-fences')
            if isinstance(root_result, BaseException):
                raise root_result
            return root_result

    hook = PythonProjectTerminalHook(Repository(), Reader(), Root())
    return await hook.maintain_capacity(), calls, hook


@pytest.mark.parametrize('passed', [False, True])
async def test_real_observed_selector_needs_no_source_baseline_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passed: bool,
) -> None:
    verifier, repository, executor, invocation = await prepared(tmp_path, passed=passed)
    before = copy.deepcopy(repository.record)
    counts = tuple(len(v) for v in (executor.calls, executor.probes, executor.inspections))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('metadata-only selection touched source or immutable baseline')

    monkeypatch.setattr(ProjectBaselineStore, 'load', forbidden)
    result = await verifier.python_expired_environment_invocation(invocation.tenant_id, invocation.run_id)
    assert result is not None and result.source_kind == 'original'
    assert result.install_allowed is False and result.timeout_seconds == 30
    assert result.original_reference.model_dump(mode='json') == (
        repository.record.routing_decision['project_input_preparation']['reference'])
    assert counts == tuple(len(v) for v in (executor.calls, executor.probes, executor.inspections))
    assert repository.record == before


async def test_legacy_observed_releases_only_through_locked_root_ttl_ready_path(tmp_path: Path) -> None:
    verifier, repository, _, _ = await prepared(tmp_path)
    session = ObservedSession(repository.record)
    before = copy.deepcopy(vars(session.row))
    result, calls, _ = await exercise(session, verifier)
    assert result == 1 and calls == ['metadata', 'root-expired-ready-fences']
    assert vars(session.row) == before
    assert 'terminal_failed_python_environment_cleanup_version' not in session.row.routing_decision


@pytest.mark.parametrize('defect', [
    'running', 'young', 'naive', 'worker_id', 'worker_lease_token', 'worker_lease_expires_at',
    'worker_heartbeat_at', 'blocked', 'approval', 'outbox', 'provider', 'artifact-write', 'unknown-route',
    'self-repair', 'decision-token', 'approval-kind', 'unknown-checkpoint', 'pending-checkpoint',
    'forged-checkpoint', 'completed-checkpoint',
])
async def test_uncertain_or_retained_legacy_consumers_never_reach_root(tmp_path: Path, defect: str) -> None:
    verifier, repository, _, invocation = await prepared(tmp_path)
    session = ObservedSession(repository.record)
    if defect == 'running':
        session.row.status = 'running'
    elif defect == 'young':
        session.row.updated_at = datetime.now(UTC)
    elif defect == 'naive':
        session.row.updated_at = session.row.updated_at.replace(tzinfo=None)
    elif defect.startswith('worker_'):
        setattr(session.row, defect, 'retained')
    elif defect == 'blocked':
        session.row.blocked_by_run_id = UUID(int=99)
    elif defect in {'approval', 'outbox', 'provider', 'artifact-write'}:
        session.block_table = {'approval': 'agent_hub_run_approvals', 'outbox': 'agent_hub_run_outbox',
                               'provider': 'agent_hub_provider_continuations',
                               'artifact-write': 'agent_hub_runtime_artifact_writes'}[defect]
    elif defect == 'unknown-route':
        session.row.routing_decision['retained'] = 'UNKNOWN'
    elif defect == 'self-repair':
        session.row.routing_decision['source'] = 'self_repair'
    elif defect in {'decision-token', 'approval-kind'}:
        session.row.routing_decision[defect.replace('-', '_')] = 'retained'
    else:
        cp = RuntimeCheckpoint(id=uuid4(), run_id=invocation.run_id, tenant_id=invocation.tenant_id,
            runtime_type='direct', runtime_version='1', mode=TaskMode.DIRECT,
            state={'completed': defect == 'completed-checkpoint',
                   'outcome': 'UNKNOWN' if defect == 'unknown-checkpoint' else 'completed'
                   if defect == 'completed-checkpoint' else 'pending'})
        session.checkpoint = SimpleNamespace(id=cp.id, payload=cp.to_payload(), state=cp.to_payload()['state'],
            state_sha256='0' * 64 if defect == 'forged-checkpoint' else cp.state_sha256,
            runtime_type=cp.runtime_type, runtime_version=cp.runtime_version, mode=cp.mode.value)
    before = copy.deepcopy(vars(session.row))
    result, calls, _ = await exercise(session, verifier)
    assert result == 0 and 'root-expired-ready-fences' not in calls
    assert vars(session.row) == before


@pytest.mark.parametrize('defect', [
    'hash', 'status', 'observation-extra', 'source', 'execution', 'recipe', 'environment-input',
    'scope', 'original-reference', 'protected-contract', 'reference-origin', 'observation-marker-bool',
])
async def test_observed_selector_rejects_rehashed_scope_reference_and_recipe_drift(
    tmp_path: Path, defect: str,
) -> None:
    verifier, repository, executor, invocation = await prepared(tmp_path)
    route = copy.deepcopy(repository.record.routing_decision)
    state = route['project_input_preparation']
    observation = json.loads(json.dumps(state['observation'], default=dict))
    if defect == 'hash':
        state['observation_sha256'] = '0' * 64
    elif defect == 'status':
        observation['status'] = 'unknown'
    elif defect == 'observation-extra':
        observation['authority'] = True
    elif defect == 'source':
        observation['verification']['source_sha256'] = '0' * 64
    elif defect == 'execution':
        observation['execution_id'] = 'different_execution'
    elif defect in {'recipe', 'environment-input'}:
        key = 'recipe_sha256' if defect == 'recipe' else 'environment_input_sha256'
        observation['python_recipe'][key] = '0' * 64
    elif defect == 'scope':
        observation['environment_reference']['scope']['run_id'] = str(UUID(int=99))
    elif defect == 'original-reference':
        state['reference']['input_source_sha256'] = '0' * 64
    elif defect == 'protected-contract':
        observation['original_check_contract']['protected_sha256'] = '0' * 64
    elif defect == 'observation-marker-bool':
        observation['python_recipe']['schema_version'] = True
    else:
        observation['environment_reference']['origin_execution_id'] = 'different_origin'
    state['observation'] = observation
    if defect != 'hash':
        state['observation_sha256'] = digest(observation)
    repository.record = replace(repository.record, routing_decision=route)
    before = copy.deepcopy(repository.record)
    counts = tuple(len(v) for v in (executor.calls, executor.probes, executor.inspections))
    with pytest.raises(ProjectVerificationError):
        await verifier.python_expired_environment_invocation(invocation.tenant_id, invocation.run_id)
    assert repository.record == before
    assert counts == tuple(len(v) for v in (executor.calls, executor.probes, executor.inspections))


@pytest.mark.parametrize('change', [
    {'run_id': UUID(int=99)}, {'tenant_id': UUID(int=99)}, {'session_id': 'foreign'},
    {'project_id': 'foreign'}, {'recipe_sha256': '0' * 64}, {'environment_input_sha256': '0' * 64},
    {'install_allowed': True}, {'source_kind': 'candidate'},
    {'original_reference': {'schema_version': 1, 'contract_sha256': '0' * 64,
                            'input_source_sha256': '0' * 64, 'file_count': 1}},
])
async def test_lifecycle_rebinds_selector_output_before_root(
    tmp_path: Path, change: dict[str, object],
) -> None:
    verifier, repository, _, _ = await prepared(tmp_path)
    result, calls, _ = await exercise(ObservedSession(repository.record), verifier, change=change)
    assert result == 0 and calls == ['metadata']


@pytest.mark.parametrize('root_result', [False, None, 1, ProcessCleanupIncomplete('python_project_cleanup_incomplete')])
async def test_native_ttl_or_retained_refusal_is_not_success(tmp_path: Path, root_result: object) -> None:
    verifier, repository, _, _ = await prepared(tmp_path)
    session = ObservedSession(repository.record)
    before = copy.deepcopy(vars(session.row))
    result, calls, _ = await exercise(session, verifier, root_result=root_result)
    assert result == 0 and calls == ['metadata', 'root-expired-ready-fences']
    assert vars(session.row) == before


async def test_failed_candidate_advances_bounded_scan_cursor_without_replaying(tmp_path: Path) -> None:
    verifier, repository, _, _ = await prepared(tmp_path)
    session = ObservedSession(repository.record)
    result, calls, hook = await exercise(session, verifier, root_result=RuntimeError('retained'))
    assert result == 0 and calls == ['metadata', 'root-expired-ready-fences']
    assert hook._capacity_cursor == session.row.id
    assert await hook.maintain_capacity() == 0
    assert calls == ['metadata', 'root-expired-ready-fences']
    assert hook._capacity_cursor is None
    scans = [q for q in session.queries if q.column_descriptions[0].get('entity').__name__ == 'RunRow'
             and q._for_update_arg is None]
    assert scans and all(q._limit_clause.value == 8 for q in scans)


async def test_legacy_hybrid3_crew11_known_failed_history_reaches_original_expired_release(
    tmp_path: Path, evidence: EvidenceSession,
) -> None:
    verifier, repository, _, _ = await prepared(tmp_path)
    session = ObservedSession(repository.record)
    session.row.mode = 'hybrid'
    session.checkpoint = evidence.saved
    session.private_rows = evidence.private_rows
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.checkpoint.payload)
    result, calls, _ = await exercise(session, verifier)
    assert result == 1 and calls == ['metadata', 'root-expired-ready-fences']
    assert (vars(session.row), session.checkpoint.payload) == before
    assert 'terminal_failed_python_environment_cleanup_version' not in session.row.routing_decision


@pytest.mark.parametrize('defect', [
    'tool-running', 'model-running', 'missing-tool', 'missing-artifact', 'artifact-sha',
    'wrong-arguments', 'wrapper-extra', 'foreign-child', 'provider', 'native-retained',
])
async def test_legacy_hybrid_proof_never_hides_unknown_missing_or_retained_consumers(
    tmp_path: Path, evidence: EvidenceSession, defect: str,
) -> None:
    verifier, repository, _, _ = await prepared(tmp_path)
    payload: Any = evidence.saved.payload
    body = payload['state']['child_checkpoint']['state']
    if defect == 'tool-running':
        next(iter(body['tools'].values()))['status'] = 'running'
    elif defect == 'model-running':
        next(iter(body['models'].values()))['status'] = 'running'
    elif defect == 'missing-tool':
        body['tools'] = {}
    elif defect == 'missing-artifact':
        evidence.private_rows.pop()
    elif defect == 'artifact-sha':
        evidence.private_rows[0].content_sha256 = '0' * 64
    elif defect == 'wrong-arguments':
        next(iter(body['tools'].values()))['arguments_sha256'] = '0' * 64
    elif defect == 'wrapper-extra':
        payload['state']['unrecognized'] = True
    elif defect == 'foreign-child':
        payload['state']['child_checkpoint']['run_id'] = str(UUID(int=99))
    rehash(evidence)
    session = ObservedSession(repository.record)
    session.row.mode = 'hybrid'
    session.checkpoint = evidence.saved
    session.private_rows = evidence.private_rows
    if defect == 'provider':
        session.block_table = 'agent_hub_provider_continuations'
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.checkpoint.payload)
    result, calls, _ = await exercise(session, verifier,
        root_result=ProcessCleanupIncomplete('python_project_cleanup_incomplete')
        if defect == 'native-retained' else True)
    assert result == 0
    assert calls == (['metadata', 'root-expired-ready-fences'] if defect == 'native-retained' else [])
    assert (vars(session.row), session.checkpoint.payload) == before
