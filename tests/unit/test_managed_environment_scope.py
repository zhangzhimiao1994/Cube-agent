from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub import execution_scope_policy, process_namespace
from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentRecord,
    PythonEnvironmentSpec,
    tenant_environment_id,
)
from agent_hub.execution_scope_policy import ExecutionScopePolicy, load_execution_scope_policy
from agent_hub.plugins import runtime as plugin_runtime
from agent_hub.plugins.runtime import PluginAdapter, PluginInvocationContext
from agent_hub.process_namespace import ProcessNamespaceUnavailable
from agent_hub.runtime.contracts import JsonValue

_TENANT = UUID("11111111-1111-4111-8111-111111111111")
_OTHER = UUID("22222222-2222-4222-8222-222222222222")
_HOST_PLATFORM = sys.platform
_HOST_GETEUID = getattr(os, "geteuid", None)


@contextmanager
def _native_filesystem_identity() -> Iterator[None]:
    # Namespace mocks must not change real staging ownership or Windows ACL handling.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "platform", _HOST_PLATFORM)
        if _HOST_GETEUID is None:
            patch.delattr(os, "geteuid", raising=False)
        else:
            patch.setattr(os, "geteuid", _HOST_GETEUID)
        yield


def _build_python(
    manager: CapabilityEnvironmentManager, spec: PythonEnvironmentSpec
) -> EnvironmentRecord:
    with _native_filesystem_identity():
        return manager.build_python(spec)


class _Adapter(PluginAdapter, Protocol):
    async def preflight(self, *, plugin: PluginResourceResponse) -> None: ...


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        with _native_filesystem_identity():
            self.manager = CapabilityEnvironmentManager(
                root / "environments", cli_probe=lambda *_: None
            )
        for name in ("workspace", "runtime", "other-store"):
            (root / name).mkdir()
        for name in ("bwrap", "probe", "static"):
            (root / name).write_bytes(b"trusted fixture")
        self.binding: dict[str, object] = {
            "kind": "plugin",
            "tenant_id": str(_TENANT),
            "source_id": "source",
            "working_directory": str(root / "workspace"),
            "approved_programs": [],
            "managed_environment_id": "capability",
        }
        self.document: dict[str, object] = {
            "schema_version": 1,
            "bubblewrap_executable": str(root / "bwrap"),
            "runtime_readonly_paths": [str(root / "runtime")],
            "native_probe_program": str(root / "probe"),
            "bindings": [self.binding],
        }
        self.record: EnvironmentRecord | None = None

    @property
    def physical_id(self) -> str:
        return tenant_environment_id(_TENANT, "capability")

    def install(self, version: str = "v1", *, physical_id: str | None = None) -> EnvironmentRecord:
        artifact = self.root / f"artifact-{version}"
        artifact.write_bytes(version.encode())
        self.record = self.manager.deploy_cli(
            CliArtifactSpec(
                self.physical_id if physical_id is None else physical_id,
                version,
                artifact,
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "tool",
            )
        )
        return self.record

    def policy(
        self,
        *extra_protected: Path,
        manager: bool = True,
        authorized_workspace_root: Path | None = None,
    ) -> ExecutionScopePolicy:
        path = self.root / "policy.json"
        path.write_text(json.dumps(self.document), encoding="utf-8")
        kwargs = {"environment_manager": self.manager} if manager else {}
        return load_execution_scope_policy(
            path,
            (self.manager.root, self.root / "other-store", *extra_protected),
            **kwargs,
            authorized_workspace_root=authorized_workspace_root,
        )

    def plugin(self, **config: JsonValue) -> PluginResourceResponse:
        assert self.record is not None
        return PluginResourceResponse(
            id="source",
            name="Managed tool",
            status="running",
            health="unknown",
            timeout_seconds=0.1,
            resource_config={"command": str(self.record.path / self.record.executable), **config},
            capabilities=[
                PluginCapabilityRequest(
                    id="echo",
                    adapter="local_command",
                    permission_class="test.exec",
                    sandbox_profile="local_process",
                )
            ],
        )

    def adapter(self, policy: ExecutionScopePolicy, plugin: PluginResourceResponse) -> _Adapter:
        return cast(_Adapter, policy.plugin_adapters(_TENANT, plugin)["local_command"])


