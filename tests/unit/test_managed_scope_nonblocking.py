from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub import process_namespace
from agent_hub.api.routers.admin import PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capability_installer import environment, managed_scope
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    PythonEnvironmentSpec,
)
from agent_hub.capability_installer.managed_scope import ManagedEnvironmentPluginAdapter
from agent_hub.plugins.runtime import LocalCommandPluginAdapter
from tests.unit.test_managed_environment_scope import (
    _build_python,
    _Fixture,
    _invoke,
    _Process,
)
from tests.unit.test_managed_environment_scope import (
    spawn as spawn,  # noqa: PLC0414 - pytest fixture
)


def _installed(
    tmp_path: Path, *, python: bool = False
) -> tuple[_Fixture, ManagedEnvironmentPluginAdapter, PluginResourceResponse]:
    scope = _Fixture(tmp_path)
    if python:

        def build(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["-m", "venv"]:
                staging = Path(argv[3])
                executable = staging / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                executable.parent.mkdir()
                executable.write_bytes(b"controlled interpreter")
                site = staging / "Lib/site-packages"
                site.mkdir(parents=True)
                (site / "module.py").write_bytes(b"VALUE = 1\n")
            return subprocess.CompletedProcess(argv, 0, "", "")

        scope.manager = CapabilityEnvironmentManager(scope.manager.root, runner=build)
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        lock = tmp_path / "requirements.lock"
        lock.write_text("demo==1.0.0 --hash=sha256:" + "a" * 64 + "\n", encoding="ascii")
        scope.record = _build_python(
            scope.manager,
            PythonEnvironmentSpec(scope.physical_id, "p1", lock, wheels, "module")
        )
    else:
        scope.install()
    plugin = scope.plugin().model_copy(update={"timeout_seconds": 5.0})
    adapter = cast(ManagedEnvironmentPluginAdapter, scope.adapter(scope.policy(), plugin))
    return scope, adapter, plugin


def _target(
    stage: str, scope: _Fixture, adapter: ManagedEnvironmentPluginAdapter
) -> tuple[object, str]:
    return {
        "active": (scope.manager, "active"),
        "python_tree": (environment, "verify_tree"),
        "add_reference": (scope.manager, "add_reference"),
        "remove_reference": (scope.manager, "remove_reference"),
        "factory": (managed_scope._ManagedVersionFactory, "__init__"),
        "revalidation": (adapter, "_check_plugin"),
        "mount_validation": (adapter._ordinary_factory, "_validated_mounts"),
        "inspection": (process_namespace, "_ensure_private_package_mount"),
    }[stage]


@pytest.mark.parametrize("operation", ["invoke", "preflight"])
@pytest.mark.parametrize(
    "stage",
    [
        "active",
        "python_tree",
        "add_reference",
        "remove_reference",
        "factory",
        "revalidation",
        "mount_validation",
        "inspection",
    ],
)
async def test_synchronous_admission_keeps_request_heartbeat_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn: list[tuple[str, ...]],
    stage: str,
    operation: str,
) -> None:
    scope, adapter, plugin = _installed(tmp_path, python=stage == "python_tree")
    target, name = _target(stage, scope, adapter)
    original: Callable[..., Any] = getattr(target, name)
    loop = asyncio.get_running_loop()
    heartbeats: list[bool] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        pulse = threading.Event()
        loop.call_soon_threadsafe(pulse.set)
        heartbeats.append(pulse.wait(0.3))
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(target, name, slow)
        if operation == "invoke":
            assert (await _invoke(adapter, plugin))["ok"] is True
        else:
            await adapter.preflight(plugin=plugin)
    assert heartbeats and all(heartbeats), f"{stage} blocked the request heartbeat"
    assert scope.record is not None
    assert scope.manager.references(scope.physical_id, scope.record.version) == ()
    assert len(spawn) == 1


@pytest.mark.parametrize(
    "stage", ["active", "add_reference", "factory", "inspection", "remove_reference"]
)
async def test_cancel_drains_sync_worker_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn: list[tuple[str, ...]],
    stage: str,
) -> None:
    scope, adapter, plugin = _installed(tmp_path)
    target, name = _target(stage, scope, adapter)
    original: Callable[..., Any] = getattr(target, name)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    first = True

    def blocked(*args: Any, **kwargs: Any) -> Any:
        nonlocal first
        if not first:
            return original(*args, **kwargs)
        first = False
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2), "sync worker blocked the request loop"
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    with monkeypatch.context() as patch:
        patch.setattr(target, name, blocked)
        task = asyncio.create_task(_invoke(adapter, plugin))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancellation abandoned a live synchronous worker"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert scope.record is not None
    references = scope.manager.references(scope.physical_id, scope.record.version)
    if stage in {"add_reference", "inspection"}:
        assert references, "unknown operation prematurely released its version"
    else:
        assert references == ()
    assert len(spawn) == (1 if stage == "remove_reference" else 0)


async def test_inspection_failure_retains_reference_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn: list[tuple[str, ...]],
) -> None:
    scope, adapter, plugin = _installed(tmp_path)

    def deny(*args: object, **kwargs: object) -> None:
        raise RuntimeCapabilityError("controlled mount denial")

    monkeypatch.setattr(process_namespace, "_ensure_private_package_mount", deny)
    with pytest.raises(RuntimeCapabilityError):
        await _invoke(adapter, plugin)
    assert scope.record is not None
    assert scope.manager.references(scope.physical_id, scope.record.version)
    assert spawn == []


async def test_child_task_cannot_reuse_parent_namespace_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn: list[tuple[str, ...]],
) -> None:
    scope, adapter, plugin = _installed(tmp_path)
    assert scope.record is not None
    command = scope.record.path / scope.record.executable
    alias = scope.record.path / "unapproved-hardlink"
    calls = 0

    async def operation(
        delegate: LocalCommandPluginAdapter,
        factory: managed_scope._ManagedVersionFactory,
    ) -> dict[str, bool]:
        async def launch() -> asyncio.subprocess.Process:
            return await factory(
                str(command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        async def create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            nonlocal calls
            calls += 1
            if calls == 1:
                os.link(command, alias)
                try:
                    child = asyncio.create_task(launch())
                    with pytest.raises(RuntimeCapabilityError):
                        await child
                finally:
                    alias.unlink()
            return cast(asyncio.subprocess.Process, _Process())

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_subprocess_exec", create)
            process = await launch()
            await process.wait()
        return {"ok": True}

    await adapter._with_version(plugin, operation)
    assert calls == 1
    assert scope.manager.references(scope.physical_id, scope.record.version) == ()
