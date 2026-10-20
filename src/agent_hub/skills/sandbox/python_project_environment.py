"""Measure a trusted private environment; never grant approval or a runtime lease.

The outer runner supplies the root, interpreter and quotas, never model arguments.
Normalization is Linux-only and requires a fresh, unpublished, private quota-tmpfs
environment with ALL project children drained. Published caches use read-only
measurement. No environment module, interpreter, hook or archive is executed.

The first recipe only removes CPython 3.12 cache files with observed sibling
sources (only optimization levels 1/2 or unoptimized). Sourceless/other bytecode
is refused; wheel archive inspection belongs upstream. Empty cache directories
remain part of the content identity. Internal evidence has fixed structure and
budgets derived from trusted ScanLimits, independent of public Artifact limits.
Windows reads are snapshots, not a native sandbox. Normalization is not
transactional: discard the fresh tree on failure. Neither mode protects against a concurrent
writer after observation or supplies authorization, cache publication or a lease.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from agent_hub.capability_installer.python_attestation import (
    MANIFEST_NAME,
    METADATA_NAME,
    ScanLimits,
    _identity,
    scan_tree,
)
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.python_project_mounts import _pin_directory, _Pinned

_ERROR = "python_project_environment_rejected"
_CONTROLS = frozenset({MANIFEST_NAME, METADATA_NAME})
_SHA = re.compile(r"[0-9a-f]{64}")
_CACHE = re.compile(r"(.+)\.cpython-312(?:\.opt-[12])?\.pyc")
_MAX_ENTRIES = 100_000
_MAX_PATH = 4096


def _need(value: object) -> None:
    if not value:
        raise ValueError(_ERROR)


def _sha(value: object) -> str:
    _need(type(value) is str and _SHA.fullmatch(value) is not None)
    assert isinstance(value, str)
    return value


def _relative(value: object) -> str:
    _need(isinstance(value, str))
    assert isinstance(value, str)
    _need(
        0 < len(value.encode("utf-8")) <= _MAX_PATH
        and "\\" not in value
        and "\0" not in value
        and not value.startswith("/")
        and re.match(r"^[A-Za-z]:", value) is None
    )
    _need(value == "." or all(part not in {"", ".", ".."} for part in value.split("/")))
    _need(len(value.split("/")) <= 64)
    return value


def _entries(manifest: object) -> dict[str, Mapping[str, Any]]:
    _need(isinstance(manifest, Mapping))
    assert isinstance(manifest, Mapping)
    _need(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1)
    entries = manifest.get("entries")
    _need(isinstance(entries, (list, tuple)) and 0 < len(entries) <= _MAX_ENTRIES + 1)
    assert isinstance(entries, (list, tuple))
    result: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        _need(isinstance(entry, Mapping))
        assert isinstance(entry, Mapping)
        path = _relative(entry.get("path"))
        _need(path not in result and path not in _CONTROLS)
        kind, identity = entry.get("kind"), entry.get("identity")
        _need(kind in ("directory", "file", "symlink") and type(kind) is str)
        assert isinstance(kind, str)
        _need(
            isinstance(identity, (tuple, list))
            and len(identity) == (3 if kind == "directory" else 7)
            and all(type(part) is int for part in identity)
        )
        assert isinstance(identity, (tuple, list))
        mode = identity[2]
        _need(mode >= 0 and identity[0] >= 0 and identity[1] >= 0)
        _need(
            stat.S_IFMT(mode)
            == {
                "directory": stat.S_IFDIR,
                "file": stat.S_IFREG,
                "symlink": stat.S_IFLNK,
            }[kind]
        )
        _need(path != "." or kind == "directory")
        if kind != "directory":
            _need(identity[3] == 1 and identity[4] >= 0)
        if kind == "file":
            _sha(entry.get("sha256"))
        elif kind == "symlink":
            target = entry.get("target")
            _need(
                type(target) is str
                and 0 < len(target.encode("utf-8")) <= _MAX_PATH
                and "\0" not in target
            )
        result[path] = entry
    _need("." in result)
    return result


def python_environment_content_sha256(
    manifest: object,
    *,
    environment_input_sha256: str,
) -> str:
    """Pure projection, NOT observation: a model's manifest proves nothing.

    Inodes, clocks, ownership and absolute root/interpreter fields are excluded.
    Literal symlink targets are content, even if a target itself is absolute.
    An external interpreter receipt, when present, binds its actual file SHA.
    """
    try:
        input_sha = _sha(environment_input_sha256)
        entries = _entries(manifest)
        projected: list[dict[str, object]] = []
        for path, entry in sorted(entries.items()):
            _need(not path.endswith(".pyc"))
            member: dict[str, object] = {
                "path": path,
                "kind": entry["kind"],
                "executable": bool(entry["identity"][2] & 0o111),
            }
            if entry["kind"] == "file":
                member["sha256"] = entry["sha256"]
            elif entry["kind"] == "symlink":
                member["target"] = entry["target"]
            projected.append(member)
        assert isinstance(manifest, Mapping)
        external = manifest.get("interpreter_receipt")
        _need(external is None or isinstance(external, Mapping))
        external_sha = None if external is None else _sha(external.get("sha256"))
        payload = {
            "schema": "python-project-environment-content-v1",
            "environment_input_sha256": input_sha,
            "entries": projected,
            "external_interpreter_sha256": external_sha,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
    except (OSError, ValueError, TypeError, RuntimeError, OverflowError):
        raise ValueError(_ERROR) from None


@dataclass(frozen=True, slots=True)
class PythonProjectEnvironmentObservation:
    """Content identity and distinct deep-frozen security snapshot; no authority."""

    environment_sha256: str
    security_manifest: Mapping[str, JsonValue]
    total_bytes: int
    removed_bytecode_files: int


def _scan_limits(limits: ScanLimits) -> None:
    _need(type(limits) is ScanLimits)
    for value, maximum in (
        (limits.max_bytes, 2**63 - 1),
        (limits.max_entries, _MAX_ENTRIES),
        (limits.max_depth, 64),
        (limits.max_path_bytes, _MAX_PATH),
    ):
        _need(type(value) is int and 0 < value <= maximum)


def _arguments(root: Path, interpreter: Path, limits: ScanLimits, normalize: bool) -> None:
    for path in (root, interpreter):
        _need(
            isinstance(path, Path)
            and path.is_absolute()
            and ".." not in path.parts
            and "\0" not in str(path)
            and len(str(path).encode("utf-8")) <= _MAX_PATH
        )
    _scan_limits(limits)
    _need(type(normalize) is bool)
    _need(not normalize or sys.platform == "linux")


def _bounded_identity(value: object, *, directory: bool = False) -> tuple[int, ...]:
    _need(isinstance(value, (list, tuple)) and len(value) == (3 if directory else 7))
    assert isinstance(value, (list, tuple))
    _need(all(type(part) is int and -(2**127) <= part < 2**128 for part in value))
    return tuple(value)


def _freeze_security_manifest(
    manifest: object,
    *,
    limits: ScanLimits,
) -> Mapping[str, JsonValue]:
    """Freeze fixed-shape backend evidence, not a public Artifact or observation.

    A supplied manifest proves no filesystem facts. Only the observer supplies
    scan_tree output here. Reject unknown fields before copying; never recurse
    into caller-defined JSON or relax runtime.contracts' public JSON limits.
    """
    try:
        _scan_limits(limits)
        _need(isinstance(manifest, Mapping))
        assert isinstance(manifest, Mapping)
        _need(
            set(manifest)
            == {
                "schema_version",
                "entries",
                "total_bytes",
                "interpreter",
                "interpreter_receipt",
            }
        )
        entries = _entries(manifest)
        _need(len(entries) - 1 <= limits.max_entries)
        total = manifest["total_bytes"]
        _need(type(total) is int and 0 <= total <= limits.max_bytes)
        interpreter = manifest["interpreter"]
        _need(
            type(interpreter) is str
            and 0 < len(interpreter) <= _MAX_PATH
            and len(interpreter.encode("utf-8")) <= _MAX_PATH
            and "\0" not in interpreter
            and Path(interpreter).is_absolute()
        )

        # Fixed records contain at most 7 identity integers and 5 fields. Bound
        # integer width separately; count keys/values/tuple nodes and UTF-8 text
        # while constructing immutable records, with no serialization workaround.
        nodes_left = (limits.max_entries + 1) * 32 + 64
        text_left = (limits.max_entries + 1) * (limits.max_path_bytes + _MAX_PATH + 256)
        text_left += 2 * _MAX_PATH + 256

        def record(fields: dict[str, JsonValue]) -> Mapping[str, JsonValue]:
            nonlocal nodes_left, text_left
            nodes_left -= 1 + 2 * len(fields)
            for key, value in fields.items():
                text_left -= len(key.encode("utf-8"))
                if isinstance(value, str):
                    text_left -= len(value.encode("utf-8"))
                elif isinstance(value, tuple):
                    nodes_left -= len(value)
            _need(nodes_left >= 0 and text_left >= 0)
            return MappingProxyType(fields)

        accounted = 0
        frozen_entries: list[JsonValue] = []
        for path, entry in entries.items():
            kind = entry["kind"]
            keys = {"path", "kind", "identity"}
            if kind == "file":
                keys.add("sha256")
            elif kind == "symlink":
                keys.add("target")
            _need(set(entry) == keys)
            _need(
                len(path.encode("utf-8")) <= limits.max_path_bytes
                and (path == "." or len(path.split("/")) <= limits.max_depth)
            )
            if path != ".":
                parent = entries.get(PurePosixPath(path).parent.as_posix())
                _need(parent is not None and parent["kind"] == "directory")
            identity = _bounded_identity(entry["identity"], directory=kind == "directory")
            fields: dict[str, JsonValue] = {"path": path, "kind": kind, "identity": identity}
            if kind == "file":
                accounted += identity[4]
                fields["sha256"] = _sha(entry["sha256"])
            elif kind == "symlink":
                fields["target"] = entry["target"]
            frozen_entries.append(record(fields))
        external = manifest["interpreter_receipt"]
        frozen_external: JsonValue = None
        if external is not None:
            _need(isinstance(external, Mapping) and set(external) == {"identity", "sha256"})
            assert isinstance(external, Mapping)
            identity = _bounded_identity(external["identity"])
            _need(
                identity[0] >= 0
                and identity[1] >= 0
                and identity[3] == 1
                and stat.S_ISREG(identity[2])
                and identity[4] >= 0
            )
            accounted += identity[4]
            frozen_external = record({"identity": identity, "sha256": _sha(external["sha256"])})
        _need(accounted == total)
        return record(
            {
                "schema_version": 1,
                "entries": tuple(frozen_entries),
                "total_bytes": total,
                "interpreter": interpreter,
                "interpreter_receipt": frozen_external,
            }
        )
    except (OSError, ValueError, TypeError, RuntimeError, OverflowError):
        raise ValueError(_ERROR) from None


def _no_controls(root: Path) -> None:
    for name in _CONTROLS:
        try:
            (root / name).lstat()
        except FileNotFoundError:
            continue
        raise ValueError(_ERROR)


def _scan(root: Path, interpreter: Path, limits: ScanLimits) -> dict[str, Any]:
    _no_controls(root)
    receipt = scan_tree(root, interpreter=interpreter, limits=limits)
    _no_controls(root)
    return receipt


def _check_pins(pins: list[_Pinned]) -> None:
    for pin in pins:
        pin.check()


def _visit_bytecode(
    root: Path,
    root_fd: int,
    entries: Mapping[str, Mapping[str, Any]],
    path: str,
    source: str,
    pins: list[_Pinned],
    directory_identities: dict[str, tuple[int, ...]],
    *,
    remove: bool,
) -> None:
    """Rooted no-follow window: O(depth) FDs, closed before the next file."""
    if sys.platform != "linux":
        raise ValueError(_ERROR)
    _need(list(entries["."]["identity"]) == _identity(os.fstat(root_fd))[:3])
    _check_pins(pins)
    with ExitStack() as window:
        directories = {".": root_fd}
        local_pins: list[_Pinned] = []
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

        def directory(relative: str) -> int:
            if relative in directories:
                return directories[relative]
            member = PurePosixPath(relative)
            parent = directory(member.parent.as_posix())
            expected = entries[relative]
            _need(expected["kind"] == "directory")
            before = os.stat(member.name, dir_fd=parent, follow_symlinks=False)
            _need(
                _identity(before)[:3] == list(expected["identity"])
                and before.st_uid in {0, os.getuid()}
                and not before.st_mode & 0o022
            )
            identity = (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid)
            previous = directory_identities.setdefault(relative, identity)
            _need(identity == previous)
            descriptor = os.open(member.name, directory_flags, dir_fd=parent)
            window.callback(os.close, descriptor)
            pin = _Pinned(parent, member.name, descriptor, identity)
            pin.check()
            local_pins.append(pin)
            directories[relative] = descriptor
            return descriptor

        opened: dict[str, tuple[int, int]] = {}
        for relative in (source, path):
            member = PurePosixPath(relative)
            parent = directory(member.parent.as_posix())
            descriptor = os.open(
                member.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
            window.callback(os.close, descriptor)
            opened[relative] = parent, descriptor

        def check_file(relative: str) -> None:
            parent, descriptor = opened[relative]
            current = os.fstat(descriptor)
            _need(
                stat.S_ISREG(current.st_mode)
                and current.st_nlink == 1
                and _identity(current) == list(entries[relative]["identity"])
                and _identity(
                    os.stat(PurePosixPath(relative).name, dir_fd=parent, follow_symlinks=False)
                )
                == list(entries[relative]["identity"])
            )

        check_file(source)
        check_file(path)
        source_size = entries[source]["identity"][4]
        count = 0
        digest = hashlib.sha256()
        while chunk := os.read(opened[source][1], min(1024 * 1024, source_size - count + 1)):
            count += len(chunk)
            _need(count <= source_size)
            digest.update(chunk)
        _need(count == source_size and digest.hexdigest() == entries[source]["sha256"])
        _check_pins(local_pins)
        _check_pins(pins)
        check_file(source)
        check_file(path)
        if remove:
            _no_controls(root)
            os.unlink(PurePosixPath(path).name, dir_fd=opened[path][0])
            check_file(source)
        _check_pins(local_pins)
        _check_pins(pins)


def _normalize(
    root: Path,
    root_fd: int,
    manifest: dict[str, Any],
    pins: list[_Pinned],
) -> set[str]:
    """Validate every candidate, then read-only preflight ALL before unlinking.

    Only identity values survive a window, never file/parent descriptors. Reopen
    from the pinned root and recheck the initial source SHA on the deletion pass.
    """
    entries = _entries(manifest)
    candidates: dict[str, str] = {}
    for path in entries:
        if not path.endswith(".pyc"):
            continue
        member = PurePosixPath(path)
        match = _CACHE.fullmatch(member.name)
        _need(member.parent.name == "__pycache__" and match is not None)
        assert match is not None
        source = (member.parent.parent / (match[1] + ".py")).as_posix()
        _need(
            entries[path]["kind"] == "file"
            and source in entries
            and entries[source]["kind"] == "file"
        )
        candidates[path] = source
    directory_identities: dict[str, tuple[int, ...]] = {}
    for remove in (False, True):
        _no_controls(root)
        for path, source in candidates.items():
            _visit_bytecode(
                root, root_fd, entries, path, source, pins, directory_identities, remove=remove
            )
    return set(candidates)


def observe_python_project_environment(
    root: Path,
    *,
    interpreter: Path,
    environment_input_sha256: str,
    limits: ScanLimits,
    normalize_bytecode: bool = False,
) -> PythonProjectEnvironmentObservation:
    """Observe actual bytes from a caller-owned tree, never a model manifest.

    Native mutation assumes a drained, private fresh environment. The caller
    must enforce that prerequisite; this API neither discovers nor grants it.
    Errors suppress underlying host paths, including exception chaining.
    """
    try:
        _arguments(root, interpreter, limits, normalize_bytecode)
        _sha(environment_input_sha256)
        with ExitStack() as stack:
            pins: list[_Pinned] = []
            root_fd = _pin_directory(root, stack, pins) if normalize_bytecode else None
            initial = _scan(root, interpreter, limits)
            removed: set[str] = set()
            actual = initial
            if root_fd is not None:
                _check_pins(pins)
                removed = _normalize(root, root_fd, initial, pins)
                actual = _scan(root, interpreter, limits)
                _check_pins(pins)
                _need(
                    _entries(actual)
                    == {
                        path: entry
                        for path, entry in _entries(initial).items()
                        if path not in removed
                    }
                    and actual["interpreter_receipt"] == initial["interpreter_receipt"]
                )
            digest = python_environment_content_sha256(
                actual,
                environment_input_sha256=environment_input_sha256,
            )
            frozen = _freeze_security_manifest(actual, limits=limits)
            _no_controls(root)
            _check_pins(pins)
            return PythonProjectEnvironmentObservation(
                digest,
                frozen,
                actual["total_bytes"],
                len(removed),
            )
    except (OSError, ValueError, TypeError, RuntimeError, OverflowError):
        raise ValueError(_ERROR) from None
