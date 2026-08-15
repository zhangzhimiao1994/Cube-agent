from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml
from pydantic import ValidationError

from agent_hub.skills.manifest import SkillManifest


class InvalidSkillPackage(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SkillPackageFile:
    path: str
    size: int
    compressed_size: int
    executable: bool = False


@dataclass(frozen=True, slots=True)
class SkillPackageInspection:
    content_sha256: str
    manifest: SkillManifest
    files: tuple[SkillPackageFile, ...]
    requested_capabilities: tuple[str, ...]
    dependency_lock_hash: str


class SkillPackageInspector:
    def __init__(
        self,
        *,
        max_files: int = 128,
        max_archive_bytes: int = 2_000_000,
        max_uncompressed_bytes: int = 10_000_000,
        max_compression_ratio: int = 25,
    ) -> None:
        self._max_files = max_files
        self._max_archive_bytes = max_archive_bytes
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._max_compression_ratio = max_compression_ratio

    def inspect(self, archive_bytes: bytes) -> SkillPackageInspection:
        if len(archive_bytes) > self._max_archive_bytes:
            raise InvalidSkillPackage("skill archive exceeds size limit")
        content_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        if zipfile.is_zipfile(io.BytesIO(archive_bytes)):
            return self._inspect_zip(archive_bytes, content_sha256)
        return self._inspect_tar(archive_bytes, content_sha256)

    def _inspect_zip(self, archive_bytes: bytes, content_sha256: str) -> SkillPackageInspection:
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                infos = archive.infolist()
                if len(infos) > self._max_files:
                    raise InvalidSkillPackage("skill archive contains too many files")
                files = self._inspect_members(infos)
                names = archive.namelist()
                manifest_name = _manifest_name(names)
                if manifest_name is None:
                    raise InvalidSkillPackage("skill archive is missing skill manifest")
                manifest = self._read_manifest_bytes(manifest_name, archive.read(manifest_name))
                self._validate_manifest_references(manifest, files)
                dependency_bytes = b""
                dependency_name = _dependency_file_name(names)
                if dependency_name is not None:
                    dependency_bytes = archive.read(dependency_name)
                    _validate_pinned_dependencies(dependency_bytes)
                self._validate_dependency_hash(dependency_bytes, manifest)
        except zipfile.BadZipFile as exc:
            raise InvalidSkillPackage("skill archive must be a valid zip file") from exc
        return SkillPackageInspection(
            content_sha256=content_sha256,
            manifest=manifest,
            files=files,
            requested_capabilities=_requested_capabilities(manifest),
            dependency_lock_hash=manifest.dependency_lock_hash,
        )

    def _inspect_tar(self, archive_bytes: bytes, content_sha256: str) -> SkillPackageInspection:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
                members = archive.getmembers()
                if len(members) > self._max_files:
                    raise InvalidSkillPackage("skill archive contains too many files")
                files = self._inspect_tar_members(members, len(archive_bytes))
                names = [member.name for member in members]
                manifest_name = _manifest_name(names)
                if manifest_name is None:
                    raise InvalidSkillPackage("skill archive is missing skill manifest")
                manifest_file = archive.extractfile(manifest_name)
                if manifest_file is None:
                    raise InvalidSkillPackage("skill manifest cannot be read")
                manifest = self._read_manifest_bytes(manifest_name, manifest_file.read())
                self._validate_manifest_references(manifest, files)
                dependency_bytes = b""
                dependency_name = _dependency_file_name(names)
                if dependency_name is not None:
                    dependency_file = archive.extractfile(dependency_name)
                    if dependency_file is None:
                        raise InvalidSkillPackage("dependency file cannot be read")
                    dependency_bytes = dependency_file.read()
                    _validate_pinned_dependencies(dependency_bytes)
                self._validate_dependency_hash(dependency_bytes, manifest)
        except tarfile.TarError as exc:
            raise InvalidSkillPackage("skill archive must be a valid zip or tar archive") from exc
        return SkillPackageInspection(
            content_sha256=content_sha256,
            manifest=manifest,
            files=files,
            requested_capabilities=_requested_capabilities(manifest),
            dependency_lock_hash=manifest.dependency_lock_hash,
        )

    def _inspect_members(self, infos: list[zipfile.ZipInfo]) -> tuple[SkillPackageFile, ...]:
        seen: set[str] = set()
        files: list[SkillPackageFile] = []
        total_size = 0
        total_compressed = 0
        for info in infos:
            normalized = _normalize_zip_path(info.filename)
            if normalized in seen:
                raise InvalidSkillPackage("skill archive contains duplicate paths")
            seen.add(normalized)
            mode = (info.external_attr >> 16) & 0o777777
            if _is_link_or_device(mode):
                raise InvalidSkillPackage("skill archive contains links or device files")
            if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                continue
            suffix = PurePosixPath(normalized).suffix.lower()
            if suffix in _FORBIDDEN_EXTENSIONS:
                raise InvalidSkillPackage("skill archive contains forbidden file extensions")
            if suffix in _NESTED_ARCHIVE_EXTENSIONS:
                raise InvalidSkillPackage("skill archive contains nested archives")
            total_size += info.file_size
            total_compressed += max(info.compress_size, 1)
            if total_size > self._max_uncompressed_bytes:
                raise InvalidSkillPackage("skill archive exceeds uncompressed size limit")
            if info.file_size > max(info.compress_size, 1) * self._max_compression_ratio:
                raise InvalidSkillPackage("skill archive compression ratio is excessive")
            executable = bool(mode & 0o111)
            files.append(
                SkillPackageFile(
                    path=normalized,
                    size=info.file_size,
                    compressed_size=info.compress_size,
                    executable=executable,
                )
            )
        if total_size > max(total_compressed, 1) * self._max_compression_ratio:
            raise InvalidSkillPackage("skill archive compression ratio is excessive")
        return tuple(files)

    def _inspect_tar_members(
        self,
        members: list[tarfile.TarInfo],
        archive_size: int,
    ) -> tuple[SkillPackageFile, ...]:
        seen: set[str] = set()
        files: list[SkillPackageFile] = []
        total_size = 0
        for member in members:
            if _tar_member_is_metadata(member):
                continue
            normalized = _normalize_zip_path(member.name)
            if normalized in seen:
                raise InvalidSkillPackage("skill archive contains duplicate paths")
            seen.add(normalized)
            if member.isdir():
                continue
            if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo():
                raise InvalidSkillPackage("skill archive contains links or device files")
            if not member.isfile():
                raise InvalidSkillPackage("skill archive contains unsupported file types")
            suffix = PurePosixPath(normalized).suffix.lower()
            if suffix in _FORBIDDEN_EXTENSIONS:
                raise InvalidSkillPackage("skill archive contains forbidden file extensions")
            if suffix in _NESTED_ARCHIVE_EXTENSIONS:
                raise InvalidSkillPackage("skill archive contains nested archives")
            total_size += member.size
            if total_size > self._max_uncompressed_bytes:
                raise InvalidSkillPackage("skill archive exceeds uncompressed size limit")
            executable = bool(member.mode & 0o111)
            files.append(
                SkillPackageFile(
                    path=normalized,
                    size=member.size,
                    compressed_size=0,
                    executable=executable,
                )
            )
        if total_size > max(archive_size, 1) * self._max_compression_ratio:
            raise InvalidSkillPackage("skill archive compression ratio is excessive")
        return tuple(files)

    def _read_manifest_bytes(self, manifest_name: str, manifest_bytes: bytes) -> SkillManifest:
        try:
            if manifest_name.endswith(".json"):
                raw = json.loads(manifest_bytes.decode("utf-8"))
            else:
                raw = yaml.safe_load(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise InvalidSkillPackage("skill manifest cannot be parsed") from exc
        if not isinstance(raw, dict):
            raise InvalidSkillPackage("skill manifest must be an object")
        try:
            return SkillManifest.model_validate(raw)
        except ValidationError as exc:
            raise InvalidSkillPackage("skill manifest is invalid") from exc

    def _validate_manifest_references(
        self,
        manifest: SkillManifest,
        files: tuple[SkillPackageFile, ...],
    ) -> None:
        paths = {file.path for file in files}
        if manifest.entry_point not in paths:
            raise InvalidSkillPackage("skill entry point is missing")
        for path in manifest.writable_paths:
            if path in paths:
                raise InvalidSkillPackage("writable paths must not overlap packaged files")
        for file in files:
            if file.executable and file.path != manifest.entry_point:
                raise InvalidSkillPackage("skill archive contains undeclared executables")

    def _validate_dependency_hash(self, dependency_bytes: bytes, manifest: SkillManifest) -> None:
        actual_hash = hashlib.sha256(dependency_bytes).hexdigest()
        if actual_hash != manifest.dependency_lock_hash:
            raise InvalidSkillPackage("dependency lock hash does not match package contents")


_FORBIDDEN_EXTENSIONS = frozenset(
    {".exe", ".dll", ".dylib", ".so", ".pyc", ".pyo", ".bat", ".cmd", ".ps1", ".sh"}
)
_NESTED_ARCHIVE_EXTENSIONS = frozenset({".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar", ".whl"})
_TAR_METADATA_TYPES = frozenset({
    tarfile.XHDTYPE,
    tarfile.XGLTYPE,
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
})
_MANIFEST_NAMES = ("skill.yaml", "skill.yml", "skill.json")
_DEPENDENCY_NAMES = ("requirements.txt", "requirements.lock", "dependencies.lock")
_PINNED_REQUIREMENT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.!+~-]*$"
)


