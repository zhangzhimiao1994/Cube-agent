from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric import ed25519

from agent_hub.api.routers.admin import PluginArchiveManifest, _plugin_signature_payload

_PLACEHOLDER_SIGNATURE = "A" * 86


@dataclass(frozen=True, slots=True)
class PluginPackageBuildResult:
    archive_sha256: str
    public_key: bytes
    signature: bytes


def build_signed_plugin_archive(
    *,
    source_dir: Path,
    output_path: Path,
    private_key: ed25519.Ed25519PrivateKey,
    key_id: str,
) -> PluginPackageBuildResult:
    source_root = source_dir.resolve()
    manifest_path = source_root / "plugin.json"
    manifest_payload = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    package = manifest_payload.get("package")
    if not isinstance(package, dict):
        raise TypeError("plugin manifest must declare a package")
    signature = package.get("signature")
    if not isinstance(signature, dict):
        raise TypeError("plugin package must declare a signature")
    signature["algorithm"] = "ed25519"
    signature["key_id"] = key_id
    signature["value"] = _PLACEHOLDER_SIGNATURE

    files = _package_source_files(source_root, output_path=output_path)
    unsigned_archive = _plugin_archive_bytes(manifest_payload, files)
    unsigned_manifest = PluginArchiveManifest.model_validate(manifest_payload)
    signature_bytes = private_key.sign(
        _plugin_signature_payload(unsigned_manifest, unsigned_archive)
    )
    signature["value"] = base64.urlsafe_b64encode(signature_bytes).rstrip(b"=").decode("ascii")

    archive_bytes = _plugin_archive_bytes(manifest_payload, files)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    temporary_path.write_bytes(archive_bytes)
    temporary_path.replace(output_path)
    return PluginPackageBuildResult(
        archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        public_key=private_key.public_key().public_bytes_raw(),
        signature=signature_bytes,
    )


def _package_source_files(
    source_root: Path,
    *,
    output_path: Path,
) -> tuple[tuple[str, bytes], ...]:
    output_resolved = output_path.resolve()
    files: list[tuple[str, bytes]] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise ValueError("plugin package source cannot contain symbolic links")
        if not path.is_file() or path.name == "plugin.json" or path.resolve() == output_resolved:
            continue
        files.append((path.relative_to(source_root).as_posix(), path.read_bytes()))
    return tuple(files)


def _plugin_archive_bytes(
    manifest_payload: dict[str, Any],
    files: tuple[tuple[str, bytes], ...],
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        _write_archive_member(
            archive,
            "plugin.json",
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        for path, content in files:
            _write_archive_member(archive, path, content)
    return buffer.getvalue()


def _write_archive_member(archive: zipfile.ZipFile, path: str, content: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


__all__ = ["PluginPackageBuildResult", "build_signed_plugin_archive"]
