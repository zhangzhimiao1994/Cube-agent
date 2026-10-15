"""Private, immutable persistence of source bytes, not a grant or installation.

Only trusted services supply FrozenSourceSnapshot, creator provenance and saved
references. This store neither authenticates creators nor verifies source grants.
It preserves the freezer's source hash; that hash cannot be recomputed without
the original actor/artifact metadata. Reads rehash the complete canonical blob.

Publication uses an exclusive tenant lock and atomic no-replace hardlink. All
complete published objects survive caller rollback or unknown DB commit results.
Recovery promotes complete private staging, removes only unlinked incomplete
staging, and never deletes or repairs a published object. No user code is loaded,
extracted, signed or executed. Calls are synchronous; async callers use a worker.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_hub.plugins.handover_sources import (
    FrozenSourceSnapshot,
    InvalidSource,
    SnapshotFile,
    SourceLimits,
    SourceTooLarge,
    _canonical,
    _check_windows_handle,
    _checked_source,
    _reject_constant,
    _stamp,
    _unique_object,
    _validate_paths,
)
from agent_hub.plugins.handover_storage import AuthorizedSource

_NAMESPACE = ".handover-snapshots"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_STAGE = re.compile(r"t-[a-f0-9]{32}\Z")
_DOMAIN: Literal["generated-handover-frozen-snapshot"] = "generated-handover-frozen-snapshot"
_RECOVERY_LIMIT = 64


class InvalidSnapshot(ValueError):
    pass


class SnapshotTooLarge(InvalidSnapshot):
    pass


class SnapshotUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotStoreLimits:
    source: SourceLimits = field(default_factory=SourceLimits)
    max_blob_bytes: int = 3_000_000
    max_snapshots_per_tenant: int = 256

    def __post_init__(self) -> None:
        if type(self.source) is not SourceLimits or any(
            type(value) is not int or value < 1 or value > ceiling
            for value, ceiling in (
                (self.max_blob_bytes, 3_000_000),
                (self.max_snapshots_per_tenant, 4096),
            )
        ):
            raise ValueError("invalid snapshot store limits")
        replace(self.source)


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    snapshot_id: str
    blob_sha256: str
    blob_size_bytes: int
    source_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class StoredSourceSnapshot:
    reference: SnapshotReference
    creator_id: UUID
    snapshot: FrozenSourceSnapshot


class _BlobFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: Annotated[str, Field(min_length=1, max_length=255)]
    size_bytes: Annotated[int, Field(ge=0, le=512_000)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
    content_base64: Annotated[str, Field(max_length=682_668)]


class _Blob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    domain: Literal["generated-handover-frozen-snapshot"]
    creator_id: UUID
    authorized: AuthorizedSource
    plugin_id: Annotated[str, Field(min_length=1, max_length=128)]
    plugin_version: Annotated[str, Field(min_length=1, max_length=128)]
    total_size_bytes: Annotated[int, Field(ge=0, le=2_000_000)]
    files: Annotated[tuple[_BlobFile, ...], Field(min_length=1, max_length=200)]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _native_path(path: Path) -> Path:
    if sys.platform != "win32":
        return path
    value = str(path)
    if value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _checked_snapshot(snapshot: FrozenSourceSnapshot, limits: SourceLimits) -> None:
    if (
        type(snapshot) is not FrozenSourceSnapshot
        or type(snapshot.authorized) is not AuthorizedSource
        or type(snapshot.files) is not tuple
        or type(snapshot.total_size_bytes) is not int
        or any(type(file) is not SnapshotFile for file in snapshot.files)
    ):
        raise InvalidSnapshot("invalid frozen snapshot")
    try:
        authorized = AuthorizedSource.model_validate(snapshot.authorized.model_dump(mode="python"))
        source = _checked_source(authorized.source)
        paths = tuple(file.path for file in snapshot.files)
        _validate_paths(paths, limits)
        if source.kind == "workspace" and tuple(sorted(paths)) != source.paths:
            raise InvalidSnapshot("source selection does not match frozen members")
        if paths != tuple(sorted(paths)):
            raise InvalidSnapshot("frozen members are not canonical")
        total = 0
        for file in snapshot.files:
            if type(file.content) is not bytes or type(file.size_bytes) is not int:
                raise InvalidSnapshot("invalid frozen member")
            if file.size_bytes > limits.max_file_bytes:
                raise SnapshotTooLarge("snapshot member exceeds limit")
            if len(file.content) != file.size_bytes or _digest(file.content) != file.sha256:
                raise InvalidSnapshot("frozen member identity does not match bytes")
            total += file.size_bytes
        if total > limits.max_total_bytes:
            raise SnapshotTooLarge("snapshot total exceeds limit")
        if total != snapshot.total_size_bytes:
            raise InvalidSnapshot("snapshot total does not match bytes")
        manifests = [file for file in snapshot.files if file.path == "plugin.json"]
        if len(manifests) != 1:
            raise InvalidSnapshot("snapshot requires original plugin.json")
        manifest = json.loads(
            manifests[0].content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(manifest, dict) or (manifest.get("id"), manifest.get("version")) != (
            snapshot.plugin_id,
            snapshot.plugin_version,
        ):
            raise InvalidSnapshot("manifest identity does not match snapshot")
    except SourceTooLarge:
        raise SnapshotTooLarge("snapshot exceeds source limits") from None
    except (
        InvalidSource,
        ValidationError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
    ) as error:
        if isinstance(error, InvalidSnapshot):
            raise
        raise InvalidSnapshot("invalid frozen snapshot") from None


def _encode(snapshot: FrozenSourceSnapshot, creator_id: UUID, limits: SnapshotStoreLimits) -> bytes:
    if type(creator_id) is not UUID:
        raise InvalidSnapshot("creator provenance requires an identifier")
    _checked_snapshot(snapshot, limits.source)
    try:
        blob = _Blob(
            schema_version=1,
            domain=_DOMAIN,
            creator_id=creator_id,
            authorized=snapshot.authorized,
            plugin_id=snapshot.plugin_id,
            plugin_version=snapshot.plugin_version,
            total_size_bytes=snapshot.total_size_bytes,
            files=tuple(
                _BlobFile(
                    path=file.path,
                    size_bytes=file.size_bytes,
                    sha256=file.sha256,
                    content_base64=base64.b64encode(file.content).decode("ascii"),
                )
                for file in snapshot.files
            ),
        )
        data = _canonical(blob.model_dump(mode="json"))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise InvalidSnapshot("invalid snapshot provenance") from None
    if len(data) > limits.max_blob_bytes:
        raise SnapshotTooLarge("snapshot blob exceeds limit")
    return data


def _decode(data: bytes, tenant_id: UUID, limits: SnapshotStoreLimits) -> StoredSourceSnapshot:
    try:
        raw = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            raise InvalidSnapshot("invalid snapshot schema")
        blob = _Blob.model_validate_json(data)
        if blob.authorized.tenant_id != tenant_id:
            raise InvalidSnapshot("snapshot scope does not match reference")
        files = tuple(
            SnapshotFile(
                file.path,
                base64.b64decode(file.content_base64, validate=True),
                file.size_bytes,
                file.sha256,
            )
            for file in blob.files
        )
        snapshot = FrozenSourceSnapshot(
            blob.authorized, files, blob.plugin_id, blob.plugin_version, blob.total_size_bytes
        )
        if _encode(snapshot, blob.creator_id, limits) != data:
            raise InvalidSnapshot("snapshot blob is not canonical")
    except (
        InvalidSource,
        ValidationError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
        binascii.Error,
    ) as error:
        if isinstance(error, InvalidSnapshot):
            raise
        raise InvalidSnapshot("invalid snapshot blob") from None
    digest = _digest(data)
    return StoredSourceSnapshot(
        SnapshotReference(
            "hs-" + digest, digest, len(data), blob.authorized.source_manifest_sha256
        ),
        blob.creator_id,
        snapshot,
    )


def _chain(path: Path) -> tuple[tuple[int, int, int], ...]:
    result = []
    for component in (*reversed(path.parents), path):
        info = _native_path(component).lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InvalidSnapshot("snapshot directory contains links or reparse points")
        result.append((info.st_dev, info.st_ino, info.st_mode))
    return tuple(result)


def _windows_security(path: Path, *, private: bool, create: bool = False) -> None:
    if sys.platform != "win32":
        raise InvalidSnapshot("unsupported ownership validation")
    import ctypes
    from ctypes import wintypes

    native_path = str(_native_path(path))
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (wintypes.LPVOID,)
    advapi.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise InvalidSnapshot("cannot verify snapshot owner")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(size)):
            raise InvalidSnapshot("cannot verify snapshot owner")
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        if create:
            text_sid = wintypes.LPWSTR()
            advapi.ConvertSidToStringSidW.argtypes = (
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.LPWSTR),
            )
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text_sid)):
                raise InvalidSnapshot("cannot create private snapshot directory")
            try:
                descriptor = wintypes.LPVOID()
                advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.LPVOID),
                    wintypes.LPVOID,
                )
                if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    f"O:{text_sid.value}D:P(A;OICI;FA;;;{text_sid.value})",
                    1,
                    ctypes.byref(descriptor),
                    None,
                ):
                    raise InvalidSnapshot("cannot create private snapshot directory")

                class SecurityAttributes(ctypes.Structure):
                    _fields_ = [
                        ("length", wintypes.DWORD),
                        ("descriptor", wintypes.LPVOID),
                        ("inherit", wintypes.BOOL),
                    ]

                attributes = SecurityAttributes(
                    ctypes.sizeof(SecurityAttributes), descriptor, False
                )
                kernel.CreateDirectoryW.argtypes = (
                    wintypes.LPCWSTR,
                    ctypes.POINTER(SecurityAttributes),
                )
                try:
                    if (
                        not kernel.CreateDirectoryW(native_path, ctypes.byref(attributes))
                        and ctypes.get_last_error() != 183
                    ):
                        raise OSError("private directory creation failed")
                finally:
                    kernel.LocalFree(descriptor)
            finally:
                kernel.LocalFree(ctypes.cast(text_sid, wintypes.LPVOID))
        owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
        result = advapi.GetNamedSecurityInfoW(
            native_path,
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise InvalidSnapshot("cannot verify snapshot ownership")
        try:
            if not advapi.EqualSid(owner, sid):
                raise InvalidSnapshot("snapshot root has another owner")
            if not dacl:
                raise InvalidSnapshot("snapshot ACL is unsafe")

            if dacl:

                class AclSize(ctypes.Structure):
                    _fields_ = [
                        ("count", wintypes.DWORD),
                        ("used", wintypes.DWORD),
                        ("free", wintypes.DWORD),
                    ]

                acl_size = AclSize()
                advapi.GetAclInformation.argtypes = (
                    wintypes.LPVOID,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.c_int,
                )
                advapi.GetAce.argtypes = (
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.LPVOID),
                )
                advapi.IsWellKnownSid.argtypes = (wintypes.LPVOID, ctypes.c_int)
                if (
                    not advapi.GetAclInformation(
                        dacl, ctypes.byref(acl_size), ctypes.sizeof(acl_size), 2
                    )
                    or acl_size.count < 1
                ):
                    raise InvalidSnapshot("snapshot ACL is not private")
                for index in range(acl_size.count):
                    ace = wintypes.LPVOID()
                    if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or ace.value is None:
                        raise InvalidSnapshot("cannot verify snapshot ACL")
                    header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
                    if not private and (header[1] & 8 or header[0] == 1):
                        continue
                    if header[0] != 0:
                        raise InvalidSnapshot("snapshot ACL cannot be verified")
                    ace_sid = wintypes.LPVOID(ace.value + 8)
                    if advapi.EqualSid(ace_sid, sid):
                        continue
                    if private:
                        raise InvalidSnapshot("snapshot ACL permits another identity")
                    mask = ctypes.cast(
                        wintypes.LPVOID(ace.value + 4), ctypes.POINTER(wintypes.DWORD)
                    )[0]
                    # The package root may be readable; only owner/SYSTEM/admin
                    # may replace its private namespace or change its security.
                    if mask & 0x500D0156 and not (
                        advapi.IsWellKnownSid(ace_sid, 22) or advapi.IsWellKnownSid(ace_sid, 26)
                    ):
                        raise InvalidSnapshot("snapshot package root permits another writer")
        finally:
            kernel.LocalFree(descriptor)
    finally:
        kernel.CloseHandle(token)


def _ownership(path: Path, info: os.stat_result, *, private: bool, directory: bool) -> None:
    if sys.platform == "win32":
        _windows_security(path, private=private)
    elif info.st_uid != os.geteuid() or (
        (stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600))
        if private
        else bool(stat.S_IMODE(info.st_mode) & 0o022)
    ):
        raise InvalidSnapshot("snapshot root or object ownership is unsafe")


class _Directory:
    def __init__(self, path: Path, *, private: bool) -> None:
        self.path, self.private, self.fd = path, private, None
        self.chain = _chain(path)
        _ownership(path, _native_path(path).lstat(), private=private, directory=True)
        if sys.platform != "win32":
            fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for component in path.parts[1:]:
                    child = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    os.close(fd)
                    fd = child
                self.fd = fd
                self.check()
            except BaseException:
                os.close(fd)
                self.fd = None
                raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def check(self) -> None:
        if _chain(self.path) != self.chain:
            raise InvalidSnapshot("snapshot directory identity changed")
        info = _native_path(self.path).lstat()
        if self.fd is not None:
            opened = os.fstat(self.fd)
            if (opened.st_dev, opened.st_ino, opened.st_mode) != self.chain[-1]:
                raise InvalidSnapshot("snapshot directory handle changed")
        _ownership(self.path, info, private=self.private, directory=True)

    def child(self, name: str) -> _Directory:
        self.check()
        path = self.path / name
        if self.fd is None:
            _windows_security(path, private=True, create=True)
        else:
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.fd)
                self.sync()
            except FileExistsError:
                pass
        child = _Directory(path, private=True)
        try:
            self.check()
        except BaseException:
            child.close()
            raise
        return child

    def open(self, name: str, flags: int) -> int:
        self.check()
        try:
            before = self.info(name)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
                raise InvalidSnapshot("snapshot object contains a link or reparse point")
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        if sys.platform == "win32":
            descriptor = os.open(_native_path(self.path / name), flags | os.O_NOINHERIT, 0o600)
        else:
            if self.fd is None:
                raise InvalidSnapshot("snapshot directory is closed")
            descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            if self.fd is None:
                try:
                    expected = str(self.path / name)
                    if expected.startswith("\\\\?\\UNC\\"):
                        expected = "\\\\" + expected[8:]
                    elif expected.startswith("\\\\?\\"):
                        expected = expected[4:]
                    _check_windows_handle(descriptor, Path(expected))
                except InvalidSource:
                    raise InvalidSnapshot("snapshot handle escaped its private namespace") from None
            self.check()
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise InvalidSnapshot("snapshot object must be a regular private file")
            _ownership(self.path / name, info, private=True, directory=False)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def info(self, name: str) -> os.stat_result:
        self.check()
        return os.stat(
            _native_path(self.path / name) if self.fd is None else name,
            dir_fd=self.fd,
            follow_symlinks=False,
        )

    def read(self, name: str, limit: int, *, links: tuple[int, ...] = (1,)) -> bytes:
        descriptor = self.open(name, os.O_RDONLY)
        try:
            before = os.fstat(descriptor)
            if before.st_nlink not in links:
                raise InvalidSnapshot("snapshot object has an unexpected alias")
            if before.st_size > limit:
                raise SnapshotTooLarge("snapshot blob exceeds limit")
            data = bytearray()
            while chunk := os.read(descriptor, min(65_536, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise SnapshotTooLarge("snapshot blob exceeds limit")
            after = self.info(name)
            if (
                _stamp(before) != _stamp(os.fstat(descriptor))
                or len(data) != before.st_size
                or (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_nlink,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_nlink,
                )
            ):
                raise InvalidSnapshot("snapshot object changed during read")
            return bytes(data)
        finally:
            os.close(descriptor)

    def entries(self, limit: int) -> tuple[str, ...]:
        self.check()
        names = []
        with os.scandir(_native_path(self.path) if self.fd is None else self.fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > limit:
                    raise SnapshotTooLarge("snapshot namespace exceeds recovery limit")
        self.check()
        if any(
            name != "lock" and not _SHA.fullmatch(name) and not _STAGE.fullmatch(name)
            for name in names
        ):
            raise InvalidSnapshot("snapshot namespace has an unknown object")
        return tuple(sorted(names))

    def publish(self, stage: str, digest: str) -> None:
        self.check()
        os.link(
            _native_path(self.path / stage) if self.fd is None else stage,
            _native_path(self.path / digest) if self.fd is None else digest,
            src_dir_fd=self.fd,
            dst_dir_fd=self.fd,
            follow_symlinks=False,
        )
        self.check()

    def unlink_stage(self, name: str, expected: os.stat_result) -> None:
        self.check()
        if _stamp(self.info(name)) != _stamp(expected):
            raise InvalidSnapshot("snapshot staging identity changed")
        os.unlink(_native_path(self.path / name) if self.fd is None else name, dir_fd=self.fd)
        self.check()

    def verify_publication(
        self, stage: str, digest: str, data: bytes, expected: os.stat_result
    ) -> os.stat_result:
        staged, published = self.info(stage), self.info(digest)
        identity = (expected.st_dev, expected.st_ino)
        if (
            (staged.st_dev, staged.st_ino) != identity
            or (published.st_dev, published.st_ino) != identity
            or staged.st_nlink != 2
            or published.st_nlink != 2
            or self.read(digest, len(data), links=(2,)) != data
        ):
            raise InvalidSnapshot("snapshot publication identity changed")
        return self.info(stage)

    def sync(self) -> None:
        if self.fd is not None:
            os.fsync(self.fd)
        self.check()


@contextlib.contextmanager
def _lock(directory: _Directory) -> Iterator[None]:
    descriptor = directory.open("lock", os.O_RDWR | os.O_CREAT)
    acquired = False
    try:
        if os.fstat(descriptor).st_nlink != 1:
            raise InvalidSnapshot("snapshot lock has an alias")
        # Windows byte locks may extend beyond EOF; initializing that byte
        # before locking races another opener that has already acquired it.
        deadline = time.monotonic() + 5
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SnapshotUnavailable("snapshot publication lock is unavailable") from None
                time.sleep(0.01)
        directory.check()
        yield
    finally:
        try:
            if acquired:
                if sys.platform == "win32":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class FrozenSourceSnapshotStore:
    def __init__(self, *, package_root: Path, limits: SnapshotStoreLimits | None = None) -> None:
        self._root = Path(os.path.abspath(package_root))
        if limits is not None and type(limits) is not SnapshotStoreLimits:
            raise ValueError("invalid snapshot store limits")
        self._limits = (
            replace(limits, source=replace(limits.source))
            if limits is not None
            else SnapshotStoreLimits()
        )
        try:
            if self._root == Path(self._root.anchor):
                raise InvalidSnapshot("snapshot package root is invalid")
            _chain(self._root.parent)
            if sys.platform == "win32":
                if not _native_path(self._root).exists():
                    _windows_security(self._root, private=True, create=True)
            else:
                try:
                    self._root.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            directory = _Directory(self._root, private=False)
            try:
                self._root_identity = directory.chain[-1]
            finally:
                directory.close()
        except OSError:
            raise SnapshotUnavailable("snapshot package root is unavailable") from None

    @contextlib.contextmanager
    def _tenant(self, tenant_id: UUID) -> Iterator[_Directory]:
        if type(tenant_id) is not UUID:
            raise InvalidSnapshot("snapshot tenant requires an identifier")
        try:
            with contextlib.ExitStack() as stack:
                directory = _Directory(self._root, private=False)
                stack.callback(directory.close)
                if directory.chain[-1] != self._root_identity:
                    raise InvalidSnapshot("snapshot package root was replaced")
                for name in (_NAMESPACE, "v1", tenant_id.hex):
                    directory = directory.child(name)
                    stack.callback(directory.close)
                with _lock(directory):
                    yield directory
        except FileNotFoundError:
            raise InvalidSnapshot("snapshot object is unavailable") from None
        except OSError:
            raise SnapshotUnavailable("snapshot storage is unavailable") from None

    def _recover(self, directory: _Directory, tenant_id: UUID) -> int:
        names = directory.entries(self._limits.max_snapshots_per_tenant + _RECOVERY_LIMIT + 1)
        stages = [name for name in names if _STAGE.fullmatch(name)]
        if len(stages) > _RECOVERY_LIMIT:
            raise SnapshotTooLarge("too many snapshot staging objects")
        recovered = 0
        for name in stages:
            data = directory.read(name, self._limits.max_blob_bytes, links=(1, 2))
            info = directory.info(name)
            try:
                stored = _decode(data, tenant_id, self._limits)
            except SnapshotTooLarge:
                raise
            except InvalidSnapshot:
                try:
                    json.loads(data)
                except RecursionError:
                    raise InvalidSnapshot("staging complexity requires trusted recovery") from None
                except (ValueError, UnicodeError):
                    pass
                else:
                    raise InvalidSnapshot("complete staging requires trusted recovery") from None
                if info.st_nlink != 1:
                    raise InvalidSnapshot("incomplete staging aliases a shared object") from None
                directory.unlink_stage(name, info)
                directory.sync()
                recovered += 1
                continue
            digest = stored.reference.blob_sha256
            try:
                existing = directory.info(digest)
            except FileNotFoundError:
                if info.st_nlink != 1:
                    raise InvalidSnapshot("staging object has an unknown alias") from None
                if (
                    sum(bool(_SHA.fullmatch(item)) for item in names)
                    >= self._limits.max_snapshots_per_tenant
                ):
                    raise SnapshotTooLarge("snapshot object quota exceeded") from None
                directory.publish(name, digest)
                info = directory.verify_publication(name, digest, data, info)
            else:
                shared_link = (existing.st_dev, existing.st_ino) == (info.st_dev, info.st_ino)
                if shared_link:
                    if existing.st_nlink != 2 or info.st_nlink != 2:
                        raise InvalidSnapshot("staging aliases an unexpected object")
                elif info.st_nlink != 1:
                    raise InvalidSnapshot("staging has an unknown shared alias")
                if (
                    directory.read(
                        digest, self._limits.max_blob_bytes, links=(2,) if shared_link else (1,)
                    )
                    != data
                ):
                    raise InvalidSnapshot("existing immutable snapshot changed")
                if _stamp(directory.info(name)) != _stamp(info):
                    raise InvalidSnapshot("snapshot staging changed during recovery")
            directory.unlink_stage(name, info)
            directory.sync()
            recovered += 1
            names = directory.entries(self._limits.max_snapshots_per_tenant + _RECOVERY_LIMIT + 1)
        return recovered

    def recover(self, *, tenant_id: UUID) -> int:
        with self._tenant(tenant_id) as directory:
            return self._recover(directory, tenant_id)

    def publish(self, snapshot: FrozenSourceSnapshot, *, creator_id: UUID) -> SnapshotReference:
        data = _encode(snapshot, creator_id, self._limits)
        stored = _decode(data, snapshot.authorized.tenant_id, self._limits)
        digest = stored.reference.blob_sha256
        with self._tenant(snapshot.authorized.tenant_id) as directory:
            self._recover(directory, snapshot.authorized.tenant_id)
            try:
                existing = directory.read(digest, self._limits.max_blob_bytes)
            except FileNotFoundError:
                pass
            else:
                if existing != data:
                    raise InvalidSnapshot("existing immutable snapshot changed")
                return stored.reference
            names = directory.entries(self._limits.max_snapshots_per_tenant + _RECOVERY_LIMIT + 1)
            if (
                sum(bool(_SHA.fullmatch(name)) for name in names)
                >= self._limits.max_snapshots_per_tenant
            ):
                raise SnapshotTooLarge("snapshot object quota exceeded")
            stage = "t-" + uuid4().hex
            descriptor = directory.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("snapshot write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            info = directory.info(stage)
            if directory.read(stage, self._limits.max_blob_bytes) != data:
                raise InvalidSnapshot("snapshot staging bytes changed")
            if _stamp(directory.info(stage)) != _stamp(info):
                raise InvalidSnapshot("snapshot staging changed before publication")
            directory.publish(stage, digest)
            info = directory.verify_publication(stage, digest, data, info)
            directory.unlink_stage(stage, info)
            directory.sync()
            if directory.read(digest, self._limits.max_blob_bytes) != data:
                raise InvalidSnapshot("snapshot published bytes changed")
            return stored.reference

    def read(
        self, *, tenant_id: UUID, snapshot_id: str, blob_sha256: str, source_manifest_sha256: str
    ) -> StoredSourceSnapshot:
        if (
            type(snapshot_id) is not str
            or type(blob_sha256) is not str
            or type(source_manifest_sha256) is not str
            or not _SHA.fullmatch(blob_sha256)
            or not _SHA.fullmatch(source_manifest_sha256)
            or snapshot_id != "hs-" + blob_sha256
        ):
            raise InvalidSnapshot("invalid opaque snapshot reference")
        with self._tenant(tenant_id) as directory:
            self._recover(directory, tenant_id)
            data = directory.read(blob_sha256, self._limits.max_blob_bytes)
            if _digest(data) != blob_sha256:
                raise InvalidSnapshot("snapshot blob does not match saved hash")
            stored = _decode(data, tenant_id, self._limits)
            if stored.reference.source_manifest_sha256 != source_manifest_sha256:
                raise InvalidSnapshot("snapshot source hash does not match saved reference")
            return stored
