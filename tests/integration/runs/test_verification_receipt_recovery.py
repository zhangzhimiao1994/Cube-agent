"""Real PostgreSQL fencing and real offline Node phases, not user-mode acceptance."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.db.models import RunRow
from agent_hub.db.session import Database, build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.project_verification_receipts import (
    ProjectVerificationReceiptRepository,
    VerificationAuthority,
    VerificationRecord,
)
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import Artifact, TaskContext, _freeze_object
from tests.unit.runtime.test_direct_project_verification import NodeFixtureExecutor


@dataclass
class Store:
    database: Database
    url: str
    tenant: UUID
    run: UUID

    async def context(self) -> TaskContext:
        record = await RunRepository(self.database.session_factory).get(self.tenant, self.run)
        assert record.mode is not None and record.routing_decision is not None
        return TaskContext(tenant_id=self.tenant, run_id=self.run, actor_id=record.actor_id,
            actor_role=record.actor_role, execution_token=record.worker_lease_token,
            mode=record.mode, request=record.request,
            routing_decision=_freeze_object(record.routing_decision, name='test scope'))

    async def authority(self) -> VerificationAuthority:
        return VerificationAuthority.from_context(await self.context(), worker_id='receipt-worker', actor='main_agent')


@pytest.fixture
async def receipt_store(database_url: str) -> AsyncIterator[Store]:
    database = build_database(database_url)
    assert database.engine.dialect.name == 'postgresql'
    repository = RunRepository(database.session_factory)
    tenant = uuid4()
    record = await repository.create_run(tenant_id=tenant, actor_id=uuid4(),
        request='Build and test current project', mode=TaskMode.DIRECT, status=RunStatus.RUNNING,
        idempotency_key=None, routing_decision={'project_id': 'native-receipt',
            'workspace_session_id': 'same-task', 'sandbox_profile': 'workspace_write',
            'execution_backend': 'systemd', 'permission_mode': 'default',
            'requested_permissions': ['workspace.read', 'workspace.write', 'command.run', 'network.read'],
            'runtime_timeout_seconds': 60}, enqueue=False)
    async with database.session_factory() as session, session.begin():
        row = await session.get(RunRow, record.id)
        assert row is not None
        row.actor_role = Role.OPERATOR.value
        row.worker_id, row.worker_lease_token = 'receipt-worker', uuid4()
        row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    try:
        yield Store(database, database_url, tenant, record.id)
    finally:
        async with database.session_factory() as session, session.begin():
            await session.execute(delete(RunRow).where(RunRow.id == record.id, RunRow.tenant_id == tenant))
        await database.dispose()


async def test_two_connections_allow_one_native_attempt_and_never_replace_unknown(receipt_store: Store) -> None:
    store = receipt_store
    other = build_database(store.url)
    try:
        left = ProjectVerificationReceiptRepository(store.database.session_factory)
        right = ProjectVerificationReceiptRepository(other.session_factory)
        authority = await store.authority()
        results = await asyncio.gather(
            left.begin(authority, action_key='first', execution_id='first_exec', source_sha256='a'*64),
            right.begin(authority, action_key='second', execution_id='second_exec', source_sha256='a'*64),
            return_exceptions=True)
        assert sum(isinstance(result, RunConflict) for result in results) == 1
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        with pytest.raises(RunConflict):
            await right.begin(authority, action_key='third', execution_id='third_exec', source_sha256='a'*64)
    finally:
        await other.dispose()


async def test_waiting_completion_rechecks_replaced_lease_after_actual_row_lock(receipt_store: Store) -> None:
    store = receipt_store
    tag = 'receipt-wait-' + uuid4().hex
    engine = create_async_engine(store.url, connect_args={'server_settings': {'application_name': tag}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    writer = ProjectVerificationReceiptRepository(sessions)
    task: asyncio.Future[VerificationRecord] | None = None
    try:
        authority = await store.authority()
        issued = await writer.begin(authority, action_key='verify', execution_id='verify_exec', source_sha256='b'*64)
        artifact = Artifact(id=uuid4(), type='text', producer='trusted_verifier', content={'text': 'lock test only'})
        async with store.database.session_factory() as holder, holder.begin():
            row = await holder.scalar(select(RunRow).where(RunRow.id == store.run).with_for_update())
            assert row is not None
            task = asyncio.ensure_future(writer.complete(authority, expected=issued, status='passed',
                artifact_references=(ArtifactReference(id=artifact.id, sha256=artifact.content_sha256),)))
            async with asyncio.timeout(5):
                while True:
                    async with store.database.session_factory() as observer:
                        waiting = await observer.scalar(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:tag AND wait_event_type='Lock'"),
                            {'tag': tag})
                    if waiting:
                        break
                    assert not task.done()
                    await asyncio.sleep(.01)
            row.worker_id, row.worker_lease_token = 'replacement-worker', uuid4()
        with pytest.raises(RunConflict):
            await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


async def test_real_install_build_test_restores_from_fresh_postgres_pool_without_reexecution(
    receipt_store: Store, tmp_path: Path,
) -> None:
    store = receipt_store
    root = tmp_path / 'projects'
    source = root / str(store.tenant) / 'projects/native-receipt/sessions/same-task'
    source.mkdir(parents=True)
    (source / 'package.json').write_text(json.dumps({'name': 'native-receipt', 'version': '1.0.0',
        'scripts': {'build': 'node build.cjs', 'test': 'node main.cjs'}}), encoding='utf-8')
    (source / 'build.cjs').write_text("console.log('BUILD_PASS');", encoding='utf-8')
    (source / 'main.cjs').write_text("require('node:assert/strict').equal(2+3, 5); console.log('BUSINESS_PASS');", encoding='utf-8')
    executor = NodeFixtureExecutor(root, tmp_path / 'executions')

    def verifier(database: Database) -> ProjectVerificationService:
        return ProjectVerificationService(root=root, repository=RunRepository(database.session_factory), executor=executor,
            receipt_repository=ProjectVerificationReceiptRepository(database.session_factory),
            artifact_repository=PostgresArtifactRepository(database.session_factory))

    context = await store.context()
    with bind_execution_authority(context, worker_id='receipt-worker'):
        result = await verifier(store.database).verify(tenant_id=store.tenant, run_id=store.run,
                                                       actor='main_agent', action_key='native-verification')
    async with store.database.session_factory() as session, session.begin():
        row = await session.get(RunRow, store.run)
        assert row is not None
        row.worker_lease_token = uuid4()
    fresh = build_database(store.url)
    try:
        cold = verifier(fresh)
        with bind_execution_authority(await store.context(), worker_id='receipt-worker'):
            assert await cold.restore(tenant_id=store.tenant, run_id=store.run,
                actor='main_agent', action_key='native-verification') == result
            assert await cold.require_current_receipt(store.tenant, store.run)
            (source / 'main.cjs').write_text('changed after success', encoding='utf-8')
            with pytest.raises(ProjectVerificationError):
                await cold.require_current_receipt(store.tenant, store.run)
        assert len(executor.invocations) == 1
        assert [check['status'] for check in executor.reports[0]['checks']] == ['passed'] * 3
    finally:
        await fresh.dispose()
