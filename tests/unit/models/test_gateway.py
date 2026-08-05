import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_DNS, uuid5

import pytest

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityLease,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    GatewayCompletion,
    ModelGateway,
    ModelGatewayError,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    JsonValue,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredResponseSchema,
)


def test_gateway_completion_is_exported_from_models_package() -> None:
    from agent_hub.models import GatewayCompletion as PublicGatewayCompletion

    assert PublicGatewayCompletion is GatewayCompletion


def deployment(
    identifier: str,
    logical_model: str = "primary",
    *,
    capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.TEXT}),
) -> Deployment:
    return Deployment(
        id=identifier,
        logical_model=logical_model,
        secret_ref=f"secret://{identifier}",
        quota_scope_id=f"scope-{identifier}",
        capabilities=capabilities,
    )


def request(
    *,
    allow_fallback: bool = True,
    required: frozenset[ModelCapability] = frozenset(),
) -> ModelRequest:
    return ModelRequest(
        logical_model="primary",
        messages=(ModelMessage(role="user", content="private prompt"),),
        allow_fallback=allow_fallback,
        required_capabilities=required,
    )


class CapacityStub:
    def __init__(self, outcomes: Sequence[CapacityLease | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.events: list[object] = []
        self.releases: list[CapacityLease] = []
        self.renews = 0
        self.records: list[tuple[str, int | None, float, bool]] = []
        self.configured: tuple[Deployment, ...] = ()
        self.record_error: Exception | None = None
        self.release_error: Exception | None = None
        self.release_block: asyncio.Event | None = None
        self.release_started = asyncio.Event()
        self.initialize_calls = 0
        self.initialized = False
        self.renew_error: Exception | None = None

    async def initialize(self) -> None:
        self.initialize_calls += 1
        self.initialized = True
        self.events.append(("initialize",))

    def validate_configuration(self, deployments: Sequence[Deployment]) -> None:
        self.configured = tuple(deployments)

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        assert self.initialized is True
        self.events.append(
            ("acquire", tuple(item.id for item in candidates), wait_timeout, estimated_tokens)
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def release(self, lease: CapacityLease) -> bool:
        self.events.append(("release", lease.id))
        self.releases.append(lease)
        self.release_started.set()
        if self.release_block is not None:
            await self.release_block.wait()
        if self.release_error is not None:
            raise self.release_error
        return True

    async def renew(self, lease: CapacityLease) -> CapacityLease | None:
        self.renews += 1
        self.events.append(("renew", lease.id))
        if self.renew_error is not None:
            raise self.renew_error
        return CapacityLease(
            lease.id,
            lease.deployment_id,
            lease.quota_scope_id,
            lease.expires_at + timedelta(seconds=30),
            lease.renew_after_seconds,
        )

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None:
        self.records.append((quota_scope_id, status_code, latency_seconds, succeeded))
        if self.record_error is not None:
            raise self.record_error


class SecretStub:
    def __init__(self, events: list[object], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure
        self.references: list[str] = []

    async def resolve(self, secret_ref: str) -> str:
        self.events.append(("resolve", secret_ref))
        self.references.append(secret_ref)
        if self.failure is not None:
            raise self.failure
        return f"key-for-{secret_ref}"


class TransportStub:
    def __init__(
        self,
        events: list[object],
        *,
        failure: Exception | None = None,
        block: asyncio.Event | None = None,
    ) -> None:
        self.events = events
        self.failure = failure
        self.block = block
        self.calls: list[tuple[Deployment, ModelRequest, str]] = []
        self.cancelled = asyncio.Event()

    async def complete(
        self, deployment: Deployment, model_request: ModelRequest, api_key: str
    ) -> ModelResponse:
        self.events.append(("transport", deployment.id))
        self.calls.append((deployment, model_request, api_key))
        if self.block is not None:
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.failure is not None:
            raise self.failure
        return ModelResponse(text="ok")


class EstimatorStub:
    def estimate(self, model_request: ModelRequest) -> int:
        assert model_request.messages[0].content == "private prompt"
        return 73


class RawFailingTransport:
    def __init__(self) -> None:
        self.task: asyncio.Task[object] | None = None

    async def complete(
        self, deployment: Deployment, model_request: ModelRequest, api_key: str
    ) -> ModelResponse:
        del deployment, model_request
        current = asyncio.current_task()
        assert current is not None
        self.task = current
        raise RuntimeError(f"raw-provider-detail:{api_key}")


class TypedLeakingTransport:
    def __init__(self) -> None:
        self.task: asyncio.Task[object] | None = None

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str
    ) -> ModelResponse:
        self.task = asyncio.current_task()
        raise ModelTransportError(f"typed leaked {api_key}", status_code=429)


class AdvancingSecretResolver:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    async def resolve(self, secret_ref: str) -> str:
        del secret_ref
        self.clock[0] += 100
        return "private-api-key"


class AdvancingTransport:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    async def complete(
        self, deployment: Deployment, model_request: ModelRequest, api_key: str
    ) -> ModelResponse:
        del deployment, model_request, api_key
        self.clock[0] += 0.25
        return ModelResponse(text="ok")


def lease(identifier: str, scope: str | None = None) -> CapacityLease:
    return CapacityLease(
        id=str(uuid5(NAMESPACE_DNS, identifier)),
        deployment_id=identifier,
        quota_scope_id=scope or f"scope-{identifier}",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        renew_after_seconds=0.01,
    )


def monotonic(values: Sequence[float]) -> Callable[[], float]:
    iterator = iter(values)
    return lambda: next(iterator)


async def test_gateway_acquires_before_resolving_selected_deployment_secret() -> None:
    alpha, beta = deployment("alpha"), deployment("beta")
    selected_lease = lease("beta")
    capacity = CapacityStub([selected_lease])
    events = capacity.events
    secrets = SecretStub(events)
    transport = TransportStub(events)
    gateway = ModelGateway(
        ModelRegistry([alpha, beta]),
        capacity,
        secrets,
        transport,
        token_estimator=EstimatorStub(),
        monotonic=monotonic([10.0, 10.25]),
    )

    response = await gateway.complete(request())

    assert response.text == "ok"
    assert events[0] == ("initialize",)
    assert events[1] == ("acquire", ("alpha", "beta"), 5.0, 73)
    assert events[2] == ("resolve", "secret://beta")
    assert transport.calls[0][0] is beta
    assert transport.calls[0][2] == "key-for-secret://beta"
    assert capacity.records == [("scope-beta", 200, 0.25, True)]
    assert capacity.releases == [selected_lease]
    assert capacity.configured == (alpha, beta)
    assert capacity.initialize_calls == 1


async def test_secret_resolution_failure_is_redacted_and_releases() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    secret_value = "plaintext-api-key"
    secrets = SecretStub(capacity.events, RuntimeError(f"bad {secret_value}"))
    gateway = ModelGateway(
        ModelRegistry([selected]), capacity, secrets, TransportStub(capacity.events)
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.complete(request())

    assert secret_value not in str(captured.value)
    assert "private prompt" not in repr(captured.value)
    assert len(capacity.releases) == 1


async def test_429_status_and_latency_are_recorded_and_release_occurs() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    transport_error = ModelTransportError("safe transport failure", status_code=429)
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events, failure=transport_error),
        monotonic=monotonic([2.0, 2.4]),
    )

    with pytest.raises(ModelTransportError, match="model transport failed"):
        await gateway.complete(request())

    assert len(capacity.records) == 1
    recorded_scope, recorded_status, recorded_latency, succeeded = capacity.records[0]
    assert (recorded_scope, recorded_status, succeeded) == ("scope-selected", 429, False)
    assert recorded_latency == pytest.approx(0.4)
    assert len(capacity.releases) == 1


@pytest.mark.parametrize("status_code", [None, 500])
async def test_transport_failure_never_records_success(status_code: int | None) -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(
            capacity.events,
            failure=ModelTransportError("safe failure", status_code=status_code),
        ),
    )

    with pytest.raises(ModelTransportError):
        await gateway.complete(request())

    assert len(capacity.records) == 1
    assert capacity.records[0][1] == status_code
    assert capacity.records[0][3] is False


async def test_typed_transport_error_traceback_drops_transport_key_locals() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(
            capacity.events,
            failure=ModelTransportError("safe transport failure", status_code=503),
        ),
    )

    with pytest.raises(ModelTransportError) as captured:
        await gateway.complete(request())

    frame_values: list[str] = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frame_values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert "key-for-secret://selected" not in " ".join(frame_values)


