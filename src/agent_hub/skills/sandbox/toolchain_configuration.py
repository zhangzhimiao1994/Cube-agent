"""Read operator-owned SDK controls and observe space without creating state.

Only a native Root broker may call these helpers. Paths and execution UID must
come from operator configuration/BrokerPolicy, never a model or project. The
caller must authenticate a live scope and hold its global supply lock across
measurement and subsequent admission. Returned data is NOT a reservation,
installation grant or proof that an execution environment is ready.
"""

from __future__ import annotations

import math
import os
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_hub.skills.sandbox.toolchain_catalog import (
    FilesystemRole,
    FilesystemSpace,
    ToolchainArtifact,
    ToolchainCatalog,
)

_MAX_CONFIG = 512 * 1024
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_Identity = tuple[int, int]


class ToolchainConfigurationError(ValueError):
    """Finite errors never expose control bytes, host paths or OS messages."""

    def __init__(self, code: str = "toolchain_configuration_unsafe") -> None:
        self.code = code
        super().__init__(code)


def _need(value: object) -> None:
    if not value:
        raise ToolchainConfigurationError()


def _check(deadline: float) -> None:
    _need(type(deadline) in {int, float})
    try:
        _need(math.isfinite(deadline) and deadline > 0)
    except OverflowError:
        raise ToolchainConfigurationError() from None
    if time.monotonic() >= deadline:
        raise ToolchainConfigurationError("toolchain_configuration_timeout")


def _native(deadline: float) -> None:
    if (
        sys.platform != "linux"
        or os.name != "posix"
        or os.geteuid() != 0
        or os.getegid() != 0
        or not all(
            getattr(os, flag, 0)
            for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
        )
        or not {os.open, os.stat} <= os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or not callable(getattr(os, "fstatvfs", None))
    ):
        raise ToolchainConfigurationError("toolchain_configuration_unsupported")
    _check(deadline)


def _path(path: Path) -> Path:
    _need(isinstance(path, Path))
    text = path.as_posix()
    parsed = PurePosixPath(text)
    _need(
        parsed.is_absolute()
        and parsed.anchor == "/"
        and 1 < len(parsed.parts) <= 64
        and parsed.as_posix() == text
        and ".." not in parsed.parts
        and len(text.encode("utf-8")) <= 4096
        and not any(c in text for c in "\\:")
        and all(ord(c) >= 32 and ord(c) != 127 for c in text)
    )
    return path


def _decode(raw: bytes) -> ToolchainCatalog:
    try:
        return ToolchainCatalog.from_json(raw)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ToolchainConfigurationError() from None


def _catalog(value: ToolchainCatalog) -> ToolchainCatalog:
    try:
        _need(type(value) is ToolchainCatalog)
        # model_copy/model_construct can bypass validation, even with extra keys
        # that Pydantic serialization would silently omit.
        _need(
            set(vars(value)) == set(ToolchainCatalog.model_fields)
            and not value.model_extra
            and type(value.artifacts) is tuple
            and 1 <= len(value.artifacts) <= 1000
        )
        for entry in value.artifacts:
            _need(
                type(entry) is ToolchainArtifact
                and set(vars(entry)) == set(ToolchainArtifact.model_fields)
                and not entry.model_extra
            )
        # Serialize plain fields rather than an invalid Pydantic instance:
        # serializer warnings can otherwise disclose untrusted private values.
        return ToolchainCatalog.from_payload(
            vars(value) | {"artifacts": [vars(entry) for entry in value.artifacts]}
        )
    except ToolchainConfigurationError:
        raise
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ToolchainConfigurationError() from None


def _stamp(info: os.stat_result, *, full: bool) -> tuple[int, ...]:
    base = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return (
        (*base, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if full else base
    )


@dataclass(frozen=True, slots=True)
class _Pin:
    parent: int
    name: str
    fd: int
    snapshot: tuple[int, ...]
    full: bool

    def check(self, deadline: float) -> None:
        _check(deadline)
        opened = os.fstat(self.fd)
        linked = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
        _need(_stamp(opened, full=self.full) == self.snapshot == _stamp(linked, full=self.full))
        _check(deadline)


def _directory(
    path: Path,
    stack: ExitStack,
    pins: list[_Pin],
    deadline: float,
    *,
    owner: int = 0,
    private: bool = False,
) -> tuple[int, tuple[_Identity, ...]]:
    _path(path)
    _check(deadline)
    fd = os.open("/", _DIRECTORY_FLAGS)
    stack.callback(os.close, fd)
    root = os.fstat(fd)
    _need(stat.S_ISDIR(root.st_mode) and root.st_uid == 0 and not root.st_mode & 0o022)
    pins.append(_Pin(fd, ".", fd, _stamp(root, full=False), False))
    chain: list[_Identity] = [(root.st_dev, root.st_ino)]
    for index, name in enumerate(path.parts[1:], 1):
        _check(deadline)
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
        stack.callback(os.close, child)
        info = os.fstat(child)
        final = index == len(path.parts) - 1
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == (owner if final else 0))
        if final:
            _need(not info.st_mode & 0o022)
            if private:
                _need(stat.S_IMODE(info.st_mode) == 0o700)
        else:
            # Match broker ancestry: Root-owned sticky temporary ancestors are
            # allowed; the final directory never receives this exception.
            _need(not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX)
        pin = _Pin(fd, name, child, _stamp(info, full=final), final)
        pin.check(deadline)
        pins.append(pin)
        chain.append((info.st_dev, info.st_ino))
        fd = child
    return fd, tuple(chain)


def _unaliased(chains: tuple[tuple[_Identity, ...], ...]) -> None:
    for index, chain in enumerate(chains):
        _need(bool(chain) and len(set(chain)) == len(chain))
        for other in chains[index + 1 :]:
            _need(bool(other) and chain[-1] not in other and other[-1] not in chain)


