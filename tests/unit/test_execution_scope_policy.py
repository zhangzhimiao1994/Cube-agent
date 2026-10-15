from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.mcp.client import SseMcpClient, StreamableHttpMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind
from agent_hub.plugins.runtime import (
    LocalCommandPluginAdapter,
    PluginAdapter,
    PluginInvocationContext,
)
from agent_hub.process_namespace import ProcessNamespaceUnavailable
from agent_hub.runtime.contracts import JsonValue

if TYPE_CHECKING:
    from agent_hub.execution_scope_policy import ExecutionScopePolicy

_A = UUID("11111111-1111-4111-8111-111111111111")
_B = UUID("22222222-2222-4222-8222-222222222222")


class _Adapter(PluginAdapter, Protocol):
    async def preflight(self, *, plugin: PluginResourceResponse) -> None: ...

    def descriptor(self) -> Mapping[str, JsonValue]: ...


def _load(path: Path, protected_paths: tuple[Path, ...] = ()) -> ExecutionScopePolicy:
    name = "agent_hub.execution_scope_policy"
    assert importlib.util.find_spec(name) is not None, "execution scope policy is missing"
    module = importlib.import_module(name)
    return cast("ExecutionScopePolicy", module.load_execution_scope_policy(path, protected_paths))


def _write(path: Path, document: dict[str, object]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _bindings(document: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], document["bindings"])


@pytest.fixture
def document(tmp_path: Path) -> dict[str, object]:
    for name in ("a", "b", "runtime"):
        (tmp_path / name).mkdir()
    for name in ("bwrap", "program", "native-probe", "docker"):
        (tmp_path / name).write_bytes(b"trusted executable fixture")
    return {
        "schema_version": 1,
        "bubblewrap_executable": str(tmp_path / "bwrap"),
        "runtime_readonly_paths": [str(tmp_path / "runtime")],
        "native_probe_program": str(tmp_path / "native-probe"),
        "bindings": [
            {
                "kind": kind,
                "tenant_id": str(tenant),
                "source_id": "shared",
                "working_directory": str(tmp_path / workspace),
                "approved_programs": [str(tmp_path / "program")],
            }
            for kind, tenant, workspace in (("plugin", _A, "a"), ("mcp", _B, "b"))
        ],
    }


@pytest.fixture
def linux_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    for key in ("PATHEXT", "SystemRoot", "COMSPEC"):
        monkeypatch.delenv(key, raising=False)


def _plugin(tmp_path: Path, **resource_config: JsonValue) -> PluginResourceResponse:
    return PluginResourceResponse(
        id="shared",
        name="Scoped CLI",
        status="running",
        health="unknown",
        timeout_seconds=0.02,
        resource_config={"command": str(tmp_path / "program"), **resource_config},
        capabilities=[
            PluginCapabilityRequest(
                id="test.echo",
                adapter="local_command",
                permission_class="test.exec",
                sandbox_profile="local_process",
            )
        ],
    )


def _server(tmp_path: Path, tenant: UUID = _B, source_id: str = "shared") -> McpServerDefinition:
    program = str(tmp_path / "program")
    return McpServerDefinition(
        tenant_id=tenant,
        id=source_id,
        transport=McpTransportKind.STDIO,
        command=program,
        executable_allowlist=(program,),
    )


def _adapter(policy: ExecutionScopePolicy, plugin: PluginResourceResponse) -> _Adapter:
    return cast(_Adapter, policy.plugin_adapters(_A, plugin)["local_command"])


def test_loads_real_json_with_typed_immutable_fields(
    tmp_path: Path, document: dict[str, object]
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    assert policy.schema_version == 1
    assert policy.bubblewrap_executable == tmp_path / "bwrap"
    assert policy.runtime_readonly_paths == (tmp_path / "runtime",)
    assert policy.native_probe_program == tmp_path / "native-probe"
    assert isinstance(policy.bindings, tuple)
    assert policy.bindings[0].tenant_id == _A
    assert policy.bindings[0].approved_programs == (tmp_path / "program",)
    with pytest.raises(ValidationError):
        policy.schema_version = 2  # type: ignore[assignment]


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "bubblewrap_executable",
        "runtime_readonly_paths",
        "native_probe_program",
        "bindings",
    ],
)
def test_required_fields(tmp_path: Path, document: dict[str, object], field: str) -> None:
    del document[field]
    with pytest.raises(ValidationError):
        _load(_write(tmp_path / "policy.json", document))


