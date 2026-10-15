"""Opt-in real Linux package-view probes using reviewed literal code only."""
from __future__ import annotations

import asyncio
import hashlib
import os
import select
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins import runtime
from agent_hub.plugins.execution_authority import (
    GeneratedExecutionAuthority,
    PackageExecutionMember,
)
from agent_hub.plugins.execution_view import SealedPackageView, capture_sealed_package

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("AGENT_HUB_NATIVE_PACKAGE_VIEW_TEST") != "1",
    reason="requires explicitly selected native Linux bubblewrap test scope",
)
IDENTITY = UUID("77777777-7777-4777-8777-777777777777")
SOURCE = b'''import json, os, pathlib
root = pathlib.Path(__file__).parent
try:
    (root / 'unexpected-write').write_text('unexpected')
except OSError:
    readonly = True
else:
    readonly = False
json.dump({'value': json.loads((root / 'config.json').read_bytes())['value'],
           'readonly': readonly,
           'readonly_mount': bool(os.statvfs(root).f_flag & os.ST_RDONLY)}, __import__('sys').stdout)
'''
CANCEL_SOURCE = b"import sys, time\nsys.stdout.write('ready\\n'); sys.stdout.flush()\ntime.sleep(60)\n"


def setup(root: Path, source: bytes = SOURCE) -> tuple[
    runtime.PythonSubprocessPluginPackageRunner, runtime.PluginPackageExecutionTarget,
    PluginResourceResponse, PluginCapabilityRequest, runtime.PluginInvocationContext,
]:
    root.mkdir()
    content = {"main.py": source, "config.json": b'{"value":"approved"}'}
    for name, data in content.items():
        (root / name).write_bytes(data)
    proof = GeneratedExecutionAuthority(
        tenant_id=IDENTITY, plugin_id="native-generated-test", registration_id=IDENTITY,
        archive_sha256="a" * 64, report_id="gs-" + "b" * 64, report_sha256="c" * 64,
        members=tuple(PackageExecutionMember(name, len(data), hashlib.sha256(data).hexdigest())
                      for name, data in sorted(content.items())),
    )
    target = runtime.PluginPackageExecutionTarget(root=root, entrypoint=root / "main.py", execution_authority=proof)
    plugin = PluginResourceResponse(id=proof.plugin_id, name="Native fixed source", content_sha256="a" * 64,
                                    status="running", health="healthy")
    capability = PluginCapabilityRequest(id="native.run", adapter="python_subprocess_v1")
    context = runtime.PluginInvocationContext(tenant_id=IDENTITY, user_id=IDENTITY, run_id=IDENTITY,
                                              actor="native-test", idempotency_key="native-test",
                                              execution_authority=proof)
    runner = runtime.PythonSubprocessPluginPackageRunner(
        process_launcher=runtime.BubblewrapPluginPackageProcessLauncher(bubblewrap_executable=Path("/usr/bin/bwrap")),
        timeout_seconds=5, environment={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    return runner, target, plugin, capability, context


def assert_closed(fds: list[int]) -> None:
    assert fds
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


async def test_actual_sealed_view_survives_host_bytes_and_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, target, plugin, capability, context = setup(tmp_path / "installed")
    original = capture_sealed_package
    descriptors: list[int] = []

    @contextmanager
    def capture(*args: Any, **kwargs: Any) -> Iterator[SealedPackageView]:
        with original(*args, **kwargs) as view:
            descriptors.extend(view.pass_fds)
            (target.root / "main.py").write_bytes(b"raise RuntimeError('must not run')\n")
            (target.root / "config.json").write_bytes(b'{"value":"changed"}')
            target.root.rename(tmp_path / "replaced")
            target.root.mkdir()
            (target.root / "main.py").write_bytes(b"raise RuntimeError('replacement must not run')\n")
            yield view

    monkeypatch.setattr(runtime, "capture_sealed_package", capture)
    result = await runner.invoke(target=target, plugin=plugin, capability=capability, arguments={}, context=context)
    assert result == {"value": "approved", "readonly": True, "readonly_mount": True}
    assert not (target.root / "unexpected-write").exists()
    assert_closed(descriptors)


async def test_actual_tamper_is_refused_before_any_process_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, target, plugin, capability, context = setup(tmp_path / "installed")
    (target.root / "config.json").write_bytes(b'{"value":"tampered"}')

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        pytest.fail("tampered approved bytes reached spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeCapabilityError, match="immutable capture"):
        await runner.invoke(target=target, plugin=plugin, capability=capability, arguments={}, context=context)


async def test_actual_cancel_reaps_process_before_sealed_fds_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux pidfd test")
    runner, target, plugin, capability, context = setup(tmp_path / "installed", CANCEL_SOURCE)
    original_capture = capture_sealed_package
    original_spawn = asyncio.create_subprocess_exec
    original_communicate = runtime._communicate_with_bounded_stdout
    ready = asyncio.Event()
    descriptors: list[int] = []
    children: list[asyncio.subprocess.Process] = []
    pidfds: list[int] = []

    @contextmanager
    def capture(*args: Any, **kwargs: Any) -> Iterator[SealedPackageView]:
        with original_capture(*args, **kwargs) as view:
            descriptors.extend(view.pass_fds)
            try:
                yield view
            finally:
                assert children[0].returncode is not None
                for descriptor in view.pass_fds:
                    os.fstat(descriptor)

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        children.append(process)
        pidfds.append(os.pidfd_open(process.pid))
        return process

    async def communicate(process: asyncio.subprocess.Process, payload: bytes, *, max_stdout_bytes: int) -> bytes:
        assert process.stdout is not None
        assert await process.stdout.readline() == b"ready\n"
        ready.set()
        return await original_communicate(process, payload, max_stdout_bytes=max_stdout_bytes)

    monkeypatch.setattr(runtime, "capture_sealed_package", capture)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(runtime, "_communicate_with_bounded_stdout", communicate)
    task = asyncio.create_task(runner.invoke(target=target, plugin=plugin, capability=capability,
                                            arguments={}, context=context))
    try:
        await asyncio.wait_for(ready.wait(), 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
        assert children[0].returncode is not None
        assert select.select(pidfds, [], [], 0)[0] == pidfds
        assert_closed(descriptors)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for descriptor in pidfds:
            os.close(descriptor)
