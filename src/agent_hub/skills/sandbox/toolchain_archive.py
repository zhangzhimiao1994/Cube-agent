"""Bounded inspection of pinned Linux Go tarballs, without extraction or execution."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tarfile
import time
import unicodedata
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

_BLOCK = 512
_CHUNK = 64 * 1024
_ERROR_CODES = frozenset(
    {
        "invalid_archive",
        "invalid_limits",
        "archive_identity",
        "archive_limit",
        "gzip_format",
        "tar_format",
        "expanded_limit",
        "entry_limit",
        "entry_type",
        "unsafe_path",
        "path_limit",
        "path_conflict",
        "required_file",
        "version_limit",
        "version_format",
        "directory_limit", "invalid_deadline", "archive_timeout",
    }
)


class ToolchainArchiveError(ValueError):
    """Finite diagnostic codes only: never include archive-controlled strings."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "invalid_archive"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_archive_bytes: int
    max_expanded_bytes: int
    max_entries: int
    max_path_bytes: int = 4096
    max_depth: int = 64
    max_directories: int = 100_000

    def __post_init__(self) -> None:
        for value in (
            self.max_archive_bytes,
            self.max_expanded_bytes,
            self.max_entries,
            self.max_path_bytes,
            self.max_depth,
            self.max_directories,
        ):
            if type(value) is not int or value <= 0:
                raise ToolchainArchiveError("invalid_limits")


@dataclass(frozen=True, slots=True)
class GoArchiveFile:
    path: str
    size_bytes: int
    sha256: str
    executable: bool


