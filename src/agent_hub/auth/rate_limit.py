"""Fail-closed per-client authentication rate limiting."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import ClassVar, Protocol, cast


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class RateLimitUnavailable(RuntimeError):
    pass


class AuthRateLimiter(Protocol):
    async def check(self, endpoint: str, client_ip: str) -> RateLimitDecision: ...


class RedisEvalClient(Protocol):
    async def eval(self, script: str, key_count: int, *args: object) -> object: ...


class RedisAuthRateLimiter:
    _SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""
    _LIMITS: ClassVar[dict[str, tuple[int, int]]] = {
        "setup": (5, 300),
        "login": (10, 60),
    }

    def __init__(self, redis_client: object, hmac_key: bytes) -> None:
        if len(hmac_key) < 32:
            raise ValueError("rate-limit HMAC key must be at least 32 bytes")
        self._redis = cast(RedisEvalClient, redis_client)
        self._hmac_key = hmac_key

    async def check(self, endpoint: str, client_ip: str) -> RateLimitDecision:
        if endpoint not in self._LIMITS:
            raise ValueError("unsupported authentication endpoint")
        limit, window = self._LIMITS[endpoint]
        digest = hmac.new(
            self._hmac_key, client_ip.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        key = f"agent-hub:auth:{endpoint}:{digest}"
        try:
            result = await self._redis.eval(self._SCRIPT, 1, key, window)
            if not isinstance(result, (list, tuple)) or len(result) != 2:
                raise TypeError("invalid Redis rate-limit result")
            count, ttl = int(result[0]), max(1, int(result[1]))
        except Exception:  # noqa: BLE001 -- limiter must fail closed for any backend failure.
            raise RateLimitUnavailable("authentication rate limiter unavailable") from None
        return RateLimitDecision(count <= limit, 0 if count <= limit else ttl)
