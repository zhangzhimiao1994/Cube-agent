import asyncio
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest
from redis import exceptions as redis_errors

from agent_hub.models import capacity as capacity_module
from agent_hub.models import gateway as gateway_module
from agent_hub.models.capacity import CapacityBackendError, CapacityPool
from agent_hub.models.gateway import (
    GatewayScopeDiagnostic,
    ModelGateway,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.registry import ModelRegistry
from tests.unit.models.test_capacity import InMemoryCapacityRedis, fingerprint
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    TransportStub,
    deployment,
    request,
)

PRIVATE = "Authorization: private prompt redis://user:password@private.invalid"


def diagnostic(error: object) -> Any:
    getter = getattr(capacity_module, "get_capacity_backend_diagnostic", None)
    assert callable(getter), "native capacity diagnostic is missing"
    return getter(error)


class FailingRedis(InMemoryCapacityRedis):
    def __init__(self, stage: int, failure: Exception | None) -> None:
        super().__init__()
        self.stage = stage
        self.failure = failure
        self.failed_calls = 0

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        if key_count == self.stage and len(args) not in {3, 8}:
            self.failed_calls += 1
            if self.failure is None:
                await asyncio.Future[None]()
            else:
                raise self.failure
        return await super().eval(script, key_count, *args)


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("stage,operation", [
    (2, "register_fingerprint"), (7, "register_scope"), (5, "acquire"),
])
@pytest.mark.parametrize("failure,reason,category,origin", [
    (TimeoutError(PRIVATE), "timeout", "builtin_timeout", "client"),
    (redis_errors.TimeoutError(PRIVATE), "timeout", "redis_timeout", "redis"),
    (redis_errors.ConnectionError(PRIVATE), "connection_error", "redis_connection", "none"),
    (redis_errors.ResponseError(PRIVATE), "server_error", "redis_server", "none"),
    (redis_errors.AuthenticationError(PRIVATE), "connection_error", "redis_connection", "none"),
    (TypeError(PRIVATE), "programming_error", "type_error", "none"),
    (RuntimeError(PRIVATE), "programming_error", "runtime_error", "none"),
    (ConnectionError(PRIVATE), "unknown_error", "unknown", "none"),
])
async def test_native_classification_precedes_redaction(
    scoped: bool, stage: int, operation: str, failure: Exception,
    reason: str, category: str, origin: str,
) -> None:
    redis = FailingRedis(stage, failure)
    selected = deployment("selected")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    capacity = root.scoped([selected]) if scoped else root
    with pytest.raises(CapacityBackendError) as caught:
        await capacity.initialize()
        await capacity.acquire([selected], 0.1, estimated_tokens=1)
    result = diagnostic(caught.value)
    assert result is not None
    assert (result.operation.value, result.backend_reason.value,
            result.exception_category.value, result.timeout_origin.value,
            result.commit_state.value) == (operation, reason, category, origin, "unknown")
    assert str(caught.value) == "model capacity backend unavailable"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert PRIVATE not in repr(result)
    assert redis.failed_calls == 1
    assert root._waiters == 0
    with pytest.raises(FrozenInstanceError):
        result.operation = "untrusted"


@pytest.mark.parametrize("stage,operation", [
    (2, "register_fingerprint"), (7, "register_scope"), (5, "acquire"),
])
async def test_outer_wait_for_deadline_is_not_redis_timeout(stage: int, operation: str) -> None:
    redis = FailingRedis(stage, None)
    selected = deployment("selected")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint,
                        lease_seconds=0.02)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
        await root.acquire([selected], 0.02, estimated_tokens=1)
    result = diagnostic(caught.value)
    assert result.operation.value == operation
    assert result.backend_reason.value == "timeout"
    assert result.exception_category.value == "builtin_timeout"
    assert result.timeout_origin.value == "local_deadline"
    assert result.commit_state.value == "unknown"
    assert redis.failed_calls == 1
    if root._lease_cleanups:
        await asyncio.gather(*root._lease_cleanups)


