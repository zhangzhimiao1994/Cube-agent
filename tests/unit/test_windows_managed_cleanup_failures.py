from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_hub import windows_managed_subprocess
from agent_hub.windows_managed_subprocess import _JobTransport

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows bridge failure fixtures")


@pytest.fixture(autouse=True)
def release_only_ram_fixture_owners() -> Iterator[None]:
    retained = windows_managed_subprocess._retained_owners
    before = set(retained)
    yield
    for owner in retained - before:
        assert isinstance(owner.job, _Job), "native retained ownership must not be discarded"
        retained.discard(owner)


class _RetireProof(asyncio.Future[bool]):
    def __init__(self) -> None:
        super().__init__()
        self.subscribed = asyncio.Event()

    def add_done_callback(self, fn: Any, *, context: Any = None) -> None:
        self.subscribed.set()
        super().add_done_callback(fn, context=context)


class _NativeWait(asyncio.Future[bool]):
    def __init__(self, mode: str = "success") -> None:
        super().__init__()
        self.mode = mode
        self._event_fut: asyncio.Future[bool] | None = None
        self.retiring = asyncio.Event()
        self.proof: _RetireProof | None = None
        self._registered = True
        self._ov: object | None = object()
        self._event: object | None = object()
        self._proactor: object | None = object()

    def _unregister_wait_cb(self, future: asyncio.Future[bool]) -> None:
        self._ov = self._event = self._proactor = None
        self._event_fut = None

    def _unregister(self) -> None:
        if not self._registered:
            return
        self._registered = False
        self.retiring.set()
        if self.mode == "missing":
            return
        self.proof = _RetireProof()
        self._event_fut = self.proof
        if self.mode != "gated":
            self.finish_retirement()

    def finish_retirement(self) -> None:
        assert self.proof is not None
        if self.mode == "error":
            self.proof.set_exception(OSError("controlled unregister failure"))
        else:
            self.proof.set_result(True)
        self._unregister_wait_cb(self.proof)
        self.proof.exception()

    def set_result(self, result: bool) -> None:
        self._unregister()
        super().set_result(result)

    def cancel(self, msg: object = None) -> bool:
        self._unregister()
        return super().cancel(msg)


class _Pipe:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def close(self) -> None:
        self.calls += 1
        if self.fail:
            raise OSError("controlled pipe close failure")


class _Job:
    pid = 1
    root_wait_handle = 2

    def __init__(self, root: asyncio.Future[bool]) -> None:
        self.root = root
        self.close_calls = 0

    def poll_root(self) -> int:
        return 0

    def terminate_members(self) -> None:
        raise OSError("controlled termination failure")

    def close(self) -> None:
        self.close_calls += 1
        if not self.root.done():
            self.root.set_result(True)


class _Loop:
    def __init__(self, root: asyncio.Future[bool]) -> None:
        self._proactor = SimpleNamespace(wait_for_handle=lambda handle: root)

    def create_future(self) -> asyncio.Future[Any]:
        return asyncio.get_running_loop().create_future()

    def create_task(self, coroutine: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine)


@pytest.mark.parametrize("allocation", ["future", "watcher"])
def test_startup_allocation_failure_precedes_native_registration(allocation: str) -> None:
    loop = asyncio.new_event_loop()
    root = loop.create_future()
    cast(Any, root)._unregister_wait_cb = lambda future: None
    registrations: list[int] = []

    class Loop(_Loop):
        allocations = 0

        def create_future(self) -> asyncio.Future[Any]:
            self.allocations += 1
            if allocation == "future" and self.allocations == 3:
                raise RuntimeError("controlled future allocation failure")
            return loop.create_future()

        def create_task(self, coroutine: Any) -> asyncio.Task[Any]:
            coroutine.close()
            raise RuntimeError("controlled watcher allocation failure")

    fixture = Loop(root)

    def register(handle: int) -> asyncio.Future[bool]:
        registrations.append(handle)
        return root

    fixture._proactor.wait_for_handle = register
    try:
        with pytest.raises(RuntimeError, match="controlled"):
            owner = _JobTransport(cast(Any, _Job(root)), {}, cast(Any, fixture))
            starter = getattr(owner, "start", None)
            if starter is not None:
                starter()
        assert not registrations, "constructor failure left a registered borrowed handle"
    finally:
        loop.close()


async def _owner(job: _Job, parents: dict[int, Any], root: _NativeWait) -> _JobTransport:
    owner = _JobTransport(cast(Any, job), parents, cast(Any, _Loop(root)))
    starter = getattr(owner, "start", None)
    if starter is not None:
        starter()
    await asyncio.sleep(0)
    return owner


async def test_watcher_fault_attempts_all_pipes_and_settles_waiter() -> None:
    root = _NativeWait()
    job = _Job(root)
    owner = await _owner(job, {}, root)
    assert owner._watcher is not None
    bad, good = _Pipe(fail=True), _Pipe()
    owner.pipes = {1: cast(Any, bad), 2: cast(Any, good)}
    root.set_result(True)
    await asyncio.gather(owner._watcher, return_exceptions=True)
    assert good.calls == 1, "pipe failure skipped another owned pipe"
    assert job.close_calls == 1, "pipe failure skipped Job cleanup"
    assert owner._finished.done(), "watcher fault left process.wait pending forever"
    assert owner._finished.exception() is not None


