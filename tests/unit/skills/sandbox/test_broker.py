from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_hub.skills.sandbox.base import SkillInvocation
from agent_hub.skills.sandbox.broker import (
    BrokerBackend,
    BrokerPolicy,
    BrokerRequest,
    BrokerRequestError,
    BrokerResponse,
    SkillBroker,
    build_broker_terminate_command,
)


@dataclass
class LocalHelpers:
    code: str = "import time; time.sleep(60)"
    commands: list[tuple[str, ...]] = field(default_factory=list)
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)
    reaped: list[int] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    wait_error: Exception | None = None
    block_reap: bool = False
    reaping: asyncio.Event = field(default_factory=asyncio.Event)
    release_reap: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
async def helpers(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LocalHelpers]:
    helpers = LocalHelpers()
    create_process = asyncio.create_subprocess_exec

    async def create(*command: str, **kwargs: Any) -> asyncio.subprocess.Process:
        if command[0] == sys.executable:
            return await create_process(*command, **kwargs)
        helpers.commands.append(command)
        process = await create_process(sys.executable, "-B", "-c", helpers.code, **kwargs)
        helpers.processes.append(process)
        wait = process.wait
        calls = 0

        async def observed_wait() -> int:
            nonlocal calls
            calls += 1
            if helpers.wait_error is not None:
                error, helpers.wait_error = helpers.wait_error, None
                raise error
            if calls > 1 and helpers.block_reap:
                helpers.reaping.set()
                await helpers.release_reap.wait()
            result = await wait()
            helpers.reaped.append(process.pid)
            return result

        monkeypatch.setattr(process, "wait", observed_wait)
        helpers.started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        yield helpers
    finally:
        helpers.release_reap.set()
        for process in helpers.processes:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=2)


def test_systemd_termination_uses_sigkill_for_entire_unit() -> None:
    assert build_broker_terminate_command("exec_1", BrokerPolicy()) == (
        "/usr/bin/systemctl",
        "kill",
        "--kill-who=all",
        "--signal=SIGKILL",
        "agent-hub-skill-exec_1.service",
    )


@pytest.mark.parametrize("backend", ["systemd", "docker"])
@pytest.mark.parametrize("exit_code", [1, 5])
async def test_terminate_nonzero_is_not_success_or_inferred_absence(
    helpers: LocalHelpers, backend: BrokerBackend, exit_code: int
) -> None:
    helpers.code = (
        "import sys; print('Unit not loaded: sensitive backend detail', file=sys.stderr); "
        f"sys.exit({exit_code})"
    )
    broker = SkillBroker(BrokerPolicy())

    response = await broker.handle(
        BrokerRequest.terminate("exec_1", backend=backend), peer_uid=10001
    )

    assert response.ok is False
    assert response.result is None
    assert response.error is not None and "cleanup_incomplete" in response.error
    assert "sensitive" not in response.model_dump_json()
    assert helpers.reaped == [helpers.processes[0].pid]


@pytest.mark.parametrize("backend", ["systemd", "docker"])
async def test_direct_terminate_propagates_controlled_nonzero_failure(
    helpers: LocalHelpers, backend: BrokerBackend
) -> None:
    helpers.code = "raise SystemExit(2)"

    with pytest.raises(BrokerRequestError, match="cleanup_incomplete"):
        await SkillBroker(BrokerPolicy())._terminate("exec_1", backend)


@pytest.mark.parametrize("backend", ["systemd", "docker"])
async def test_terminate_timeout_kills_and_reaps_owned_helper(
    helpers: LocalHelpers, backend: BrokerBackend
) -> None:
    broker = SkillBroker(BrokerPolicy())
    start = asyncio.get_running_loop().time()

    (response,) = await asyncio.wait_for(
        asyncio.gather(
            broker.handle(BrokerRequest.terminate("exec_1", backend=backend), peer_uid=10001),
            return_exceptions=True,
        ),
        timeout=9,
    )

    assert helpers.reaped == [helpers.processes[0].pid]
    assert helpers.processes[0].returncode is not None
    assert isinstance(response, BrokerResponse)
    assert response.ok is False
    assert response.error is not None and "cleanup_incomplete" in response.error
    assert asyncio.get_running_loop().time() - start < 9


@pytest.mark.parametrize("backend", ["systemd", "docker"])
async def test_terminate_cancellation_reaps_helper_before_propagating(
    helpers: LocalHelpers, backend: BrokerBackend
) -> None:
    task = asyncio.create_task(SkillBroker(BrokerPolicy())._terminate("exec_1", backend))
    await asyncio.wait_for(helpers.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)

    assert helpers.reaped == [helpers.processes[0].pid]
    assert helpers.processes[0].returncode is not None


