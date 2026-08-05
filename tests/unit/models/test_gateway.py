import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityLease,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    ModelGateway,
    ModelGatewayError,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)


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

    async def initialize(self) -> None:
        self.initialize_calls += 1
        self.initialized = True

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
        return CapacityLease(
            lease.id,
            lease.deployment_id,
            lease.quota_scope_id,
            lease.expires_at + timedelta(seconds=30),
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

    async def complete(
        self, deployment: Deployment, model_request: ModelRequest, api_key: str
    ) -> ModelResponse:
        self.events.append(("transport", deployment.id))
        self.calls.append((deployment, model_request, api_key))
        if self.block is not None:
            await self.block.wait()
        if self.failure is not None:
            raise self.failure
        return ModelResponse(text="ok")


class EstimatorStub:
    def estimate(self, model_request: ModelRequest) -> int:
        assert model_request.messages[0].content == "private prompt"
        return 73


def lease(identifier: str, scope: str | None = None) -> CapacityLease:
    return CapacityLease(
        id=f"lease-{identifier}",
        deployment_id=identifier,
        quota_scope_id=scope or f"scope-{identifier}",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
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
    assert events[0] == ("acquire", ("alpha", "beta"), 5.0, 73)
    assert events[1] == ("resolve", "secret://beta")
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

    with pytest.raises(ModelTransportError, match="safe transport failure"):
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

    with pytest.raises(ModelTransportError, match="primary safe"):
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
    assert acquire_events == [
        ("acquire", ("primary-key",), 0.02, 520),
        ("acquire", ("backup-key",), 0.02, 520),
    ]


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
    assert estimator.estimate(model_request) == estimator.estimate(model_request) == 520
    assert "private prompt" not in repr(estimator)


@pytest.mark.parametrize("status", [99, 600, True, "429"])
def test_transport_error_rejects_invalid_status(status: object) -> None:
    with pytest.raises(ValueError, match="status_code"):
        ModelTransportError("safe", status_code=status)  # type: ignore[arg-type]


def test_transport_error_exposes_valid_safe_status() -> None:
    error = ModelTransportError("safe", status_code=503)
    assert error.status_code == 503
    assert repr(error) == "ModelTransportError('safe')"
