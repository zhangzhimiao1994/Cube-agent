import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, AuthResult, Role
from agent_hub.auth.rate_limit import RedisAuthRateLimiter


class AuthStub:
    def __init__(self) -> None:
        self.principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.ADMIN)

    async def login(self, tenant_id: UUID, username: str, password: str) -> AuthResult:
        del tenant_id, username, password
        return AuthResult(self.principal, "token")

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        del token
        return self.principal


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


async def _delete_prefix(client: Redis, prefix: str) -> None:
    keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
    if keys:
        await client.delete(*keys)


@pytest.mark.integration
async def test_real_redis_lua_sets_ttl_once_and_counts_atomically(
    redis_client: Redis,
) -> None:
    ttl_prefix = f"agent-hub:test:ttl:{uuid4()}:"
    concurrent_prefix = f"agent-hub:test:concurrent:{uuid4()}:"
    try:
        ttl_limiter = RedisAuthRateLimiter(
            redis_client, uuid4().bytes + uuid4().bytes, key_prefix=ttl_prefix
        )
        first = await ttl_limiter.check("login", "198.51.100.10")
        keys = [key async for key in redis_client.scan_iter(match=f"{ttl_prefix}*")]
        assert first.allowed is True
        assert len(keys) == 1
        first_ttl = await redis_client.ttl(keys[0])
        assert first_ttl > 0

        await asyncio.sleep(1.1)
        await ttl_limiter.check("login", "198.51.100.10")
        second_ttl = await redis_client.ttl(keys[0])
        assert 0 < second_ttl < first_ttl

        concurrent = RedisAuthRateLimiter(
            redis_client,
            uuid4().bytes + uuid4().bytes,
            key_prefix=concurrent_prefix,
        )
        decisions = await asyncio.gather(
            *(concurrent.check("login", "203.0.113.5") for _ in range(12))
        )
        assert sum(decision.allowed for decision in decisions) == 10
        concurrent_keys = [
            key async for key in redis_client.scan_iter(match=f"{concurrent_prefix}*")
        ]
        assert len(concurrent_keys) == 1
        assert int(await redis_client.get(concurrent_keys[0])) == 12
    finally:
        await _delete_prefix(redis_client, ttl_prefix)
        await _delete_prefix(redis_client, concurrent_prefix)


@pytest.mark.integration
async def test_real_redis_route_429_and_connection_failure_503(
    redis_client: Redis, redis_url: str
) -> None:
    prefix = f"agent-hub:test:route:{uuid4()}:"
    limiter = RedisAuthRateLimiter(
        redis_client, uuid4().bytes + uuid4().bytes, key_prefix=prefix
    )
    auth = AuthStub()
    try:
        for _ in range(10):
            assert (await limiter.check("login", "127.0.0.1")).allowed is True
        app = create_app(
            auth_service=auth,
            config_service=object(),
            rate_limiter=limiter,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            limited = await client.post(
                "/api/v1/auth/login",
                json={
                    "tenant_id": str(auth.principal.tenant_id),
                    "username": "owner",
                    "password": "correct horse battery staple",
                },
            )
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0

        unavailable_redis = Redis.from_url("redis://127.0.0.1:1/15")
        try:
            unavailable = create_app(
                auth_service=auth,
                config_service=object(),
                rate_limiter=RedisAuthRateLimiter(
                    unavailable_redis,
                    uuid4().bytes + uuid4().bytes,
                    key_prefix=f"agent-hub:test:failure:{uuid4()}:",
                ),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=unavailable),
                base_url="http://test",
            ) as client:
                failed = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "tenant_id": str(auth.principal.tenant_id),
                        "username": "owner",
                        "password": "correct horse battery staple",
                    },
                )
            assert failed.status_code == 503
        finally:
            await unavailable_redis.aclose()
    finally:
        await _delete_prefix(redis_client, prefix)


async def _database_ready() -> None:
    return None


@pytest.mark.integration
async def test_real_redis_readiness_success_and_failure(redis_client: Redis) -> None:
    healthy = create_app(
        auth_service=object(),
        config_service=object(),
        rate_limiter=object(),
        redis_client=redis_client,
        database_probe=_database_ready,
    )
    async with healthy.router.lifespan_context(healthy), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=healthy), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200

    unavailable_redis = Redis.from_url("redis://127.0.0.1:1/15")
    try:
        unhealthy = create_app(
            auth_service=object(),
            config_service=object(),
            rate_limiter=object(),
            redis_client=unavailable_redis,
            database_probe=_database_ready,
        )
        async with unhealthy.router.lifespan_context(unhealthy), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=unhealthy), base_url="http://test"
        ) as client:
            response = await client.get("/health/ready")
        assert response.status_code == 503
    finally:
        await unavailable_redis.aclose()