async def test_close_termination_failure_still_closes_pipes_and_finishes() -> None:
    root = _NativeWait()
    job = _Job(root)
    owner = await _owner(job, {}, root)
    assert owner._watcher is not None
    pipe = _Pipe()
    owner.pipes = {1: cast(Any, pipe)}
    try:
        with pytest.raises(OSError):
            owner.close()
        assert pipe.calls == 1, "termination error skipped owned pipe closure"
        await asyncio.wait_for(asyncio.gather(owner._watcher, return_exceptions=True), 1)
        assert owner._finished.done()
        assert owner._finished.exception() is not None
    finally:
        if not root.done():
            root.set_result(True)
        await asyncio.gather(owner._watcher, return_exceptions=True)


def test_raw_handle_failure_does_not_skip_remaining_owned_handles() -> None:
    closer = getattr(windows_managed_subprocess, "_close_raw_handles", None)
    assert callable(closer), "raw handle cleanup must attempt all handles"
    bad, good, another = _Pipe(fail=True), _Pipe(), _Pipe()
    errors = closer([bad, good, another])
    assert good.calls == another.calls == 1
    assert len(errors) == 1 and isinstance(errors[0], OSError)


async def test_close_error_interrupts_watcher_already_waiting_for_pipe_eof() -> None:
    root = _NativeWait()

    class Job(_Job):
        entered = asyncio.Event()

        def terminate_members(self) -> None:
            self.entered.set()

    job = Job(root)
    owner = await _owner(job, {1: _Pipe(fail=True)}, root)
    assert owner._watcher is not None
    root.set_result(True)
    await job.entered.wait()
    try:
        with pytest.raises(OSError):
            owner.close()
        done, _ = await asyncio.wait({owner._watcher}, timeout=0.1)
        assert owner._watcher in done, "pipe EOF wait ignored an explicit cleanup failure"
        assert owner._finished.done() and owner._finished.exception() is not None
    finally:
        owner._outputs_closed.set()
        await asyncio.gather(owner._watcher, return_exceptions=True)


@pytest.mark.parametrize("mode", ["missing", "error"])
async def test_unconfirmed_retirement_preserves_native_handle_ownership(mode: str) -> None:
    root = _NativeWait(mode)

    class Job(_Job):
        terminations = 0

        def terminate_members(self) -> None:
            self.terminations += 1

    job = Job(root)
    owner = await _owner(job, {}, root)
    assert owner._watcher is not None
    root.set_result(True)
    await asyncio.wait_for(asyncio.gather(owner._watcher, return_exceptions=True), 1)
    assert job.close_calls == 0, "released a handle without successful retirement proof"
    assert job.terminations >= 2, "retirement failure skipped family termination"
    assert owner._finished.done() and owner._finished.exception() is not None
    assert root._ov is not None, "unconfirmed retirement cleared registration ownership"


async def test_late_close_error_during_retirement_is_not_success() -> None:
    root = _NativeWait("gated")

    class Job(_Job):
        def terminate_members(self) -> None:
            pass

    job = Job(root)
    owner = await _owner(job, {}, root)
    assert owner._watcher is not None
    root.set_result(True)
    await root.retiring.wait()
    assert root.proof is not None
    await asyncio.wait_for(root.proof.subscribed.wait(), 1)
    assert not owner._watcher.done(), "retirement gate was not held"
    owner.pipes = {1: cast(Any, _Pipe(fail=True))}
    try:
        with pytest.raises(OSError):
            owner.close()
    finally:
        root.finish_retirement()
        await asyncio.gather(owner._watcher, return_exceptions=True)
    assert job.close_calls == 1
    assert owner._finished.exception() is not None, "late cleanup failure became success"


async def test_retirement_failure_is_not_hidden_by_pipe_failure() -> None:
    root = _NativeWait("error")

    class Job(_Job):
        def terminate_members(self) -> None:
            pass

    job = Job(root)
    owner = await _owner(job, {}, root)
    assert owner._watcher is not None
    owner.pipes = {1: cast(Any, _Pipe(fail=True))}
    with pytest.raises(OSError):
        owner.close()
    await asyncio.gather(owner._watcher, return_exceptions=True)
    error = owner._finished.exception()
    assert job.close_calls == 0
    assert error is not None and "retirement" in str(error), "retained ownership was hidden"
    assert error.__cause__ is not None and "unregister" in str(error.__cause__)


async def test_registration_allocation_error_is_unknown_not_unregistered() -> None:
    root = _NativeWait()
    job = _Job(root)
    loop = _Loop(root)

    def fail_registration(handle: int) -> None:
        raise OSError("controlled allocation after native registration")

    loop._proactor.wait_for_handle = fail_registration
    owner = _JobTransport(cast(Any, job), {}, cast(Any, loop))
    owner.start()
    assert owner._watcher is not None
    await asyncio.gather(owner._watcher, return_exceptions=True)
    assert job.close_calls == 0, "registration uncertainty released a borrowed handle"
    error = owner._finished.exception()
    assert error is not None and "ownership retained" in str(error)


async def test_startup_abort_without_watcher_respects_unknown_registration() -> None:
    root = _NativeWait()
    job = _Job(root)
    owner = _JobTransport(cast(Any, job), {}, cast(Any, _Loop(root)))
    owner._registration_attempted = True
    with pytest.raises(OSError, match="ownership retained"):
        await owner.abort_startup()
    assert job.close_calls == 0
