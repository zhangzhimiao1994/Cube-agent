"""Bounded local integrity receipts, never package approval or signing authority.

POSIX directory-relative descriptors prevent following replaced tree members.
Windows uses descriptor identity checks and rejects reparse points; this is not
a Windows filesystem sandbox. Verification is a snapshot, not a runtime lease.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_NAME = "python-tree-attestation.json"
METADATA_NAME = "environment.json"
MAX_CONTROL_BYTES = 32 * 1024 * 1024


class PythonAttestationError(ValueError):
    """An installed tree or its receipt cannot be trusted as a local snapshot."""


class PythonAttestationLimitError(PythonAttestationError):
    """A scan exceeded a finite storage or traversal limit."""


@dataclass(frozen=True)
class ScanLimits:
    max_bytes: int
    max_entries: int = 100_000
    max_depth: int = 64
    max_path_bytes: int = 4096


@dataclass
class _Budget:
    limits: ScanLimits
    bytes: int = 0
    entries: int = 0

    def consume(self, count: int) -> None:
        self.bytes += count
        if self.bytes > self.limits.max_bytes:
            raise PythonAttestationLimitError("Python attestation byte quota exceeded")

    def member(self, relative: str, depth: int) -> None:
        self.entries += 1
        if (
            self.entries > self.limits.max_entries
            or depth > self.limits.max_depth
            or len(relative.encode("utf-8")) > self.limits.max_path_bytes
        ):
            raise PythonAttestationLimitError("Python attestation traversal limit exceeded")


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _identity(info: os.stat_result) -> list[int]:
    # Windows synthesizes execute bits from filenames differently for fstat/lstat.
    mode = info.st_mode & ~0o111 if os.name == "nt" else info.st_mode
    return [
        info.st_dev,
        info.st_ino,
        mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _lstat(path: Path, parent_fd: int | None = None) -> os.stat_result:
    if parent_fd is not None:
        return os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    return path.lstat()


def _open(path: Path, flags: int, parent_fd: int | None) -> int:
    if parent_fd is not None:
        return os.open(path.name, flags, dir_fd=parent_fd)
    return os.open(path, flags)


@contextmanager
def _directory(path: Path, parent_fd: int | None = None) -> Iterator[int | None]:
    before = _lstat(path, parent_fd)
    if not stat.S_ISDIR(before.st_mode) or _reparse(before):
        raise PythonAttestationError("Python tree directory identity is unsafe")
    if parent_fd is None and path.resolve(strict=True) != path:
        raise PythonAttestationError("Python tree directory ancestry changed")
    if os.name == "nt":
        yield None
    else:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        nofollow_flag = getattr(os, "O_NOFOLLOW", None)
        if directory_flag is None or nofollow_flag is None:
            raise PythonAttestationError("Python attestation requires no-follow directory support")
        fd = _open(path, os.O_RDONLY | directory_flag | nofollow_flag, parent_fd)
        try:
            if _identity(os.fstat(fd)) != _identity(before):
                raise PythonAttestationError("Python tree directory identity changed")
            yield fd
            if _identity(os.fstat(fd)) != _identity(before):
                raise PythonAttestationError("Python tree changed during scan")
        finally:
            os.close(fd)
    if _identity(_lstat(path, parent_fd)) != _identity(before):
        raise PythonAttestationError("Python tree directory changed during scan")
    if parent_fd is None and path.resolve(strict=True) != path:
        raise PythonAttestationError("Python tree directory ancestry changed")


def _regular(
    path: Path,
    parent_fd: int | None,
    budget: _Budget,
    *,
    capture: bool = False,
) -> tuple[dict[str, Any], bytes]:
    before = _lstat(path, parent_fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _reparse(before):
        raise PythonAttestationError("Python attestation requires regular single-link files")
    if before.st_size > budget.limits.max_bytes - budget.bytes:
        raise PythonAttestationLimitError("Python attestation byte quota exceeded")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = _open(path, flags, parent_fd)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    count = 0
    try:
        opened = os.fstat(fd)
        # Some Windows Python builds report creation time through fstat and
        # change time through lstat. Compare each clock through its own API.
        descriptor_identity = _identity(opened)
        path_identity = _identity(before)
        if (descriptor_identity[:-1] if os.name == "nt" else descriptor_identity) != (
            path_identity[:-1] if os.name == "nt" else path_identity
        ):
            raise PythonAttestationError("Python attestation file identity changed before read")
        while chunk := os.read(fd, min(1024 * 1024, budget.limits.max_bytes - budget.bytes + 1)):
            budget.consume(len(chunk))
            count += len(chunk)
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
        if count != before.st_size or _identity(os.fstat(fd)) != descriptor_identity:
            raise PythonAttestationError("Python attestation file changed during read")
        if _identity(_lstat(path, parent_fd)) != _identity(before):
            raise PythonAttestationError("Python attestation file replaced during read")
    finally:
        os.close(fd)
    return {"identity": _identity(before), "sha256": digest.hexdigest()}, b"".join(chunks)


def read_control(path: Path, *, max_bytes: int = MAX_CONTROL_BYTES) -> tuple[bytes, dict[str, Any]]:
    """Read a bounded regular control file without following its final component."""
    try:
        with _directory(path.parent) as parent_fd:
            receipt, data = _regular(path, parent_fd, _Budget(ScanLimits(max_bytes)), capture=True)
        return data, receipt
    except (OSError, RuntimeError) as error:
        raise PythonAttestationError("Python attestation control identity is invalid") from error


def bounded_tree_size(root: Path, *, limits: ScanLimits) -> int:
    """Account owned storage without traversing symlinks or unbounded fanout."""
    budget = _Budget(limits)

    def walk(directory: Path, parent_fd: int | None, depth: int) -> None:
        with (
            _directory(directory, parent_fd) as fd,
            os.scandir(fd if fd is not None else directory) as members,
        ):
            for member in members:
                path = directory / member.name
                budget.member(path.relative_to(root).as_posix(), depth + 1)
                info = _lstat(path, fd)
                if stat.S_ISLNK(info.st_mode):
                    budget.consume(info.st_size)
                elif stat.S_ISDIR(info.st_mode) and not _reparse(info):
                    walk(path, fd, depth + 1)
                elif stat.S_ISREG(info.st_mode) and not _reparse(info) and info.st_nlink == 1:
                    budget.consume(info.st_size)
                else:
                    raise PythonAttestationError("Python storage member identity is unsafe")

    try:
        walk(root, None, 0)
    except (OSError, RuntimeError) as error:
        raise PythonAttestationError("Python storage tree identity is invalid") from error
    return budget.bytes


def scan_tree(root: Path, *, interpreter: Path, limits: ScanLimits) -> dict[str, Any]:
    """Hash every member except the two separately bound root control files.

    Only standard venv interpreter links to the installer-selected interpreter
    and the POSIX lib64 -> lib alias are allowed to escape ordinary file rules.
    Paths are relative so publication rename leaves the receipt unchanged.
    """
    budget = _Budget(limits)
    entries: list[dict[str, Any]] = []
    observed: dict[Path, list[int]] = {}
    external_receipt: dict[str, Any] | None = None

    def walk(directory: Path, parent_fd: int | None, depth: int) -> None:
        nonlocal external_receipt
        with _directory(directory, parent_fd) as fd:
            relative = directory.relative_to(root).as_posix()
            info = _lstat(directory, parent_fd)
            observed[directory] = _identity(info)
            entries.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "identity": [info.st_dev, info.st_ino, info.st_mode],
                }
            )
            # Consume names before sorting; directory fanout is bounded too.
            names: list[str] = []
            with os.scandir(fd if fd is not None else directory) as members:
                for member in members:
                    child = directory / member.name
                    rel = child.relative_to(root).as_posix()
                    budget.member(rel, depth + 1)
                    names.append(member.name)
            for name in sorted(names):
                child = directory / name
                relative = child.relative_to(root).as_posix()
                if relative in {METADATA_NAME, MANIFEST_NAME}:
                    continue
                info = _lstat(child, fd)
                observed[child] = _identity(info)
                if stat.S_ISLNK(info.st_mode):
                    if os.name == "nt":
                        raise PythonAttestationError("Python tree reparse link is forbidden")
                    target = os.readlink(name, dir_fd=fd) if fd is not None else os.readlink(child)
                    if relative == "lib64" and target == "lib":
                        if child.resolve(strict=True) != root / "lib":
                            raise PythonAttestationError("Python tree lib64 alias is invalid")
                    elif re.fullmatch(r"bin/python(?:\d+(?:\.\d+)?)?", relative):
                        if child.resolve(strict=True) != interpreter:
                            raise PythonAttestationError("Python tree interpreter link changed")
                        if external_receipt is None:
                            with _directory(interpreter.parent) as interpreter_fd:
                                external_receipt, _ = _regular(interpreter, interpreter_fd, budget)
                    else:
                        raise PythonAttestationError("Python tree contains an unapproved symlink")
                    if _identity(_lstat(child, fd)) != _identity(info):
                        raise PythonAttestationError("Python tree link changed during scan")
                    entries.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "target": target,
                            "identity": _identity(info),
                        }
                    )
                elif stat.S_ISDIR(info.st_mode):
                    walk(child, fd, depth + 1)
                else:
                    receipt, _ = _regular(child, fd, budget)
                    entries.append({"path": relative, "kind": "file", **receipt})

    try:
        walk(root, None, 0)
        # Catch changes to earlier members while later files were being hashed.
        # This remains a bounded snapshot, never a claim of runtime immutability.
        for path, identity in observed.items():
            with _directory(path.parent) as fd:
                if _identity(_lstat(path, fd)) != identity:
                    raise PythonAttestationError("Python tree changed before scan completed")
        if external_receipt is not None:
            with _directory(interpreter.parent) as fd:
                if _identity(_lstat(interpreter, fd)) != external_receipt["identity"]:
                    raise PythonAttestationError("Python tree interpreter changed during scan")
    except (OSError, RuntimeError) as error:
        raise PythonAttestationError("Python tree identity is invalid") from error
    return {
        "schema_version": 1,
        "entries": entries,
        "total_bytes": budget.bytes,
        "interpreter": str(interpreter),
        "interpreter_receipt": external_receipt,
    }


def verify_tree(
    root: Path,
    *,
    metadata: Mapping[str, Any],
    metadata_receipt: Mapping[str, Any],
    anchor: Mapping[str, Any],
    limits: ScanLimits,
) -> str:
    """Verify against a state-held local reference; never bless a new baseline."""
    if anchor.get("metadata") != metadata_receipt:
        raise PythonAttestationError("Python attestation metadata identity changed")
    data, receipt = read_control(
        root / MANIFEST_NAME,
        max_bytes=min(
            MAX_CONTROL_BYTES,
            limits.max_bytes,
        ),
    )
    if anchor.get("manifest") != receipt:
        raise PythonAttestationError("Python attestation manifest identity changed")
    expected = {"schema_version": 1, "manifest": MANIFEST_NAME, "sha256": receipt["sha256"]}
    if metadata.get("python_attestation") != expected:
        raise PythonAttestationError("Python attestation metadata reference is invalid")
    try:
        manifest = json.loads(data)
        if not isinstance(manifest, dict) or type(manifest.get("interpreter")) is not str:
            raise PythonAttestationError("Python attestation manifest format is invalid")
        interpreter = Path(manifest["interpreter"])
        if not interpreter.is_absolute() or interpreter.resolve(strict=True) != interpreter:
            raise PythonAttestationError("Python attestation interpreter identity changed")
        current = scan_tree(root, interpreter=interpreter, limits=limits)
        if current != manifest:
            raise PythonAttestationError("installed Python tree attestation changed")
        # Controls are excluded from the tree hash, but must stay pinned across the scan.
        if read_control(root / METADATA_NAME, max_bytes=65536)[1] != metadata_receipt:
            raise PythonAttestationError("Python attestation metadata replaced during scan")
        if (
            read_control(root / MANIFEST_NAME, max_bytes=min(MAX_CONTROL_BYTES, limits.max_bytes))[
                1
            ]
            != receipt
        ):
            raise PythonAttestationError("Python attestation manifest replaced during scan")
        return str(receipt["sha256"])
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, PythonAttestationError):
            raise
        raise PythonAttestationError("Python attestation manifest is invalid") from error
