from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.feishu_oauth import (
    FeishuOAuthProfile,
    LastSuperAdminError,
    ManagedUser,
    OAuthBindingError,
    ProtectedUserError,
    UserAlreadyExistsError,
)
from agent_hub.auth.models import AuthenticatedPrincipal, Role, UsernameValidationError
from agent_hub.auth.passwords import PasswordService
from agent_hub.db.models import UserRow

_USERNAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")


class PersistentUserAdminService:
    """Tenant-scoped user administration backed by the production database."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        passwords: PasswordService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._passwords = passwords or PasswordService()

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

    async def create_user(
        self,
        actor: AuthenticatedPrincipal,
        *,
        username: str,
        password: str,
        role: Role,
    ) -> ManagedUser:
        _require_admin(actor)
        if actor.role is not Role.SUPER_ADMIN and role is Role.SUPER_ADMIN:
            raise PermissionError("only super admin can create super admin users")
        if not _is_valid_username(username):
            raise UsernameValidationError(
                "username must be a lowercase safe identifier of 3-64 characters"
            )
        password_hash = await self._passwords.hash_async(password)
        try:
            async with self._session_factory() as session, session.begin():
                row = UserRow(
                    id=uuid4(),
                    tenant_id=actor.tenant_id,
                    username=username,
                    password_hash=password_hash,
                    role=role.value,
                    disabled=False,
                    protected=False,
                )
                session.add(row)
                await session.flush()
                return _managed_user_from_row(row)
        except IntegrityError as error:
            raise UserAlreadyExistsError("user already exists") from error
        finally:
            del password, password_hash

    async def change_role(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        role: Role,
    ) -> ManagedUser:
        _require_admin(actor)
        if actor.role is not Role.SUPER_ADMIN and role is Role.SUPER_ADMIN:
            raise PermissionError("only super admin can assign super admin role")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(UserRow)
                .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("user not found")
            if row.protected and role is not Role.SUPER_ADMIN:
                raise ProtectedUserError("protected user cannot be demoted")
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

    async def update_user(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        *,
        username: str | None = None,
        role: Role | None = None,
        disabled: bool | None = None,
    ) -> ManagedUser:
        _require_admin(actor)
        if username is not None and not _is_valid_username(username):
            raise UsernameValidationError(
                "username must be a lowercase safe identifier of 3-64 characters"
            )
        if actor.role is not Role.SUPER_ADMIN and role is Role.SUPER_ADMIN:
            raise PermissionError("only super admin can assign super admin role")
        if user_id == actor.user_id and disabled is True:
            raise ProtectedUserError("current user cannot be disabled")
        try:
            async with self._session_factory() as session, session.begin():
                row = await session.scalar(
                    select(UserRow)
                    .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                    .with_for_update()
                )
                if row is None:
                    raise KeyError("user not found")
                next_username = username if username is not None else row.username
                next_role = role.value if role is not None else row.role
                next_disabled = disabled if disabled is not None else row.disabled
                if row.protected and next_username != row.username:
                    raise ProtectedUserError("protected user cannot be renamed")
                if row.protected and next_role != Role.SUPER_ADMIN.value:
                    raise ProtectedUserError("protected user cannot be demoted")
                if row.protected and next_disabled:
                    raise ProtectedUserError("protected user cannot be disabled")
                if row.role == Role.SUPER_ADMIN.value and (
                    next_role != Role.SUPER_ADMIN.value or next_disabled
                ):
                    remaining_super_admins = await session.scalar(
                        select(func.count())
                        .select_from(UserRow)
                        .where(
                            UserRow.tenant_id == actor.tenant_id,
                            UserRow.id != user_id,
                            UserRow.role == Role.SUPER_ADMIN.value,
                            UserRow.disabled.is_(False),
                        )
                    )
                    if int(remaining_super_admins or 0) == 0:
                        raise LastSuperAdminError("cannot modify last super admin")
                row.username = next_username
                row.role = next_role
                row.disabled = next_disabled
                await session.flush()
                return _managed_user_from_row(row)
        except IntegrityError as error:
            raise UserAlreadyExistsError("user already exists") from error

    async def set_disabled(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        disabled: bool,
    ) -> ManagedUser:
        _require_admin(actor)
        if user_id == actor.user_id and disabled:
            raise ProtectedUserError("current user cannot be disabled")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(UserRow)
                .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("user not found")
            if row.protected and disabled:
                raise ProtectedUserError("protected user cannot be disabled")
            if row.role == Role.SUPER_ADMIN.value and disabled:
                remaining_super_admins = await session.scalar(
                    select(func.count())
                    .select_from(UserRow)
                    .where(
                        UserRow.tenant_id == actor.tenant_id,
                        UserRow.id != user_id,
                        UserRow.role == Role.SUPER_ADMIN.value,
                        UserRow.disabled.is_(False),
                    )
                )
                if int(remaining_super_admins or 0) == 0:
                    raise LastSuperAdminError("cannot disable last super admin")
            row.disabled = disabled
            await session.flush()
            return _managed_user_from_row(row)

    async def reset_password(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        password: str,
    ) -> ManagedUser:
        _require_admin(actor)
        password_hash = await self._passwords.hash_async(password)
        try:
            async with self._session_factory() as session, session.begin():
                row = await session.scalar(
                    select(UserRow)
                    .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                    .with_for_update()
                )
                if row is None:
                    raise KeyError("user not found")
                if row.protected:
                    raise ProtectedUserError("protected user password cannot be reset")
                row.password_hash = password_hash
                await session.flush()
                return _managed_user_from_row(row)
        finally:
            del password, password_hash

    async def delete_user(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
    ) -> ManagedUser:
        _require_admin(actor)
        if user_id == actor.user_id:
            raise ProtectedUserError("current user cannot be deleted")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(UserRow)
                .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("user not found")
            if row.protected:
                raise ProtectedUserError("protected user cannot be deleted")
            if row.role == Role.SUPER_ADMIN.value:
                remaining_super_admins = await session.scalar(
                    select(func.count())
                    .select_from(UserRow)
                    .where(
                        UserRow.tenant_id == actor.tenant_id,
                        UserRow.id != user_id,
                        UserRow.role == Role.SUPER_ADMIN.value,
                        UserRow.disabled.is_(False),
                    )
                )
                if int(remaining_super_admins or 0) == 0:
                    raise LastSuperAdminError("cannot delete last super admin")
            deleted = _managed_user_from_row(row)
            await session.delete(row)
            return deleted

    async def bind_feishu_open_id(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        profile: FeishuOAuthProfile,
    ) -> ManagedUser:
        _require_admin(actor)
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(UserRow)
                .where(
                    UserRow.tenant_id == actor.tenant_id,
                    UserRow.feishu_open_id == profile.open_id,
                    UserRow.id != user_id,
                )
                .with_for_update()
            )
            if existing is not None:
                raise OAuthBindingError("feishu account is already bound")
            row = await session.scalar(
                select(UserRow)
                .where(UserRow.tenant_id == actor.tenant_id, UserRow.id == user_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("user not found")
            row.feishu_open_id = profile.open_id
            await session.flush()
            return _managed_user_from_row(row)

    async def unbind_feishu_open_id(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
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
            row.feishu_open_id = None
            await session.flush()
            return _managed_user_from_row(row)


def _managed_user_from_row(row: UserRow) -> ManagedUser:
    return ManagedUser(
        id=row.id,
        username=row.username,
        role=Role(row.role),
        disabled=row.disabled,
        feishu_open_id=row.feishu_open_id,
        protected=row.protected,
    )


def _require_admin(actor: AuthenticatedPrincipal) -> None:
    if actor.role not in {Role.SUPER_ADMIN, Role.ADMIN}:
        raise PermissionError("administrator required")


def _is_valid_username(username: object) -> bool:
    return (
        isinstance(username, str)
        and _USERNAME_PATTERN.fullmatch(username) is not None
        and ".." not in username
        and "--" not in username
        and "__" not in username
    )


__all__ = ["PersistentUserAdminService"]
