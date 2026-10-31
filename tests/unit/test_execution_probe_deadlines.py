"""Probe protocol deadlines and shared discovery/admission; no host runner or network."""
from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Self, cast

import pytest

from agent_hub import execution_backends as backends
from agent_hub.skills.sandbox import broker as protocol
from agent_hub.skills.sandbox.base import SkillInvocation, SkillResult


@pytest.fixture(autouse=True)
def clean_cache() -> Any:
    backends.clear_execution_backend_probe_cache()
    yield
    backends.clear_execution_backend_probe_cache()


class ReplySocket:
    def __init__(self, delay: float, fragments: bool = False) -> None:
        payload = protocol.BrokerResponse(ok=True).model_dump_json().encode()
        self.reply = struct.pack('!I', len(payload)) + payload
        self.now = 0.0
        self.delay = delay
        self.fragments = fragments
        self.timeout = 0.0
        self.timeouts: list[float] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds
        self.timeouts.append(seconds)

    def connect(self, path: str) -> None:
        pass

    def sendall(self, payload: bytes) -> None:
        request = protocol.BrokerRequest.model_validate_json(payload[4:], strict=True)
        assert request.action == 'probe'

    def recv(self, size: int) -> bytes:
        delay = self.delay if self.fragments or self.now == 0 else 0.0
        if delay > self.timeout:
            self.now += self.timeout
            raise TimeoutError
        self.now += delay
        size = min(size, 1) if self.fragments else size
        data, self.reply = self.reply[:size], self.reply[size:]
        return data


def socket_reply(monkeypatch: pytest.MonkeyPatch, reply: ReplySocket) -> None:
    original = protocol._module_attribute
    monkeypatch.setattr(protocol, '_module_attribute',
                        lambda module, name: 1 if name == 'AF_UNIX' else original(module, name))
    monkeypatch.setattr('agent_hub.skills.sandbox.broker.socket.socket', lambda *args: reply)
    # Replacing the module's clock would also replace threading's real clock.
    class Clock:
        @staticmethod
        def monotonic() -> float:
            return reply.now
    monkeypatch.setattr(protocol, 'time', Clock, raising=False)


def test_valid_reply_after_seven_seconds_fits_actual_runner_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = ReplySocket(7)
    socket_reply(monkeypatch, reply)
    assert protocol.probe_skill_broker('docker') is None
    assert max(reply.timeouts) < 40


def test_fragmented_reply_cannot_reset_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = ReplySocket(2, fragments=True)
    socket_reply(monkeypatch, reply)
    assert protocol.probe_skill_broker('docker') == 'docker_broker_probe_timeout'
    assert reply.now < 40


