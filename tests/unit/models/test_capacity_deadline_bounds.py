import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest

from agent_hub.harness.provider import NormalizedProviderEvent
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityPool,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.registry import ModelRegistry
from tests.unit.models.test_capacity import InMemoryCapacityRedis, fingerprint, pool
from tests.unit.models.test_gateway import (
    SecretStub,
    StreamingTransportStub,
    deployment,
    request,
)


class DeadlineRedis(InMemoryCapacityRedis):
    """Model committed admission separately from a lost response or slow release."""

    def __init__(self) -> None:
        super().__init__()
        self.leases: set[str] = set()
        self.busy_keys: set[str] = set()
        self.busy_reason = 1
        self.on_busy: Callable[[], None] = lambda: None
        self.acquire_delay = 0.0
        self.hang_acquire = False
        self.malformed = False
        self.connection_error = False
        self.block_release = False
        self.allow_release = asyncio.Event()
        self.release_started = asyncio.Event()
        self.release_finished = asyncio.Event()
        self.release_cancelled = asyncio.Event()
        self.attempted_keys: list[str] = []

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        if key_count == 5:
            self.attempted_keys.append(str(args[0]))
            await asyncio.sleep(self.acquire_delay)
            if str(args[0]) in self.busy_keys:
                self.on_busy()
                return [0, 1, 1, self.busy_reason]
            self.leases.add(str(args[5]))
            self.acquire_started.set()
            if self.hang_acquire:
                await asyncio.Future[None]()
            if self.connection_error:
                raise ConnectionError("private backend detail")
            if self.malformed:
                return [True, 1, 1, 1_800_000_000_000]
            return [1, 1, 1, 1_800_000_000_000]
        if key_count == 1 and len(args) == 3:
            self.release_started.set()
            try:
                if self.block_release:
                    await self.allow_release.wait()
                self.leases.discard(str(args[1]))
                self.release_finished.set()
                return 1
            except asyncio.CancelledError:
                self.release_cancelled.set()
                raise
        return await super().eval(script, key_count, *args)


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("reason", [1, 2])
async def test_acknowledged_congestion_stops_scanning_at_shared_window(
    monkeypatch: pytest.MonkeyPatch, scoped: bool, reason: int,
) -> None:
    redis = DeadlineRedis()
    candidates = [deployment(f"primary-{index}") for index in range(8)]
    root = pool(redis, candidates)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()
    redis.busy_keys = {root._keys(item.quota_scope_id)["leases"] for item in candidates}
    redis.busy_reason = reason
    loop = asyncio.get_running_loop()
    real_time = loop.time
    elapsed = 0.0

    def busy_reply() -> None:
        nonlocal elapsed
        elapsed += 4

    redis.on_busy = busy_reply
    monkeypatch.setattr(loop, "time", lambda: real_time() + elapsed)
    with pytest.raises(CapacityWaitTimeout):
        await capacity.acquire(candidates, wait_timeout=10, estimated_tokens=11)

    assert len(redis.attempted_keys) == 3
    assert not redis.leases
    assert root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("reason", [1, 2])
@pytest.mark.parametrize("window", [0.004, 0.054], ids=["terminal-sleep", "regular-poll"])
async def test_terminal_poll_does_not_restart_admission_when_timer_wakes_early(
    monkeypatch: pytest.MonkeyPatch, scoped: bool, reason: int, window: float,
) -> None:
    redis = DeadlineRedis()
    selected = deployment("selected")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    redis.busy_keys = {root._keys(selected.quota_scope_id)["leases"]}
    redis.busy_reason = reason
    loop = asyncio.get_running_loop()
    real_sleep = asyncio.sleep
    now = loop.time()
    sleeps: list[float] = []

    async def early_sleep(delay: float) -> None:
        nonlocal now
        if delay > 0:
            sleeps.append(delay)
            # An event-loop timer may run just before its nominal deadline.
            now += delay + (-0.000001 if len(sleeps) == 1 else 0.000001)
        await real_sleep(0)

    with monkeypatch.context() as patch:
        patch.setattr(loop, "time", lambda: now)
        patch.setattr(asyncio, "sleep", early_sleep)
        with pytest.raises(CapacityWaitTimeout):
            await capacity.acquire([selected], wait_timeout=window, estimated_tokens=11)

    assert len(redis.attempted_keys) == 1, "terminal polling must not issue a near-zero-budget RPC"
    assert len(sleeps) == (1 if window < root._poll_interval else 2)
    assert not redis.leases and root._waiters == 0