@pytest.fixture
def scope(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


class _Process:
    def __init__(self, *, pending: bool = False) -> None:
        self.returncode: int | None = None if pending else 0
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.kills = 0
        if not pending:
            self.stdout.feed_data(b"managed output")
            self.finish()

    def finish(self) -> None:
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.released.set()

    async def wait(self) -> int:
        self.started.set()
        await self.released.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.kills += 1


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    for key in ("PATHEXT", "SystemRoot", "COMSPEC"):
        monkeypatch.delenv(key, raising=False)

    async def create(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        calls.append(argv)
        return cast(asyncio.subprocess.Process, _Process())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return calls


async def _invoke(
    adapter: _Adapter, plugin: PluginResourceResponse, tenant: UUID = _TENANT
) -> Mapping[str, JsonValue]:
    return await adapter.invoke(
        plugin=plugin,
        capability=plugin.capabilities[0],
        arguments={"args": ("-I", "literal")},
        context=PluginInvocationContext(tenant, tenant, tenant, "test", "test"),
    )


def test_static_installed_command_reproduces_protected_path_failure(scope: _Fixture) -> None:
    record = scope.install()
    del scope.binding["managed_environment_id"]
    scope.binding["approved_programs"] = [str(record.path / record.executable)]
    with pytest.raises(ProcessNamespaceUnavailable, match="protected storage"):
        scope.policy(manager=False)


async def test_managed_opt_in_executes_exact_installed_version(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    record = scope.install()
    policy = scope.policy()
    plugin = scope.plugin()
    adapter = scope.adapter(policy, plugin)
    await adapter.preflight(plugin=plugin)
    assert (await _invoke(adapter, plugin))["stdout"] == "managed output"
    assert spawn[0][-2:] == ("--", str(scope.root / "probe"))
    assert spawn[1][-4:] == ("--", str(record.path / record.executable), "-I", "literal")
    for argv in spawn:
        assert argv[0] == str(scope.root / "bwrap")
        assert "--unshare-net" in argv
        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--ro-bind"]
        assert mounts.count(str(record.path)) == 1
        assert str(record.path / record.executable) not in mounts
        assert str(scope.manager.root) not in mounts
        assert str(record.path.parent) not in mounts
    assert scope.manager.references(scope.physical_id, record.version) == ()


async def test_load_before_install_is_lazy(scope: _Fixture, spawn: list[tuple[str, ...]]) -> None:
    policy = scope.policy()
    record = scope.install()
    plugin = scope.plugin()
    await scope.adapter(policy, plugin).preflight(plugin=plugin)
    assert str(record.path) in spawn[0]


@pytest.mark.parametrize("value", ["", ".", "..", "../escape", "a/b", "a\\b", " x", ".env"])
def test_managed_id_must_be_safe_component(scope: _Fixture, value: str) -> None:
    scope.binding["managed_environment_id"] = value
    with pytest.raises(ValidationError):
        scope.policy()


def test_plain_empty_binding_and_mcp_managed_are_invalid(scope: _Fixture) -> None:
    del scope.binding["managed_environment_id"]
    with pytest.raises(ValidationError):
        scope.policy()
    scope.binding.update(managed_environment_id="capability", kind="mcp")
    with pytest.raises(ValidationError):
        scope.policy()


def test_managed_requires_actual_operator_manager(scope: _Fixture) -> None:
    with pytest.raises(ProcessNamespaceUnavailable, match="manager"):
        scope.policy(manager=False)


@pytest.mark.parametrize("mount", ["root", "version", "parent", "store", "workspace"])
def test_ordinary_mounts_checked_against_full_protections(scope: _Fixture, mount: str) -> None:
    record = scope.install()
    paths = {
        "root": scope.manager.root,
        "version": record.path,
        "parent": scope.root,
        "store": scope.root / "other-store",
        "workspace": scope.manager.root / "workspace",
    }
    if mount == "workspace":
        paths[mount].mkdir()
        scope.binding["working_directory"] = str(paths[mount])
    else:
        scope.document["runtime_readonly_paths"] = [str(paths[mount])]
    with pytest.raises(ProcessNamespaceUnavailable, match="protected storage"):
        scope.policy()


@pytest.mark.parametrize("protection", ["ancestor", "version", "nested", "alias"])
async def test_other_overlapping_protections_fail_closed(
    scope: _Fixture, spawn: list[tuple[str, ...]], protection: str
) -> None:
    record = scope.install()
    extra = {
        "ancestor": scope.root,
        "version": record.path,
        "nested": record.path / "secrets",
        "alias": scope.manager.root / ".." / "environments",
    }
    with pytest.raises((ProcessNamespaceUnavailable, RuntimeCapabilityError)):
        policy = scope.policy(extra[protection])
        plugin = scope.plugin()
        await scope.adapter(policy, plugin).preflight(plugin=plugin)
    assert spawn == []


@pytest.mark.parametrize("change", ["version", "tamper", "command", "source", "tenant"])
async def test_revalidate_before_invoke(
    scope: _Fixture, spawn: list[tuple[str, ...]], change: str
) -> None:
    record = scope.install()
    plugin = scope.plugin()
    adapter = scope.adapter(scope.policy(), plugin)
    await adapter.preflight(plugin=plugin)
    if change == "version":
        scope.install("v2")
    elif change == "tamper":
        (record.path / record.executable).write_bytes(b"tampered")
    elif change == "command":
        plugin.resource_config["command"] = str(scope.root / "static")
    elif change == "source":
        plugin = plugin.model_copy(update={"id": "other"})
    with pytest.raises(RuntimeCapabilityError):
        await _invoke(adapter, plugin, _OTHER if change == "tenant" else _TENANT)
    assert len(spawn) == 1
    assert scope.manager.references(scope.physical_id, record.version) == ()


@pytest.mark.parametrize(
    "field",
    [
        "required_commands",
        "env_passthrough",
        "required_env_any",
        "model",
        "model_id",
        "model_provider",
        "credential_ref",
    ],
)
async def test_managed_rejects_unapproved_requirements_and_credentials(
    scope: _Fixture, spawn: list[tuple[str, ...]], field: str
) -> None:
    record = scope.install()
    value: JsonValue = (
        (str(record.path / record.executable),)
        if field == "required_commands"
        else (("SECRET",) if field in {"env_passthrough", "required_env_any"} else "secret")
    )
    plugin = scope.plugin(**{field: value})
    if field == "credential_ref":
        plugin = plugin.model_copy(update={"credential_ref": "credential"})
    with pytest.raises(RuntimeCapabilityError):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert spawn == []


async def test_reference_held_through_repeated_cancellation_and_drain(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = scope.install()
    plugin = scope.plugin().model_copy(update={"timeout_seconds": 30.0})
    adapter = scope.adapter(scope.policy(), plugin)
    process = _Process(pending=True)
    child_started = asyncio.Event()

    async def create(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert scope.manager.references(scope.physical_id, record.version)
        child_started.set()
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(_invoke(adapter, plugin))
    await asyncio.wait_for(child_started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(process.started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert scope.manager.references(scope.physical_id, record.version)
    assert not task.done()
    process.finish()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.kills == 1
    assert scope.manager.references(scope.physical_id, record.version)


async def test_windows_has_no_host_fallback(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope.install()
    monkeypatch.setattr(sys, "platform", "win32")
    plugin = scope.plugin()
    with pytest.raises(RuntimeCapabilityError):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert spawn == []


@pytest.mark.parametrize("script", [False, True])
async def test_python_record_adds_B_before_user_I(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, script: bool
) -> None:
    original = scope.install()
    directory = original.path / "bin"
    directory.mkdir()
    python = directory / "python"
    python.write_bytes(b"trusted python")
    command = directory / "console" if script else python
    if script:
        command.write_text(f"#!{python}\nprint('ok')\n", encoding="utf-8")
    record = replace(
        original,
        kind="python",
        executable=command.relative_to(original.path).as_posix(),
        attestation_sha256="a" * 64,
    )
    scope.record = record
    # Tree attestation is tested by the environment worker; isolate the adapter's record contract.
    monkeypatch.setattr(scope.manager, "active", lambda _: record)
    monkeypatch.setattr(scope.manager, "add_reference", lambda *_: None)
    monkeypatch.setattr(scope.manager, "remove_reference", lambda *_: None)
    plugin = scope.plugin()
    await _invoke(scope.adapter(scope.policy(), plugin), plugin)
    expected = ("--", str(command), *(("-B",) if not script else ()), "-I", "literal")
    assert spawn[0][-len(expected) :] == expected


async def test_legacy_python_record_requires_rebuild(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = replace(scope.install(), kind="python", attestation_sha256=None)
    monkeypatch.setattr(scope.manager, "active", lambda _: record)
    plugin = scope.plugin()
    with pytest.raises(RuntimeCapabilityError, match="rebuild required"):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert spawn == []


async def test_legacy_global_environment_is_never_selected(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    scope.install(physical_id="capability")
    plugin = scope.plugin()
    with pytest.raises(RuntimeCapabilityError):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert spawn == []


async def test_other_tenant_environment_is_never_selected(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    scope.install(physical_id=tenant_environment_id(_OTHER, "capability"))
    plugin = scope.plugin()
    with pytest.raises(RuntimeCapabilityError):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert spawn == []


@pytest.mark.parametrize("field", ["root", "version", "trust", "manager"])
def test_operator_binding_rejects_caller_environment_overrides(scope: _Fixture, field: str) -> None:
    scope.binding[field] = "untrusted"
    with pytest.raises(ValidationError):
        scope.policy()


async def test_static_required_command_is_operator_allowlist_only(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    scope.install()
    scope.binding["approved_programs"] = [str(scope.root / "static")]
    plugin = scope.plugin(required_commands=(str(scope.root / "static"),))
    await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert len(spawn) == 1
    plugin.resource_config["required_commands"] = (str(scope.root / "probe"),)
    with pytest.raises(RuntimeCapabilityError, match="operator approved"):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert len(spawn) == 1


async def test_ordinary_mount_identity_is_pinned_at_policy_load(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    scope.install()
    policy = scope.policy()
    runtime = scope.root / "runtime"
    runtime.rename(scope.root / "old-runtime")
    runtime.mkdir()
    plugin = scope.plugin()
    with pytest.raises((RuntimeCapabilityError, ProcessNamespaceUnavailable), match="identity"):
        await scope.adapter(policy, plugin).preflight(plugin=plugin)
    assert spawn == []


@pytest.mark.parametrize("outcome", ["spawn", "timeout", "cleanup-incomplete"])
async def test_exceptional_runner_retains_reference(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    record = scope.install()
    plugin = scope.plugin()
    process = _Process(pending=True)
    if outcome == "timeout":

        def kill() -> None:
            process.kills += 1
            process.finish()

        process.kill = kill  # type: ignore[method-assign]

    async def create(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert scope.manager.references(scope.physical_id, record.version)
        if outcome == "spawn":
            raise OSError("controlled namespace spawn failure")
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(RuntimeCapabilityError):
        await _invoke(scope.adapter(scope.policy(), plugin), plugin)
    assert scope.manager.references(scope.physical_id, record.version)
    if outcome != "spawn":
        assert process.kills >= 1


async def test_ambient_environment_is_not_forwarded(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope.install()
    monkeypatch.setenv("PATH", "untrusted-path")
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-secret")
    plugin = scope.plugin()
    await _invoke(scope.adapter(scope.policy(), plugin), plugin)
    argv = spawn[0]
    values = {
        argv[index + 1]: argv[index + 2]
        for index, argument in enumerate(argv)
        if argument == "--setenv"
    }
    assert values == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
    }
    assert "not-a-real-secret" not in argv


async def test_env_named_manager_root_has_no_exemption(
    tmp_path: Path, spawn: list[tuple[str, ...]]
) -> None:
    root = tmp_path / ".env"
    root.mkdir()
    scope = _Fixture(root)
    scope.install()
    plugin = scope.plugin()
    with pytest.raises(RuntimeCapabilityError, match=".env"):
        await scope.adapter(scope.policy(), plugin).preflight(plugin=plugin)
    assert spawn == []


async def test_attested_python_version_uses_manager_tree_validation(
    scope: _Fixture, spawn: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = scope.install()
    record = replace(original, kind="python", attestation_sha256="a" * 64)
    monkeypatch.setattr(scope.manager, "active", lambda _: record)
    monkeypatch.setattr(scope.manager, "add_reference", lambda *_: None)
    monkeypatch.setattr(scope.manager, "remove_reference", lambda *_: None)
    inspect = plugin_runtime._ensure_private_package_mount

    def reject_generic_python_tree(root: Path, *, deadline: float) -> None:
        if root == record.path:
            raise RuntimeCapabilityError("generic scanner rejects attested venv interpreter links")
        inspect(root, deadline=deadline)

    monkeypatch.setattr(
        process_namespace, "_ensure_private_package_mount", reject_generic_python_tree
    )
    plugin = scope.plugin()
    await _invoke(scope.adapter(scope.policy(), plugin), plugin)
    assert len(spawn) == 1


async def test_real_python_record_is_revalidated_on_invoke(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    def build_fixture(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["-m", "venv"]:
            staging = Path(argv[3])
            python = staging / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir()
            python.write_bytes(b"controlled interpreter")
            site = staging / "Lib/site-packages"
            site.mkdir(parents=True)
            (site / "module.py").write_bytes(b"VALUE = 1\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    scope.manager = CapabilityEnvironmentManager(scope.manager.root, runner=build_fixture)
    wheels = scope.root / "wheels"
    wheels.mkdir()
    lock = scope.root / "requirements.lock"
    lock.write_text("demo==1.0.0 --hash=sha256:" + "a" * 64 + "\n", encoding="ascii")
    record = _build_python(
        scope.manager,
        PythonEnvironmentSpec(scope.physical_id, "p1", lock, wheels, "module")
    )
    scope.record = record
    plugin = scope.plugin()
    adapter = scope.adapter(scope.policy(), plugin)
    await adapter.preflight(plugin=plugin)
    assert (await _invoke(adapter, plugin))["ok"] is True
    assert scope.manager.active(scope.physical_id) == record
    assert scope.manager.references(scope.physical_id, record.version) == ()
    (record.path / "Lib/site-packages/module.py").write_bytes(b"VALUE = 2\n")
    with pytest.raises(RuntimeCapabilityError, match="attestation"):
        await _invoke(adapter, plugin)
    assert len(spawn) == 2


def _project(scope: _Fixture, *, managed: bool) -> tuple[Path, Path]:
    root = scope.root / "projects"
    cwd = root / "project-a"
    cwd.mkdir(parents=True)
    (root / "project-b").mkdir()
    scope.binding["working_directory"] = str(cwd)
    if not managed:
        del scope.binding["managed_environment_id"]
        scope.binding["approved_programs"] = [str(scope.root / "static")]
    return root, cwd


def test_project_cwd_reproduces_full_root_protection(scope: _Fixture) -> None:
    root, _ = _project(scope, managed=False)
    with pytest.raises(ProcessNamespaceUnavailable, match="protected storage"):
        scope.policy(root, manager=False)


@pytest.mark.parametrize("managed", [False, True])
async def test_trusted_project_subdirectory_is_readonly_cwd_only(
    scope: _Fixture,
    spawn: list[tuple[str, ...]],
    managed: bool,
) -> None:
    record = scope.install()
    root, cwd = _project(scope, managed=managed)
    policy = scope.policy(root, authorized_workspace_root=root)
    plugin = scope.plugin(**({} if managed else {"command": str(scope.root / "static")}))
    adapter = scope.adapter(policy, plugin)
    await adapter.preflight(plugin=plugin)
    await _invoke(adapter, plugin)
    for argv in spawn:
        mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--ro-bind"]
        assert mounts.count(str(cwd)) == 1
        assert str(root) not in mounts
        assert str(root / "project-b") not in mounts
        assert "--bind" not in argv
        assert argv[argv.index("--chdir") + 1] == str(cwd)
    if managed:
        assert scope.manager.references(scope.physical_id, record.version) == ()


def test_read_policy_parses_managed_bindings_without_initializing_scope(scope: _Fixture) -> None:
    path = scope.root / "parse-only.json"
    scope.binding["working_directory"] = str(scope.root / "not-installed-workspace")
    scope.document["native_probe_program"] = str(scope.root / "not-installed-probe")
    path.write_text(json.dumps(scope.document), encoding="utf-8")
    policy = execution_scope_policy.read_execution_scope_policy(path)
    assert policy.bindings[0].managed_environment_id == "capability"
    assert policy.bindings[0].source_id == "source"
    with pytest.raises(ProcessNamespaceUnavailable, match="not been loaded"):
        _ = policy.mcp_client_factory
    assert not (scope.root / "not-installed-workspace").exists()


@pytest.mark.parametrize("managed", [False, True])
def test_trusted_workspace_cannot_mount_root_as_cwd(scope: _Fixture, managed: bool) -> None:
    root, _ = _project(scope, managed=managed)
    scope.binding["working_directory"] = str(root)
    with pytest.raises(ProcessNamespaceUnavailable, match="strict child"):
        scope.policy(root, authorized_workspace_root=root)


@pytest.mark.parametrize("mount", ["cwd", "root", "sibling", "program", "probe"])
def test_workspace_cwd_exception_never_authorizes_ordinary_mounts(
    scope: _Fixture, mount: str
) -> None:
    root, cwd = _project(scope, managed=False)
    target = {"cwd": cwd, "root": root, "sibling": root / "project-b"}.get(mount)
    if target is not None:
        scope.document["runtime_readonly_paths"] = [str(target)]
    else:
        program = cwd / "tool"
        program.write_bytes(b"not implicitly approved by cwd")
        if mount == "program":
            scope.binding["approved_programs"] = [str(program)]
        else:
            scope.document["native_probe_program"] = str(program)
    with pytest.raises(ProcessNamespaceUnavailable, match="protected storage"):
        scope.policy(root, authorized_workspace_root=root)


@pytest.mark.parametrize("protection", ["ancestor", "cwd", "nested", "root-alias"])
def test_other_workspace_protections_are_not_exempted(scope: _Fixture, protection: str) -> None:
    root, cwd = _project(scope, managed=False)
    extra = {
        "ancestor": scope.root,
        "cwd": cwd,
        "nested": cwd / "secrets",
        "root-alias": root / ".." / "projects",
    }[protection]
    with pytest.raises(ProcessNamespaceUnavailable, match="protected storage"):
        scope.policy(root, extra, authorized_workspace_root=root)


@pytest.mark.parametrize("overlap", ["equal", "manager-inside", "workspace-inside"])
def test_manager_and_workspace_roles_must_not_overlap(scope: _Fixture, overlap: str) -> None:
    workspace = {
        "equal": scope.manager.root,
        "manager-inside": scope.root,
        "workspace-inside": scope.manager.root / "projects",
    }[overlap]
    workspace.mkdir(exist_ok=True)
    with pytest.raises(ProcessNamespaceUnavailable, match="roots overlap"):
        scope.policy(workspace, authorized_workspace_root=workspace)


async def test_app_supplied_manager_root_is_not_duplicated(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    scope.install()
    plugin = scope.plugin()
    policy = scope.policy(scope.manager.root)
    await scope.adapter(policy, plugin).preflight(plugin=plugin)
    assert len(spawn) == 1
    factory = policy._factories[policy.bindings[0].key]
    assert factory._protected_paths.count(scope.manager.root) == 1


async def test_canonical_sibling_scope_cwds_remain_separate(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    root, _ = _project(scope, managed=False)
    other = {**scope.binding, "source_id": "other", "working_directory": str(root / "project-b")}
    scope.document["bindings"] = [scope.binding, other]
    policy = scope.policy(root, authorized_workspace_root=root)
    scope.install()
    plugin = scope.plugin(command=str(scope.root / "static"))
    await scope.adapter(policy, plugin).preflight(plugin=plugin)
    assert str(root / "project-b") not in spawn[0]


async def test_workspace_root_identity_is_pinned(
    scope: _Fixture, spawn: list[tuple[str, ...]]
) -> None:
    scope.install()
    root, cwd = _project(scope, managed=True)
    policy = scope.policy(root, authorized_workspace_root=root)
    root.rename(scope.root / "old-projects")
    cwd.mkdir(parents=True)
    plugin = scope.plugin()
    with pytest.raises((RuntimeCapabilityError, ProcessNamespaceUnavailable), match="identity"):
        await scope.adapter(policy, plugin).preflight(plugin=plugin)
    assert spawn == []


@pytest.mark.parametrize("alias", ["root", "cwd", "env"])
def test_workspace_authority_rejects_aliases(scope: _Fixture, alias: str) -> None:
    root, cwd = _project(scope, managed=False)
    authority = root
    if alias == "root":
        authority = root / ".." / "projects"
    elif alias == "cwd":
        scope.binding["working_directory"] = str(cwd / ".." / "project-a")
    else:
        hidden = root / ".env"
        hidden.mkdir()
        scope.binding["working_directory"] = str(hidden)
    with pytest.raises(ProcessNamespaceUnavailable, match="alias"):
        scope.policy(root, authorized_workspace_root=authority)