async def test_typed_transport_error_message_and_captured_locals_are_redacted() -> None:
    selected = Deployment(
        id="selected",
        logical_model="primary",
        api_base="https://private-model.example/v1",
        secret_ref="secret://selected",
        quota_scope_id="private-scope",
    )
    capacity = CapacityStub([lease("selected", "private-scope")])
    transport = TypedLeakingTransport()
    gateway = ModelGateway(
        ModelRegistry([selected]), capacity, SecretStub(capacity.events), transport
    )

    with pytest.raises(ModelTransportError) as captured:
        await gateway.complete(request())

    error = captured.value
    assert str(error) == "model transport failed"
    assert error.status_code == 429
    assert error.__cause__ is None and error.__context__ is None
    assert transport.task is not None and transport.task.exception() is None
    locals_repr: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        locals_repr.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    exposed = " ".join(locals_repr)
    for secret in (
        "key-for-secret://selected",
        "private-scope",
        "private prompt",
        "private-model.example",
        "typed leaked",
    ):
        assert secret not in exposed


async def test_raw_transport_failure_does_not_escape_task_or_public_traceback() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    transport = RawFailingTransport()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        transport,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.complete(request())

    public = captured.value
    assert "raw-provider-detail" not in str(public)
    assert "key-for-secret://selected" not in repr(public)
    assert public.__cause__ is None
    assert public.__context__ is None
    assert transport.task is not None and transport.task.done()
    assert transport.task.exception() is None
    frame_values: list[str] = []
    traceback = public.__traceback__
    while traceback is not None:
        frame_values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    captured_locals = " ".join(frame_values)
    assert "raw-provider-detail" not in captured_locals
    assert "key-for-secret://selected" not in captured_locals


