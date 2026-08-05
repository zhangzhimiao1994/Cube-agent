import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.config.repository import (
    ConfigNotFoundError,
    ConfigRepository,
    ConfigRevision,
    ConfigStatus,
    ConfigStatusError,
)
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import (
    ConfigPublishedEvent,
    ConfigService,
    ConfigValidationError,
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


def normalized(document_value: dict[str, object]) -> dict[str, object]:
    return PlatformConfig.model_validate(document_value).model_dump(mode="json")


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
        service = ConfigService(database.session_factory, ConfigRepository(), notifier)
        draft = await service.create_draft(tenant_id, actor_id, document())
        published = await service.publish(tenant_id, draft.version, actor_id)
        current = await service.get_current(tenant_id)

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
        service = ConfigService(database.session_factory)

        async def create(tenant_id: UUID) -> int:
            revision = await service.create_draft(tenant_id, actor_id, document())
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
        service = ConfigService(database.session_factory)
        first = await service.create_draft(tenant_id, actor_id, first_document)
        await service.publish(tenant_id, first.version, actor_id)
        old_draft = await service.create_draft(
            tenant_id, actor_id, document(prompt="Skipped prompt.")
        )
        newest = await service.create_draft(
            tenant_id, actor_id, document(prompt="Newest prompt.")
        )
        await service.publish(tenant_id, newest.version, actor_id)

        with pytest.raises(ConfigStatusError, match="older than the current published"):
            await service.publish(tenant_id, old_draft.version, actor_id)

        stored_first = await service.get_version(tenant_id, first.version)
        versions = await service.list_versions(tenant_id)

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
        service = ConfigService(database.session_factory)
        created = await service.create_draft(tenant_id, actor_id, raw)
        raw["agents"] = []
        created.document["agents"] = []
        reread = await service.get_version(tenant_id, created.version)
        reread.document["agents"] = []
        read_again = await service.get_version(tenant_id, created.version)

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
        service = ConfigService(database.session_factory, ConfigRepository(), notifier)
        first = await service.create_draft(tenant_id, actor_id, first_document)
        await service.publish(tenant_id, first.version, actor_id)
        second = await service.create_draft(tenant_id, actor_id, second_document)
        await service.publish(tenant_id, second.version, actor_id)
        rolled_back = await service.rollback(tenant_id, first.version, actor_id)
        stored_first = await service.get_version(tenant_id, first.version)
        stored_second = await service.get_version(tenant_id, second.version)

    assert rolled_back.version == 3
    assert rolled_back.status == "published"
    assert rolled_back.created_by == actor_id
    assert rolled_back.document == normalized(first_document)
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
        service = ConfigService(database.session_factory, ConfigRepository(), notifier)
        draft = await service.create_draft(tenant_id, actor_id, document())
        with pytest.raises(ConfigStatusError, match="draft revision"):
            await service.rollback(tenant_id, draft.version, actor_id)
        with pytest.raises(ConfigNotFoundError):
            await service.publish(tenant_id, 999, actor_id)
        assert notifier.events == []

        await service.publish(tenant_id, draft.version, actor_id)
        with pytest.raises(ConfigStatusError, match="only draft"):
            await service.publish(tenant_id, draft.version, actor_id)

    assert len(notifier.events) == 1


@pytest.mark.integration
async def test_notification_failure_is_reported_after_commit(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "notification-failure")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService(
            database.session_factory, ConfigRepository(), FailingNotifier()
        )
        draft = await service.create_draft(tenant_id, actor_id, document())
        with pytest.raises(PostCommitNotificationError, match="committed"):
            await service.publish(tenant_id, draft.version, actor_id)

        current = await ConfigService(database.session_factory).get_current(tenant_id)

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
        draft = await ConfigService(database.session_factory).create_draft(
            tenant_id, actor_id, document()
        )
        notifier = CollectingNotifier()
        service = ConfigService(
            database.session_factory, InvalidPublishStatusRepository(), notifier
        )

        with pytest.raises(IntegrityError):
            await service.publish(tenant_id, draft.version, actor_id)

        reader = ConfigService(database.session_factory)
        stored = await reader.get_version(tenant_id, draft.version)
        current = await reader.get_current(tenant_id)

    assert stored.status == "draft"
    assert current is None
    assert notifier.events == []


@pytest.mark.integration
async def test_publish_rejects_invalid_persisted_document_without_state_change(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "invalid-publish-document")
    actor_id = uuid4()
    invalid_document = {
        "models": {},
        "agents": [
            {
                "id": "assistant",
                "role": "assistant",
                "prompt": "Help.",
                "model": "missing",
            }
        ],
    }
    async with database_resource(database_url) as database:
        async with database.session_factory() as session:
            session.add(
                ConfigRevisionRow(
                    tenant_id=tenant_id,
                    version=1,
                    status="draft",
                    document=invalid_document,
                    created_by=actor_id,
                )
            )
            await session.commit()

        notifier = CollectingNotifier()
        service = ConfigService(database.session_factory, notifier=notifier)
        with pytest.raises(ConfigValidationError) as error:
            await service.publish(tenant_id, 1, actor_id)

        stored = await service.get_version(tenant_id, 1)
        current = await service.get_current(tenant_id)

    assert isinstance(error.value.__cause__, ValidationError)
    assert stored.status == "draft"
    assert current is None
    assert notifier.events == []


@pytest.mark.integration
async def test_rollback_rejects_invalid_persisted_history_without_state_change(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "invalid-rollback-document")
    actor_id = uuid4()
    invalid_document = {"models": {}, "agents": [{"id": "broken"}]}
    async with database_resource(database_url) as database:
        setup_service = ConfigService(database.session_factory)
        current_revision = await setup_service.create_draft(
            tenant_id, actor_id, document()
        )
        await setup_service.publish(tenant_id, current_revision.version, actor_id)
        async with database.session_factory() as session:
            session.add(
                ConfigRevisionRow(
                    tenant_id=tenant_id,
                    version=2,
                    status="superseded",
                    document=invalid_document,
                    created_by=actor_id,
                )
            )
            await session.commit()

        notifier = CollectingNotifier()
        service = ConfigService(database.session_factory, notifier=notifier)
        with pytest.raises(ConfigValidationError) as error:
            await service.rollback(tenant_id, 2, actor_id)

        current = await service.get_current(tenant_id)
        source = await service.get_version(tenant_id, 2)
        versions = await service.list_versions(tenant_id)

    assert isinstance(error.value.__cause__, ValidationError)
    assert current is not None
    assert current.version == 1
    assert current.status == "published"
    assert source.status == "superseded"
    assert source.document == invalid_document
    assert [revision.version for revision in versions] == [1, 2]
    assert notifier.events == []


@pytest.mark.integration
async def test_rollback_normalizes_valid_direct_written_history(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "normalize-rollback-document")
    actor_id = uuid4()
    historical_document = document(prompt="Legacy prompt.")
    async with database_resource(database_url) as database:
        service = ConfigService(database.session_factory)
        current = await service.create_draft(tenant_id, actor_id, document())
        await service.publish(tenant_id, current.version, actor_id)
        async with database.session_factory() as session:
            session.add(
                ConfigRevisionRow(
                    tenant_id=tenant_id,
                    version=2,
                    status="superseded",
                    document=historical_document,
                    created_by=actor_id,
                )
            )
            await session.commit()

        rolled_back = await service.rollback(tenant_id, 2, actor_id)

    assert rolled_back.version == 3
    assert rolled_back.document == normalized(historical_document)


@pytest.mark.integration
async def test_public_read_then_write_uses_independent_session_lifecycles(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "read-then-write")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService(database.session_factory)
        draft = await service.create_draft(tenant_id, actor_id, document())
        read = await service.get_version(tenant_id, draft.version)
        published = await service.publish(tenant_id, read.version, actor_id)

    assert published.status == "published"


@pytest.mark.integration
async def test_database_allows_only_one_published_revision_per_tenant(
    db_session: AsyncSession,
) -> None:
    tenant_id = await create_tenant(db_session, "unique-current")
    actor_id = uuid4()
    db_session.add_all(
        [
            ConfigRevisionRow(
                tenant_id=tenant_id,
                version=version,
                status="published",
                document={"models": {}, "agents": []},
                created_by=actor_id,
            )
            for version in (1, 2)
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_concurrent_publish_has_one_winner_and_one_domain_error(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "concurrent-publish")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService(database.session_factory)
        draft = await service.create_draft(tenant_id, actor_id, document())

        results = await asyncio.gather(
            service.publish(tenant_id, draft.version, actor_id),
            service.publish(tenant_id, draft.version, actor_id),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, ConfigRevision)]
        domain_errors = [result for result in results if isinstance(result, ConfigStatusError)]
        unexpected_errors = [
            result
            for result in results
            if isinstance(result, BaseException)
            and not isinstance(result, ConfigStatusError)
        ]
        versions = await service.list_versions(tenant_id)
        current = await service.get_current(tenant_id)

    assert len(successes) == 1
    assert len(domain_errors) == 1
    assert unexpected_errors == []
    assert [(revision.version, revision.status) for revision in versions] == [
        (1, "published")
    ]
    assert current is not None
    assert current.version == 1


@pytest.mark.integration
async def test_concurrent_rollbacks_allocate_monotonic_versions_and_one_current(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "concurrent-rollback")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        setup = ConfigService(database.session_factory)
        first = await setup.create_draft(tenant_id, actor_id, document(prompt="First."))
        await setup.publish(tenant_id, first.version, actor_id)
        second = await setup.create_draft(tenant_id, actor_id, document(prompt="Second."))
        await setup.publish(tenant_id, second.version, actor_id)

        notifier = CollectingNotifier()
        service = ConfigService(database.session_factory, notifier=notifier)
        results = await asyncio.gather(
            service.rollback(tenant_id, first.version, actor_id),
            service.rollback(tenant_id, first.version, actor_id),
        )
        versions = await service.list_versions(tenant_id)
        current = await service.get_current(tenant_id)

    assert sorted(result.version for result in results) == [3, 4]
    assert [revision.version for revision in versions] == [1, 2, 3, 4]
    assert sum(revision.status == "published" for revision in versions) == 1
    assert current is not None
    assert current.version == 4
    assert [event.action for event in notifier.events] == ["rollback", "rollback"]


@pytest.mark.integration
async def test_publish_revision_uses_tenant_scoped_uuid(
    db_session: AsyncSession, database_url: str
) -> None:
    first_tenant = await create_tenant(db_session, "publish-by-id-first")
    second_tenant = await create_tenant(db_session, "publish-by-id-second")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService(database.session_factory)
        draft = await service.create_draft(first_tenant, actor_id, document())

        with pytest.raises(ConfigNotFoundError):
            await service.publish_revision(second_tenant, draft.id, actor_id)

        published = await service.publish_revision(first_tenant, draft.id, actor_id)

    assert published.id == draft.id
    assert published.status == "published"


@pytest.mark.integration
async def test_list_versions_applies_limit_and_offset(
    db_session: AsyncSession, database_url: str
) -> None:
    tenant_id = await create_tenant(db_session, "bounded-history")
    actor_id = uuid4()
    async with database_resource(database_url) as database:
        service = ConfigService(database.session_factory)
        for prompt in ("One.", "Two.", "Three."):
            await service.create_draft(tenant_id, actor_id, document(prompt=prompt))

        page = await service.list_versions(tenant_id, limit=1, offset=1)

    assert [revision.version for revision in page] == [2]
