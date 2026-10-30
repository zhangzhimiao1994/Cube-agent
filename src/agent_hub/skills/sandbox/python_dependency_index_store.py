"""Root-owned metadata index IO, outside the flat wheel cache.

Only the explicit --publish CLI builds an index from configured local wheels.
Readers never touch wheel bytes. Directory flock serializes all readers/writers,
including the brief two-link publication window. No final entry is ever removed
or replaced; uncertain outcomes retain evidence. No network or execution occurs.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import (
    RootSupplyConfig,
    _check_pins,
    _directory,
    _DirectoryPin,
    _flags,
    _stamp,
)

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.python_project_backend import RootConfig

_Code = Literal["metadata_unavailable", "metadata_untrusted", "catalog_changed", "unsupported", "timeout"]
_CODES = frozenset({"metadata_unavailable", "metadata_untrusted", "catalog_changed", "unsupported", "timeout"})
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_READ_SECONDS = 10.0
_PUBLISH_SECONDS = 60.0


class PythonDependencyIndexError(ValueError):
    """Finite reasons only, with no host paths, URLs or parser messages."""

    def __init__(self, code: _Code) -> None:
        self.code: _Code = code if type(code) is str and code in _CODES else "metadata_untrusted"
        super().__init__(self.code)


def _need(value: object, code: _Code = "metadata_untrusted") -> None:
    if not value:
        raise PythonDependencyIndexError(code)


def _native() -> None:
    if sys.platform != "linux":
        raise PythonDependencyIndexError("unsupported")
    _need(os.name == "posix", "unsupported")
    _need(os.geteuid() == os.getegid() == 0, "unsupported")
    _need({os.open, os.stat, os.link, os.unlink} <= os.supports_dir_fd
          and {os.stat, os.link} <= os.supports_follow_symlinks, "unsupported")
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
        flag = getattr(os, name, None)
        _need(type(flag) is int and flag > 0, "unsupported")


def _check(deadline: float) -> None:
    _need(type(deadline) in {int, float} and math.isfinite(deadline)
          and time.monotonic() < deadline, "timeout")


def _bounded_deadline(deadline: float | None, seconds: float) -> float:
    limit = time.monotonic() + seconds
    if deadline is None:
        return limit
    try:
        _check(deadline)
    except OverflowError:
        raise PythonDependencyIndexError("timeout") from None
    return min(limit, deadline)


def _supply(value: RootSupplyConfig) -> RootSupplyConfig:
    _need(type(value) is RootSupplyConfig)
    return replace(value)


def _same_supply(value: RootSupplyConfig, digest: str) -> None:
    try:
        _need(_supply(value).digest == digest, "catalog_changed")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise PythonDependencyIndexError("catalog_changed") from None


def _directories(pins: list[_DirectoryPin], deadline: float) -> None:
    _check(deadline)
    try:
        _check_pins(pins)
    except FileNotFoundError:
        raise PythonDependencyIndexError("metadata_untrusted") from None
    _check(deadline)


def _private_state(parent: int) -> None:
    info = os.fstat(parent)
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0
          and stat.S_IMODE(info.st_mode) == 0o700)


def _lock(parent: int, *, exclusive: bool, deadline: float) -> None:
    if sys.platform != "linux":
        raise PythonDependencyIndexError("unsupported")
    try:
        import fcntl
    except ImportError:
        raise PythonDependencyIndexError("unsupported") from None
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        _check(deadline)
        try:
            fcntl.flock(parent, mode | fcntl.LOCK_NB)
            _check(deadline)
            return
        except (BlockingIOError, InterruptedError):
            _check(deadline)
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _state(supply: RootSupplyConfig, stack: ExitStack, pins: list[_DirectoryPin],
           *, exclusive: bool, deadline: float) -> int:
    _check(deadline)
    parent = _directory(supply.state_root, stack, pins)
    _private_state(parent)
    _directories(pins, deadline)
    _lock(parent, exclusive=exclusive, deadline=deadline)
    _directories(pins, deadline)
    _private_state(parent)
    return parent


@dataclass(frozen=True, slots=True)
class _FilePin:
    parent: int
    name: str
    descriptor: int
    identity: tuple[int, ...]

    def check(self) -> None:
        try:
            _need(_stamp(os.fstat(self.descriptor)) == self.identity
                  == _stamp(os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)))
        except FileNotFoundError:
            raise PythonDependencyIndexError("metadata_untrusted") from None


def _read_bytes(descriptor: int, maximum: int, deadline: float) -> bytes:
    result = bytearray()
    while True:
        _check(deadline)
        chunk = os.read(descriptor, min(65536, maximum + 1 - len(result)))
        if not chunk:
            break
        _need(len(result) + len(chunk) <= maximum)
        result.extend(chunk)
    _check(deadline)
    return bytes(result)


def _read_file(parent: int, name: str, maximum: int, stack: ExitStack,
               deadline: float, *, expected_size: int | None = None,
               private: bool = True) -> tuple[bytes, _FilePin]:
    _check(deadline)
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _need(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 0
          and before.st_nlink == 1 and before.st_dev == os.fstat(parent).st_dev
          and 0 < before.st_size <= maximum
          and (stat.S_IMODE(before.st_mode) == 0o600 if private else not before.st_mode & 0o7022)
          and (expected_size is None or before.st_size == expected_size))
    _check(deadline)
    try:
        descriptor = os.open(name, _flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"), dir_fd=parent)
    except FileNotFoundError:
        raise PythonDependencyIndexError("metadata_untrusted") from None
    stack.callback(os.close, descriptor)
    pin = _FilePin(parent, name, descriptor, _stamp(before))
    pin.check()
    raw = _read_bytes(descriptor, before.st_size, deadline)
    _need(len(raw) == before.st_size)
    pin.check()
    return raw, pin


def _read_index(parent: int, name: str, supply: RootSupplyConfig, stack: ExitStack,
                deadline: float) -> tuple[PythonDependencyMetadataIndex, _FilePin]:
    raw, pin = _read_file(parent, name, _MAX_INDEX_BYTES, stack, deadline)
    try:
        value = PythonDependencyMetadataIndex.from_bytes(raw)
    except ValueError:
        raise PythonDependencyIndexError("metadata_untrusted") from None
    _check(deadline)
    try:
        value.require_catalog(supply.catalog, supply.digest)
    except ValueError:
        raise PythonDependencyIndexError("catalog_changed") from None
    pin.check()
    _check(deadline)
    return value, pin


def read_dependency_metadata_index(
    supply: RootSupplyConfig, *, deadline: float | None = None,
) -> PythonDependencyMetadataIndex:
    """Read only the fixed catalog-bound index under a shared directory lock."""
    try:
        deadline = _bounded_deadline(deadline, _READ_SECONDS)
        _native()
        configured = _supply(supply)
        digest = configured.digest
        name = "dependency-metadata-" + digest + ".json"
        with ExitStack() as stack:
            pins: list[_DirectoryPin] = []
            parent = _state(configured, stack, pins, exclusive=False, deadline=deadline)
            value, file_pin = _read_index(parent, name, configured, stack, deadline)
            _same_supply(supply, digest)
            _directories(pins, deadline)
            file_pin.check()
            return value
    except PythonDependencyIndexError:
        raise
    except FileNotFoundError:
        raise PythonDependencyIndexError("metadata_unavailable") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise PythonDependencyIndexError("metadata_untrusted") from None


def _discard_own_temp(parent: int, name: str, descriptor: int, identity: tuple[int, ...],
                      pins: list[_DirectoryPin]) -> None:
    # Cleanup does not depend on an expired work deadline, only proven ownership.
    _check_pins(pins)
    _private_state(parent)
    info = os.fstat(descriptor)
    _need(_stamp(info)[:2] == identity and stat.S_ISREG(info.st_mode)
          and info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o600
          and 0 <= info.st_nlink <= 2)
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    _need(_stamp(named) == _stamp(info) and info.st_nlink in {1, 2})
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)
    _check_pins(pins)


def _prepared_file(descriptor: int, prepared: tuple[int, ...], links: int) -> tuple[int, ...]:
    current = _stamp(os.fstat(descriptor))
    # link/unlink change ctime and nlink, but must not change bytes, mode or mtime.
    _need(current[:5] == prepared[:5] and current[6:8] == prepared[6:8] and current[5] == links)
    return current


def _publish(parent: int, name: str, value: PythonDependencyMetadataIndex,
             pins: list[_DirectoryPin], check: Callable[[], None], deadline: float) -> None:
    if sys.platform != "linux":
        raise PythonDependencyIndexError("unsupported")
    raw = value.to_bytes()
    _need(0 < len(raw) <= _MAX_INDEX_BYTES)
    temporary = ".dependency-metadata-" + secrets.token_hex(16) + ".tmp"
    owned: tuple[int, ...] | None = None
    with ExitStack() as stack:
        check()
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL
                             | _flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"),
                             0o600, dir_fd=parent)
        stack.callback(os.close, descriptor)
        try:
            created = os.fstat(descriptor)
            _need(stat.S_ISREG(created.st_mode) and created.st_uid == created.st_gid == 0
                  and created.st_nlink == 1 and created.st_dev == os.fstat(parent).st_dev)
            owned = _stamp(created)[:2]
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(raw)
            while remaining:
                _check(deadline)
                count = os.write(descriptor, remaining[:65536])
                _need(0 < count <= min(65536, len(remaining)))
                remaining = remaining[count:]
            os.fsync(descriptor)
            prepared = _stamp(os.fstat(descriptor))
            _need(prepared[:2] == owned and prepared[5] == 1 and prepared[6] == len(raw)
                  and prepared[3] == prepared[4] == 0 and stat.S_ISREG(prepared[2])
                  and stat.S_IMODE(prepared[2]) == 0o600)
            temp_pin = _FilePin(parent, temporary, descriptor, prepared)
            temp_pin.check()
            os.lseek(descriptor, 0, os.SEEK_SET)
            _need(_read_bytes(descriptor, len(raw), deadline) == raw)
            temp_pin.check()
            check()
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            linked = _prepared_file(descriptor, prepared, 2)
            _FilePin(parent, temporary, descriptor, linked).check()
            _FilePin(parent, name, descriptor, linked).check()
            check()
            os.fsync(descriptor)
            os.fsync(parent)
            _discard_own_temp(parent, temporary, descriptor, owned, pins)
            owned = None
            final = _prepared_file(descriptor, prepared, 1)
            _FilePin(parent, name, descriptor, final).check()
            os.fsync(descriptor)
            os.fsync(parent)
            check()
            _FilePin(parent, name, descriptor, final).check()
        except BaseException:
            if owned is not None:
                try:
                    _discard_own_temp(parent, temporary, descriptor, owned, pins)
                except (OSError, ValueError, TypeError, AttributeError):
                    raise PythonDependencyIndexError("metadata_untrusted") from None
            raise


def publish_dependency_metadata_index(
    root: RootConfig, supply: RootSupplyConfig, *, deadline: float | None = None,
) -> PythonDependencyMetadataIndex:
    """Explicit Root publisher: verify every configured local wheel, never fetch."""
    try:
        deadline = _bounded_deadline(deadline, _PUBLISH_SECONDS)
        _native()
        # Lazy import avoids extending the backend's initialization dependency graph.
        from agent_hub.skills.sandbox.python_project_backend import RootConfig

        _need(type(root) is RootConfig)
        configured_root = replace(root)
        configured = _supply(supply)
        digest = configured.digest
        _need(not configured.state_root.is_relative_to(configured_root.wheel_root)
              and not configured_root.wheel_root.is_relative_to(configured.state_root))
        name = "dependency-metadata-" + digest + ".json"
        with ExitStack() as stack:
            pins: list[_DirectoryPin] = []
            parent = _state(configured, stack, pins, exclusive=True, deadline=deadline)
            wheel_pins: list[_DirectoryPin] = []
            files: list[tuple[int, str, tuple[int, ...]]] = []

            def check() -> None:
                _same_supply(supply, digest)
                _need(root.to_payload() == configured_root.to_payload())
                _directories(pins + wheel_pins, deadline)
                for directory, filename, identity in files:
                    _check(deadline)
                    _need(_stamp(os.stat(filename, dir_fd=directory, follow_symlinks=False)) == identity)

            try:
                existing, file_pin = _read_index(parent, name, configured, stack, deadline)
            except FileNotFoundError:
                pass
            else:
                check()
                file_pin.check()
                return existing
            check()
            _need(sum(entry.size_bytes for entry in configured.catalog) <= configured_root.wheel_max_bytes)
            cache = _directory(configured_root.wheel_root, stack, wheel_pins)
            state_id, cache_id = _stamp(os.fstat(parent))[:2], _stamp(os.fstat(cache))[:2]
            _need(state_id not in {pin[3][:2] for pin in wheel_pins}
                  and cache_id not in {pin[3][:2] for pin in pins})
            records = []
            for entry in configured.catalog:
                check()
                with ExitStack() as wheel_stack:
                    raw, wheel_pin = _read_file(cache, entry.filename, entry.size_bytes, wheel_stack,
                        deadline, expected_size=entry.size_bytes, private=False)
                    _check(deadline)
                    records.append(extract_python_wheel_metadata(entry, raw, deadline=deadline))
                    _check(deadline)
                    wheel_pin.check()
                    files.append((cache, entry.filename, wheel_pin.identity))
            records.sort(key=lambda item: (item.wheel.name, item.wheel.version,
                                          item.wheel.filename, item.wheel.sha256))
            value = PythonDependencyMetadataIndex(digest, tuple(records))
            value.require_catalog(configured.catalog, digest)
            check()
            _publish(parent, name, value, pins, check, deadline)
            check()
            return value
    except PythonDependencyIndexError:
        raise
    except FileNotFoundError:
        raise PythonDependencyIndexError("metadata_unavailable") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise PythonDependencyIndexError("metadata_untrusted") from None


def main(argv: list[str] | None = None) -> int:
    """No caller-supplied paths, URLs, overrides or implicit publication."""
    try:
        arguments = sys.argv[1:] if argv is None else argv
        _need(type(arguments) is list and arguments == ["--publish"])
        _native()
        from agent_hub.skills.sandbox.python_input_supply_catalog import read_supply_configuration
        from agent_hub.skills.sandbox.python_project_backend import read_root_configuration

        value = publish_dependency_metadata_index(read_root_configuration(), read_supply_configuration())
        sys.stdout.write(json.dumps({"schema_version": 1, "catalog_sha256": value.catalog_sha256,
                                     "index_sha256": value.sha256}, sort_keys=True) + "\n")
        return 0
    except PythonDependencyIndexError as error:
        code = error.code
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as error:
        original = getattr(error, "code", "")
        code = ("unsupported" if original in {"python_project_unsupported", "input_supply_unsupported"}
                    else "metadata_unavailable" if isinstance(error, FileNotFoundError)
                    or original in {"python_project_runtime_missing", "input_supply_configuration_missing"}
                    else "metadata_untrusted")
    sys.stderr.write(code + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
