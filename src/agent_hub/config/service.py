from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.config.repository import (
    ConfigNotFoundError,
    ConfigRepository,
    ConfigRevision,
    ConfigStatus,
    ConfigStatusError,
)
from agent_hub.config.schema import PlatformConfig

ConfigPublishedAction = Literal["publish", "rollback"]


@dataclass(frozen=True, slots=True)
class ConfigPublishedEvent:
    tenant_id: UUID
    version: int
    actor_id: UUID
    action: ConfigPublishedAction


class ConfigPublishedNotifier(Protocol):
    async def notify(self, event: ConfigPublishedEvent) -> None: ...


class NoopConfigPublishedNotifier:
    async def notify(self, event: ConfigPublishedEvent) -> None:
        return None


class PostCommitNotificationError(RuntimeError):
    def __init__(self, event: ConfigPublishedEvent) -> None:
        self.event = event
        super().__init__(
            f"configuration version {event.version} committed, but its notification failed"
        )


class ConfigValidationError(ValueError):
    pass


def _validated_document(
    document: object, *, include_defaults: bool = False
) -> dict[str, object]:
    try:
        config = PlatformConfig.model_validate(document)
    except ValidationError as error:
        raise ConfigValidationError("platform configuration is invalid") from error
    return config.model_dump(mode="json", exclude_unset=not include_defaults)


class ConfigService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: ConfigRepository | None = None,
        notifier: ConfigPublishedNotifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or ConfigRepository()
        self._notifier = notifier or NoopConfigPublishedNotifier()

    async def create_draft(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        document: object,
    ) -> ConfigRevision:
        validated = _validated_document(document)
        async with self._session_factory() as session, session.begin():
            await self._repository.lock_tenant(session, tenant_id)
            version = await self._repository.next_version(session, tenant_id)
            return await self._repository.create(
                session,
                tenant_id=tenant_id,
                version=version,
                status=ConfigStatus.DRAFT,
                document=validated,
                actor_id=actor_id,
            )

    async def publish(
        self,
        tenant_id: UUID,
        version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        async with self._session_factory() as session, session.begin():
            await self._repository.lock_tenant(session, tenant_id)
            target = await self._repository.get_version(
                session, tenant_id, version, for_update=True
            )
            if target is None:
                raise ConfigNotFoundError(
                    f"configuration version {version} was not found for tenant {tenant_id}"
                )
            if target.status != ConfigStatus.DRAFT.value:
                raise ConfigStatusError("only draft revisions can be published")
            _validated_document(target.document)

            current = await self._repository.get_current_row(
                session, tenant_id, for_update=True
            )
            if current is not None and version <= current.version:
                raise ConfigStatusError(
                    f"configuration version {version} is older than "
                    "the current published version"
                )
            if current is not None:
                self._repository.set_status(current, ConfigStatus.SUPERSEDED)
                await session.flush([current])
            self._repository.set_status(target, ConfigStatus.PUBLISHED)
            await session.flush([target])
            published = self._repository.materialize(target)

        await self._notify_after_commit(
            ConfigPublishedEvent(
                tenant_id=tenant_id,
                version=published.version,
                actor_id=actor_id,
                action="publish",
            )
        )
        return published

    async def rollback(
        self,
        tenant_id: UUID,
        source_version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        async with self._session_factory() as session, session.begin():
            await self._repository.lock_tenant(session, tenant_id)
            source = await self._repository.get_version(
                session, tenant_id, source_version, for_update=True
            )
            if source is None:
                raise ConfigNotFoundError(
                    f"configuration version {source_version} was not found "
                    f"for tenant {tenant_id}"
                )
            rollback_source_statuses = {
                ConfigStatus.PUBLISHED.value,
                ConfigStatus.SUPERSEDED.value,
            }
            if source.status not in rollback_source_statuses:
                raise ConfigStatusError(
                    "rollback source must be a published or superseded revision; "
                    f"{source.status} revision is not eligible"
                )
            validated_source = _validated_document(
                source.document, include_defaults=True
            )

            current = await self._repository.get_current_row(
                session, tenant_id, for_update=True
            )
            if current is not None:
                self._repository.set_status(current, ConfigStatus.SUPERSEDED)
                await session.flush([current])
            version = await self._repository.next_version(session, tenant_id)
            rolled_back = await self._repository.create(
                session,
                tenant_id=tenant_id,
                version=version,
                status=ConfigStatus.PUBLISHED,
                document=validated_source,
                actor_id=actor_id,
            )

        await self._notify_after_commit(
            ConfigPublishedEvent(
                tenant_id=tenant_id,
                version=rolled_back.version,
                actor_id=actor_id,
                action="rollback",
            )
        )
        return rolled_back

    async def get_version(
        self, tenant_id: UUID, version: int
    ) -> ConfigRevision:
        async with self._session_factory() as session:
            revision = await self._repository.read_version(session, tenant_id, version)
        if revision is None:
            raise ConfigNotFoundError(
                f"configuration version {version} was not found for tenant {tenant_id}"
            )
        return revision

    async def list_versions(
        self, tenant_id: UUID
    ) -> list[ConfigRevision]:
        async with self._session_factory() as session:
            return await self._repository.list_versions(session, tenant_id)

    async def get_current(
        self, tenant_id: UUID
    ) -> ConfigRevision | None:
        async with self._session_factory() as session:
            return await self._repository.get_current(session, tenant_id)

    async def _notify_after_commit(self, event: ConfigPublishedEvent) -> None:
        try:
            await self._notifier.notify(event)
        except Exception as error:
            raise PostCommitNotificationError(event) from error
