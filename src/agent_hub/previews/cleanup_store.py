"""Bounded private owner receipts, for one owning PreviewManager process."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from agent_hub.previews.cleanup import CleanupReceiptV1, PreviewCleanupRecord, strict_json

MAX_RECORD_BYTES = 64 * 1024
MAX_TERMINAL = 256
TERMINAL_TTL = timedelta(hours=24)


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _private(info: os.stat_result, *, directory: bool) -> None:
    if (getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ValueError("unsafe receipt path identity")
    if sys.platform == "linux" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("receipt storage is not service private")


class PreviewReceiptStore:
    def __init__(self, root: Path, *, max_active: int, clock: Callable[[], datetime]) -> None:
        if max_active < 1:
            raise ValueError("invalid receipt capacity")
        self._workspace = root.absolute()
        self._root = self._workspace / ".preview-receipts"
        self._clock = clock
        self._max_active = max_active
        self._records: dict[str, PreviewCleanupRecord] = {}
        self._guard_parents()
        self._root.mkdir(mode=0o700, exist_ok=True)
        self._root_identity = self._root.lstat()
        _private(self._root_identity, directory=True)
        self._reload()

    def _guard_parents(self) -> None:
        for path in (*reversed(self._workspace.parents), self._workspace):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("receipt parent alias")
            if sys.platform == "linux":
                if info.st_uid not in {0, os.getuid()}:
                    raise ValueError("unowned receipt parent")
                if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                    raise ValueError("writable receipt parent")

    def _guard(self) -> None:
        self._guard_parents()
        current = self._root.lstat()
        _private(current, directory=True)
        if not _same(current, self._root_identity):
            raise ValueError("receipt directory replaced")

    def _read(self, path: Path) -> PreviewCleanupRecord:
        self._guard()
        return self._read_file(path)

    def _read_file(self, path: Path) -> PreviewCleanupRecord:
        before = path.lstat()
        _private(before, directory=False)
        if before.st_size > MAX_RECORD_BYTES:
            raise ValueError("oversized receipt")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            _private(opened, directory=False)
            if not _same(before, opened):
                raise ValueError("receipt replaced during open")
            raw = stream.read(MAX_RECORD_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > MAX_RECORD_BYTES or not _same(after, path.lstat())
                or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise ValueError("receipt changed during read")
        result = PreviewCleanupRecord.from_wire(strict_json(raw))
        if path.name != result.identity.preview_id + ".json":
            raise ValueError("receipt filename identity mismatch")
        return result

    def _bounded_paths(self) -> list[Path]:
        self._guard()
        paths: list[Path] = []
        # Enumeration and bytes are bounded before sorting/parsing any entry.
        with os.scandir(self._root) as entries:
            for entry in entries:
                if len(paths) >= 2 * (MAX_TERMINAL + self._max_active):
                    raise ValueError("receipt directory entry budget exceeded")
                paths.append(Path(entry.path))
        self._guard()
        return paths

    def _reload(self) -> None:
        abandoned: list[PreviewCleanupRecord] = []
        paths = sorted(self._bounded_paths())
        isolated: set[str] = set()
        for path in paths:
            if path.suffix == ".isolated":
                try:
                    canonical = str(UUID(path.stem)) == path.stem
                except ValueError:
                    canonical = False
                if canonical:
                    self._isolate(path.stem)
                    isolated.add(path.stem)
        for path in paths:
            self._guard()
            try:
                if path.suffix != ".json" or str(UUID(path.stem)) != path.stem:
                    continue
                if path.stem in isolated:
                    continue
                record = self._read_file(path)
            except (OSError, ValueError, TypeError, UnicodeError):
                self._guard()
                continue
            self._guard()
            if record.retention_expires_at is None:
                now = self._clock()
                record = PreviewCleanupRecord(record.identity, CleanupReceiptV1.create(
                    record.identity, (), requested_at=now, reason="recovery", reason_code="interrupted",
                ), now + TERMINAL_TTL)
                abandoned.append(record)
            self._records[record.identity.preview_id] = record
        self._prune()
        for record in abandoned:
            if record.identity.preview_id in self._records:
                self.put(record)

    def _isolation_exists(self, key: str) -> bool:
        self._guard()
        try:
            (self._root / (key + ".isolated")).lstat()
        except FileNotFoundError:
            return False
        return True

    def _isolate(self, key: str) -> None:
        # An empty private marker reserves the identity without touching evidence.
        path = self._root / (key + ".isolated")
        paths = self._bounded_paths()
        if path not in paths:
            if len(paths) >= 2 * (MAX_TERMINAL + self._max_active):
                raise ValueError("receipt directory entry budget exhausted")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        else:
            before = path.lstat()
            _private(before, directory=False)
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            _private(opened, directory=False)
            if (opened.st_size != 0 or not _same(opened, path.lstat())
                    or (path in paths and not _same(before, opened))):
                raise ValueError("unsafe receipt isolation marker")
            os.fsync(fd)
            self._guard()
            if not _same(opened, path.lstat()) or os.fstat(fd).st_size != 0:
                raise ValueError("receipt isolation marker changed")
        finally:
            os.close(fd)
        self._sync_directory()

    def _prune(self) -> None:
        now = self._clock()
        terminal = sorted((r for r in self._records.values() if r.retention_expires_at is not None),
                          key=lambda r: (r.retention_expires_at, r.identity.preview_id))
        excess = max(0, len(terminal) - MAX_TERMINAL)
        for index, record in enumerate(terminal):
            assert record.retention_expires_at is not None
            if index >= excess and record.retention_expires_at > now:
                continue
            path = self._root / (record.identity.preview_id + ".json")
            self._guard()
            if self._isolation_exists(record.identity.preview_id):
                self._isolate(record.identity.preview_id)
                self._records.pop(record.identity.preview_id, None)
                continue
            valid = False
            try:
                valid = self._read_file(path) == record
            except (OSError, ValueError, TypeError, UnicodeError):
                pass
            # Only leaf failures are isolated; global guards stay outside the catch.
            self._guard()
            if valid:
                path.unlink()
                self._sync_directory()
            else:
                self._isolate(record.identity.preview_id)
            # Release only after deletion or isolation is durably synchronized.
            self._records.pop(record.identity.preview_id, None)
        if terminal:
            self._sync_directory()

    def _sync_directory(self) -> None:
        self._guard()
        if sys.platform == "linux":
            fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if not _same(os.fstat(fd), self._root_identity):
                    raise ValueError("receipt directory replaced")
                os.fsync(fd)
            finally:
                os.close(fd)

    def put(self, record: PreviewCleanupRecord) -> None:
        # Roundtrip validates even callers that bypassed dataclass construction.
        record = PreviewCleanupRecord.from_wire(record.to_wire())
        self._guard()
        key = record.identity.preview_id
        if self._isolation_exists(key):
            raise ValueError("isolated receipt evidence cannot be overwritten")
        prior = self._records.get(key)
        if prior is not None and prior.identity != record.identity:
            raise ValueError("owner identity is immutable")
        self._prune()
        if self._isolation_exists(key):
            raise ValueError("isolated receipt evidence cannot be overwritten")
        active = sum(r.retention_expires_at is None for k, r in self._records.items() if k != key)
        if record.retention_expires_at is None and active >= self._max_active:
            raise ValueError("active receipt capacity exceeded")
        raw = json.dumps(record.to_wire(), separators=(",", ":"), sort_keys=True).encode()
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("oversized receipt")
        destination = self._root / (key + ".json")
        paths = self._bounded_paths()
        if destination not in paths and len(paths) >= 2 * (MAX_TERMINAL + self._max_active):
            raise ValueError("receipt directory entry budget exhausted")
        try:
            info = destination.lstat()
        except FileNotFoundError:
            pass
        else:
            _private(info, directory=False)
            if key not in self._records:
                raise ValueError("isolated receipt evidence cannot be overwritten")
            if self._read(destination).identity != record.identity:
                raise ValueError("stored owner identity changed")
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=self._root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._guard()
            os.replace(temporary, destination)
            self._sync_directory()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self._records[key] = record
        self._prune()

    def get(self, preview_id: str) -> PreviewCleanupRecord | None:
        # Retrieval never prunes, rewrites, or drives cleanup.
        record = self._records.get(preview_id)
        if record is None or (record.retention_expires_at is not None
                              and record.retention_expires_at <= self._clock()):
            return None
        self._guard()
        if self._isolation_exists(preview_id):
            return None
        current = self._read(self._root / (preview_id + ".json"))
        if current != record:
            raise ValueError("receipt changed outside owning manager")
        return record
