from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from agent_hub.auth.models import AuthenticatedPrincipal, Role


class OAuthStateError(RuntimeError):
    pass


class OAuthBindingError(RuntimeError):
    pass


class LastSuperAdminError(RuntimeError):
    pass


class ProtectedUserError(RuntimeError):
    pass


class UserAlreadyExistsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FeishuOAuthProfile:
    open_id: str
    tenant_key: str


@dataclass(frozen=True, slots=True)
class _OAuthState:
    tenant_key: str
    redirect_uri: str
    code_challenge: str
    expires_at: float


class InMemoryOAuthStateStore:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._states: dict[str, _OAuthState] = {}

    async def put(self, state: str, record: _OAuthState) -> None:
        self._states[state] = record

    async def pop(self, state: str) -> _OAuthState | None:
        record = self._states.pop(state, None)
        if record is None or self._monotonic() >= record.expires_at:
            return None
        return record


class FeishuOAuthClientProtocol(Protocol):
    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> FeishuOAuthProfile: ...


class FeishuOAuthService:
    def __init__(
        self,
        *,
        store: InMemoryOAuthStateStore,
        client: FeishuOAuthClientProtocol,
        allowed_tenant_keys: frozenset[str],
        monotonic: Callable[[], float] = time.monotonic,
        state_ttl_seconds: float = 300,
    ) -> None:
        if not allowed_tenant_keys:
            raise ValueError("at least one Feishu tenant key is required")
        self._store = store
        self._client = client
        self._allowed_tenant_keys = allowed_tenant_keys
        self._monotonic = monotonic
        self._state_ttl_seconds = state_ttl_seconds

    async def issue_state(
        self,
        *,
        tenant_key: str,
        redirect_uri: str,
        code_challenge: str,
    ) -> str:
        if tenant_key not in self._allowed_tenant_keys or not _safe_oauth_text(redirect_uri):
            raise OAuthStateError("invalid oauth state")
        state = secrets.token_urlsafe(32)
        await self._store.put(
            state,
            _OAuthState(
                tenant_key=tenant_key,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                expires_at=self._monotonic() + self._state_ttl_seconds,
            ),
        )
        return state

    async def consume_state(
        self,
        state: str,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> FeishuOAuthProfile:
        record = await self._store.pop(state)
        if record is None or record.redirect_uri != redirect_uri:
            raise OAuthStateError("invalid oauth state")
        profile = await self._client.exchange_code(
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        if profile.tenant_key != record.tenant_key or profile.tenant_key not in self._allowed_tenant_keys:
            raise OAuthStateError("invalid oauth tenant")
        if not _safe_open_id(profile.open_id):
            raise OAuthStateError("invalid oauth profile")
        return profile


@dataclass(frozen=True, slots=True)
class ManagedUser:
    id: UUID
    username: str
    role: Role
    disabled: bool = False
    feishu_open_id: str | None = None
    protected: bool = False


class InMemoryUserAdminService:
    def __init__(self, users: tuple[ManagedUser, ...]) -> None:
        self._users = {user.id: user for user in users}

    async def list_users(self, actor: AuthenticatedPrincipal) -> tuple[ManagedUser, ...]:
        _require_admin(actor)
        return tuple(self._users.values())

    async def change_role(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        role: Role,
    ) -> ManagedUser:
        _require_admin(actor)
        user = self._users[user_id]
        if user.protected and role is not Role.SUPER_ADMIN:
            raise ProtectedUserError("protected user cannot be demoted")
        if (
            user.role is Role.SUPER_ADMIN
            and role is not Role.SUPER_ADMIN
            and self._super_admin_count(excluding=user_id) == 0
        ):
            raise LastSuperAdminError("cannot demote last super admin")
        updated = ManagedUser(
            id=user.id,
            username=user.username,
            role=role,
            disabled=user.disabled,
            feishu_open_id=user.feishu_open_id,
            protected=user.protected,
        )
        self._users[user_id] = updated
        return updated

    async def create_user(
        self,
        actor: AuthenticatedPrincipal,
        *,
        username: str,
        password: str,
        role: Role,
    ) -> ManagedUser:
        del password
        _require_admin(actor)
        if any(user.username == username for user in self._users.values()):
            raise UserAlreadyExistsError("user already exists")
        user = ManagedUser(id=uuid4(), username=username, role=role)
        self._users[user.id] = user
        return user

    async def set_disabled(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        disabled: bool,
    ) -> ManagedUser:
        _require_admin(actor)
        user = self._users[user_id]
        if user_id == actor.user_id and disabled:
            raise ProtectedUserError("current user cannot be disabled")
        if user.protected and disabled:
            raise ProtectedUserError("protected user cannot be disabled")
        if user.role is Role.SUPER_ADMIN and disabled and self._super_admin_count(excluding=user_id) == 0:
            raise LastSuperAdminError("cannot disable last super admin")
        updated = ManagedUser(
            id=user.id,
            username=user.username,
            role=user.role,
            disabled=disabled,
            feishu_open_id=user.feishu_open_id,
            protected=user.protected,
        )
        self._users[user_id] = updated
        return updated

    async def reset_password(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        password: str,
    ) -> ManagedUser:
        del password
        _require_admin(actor)
        user = self._users[user_id]
        if user.protected:
            raise ProtectedUserError("protected user password cannot be reset")
        return user

    async def delete_user(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
    ) -> ManagedUser:
        _require_admin(actor)
        user = self._users[user_id]
        if user_id == actor.user_id:
            raise ProtectedUserError("current user cannot be deleted")
        if user.protected:
            raise ProtectedUserError("protected user cannot be deleted")
        if user.role is Role.SUPER_ADMIN and self._super_admin_count(excluding=user_id) == 0:
            raise LastSuperAdminError("cannot delete last super admin")
        return self._users.pop(user_id)

    async def bind_feishu_open_id(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        profile: FeishuOAuthProfile,
    ) -> ManagedUser:
        _require_admin(actor)
        if any(user.feishu_open_id == profile.open_id for user in self._users.values()):
            raise OAuthBindingError("feishu account is already bound")
        user = self._users[user_id]
        updated = ManagedUser(
            id=user.id,
            username=user.username,
            role=user.role,
            disabled=user.disabled,
            feishu_open_id=profile.open_id,
            protected=user.protected,
        )
        self._users[user_id] = updated
        return updated

    def _super_admin_count(self, *, excluding: UUID | None = None) -> int:
        return sum(
            1
            for user in self._users.values()
            if user.id != excluding and user.role is Role.SUPER_ADMIN and not user.disabled
        )


def _require_admin(actor: AuthenticatedPrincipal) -> None:
    if actor.role not in {Role.SUPER_ADMIN, Role.ADMIN}:
        raise PermissionError("administrator required")


def _safe_oauth_text(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 2048 and value == value.strip()


def _safe_open_id(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and value.replace("_", "").isalnum()


__all__ = [
    "FeishuOAuthProfile",
    "FeishuOAuthService",
    "InMemoryOAuthStateStore",
    "InMemoryUserAdminService",
    "LastSuperAdminError",
    "ManagedUser",
    "OAuthBindingError",
    "OAuthStateError",
    "ProtectedUserError",
    "UserAlreadyExistsError",
]
