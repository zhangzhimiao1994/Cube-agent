from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.feishu_oauth import LastSuperAdminError, ManagedUser
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import UserRow


class PersistentUserAdminService:
    """Tenant-scoped user administration backed by the production database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_users(self, actor: AuthenticatedPrincipal) -> tuple[ManagedUser, ...]:
        _require_admin(actor)
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserRow)
                    .where(UserRow.tenant_id == actor.tenant_id)
                    .order_by(UserRow.username.asc())
                )
            ).all()
        return tuple(_managed_user_from_row(row) for row in rows)

    async def change_role(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        role: Role,
    ) -> ManagedUser:
        _require_admin(actor)
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(UserRow)
                .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("user not found")
            if row.role == Role.SUPER_ADMIN.value and role is not Role.SUPER_ADMIN:
                remaining_super_admins = await session.scalar(
                    select(func.count())
                    .select_from(UserRow)
                    .where(
                        UserRow.tenant_id == actor.tenant_id,
                        UserRow.id != user_id,
                        UserRow.role == Role.SUPER_ADMIN.value,
                    )
                )
                if int(remaining_super_admins or 0) == 0:
                    raise LastSuperAdminError("cannot demote last super admin")
            row.role = role.value
            await session.flush()
            return _managed_user_from_row(row)


def _managed_user_from_row(row: UserRow) -> ManagedUser:
    return ManagedUser(
        id=row.id,
        username=row.username,
        role=Role(row.role),
        disabled=False,
        feishu_open_id=row.feishu_open_id,
    )


def _require_admin(actor: AuthenticatedPrincipal) -> None:
    if actor.role not in {Role.SUPER_ADMIN, Role.ADMIN}:
        raise PermissionError("administrator required")


__all__ = ["PersistentUserAdminService"]
