"""FastAPI authentication and authorization dependencies."""

from collections.abc import Callable
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agent_hub.api.errors import PublicAPIError
from agent_hub.auth.models import (
    AuthenticatedPrincipal,
    Authorizer,
    InvalidCredentials,
    PermissionDenied,
)
from agent_hub.auth.tokens import InvalidTokenError

_MAX_TOKEN_CHARS = 8192
_BEARER_HEADERS = {"WWW-Authenticate": "Bearer"}
_BEARER_SCHEME = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


class TokenAuthenticator(Protocol):
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal: ...


def get_auth_service(request: Request) -> TokenAuthenticator:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(TokenAuthenticator, service)


def _authorization_values(request: Request) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"authorization"
    ]


def current_principal(
    request: Request,
    auth_service: Annotated[TokenAuthenticator, Depends(get_auth_service)],
    documented_credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(_BEARER_SCHEME)
    ],
) -> AuthenticatedPrincipal:
    del documented_credentials
    values = _authorization_values(request)
    if len(values) != 1:
        raise _invalid_token_error()
    parts = values[0].split()
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not parts[1]
        or len(parts[1]) > _MAX_TOKEN_CHARS
    ):
        raise _invalid_token_error()
    token = parts[1]
    try:
        return auth_service.authenticate_token(token)
    except (InvalidTokenError, InvalidCredentials):
        raise _invalid_token_error() from None
    finally:
        del token


def require_permission(
    permission: str,
) -> Callable[[AuthenticatedPrincipal], AuthenticatedPrincipal]:
    def dependency(
        principal: Annotated[
            AuthenticatedPrincipal, Depends(current_principal)
        ],
    ) -> AuthenticatedPrincipal:
        try:
            return Authorizer().require(principal, permission)
        except PermissionDenied:
            raise PublicAPIError(403, "permission_denied", "permission denied") from None

    return dependency


def _invalid_token_error() -> PublicAPIError:
    return PublicAPIError(
        401,
        "invalid_token",
        "invalid access token",
        headers=_BEARER_HEADERS,
    )
