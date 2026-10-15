"""Opt-in native bwrap fixtures; not ordinary-user/plugin delivery acceptance."""

from __future__ import annotations

import asyncio
import os
import select
import shutil
import signal
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub import subprocess_cleanup
from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.runtime import (
    BubblewrapPluginPackageProcessLauncher,
    PluginInvocationContext,
    PluginPackageExecutionTarget,
    PythonSubprocessPluginPackageRunner,
)
from agent_hub.runtime.contracts import JsonValue

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENT_HUB_NATIVE_PACKAGE_NAMESPACE") != "1" or os.name != "posix",
        reason="explicit native Linux isolation opt-in required",
    ),
]

_PEER = """import json, os, pathlib, socket, subprocess, sys, time
request = json.load(sys.stdin)
args = request['arguments']
if args.get('hold'):
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                     stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
    time.sleep(30)
else:
    result = {'uid': os.getuid(), 'secret_absent': 'NATIVE_SYNTHETIC_SECRET' not in os.environ}
    for operation in ('read', 'write'):
        denied = []
        for name in args.get('private_paths', []):
            try:
                with open(name, 'r' if operation == 'read' else 'w') as stream:
                    stream.read() if operation == 'read' else stream.write('changed')
            except OSError:
                denied.append(True)
            else:
                denied.append(False)
        result[operation + '_denied'] = denied
    try:
        pathlib.Path(__file__).with_name('unexpected-write').write_text('bad')
    except OSError:
        result['package_readonly'] = True
    else:
        result['package_readonly'] = False
    pathlib.Path('/tmp/native-private-scratch').write_text('allowed')
    result['scratch_write'] = True
    if args.get('dependency'):
        import native_dependency
        result['dependency_value'] = native_dependency.VALUE
    if args.get('tcp_port'):
        try:
            socket.create_connection(('127.0.0.1', args['tcp_port']), timeout=0.2).close()
        except OSError:
            result['tcp_denied'] = True
        else:
            result['tcp_denied'] = False
        peer = socket.socket(socket.AF_UNIX)
        peer.settimeout(0.2)
        try:
            peer.connect(args['unix_path'])
        except OSError:
            result['unix_denied'] = True
        else:
            result['unix_denied'] = False
        finally:
            peer.close()
    json.dump(result, sys.stdout)
"""


def _native_attribute(module: object, name: str) -> Any:
    value = getattr(module, name, None)
    if value is None:
        raise OSError(f"Native fixture requires {name}")
    return value


@pytest.fixture
def native_target(tmp_path: Path) -> PluginPackageExecutionTarget:
    if int(_native_attribute(os, "getuid")()) != 10001:
        pytest.fail("Native acceptance requires the declared nonroot fixture uid")
    scope = tmp_path.parent / ("n-" + uuid4().hex[:8])
    scope.mkdir()
    root = scope / "pkg"
    root.mkdir()
    entrypoint = root / "main.py"
    entrypoint.write_text(_PEER, encoding="ascii")
    return PluginPackageExecutionTarget(root=root, entrypoint=entrypoint)


def _runner(
    target: PluginPackageExecutionTarget, *, timeout: float = 5
) -> PythonSubprocessPluginPackageRunner:
    launcher = shutil.which("bwrap")
    if launcher is None:
        pytest.fail("Required native bwrap launcher is missing")
    return PythonSubprocessPluginPackageRunner(
        process_launcher=BubblewrapPluginPackageProcessLauncher(
            bubblewrap_executable=Path(launcher), protected_paths=(target.root.parent / "private",)
        ),
        timeout_seconds=timeout,
        environment={"PATH": "/usr/bin:/bin", "NATIVE_SYNTHETIC_SECRET": "not-a-real-secret"},
    )


