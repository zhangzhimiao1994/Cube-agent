"""Local bootstrap and login endpoints."""

from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import BASE_ERROR_RESPONSES, PublicAPIError, error_responses
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
from agent_hub.security.network import canonical_ip

router = APIRouter(prefix="/api/v1", tags=["auth"], responses=BASE_ERROR_RESPONSES)


class AuthenticationService(Protocol):
    async def consume_bootstrap_code(
        self, code: str, username: str, password: str
    ) -> AuthResult: ...

    async def login(self, tenant_id: object, username: str, password: str) -> AuthResult: ...


class AuditRecorder(Protocol):
    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
    ) -> object: ...


def _auth_service(request: Request) -> AuthenticationService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(AuthenticationService, service)


def _audit_recorder(request: Request) -> AuditRecorder | None:
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None or not hasattr(service, "record_audit_event"):
        return None
    return cast(AuditRecorder, service)


def _client_ip(request: Request) -> str:
    socket_ip = request.client.host if request.client is not None else "unknown"
    canonical_socket = canonical_ip(socket_ip)
    if canonical_socket is None:
        return socket_ip
    trusted: frozenset[str] = getattr(
        request.app.state, "trusted_proxy_ips", frozenset()
    )
    if canonical_socket not in trusted:
        return canonical_socket
    forwarded_values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"x-forwarded-for"
    ]
    if len(forwarded_values) != 1:
        return canonical_socket
    forwarded = forwarded_values[0]
    if len(forwarded) > 2048:
        return canonical_socket
    raw_hops = forwarded.split(",")
    if not 1 <= len(raw_hops) <= 32:
        return canonical_socket
    chain: list[str] = []
    for raw_hop in raw_hops:
        hop = canonical_ip(raw_hop.strip())
        if hop is None:
            return canonical_socket
        chain.append(hop)
    chain.append(canonical_socket)
    for hop in reversed(chain):
        if hop not in trusted:
            return hop
    return chain[0]


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


async def _record_login_audit(
    request: Request,
    *,
    action: str,
    tenant_id: object,
    username: str,
    principal: AuthenticatedPrincipal | None = None,
) -> None:
    recorder = _audit_recorder(request)
    if recorder is None:
        return
    actor = str(principal.user_id) if principal is not None else username
    try:
        await recorder.record_audit_event(
            actor=actor,
            action=action,
            resource=f"tenant:{tenant_id}",
            details={
                "username": username,
                "tenant_id": str(tenant_id),
                "client_ip": _client_ip(request),
            },
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return


@router.post(
    "/setup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 409, 413, 422, 429, 503),
)
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


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    responses=error_responses(401, 413, 422, 429, 503),
)
async def login(
    body: LoginRequest,
    request: Request,
    service: Annotated[AuthenticationService, Depends(_auth_service)],
) -> TokenResponse:
    await _enforce_rate_limit(request, "login")
    tenant_id = body.tenant_id
    if tenant_id is None:
        configured_tenant_id = getattr(request.app.state, "bootstrap_tenant_id", None)
        if not isinstance(configured_tenant_id, UUID):
            raise PublicAPIError(503, "service_unavailable", "service unavailable")
        tenant_id = configured_tenant_id
    try:
        result = await service.login(
            tenant_id,
            body.username,
            body.password.get_secret_value(),
        )
    except InvalidCredentials:
        await _record_login_audit(
            request,
            action="auth.login_failed",
            tenant_id=tenant_id,
            username=body.username,
        )
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
    await _record_login_audit(
        request,
        action="auth.login",
        tenant_id=tenant_id,
        username=body.username,
        principal=result.principal,
    )
    return _token_response(result)


@router.get(
    "/auth/me",
    response_model=PrincipalResponse,
    responses=error_responses(401, 503),
)
async def me(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
) -> PrincipalResponse:
    return PrincipalResponse.from_principal(principal)
