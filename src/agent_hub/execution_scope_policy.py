from __future__ import annotations

import asyncio
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capability_installer.environment import CapabilityEnvironmentManager, _safe_component
from agent_hub.capability_installer.managed_scope import (
    ManagedEnvironmentPluginAdapter,
    WorkspaceScopeProcessFactory,
)
from agent_hub.mcp.runtime import McpClientFactory, build_scoped_mcp_client_factory
from agent_hub.mcp.types import McpServerDefinition
from agent_hub.plugins import runtime as plugin_runtime
from agent_hub.plugins.runtime import (
    LocalCommandPluginAdapter,
    PluginAdapter,
    PluginInvocationContext,
)
from agent_hub.process_namespace import BubblewrapStdioProcessFactory, ProcessNamespaceUnavailable
from agent_hub.runtime.contracts import JsonValue


def _absolute_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Execution scope path must be absolute")
    return path


type AbsolutePath = Annotated[Path, AfterValidator(_absolute_path)]
type BindingKey = tuple[Literal["plugin", "mcp"], UUID, str]


class ExecutionScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["plugin", "mcp"]
    tenant_id: UUID
    source_id: str = Field(min_length=1)
    working_directory: AbsolutePath
    approved_programs: tuple[AbsolutePath, ...]
    managed_environment_id: str | None = None

    @field_validator("managed_environment_id")
    @classmethod
    def _safe_managed_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_component(value, "managed environment id")
            if value.lower() == ".env":
                raise ValueError("Managed environment id is reserved")
        return value

    @model_validator(mode="after")
    def _managed_plugin_or_static_programs(self) -> ExecutionScopeBinding:
        if self.managed_environment_id is not None and self.kind != "plugin":
            raise ValueError("Managed environment binding is plugin only")
        if not self.approved_programs and self.managed_environment_id is None:
            raise ValueError("Execution scope requires approved programs or a managed environment")
        return self

    @field_validator("source_id")
    @classmethod
    def _nonblank_source_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Execution scope source_id must not be blank")
        return value

    @property
    def key(self) -> BindingKey:
        return self.kind, self.tenant_id, self.source_id


