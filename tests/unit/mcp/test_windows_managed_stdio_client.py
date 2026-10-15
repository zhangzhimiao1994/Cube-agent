from __future__ import annotations

import asyncio
import ctypes
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.mcp import client as mcp_client
from agent_hub.mcp.client import StdioMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind
from agent_hub.subprocess_cleanup import drain_and_reap_process
from agent_hub.windows_managed_subprocess import create_subprocess_exec

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native managed MCP stdio")


async def test_real_managed_client_discovers_and_invokes_peer(tmp_path: Path) -> None:
    script = tmp_path / "managed_peer.py"
    script.write_text(
        r"""
import json, sys
while True:
    header = sys.stdin.buffer.readline()
    if not header: break
    length = int(header.decode().split(':',1)[1])
    assert sys.stdin.buffer.readline() == b'\r\n'
    request = json.loads(sys.stdin.buffer.read(length))
    method = request['method']
    if method == 'notifications/initialized': continue
    if method == 'tools/list':
        result = {'tools':[{'name':'echo','description':'Echo','inputSchema':{'type':'object'}}]}
    elif method == 'tools/call':
        result = {'content':[{'type':'text','text':request['params']['arguments']['text']}]}
    else: result = {}
    raw = json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode()+raw)
    sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )
    processes: list[asyncio.subprocess.Process] = []

    async def factory(
        program: str,
        *args: str,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        process = await create_subprocess_exec(
            program,
            *args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        processes.append(process)
        return process

    executable = str(Path(str(getattr(sys, "_base_executable", sys.executable))).resolve())
    server = McpServerDefinition(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        id="managed",
        transport=McpTransportKind.STDIO,
        command=executable,
        args=("-I", "-u", str(script)),
        executable_allowlist=(executable,),
    )
    before = asyncio.all_tasks()
    try:
        client = StdioMcpClient(process_factory=factory)
        assert await client.health(server) == "healthy"
        tools = await client.discover_tools(server)
        result = await client.invoke(server, "echo", {"text": "managed reply"})
        assert [tool.name for tool in tools] == ["echo"]
        assert result.content == {"content": [{"type": "text", "text": "managed reply"}]}
        assert len(processes) == 3
        assert all(process.returncode == 0 for process in processes)
        assert not (asyncio.all_tasks() - before)
    finally:
        for process in processes:
            with suppress(ProcessLookupError):
                process.kill()
            if process.stdin is not None:
                process.stdin.close()
            await asyncio.wait_for(process.communicate(), timeout=5)


def _monitor_kernel() -> Any:
    if sys.platform != "win32":
        raise OSError("Native Windows monitor unavailable")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
        "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
        "GetExitCodeProcess": ((wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        "TerminateProcess": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _monitor_error() -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError("Native Windows monitor unavailable")


class _NativeMonitor:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kernel = _monitor_kernel()
        handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not handle:
            raise _monitor_error()
        # Independent test-owned handle: rescue never acts on a borrowed Job handle or PID.
        self.handle = int(handle)

    def poll(self) -> int | None:
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise _monitor_error()
        code = ctypes.c_uint32()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise _monitor_error()
        return int(code.value)

    async def wait_exit(self) -> int:
        async with asyncio.timeout(3):
            while (code := self.poll()) is None:
                await asyncio.sleep(0.01)
        return code

    def terminate_if_running(self) -> None:
        try:
            alive = self.poll() is None
        except OSError:
            alive = True
        if alive and not self.kernel.TerminateProcess(self.handle, 1) and self.poll() is None:
            raise _monitor_error()

    def close(self) -> None:
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise _monitor_error()
            self.handle = 0


_FAMILY_PEER = r"""import json
import os
import pathlib
import subprocess
import sys
import threading
import time

threading.Thread(target=lambda: (time.sleep(15), os._exit(124)), daemon=True).start()
mode, role, directory = sys.argv[1:]
directory = pathlib.Path(directory)

def mark(name, text='ready'):
    target = directory / name
    temporary = directory / (name + '.tmp')
    temporary.write_text(text, encoding='ascii')
    temporary.replace(target)

def spawn(role):
    subprocess.Popen(
        [sys.executable, '-I', '-u', __file__, mode, role, str(directory)],
        stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

if role == 'root':
    spawn('child')
elif role == 'child':
    spawn('grandchild')
mark(role + '.pid', str(os.getpid()))
if role == 'root':
    header = sys.stdin.buffer.readline()
    length = int(header.decode('ascii').split(':', 1)[1])
    if sys.stdin.buffer.readline() != b'\r\n':
        raise RuntimeError('controlled peer received invalid framing')
    request = json.loads(sys.stdin.buffer.read(length))
    if request['method'] != 'initialize':
        raise RuntimeError('controlled peer expected initialize')
    mark('request-read')
    while not (directory / 'go').exists():
        time.sleep(0.01)
    if mode == 'malformed':
        sys.stdout.buffer.write(b'Content-Length: invalid\r\n\r\n')
        sys.stdout.buffer.flush()
    elif mode == 'root-exit':
        raw = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}).encode()
        sys.stdout.buffer.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
        sys.stdout.buffer.flush()
    sys.stdin.buffer.read()
    mark('stdin-eof')
    if mode == 'root-exit':
        sys.exit(0)