async def test_sync_eval_signature_failure_keeps_commit_unknown() -> None:
    class BadSignature:
        def eval(self) -> object:
            raise AssertionError("body must not be entered")

    selected = deployment("selected")
    root = CapacityPool(BadSignature(), deployments=[selected], fingerprint_resolver=fingerprint)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
    result = diagnostic(caught.value)
    assert result.operation.value == "register_fingerprint"
    assert result.exception_category.value == "type_error"
    assert result.commit_state.value == "unknown"


async def test_missing_eval_is_not_started() -> None:
    selected = deployment("selected")
    root = CapacityPool(object(), deployments=[selected], fingerprint_resolver=fingerprint)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
    assert diagnostic(caught.value).commit_state.value == "not_started"


@pytest.mark.parametrize("stage", [2, 7, 5])
async def test_gateway_carries_native_metadata_without_transport_or_fallback(
    stage: int, caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FailingRedis(stage, redis_errors.TimeoutError(PRIVATE))
    primary, backup = deployment("primary"), deployment("backup", "backup")
    root = CapacityPool(redis, deployments=[primary, backup], fingerprint_resolver=fingerprint)
    events: list[object] = []
    secrets, transport = SecretStub(events), TransportStub(events)
    gateway = ModelGateway(ModelRegistry([primary, backup]), root, secrets, transport,
                           fallbacks={"primary": "backup"})
    observed: list[gateway_module.GatewayScopeDiagnostic] = []
    token = gateway_module._gateway_scope_observer.set(observed.append)
    try:
        with pytest.raises(CapacityBackendError) as caught:
            await gateway.complete(request())
    finally:
        gateway_module._gateway_scope_observer.reset(token)
    result = diagnostic(caught.value)
    assert result is not None
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None and scope.capacity_backend is result
    assert result is not None
    assert observed == [scope]
    assert scope.transport_entered_count == scope.failure_attempt_count == 0
    assert scope.reason.value == "capacity_backend_failure"
    assert not secrets.references and not transport.calls and redis.failed_calls == 1
    records = [r for r in caplog.records if r.name == gateway_module.__name__]
    assert len(records) == 1 and records[0].exc_info is None
    assert records[0].getMessage() == (
        "model_capacity_failure kind=backend reason=backend_unavailable "
        f"operation={result.operation.value} backend_reason=timeout "
        "exception_category=redis_timeout timeout_origin=redis commit_state=unknown"
    )
    assert PRIVATE not in caplog.text


async def test_legacy_and_replayed_metadata_are_not_trusted() -> None:
    legacy = CapacityBackendError("model capacity backend unavailable")
    legacy.diagnostic = {"operation": "acquire"}  # type: ignore[attr-defined]
    assert diagnostic(legacy) is None
    redis = FailingRedis(2, RuntimeError(PRIVATE))
    selected = deployment("selected")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
    other = CapacityBackendError(*caught.value.args)
    other.__dict__.update(caught.value.__dict__)
    assert diagnostic(other) is None
    original = diagnostic(caught.value)
    assert original is not None
    copied = replace(original)
    assert copied == original and hash(copied) == hash(original)


async def test_hostile_exception_attributes_and_metaclass_are_not_consulted() -> None:
    class HostileType(type):
        def __hash__(cls) -> int:
            raise AssertionError("must not hash adapter type")

        def __eq__(cls, other: object) -> bool:
            raise AssertionError("must not compare adapter type")

    class Hostile(RuntimeError, metaclass=HostileType):
        def __getattribute__(self, name: str) -> Any:
            if name in {"diagnostic", "timeout_origin", "commit_state", "args",
                        "__cause__", "__context__", "__class__"}:
                raise AssertionError("must not read adapter attributes")
            return super().__getattribute__(name)

        def __str__(self) -> str:
            raise AssertionError("must not format adapter error")

    error = Hostile()
    assert diagnostic(error) is None
    redis = FailingRedis(2, error)
    selected = deployment("selected")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
    result = diagnostic(caught.value)
    assert result.exception_category.value == "unknown"
    assert result.backend_reason.value == "unknown_error"


def payload(value: object) -> Any:
    helper = getattr(capacity_module, "capacity_backend_diagnostic_payload", None)
    assert callable(helper), "safe capacity payload helper is missing"
    return helper(value)


async def test_payload_is_issued_enum_values_only_and_rejects_reconstruction() -> None:
    redis = FailingRedis(2, TypeError(PRIVATE))
    selected = deployment("selected")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
    original = diagnostic(caught.value)
    assert payload(original) == {
        "operation": "register_fingerprint", "backend_reason": "programming_error",
        "exception_category": "type_error", "timeout_origin": "none", "commit_state": "unknown",
    }
    assert payload(None) is None
    assert payload(replace(original)) is None
    assert payload({"operation": "acquire"}) is None
    object.__setattr__(original, "operation", PRIVATE)
    assert payload(original) is None
    assert diagnostic(caught.value) is None


async def test_gateway_initialization_deadline_is_diagnostic_only() -> None:
    redis = FailingRedis(2, None)
    selected = deployment("primary")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint,
                        lease_seconds=0.1)
    events: list[object] = []
    secrets, transport = SecretStub(events), TransportStub(events)
    gateway = ModelGateway(ModelRegistry([selected]), root, secrets, transport,
                           capacity_wait_timeout=0.01)
    try:
        with pytest.raises(CapacityBackendError) as caught:
            await gateway.complete(replace(request(allow_fallback=False), timeout_seconds=0.1))
        result = diagnostic(caught.value)
        assert result is not None
        assert payload(result) == {
            "operation": "initialize", "backend_reason": "timeout",
            "exception_category": "builtin_timeout", "timeout_origin": "local_deadline",
            "commit_state": "unknown",
        }
        assert str(caught.value) == "model capacity initialization timed out"
        scope = get_gateway_scope_diagnostic(caught.value)
        assert scope is not None and scope.capacity_backend is result
        assert not secrets.references and not transport.calls
        assert redis.failed_calls == 1
    finally:
        if gateway._capacity_initializers:
            await asyncio.gather(*gateway._capacity_initializers, return_exceptions=True)


