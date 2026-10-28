"""Real verifier and private artifact repository; SQL adapter is an in-memory double."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import TaskContext
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file
from tests.unit.runs.test_project_verification_receipts import Database


class Runs:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, tenant: UUID, run: UUID) -> Any:
        assert (tenant, run) == (self.db.row.tenant_id, self.db.row.id)
        return RunRepository._record(self.db.row)


class Fixture:
    def __init__(self, tmp_path: Path, *, failed: bool = False) -> None:
        self.db = Database()
        self.db.row.tenant_id, self.db.row.id = TENANT, RUN
        self.db.row.routing_decision['requested_permissions'].append('network.read')
        self.root = tmp_path / 'projects'
        write_file(self.root, project_path() + '/package.json', '{"scripts":{"build":"node -v","test":"node --test"}}')
        self.executor = Executor(failed=failed)
        self.executor.root = self.root
        self.artifacts = InMemoryArtifactRepository()
        self.runs = Runs(self.db)

    def context(self) -> TaskContext:
        row = self.db.row
        return TaskContext(tenant_id=TENANT, run_id=RUN, actor_id=row.actor_id,
            actor_role=Role.OPERATOR, execution_token=row.worker_lease_token,
            mode=TaskMode.DIRECT, request=row.request, routing_decision=row.routing_decision)

    def verifier(self) -> ProjectVerificationService:
        parameters = inspect.signature(ProjectVerificationService).parameters
        assert 'receipt_repository' in parameters and 'artifact_repository' in parameters, 'durable verifier injection missing'
        return ProjectVerificationService(root=self.root, repository=self.runs, executor=self.executor,
            receipt_repository=self.db.repository(), artifact_repository=self.artifacts)

    def bind(self) -> Any:
        return bind_execution_authority(self.context(), worker_id=self.db.row.worker_id)


async def verify(service: ProjectVerificationService, key: str = 'verify-one') -> Any:
    return await service.verify(tenant_id=TENANT, run_id=RUN, actor='main_agent', action_key=key)


async def restore(service: ProjectVerificationService, key: str = 'verify-one') -> Any:
    return await service.restore(tenant_id=TENANT, run_id=RUN, actor='main_agent', action_key=key)


async def test_success_is_issued_before_cold_worker_restores_without_executor(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    original = fixture.executor.run

    async def run(invocation: Any) -> Any:
        assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'
        return await original(invocation)

    fixture.executor.run = run  # type: ignore[method-assign]
    with fixture.bind():
        result = await verify(service)
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'passed'
    fixture.db.new_lease()
    cold = fixture.verifier()
    with fixture.bind():
        assert await restore(cold) == result
        assert await cold.require_current_receipt(TENANT, RUN) == result['verification']['source_sha256']
    assert len(fixture.executor.calls) == 1


@pytest.mark.parametrize('defect', ['source', 'actor', 'action', 'permissions', 'permission_mode', 'missing_artifact'])
async def test_cold_recovery_rechecks_current_scope_and_private_issuance(tmp_path: Path, defect: str) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    with fixture.bind():
        await verify(service)
    fixture.db.new_lease()
    if defect == 'source':
        write_file(fixture.root, project_path() + '/changed.js', 'changed after success')
    elif defect == 'permissions':
        fixture.db.row.routing_decision['requested_permissions'].remove('network.read')
    elif defect == 'permission_mode':
        fixture.db.row.routing_decision['permission_mode'] = 'auto_review'
    elif defect == 'missing_artifact':
        fixture.artifacts = InMemoryArtifactRepository()
    cold = fixture.verifier()
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        if defect == 'actor':
            await cold.restore(tenant_id=TENANT, run_id=RUN, actor='other_agent', action_key='verify-one')
        else:
            await restore(cold, 'other-key' if defect == 'action' else 'verify-one')
    with pytest.raises(ProjectVerificationError):
        await cold.require_current_receipt(TENANT, RUN)
    assert len(fixture.executor.calls) == 1


async def test_stale_worker_cannot_issue_and_unknown_attempt_is_not_reexecuted(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    original = fixture.executor.run

    async def run(invocation: Any) -> Any:
        value = await original(invocation)
        fixture.db.new_lease()
        return value

    fixture.executor.run = run  # type: ignore[method-assign]
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await verify(service)
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await verify(fixture.verifier(), 'new-action-cannot-replay-unknown')
    assert len(fixture.executor.calls) == 1
    with pytest.raises(ProjectVerificationError):
        await service.require_current_receipt(TENANT, RUN)


async def test_real_failed_report_closes_failed_without_reviving_old_pass(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    with fixture.bind():
        await verify(service)
    fixture.executor.failed = True
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await verify(service, 'verify-two')
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'failed'
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await restore(fixture.verifier())
    assert len(fixture.executor.calls) == 2


async def test_missing_worker_identity_stops_before_executor(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    with pytest.raises(ProjectVerificationError):
        await verify(fixture.verifier())
    assert fixture.executor.calls == []


async def test_latest_success_is_bound_to_exact_action(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    with fixture.bind():
        await verify(service)
        second = await verify(service, 'verify-two')
    fixture.db.new_lease()
    with fixture.bind():
        with pytest.raises(ProjectVerificationError):
            await restore(fixture.verifier())
        assert await restore(fixture.verifier(), 'verify-two') == second
    assert len(fixture.executor.calls) == 2


@pytest.mark.parametrize('fault', ['put', 'put_cancel', 'complete_unknown', 'malformed_report'])
async def test_storage_or_execution_ambiguity_never_authorizes_or_repeats(tmp_path: Path, fault: str) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    assert service._durable is not None
    original_put = fixture.artifacts.put
    if fault in ('put', 'put_cancel'):
        async def broken_put(*args: Any, **kwargs: Any) -> None:
            await original_put(*args, **kwargs)
            if fault == 'put_cancel':
                raise asyncio.CancelledError
            raise OSError('private storage unavailable')
        fixture.artifacts.put = broken_put  # type: ignore[method-assign]
    elif fault == 'complete_unknown':
        original_complete = service._durable.receipts.complete
        async def uncertain(*args: Any, **kwargs: Any) -> Any:
            await original_complete(*args, **kwargs)
            raise OSError('commit response lost')
        service._durable.receipts.complete = uncertain  # type: ignore[method-assign]
    else:
        original_run = fixture.executor.run
        async def malformed(invocation: Any) -> Any:
            value = await original_run(invocation)
            return value.model_copy(update={'stdout': '{"status":"passed"}'})
        fixture.executor.run = malformed  # type: ignore[method-assign]
    expected = asyncio.CancelledError if fault == 'put_cancel' else ProjectVerificationError
    with fixture.bind(), pytest.raises(expected):
        await verify(service)
    with pytest.raises(ProjectVerificationError):
        await service.require_current_receipt(TENANT, RUN)
    if fault == 'complete_unknown':
        assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'passed'
        with fixture.bind():
            await restore(fixture.verifier())
    else:
        assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'
        if fault in ('put', 'put_cancel'):
            assert not fixture.artifacts._artifacts
        with fixture.bind(), pytest.raises(ProjectVerificationError):
            await verify(fixture.verifier(), 'second-attempt')
    assert len(fixture.executor.calls) == 1


@pytest.mark.parametrize('change', ['lease', 'permission', 'new_generation'])
async def test_warm_delivery_cannot_keep_authority_after_revocation(tmp_path: Path, change: str) -> None:
    fixture = Fixture(tmp_path)
    warm = fixture.verifier()
    with fixture.bind():
        await verify(warm)
        if change == 'lease':
            fixture.db.new_lease()
        elif change == 'permission':
            fixture.db.row.routing_decision['requested_permissions'].remove('network.read')
        else:
            await verify(fixture.verifier(), 'new-verification')
        with pytest.raises(ProjectVerificationError):
            await warm.require_current_receipt(TENANT, RUN)


async def test_cold_restore_is_readonly_and_does_not_adopt_unbound_lease(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    with fixture.bind():
        await verify(fixture.verifier())
    fixture.db.new_lease()
    with pytest.raises(ProjectVerificationError):
        await restore(fixture.verifier())
    assert len(fixture.executor.calls) == 1


@pytest.mark.parametrize('defect', ['producer', 'type', 'schema', 'generation', 'source', 'scope', 'result'])
async def test_private_body_hash_alone_does_not_authorize_a_recovery(tmp_path: Path, defect: str) -> None:
    from uuid import uuid4

    from agent_hub.runtime.artifacts import ArtifactReference
    from agent_hub.runtime.contracts import Artifact

    fixture = Fixture(tmp_path)
    with fixture.bind():
        await verify(fixture.verifier())
    old = next(iter(fixture.artifacts._artifacts.values()))
    body = dict(old.content)
    fields: dict[str, Any] = {}
    if defect in ('producer', 'type'):
        fields[defect] = 'untrusted'
    elif defect == 'schema':
        body['schema_version'] = True
    elif defect == 'generation':
        body['generation'] = True
    elif defect == 'source':
        body['source_sha256'] = 'f' * 64
    elif defect == 'scope':
        body['scope'] = {}
    else:
        body['result'] = {'verification': {'status': 'passed', 'source_sha256': body['source_sha256']}}
    replacement = Artifact(id=uuid4(), type=fields.get('type', old.type), producer=fields.get('producer', old.producer), content=body)
    await fixture.artifacts.put(TENANT, RUN, replacement)
    fixture.db.row.routing_decision[RECEIPT_KEY]['artifact_references'] = [
        ArtifactReference(id=replacement.id, sha256=replacement.content_sha256).to_payload()]
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await restore(fixture.verifier())
    assert len(fixture.executor.calls) == 1


async def test_source_change_while_private_evidence_is_written_cannot_be_issued(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    original = fixture.artifacts.put
    async def mutate(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        write_file(fixture.root, project_path() + '/changed.js', 'late source mutation')
    fixture.artifacts.put = mutate  # type: ignore[method-assign]
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await verify(fixture.verifier())
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'
    assert not fixture.artifacts._artifacts


async def test_completed_restore_revokes_inherited_validation_bypass(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    with fixture.bind():
        await verify(fixture.verifier())
        cold = fixture.verifier()
        original = cold.require_current_receipt
        released = asyncio.Event()
        inherited: list[asyncio.Task[Any]] = []
        async def late() -> Any:
            await released.wait()
            return await original(TENANT, RUN)
        async def inspect_current(tenant_id: UUID, run_id: UUID) -> Any:
            if not inherited:
                inherited.append(asyncio.create_task(late()))
            return await original(tenant_id, run_id)
        cold.require_current_receipt = inspect_current  # type: ignore[method-assign]
        await restore(cold)
        await verify(fixture.verifier(), 'new-generation')
        released.set()
        with pytest.raises(ProjectVerificationError):
            await inherited[0]