async def _invoke(
    runner: PythonSubprocessPluginPackageRunner,
    target: PluginPackageExecutionTarget,
    arguments: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    identity = UUID("77777777-7777-4777-8777-777777777777")
    capability = PluginCapabilityRequest(
        id="native.inspect",
        adapter="native_test",
        permission_class="test.execute",
        sandbox_profile="local_process",
    )
    return await runner.invoke(
        target=target,
        plugin=PluginResourceResponse(
            id="native-isolation",
            name="Native isolation",
            status="running",
            health="healthy",
            capabilities=[capability],
        ),
        capability=capability,
        arguments=arguments,
        context=PluginInvocationContext(
            tenant_id=identity,
            user_id=identity,
            run_id=identity,
            actor="tester",
            idempotency_key="native-isolation-fixture",
        ),
    )


async def test_real_preflight_and_private_store_denials(
    native_target: PluginPackageExecutionTarget,
) -> None:
    private = native_target.root.parent / "private"
    private.mkdir()
    paths = [
        private / name for name in ("publisher", "ledger", "journal", "other-tenant", "secret")
    ]
    for path in paths:
        path.write_text("synthetic-private", encoding="ascii")
    runner = _runner(native_target)
    await runner.preflight(target=native_target)
    result = await _invoke(
        runner, native_target, {"private_paths": tuple(str(path) for path in paths)}
    )
    assert result["uid"] == 10001 and result["secret_absent"] is True
    assert result["read_denied"] == (True,) * len(paths)
    assert result["write_denied"] == (True,) * len(paths)
    assert result["package_readonly"] is True and result["scratch_write"] is True
    assert all(path.read_text(encoding="ascii") == "synthetic-private" for path in paths)
    assert not (native_target.root / "unexpected-write").exists()
    assert not (native_target.root.parent / "native-private-scratch").exists()


async def test_exact_dependency_mount_imports_without_service_prefix(
    native_target: PluginPackageExecutionTarget,
) -> None:
    dependency = native_target.root.parent / "deps"
    dependency.mkdir()
    (dependency / "native_dependency.py").write_text("VALUE = 'approved-input'\n", encoding="ascii")
    target = PluginPackageExecutionTarget(
        root=native_target.root, entrypoint=native_target.entrypoint, dependency_root=dependency
    )
    runner = _runner(target)
    await runner.preflight(target=target)
    result = await _invoke(runner, target, {"dependency": True})
    assert result["dependency_value"] == "approved-input"


async def test_live_host_tcp_and_unix_are_not_reachable(
    native_target: PluginPackageExecutionTarget,
) -> None:
    unix_path = native_target.root.parent / "host.sock"

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    tcp = await asyncio.start_server(peer, "127.0.0.1", 0)
    unix = cast(
        asyncio.AbstractServer,
        await _native_attribute(asyncio, "start_unix_server")(peer, path=unix_path),
    )
    try:
        port = int(tcp.sockets[0].getsockname()[1])
        for connection in (
            asyncio.open_connection("127.0.0.1", port),
            _native_attribute(asyncio, "open_unix_connection")(unix_path),
        ):
            _, writer = await connection
            writer.close()
            await writer.wait_closed()
        result = await _invoke(
            _runner(native_target), native_target, {"tcp_port": port, "unix_path": str(unix_path)}
        )
        assert result["tcp_denied"] is True and result["unix_denied"] is True
        assert tcp.is_serving() and unix.is_serving()
    finally:
        tcp.close()
        unix.close()
        await tcp.wait_closed()
        await unix.wait_closed()


@pytest.mark.parametrize("interface", ["preflight", "invoke"])
@pytest.mark.parametrize("role", ["package", "dependency"])
@pytest.mark.parametrize("kind", ["hardlink", "symlink", "fifo", "socket"])
async def test_mount_alias_and_host_ipc_refused_before_execution(
    native_target: PluginPackageExecutionTarget,
    monkeypatch: pytest.MonkeyPatch,
    interface: str,
    role: str,
    kind: str,
) -> None:
    dependency = native_target.root.parent / "deps"
    dependency.mkdir()
    host = native_target.root.parent / "synthetic-private"
    host.write_text("private", encoding="ascii")
    mounted = native_target.root if role == "package" else dependency
    entry = mounted / "unsafe-member"
    listener: socket.socket | None = None
    if kind == "hardlink":
        os.link(host, entry)
    elif kind == "symlink":
        entry.symlink_to(host)
    elif kind == "fifo":
        _native_attribute(os, "mkfifo")(entry)
    else:
        listener = socket.socket(int(_native_attribute(socket, "AF_UNIX")))
        listener.bind(str(entry))
        listener.listen(1)
    target = PluginPackageExecutionTarget(
        root=native_target.root, entrypoint=native_target.entrypoint, dependency_root=dependency
    )
    spawns = 0

    async def forbidden(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawns
        spawns += 1
        raise OSError("unsafe mount reached spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    try:
        runner = _runner(target)
        with pytest.raises(RuntimeCapabilityError, match="unsafe filesystem"):
            if interface == "preflight":
                await runner.preflight(target=target)
            else:
                await _invoke(runner, target, {})
        assert spawns == 0 and host.read_text(encoding="ascii") == "private"
    finally:
        if listener is not None:
            listener.close()


def _family(root: int) -> set[int]:
    found, pending = {root}, [root]
    while pending:
        pid = pending.pop()
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        for value in children.split():
            child = int(value)
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


async def _wait_exited(descriptors: list[int]) -> None:
    create_poller = getattr(select, "poll", None)
    if not callable(create_poller):
        raise OSError("Native pidfd readiness unavailable")
    poller = create_poller()
    for descriptor in descriptors:
        poller.register(descriptor, 1)
    pending = set(descriptors)
    async with asyncio.timeout(2):
        while pending:
            for descriptor, event in poller.poll(0):
                if event & 1:
                    pending.discard(descriptor)
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("action", ["timeout", "cancel", "cancel_twice"])
async def test_native_namespace_family_cleanup(
    native_target: PluginPackageExecutionTarget, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    processes: list[asyncio.subprocess.Process] = []
    descriptors: dict[int, int] = {}
    create = asyncio.create_subprocess_exec

    async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        processes.append(process)
        descriptors[process.pid] = int(_native_attribute(os, "pidfd_open")(process.pid))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    before = asyncio.all_tasks()
    entered, release = asyncio.Event(), asyncio.Event()
    drain = subprocess_cleanup.drain_and_reap_process

    async def gate(process: asyncio.subprocess.Process, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        await drain(process, **kwargs)

    if action == "cancel_twice":
        monkeypatch.setattr(subprocess_cleanup, "drain_and_reap_process", gate)
    task = asyncio.create_task(_invoke(_runner(native_target), native_target, {"hold": True}))
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
                await asyncio.wait_for(entered.wait(), 1)
                assert not task.done()
                task.cancel()
                release.set()
        finished, _ = await asyncio.wait({task}, timeout=7)
        assert task in finished, "native cleanup did not finish before fixture rescue"
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
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except Exception as error:  # noqa: BLE001 - rescue all independently owned resources
                errors.append(error)
        for descriptor in descriptors.values():
            try:
                _native_attribute(signal, "pidfd_send_signal")(descriptor, 9)
            except ProcessLookupError:
                pass
            except Exception as error:  # noqa: BLE001 - one failure must not skip other pidfds
                errors.append(error)
        if not task.done():
            task.cancel()
        finished, _ = await asyncio.wait({task}, timeout=5)
        if task in finished:
            await asyncio.gather(task, return_exceptions=True)
        else:
            errors.append(TimeoutError("native fixture rescue did not settle runner"))
        try:
            await _wait_exited(list(descriptors.values()))
        except Exception as error:  # noqa: BLE001 - unknown readiness still requires handle closes
            errors.append(error)
        finally:
            for descriptor in descriptors.values():
                try:
                    os.close(descriptor)
                except Exception as error:  # noqa: BLE001 - attempt every independently owned close
                    errors.append(error)
        assert not (asyncio.all_tasks() - before)
        if errors:
            raise OSError("native fixture rescue incomplete") from errors[0]