class ExecutionScopePolicy(BaseModel):
    """Trusted local bindings; loading establishes filesystem identities once."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    bubblewrap_executable: AbsolutePath
    runtime_readonly_paths: tuple[AbsolutePath, ...]
    native_probe_program: AbsolutePath
    bindings: tuple[ExecutionScopeBinding, ...]

    _factories: dict[BindingKey, BubblewrapStdioProcessFactory] = PrivateAttr(default_factory=dict)
    _adapters: dict[BindingKey, PluginAdapter] = PrivateAttr(default_factory=dict)
    _mcp_factory: McpClientFactory | None = PrivateAttr(default=None)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version_one(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Execution scope schema_version must be integer 1")
        return value

    @field_validator("bindings")
    @classmethod
    def _unique_bindings(
        cls,
        bindings: tuple[ExecutionScopeBinding, ...],
    ) -> tuple[ExecutionScopeBinding, ...]:
        keys = [binding.key for binding in bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate execution scope binding")
        return bindings

    def _initialize(
        self,
        protected_paths: Sequence[Path],
        environment_manager: CapabilityEnvironmentManager | None = None,
        authorized_workspace_root: Path | None = None,
    ) -> None:
        if any(binding.managed_environment_id is not None for binding in self.bindings):
            if environment_manager is None:
                raise ProcessNamespaceUnavailable("Managed execution scope requires a manager")
            if environment_manager.root not in protected_paths:
                protected_paths = (*protected_paths, environment_manager.root)
        if (
            environment_manager is not None
            and authorized_workspace_root is not None
            and (
                environment_manager.root.resolve().is_relative_to(
                    authorized_workspace_root.resolve()
                )
                or authorized_workspace_root.resolve().is_relative_to(
                    environment_manager.root.resolve()
                )
            )
        ):
            raise ProcessNamespaceUnavailable("Workspace and manager protected roots overlap")
        if (
            authorized_workspace_root is not None
            and authorized_workspace_root not in protected_paths
        ):
            protected_paths = (*protected_paths, authorized_workspace_root)
        protected = tuple(dict.fromkeys(_absolute_path(path) for path in protected_paths))
        for binding in self.bindings:
            programs = binding.approved_programs
            if binding.kind == "plugin":
                # Probe approval is deliberately absent from the user command allowlist.
                programs = tuple(dict.fromkeys((*programs, self.native_probe_program)))
            factory = WorkspaceScopeProcessFactory(
                bubblewrap_executable=self.bubblewrap_executable,
                working_directory=binding.working_directory,
                approved_programs=programs,
                runtime_readonly_paths=self.runtime_readonly_paths,
                authorized_workspace_root=authorized_workspace_root,
                protected_paths=(
                    *protected,
                    *(
                        other.working_directory
                        for other in self.bindings
                        if other.key != binding.key
                        and not (
                            other.tenant_id == binding.tenant_id
                            and other.working_directory == binding.working_directory
                        )
                    ),
                ),
            )
            self._factories[binding.key] = factory
            if binding.kind == "plugin":
                if binding.managed_environment_id is not None:
                    assert environment_manager is not None
                    self._adapters[binding.key] = ManagedEnvironmentPluginAdapter(
                        binding=binding,
                        manager=environment_manager,
                        ordinary_factory=factory,
                        bubblewrap_executable=self.bubblewrap_executable,
                        native_probe_program=self.native_probe_program,
                        runtime_readonly_paths=self.runtime_readonly_paths,
                        protected_paths=factory._protected_paths,
                        authorized_workspace_root=authorized_workspace_root,
                    )
                else:
                    self._adapters[binding.key] = _ScopedLocalCommandAdapter(
                        binding,
                        factory,
                        self.native_probe_program,
                    )
        self._mcp_factory = build_scoped_mcp_client_factory(self._mcp_process_factory)

    def plugin_adapters(
        self,
        tenant_id: UUID,
        plugin: PluginResourceResponse,
    ) -> Mapping[str, PluginAdapter]:
        adapter = self._adapters.get(("plugin", tenant_id, plugin.id))
        return {} if adapter is None else {"local_command": adapter}

    @property
    def mcp_client_factory(self) -> McpClientFactory:
        if self._mcp_factory is None:
            raise ProcessNamespaceUnavailable("Execution scope policy has not been loaded")
        return self._mcp_factory

    def _mcp_process_factory(self, server: McpServerDefinition) -> BubblewrapStdioProcessFactory:
        key: BindingKey = "mcp", server.tenant_id, server.id
        factory = self._factories.get(key)
        if factory is None:
            raise ProcessNamespaceUnavailable("MCP execution scope binding unavailable")
        binding = next(binding for binding in self.bindings if binding.key == key)
        if server.command is None or Path(server.command) not in binding.approved_programs:
            raise ProcessNamespaceUnavailable("MCP execution scope command is not approved")
        if server.oauth_token_ref is not None or server.header_secret_refs:
            raise ProcessNamespaceUnavailable("MCP credentials are unsupported in deny-net scope")
        return factory


def read_execution_scope_policy(path: Path) -> ExecutionScopePolicy:
    """Parse trusted operator JSON only; does not initialize or establish scope readiness."""
    return ExecutionScopePolicy.model_validate_json(path.read_bytes())


def load_execution_scope_policy(
    path: Path,
    protected_paths: Sequence[Path] = (),
    *,
    environment_manager: CapabilityEnvironmentManager | None = None,
    authorized_workspace_root: Path | None = None,
) -> ExecutionScopePolicy:
    """Read operator-owned JSON without provisioning configuration or workspaces."""
    policy = read_execution_scope_policy(path)
    policy._initialize(
        (*protected_paths, path.absolute()), environment_manager, authorized_workspace_root
    )
    return policy


class _ScopedLocalCommandAdapter:
    def __init__(
        self,
        binding: ExecutionScopeBinding,
        factory: BubblewrapStdioProcessFactory,
        native_probe_program: Path,
    ) -> None:
        self._binding = binding
        self._factory = factory
        self._native_probe_program = native_probe_program
        self._delegate = LocalCommandPluginAdapter(process_factory=factory)

    def _approved_command(self, command: str) -> None:
        resolved = plugin_runtime._resolve_local_command(command, shutil.which)
        if resolved is None or Path(resolved) not in self._binding.approved_programs:
            raise RuntimeCapabilityError("Plugin execution scope command is not approved")

    def _check_plugin(self, plugin: PluginResourceResponse) -> None:
        if plugin.id != self._binding.source_id:
            raise RuntimeCapabilityError("Plugin execution scope source mismatch")
        command = plugin_runtime._local_command_string(plugin.resource_config.get("command"))
        self._approved_command(command)
        if plugin.credential_ref is not None or any(
            plugin.resource_config.get(field) for field in ("model", "model_id", "model_provider")
        ):
            raise RuntimeCapabilityError(
                "Plugin model or credentials unsupported in deny-net scope"
            )
        for field in ("env_passthrough", "required_env_any"):
            if plugin_runtime._local_command_string_list(plugin.resource_config.get(field), field):
                raise RuntimeCapabilityError("Plugin environment requirements unsupported in scope")

    async def preflight(self, *, plugin: PluginResourceResponse) -> None:
        self._check_plugin(plugin)
        await self._delegate.preflight(plugin=plugin)
        for command in plugin_runtime._local_command_string_list(
            plugin.resource_config.get("required_commands"),
            "required_commands",
        ):
            self._approved_command(command)
        try:
            process = await self._factory(
                str(self._native_probe_program),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=plugin_runtime._local_command_environment(None),
            )
        except Exception as error:
            raise RuntimeCapabilityError("Plugin namespace probe unavailable") from error
        try:
            exit_code = await asyncio.wait_for(
                process.wait(), timeout=min(plugin.timeout_seconds, 5)
            )
            if exit_code != 0:
                raise RuntimeCapabilityError("Plugin namespace probe failed")
        except BaseException as error:
            await plugin_runtime._cleanup_local_command(process, [])
            if not isinstance(error, Exception):
                raise
            raise RuntimeCapabilityError("Plugin namespace probe failed") from error

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        if context.tenant_id != self._binding.tenant_id:
            raise RuntimeCapabilityError("Plugin execution scope tenant mismatch")
        self._check_plugin(plugin)
        return await self._delegate.invoke(
            plugin=plugin,
            capability=capability,
            arguments=arguments,
            context=context,
        )

    def descriptor(self) -> Mapping[str, JsonValue]:
        return self._delegate.descriptor()
