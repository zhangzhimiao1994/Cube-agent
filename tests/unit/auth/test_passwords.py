import asyncio
import threading
from typing import Literal

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from agent_hub.auth.models import AuthenticationBusy
from agent_hub.auth.passwords import PasswordService, PasswordValidationError


class RejectingEmbeddedCostHasher(PasswordHasher):
    def __init__(self) -> None:
        super().__init__(type=Type.ID)
        self.verify_calls = 0
        self.rehash_calls = 0

    def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:
        self.verify_calls += 1
        return super().verify(hash, password)

    def check_needs_rehash(self, hash: str | bytes) -> bool:
        self.rehash_calls += 1
        return super().check_needs_rehash(hash)


class BlockingHasher(PasswordHasher):
    def __init__(self) -> None:
        super().__init__(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.active = 0
        self.peak = 0
        self.completed = 0
        self._lock = threading.Lock()

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.started.set()
        self.release.wait(timeout=2)
        try:
            return super().hash(password, salt=salt)
        finally:
            with self._lock:
                self.active -= 1
                self.completed += 1
                self.finished.set()


def _assert_password_absent(error: BaseException, password: str) -> None:
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_hub/" in filename:
            assert all(password not in repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert error.__cause__ is None
    assert error.__context__ is None


def test_argon2id_round_trip_and_wrong_password() -> None:
    service = PasswordService()
    password = "correct horse battery staple"

    password_hash = service.hash(password)

    assert password_hash.startswith("$argon2id$")
    assert password not in password_hash
    assert service.verify(password_hash, password) is True
    assert service.verify(password_hash, "incorrect password") is False


def test_malformed_hash_is_the_same_boolean_failure_as_a_wrong_password() -> None:
    service = PasswordService()

    assert service.verify("not-an-argon-hash", "correct horse battery staple") is False


def test_needs_rehash_uses_the_configured_argon2_parameters() -> None:
    old_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    current = PasswordService()
    old_hash = old_hasher.hash("correct horse battery staple")

    assert current.needs_rehash(old_hash) is True
    assert current.needs_rehash(current.hash("correct horse battery staple")) is False
    assert current.needs_rehash("malformed") is False


@pytest.mark.parametrize(
    "password",
    [
        None,
        123,
        "",
        " " * 12,
        "short",
        "x" * 1025,
        "\ud800" + "x" * 11,
    ],
)
def test_password_policy_rejects_invalid_values_without_echoing_them(password: object) -> None:
    service = PasswordService()

    with pytest.raises(PasswordValidationError) as captured:
        service.hash(password)  # type: ignore[arg-type]

    if repr(password) != "''":
        assert repr(password) not in repr(captured.value)
    if str(password):
        assert str(password) not in str(captured.value)


def test_password_policy_accepts_boundaries_without_normalizing() -> None:
    service = PasswordService()

    minimum_hash = service.hash(" x" + "y" * 10)
    maximum_hash = service.hash("z" * 1024)

    assert service.verify(minimum_hash, " x" + "y" * 10)
    assert not service.verify(minimum_hash, "x" + "y" * 10)
    assert service.verify(maximum_hash, "z" * 1024)


def test_embedded_extreme_argon_cost_is_rejected_before_the_hasher() -> None:
    hasher = RejectingEmbeddedCostHasher()
    service = PasswordService(hasher)
    extreme = "$argon2id$v=19$m=999999999,t=999,p=999$c2FsdHNhbHQ$YWJjZA"

    assert service.verify(extreme, "correct horse battery staple") is False
    assert service.needs_rehash(extreme) is False
    assert hasher.verify_calls == 0
    assert hasher.rehash_calls == 0


@pytest.mark.asyncio
async def test_async_argon_is_offloaded_and_has_bounded_admission() -> None:
    hasher = BlockingHasher()
    service = PasswordService(hasher, max_concurrency=1, max_waiters=0)
    first = asyncio.create_task(service.hash_async("first valid password"))
    assert await asyncio.to_thread(hasher.started.wait, 1)

    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    await heartbeat
    with pytest.raises(AuthenticationBusy, match="authentication is busy") as busy:
        await service.hash_async("second valid password")
    _assert_password_absent(busy.value, "second valid password")

    hasher.release.set()
    assert (await first).startswith("$argon2id$")
    assert hasher.peak == 1


@pytest.mark.asyncio
async def test_cancelling_running_argon_does_not_release_real_capacity() -> None:
    hasher = BlockingHasher()
    service = PasswordService(
        hasher, max_concurrency=1, max_waiters=0, acquire_timeout_seconds=2
    )
    running = asyncio.create_task(service.hash_async("first valid password"))
    assert await asyncio.to_thread(hasher.started.wait, 1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await running
    _assert_password_absent(cancelled.value, "first valid password")
    with pytest.raises(AuthenticationBusy):
        await service.hash_async("second valid password")

    hasher.release.set()
    assert await asyncio.to_thread(hasher.finished.wait, 2)
    assert (await service.hash_async("third valid password")).startswith("$argon2id$")
    assert hasher.peak == 1


@pytest.mark.asyncio
async def test_cancelling_waiting_argon_keeps_admission_until_worker_finishes() -> None:
    hasher = BlockingHasher()
    service = PasswordService(
        hasher, max_concurrency=1, max_waiters=1, acquire_timeout_seconds=2
    )
    running = asyncio.create_task(service.hash_async("first valid password"))
    assert await asyncio.to_thread(hasher.started.wait, 1)
    waiting = asyncio.create_task(service.hash_async("second valid password"))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    with pytest.raises(AuthenticationBusy):
        await service.hash_async("third valid password")

    hasher.release.set()
    await running
    for _ in range(100):
        if hasher.completed >= 2:
            break
        await asyncio.sleep(0.01)
    assert hasher.completed == 2
    assert (await service.hash_async("fourth valid password")).startswith("$argon2id$")
    assert hasher.peak == 1


def test_password_service_is_safe_across_concurrent_event_loops() -> None:
    hasher = BlockingHasher()
    service = PasswordService(
        hasher, max_concurrency=1, max_waiters=1, acquire_timeout_seconds=2
    )
    outcomes: list[str | RuntimeError] = []

    def run(password: str) -> None:
        try:
            outcomes.append(asyncio.run(service.hash_async(password)))
        except RuntimeError as error:
            outcomes.append(error)

    first = threading.Thread(target=run, args=("first valid password",))
    second = threading.Thread(target=run, args=("second valid password",))
    first.start()
    assert hasher.started.wait(timeout=1)
    second.start()
    hasher.release.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert len(outcomes) == 2
    assert all(isinstance(outcome, str) for outcome in outcomes)
    assert hasher.peak == 1


@pytest.mark.asyncio
async def test_async_password_validation_error_has_no_password_in_frames() -> None:
    password = "too-short"

    with pytest.raises(PasswordValidationError) as captured:
        await PasswordService().hash_async(password)

    _assert_password_absent(captured.value, password)


def test_sync_password_validation_error_has_no_password_in_frames() -> None:
    password = "too-short"

    with pytest.raises(PasswordValidationError) as captured:
        PasswordService().hash(password)

    _assert_password_absent(captured.value, password)
