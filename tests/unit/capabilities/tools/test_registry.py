from __future__ import annotations

from types import SimpleNamespace

from agent_hub.capabilities.tools.registry import (
    PluginConfigCapabilityManifestSource,
    ToolRegistry,
    create_builtin_tool_registry,
)


def test_registry_registers_builtin_tool_names() -> None:
    registry = create_builtin_tool_registry()

    assert registry.names() == (
        "calculator.evaluate",
        "document.generate_docx",
        "http.read",
        "presentation.generate_pptx",
        "project.generate_zip",
        "workspace.read",
    )


def test_registry_exposes_schemas_without_executor_callables() -> None:
    registry = ToolRegistry()
    registry.register("sample.tool", object())

    projected = registry.schemas()

    assert projected == ({"name": "sample.tool"},)


def test_registry_exposes_builtin_capability_manifests() -> None:
    registry = create_builtin_tool_registry()

    assert registry.manifests() == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "calculator.evaluate",
                "kind": "builtin",
                "adapter": "runtime_builtin",
                "permission_class": "calculator.evaluate",
                "sandbox_profile": "in_process",
                "replay_safe": True,
                "aliases": (),
            },
            {
                "id": "document.generate_docx",
                "kind": "builtin",
                "adapter": "runtime_builtin",
                "permission_class": "file.create",
                "sandbox_profile": "generated_artifact_store",
                "replay_safe": True,
                "aliases": (),
            },
            {
                "id": "http.read",
                "kind": "builtin",
                "adapter": "runtime_builtin",
                "permission_class": "network.read",
                "sandbox_profile": "http_read",
                "replay_safe": False,
                "aliases": (),
            },
            {
                "id": "presentation.generate_pptx",
                "kind": "builtin",
                "adapter": "runtime_builtin",
                "permission_class": "file.create",
                "sandbox_profile": "generated_artifact_store",
                "replay_safe": True,
                "aliases": (),
            },
            {
                "id": "project.generate_zip",
                "kind": "builtin",
                "adapter": "runtime_builtin",
                "permission_class": "file.create",
                "sandbox_profile": "generated_artifact_store",
                "replay_safe": True,
                "aliases": (),
            },
            {
                "id": "workspace.read",
                "kind": "builtin",
                "adapter": "runtime_builtin",
                "permission_class": "file.read",
                "sandbox_profile": "workspace_read",
                "replay_safe": True,
                "aliases": ("workspace_read",),
            },
        ),
    }


def test_registry_registers_pluggable_capability_manifest_without_callable() -> None:
    executor = object()
    registry = ToolRegistry()
    registry.register(
        "mcp.search",
        executor,
        kind="mcp",
        adapter="mcp_server",
        permission_class="mcp.call",
        sandbox_profile="remote_connector",
        replay_safe=False,
        aliases=("search_web",),
    )

    manifest = registry.manifests()

    assert manifest == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "mcp.search",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.call",
                "sandbox_profile": "remote_connector",
                "replay_safe": False,
                "aliases": ("search_web",),
            },
        ),
    }
    assert repr(executor) not in repr(manifest)


def test_plugin_manifest_source_marks_stopped_plugins_unavailable() -> None:
    source = PluginConfigCapabilityManifestSource(
        (
            SimpleNamespace(
                id="search",
                enabled=True,
                status="stopped",
                health="stopped",
                capabilities=(
                    SimpleNamespace(
                        id="search.web",
                        adapter="plugin_runtime",
                        permission_class="network.read",
                        sandbox_profile="remote_connector",
                        replay_safe=False,
                        aliases=("search_web",),
                    ),
                ),
            ),
            SimpleNamespace(
                id="calendar",
                enabled=False,
                status="disabled",
                health="disabled",
                capabilities=(
                    SimpleNamespace(
                        id="calendar.create_event",
                        adapter="plugin_runtime",
                        permission_class="calendar.write",
                        sandbox_profile="remote_connector",
                        replay_safe=False,
                        aliases=(),
                    ),
                ),
            ),
        )
    )

    assert source.manifests() == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "search.web",
                "kind": "plugin",
                "adapter": "plugin_runtime",
                "permission_class": "network.read",
                "sandbox_profile": "remote_connector",
                "available": False,
                "availability_reason": "plugin_stopped",
                "replay_safe": False,
                "aliases": ("search_web",),
            },
            {
                "id": "calendar.create_event",
                "kind": "plugin",
                "adapter": "plugin_runtime",
                "permission_class": "calendar.write",
                "sandbox_profile": "remote_connector",
                "available": False,
                "availability_reason": "plugin_disabled",
                "replay_safe": False,
                "aliases": (),
            },
        ),
    }
