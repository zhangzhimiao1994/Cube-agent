"""Authorize real run sources and freeze bounded bytes without executing code.

Routes must inject the server's RunRepository, workspace store and configured
generated-file root, never request mappings or an authorization DTO. Every public
read repeats permission/tenant/run/actor checks; workspace selection must match
the persisted run routing. Only top-level published artifact IDs are accepted.

Snapshots are immutable in-memory data, not durable plans, signed packages or
approval. Before confirming, the parent service must load/lock its persisted
plan, reject cancelled/expired states, and call revalidate with the stored hash.
Persistence, signature verification/trust, installation and execution are outside
this service. Cancellation propagates and no user files or staging are written.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied
from agent_hub.files.generated import ZIP_MIME_TYPE, GeneratedFileStore, safe_generated_filename
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.plugins.handover_storage import ArtifactSource, AuthorizedSource, WorkspaceSource
from agent_hub.runs.repository import RunNotFound, RunRecord

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class InvalidSource(ValueError):
    pass


class SourceTooLarge(InvalidSource):
    pass


class SourceChanged(InvalidSource):
    pass


class SourceRunRepository(Protocol):
    """Existing RunRepository methods, injected from trusted server dependencies.

    A request-fed mapping provider is not an implementation of the authorization
    boundary: these methods must query persisted rows by tenant_id and run_id.
    """

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord: ...

    async def raw_artifacts(
        self,
        tenant_id: UUID,
        run_id: UUID,
    ) -> tuple[dict[str, object], ...]: ...


@dataclass(frozen=True, slots=True)
class SourceLimits:
    max_files: int = 200
    max_file_bytes: int = 512_000
    max_total_bytes: int = 2_000_000
    max_archive_bytes: int = 2_000_000
    max_compression_ratio: int = 200

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_archive_bytes,
            self.max_compression_ratio,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("source limits must be positive integers")
        if (
            values[0] > 200
            or values[1] > 512_000
            or any(v > 2_000_000 for v in values[2:4])
            or values[4] > 200
        ):
            raise ValueError("source limits cannot exceed the handover safety ceiling")


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    content: bytes = field(repr=False)
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FrozenSourceSnapshot:
    authorized: AuthorizedSource
    files: tuple[SnapshotFile, ...]
    plugin_id: str
    plugin_version: str
    total_size_bytes: int


class HandoverSourceService:
    def __init__(
        self,
        *,
        runs: SourceRunRepository,
        workspaces: ProjectWorkspaceStore,
        generated_root: Path,
        limits: SourceLimits | None = None,
    ) -> None:
        self._runs = runs
        self._workspaces = workspaces
        # These are application configuration, never a source path from a request.
        self._generated_root = Path(os.path.abspath(generated_root))
        self._generated_store = GeneratedFileStore(self._generated_root)
        self._limits = limits if limits is not None else SourceLimits()

    async def freeze(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource | WorkspaceSource,
    ) -> FrozenSourceSnapshot:
        """Authorize identifiers against repository state, then freeze actual bytes."""
        checked = _checked_source(source)
        record = await self._authorize(principal, run_id, checked)
        disk_reads: list[_DiskRead] = []
        artifact_payload: dict[str, object] | None = None
        try:
            if isinstance(checked, WorkspaceSource):
                files = await self._workspace_files(principal, checked, disk_reads)
            else:
                artifact_payload = await self._artifact_payload(principal, run_id, checked)
                files = await self._artifact_files(
                    principal,
                    run_id,
                    checked,
                    artifact_payload,
                    disk_reads,
                )
            await asyncio.sleep(0)
            plugin_id, plugin_version = _validate_plugin(files)
            fresh = await self._authorize(principal, run_id, checked)
            if _association(fresh) != _association(record):
                raise SourceChanged("run workspace association changed")
            if isinstance(checked, ArtifactSource):
                current = await self._artifact_payload(principal, run_id, checked)
                if _canonical(current) != _canonical(artifact_payload):
                    raise SourceChanged("published artifact changed")
            for read in disk_reads:
                await asyncio.sleep(0)
                read.verify()
                current_bytes, _ = _read_file(read.path, read.limit)
                if hashlib.sha256(current_bytes).hexdigest() != read.sha256:
                    raise SourceChanged("source content changed while freezing")
        except (OSError, ValueError, zipfile.BadZipFile, UnicodeError, zlib.error) as exc:
            if isinstance(exc, InvalidSource):
                raise
            raise InvalidSource("source is unavailable or invalid") from None
        digest = hashlib.sha256(
            _canonical(
                {
                    "schema_version": 1,
                    "domain": "generated-handover-source",
                    "tenant_id": str(principal.tenant_id),
                    "run_id": str(run_id),
                    "actor_id": str(principal.user_id),
                    "source": checked.model_dump(mode="json"),
                    "artifact_sha256": (
                        None
                        if artifact_payload is None
                        else hashlib.sha256(_canonical(artifact_payload)).hexdigest()
                    ),
                    "files": [
                        {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
                        for item in files
                    ],
                }
            )
        ).hexdigest()
        return FrozenSourceSnapshot(
            authorized=AuthorizedSource(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                source=checked,
                source_manifest_sha256=digest,
            ),
            files=files,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            total_size_bytes=sum(item.size_bytes for item in files),
        )

    async def revalidate(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource | WorkspaceSource,
        expected_source_manifest_sha256: str,
    ) -> FrozenSourceSnapshot:
        """Re-read sources; the expected hash must come from the locked stored plan.

        This method grants no authority to a client-provided expected hash and
        does not validate a plan's status, creator, expiry or signature.
        """
        if type(expected_source_manifest_sha256) is not str or not _SHA.fullmatch(
            expected_source_manifest_sha256,
        ):
            raise InvalidSource("expected source hash is invalid")
        snapshot = await self.freeze(principal, run_id, source)
        if snapshot.authorized.source_manifest_sha256 != expected_source_manifest_sha256:
            raise SourceChanged("source no longer matches the frozen plan")
        return snapshot

    async def _authorize(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource | WorkspaceSource,
    ) -> RunRecord:
        if type(principal) is not AuthenticatedPrincipal or type(run_id) is not UUID:
            raise PermissionDenied("source access denied")
        for permission in ("run:create", "run:read", "plugin:read"):
            Authorizer().require(principal, permission)
        try:
            record = await self._runs.get(principal.tenant_id, run_id)
        except RunNotFound:
            raise PermissionDenied("source access denied") from None
        if (
            type(record) is not RunRecord
            or record.id != run_id
            or record.tenant_id != principal.tenant_id
            or record.actor_id != principal.user_id
        ):
            raise PermissionDenied("source access denied")
        if isinstance(source, WorkspaceSource) and _association(record) != (
            source.project_id,
            source.session_id,
        ):
            raise PermissionDenied("source access denied")
        return record

    async def _workspace_files(
        self,
        principal: AuthenticatedPrincipal,
        source: WorkspaceSource,
        reads: list[_DiskRead],
    ) -> tuple[SnapshotFile, ...]:
        _validate_paths(source.paths, self._limits)
        root = self._workspaces.session_root(
            principal.tenant_id,
            source.project_id,
            source.session_id,
        )
        files: list[SnapshotFile] = []
        total = 0
        for relative in source.paths:
            await asyncio.sleep(0)
            path = root / relative
            # Check lexical ancestors before the existing service resolves links.
            _path_chain(path)
            resolved = self._workspaces.resolve_file(
                principal.tenant_id,
                source.project_id,
                source.session_id,
                relative,
            )
            if resolved != path:
                raise InvalidSource("source path aliases another file")
            content, read = _read_file(path, self._limits.max_file_bytes)
            reads.append(read)
            total += len(content)
            if total > self._limits.max_total_bytes:
                raise SourceTooLarge("source total size exceeds limit")
            files.append(_file(relative, content))
        return tuple(files)

    async def _artifact_payload(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource,
    ) -> dict[str, object]:
        payloads = await self._runs.raw_artifacts(principal.tenant_id, run_id)
        matches = [payload for payload in payloads if payload.get("id") == source.artifact_id]
        if len(matches) != 1:
            raise PermissionDenied("source access denied")
        # Detach repository mappings before any subsequent await can mutate them.
        detached: object = json.loads(_canonical(matches[0]))
        if not isinstance(detached, dict):
            raise InvalidSource("invalid artifact")
        return detached

    async def _artifact_files(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource,
        payload: dict[str, object],
        reads: list[_DiskRead],
    ) -> tuple[SnapshotFile, ...]:
        content = payload.get("content")
        if not isinstance(content, Mapping):
            raise InvalidSource("artifact has no source content")
        envelopes = [content[name] for name in ("result", "workspace_delivery") if name in content]
        if envelopes:
            if (
                len(envelopes) != 1
                or not isinstance(envelopes[0], Mapping)
                or any(name in content for name in ("workspace_bundle", "file", "metadata"))
            ):
                raise InvalidSource("artifact has ambiguous source content")
            content = envelopes[0]
        bundle = content.get("workspace_bundle")
        if isinstance(bundle, Mapping):
            members = bundle.get("files")
            if not isinstance(members, Mapping):
                raise InvalidSource("invalid workspace bundle")
            _validate_paths(tuple(members), self._limits)
            files: list[SnapshotFile] = []
            total = 0
            for path, text in sorted(members.items()):
                await asyncio.sleep(0)
                if not isinstance(path, str) or not isinstance(text, str):
                    raise InvalidSource("workspace bundle must contain text files")
                if len(text) > self._limits.max_file_bytes:
                    raise SourceTooLarge("source file exceeds limit")
                data = text.encode("utf-8")
                total += len(data)
                _check_sizes(len(data), total, self._limits)
                files.append(_file(path, data))
            return tuple(files)
        file = content.get("file")
        metadata = content.get("metadata")
        if not isinstance(file, Mapping) or not isinstance(metadata, Mapping):
            raise InvalidSource("artifact has no generated package file")
        filename = metadata.get("filename", file.get("filename"))
        mime = metadata.get("mime_type", file.get("mime_type"))
        key = metadata.get("storage_key", file.get("storage_key"))
        size = file.get("size_bytes")
        digest = file.get("sha256")
        if (
            not isinstance(filename, str)
            or mime != ZIP_MIME_TYPE
            or not isinstance(key, str)
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not _SHA.fullmatch(digest)
        ):
            raise InvalidSource("invalid generated archive metadata")
        safe_generated_filename(filename)
        file_id = _generated_file_id(content, file, metadata, fallback=source.artifact_id)
        expected_key = f"{principal.tenant_id}/{run_id}/{file_id}/{filename}"
        if key != expected_key:
            raise InvalidSource("generated archive scope mismatch")
        download_url = f"/api/v1/runs/{run_id}/artifacts/{file_id}/download"
        for name, expected in (
            ("filename", filename),
            ("mime_type", mime),
            ("storage_key", key),
            ("size_bytes", size),
            ("sha256", digest),
            ("download_url", download_url),
        ):
            if any(name in item and item[name] != expected for item in (file, metadata)):
                raise InvalidSource("generated archive metadata disagrees")
        if size > self._limits.max_archive_bytes:
            raise SourceTooLarge("archive exceeds limit")
        path = self._generated_root / key
        _path_chain(path)
        resolved = self._generated_store.resolve_for(
            principal.tenant_id,
            run_id,
            UUID(file_id),
            key,
        )
        if resolved != path:
            raise InvalidSource("generated archive path aliases another file")
        data, read = _read_file(path, self._limits.max_archive_bytes)
        reads.append(read)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise SourceChanged("generated archive changed")
        return _zip_files(data, self._limits)


def _generated_file_id(
    content: Mapping[str, object],
    file: Mapping[str, object],
    metadata: Mapping[str, object],
    *,
    fallback: str,
) -> str:
    # The selected outer artifact remains publication authority. Its generated
    # file result has a separate, consistent UUID used only for scoped storage.
    ids = [item["artifact_id"] for item in (content, file, metadata) if "artifact_id" in item]
    if not ids:
        return fallback
    file_id = ids[0]
    if (
        len(ids) != 3
        or type(file_id) is not str
        or str(UUID(file_id)) != file_id
        or any(type(value) is not str or value != file_id for value in ids)
    ):
        raise InvalidSource("generated archive identity disagrees")
    return file_id


def _association(record: RunRecord) -> tuple[object, object]:
    routing = record.routing_decision or {}
    return routing.get("project_id"), routing.get("workspace_session_id")


def _checked_source(source: ArtifactSource | WorkspaceSource) -> ArtifactSource | WorkspaceSource:
    try:
        if type(source) is ArtifactSource:
            checked = ArtifactSource.model_validate(source.model_dump(mode="python"))
            if str(UUID(checked.artifact_id)) != checked.artifact_id:
                raise ValueError
            return checked
        if type(source) is WorkspaceSource:
            workspace = WorkspaceSource.model_validate(source.model_dump(mode="python"))
            if not _SEGMENT.fullmatch(workspace.project_id) or not _SEGMENT.fullmatch(
                workspace.session_id,
            ):
                raise ValueError
            for path in workspace.paths:
                _safe_path(path)
            return workspace
    except (ValidationError, ValueError):
        raise InvalidSource("invalid source selection") from None
    raise InvalidSource("source identifiers are required; authorization DTOs are not inputs")


def _safe_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 255
        or any(c in path for c in "\\:%\x00")
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise InvalidSource("unsafe source path")
    for part in path.split("/"):
        safe_generated_filename(part)
    return path


def _validate_paths(paths: tuple[object, ...], limits: SourceLimits) -> None:
    if not paths:
        raise InvalidSource("source has no files")
    if len(paths) > limits.max_files:
        raise SourceTooLarge("source file count exceeds limit")
    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, str):
            raise InvalidSource("invalid source path")
        normalized = _safe_path(path).casefold()
        if normalized in seen:
            raise InvalidSource("duplicate or platform-aliased source paths")
        seen.add(normalized)
    if any(
        "/".join(path.split("/")[:i]) in seen
        for path in seen
        for i in range(1, len(path.split("/")))
    ):
        raise InvalidSource("source file conflicts with a directory")


def _check_sizes(size: int, total: int, limits: SourceLimits) -> None:
    if size > limits.max_file_bytes or total > limits.max_total_bytes:
        raise SourceTooLarge("source size exceeds limit")


def _file(path: str, content: bytes) -> SnapshotFile:
    return SnapshotFile(path, content, len(content), hashlib.sha256(content).hexdigest())


def _zip_files(data: bytes, limits: SourceLimits) -> tuple[SnapshotFile, ...]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        # Bound all entries, including directory-only archive floods.
        if len(members) > limits.max_files:
            raise SourceTooLarge("archive member count exceeds limit")
        paths: list[str] = []
        seen: set[str] = set()
        total = 0
        for member in members:
            mode = stat.S_IFMT(member.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                raise InvalidSource("archive contains links, devices or encrypted files")
            if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise InvalidSource("archive compression method is unsupported")
            path = _safe_path(member.filename[:-1] if member.is_dir() else member.filename)
            if path.casefold() in seen:
                raise InvalidSource("archive contains duplicate or aliased members")
            seen.add(path.casefold())
            if member.is_dir():
                continue
            if mode == stat.S_IFDIR:
                raise InvalidSource("archive member type disagrees")
            total += member.file_size
            _check_sizes(member.file_size, total, limits)
            if member.file_size > limits.max_compression_ratio * max(member.compress_size, 1):
                raise SourceTooLarge("archive compression ratio exceeds limit")
            paths.append(member.filename)
        _validate_paths(tuple(paths), limits)
        files: list[SnapshotFile] = []
        total = 0
        for member in members:
            if member.is_dir():
                continue
            with archive.open(member) as opened:
                content = opened.read(limits.max_file_bytes + 1)
            total += len(content)
            _check_sizes(len(content), total, limits)
            if len(content) != member.file_size:
                raise SourceChanged("archive member changed")
            files.append(_file(member.filename, content))
    return tuple(sorted(files, key=lambda item: item.path))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSource("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise InvalidSource("nonfinite JSON number")


def _validate_plugin(files: tuple[SnapshotFile, ...]) -> tuple[str, str]:
    # Existing boundaries parse manifest/member data only. Never call installers,
    # runtime validation/preflight, the signer, a scanner, or import an entrypoint.
    from agent_hub.api.routers.admin import (
        SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS,
        _plugin_archive_manifest_from_archive,
        _plugin_archive_signature_files,
    )
    from agent_hub.skills.package import InvalidSkillPackage

    manifests = [item for item in files if item.path == "plugin.json"]
    if len(manifests) != 1:
        raise InvalidSource("source must select root plugin.json")
    try:
        json.loads(
            manifests[0].content, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            for item in files:
                archive.writestr(item.path, item.content)
        data = buffer.getvalue()
        manifest = _plugin_archive_manifest_from_archive(data)
        package = manifest.package
        if (
            package is None
            or package.kind != "adapter_package"
            or package.entrypoint is None
            or package.sdk_api_version not in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS
            or package.package_version != manifest.version
        ):
            raise InvalidSource("source must declare a supported adapter package and entrypoint")
        _safe_path(package.entrypoint)
        # Reuse the package member boundary and ensure it did not discard a member.
        actual = _plugin_archive_signature_files(data)
        expected = tuple((item.path, item.content) for item in files if item.path != "plugin.json")
        if actual != expected:
            raise InvalidSource("package member selection is ambiguous")
        return manifest.id, manifest.version
    except (InvalidSkillPackage, ValidationError, ValueError, UnicodeError, RecursionError):
        raise InvalidSource("source is not a valid plugin package") from None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _path_chain(path: Path) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    chain: list[tuple[Path, tuple[int, int, int]]] = []
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InvalidSource("source cannot contain symlinks or reparse points")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise InvalidSource("source parent is not a directory")
        chain.append((part, _identity(info)))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InvalidSource("source must be a regular file with no aliases")
    return tuple(chain)


@dataclass(frozen=True, slots=True)
class _DiskRead:
    path: Path
    chain: tuple[tuple[Path, tuple[int, int, int]], ...]
    stamp: tuple[int, ...]
    sha256: str
    limit: int

    def verify(self) -> None:
        if _path_chain(self.path) != self.chain or _stamp(self.path.lstat()) != self.stamp:
            raise SourceChanged("source changed while freezing")


def _open_source(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if sys.platform == "win32":
        descriptor = os.open(path, flags | os.O_NOINHERIT)
        try:
            _check_windows_handle(descriptor, path)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    # Anchor every component to an already-open directory; no ancestor link can
    # redirect a Linux read even if renamed between the lexical checks and open.
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        return os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def _check_windows_handle(descriptor: int, expected: Path) -> None:
    if sys.platform != "win32":
        raise InvalidSource("unsupported handle validation")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_name = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    get_name.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_name.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32_768)
    length = get_name(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise InvalidSource("cannot validate source handle")
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]
    if os.path.normcase(final) != os.path.normcase(str(expected)):
        raise InvalidSource("source handle escaped its authorized path")


def _read_file(path: Path, limit: int) -> tuple[bytes, _DiskRead]:
    chain = _path_chain(path)
    before = path.lstat()
    if before.st_size > limit:
        raise SourceTooLarge("source file exceeds limit")
    descriptor = _open_source(path)
    try:
        opened = os.fstat(descriptor)
        # Python 3.12 Windows path-stat ctime is birth time while handle-stat may
        # expose change time. Compare them only within the same observation API.
        if (
            _stamp(opened)[:5] != _stamp(before)[:5]
            or opened.st_nlink != before.st_nlink
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise SourceChanged("source replaced before read")
        if _path_chain(path) != chain:
            raise SourceChanged("source parent changed before read")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(65_536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise SourceTooLarge("source file exceeds limit")
        if _stamp(os.fstat(descriptor)) != _stamp(opened) or size != opened.st_size:
            raise SourceChanged("source changed during read")
        content = b"".join(chunks)
        read = _DiskRead(path, chain, _stamp(before), hashlib.sha256(content).hexdigest(), limit)
        read.verify()
        return content, read
    finally:
        os.close(descriptor)
