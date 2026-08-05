"""Local bootstrap and login endpoints."""

from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, Request, status

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import PublicAPIError
from agent_hub.api.schemas import (
    LoginRequest,
    PrincipalResponse,
    SetupRequest,
    TokenResponse,
)
from agent_hub.auth.models import (
    AuthenticatedPrincipal,
    AuthenticationBusy,
    AuthenticationOperationError,
    AuthenticationPersistenceError,
    AuthResult,
    BootstrapClosed,
    InvalidBootstrapCode,
    InvalidCredentials,
    UsernameValidationError,
)
from agent_hub.auth.passwords import PasswordValidationError
from agent_hub.auth.rate_limit import AuthRateLimiter, RateLimitUnavailable

router = APIRouter(prefix="/api/v1", tags=["auth"])


class AuthenticationService(Protocol):
    async def consume_bootstrap_code(
        self, code: str, username: str, password: str
    ) -> AuthResult: ...

    async def login(self, tenant_id: object, username: str, password: str) -> AuthResult: ...


def _auth_service(request: Request) -> AuthenticationService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(AuthenticationService, service)


def _client_ip(request: Request) -> str:
    socket_ip = request.client.host if request.client is not None else "unknown"
    trusted: frozenset[str] = getattr(
        request.app.state, "trusted_proxy_ips", frozenset()
    )
    if socket_ip not in trusted:
        return socket_ip
    forwarded_values = request.headers.getlist("x-forwarded-for")
    if len(forwarded_values) != 1:
        return socket_ip
    candidate = forwarded_values[0].split(",", 1)[0].strip()
    return candidate or socket_ip


async def _enforce_rate_limit(request: Request, endpoint: str) -> None:
    limiter = cast(AuthRateLimiter | None, getattr(request.app.state, "rate_limiter", None))
    if limiter is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    try:
        decision = await limiter.check(endpoint, _client_ip(request))
    except RateLimitUnavailable:
        raise PublicAPIError(503, "service_unavailable", "service unavailable") from None
    if not decision.allowed:
        raise PublicAPIError(
            429,
            "rate_limited",
            "too many requests",
            headers={"Retry-After": str(max(1, decision.retry_after))},
        )


def _token_response(result: AuthResult) -> TokenResponse:
    return TokenResponse(
        access_token=result.access_token,
        principal=PrincipalResponse.from_principal(result.principal),
    )


@router.post("/setup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def setup(
    body: SetupRequest,
    request: Request,
    service: Annotated[AuthenticationService, Depends(_auth_service)],
) -> TokenResponse:
    await _enforce_rate_limit(request, "setup")
    try:
        result = await service.consume_bootstrap_code(
            body.code.get_secret_value(),
            body.username,
            body.password.get_secret_value(),
        )
    except BootstrapClosed:
        raise PublicAPIError(409, "setup_closed", "initialization is already complete") from None
    except InvalidBootstrapCode:
        raise PublicAPIError(401, "invalid_bootstrap", "invalid bootstrap code") from None
    except (UsernameValidationError, PasswordValidationError):
        raise PublicAPIError(422, "request_validation", "request validation failed") from None
    except AuthenticationBusy:
        raise PublicAPIError(
            429, "authentication_busy", "authentication busy", headers={"Retry-After": "1"}
        ) from None
    except (AuthenticationOperationError, AuthenticationPersistenceError):
        raise PublicAPIError(503, "service_unavailable", "service unavailable") from None
    return _token_response(result)


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    service: Annotated[AuthenticationService, Depends(_auth_service)],
) -> TokenResponse:
    await _enforce_rate_limit(request, "login")
    try:
        result = await service.login(
            body.tenant_id,
            body.username,
            body.password.get_secret_value(),
        )
    except InvalidCredentials:
        raise PublicAPIError(
            401,
            "invalid_credentials",
            "invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except AuthenticationBusy:
        raise PublicAPIError(
            429, "authentication_busy", "authentication busy", headers={"Retry-After": "1"}
        ) from None
    except (AuthenticationOperationError, AuthenticationPersistenceError):
        raise PublicAPIError(503, "service_unavailable", "service unavailable") from None
    return _token_response(result)


@router.get("/auth/me", response_model=PrincipalResponse)
async def me(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
) -> PrincipalResponse:
    return PrincipalResponse.from_principal(principal)
