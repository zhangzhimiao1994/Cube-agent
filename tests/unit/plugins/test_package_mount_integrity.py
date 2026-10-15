"""Pre-spawn mount integrity, without native namespace credit."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins import runtime
from agent_hub.plugins.runtime import (
    PluginInvocationContext,
    PluginPackageExecutionTarget,
    PythonSubprocessPluginPackageRunner,
)


@pytest.mark.parametrize("interface", ["preflight", "invoke"])
@pytest.mark.parametrize("role", ["package", "dependency"])
async def test_shared_host_inode_refused_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str, role: str
) -> None:
    package, dependency = tmp_path / "package", tmp_path / "dependency"
    package.mkdir()
    dependency.mkdir()
    entrypoint = package / "main.py"
    entrypoint.write_text("pass", encoding="ascii")
    sentinel = tmp_path / "host-private"
    sentinel.write_text("synthetic-private-content", encoding="ascii")
    mounted = package if role == "package" else dependency
    os.link(sentinel, mounted / "shared-inode")
    spawns = 0

    async def spawn(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawns
        spawns += 1
        raise OSError("unsafe mount reached process factory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    target = PluginPackageExecutionTarget(
        root=package, entrypoint=entrypoint, dependency_root=dependency
    )
    runner = PythonSubprocessPluginPackageRunner()
    identity = UUID("77777777-7777-4777-8777-777777777777")
    with pytest.raises(RuntimeCapabilityError, match="unsafe filesystem"):
        if interface == "preflight":
            await runner.preflight(target=target)
        else:
            capability = PluginCapabilityRequest(
                id="mount.inspect",
                adapter="native_test",
                permission_class="test.execute",
                sandbox_profile="local_process",
            )
            await runner.invoke(
                target=target,
                plugin=PluginResourceResponse(
                    id="mount-integrity",
                    name="Mount integrity",
                    status="running",
                    health="healthy",
                    capabilities=[capability],
                ),
                capability=capability,
                arguments={},
                context=PluginInvocationContext(
                    tenant_id=identity,
                    user_id=identity,
                    run_id=identity,
                    actor="tester",
                    idempotency_key="mount-integrity",
                ),
            )
    assert spawns == 0
    assert sentinel.read_text(encoding="ascii") == "synthetic-private-content"


def test_mount_inspection_uses_the_selected_time_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = tmp_path / "main.py"
    entrypoint.write_text("pass", encoding="ascii")
    clock = iter((1.0, 2.0))
    monkeypatch.setattr(runtime, "default_monotonic", lambda: next(clock))
    with pytest.raises(RuntimeCapabilityError, match="inspection timed out"):
        runtime._ensure_plugin_package_runner_target(
            PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
            inspection_timeout_seconds=0.5,
        )


def test_windows_cached_directory_metadata_is_not_link_count_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = tmp_path / "main.py"
    entrypoint.write_text("pass", encoding="ascii")

    class Entry:
        path = str(entrypoint)

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            pytest.fail("Cached DirEntry stat must not decide native hardlink identity")

    class Scan:
        def __enter__(self) -> Iterator[Entry]:
            return iter((Entry(),))

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(vars(runtime)["os"], "scandir", lambda _: Scan())
    runtime._ensure_plugin_package_runner_target(
        PluginPackageExecutionTarget(root=tmp_path, entrypoint=entrypoint),
        inspection_timeout_seconds=1,
    )