def _roots(
    catalog: ToolchainCatalog,
    stack: ExitStack,
    pins: list[_Pin],
    deadline: float,
    *,
    execution_root: Path | None = None,
    execution_owner_uid: int = 0,
) -> tuple[tuple[FilesystemRole, int], ...]:
    entries: list[tuple[FilesystemRole, Path, int, bool]] = [
        ("stage", Path(catalog.stage_root), 0, True),
        ("runtime", Path(catalog.runtime_root), 0, True),
        ("state", Path(catalog.state_root), 0, True),
    ]
    if execution_root is not None:
        entries.append(("execution", _path(execution_root), execution_owner_uid, False))
    paths = tuple(PurePosixPath(path.as_posix()) for _, path, _, _ in entries)
    for index, path in enumerate(paths):
        _need(
            all(
                path != other and not path.is_relative_to(other) and not other.is_relative_to(path)
                for other in paths[index + 1 :]
            )
        )
    result: list[tuple[FilesystemRole, int]] = []
    chains: list[tuple[_Identity, ...]] = []
    for role, root, owner, private in entries:
        fd, chain = _directory(root, stack, pins, deadline, owner=owner, private=private)
        result.append((role, fd))
        chains.append(chain)
    _unaliased(tuple(chains))
    return tuple(result)


def _verify(pins: list[_Pin], deadline: float) -> None:
    for pin in pins:
        pin.check(deadline)
    _check(deadline)


def read_toolchain_configuration(
    path: Path = Path("/etc/agent-hub/toolchains.json"),
    *,
    deadline: float,
) -> ToolchainCatalog:
    """Read a Root600/single-link JSON control and verify three existing Root700 roots.

    All traversal is native nofollow DirFD. Root-owned sticky ancestors follow
    broker policy; neither a final SDK root nor the control gets that exception.
    No directories, locks or files are created. The operator chooses this path.
    """
    try:
        _native(deadline)
        _path(path)
        with ExitStack() as stack:
            pins: list[_Pin] = []
            # The control's parent is ancestry, not a private SDK destination.
            parent = _control_parent(path, stack, pins, deadline)
            _check(deadline)
            try:
                fd = os.open(path.name, _READ_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                raise ToolchainConfigurationError("toolchain_configuration_missing") from None
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            _need(
                stat.S_ISREG(before.st_mode)
                and before.st_uid == 0
                and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600
                and 0 < before.st_size <= _MAX_CONFIG
            )
            control = _Pin(parent, path.name, fd, _stamp(before, full=True), True)
            control.check(deadline)
            pins.append(control)
            chunks: list[bytes] = []
            total = 0
            while True:
                _check(deadline)
                chunk = os.read(fd, min(65536, _MAX_CONFIG + 1 - total))
                _check(deadline)
                if not chunk:
                    break
                total += len(chunk)
                _need(total <= _MAX_CONFIG)
                chunks.append(chunk)
            _need(total == before.st_size)
            control.check(deadline)
            catalog = _decode(b"".join(chunks))
            _check(deadline)
            _roots(catalog, stack, pins, deadline)
            _verify(pins, deadline)
            return catalog
    except ToolchainConfigurationError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ToolchainConfigurationError() from None


def _control_parent(path: Path, stack: ExitStack, pins: list[_Pin], deadline: float) -> int:
    fd = os.open("/", _DIRECTORY_FLAGS)
    stack.callback(os.close, fd)
    for name in (".", *path.parts[1:-1]):
        _check(deadline)
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
        stack.callback(os.close, child)
        info = os.fstat(child)
        _need(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX)
        )
        pin = _Pin(fd, name, child, _stamp(info, full=False), False)
        pin.check(deadline)
        pins.append(pin)
        fd = child
    return fd


def measure_toolchain_spaces(
    catalog: ToolchainCatalog,
    execution_root: Path,
    *,
    execution_owner_uid: int,
    deadline: float,
) -> tuple[FilesystemSpace, ...]:
    """Observe stage/runtime/state/execution by pinned FD, once per actual device.

    execution_root/UID must be supplied by operator BrokerPolicy, never a model.
    A validated catalog object alone is not proof of trusted configuration origin.
    The caller must retain its global lock and authenticated live scope, recheck
    admission, and own reservations/lifetime; this function reserves nothing.
    """
    try:
        _native(deadline)
        _need(type(execution_owner_uid) is int and 0 <= execution_owner_uid < 2**32 - 1)
        catalog = _catalog(catalog)
        _path(execution_root)
        with ExitStack() as stack:
            pins: list[_Pin] = []
            roots = _roots(
                catalog,
                stack,
                pins,
                deadline,
                execution_root=execution_root,
                execution_owner_uid=execution_owner_uid,
            )
            _verify(pins, deadline)
            devices: dict[int, tuple[int, int]] = {}
            result: list[FilesystemSpace] = []
            statvfs = getattr(os, "fstatvfs", None)
            _need(callable(statvfs))
            assert callable(statvfs)
            for role, fd in roots:
                _check(deadline)
                device = os.fstat(fd).st_dev
                if device not in devices:
                    disk = statvfs(fd)
                    _check(deadline)
                    _need(disk.f_frsize > 0 and disk.f_bsize > 0 and disk.f_bavail >= 0)
                    devices[device] = (
                        disk.f_bavail * disk.f_frsize,
                        max(disk.f_frsize, disk.f_bsize),
                    )
                free, unit = devices[device]
                result.append(
                    FilesystemSpace(
                        role=role, device=device, available_bytes=free, allocation_unit=unit
                    )
                )
            _verify(pins, deadline)
            return tuple(result)
    except ToolchainConfigurationError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ToolchainConfigurationError() from None