async def test_terminate_repeated_cancellation_does_not_interrupt_reaping(
    helpers: LocalHelpers,
) -> None:
    helpers.block_reap = True
    task = asyncio.create_task(SkillBroker(BrokerPolicy())._terminate("exec_1", "systemd"))
    try:
        await asyncio.wait_for(helpers.started.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(helpers.reaping.wait(), timeout=2)
        task.cancel()
        helpers.release_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert helpers.reaped == [helpers.processes[0].pid]
    finally:
        helpers.release_reap.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
async def test_terminate_wait_exception_reaps_helper_and_hides_raw_error(
    helpers: LocalHelpers,
    error_type: type[Exception],
) -> None:
    helpers.wait_error = error_type("sensitive helper wait error")

    response = await SkillBroker(BrokerPolicy()).handle(
        BrokerRequest.terminate("exec_1"), peer_uid=10001
    )

    assert helpers.reaped == [helpers.processes[0].pid]
    assert response.ok is False
    assert response.error is not None and "cleanup_incomplete" in response.error
    assert "sensitive" not in response.model_dump_json()


async def test_terminate_reap_failure_returns_bounded_cleanup_incomplete(
    helpers: LocalHelpers,
) -> None:
    helpers.wait_error = OSError("sensitive wait failure")
    helpers.block_reap = True
    start = asyncio.get_running_loop().time()

    response = await asyncio.wait_for(
        SkillBroker(BrokerPolicy()).handle(BrokerRequest.terminate("exec_1"), peer_uid=10001),
        timeout=3,
    )

    assert helpers.reaping.is_set()
    assert helpers.processes[0].returncode is not None
    assert response.ok is False
    assert response.error is not None and "cleanup_incomplete" in response.error
    assert "sensitive" not in response.model_dump_json()
    assert asyncio.get_running_loop().time() - start < 3


async def test_terminate_cancelled_reap_failure_preserves_cancellation(
    helpers: LocalHelpers,
) -> None:
    helpers.block_reap = True
    task = asyncio.create_task(SkillBroker(BrokerPolicy())._terminate("exec_1", "systemd"))
    await asyncio.wait_for(helpers.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=3)

    assert isinstance(caught.value.__cause__, BrokerRequestError)
    assert "cleanup_incomplete" in str(caught.value.__cause__)
    assert helpers.processes[0].returncode is not None


async def test_backend_failure_propagates_through_existing_run_cleanup(
    helpers: LocalHelpers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helpers.code = "raise SystemExit(1)"
    launcher_command = (
        sys.executable,
        "-B",
        "-c",
        "import sys,time; print('x' * 128, flush=True); time.sleep(60)",
    )
    monkeypatch.setattr(
        "agent_hub.skills.sandbox.broker.build_broker_systemd_command",
        lambda *_args: launcher_command,
    )
    invocation = SkillInvocation(
        execution_id="exec_1",
        package_path=tmp_path / "package.zip",
        package_sha256="a" * 64,
        timeout_seconds=3,
        output_limit_bytes=64,
        memory_limit_bytes=64 * 1024 * 1024,
        cpu_quota_percent=25,
        writable_tmp_path=tmp_path,
    )
    broker = SkillBroker(BrokerPolicy())
    task = asyncio.create_task(broker._run(invocation, "systemd"))
    launcher: asyncio.subprocess.Process | None = None
    try:
        await asyncio.wait_for(helpers.started.wait(), timeout=2)
        launcher = broker._processes["exec_1"]
        with pytest.raises(BrokerRequestError, match="cleanup_incomplete"):
            await asyncio.wait_for(task, timeout=8)
        assert helpers.reaped == [helpers.processes[0].pid]
        assert launcher.returncode is not None
        assert not broker._processes
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if launcher is not None:
            if launcher.returncode is None:
                launcher.kill()
            await asyncio.wait_for(launcher.wait(), timeout=2)


async def test_terminate_spawn_exception_is_controlled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        raise OSError("sensitive executable location")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)

    response = await SkillBroker(BrokerPolicy()).handle(
        BrokerRequest.terminate("exec_1"), peer_uid=10001
    )

    assert response.ok is False
    assert response.error is not None and "cleanup_incomplete" in response.error
    assert "sensitive" not in response.model_dump_json()


@pytest.mark.parametrize("backend", ["systemd", "docker"])
async def test_terminate_success_preserves_v1_response_and_backend_target(
    helpers: LocalHelpers, backend: BrokerBackend
) -> None:
    helpers.code = "pass"
    policy = BrokerPolicy(systemctl="local-systemctl", docker="local-docker")
    request = BrokerRequest.terminate("exec_1", backend=backend)

    response = await SkillBroker(policy).handle(request, peer_uid=policy.allowed_uid)

    assert request.version == 1
    assert response.model_dump() == {"ok": True, "result": None, "error": None}
    expected = (
        ("local-docker", "kill", "agent-hub-skill-exec_1")
        if backend == "docker"
        else (
            "local-systemctl",
            "kill",
            "--kill-who=all",
            "--signal=SIGKILL",
            "agent-hub-skill-exec_1.service",
        )
    )
    assert helpers.commands == [expected]
    assert helpers.reaped == [helpers.processes[0].pid]


async def test_terminate_still_requires_authorized_caller(helpers: LocalHelpers) -> None:
    response = await SkillBroker(BrokerPolicy()).handle(
        BrokerRequest.terminate("exec_1"), peer_uid=10002
    )

    assert response.ok is False
    assert helpers.commands == []
