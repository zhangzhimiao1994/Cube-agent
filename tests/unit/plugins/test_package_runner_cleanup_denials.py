"""RAM lifecycle failures; no child, launcher, environment or network is started."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub import subprocess_cleanup
from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import (
    PluginInvocationContext,
    PluginPackageExecutionTarget,
    PythonSubprocessPluginPackageRunner,
)


class _Input:
    def __init__(self, entered: asyncio.Event) -> None:
        self.entered = entered

    def write(self, payload: bytes) -> None:
        assert payload

    async def drain(self) -> None:
        self.entered.set()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _Process:
    def __init__(self, interface: str, action: str) -> None:
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()
        self.kills = 0
        self.waits = 0
        self.waits_after_kill = 0
        self.reaps_after_kill = 0
        self.returncode: int | None = None
        self.error = OSError("controlled unknown process exit")
        self.remaining_errors = -1 if action == "wait_error" else int(action == "wait_once")
        self.stdin = _Input(self.entered) if interface == "invoke" else None
        self.stdout = asyncio.StreamReader() if interface == "invoke" else None
        self.stderr = None
        if self.stdout is not None and action.startswith("wait"):
            self.stdout.feed_data(b'{"ok":true}')
            self.stdout.feed_eof()

    async def wait(self) -> int:
        self.entered.set()
        self.waits += 1
        if self.kills:
            self.waits_after_kill += 1
        if self.remaining_errors:
            if self.remaining_errors > 0:
                self.remaining_errors -= 1
            raise self.error
        await self.exited.wait()
        assert self.returncode is not None
        if self.kills:
            self.reaps_after_kill += 1
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = 1
        self.exited.set()
        if self.stdout is not None:
            self.stdout.feed_eof()


@pytest.mark.parametrize("interface", ["preflight", "invoke"])
@pytest.mark.parametrize("action", ["cancel", "cancel_twice", "timeout", "wait_error", "wait_once"])
async def test_package_runner_failure_keeps_cleanup_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str, action: str
) -> None:
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text("never executed", encoding="ascii")
    target = PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint)
    process = _Process(interface, action)
    spawn_calls = 0

    async def factory(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawn_calls
        spawn_calls += 1
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()
    original_drain = subprocess_cleanup.drain_and_reap_process

    async def gated_cleanup(child: asyncio.subprocess.Process, **kwargs: Any) -> None:
        cleanup_entered.set()
        await release_cleanup.wait()
        await original_drain(child, **kwargs)

    if action == "cancel_twice":
        monkeypatch.setattr(subprocess_cleanup, "drain_and_reap_process", gated_cleanup)
    runner = PythonSubprocessPluginPackageRunner(timeout_seconds=0.02 if action == "timeout" else 3)
    before = asyncio.all_tasks()
    task: asyncio.Task[Any]
    if interface == "preflight":
        task = asyncio.create_task(runner.preflight(target=target))
    else:
        identity = UUID("77777777-7777-4777-8777-777777777777")
        capability = PluginCapabilityRequest(
            id="demo.run",
            adapter="test_package",
            permission_class="test.execute",
            sandbox_profile="local_process",
        )
        plugin = PluginResourceResponse(
            id="package-negative",
            name="Package negative",
            status="running",
            health="healthy",
            capabilities=[capability],
        )
        context = PluginInvocationContext(
            tenant_id=identity,
            user_id=identity,
            run_id=identity,
            actor="tester",
            idempotency_key="package-cleanup-test",
        )
        task = asyncio.create_task(
            runner.invoke(
                target=target,
                plugin=plugin,
                capability=capability,
                arguments={},
                context=context,
            )
        )
    observer: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(process.entered.wait(), 1)
        if action.startswith("cancel"):
            assert task.cancel()
            if action == "cancel_twice":
                observer = asyncio.create_task(cleanup_entered.wait())
                done, _ = await asyncio.wait({task, observer}, timeout=1)
                assert observer in done, "cancellation escaped owned package cleanup"
                assert not task.done() and not release_cleanup.is_set()
                assert task.cancel(), "second cancellation missed active cleanup"
                release_cleanup.set()
        finished, _ = await asyncio.wait({task}, timeout=3)
        assert task in finished, "package failure cleanup exceeded its bounded deadline"
        if action.startswith("cancel"):
            with pytest.raises(asyncio.CancelledError):
                task.result()
        elif action == "timeout":
            message = (
                "Plugin package preflight timed out"
                if interface == "preflight"
                else "Plugin tool timed out"
            )
            with pytest.raises(RuntimeCapabilityError, match="^" + message + "$"):
                task.result()
        elif action == "wait_once":
            with pytest.raises(OSError) as raised:
                task.result()
            assert raised.value is process.error, "successful reap replaced the original wait error"
        else:
            with pytest.raises(
                RuntimeCapabilityError, match="^Plugin process cleanup incomplete$"
            ) as raised_incomplete:
                task.result()
            cause = raised_incomplete.value.__cause__
            assert isinstance(cause, subprocess_cleanup.ProcessCleanupIncomplete)
            assert cause.__cause__ is process.error
        assert spawn_calls == 1
        assert process.kills > 0, "package error returned without terminating the owned child"
        assert process.waits_after_kill > 0, "package error skipped the post-kill reap attempt"
        if action == "wait_error":
            assert process.reaps_after_kill == 0, "unknown exit was counted as a successful reap"
        else:
            assert process.reaps_after_kill > 0, "package cleanup killed but never reaped"
        assert process.exited.is_set()
        assert not (asyncio.all_tasks() - before), "package cleanup left asynchronous tasks"
    finally:
        release_cleanup.set()
        if observer is not None:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        if not task.done():
            task.cancel()
        process.kill()
        finished, _ = await asyncio.wait({task}, timeout=3)
        assert task in finished, "RAM package fixture rescue did not settle the task"
        await asyncio.gather(task, return_exceptions=True)
