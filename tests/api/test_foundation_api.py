import asyncio
import base64
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from agent_hub.api.middleware import SafeExceptionMiddleware, StreamAbortedError
from agent_hub.app import _cleanup_owned_resources, create_app
from agent_hub.auth.models import (
    AuthenticatedPrincipal,
    AuthenticationPersistenceError,
    AuthResult,
    InvalidCredentials,
    Role,
)
from agent_hub.config.repository import ConfigNotFoundError, ConfigRevision, ConfigStatus
from agent_hub.config.service import ConfigPublishedEvent, PostCommitNotificationError
from agent_hub.settings import Settings


class Probe:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.outcome is not None:
            raise self.outcome


class HangingProbe:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.task: asyncio.Task[object] | None = None

    async def __call__(self) -> None:
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def failing_probe() -> None:
    raise RuntimeError("probe failed")


async def never_returns() -> None:
    await asyncio.Event().wait()


@pytest.mark.parametrize("failed_probe", ["database", "redis"])
def test_ready_is_generic_and_requires_both_dependencies(failed_probe: str) -> None:
    probes: dict[str, Callable[[], Awaitable[None]]] = {
        "database": Probe(),
        "redis": Probe(),
    }
    probes[failed_probe] = Probe(RuntimeError("secret backend address"))
    app = create_app(
        database_probe=probes["database"],
        redis_probe=probes["redis"],
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "service_unavailable",
        "message": "service unavailable",
    }
    assert response.json()["checks"][failed_probe] == "failed"
    assert "secret backend address" not in response.text


def test_ready_times_out_and_live_stays_live() -> None:
    app = create_app(
        database_probe=never_returns,
        redis_probe=Probe(),
        readiness_timeout_seconds=0.01,
    )
    client = TestClient(app)

    ready = client.get("/health/ready")
    live = client.get("/health/live")

    assert ready.status_code == 503
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}


def test_ready_reports_ok_when_dependencies_respond() -> None:
    database = Probe()
    redis = Probe()
    app = create_app(database_probe=database, redis_probe=redis)

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert database.calls == redis.calls == 1


