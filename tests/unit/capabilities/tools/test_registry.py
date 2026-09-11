from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from agent_hub.capabilities.tools.registry import (
    CompositeCapabilityManifestSource,
    PluginConfigCapabilityManifestSource,
    ToolRegistry,
    create_builtin_tool_registry,
)
from agent_hub.plugins.dependency_policy import PluginPackageDependencyPolicy


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
                "policy_effect": "inherit",
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
                "policy_effect": "inherit",
                "available": False,
                "availability_reason": "plugin_disabled",
                "replay_safe": False,
                "aliases": (),
            },
        ),
    }


def test_plugin_manifest_source_maps_package_activation_reason_to_safe_token() -> None:
    source = PluginConfigCapabilityManifestSource(
        (
            SimpleNamespace(
                id="calendar",
                enabled=True,
                status="running",
                health="healthy",
                package_metadata=SimpleNamespace(
                    kind="adapter_package",
                    activation_state="blocked_unsupported_runtime",
                    activation_reason="plugin package dependencies are not supported by this runtime",
                ),
                capabilities=(
                    SimpleNamespace(
                        id="calendar.create_event",
                        adapter="calendar_python",
                        permission_class="calendar.write",
                        sandbox_profile="local_process",
                        replay_safe=False,
                        aliases=(),
                    ),
                ),
            ),
        )
    )

    capabilities = source.manifests()["capabilities"]
    assert isinstance(capabilities, tuple)
    capability = capabilities[0]
    assert isinstance(capability, dict)

    assert capability["available"] is False
    assert capability["availability_reason"] == "plugin_package_dependencies_unsupported"


def test_plugin_manifest_source_exposes_fail_closed_dependency_policy() -> None:
    source = PluginConfigCapabilityManifestSource(
        (
            SimpleNamespace(
                id="calendar",
                enabled=True,
                status="running",
                health="healthy",
                package_metadata=SimpleNamespace(
                    kind="adapter_package",
                    activation_state="blocked_unsupported_runtime",
                    activation_reason="plugin package dependencies are not supported by this runtime",
                    dependencies=(
                        SimpleNamespace(
                            kind="python",
                            source="pypi",
                            name="Requests",
                            version="2.32.0",
                        ),
                    ),
                ),
                capabilities=(
                    SimpleNamespace(
                        id="calendar.create_event",
                        adapter="calendar_python",
                        permission_class="calendar.write",
                        sandbox_profile="local_process",
                        replay_safe=False,
                        aliases=(),
                    ),
                ),
            ),
        )
    )

    capabilities = source.manifests()["capabilities"]
    assert isinstance(capabilities, tuple)
    capability = capabilities[0]
    assert isinstance(capability, dict)

    assert capability["package_dependency_lock"] == {
        "status": "unsupported",
        "install_policy": "not_configured",
        "cache_status": "missing",
        "allowlist_status": "missing",
        "sha256": hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest(),
        "dependency_count": 1,
        "dependencies": (
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            },
        ),
    }


def test_plugin_manifest_source_reports_offline_dependency_policy_readiness(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    (tmp_path / lock_hash).mkdir()
    source = PluginConfigCapabilityManifestSource(
        (
            SimpleNamespace(
                id="calendar",
                enabled=True,
                status="running",
                health="healthy",
                package_metadata=SimpleNamespace(
                    kind="adapter_package",
                    activation_state="blocked_unsupported_runtime",
                    activation_reason="plugin package dependencies are not supported by this runtime",
                    dependencies=(
                        SimpleNamespace(
                            kind="python",
                            source="pypi",
                            name="Requests",
                            version="2.32.0",
                        ),
                    ),
                ),
                capabilities=(
                    SimpleNamespace(
                        id="calendar.create_event",
                        adapter="calendar_python",
                        permission_class="calendar.write",
                        sandbox_profile="local_process",
                        replay_safe=False,
                        aliases=(),
                    ),
                ),
            ),
        ),
        dependency_policy=PluginPackageDependencyPolicy(
            install_policy="offline_cache",
            allowlist=frozenset({"python:pypi:requests==2.32.0"}),
            cache_dir=tmp_path,
        ),
    )

    capabilities = source.manifests()["capabilities"]
    assert isinstance(capabilities, tuple)
    capability = capabilities[0]
    assert isinstance(capability, dict)

    assert capability["package_dependency_lock"] == {
        "status": "unsupported",
        "install_policy": "offline_cache",
        "cache_status": "present",
        "allowlist_status": "allowed",
        "sha256": lock_hash,
        "dependency_count": 1,
        "dependencies": (
            {
                "kind": "python",
                "source": "pypi",
                "name": "requests",
                "version": "2.32.0",
            },
        ),
    }


def test_plugin_manifest_source_reports_offline_dependency_policy_gaps(
    tmp_path: Path,
) -> None:
    source = PluginConfigCapabilityManifestSource(
        (
            SimpleNamespace(
                id="calendar",
                enabled=True,
                status="running",
                health="healthy",
                package_metadata=SimpleNamespace(
                    kind="adapter_package",
                    activation_state="blocked_unsupported_runtime",
                    activation_reason="plugin package dependencies are not supported by this runtime",
                    dependencies=(
                        SimpleNamespace(
                            kind="python",
                            source="pypi",
                            name="Requests",
                            version="2.32.0",
                        ),
                    ),
                ),
                capabilities=(
                    SimpleNamespace(
                        id="calendar.create_event",
                        adapter="calendar_python",
                        permission_class="calendar.write",
                        sandbox_profile="local_process",
                        replay_safe=False,
                        aliases=(),
                    ),
                ),
            ),
        ),
        dependency_policy=PluginPackageDependencyPolicy(
            install_policy="offline_cache",
            allowlist=frozenset(),
            cache_dir=tmp_path,
        ),
    )

    capabilities = source.manifests()["capabilities"]
    assert isinstance(capabilities, tuple)
    capability = capabilities[0]
    assert isinstance(capability, dict)

    assert capability["package_dependency_lock"]["install_policy"] == "offline_cache"
    assert capability["package_dependency_lock"]["cache_status"] == "missing"
    assert capability["package_dependency_lock"]["allowlist_status"] == "not_allowed"


def test_composite_manifest_source_combines_sources_for_tenant() -> None:
    class TenantSource:
        def manifests_for_tenant(self, tenant_id: object) -> dict[str, object]:
            assert str(tenant_id) == "tenant-1"
            return {
                "schema_version": 1,
                "capabilities": (
                    {
                        "id": "search.web_search",
                        "kind": "mcp",
                        "adapter": "mcp_server",
                    },
                ),
            }

    class PlainSource:
        def manifests(self) -> dict[str, object]:
            return {
                "schema_version": 1,
                "capabilities": (
                    {
                        "id": "calendar.create_event",
                        "kind": "plugin",
                        "adapter": "plugin_runtime",
                    },
                ),
            }

    source = CompositeCapabilityManifestSource((TenantSource(), PlainSource()))

    assert source.manifests_for_tenant("tenant-1") == {
        "schema_version": 1,
        "capabilities": (
            {
                "id": "search.web_search",
                "kind": "mcp",
                "adapter": "mcp_server",
            },
            {
                "id": "calendar.create_event",
                "kind": "plugin",
                "adapter": "plugin_runtime",
            },
        ),
    }