class RegistrationDeadlineRedis(DeadlineRedis):
    """Delay a metadata commit and retain owner-specific rollback side effects."""

    def __init__(self, *, attempt: int) -> None:
        super().__init__()
        self.blocked_attempt = attempt
        self.blocked_owner: str | None = None
        self.registration_attempts = 0
        self.registration_started = asyncio.Event()
        self.registration_finished = asyncio.Event()
        self.allow_registration = asyncio.Event()

    async def eval(self, script: str, key_count: int, *args: object) -> object:
        if key_count == 2 and len(args) == 3:
            self.fingerprint_owners.get(str(args[0]), set()).discard(str(args[2]))
            return 1
        if key_count == 7 and len(args) == 8:
            self.policy_owners.get(str(args[1]), set()).discard(str(args[7]))
            return 1
        if (
            key_count == 2
            and len(args) == 6
            and str(args[2]) == self.blocked_owner
        ):
            self.registration_attempts += 1
            if self.registration_attempts == self.blocked_attempt:
                self.registration_started.set()
                try:
                    # Commit only after release, so premature rollback cannot pass.
                    await self.allow_registration.wait()
                    return await super().eval(script, key_count, *args)
                finally:
                    self.registration_finished.set()
        return await super().eval(script, key_count, *args)


@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("attempt", [1, 2], ids=["first-init", "retry-init"])
async def test_initialize_deadline_and_eventual_metadata_ownership(
    streaming: bool, attempt: int,
) -> None:
    redis = RegistrationDeadlineRedis(attempt=attempt)
    selected = deployment("selected")
    peer = pool(redis, [selected])
    await peer.initialize()
    root = CapacityPool(
        redis,
        deployments=[selected],
        fingerprint_resolver=fingerprint,
        key_prefix="agent-hub:test:scoped-capacity:",
        lease_seconds=0.4,
        poll_interval=0.005,
        metadata_refresh_interval=0.02,
    )
    redis.blocked_owner = root._owner_id
    redis.busy_keys = {root._keys(selected.quota_scope_id)["leases"]}
    events: list[object] = []
    secrets = SecretStub(events)
    transport = StreamingTransportStub(events, [])
    gateway = ModelGateway(
        ModelRegistry([selected]), root, secrets, transport, capacity_wait_timeout=0.05,
    )
    model_request = replace(request(allow_fallback=False), timeout_seconds=0.12)

    async def invoke() -> None:
        if streaming:
            _events = [
                event async for event in gateway.stream_openai_compatible_events(model_request)
            ]
        else:
            await gateway.complete(model_request)

    async def metadata_settled() -> None:
        await redis.registration_finished.wait()
        # Registration holds this lock through its owner rollback.
        async with root._registration_lock:
            pass

    loop = asyncio.get_running_loop()
    started = loop.time()
    pending = asyncio.create_task(invoke())
    settlement = asyncio.create_task(metadata_settled())
    try:
        await asyncio.wait_for(redis.registration_started.wait(), timeout=1)
        done, _ = await asyncio.wait({pending}, timeout=max(0, started + 0.3 - loop.time()))
        returned_before_release = pending in done
    finally:
        # Drain even on RED without cancellation concealing the deadline defect.
        redis.allow_registration.set()
        results = await asyncio.wait_for(
            asyncio.gather(pending, settlement, return_exceptions=True), timeout=1,
        )

    expected_owners = {peer._owner_id}
    if attempt == 2:
        expected_owners.add(root._owner_id)
    assert list(redis.fingerprint_owners.values()) == [expected_owners]
    assert list(redis.policy_owners.values()) == [expected_owners]
    assert results[1] is None
    assert not secrets.references and not transport.calls and not transport.stream_calls
    assert not redis.leases and root._waiters == 0
    assert returned_before_release, "initialize must return within the total request deadline"
    assert isinstance(results[0], CapacityBackendError)


