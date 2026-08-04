import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.config.repository import (
    ConfigNotFoundError,
    ConfigRepository,
    ConfigStatus,
    ConfigStatusError,
)
from agent_hub.config.service import (
    ConfigPublishedEvent,
    ConfigService,
    PostCommitNotificationError,
)
from agent_hub.db.models import ConfigRevisionRow, TenantRow
from agent_hub.db.session import Database, build_database


def document(model_name: str = "primary", prompt: str = "Be helpful.") -> dict[str, object]:
    return {
        "models": {
            model_name: {
                "deployments": [
                    {
                        "provider": "openai",
                        "model": "gpt-5",
                        "secret_ref": "OPENAI_API_KEY",
                        "quota_scope_id": "primary",
                    }
                ]
            }
        },
        "agents": [
            {
                "id": "assistant",
                "role": "assistant",
                "prompt": prompt,
                "model": model_name,
            }
        ],
    }


class RecordingNotifier:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.events: list[ConfigPublishedEvent] = []
        self.committed_versions_seen: list[int | None] = []

    async def notify(self, event: ConfigPublishedEvent) -> None:
        async with self.session_factory() as observer:
            current = await ConfigRepository().get_current(observer, event.tenant_id)
        self.committed_versions_seen.append(None if current is None else current.version)
        self.events.append(event)


class FailingNotifier:
    async def notify(self, event: ConfigPublishedEvent) -> None:
        raise RuntimeError(f"delivery failed for {event.version}")


class CollectingNotifier:
    def __init__(self) -> None:
        self.events: list[ConfigPublishedEvent] = []

    async def notify(self, event: ConfigPublishedEvent) -> None:
        self.events.append(event)


class InvalidPublishStatusRepository(ConfigRepository):
    def set_status(
        self,
        row: ConfigRevisionRow,
        status: Literal[ConfigStatus.PUBLISHED, ConfigStatus.SUPERSEDED],
    ) -> None:
        if status is ConfigStatus.PUBLISHED:
            row.status = "archived"
            return
        super().set_status(row, status)


@asynccontextmanager
async def database_resource(database_url: str) -> AsyncIterator[Database]:
    database = build_database(database_url)
    try:
        yield database
    finally:
        await database.dispose()


async def create_tenant(session: AsyncSession, slug: str) -> UUID:
    tenant = TenantRow(slug=slug, name=slug.title())
    session.add(tenant)
    await session.commit()
    return tenant.id


@pytest.mark.integration
async def test_create_publish_starts_at_one_and_notifies_after_commit(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "first")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        notifier = RecordingNotifier(database.session_factory)
        service = ConfigService(ConfigRepository(), notifier)
        async with database.session_factory() as session:
            draft = await service.create_draft(session, tenant_id, actor_id, document())
            published = await service.publish(session, tenant_id, draft.version, actor_id)
            current = await service.get_current(session, tenant_id)

    assert draft.version == 1
    assert draft.status == "draft"
    assert published.version == 1
    assert published.status == "published"
    assert current == published
    assert notifier.committed_versions_seen == [1]
    assert notifier.events == [
        ConfigPublishedEvent(
            tenant_id=tenant_id,
            version=1,
            actor_id=actor_id,
            action="publish",
        )
    ]


@pytest.mark.integration
async def test_concurrent_drafts_get_distinct_versions_and_tenants_start_independently(
    db_session: AsyncSession, database_url: str
) -> None:
    first_tenant = await create_tenant(db_session, "concurrent-first")
    second_tenant = await create_tenant(db_session, "concurrent-second")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService()

        async def create(tenant_id: UUID) -> int:
            async with database.session_factory() as session:
                revision = await service.create_draft(session, tenant_id, actor_id, document())
                return revision.version

        first_versions = await asyncio.gather(create(first_tenant), create(first_tenant))
        second_version = await create(second_tenant)

    assert sorted(first_versions) == [1, 2]
    assert second_version == 1


@pytest.mark.integration
async def test_new_publish_supersedes_current_without_changing_its_document(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "supersede")
    actor_id = uuid4()
    first_document = document(prompt="First immutable prompt.")
    async with database_resource(database_url) as database:
        service = ConfigService()
        async with database.session_factory() as session:
            first = await service.create_draft(session, tenant_id, actor_id, first_document)
            await service.publish(session, tenant_id, first.version, actor_id)
            old_draft = await service.create_draft(
                session, tenant_id, actor_id, document(prompt="Skipped prompt.")
            )
            newest = await service.create_draft(
                session, tenant_id, actor_id, document(prompt="Newest prompt.")
            )
            await service.publish(session, tenant_id, newest.version, actor_id)

            with pytest.raises(ConfigStatusError, match="older than the current published"):
                await service.publish(session, tenant_id, old_draft.version, actor_id)

            stored_first = await service.get_version(session, tenant_id, first.version)
            versions = await service.list_versions(session, tenant_id)

    assert stored_first.status == "superseded"
    assert stored_first.document == first_document
    assert [(item.version, item.status) for item in versions] == [
        (1, "superseded"),
        (2, "draft"),
        (3, "published"),
    ]


