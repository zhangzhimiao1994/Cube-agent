from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.channels.base import Channel
from agent_hub.db.models import UserRow


class PersistentChannelIdentityResolver:
    """Resolve channel identities to managed Agent Hub users when bound."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_actor_id(
        self,
        *,
        tenant_id: UUID,
        channel: Channel,
        sender_external_id: str,
    ) -> UUID | None:
        if channel is not Channel.FEISHU:
            return None
        async with self._session_factory() as session:
            user_id = await session.scalar(
                select(UserRow.id).where(
                    UserRow.tenant_id == tenant_id,
                    UserRow.feishu_open_id == sender_external_id,
                    UserRow.disabled.is_(False),
                )
            )
        return user_id


__all__ = ["PersistentChannelIdentityResolver"]
