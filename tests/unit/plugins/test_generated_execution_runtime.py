"""Generated execution plumbing; factories are inert, native seals tested separately."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins import runtime
from agent_hub.plugins.execution_authority import (
    GeneratedExecutionAuthority,
    PackageExecutionMember,
)
from agent_hub.runtime.contracts import JsonValue

IDENTITY = UUID("77777777-7777-4777-8777-777777777777")


def authority() -> GeneratedExecutionAuthority:
    return GeneratedExecutionAuthority(
        tenant_id=IDENTITY, plugin_id="generated-test", registration_id=IDENTITY,
        archive_sha256="a" * 64, report_id="gs-" + "b" * 64, report_sha256="c" * 64,
        members=(PackageExecutionMember("main.py", 3, "d" * 64),),
    )


def fixture(tmp_path: Path) -> tuple[runtime.PluginPackageExecutionTarget, PluginResourceResponse,
                                      PluginCapabilityRequest, runtime.PluginInvocationContext]:
    entrypoint = tmp_path / "main.py"
    entrypoint.write_bytes(b"abc")
    proof = authority()
    target = runtime.PluginPackageExecutionTarget(
        root=tmp_path, entrypoint=entrypoint, execution_authority=proof,
    )
    capability = PluginCapabilityRequest(id="demo.run", adapter="python_subprocess_v1")
    plugin = PluginResourceResponse(id="generated-test", name="Test", content_sha256="a" * 64,
                                    status="running", health="healthy")
    context = runtime.PluginInvocationContext(
        tenant_id=IDENTITY, user_id=IDENTITY, run_id=IDENTITY, actor="test",
        idempotency_key="generated-test", execution_authority=proof,
    )
    return target, plugin, capability, context


@pytest.mark.parametrize("backend", ["none", "custom"])
async def test_generated_target_cannot_fall_back_to_host_or_custom_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str,
) -> None:
    target, plugin, capability, context = fixture(tmp_path)
    calls = 0

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        raise AssertionError("generated host fallback")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    launcher = None if backend == "none" else cast(Any, SimpleNamespace(argv=lambda **kw: ("unsafe",)))
    runner = runtime.PythonSubprocessPluginPackageRunner(process_launcher=launcher)
    with pytest.raises(RuntimeCapabilityError, match="immutable execution"):
        await runner.invoke(target=target, plugin=plugin, capability=capability,
                            arguments={}, context=context)
    assert calls == 0


def test_sealed_launcher_uses_fd_tree_without_host_package_bind(tmp_path: Path) -> None:
    root = tmp_path / "private-view"
    view = cast(Any, SimpleNamespace(files=(SimpleNamespace(path="lib/main.py", fd=97),), pass_fds=(97,)))
    target = runtime.PluginPackageExecutionTarget(
        root=root, entrypoint=root / "lib/main.py", execution_authority=authority(), sealed_view=view,
    )
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(tmp_path / "runtime",),
    )
    for argv in (launcher.argv(python_executable="/python", target=target),
                 launcher.probe_argv(python_executable="/python", target=target)):
        triples = tuple(zip(argv, argv[1:], argv[2:]))
        assert ("--ro-bind-data", "97", str(root / "lib/main.py")) in triples
        assert ("--ro-bind", str(root), str(root)) not in triples
        assert ("--tmpfs", str(root)) in tuple(pairwise(argv))
        assert ("--remount-ro", str(root)) in tuple(pairwise(argv))


def test_host_alias_cannot_redirect_runtime_mount_over_private_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, trusted = tmp_path / "private-view", tmp_path / "trusted-runtime"
    alias = root / "lib"
    original = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return trusted if path == alias else original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    view = cast(Any, SimpleNamespace(files=(SimpleNamespace(path="lib/main.py", fd=97),), pass_fds=(97,)))
    target = runtime.PluginPackageExecutionTarget(
        root=root, entrypoint=root / "lib/main.py", execution_authority=authority(), sealed_view=view,
    )
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(alias,),
    )
    with pytest.raises(RuntimeCapabilityError, match="overlaps protected storage"):
        launcher.argv(python_executable="/python", target=target)


async def test_generated_runner_passes_and_owns_sealed_fds_until_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, plugin, capability, context = fixture(tmp_path)
    events: list[str] = []
    view = cast(Any, SimpleNamespace(files=(SimpleNamespace(path="main.py", fd=97),), pass_fds=(97,)))

    @contextmanager
    def capture(root: Path, members: tuple[PackageExecutionMember, ...], **kwargs: object) -> Iterator[Any]:
        assert root == target.root and members == authority().members
        events.append("capture")
        try:
            yield view
        finally:
            events.append("closed")

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        assert kwargs["pass_fds"] == (97,)
        assert kwargs["cwd"] != target.root
        assert str(target.root) not in argv
        events.append("spawn")
        raise OSError("controlled spawn failure")

    monkeypatch.setattr(runtime, "capture_sealed_package", capture)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(tmp_path / "runtime",),
    )
    runner = runtime.PythonSubprocessPluginPackageRunner(process_launcher=launcher)
    with pytest.raises(RuntimeCapabilityError, match="Plugin tool failed"):
        await runner.invoke(target=target, plugin=plugin, capability=capability,
                            arguments={}, context=context)
    assert events == ["capture", "spawn", "closed"]


@pytest.mark.parametrize("change", ["tenant", "plugin", "archive", "context", "dependencies"])
async def test_generated_authority_mismatch_is_rejected_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    target, plugin, capability, context = fixture(tmp_path)
    proof = authority()
    if change == "tenant":
        proof = replace(proof, tenant_id=UUID(int=3))
    elif change == "plugin":
        proof = replace(proof, plugin_id="other-plugin")
    elif change == "archive":
        proof = replace(proof, archive_sha256="e" * 64)
    elif change == "context":
        context = replace(context, execution_authority=None)
    else:
        target = replace(target, dependency_root=tmp_path)
    target = replace(target, execution_authority=proof)
    if change != "context":
        context = replace(context, execution_authority=proof)

    def capture(*args: object, **kwargs: object) -> Any:
        pytest.fail("invalid authority reached byte capture")

    monkeypatch.setattr(runtime, "capture_sealed_package", capture)
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(tmp_path / "runtime",),
    )
    runner = runtime.PythonSubprocessPluginPackageRunner(process_launcher=launcher)
    with pytest.raises(RuntimeCapabilityError, match="immutable execution"):
        await runner.invoke(target=target, plugin=plugin, capability=capability,
                            arguments={}, context=context)


async def test_cancel_keeps_view_open_until_process_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, plugin, capability, context = fixture(tmp_path)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events: list[str] = []
    view = cast(Any, SimpleNamespace(files=(SimpleNamespace(path="main.py", fd=97),), pass_fds=(97,)))
    process = cast(asyncio.subprocess.Process, SimpleNamespace(returncode=None))

    @contextmanager
    def capture(*args: object, **kwargs: object) -> Iterator[Any]:
        try:
            yield view
        finally:
            events.append("closed")

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        return process

    async def communicate(*args: object, **kwargs: object) -> bytes:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cleanup(*args: object, **kwargs: object) -> None:
        assert events == []
        cleaning.set()
        await release.wait()
        events.append("reaped")

    monkeypatch.setattr(runtime, "capture_sealed_package", capture)
    monkeypatch.setattr(runtime, "_communicate_with_bounded_stdout", communicate)
    monkeypatch.setattr(runtime, "_cleanup_local_command", cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(tmp_path / "runtime",),
    )
    runner = runtime.PythonSubprocessPluginPackageRunner(process_launcher=launcher)
    task = asyncio.create_task(runner.invoke(target=target, plugin=plugin, capability=capability,
                                            arguments={}, context=context))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    assert not task.done() and events == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert events == ["reaped", "closed"]


async def test_scope_authority_reaches_adapter_but_not_wire_request() -> None:
    proof = authority()

    class Admin:
        async def list_plugins(self, *, tenant_id: UUID | None = None) -> tuple[PluginResourceResponse, ...]:
            return (PluginResourceResponse(
                id=proof.plugin_id, name="Test", enabled=True, status="running", health="healthy",
                capabilities=[PluginCapabilityRequest(id="demo.run", adapter="custom")],
            ),)

        @asynccontextmanager
        async def plugin_invocation_scope(self, plugin: PluginResourceResponse, *, tenant_id: UUID) -> Any:
            yield proof

    class Adapter:
        async def invoke(self, *, context: runtime.PluginInvocationContext, **kwargs: object) -> Mapping[str, JsonValue]:
            assert context.execution_authority is proof
            request = runtime._plugin_package_runner_request(
                plugin=cast(PluginResourceResponse, kwargs["plugin"]),
                capability=cast(PluginCapabilityRequest, kwargs["capability"]),
                arguments={}, context=context,
            )
            assert "execution_authority" not in str(request)
            assert proof.report_id not in str(request)
            return {"ok": True}

    service = runtime.RuntimePluginService(tenant_id=IDENTITY, admin_service=Admin(), adapters={"custom": Adapter()})
    result = await service.invoke(tenant_id=IDENTITY, user_id=IDENTITY, run_id=IDENTITY,
                                  actor="test", name="demo.run", arguments={}, idempotency_key="test")
    assert result == {"ok": True}