@dataclass(frozen=True, slots=True)
class GoArchiveManifest:
    archive_sha256: str
    archive_size_bytes: int
    files: tuple[GoArchiveFile, ...]
    expanded_bytes: int
    version: str
    directories: tuple[str, ...] = ()

    @property
    def tree_sha256(self) -> str:
        encoded = json.dumps(
            {"directories": list(self.directories), "files": [
                {
                    "path": file.path,
                    "size_bytes": file.size_bytes,
                    "sha256": file.sha256,
                    "executable": file.executable,
                }
                for file in sorted(self.files, key=lambda file: file.path)
            ]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


def _check(deadline: float | None) -> None:
    if deadline is None:
        return
    try:
        if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline <= 0:
            raise ToolchainArchiveError("invalid_deadline")
    except OverflowError:
        raise ToolchainArchiveError("invalid_deadline") from None
    if time.monotonic() >= deadline:
        raise ToolchainArchiveError("archive_timeout")


def _inflate(data: bytes, limit: int, deadline: float | None) -> Iterator[bytes]:
    # zlib (unlike GzipFile) stops at the first member and exposes trailing bytes.
    decoder = zlib.decompressobj(wbits=31)
    offset = 0
    total = 0
    while offset < len(data):
        _check(deadline)
        compressed = data[offset : offset + _CHUNK]
        offset += len(compressed)
        while compressed:
            _check(deadline)
            try:
                chunk = decoder.decompress(compressed, min(_CHUNK, limit - total + 1))
            except zlib.error:
                raise ToolchainArchiveError("gzip_format") from None
            total += len(chunk)
            if total > limit:
                raise ToolchainArchiveError("expanded_limit")
            if chunk:
                yield chunk
            if decoder.eof:
                if decoder.unused_data or offset != len(data):
                    raise ToolchainArchiveError("gzip_format")
                return
            compressed = decoder.unconsumed_tail
    raise ToolchainArchiveError("gzip_format")


class _TarStream:
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks
        self.pending = b""
        self.consumed = 0

    def read(self, size: int) -> bytes:
        # Callers request at most CHUNK bytes; no archive-declared allocation.
        result = bytearray()
        while len(result) < size:
            if not self.pending:
                self.pending = next(self.chunks, b"")
                if not self.pending:
                    break
            take = min(size - len(result), len(self.pending))
            result.extend(self.pending[:take])
            self.pending = self.pending[take:]
        self.consumed += len(result)
        return bytes(result)

    def exact(self, size: int) -> bytes:
        result = self.read(size)
        if len(result) != size:
            raise ToolchainArchiveError("tar_format")
        return result

    def finish(self) -> None:
        # Drain even zero padding through the limiter, CRC and single-member gate.
        while chunk := self.read(_CHUNK):
            if any(chunk):
                raise ToolchainArchiveError("tar_format")
        if self.consumed % _BLOCK:
            raise ToolchainArchiveError("tar_format")


def _cstring(field: bytes) -> str:
    before, separator, after = field.partition(b"\0")
    if separator and any(after):
        raise ToolchainArchiveError("tar_format")
    try:
        return before.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ToolchainArchiveError("unsafe_path") from None


def _header(block: bytes) -> tuple[str, int, int, bool, bool]:
    # Only parse one ordinary header. TarFile.next() would expand PAX/longname
    # bodies before the caller can reject them or apply a payload budget.
    kind = block[156:157]
    if kind not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.XHDTYPE):
        raise ToolchainArchiveError("entry_type")
    magic = block[257:265]
    if magic not in (b"ustar\x0000", b"ustar  \x00"):
        raise ToolchainArchiveError("tar_format")
    if magic == b"ustar  \x00" and any(block[345:500]):
        raise ToolchainArchiveError("tar_format")
    if any(block[157:257]) or any(block[500:512]):
        raise ToolchainArchiveError("tar_format")
    size_field = block[124:136].strip(b" \0")
    if not size_field or any(char not in b"01234567" for char in size_field):
        raise ToolchainArchiveError("tar_format")
    try:
        info = tarfile.TarInfo.frombuf(block, encoding="utf-8", errors="strict")
    except (tarfile.HeaderError, ValueError, UnicodeError, OverflowError):
        raise ToolchainArchiveError("tar_format") from None
    if info.mode & ~0o777:
        raise ToolchainArchiveError("tar_format")
    extended = kind == tarfile.XHDTYPE
    if extended and not 0 < info.size <= 8192:
        raise ToolchainArchiveError("entry_type")
    name = _cstring(block[:100])
    if magic == b"ustar\x0000":
        prefix = _cstring(block[345:500])
        if prefix:
            name = prefix + "/" + name
    directory = kind == tarfile.DIRTYPE
    if directory and info.size:
        raise ToolchainArchiveError("tar_format")
    return name, info.size, info.mode, directory, extended


def _pax_path(data: bytes, limits: ArchiveLimits) -> str:
    # Official Go uses two tiny local path records for UTF-8 test filenames.
    # Do not admit global metadata, size/link overrides or recursive records.
    if len(data) > limits.max_path_bytes + 64:
        raise ToolchainArchiveError("path_limit")
    match = re.fullmatch(rb"([1-9][0-9]{0,7}) path=([^\n]+)\n", data)
    if match is None or int(match[1]) != len(data):
        raise ToolchainArchiveError("tar_format")
    try:
        return _path(match[2].decode("utf-8", errors="strict"), False, limits)
    except UnicodeError:
        raise ToolchainArchiveError("unsafe_path") from None


def _path(name: str, directory: bool, limits: ArchiveLimits) -> str:
    if len(name.encode("utf-8")) > limits.max_path_bytes:
        raise ToolchainArchiveError("path_limit")
    if directory and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if (
        parts[0] != "go"
        or (not directory and len(parts) == 1)
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in name
        or unicodedata.normalize("NFC", name) != name
        or any(unicodedata.category(char).startswith("C") for char in name)
    ):
        raise ToolchainArchiveError("unsafe_path")
    if len(parts) > limits.max_depth:
        raise ToolchainArchiveError("path_limit")
    return name


class _Paths:
    def __init__(self, max_directories: int) -> None:
        self.explicit: set[str] = set()
        self.nodes: dict[str, bool] = {}
        self.folded: dict[str, str] = {}
        self.directory_count = 0
        self.max_directories = max_directories

    def add(self, path: str, directory: bool) -> None:
        if path in self.explicit or (not directory and path in self.nodes):
            raise ToolchainArchiveError("path_conflict")
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            folded = prefix.casefold()
            if self.folded.get(folded, prefix) != prefix:
                raise ToolchainArchiveError("path_conflict")
            if index < len(parts) and self.nodes.get(prefix) is False:
                raise ToolchainArchiveError("path_conflict")
            self.folded[folded] = prefix
            if index < len(parts):
                if prefix not in self.nodes:
                    self.directory_count += 1
                self.nodes[prefix] = True
        if directory and path not in self.nodes:
            self.directory_count += 1
        if self.directory_count > self.max_directories:
            raise ToolchainArchiveError("directory_limit")
        self.nodes[path] = directory
        self.explicit.add(path)


def _version(content: bytes) -> str:
    try:
        text = content.decode("ascii", errors="strict")
    except UnicodeError:
        raise ToolchainArchiveError("version_format") from None
    lines = text.removesuffix("\n").split("\n")
    if (
        len(lines) not in (1, 2)
        or re.fullmatch(r"go1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", lines[0]) is None
    ):
        raise ToolchainArchiveError("version_format")
    if len(lines) == 2:
        if (
            re.fullmatch(
                r"time [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
                lines[1],
            )
            is None
        ):
            raise ToolchainArchiveError("version_format")
        try:
            datetime.fromisoformat(lines[1][5:])
        except ValueError:
            raise ToolchainArchiveError("version_format") from None
    return lines[0]


def inspect_go_archive(
    data: bytes,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    limits: ArchiveLimits,
    deadline: float | None = None,
) -> GoArchiveManifest:
    """Inspect pinned bytes only; catalog version/platform binding is separate.

    max_expanded_bytes bounds the entire inflated tar stream (including headers,
    alignment and trailer). The manifest expanded_bytes counts file content only.
    """
    _check(deadline)
    if not isinstance(limits, ArchiveLimits):
        raise ToolchainArchiveError("invalid_limits")
    if not isinstance(data, bytes):
        raise ToolchainArchiveError("invalid_archive")
    if len(data) > limits.max_archive_bytes:
        raise ToolchainArchiveError("archive_limit")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(expected_size_bytes) is not int
        or expected_size_bytes != len(data)
    ):
        raise ToolchainArchiveError("archive_identity")
    archive_digest = hashlib.sha256()
    for offset in range(0, len(data), _CHUNK):
        _check(deadline)
        archive_digest.update(data[offset:offset + _CHUNK])
    if archive_digest.hexdigest() != expected_sha256:
        raise ToolchainArchiveError("archive_identity")
    stream = _TarStream(_inflate(data, limits.max_expanded_bytes, deadline))
    paths = _Paths(limits.max_directories)
    files: list[GoArchiveFile] = []
    expanded = 0
    entries = 0
    version_content = b""
    pending_path: str | None = None
    while True:
        _check(deadline)
        block = stream.exact(_BLOCK)
        if block == bytes(_BLOCK):
            if pending_path is not None:
                raise ToolchainArchiveError("tar_format")
            if stream.exact(_BLOCK) != bytes(_BLOCK):
                raise ToolchainArchiveError("tar_format")
            stream.finish()
            break
        entries += 1
        if entries > limits.max_entries:
            raise ToolchainArchiveError("entry_limit")
        name, size, mode, directory, extended = _header(block)
        if extended:
            if pending_path is not None:
                raise ToolchainArchiveError("entry_type")
            pending_path = _pax_path(stream.exact(size), limits)
            if any(stream.exact((-size) % _BLOCK)):
                raise ToolchainArchiveError("tar_format")
            continue
        if pending_path is not None:
            if directory:
                raise ToolchainArchiveError("entry_type")
            name, pending_path = pending_path, None
        path = _path(name, directory, limits)
        paths.add(path, directory)
        if path == "go/VERSION" and size > 4096:
            raise ToolchainArchiveError("version_limit")
        if size + (-size) % _BLOCK > limits.max_expanded_bytes - stream.consumed:
            raise ToolchainArchiveError("expanded_limit")
        if directory:
            continue
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            _check(deadline)
            chunk = stream.exact(min(remaining, _CHUNK))
            digest.update(chunk)
            if path == "go/VERSION":
                version_content += chunk
            remaining -= len(chunk)
        if any(stream.exact((-size) % _BLOCK)):
            raise ToolchainArchiveError("tar_format")
        files.append(GoArchiveFile(path, size, digest.hexdigest(), bool(mode & 0o111)))
        expanded += size
    by_path = {file.path: file for file in files}
    for required in ("go/bin/go", "go/bin/gofmt", "go/VERSION"):
        file = by_path.get(required)
        if file is None or (required.startswith("go/bin/") and not file.executable):
            raise ToolchainArchiveError("required_file")
    return GoArchiveManifest(
        expected_sha256,
        len(data),
        tuple(sorted(files, key=lambda file: file.path)),
        expanded,
        _version(version_content),
        tuple(sorted(path for path, directory in paths.nodes.items() if directory)),
    )