@pytest.mark.parametrize("hanging_side", ["database", "redis"])
async def test_ready_fast_failure_cancels_and_reaps_hanging_sibling(
    hanging_side: str,
) -> None:
    hanging = HangingProbe()
    database_probe = hanging if hanging_side == "database" else failing_probe
    redis_probe = hanging if hanging_side == "redis" else failing_probe
    app = create_app(
        database_probe=database_probe,
        redis_probe=redis_probe,
        readiness_timeout_seconds=5,
        auth_service=object(),
        config_service=object(),
        rate_limiter=object(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert hanging.cancelled.is_set()
    assert hanging.task is not None and hanging.task.done()


async def test_ready_timeout_cancels_and_reaps_both_probes() -> None:
    database = HangingProbe()
    redis = HangingProbe()
    app = create_app(
        database_probe=database,
        redis_probe=redis,
        readiness_timeout_seconds=0.01,
        auth_service=object(),
        config_service=object(),
        rate_limiter=object(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert database.cancelled.is_set() and redis.cancelled.is_set()
    assert database.task is not None and database.task.done()
    assert redis.task is not None and redis.task.done()


async def test_cancelling_ready_request_cancels_and_reaps_both_probes() -> None:
    database = HangingProbe()
    redis = HangingProbe()
    app = create_app(
        database_probe=database,
        redis_probe=redis,
        readiness_timeout_seconds=5,
        auth_service=object(),
        config_service=object(),
        rate_limiter=object(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get("/health/ready"))
        await database.started.wait()
        await redis.started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert database.cancelled.is_set() and redis.cancelled.is_set()
    assert database.task is not None and database.task.done()
    assert redis.task is not None and redis.task.done()


def test_default_development_key_allows_lifespan_to_start() -> None:
    app = create_app(
        settings=Settings.model_validate({}),
        database=FakeDatabase(),
        redis_client=FakeRedis(),
    )

    with TestClient(app):
        pass


class StubAuthService:
    def __init__(self, principal: AuthenticatedPrincipal | None = None) -> None:
        self.principal = principal or AuthenticatedPrincipal(uuid4(), uuid4(), Role.ADMIN)
        self.login_error: Exception | None = None
        self.seen_tokens: list[str] = []
        self.login_tenant_ids: list[UUID] = []

    async def consume_bootstrap_code(
        self, code: str, username: str, password: str
    ) -> AuthResult:
        return AuthResult(self.principal, "setup-access-token")

    async def login(self, tenant_id: UUID, username: str, password: str) -> AuthResult:
        self.login_tenant_ids.append(tenant_id)
        if self.login_error is not None:
            raise self.login_error
        return AuthResult(self.principal, "login-access-token")

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        self.seen_tokens.append(token)
        if token != "valid-token":
            raise InvalidCredentials("internal token detail")
        return self.principal


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    retry_after: int = 0


class StubRateLimiter:
    def __init__(self, decision: LimitDecision | Exception | None = None) -> None:
        self.decision = decision or LimitDecision(True)
        self.calls: list[tuple[str, str]] = []

    async def check(self, endpoint: str, client_ip: str) -> LimitDecision:
        self.calls.append((endpoint, client_ip))
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def auth_client(
    auth: StubAuthService | None = None,
    limiter: StubRateLimiter | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            auth_service=auth or StubAuthService(),
            rate_limiter=limiter or StubRateLimiter(),
        )
    )


def test_setup_and_login_return_only_safe_principal_fields() -> None:
    auth = StubAuthService()
    client = auth_client(auth)

    setup = client.post(
        "/api/v1/setup",
        json={
            "code": "a" * 43,
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(auth.principal.tenant_id),
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )

    assert setup.status_code == 201
    assert login.status_code == 200
    assert setup.json() == {
        "access_token": "setup-access-token",
        "token_type": "bearer",
        "principal": {
            "user_id": str(auth.principal.user_id),
            "tenant_id": str(auth.principal.tenant_id),
            "role": "admin",
        },
    }
    assert "password" not in setup.text
    assert "code" not in setup.text


def test_login_uses_bootstrap_tenant_when_tenant_id_is_omitted() -> None:
    principal = AuthenticatedPrincipal(
        uuid4(),
        Settings.model_validate({}).bootstrap_tenant_id,
        Role.ADMIN,
    )
    auth = StubAuthService(principal)
    client = auth_client(auth)

    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 200
    assert auth.login_tenant_ids == [principal.tenant_id]


def test_login_invalid_credentials_is_generic_401_with_challenge() -> None:
    auth = StubAuthService()
    auth.login_error = InvalidCredentials("database username leaked")
    response = auth_client(auth).post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(auth.principal.tenant_id),
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "invalid_credentials", "message": "invalid credentials"}
    }
    assert "database username" not in response.text


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic abc", "Bearer", "Bearer a b", "Bearer " + "a" * 8193],
)
def test_bearer_dependency_rejects_missing_or_malformed_values(
    authorization: str | None,
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = auth_client().get("/api/v1/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "invalid_token", "message": "invalid access token"}
    }


def test_bearer_dependency_rejects_duplicate_headers_without_authenticating() -> None:
    auth = StubAuthService()
    response = auth_client(auth).get(
        "/api/v1/auth/me",
        headers=[("Authorization", "Bearer valid-token"), ("Authorization", "Bearer other")],
    )

    assert response.status_code == 401
    assert auth.seen_tokens == []
    assert "valid-token" not in response.text


def test_bearer_scheme_is_case_insensitive() -> None:
    auth = StubAuthService()
    response = auth_client(auth).get(
        "/api/v1/auth/me", headers={"Authorization": "bEaReR valid-token"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_auth_rate_limit_uses_socket_client_and_returns_retry_after() -> None:
    limiter = StubRateLimiter(LimitDecision(False, retry_after=37))
    response = auth_client(limiter=limiter).post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "203.0.113.9"},
        json={
            "tenant_id": str(uuid4()),
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "37"
    assert limiter.calls == [("login", "testclient")]


def test_rate_limiter_failure_is_fail_closed() -> None:
    from agent_hub.auth.rate_limit import RateLimitUnavailable

    limiter = StubRateLimiter(RateLimitUnavailable("redis host leaked"))
    response = auth_client(limiter=limiter).post(
        "/api/v1/setup",
        json={
            "code": "a" * 43,
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "service unavailable"
    assert "redis host" not in response.text


def test_request_validation_does_not_echo_sensitive_input() -> None:
    code = "TOP_SECRET_CODE"
    password = "TOP_SECRET_PASSWORD"
    response = auth_client().post(
        "/api/v1/setup",
        json={"code": code, "username": "INVALID USER", "password": password},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation"
    assert code not in response.text
    assert password not in response.text


def test_auth_persistence_failure_is_generic_503() -> None:
    auth = StubAuthService()
    auth.login_error = AuthenticationPersistenceError("postgres DSN leaked")
    response = auth_client(auth).post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(auth.principal.tenant_id),
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 503
    assert "postgres DSN" not in response.text


def config_document(prompt: str = "Help.") -> dict[str, object]:
    return {
        "models": {
            "main-model": {
                "deployments": [
                    {
                        "provider": "openai",
                        "model": "gpt-test",
                        "secret_ref": "OPENAI_API_KEY",
                        "quota_scope_id": "primary",
                    }
                ]
            }
        },
        "agents": [
            {
                "id": "assistant",
                "role": "assistant",
                "prompt": prompt,
                "model": "main-model",
            }
        ],
    }


class StubConfigService:
    def __init__(self) -> None:
        self.revisions: list[ConfigRevision] = []
        self.publish_notification_failure = False
        self.create_calls = 0

    async def create_draft(
        self, tenant_id: UUID, actor_id: UUID, document: object
    ) -> ConfigRevision:
        self.create_calls += 1
        revision = ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=len(self.revisions) + 1,
            status=ConfigStatus.DRAFT,
            document=cast_dict(document),
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )
        self.revisions.append(revision)
        return revision

    async def publish_revision(
        self, tenant_id: UUID, revision_id: UUID, actor_id: UUID
    ) -> ConfigRevision:
        target = next(
            (
                revision
                for revision in self.revisions
                if revision.tenant_id == tenant_id and revision.id == revision_id
            ),
            None,
        )
        if target is None:
            raise ConfigNotFoundError("other tenant detail")
        published = replace(target, status=ConfigStatus.PUBLISHED)
        self.revisions[self.revisions.index(target)] = published
        if self.publish_notification_failure:
            raise PostCommitNotificationError(
                ConfigPublishedEvent(tenant_id, published.version, actor_id, "publish")
            )
        return published

    async def rollback(
        self, tenant_id: UUID, source_version: int, actor_id: UUID
    ) -> ConfigRevision:
        source = await self.get_version(tenant_id, source_version)
        rolled_back = ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=len(self.revisions) + 1,
            status=ConfigStatus.PUBLISHED,
            document=source.document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )
        self.revisions.append(rolled_back)
        return rolled_back

    async def get_version(self, tenant_id: UUID, version: int) -> ConfigRevision:
        target = next(
            (
                revision
                for revision in self.revisions
                if revision.tenant_id == tenant_id and revision.version == version
            ),
            None,
        )
        if target is None:
            raise ConfigNotFoundError("missing")
        return target

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
        return next(
            (
                revision
                for revision in reversed(self.revisions)
                if revision.tenant_id == tenant_id
                and revision.status is ConfigStatus.PUBLISHED
            ),
            None,
        )

    async def list_versions(
        self, tenant_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> list[ConfigRevision]:
        matches = [r for r in self.revisions if r.tenant_id == tenant_id]
        return matches[offset : offset + limit]


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def config_client(
    role: Role = Role.ADMIN,
    *,
    service: StubConfigService | None = None,
    tenant_id: UUID | None = None,
) -> tuple[TestClient, StubConfigService, StubAuthService]:
    principal = AuthenticatedPrincipal(uuid4(), tenant_id or uuid4(), role)
    auth = StubAuthService(principal)
    config = service or StubConfigService()
    client = TestClient(
        create_app(
            auth_service=auth,
            rate_limiter=StubRateLimiter(),
            config_service=config,
        )
    )
    return client, config, auth


def bearer() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def test_config_create_publish_current_history_and_rollback() -> None:
    client, service, _ = config_client()

    draft = client.post("/api/v1/config/drafts", headers=bearer(), json=config_document())
    published = client.post(
        f"/api/v1/config/drafts/{draft.json()['id']}/publish", headers=bearer()
    )
    current = client.get("/api/v1/config/current", headers=bearer())
    history = client.get("/api/v1/config/history?limit=20&offset=0", headers=bearer())
    rollback = client.post(
        "/api/v1/config/history/1/rollback", headers=bearer()
    )

    assert draft.status_code == 201
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert current.json()["id"] == published.json()["id"]
    assert history.json()["items"][0]["version"] == 1
    assert rollback.status_code == 200
    assert rollback.json()["version"] == 2
    assert service.create_calls == 1


def test_config_validate_does_not_persist_and_invalid_is_422() -> None:
    client, service, _ = config_client()

    valid = client.post("/api/v1/config/validate", headers=bearer(), json=config_document())
    invalid = client.post(
        "/api/v1/config/validate",
        headers=bearer(),
        json={"models": {}, "agents": [{"id": "a", "model": "missing"}]},
    )

    assert valid.status_code == 200
    assert valid.json() == {"valid": True}
    assert invalid.status_code == 422
    assert service.create_calls == 0


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.VIEWER])
def test_read_roles_cannot_create_or_publish(role: Role) -> None:
    client, service, _ = config_client(role)
    draft = asyncio.run(
        service.create_draft(uuid4(), uuid4(), config_document())
    )

    create = client.post("/api/v1/config/drafts", headers=bearer(), json=config_document())
    publish = client.post(
        f"/api/v1/config/drafts/{draft.id}/publish", headers=bearer()
    )
    read = client.get("/api/v1/config/history", headers=bearer())

    assert create.status_code == 403
    assert publish.status_code == 403
    assert read.status_code == 200


def test_publish_revision_is_tenant_scoped_and_missing_is_generic_404() -> None:
    service = StubConfigService()
    other_tenant_draft = asyncio.run(
        service.create_draft(uuid4(), uuid4(), config_document())
    )
    client, _, _ = config_client(service=service)

    response = client.post(
        f"/api/v1/config/drafts/{other_tenant_draft.id}/publish", headers=bearer()
    )

    assert response.status_code == 404
    assert "other tenant" not in response.text


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_history_pagination_is_bounded(query: str) -> None:
    client, _, _ = config_client()
    response = client.get(f"/api/v1/config/history?{query}", headers=bearer())

    assert response.status_code == 422


def test_diff_is_deterministic_and_same_version_is_empty() -> None:
    client, service, auth = config_client()
    first = asyncio.run(
        service.create_draft(auth.principal.tenant_id, auth.principal.user_id, config_document())
    )
    second = asyncio.run(
        service.create_draft(
            auth.principal.tenant_id,
            auth.principal.user_id,
            config_document(prompt="Changed."),
        )
    )

    changed = client.get(
        f"/api/v1/config/diff?from_version={first.version}&to_version={second.version}",
        headers=bearer(),
    )
    same = client.get(
        f"/api/v1/config/diff?from_version={first.version}&to_version={first.version}",
        headers=bearer(),
    )

    assert changed.status_code == 200
    assert changed.json() == {
        "from_version": 1,
        "to_version": 2,
        "added": [],
        "removed": [],
        "changed": [
            {
                "path": "/agents",
                "from": config_document()["agents"],
                "to": config_document(prompt="Changed.")["agents"],
            }
        ],
    }
    assert same.json()["added"] == same.json()["removed"] == same.json()["changed"] == []


def test_json_diff_escapes_pointer_segments() -> None:
    from agent_hub.api.routers.config import structured_diff

    assert structured_diff({"a/b~c": 1}, {"a/b~c": 2})["changed"] == [
        {"path": "/a~1b~0c", "from": 1, "to": 2}
    ]


def test_post_commit_notification_failure_returns_committed_revision() -> None:
    client, service, _ = config_client()
    draft = client.post("/api/v1/config/drafts", headers=bearer(), json=config_document())
    service.publish_notification_failure = True

    response = client.post(
        f"/api/v1/config/drafts/{draft.json()['id']}/publish", headers=bearer()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["notification_status"] == "failed"


class FakeSession:
    def __init__(self, execute_error: Exception | None = None) -> None:
        self.execute_error = execute_error

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> "FakeSession":
        return self

    async def execute(self, statement: object) -> None:
        del statement
        if self.execute_error is not None:
            raise self.execute_error


class FakeDatabase:
    def __init__(
        self,
        *,
        cleanup_error: BaseException | None = None,
        execute_error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.disposed = False
        self.cleanup_error = cleanup_error
        self.events = events
        self.session_factory = lambda: FakeSession(execute_error)

    async def dispose(self) -> None:
        self.disposed = True
        if self.events is not None:
            self.events.append("database.dispose")
        if self.cleanup_error is not None:
            raise self.cleanup_error


class FakeRedis:
    def __init__(
        self,
        *,
        cleanup_error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.closed = False
        self.cleanup_error = cleanup_error
        self.events = events

    async def aclose(self) -> None:
        self.closed = True
        if self.events is not None:
            self.events.append("redis.aclose")
        if self.cleanup_error is not None:
            raise self.cleanup_error

    async def ping(self, **kwargs: object) -> bool:
        del kwargs
        return True


def valid_settings() -> Settings:
    key = base64.urlsafe_b64encode(b"x" * 32).decode("ascii").rstrip("=")
    return Settings.model_validate(
        {"jwt_signing_key": "base64url:" + key}
    )


def test_injected_database_and_redis_are_not_closed() -> None:
    database = FakeDatabase()
    redis = FakeRedis()
    with TestClient(
        create_app(
            settings=valid_settings(),
            database=database,
            redis_client=redis,
            auth_service=StubAuthService(),
            config_service=StubConfigService(),
            rate_limiter=StubRateLimiter(),
            database_probe=Probe(),
            redis_probe=Probe(),
        )
    ):
        pass

    assert database.disposed is False
    assert redis.closed is False


def test_factory_created_database_and_redis_receive_unredacted_urls_and_are_closed() -> None:
    database = FakeDatabase()
    redis = FakeRedis()
    database_url = "postgresql+asyncpg://user:SECRET_DB@localhost/application"
    redis_url = "redis://:SECRET_REDIS@localhost:6379/0"
    configured = Settings.model_validate(
        {
            "jwt_signing_key": valid_settings().jwt_signing_key_value(),
            "database_url": database_url,
            "redis_url": redis_url,
        }
    )
    received: list[str] = []

    def database_factory(url: str) -> FakeDatabase:
        received.append(url)
        return database

    def redis_factory(url: str) -> FakeRedis:
        received.append(url)
        return redis

    with TestClient(
        create_app(
            settings=configured,
            database_factory=database_factory,
            redis_factory=redis_factory,
            database_probe=Probe(),
            redis_probe=Probe(),
        )
    ):
        pass

    assert database.disposed is True
    assert redis.closed is True
    assert received == [database_url, redis_url]


@pytest.mark.parametrize(
    ("redis_fails", "database_fails"),
    [(True, False), (False, True), (True, True)],
)
def test_all_owned_resource_cleanups_are_attempted_and_errors_are_safe(
    redis_fails: bool, database_fails: bool
) -> None:
    events: list[str] = []
    database = FakeDatabase(
        cleanup_error=(RuntimeError("LEAK_DATABASE_URL") if database_fails else None),
        events=events,
    )
    redis = FakeRedis(
        cleanup_error=(RuntimeError("LEAK_REDIS_URL") if redis_fails else None),
        events=events,
    )
    app = create_app(
        settings=valid_settings(),
        database_factory=lambda url: database,
        redis_factory=lambda url: redis,
        database_probe=Probe(),
        redis_probe=Probe(),
    )

    with pytest.raises(Exception) as captured, TestClient(app):
        pass

    assert events == ["redis.aclose", "database.dispose"]
    assert type(captured.value).__name__ == "ResourceCleanupError"
    assert "LEAK_" not in str(captured.value)


@pytest.mark.parametrize("cancelled_resource", ["redis", "database"])
async def test_cleanup_cancellation_is_rethrown_after_all_owned_resources(
    cancelled_resource: str,
) -> None:
    events: list[str] = []
    database = FakeDatabase(
        cleanup_error=(asyncio.CancelledError() if cancelled_resource == "database" else None),
        events=events,
    )
    redis = FakeRedis(
        cleanup_error=(asyncio.CancelledError() if cancelled_resource == "redis" else None),
        events=events,
    )
    task = asyncio.create_task(
        _cleanup_owned_resources(
            [("database", database.dispose), ("redis", redis.aclose)],
            primary_error=None,
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["redis.aclose", "database.dispose"]
    assert task.cancelled()


async def test_cleanup_cancellation_wins_over_ordinary_cleanup_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    database = FakeDatabase(cleanup_error=RuntimeError("LEAK_DB"), events=events)
    redis = FakeRedis(cleanup_error=asyncio.CancelledError(), events=events)
    task = asyncio.create_task(
        _cleanup_owned_resources(
            [("database", database.dispose), ("redis", redis.aclose)],
            primary_error=None,
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["redis.aclose", "database.dispose"]
    assert task.cancelled()
    assert "error_type=CancelledError" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "LEAK_DB" not in caplog.text


def test_primary_startup_error_is_preserved_over_cleanup_cancellation() -> None:
    events: list[str] = []
    database = FakeDatabase(
        execute_error=RuntimeError("PRIMARY_TENANT_FAILURE"), events=events
    )
    redis = FakeRedis(cleanup_error=asyncio.CancelledError(), events=events)
    app = create_app(
        settings=valid_settings(),
        database_factory=lambda url: database,
        redis_factory=lambda url: redis,
    )

    with pytest.raises(RuntimeError, match="PRIMARY_TENANT_FAILURE"), TestClient(app):
        pass

    assert events == ["redis.aclose", "database.dispose"]


@pytest.mark.parametrize("fatal_error", [SystemExit(7), KeyboardInterrupt()])
def test_fatal_base_exceptions_from_cleanup_are_not_swallowed(
    fatal_error: BaseException,
) -> None:
    async def fatal_cleanup() -> None:
        raise fatal_error

    cleanup = _cleanup_owned_resources(
        [("database", fatal_cleanup)], primary_error=None
    )

    with pytest.raises(type(fatal_error)):
        cleanup.send(None)


def test_startup_failure_cleans_every_resource_and_preserves_primary_error() -> None:
    events: list[str] = []
    database = FakeDatabase(
        execute_error=RuntimeError("PRIMARY_TENANT_FAILURE"),
        cleanup_error=RuntimeError("LEAK_DATABASE_URL"),
        events=events,
    )
    redis = FakeRedis(
        cleanup_error=RuntimeError("LEAK_REDIS_URL"),
        events=events,
    )
    factories: list[str] = []

    def database_factory(url: str) -> FakeDatabase:
        del url
        factories.append("database")
        return database

    def redis_factory(url: str) -> FakeRedis:
        del url
        factories.append("redis")
        return redis

    app = create_app(
        settings=valid_settings(),
        database_factory=database_factory,
        redis_factory=redis_factory,
    )

    with pytest.raises(RuntimeError, match="PRIMARY_TENANT_FAILURE"), TestClient(app):
        pass

    assert factories == ["database", "redis"]
    assert events == ["redis.aclose", "database.dispose"]


def test_invalid_jwt_key_is_rejected_before_resource_factories_run() -> None:
    factories: list[str] = []

    def database_factory(url: str) -> FakeDatabase:
        del url
        factories.append("database")
        return FakeDatabase()

    def redis_factory(url: str) -> FakeRedis:
        del url
        factories.append("redis")
        return FakeRedis()

    app = create_app(
        settings=Settings.model_validate(
            {"jwt_signing_key": "development-only-change-me"}
        ),
        database_factory=database_factory,
        redis_factory=redis_factory,
    )

    with pytest.raises(ValueError), TestClient(app):
        pass

    assert factories == []


def test_auth_request_body_size_is_bounded_before_validation() -> None:
    secret = "S" * 20_000
    response = auth_client().post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(uuid4()),
            "username": "owner",
            "password": secret,
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "request_too_large", "message": "request body is too large"}
    }
    assert secret not in response.text


def test_openapi_does_not_expose_persistence_secret_fields() -> None:
    schema = auth_client().get("/openapi.json")

    assert schema.status_code == 200
    serialized = schema.text.lower()
    for forbidden in (
        "password_hash",
        "code_hash",
        "ciphertext",
        "fingerprint",
        "nonce",
    ):
        assert forbidden not in serialized


def test_framework_404_uses_safe_error_envelope() -> None:
    response = auth_client().get("/missing/private-resource")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "resource not found"}
    }
    assert "detail" not in response.text


def test_framework_405_uses_envelope_and_preserves_allow() -> None:
    response = auth_client().post("/health/live")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json() == {
        "error": {
            "code": "method_not_allowed",
            "message": "method not allowed",
        }
    }


def test_unhandled_exception_is_generic_and_does_not_leak_request_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=StubRateLimiter(),
    )
    authorization = "Bearer SECRET_RUNTIME_TOKEN"
    password = "SECRET_RUNTIME_PASSWORD"

    async def fail(request: Request) -> None:
        raise RuntimeError(
            f"postgresql://secret-dsn {request.headers.get('authorization')} {password}"
        )

    app.add_api_route("/api/v1/fail", fail, methods=["POST"])
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post(
        "/api/v1/fail",
        headers={"Authorization": authorization},
        json={"password": password},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "internal server error"}
    }
    error_id = response.headers["x-error-id"]
    assert UUID(error_id)
    assert f"error_id={error_id}" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    captured = response.text + caplog.text
    assert "secret-dsn" not in captured
    assert authorization not in captured
    assert password not in captured


@pytest.mark.asyncio
async def test_safe_exception_middleware_aborts_started_stream_without_fake_eof(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def started_then_failed(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("SECRET_AFTER_START")

    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = SafeExceptionMiddleware(started_then_failed)
    with pytest.raises(StreamAbortedError) as captured:
        await middleware({"type": "http", "method": "GET", "path": "/"}, receive, send)

    assert [message["type"] for message in messages].count("http.response.start") == 1
    assert messages == [{"type": "http.response.start", "status": 200, "headers": []}]
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SECRET_AFTER_START" not in caplog.text
    rendered_traceback = "".join(
        traceback.format_exception(captured.type, captured.value, captured.tb)
    )
    assert "SECRET_AFTER_START" not in rendered_traceback


@pytest.mark.asyncio
async def test_safe_exception_middleware_propagates_request_cancellation() -> None:
    async def cancelled(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        raise asyncio.CancelledError

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        del message

    with pytest.raises(asyncio.CancelledError):
        await SafeExceptionMiddleware(cancelled)(
            {"type": "http", "method": "GET", "path": "/"}, receive, send
        )


def test_openapi_describes_security_health_and_route_specific_errors() -> None:
    schema = auth_client().get("/openapi.json").json()
    assert "ErrorResponse" in schema["components"]["schemas"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert schema["components"]["securitySchemes"] == {
        "BearerAuth": {"type": "http", "scheme": "bearer"}
    }

    assert "security" not in schema["paths"]["/health/live"]["get"]
    assert "security" not in schema["paths"]["/api/v1/setup"]["post"]
    assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]
    assert schema["paths"]["/api/v1/auth/me"]["get"]["security"] == [
        {"BearerAuth": []}
    ]
    assert schema["paths"]["/api/v1/config/current"]["get"]["security"] == [
        {"BearerAuth": []}
    ]

    config_403_matrix = {
        ("/api/v1/config/validate", "post"): True,
        ("/api/v1/config/drafts", "post"): True,
        ("/api/v1/config/drafts/{revision_id}/publish", "post"): True,
        ("/api/v1/config/history/{version}/rollback", "post"): True,
        ("/api/v1/config/current", "get"): False,
        ("/api/v1/config/history", "get"): False,
        ("/api/v1/config/history/{version}", "get"): False,
        ("/api/v1/config/diff", "get"): False,
    }
    for (path, method), expects_forbidden in config_403_matrix.items():
        assert ("403" in schema["paths"][path][method]["responses"]) is expects_forbidden

    for path in ("/health/live", "/health/ready"):
        assert schema["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/HealthResponse"}

    assert set(schema["paths"]["/health/live"]["get"]["responses"]) == {
        "200",
        "405",
        "500",
    }
    assert set(schema["paths"]["/health/ready"]["get"]["responses"]) == {
        "200",
        "405",
        "500",
        "503",
    }
    setup_responses = set(schema["paths"]["/api/v1/setup"]["post"]["responses"])
    assert setup_responses == {"201", "401", "405", "409", "413", "422", "429", "500", "503"}
    assert "404" not in setup_responses

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = operation["responses"]
            assert "405" in responses, (path, method, responses)
            for status_code in set(responses) - {"200", "201", "202"}:
                content = responses[status_code]["content"]["application/json"]
                assert content["schema"] == {
                    "$ref": "#/components/schemas/ErrorResponse"
                }


def proxy_auth_client(
    peer: str, trusted: list[str]
) -> tuple[TestClient, StubRateLimiter]:
    limiter = StubRateLimiter()
    app = create_app(
        settings=Settings.model_validate({"trusted_proxy_ips": trusted}),
        auth_service=StubAuthService(),
        rate_limiter=limiter,
    )
    return TestClient(app, client=(peer, 12345)), limiter


def proxy_login(
    client: TestClient, headers: dict[str, str] | list[tuple[str, str]]
) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers=headers,
        json={
            "tenant_id": str(uuid4()),
            "username": "owner",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 200


def test_trusted_proxy_single_hop_uses_canonical_forwarded_client() -> None:
    client, limiter = proxy_auth_client("192.0.2.10", ["192.0.2.10"])
    proxy_login(client, {"X-Forwarded-For": "2001:0db8:0:0:0:0:0:1"})

    assert limiter.calls == [("login", "2001:db8::1")]


def test_ipv4_mapped_socket_matches_ipv4_trusted_proxy_configuration() -> None:
    client, limiter = proxy_auth_client("::ffff:192.0.2.10", ["192.0.2.10"])
    proxy_login(client, {"X-Forwarded-For": "::ffff:198.51.100.7"})

    assert limiter.calls == [("login", "198.51.100.7")]


def test_proxy_chain_strips_trusted_hops_from_the_right() -> None:
    client, limiter = proxy_auth_client(
        "192.0.2.10", ["192.0.2.10", "192.0.2.20"]
    )
    proxy_login(
        client,
        {"X-Forwarded-For": "198.51.100.7, 192.0.2.20"},
    )

    assert limiter.calls == [("login", "198.51.100.7")]


def test_proxy_chain_ignores_attacker_prepended_spoof() -> None:
    client, limiter = proxy_auth_client(
        "192.0.2.10", ["192.0.2.10", "192.0.2.20"]
    )
    proxy_login(
        client,
        {"X-Forwarded-For": "203.0.113.99, 198.51.100.7, 192.0.2.20"},
    )

    assert limiter.calls == [("login", "198.51.100.7")]


@pytest.mark.parametrize(
    "forwarded",
    [
        "198.51.100.7,,192.0.2.20",
        "not-an-ip",
        "fe80::1%eth0",
        ",198.51.100.7",
        "1" * 2049,
        ",".join(["198.51.100.7"] * 33),
    ],
)
def test_invalid_or_oversized_forwarded_chain_falls_back_to_socket_peer(
    forwarded: str,
) -> None:
    client, limiter = proxy_auth_client("192.0.2.10", ["192.0.2.10"])
    proxy_login(client, {"X-Forwarded-For": forwarded})

    assert limiter.calls == [("login", "192.0.2.10")]


def test_multiple_forwarded_headers_fall_back_to_socket_peer() -> None:
    client, limiter = proxy_auth_client("192.0.2.10", ["192.0.2.10"])
    proxy_login(
        client,
        [("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-For", "203.0.113.8")],
    )

    assert limiter.calls == [("login", "192.0.2.10")]


def test_all_trusted_proxy_chain_uses_leftmost_canonical_hop() -> None:
    client, limiter = proxy_auth_client(
        "192.0.2.10", ["192.0.2.10", "192.0.2.20", "2001:db8::1"]
    )
    proxy_login(
        client,
        {"X-Forwarded-For": "2001:0db8:0:0:0:0:0:1, 192.0.2.20"},
    )

    assert limiter.calls == [("login", "2001:db8::1")]


def test_config_request_body_limit_accepts_boundary_and_rejects_overflow() -> None:
    client, _, _ = config_client()
    boundary = b"x" * (1024 * 1024)
    marker = b"DO_NOT_ECHO_CONFIG_MARKER"

    at_limit = client.post(
        "/api/v1/config/validate",
        headers={**bearer(), "Content-Type": "application/json"},
        content=boundary,
    )
    too_large = client.post(
        "/api/v1/config/validate",
        headers={**bearer(), "Content-Type": "application/json"},
        content=boundary + marker,
    )

    assert at_limit.status_code == 422
    assert too_large.status_code == 413
    assert too_large.json() == {
        "error": {
            "code": "request_too_large",
            "message": "request body is too large",
        }
    }
    assert marker.decode() not in too_large.text
