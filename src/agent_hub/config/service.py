import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

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


def _validated_document(document: object) -> dict[str, object]:
    config = PlatformConfig.model_validate(document)
    serialized = config.model_dump_json(exclude_unset=True)
    loaded = json.loads(serialized)
    if not isinstance(loaded, dict):
        raise TypeError("platform configuration must serialize to an object")
    return loaded


class ConfigService:
    def __init__(
        self,
        repository: ConfigRepository | None = None,
        notifier: ConfigPublishedNotifier | None = None,
    ) -> None:
        self._repository = repository or ConfigRepository()
        self._notifier = notifier or NoopConfigPublishedNotifier()

    async def create_draft(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: UUID,
        document: object,
    ) -> ConfigRevision:
        validated = _validated_document(document)
        async with session.begin():
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
        session: AsyncSession,
        tenant_id: UUID,
        version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        async with session.begin():
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

            current = await self._repository.get_current_row(
                session, tenant_id, for_update=True
            )
            if current is not None and version <= current.version:
                raise ConfigStatusError(
                    f"configuration version {version} is older than the current published version"
                )
            if current is not None:
                self._repository.set_status(current, ConfigStatus.SUPERSEDED)
            self._repository.set_status(target, ConfigStatus.PUBLISHED)
            await session.flush()
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
        session: AsyncSession,
        tenant_id: UUID,
        source_version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        async with session.begin():
            await self._repository.lock_tenant(session, tenant_id)
            source = await self._repository.get_version(
                session, tenant_id, source_version, for_update=True
            )
            if source is None:
                raise ConfigNotFoundError(
                    f"configuration version {source_version} was not found for tenant {tenant_id}"
                )
            if source.status == ConfigStatus.DRAFT.value:
                raise ConfigStatusError("cannot roll back from a draft revision")

            current = await self._repository.get_current_row(
                session, tenant_id, for_update=True
            )
            if current is not None:
                self._repository.set_status(current, ConfigStatus.SUPERSEDED)
            version = await self._repository.next_version(session, tenant_id)
            rolled_back = await self._repository.create(
                session,
                tenant_id=tenant_id,
                version=version,
                status=ConfigStatus.PUBLISHED,
                document=source.document,
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
        self, session: AsyncSession, tenant_id: UUID, version: int
    ) -> ConfigRevision:
        revision = await self._repository.read_version(session, tenant_id, version)
        if revision is None:
            raise ConfigNotFoundError(
                f"configuration version {version} was not found for tenant {tenant_id}"
            )
        return revision

    async def list_versions(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[ConfigRevision]:
        return await self._repository.list_versions(session, tenant_id)

    async def get_current(
        self, session: AsyncSession, tenant_id: UUID
    ) -> ConfigRevision | None:
        return await self._repository.get_current(session, tenant_id)

    async def _notify_after_commit(self, event: ConfigPublishedEvent) -> None:
        try:
            await self._notifier.notify(event)
        except Exception as error:
            raise PostCommitNotificationError(event) from error