@pytest.mark.integration
async def test_returned_documents_cannot_mutate_stored_history(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "deep-copy")
    actor_id = uuid4()
    raw = document()
    expected = deepcopy(raw)
    async with database_resource(database_url) as database:
        service = ConfigService()
        async with database.session_factory() as session:
            created = await service.create_draft(session, tenant_id, actor_id, raw)
            raw["agents"] = []
            created.document["agents"] = []
            reread = await service.get_version(session, tenant_id, created.version)
            reread.document["agents"] = []
            read_again = await service.get_version(session, tenant_id, created.version)

    assert read_again.document == expected


@pytest.mark.integration
async def test_rollback_copies_history_into_new_published_version(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "rollback")
    actor_id = uuid4()
    first_document = document(prompt="First prompt.")
    second_document = document(prompt="Second prompt.")
    async with database_resource(database_url) as database:
        notifier = RecordingNotifier(database.session_factory)
        service = ConfigService(ConfigRepository(), notifier)
        async with database.session_factory() as session:
            first = await service.create_draft(session, tenant_id, actor_id, first_document)
            await service.publish(session, tenant_id, first.version, actor_id)
            second = await service.create_draft(session, tenant_id, actor_id, second_document)
            await service.publish(session, tenant_id, second.version, actor_id)
            rolled_back = await service.rollback(session, tenant_id, first.version, actor_id)
            stored_first = await service.get_version(session, tenant_id, first.version)
            stored_second = await service.get_version(session, tenant_id, second.version)

    assert rolled_back.version == 3
    assert rolled_back.status == "published"
    assert rolled_back.created_by == actor_id
    assert rolled_back.document == first_document
    assert stored_first.status == "superseded"
    assert stored_first.document == first_document
    assert stored_second.status == "superseded"
    assert stored_second.document == second_document
    assert [event.action for event in notifier.events] == ["publish", "publish", "rollback"]


@pytest.mark.integration
async def test_invalid_status_and_missing_versions_do_not_notify(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "errors")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        notifier = RecordingNotifier(database.session_factory)
        service = ConfigService(ConfigRepository(), notifier)
        async with database.session_factory() as session:
            draft = await service.create_draft(session, tenant_id, actor_id, document())
            with pytest.raises(ConfigStatusError, match="draft revision"):
                await service.rollback(session, tenant_id, draft.version, actor_id)
            with pytest.raises(ConfigNotFoundError):
                await service.publish(session, tenant_id, 999, actor_id)
            assert notifier.events == []

            await service.publish(session, tenant_id, draft.version, actor_id)
            with pytest.raises(ConfigStatusError, match="only draft"):
                await service.publish(session, tenant_id, draft.version, actor_id)

    assert len(notifier.events) == 1


@pytest.mark.integration
async def test_notification_failure_is_reported_after_commit(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "notification-failure")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService(ConfigRepository(), FailingNotifier())
        async with database.session_factory() as session:
            draft = await service.create_draft(session, tenant_id, actor_id, document())
            with pytest.raises(PostCommitNotificationError, match="committed"):
                await service.publish(session, tenant_id, draft.version, actor_id)

        async with database.session_factory() as observer:
            current = await ConfigService().get_current(observer, tenant_id)

    assert current is not None
    assert current.version == draft.version
    assert current.status == "published"


@pytest.mark.integration
async def test_database_rejects_configuration_status_outside_state_machine(
    db_session: AsyncSession,
) -> None:
    tenant_id = await create_tenant(db_session, "invalid-database-status")
    db_session.add(
        ConfigRevisionRow(
            tenant_id=tenant_id,
            version=1,
            status="archived",
            document={"models": {}, "agents": []},
            created_by=uuid4(),
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_publish_flush_failure_rolls_back_and_does_not_notify(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "publish-flush-failure")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        async with database.session_factory() as session:
            draft = await ConfigService().create_draft(
                session, tenant_id, actor_id, document()
            )
            notifier = CollectingNotifier()
            service = ConfigService(InvalidPublishStatusRepository(), notifier)

            with pytest.raises(IntegrityError):
                await service.publish(session, tenant_id, draft.version, actor_id)

        async with database.session_factory() as observer:
            stored = await ConfigService().get_version(observer, tenant_id, draft.version)
            current = await ConfigService().get_current(observer, tenant_id)

    assert stored.status == "draft"
    assert current is None
    assert notifier.events == []
