"""Opt-in PostgreSQL interleaving proof. B33_OFFLINE_REVIEW_PG_URL must use a local Unix socket."""

from __future__ import annotations

import os
from asyncio import Event, Task, create_task, current_task, gather, sleep, wait_for
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from agent_hub.api.routers.admin import PersistentAdminResourceService, PersistentToolApprovalMode
from agent_hub.auth.models import Role
from agent_hub.capabilities import python_project_inputs
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.python_project_service import python_recipe_marker
from agent_hub.db.base import Base
from agent_hub.db.models import (
    AdminResourceRow,
    RunApprovalRow,
    RunCheckpointRow,
    RunEventRow,
    RunRow,
    TenantRow,
    UserRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.original_python_offline_review import OriginalPythonOfflineReview
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.capabilities.test_python_project_inputs import bind


@pytest.mark.integration
async def test_original_receipt_consumption_serializes_with_system_setting_upsert() -> None:
    raw = os.getenv('B33_OFFLINE_REVIEW_PG_URL')
    if raw is None:
        pytest.skip('Set B33_OFFLINE_REVIEW_PG_URL for a dedicated local PostgreSQL Unix socket')
    url = make_url(raw)
    socket = url.query.get('host')
    if (url.drivername != 'postgresql+asyncpg' or url.host is not None or url.port is not None
            or set(url.query) - {'host', 'port'} or not isinstance(socket, str)
            or not PurePosixPath(socket).is_absolute() or '..' in PurePosixPath(socket).parts
            or ',' in socket or not url.database):
        pytest.fail('A PostgreSQL asyncpg URL with an explicit absolute Unix socket is required', pytrace=False)

    schema = 'b33_offline_' + uuid4().hex
    engine = create_async_engine(url, pool_size=3, max_overflow=0, hide_parameters=True,
                                 connect_args={'server_settings': {'application_name': schema}})
    scoped = engine.execution_options(schema_translate_map={None: schema})
    flushed, allow_commit = Event(), Event()
    consumer: Task[None] | None = None
    consumer_pid: int | None = None
    writer: Task[bool] | None = None

    class PausableSession(AsyncSession):
        async def flush(self, objects: Sequence[Any] | None = None) -> None:
            nonlocal consumer_pid
            await super().flush(objects)
            if current_task() is consumer and consumer is not None:
                pid = await self.scalar(text('SELECT pg_backend_pid()'))
                assert type(pid) is int
                consumer_pid = pid
                flushed.set()
                await allow_commit.wait()

    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        scoped, class_=PausableSession, expire_on_commit=False,
    )
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True
        async with scoped.begin() as connection:
            tables = [Base.metadata.tables[model.__tablename__] for model in (
                TenantRow, UserRow, RunRow, RunApprovalRow, RunCheckpointRow, RunEventRow, AdminResourceRow,
            )]
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))

        inputs = bind(python_project_inputs)
        tenant, run = inputs.contract.tenant_id, inputs.contract.run_id
        user, token = uuid4(), uuid4()
        route: dict[str, JsonValue] = {
            'project_id': inputs.contract.project_id, 'workspace_session_id': inputs.contract.session_id,
            'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd', 'permission_mode': 'auto_review',
            'requested_permissions': ('workspace.read', 'workspace.write', 'command.run'),
            'project_input_contract_version': 1,
            'project_input': cast(dict[str, JsonValue], ManagedProjectInput(inputs.contract.editable_paths).to_payload()),
            'project_input_preparation': {
                'schema_version': 2, 'phase': 'captured',
                'reference': cast(dict[str, JsonValue], inputs.original_reference.to_payload()),
                'python_recipe': python_recipe_marker(inputs),
            },
        }
        async with factory() as session, session.begin():
            session.add(TenantRow(id=tenant, name='Offline concurrency fixture', slug='offline-fixture'))
            await session.flush()
            session.add(UserRow(id=user, tenant_id=tenant, username='offline-operator', role=Role.OPERATOR.value))
            session.add(RunRow(id=run, tenant_id=tenant, actor_id=user, actor_role=Role.OPERATOR.value,
                request='Offline fixture', mode=TaskMode.DISCUSS.value, status=RunStatus.RUNNING.value,
                routing_decision=route, worker_id='offline-worker', worker_lease_token=token,
                worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=2)))
        context = TaskContext(tenant_id=tenant, run_id=run, actor_id=user, actor_role=Role.OPERATOR,
                              request='Offline fixture', execution_token=token, mode=TaskMode.DISCUSS,
                              routing_decision=route)
        repository = RunRepository(factory)
        service = PersistentAdminResourceService.__new__(PersistentAdminResourceService)
        service._tenant_id = service._actor_id = tenant
        service._session_factory = factory
        reader = PersistentToolApprovalMode(service)
        policy = DefaultRuntimeCapabilityPolicyGateway(
            ApprovalService(InMemoryApprovalStore()), repository, tool_approval_mode=reader)
        request = inputs.approval_request(user_id=user, agent_id='main_agent',
            idempotency_key='original-project-admission-' + run.hex)
        review = OriginalPythonOfflineReview(repository, policy, request, Role.OPERATOR, inputs)

        async def mode(value: str) -> bool:
            return await service._upsert_admin_payload('setting', 'system', {'tool_approval_mode': value})

        with bind_execution_authority(context, worker_id='offline-worker'):
            assert await mode('auto_review') and await review.eligible()
            async with factory() as session, session.begin():
                row = await repository.get_for_update(session, run)
                await review.reserve(session, row)
                captured = cast(dict[str, JsonValue], route['project_input_preparation'])
                row.routing_decision = {**route, 'project_input_preparation': {
                    **captured, 'phase': 'executing', 'execution_id': review.execution_id}}
                row.version += 1

            # Writer commits first. The exact same reserved receipt cannot be consumed.
            assert await mode('ask')
            with pytest.raises(RunConflict, match='install_approval_required'):
                await review.verify(repository, inputs, actor='main_agent', execution_id=review.execution_id,
                                    consume=True)
            assert await mode('auto_review')

            # Consumer has flushed, while the transaction (including the settings lock) is open.
            consumer = create_task(review.verify(
                repository, inputs, actor='main_agent', execution_id=review.execution_id, consume=True))
            await wait_for(flushed.wait(), timeout=5)
            assert consumer_pid is not None
            blocking_pid = consumer_pid
            writer = create_task(mode('ask'))
            async with factory() as observer:
                async def writer_is_blocked() -> None:
                    assert writer is not None
                    while not await observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE application_name = :name AND :blocker = ANY(pg_blocking_pids(pid)))"
                    ), {'name': schema, 'blocker': blocking_pid}):
                        assert not writer.done(), 'settings update escaped the consume transaction'
                        await observer.rollback()
                        await sleep(0.02)

                await wait_for(writer_is_blocked(), timeout=5)
                assert not consumer.done() and not writer.done()
                assert await observer.scalar(select(RunApprovalRow.status)) == 'reserved'
            allow_commit.set()
            await wait_for(consumer, timeout=5)
            assert await wait_for(writer, timeout=5)
            async with factory() as observer:
                assert await observer.scalar(select(RunApprovalRow.status)) == 'consumed'
            assert await reader(tenant) == 'ask'
    finally:
        allow_commit.set()
        tasks = [task for task in (consumer, writer) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await gather(*tasks, return_exceptions=True)
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
        finally:
            await engine.dispose()
