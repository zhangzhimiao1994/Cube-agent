"""Synchronous v4 worker for a single-threaded privileged helper, never the RPC loop.

Root configuration pins data, not installation authority. The caller authenticates
and authorizes the stored recipe, owns execution slots/duplicate fences and stage
lifetime, and preserves UNKNOWN on uncertain cleanup. Only fixed systemd/bwrap
launchers execute project code. This worker never deletes a stage or runs GC.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Never, cast

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.python_project_input_store import _capture
from agent_hub.capabilities.python_project_inputs import (
    PythonInterpreterIdentity,
    PythonProjectInputs,
)
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy, load_broker_policy
from agent_hub.skills.sandbox.project import (
    ProjectExecutionBroker,
    ProjectInvocation,
    _Execution,
    snapshot_project_source,
)
from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _object,
    _Observed,
    _open_directory,
    _read_control,
    _root,
    _seal_readonly,
    _stamp,
    _write_all,
    stage_original_contract,
)
from agent_hub.skills.sandbox.python_project_cache import (
    _CONTROL_ALLOWANCE,
    PythonProjectCacheError,
    PythonProjectCacheLease,
    PythonProjectCachePolicy,
    PythonProjectEnvironmentCache,
)
from agent_hub.skills.sandbox.python_project_command import build_python_project_systemd_command
from agent_hub.skills.sandbox.python_project_copy import (
    PythonProjectCopyError,
    _directory_flags,
    _observe,
    _recipe_entries,
    _same,
    _snapshot,
)
from agent_hub.skills.sandbox.python_project_process import (
    PythonPhaseCleanupError,
    PythonPhaseProcessResult,
    python_native_report_limit,
    python_phase_capture_limit,
    run_python_phase,
)
from agent_hub.skills.sandbox.python_project_product_export import (
    open_python_product as open_python_product,  # noqa: PLC0414 - public broker entry point.
)
from agent_hub.skills.sandbox.python_project_product_export import (
    retain_python_product,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PYTHON_PROJECT_NATIVE_UNCERTAIN_EXIT_CODE,
    PythonEnvironmentReference,
    PythonProjectBrokerError,
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInputsObservation,
    PythonProjectInspectEnvironmentRequest,
    PythonProjectProbeRequest,
    PythonProjectReleaseEnvironmentRequest,
    PythonProjectScope,
    base_python_invocation,
)
from agent_hub.skills.sandbox.python_project_runner import (
    PythonProjectRunnerPlan,
    python_phase_detail,
)
from agent_hub.skills.sandbox.python_project_runtime import (
    PythonDistributionError,
    observe_python_distribution,
)
from agent_hub.skills.sandbox.python_project_staging import (
    StagedPythonProjectInputs,
    stage_python_project_inputs,
)
from agent_hub.skills.sandbox.python_project_wheel_cache import (
    PythonWheelCacheError,
    observe_python_wheel_cache,
)
from agent_hub.skills.sandbox.python_project_wheel_stage import (
    PythonWheelStageError,
    stage_python_project_wheels,
    verify_staged_python_wheels,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_Request = (
    PythonProjectBrokerRequest
    | PythonProjectProbeRequest
    | PythonProjectInspectEnvironmentRequest
    | PythonProjectReleaseEnvironmentRequest
)
_CONFIG_PATH = Path("/etc/agent-hub/python-project.json")
_MAX_CONTROL = 16 * 1024
_MAX_REQUEST = 64 * 1024
_MAX_RECIPE_CONTROL = 32 * 1024**2
# check_contract.json, python-inputs.json and plan.json, plus small broker controls.
_STAGE_CONTROL_ALLOWANCE = 3 * _MAX_RECIPE_CONTROL + _CONTROL_ALLOWANCE
_DiagnosticStage = Literal[
    "native", "arguments", "policy", "request", "configuration", "execution", "stage_validation"
]


class PythonProjectBackendError(ValueError):
    """Finite protocol error; never underlying host paths, controls or tracebacks."""

    def __init__(self, code: PythonProjectBrokerError) -> None:
        self.code = code
        super().__init__(code)


def _diagnostic(stage: _DiagnosticStage, error: BaseException) -> None:
    """Private helper stderr only; callers must not relay it as model output.

    Fixed class names avoid arbitrary exception names/messages carrying secrets.
    No traceback, request bytes, exception text or host path is retained.
    """
    kinds = (
        PythonProjectBackendError,
        PythonProjectCacheError,
        ProcessCleanupIncomplete,
        PythonPhaseCleanupError,
        FileNotFoundError,
        PermissionError,
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        AssertionError,
        SystemExit,
    )
    kind = next((item.__name__ for item in kinds if isinstance(error, item)), "Exception")
    sys.stderr.write(
        json.dumps({"python_project_diagnostic": {"stage": stage, "exception_type": kind}}) + "\n"
    )


def _need(
    value: object, code: PythonProjectBrokerError = "python_project_execution_unavailable"
) -> None:
    if not value:
        raise PythonProjectBackendError(code)


def _check(deadline: float) -> None:
    _need(time.monotonic() < deadline)


def _path(value: object) -> Path:
    _need(type(value) is str, "python_project_runtime_untrusted")
    assert isinstance(value, str)
    parsed = PurePosixPath(value)
    _need(
        parsed.is_absolute()
        and len(parsed.parts) > 1
        and parsed.as_posix() == value
        and ".." not in parsed.parts
        and len(parsed.parts) <= 64
        and len(value.encode("utf-8")) <= 4096
        and ":" not in value
        and "\\" not in value
        and all(ord(char) >= 32 and ord(char) != 127 for char in value),
        "python_project_runtime_untrusted",
    )
    return Path(value)


@dataclass(frozen=True, slots=True)
class RootConfig:
    runtime_root: Path
    interpreter: PythonInterpreterIdentity
    wheel_root: Path
    environment_root: Path
    stage_root: Path
    cache_policy: PythonProjectCachePolicy
    runtime_max_bytes: int
    wheel_max_bytes: int
    max_environment_bytes: int

    def __post_init__(self) -> None:
        roots = (self.runtime_root, self.wheel_root, self.environment_root, self.stage_root)
        for root in roots:
            _need(isinstance(root, Path), "python_project_runtime_untrusted")
            _path(root.as_posix())
        _need(
            all(
                not left.is_relative_to(right) for left in roots for right in roots if left != right
            )
            and len(set(roots)) == 4,
            "python_project_runtime_untrusted",
        )
        _need(
            type(self.interpreter) is PythonInterpreterIdentity
            and type(self.cache_policy) is PythonProjectCachePolicy,
            "python_project_runtime_untrusted",
        )
        PythonInterpreterIdentity.from_payload(self.interpreter.to_payload())
        for value in (self.runtime_max_bytes, self.wheel_max_bytes, self.max_environment_bytes):
            _need(type(value) is int and 0 < value < 2**63, "python_project_runtime_untrusted")
        _need(
            self.max_environment_bytes + _CONTROL_ALLOWANCE <= self.cache_policy.max_bytes,
            "python_project_runtime_untrusted",
        )

    @classmethod
    def from_payload(cls, value: object) -> RootConfig:
        try:
            _need(type(value) is dict, "python_project_runtime_untrusted")
            assert isinstance(value, dict)
            _need(
                set(value)
                == {
                    "schema_version",
                    "runtime_root",
                    "interpreter",
                    "wheel_root",
                    "environment_root",
                    "stage_root",
                    "cache_policy",
                    "runtime_max_bytes",
                    "wheel_max_bytes",
                    "max_environment_bytes",
                }
                and type(value["schema_version"]) is int
                and value["schema_version"] == 1,
                "python_project_runtime_untrusted",
            )
            policy = value["cache_policy"]
            _need(
                type(policy) is dict
                and set(policy)
                == {"max_bytes", "max_entries", "disk_reserve_bytes", "retention_seconds"},
                "python_project_runtime_untrusted",
            )
            return cls(
                _path(value["runtime_root"]),
                PythonInterpreterIdentity.from_payload(value["interpreter"]),
                _path(value["wheel_root"]),
                _path(value["environment_root"]),
                _path(value["stage_root"]),
                PythonProjectCachePolicy(**policy),
                value["runtime_max_bytes"],
                value["wheel_max_bytes"],
                value["max_environment_bytes"],
            )
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            raise PythonProjectBackendError("python_project_runtime_untrusted") from None

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "runtime_root": self.runtime_root.as_posix(),
            "interpreter": self.interpreter.to_payload(),
            "wheel_root": self.wheel_root.as_posix(),
            "environment_root": self.environment_root.as_posix(),
            "stage_root": self.stage_root.as_posix(),
            "cache_policy": {
                "max_bytes": self.cache_policy.max_bytes,
                "max_entries": self.cache_policy.max_entries,
                "disk_reserve_bytes": self.cache_policy.disk_reserve_bytes,
                "retention_seconds": self.cache_policy.retention_seconds,
            },
            "runtime_max_bytes": self.runtime_max_bytes,
            "wheel_max_bytes": self.wheel_max_bytes,
            "max_environment_bytes": self.max_environment_bytes,
        }


def _root_native() -> None:
    if sys.platform != "linux":
        raise PythonProjectBackendError("python_project_unsupported")
    _need(os.geteuid() == 0 and os.getegid() == 0, "python_project_unsupported")


def _native() -> None:
    _root_native()
    _need(
        threading.active_count() == 1 and threading.current_thread() is threading.main_thread(),
        "python_project_unsupported",
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise PythonProjectBackendError("python_project_unsupported")


def load_root_configuration() -> RootConfig:
    """Single-threaded worker loader; kept separate from the parent metadata API."""
    _native()
    return read_root_configuration()


def read_root_configuration() -> RootConfig:
    """Read fixed root-owned bounded metadata, with no fork or environment access.

    A privileged async parent may use this to confine its stage/helper paths.
    This does not relax execute_python_project_request's single-thread/loop guard.
    """
    _root_native()
    try:
        with ExitStack() as stack:
            pins: list[_Observed] = []
            parent = _root(_CONFIG_PATH.parent, 0, stack, pins)
            info = os.stat(_CONFIG_PATH.name, dir_fd=parent, follow_symlinks=False)
            _need(0 < info.st_size <= _MAX_CONTROL, "python_project_runtime_untrusted")
            raw = _read_control(parent, _CONFIG_PATH.name, 0, pins)
            value: object = json.loads(raw, object_pairs_hook=_object)
            configured = RootConfig.from_payload(value)
            for pin in pins:
                pin.check()
            return configured
    except FileNotFoundError as error:
        _diagnostic("configuration", error)
        raise PythonProjectBackendError("python_project_runtime_missing") from None
    except (OSError, ValueError, TypeError, RuntimeError, OverflowError) as error:
        _diagnostic("configuration", error)
        raise PythonProjectBackendError("python_project_runtime_untrusted") from None


@dataclass(frozen=True, slots=True)
class _Roots:
    stage_root_fd: int
    stage_fd: int
    cache_fd: int | None
    pins: list[_Observed]


@contextmanager
def _roots(
    policy: BrokerPolicy, config: RootConfig, stage: Path, needs_cache: bool
) -> Iterator[_Roots]:
    _need(
        type(policy) is BrokerPolicy
        and type(policy.allowed_uid) is int
        and 0 < policy.allowed_uid < 2**32 - 1
        and type(policy.allowed_gid) is int
        and 0 < policy.allowed_gid < 2**32 - 1,
        "python_project_request_invalid",
    )
    _need(
        policy.systemd_run == "/usr/bin/systemd-run"
        and policy.systemctl == "/usr/bin/systemctl"
        and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", policy.service_group) is not None,
        "python_project_unsupported",
    )
    _need(
        isinstance(stage, Path)
        and stage.parent == config.stage_root
        and config.stage_root == policy.python_project_runtime_root,
        "python_project_request_invalid",
    )
    configured = (config.runtime_root, config.wheel_root, config.environment_root)
    _need(
        all(
            not left.is_relative_to(right) and not right.is_relative_to(left)
            for left in configured
            for right in (stage, policy.project_workspace_root, policy.source_path)
        ),
        "python_project_runtime_untrusted",
    )
    with ExitStack() as stack:
        pins: list[_Observed] = []
        host_stage = _root(config.stage_root, 0, stack, pins, stage=True)
        _host_disk(host_stage)
        target = _root(stage, 0, stack, pins, stage=True)
        seen = {(os.fstat(target).st_dev, os.fstat(target).st_ino)}
        cache_fd = None
        for path, code, private in (
            (config.runtime_root, "python_project_runtime_missing", False),
            (config.wheel_root, "python_project_wheel_cache_missing", False),
            *(
                ([(config.environment_root, "python_project_environment_unavailable", True)])
                if needs_cache
                else []
            ),
        ):
            try:
                descriptor = _root(path, 0, stack, pins, stage=private)
            except FileNotFoundError:
                raise PythonProjectBackendError(code) from None  # type: ignore[arg-type]
            identity = (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
            _need(identity not in seen, "python_project_runtime_untrusted")
            seen.add(identity)
            if private:
                _host_disk(descriptor)
                cache_fd = descriptor
        yield _Roots(host_stage, target, cache_fd, pins)
        for pin in pins:
            pin.check()


@contextmanager
def _admission(parent: int, deadline: float) -> Iterator[None]:
    """Serialize cooperating workers before heavy IO; not a filesystem reservation.

    Held through staging, observers, the unit, quiescence and publication. Forked
    observers inherit the open description; their roothelper cgroup must drain.
    The root broker owns stage cleanup after helper/launcher drain, not this lock.
    """
    _root_native()
    if sys.platform != "linux":
        raise PythonProjectBackendError("python_project_unsupported")
    import fcntl

    _check(deadline)
    root = os.fstat(parent)
    _need(stat.S_ISDIR(root.st_mode) and root.st_uid == 0 and stat.S_IMODE(root.st_mode) == 0o700)
    with ExitStack() as stack:
        fd = os.open(
            "active.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        _need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and info.st_size == 0
        )
        pin = _Observed(parent, "active.lock", _stamp(info, full=True), True, fd)
        pin.check()
        while True:
            _check(deadline)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        # Closing the last inherited description releases flock. Do not LOCK_UN
        # early while a forked observer may still hold this admission authority.
        pin.check()
        _check(deadline)
        yield
        pin.check()


def _measured_inputs(
    original: StoredProjectBaseline, invocation: Any, config: RootConfig, deadline: float
) -> PythonProjectInputs:
    contract = original.contract
    _need(
        (contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
        == (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id)
        and original.reference.to_payload() == invocation.original_reference.model_dump(),
        "python_project_recipe_unavailable",
    )
    maximum = min(_MAX_RECIPE_CONTROL, invocation.source_limit_bytes)
    controls = {
        name: _read_no_links(original.snapshot, (name,), max_bytes=maximum)
        for name in ("pyproject.toml", "requirements.lock")
    }
    declaration = parse_python_project_declaration(
        controls["pyproject.toml"],
        controls["requirements.lock"],
        max_control_bytes=maximum,
        max_requirements=1000,
    )
    try:
        runtime = observe_python_distribution(
            config.runtime_root,
            interpreter_relative="bin/python3.12",
            limits=ScanLimits(config.runtime_max_bytes),
            deadline=deadline,
        )
    except PythonDistributionError:
        raise PythonProjectBackendError("python_project_runtime_untrusted") from None
    _need(
        runtime.binary_sha256 == config.interpreter.binary_sha256
        and runtime.content_sha256 == config.interpreter.base_runtime_sha256,
        "python_project_runtime_untrusted",
    )
    try:
        wheels = observe_python_wheel_cache(
            config.wheel_root,
            declaration.requirements,
            limits=ScanLimits(config.wheel_max_bytes),
            deadline=deadline,
        )
    except PythonWheelCacheError as error:
        if error.code == "wheel_cache_wheel_missing":
            raise PythonProjectBackendError("python_project_wheel_missing") from None
        raise PythonProjectBackendError("python_project_wheel_integrity") from None
    result = _capture(
        original,
        interpreter=config.interpreter,
        wheels=wheels,
        max_control_bytes=maximum,
        max_requirements=1000,
    )
    _need(result.declaration == declaration, "python_project_recipe_unavailable")
    _check(deadline)
    return result


def _source_files(root: Path, invocation: Any, deadline: float) -> dict[str, str]:
    """Independent root-owned sealed snapshot measurement with depth-scoped FDs."""
    if sys.platform != "linux":
        raise PythonProjectBackendError("python_project_unsupported")
    files: dict[str, str] = {}
    total = visited = 0
    with ExitStack() as stack:
        pins: list[_Observed] = []
        descriptor = _root(root, 0, stack, pins)

        def walk(fd: int, prefix: str, depth: int) -> None:
            nonlocal total, visited
            _check(deadline)
            before = os.fstat(fd)
            _need(stat.S_IMODE(before.st_mode) == 0o555 and depth <= 64)
            with os.scandir(fd) as members:
                for member in members:
                    _check(deadline)
                    visited += 1
                    path = prefix + member.name
                    _need(visited <= invocation.file_limit * 10 and len(os.fsencode(path)) <= 4096)
                    info = os.stat(member.name, dir_fd=fd, follow_symlinks=False)
                    directory = stat.S_ISDIR(info.st_mode)
                    _need(
                        info.st_uid == 0
                        and (directory or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                    )
                    flags = (
                        _directory_flags()
                        if directory
                        else (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                    )
                    child = os.open(member.name, flags, dir_fd=fd)
                    try:
                        _same(fd, member.name, info, child)
                        if directory:
                            walk(child, path + "/", depth + 1)
                        else:
                            _need(
                                len(files) < invocation.file_limit
                                and stat.S_IMODE(info.st_mode) in {0o444, 0o555}
                            )
                            _need(info.st_size <= invocation.source_limit_bytes - total)
                            digest = hashlib.sha256(b"x\0" if info.st_mode & 0o111 else b"f\0")
                            count = 0
                            while True:
                                _check(deadline)
                                chunk = os.read(child, min(1024**2, info.st_size - count + 1))
                                if not chunk:
                                    break
                                count += len(chunk)
                                total += len(chunk)
                                _need(
                                    count <= info.st_size and total <= invocation.source_limit_bytes
                                )
                                digest.update(chunk)
                            _need(count == info.st_size)
                            files[path] = digest.hexdigest()
                        _same(fd, member.name, info, child)
                    finally:
                        os.close(child)
            _need(_stamp(os.fstat(fd), full=True) == _stamp(before, full=True))

        walk(descriptor, "", 0)
        for pin in pins:
            pin.check()
    return files


def _source_snapshot(
    prepared: StagedPythonProjectInputs,
    invocation: Any,
    policy: BrokerPolicy,
    stage: Path,
    deadline: float,
) -> str:
    if invocation.source_kind == "original":
        digest = prepared.original.reference.input_source_sha256
        snapshot = prepared.original.snapshot
    else:
        snapshot = stage / "snapshot"
        manifest: dict[str, str] = {}
        digest = asyncio.run(
            snapshot_project_source(
                policy.project_workspace_root,
                base_python_invocation(invocation),
                destination=snapshot,
                _manifest=manifest,
            )
        )
        prepared.original.contract.require_current(manifest)
    _recheck_source(prepared, invocation, snapshot, digest, deadline)
    return digest


def _recheck_source(
    prepared: StagedPythonProjectInputs,
    invocation: Any,
    snapshot: Path,
    digest: str,
    deadline: float,
) -> None:
    original = _source_files(prepared.original.snapshot, invocation, deadline)
    _need(original == prepared.original.source_files, "python_project_recipe_unavailable")
    current = (
        original
        if snapshot == prepared.original.snapshot
        else _source_files(snapshot, invocation, deadline)
    )
    prepared.original.contract.require_current(current)
    _need(
        hashlib.sha256(_encoded(current)).hexdigest() == digest, "python_project_recipe_unavailable"
    )


def _report(
    raw: str,
    invocation: Any,
    inputs: PythonProjectInputs,
    digest: str,
    reused: PythonEnvironmentReference | None,
    *, generated_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        _need(len(raw.encode("utf-8")) <= python_native_report_limit(invocation.output_limit_bytes))
        payload: object = json.loads(raw, object_pairs_hook=_object)
        _need(type(payload) is dict)
        assert isinstance(payload, dict)
        extra = {"generated_test_execution"} if "generated_test_execution" in payload else set()
        _need(
            set(payload)
            == {
                "schema_version",
                "kind",
                "evidence",
                "status",
                "execution_id",
                "source_sha256",
                "recipe_sha256",
                "environment_input_sha256",
                "environment_sha256",
                "original_check_contract",
                "runtime",
                "checks",
                "wheel",
                "test_count",
                "exported_environment",
                "dependency_install",
            } | extra
        )
        _need(
            type(payload["schema_version"]) is int
            and payload["schema_version"] == 2
            and payload["kind"] == "python_project"
            and payload["evidence"] == "native_process_only"
            and payload["execution_id"] == invocation.execution_id
            and payload["source_sha256"] == digest
            and payload["recipe_sha256"] == inputs.sha256
            and payload["environment_input_sha256"] == inputs.environment_input_sha256
            and payload["original_check_contract"] == inputs.contract.protected_sha256
            and payload["runtime"] == inputs.interpreter.to_payload()
        )
        checks = payload["checks"]
        _need(type(checks) is list and 1 <= len(checks) <= 3)
        phases = ("install", "build", "test")
        details: dict[str, dict[str, Any] | None] = {}
        maximum = python_phase_capture_limit(invocation.output_limit_bytes)
        for index, check in enumerate(checks):
            _need(
                type(check) is dict
                and set(check)
                == {
                    "phase",
                    "status",
                    "exit_code",
                    "timed_out",
                    "stdout",
                    "stderr",
                    "stdout_truncated",
                    "stderr_truncated",
                    "stdout_observed_bytes",
                    "stderr_observed_bytes",
                }
                and check["phase"] == phases[index]
            )
            _need(
                check["status"] in {"passed", "failed"}
                and (check["exit_code"] is None or type(check["exit_code"]) is int)
            )
            for field in ("timed_out", "stdout_truncated", "stderr_truncated"):
                _need(type(check[field]) is bool)
            for field in ("stdout", "stderr"):
                _need(type(check[field]) is str and len(check[field].encode("utf-8")) <= maximum)
                size = check[field + "_observed_bytes"]
                _need(type(size) is int and len(check[field].encode("utf-8")) <= size < 2**63)
            phase = "check_environment" if index == 0 and reused is not None else phases[index]
            observed = PythonPhaseProcessResult(
                check["exit_code"],
                check["timed_out"],
                check["stdout"],
                check["stderr"],
                check["stdout_truncated"],
                check["stderr_truncated"],
                check["stdout_observed_bytes"],
                check["stderr_observed_bytes"],
            )
            detail = python_phase_detail(
                observed, phase=phase, import_module=inputs.declaration.import_module
            )
            _need((detail is not None) == (check["status"] == "passed"))
            _need(index == len(checks) - 1 or detail is not None)
            details[phases[index]] = detail
        passed = len(checks) == 3 and all(check["status"] == "passed" for check in checks)
        _need(payload["status"] == ("passed" if passed else "failed"))
        installed = details["install"] is not None
        sha = payload["environment_sha256"]
        _need(
            (type(sha) is str and re.fullmatch(r"[a-f0-9]{64}", sha) is not None)
            if installed
            else sha is None
        )
        if reused is not None and installed:
            _need(sha == reused.environment_sha256, "python_project_environment_changed")
        _need(
            type(payload["exported_environment"]) is bool
            and payload["exported_environment"] == (installed and reused is None)
        )
        _need(
            payload["dependency_install"]
            == {
                "mode": "reused" if reused is not None else "created",
                "origin_execution_id": reused.origin_execution_id
                if reused is not None
                else invocation.execution_id,
            }
        )
        build = details.get("build")
        test = details.get("test")
        _need(payload["wheel"] == (build["wheel"] if build else None))
        _need(
            (type(payload["test_count"]) is int and payload["test_count"] == test["test_count"])
            if test
            else payload["test_count"] is None
        )
        proof = test.get("generated_test_execution") if test is not None else None
        _need(bool(extra) == (proof is not None))
        if proof is not None:
            from agent_hub.skills.sandbox.python_project_phase import (
                validate_generated_python_test_execution,
            )

            validated = validate_generated_python_test_execution(payload["generated_test_execution"])
            _need(passed and generated_manifest_sha256 is not None
                  and validated == validate_generated_python_test_execution(proof)
                  and validated["manifest_sha256"] == generated_manifest_sha256)
        elif passed:
            _need(generated_manifest_sha256 is None)
        return payload
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise PythonProjectBackendError("python_project_execution_unavailable") from None


def _mount_type(fdinfo: bytes, mountinfo: bytes) -> str:
    """Kernel mount identity from the actual FD, never a guessed '/' filesystem."""
    _need(len(fdinfo) <= 4096 and len(mountinfo) <= 1024**2, "python_project_environment_quota")
    ids = [
        line.partition(b":")[2].strip()
        for line in fdinfo.splitlines()
        if line.startswith(b"mnt_id:")
    ]
    _need(len(ids) == 1 and ids[0].isdigit(), "python_project_environment_quota")
    matches = [
        line.partition(b" - ")[2].split()
        for line in mountinfo.splitlines()
        if line.partition(b" ")[0] == ids[0]
    ]
    _need(len(matches) == 1 and len(matches[0]) >= 3, "python_project_environment_quota")
    filesystem = matches[0][0].decode("ascii")
    _need(
        filesystem
        not in {
            "tmpfs",
            "ramfs",
            "devtmpfs",
            "hugetlbfs",
            "proc",
            "sysfs",
            "cgroup",
            "cgroup2",
            "mqueue",
            "fusectl",
        },
        "python_project_environment_quota",
    )
    return filesystem


def _host_disk(fd: int) -> None:
    with Path(f"/proc/self/fdinfo/{fd}").open("rb") as info:
        fdinfo = info.read(4097)
    with Path("/proc/self/mountinfo").open("rb") as info:
        mountinfo = info.read(1024**2 + 1)
    _mount_type(fdinfo, mountinfo)


def _disk_budget(
    config: RootConfig, roots: _Roots, budget: int, copying: bool, *, source_bytes: int = 0,
    product_bytes: int = 0,
) -> None:
    # Native FD authority is established by _roots; this is only bounded admission
    # arithmetic on the actual filesystems. External writers remain outside it.
    statvfs = getattr(os, "fstatvfs", None)
    _need(callable(statvfs), "python_project_unsupported")
    assert callable(statvfs)
    _need(not copying or roots.cache_fd is not None, "python_project_environment_unavailable")
    reserve = config.cache_policy.disk_reserve_bytes
    _need(type(product_bytes) is int and product_bytes >= 0, "python_project_environment_quota")
    stage_required = (source_bytes + product_bytes + _STAGE_CONTROL_ALLOWANCE
                      + (budget if copying else 0))
    requirements = [(roots.stage_fd, stage_required)]
    if roots.cache_fd is not None:
        if os.fstat(roots.stage_fd).st_dev == os.fstat(roots.cache_fd).st_dev and copying:
            requirements[0] = (roots.stage_fd, stage_required + budget)
        requirements.append((roots.cache_fd, _CONTROL_ALLOWANCE + (budget if copying else 0)))
    for fd, required in requirements:
        disk = statvfs(fd)
        _need(
            disk.f_bavail * disk.f_frsize >= required + reserve, "python_project_environment_quota"
        )


def _write_plan(plan: PythonProjectRunnerPlan, parent: int, stage: Path) -> Path:
    if sys.platform != "linux":
        raise PythonProjectBackendError("python_project_unsupported")
    raw = plan.model_dump_json().encode("utf-8")
    _need(len(raw) <= _MAX_RECIPE_CONTROL, "python_project_recipe_unavailable")
    fd = os.open(
        "plan.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    try:
        _write_all(fd, raw)
        _seal_readonly(fd, 0o444)
    finally:
        os.close(fd)
    os.fsync(parent)
    return stage / "plan.json"


def _make_export(parent: int, stage: Path) -> Path:
    if sys.platform != "linux":
        raise PythonProjectBackendError("python_project_unsupported")
    os.mkdir("export", 0o700, dir_fd=parent)
    fd = os.open("export", _directory_flags(), dir_fd=parent)
    try:
        _need(os.fstat(fd).st_uid == 0)
        os.fchmod(fd, 0o1777)
        _same(parent, "export", os.fstat(fd), fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)
    return stage / "export"


def _export_empty(parent: int, deadline: float, product: bool = False) -> None:
    with ExitStack() as stack:
        pins: list[_Observed] = []
        fd = _open_directory(parent, "export", 0, stack, pins, mode=0o1777)
        _check(deadline)
        with os.scandir(fd) as entries:
            names = [entry.name for _, entry in zip(range(2), entries, strict=False)]
            _need(names == (["product"] if product else []), "python_project_environment_changed")
        for pin in pins:
            pin.check()


def _minimum_environment_bytes(config: RootConfig, deadline: float) -> int:
    """Trusted lower bound: the environment requires three physical interpreter files."""
    raw = _read_no_links(config.runtime_root, ("bin", "python3.12"), max_bytes=config.runtime_max_bytes)
    _need(bool(raw) and hashlib.sha256(raw).hexdigest() == config.interpreter.binary_sha256,
        "python_project_runtime_untrusted")
    _check(deadline)
    return 3 * len(raw)


def _publish_export(
    lease: PythonProjectCacheLease,
    parent: int,
    export: Path,
    inputs: PythonProjectInputs,
    config: RootConfig,
    budget: int,
    reported_sha: str,
    deadline: float,
    product: bool = False,
    *, execution: _Execution | None = None,
) -> PythonEnvironmentReference:
    """Only called AFTER manager/cgroup quiescence, never from runner evidence alone."""
    with ExitStack() as stack:
        pins: list[_Observed] = []
        exported = _open_directory(parent, "export", 0, stack, pins, mode=0o1777)
        with os.scandir(exported) as members:
            _need(
                sorted(entry.name for _, entry in zip(range(3), members, strict=False))
                == (["product", "ready"] if product else ["ready"]),
                "python_project_environment_changed",
            )
        ready_info = os.stat("ready", dir_fd=exported, follow_symlinks=False)
        owner = (
            ready_info.st_uid
        )  # Physical UID from the broker-private export, never an RPC field.
        ready = _open_directory(exported, "ready", owner, stack, pins, mode=0o700)
        with os.scandir(ready) as members:
            _need(
                [entry.name for _, entry in zip(range(2), members, strict=False)]
                == ["environment"],
                "python_project_environment_changed",
            )
        environment = _open_directory(ready, "environment", owner, stack, pins, mode=0o555)
        _need(lease.limits.max_bytes <= budget, "python_project_environment_quota")
        try:
            _snapshot(environment, owner, lease.limits, deadline)
            actual = _observe(export / "ready/environment", inputs.environment_input_sha256,
                lease.limits, deadline)
        except PythonProjectCopyError as error:
            if error.reason != "copy_limit":
                raise
            # No cache copy has started. Preserve a finite consumed outcome only
            # after actual unit quiescence; never reinterpret it as install failure.
            for pin in pins:
                pin.check()
            _need(execution is not None and execution.quiet is True)
            assert execution is not None
            lease.record_unpublished_capacity(execution=execution, deadline=deadline)
            raise PythonProjectBackendError("python_project_environment_quota") from None
        _recipe_entries(actual.security_manifest, inputs.interpreter.binary_sha256)
        _need(actual.environment_sha256 == reported_sha, "python_project_environment_changed")
        for pin in pins:
            pin.check()
        result = lease.publish(
            export / "ready/environment",
            source_owner=owner,
            interpreter=config.runtime_root / "bin/python3.12",
            interpreter_sha256=inputs.interpreter.binary_sha256,
            expected_environment_sha256=actual.environment_sha256,
            deadline=deadline,
        )
        for pin in pins:
            pin.check()
        return result


def _quiesce(policy: BrokerPolicy, job: _Execution) -> None:
    try:
        asyncio.run(ProjectExecutionBroker(policy)._kill_and_confirm(job))
        job.quiet = True
    except BaseException:  # noqa: BLE001 - uncertain cleanup must override every normal response.
        job.quiet = False
        raise ProcessCleanupIncomplete("python_project_cleanup_incomplete") from None


def _scope(request: _Request) -> PythonProjectScope:
    item = request.invocation
    return PythonProjectScope(
        tenant_id=item.tenant_id,
        run_id=item.run_id,
        project_id=item.project_id,
        session_id=item.session_id,
        original_reference=item.original_reference,
    )


def _probe(
    request: PythonProjectProbeRequest,
    policy: BrokerPolicy,
    config: RootConfig,
    stage: Path,
    deadline: float,
) -> PythonProjectBrokerResponse:
    invocation = request.invocation
    original = stage_original_contract(
        policy.project_workspace_root,
        base_python_invocation(invocation),
        ProjectBaselineReference.from_payload(invocation.original_reference.model_dump()),
        stage,
        policy.allowed_uid,
    )
    inputs = _measured_inputs(original, invocation, config, deadline)
    _need(
        _measured_inputs(original, invocation, config, deadline) == inputs,
        "python_project_runtime_untrusted",
    )
    return PythonProjectBrokerResponse(
        action=request.action,
        execution_id=invocation.execution_id,
        ok=True,
        observation=PythonProjectInputsObservation(
            scope=_scope(request),
            declaration=inputs.declaration.to_payload(),
            interpreter=inputs.interpreter.to_payload(),
            wheels=tuple(dict[str, object](item.to_payload()) for item in inputs.wheels),
        ),
    )


def _prepared(
    request: PythonProjectBrokerRequest | PythonProjectInspectEnvironmentRequest,
    policy: BrokerPolicy,
    config: RootConfig,
    stage: Path,
    deadline: float,
) -> StagedPythonProjectInputs:
    prepared = stage_python_project_inputs(
        workspace_root=policy.project_workspace_root,
        invocation=request.invocation,
        stage=stage,
        allowed_uid=policy.allowed_uid,
    )
    _need(prepared.original.generated_metadata is None or request.invocation.source_kind == 'candidate',
          'python_project_recipe_unavailable')
    actual = _measured_inputs(prepared.original, request.invocation, config, deadline)
    _need(actual == prepared.inputs, "python_project_recipe_unavailable")
    return prepared


def _inspect(
    request: PythonProjectInspectEnvironmentRequest,
    policy: BrokerPolicy,
    config: RootConfig,
    stage: Path,
    deadline: float,
) -> PythonProjectBrokerResponse:
    prepared = _prepared(request, policy, config, stage, deadline)
    cache = PythonProjectEnvironmentCache(config.environment_root, config.cache_policy)
    reference = cache.inspect(request.invocation, request.expected_environment, deadline=deadline)
    _need(reference is not None, "python_project_environment_unavailable")
    _need(reference == request.expected_environment, "python_project_environment_changed")
    _need(
        _measured_inputs(prepared.original, request.invocation, config, deadline)
        == prepared.inputs,
        "python_project_recipe_unavailable",
    )
    return PythonProjectBrokerResponse(
        action=request.action,
        execution_id=request.invocation.execution_id,
        ok=True,
        environment_reference=reference,
    )


async def _admit_release_unit(broker: ProjectExecutionBroker, job: _Execution) -> None:
    from agent_hub.skills.sandbox.python_project_broker import _admit_unit

    await _admit_unit(broker, job)


def _release_stage_invocation(value: object, name: str) -> ProjectInvocation:
    """Decode the existing broker marker; never infer scope from a directory name."""
    _need(type(value) is dict)
    assert isinstance(value, dict)
    _need(set(value) == {"execution_id", "project_id", "run_id", "session_id",
                         "source_sha256", "state", "tenant_id", "unit"})
    _need(type(value["state"]) is str and value["state"] in {"STAGED", "RUNNING", "UNKNOWN"})
    source = value["source_sha256"]
    _need(source is None or type(source) is str and re.fullmatch(r"[0-9a-f]{64}", source))
    # Validate marker identifiers with the existing schema, not execution authority.
    invocation = ProjectInvocation.model_validate_json(_encoded({
        field: value[field]
        for field in ("execution_id", "tenant_id", "run_id", "project_id", "session_id")
    } | {"sandbox_profile": "restricted", "timeout_seconds": 1}), strict=True)
    _need(name.rsplit("-", 1)[0] == invocation.execution_id
          and name != invocation.execution_id + "-"
          and value["unit"] == f"agent-hub-project-{invocation.execution_id}.service")
    return invocation


def _release_scope_guard(
    request: PythonProjectReleaseEnvironmentRequest, stage_root: Path, stage: Path, deadline: float,
) -> None:
    """Under admission: another execution's stage fences its entire tenant/run.

    Normal native consumers share admission, but an orphaned project unit does
    not inherit that FD. Its durable stage must independently veto release.
    This only inventories markers; it never cleans a stage or kills a unit.
    """
    if sys.platform != "linux":
        raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
    _need(stage.parent == stage_root)
    with ExitStack() as stack:
        pins: list[_Observed] = []
        parent = _root(stage_root, 0, stack, pins, stage=True)
        names: list[str] = []
        with os.scandir(parent) as members:
            for member in members:
                _check(deadline)
                _need(len(names) < 4096)
                if member.name != "active.lock":
                    names.append(member.name)
        own = False
        for name in sorted(names):
            _check(deadline)
            with ExitStack() as entry_stack:
                observed: list[_Observed] = []
                directory = _open_directory(parent, name, 0, entry_stack, observed, mode=0o700)
                fd = os.open("execution.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | os.O_CLOEXEC, dir_fd=directory)
                entry_stack.callback(os.close, fd)
                info = os.fstat(fd)
                # Legacy root-owned 0644 markers only supply a liveness fence;
                # this is not a product reader or a grant/receipt authority.
                _need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                      and stat.S_IMODE(info.st_mode) in {0o600, 0o644}
                      and 0 < info.st_size <= _MAX_CONTROL)
                marker = _Observed(directory, "execution.json", _stamp(info, full=True), True, fd)
                marker.check()
                raw = os.read(fd, _MAX_CONTROL + 1)
                _check(deadline)
                _need(len(raw) == info.st_size and not os.read(fd, 1))
                payload = json.loads(raw, object_pairs_hook=_object)
                item = _release_stage_invocation(payload, name)
                marker.check()
                for pin in observed:
                    pin.check()
                expected = request.invocation
                if name == stage.name:
                    _need(not own and item.execution_id == expected.execution_id
                          and (item.tenant_id, item.run_id, item.project_id, item.session_id)
                          == (expected.tenant_id, expected.run_id,
                              expected.project_id, expected.session_id))
                    _need(payload["state"] == "RUNNING")
                    own = True
                else:
                    _need(item.execution_id != expected.execution_id
                          and (item.tenant_id, item.run_id) != (expected.tenant_id, expected.run_id))
        _need(own)
        for pin in pins:
            pin.check()


def _release(
    request: PythonProjectReleaseEnvironmentRequest,
    policy: BrokerPolicy,
    config: RootConfig,
    deadline: float,
    *,
    stage: Path | None = None,
) -> PythonProjectBrokerResponse:
    """Called under shared admission, without staging or running project code."""
    from agent_hub.skills.sandbox.python_project_broker import PythonRootExecution

    origin = base_python_invocation(request.invocation).model_copy(
        update={"execution_id": request.expected_environment.origin_execution_id}
    )

    async def quiescent() -> None:
        broker = ProjectExecutionBroker(policy, retained_runtime_roots=(config.stage_root,))
        if broker._retained(origin.execution_id):
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
        await _admit_release_unit(broker, PythonRootExecution(origin, None))
        await _admit_release_unit(broker, _Execution(origin, None))

    try:
        asyncio.run(quiescent())
        _need(stage is not None)
        assert stage is not None
        _release_scope_guard(request, config.stage_root, stage, deadline)
    except (OSError, ValueError, RuntimeError, TimeoutError, TypeError, KeyError, AttributeError):
        raise ProcessCleanupIncomplete("python_project_cleanup_incomplete") from None
    _check(deadline)
    cache = PythonProjectEnvironmentCache(config.environment_root, config.cache_policy)
    removed = cache.release_completed(
        request.invocation, request.expected_environment, deadline=deadline
    )
    _need(type(removed) is bool, "python_project_environment_changed")
    return PythonProjectBrokerResponse(
        action=request.action,
        execution_id=request.invocation.execution_id,
        ok=True,
        released_environment=request.expected_environment,
        release_outcome="released" if removed else "absent",
    )


def _run(
    request: PythonProjectBrokerRequest,
    policy: BrokerPolicy,
    config: RootConfig,
    stage: Path,
    roots: _Roots,
    deadline: float,
) -> PythonProjectBrokerResponse:
    invocation = request.invocation
    prepared = _prepared(request, policy, config, stage, deadline)
    digest = _source_snapshot(prepared, invocation, policy, stage, deadline)
    snapshot = (
        prepared.original.snapshot if invocation.source_kind == "original" else stage / "snapshot"
    )
    budget = min(config.max_environment_bytes, invocation.workspace_limit_bytes)
    wheel_limits = ScanLimits(config.wheel_max_bytes)
    wheels = stage_python_project_wheels(
        config.wheel_root,
        stage,
        prepared.inputs.wheels,
        limits=wheel_limits,
        deadline=deadline,
        disk_reserve_bytes=config.cache_policy.disk_reserve_bytes,
    )
    cache = PythonProjectEnvironmentCache(config.environment_root, config.cache_policy)
    minimum = _minimum_environment_bytes(config, deadline)
    def fresh_check() -> None:
        # Runner export still has the original producer ceiling. Lowering the
        # cache copy budget must not lower the temporary-disk safety reserve.
        try:
            _disk_budget(config, roots, budget, True, product_bytes=invocation.workspace_limit_bytes)
        except PythonProjectBackendError as error:
            if error.code == "python_project_environment_quota":
                raise PythonProjectCacheError("cache_quota") from None
            raise

    with cache.reserve(invocation, next_bytes=budget, deadline=deadline,
            adaptive=True, minimum_bytes=minimum, fresh_check=fresh_check) as lease:
        reused = lease.current(deadline=deadline)
        _disk_budget(config, roots, budget, reused is None,
                     product_bytes=invocation.workspace_limit_bytes)
        plan = PythonProjectRunnerPlan(
            invocation=invocation,
            inputs=prepared.inputs.to_payload(),
            source_sha256=digest,
            reused_environment=reused,
            generated_manifest_sha256=(cast(str, prepared.original.generated_metadata["manifest_sha256"])
                if prepared.original.generated_metadata is not None else None),
        )
        plan_path = _write_plan(plan, roots.stage_fd, stage)
        export = _make_export(roots.stage_fd, stage)
        command = build_python_project_systemd_command(
            invocation,
            policy,
            snapshot=snapshot,
            original=prepared.original.snapshot,
            plan=plan_path,
            runtime=config.runtime_root,
            wheels=wheels,
            export=export,
            environment=lease.path / "environment" if reused is not None else None,
        )
        job = _Execution(
            base_python_invocation(invocation),
            None,
            stage=stage,
            launched=True,
            source_sha256=digest,
        )
        try:
            process = run_python_phase(
                command,
                cwd=stage,
                deadline=deadline,
                capture_limit_bytes=python_native_report_limit(invocation.output_limit_bytes),
            )
        except PythonPhaseCleanupError as error:
            _diagnostic("execution", error)
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete") from None
        finally:
            _quiesce(policy, job)
        _native()
        _check(deadline)
        _need(not process.timed_out and process.exit_code == 0 and not process.stdout_truncated)
        payload = _report(process.stdout, invocation, prepared.inputs, digest, reused,
            generated_manifest_sha256=plan.generated_manifest_sha256)
        verify_staged_python_wheels(
            wheels, prepared.inputs.wheels, limits=wheel_limits, deadline=deadline
        )
        _recheck_source(prepared, invocation, snapshot, digest, deadline)
        _need(
            _measured_inputs(prepared.original, invocation, config, deadline) == prepared.inputs,
            "python_project_recipe_unavailable",
        )
        for pin in roots.pins:
            pin.check()
        installed = payload["checks"][0]["status"] == "passed"
        reference = None
        if reused is None and installed:
            reference = _publish_export(
                lease,
                roots.stage_fd,
                export,
                prepared.inputs,
                config,
                lease.limits.max_bytes,
                payload["environment_sha256"],
                deadline,
                payload["status"] == "passed",
                execution=job,
            )
        else:
            _export_empty(roots.stage_fd, deadline, payload["status"] == "passed")
            if reused is not None and installed:
                reference = lease.current(deadline=deadline, expected=reused)
                _need(reference == reused, "python_project_environment_changed")
        _need(
            _measured_inputs(prepared.original, invocation, config, deadline) == prepared.inputs,
            "python_project_recipe_unavailable",
        )
        _check(deadline)
        if reused is None and not installed:
            check = payload["checks"][0]
            # Only a natural failed install can become retryable. Malformed or
            # timed-out phase output keeps the consumed partial/UNKNOWN fence.
            if (
                type(check["exit_code"]) is int
                and check["exit_code"] != 0
                and check["timed_out"] is False
            ):
                lease.record_install_failure(
                    PythonPhaseProcessResult(
                        check["exit_code"],
                        check["timed_out"],
                        check["stdout"],
                        check["stderr"],
                        check["stdout_truncated"],
                        check["stderr_truncated"],
                        check["stdout_observed_bytes"],
                        check["stderr_observed_bytes"],
                    ),
                    execution=job,
                    deadline=deadline,
                )
        if payload["status"] == "passed":
            retain_python_product(stage, invocation, payload, service_uid=policy.allowed_uid,
                                  deadline=deadline)
        return PythonProjectBrokerResponse(
            action=request.action,
            execution_id=invocation.execution_id,
            ok=True,
            result=SkillResult(
                exit_code=process.exit_code,
                stdout=process.stdout,
                stderr=process.stderr,
                timed_out=False,
            ),
            environment_reference=reference,
        )


def execute_python_project_request(
    request: _Request, *, policy: BrokerPolicy, configuration: RootConfig, stage: Path
) -> PythonProjectBrokerResponse:
    """Trusted root caller only; never call from an event loop or asyncio.to_thread.

    The caller supplies an already-created root0700 stage and a recipe grant;
    this function neither authenticates peers nor consumes service-side grants.
    ProcessCleanupIncomplete propagates, forcing the caller's UNKNOWN fence.
    """
    _need(
        type(request)
        in {
            PythonProjectBrokerRequest,
            PythonProjectProbeRequest,
            PythonProjectInspectEnvironmentRequest,
            PythonProjectReleaseEnvironmentRequest,
        },
        "python_project_request_invalid",
    )
    try:
        request = type(request).model_validate(request.model_dump(), strict=True)
    except (ValueError, TypeError, AttributeError):
        raise PythonProjectBackendError("python_project_request_invalid") from None
    try:
        _native()
        _need(type(configuration) is RootConfig, "python_project_runtime_untrusted")
        configuration = RootConfig.from_payload(configuration.to_payload())
        deadline = time.monotonic() + request.invocation.timeout_seconds
        with (
            _roots(
                policy, configuration, stage, not isinstance(request, PythonProjectProbeRequest)
            ) as roots,
            _admission(roots.stage_root_fd, deadline),
        ):
            if isinstance(request, PythonProjectReleaseEnvironmentRequest):
                response = _release(request, policy, configuration, deadline, stage=stage)
                response.require_for(request)
                return response
            copying = isinstance(request, PythonProjectBrokerRequest)
            source_copies = (
                2
                if isinstance(request, PythonProjectBrokerRequest)
                and request.invocation.source_kind == "candidate"
                else 1
            )
            # Initial admission covers staging/product. Environment allocation is
            # checked after the locked cache lease determines fresh versus reuse.
            _disk_budget(
                configuration,
                roots,
                0,
                False,
                source_bytes=request.invocation.source_limit_bytes * source_copies,
                product_bytes=request.invocation.workspace_limit_bytes if copying else 0,
            )
            if isinstance(request, PythonProjectProbeRequest):
                response = _probe(request, policy, configuration, stage, deadline)
            elif isinstance(request, PythonProjectInspectEnvironmentRequest):
                response = _inspect(request, policy, configuration, stage, deadline)
            else:
                response = _run(request, policy, configuration, stage, roots, deadline)
            response.require_for(request)
            _check(deadline)
        return response
    except ProcessCleanupIncomplete as error:
        _diagnostic("execution", error)
        raise
    except PythonProjectBackendError as error:
        _diagnostic("execution", error)
        code = error.code
    except PythonProjectCacheError as error:
        _diagnostic("execution", error)
        if error.reason == "cache_quota":
            code = "python_project_environment_quota"
        elif error.reason in {"cache_changed", "cache_unsafe"}:
            code = "python_project_environment_changed"
        else:
            code = "python_project_environment_unavailable"
    except PythonWheelStageError as error:
        _diagnostic("execution", error)
        code = (
            "python_project_environment_quota"
            if str(error) == "wheel_stage_limit"
            else "python_project_wheel_integrity"
        )
    except Exception as error:  # noqa: BLE001 - finite privileged execution boundary
        _diagnostic("execution", error)
        code = "python_project_execution_unavailable"
    return PythonProjectBrokerResponse(
        action=request.action, execution_id=request.invocation.execution_id, ok=False, error=code
    )


def _request(raw: bytes) -> _Request:
    try:
        payload: object = json.loads(raw, object_pairs_hook=_object)
        _need(type(payload) is dict, "python_project_request_invalid")
        assert isinstance(payload, dict)
        _need(
            type(payload.get("version")) is int and payload["version"] == 4,
            "python_project_request_invalid",
        )
        classes: dict[str, type[_Request]] = {
            "probe_python_project": PythonProjectProbeRequest,
            "run_python_project": PythonProjectBrokerRequest,
            "inspect_python_project_environment": PythonProjectInspectEnvironmentRequest,
            "release_python_project_environment": PythonProjectReleaseEnvironmentRequest,
        }
        action = payload.get("action")
        _need(type(action) is str and action in classes, "python_project_request_invalid")
        assert isinstance(action, str)
        return classes[action].model_validate_json(raw, strict=True)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise PythonProjectBackendError("python_project_request_invalid") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise PythonProjectBackendError("python_project_request_invalid")


def main(argv: list[str] | None = None) -> int:
    """Fixed config; --stage comes from the root broker, never a model path."""
    request = None
    phase: _DiagnosticStage = "native"
    try:
        _native()
        phase = "arguments"
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument("--stage", required=True)
        options = parser.parse_args(argv)
        stage = _path(options.stage)
        phase = "policy"
        policy = load_broker_policy()
        phase = "request"
        with ExitStack() as stack:
            pins: list[_Observed] = []
            parent = _root(stage, 0, stack, pins, stage=True)
            info = os.stat("request.json", dir_fd=parent, follow_symlinks=False)
            _need(0 < info.st_size <= _MAX_REQUEST, "python_project_request_invalid")
            request = _request(_read_control(parent, "request.json", 0, pins))
            phase = "configuration"
            configuration = load_root_configuration()
            phase = "execution"
            response = execute_python_project_request(
                request, policy=policy, configuration=configuration, stage=stage
            )
            phase = "stage_validation"
            for pin in pins:
                pin.check()
    except ProcessCleanupIncomplete as error:
        _diagnostic(phase, error)
        sys.stderr.write("python_project_cleanup_incomplete\n")
        return PYTHON_PROJECT_NATIVE_UNCERTAIN_EXIT_CODE
    except PythonProjectBackendError as error:
        _diagnostic(phase, error)
        if request is None:
            sys.stderr.write(error.code + "\n")
            return 2
        response = PythonProjectBrokerResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            ok=False,
            error=error.code,
        )
    except (Exception, SystemExit) as error:  # noqa: BLE001 - CLI must not emit host tracebacks
        _diagnostic(phase, error)
        if request is None:
            code = (
                "python_project_request_invalid"
                if phase in {"arguments", "request"}
                else "python_project_execution_unavailable"
            )
            sys.stderr.write(code + "\n")
            return 2
        response = PythonProjectBrokerResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            ok=False,
            error="python_project_execution_unavailable",
        )
    sys.stdout.write(response.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
