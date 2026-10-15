"""Internal same-BEGIN confirmation bridge, never an API DTO authorization path.

No signing, activation or execution. SQL failure/unknown commit retains package
bytes: reclamation needs a separate ownership protocol. Stage locks coordinate
trusted package writers; pinned directories and no-replace publication preserve
unknown/shared objects. Exact members are rechecked after SQL waits.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied
from agent_hub.db.models import AdminResourceRow
from agent_hub.plugins.handover_grants import DurableSourceGrantService, GrantBinding, SourceGrant
from agent_hub.plugins.handover_keys import load_locked_signing_key
from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, StoredSourceSnapshot
from agent_hub.plugins.handover_sources import SourceLimits
from agent_hub.plugins.handover_storage import (
    GENERATED_HANDOVER_MARKER,
    Conflict,
    HandoverReceipt,
    PluginHandoverStorage,
    VerifiedPendingPlugin,
    _check_confirmation_identity,
    _receipt,
    _safe_key,
    _utc,
    acquire_plugin_registration_lock,
)
from agent_hub.plugins.handover_verification import (
    FrozenSigningKey,
    _archive_files,
    _contract,
    _manifest,
    verify_signed_snapshot_package,
)

if TYPE_CHECKING:
    from agent_hub.plugins.handover_stage_journal import StagePublicationJournal
    from agent_hub.plugins.handover_stage_reservations import (
        ScopeLockReservation,
        StagePublicationContext,
        StageReservationStore,
    )


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _unlinked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _node(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _windows_open(path: Path, *, directory: bool, flags: int = 0) -> int:
    """Pin Windows ancestors without FILE_SHARE_DELETE; open leaves as reparse points."""
    if sys.platform != "win32":
        raise Conflict("Windows file handles unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    from agent_hub.plugins.handover_snapshots import _native_path

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    attributes = kernel.GetFileInformationByHandleEx
    attributes.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    attributes.restype = wintypes.BOOL

    class AttributeInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    disposition = 3 if directory or not flags & os.O_CREAT else (1 if flags & os.O_EXCL else 4)
    handle = create(
        str(_native_path(path)),
        0x80000000 | (0 if directory else 0x40000000),
        3,
        None,
        disposition,
        0x00200000 | (0x02000000 if directory else 0),
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = AttributeInfo()
        if not attributes(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise Conflict("package handle has an unsafe node or reparse point")
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_BINARY | (os.O_RDONLY if directory else os.O_RDWR)
        )
    except BaseException:
        close(handle)
        raise
    return descriptor


class _PinnedDirectory:
    """Descriptor-relative Linux I/O; non-deletable ancestor handles on Windows."""

    def __init__(self, path: Path, *, create: bool = False) -> None:
        _directory_chain(path)
        self.path, self.fd = path, -1
        self.handles: list[int] = []
        self.nodes: list[tuple[Path, tuple[int, int, int]]] = []
        current = Path(path.anchor)
        try:
            if sys.platform == "win32":
                self.handles.append(_windows_open(current, directory=True))
            else:
                self.fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in path.parts[1:]:
                current /= part
                if create:
                    try:
                        if sys.platform == "win32":
                            current.mkdir(mode=0o750)
                        else:
                            os.mkdir(part, mode=0o750, dir_fd=self.fd)
                            os.fsync(self.fd)
                    except FileExistsError:
                        pass
                if sys.platform == "win32":
                    self.handles.append(_windows_open(current, directory=True))
                else:
                    opened = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd
                    )
                    previous, self.fd = self.fd, opened
                    os.close(previous)
                info = current.lstat()
                if _unlinked(info) or not stat.S_ISDIR(info.st_mode):
                    raise Conflict("package directory contains an alias")
                self.nodes.append((current, _node(info)))
            self.check()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        handles, self.handles = self.handles, []
        descriptor, self.fd = self.fd, -1
        if descriptor >= 0:
            handles.append(descriptor)
        failure: OSError | None = None
        for handle in handles:
            try:
                os.close(handle)
            except OSError as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def sync(self) -> None:
        self.check()
        if self.fd >= 0:
            os.fsync(self.fd)
        self.check()

    def check(self) -> None:
        for path, identity in self.nodes:
            info = path.lstat()
            if _unlinked(info) or _node(info) != identity:
                raise Conflict("package directory identity changed")
        if self.fd >= 0 and _node(os.fstat(self.fd)) != self.nodes[-1][1]:
            raise Conflict("package directory descriptor changed")

    def info(self, name: str) -> os.stat_result:
        self.check()
        if sys.platform == "win32":
            return (self.path / name).lstat()
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def _native_open(self, name: str, flags: int) -> int:
        if sys.platform == "win32":
            return _windows_open(self.path / name, directory=False, flags=flags)
        return os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=self.fd
        )

    def open(self, name: str, flags: int) -> int:
        self.check()
        if name in {"", ".", ".."} or "/" in name or "\\" in name:
            raise Conflict("unsafe package leaf")
        try:
            before = self.info(name)
        except FileNotFoundError:
            before = None
        if before is not None and (
            _unlinked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
        ):
            raise Conflict("package leaf contains an alias")
        descriptor = self._native_open(name, flags)
        try:
            self.check()
            opened, current = os.fstat(descriptor), self.info(name)
            if (
                _unlinked(current)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _node(opened) != _node(current)
                or (before is not None and _node(opened) != _node(before))
            ):
                raise Conflict("package leaf descriptor identity changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def mkdir(self, name: str) -> None:
        self.check()
        if sys.platform == "win32":
            (self.path / name).mkdir(mode=0o750)
        else:
            os.mkdir(name, mode=0o750, dir_fd=self.fd)
        self.check()


def _rename_noreplace(parent: _PinnedDirectory, stage: str, digest: str) -> None:
    parent.check()
    if sys.platform == "win32":
        os.rename(parent.path / stage, parent.path / digest)
    elif sys.platform == "linux":
        import ctypes

        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise Conflict("atomic no-replace package publication unavailable")
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(parent.fd, os.fsencode(stage), parent.fd, os.fsencode(digest), 1):
            error = ctypes.get_errno()
            raise OSError(error, "atomic package publication failed")
    else:
        raise Conflict("safe package publication unsupported on this platform")
    parent.check()


def _publication_node(info: os.stat_result) -> dict[str, int]:
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": info.st_mode,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "nlink": info.st_nlink,
    }


def _check_created_member(
    directory: _PinnedDirectory, name: str, descriptor: int, expected: dict[str, int],
) -> None:
    for info in (os.fstat(descriptor), directory.info(name)):
        if (
            _unlinked(info) or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or _publication_node(info) != expected
        ):
            raise Conflict("package artifact file identity changed")


def _request_publication_reservations(request: Request) -> StageReservationStore | None:
    from agent_hub.plugins.handover_stage_reservations import StageReservationStore

    selected = getattr(request.app.state, "plugin_publication_reservations", None)
    if selected is None:
        return None
    if type(selected) is not StageReservationStore:
        raise Conflict("publication reservation configuration unavailable")
    return selected


def _publish_artifact(
    root: Path,
    files: tuple[tuple[str, bytes], ...],
    *,
    store_root: Path | None = None,
    tenant: UUID | None = None,
    plugin_id: str | None = None,
    reservation_store: StageReservationStore | None = None,
    deadline: datetime | None = None,
    revalidate: Callable[[], bool] | None = None,
) -> bool:
    if reservation_store is None:
        return _publish_artifact_locked(
            root, files, store_root=store_root, tenant=tenant, plugin_id=plugin_id,
        )
    from agent_hub.plugins.handover_stage_reservations import StageReservationStore

    if (
        type(reservation_store) is not StageReservationStore
        or store_root is None or reservation_store.store_root != store_root
        or tenant is None or plugin_id is None
        or root != store_root / str(tenant) / plugin_id / root.name
        or deadline is None or not callable(revalidate)
    ):
        raise Conflict("publication reservation configuration differs")
    try:
        existing = _PinnedDirectory(root.parent)
    except FileNotFoundError:
        pass
    else:
        try:
            try:
                existing.info(root.name)
            except FileNotFoundError:
                pass
            else:
                _artifact_matches(root, files, required=True)
                existing.check()
                return False
        finally:
            existing.close()
    with reservation_store.publication(tenant=tenant, plugin_id=plugin_id) as publication:
        return _publish_artifact_locked(
            root, files, store_root=store_root, tenant=tenant, plugin_id=plugin_id,
            publication=publication, deadline=deadline, revalidate=revalidate,
        )


def _publish_artifact_locked(
    root: Path,
    files: tuple[tuple[str, bytes], ...],
    *,
    store_root: Path | None = None,
    tenant: UUID | None = None,
    plugin_id: str | None = None,
    publication: StagePublicationContext | None = None,
    deadline: datetime | None = None,
    revalidate: Callable[[], bool] | None = None,
) -> bool:
    """Never rmtree or replace any existing canonical object, including an empty directory."""
    if store_root is not None and (
        tenant is None or plugin_id is None
        or root != store_root / str(tenant) / plugin_id / root.name
    ):
        raise Conflict("publication journal scope differs from artifact")
    if store_root is None and (tenant is not None or plugin_id is not None):
        raise Conflict("publication journal store absent")
    stage = "tmp-gh-" + uuid4().hex
    directories = {
        ancestor
        for name, _ in files
        for ancestor in PurePosixPath(name).parents
        if str(ancestor) != "."
    }
    reservation = None
    if publication is not None:
        assert tenant is not None and plugin_id is not None
        assert deadline is not None and revalidate is not None
        reservation = publication.reserve(
            operation_id=uuid4(), tenant=tenant, plugin_id=plugin_id,
            stage_name=stage, target_digest=root.name,
            source_bytes=sum(len(data) for _, data in files),
            source_members=1 + len(files) + len(directories),
            deadline=deadline, revalidate=revalidate,
        )
    parent = _PinnedDirectory(root.parent, create=True)
    journal: StagePublicationJournal | None = None
    try:
        from agent_hub.plugins.handover_stage_journal import StagePublicationJournal

        try:
            parent.info(root.name)
        except FileNotFoundError:
            pass
        else:
            _artifact_matches(root, files, required=True)
            parent.check()
            return False
        if store_root is not None and tenant is not None and plugin_id is not None:
            journal = StagePublicationJournal(
                store_root=store_root,
                tenant=tenant,
                plugin_id=plugin_id,
                stage_name=stage,
                target_digest=root.name,
                reserved_bytes=sum(len(data) for _, data in files),
                reserved_members=1 + len(files) + len(directories),
                member_budget=1 + len(files) + len(directories),
                reservation=reservation,
                publication=publication,
            )
        parent.mkdir(stage)
        stage_path = parent.path / stage
        owned = _PinnedDirectory(stage_path)
        try:
            original = _node(stage_path.lstat())
            if journal is not None:
                journal.record("recording", {
                    "kind": "stage_created", "path": stage,
                    "node": _publication_node(stage_path.lstat()),
                })
            inventory: list[dict[str, object]] = []
            for name, data in files:
                parts = PurePosixPath(name).parts
                directory = _PinnedDirectory(stage_path.joinpath(*parts[:-1]), create=True)
                try:
                    descriptor = directory.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    try:
                        created = _publication_node(os.fstat(descriptor))
                        if journal is not None:
                            journal.record("recording", {
                                "kind": "file_created", "path": name, "node": created,
                            })

                        view = memoryview(data)
                        _check_created_member(directory, parts[-1], descriptor, created)
                        while view:
                            _check_created_member(directory, parts[-1], descriptor, created)
                            count = os.write(descriptor, view)
                            if not 0 < count <= len(view):
                                raise Conflict("package artifact write failed")
                            view = view[count:]
                        os.fsync(descriptor)
                        _check_created_member(directory, parts[-1], descriptor, created)
                        member: dict[str, object] = {
                            "path": name,
                            "node": _publication_node(os.fstat(descriptor)),
                            "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                        inventory.append(member)
                        if journal is not None:
                            journal.record("recording", {"kind": "file_flushed", **member})
                    finally:
                        os.close(descriptor)
                finally:
                    directory.close()
            _artifact_matches(stage_path, files, required=True)
            # File fsync does not persist directory entries. Flush descendants
            # before their parents, then the rename's parent before SQL commit.
            for relative in sorted(directories, key=lambda p: (-len(p.parts), str(p))):
                directory = _PinnedDirectory(stage_path.joinpath(*relative.parts))
                try:
                    directory.sync()
                    inventory.append({
                        "path": str(relative), "kind": "directory",
                        "node": _publication_node(directory.path.lstat()),
                    })
                finally:
                    directory.close()
            owned.sync()
            owned.check()
            if journal is not None:
                journal.record("ready", {
                    "stage": stage, "target": root.name,
                    "node": _publication_node(stage_path.lstat()),
                    "members": inventory,
                })
        finally:
            owned.close()
        if journal is not None:
            # Once this fence is durable, no failure or SQL rollback grants GC ownership.
            journal.record("rename_attempted", {"stage": stage, "target": root.name})
        _rename_noreplace(parent, stage, root.name)
        if _node(parent.info(root.name)) != original:
            raise Conflict("published package directory identity changed")
        _artifact_matches(root, files, required=True)
        parent.sync()
        if journal is not None:
            journal.record("published", {
                "stage": stage, "target": root.name,
                "node": _publication_node(parent.info(root.name)),
            })
        return True
    finally:
        # Preserve both canonical and staging bytes on failure/unknown ownership.
        try:
            if journal is not None:
                journal.close()
        finally:
            parent.close()


def _directory_chain(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise Conflict("package store path is not canonical")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if _unlinked(info) or not stat.S_ISDIR(info.st_mode):
            raise Conflict("package store contains an unsafe directory")


def _read_regular(path: Path, expected: bytes) -> None:
    before = path.lstat()
    if (
        _unlinked(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != len(expected)
    ):
        raise Conflict("package artifact contains an unsafe file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            opened = os.fstat(fd)

            # Windows path and descriptor ctime can differ after rename. Compare
            # stable node identity across APIs; metadata within each API below.
            def node(info: os.stat_result) -> tuple[int, ...]:
                return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink

            if node(opened) != node(before):
                raise Conflict("package artifact changed while reading")
            if handle.read(len(expected) + 1) != expected:
                raise Conflict("package artifact differs from verified bytes")
            if _identity(os.fstat(fd)) != _identity(opened):
                raise Conflict("package artifact changed while reading")
    finally:
        os.close(fd)
    if _identity(path.lstat()) != _identity(before):
        raise Conflict("package artifact file was rebound")


def _artifact_matches(root: Path, files: tuple[tuple[str, bytes], ...], *, required: bool) -> None:
    _directory_chain(root)
    try:
        original = root.lstat()
    except FileNotFoundError:
        if required:
            raise Conflict("verified package artifact is absent") from None
        return
    expected = dict(files)
    directories = {
        str(parent)
        for path in expected
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    }
    found: set[str] = set()
    found_directories: set[str] = set()

    def visit(directory: Path) -> None:
        before = directory.lstat()
        if _unlinked(before) or not stat.S_ISDIR(before.st_mode):
            raise Conflict("package artifact directory was rebound")
        with os.scandir(directory) as entries:
            for entry in entries:
                target = directory / entry.name
                name = target.relative_to(root).as_posix()
                info = entry.stat(follow_symlinks=False)
                if _unlinked(info):
                    raise Conflict("package artifact contains a link")
                if stat.S_ISDIR(info.st_mode) and name in directories:
                    found_directories.add(name)
                    visit(target)
                elif stat.S_ISREG(info.st_mode) and name in expected:
                    _read_regular(target, expected[name])
                    found.add(name)
                else:
                    raise Conflict("package artifact has unexpected members")
        if _identity(directory.lstat()) != _identity(before):
            raise Conflict("package artifact directory changed")

    visit(root)
    if (
        found != expected.keys()
        or found_directories != directories
        or _identity(root.lstat()) != _identity(original)
    ):
        raise Conflict("package artifact member set differs")
    _directory_chain(root)


@contextmanager
def _shared_fs_lock(
    request: Request, tenant: UUID, plugin_id: str, *,
    deadline: datetime | None = None, revalidate: Callable[[], bool] | None = None,
) -> Iterator[None]:
    from agent_hub.api.routers.admin import _plugin_package_store_root

    root = _plugin_package_store_root(request)
    store = _request_publication_reservations(request)
    scope = None
    if store is not None:
        if store.store_root != root:
            raise Conflict("publication reservation store differs from request")
        validation_error: BaseException | None = None

        def checked() -> bool:
            nonlocal validation_error
            try:
                return True if revalidate is None else revalidate()
            except BaseException as error:
                validation_error = error
                raise

        try:
            scope = store.prepare_scope_lock(
                tenant=tenant, plugin_id=plugin_id,
                deadline=datetime.now(UTC) + timedelta(seconds=30) if deadline is None else deadline,
                revalidate=checked,
            )
        except (OSError, ValueError, RuntimeError) as error:
            if validation_error is error:
                raise
            raise Conflict("publication scope admission unavailable") from error
    with _shared_fs_lock_at_root(root, tenant, plugin_id, scope=scope):
        yield


@contextmanager
def _shared_fs_lock_at_root(
    root: Path, tenant: UUID, plugin_id: str, *, scope: ScopeLockReservation | None = None,
) -> Iterator[None]:
    """Coordinate package writers under a trusted server-configured store root."""
    lock = root / ".uninstall-locks" / str(tenant) / f"{plugin_id}.lock"
    if scope is not None:
        from agent_hub.plugins.handover_stage_reservations import ScopeLockReservation

        if (
            type(scope) is not ScopeLockReservation or scope.store_root != root
            or scope.tenant != tenant or scope.plugin_id != plugin_id or scope.lock_path != lock
            or len(scope.scope_lock_identity) != 15
        ):
            raise Conflict("publication lock enrollment scope differs")
    try:
        directory = _PinnedDirectory(lock.parent, create=scope is None)
        try:
            descriptor = directory.open(lock.name, os.O_RDWR | (os.O_CREAT if scope is None else 0))
            acquired = False
            try:
                def checked_lock() -> os.stat_result:
                    opened, current = os.fstat(descriptor), directory.info(lock.name)
                    if (
                        _unlinked(opened) or _unlinked(current)
                        or not stat.S_ISREG(opened.st_mode)
                        or not stat.S_ISREG(current.st_mode)
                        or opened.st_nlink != 1 or current.st_nlink != 1
                    ):
                        raise Conflict("package stage lock is unsafe")
                    if _node(current) != _node(opened):
                        raise Conflict("package stage lock was rebound")
                    if scope is not None:
                        facts = (lock.parent.parent.lstat(), lock.parent.lstat(), opened)
                        identities = tuple(
                            value for info in facts
                            for value in (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
                        )
                        if (
                            identities != scope.scope_lock_identity
                            or opened.st_size != 1 or current.st_size != 1
                            or any(_unlinked(info) or not stat.S_ISDIR(info.st_mode) for info in facts[:2])
                        ):
                            raise Conflict("publication lock enrollment identity changed")
                    return opened

                deadline = time.monotonic() + 5
                while True:
                    checked_lock()
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
                            raise Conflict("package stage lock unavailable") from None
                        time.sleep(0.01)
                opened = checked_lock()
                if opened.st_size not in {0, 1}:
                    raise Conflict("package stage lock is unsafe")
                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                yield
                checked_lock()
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
        finally:
            directory.close()
    except OSError:
        raise Conflict("package artifact or stage lock is unavailable") from None


def _pair(receipt: HandoverReceipt, grant: SourceGrant) -> GrantBinding:
    b = grant.binding
    if (receipt.source_grant_id, receipt.source_grant_binding_sha256) != (
        grant.grant_id,
        grant.binding_sha256,
    ) or (
        b.plan_id,
        b.plan_version,
        b.creator_id,
        b.run_id,
        b.expires_at,
        b.source,
        b.source_manifest_sha256,
        b.plugin_id,
        b.plugin_version,
    ) != (
        receipt.handover_id,
        1,
        receipt.creator_id,
        receipt.run_id,
        receipt.expires_at,
        receipt.source,
        receipt.source_manifest_sha256,
        receipt.plugin_id,
        receipt.plugin_version,
    ):
        raise Conflict("persisted source grant differs from locked plan")
    return b


class GeneratedPluginConfirmationService:
    def __init__(
        self,
        *,
        storage: PluginHandoverStorage,
        snapshots: FrozenSourceSnapshotStore,
        grants: DurableSourceGrantService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage, self._snapshots, self._grants = storage, snapshots, grants
        self._clock = clock or (lambda: datetime.now(UTC))

    def _live(self, receipt: HandoverReceipt) -> datetime:
        now = _utc(self._clock())
        if receipt.expires_at <= now:
            raise Conflict("plan is no longer confirmable")
        return now

    async def _plugin(
        self,
        session: AsyncSession,
        tenant: UUID,
        receipt: HandoverReceipt,
    ) -> None:
        row = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == tenant,
                AdminResourceRow.kind == "plugin",
                AdminResourceRow.resource_id == receipt.plugin_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True, autoflush=False)
        )
        if receipt.status != "pending":
            if row is not None:
                raise Conflict("plugin is already registered")
            return
        if row is None:
            raise Conflict("plugin is absent; read the historical handover receipt")
        payload = row.payload
        package = payload.get("package_metadata")
        artifact = package.get("artifact") if isinstance(package, dict) else None
        if (
            payload.get(GENERATED_HANDOVER_MARKER) != receipt.handover_id
            or payload.get("id") != receipt.plugin_id
            or payload.get("version") != receipt.plugin_version
            or payload.get("content_sha256") != receipt.archive_sha256
            or not isinstance(artifact, dict)
            or artifact.get("content_sha256") != receipt.archive_sha256
            or artifact.get("storage_key")
            != f"{tenant}/{receipt.plugin_id}/{receipt.archive_sha256}"
        ):
            raise Conflict("live plugin differs; read the historical handover receipt")

    def _snapshot(self, tenant: UUID, binding: GrantBinding) -> StoredSourceSnapshot:
        stored = self._snapshots.read(
            tenant_id=tenant,
            snapshot_id=binding.snapshot_id,
            blob_sha256=binding.blob_sha256,
            source_manifest_sha256=binding.source_manifest_sha256,
        )
        frozen = stored.snapshot
        if (
            binding.tenant_id != tenant
            or stored.creator_id != binding.creator_id
            or stored.reference.blob_size_bytes != binding.blob_size_bytes
            or frozen.authorized.tenant_id != tenant
            or frozen.authorized.run_id != binding.run_id
            or frozen.authorized.source != binding.source
            or frozen.plugin_id != binding.plugin_id
            or frozen.plugin_version != binding.plugin_version
        ):
            raise PermissionDenied("private snapshot differs from source grant")
        return stored

    async def confirm(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
        confirm: bool,
        idempotency_key: str,
        archive_bytes: bytes,
        key_id: str,
        request: Request,
    ) -> HandoverReceipt:
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(principal.user_id) is not UUID
            or type(principal.tenant_id) is not UUID
            or type(run_id) is not UUID
        ):
            raise PermissionDenied("handover confirmation access denied")
        self._storage._require(principal, "plugin:write", "plugin:read", "run:read")
        _check_confirmation_identity(expected_version, plan_hash)
        _safe_key(idempotency_key)
        if (
            confirm is not True
            or not isinstance(request, Request)
            or type(archive_bytes) is not bytes
            or not archive_bytes
            or len(archive_bytes) > SourceLimits().max_archive_bytes
        ):
            raise ValueError("explicit confirmation and bounded archive bytes required")
        async with self._storage._transaction() as session:
            _, record = await self._storage._load(
                session,
                principal.tenant_id,
                run_id,
                handover_id,
                lock=True,
            )
            locked = _receipt(record)
            if (
                locked.source_grant_id is None
                or locked.plan_hash != plan_hash
                or expected_version != 1
                or locked.status not in {"planned", "awaiting_signature", "pending"}
            ):
                raise Conflict("bound plan is no longer confirmable")
            self._live(locked)
            await acquire_plugin_registration_lock(session, principal.tenant_id, locked.plugin_id)
            await self._plugin(session, principal.tenant_id, locked)
            _, grant = await self._grants._load(
                session, principal.tenant_id, locked.source_grant_id
            )
            binding = _pair(locked, grant)

            async def current(
                receipt: HandoverReceipt,
            ) -> tuple[StoredSourceSnapshot, FrozenSigningKey]:
                _pair(receipt, grant)
                _, live = await self._grants._verify(
                    session,
                    principal,
                    binding,
                    consumed=receipt.status == "pending",
                )
                _pair(receipt, live)
                key = await load_locked_signing_key(
                    session, tenant_id=principal.tenant_id, key_id=key_id
                )
                # Key SHARE can wait after the first grant check. Re-read authority
                # and the key in this BEGIN; no frozen key is a reusable capability.
                _, live = await self._grants._verify(
                    session,
                    principal,
                    binding,
                    consumed=receipt.status == "pending",
                )
                _pair(receipt, live)
                key = await load_locked_signing_key(
                    session, tenant_id=principal.tenant_id, key_id=key_id
                )
                return self._snapshot(principal.tenant_id, binding), key

            stored, key = await current(locked)
            facts = verify_signed_snapshot_package(
                snapshot=stored.snapshot,
                archive_bytes=archive_bytes,
                key=key,
                now=self._live(locked),
            )
            files = _archive_files(archive_bytes, SourceLimits())
            manifest = _contract(
                _manifest(next(f.content for f in files if f.path == "plugin.json")), files
            )
            if (
                tuple(sorted({c.permission_class for c in manifest.capabilities}))
                != locked.permissions
            ):
                raise Conflict("manifest permissions differ from locked plan")

            from agent_hub.api.routers.admin import (
                PluginPackageArtifactMetadata,
                PluginPackageMetadata,
                PluginResourceResponse,
                _plugin_archive_signature_files,
                _plugin_package_store_root,
            )

            artifact_files = _plugin_archive_signature_files(archive_bytes)
            root = (
                _plugin_package_store_root(request)
                / str(principal.tenant_id)
                / facts.plugin_id
                / facts.archive_sha256
            )
            with _shared_fs_lock(
                request, principal.tenant_id, facts.plugin_id,
                deadline=min(locked.expires_at, key.not_after or locked.expires_at),
                revalidate=lambda: bool(verify_signed_snapshot_package(
                    snapshot=stored.snapshot, archive_bytes=archive_bytes,
                    key=key, now=self._live(locked),
                )),
            ):
                _artifact_matches(root, artifact_files, required=locked.status == "pending")
                # The filesystem lock may also wait; sample liveness only after it.
                verify_signed_snapshot_package(
                    snapshot=stored.snapshot,
                    archive_bytes=archive_bytes,
                    key=key,
                    now=self._live(locked),
                )
                if manifest.package is None:
                    raise Conflict("verified adapter metadata absent")
                package = PluginPackageMetadata.model_validate(
                    {
                        **manifest.package.model_dump(mode="python"),
                        "signature_verification": "verified",
                        "verified_public_key_sha256": facts.public_key_sha256,
                        "signature_trust_expires_at": key.not_after,
                        "approval_state": "pending",
                        "approval_reason": "",
                        "approved_by": None,
                        "approved_at": None,
                        "activation_state": "blocked_pending_approval",
                        "activation_reason": "",
                        "artifact": None,
                    }
                )
                # Both publishers use pinned directories, durable intent and
                # no-replace rename; SQL uncertainty never grants cleanup authority.
                _publish_artifact(
                    root, artifact_files,
                    store_root=_plugin_package_store_root(request),
                    tenant=principal.tenant_id, plugin_id=facts.plugin_id,
                    reservation_store=_request_publication_reservations(request),
                    deadline=min(locked.expires_at, key.not_after or locked.expires_at),
                    revalidate=lambda: bool(verify_signed_snapshot_package(
                        snapshot=stored.snapshot, archive_bytes=archive_bytes,
                        key=key, now=self._live(locked),
                    )),
                )
                package = package.model_copy(
                    update={
                        "artifact": PluginPackageArtifactMetadata(
                            storage_key=f"{principal.tenant_id}/{facts.plugin_id}/{facts.archive_sha256}",
                            content_sha256=facts.archive_sha256,
                            file_count=len(artifact_files),
                            total_size_bytes=sum(len(data) for _, data in artifact_files),
                            stored_at=self._live(locked),
                            quarantine_state="stored",
                        )
                    }
                )
                _artifact_matches(root, artifact_files, required=True)
            payload = PluginResourceResponse.model_validate(
                {
                    **manifest.model_dump(exclude={"package"}),
                    "enabled": False,
                    "status": "disabled",
                    "health": "disabled",
                    "content_sha256": facts.archive_sha256,
                    "package_metadata": package,
                }
            ).model_dump(mode="json")
            verified = VerifiedPendingPlugin(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                handover_id=locked.handover_id,
                plan_hash=locked.plan_hash,
                source_manifest_sha256=facts.source_manifest_sha256,
                signature_payload_sha256=facts.signature_payload_sha256,
                archive_sha256=facts.archive_sha256,
                key_id=facts.key_id,
                verified_public_key_sha256=facts.public_key_sha256,
                artifact_ref="hp-" + facts.archive_sha256,
                plugin_payload=payload,
            )
            final_key = key

            async def revalidate(receipt: HandoverReceipt) -> None:
                nonlocal final_key
                snapshot, current_key = await current(receipt)
                final_key = current_key
                with _shared_fs_lock(
                    request, principal.tenant_id, facts.plugin_id,
                    deadline=min(receipt.expires_at, current_key.not_after or receipt.expires_at),
                    revalidate=lambda: bool(verify_signed_snapshot_package(
                        snapshot=snapshot.snapshot, archive_bytes=archive_bytes,
                        key=current_key, now=self._live(receipt),
                    )),
                ):
                    _artifact_matches(root, artifact_files, required=True)
                    fresh = verify_signed_snapshot_package(
                        snapshot=snapshot.snapshot,
                        archive_bytes=archive_bytes,
                        key=current_key,
                        now=self._live(receipt),
                    )
                    if (
                        fresh.archive_sha256,
                        fresh.signature_payload_sha256,
                        fresh.public_key_sha256,
                        fresh.members,
                    ) != (
                        facts.archive_sha256,
                        facts.signature_payload_sha256,
                        facts.public_key_sha256,
                        facts.members,
                    ):
                        raise Conflict("verified package facts changed")

            async def validate_bridge(
                same: AsyncSession,
                receipt: HandoverReceipt,
                package_dto: VerifiedPendingPlugin,
            ) -> None:
                if same is not session or receipt != locked or package_dto != verified:
                    raise Conflict("locked confirmation facts changed")
                await self._plugin(same, principal.tenant_id, receipt)
                await revalidate(receipt)

            async def consume_bridge(same: AsyncSession, pending: HandoverReceipt) -> None:
                if same is not session:
                    raise Conflict("confirmation transaction changed")
                _, actual = await self._storage._load(
                    same,
                    principal.tenant_id,
                    run_id,
                    handover_id,
                    lock=True,
                )
                actual_receipt = _receipt(actual)
                _pair(actual_receipt, grant)
                if (
                    actual_receipt != pending
                    or actual.status != "pending"
                    or actual.installer_id != principal.user_id
                    or actual.receipt_id is None
                    or actual.archive_sha256 != facts.archive_sha256
                    or actual.signature_payload_sha256 != facts.signature_payload_sha256
                    or actual.verified_public_key_sha256 != facts.public_key_sha256
                ):
                    raise Conflict("actual pending receipt differs from confirmation")
                await self._plugin(same, principal.tenant_id, actual_receipt)
                await self._grants.consume(
                    same,
                    principal,
                    expected=binding,
                    receipt_id=actual.receipt_id,
                )
                await revalidate(actual_receipt)

            receipt = await self._storage._commit_pending_in_session(
                session,
                principal,
                run_id,
                handover_id,
                expected_version,
                plan_hash,
                confirm,
                idempotency_key,
                verified,
                validate_bridge=validate_bridge,
                consume_bridge=consume_bridge,
            )
            verify_signed_snapshot_package(
                snapshot=stored.snapshot,
                archive_bytes=archive_bytes,
                key=final_key,
                now=self._live(receipt),
            )
            return receipt
