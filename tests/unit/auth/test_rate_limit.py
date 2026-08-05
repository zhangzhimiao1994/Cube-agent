from typing import Any

import pytest

from agent_hub.auth.rate_limit import RateLimitUnavailable, RedisAuthRateLimiter


class RedisStub:
    def __init__(self, result: object = (1, 60)) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: object) -> object:
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_redis_limiter_uses_atomic_script_and_hashed_client_key() -> None:
    redis = RedisStub((6, 37))
    limiter = RedisAuthRateLimiter(redis, b"k" * 32)

    decision = await limiter.check("setup", "203.0.113.19")

    assert decision.allowed is False
    assert decision.retry_after == 37
    assert len(redis.calls) == 1
    script, key_count, key, window = redis.calls[0]
    assert "INCR" in str(script) and "EXPIRE" in str(script)
    assert key_count == 1
    assert str(key).startswith("agent-hub:auth:setup:")
    assert "203.0.113.19" not in str(key)
    assert window == 300


async def test_login_limiter_allows_at_limit() -> None:
    redis = RedisStub((10, 12))
    decision = await RedisAuthRateLimiter(redis, b"k" * 32).check(
        "login", "198.51.100.2"
    )

    assert decision.allowed is True
    assert decision.retry_after == 0
    assert redis.calls[0][3] == 60


async def test_redis_limiter_fails_closed_without_backend_details() -> None:
    limiter = RedisAuthRateLimiter(
        RedisStub(RuntimeError("redis://password@secret-host")), b"k" * 32
    )

    with pytest.raises(RateLimitUnavailable) as captured:
        await limiter.check("login", "192.0.2.10")

    assert "secret-host" not in str(captured.value)
