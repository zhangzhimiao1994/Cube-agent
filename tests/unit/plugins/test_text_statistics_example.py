from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ed25519

from agent_hub.api.routers.admin import (
    PluginArchiveManifest,
    PluginResourceRequest,
    PluginResourceResponse,
    _plugin_signature_payload,
)
from agent_hub.plugins.runtime import (
    PluginInvocationContext,
    PluginPackageExecutionTarget,
    PythonSubprocessPluginPackageRunner,
)

_PROJECT_ROOT = Path(__file__).parents[3]
_PLUGIN_ROOT = _PROJECT_ROOT / "examples" / "plugins" / "text-statistics"


def test_text_statistics_plugin_builds_a_verifiable_archive(tmp_path: Path) -> None:
    from agent_hub.plugins.package_builder import build_signed_plugin_archive

    private_key = ed25519.Ed25519PrivateKey.generate()
    archive_path = tmp_path / "text-statistics.zip"

    result = build_signed_plugin_archive(
        source_dir=_PLUGIN_ROOT,
        output_path=archive_path,
        private_key=private_key,
        key_id="test-first-party",
    )

    archive_bytes = archive_path.read_bytes()
    with zipfile.ZipFile(archive_path) as archive:
        manifest_payload = archive.read("plugin.json")
        assert set(archive.namelist()) == {
            "README.md",
            "adapter/main.py",
            "plugin.json",
        }
    manifest = PluginArchiveManifest.model_validate_json(manifest_payload)
    assert manifest.package is not None
    assert manifest.package.signature is not None
    assert manifest.package.signature.key_id == "test-first-party"
    assert manifest.package.signature.value != "A" * 86
    private_key.public_key().verify(
        result.signature,
        _plugin_signature_payload(manifest, archive_bytes),
    )
    assert result.archive_sha256 == __import__("hashlib").sha256(archive_bytes).hexdigest()
    assert result.public_key == private_key.public_key().public_bytes_raw()


def test_text_statistics_plugin_declares_executable_package_contract() -> None:
    manifest = PluginArchiveManifest.model_validate_json(
        (_PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest.id == "text-statistics"
    assert manifest.package is not None
    assert manifest.package.kind == "adapter_package"
    assert manifest.package.install_mode == "runtime_registered"
    assert manifest.package.runtime == "python"
    assert manifest.package.isolation == "local_process"
    assert manifest.package.entrypoint == "adapter/main.py"
    assert [capability.id for capability in manifest.capabilities] == ["text.statistics"]
    assert manifest.capabilities[0].input_schema == {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": 200000}},
        "required": ("text",),
        "additionalProperties": False,
    }


async def test_text_statistics_plugin_executes_through_package_protocol() -> None:
    manifest = PluginArchiveManifest.model_validate_json(
        (_PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8")
    )
    capability = manifest.capabilities[0]
    plugin = PluginResourceResponse(
        **PluginResourceRequest(
            id=manifest.id,
            name=manifest.name,
            description=manifest.description,
            version=manifest.version,
            capabilities=manifest.capabilities,
        ).model_dump(),
        status="running",
        health="healthy",
        package_metadata=manifest.package,
    )
    runner = PythonSubprocessPluginPackageRunner(
        python_executable=sys.executable,
        timeout_seconds=5,
    )

    result = await runner.invoke(
        target=PluginPackageExecutionTarget(
            root=_PLUGIN_ROOT,
            entrypoint=_PLUGIN_ROOT / "adapter" / "main.py",
        ),
        plugin=plugin,
        capability=capability,
        arguments={"text": "Cube Agent\n\u4f60\u597d world 123"},
        context=PluginInvocationContext(
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            user_id=UUID("00000000-0000-4000-8000-000000000002"),
            run_id=UUID("00000000-0000-4000-8000-000000000003"),
            actor="tester",
            idempotency_key="text-statistics-1",
        ),
    )

    assert result == {
        "ok": True,
        "character_count": 23,
        "non_whitespace_count": 19,
        "line_count": 2,
        "word_count": 6,
        "chinese_character_count": 2,
        "utf8_byte_count": 27,
    }
