"""Three-phase legacy-owner maintenance with a fake DB/RPC, no native deletion."""
from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.db.models import (
    ProviderContinuationRow,
    RunCheckpointRow,
    RunRow,
    RuntimeArtifactRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import RuntimeCheckpoint
from tests.unit.capabilities.test_python_expired_capacity_oct29 import Session as ExistingSession
from tests.unit.capabilities.test_python_project_lifecycle import Rows


class Session(ExistingSession):
    archive: RuntimeArtifactRow | None = None
    checkpoint: object = None

    async def scalar(self, query: Any) -> Any:
        entity = query.column_descriptions[0].get('entity')
        if entity is RuntimeArtifactRow:
            return self.archive
        if entity is RunCheckpointRow:
            assert query._limit_clause.value == 1 and query._order_by_clauses
            return self.checkpoint
        if entity is ProviderContinuationRow:
            raise AssertionError('historical provider row existence is not pending authority')
        return await super().scalar(query)

    async def scalars(self, query: Any) -> Any:
        entity = query.column_descriptions[0].get('entity')
        return Rows([self.row]) if entity is RunRow else Rows([])


@pytest.mark.parametrize('defect', [None, 'lease', 'approval', 'outbox', 'model-running', 'model-prepared',
    'tool-unknown', 'route-unknown', 'decision-token', 'archive-write', 'archive-readback',
    'owner-change', 'native-false', 'nested-models-none', 'nested-models-empty', 'nested-incompatible',
    'model-status-missing', 'model-status-unsupported', 'native-models-empty', 'native-models-none',
    'foreign-child', 'child-version', 'child-mode'])
async def test_normal_maintenance_archives_legacy_selfrepair_without_recursive_owner_lock(defect: str | None) -> None:
    session = Session()
    session.row.mode = 'hybrid'
    session.row.routing_decision.pop('project_input_preparation')
    session.row.routing_decision.update(source='self_repair', self_repair_accepted=True,
        generated_project_contract_version=1,
        generated_project_preparation={'schema_version': 1, 'phase': 'python_ready', 'stack': 'python'})
    session.row.version = 25
    # Latest369, unlike older367(prepared), contains completed/failed history.
    # The historical provider continuation object has no invented status field.
    models = {str(index): {'status': 'succeeded'} for index in range(22)}
    models['0']['provider_continuation'] = {'version': 1, 'sha256': 'a' * 64}
    models['failed'] = {'status': 'failed'}
    tools = {str(index): {'status': 'succeeded'} for index in range(35)}
    tools.update(failed_a={'status': 'failed'}, failed_b={'status': 'failed'})
    if defect in {'model-running', 'model-prepared'}:
        models['failed']['status'] = defect.split('-')[1]
    if defect == 'tool-unknown':
        tools['failed_a']['status'] = 'UNKNOWN'
    if defect in {'nested-models-none', 'nested-models-empty', 'nested-incompatible'}:
        models['0']['provider_continuation'] = {
            'phase': 'running', 'models': None if defect == 'nested-models-none' else {},
            **({'runtime_type': 'crew', 'runtime_version': '11', 'mode': 'dispatch'}
               if defect == 'nested-incompatible' else {})}
    if defect == 'model-status-missing':
        models['failed'].pop('status')
    elif defect == 'model-status-unsupported':
        models['failed']['status'] = 'not_a_native_status'
    if defect == 'native-models-empty':
        models = {}
    elif defect == 'native-models-none':
        models = None
    child = RuntimeCheckpoint(id=uuid4(), runtime_type='crew', runtime_version='11',
        tenant_id=session.row.tenant_id, run_id=uuid4() if defect == 'foreign-child' else session.row.id,
        mode=TaskMode.DIRECT if defect == 'child-mode' else TaskMode.DISPATCH,
        state={'phase': 'running', 'models': models, 'tools': tools})
    if defect == 'child-version':
        child = child.model_copy(update={'runtime_version': 'unrecognized'})
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='hybrid', runtime_version='3',
        tenant_id=session.row.tenant_id, run_id=session.row.id, mode=TaskMode.HYBRID,
        state={'child_checkpoint': child.to_payload()})
    session.checkpoint = RunCheckpointRow(id=checkpoint.id, tenant_id=checkpoint.tenant_id, run_id=checkpoint.run_id,
        sequence=369, runtime_type='hybrid', runtime_version='3', mode='hybrid', state=checkpoint.to_payload()['state'],
        state_sha256=checkpoint.state_sha256, payload=checkpoint.to_payload())
    if defect == 'lease':
        session.row.worker_id = 'active'
    elif defect in {'approval', 'outbox'}:
        session.block_table = {'approval': 'agent_hub_run_approvals', 'outbox': 'agent_hub_run_outbox'}[defect]
    elif defect == 'route-unknown':
        session.row.routing_decision['native_state'] = 'UNKNOWN'
    elif defect == 'decision-token':
        session.row.routing_decision.update(approval_kind='self_repair', decision_token='still-pending')
    before = copy.deepcopy(vars(session.row))
    calls: list[str] = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Archive(InMemoryArtifactRepository):
        async def put(self, tenant: Any, run: Any, artifact: Any, **kwargs: Any) -> None:
            assert not session.open, 'ArtifactRepo would deadlock by relocking this RunRow'
            calls.append('archive')
            if defect == 'archive-write':
                raise ValueError('archive conflict or storage refusal')
            await super().put(tenant, run, artifact, **kwargs)
            raw = PostgresArtifactRepository._encode_payload(artifact.to_payload())
            session.archive = RuntimeArtifactRow(tenant_id=tenant, run_id=run, id=artifact.id,
                content_sha256=artifact.content_sha256, payload=raw, byte_size=len(raw.encode()), permanent=True)

        async def get_many(self, *args: Any, **kwargs: Any) -> Any:
            assert not session.open
            calls.append('readback')
            if defect == 'archive-readback':
                return ()
            if defect == 'owner-change':
                session.row.version += 1
            return await super().get_many(*args, **kwargs)

    class Executor:
        async def inspect_failed(self, invocation: Any) -> Any:
            from tests.unit.skills.sandbox.test_python_expired_empty_failure_oct30 import snapshot
            assert session.open and invocation.install_allowed is False
            calls.append('inspect')
            return snapshot(project='demo')

        async def commit_failed(self, invocation: Any, value: Any) -> bool:
            assert session.open and session.archive is not None and session.archive.permanent
            assert invocation.run_id == session.row.id
            calls.append('commit')
            return defect != 'native-false'

    hook = PythonProjectTerminalHook(Repository(), object(), Executor())
    hook._failure_archive_repository = Archive()
    assert await hook.maintain_capacity() == (1 if defect is None else 0)
    if defect in {None, 'native-false'}:
        assert calls == ['inspect', 'archive', 'readback', 'commit']
    else:
        assert 'commit' not in calls
    if defect == 'owner-change':
        before['version'] += 1  # Simulated concurrent authoritative owner change.
    assert vars(session.row) == before
