"""Independent verifier review; real service/DAO with controlled transport faults.

The SQL adapter is an in-memory double, not evidence of PostgreSQL locking.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.python_generated_service import PythonGeneratedRecipeApprovalRequired
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import Artifact
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_generated_project_delivery import configured
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.capabilities.test_verification_cold_recovery import Fixture, Runs, restore, verify


@pytest.mark.parametrize('entry', ['verify', 'restore'])
@pytest.mark.parametrize('revocation', ['running', 'failed', 'lease', 'binding_exit'])
async def test_finished_verifier_context_cannot_authorize_inherited_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, revocation: str,
) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    release = asyncio.Event()
    child: asyncio.Task[str] | None = None

    async def deferred_delivery_check() -> str:
        await release.wait()
        return await service.require_current_receipt(TENANT, RUN)

    if entry == 'verify':
        original_run = fixture.executor.run

        async def run(invocation: Any) -> Any:
            nonlocal child
            child = asyncio.create_task(deferred_delivery_check())
            return await original_run(invocation)

        monkeypatch.setattr(fixture.executor, 'run', run)
    else:
        with fixture.bind():
            await verify(service)
        original_get = fixture.artifacts.get_many

        async def get_many(*args: Any, **kwargs: Any) -> tuple[Artifact, ...]:
            nonlocal child
            child = asyncio.create_task(deferred_delivery_check())
            return await original_get(*args, **kwargs)

        monkeypatch.setattr(fixture.artifacts, 'get_many', get_many)

    async def revoke_and_check() -> None:
        assert child is not None
        if revocation == 'lease':
            fixture.db.new_lease()
        elif revocation in {'running', 'failed'}:
            current = fixture.db.row.routing_decision[RECEIPT_KEY]
            dao, authority = fixture.db.repository(), fixture.db.authority()
            running = await dao.begin(authority, action_key='verify-two',
                execution_id='review_new_attempt', source_sha256=current['source_sha256'])
            if revocation == 'failed':
                await dao.complete(authority, expected=running, status='failed')
            assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == revocation
            assert await dao.load(authority) is None
        release.set()
        with pytest.raises(ProjectVerificationError):
            await asyncio.wait_for(child, timeout=2)

    try:
        with fixture.bind():
            if entry == 'verify':
                await verify(service)
            else:
                await restore(service)
            assert child is not None and not child.done()
            if revocation != 'binding_exit':
                await revoke_and_check()
        if revocation == 'binding_exit':
            await revoke_and_check()
    finally:
        release.set()
        if child is not None:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
    assert len(fixture.executor.calls) == 1


async def test_plain_native_cleanup_incomplete_keeps_its_exception_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    error = ProcessCleanupIncomplete('owned native process has not drained')

    async def run(invocation: Any) -> Any:
        fixture.executor.calls.append(invocation)
        raise error

    monkeypatch.setattr(fixture.executor, 'run', run)
    try:
        with fixture.bind(), pytest.raises(ProcessCleanupIncomplete) as caught:
            await verify(service)
        assert caught.value is error
    finally:
        assert len(fixture.executor.calls) == 1
        assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'
        assert not fixture.artifacts._artifacts


async def test_repeated_cancellation_still_drains_owned_private_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    put_entered, abort_entered = asyncio.Event(), asyncio.Event()
    release_abort = asyncio.Event()
    abort_cancelled = asyncio.Event()
    original_put, original_abort = fixture.artifacts.put, fixture.artifacts.abort_write

    async def put(*args: Any, **kwargs: Any) -> None:
        await original_put(*args, **kwargs)
        put_entered.set()
        await asyncio.Event().wait()

    async def abort(*args: Any, **kwargs: Any) -> bool:
        abort_entered.set()
        try:
            await release_abort.wait()
            return await original_abort(*args, **kwargs)
        except asyncio.CancelledError:
            abort_cancelled.set()
            raise

    monkeypatch.setattr(fixture.artifacts, 'put', put)
    monkeypatch.setattr(fixture.artifacts, 'abort_write', abort)
    with fixture.bind():
        task = asyncio.create_task(verify(service))
        try:
            await asyncio.wait_for(put_entered.wait(), timeout=2)
            task.cancel()
            await asyncio.wait_for(abort_entered.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release_abort.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
            assert not abort_cancelled.is_set(), 'repeated cancel interrupted the owned abort'
            assert not fixture.artifacts._artifacts, 'uncommitted private receipt was not removed'
            assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'
        finally:
            release_abort.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_recipe_approval_exception_keeps_the_actual_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    request = CapabilityRequest(tenant_id=TENANT, run_id=RUN,
        user_id=fixture.db.row.actor_id, agent_id='main_agent', capability='command',
        operation='run', resource='workspace/current/project-verification/python/review',
        arguments={}, idempotency_key='recipe-review')
    error = PythonGeneratedRecipeApprovalRequired(request)

    async def needs_approval(**kwargs: Any) -> Mapping[str, Any]:
        raise error

    monkeypatch.setattr(service, '_verify', needs_approval)
    with fixture.bind(), pytest.raises(PythonGeneratedRecipeApprovalRequired) as caught:
        await verify(service)
    assert caught.value is error and caught.value.approval_request is request
    assert RECEIPT_KEY not in fixture.db.row.routing_decision
    assert fixture.executor.calls == []


async def test_cancelled_put_aborts_only_its_private_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture(tmp_path)
    service = fixture.verifier()
    other_run = uuid4()
    other = Artifact(id=uuid4(), type='text', producer='user', content={'text': 'keep'})
    await fixture.artifacts.put(TENANT, other_run, other)
    original_put = fixture.artifacts.put

    async def cancelled_put(*args: Any, **kwargs: Any) -> None:
        await original_put(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(fixture.artifacts, 'put', cancelled_put)
    with fixture.bind(), pytest.raises(asyncio.CancelledError):
        await verify(service)
    reference = ArtifactReference(id=other.id, sha256=other.content_sha256)
    assert await fixture.artifacts.get_many(TENANT, other_run, (reference,)) == (other,)
    assert len(fixture.artifacts._artifacts) == 1
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'running'


class PreparationRuns(Runs):
    updates = 0

    async def compare_and_set_generated_preparation(
        self, tenant_id: UUID, run_id: UUID, **kwargs: Any,
    ) -> object:
        row = self.db.row
        assert (tenant_id, run_id, kwargs['actor_id'], kwargs['lease_token']) == (
            row.tenant_id, row.id, row.actor_id, row.worker_lease_token)
        assert row.routing_decision.get('generated_project_preparation') == kwargs['expected']
        row.routing_decision = {**row.routing_decision,
                                'generated_project_preparation': kwargs['replacement']}
        self.updates += 1
        return await self.get(tenant_id, run_id)


@pytest.mark.parametrize('draft_missing', [False, True])
async def test_generated_preparation_stabilizes_before_begin_and_restore_is_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft_missing: bool,
) -> None:
    backend, generated_runs = await configured(tmp_path / 'generated')
    native_service = backend._project_verification
    assert native_service is not None and native_service.executor is not None
    fixture = Fixture(tmp_path / 'durable')
    fixture.root = native_service.root
    fixture.db.row.actor_id = generated_runs.row.actor_id
    fixture.db.row.request = generated_runs.row.request
    fixture.db.row.routing_decision = dict(generated_runs.row.routing_decision)
    runs = PreparationRuns(fixture.db)
    fixture.runs = runs
    if draft_missing:
        from tests.unit.capabilities.test_scoped_read import project_path

        (fixture.root / project_path() / 'ACCEPTANCE.json').unlink()

    async def native_run(invocation: Any) -> Any:
        fixture.executor.calls.append(invocation)
        current = fixture.db.row.routing_decision[RECEIPT_KEY]
        assert current['status'] == 'running'
        assert fixture.db.row.routing_decision['generated_project_preparation']['phase'] == 'captured'
        assert current['authority']['scope_sha256'] == fixture.db.authority().scope_sha256
        assert native_service.executor is not None
        return await native_service.executor.run(invocation)

    monkeypatch.setattr(fixture.executor, 'run', native_run)
    warm = fixture.verifier()
    assert 'generated_project_preparation' not in fixture.db.row.routing_decision
    with fixture.bind():
        if draft_missing:
            with pytest.raises(GeneratedProjectDraftRejected) as caught:
                await verify(warm)
            assert caught.value.rejection.reason == 'generated_acceptance_manifest_required'
            assert RECEIPT_KEY not in fixture.db.row.routing_decision
            assert runs.updates == 0 and fixture.executor.calls == []
            return
        result = await verify(warm)
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['status'] == 'passed'
    assert runs.updates == 2
    fixture.db.new_lease()
    cold = fixture.verifier()

    async def forbidden_prepare(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('cold recovery must not rerun mutable preparation')

    monkeypatch.setattr(cold.generated, 'prepare', forbidden_prepare)
    with fixture.bind():
        assert await restore(cold) == result
    assert runs.updates == 2 and len(fixture.executor.calls) == 1
