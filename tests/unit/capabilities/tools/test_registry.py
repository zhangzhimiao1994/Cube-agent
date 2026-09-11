from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from agent_hub.capabilities.tools.registry import (
    CompositeCapabilityManifestSource,
    PluginConfigCapabilityManifestSource,
    ToolRegistry,
    create_builtin_tool_registry,
)
from agent_hub.plugins.dependency_policy import (
    PluginPackageDependencyPolicy,
    plugin_package_dependency_cache_signature_payload_sha256,
    plugin_package_dependency_lock,
)


def _artifact_origin(name: str, version: str, sha256: str) -> dict[str, str]:
    return {
        "type": "package_index",
        "index_url": f"https://pypi.org/simple/{name}/",
        "archive_url": f"https://files.pythonhosted.org/packages/{name}-{version}.whl",
        "archive_sha256": sha256,
    }


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
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    marker_bytes = b"READY = True\n"
    marker_path = cache_entry / "site-packages" / "dependency_cache_marker.py"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(marker_bytes)
    artifact_bytes = b"requests==2.32.0\n"
    artifact_path = cache_entry / "artifacts" / "requests-2.32.0.whl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    dependencies = [
        {
            "kind": "python",
            "source": "pypi",
            "name": "requests",
            "version": "2.32.0",
        }
    ]
    artifacts = [
        {
            "kind": "python",
            "source": "pypi",
            "name": "requests",
            "version": "2.32.0",
            "path": "artifacts/requests-2.32.0.whl",
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
            "origin": _artifact_origin(
                "requests",
                "2.32.0",
                hashlib.sha256(artifact_bytes).hexdigest(),
            ),
        }
    ]
    files = [
        {
            "path": "artifacts/requests-2.32.0.whl",
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
        },
        {
            "path": "site-packages/dependency_cache_marker.py",
            "sha256": hashlib.sha256(marker_bytes).hexdigest(),
            "size_bytes": len(marker_bytes),
        },
    ]
    lock = plugin_package_dependency_lock(
        (
            SimpleNamespace(
                kind="python",
                source="pypi",
                name="requests",
                version="2.32.0",
            ),
        )
    )
    assert lock is not None
    signature_sha256 = plugin_package_dependency_cache_signature_payload_sha256(
        lock,
        dependencies,
        files,
        artifacts,
        builder_id="agent-hub-offline-cache-builder",
    )
    assert signature_sha256 is not None
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": dependencies,
                "artifacts": artifacts,
                "files": files,
                "cache_signature": {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "builder_id": "agent-hub-offline-cache-builder",
                    "payload_sha256": signature_sha256,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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
            trusted_cache_builders=frozenset({"agent-hub-offline-cache-builder"}),
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


def test_plugin_manifest_source_requires_dependency_cache_manifest(
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"
    assert capability["package_dependency_lock"]["allowlist_status"] == "allowed"


def test_plugin_manifest_source_requires_dependency_cache_file_provenance(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


def test_plugin_manifest_source_requires_dependency_cache_artifact_provenance(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    cached_file = cache_entry / "site-packages" / "requests" / "__init__.py"
    cached_bytes = b"__version__ = '2.32.0'\n"
    cached_file.parent.mkdir(parents=True)
    cached_file.write_bytes(cached_bytes)
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    }
                ],
                "files": [
                    {
                        "path": "site-packages/requests/__init__.py",
                        "sha256": hashlib.sha256(cached_bytes).hexdigest(),
                        "size_bytes": len(cached_bytes),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


def test_dependency_policy_requires_valid_dependency_cache_signature(
    tmp_path: Path,
) -> None:
    lock = plugin_package_dependency_lock(
        (
            SimpleNamespace(
                kind="python",
                source="pypi",
                name="Requests",
                version="2.32.0",
            ),
        )
    )
    assert lock is not None
    cache_entry = tmp_path / lock.sha256
    cache_entry.mkdir()
    artifact_bytes = b"requests==2.32.0\n"
    artifact_path = cache_entry / "artifacts" / "requests-2.32.0.whl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    marker_bytes = b"READY = True\n"
    marker_path = cache_entry / "site-packages" / "dependency_cache_marker.py"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(marker_bytes)
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock.sha256,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    }
                ],
                "artifacts": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                        "path": "artifacts/requests-2.32.0.whl",
                        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "size_bytes": len(artifact_bytes),
                    }
                ],
                "files": [
                    {
                        "path": "artifacts/requests-2.32.0.whl",
                        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "size_bytes": len(artifact_bytes),
                    },
                    {
                        "path": "site-packages/dependency_cache_marker.py",
                        "sha256": hashlib.sha256(marker_bytes).hexdigest(),
                        "size_bytes": len(marker_bytes),
                    },
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    policy = PluginPackageDependencyPolicy(
        install_policy="offline_cache",
        allowlist=frozenset({"python:pypi:requests==2.32.0"}),
        cache_dir=tmp_path,
    )

    assert policy.evaluate(lock) == ("offline_cache", "missing", "allowed")
    payload = json.loads((cache_entry / "dependency-lock.json").read_text())
    assert isinstance(payload, dict)
    payload["cache_signature"] = {
        "schema_version": 1,
        "algorithm": "sha256",
        "payload_sha256": "0" * 64,
    }
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    assert policy.evaluate(lock) == ("offline_cache", "missing", "allowed")


def test_dependency_policy_requires_trusted_dependency_cache_builder(
    tmp_path: Path,
) -> None:
    lock = plugin_package_dependency_lock(
        (
            SimpleNamespace(
                kind="python",
                source="pypi",
                name="Requests",
                version="2.32.0",
            ),
        )
    )
    assert lock is not None
    cache_entry = tmp_path / lock.sha256
    cache_entry.mkdir()
    artifact_bytes = b"requests==2.32.0\n"
    artifact_path = cache_entry / "artifacts" / "requests-2.32.0.whl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    marker_bytes = b"READY = True\n"
    marker_path = cache_entry / "site-packages" / "dependency_cache_marker.py"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(marker_bytes)
    dependencies = [
        {
            "kind": "python",
            "source": "pypi",
            "name": "requests",
            "version": "2.32.0",
        }
    ]
    artifacts = [
        {
            "kind": "python",
            "source": "pypi",
            "name": "requests",
            "version": "2.32.0",
            "path": "artifacts/requests-2.32.0.whl",
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
            "origin": _artifact_origin(
                "requests",
                "2.32.0",
                hashlib.sha256(artifact_bytes).hexdigest(),
            ),
        }
    ]
    files = [
        {
            "path": "artifacts/requests-2.32.0.whl",
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
        },
        {
            "path": "site-packages/dependency_cache_marker.py",
            "sha256": hashlib.sha256(marker_bytes).hexdigest(),
            "size_bytes": len(marker_bytes),
        },
    ]
    signature_sha256 = plugin_package_dependency_cache_signature_payload_sha256(
        lock,
        dependencies,
        files,
        artifacts,
        builder_id="untrusted-builder",
    )
    assert signature_sha256 is not None
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock.sha256,
                "dependencies": dependencies,
                "artifacts": artifacts,
                "files": files,
                "cache_signature": {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "builder_id": "untrusted-builder",
                    "payload_sha256": signature_sha256,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    policy = PluginPackageDependencyPolicy(
        install_policy="offline_cache",
        allowlist=frozenset({"python:pypi:requests==2.32.0"}),
        cache_dir=tmp_path,
    )

    assert policy.evaluate(lock) == ("offline_cache", "missing", "allowed")
    trusted_policy = PluginPackageDependencyPolicy(
        install_policy="offline_cache",
        allowlist=frozenset({"python:pypi:requests==2.32.0"}),
        cache_dir=tmp_path,
        trusted_cache_builders=frozenset({"trusted-builder"}),
    )

    assert trusted_policy.evaluate(lock) == ("offline_cache", "missing", "allowed")


def test_dependency_policy_requires_signed_dependency_artifact_origin(
    tmp_path: Path,
) -> None:
    lock = plugin_package_dependency_lock(
        (
            SimpleNamespace(
                kind="python",
                source="pypi",
                name="Requests",
                version="2.32.0",
            ),
        )
    )
    assert lock is not None
    cache_entry = tmp_path / lock.sha256
    cache_entry.mkdir()
    artifact_bytes = b"requests==2.32.0\n"
    artifact_path = cache_entry / "artifacts" / "requests-2.32.0.whl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    marker_bytes = b"READY = True\n"
    marker_path = cache_entry / "site-packages" / "dependency_cache_marker.py"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(marker_bytes)
    dependencies = [
        {
            "kind": "python",
            "source": "pypi",
            "name": "requests",
            "version": "2.32.0",
        }
    ]
    artifact_origin = {
        "type": "package_index",
        "index_url": "https://pypi.org/simple/requests/",
        "archive_url": "https://files.pythonhosted.org/packages/requests-2.32.0.whl",
        "archive_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
    }
    artifacts = [
        {
            "kind": "python",
            "source": "pypi",
            "name": "requests",
            "version": "2.32.0",
            "path": "artifacts/requests-2.32.0.whl",
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
            "origin": artifact_origin,
        }
    ]
    files = [
        {
            "path": "artifacts/requests-2.32.0.whl",
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
        },
        {
            "path": "site-packages/dependency_cache_marker.py",
            "sha256": hashlib.sha256(marker_bytes).hexdigest(),
            "size_bytes": len(marker_bytes),
        },
    ]
    signature_sha256 = plugin_package_dependency_cache_signature_payload_sha256(
        lock,
        dependencies,
        files,
        artifacts,
        builder_id="agent-hub-offline-cache-builder",
    )
    assert signature_sha256 is not None
    artifacts[0]["origin"] = {
        **artifact_origin,
        "archive_url": "https://example.invalid/requests-2.32.0.whl",
    }
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock.sha256,
                "dependencies": dependencies,
                "artifacts": artifacts,
                "files": files,
                "cache_signature": {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "builder_id": "agent-hub-offline-cache-builder",
                    "payload_sha256": signature_sha256,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    policy = PluginPackageDependencyPolicy(
        install_policy="offline_cache",
        allowlist=frozenset({"python:pypi:requests==2.32.0"}),
        cache_dir=tmp_path,
        trusted_cache_builders=frozenset({"agent-hub-offline-cache-builder"}),
    )

    assert policy.evaluate(lock) == ("offline_cache", "missing", "allowed")


def test_plugin_manifest_source_rejects_mismatched_dependency_cache_manifest(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "urllib3",
                        "version": "2.32.0",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


def test_plugin_manifest_source_rejects_dependency_cache_manifest_with_bad_file_digest(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    cached_file = cache_entry / "site-packages" / "requests" / "__init__.py"
    cached_file.parent.mkdir(parents=True)
    cached_file.write_text("__version__ = '2.32.0'\n", encoding="utf-8")
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    }
                ],
                "files": [
                    {
                        "path": "site-packages/requests/__init__.py",
                        "sha256": "0" * 64,
                        "size_bytes": cached_file.stat().st_size,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


def test_plugin_manifest_source_rejects_dependency_cache_with_unlisted_file(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    listed_file = cache_entry / "site-packages" / "requests" / "__init__.py"
    listed_bytes = b"__version__ = '2.32.0'\n"
    listed_file.parent.mkdir(parents=True)
    listed_file.write_bytes(listed_bytes)
    unlisted_file = cache_entry / "site-packages" / "requests" / "session.py"
    unlisted_file.write_text("SECRET = 'not in provenance'\n", encoding="utf-8")
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    }
                ],
                "files": [
                    {
                        "path": "site-packages/requests/__init__.py",
                        "sha256": hashlib.sha256(listed_bytes).hexdigest(),
                        "size_bytes": len(listed_bytes),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


def test_plugin_manifest_source_rejects_dependency_cache_artifact_digest_mismatch(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(b"python pypi requests==2.32.0\n").hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    artifact_bytes = b"requests==2.32.0\n"
    artifact_path = cache_entry / "artifacts" / "requests-2.32.0.whl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    }
                ],
                "artifacts": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                        "path": "artifacts/requests-2.32.0.whl",
                        "sha256": "0" * 64,
                        "size_bytes": len(artifact_bytes),
                    }
                ],
                "files": [
                    {
                        "path": "artifacts/requests-2.32.0.whl",
                        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "size_bytes": len(artifact_bytes),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


def test_plugin_manifest_source_rejects_dependency_cache_artifact_missing_dependency(
    tmp_path: Path,
) -> None:
    lock_hash = hashlib.sha256(
        b"python pypi requests==2.32.0\npython pypi zlib==1.0\n"
    ).hexdigest()
    cache_entry = tmp_path / lock_hash
    cache_entry.mkdir()
    artifact_bytes = b"requests==2.32.0\n"
    artifact_path = cache_entry / "artifacts" / "requests-2.32.0.whl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    (cache_entry / "dependency-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": lock_hash,
                "dependencies": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                    },
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "zlib",
                        "version": "1.0",
                    },
                ],
                "artifacts": [
                    {
                        "kind": "python",
                        "source": "pypi",
                        "name": "requests",
                        "version": "2.32.0",
                        "path": "artifacts/requests-2.32.0.whl",
                        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "size_bytes": len(artifact_bytes),
                    }
                ],
                "files": [
                    {
                        "path": "artifacts/requests-2.32.0.whl",
                        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "size_bytes": len(artifact_bytes),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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
                        SimpleNamespace(
                            kind="python",
                            source="pypi",
                            name="Zlib",
                            version="1.0",
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
            allowlist=frozenset(
                {
                    "python:pypi:requests==2.32.0",
                    "python:pypi:zlib==1.0",
                }
            ),
            cache_dir=tmp_path,
        ),
    )

    capabilities = source.manifests()["capabilities"]
    assert isinstance(capabilities, tuple)
    capability = capabilities[0]
    assert isinstance(capability, dict)

    assert capability["package_dependency_lock"]["cache_status"] == "missing"


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
