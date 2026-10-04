"""Fail-closed, stdlib-only tree copies for same-version validation relocation.

The caller owns the target's existing parent and must keep it private throughout
the operation. Writers must be stopped: stat/content checks detect observed
changes, but cannot turn a mutable filesystem into an atomic snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

_CHUNK = 1024 * 1024
_REPARSE = 0x400
_SYMLINK_TAG = 0xA000000C
_Parts = tuple[str, ...]


class _Entry(NamedTuple):
    info: os.stat_result
    kind: str
    value: str = ""


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("validation tree copy deadline exceeded")


def _kind(info: os.stat_result) -> str:
    reparse = int(getattr(info, "st_file_attributes", 0)) & _REPARSE
    if reparse and not (
        stat.S_ISLNK(info.st_mode) and int(getattr(info, "st_reparse_tag", 0)) == _SYMLINK_TAG
    ):
        raise ValueError("junction/reparse point in validation tree")
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    raise ValueError("special file in validation tree")


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
        int(getattr(info, "st_reparse_tag", 0)),
    )


def _stat_key(info: os.stat_result, *, descriptor: bool = False) -> tuple[int, ...]:
    # Python 3.12 Windows lstat uses birth time for ctime, whereas fstat may use
    # change time. Compare ctime between path stats, not across those two APIs.
    return (
        *_identity(info), info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
        info.st_size, info.st_mtime_ns,
        0 if descriptor and sys.platform == "win32" else info.st_ctime_ns,
        int(getattr(info, "st_file_attributes", 0)),
    )


def _unchanged(actual: os.stat_result, expected: os.stat_result, *, fd: bool = False) -> None:
    if _stat_key(actual, descriptor=fd) != _stat_key(expected, descriptor=fd):
        raise RuntimeError("validation tree source identity/stat changed")


class _Tree:
    def __init__(self, root: Path, info: os.stat_result, deadline: float) -> None:
        if _kind(info) != "directory":
            raise ValueError("validation tree root must be a real directory")
        self.root = root
        self.deadline = deadline
        self.directories: dict[_Parts, os.stat_result] = {(): info}

    @contextmanager
    def directory(self, parts: _Parts) -> Iterator[int | None]:
        # Linux opens every component relative to a checked directory descriptor;
        # no source/cleanup lookup can follow a concurrently substituted symlink.
        # Windows lacks dir_fd: check the entire directory chain before and after.
        fd: int | None = None
        checks: list[tuple[Path, os.stat_result]] = []
        with ExitStack() as stack:
            for length in range(len(parts) + 1):
                _check_deadline(self.deadline)
                prefix = parts[:length]
                path = self.root.joinpath(*prefix)
                expected = self.directories[prefix]
                name = str(self.root) if length == 0 else parts[length - 1]
                actual = os.lstat(path if fd is None else name, dir_fd=fd)
                if _kind(actual) != "directory" or _identity(actual) != _identity(expected):
                    raise RuntimeError("validation tree directory identity changed")
                checks.append((path, expected))
                if sys.platform != "win32":
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    fd = os.open(name, flags, dir_fd=fd)
                    stack.callback(os.close, fd)
                    if _identity(os.fstat(fd)) != _identity(expected):
                        raise RuntimeError("validation tree directory replaced while opening")
            try:
                yield fd
            finally:
                for path, expected in checks:
                    actual = path.lstat()
                    if _kind(actual) != "directory" or _identity(actual) != _identity(expected):
                        raise RuntimeError("validation tree directory identity changed")

    def lstat(self, parts: _Parts) -> os.stat_result:
        if not parts:
            with self.directory(()):
                return self.root.lstat()
        with self.directory(parts[:-1]) as fd:
            return os.lstat(self.root.joinpath(*parts) if fd is None else parts[-1], dir_fd=fd)

    def readlink(self, parts: _Parts) -> str:
        with self.directory(parts[:-1]) as fd:
            return os.readlink(self.root.joinpath(*parts) if fd is None else parts[-1], dir_fd=fd)

    def names(self, parts: _Parts) -> list[str]:
        with self.directory(parts) as fd:
            names: list[str] = []
            with os.scandir(self.root.joinpath(*parts) if fd is None else fd) as entries:
                for entry in entries:
                    _check_deadline(self.deadline)
                    names.append(entry.name)
            names.sort()
            _check_deadline(self.deadline)
            return names

    @contextmanager
    def open_file(self, parts: _Parts, flags: int, mode: int = 0o600) -> Iterator[int]:
        with self.directory(parts[:-1]) as parent_fd:
            flags |= int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NONBLOCK", 0))
            fd = os.open(
                self.root.joinpath(*parts) if parent_fd is None else parts[-1],
                flags, mode, dir_fd=parent_fd,
            )
            try:
                yield fd
            finally:
                os.close(fd)


def _read_file(
    tree: _Tree, parts: _Parts, info: os.stat_result, output: int | None = None,
) -> str:
    _unchanged(tree.lstat(parts), info)
    digest = hashlib.sha256()
    total = 0
    with tree.open_file(parts, os.O_RDONLY) as fd:
        _unchanged(os.fstat(fd), info, fd=True)
        while True:
            _check_deadline(tree.deadline)
            chunk = os.read(fd, _CHUNK)
            _check_deadline(tree.deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > info.st_size:
                raise RuntimeError("validation tree file grew while reading")
            digest.update(chunk)
            if output is not None:
                remaining = memoryview(chunk)
                while remaining:
                    _check_deadline(tree.deadline)
                    written = os.write(output, remaining)
                    if written <= 0:
                        raise OSError("validation tree copy made no write progress")
                    remaining = remaining[written:]
        _unchanged(os.fstat(fd), info, fd=True)
    _unchanged(tree.lstat(parts), info)
    if total != info.st_size:
        raise RuntimeError("validation tree file size changed while reading")
    return digest.hexdigest()


def _link_parts(text: str) -> tuple[str, ...]:
    windows = PureWindowsPath(text)
    if not text or text.startswith("/") or windows.drive or windows.root:
        raise ValueError("absolute or drive-relative validation tree symlink")
    # Keep trailing separators and dot components: file/ and file/. must fail
    # instead of being normalized into an apparently valid file target.
    return tuple((text.replace("\\", "/") if sys.platform == "win32" else text).split("/"))


def _resolve_link(parts: _Parts, entries: dict[_Parts, _Entry], deadline: float) -> _Parts:
    current = parts[:-1]
    active: set[_Parts] = {parts}
    pending: list[tuple[str, _Parts | None]] = [
        (piece, None) for piece in reversed(_link_parts(entries[parts].value))
    ]
    while pending:
        _check_deadline(deadline)
        piece, finished = pending.pop()
        if finished is not None:
            active.remove(finished)
            continue
        if entries[current].kind != "directory":
            raise ValueError("validation tree symlink traverses a non-directory")
        if piece in {"", "."}:
            continue
        if piece == "..":
            if not current:
                raise ValueError("escaping validation tree symlink")
            current = current[:-1]
            continue
        candidate = (*current, piece)
        entry = entries.get(candidate)
        if entry is None:
            raise ValueError("dangling validation tree symlink")
        if entry.kind == "link":
            if candidate in active:
                raise ValueError("cyclic validation tree symlink")
            active.add(candidate)
            pending.append(("", candidate))
            pending.extend((part, None) for part in reversed(_link_parts(entry.value)))
        else:
            current = candidate
    return current


def _validate_links(entries: dict[_Parts, _Entry], deadline: float) -> dict[_Parts, _Parts]:
    destinations: dict[_Parts, _Parts] = {}
    edges: dict[_Parts, list[_Parts]] = {
        parts: [] for parts, entry in entries.items() if entry.kind == "directory"
    }
    for parts, entry in entries.items():
        _check_deadline(deadline)
        if parts and entry.kind == "directory":
            edges[parts[:-1]].append(parts)
        elif entry.kind == "link":
            destination = _resolve_link(parts, entries, deadline)
            destinations[parts] = destination
            if entries[destination].kind == "directory":
                edges[parts[:-1]].append(destination)
    # Directory links can make a traversal cyclic even when readlink resolution
    # terminates (e.g. a/back -> .., or two real directories linking to each other).
    visiting: set[_Parts] = set()
    visited: set[_Parts] = set()
    pending: list[tuple[_Parts, bool]] = [((), False)]
    while pending:
        _check_deadline(deadline)
        parts, finished = pending.pop()
        if finished:
            visiting.remove(parts)
            visited.add(parts)
        elif parts in visiting:
            raise ValueError("cyclic validation tree directory links")
        elif parts not in visited:
            visiting.add(parts)
            pending.append((parts, True))
            pending.extend((child, False) for child in edges[parts])
    return destinations


def _verify_stats(tree: _Tree, entries: dict[_Parts, _Entry]) -> None:
    for parts, entry in entries.items():
        _check_deadline(tree.deadline)
        _unchanged(tree.lstat(parts), entry.info)
        if entry.kind == "link" and tree.readlink(parts) != entry.value:
            raise RuntimeError("validation tree symlink changed")


def _snapshot(tree: _Tree, reject_hardlinks: bool) -> dict[_Parts, _Entry]:
    entries: dict[_Parts, _Entry] = {}
    pending: list[_Parts] = [()]
    while pending:
        _check_deadline(tree.deadline)
        parts = pending.pop()
        info = tree.lstat(parts)
        kind = _kind(info)
        value = ""
        if kind == "directory":
            tree.directories[parts] = info
            pending.extend((*parts, name) for name in reversed(tree.names(parts)))
        elif kind == "file":
            if reject_hardlinks and info.st_nlink > 1:
                raise ValueError("hardlinked validation tree source file")
            value = _read_file(tree, parts, info)
        else:
            value = tree.readlink(parts)
        entries[parts] = _Entry(info, kind, value)
    _validate_links(entries, tree.deadline)
    _verify_stats(tree, entries)
    return entries


def _summary(entries: dict[_Parts, _Entry], deadline: float) -> dict[str, object]:
    digest = hashlib.sha256()
    files = size = 0
    for parts in sorted(entries):
        _check_deadline(deadline)
        entry = entries[parts]
        length = entry.info.st_size if entry.kind == "file" else 0
        record = [
            "/".join(parts), entry.kind, length,
            entry.value if entry.kind == "file" else "",
            entry.value if entry.kind == "link" else "",
        ]
        digest.update((json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
        if entry.kind == "file":
            files += 1
            size += length
    return {"sha256": digest.hexdigest(), "files": files, "bytes": size}


def _populate(
    source: _Tree, target: _Tree, entries: dict[_Parts, _Entry],
    owned: dict[_Parts, os.stat_result],
) -> None:
    destinations = _validate_links(entries, source.deadline)
    for parts, entry in entries.items():
        _check_deadline(source.deadline)
        if not parts:
            continue
        _unchanged(source.lstat(parts), entry.info)
        if entry.kind == "file":
            with target.open_file(parts, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                owned[parts] = os.fstat(fd)
                if _read_file(source, parts, entry.info, fd) != entry.value:
                    raise RuntimeError("validation tree content changed during copy")
                if sys.platform != "win32":
                    os.fchmod(fd, stat.S_IMODE(entry.info.st_mode) & 0o777)
        else:
            with target.directory(parts[:-1]) as fd:
                path = target.root.joinpath(*parts) if fd is None else parts[-1]
                if entry.kind == "directory":
                    os.mkdir(path, 0o700, dir_fd=fd)
                else:
                    os.symlink(
                        entry.value, path, dir_fd=fd,
                        target_is_directory=entries[destinations[parts]].kind == "directory",
                    )
                info = os.lstat(path, dir_fd=fd)
                owned[parts] = info
                if entry.kind == "directory":
                    target.directories[parts] = info


def _cleanup(target: _Tree, owned: dict[_Parts, os.stat_result]) -> None:
    # Rollback visits only the creation ledger, never discovers or recursively
    # deletes unknown contents. Its separate cleanup budget cannot grant a pass
    # or renew the exhausted copy deadline; the owner handles any remaining tree.
    bounded = _Tree(target.root, target.directories[()], time.monotonic() + 1.0)
    bounded.directories = target.directories
    target = bounded
    for parts, expected in reversed(owned.items()):
        _check_deadline(target.deadline)
        if not parts:
            continue
        with target.directory(parts[:-1]) as fd:
            path = target.root.joinpath(*parts) if fd is None else parts[-1]
            actual = os.lstat(path, dir_fd=fd)
            if _identity(actual) != _identity(expected):
                raise RuntimeError("validation tree cleanup refused a replaced entry")
            if _kind(actual) == "directory":
                os.rmdir(path, dir_fd=fd)
            else:
                os.unlink(path, dir_fd=fd)
    with target.directory(()):
        pass
    _check_deadline(target.deadline)
    target.root.rmdir()


def copy_validation_tree(
    source: Path, target: Path, *, deadline: float, reject_hardlinks: bool,
) -> dict[str, object]:
    """Copy to an absent target, returning a canonical SHA-256 and file totals.

    Unsafe input raises ValueError; observed changes raise RuntimeError; filesystem
    errors propagate. TimeoutError uses the caller's single monotonic deadline.
    Rollback failures also raise, and never grant a successful validation result.
    """
    if not math.isfinite(deadline):
        raise ValueError("validation tree deadline must be finite")
    _check_deadline(deadline)
    source_info = source.lstat()
    if _kind(source_info) != "directory":
        raise ValueError("validation tree source root must be a real directory")
    source = source.resolve(strict=True)
    _unchanged(source.lstat(), source_info)
    # Resolve the trusted existing parent only, never a pre-existing target link.
    target = target.parent.resolve(strict=True) / target.name
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("validation tree source and target overlap")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"validation tree target already exists: {target}")
    src = _Tree(source, source_info, deadline)
    before = _snapshot(src, reject_hardlinks)
    result = _summary(before, deadline)
    _check_deadline(deadline)
    target.mkdir(mode=0o700)
    target_info = target.lstat()
    dst = _Tree(target, target_info, deadline)
    owned: dict[_Parts, os.stat_result] = {(): target_info}
    try:
        _populate(src, dst, before, owned)
        copied = _snapshot(dst, True)
        if _summary(copied, deadline) != result:
            raise RuntimeError("validation tree source/target digest mismatch")
        after = _snapshot(src, reject_hardlinks)
        if _summary(after, deadline) != result or set(after) != set(before):
            raise RuntimeError("validation tree source changed during copy")
        for parts, entry in before.items():
            _check_deadline(deadline)
            _unchanged(after[parts].info, entry.info)
        _verify_stats(src, before)
        _verify_stats(dst, copied)
        _check_deadline(deadline)
        return result
    except BaseException as exc:
        try:
            _cleanup(dst, owned)
        except (OSError, ValueError, RuntimeError) as cleanup_error:
            raise RuntimeError(f"validation tree cleanup failed: {cleanup_error}") from exc
        raise