@pytest.mark.parametrize("version", [0, 2, True, 1.0, "1", None])
def test_strict_schema_version(
    tmp_path: Path, document: dict[str, object], version: object
) -> None:
    document["schema_version"] = version
    with pytest.raises(ValidationError):
        _load(_write(tmp_path / "policy.json", document))


@pytest.mark.parametrize("nested", [False, True])
def test_rejects_extra_fields(tmp_path: Path, document: dict[str, object], nested: bool) -> None:
    target = _bindings(document)[0] if nested else document
    target["untrusted_override"] = "ignored?"
    with pytest.raises(ValidationError):
        _load(_write(tmp_path / "policy.json", document))


@pytest.mark.parametrize(
    "field",
    [
        "bubblewrap_executable",
        "runtime_readonly_paths",
        "native_probe_program",
        "working_directory",
        "approved_programs",
    ],
)
def test_rejects_relative_paths(tmp_path: Path, document: dict[str, object], field: str) -> None:
    target = (
        _bindings(document)[0] if field in {"working_directory", "approved_programs"} else document
    )
    target[field] = (
        ["relative"] if field.endswith("paths") or field == "approved_programs" else "relative"
    )
    with pytest.raises(ValidationError, match="absolute"):
        _load(_write(tmp_path / "policy.json", document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "shell"),
        ("tenant_id", "not-a-uuid"),
        ("source_id", ""),
        ("source_id", "   "),
        ("source_id", 42),
        ("approved_programs", []),
    ],
)
def test_invalid_binding(
    tmp_path: Path, document: dict[str, object], field: str, value: object
) -> None:
    _bindings(document)[0][field] = value
    with pytest.raises(ValidationError):
        _load(_write(tmp_path / "policy.json", document))


def test_duplicate_binding_rejected(tmp_path: Path, document: dict[str, object]) -> None:
    _bindings(document).append(dict(_bindings(document)[0]))
    with pytest.raises(ValidationError, match="[Dd]uplicate"):
        _load(_write(tmp_path / "policy.json", document))


def test_malformed_json_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text('{"schema_version":1,', encoding="utf-8")
    with pytest.raises(ValueError):
        _load(path)


def test_load_never_creates_missing_paths(tmp_path: Path, document: dict[str, object]) -> None:
    missing = tmp_path / "missing" / "workspace"
    _bindings(document)[0]["working_directory"] = str(missing)
    path = _write(tmp_path / "policy.json", document)
    original = path.read_bytes()
    with pytest.raises(ProcessNamespaceUnavailable):
        _load(path)
    assert not missing.parent.exists()
    assert path.read_bytes() == original
    with pytest.raises(FileNotFoundError):
        _load(tmp_path / "not-created.json")
    assert not (tmp_path / "not-created.json").exists()


@pytest.mark.parametrize("protected_kind", ["configured", "other-workspace", "parent"])
def test_protected_mounts_rejected(
    tmp_path: Path,
    document: dict[str, object],
    protected_kind: str,
) -> None:
    protected = tmp_path / "control"
    protected.mkdir()
    selected = {"configured": protected, "other-workspace": tmp_path / "b", "parent": tmp_path}
    document["runtime_readonly_paths"] = [str(selected[protected_kind])]
    with pytest.raises(ProcessNamespaceUnavailable, match="protected storage"):
        _load(_write(tmp_path / "policy.json", document), (protected,))


