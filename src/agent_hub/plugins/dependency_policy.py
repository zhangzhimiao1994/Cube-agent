"""Offline dependency policy helpers for plugin packages."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast

PluginPackageDependencyInstallPolicy = Literal["disabled", "offline_cache"]
PluginPackageDependencyCacheStatus = Literal["missing", "present"]
PluginPackageDependencyAllowlistStatus = Literal["missing", "allowed", "not_allowed"]


class PluginPackageDependencyLike(Protocol):
    kind: str
    source: str
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class NormalizedPluginPackageDependency:
    kind: str
    source: str
    name: str
    version: str

    @property
    def allowlist_key(self) -> str:
        return f"{self.kind}:{self.source}:{self.name}=={self.version}"


@dataclass(frozen=True, slots=True)
class PluginPackageDependencyLock:
    sha256: str
    dependencies: tuple[NormalizedPluginPackageDependency, ...]


@dataclass(frozen=True, slots=True)
class PluginPackageDependencyPolicy:
    install_policy: PluginPackageDependencyInstallPolicy = "disabled"
    allowlist: frozenset[str] = frozenset()
    trusted_cache_builders: frozenset[str] = frozenset()
    cache_dir: Path | None = None

    def evaluate(
        self,
        lock: PluginPackageDependencyLock,
    ) -> tuple[
        Literal["not_configured", "offline_cache"],
        PluginPackageDependencyCacheStatus,
        PluginPackageDependencyAllowlistStatus,
    ]:
        if self.install_policy != "offline_cache":
            return ("not_configured", "missing", "missing")
        cache_status: PluginPackageDependencyCacheStatus = (
            "present"
            if self.cache_dir is not None
            and _dependency_cache_entry_matches(
                self.cache_dir,
                lock,
                trusted_cache_builders=self.trusted_cache_builders,
            )
            else "missing"
        )
        dependency_keys = frozenset(dependency.allowlist_key for dependency in lock.dependencies)
        allowlist_status: PluginPackageDependencyAllowlistStatus = (
            "allowed" if dependency_keys <= self.allowlist else "not_allowed"
        )
        return ("offline_cache", cache_status, allowlist_status)


def plugin_package_dependency_policy_from_settings(
    settings: object,
) -> PluginPackageDependencyPolicy:
    raw_install_policy = getattr(
        settings, "plugin_package_dependency_install_policy", "disabled"
    )
    install_policy: PluginPackageDependencyInstallPolicy = (
        cast(PluginPackageDependencyInstallPolicy, raw_install_policy)
        if raw_install_policy in {"disabled", "offline_cache"}
        else "disabled"
    )
    allowlist: object = getattr(
        settings, "plugin_package_dependency_allowlist", frozenset[str]()
    )
    trusted_cache_builders: object = getattr(
        settings, "plugin_package_dependency_trusted_cache_builders", frozenset[str]()
    )
    cache_dir = getattr(settings, "plugin_package_dependency_cache_dir", None)
    normalized_allowlist: frozenset[str] = (
        frozenset(str(item) for item in allowlist)
        if isinstance(allowlist, frozenset | set)
        else frozenset()
    )
    normalized_trusted_cache_builders: frozenset[str] = (
        frozenset(str(item) for item in trusted_cache_builders)
        if isinstance(trusted_cache_builders, frozenset | set)
        else frozenset()
    )
    return PluginPackageDependencyPolicy(
        install_policy=install_policy,
        allowlist=normalized_allowlist,
        trusted_cache_builders=normalized_trusted_cache_builders,
        cache_dir=cache_dir if isinstance(cache_dir, Path) else None,
    )


def _dependency_cache_entry_matches(
    cache_dir: Path,
    lock: PluginPackageDependencyLock,
    *,
    trusted_cache_builders: frozenset[str],
) -> bool:
    entry_dir = cache_dir / lock.sha256
    if not entry_dir.is_dir():
        return False
    manifest_path = entry_dir / "dependency-lock.json"
    if not manifest_path.is_file():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        return False
    files = payload.get("files")
    if (
        not isinstance(files, list)
        or not files
        or not _dependency_cache_files_match(entry_dir, files)
    ):
        return False
    artifacts = payload.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not _dependency_cache_artifacts_match(lock, artifacts, files)
    ):
        return False
    expected_dependencies: list[dict[str, str]] = [
        {
            "kind": dependency.kind,
            "source": dependency.source,
            "name": dependency.name,
            "version": dependency.version,
        }
        for dependency in lock.dependencies
    ]
    if not _dependency_cache_signature_matches(
        lock,
        expected_dependencies,
        files,
        artifacts,
        payload.get("cache_signature"),
        trusted_cache_builders=trusted_cache_builders,
    ):
        return False
    return (
        payload.get("schema_version") == 1
        and payload.get("sha256") == lock.sha256
        and dependencies == expected_dependencies
    )


def _dependency_cache_signature_matches(
    lock: PluginPackageDependencyLock,
    dependencies: Sequence[dict[str, str]],
    files: Sequence[object],
    artifacts: Sequence[object],
    signature: object,
    *,
    trusted_cache_builders: frozenset[str],
) -> bool:
    if not isinstance(signature, dict):
        return False
    payload_sha256 = signature.get("payload_sha256")
    builder_id = signature.get("builder_id")
    return (
        signature.get("schema_version") == 1
        and signature.get("algorithm") == "sha256"
        and type(builder_id) is str
        and builder_id in trusted_cache_builders
        and _dependency_cache_builder_id_valid(builder_id)
        and type(payload_sha256) is str
        and re.fullmatch(r"[a-f0-9]{64}", payload_sha256) is not None
        and payload_sha256
        == plugin_package_dependency_cache_signature_payload_sha256(
            lock,
            dependencies,
            files,
            artifacts,
            builder_id=builder_id,
        )
    )


def plugin_package_dependency_cache_signature_payload_sha256(
    lock: PluginPackageDependencyLock,
    dependencies: Sequence[dict[str, str]],
    files: Sequence[object],
    artifacts: Sequence[object],
    *,
    builder_id: str,
) -> str | None:
    if not _dependency_cache_builder_id_valid(builder_id):
        return None
    normalized_files = _dependency_cache_signature_files(files)
    normalized_artifacts = _dependency_cache_signature_artifacts(artifacts)
    if normalized_files is None or normalized_artifacts is None:
        return None
    payload = {
        "schema_version": 1,
        "sha256": lock.sha256,
        "dependencies": list(dependencies),
        "files": normalized_files,
        "artifacts": normalized_artifacts,
        "builder_id": builder_id,
    }
    payload_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload_bytes).hexdigest()


def _dependency_cache_builder_id_valid(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is not None


def _dependency_cache_signature_files(
    files: Sequence[object],
) -> list[dict[str, object]] | None:
    normalized: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        if type(path) is not str or type(sha256) is not str or type(size_bytes) is not int:
            return None
        normalized.append(
            {
                "path": path,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
    return sorted(normalized, key=lambda item: cast(str, item["path"]))


def _dependency_cache_signature_artifacts(
    artifacts: Sequence[object],
) -> list[dict[str, object]] | None:
    normalized: list[dict[str, object]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            return None
        kind = item.get("kind")
        source = item.get("source")
        name = item.get("name")
        version = item.get("version")
        path = item.get("path")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        origin = _dependency_cache_artifact_origin(item.get("origin"))
        if (
            type(kind) is not str
            or type(source) is not str
            or type(name) is not str
            or type(version) is not str
            or type(path) is not str
            or type(sha256) is not str
            or type(size_bytes) is not int
            or origin is None
        ):
            return None
        normalized.append(
            {
                "kind": kind,
                "source": source,
                "name": normalize_plugin_package_dependency_name(name),
                "version": version,
                "path": path,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "origin": origin,
            }
        )
    return sorted(
        normalized,
        key=lambda item: (
            cast(str, item["kind"]),
            cast(str, item["source"]),
            cast(str, item["name"]),
            cast(str, item["version"]),
            cast(str, item["path"]),
        ),
    )


def _dependency_cache_artifacts_match(
    lock: PluginPackageDependencyLock,
    artifacts: Sequence[object],
    files: Sequence[object],
) -> bool:
    artifact_files = {
        cast(str, item["path"]): item
        for item in files
        if isinstance(item, dict) and type(item.get("path")) is str
    }
    expected_dependencies = {
        dependency.allowlist_key: dependency for dependency in lock.dependencies
    }
    seen_dependencies: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            return False
        kind = item.get("kind")
        source = item.get("source")
        name = item.get("name")
        version = item.get("version")
        raw_path = item.get("path")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        origin = _dependency_cache_artifact_origin(item.get("origin"))
        if (
            type(kind) is not str
            or type(source) is not str
            or type(name) is not str
            or type(version) is not str
            or type(raw_path) is not str
            or type(sha256) is not str
            or type(size_bytes) is not int
            or origin is None
        ):
            return False
        normalized_name = normalize_plugin_package_dependency_name(name)
        dependency_key = f"{kind}:{source}:{normalized_name}=={version}"
        expected_dependency = expected_dependencies.get(dependency_key)
        if expected_dependency is None or dependency_key in seen_dependencies:
            return False
        seen_dependencies.add(dependency_key)
        expected_file = artifact_files.get(raw_path)
        if expected_file is None:
            return False
        if (
            expected_file.get("sha256") != sha256
            or expected_file.get("size_bytes") != size_bytes
            or origin["archive_sha256"] != sha256
            or not _dependency_cache_manifest_path_valid(raw_path)
        ):
            return False
    return seen_dependencies == set(expected_dependencies)


def _dependency_cache_artifact_origin(origin: object) -> dict[str, str] | None:
    if not isinstance(origin, dict):
        return None
    origin_type = origin.get("type")
    index_url = origin.get("index_url")
    archive_url = origin.get("archive_url")
    archive_sha256 = origin.get("archive_sha256")
    if (
        origin_type != "package_index"
        or not _dependency_cache_origin_url_valid(index_url)
        or not _dependency_cache_origin_url_valid(archive_url)
        or type(archive_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", archive_sha256) is None
    ):
        return None
    return {
        "type": "package_index",
        "index_url": cast(str, index_url),
        "archive_url": cast(str, archive_url),
        "archive_sha256": archive_sha256,
    }


def _dependency_cache_origin_url_valid(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith("https://")
        and 8 < len(value) <= 2048
        and not any(ord(character) < 32 for character in value)
    )


def _dependency_cache_files_match(
    entry_dir: Path,
    files: Sequence[object],
) -> bool:
    listed_paths: set[PurePosixPath] = set()
    for item in files:
        if not isinstance(item, dict):
            return False
        raw_path = item.get("path")
        expected_sha256 = item.get("sha256")
        expected_size = item.get("size_bytes")
        if (
            type(raw_path) is not str
            or type(expected_sha256) is not str
            or type(expected_size) is not int
            or expected_size < 0
            or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
        ):
            return False
        if not _dependency_cache_manifest_path_valid(raw_path):
            return False
        file_path = PurePosixPath(raw_path)
        if file_path in listed_paths:
            return False
        listed_paths.add(file_path)
        candidate = entry_dir.joinpath(*file_path.parts)
        try:
            candidate.resolve().relative_to(entry_dir.resolve())
        except (OSError, ValueError):
            return False
        if candidate.is_symlink() or not candidate.is_file():
            return False
        try:
            stat = candidate.stat()
        except OSError:
            return False
        if stat.st_size != expected_size:
            return False
        if _sha256_file(candidate) != expected_sha256:
            return False
    return listed_paths == _dependency_cache_entry_file_paths(entry_dir)


def _dependency_cache_manifest_path_valid(raw_path: str) -> bool:
    if "\\" in raw_path:
        return False
    file_path = PurePosixPath(raw_path)
    return not (
        not file_path.parts
        or file_path.is_absolute()
        or any(part in {"", ".", ".."} for part in file_path.parts)
        or file_path == PurePosixPath("dependency-lock.json")
    )


def _dependency_cache_entry_file_paths(entry_dir: Path) -> set[PurePosixPath]:
    paths: set[PurePosixPath] = set()
    try:
        children = tuple(entry_dir.rglob("*"))
    except OSError:
        return {PurePosixPath("__invalid__")}
    for child in children:
        if child == entry_dir / "dependency-lock.json":
            continue
        try:
            child.relative_to(entry_dir)
        except ValueError:
            return {PurePosixPath("__invalid__")}
        if child.is_dir() and not child.is_symlink():
            continue
        if child.is_symlink() or not child.is_file():
            return {PurePosixPath("__invalid__")}
        try:
            relative = child.relative_to(entry_dir)
        except ValueError:
            return {PurePosixPath("__invalid__")}
        paths.add(PurePosixPath(*relative.parts))
    return paths


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def normalize_plugin_package_dependency_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def normalize_plugin_package_dependency_allowlist_entry(value: str) -> str:
    match = re.fullmatch(
        r"python:pypi:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})=="
        r"([A-Za-z0-9][A-Za-z0-9!+.,<=>~_-]{0,127})",
        value,
    )
    if match is None:
        raise ValueError("plugin package dependency allowlist entry is invalid")
    return (
        "python:pypi:"
        f"{normalize_plugin_package_dependency_name(match.group(1))}=={match.group(2)}"
    )


def plugin_package_dependency_lock(
    dependencies: Sequence[PluginPackageDependencyLike],
) -> PluginPackageDependencyLock | None:
    locked: list[NormalizedPluginPackageDependency] = []
    for dependency in dependencies:
        kind = getattr(dependency, "kind", None)
        source = getattr(dependency, "source", None)
        name = getattr(dependency, "name", None)
        version = getattr(dependency, "version", None)
        if not all(type(value) is str for value in (kind, source, name, version)):
            return None
        normalized_kind = cast(str, kind)
        normalized_source = cast(str, source)
        normalized_name = cast(str, name)
        normalized_version = cast(str, version)
        locked.append(
            NormalizedPluginPackageDependency(
                kind=normalized_kind,
                source=normalized_source,
                name=normalize_plugin_package_dependency_name(normalized_name),
                version=normalized_version,
            )
        )
    ordered = tuple(
        sorted(
            locked,
            key=lambda item: (item.kind, item.source, item.name, item.version),
        )
    )
    lock_bytes = "".join(
        f"{item.kind} {item.source} {item.name}=={item.version}\n" for item in ordered
    ).encode("utf-8")
    return PluginPackageDependencyLock(
        sha256=hashlib.sha256(lock_bytes).hexdigest(),
        dependencies=ordered,
    )