async def test_health_latency_excludes_secret_resolution_time() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    clock = [5.0]
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        AdvancingSecretResolver(clock),
        AdvancingTransport(clock),
        monotonic=lambda: clock[0],
    )

    await gateway.complete(request())

    assert capacity.records[0][2] == pytest.approx(0.25)


async def test_record_and_release_failures_do_not_replace_primary_transport_error() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    capacity.record_error = RuntimeError("redis record secret")
    capacity.release_error = RuntimeError("redis release secret")
    primary = ModelTransportError("primary safe", status_code=503)
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events, failure=primary),
    )

    with pytest.raises(ModelTransportError, match="model transport failed"):
        await gateway.complete(request())
    assert len(capacity.releases) == 1


async def test_cancellation_propagates_and_releases_exactly_once() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    block = asyncio.Event()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events, block=block),
        heartbeat_interval=0.01,
    )
    task = asyncio.create_task(gateway.complete(request()))
    while not any(event == ("transport", "selected") for event in capacity.events):
        await asyncio.sleep(0)
    task.cancel("caller cancellation")

    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert captured.value.args == ("caller cancellation",)
    assert len(capacity.releases) == 1


async def test_cancellation_during_release_wait_propagates_after_cleanup() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    capacity.release_block = asyncio.Event()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
    )
    task = asyncio.create_task(gateway.complete(request()))
    await capacity.release_started.wait()
    task.cancel("release cancellation")
    capacity.release_block.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert captured.value.args == ("release cancellation",)
    assert len(capacity.releases) == 1


async def test_cancellation_during_unknown_lease_cleanup_remains_cancelled() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("unknown")])
    capacity.release_block = asyncio.Event()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
    )
    task = asyncio.create_task(gateway.complete(request()))
    await capacity.release_started.wait()
    task.cancel("unknown lease cleanup cancelled")
    capacity.release_block.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert captured.value.args == ("unknown lease cleanup cancelled",)
    assert task.cancelled()
    assert len(capacity.releases) == 1


async def test_long_call_renews_lease_until_nonstream_response_finishes() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    block = asyncio.Event()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events, block=block),
        heartbeat_interval=0.005,
    )
    task = asyncio.create_task(gateway.complete(request()))
    while capacity.renews < 2:
        await asyncio.sleep(0.002)
    assert capacity.releases == []
    block.set()
    assert (await task).text == "ok"
    assert len(capacity.releases) == 1


async def test_short_lease_renews_before_expiry_with_large_heartbeat_cap() -> None:
    selected = deployment("selected")
    short_lease = CapacityLease(
        id=str(uuid5(NAMESPACE_DNS, "short-lease")),
        deployment_id="selected",
        quota_scope_id="scope-selected",
        expires_at=datetime.now(UTC) + timedelta(seconds=0.08),
        renew_after_seconds=0.02,
    )
    capacity = CapacityStub([short_lease])
    block = asyncio.Event()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events, block=block),
        heartbeat_interval=10,
    )
    task = asyncio.create_task(gateway.complete(request()))

    async def wait_for_renew() -> None:
        while capacity.renews == 0:
            await asyncio.sleep(0.002)

    await asyncio.wait_for(wait_for_renew(), timeout=0.2)
    block.set()
    await task
    assert capacity.renews >= 1


