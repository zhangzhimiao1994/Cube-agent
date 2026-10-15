from __future__ import annotations

import asyncio
import ctypes
import inspect
import sys
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub import subprocess_cleanup
from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import (
    LocalCommandPluginAdapter,
    PluginInvocationContext,
    _probe_local_runtime,
)
from agent_hub.windows_managed_subprocess import create_subprocess_exec


def _plugin(
    source: str, *, output_budget: int = 128, timeout_seconds: float = 10
) -> PluginResourceResponse:
    executable = str(Path(str(getattr(sys, "_base_executable", sys.executable))).resolve())
    return PluginResourceResponse(
        id="managed-cli",
        name="Managed CLI",
        status="running",
        health="healthy",
        timeout_seconds=timeout_seconds,
        resource_config={
            "command": executable,
            "base_args": ("-I", "-u", "-c", source),
            "max_output_bytes": output_budget,
        },
        capabilities=[
            PluginCapabilityRequest(
                id="demo.echo",
                adapter="local_command",
                permission_class="test.exec",
                sandbox_profile="local_process",
            )
        ],
    )


def _context() -> PluginInvocationContext:
    identity = UUID("11111111-1111-4111-8111-111111111111")
    return PluginInvocationContext(
        tenant_id=identity,
        user_id=identity,
        run_id=identity,
        actor="tester",
        idempotency_key="managed-cli-test",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows managed adapter")
async def test_adapter_invokes_real_managed_stdio_factory() -> None:
    assert "process_factory" in inspect.signature(LocalCommandPluginAdapter).parameters
    plugin = _plugin("import sys; print('echo:'+sys.argv[1]); sys.stderr.write('warning')")
    adapter = LocalCommandPluginAdapter(process_factory=create_subprocess_exec)
    await adapter.preflight(plugin=plugin)
    result = await adapter.invoke(
        plugin=plugin,
        capability=plugin.capabilities[0],
        arguments={"args": ("payload",)},
        context=_context(),
    )
    assert result["ok"] is True and result["exit_code"] == 0
    assert result["stdout"] == "echo:payload\r\n"
    assert result["stderr"] == "warning"


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows managed adapter")
async def test_adapter_retains_aggregate_output_budget() -> None:
    assert "process_factory" in inspect.signature(LocalCommandPluginAdapter).parameters
    plugin = _plugin("import os; os.write(1,b'o'*33); os.write(2,b'e'*32)", output_budget=64)
    adapter = LocalCommandPluginAdapter(process_factory=create_subprocess_exec)
    with pytest.raises(RuntimeCapabilityError, match="^Plugin result is invalid$"):
        await adapter.invoke(
            plugin=plugin,
            capability=plugin.capabilities[0],
            arguments={},
            context=_context(),
        )


async def test_injected_factory_failure_has_no_host_spawn_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "process_factory" in inspect.signature(LocalCommandPluginAdapter).parameters
    calls = 0

    async def failed(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        raise OSError("controlled factory failure")

    async def forbidden(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        raise AssertionError("unmanaged fallback executed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    plugin = _plugin("pass")
    adapter = LocalCommandPluginAdapter(process_factory=failed)
    with pytest.raises(RuntimeCapabilityError, match="^Plugin command unavailable$"):
        await adapter.invoke(
            plugin=plugin,
            capability=plugin.capabilities[0],
            arguments={},
            context=_context(),
        )
    assert calls == 1


_DEADLINE = """
import os, threading, time
threading.Thread(target=lambda: (time.sleep(15), os._exit(124)), daemon=True).start()
"""

_GRANDCHILD = (
    _DEADLINE
    + """
import pathlib, sys
ready, release = map(pathlib.Path, sys.argv[1:])
temporary = ready.with_suffix('.tmp')
temporary.write_text(str(os.getpid()), encoding='ascii')
temporary.replace(ready)
while not release.exists():
    time.sleep(0.01)
"""
)

_FAMILY = (
    _DEADLINE
    + """
import pathlib, subprocess, sys
source, root_ready, child_ready, release, trigger = sys.argv[1:]
subprocess.Popen([sys.executable, '-I', '-u', '-c', source, child_ready, release],
                 stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                 creationflags=subprocess.CREATE_NO_WINDOW)
ready = pathlib.Path(root_ready)
temporary = ready.with_suffix('.tmp')
temporary.write_text(str(os.getpid()), encoding='ascii')
temporary.replace(ready)
while not pathlib.Path(release).exists():
    if pathlib.Path(trigger).exists():
        os.write(1, b'x' * 65)
        pathlib.Path(trigger).unlink()
    time.sleep(0.01)
"""
)


def _monitor(pid: int) -> tuple[Any, int]:
    if sys.platform != "win32":
        raise RuntimeError("Windows monitor unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, int(handle)


async def _pid(path: Path, task: asyncio.Task[Any]) -> int:
    async with asyncio.timeout(3):
        while True:
            try:
                return int(path.read_text(encoding="ascii"))
            except (FileNotFoundError, PermissionError):
                pass
            if task.done():
                task.result()
                raise AssertionError("controlled family exited before readiness")
            await asyncio.sleep(0.01)


def _rescue_monitors(monitors: list[tuple[Any, int]]) -> None:
    errors: list[Exception] = []
    for kernel, handle in monitors:
        try:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                assert kernel.TerminateProcess(handle, 1)
            assert kernel.WaitForSingleObject(handle, 3000) == 0
        except Exception as error:  # noqa: BLE001 - every monitor is independently owned
            errors.append(error)
        try:
            assert kernel.CloseHandle(handle)
        except Exception as error:  # noqa: BLE001 - attempt remaining monitors after a failure
            errors.append(error)
    if errors:
        raise ExceptionGroup("controlled native monitor rescue incomplete", errors)


def test_monitor_rescue_failure_still_closes_other_owned_handles() -> None:
    calls: list[int] = []

    class Kernel:
        def WaitForSingleObject(self, handle: int, timeout: int) -> int:
            return 258 if handle == 1 else 0

        def TerminateProcess(self, handle: int, code: int) -> bool:
            return False

        def CloseHandle(self, handle: int) -> bool:
            calls.append(handle)
            return handle != 1

    kernel = Kernel()
    with pytest.raises(ExceptionGroup):
        _rescue_monitors([(kernel, 1), (kernel, 2)])
    assert calls == [1, 2]


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows managed CLI family")
@pytest.mark.parametrize(
    "action", ["timeout", "cancel_twice", "output_limit", "probe_timeout", "probe_cancel_twice"]
)
async def test_real_adapter_failure_reaps_family_with_inherited_pipes(
    tmp_path: Path, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_ready, child_ready = tmp_path / "root.pid", tmp_path / "child.pid"
    release, trigger = tmp_path / "release", tmp_path / "trigger"
    plugin = _plugin(
        "exec(" + repr(_FAMILY) + ")",
        output_budget=64,
        timeout_seconds=2 if action.endswith("timeout") else 10,
    )
    adapter = LocalCommandPluginAdapter(process_factory=create_subprocess_exec)
    before = asyncio.all_tasks()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    original_drain = subprocess_cleanup.drain_and_reap_process

    async def gated_drain(process: asyncio.subprocess.Process, **kwargs: Any) -> None:
        cleanup_entered.set()
        await cleanup_release.wait()
        await original_drain(process, **kwargs)

    if action.endswith("cancel_twice"):
        monkeypatch.setattr(subprocess_cleanup, "drain_and_reap_process", gated_drain)
    family_args = (
        "exec(" + repr(_GRANDCHILD) + ")",
        str(root_ready),
        str(child_ready),
        str(release),
        str(trigger),
    )

    async def probe_factory(
        program: str, *args: str, env: Mapping[str, str], stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        assert args == ("info", "--format", "{{.ServerVersion}}")
        return await create_subprocess_exec(
            program,
            "-I",
            "-u",
            "-c",
            "exec(" + repr(_FAMILY) + ")",
            *family_args,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    task: asyncio.Task[Any]
    if action.startswith("probe_"):
        task = asyncio.create_task(
            _probe_local_runtime(
                str(plugin.resource_config["command"]),
                plugin.timeout_seconds,
                {},
                process_factory=probe_factory,
            )
        )
    else:
        task = asyncio.create_task(
            adapter.invoke(
                plugin=plugin,
                capability=plugin.capabilities[0],
                arguments={"args": family_args},
                context=_context(),
            )
        )
    monitors: list[tuple[Any, int]] = []
    try:
        monitors.append(_monitor(await _pid(root_ready, task)))
        monitors.append(_monitor(await _pid(child_ready, task)))
        assert all(kernel.WaitForSingleObject(handle, 0) == 258 for kernel, handle in monitors)
        if action.endswith("cancel_twice"):
            assert task.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), 3)
            assert not task.done() and not cleanup_release.is_set()
            assert task.cancel(), "second cancellation missed active cleanup"
            cleanup_release.set()
            expected: type[BaseException] = asyncio.CancelledError
            message: str | None = None
        else:
            if action == "output_limit":
                trigger.touch()
            expected = RuntimeCapabilityError
            message = "Plugin tool timed out" if action == "timeout" else "Plugin result is invalid"
        async with asyncio.timeout(5):
            if action == "probe_timeout":
                assert await task is False
            else:
                with pytest.raises(expected, match=message):
                    await task
            for kernel, handle in monitors:
                while kernel.WaitForSingleObject(handle, 0) == 258:
                    await asyncio.sleep(0.01)
                assert kernel.WaitForSingleObject(handle, 0) == 0
                code = wintypes.DWORD()
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                assert code.value != 124, "cleanup relied on the fixture self-expiry timer"
        assert not release.exists(), "cleanup relied on releasing controlled children"
        assert not (asyncio.all_tasks() - before), "adapter left owned asynchronous tasks"
    finally:
        cleanup_release.set()
        release.touch()
        if not task.done():
            task.cancel()
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(task, return_exceptions=True)
        finally:
            _rescue_monitors(monitors)


def test_injected_runner_and_process_factory_are_mutually_exclusive() -> None:
    assert "process_factory" in inspect.signature(LocalCommandPluginAdapter).parameters

    async def runner(*args: object, **kwargs: object) -> tuple[int, str, str]:
        return 0, "", ""

    with pytest.raises(ValueError):
        LocalCommandPluginAdapter(command_runner=runner, process_factory=create_subprocess_exec)


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows managed probe")
async def test_runtime_probe_uses_injected_managed_factory() -> None:
    assert "process_factory" in inspect.signature(_probe_local_runtime).parameters
    calls = 0

    async def probe_factory(
        program: str,
        *args: str,
        env: Mapping[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        assert args == ("info", "--format", "{{.ServerVersion}}")
        assert stdin == stdout == stderr == asyncio.subprocess.DEVNULL
        return await create_subprocess_exec(
            program,
            "-I",
            "-u",
            "-c",
            "pass",
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    executable = str(Path(str(getattr(sys, "_base_executable", sys.executable))).resolve())
    assert await _probe_local_runtime(executable, 3, {}, process_factory=probe_factory) is True
    assert calls == 1
