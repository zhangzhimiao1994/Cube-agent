from __future__ import annotations

from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import BASE_ERROR_RESPONSES, PublicAPIError, error_responses
from agent_hub.auth.feishu_oauth import (
    LastSuperAdminError,
    ManagedUser,
    ProtectedUserError,
    UserAlreadyExistsError,
)
from agent_hub.auth.models import (
    AuthenticatedPrincipal,
    AuthenticationBusy,
    Authorizer,
    PermissionDenied,
    Role,
    UsernameValidationError,
)
from agent_hub.auth.passwords import PasswordBackendError, PasswordValidationError

router = APIRouter(prefix="/api/v1/users", tags=["users"], responses=BASE_ERROR_RESPONSES)


class UserAdminServiceProtocol(Protocol):
    async def list_users(self, actor: AuthenticatedPrincipal) -> tuple[ManagedUser, ...]: ...

    async def change_role(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        role: Role,
    ) -> ManagedUser: ...

    async def create_user(
        self,
        actor: AuthenticatedPrincipal,
        *,
        username: str,
        password: str,
        role: Role,
    ) -> ManagedUser: ...

    async def set_disabled(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        disabled: bool,
    ) -> ManagedUser: ...

    async def reset_password(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
        password: str,
    ) -> ManagedUser: ...

    async def delete_user(
        self,
        actor: AuthenticatedPrincipal,
        user_id: UUID,
    ) -> ManagedUser: ...


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    username: str
    role: Role
    disabled: bool
    feishu_open_id: str | None
    protected: bool

    @classmethod
    def from_user(cls, user: ManagedUser) -> UserResponse:
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            disabled=user.disabled,
            feishu_open_id=user.feishu_open_id,
            protected=user.protected,
        )


class ChangeRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=1024)
    role: Role


class SetDisabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: bool


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=12, max_length=1024, repr=False)


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
    _require(principal, "user:read")
    try:
        users = await service.list_users(principal)
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    return [UserResponse.from_user(user) for user in users]


@router.post("", response_model=UserResponse, responses=error_responses(400, 401, 403, 409, 422, 503))
async def create_user(
    body: CreateUserRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> UserResponse:
    _require(principal, "user:write")
    try:
        user = await service.create_user(
            principal,
            username=body.username,
            password=body.password,
            role=body.role,
        )
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    except UsernameValidationError as error:
        raise PublicAPIError(422, "invalid_username", str(error)) from None
    except PasswordValidationError as error:
        raise PublicAPIError(422, "invalid_password", str(error)) from None
    except UserAlreadyExistsError:
        raise PublicAPIError(409, "user_exists", "user already exists") from None
    except AuthenticationBusy:
        raise PublicAPIError(503, "authentication_busy", "password worker is busy") from None
    except PasswordBackendError:
        raise PublicAPIError(503, "password_backend_failed", "password backend failed") from None
    await _record_user_audit(
        request,
        principal,
        action="user.create",
        resource=f"user:{user.id}",
        details={"username": user.username, "role": user.role.value},
    )
    return UserResponse.from_user(user)


@router.patch(
    "/{user_id}/role",
    response_model=UserResponse,
    responses=error_responses(400, 401, 403, 404, 409, 422, 503),
)
async def change_role(
    user_id: UUID,
    body: ChangeRoleRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> UserResponse:
    _require(principal, "user:write")
    try:
        user = await service.change_role(principal, user_id, body.role)
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    except KeyError:
        raise PublicAPIError(404, "user_not_found", "user not found") from None
    except LastSuperAdminError:
        raise PublicAPIError(409, "last_super_admin", "cannot modify last super admin") from None
    except ProtectedUserError as error:
        raise PublicAPIError(409, "protected_user", str(error)) from None
    await _record_user_audit(
        request,
        principal,
        action="user.role",
        resource=f"user:{user.id}",
        details={"username": user.username, "role": user.role.value},
    )
    return UserResponse.from_user(user)


@router.patch(
    "/{user_id}/disabled",
    response_model=UserResponse,
    responses=error_responses(400, 401, 403, 404, 409, 422, 503),
)
async def set_disabled(
    user_id: UUID,
    body: SetDisabledRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> UserResponse:
    _require(principal, "user:write")
    try:
        user = await service.set_disabled(principal, user_id, body.disabled)
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    except KeyError:
        raise PublicAPIError(404, "user_not_found", "user not found") from None
    except LastSuperAdminError:
        raise PublicAPIError(409, "last_super_admin", "cannot modify last super admin") from None
    except ProtectedUserError as error:
        raise PublicAPIError(409, "protected_user", str(error)) from None
    await _record_user_audit(
        request,
        principal,
        action="user.disable" if user.disabled else "user.enable",
        resource=f"user:{user.id}",
        details={"username": user.username, "disabled": str(user.disabled).lower()},
    )
    return UserResponse.from_user(user)


@router.patch(
    "/{user_id}/password",
    response_model=UserResponse,
    responses=error_responses(400, 401, 403, 404, 409, 422, 503),
)
async def reset_password(
    user_id: UUID,
    body: ResetPasswordRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> UserResponse:
    _require(principal, "user:write")
    try:
        user = await service.reset_password(
            principal,
            user_id,
            body.password.get_secret_value(),
        )
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    except KeyError:
        raise PublicAPIError(404, "user_not_found", "user not found") from None
    except PasswordValidationError as error:
        raise PublicAPIError(422, "invalid_password", str(error)) from None
    except AuthenticationBusy:
        raise PublicAPIError(503, "authentication_busy", "password worker is busy") from None
    except PasswordBackendError:
        raise PublicAPIError(503, "password_backend_failed", "password backend failed") from None
    except ProtectedUserError as error:
        raise PublicAPIError(409, "protected_user", str(error)) from None
    await _record_user_audit(
        request,
        principal,
        action="user.password",
        resource=f"user:{user.id}",
        details={"username": user.username, "role": user.role.value},
    )
    return UserResponse.from_user(user)


@router.delete(
    "/{user_id}",
    status_code=204,
    responses=error_responses(400, 401, 403, 404, 409, 422, 503),
)
async def delete_user(
    user_id: UUID,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[UserAdminServiceProtocol, Depends(_user_service)],
) -> Response:
    _require(principal, "user:write")
    try:
        user = await service.delete_user(principal, user_id)
    except PermissionError:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None
    except KeyError:
        raise PublicAPIError(404, "user_not_found", "user not found") from None
    except LastSuperAdminError:
        raise PublicAPIError(409, "last_super_admin", "cannot modify last super admin") from None
    except ProtectedUserError as error:
        raise PublicAPIError(409, "protected_user", str(error)) from None
    await _record_user_audit(
        request,
        principal,
        action="user.delete",
        resource=f"user:{user.id}",
        details={"username": user.username, "role": user.role.value},
    )
    return Response(status_code=204)


def _require(principal: AuthenticatedPrincipal, permission: str) -> None:
    try:
        Authorizer().require(principal, permission)
    except PermissionDenied:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None


async def _record_user_audit(
    request: Request,
    principal: AuthenticatedPrincipal,
    *,
    action: str,
    resource: str,
    details: dict[str, object],
) -> None:
    service = getattr(request.app.state, "admin_resource_service", None)
    recorder = getattr(service, "record_audit_event", None)
    if recorder is None:
        return
    await recorder(
        actor=str(principal.user_id),
        action=action,
        resource=resource,
        details=details,
    )


__all__ = ["router"]