@pytest.mark.parametrize("scoped", [False, True])
async def test_inflight_rpc_uses_remaining_window_not_a_fresh_window(scoped: bool) -> None:
    redis = DeadlineRedis()
    candidates = [deployment(f"primary-{index}") for index in range(4)]
    root = pool(redis, candidates)
    capacity = root.scoped(candidates) if scoped else root
    await capacity.initialize()
    redis.busy_keys = {root._keys(item.quota_scope_id)["leases"] for item in candidates}
    redis.acquire_delay = 0.12
    started = asyncio.get_running_loop().time()

    with pytest.raises(CapacityBackendError):
        await capacity.acquire(candidates, wait_timeout=0.2, estimated_tokens=11)

    assert asyncio.get_running_loop().time() - started < 0.32
    assert len(redis.attempted_keys) == 2
    assert root._waiters == 0


@pytest.mark.parametrize("scoped", [False, True])
async def test_backend_timeout_returns_before_blocked_cleanup(scoped: bool) -> None:
    redis = DeadlineRedis()
    redis.hang_acquire = redis.block_release = True
    selected = deployment("selected")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(CapacityBackendError):
            await capacity.acquire([selected], wait_timeout=0.2, estimated_tokens=11)
        assert asyncio.get_running_loop().time() - started < 0.32
        assert root._waiters == 0
        assert redis.leases
    finally:
        redis.allow_release.set()
        if not redis.release_cancelled.is_set():
            await asyncio.wait_for(redis.release_finished.wait(), timeout=1)
    assert not redis.leases


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("external_deadline", [False, True])
async def test_cancellation_does_not_wait_for_committed_lease_cleanup(
    scoped: bool, external_deadline: bool,
) -> None:
    redis = DeadlineRedis()
    redis.hang_acquire = redis.block_release = True
    selected = deployment("selected")
    root = pool(redis, [selected])
    capacity = root.scoped([selected]) if scoped else root
    await capacity.initialize()

    async def acquire() -> None:
        if external_deadline:
            await asyncio.wait_for(
                capacity.acquire([selected], wait_timeout=1, estimated_tokens=11), 0.1,
            )
        else:
            await capacity.acquire([selected], wait_timeout=1, estimated_tokens=11)

    pending = asyncio.create_task(acquire())
    try:
        await asyncio.wait_for(redis.acquire_started.wait(), timeout=1)
        started = asyncio.get_running_loop().time()
        if not external_deadline:
            pending.cancel("caller cancelled")
        done, _ = await asyncio.wait({pending}, timeout=0.25)
        assert pending in done, "cleanup must not extend cancellation or the total deadline"
        with pytest.raises(TimeoutError if external_deadline else asyncio.CancelledError) as error:
            await pending
        if not external_deadline:
            assert error.value.args == ("caller cancelled",)
        assert asyncio.get_running_loop().time() - started < 0.25
        assert root._waiters == 0
    finally:
        redis.allow_release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.wait_for(redis.release_finished.wait(), timeout=1)
    assert not redis.leases