while True:
    time.sleep(0.01)
"""


class _FamilyPeer:
    def __init__(self, directory: Path, mode: str) -> None:
        self.directory = directory
        self.script = directory / "managed_family_peer.py"
        self.script.write_text(_FAMILY_PEER, encoding="utf-8")
        executable = str(Path(str(getattr(sys, "_base_executable", sys.executable))).resolve())
        self.server = McpServerDefinition(
            tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
            id="managed-family",
            transport=McpTransportKind.STDIO,
            command=executable,
            args=("-I", "-u", str(self.script), mode, "root", str(directory)),
            executable_allowlist=(executable,),
        )
        self.monitors: dict[str, _NativeMonitor] = {}
        self.processes: list[asyncio.subprocess.Process] = []
        self.request: asyncio.Task[str] | None = None

    async def factory(
        self, program: str, *args: str, stdin: int, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        assert (stdin, stdout, stderr) == (
            asyncio.subprocess.PIPE,
            asyncio.subprocess.PIPE,
            asyncio.subprocess.DEVNULL,
        )
        process = await create_subprocess_exec(
            program, *args, stdin=stdin, stdout=stdout, stderr=stderr
        )
        self.processes.append(process)
        self.monitors["root"] = _NativeMonitor(process.pid)
        return process

    def start(self) -> asyncio.Task[str]:
        client = StdioMcpClient(process_factory=self.factory)
        self.request = asyncio.create_task(client.health(self.server))
        return self.request

    async def wait_marker(self, name: str) -> None:
        async with asyncio.timeout(5):
            while not (self.directory / name).exists():
                if self.request is not None and self.request.done():
                    self.request.result()
                    raise AssertionError("controlled peer exited before becoming ready")
                await asyncio.sleep(0.01)

    async def capture_family(self) -> None:
        await self.wait_marker("root.pid")
        assert len(self.processes) == 1
        assert await self.read_pid("root") == self.processes[0].pid
        for role in ("child", "grandchild"):
            await self.wait_marker(role + ".pid")
            pid = await self.read_pid(role)
            self.monitors[role] = _NativeMonitor(pid)
        await self.wait_marker("request-read")
        assert all(monitor.poll() is None for monitor in self.monitors.values())

    async def read_pid(self, role: str) -> int:
        async with asyncio.timeout(5):
            while True:
                try:
                    return int((self.directory / (role + ".pid")).read_text(encoding="ascii"))
                except PermissionError:
                    await asyncio.sleep(0.01)

    async def result(self) -> str:
        assert self.request is not None
        done, _ = await asyncio.wait({self.request}, timeout=4)
        assert self.request in done, "managed MCP request exceeded its cleanup deadline"
        return self.request.result()

    async def assert_family_exited(self, *, root_code: int | None = None) -> None:
        assert set(self.monitors) == {"root", "child", "grandchild"}
        for role, monitor in self.monitors.items():
            code = await monitor.wait_exit()
            if role == "root" and root_code is not None:
                assert code == root_code
            else:
                assert code != 0, f"{role} was not terminated by managed cleanup"
            assert code != 124, f"{role} reached the 15-second rescue deadline"
        assert (self.directory / "stdin-eof").exists()

    async def cleanup(self) -> None:
        errors: list[Exception] = []
        if self.request is not None and not self.request.done():
            self.request.cancel()
        # Factory failure before returning still leaves this captured Process owned by the test.
        for process in self.processes:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except Exception as error:  # noqa: BLE001 - attempt every owned rescue resource
                errors.append(error)
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception as error:  # noqa: BLE001 - kill failure must not skip stdin close
                errors.append(error)
        for monitor in reversed(list(self.monitors.values())):
            try:
                monitor.terminate_if_running()
            except Exception as error:  # noqa: BLE001 - attempt every independent monitor
                errors.append(error)
        for monitor in self.monitors.values():
            try:
                await monitor.wait_exit()
            except Exception as error:  # noqa: BLE001 - attempt every independent monitor
                errors.append(error)
        if self.request is not None:
            done, _ = await asyncio.wait({self.request}, timeout=5)
            if self.request in done:
                await asyncio.gather(self.request, return_exceptions=True)
            else:
                errors.append(TimeoutError("MCP task still pending after owned-handle rescue"))
        for process in self.processes:
            try:
                # Do not compete with the request's output reader if request cleanup is stuck.
                if self.request is None or self.request.done():
                    await asyncio.wait_for(process.communicate(), timeout=5)
                else:
                    await asyncio.wait_for(process.wait(), timeout=5)
            except Exception as error:  # noqa: BLE001 - reap failure must not skip handle closes
                errors.append(error)
        for monitor in self.monitors.values():
            try:
                monitor.close()
            except Exception as error:  # noqa: BLE001 - close every owned native handle
                errors.append(error)
        if errors:
            raise OSError("controlled family rescue incomplete") from errors[0]


async def test_managed_malformed_frame_reaps_family_and_preserves_error(tmp_path: Path) -> None:
    peer = _FamilyPeer(tmp_path, "malformed")
    before = asyncio.all_tasks()
    try:
        peer.start()
        await peer.capture_family()
        started = asyncio.get_running_loop().time()
        (tmp_path / "go").touch()
        with pytest.raises(RuntimeError, match="^MCP Content-Length is invalid$"):
            await peer.result()
        assert asyncio.get_running_loop().time() - started >= 0.8, "MCP grace was bypassed"
        await peer.assert_family_exited()
        assert not (asyncio.all_tasks() - before), "MCP error left async cleanup tasks"
    finally:
        await peer.cleanup()


@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_managed_request_cancellation_reaps_family_after_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_twice: bool
) -> None:
    peer = _FamilyPeer(tmp_path, "cancel")
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_calls = 0
    original_cleanup = drain_and_reap_process
    assert vars(mcp_client)["drain_and_reap_process"] is original_cleanup

    async def gated_cleanup(
        process: asyncio.subprocess.Process, *, grace_seconds: float = 1.0
    ) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_entered.set()
        await release_cleanup.wait()
        await original_cleanup(process, grace_seconds=grace_seconds)

    if cancel_twice:
        monkeypatch.setattr(mcp_client, "drain_and_reap_process", gated_cleanup)
    before = asyncio.all_tasks()
    try:
        request = peer.start()
        await peer.capture_family()
        (tmp_path / "go").touch()
        started = asyncio.get_running_loop().time()
        assert request.cancel(), "first cancellation did not reach a live request"
        await peer.wait_marker("stdin-eof")
        if cancel_twice:
            await asyncio.wait_for(cleanup_entered.wait(), timeout=3)
            assert not request.done() and not release_cleanup.is_set()
            assert request.cancel(), "second cancellation missed gated cleanup"
            await asyncio.sleep(0)
            assert not request.done(), "second cancellation escaped owned cleanup"
            started = asyncio.get_running_loop().time()
            release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await peer.result()
        assert asyncio.get_running_loop().time() - started >= 0.8, "MCP grace was bypassed"
        await peer.assert_family_exited()
        if cancel_twice:
            assert cleanup_calls == 1, "gated original cleanup did not run exactly once"
        assert not (asyncio.all_tasks() - before), "MCP cancellation left async cleanup tasks"
    finally:
        release_cleanup.set()
        await peer.cleanup()


async def test_managed_root_exit_closes_descendant_stdout_without_release(tmp_path: Path) -> None:
    peer = _FamilyPeer(tmp_path, "root-exit")
    before = asyncio.all_tasks()
    try:
        peer.start()
        await peer.capture_family()
        (tmp_path / "go").touch()
        assert await peer.result() == "healthy"
        await peer.assert_family_exited(root_code=0)
        assert peer.processes[0].returncode == 0
        assert not (asyncio.all_tasks() - before), "inherited stdout left async cleanup tasks"
    finally:
        await peer.cleanup()


async def test_monitor_setup_failure_rescues_captured_real_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _FamilyPeer(tmp_path, "cancel")
    before = asyncio.all_tasks()

    def failed_monitor(pid: int) -> Any:
        raise OSError("controlled root monitor setup failure")

    monkeypatch.setattr(sys.modules[__name__], "_NativeMonitor", failed_monitor)
    try:
        peer.start()
        with pytest.raises(OSError, match="controlled root monitor setup failure"):
            await peer.result()
        assert len(peer.processes) == 1 and not peer.monitors
        assert peer.processes[0].returncode is None
    finally:
        await peer.cleanup()
    assert peer.processes[0].returncode is not None
    assert await peer.processes[0].wait() != 124
    assert not (asyncio.all_tasks() - before), "monitor setup failure leaked owned process tasks"
