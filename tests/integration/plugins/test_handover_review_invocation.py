"""Real SQL invocation admission/lock lifetime; no adapter or target code executes."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import cast

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
    PersistentAdminResourceService,
    PluginResourceResponse,
    _plugin_response_from_payload,
)
from agent_hub.config.service import ConfigService
from agent_hub.db.models import AdminResourceRow
from agent_hub.security.secrets import SecretService
from tests.integration.plugins import test_handover_reviews as review_tests

pytestmark = review_tests.pytestmark
sql_context = review_tests.sql_context
context = review_tests.context


async def prepared(c: review_tests.Context) -> tuple[PersistentAdminResourceService, PluginResourceResponse]:
    report = await c.scan()
    await c.approve(report)
    async with c.sql.factory() as session, session.begin():
        row = await c.plugin(session, lock=True)
        payload = deepcopy(row.payload)
        # This admission fixture never starts a scan_only package or invokes an adapter.
        payload.update(enabled=True, status="running", health="healthy")
        await session.execute(update(AdminResourceRow).where(AdminResourceRow.id == row.id).values(payload=payload))
    service = PersistentAdminResourceService(
        config_service=cast(ConfigService, object()), secret_service=cast(SecretService, object()),
        tenant_id=c.sql.installer.tenant_id, actor_id=c.sql.installer.user_id,
        session_factory=c.sql.factory,
        generated_plugin_approval_validator=c.service.validate_generated_approval_in_session,
    )
    return service, _plugin_response_from_payload(payload)


async def test_actual_invocation_keeps_source_grant_locked_through_execution_scope(
    context: review_tests.Context,
) -> None:
    c = context
    service, cached = await prepared(c)
    grant = await c.sql.grant()
    query = select(AdminResourceRow).where(
        AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
        AdminResourceRow.kind == "plugin_handover", AdminResourceRow.resource_id == grant.grant_id,
    ).with_for_update(nowait=True)
    async with service.plugin_invocation_scope(cached, tenant_id=c.sql.creator.tenant_id), c.sql.factory() as competing:
        with pytest.raises(DBAPIError):
            await competing.scalar(query)
        await competing.rollback()
    async with c.sql.factory() as competing, competing.begin():
        assert await competing.scalar(query) is not None


@pytest.mark.parametrize("change", ["grant", "configuration", "disabled"])
async def test_actual_invocation_rechecks_after_cache_warmup(
    context: review_tests.Context, change: str,
) -> None:
    c = context
    service, cached = await prepared(c)
    async with service.plugin_invocation_scope(cached, tenant_id=c.sql.creator.tenant_id):
        pass
    async with c.sql.factory() as session, session.begin():
        if change == "grant":
            grant = await c.sql.grant()
            await c.sql.grants.revoke(session, c.sql.creator, grant_id=grant.grant_id)
        else:
            row = await c.plugin(session, lock=True)
            payload = deepcopy(row.payload)
            payload.update({"description": "changed"} if change == "configuration" else {"enabled": False})
            await session.execute(update(AdminResourceRow).where(AdminResourceRow.id == row.id).values(payload=payload))
    with pytest.raises(PublicAPIError):
        async with service.plugin_invocation_scope(cached, tenant_id=c.sql.creator.tenant_id):
            pytest.fail("stale cache admitted")


async def test_actual_cancelled_invocation_releases_registration_lock(context: review_tests.Context) -> None:
    c = context
    service, cached = await prepared(c)
    entered = asyncio.Event()

    async def call() -> None:
        async with service.plugin_invocation_scope(cached, tenant_id=c.sql.creator.tenant_id):
            entered.set()
            await asyncio.Future[None]()

    task = asyncio.create_task(call())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with c.sql.factory() as session, session.begin():
        query = select(AdminResourceRow).where(
            AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
            AdminResourceRow.kind == "plugin", AdminResourceRow.resource_id == cached.id,
        ).with_for_update(nowait=True)
        assert await session.scalar(query) is not None
