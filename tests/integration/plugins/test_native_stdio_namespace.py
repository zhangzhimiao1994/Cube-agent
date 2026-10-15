"""Explicit scoped CLI/MCP native execution, not default app or user delivery proof."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import sysconfig
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub import subprocess_cleanup
from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.mcp import client as mcp_client
from agent_hub.mcp.client import StdioMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind
from agent_hub.plugins.runtime import (
    LocalCommandPluginAdapter,
    PluginInvocationContext,
)
from agent_hub.process_namespace import BubblewrapStdioProcessFactory
from tests.integration.plugins.test_native_package_namespace import (
    _family,
    _native_attribute,
    _wait_exited,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENT_HUB_NATIVE_STDIO_NAMESPACE") != "1" or os.name != "posix",
        reason="explicit native Linux stdio opt-in required",
    ),
]
_IDENTITY = UUID("77777777-7777-4777-8777-777777777777")
_PROGRAM = str(Path(sys.executable).resolve())
_INSPECT = """import json, os, pathlib, socket, subprocess, sys, time
def inspect(hold=False):
    if hold:
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                         stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
        time.sleep(30)
    private = pathlib.Path(sys.argv[1])
    result = {'secret_absent': 'NATIVE_SYNTHETIC_SECRET' not in os.environ,
              'uid': os.getuid(), 'home': os.environ.get('HOME')}
    for mode in ('r', 'w'):
        try:
            with private.open(mode) as stream:
                stream.read() if mode == 'r' else stream.write('bad')
        except OSError:
            result[mode + '_denied'] = True
        else:
            result[mode + '_denied'] = False
    try:
        pathlib.Path('unauthorized-write').write_text('bad')
    except OSError:
        result['readonly'] = True
    else:
        result['readonly'] = False
    pathlib.Path('/tmp/permitted-scratch').write_text('good')
    result['scratch'] = True
    if len(sys.argv) > 2:
        try:
            socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=0.2).close()
        except OSError:
            result['tcp_denied'] = True
        else:
            result['tcp_denied'] = False
        peer = socket.socket(socket.AF_UNIX)
        peer.settimeout(0.2)
        try:
            peer.connect(sys.argv[3])
        except OSError:
            result['unix_denied'] = True
        else:
            result['unix_denied'] = False
        finally:
            peer.close()
    return result
"""
_CLI = "print(json.dumps(inspect(os.environ.get('HOLD') == '1')))\n"
_MCP = """
def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    length = int(line.decode('ascii').split(':', 1)[1])
    while sys.stdin.buffer.readline() not in (b'\\r\\n', b'\\n', b''):
        pass
    return json.loads(sys.stdin.buffer.read(length))
def write_message(payload):
    raw = json.dumps(payload).encode('utf-8')
    sys.stdout.buffer.write(f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode('ascii') + raw)
    sys.stdout.buffer.flush()
while True:
    message = read_message()
    if message is None:
        break
    method = message.get('method')
    if method == 'notifications/initialized':
        continue
    if method == 'tools/list':
        result = {'tools': [{'name': 'inspect', 'description': 'Native scope inspection',
                            'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': json.dumps(
            inspect(message['params']['arguments'].get('hold', False)))}]}
    else:
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'native', 'version': '1.0.0'}}
    write_message({'jsonrpc': '2.0', 'id': message.get('id'), 'result': result})