def test_tenant_kind_source_selection_and_unknown_denial(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(tmp_path)
    assert set(policy.plugin_adapters(_A, plugin)) == {"local_command"}
    assert policy.plugin_adapters(_B, plugin) == {}
    assert policy.plugin_adapters(_A, plugin.model_copy(update={"id": "unknown"})) == {}
    for tenant, source_id in ((_A, "shared"), (_B, "unknown")):
        with pytest.raises(ProcessNamespaceUnavailable, match="binding"):
            policy.mcp_client_factory(_server(tmp_path, tenant, source_id))


@pytest.mark.parametrize(
    ("transport", "client_type"),
    [
        (McpTransportKind.SSE, SseMcpClient),
        (McpTransportKind.STREAMABLE_HTTP, StreamableHttpMcpClient),
    ],
)
def test_http_mcp_does_not_require_binding(
    tmp_path: Path,
    document: dict[str, object],
    transport: McpTransportKind,
    client_type: type,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    server = McpServerDefinition(
        tenant_id=_A,
        id="unknown",
        transport=transport,
        url="https://mcp.example.com/rpc",
        domain_allowlist=("mcp.example.com",),
    )
    assert isinstance(policy.mcp_client_factory(server), client_type)


class _Process:
    def __init__(self, exit_code: int | None = 0, *, output: bool = False) -> None:
        self.returncode = exit_code
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None
        self.kills = 0
        self.waits = 0
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        if exit_code is not None:
            self.released.set()
        if output:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"scoped output")
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

    async def wait(self) -> int:
        self.waits += 1
        self.started.set()
        await self.released.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        if self.returncode is None:
            self.returncode = -9
        self.released.set()


@pytest.mark.parametrize("outcome", ["success", "exit", "timeout", "cancel", "wait-error", "spawn"])
async def test_native_preflight_probe_and_cleanup(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
    outcome: str,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(tmp_path)
    process = _Process(7 if outcome == "exit" else None if outcome in {"timeout", "cancel"} else 0)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap"), "host fallback"
        calls.append((argv, kwargs))
        if outcome == "spawn":
            raise OSError("namespace unavailable")
        return cast(asyncio.subprocess.Process, process)

    if outcome == "wait-error":

        async def wait_once_failed() -> int:
            process.waits += 1
            if process.waits == 1:
                raise OSError("wait failed")
            return 0

        process.wait = wait_once_failed  # type: ignore[method-assign]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(_adapter(policy, plugin).preflight(plugin=plugin))
    if outcome == "cancel":
        await asyncio.wait_for(process.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "success":
        await task
    else:
        with pytest.raises(RuntimeCapabilityError, match="probe"):
            await task
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[-2:] == ("--", str(tmp_path / "native-probe"))
    assert "--unshare-net" in argv
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert plugin.health == "unknown"
    if outcome not in {"success", "spawn"}:
        assert process.kills == 1
        assert process.waits >= 2


@pytest.mark.parametrize("command", ["native-probe", "docker"])
async def test_unapproved_user_command_cannot_use_probe_approval(
    tmp_path: Path,
    document: dict[str, object],
    command: str,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(tmp_path, command=str(tmp_path / command))
    with pytest.raises(RuntimeCapabilityError, match="approved"):
        await _adapter(policy, plugin).preflight(plugin=plugin)


@pytest.mark.parametrize("field", ["required_env_any", "env_passthrough", "model"])
async def test_unsupported_environment_and_model_requirements_denied(
    tmp_path: Path,
    document: dict[str, object],
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-only-not-a-real-secret")
    value: JsonValue = "remote-model" if field == "model" else ("MODEL_API_KEY",)
    plugin = _plugin(tmp_path, **{field: value})
    policy = _load(_write(tmp_path / "policy.json", document))
    with pytest.raises(RuntimeCapabilityError):
        await _adapter(policy, plugin).preflight(plugin=plugin)


async def test_normal_preflight_runs_before_native_probe(
    tmp_path: Path, document: dict[str, object]
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(tmp_path, required_commands=(str(tmp_path / "does-not-exist"),))
    with pytest.raises(RuntimeCapabilityError, match="command unavailable"):
        await _adapter(policy, plugin).preflight(plugin=plugin)


async def test_docker_probe_and_native_probe_share_scope_factory(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    _bindings(document)[0]["approved_programs"] = [
        str(tmp_path / "program"),
        str(tmp_path / "docker"),
    ]
    policy = _load(_write(tmp_path / "policy.json", document))
    calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        calls.append(argv)
        return cast(asyncio.subprocess.Process, _Process())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    plugin = _plugin(tmp_path, required_commands=(str(tmp_path / "docker"),))
    await _adapter(policy, plugin).preflight(plugin=plugin)
    assert calls[0][-5:] == (
        "--",
        str(tmp_path / "docker"),
        "info",
        "--format",
        "{{.ServerVersion}}",
    )
    assert calls[1][-2:] == ("--", str(tmp_path / "native-probe"))


async def test_factory_cache_keeps_initial_workspace_identity(
    tmp_path: Path,
    document: dict[str, object],
    linux_environment: None,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(tmp_path)
    first = _adapter(policy, plugin)
    (tmp_path / "a").rename(tmp_path / "original-a")
    (tmp_path / "a").mkdir()
    for adapter in (first, _adapter(policy, plugin)):
        with pytest.raises(RuntimeCapabilityError, match="probe") as caught:
            await adapter.preflight(plugin=plugin)
        assert isinstance(caught.value.__cause__, ProcessNamespaceUnavailable)
        assert "identity" in str(caught.value.__cause__)


async def test_real_delegate_invoke_and_descriptor(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        calls.append(argv)
        return cast(asyncio.subprocess.Process, _Process(output=True))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    plugin = _plugin(tmp_path)
    adapter = _adapter(policy, plugin)
    assert adapter.descriptor() == LocalCommandPluginAdapter().descriptor()
    context = PluginInvocationContext(_A, _A, _A, "tester", "request")
    result = await adapter.invoke(
        plugin=plugin,
        capability=plugin.capabilities[0],
        arguments={"args": ("literal argument",)},
        context=context,
    )
    assert result["ok"] is True
    assert result["stdout"] == "scoped output"
    assert calls[0][-3:] == ("--", str(tmp_path / "program"), "literal argument")
    with pytest.raises(RuntimeCapabilityError, match="scope"):
        await adapter.invoke(
            plugin=plugin,
            capability=plugin.capabilities[0],
            arguments={},
            context=PluginInvocationContext(_B, _B, _B, "tester", "other-tenant"),
        )
    assert len(calls) == 1


async def test_mcp_factory_routes_by_tenant_server_and_retains_identity(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        calls.append(argv)
        raise OSError("controlled bottom-level spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    server = _server(tmp_path)
    for _ in range(2):
        with pytest.raises(OSError, match="controlled"):
            await policy.mcp_client_factory(server).health(server)
    assert len(calls) == 2
    assert calls[0][calls[0].index("--chdir") + 1] == str(tmp_path / "b")
    (tmp_path / "b").rename(tmp_path / "original-b")
    (tmp_path / "b").mkdir()
    with pytest.raises(ProcessNamespaceUnavailable, match="identity"):
        await policy.mcp_client_factory(server).health(server)
    assert len(calls) == 2


def test_mcp_cannot_use_native_probe_as_user_command(
    tmp_path: Path, document: dict[str, object]
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    server = _server(tmp_path).model_copy(update={"command": str(tmp_path / "native-probe")})
    with pytest.raises(ProcessNamespaceUnavailable, match="approved"):
        policy.mcp_client_factory(server)


async def test_resource_config_cannot_override_policy_scope(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(
        tmp_path,
        working_directory=str(tmp_path / "b"),
        approved_programs=(str(tmp_path / "docker"),),
    )
    calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        calls.append(argv)
        return cast(asyncio.subprocess.Process, _Process())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _adapter(policy, plugin).preflight(plugin=plugin)
    assert calls[0][calls[0].index("--chdir") + 1] == str(tmp_path / "a")
    assert str(tmp_path / "b") not in calls[0]
    plugin.resource_config["command"] = str(tmp_path / "docker")
    with pytest.raises(RuntimeCapabilityError, match="approved"):
        await _adapter(policy, plugin).preflight(plugin=plugin)
    assert len(calls) == 1


async def test_command_name_is_resolved_before_policy_approval(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    (tmp_path / "program").chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    policy = _load(_write(tmp_path / "policy.json", document))
    calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        calls.append(argv)
        return cast(asyncio.subprocess.Process, _Process())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    plugin = _plugin(tmp_path, command="program")
    await _adapter(policy, plugin).preflight(plugin=plugin)
    plugin.resource_config["command"] = "docker"
    with pytest.raises(RuntimeCapabilityError, match="approved"):
        await _adapter(policy, plugin).preflight(plugin=plugin)
    assert len(calls) == 1


async def test_probe_wait_is_capped_at_five_seconds(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    process = _Process(None)

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    plugin = _plugin(tmp_path).model_copy(update={"timeout_seconds": 120.0})
    loop = asyncio.get_running_loop()
    start = loop.time()
    async with asyncio.timeout(8):
        with pytest.raises(RuntimeCapabilityError, match="probe") as caught:
            await _adapter(policy, plugin).preflight(plugin=plugin)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert 4.5 <= loop.time() - start < 8
    assert process.kills == 1 and process.waits >= 2


async def test_same_tenant_different_server_uses_distinct_workspace(
    tmp_path: Path,
    document: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    linux_environment: None,
) -> None:
    (tmp_path / "c").mkdir()
    other = dict(_bindings(document)[1])
    other.update(source_id="other", working_directory=str(tmp_path / "c"))
    _bindings(document).append(other)
    policy = _load(_write(tmp_path / "policy.json", document))
    calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert argv[0] == str(tmp_path / "bwrap")
        calls.append(argv)
        raise OSError("controlled bottom-level spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for source_id in ("shared", "other"):
        server = _server(tmp_path, _B, source_id)
        with pytest.raises(OSError, match="controlled"):
            await policy.mcp_client_factory(server).health(server)
    assert [argv[argv.index("--chdir") + 1] for argv in calls] == [
        str(tmp_path / "b"),
        str(tmp_path / "c"),
    ]


@pytest.mark.parametrize("kind", ["plugin", "mcp"])
def test_explicit_same_tenant_bindings_can_share_one_project(
    tmp_path: Path, document: dict[str, object], kind: str
) -> None:
    first = _bindings(document)[0]
    second = {**first, "kind": kind, "source_id": "second-tool"}
    _bindings(document).append(second)

    policy = _load(_write(tmp_path / "policy.json", document))

    for binding in (policy.bindings[0], policy.bindings[2]):
        factory = policy._factories[binding.key]
        assert (tmp_path / "a", tmp_path / "a") in factory._validated_mounts()
        assert tmp_path / "b" in factory._protected_paths
        assert tmp_path / "a" not in factory._protected_paths


def test_same_project_is_not_shared_across_tenants(
    tmp_path: Path, document: dict[str, object]
) -> None:
    _bindings(document)[1]["working_directory"] = _bindings(document)[0]["working_directory"]
    with pytest.raises(ProcessNamespaceUnavailable, match="protected"):
        _load(_write(tmp_path / "policy.json", document))


def test_same_tenant_nested_workspace_does_not_gain_shared_project_exception(
    tmp_path: Path, document: dict[str, object]
) -> None:
    nested = tmp_path / "a" / "nested"
    nested.mkdir()
    first = _bindings(document)[0]
    _bindings(document).append(
        {**first, "source_id": "nested-tool", "working_directory": str(nested)}
    )
    with pytest.raises(ProcessNamespaceUnavailable, match="protected"):
        _load(_write(tmp_path / "policy.json", document))


async def test_plugin_credentials_rejected(tmp_path: Path, document: dict[str, object]) -> None:
    policy = _load(_write(tmp_path / "policy.json", document))
    plugin = _plugin(tmp_path).model_copy(update={"credential_ref": "test-only-reference"})
    with pytest.raises(RuntimeCapabilityError, match="credentials"):
        await _adapter(policy, plugin).preflight(plugin=plugin)
