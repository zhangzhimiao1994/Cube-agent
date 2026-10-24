"""Real signed-token HTTP authorization against mutable, offline user-state storage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Annotated, Any, Self, cast
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, public_error_handler
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService


class UserStateSession:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.user: SimpleNamespace | None = SimpleNamespace(
            id=principal.user_id, tenant_id=principal.tenant_id,
            role=principal.role.value, disabled=False,
        )
        self.statements: list[Any] = []
        self.error: Exception | None = None
        self.closed = 0

    def __call__(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed += 1

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(one_or_none=lambda: self.user)


def client(auth: object) -> TestClient:
    app = FastAPI()
    app.state.auth_service = auth
    app.add_exception_handler(PublicAPIError, public_error_handler)

    @app.get("/protected")
    def protected(principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))]) -> dict[str, str]:
        return {"user_id": str(principal.user_id), "tenant_id": str(principal.tenant_id),
                "role": principal.role.value}

    return TestClient(app)


def setup() -> tuple[AuthenticatedPrincipal, UserStateSession, AuthService, str]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    state = UserStateSession(principal)
    tokens = AccessTokenService(b"synthetic-live-principal-test-key!")
    service = AuthService(cast(Any, state), principal.tenant_id, cast(PasswordService, object()), tokens)
    token = tokens.encode(principal.user_id, principal.tenant_id, principal.role)
    return principal, state, service, token


def test_api_checks_current_user_on_every_request_with_scoped_metadata_only() -> None:
    principal, state, service, token = setup()
    api = client(service)
    for _ in range(2):
        response = api.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["role"] == principal.role.value
    assert len(state.statements) == state.closed == 2
    for statement in state.statements:
        assert {principal.user_id, principal.tenant_id}.issubset(statement.compile().params.values())
        assert {column.key for column in statement.selected_columns} == {"id", "tenant_id", "role", "disabled"}


@pytest.mark.parametrize("change", ("disabled", "deleted", "tenant", "user", "demoted", "promoted", "unknown_role"))
def test_previous_access_token_cannot_authorize_after_current_user_changes(change: str) -> None:
    principal, state, service, token = setup()
    api = client(service)
    headers = {"Authorization": f"Bearer {token}"}
    assert api.get("/protected", headers=headers).status_code == 200
    assert state.user is not None
    if change == "deleted":
        state.user = None
    elif change == "disabled":
        state.user.disabled = True
    elif change in {"tenant", "user"}:
        setattr(state.user, "tenant_id" if change == "tenant" else "id", uuid4())
    else:
        state.user.role = {"demoted": Role.VIEWER.value, "promoted": Role.ADMIN.value,
                           "unknown_role": "invalid-role"}[change]
    # Low-level decoding remains compatible, but it is no longer API authorization.
    assert service.authenticate_token(token) == principal
    response = api.get("/protected", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"error": {"code": "invalid_token", "message": "invalid access token"}}
    assert len(state.statements) == state.closed == 2


def test_new_token_for_changed_role_works_only_when_live_role_matches() -> None:
    principal, state, service, _ = setup()
    assert state.user is not None
    state.user.role = Role.VIEWER.value
    token = service._tokens.encode(principal.user_id, principal.tenant_id, Role.VIEWER)
    response = client(service).get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.json()["role"] == Role.VIEWER.value
    assert len(state.statements) == 1


@pytest.mark.parametrize("error", (SQLAlchemyError("synthetic-private-dsn"), RuntimeError("synthetic-private-backend")))
def test_unavailable_live_state_fails_closed_without_raw_backend_details(error: Exception) -> None:
    _, state, service, token = setup()
    state.error = error
    response = client(service).get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "service_unavailable", "message": "service unavailable"}}
    assert "synthetic-private" not in response.text
    assert state.closed == 1


def test_invalid_signed_token_does_not_read_database() -> None:
    _, state, service, _ = setup()
    response = client(service).get("/protected", headers={"Authorization": "Bearer invalid-token"})
    assert response.status_code == 401 and state.statements == []


def test_decode_only_injected_service_is_not_a_live_validation_fallback() -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)

    class DecodeOnly:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            return principal

    assert client(DecodeOnly()).get("/protected", headers={"Authorization": "Bearer fixed"}).status_code == 503


@pytest.mark.parametrize("result", ("boolean", "different_actor", "different_role", "none"))
def test_validator_must_return_exact_validated_principal(result: str) -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)

    class BrokenValidator:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            return principal

        async def validate_principal(self, candidate: AuthenticatedPrincipal) -> Any:
            return {"boolean": True, "different_actor": replace(principal, user_id=uuid4()),
                    "different_role": replace(principal, role=Role.ADMIN), "none": None}[result]

    response = client(BrokenValidator()).get("/protected", headers={"Authorization": "Bearer fixed"})
    assert response.status_code == 503


def test_explicit_inmemory_fixture_revalidates_its_authoritative_actor_state() -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)

    class InMemoryAuth:
        current: AuthenticatedPrincipal | None = principal

        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            return principal

        async def validate_principal(self, candidate: AuthenticatedPrincipal) -> AuthenticatedPrincipal:
            if self.current != candidate:
                raise InvalidCredentials("actor unavailable")
            return candidate

    service = InMemoryAuth()
    api = client(service)
    assert api.get("/protected", headers={"Authorization": "Bearer fixed"}).status_code == 200
    service.current = None
    assert api.get("/protected", headers={"Authorization": "Bearer fixed"}).status_code == 401


@pytest.mark.parametrize("change", ("disabled", "deleted", "demoted"))
def test_real_same_tenant_run_route_rejects_previous_token_after_actor_change(change: str) -> None:
    from agent_hub.app import create_app
    from tests.api.test_runs_api import StubRunService

    _, state, service, token = setup()
    api = TestClient(create_app(auth_service=service, rate_limiter=object(), run_service=StubRunService([])))
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/runs/{uuid4()}"
    assert api.get(path, headers=headers).status_code == 200
    assert state.user is not None
    if change == "disabled":
        state.user.disabled = True
    elif change == "deleted":
        state.user = None
    else:
        state.user.role = Role.VIEWER.value
    response = api.get(path, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert len(state.statements) == state.closed == 2


async def test_live_validation_cancellation_closes_session_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    principal, state, service, _ = setup()
    entered = asyncio.Event()

    async def blocked_execute(statement: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(state, "execute", blocked_execute)
    task = asyncio.create_task(service.validate_principal(principal))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.closed == 1