@pytest.mark.parametrize("failure", ["timeout", "malformed", "connection"])
async def test_unknown_commit_is_cleaned_without_masking_backend_failure(failure: str) -> None:
    redis = DeadlineRedis()
    redis.hang_acquire = failure == "timeout"
    redis.malformed = failure == "malformed"
    redis.connection_error = failure == "connection"
    selected = deployment("selected")
    root = pool(redis, [selected])
    await root.initialize()
    with pytest.raises(CapacityBackendError) as error:
        await root.acquire([selected], wait_timeout=0.05, estimated_tokens=11)
    assert "private" not in str(error.value)
    await asyncio.wait_for(redis.release_finished.wait(), timeout=0.25)
    assert not redis.leases
    assert root._waiters == 0


@pytest.mark.parametrize("streaming", [False, True])
async def test_shared_window_leaves_time_for_authorized_fallback(
    monkeypatch: pytest.MonkeyPatch, streaming: bool,
) -> None:
    redis = DeadlineRedis()
    primary = [deployment(f"primary-{index}") for index in range(8)]
    backup = deployment("backup", "backup")
    root = pool(redis, [*primary, backup])
    redis.busy_keys = {root._keys(item.quota_scope_id)["leases"] for item in primary}
    loop = asyncio.get_running_loop()
    real_time = loop.time
    elapsed = 0.0

    def busy_reply() -> None:
        nonlocal elapsed
        elapsed += 4

    redis.on_busy = busy_reply
    monkeypatch.setattr(loop, "time", lambda: real_time() + elapsed)
    events: list[object] = []
    secrets = SecretStub(events)
    transport = StreamingTransportStub(events, [{"choices": [{"delta": {"content": "ok"}}]}])
    gateway = ModelGateway(
        ModelRegistry([*primary, backup]), root, secrets, transport,
        fallbacks={"primary": "backup"}, capacity_wait_timeout=10,
    )
    model_request = replace(request(), timeout_seconds=25)
    if streaming:
        stream_events: list[NormalizedProviderEvent] = [
            event async for event in gateway.stream_openai_compatible_events(model_request)
        ]
        assert any(event.kind == "model.fallback" for event in stream_events)
    else:
        assert (await gateway.complete(model_request)).text == "ok"
    assert secrets.references == [backup.secret_ref]
    assert len(redis.attempted_keys) == 4
    assert redis.attempted_keys[-1] == root._keys(backup.quota_scope_id)["leases"]
    assert len(transport.stream_calls if streaming else transport.calls) == 1
    assert not redis.leases


async def test_detached_cleanup_is_bounded_and_does_not_mask_backend_error() -> None:
    redis = DeadlineRedis()
    redis.hang_acquire = redis.block_release = True
    selected = deployment("selected")
    root = pool(redis, [selected])
    await root.initialize()
    with pytest.raises(CapacityBackendError):
        await root.acquire([selected], wait_timeout=0.05, estimated_tokens=11)
    assert root._waiters == 0
    await asyncio.wait_for(redis.release_cancelled.wait(), timeout=1.5)
    # A permanently unreachable backend keeps only the original TTL-fenced lease.
    assert redis.leases
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not root._lease_cleanups


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
async def test_gateway_last_window_keeps_backend_failure_classification(
    streaming: bool, scoped: bool,
) -> None:
    redis = DeadlineRedis()
    redis.hang_acquire = redis.block_release = True
    selected = deployment("selected")
    root = pool(redis, [selected])
    events: list[object] = []
    secrets = SecretStub(events)
    transport = StreamingTransportStub(events, [])
    gateway = ModelGateway(
        ModelRegistry([selected]), root.scoped([selected]) if scoped else root,
        secrets, transport, capacity_wait_timeout=1,
    )
    model_request = replace(request(allow_fallback=False), timeout_seconds=0.1)
    try:
        with pytest.raises(CapacityBackendError):
            if streaming:
                _events = [
                    event async for event in gateway.stream_openai_compatible_events(model_request)
                ]
            else:
                await gateway.complete(model_request)
    finally:
        redis.allow_release.set()
        await asyncio.wait_for(redis.release_finished.wait(), timeout=1)
    assert not secrets.references and not transport.calls and not transport.stream_calls
    assert not redis.leases
    assert root._waiters == 0