def _normalize_zip_path(name: str) -> str:
    normalized = name.replace("\\", "/").rstrip("/")
    if normalized.startswith(("/", "../")) or "/../" in normalized:
        raise InvalidSkillPackage("skill archive contains path traversal")
    if normalized in {"", ".", ".."} or normalized.endswith("/.."):
        raise InvalidSkillPackage("skill archive contains path traversal")
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        raise InvalidSkillPackage("skill archive contains absolute paths")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidSkillPackage("skill archive contains unsafe paths")
    return path.as_posix()


def _is_link_or_device(mode: int) -> bool:
    file_type = stat.S_IFMT(mode)
    return file_type in {
        stat.S_IFLNK,
        stat.S_IFCHR,
        stat.S_IFBLK,
        stat.S_IFIFO,
        stat.S_IFSOCK,
    }


def _tar_member_is_metadata(member: tarfile.TarInfo) -> bool:
    return member.type in _TAR_METADATA_TYPES


def _manifest_name(names: list[str]) -> str | None:
    normalized = {_normalize_zip_path(name): name for name in names}
    for candidate in _MANIFEST_NAMES:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _dependency_file_name(names: list[str]) -> str | None:
    normalized = {_normalize_zip_path(name): name for name in names}
    for candidate in _DEPENDENCY_NAMES:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _validate_pinned_dependencies(dependency_bytes: bytes) -> None:
    try:
        text = dependency_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSkillPackage("dependency file must be utf-8") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("-r", "-", ".", "/", "\\", "~")):
            raise InvalidSkillPackage("dependencies must be pinned package requirements")
        if any(fragment in stripped for fragment in (" @ ", "://", "git+", "file:", "*")):
            raise InvalidSkillPackage("dependencies must be pinned package requirements")
        if _PINNED_REQUIREMENT_PATTERN.fullmatch(stripped) is None:
            raise InvalidSkillPackage("dependencies must be pinned with ==")


def _requested_capabilities(manifest: SkillManifest) -> tuple[str, ...]:
    capabilities: list[str] = []
    capabilities.extend(tool if tool.startswith("tool:") else f"tool:{tool}" for tool in manifest.declared_tools)
    if manifest.network_policy.mode == "allowlist":
        capabilities.extend(f"network:{host}" for host in manifest.network_policy.allow_hosts)
    capabilities.extend(f"write:{path}" for path in manifest.writable_paths)
    capabilities.extend(f"secret:{secret}" for secret in manifest.env_secret_refs)
    return tuple(capabilities)
