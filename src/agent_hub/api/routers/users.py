from __future__ import annotations

from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import BASE_ERROR_RESPONSES, PublicAPIError, error_responses
from agent_hub.auth.feishu_oauth import LastSuperAdminError, ManagedUser
from agent_hub.auth.models import AuthenticatedPrincipal, Role

router = APIRouter(prefix="/api/v1/users", tags=["users"], responses=BASE_ERROR_RESPONSES)


class UserAdminServiceProtocol(Protocol):
    async def list_users(self, actor: AuthenticatedPrincipal) -> tuple[ManagedUser, ...]: ...

    async def change_role(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        role: Role,
    ) -> ManagedUser: ...


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    username: str
    role: Role
    disabled: bool
    feishu_open_id: str | None

    @classmethod
    def from_user(cls, user: ManagedUser) -> UserResponse:
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            disabled=user.disabled,
            feishu_open_id=user.feishu_open_id,
        )


class ChangeRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


def _user_service(request: Request) -> UserAdminServiceProtocol:
    service = getattr(request.app.state, "user_admin_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(UserAdminServiceProtocol, service)


@router.get("", response_model=list[UserResponse], responses=error_responses(401, 403, 422, 503))
async def list_users(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> list[UserResponse]:
    try:
        users = await service.list_users(principal)
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    return [UserResponse.from_user(user) for user in users]


@router.patch(
    "/{user_id}/role",
    response_model=UserResponse,
    responses=error_responses(400, 401, 403, 409, 422, 503),
)
async def change_role(
    user_id: UUID,
    body: ChangeRoleRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> UserResponse:
    try:
        user = await service.change_role(principal, user_id, body.role)
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    except LastSuperAdminError:
        raise PublicAPIError(409, "last_super_admin", "cannot modify last super admin") from None
    return UserResponse.from_user(user)


__all__ = ["router"]