async def test_legacy_scope_hash_reason_and_log_are_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = CapacityBackendError("model capacity backend unavailable")
    error.capacity_backend = {"timeout_origin": "redis"}  # type: ignore[attr-defined]
    capacity = CapacityStub([error])
    selected = deployment("primary")
    gateway = ModelGateway(ModelRegistry([selected]), capacity,
                           SecretStub(capacity.events), TransportStub(capacity.events))
    with pytest.raises(CapacityBackendError) as caught:
        await gateway.complete(request())
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None and scope.capacity_backend is None
    assert hash(scope) == hash((ScopeIncompletePhase.PRETRANSPORT_CAPACITY,
                               ScopeIncompleteReason.CAPACITY_BACKEND_FAILURE, 0, 0))
    assert str(caught.value) == "model capacity backend unavailable"
    assert caplog.records[-1].getMessage() == (
        "model_capacity_failure kind=backend reason=backend_unavailable"
    )
    assert payload(scope.capacity_backend) is None


async def test_native_metadata_does_not_change_scope_hash_or_allow_forgery() -> None:
    redis = FailingRedis(2, RuntimeError(PRIVATE))
    selected = deployment("primary")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    events: list[object] = []
    gateway = ModelGateway(ModelRegistry([selected]), root, SecretStub(events), TransportStub(events))
    with pytest.raises(CapacityBackendError) as caught:
        await gateway.complete(request())
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None
    legacy = GatewayScopeDiagnostic(scope.phase, scope.reason, 0, 0)
    assert scope == legacy and hash(scope) == hash(legacy)
    assert scope.capacity_backend is not None
    with pytest.raises(ValueError, match="invalid gateway scope diagnostic"):
        GatewayScopeDiagnostic(scope.phase, scope.reason, 0, 0,
                               capacity_backend=replace(scope.capacity_backend))


async def test_redis_named_adapter_subclass_is_unknown() -> None:
    class RedisTimeoutImpostor(redis_errors.TimeoutError):
        timeout_origin = "redis"

    redis = FailingRedis(2, RedisTimeoutImpostor(PRIVATE))
    selected = deployment("selected")
    root = CapacityPool(redis, deployments=[selected], fingerprint_resolver=fingerprint)
    with pytest.raises(CapacityBackendError) as caught:
        await root.initialize()
    assert diagnostic(caught.value).exception_category.value == "unknown"
    assert diagnostic(caught.value).timeout_origin.value == "none"