def test_pending_discovery_is_shared_with_selected_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    entered = {name: threading.Event() for name in ('systemd', 'docker')}
    calls: list[str] = []
    def probe(backend: str) -> None:
        calls.append(backend)
        entered[backend].set()
        assert gate.wait(2)
    monkeypatch.setattr(backends, 'probe_skill_broker', probe)
    monkeypatch.setattr(backends, 'remote_backend_unavailable_reason', lambda name: name+'_not_configured')
    monkeypatch.setattr(backends, '_DISCOVERY_WAIT_SECONDS', 0.03, raising=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            listing = pool.submit(backends.probe_execution_backends)
            assert entered['systemd'].wait(0.5)
            assert entered['docker'].wait(0.5), 'another backend is blocked by global probe lock'
            statuses = {item.id: item for item in listing.result(timeout=0.5)}
            assert not statuses['docker'].available
            assert statuses['docker'].reason == 'docker_probe_pending'
            selected = pool.submit(backends.execution_backend_unavailable_reason, 'docker')
            assert not selected.done(), 'selected admission must wait, not reject discovery pending'
            gate.set()
            assert selected.result(timeout=0.5) is None
            assert backends.execution_backend_unavailable_reason('docker') is None
            assert sorted(calls) == ['docker', 'systemd']
        finally:
            gate.set()


@pytest.mark.parametrize('reason', ['docker_broker_probe_timeout', 'docker_probe_failed'])
def test_transient_result_has_retry_ttl_not_hard_failure_ttl(
    monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    replies = iter([reason, None])
    monkeypatch.setattr(backends, 'probe_skill_broker', lambda backend: next(replies))
    monkeypatch.setattr(backends, '_PROBE_TRANSIENT_TTL_SECONDS', 0.0, raising=False)
    assert backends.execution_backend_unavailable_reason('docker') == reason
    assert backends.execution_backend_unavailable_reason('docker') is None


def test_known_hard_unavailable_remains_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    def probe(backend: str) -> str:
        calls.append(backend)
        return 'docker_runner_image_not_found'
    monkeypatch.setattr(backends, 'probe_skill_broker', probe)
    assert backends.execution_backend_unavailable_reason('docker') == 'docker_runner_image_not_found'
    assert backends.execution_backend_unavailable_reason('docker') == 'docker_runner_image_not_found'
    assert calls == ['docker']


async def test_root_probe_deadline_includes_startup_and_waits_for_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cleaned = False
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    async def blocked() -> protocol.BrokerResponse:
        nonlocal cleaned
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            cleaned = True
        raise AssertionError('blocked probe unexpectedly resumed')
    monkeypatch.setattr(broker, '_probe_docker', blocked)
    monkeypatch.setattr(protocol, '_PROBE_WORK_SECONDS', 0.02, raising=False)
    async with asyncio.timeout(0.3):
        result = await broker.handle(protocol.BrokerRequest.probe(backend='docker'), peer_uid=10001)
    assert result.error == 'docker_broker_probe_timeout'
    assert cleaned


async def test_systemd_startup_is_inside_probe_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def never_started(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', never_started)
    monkeypatch.setattr(protocol, '_PROBE_WORK_SECONDS', 0.02, raising=False)
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    cleaned: list[tuple[str, str]] = []
    async def terminate(execution_id: str, backend: str) -> None:
        cleaned.append((execution_id, backend))
    monkeypatch.setattr(broker, '_terminate', terminate)
    async with asyncio.timeout(0.3):
        result = await broker.handle(protocol.BrokerRequest.probe(), peer_uid=10001)
    assert result.error == 'systemd_broker_probe_timeout'
    assert len(cleaned) == 1 and cleaned[0][0].startswith('probe-')
    assert cleaned[0][1] == 'systemd'


async def test_docker_probe_execution_guard_is_still_ten_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    monkeypatch.setattr('agent_hub.skills.sandbox.broker.os.chown', lambda *args: None, raising=False)
    async def run(invocation: SkillInvocation, backend: str) -> SkillResult:
        assert invocation.timeout_seconds == 10
        assert backend == 'docker'
        return SkillResult(exit_code=0, stdout='{"docker_probe":"ok"}', stderr='', timed_out=False)
    monkeypatch.setattr(broker, '_run', run)
    assert (await broker.handle(protocol.BrokerRequest.probe(backend='docker'), peer_uid=10001)).ok


def test_invalidation_cannot_reuse_old_success_or_duplicate_running_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls = 0
    def probe(backend: str) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(2)
            return None
        return 'docker_runner_image_not_found'
    monkeypatch.setattr(backends, 'probe_skill_broker', probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(backends.execution_backend_unavailable_reason, 'docker')
            assert started.wait(0.5)
            backends.clear_execution_backend_probe_cache()
            second = pool.submit(backends.execution_backend_unavailable_reason, 'docker')
            assert calls == 1
            release.set()
            assert first.result(timeout=0.5) == 'docker_runner_image_not_found'
            assert second.result(timeout=0.5) == 'docker_runner_image_not_found'
            assert calls == 2
        finally:
            release.set()


def test_selected_wait_is_bounded_and_pending_does_not_poison_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    calls = 0
    def probe(backend: str) -> None:
        nonlocal calls
        calls += 1
        assert release.wait(2)
    monkeypatch.setattr(backends, 'probe_skill_broker', probe)
    try:
        assert backends.execution_backend_unavailable_reason('docker', timeout_seconds=0.01) == 'docker_probe_pending'
        release.set()
        assert backends.execution_backend_unavailable_reason('docker') is None
        assert calls == 1
    finally:
        release.set()


async def test_systemd_timeout_reclaims_the_owned_probe_unit_and_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class Process:
        returncode: int | None = None
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b'', b''
    process = Process()
    async def spawn(*args: object, **kwargs: object) -> Process:
        assert 'RuntimeMaxSec=5s' in args
        return process
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    terminated: list[str] = []
    reaped: list[Process] = []
    async def terminate(execution_id: str, backend: str) -> None:
        owned: object = broker._processes[execution_id]
        assert owned is process
        assert backend == 'systemd'
        assert execution_id.startswith('probe-')
        terminated.append(execution_id)
        process.returncode = -9
    async def reap(owned: Process) -> None:
        reaped.append(owned)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(broker, '_terminate', terminate)
    monkeypatch.setattr(protocol, 'drain_and_reap_process', reap)
    monkeypatch.setattr(protocol, '_PROBE_WORK_SECONDS', 0.02)
    response = await broker.handle(protocol.BrokerRequest.probe(), peer_uid=10001)
    assert response.error == 'systemd_broker_probe_timeout'
    assert len(terminated) == 1 and reaped == [process]
    assert not broker._processes


@pytest.mark.parametrize('action', ['probe', 'run', 'terminate'])
async def test_async_client_uses_probe_protocol_budget_without_extending_action_budgets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    reader = asyncio.StreamReader()
    payload = protocol.BrokerResponse(ok=True).model_dump_json().encode()
    reader.feed_data(struct.pack('!I', len(payload)) + payload)
    reader.feed_eof()
    class Writer:
        closed = False
        def write(self, payload: bytes) -> None:
            assert protocol.BrokerRequest.model_validate_json(payload[4:]).action == action
        async def drain(self) -> None:
            pass
        def close(self) -> None:
            self.closed = True
        async def wait_closed(self) -> None:
            assert self.closed
    writer = Writer()
    async def connect(path: str) -> tuple[asyncio.StreamReader, Writer]:
        return reader, writer
    original_attribute = protocol._module_attribute
    monkeypatch.setattr(protocol, '_module_attribute', lambda module, name:
                        connect if name == 'open_unix_connection' else original_attribute(module, name))
    limits: list[float | None] = []
    original_timeout = asyncio.timeout
    def timeout(seconds: float | None) -> asyncio.Timeout:
        limits.append(seconds)
        return original_timeout(seconds)
    monkeypatch.setattr(asyncio, 'timeout', timeout)
    if action == 'probe':
        request = protocol.BrokerRequest.probe()
    elif action == 'terminate':
        request = protocol.BrokerRequest.terminate('probe_budget')
    else:
        request = protocol.BrokerRequest.run(SkillInvocation(
            execution_id='probe_budget', package_path=tmp_path/'skill.zip', package_sha256='a'*64,
            input={}, timeout_seconds=3, output_limit_bytes=4096, memory_limit_bytes=64*1024*1024,
            cpu_quota_percent=25, sandbox_profile='read_only', writable_tmp_path=tmp_path,
        ))
    assert (await protocol.request_skill_broker(request)).ok
    assert writer.closed
    if action == 'probe':
        # Cover server startup+work+cleanup, without making GET wait this long.
        assert limits[0] is not None and 25 < limits[0] < 30
    else:
        assert limits == [13 if action == 'run' else 10]


async def test_repeated_cancellation_waits_for_systemd_probe_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Process:
        returncode: int | None = None
        async def communicate(self) -> tuple[bytes, bytes]:
            running.set()
            await asyncio.Event().wait()
            return b'', b''
    process = Process()
    async def spawn(*args: object, **kwargs: object) -> Process:
        return process
    async def terminate(execution_id: str, backend: str) -> None:
        cleaning.set()
        await release.wait()
        process.returncode = -9
    reaped: list[Process] = []
    async def reap(owned: Process) -> None:
        reaped.append(owned)
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(broker, '_terminate', terminate)
    monkeypatch.setattr(protocol, 'drain_and_reap_process', reap)
    task = asyncio.create_task(broker.handle(protocol.BrokerRequest.probe(), peer_uid=10001))
    await running.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reaped == [process] and not broker._processes


async def test_docker_startup_deadline_still_reclaims_named_probe_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    monkeypatch.setattr('agent_hub.skills.sandbox.broker.os.chown', lambda *args: None, raising=False)
    monkeypatch.setattr(protocol, '_PROBE_WORK_SECONDS', 0.02)
    execution_ids: list[str] = []
    cleaned: list[tuple[str, str]] = []
    async def starting(invocation: SkillInvocation, backend: str) -> SkillResult:
        execution_ids.append(invocation.execution_id)
        await asyncio.Event().wait()
        raise AssertionError('launcher startup unexpectedly resumed')
    async def terminate(execution_id: str, backend: str) -> None:
        assert list(tmp_path.iterdir()), 'cleanup must precede temporary workspace removal'
        cleaned.append((execution_id, backend))
    monkeypatch.setattr(broker, '_run', starting)
    monkeypatch.setattr(broker, '_terminate', terminate)
    response = await broker.handle(protocol.BrokerRequest.probe(backend='docker'), peer_uid=10001)
    assert response.error == 'docker_broker_probe_timeout'
    assert cleaned == [(execution_ids[0], 'docker')]
    assert not list(tmp_path.iterdir())


async def test_started_docker_probe_keeps_original_cleanup_without_second_termination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    broker = protocol.SkillBroker(protocol.BrokerPolicy(runtime_root=tmp_path))
    monkeypatch.setattr('agent_hub.skills.sandbox.broker.os.chown', lambda *args: None, raising=False)
    monkeypatch.setattr(protocol, '_PROBE_WORK_SECONDS', 0.02)
    owned = cast(asyncio.subprocess.Process, object())
    terminated: list[str] = []
    async def terminate(execution_id: str, backend: str) -> None:
        assert backend == 'docker'
        assert broker._processes[execution_id] is owned
        terminated.append(execution_id)
    async def run_limited(
        command: tuple[str, ...], invocation: SkillInvocation, *,
        process_started: Callable[[asyncio.subprocess.Process], None],
        on_forced_terminate: Callable[[], Awaitable[None]],
    ) -> SkillResult:
        assert invocation.timeout_seconds == 10
        assert command[1:3] == ('run', '--rm')
        process_started(owned)
        try:
            await asyncio.Event().wait()
        finally:
            await on_forced_terminate()
        raise AssertionError('runner unexpectedly resumed')
    monkeypatch.setattr(protocol, 'run_subprocess_with_limits', run_limited)
    monkeypatch.setattr(broker, '_terminate', terminate)
    response = await broker.handle(protocol.BrokerRequest.probe(backend='docker'), peer_uid=10001)
    assert response.error == 'docker_broker_probe_timeout'
    assert len(terminated) == 1 and not broker._processes
    assert not list(tmp_path.iterdir())
