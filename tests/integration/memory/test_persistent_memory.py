from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import MemoryCreateRequest, PersistentAdminResourceService
from agent_hub.db.models import AdminResourceRow, TenantRow
from agent_hub.memory.persistent import PersistentRuntimeMemoryRecall


@pytest.mark.integration
async def test_persistent_memory_recall_is_scoped_and_read_only(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    other_actor_id = uuid4()
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"memory-{tenant_id.hex}", name="Memory"))
        session.add_all(
            [
                AdminResourceRow(
                    tenant_id=tenant_id,
                    kind="memory",
                    resource_id="project-policy",
                    payload={
                        "id": "project-policy",
                        "scope": f"user:{actor_id}",
                        "owner_actor_id": str(actor_id),
                        "value": "Use pytest for backend verification.",
                        "layer": "episodic",
                        "category": "fact",
                        "confidence": 0.9,
                        "heat": 0.5,
                        "recall_count": 0,
                        "project_id": "cube-agent",
                    },
                ),
                AdminResourceRow(
                    tenant_id=tenant_id,
                    kind="memory",
                    resource_id="other-user-policy",
                    payload={
                        "id": "other-user-policy",
                        "scope": f"user:{other_actor_id}",
                        "owner_actor_id": str(other_actor_id),
                        "value": "Use pytest for backend verification.",
                        "layer": "core",
                    },
                ),
            ]
        )

    recall = PersistentRuntimeMemoryRecall(auth_session_factory)
    selected = await recall.recall(
        tenant_id=tenant_id,
        actor_id=actor_id,
        query="verify backend with pytest",
        project_id="cube-agent",
        conversation_id="conv-1",
    )

    assert [item.id for item in selected] == ["project-policy"]
    async with auth_session_factory() as session:
        row = (
            await session.execute(
                select(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == tenant_id)
                .where(AdminResourceRow.resource_id == "project-policy")
            )
        ).scalar_one()
    assert row.payload["recall_count"] == 0
    assert row.payload["heat"] == 0.5
    assert "last_recalled_at" not in row.payload


@pytest.mark.integration
async def test_persistent_memory_concurrent_create_keeps_actor_ownership(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    first_actor = uuid4()
    second_actor = uuid4()
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"memory-race-{tenant_id.hex}", name="Memory race"))

    def service(actor_id: object) -> PersistentAdminResourceService:
        return PersistentAdminResourceService(
            config_service=cast(Any, None),
            secret_service=cast(Any, None),
            tenant_id=tenant_id,
            actor_id=cast(Any, actor_id),
            session_factory=auth_session_factory,
        )

    request = MemoryCreateRequest(
        id="same-memory-id",
        scope="user",
        value="Keep concurrent memory ownership isolated.",
    )
    results = await asyncio.gather(
        service(first_actor).create_memory(request),
        service(second_actor).create_memory(request),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PublicAPIError)
    assert failures[0].status_code == 409

    async with auth_session_factory() as session:
        row = (
            await session.execute(
                select(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == tenant_id)
                .where(AdminResourceRow.kind == "memory")
                .where(AdminResourceRow.resource_id == "same-memory-id")
            )
        ).scalar_one()
    assert row.payload["owner_actor_id"] in {str(first_actor), str(second_actor)}
