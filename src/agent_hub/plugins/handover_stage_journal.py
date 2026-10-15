"""Bounded publication metadata, without recovery, deletion or aborted authority.

The trusted publisher supplies metadata only. Intent precedes stage creation;
an uncertain write poisons this handle and leaves every object in place. Windows
private ACLs protect records but provide no directory durability or GC credit.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self
from uuid import UUID, uuid4

from agent_hub.plugins.handover_snapshots import (
    _Directory,
    _native_path,
    _ownership,
    _windows_security,
)

if TYPE_CHECKING:
    from agent_hub.plugins.handover_stage_reservations import (
        ReservationToken,
        StagePublicationContext,
    )

_PLUGIN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_STAGE = re.compile(r"tmp-gh-[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_MAX_BYTES = 1_048_576
_MAX_EVENTS = 4096
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MIN_FREE_BYTES = 128 * 1024 * 1024


def _available_bytes(directory: _Directory) -> int:
    if directory.fd is not None:
        capacity = os.fstatvfs(directory.fd)
        return capacity.f_bavail * capacity.f_frsize
    if sys.platform != "win32":
        raise RuntimeError("publication journal capacity descriptor is unavailable")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDiskFreeSpaceExW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    )
    kernel.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    available = ctypes.c_ulonglong()
    if not kernel.GetDiskFreeSpaceExW(
        str(_native_path(directory.path)),
        ctypes.byref(available),
        None,
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return available.value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clock_time() -> datetime:
    now = _utc_now()
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise RuntimeError("publication journal clock is not an aware UTC time")
    return now


def _timestamp(now: datetime) -> str:
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink)


def _facts(info: os.stat_result) -> dict[str, int]:
    return dict(zip(("dev", "ino", "uid", "gid", "mode", "nlink"), _identity(info), strict=True))


def _exclusive_windows_directory(path: Path, parent: Path) -> None:
    """Use the validated parent's owner and the snapshot private ACL policy at creation."""
    if sys.platform != "win32":
        raise RuntimeError("Windows private directory creation is unavailable")
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = (wintypes.LPVOID,)
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
    )

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    kernel.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    owner, inherited = wintypes.LPVOID(), wintypes.LPVOID()
    result = advapi.GetNamedSecurityInfoW(
        str(_native_path(parent)),
        1,
        1,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(inherited),
    )
    if result:
        raise OSError(result, "cannot read trusted journal owner")
    try:
        text_sid = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(owner, ctypes.byref(text_sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = wintypes.LPVOID()
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"O:{text_sid.value}D:P(A;OICI;FA;;;{text_sid.value})",
                1,
                ctypes.byref(descriptor),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                attributes = SecurityAttributes(
                    ctypes.sizeof(SecurityAttributes), descriptor, False
                )
                if not kernel.CreateDirectoryW(str(_native_path(path)), ctypes.byref(attributes)):
                    error = ctypes.get_last_error()
                    if error in (80, 183):
                        raise FileExistsError(error, "journal directory already exists", str(path))
                    raise ctypes.WinError(error)
            finally:
                kernel.LocalFree(descriptor)
        finally:
            kernel.LocalFree(ctypes.cast(text_sid, wintypes.LPVOID))
    finally:
        kernel.LocalFree(inherited)
    _windows_security(path, private=True)


def _metadata(value: object, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [65_536]
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise ValueError("journal metadata exceeds structural limit")
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("journal metadata requires string keys")
            _metadata(child, depth + 1, budget)
    elif type(value) is list:
        for child in value:
            _metadata(child, depth + 1, budget)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError("journal details must contain JSON metadata only")


class StagePublicationJournal:
    def __init__(
        self,
        store_root: Path,
        tenant: UUID,
        plugin_id: str,
        stage_name: str,
        target_digest: str,
        reserved_bytes: int,
        reserved_members: int,
        member_budget: int = 2048,
        reservation: ReservationToken | None = None,
        publication: StagePublicationContext | None = None,
    ) -> None:
        self._publication_stack = contextlib.ExitStack()
        self._publication: StagePublicationContext | None = None
        self._directories: dict[str, _Directory] = {}
        self._closed, self._failed = False, False
        self._watermark = _MIN_FREE_BYTES
        try:
            self._initialize(
                store_root, tenant, plugin_id, stage_name, target_digest,
                reserved_bytes, reserved_members, member_budget, reservation, publication,
            )
        except BaseException:
            with contextlib.suppress(BaseException):
                self.close()
            raise

    def _initialize(
        self,
        store_root: Path,
        tenant: UUID,
        plugin_id: str,
        stage_name: str,
        target_digest: str,
        reserved_bytes: int,
        reserved_members: int,
        member_budget: int = 2048,
        reservation: ReservationToken | None = None,
        publication: StagePublicationContext | None = None,
    ) -> None:
        # The trusted publisher derives this count from verified files/directories;
        # bounded log bytes/events still apply, and this is not a capacity lease.
        if (
            not isinstance(store_root, Path)
            or not store_root.is_absolute()
            or ".." in store_root.parts
            or type(tenant) is not UUID
            or type(plugin_id) is not str
            or not _PLUGIN.fullmatch(plugin_id)
            or plugin_id.endswith(".")
            or plugin_id.split(".")[0]
            in {
                "con",
                "prn",
                "aux",
                "nul",
                *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10)),
            }
            or type(stage_name) is not str
            or not _STAGE.fullmatch(stage_name)
            or type(target_digest) is not str
            or not _DIGEST.fullmatch(target_digest)
            or type(reserved_bytes) is not int
            or not 0 <= reserved_bytes <= 2_000_000
            or type(reserved_members) is not int
            or type(member_budget) is not int
            or not 0 < reserved_members <= member_budget
        ):
            raise ValueError("invalid publication journal binding")
        reference: dict[str, object] | None = None
        if publication is not None and reservation is None:
            raise ValueError("publication context requires a reservation")
        if reservation is not None:
            from agent_hub.plugins.handover_stage_reservations import (
                ReservationToken,
                StagePublicationContext,
                StageReservationStore,
                reservation_reference,
            )

            if (
                type(reservation) is not ReservationToken
                or reservation.store_root != store_root
                or reservation.tenant != tenant
                or reservation.plugin_id != plugin_id
                or reservation.stage_name != stage_name
                or reservation.target_digest != target_digest
                or reservation.source_bytes != reserved_bytes
                or reservation.source_members != reserved_members
            ):
                raise ValueError("publication reservation scope differs")
            if publication is None:
                publication = self._publication_stack.enter_context(
                    StageReservationStore(store_root, reservation.policy).publication(
                        tenant=tenant, plugin_id=plugin_id, initialize=False,
                    )
                )
            elif type(publication) is not StagePublicationContext:
                raise ValueError("invalid publication context")
            publication.validate_token(reservation)
            self._publication = publication
            self._watermark = reservation.policy.min_available_bytes
            reference = reservation_reference(reservation)
        self._created_at = _clock_time()
        if reservation is not None and self._created_at < reservation.created_at:
            raise RuntimeError("publication clock precedes reservation")
        self._last_recorded_at = self._created_at
        self._expected: dict[str, tuple[int, ...]] = {}
        self._closed, self._failed = False, False
        self._state, self._ordinal = "", 0
        self._record_bytes = 0
        self._reserved_bytes = reserved_bytes
        self._stage_name, self._target_digest = stage_name, target_digest
        self._operation_id = uuid4() if reservation is None else reservation.operation_id
        self._directory_durability = sys.platform != "win32"
        self._root = (
            store_root / ".stage-ownership" / str(tenant) / plugin_id / str(self._operation_id)
        )
        try:
            # Validate all existing publication parents before creating any ledger object.
            self._bind("store", store_root, private=False)
            self._bind("tenant_parent", store_root / str(tenant), private=False)
            self._bind("plugin_parent", store_root / str(tenant) / plugin_id, private=False)
            self._capacity()
            parent = "store"
            for label, name in (
                ("namespace", ".stage-ownership"),
                ("ledger_tenant", str(tenant)),
                ("ledger_plugin", plugin_id),
                ("operation", str(self._operation_id)),
            ):
                self._child(parent, label, name, exclusive=label == "operation")
                parent = label
            self._manifest: dict[str, object] = {
                "schema_version": 1,
                "created_at": _timestamp(self._created_at),
                "operation_id": str(self._operation_id),
                "tenant": str(tenant),
                "plugin_id": plugin_id,
                "stage_name": stage_name,
                "target_digest": target_digest,
                "reserved_bytes": reserved_bytes,
                "reserved_members": reserved_members,
                "member_budget": member_budget,
                "directory_durability": self._directory_durability,
                "bindings": {
                    name: {"path": str(directory.path), **_facts(directory.path.lstat())}
                    for name, directory in self._directories.items()
                },
            }
            if reference is not None:
                self._manifest["reservation"] = reference
            self._append("creating", {})
        except BaseException:
            with contextlib.suppress(BaseException):
                self.close()
            raise

    @property
    def root(self) -> Path:
        return self._root

    @property
    def operation_id(self) -> UUID:
        return self._operation_id

    @property
    def directory_durability(self) -> bool:
        return self._directory_durability

    def _bind(self, label: str, path: Path, *, private: bool) -> None:
        directory = _Directory(path, private=private)
        self._directories[label] = directory
        info = _native_path(path).lstat()
        if info.st_nlink < 1:
            raise RuntimeError("unlinked journal directory")
        if directory.fd is not None:
            opened, current = _identity(os.fstat(directory.fd)), _identity(info)
            if (
                opened[-1] < 1
                or opened[:-1] != current[:-1]
                or (label == "operation" and opened != current)
            ):
                raise RuntimeError("journal directory descriptor differs from entry")
        identity = _identity(info)
        if label != "store" and identity[0] != self._expected["store"][0]:
            raise RuntimeError("publication journal directories must share the store volume")
        self._expected[label] = identity

    def _child(self, parent_label: str, label: str, name: str, *, exclusive: bool) -> None:
        parent = self._directories[parent_label]
        self._check()
        expected = _identity(_native_path(parent.path).lstat())
        try:
            if sys.platform == "win32":
                _exclusive_windows_directory(parent.path / name, parent.path)
            else:
                os.mkdir(name, mode=0o700, dir_fd=parent.fd)
        except FileExistsError:
            if exclusive:
                raise
        parent.check()
        current = _identity(_native_path(parent.path).lstat())
        # Other trusted publishers can create siblings under shared ledger parents.
        if current[:-1] != expected[:-1] or current[-1] < 1:
            raise RuntimeError("journal parent changed during directory creation")
        self._expected[parent_label] = current
        self._bind(label, parent.path / name, private=True)
        # A reused entry may have been created by a publisher not yet at its own fsync.
        parent.sync()
        self._check()

    def _check(self) -> None:
        for label, directory in self._directories.items():
            directory.check()
            info = _native_path(directory.path).lstat()
            identity = _identity(info)
            expected = self._expected[label]
            shared = label != "operation"
            # Shared parents gain subdirectory links through legitimate publishers.
            # Preserve the captured nlink as evidence, not as a stable inode identity.
            if info.st_nlink < 1 or (
                identity[:-1] != expected[:-1] if shared else identity != expected
            ):
                raise RuntimeError("publication journal parent identity changed")
            if directory.fd is not None:
                opened = _identity(os.fstat(directory.fd))
                if (
                    opened[-1] < 1
                    or opened[:-1] != identity[:-1]
                    or (not shared and opened != identity)
                ):
                    raise RuntimeError("publication journal directory descriptor changed")

    def _capacity(self) -> None:
        # A watermark is not an exclusive reservation or permission to reclaim objects.
        self._check()
        available = _available_bytes(self._directories["store"])
        self._check()
        required = self._watermark + _MAX_TOTAL_BYTES - self._record_bytes + self._reserved_bytes
        if type(available) is not int or available < required:
            raise RuntimeError("publication journal disk capacity is insufficient or unknown")

    def record(self, state: str, details: dict[str, object]) -> None:
        if self._closed or self._failed:
            raise RuntimeError("publication journal is closed or uncertain")
        allowed = {
            "creating": {"recording", "ready"},
            "ready": {"rename_attempted"},
            "rename_attempted": {"published"},
            "published": set(),
        }
        if type(state) is not str or state not in allowed[self._state]:
            raise ValueError("publication journal transition is not permitted")
        self._append(state, details)

    def _file_check(self, name: str, fd: int, expected: tuple[int, ...], size: int) -> None:
        directory = self._directories["operation"]
        self._check()
        opened, entry = os.fstat(fd), directory.info(name)
        for info in (opened, entry):
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
                or _identity(info) != expected
                or info.st_size != size
            ):
                raise RuntimeError("publication journal event identity changed")
        _ownership(directory.path / name, opened, private=True, directory=False)

    def _append(self, state: str, details: dict[str, object]) -> None:
        if self._publication is not None:
            self._publication._check()
        if self._ordinal >= _MAX_EVENTS:
            raise ValueError("publication journal event limit reached")
        if type(details) is not dict:
            raise ValueError("publication journal details must be metadata")
        _metadata(details)
        try:
            recorded_at = _clock_time()
            if recorded_at < self._last_recorded_at:
                raise RuntimeError("publication journal clock moved backwards")
        except BaseException:
            self._failed = True
            raise
        payload = {
            **self._manifest,
            "recorded_at": _timestamp(recorded_at),
            "ordinal": self._ordinal + 1,
            "state": state,
            "details": details,
        }
        data = bytearray()
        for chunk in json.JSONEncoder(
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).iterencode(payload):
            encoded = chunk.encode("ascii")
            if len(data) + len(encoded) > _MAX_BYTES:
                raise ValueError("publication journal record exceeds byte limit")
            data.extend(encoded)
        if self._record_bytes + len(data) > _MAX_TOTAL_BYTES:
            self._failed = True
            raise ValueError("publication journal aggregate metadata limit reached")
        directory = self._directories["operation"]
        name = f"{self._ordinal + 1:06d}.json"
        try:
            self._capacity()
            fd = directory.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                identity = _identity(os.fstat(fd))
                self._file_check(name, fd, identity, 0)
                view = memoryview(data)
                while view:
                    try:
                        count = os.write(fd, view)
                    except InterruptedError:
                        continue
                    if not 0 < count <= len(view):
                        raise RuntimeError("publication journal write made no progress")
                    view = view[count:]
                self._file_check(name, fd, identity, len(data))
                before = os.fstat(fd)
                os.fsync(fd)
                self._file_check(name, fd, identity, len(data))
                after = os.fstat(fd)
                if (before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise RuntimeError("publication journal event changed during fsync")
                directory.sync()
                self._file_check(name, fd, identity, len(data))
                after_directory = os.fstat(fd)
                if (before.st_mtime_ns, before.st_ctime_ns) != (
                    after_directory.st_mtime_ns,
                    after_directory.st_ctime_ns,
                ):
                    raise RuntimeError("publication journal event changed during directory sync")
            finally:
                if sys.exc_info()[0] is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                else:
                    os.close(fd)
            self._check()
        except BaseException:
            self._failed = True
            raise
        self._ordinal += 1
        self._record_bytes += len(data)
        self._last_recorded_at = recorded_at
        if state != "recording":
            self._state = state

    def close(self) -> None:
        if not self._closed and self._publication is not None:
            self._publication._check_owner()
        self._closed = True
        failure: OSError | None = None
        try:
            while self._directories:
                _, directory = self._directories.popitem()
                try:
                    directory.close()
                except OSError as error:
                    if failure is None:
                        failure = error
        finally:
            self._publication_stack.close()
        if failure is not None and sys.exc_info()[0] is None:
            raise failure

    def __enter__(self) -> Self:
        if self._closed or self._failed:
            raise RuntimeError("publication journal is closed or uncertain")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            with contextlib.suppress(BaseException):
                self.close()
        else:
            self.close()