async def test_heartbeat_ownership_conflict_cancels_transport_and_releases_once() -> None:
    selected = deployment("selected")
    capacity = CapacityStub([lease("selected")])
    capacity.renew_error = CapacityConfigurationError("credential ownership conflict")
    block = asyncio.Event()
    transport = TransportStub(capacity.events, block=block)
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        transport,
        heartbeat_interval=0.005,
    )

    with pytest.raises(CapacityConfigurationError, match="ownership conflict"):
        await gateway.complete(request())

    assert transport.cancelled.is_set()
    assert len(capacity.releases) == 1
    assert capacity.events.count(("release", lease("selected").id)) == 1


async def test_capacity_timeout_traverses_fallback_only_when_allowed() -> None:
    primary = deployment("primary-key")
    backup = deployment("backup-key", "backup")
    capacity = CapacityStub([CapacityWaitTimeout("busy"), lease("backup-key")])
    gateway = ModelGateway(
        ModelRegistry([primary, backup]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
        fallbacks={"primary": "backup"},
        capacity_wait_timeout=0.02,
    )

    assert (await gateway.complete(request())).text == "ok"
    acquire_events = [event for event in capacity.events if event[0] == "acquire"]  # type: ignore[index]
    estimated = ConservativeTokenEstimator().estimate(request())
    assert acquire_events == [
        ("acquire", ("primary-key",), 0.02, estimated),
        ("acquire", ("backup-key",), 0.02, estimated),
    ]
    assert capacity.initialize_calls == 2


async def test_completion_context_reports_actual_fallback_provenance() -> None:
    primary = deployment("primary-key")
    backup = deployment("backup-key", "backup")
    capacity = CapacityStub([CapacityWaitTimeout("busy"), lease("backup-key")])
    gateway = ModelGateway(
        ModelRegistry([primary, backup]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
        fallbacks={"primary": "backup"},
    )

    completion = await gateway.complete_with_context(request())

    assert completion.deployment_id == "backup-key"
    assert completion.logical_model == "backup"
    assert completion.provider_id == "openai"
    assert completion.provider_model == "openai/gpt-4o-mini"
    assert "provider_model" not in repr(completion)
    assert not hasattr(completion, "quota_scope_id")


async def test_no_fallback_when_request_disallows_it() -> None:
    primary = deployment("primary-key")
    backup = deployment("backup-key", "backup")
    capacity = CapacityStub([CapacityWaitTimeout("busy")])
    gateway = ModelGateway(
        ModelRegistry([primary, backup]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
        fallbacks={"primary": "backup"},
    )

    with pytest.raises(CapacityUnavailable, match="model capacity unavailable"):
        await gateway.complete(request(allow_fallback=False))
    assert len([event for event in capacity.events if event[0] == "acquire"]) == 1  # type: ignore[index]


async def test_fallback_must_satisfy_request_capabilities() -> None:
    primary = deployment(
        "primary-key", capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION})
    )
    backup = deployment("backup-key", "backup")
    capacity = CapacityStub([CapacityWaitTimeout("busy")])
    gateway = ModelGateway(
        ModelRegistry([primary, backup]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
        fallbacks={"primary": "backup"},
    )

    with pytest.raises(CapacityUnavailable, match="model capacity unavailable"):
        await gateway.complete(request(required=frozenset({ModelCapability.VISION})))
    assert len([event for event in capacity.events if event[0] == "acquire"]) == 1  # type: ignore[index]


@pytest.mark.parametrize(
    "failure",
    [CapacityQueueFull("full"), CapacityBackendError("backend unavailable")],
)
async def test_immediate_capacity_failure_does_not_fallback(failure: Exception) -> None:
    primary = deployment("primary-key")
    backup = deployment("backup-key", "backup")
    capacity = CapacityStub([failure])
    gateway = ModelGateway(
        ModelRegistry([primary, backup]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events),
        fallbacks={"primary": "backup"},
    )

    with pytest.raises(type(failure)):
        await gateway.complete(request())
    assert len([event for event in capacity.events if event[0] == "acquire"]) == 1  # type: ignore[index]


@pytest.mark.parametrize(
    "fallbacks, message",
    [
        ({"primary": "missing"}, "unknown fallback"),
        ({"primary": "primary"}, "must not reference itself"),
        ({"primary": "backup", "backup": "primary"}, "cycle"),
        ({"Primary": "backup"}, "safe identifier"),
    ],
)
def test_fallback_configuration_is_validated(
    fallbacks: dict[str, str], message: str
) -> None:
    registry = ModelRegistry([deployment("one"), deployment("two", "backup")])
    with pytest.raises(ValueError, match=message):
        ModelGateway(
            registry,
            CapacityStub([]),
            SecretStub([]),
            TransportStub([]),
            fallbacks=fallbacks,
        )


def test_default_estimator_is_deterministic_positive_and_prompt_safe() -> None:
    model_request = request()
    estimator = ConservativeTokenEstimator()
    assert estimator.estimate(model_request) == estimator.estimate(model_request)
    assert estimator.estimate(model_request) >= model_request.max_output_tokens + len(
        b"private prompt"
    )
    assert "private prompt" not in repr(estimator)


def test_estimator_covers_normalized_utf8_numbers_booleans_null_and_nesting() -> None:
    huge_integer = 10**999
    schema_value: Mapping[str, JsonValue] = {
        "type": "object",
        "properties": {
            "value": {"enum": (huge_integer, 1.25, True, None, "雪")},
        },
    }
    model_request = ModelRequest(
        logical_model="primary",
        messages=(
            ModelMessage(role="user", content="你好"),
            ModelMessage(
                role="user",
                content=(
                    {"type": "text", "text": "nested"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/雪.png", "detail": "high"},
                    },
                ),
            ),
        ),
        required_capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
        response_schema=StructuredResponseSchema(name="Payload", schema=schema_value),
        max_output_tokens=17,
    )
    estimator = ConservativeTokenEstimator()
    serialized_schema_bytes = len(
        json.dumps(schema_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    estimate = estimator.estimate(model_request)

    assert estimate >= serialized_schema_bytes + 17
    assert estimate > 1100


@pytest.mark.parametrize("allowance", [0, -1, 1.0, True, None, 1_000_001])
def test_model_request_requires_strict_bounded_output_budget(allowance: object) -> None:
    with pytest.raises((TypeError, ValueError), match="max_output_tokens"):
        ModelRequest(
            logical_model="primary",
            messages=(),
            max_output_tokens=allowance,  # type: ignore[arg-type]
        )


def test_capacity_lease_repr_is_redacted_and_fields_are_validated() -> None:
    active = lease("sensitive-deployment", "sensitive-scope")
    assert repr(active) == "CapacityLease(<redacted>)"
    assert "sensitive" not in repr(active)

    with pytest.raises(ValueError, match="lease id"):
        CapacityLease("not-a-uuid", "deployment", "scope", datetime.now(UTC), 1)
    with pytest.raises(ValueError, match="deployment id"):
        CapacityLease(
            str(uuid5(NAMESPACE_DNS, "x")), "Bad Deployment", "scope", datetime.now(UTC), 1
        )
    for invalid_delay in (-1, float("inf"), float("nan"), True, "1"):
        with pytest.raises(ValueError, match="renewal delay"):
            CapacityLease(
                str(uuid5(NAMESPACE_DNS, "delay")),
                "deployment",
                "scope",
                datetime.now(UTC),
                invalid_delay,  # type: ignore[arg-type]
            )


async def test_zero_relative_renewal_delay_fails_closed_without_spinning() -> None:
    selected = deployment("selected")
    immediate = CapacityLease(
        str(uuid5(NAMESPACE_DNS, "immediate")),
        "selected",
        "scope-selected",
        datetime.now(UTC) + timedelta(seconds=30),
        0,
    )
    capacity = CapacityStub([immediate])
    block = asyncio.Event()
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        TransportStub(capacity.events, block=block),
    )
    with pytest.raises(CapacityBackendError, match="renewal timing"):
        await gateway.complete(request())
    assert capacity.renews == 3
    with pytest.raises(ValueError, match="quota_scope_id"):
        CapacityLease(
            str(uuid5(NAMESPACE_DNS, "x")), "deployment", "bad scope", datetime.now(UTC), 1
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        CapacityLease(
            str(uuid5(NAMESPACE_DNS, "x")),
            "deployment",
            "scope",
            datetime(2025, 1, 1),  # noqa: DTZ001 - intentionally exercises naive rejection
            1,
        )


def test_deployment_repr_omits_operational_and_credential_details() -> None:
    configured = Deployment(
        id="safe-id",
        logical_model="safe-model",
        api_base="https://private.example/v1",
        secret_ref="secret://private-key",
        quota_scope_id="private-scope",
    )
    exposed = repr(configured)
    assert "safe-id" in exposed and "safe-model" in exposed
    assert "private.example" not in exposed
    assert "secret://private-key" not in exposed
    assert "private-scope" not in exposed


@pytest.mark.parametrize("status", [99, 600, True, "429"])
def test_transport_error_rejects_invalid_status(status: object) -> None:
    with pytest.raises(ValueError, match="status_code"):
        ModelTransportError("safe", status_code=status)  # type: ignore[arg-type]


def test_transport_error_exposes_valid_safe_status() -> None:
    error = ModelTransportError("safe", status_code=503)
    assert error.status_code == 503
    assert repr(error) == "ModelTransportError('safe')"
