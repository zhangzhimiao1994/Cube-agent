"""Trusted outer orchestrator; project hooks run exclusively in fixed bwrap phases.

Launch this module with the installed broker interpreter, not a project venv.
The privileged broker supplies read-only inputs at fixed mount points and owns
the cgroup, quota tmpfs, deadline and cleanup. The private export is exposed to
this orchestrator ONLY; it is never a phase mount or inherited directory FD.
Native metadata is not an independent judgment of business correctness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_hub.capabilities.python_original_private_recipe import canonical_original_lock
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.project_contract_mounts import validate_contract_work
from agent_hub.skills.sandbox.project_runner import _copy_snapshot
from agent_hub.skills.sandbox.python_project_copy import _observe, copy_python_project_environment
from agent_hub.skills.sandbox.python_project_environment import (
    _MAX_ENTRIES,
    observe_python_project_environment,
)
from agent_hub.skills.sandbox.python_project_mounts import build_python_phase_mounts
from agent_hub.skills.sandbox.python_project_phase import validate_generated_python_test_execution
from agent_hub.skills.sandbox.python_project_process import (
    PythonPhaseProcessResult,
    python_phase_capture_limit,
    run_python_phase,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
)

_INPUT = Path("/run/agent-hub-python-source")
_ORIGINAL = Path("/run/agent-hub-python-original")
_RUNTIME = Path("/run/agent-hub-python-runtime")
_WHEELS = Path("/run/agent-hub-python-wheels")
_REUSED = Path("/run/agent-hub-python-environment")
_EXPORT = Path("/run/agent-hub-python-export")
_WORK = Path("/run/agent-hub-python-work/owned")
_PLAN = Path("/run/agent-hub-python-plan.json")
_SHA = re.compile(r"[a-f0-9]{64}")


def _need(value: object) -> None:
    if not value:
        raise ValueError("python_project_runner_rejected")


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class PythonProjectRunnerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    invocation: PythonProjectInvocation
    inputs: dict[str, object]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reused_environment: PythonEnvironmentReference | None = None
    environment_storage: Literal['persistent_cache', 'ephemeral_current_execution'] = 'persistent_cache'
    generated_manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    private_lock_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bound_plan(self) -> Self:
        prepared = PythonProjectInputs.from_payload(self.inputs)
        invocation = self.invocation
        _need(self.environment_storage != 'ephemeral_current_execution' or self.reused_environment is None)
        _need(self.generated_manifest_sha256 is None or invocation.source_kind == "candidate")
        if self.private_lock_sha256 is not None:
            _need(self.generated_manifest_sha256 is None
                and self.private_lock_sha256 == prepared.declaration.lock_sha256
                and hashlib.sha256(canonical_original_lock(prepared.declaration.requirements)).hexdigest()
                == self.private_lock_sha256)
        _need(
            (
                prepared.contract.tenant_id,
                prepared.contract.run_id,
                prepared.contract.project_id,
                prepared.contract.session_id,
            )
            == (
                invocation.tenant_id,
                invocation.run_id,
                invocation.project_id,
                invocation.session_id,
            )
        )
        _need(
            prepared.original_reference.to_payload() == invocation.original_reference.model_dump()
        )
        _need(
            prepared.sha256 == invocation.recipe_sha256
            and prepared.environment_input_sha256 == invocation.environment_input_sha256
        )
        if invocation.source_kind == "original":
            _need(self.source_sha256 == prepared.original_reference.input_source_sha256)
        if self.reused_environment is None:
            _need(invocation.install_allowed)
        else:
            reference = self.reused_environment
            _need(
                reference.scope.tenant_id == invocation.tenant_id
                and reference.scope.run_id == invocation.run_id
                and reference.scope.project_id == invocation.project_id
                and reference.scope.session_id == invocation.session_id
                and reference.scope.original_reference == invocation.original_reference
                and reference.recipe_sha256 == invocation.recipe_sha256
                and reference.environment_input_sha256 == invocation.environment_input_sha256
                and reference.origin_execution_id != invocation.execution_id
            )
        return self


def python_phase_detail(
    result: PythonPhaseProcessResult, *, phase: str, import_module: str
) -> dict[str, Any] | None:
    """Validate fixed subprocess metadata; stderr truncation is not process failure."""
    if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
        return None
    try:
        def pairs(rows: list[tuple[str, object]]) -> dict[str, object]:
            unique: dict[str, object] = {}
            for key, value in rows:
                _need(key not in unique)
                unique[key] = value
            return unique

        def constant(_: str) -> object:
            raise ValueError("python_project_runner_rejected")

        item: object = json.loads(result.stdout, object_pairs_hook=pairs, parse_constant=constant)
        _need(type(item) is dict)
        assert isinstance(item, dict)
        base = {"status", "phase", "evidence"}
        _need(
            item.get("status") == "completed"
            and item.get("phase") == phase
            and item.get("evidence") == "native_process_only"
        )
        if phase in {"install", "check_environment"}:
            _need(
                set(item) == base | {"requires_dist_check"}
                and item["requires_dist_check"] == "completed"
            )
        elif phase == "build":
            _need(set(item) == base | {"wheel"} and type(item["wheel"]) is dict)
            wheel = item["wheel"]
            _need(
                set(wheel) == {"filename", "sha256", "size"}
                and type(wheel["filename"]) is str
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl", wheel["filename"])
                is not None
                and type(wheel["sha256"]) is str
                and _SHA.fullmatch(wheel["sha256"]) is not None
                and type(wheel["size"]) is int
                and 0 < wheel["size"] <= 2**63 - 1
            )
        elif phase == "test":
            extra = {"generated_test_execution"} if "generated_test_execution" in item else set()
            _need(
                set(item) == base | {"test_count", "import"} | extra
                and type(item["test_count"]) is int
                and 0 < item["test_count"] <= 2**63 - 1
                and item["import"]
                == {
                    "module": import_module,
                    "origin": import_module.replace(".", "/") + "/__init__.py",
                }
            )
            if extra:
                proof = validate_generated_python_test_execution(item["generated_test_execution"])
                _need(proof["count"] == item["test_count"])
                item["generated_test_execution"] = dict(proof)
        else:
            return None
        return item
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _seal_environment(root: Path, *, deadline: float) -> None:
    """Drained private tree only: never chmod a symlink or a shared inode."""
    if sys.platform != "linux":
        raise ValueError("python_project_runner_rejected")

    def walk(fd: int, depth: int) -> None:
        if sys.platform != "linux":
            raise ValueError("python_project_runner_rejected")
        _need(depth <= 64 and time.monotonic() < deadline)
        before = os.fstat(fd)
        _need(
            stat.S_ISDIR(before.st_mode)
            and before.st_uid == os.getuid()
            and not before.st_mode & 0o7022
        )
        with os.scandir(fd) as members:
            for member in members:
                _need(time.monotonic() < deadline)
                info = os.stat(member.name, dir_fd=fd, follow_symlinks=False)
                _need(info.st_uid == os.getuid())
                if stat.S_ISLNK(info.st_mode):
                    _need(
                        depth == 0
                        and member.name == "lib64"
                        and info.st_nlink == 1
                        and os.readlink(member.name, dir_fd=fd) == "lib"
                    )
                    continue
                directory = stat.S_ISDIR(info.st_mode)
                _need(directory or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                if directory:
                    flags |= os.O_DIRECTORY
                child = os.open(member.name, flags, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    _need(
                        (
                            opened.st_dev,
                            opened.st_ino,
                            opened.st_mode,
                            opened.st_uid,
                            opened.st_nlink,
                        )
                        == (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
                    )
                    if directory:
                        walk(child, depth + 1)
                    else:
                        os.fchmod(child, 0o555 if info.st_mode & 0o111 else 0o444)
                finally:
                    os.close(child)
        os.fchmod(fd, 0o555)

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        walk(fd, 0)
    finally:
        os.close(fd)


def _wheel_actual(output: Path, expected: dict[str, Any], *, deadline: float,
                  export_root: Path | None = None, maximum: int | None = None) -> dict[str, Any]:
    if sys.platform != "linux":
        raise ValueError("python_project_runner_rejected")
    directory = output / "wheels"
    _need(directory.resolve(strict=True) == directory and directory.is_dir())
    _need([item.name for item in directory.iterdir()] == [expected["filename"]])
    path = directory / expected["filename"]
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        _need(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.getuid()
            and before.st_size == expected["size"]
        )
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(fd, min(1024**2, before.st_size - count + 1)):
            _need(time.monotonic() < deadline)
            count += len(chunk)
            _need(count <= before.st_size)
            digest.update(chunk)
        _need(
            count == before.st_size
            and digest.hexdigest() == expected["sha256"]
            and _file_identity(os.fstat(fd)) == _file_identity(before)
            and _file_identity(path.lstat()) == _file_identity(before)
        )
    finally:
        os.close(fd)
    if export_root is not None:
        from agent_hub.skills.sandbox.python_project_product_export import export_python_product

        _need(maximum is not None)
        assert maximum is not None
        export_python_product(output, export_root, expected=expected,
                              maximum=maximum, deadline=deadline)
    return dict(expected)


def run_python_project(plan: PythonProjectRunnerPlan) -> dict[str, Any]:
    if sys.platform != "linux":
        raise ValueError("python_project_runner_rejected")
    _need(os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX") == "systemd")
    plan = PythonProjectRunnerPlan.model_validate(plan.model_dump(), strict=True)
    invocation = plan.invocation
    inputs = PythonProjectInputs.from_payload(plan.inputs)
    deadline = time.monotonic() + max(0.1, invocation.timeout_seconds - 5)
    limits = ScanLimits(
        invocation.workspace_limit_bytes, max_entries=min(invocation.file_limit * 10, _MAX_ENTRIES)
    )
    _WORK.mkdir(mode=0o700)
    source, output = _WORK / "source", _WORK / "output"
    output.mkdir(mode=0o700)
    digest = _copy_snapshot(_INPUT, source, deadline)
    _need(digest == plan.source_sha256)
    arguments: dict[str, Any] = {
        "contract_payload": inputs.contract.to_payload(),
        "expected_contract_sha256": inputs.contract.protected_sha256,
        "original_root": _ORIGINAL,
        "work_root": source,
        "expected_source_sha256": digest,
        "ephemeral_dirs": (),
    }
    environment = _REUSED if plan.reused_environment is not None else _WORK / "environment"
    if plan.reused_environment is None:
        environment.mkdir(mode=0o700)
    initial = None
    if plan.reused_environment is not None:
        initial = _observe(environment, inputs.environment_input_sha256, limits, deadline)
        _need(initial.environment_sha256 == plan.reused_environment.environment_sha256)
    checks: list[dict[str, Any]] = []
    wheel = None
    test_count = None
    generated_test_execution = None
    private_lock = (canonical_original_lock(inputs.declaration.requirements)
                    if plan.private_lock_sha256 is not None else None)
    phases = ("check_environment" if initial is not None else "install", "build", "test")
    for phase in phases:
        with build_python_phase_mounts(
            phase,
            work_root=source,
            runtime_root=_RUNTIME,
            wheel_root=_WHEELS,
            environment_root=environment,
            output_root=output,
            phase_script=Path(__file__).with_name("python_project_phase.py"),
            import_module=inputs.declaration.import_module,
            contract_arguments=arguments,
            private_lock=private_lock if phase == "install" else None,
        ) as mounts:
            command = mounts.argv
            if phase == "test" and plan.generated_manifest_sha256 is not None:
                command = (*command, "--generated-manifest-sha256", plan.generated_manifest_sha256)
            result = run_python_phase(
                command,
                cwd=source,
                deadline=deadline,
                capture_limit_bytes=python_phase_capture_limit(invocation.output_limit_bytes),
                pass_fds=mounts.pass_fds,
            )
        detail = python_phase_detail(
            result, phase=phase, import_module=inputs.declaration.import_module
        )
        checks.append(
            {
                "phase": "install" if phase == "check_environment" else phase,
                "status": "passed" if detail is not None else "failed",
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "stdout_observed_bytes": result.stdout_observed_bytes,
                "stderr_observed_bytes": result.stderr_observed_bytes,
            }
        )
        if detail is None:
            if phase in {"install", "check_environment"}:
                initial = None
            break
        if phase == "install":
            normalized = observe_python_project_environment(
                environment,
                interpreter=environment / "bin/python",
                environment_input_sha256=inputs.environment_input_sha256,
                limits=limits,
                normalize_bytecode=True,
            )
            _seal_environment(environment, deadline=deadline)
            initial = _observe(environment, inputs.environment_input_sha256, limits, deadline)
            _need(normalized.environment_sha256 == initial.environment_sha256)
        elif phase == "build":
            wheel = _wheel_actual(output, detail["wheel"], deadline=deadline)
        elif phase == "test":
            test_count = detail["test_count"]
            generated_test_execution = detail.get("generated_test_execution")
            _need((generated_test_execution is not None) == (plan.generated_manifest_sha256 is not None))
            if generated_test_execution is not None:
                _need(generated_test_execution["manifest_sha256"] == plan.generated_manifest_sha256)
    validate_contract_work(**arguments)
    exported = False
    if initial is not None:
        current = _observe(environment, inputs.environment_input_sha256, limits, deadline)
        _need(
            current.environment_sha256 == initial.environment_sha256
            and current.security_manifest == initial.security_manifest
        )
        if plan.reused_environment is None:
            private = _EXPORT / "ready"
            private.mkdir(mode=0o700)
            copy_python_project_environment(
                environment,
                private / "environment",
                source_owner=os.getuid(),
                interpreter=_RUNTIME / "bin/python3.12",
                interpreter_sha256=inputs.interpreter.binary_sha256,
                environment_input_sha256=inputs.environment_input_sha256,
                expected_environment_sha256=current.environment_sha256,
                limits=limits,
                deadline=deadline,
            )
            exported = True
    if len(checks) == 3 and all(check["status"] == "passed" for check in checks):
        _need(wheel is not None)
        assert wheel is not None
        _wheel_actual(output, wheel, deadline=deadline, export_root=_EXPORT,
                      maximum=invocation.workspace_limit_bytes)
    return {
        "schema_version": 2,
        "kind": "python_project",
        "evidence": "native_process_only",
        "status": "passed"
        if len(checks) == 3 and all(check["status"] == "passed" for check in checks)
        else "failed",
        "execution_id": invocation.execution_id,
        "source_sha256": digest,
        "recipe_sha256": inputs.sha256,
        "environment_input_sha256": inputs.environment_input_sha256,
        "environment_sha256": initial.environment_sha256 if initial is not None else None,
        "original_check_contract": inputs.contract.protected_sha256,
        "runtime": inputs.interpreter.to_payload(),
        "checks": checks,
        "wheel": wheel,
        "test_count": test_count,
        "exported_environment": exported,
        "dependency_install": {
            "mode": "reused" if plan.reused_environment is not None else "created",
            "origin_execution_id": plan.reused_environment.origin_execution_id
            if plan.reused_environment is not None
            else invocation.execution_id,
        },
        **({"generated_test_execution": generated_test_execution}
           if generated_test_execution is not None else {}),
    }


def main() -> int:
    if sys.platform != "linux":
        raise ValueError("python_project_runner_rejected")
    _need(os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX") == "systemd")
    _need(len(sys.argv) == 1)
    fd = os.open(_PLAN, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        _need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and not info.st_mode & 0o022
            and 0 < info.st_size <= 32 * 1024**2
        )
        raw = bytearray()
        while chunk := os.read(fd, min(1024**2, info.st_size - len(raw) + 1)):
            raw.extend(chunk)
            _need(len(raw) <= info.st_size)
        _need(
            len(raw) == info.st_size
            and _file_identity(os.fstat(fd)) == _file_identity(info)
            and _file_identity(_PLAN.lstat()) == _file_identity(info)
        )
    finally:
        os.close(fd)
    plan = PythonProjectRunnerPlan.model_validate_json(raw, strict=True)
    report = run_python_project(plan)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