"""


@pytest.fixture
def scope(tmp_path: Path) -> tuple[Path, Path, BubblewrapStdioProcessFactory]:
    assert int(_native_attribute(os, "geteuid")()) == 10001
    root = tmp_path.parent / ("s-" + uuid4().hex[:8])
    root.mkdir()
    workspace = root / "work"
    workspace.mkdir()
    private = root / "private"
    private.write_text("synthetic-private", encoding="ascii")
    launcher = shutil.which("bwrap")
    assert launcher is not None, "required native launcher is absent"
    factory = _factory(workspace, private)
    return workspace, private, factory


def _factory(
    workspace: Path, private: Path, extra: tuple[Path, ...] = ()
) -> BubblewrapStdioProcessFactory:
    launcher = shutil.which("bwrap")
    assert launcher is not None
    runtime = [Path(sysconfig.get_path("stdlib"))]
    # Exact ELF files observed for the trusted server CPython, not whole /usr/lib.
    runtime.extend(
        Path("/lib/x86_64-linux-gnu") / name
        for name in (
            "libpthread.so.0",
            "libdl.so.2",
            "libutil.so.1",
            "libm.so.6",
            "librt.so.1",
            "libc.so.6",
        )
    )
    runtime.append(Path("/lib64/ld-linux-x86-64.so.2"))
    return BubblewrapStdioProcessFactory(
        bubblewrap_executable=Path(launcher),
        working_directory=workspace,
        approved_programs=(Path(_PROGRAM),),
        runtime_readonly_paths=(*runtime, *extra),
        protected_paths=(private,),
    )


def _plugin(
    script: Path, private: Path, *, timeout: float = 5, extra: tuple[str, ...] = ()
) -> PluginResourceResponse:
    return PluginResourceResponse(
        id="native-cli",
        name="Native CLI",
        status="running",
        health="healthy",
        timeout_seconds=timeout,
        resource_config={
            "command": _PROGRAM,
            "base_args": ("-I", "-u", str(script), str(private), *extra),
        },
        capabilities=[
            PluginCapabilityRequest(
                id="native.inspect",
                adapter="local_command",
                permission_class="test.exec",
                sandbox_profile="local_process",
            )
        ],
    )


async def _cli(
    factory: BubblewrapStdioProcessFactory, plugin: PluginResourceResponse
) -> dict[str, Any]:
    adapter = LocalCommandPluginAdapter(process_factory=factory)
    await adapter.preflight(plugin=plugin)
    result = await adapter.invoke(
        plugin=plugin,
        capability=plugin.capabilities[0],
        arguments={},
        context=PluginInvocationContext(
            tenant_id=_IDENTITY,
            user_id=_IDENTITY,
            run_id=_IDENTITY,
            actor="tester",
            idempotency_key="native-cli-scope",
        ),
    )
    assert result["ok"] is True and result["exit_code"] == 0
    return dict(json.loads(str(result["stdout"])))


def _server(script: Path, private: Path) -> McpServerDefinition:
    return McpServerDefinition(
        tenant_id=_IDENTITY,
        id="native-stdio",
        transport=McpTransportKind.STDIO,
        command=_PROGRAM,
        args=("-I", "-u", str(script), str(private)),
        executable_allowlist=(_PROGRAM,),
        timeout_seconds=5,
    )


def _assert_scope(result: dict[str, Any], workspace: Path, private: Path) -> None:
    assert result == {
        "uid": 10001,
        "secret_absent": True,
        "home": "/tmp",
        "r_denied": True,
        "w_denied": True,
        "readonly": True,
        "scratch": True,
    }
    assert private.read_text(encoding="ascii") == "synthetic-private"
    assert not (workspace / "unauthorized-write").exists()


async def test_real_cli_adapter_runs_inside_selected_namespace(
    scope: tuple[Path, Path, BubblewrapStdioProcessFactory], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, private, factory = scope
    monkeypatch.setenv("NATIVE_SYNTHETIC_SECRET", "not-a-real-secret")
    script = workspace / "cli.py"
    script.write_text(_INSPECT + _CLI, encoding="ascii")
    _assert_scope(await _cli(factory, _plugin(script, private)), workspace, private)


async def test_real_mcp_handshake_discovery_and_invocation_are_isolated(
    scope: tuple[Path, Path, BubblewrapStdioProcessFactory], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, private, factory = scope
    monkeypatch.setenv("NATIVE_SYNTHETIC_SECRET", "not-a-real-secret")
    script = workspace / "mcp.py"
    script.write_text(_INSPECT + _MCP, encoding="ascii")
    server = _server(script, private)
    client = StdioMcpClient(process_factory=factory)
    assert await asyncio.wait_for(client.health(server), 5) == "healthy"
    tools = await asyncio.wait_for(client.discover_tools(server), 5)
    assert [tool.name for tool in tools] == ["inspect"]
    result = await asyncio.wait_for(client.invoke(server, "inspect", {}), 5)
    content = result.content["content"]
    assert isinstance(content, list) and isinstance(content[0], dict)
    _assert_scope(dict(json.loads(content[0]["text"])), workspace, private)


async def test_cli_cannot_connect_to_live_host_tcp_or_unix_socket(
    scope: tuple[Path, Path, BubblewrapStdioProcessFactory],
) -> None:
    workspace, private, factory = scope
    script = workspace / "network.py"
    script.write_text(_INSPECT + _CLI, encoding="ascii")
    tcp = socket.socket()
    unix = socket.socket(int(_native_attribute(socket, "AF_UNIX")))
    host_socket = workspace.parent / "host.sock"
    try:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(4)
        unix.bind(str(host_socket))
        unix.listen(4)
        port = tcp.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        baseline = socket.socket(int(_native_attribute(socket, "AF_UNIX")))
        try:
            baseline.connect(str(host_socket))
        finally:
            baseline.close()
        result = await _cli(factory, _plugin(script, private, extra=(str(port), str(host_socket))))
        assert result.pop("tcp_denied") is True and result.pop("unix_denied") is True
        _assert_scope(result, workspace, private)
    finally:
        tcp.close()
        unix.close()


@pytest.mark.parametrize("kind", ["hardlink", "socket", "fifo", "symlink", "file_hardlink"])
async def test_unsafe_runtime_alias_and_host_ipc_are_refused_before_spawn(
    scope: tuple[Path, Path, BubblewrapStdioProcessFactory],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    workspace, private, _ = scope
    runtime = workspace.parent / "extra-runtime"
    listener: socket.socket | None = None
    if kind == "file_hardlink":
        os.link(private, runtime)
    else:
        runtime.mkdir()
        alias = runtime / "unsafe"
        if kind == "hardlink":
            os.link(private, alias)
        elif kind == "symlink":
            alias.symlink_to(private)
        elif kind == "fifo":
            _native_attribute(os, "mkfifo")(alias)
        else:
            listener = socket.socket(int(_native_attribute(socket, "AF_UNIX")))
            listener.bind(str(alias))
            listener.listen(1)
    spawns = 0

    async def forbidden(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawns
        spawns += 1
        raise OSError("unsafe runtime reached native execution")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    try:
        factory = _factory(workspace, private, (runtime,))
        with pytest.raises(OSError, match="unsafe filesystem"):
            await factory(_PROGRAM, stdin=-1, stdout=-1, stderr=-3)
        assert spawns == 0 and private.read_text(encoding="ascii") == "synthetic-private"
    finally:
        if listener is not None:
            listener.close()


@pytest.mark.parametrize(
    "interface,action",
    [
        ("cli", "timeout"),
        ("cli", "cancel"),
        ("cli", "cancel_twice"),
        ("mcp", "cancel"),
        ("mcp", "cancel_twice"),
    ],
)
async def test_real_stdio_family_exits_before_fixture_rescue(
    scope: tuple[Path, Path, BubblewrapStdioProcessFactory],
    monkeypatch: pytest.MonkeyPatch,
    interface: str,
    action: str,
) -> None:
    workspace, private, factory = scope
    script = workspace / "family.py"
    source = _INSPECT + (
        _CLI.replace("os.environ.get('HOLD') == '1'", "True") if interface == "cli" else _MCP
    )
    script.write_text(source, encoding="ascii")
    processes: list[asyncio.subprocess.Process] = []
    descriptors: dict[int, int] = {}
    spawn = asyncio.create_subprocess_exec

    async def capture(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        processes.append(process)
        descriptors[process.pid] = int(_native_attribute(os, "pidfd_open")(process.pid))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    entered, release = asyncio.Event(), asyncio.Event()
    drain = subprocess_cleanup.drain_and_reap_process

    async def gate(process: asyncio.subprocess.Process, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        await drain(process, **kwargs)

    if action == "cancel_twice":
        monkeypatch.setattr(
            subprocess_cleanup if interface == "cli" else mcp_client, "drain_and_reap_process", gate
        )

    async def operation() -> object:
        if interface == "cli":
            return await _cli(factory, _plugin(script, private, timeout=2))
        return await StdioMcpClient(process_factory=factory).invoke(
            _server(script, private), "inspect", {"hold": True}
        )

    before = asyncio.all_tasks()
    task = asyncio.create_task(operation())
    try:
        async with asyncio.timeout(3):
            while True:
                if task.done():
                    task.result()
                if processes:
                    for pid in _family(processes[0].pid):
                        if pid not in descriptors:
                            descriptors[pid] = int(_native_attribute(os, "pidfd_open")(pid))
                    python_members = 0
                    for pid in descriptors:
                        try:
                            executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
                        except FileNotFoundError:
                            continue
                        python_members += executable == Path(sys.executable).resolve()
                    if python_members >= 2:
                        break
                await asyncio.sleep(0.01)
        if action != "timeout":
            task.cancel()
            if action == "cancel_twice":
                await asyncio.wait_for(entered.wait(), 2)
                assert not task.done()
                task.cancel()
                release.set()
        finished, _ = await asyncio.wait({task}, timeout=5)
        assert task in finished, "cleanup exceeded deadline before fixture rescue"
        if action == "timeout":
            with pytest.raises(RuntimeCapabilityError, match="Plugin tool timed out"):
                task.result()
        else:
            with pytest.raises(asyncio.CancelledError):
                task.result()
        await _wait_exited(list(descriptors.values()))
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        errors: list[Exception] = []
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except Exception as error:  # noqa: BLE001 - rescue every independently owned resource
                    errors.append(error)
        for descriptor in descriptors.values():
            try:
                _native_attribute(signal, "pidfd_send_signal")(descriptor, 9)
            except ProcessLookupError:
                pass
            except Exception as error:  # noqa: BLE001 - one pidfd failure must not skip the others
                errors.append(error)
        if not task.done():
            task.cancel()
        finished, _ = await asyncio.wait({task}, timeout=5)
        if task in finished:
            await asyncio.gather(task, return_exceptions=True)
        else:
            errors.append(TimeoutError("native stdio rescue did not settle operation"))
        try:
            await _wait_exited(list(descriptors.values()))
        except Exception as error:  # noqa: BLE001 - readiness uncertainty still needs handle closes
            errors.append(error)
        finally:
            for descriptor in descriptors.values():
                try:
                    os.close(descriptor)
                except Exception as error:  # noqa: BLE001 - close every independent handle
                    errors.append(error)
        if errors:
            raise OSError("native stdio fixture rescue incomplete") from errors[0]
