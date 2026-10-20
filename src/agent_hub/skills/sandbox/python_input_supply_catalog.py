"""Root-owned input catalog and measured plans; no fetch, execution or publication.

The caller stages the original contract and holds native admission. Root topology,
approval authority and unit lifetime remain caller responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, fields, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from agent_hub.capabilities.project_baselines import StoredProjectBaseline
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.scoped_read import ScopedReadError, _read_no_links
from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyArtifact,
    PythonInputSupplyPlan,
    PythonSupplyInterpreter,
    SupplyError,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectProbeInvocation,
    PythonProjectScope,
)
from agent_hub.skills.sandbox.python_project_runtime import (
    PythonDistributionError,
    observe_python_distribution,
)
from agent_hub.skills.sandbox.python_project_wheel_cache import (
    PythonWheelCacheError,
    observe_python_wheel_cache,
)

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.python_project_backend import RootConfig

_CONFIG_PATH = Path("/etc/agent-hub/python-input-supply.json")
_MAX_CONFIG = 512 * 1024
_MAX_CONTROLS = 32 * 1024**2
_MAX_SUPPLY_BYTES = 64 * 1024**2
_ENTRY_KEYS = frozenset(field.name for field in fields(WheelDownload))
_DirectoryPin = tuple[int, str, int, tuple[int, ...]]


class InputSupplyError(ValueError):
    def __init__(self, code: SupplyError) -> None:
        self.code = code
        super().__init__(code)


def _need(value: object, code: SupplyError = "input_supply_configuration_untrusted") -> None:
    if not value:
        raise InputSupplyError(code)


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _path(value: object) -> Path:
    _need(type(value) is str)
    text = cast(str, value)
    parsed = PurePosixPath(text)
    _need(parsed.is_absolute() and len(parsed.parts) > 1 and parsed.as_posix() == text
          and ".." not in parsed.parts and len(parsed.parts) <= 64
          and len(text.encode("utf-8")) <= 4096 and not any(c in text for c in "\\:")
          and all(ord(c) >= 32 and ord(c) != 127 for c in text))
    return Path(text)


def _download_payload(entry: WheelDownload) -> dict[str, object]:
    return {key: getattr(entry, key) for key in _ENTRY_KEYS}


@dataclass(frozen=True, slots=True)
class RootSupplyConfig:
    state_root: Path
    catalog: tuple[WheelDownload, ...]
    max_total_bytes: int
    min_free_bytes: int
    fetch_timeout_seconds: int

    def __post_init__(self) -> None:
        try:
            _need(isinstance(self.state_root, Path))
            _path(self.state_root.as_posix())
            _need(type(self.catalog) is tuple and 0 < len(self.catalog) <= 1000)
            for item in self.catalog:
                _need(type(item) is WheelDownload)
                replace(item)
            _need(len({(w.name, w.version) for w in self.catalog}) == len(self.catalog)
                  and len({w.filename for w in self.catalog}) == len(self.catalog))
            _need(type(self.max_total_bytes) is int and 0 < self.max_total_bytes <= _MAX_SUPPLY_BYTES
                  and type(self.min_free_bytes) is int and 0 <= self.min_free_bytes < 2**63
                  and type(self.fetch_timeout_seconds) is int and 0 < self.fetch_timeout_seconds <= 600)
            _need(len(_encoded(self.to_payload())) <= _MAX_CONFIG)
        except InputSupplyError:
            raise
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise InputSupplyError("input_supply_configuration_untrusted") from None

    @classmethod
    def from_payload(cls, value: object) -> RootSupplyConfig:
        try:
            _need(type(value) is dict)
            item = cast(dict[str, object], value)
            _need(set(item) == {"schema_version", "state_root", "catalog", "max_total_bytes",
                               "min_free_bytes", "fetch_timeout_seconds"}
                  and type(item["schema_version"]) is int and item["schema_version"] == 1)
            root = _path(item["state_root"])
            _need(type(item["catalog"]) is list and 0 < len(cast(list[object], item["catalog"])) <= 1000)
            entries: list[WheelDownload] = []
            for entry_value in cast(list[object], item["catalog"]):
                _need(type(entry_value) is dict)
                entry = cast(dict[str, object], entry_value)
                _need(set(entry) == _ENTRY_KEYS)
                entries.append(WheelDownload(
                    filename=cast(str, entry["filename"]), name=cast(str, entry["name"]),
                    version=cast(str, entry["version"]), sha256=cast(str, entry["sha256"]),
                    size_bytes=cast(int, entry["size_bytes"]), url=cast(str, entry["url"])))
            return cls(root, tuple(entries), cast(int, item["max_total_bytes"]),
                       cast(int, item["min_free_bytes"]), cast(int, item["fetch_timeout_seconds"]))
        except InputSupplyError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise InputSupplyError("input_supply_configuration_untrusted") from None

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, "state_root": self.state_root.as_posix(),
                "catalog": [_download_payload(w) for w in self.catalog],
                "max_total_bytes": self.max_total_bytes, "min_free_bytes": self.min_free_bytes,
                "fetch_timeout_seconds": self.fetch_timeout_seconds}

    @property
    def digest(self) -> str:
        return hashlib.sha256(_encoded(self.to_payload())).hexdigest()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise InputSupplyError("input_supply_configuration_untrusted")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _flags(*names: str) -> int:
    result = os.O_RDONLY
    for name in names:
        flag = getattr(os, name, None)
        _need(type(flag) is int and flag > 0, "input_supply_unsupported")
        result |= cast(int, flag)
    return result


def _directory(path: Path, stack: ExitStack, pins: list[_DirectoryPin]) -> int:
    flags = _flags("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    fd = os.open("/", flags)
    stack.callback(os.close, fd)
    for name in (".", *path.parts[1:]):
        child = os.open(name, flags, dir_fd=fd)
        stack.callback(os.close, child)
        info = os.fstat(child)
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
        pins.append((fd, name, child, _stamp(info)[:5]))
        fd = child
    return fd


def _check_pins(pins: list[_DirectoryPin]) -> None:
    for parent, name, child, identity in pins:
        before = os.fstat(child)
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _need(_stamp(before)[:5] == _stamp(after)[:5] == identity
              and before.st_uid == 0 and not before.st_mode & 0o022)


def read_supply_configuration(path: Path = _CONFIG_PATH) -> RootSupplyConfig:
    """Read a bounded Root600 control and verify its separate existing Root700 state."""
    _need(sys.platform == "linux" and os.name == "posix", "input_supply_unsupported")
    _need({os.open, os.stat} <= os.supports_dir_fd and os.stat in os.supports_follow_symlinks,
          "input_supply_unsupported")
    try:
        _need(isinstance(path, Path))
        _path(path.as_posix())
        with ExitStack() as stack:
            pins: list[_DirectoryPin] = []
            parent = _directory(path.parent, stack, pins)
            fd = os.open(path.name, _flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"),
                         dir_fd=parent)
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            _need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                  and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= _MAX_CONFIG)
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, min(65536, _MAX_CONFIG + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                _need(total <= _MAX_CONFIG)
            _need(total == before.st_size)
            raw = b"".join(chunks)
            config = RootSupplyConfig.from_payload(json.loads(
                raw, object_pairs_hook=_object, parse_constant=_constant))
            state = _directory(config.state_root, stack, pins)
            _need(stat.S_IMODE(os.fstat(state).st_mode) == 0o700)
            _need(_stamp(before) == _stamp(os.fstat(fd))
                  == _stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False)))
            _check_pins(pins)
            return config
    except InputSupplyError:
        raise
    except FileNotFoundError:
        raise InputSupplyError("input_supply_configuration_missing") from None
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise InputSupplyError("input_supply_configuration_untrusted") from None


@dataclass(frozen=True, slots=True)
class PlannedInputs:
    plan: PythonInputSupplyPlan
    missing_wheels: tuple[str, ...]
    downloads: tuple[WheelDownload, ...]


def _check(deadline: float) -> None:
    _need(type(deadline) in {int, float}, "input_supply_request_invalid")
    try:
        finite = math.isfinite(deadline)
    except OverflowError:
        finite = False
    _need(finite and deadline > 0, "input_supply_request_invalid")
    _need(time.monotonic() < deadline, "input_supply_timeout")


def plan_inputs(
    original: StoredProjectBaseline,
    invocation: PythonProjectProbeInvocation,
    configuration: RootConfig,
    supply: RootSupplyConfig,
    *,
    deadline: float,
) -> PlannedInputs:
    """Measure immutable controls/runtime and select exact catalog bytes under admission."""
    _check(deadline)
    try:
        _need(type(invocation) is PythonProjectProbeInvocation, "input_supply_request_invalid")
        invocation = PythonProjectProbeInvocation.model_validate(invocation.model_dump(), strict=True)
        _need(type(supply) is RootSupplyConfig, "input_supply_configuration_untrusted")
        supply = replace(supply)
        contract = original.contract
        _need((contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
              == (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id)
              and original.reference.to_payload() == invocation.original_reference.model_dump()
              and contract.input_source_sha256 == original.reference.input_source_sha256,
              "input_supply_scope_changed")
        controls: dict[str, bytes] = {}
        maximum = min(_MAX_CONTROLS, invocation.source_limit_bytes)
        for name in ("pyproject.toml", "requirements.lock"):
            _check(deadline)
            raw = _read_no_links(original.snapshot, (name,), max_bytes=maximum)
            _need(0 < len(raw) <= maximum, "input_supply_quota")
            _need(contract.protected_files.get(name) in {
                hashlib.sha256(tag + raw).hexdigest() for tag in (b"f\0", b"x\0")},
                "input_supply_scope_changed")
            controls[name] = raw
        _check(deadline)
        _need(sum(len(raw) for raw in controls.values()) <= maximum, "input_supply_quota")
        declaration = parse_python_project_declaration(controls["pyproject.toml"],
            controls["requirements.lock"], max_control_bytes=maximum, max_requirements=1000)
        runtime = observe_python_distribution(configuration.runtime_root,
            interpreter_relative="bin/python3.12", limits=ScanLimits(configuration.runtime_max_bytes),
            deadline=deadline)
        _check(deadline)
        _need(runtime.binary_sha256 == configuration.interpreter.binary_sha256
              and runtime.content_sha256 == configuration.interpreter.base_runtime_sha256,
              "input_supply_unavailable")
        indexed = {(w.name, w.version): w for w in supply.catalog}
        selected: list[WheelDownload] = []
        for requirement in declaration.requirements:
            _check(deadline)
            entry = indexed.get((requirement.name, requirement.version))
            _need(entry is not None, "input_supply_artifact_missing")
            assert entry is not None
            _need(entry.sha256 in requirement.hashes, "input_supply_artifact_integrity")
            selected.append(entry)
        _need(sum(w.size_bytes for w in selected)
              <= min(supply.max_total_bytes, configuration.wheel_max_bytes), "input_supply_quota")
        missing: list[WheelDownload] = []
        for requirement, entry in zip(declaration.requirements, selected, strict=True):
            _check(deadline)
            try:
                observed = observe_python_wheel_cache(configuration.wheel_root, (requirement,),
                    limits=ScanLimits(configuration.wheel_max_bytes), deadline=deadline)
            except PythonWheelCacheError as error:
                if error.code == "wheel_cache_wheel_missing":
                    missing.append(entry)
                    continue
                if error.code == "wheel_cache_timeout":
                    raise InputSupplyError("input_supply_timeout") from None
                raise InputSupplyError("input_supply_artifact_integrity") from None
            _need(len(observed) == 1 and observed[0].to_payload() == {
                "filename": entry.filename, "name": entry.name, "version": entry.version,
                "sha256": entry.sha256}, "input_supply_artifact_integrity")
        _check(deadline)
        plan = PythonInputSupplyPlan(
            scope=PythonProjectScope.model_validate({name: getattr(invocation, name) for name in (
                "tenant_id", "run_id", "project_id", "session_id", "original_reference")}, strict=True),
            declaration_sha256=declaration.sha256, manifest_sha256=declaration.manifest_sha256,
            lock_sha256=declaration.lock_sha256, catalog_sha256=supply.digest,
            interpreter=PythonSupplyInterpreter.model_validate(configuration.interpreter.to_payload(), strict=True),
            artifacts=tuple(PythonInputSupplyArtifact(filename=w.filename, name=w.name, version=w.version,
                sha256=w.sha256, size_bytes=w.size_bytes) for w in selected))
        plan.require_scope(invocation)
        _check(deadline)
        return PlannedInputs(plan, tuple(sorted(w.filename for w in missing)), tuple(missing))
    except InputSupplyError:
        raise
    except ScopedReadError:
        raise InputSupplyError("input_supply_scope_changed") from None
    except PythonDistributionError:
        _check(deadline)
        raise InputSupplyError("input_supply_unavailable") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        _check(deadline)
        raise InputSupplyError("input_supply_request_invalid") from None
