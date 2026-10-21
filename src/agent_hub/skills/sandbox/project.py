"""Trusted project execution over the privileged broker's v2 protocol."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_hub.capabilities import scoped_read
from agent_hub.files.workspace import WORKSPACE_METADATA_DOTFILES, project_private_entry
from agent_hub.skills.sandbox.base import SkillResult, run_subprocess_with_limits
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy, BrokerResponse

_CLEANUP_SECONDS = 7.0
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_INPUT = "/run/agent-hub-project-input"
_WORK = "/run/agent-hub-project-work"
_PATH = "/opt/agent-hub/node/bin:/usr/bin:/bin"
_CACHE = frozenset({"node_modules", "venv", "__pycache__"})


class ProjectInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    tenant_id: UUID
    run_id: UUID
    project_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    sandbox_profile: Literal["restricted", "workspace_write"]
    install_allowed: bool = False
    timeout_seconds: int = Field(ge=1, le=3600)
    output_limit_bytes: int = Field(default=262144, ge=1, le=1024**2)
    memory_limit_bytes: int = Field(default=1024**3, ge=1, le=4 * 1024**3)
    cpu_quota_percent: int = Field(default=100, ge=1, le=400)
    workspace_limit_bytes: int = Field(default=512 * 1024**2, ge=1, le=2 * 1024**3)
    source_limit_bytes: int = Field(default=100 * 1024**2, ge=1, le=500 * 1024**2)
    file_limit: int = Field(default=10000, ge=1, le=100000)

    @property
    def input(self) -> dict[str, object]:
        return {}


class ProjectBrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[2] = 2
    action: Literal["run_project"] = "run_project"
    invocation: ProjectInvocation


class OriginalProjectReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_count: int = Field(ge=1, le=100000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("original project reference version must be an integer")
        return value


class ContractProjectInvocation(ProjectInvocation):
    original_reference: OriginalProjectReference
    source_kind: Literal["original", "candidate"]


class ContractProjectBrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[3] = 3
    action: Literal["run_project"] = "run_project"
    invocation: ContractProjectInvocation


def _source_invocation(invocation: ProjectInvocation) -> ProjectInvocation:
    if type(invocation) is ContractProjectInvocation:
        invocation = ContractProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        return ProjectInvocation.model_validate(
            {name: getattr(invocation, name) for name in ProjectInvocation.model_fields}, strict=True,
        )
    return ProjectInvocation.model_validate(invocation.model_dump(), strict=True)


class ProjectTerminateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[2] = 2
    action: Literal["terminate_project"] = "terminate_project"
    execution_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")


def _directory(path: Path) -> None:
    info = path.lstat()
    if scoped_read._is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("unsafe directory")
    if str(path.absolute()) != str(path.resolve(strict=True)):
        raise ValueError("directory alias")


def _scope(root: Path, invocation: ProjectInvocation) -> tuple[Path, tuple[str, ...]]:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("workspace root must be absolute")
    _directory(root)
    parts = (
        str(invocation.tenant_id),
        "projects",
        invocation.project_id,
        "sessions",
        invocation.session_id,
    )
    current = root
    for part in parts:
        # Exact spelling also rejects case aliases and Windows short-name aliases.
        with os.scandir(current) as entries:
            if not any(entry.name == part for entry in entries):
                raise ValueError("workspace scope missing")
        current /= part
        _directory(current)
    return current, parts


def _excluded(name: str, *, at_root: bool) -> bool:
    lower = name.casefold()
    return (
        lower in _CACHE
        or project_private_entry(name)
        or (name.startswith(".") and not (at_root and name in WORKSPACE_METADATA_DOTFILES))
    )


async def snapshot_project_source(
    root: Path,
    invocation: ProjectInvocation,
    *,
    destination: Path | None = None,
    _manifest: dict[str, str] | None = None,
) -> str:
    """Hash deliverable files; optionally write the same bytes into a new owned directory."""
    try:
        # Validate even model_copy/model_construct callers before touching source paths.
        invocation = _source_invocation(invocation)
        source, scope = _scope(root, invocation)
        if destination is not None:
            destination.mkdir(mode=0o700)
        hashes: dict[str, str] = {}
        total = 0
        visited = 0
        pending = [source]
        while pending:
            directory = pending.pop()
            _directory(directory)
            relative = directory.relative_to(source)
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited % 128 == 0:
                        await asyncio.sleep(0)
                    if visited > invocation.file_limit * 10:
                        raise ValueError("directory entry limit exceeded")
                    # Prune before stat/traversal: excluded directories are never read.
                    if _excluded(entry.name, at_root=directory == source):
                        continue
                    scoped_read._path_parts(entry.name)
                    info = (directory / entry.name).lstat()
                    if scoped_read._is_link(info):
                        raise ValueError("source link denied")
                    member = relative / entry.name
                    if stat.S_ISDIR(info.st_mode):
                        if entry.name.startswith("."):
                            continue
                        pending.append(directory / entry.name)
                        if destination is not None:
                            (destination / member).mkdir(mode=0o700)
                        continue
                    scoped_read._regular_file(info)
                    if len(hashes) >= invocation.file_limit:
                        raise ValueError("source file limit exceeded")
                    if info.st_size > invocation.source_limit_bytes - total:
                        raise ValueError("source byte limit exceeded")
                    data = scoped_read._read_no_links(
                        root,
                        (*scope, *member.parts),
                        max_bytes=invocation.source_limit_bytes - total,
                    )
                    total += len(data)
                    if total > invocation.source_limit_bytes:
                        raise ValueError("source byte limit exceeded")
                    mode_tag = b"x\0" if info.st_mode & 0o111 else b"f\0"
                    hashes[member.as_posix()] = hashlib.sha256(mode_tag + data).hexdigest()
                    if destination is not None:
                        target = destination / member
                        with target.open("xb") as handle:
                            handle.write(data)
                        target.chmod(0o555 if info.st_mode & 0o111 else 0o444)
                    await asyncio.sleep(0)
        if destination is not None:
            for directory_name, _, _ in os.walk(destination, topdown=False):
                Path(directory_name).chmod(0o555)
        manifest = json.dumps(hashes, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if _manifest is not None:
            _manifest.update(hashes)
        return hashlib.sha256(manifest.encode("ascii")).hexdigest()
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError("project source unavailable or unsafe or limits exceeded") from None


async def project_source_fingerprint(root: Path, invocation: ProjectInvocation) -> str:
    return await snapshot_project_source(root, invocation, destination=None)


async def project_source_manifest(root: Path, invocation: ProjectInvocation) -> dict[str, str]:
    files: dict[str, str] = {}
    await snapshot_project_source(root, invocation, _manifest=files)
    return files


def build_project_systemd_command(
    invocation: ProjectInvocation,
    policy: BrokerPolicy,
    snapshot: Path,
    *,
    contract_path: Path | None = None,
    original_snapshot: Path | None = None,
    contract_sha256: str | None = None,
    generated_manifest_sha256: str | None = None,
) -> tuple[str, ...]:
    from agent_hub.skills.sandbox.broker import _systemd_path

    # systemd specifiers and property word separators must remain literal path bytes.
    def escaped(path: Path) -> str:
        value = _systemd_path(path)
        return "".join(
            "%%"
            if character == "%"
            else f"\\x{ord(character):02x}"
            if character in "\\ '\""
            else character
            for character in value
        )

    contract_enabled = (contract_path, original_snapshot, contract_sha256)
    if any(item is not None for item in contract_enabled) and (
        not all(item is not None for item in contract_enabled)
        or type(invocation) is not ContractProjectInvocation
        or type(contract_sha256) is not str or len(contract_sha256) != 64
        or any(char not in "0123456789abcdef" for char in contract_sha256)
    ):
        raise ValueError("original project contract command is incomplete")
    if type(invocation) is ContractProjectInvocation and not all(item is not None for item in contract_enabled):
        raise ValueError("original project contract command is incomplete")
    if generated_manifest_sha256 is not None and (
        type(generated_manifest_sha256) is not str or len(generated_manifest_sha256) != 64
        or any(char not in "0123456789abcdef" for char in generated_manifest_sha256)
        or not all(item is not None for item in contract_enabled)
        or type(invocation) is not ContractProjectInvocation or invocation.source_kind != "candidate"
    ):
        raise ValueError("generated acceptance requires a protected candidate contract")
    properties: tuple[str, ...] = (
        "DynamicUser=yes",
        f"SupplementaryGroups={policy.service_group}",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        # Masking /proc submounts prevents rootless bwrap from mounting its own proc.
        # Only the trusted runner runs outside bwrap; project code always runs inside it.
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "TasksMax=128",
        "Slice=system.slice",
        "KillMode=control-group",
        "TimeoutStopSec=2s",
        f"MemoryMax={invocation.memory_limit_bytes}",
        f"CPUQuota={invocation.cpu_quota_percent}%",
        f"RuntimeMaxSec={invocation.timeout_seconds}s",
        (
            f"TemporaryFileSystem={_WORK}:rw,nosuid,nodev,size={invocation.workspace_limit_bytes},"
            f"nr_inodes={invocation.file_limit * 10},mode=1777"
        ),
        f"BindReadOnlyPaths={escaped(snapshot)}:{_INPUT}",
        "InaccessiblePaths=/var/lib/agent-hub",
    )
    if contract_path is not None and original_snapshot is not None:
        properties += (
            f"BindReadOnlyPaths={escaped(contract_path)}:/run/agent-hub-project-contract.json",
            f"BindReadOnlyPaths={escaped(original_snapshot)}:/run/agent-hub-project-original",
        )
    command = [
        policy.systemd_run,
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        f"agent-hub-project-{invocation.execution_id}",
    ]
    for prop in properties:
        command.extend(("-p", prop))
    # env -i prevents manager/service credentials leaking into the trusted stdlib runner.
    command.extend(
        (
            "/usr/bin/env",
            "-i",
            f"PATH={_PATH}",
            "AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX=systemd",
            "/usr/bin/python3",
            "-I",
            "-B",
            _systemd_path(
                policy.source_path / "agent_hub/skills/sandbox/project_runner.py"
            ).replace("%", "%%"),
            "--input",
            _INPUT,
            "--work",
            f"{_WORK}/project",
            "--timeout",
            str(invocation.timeout_seconds),
        )
    )
    if invocation.install_allowed:
        command.append("--install-allowed")
    if contract_sha256 is not None:
        command.extend(("--check-contract", "/run/agent-hub-project-contract.json",
                        "--original-input", "/run/agent-hub-project-original",
                        "--expected-contract-sha256", contract_sha256,
                        '--workspace-limit-bytes', str(invocation.workspace_limit_bytes),
                        '--file-limit', str(invocation.file_limit)))
    if generated_manifest_sha256 is not None:
        command.extend(("--generated-manifest-sha256", generated_manifest_sha256))
    return tuple(command)


@dataclass(frozen=True)
class _Limits:
    timeout_seconds: float
    output_limit_bytes: int

    @property
    def input(self) -> Mapping[str, object]:
        return {}


@dataclass
class _Execution:
    invocation: ProjectInvocation
    task: asyncio.Task[Any] | None
    stage: Path | None = None
    launched: bool = False
    process: asyncio.subprocess.Process | None = None
    cleanup: asyncio.Task[None] | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    quiet: bool = False
    source_sha256: str | None = None
    slot_acquired: bool = False

    @property
    def unit(self) -> str:
        return f"agent-hub-project-{self.invocation.execution_id}.service"


async def _uninterrupted[T](awaitable: Awaitable[T]) -> T:
    task = asyncio.ensure_future(awaitable)
    interruption: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            interruption = error
        except Exception:  # noqa: BLE001 - retrieve and propagate task failure below
            break
    result = task.result()
    if interruption is not None:
        raise interruption
    return result


def _systemd_supported() -> bool:
    return sys.platform == "linux"


def _private_project_runtime(path: Path) -> None:
    from agent_hub.skills.sandbox.project_contract_broker import _root

    try:
        with contextlib.ExitStack() as stack:
            _root(path, 0, stack, [], stage=True)
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError('project_private_runtime_unavailable') from None


class ProjectExecutionBroker:
    def __init__(self, policy: BrokerPolicy, slots: asyncio.Semaphore | None = None,
                 *, retained_runtime_roots: tuple[Path, ...] = ()) -> None:
        self.policy = policy
        self.retained_runtime_roots = retained_runtime_roots
        self.slots = slots if slots is not None else asyncio.Semaphore(8)
        self.executions: dict[str, _Execution] = {}
        self._cancelled_ids: set[str] = set()

    def _retained(self, execution_id: str) -> bool:
        return any(root.exists() and any(
            path.parent.name.rsplit("-", 1)[0] == execution_id
            for path in root.glob(f"{execution_id}-*/execution.json")
        ) for root in (self.policy.runtime_root, *self.retained_runtime_roots))

    async def handle(
        self,
        request: ProjectBrokerRequest | ContractProjectBrokerRequest | ProjectTerminateRequest,
        *,
        peer_uid: int,
    ) -> BrokerResponse:
        from agent_hub.skills.sandbox.broker import BrokerResponse

        if peer_uid != self.policy.allowed_uid:
            return BrokerResponse(ok=False, error="caller uid is not authorized")
        if not _systemd_supported():
            return BrokerResponse(ok=False, error="project_systemd_linux_unsupported")
        try:
            request_type = (
                ProjectTerminateRequest
                if isinstance(request, ProjectTerminateRequest)
                else ContractProjectBrokerRequest
                if isinstance(request, ContractProjectBrokerRequest)
                else ProjectBrokerRequest
            )
            request = request_type.model_validate(request.model_dump(), strict=True)
            if isinstance(request, ProjectTerminateRequest):
                await self.terminate(request.execution_id)
                return BrokerResponse(ok=True)
            invocation = request.invocation
            if (
                invocation.execution_id in self.executions
                or invocation.execution_id in self._cancelled_ids
            ):
                raise RuntimeError("duplicate project execution_id")
            # UNKNOWN stages survive broker restarts and must never be replayed.
            if self._retained(invocation.execution_id):
                raise RuntimeError("duplicate project execution_id: retained stage")
            job = _Execution(invocation, asyncio.current_task())
            self.executions[invocation.execution_id] = job
            try:
                deadline = asyncio.get_running_loop().time() + invocation.timeout_seconds
                async with asyncio.timeout_at(deadline):
                    await self.slots.acquire()
                    job.slot_acquired = True
                    _private_project_runtime(self.policy.runtime_root)
                    job.stage = Path(
                        tempfile.mkdtemp(
                            prefix=f"{invocation.execution_id}-",
                            dir=self.policy.runtime_root,
                        )
                    )
                    job.stage.chmod(0o700)
                    self._metadata(job, "STAGED")
                    snapshot = job.stage / "snapshot"
                    contract_fields: dict[str, Any] = {}
                    if isinstance(request, ContractProjectBrokerRequest):
                        from agent_hub.capabilities.project_baselines import (
                            ProjectBaselineReference,
                        )
                        from agent_hub.skills.sandbox.project_contract_broker import (
                            stage_original_contract,
                        )

                        stored = await asyncio.to_thread(
                            stage_original_contract,
                            workspace_root=self.policy.project_workspace_root,
                            invocation=_source_invocation(invocation),
                            reference=ProjectBaselineReference.from_payload(
                                request.invocation.original_reference.model_dump(),
                            ), stage=job.stage, allowed_uid=self.policy.allowed_uid,
                        )
                        generated_metadata = getattr(stored, "generated_metadata", None)
                        if generated_metadata is not None:
                            from agent_hub.capabilities.project_baselines import _generated_metadata

                            generated_metadata = _generated_metadata(generated_metadata)
                            if request.invocation.source_kind != "candidate":
                                raise RuntimeError("generated_acceptance_original_credit_forbidden")
                        if request.invocation.source_kind == "original":
                            snapshot = stored.snapshot
                            digest = stored.reference.input_source_sha256
                        else:
                            files: dict[str, str] = {}
                            digest = await snapshot_project_source(
                                self.policy.project_workspace_root, invocation,
                                destination=snapshot, _manifest=files,
                            )
                            stored.contract.require_current(files)
                        contract_fields = {
                            "contract_path": job.stage / "check_contract.json",
                            "original_snapshot": stored.snapshot,
                            "contract_sha256": stored.contract.protected_sha256,
                        }
                        if generated_metadata is not None:
                            contract_fields["generated_manifest_sha256"] = generated_metadata["manifest_sha256"]
                    else:
                        digest = await snapshot_project_source(
                            self.policy.project_workspace_root, invocation, destination=snapshot,
                        )
                    command = build_project_systemd_command(
                        invocation, self.policy, snapshot, **contract_fields,
                    )
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    job.launched = True
                    self._metadata(job, "RUNNING", digest)
                    result = await run_subprocess_with_limits(
                        command,
                        _Limits(remaining, invocation.output_limit_bytes),
                        process_started=lambda process: setattr(job, "process", process),
                        on_forced_terminate=lambda: self._cleanup(job),
                    )
                if contract_fields:
                    self._validate_result(result, digest, expected_contract_sha256=contract_fields['contract_sha256'])
                else:
                    self._validate_result(result, digest)
                return BrokerResponse(ok=True, result=result)
            finally:
                try:
                    await _uninterrupted(self._cleanup(job))
                finally:
                    if job.slot_acquired and job.quiet:
                        self.slots.release()
                        job.slot_acquired = False
                    job.task = None
                    job.process = None
                    job.finished.set()
        except TimeoutError:
            return BrokerResponse(ok=False, error="project execution deadline exceeded")
        except (OSError, ValueError, RuntimeError) as error:
            # Stable errors never disclose scoped source, host paths or runner stderr.
            if isinstance(error, ProcessCleanupIncomplete):
                message = "project_termination_cleanup_incomplete"
            elif isinstance(error, RuntimeError):
                message = str(error)
            else:
                message = "project execution unavailable"
            return BrokerResponse(ok=False, error=message)

    def _metadata(self, job: _Execution, state: str, digest: str | None = None) -> None:
        assert job.stage is not None
        if digest is not None:
            job.source_sha256 = digest
        data = {
            "state": state,
            "unit": job.unit,
            "execution_id": job.invocation.execution_id,
            "tenant_id": str(job.invocation.tenant_id),
            "run_id": str(job.invocation.run_id),
            "project_id": job.invocation.project_id,
            "session_id": job.invocation.session_id,
            "source_sha256": job.source_sha256,
        }
        temporary = job.stage / "execution.json.tmp"
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="ascii")
        temporary.replace(job.stage / "execution.json")

    @staticmethod
    def _validate_result(
        result: SkillResult, digest: str, *, expected_contract_sha256: str | None = None,
    ) -> None:
        if result.exit_code != 0 or result.timed_out:
            return
        try:
            report = json.loads(result.stdout)
            if (
                not isinstance(report, dict)
                or type(report.get("schema_version")) is not int
                or report["schema_version"] != 1
                or report.get("status") not in ("passed", "failed", "blocked")
                or report.get("source_sha256") != digest
                or not isinstance(report.get("checks"), list)
                or not isinstance(report.get("runtime"), dict)
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise RuntimeError("project_runner_invalid_report") from None
        if expected_contract_sha256 is not None:
            binding = report.get('original_check_contract')
            if (not isinstance(binding, dict) or type(binding.get('schema_version')) is not int
                    or binding != {'schema_version': 1, 'contract_sha256': expected_contract_sha256,
                                   'protected_sha256': expected_contract_sha256}):
                raise RuntimeError('project_runner_invalid_contract_report')

    async def _cleanup(self, job: _Execution) -> None:
        if job.cleanup is None:
            job.cleanup = asyncio.create_task(self._finish(job))
        await asyncio.shield(job.cleanup)

    async def _finish(self, job: _Execution) -> None:
        try:
            if job.launched:
                await self._kill_and_confirm(job)
            job.quiet = True
            if job.stage is not None:
                from agent_hub.skills.sandbox.project_cleanup import make_private_stage_writable

                make_private_stage_writable(job.stage)
                shutil.rmtree(job.stage)
        except BaseException:  # noqa: BLE001 - preserve UNKNOWN on every cleanup failure
            job.quiet = False
            if job.stage is not None:
                self._metadata(job, "UNKNOWN")
            raise ProcessCleanupIncomplete("project_termination_cleanup_incomplete") from None

    async def _helper(self, command: tuple[str, ...], timeout: float) -> SkillResult:
        return await run_subprocess_with_limits(command, _Limits(timeout, 65536), stdin_payload=b"")

    async def _kill_and_confirm(self, job: _Execution) -> None:
        deadline = asyncio.get_running_loop().time() + _CLEANUP_SECONDS
        # Reserve time for base.py to kill/reap a helper that exhausts its own deadline.
        reap_reserve = min(2.0, _CLEANUP_SECONDS / 3)
        try:
            async with asyncio.timeout_at(deadline):
                await self._helper(
                    (
                        self.policy.systemctl,
                        "kill",
                        "--kill-who=all",
                        "--signal=SIGKILL",
                        job.unit,
                    ),
                    min(2.0, (_CLEANUP_SECONDS - reap_reserve) / 2),
                )
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= reap_reserve:
                        raise TimeoutError
                    result = await self._helper(
                        (
                            self.policy.systemctl,
                            "show",
                            job.unit,
                            "--no-pager",
                            "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,TasksCurrent",
                        ),
                        min(1.0, remaining - reap_reserve),
                    )
                    if self._unit_quiet(job, result):
                        return
                    await asyncio.sleep(min(0.1, remaining))
        finally:
            # Reap the local systemd-run launcher even when the manager is unavailable.
            if job.process is not None and job.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    job.process.kill()

    @staticmethod
    def _unit_quiet(job: _Execution, result: SkillResult) -> bool:
        if result.timed_out:
            return False
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        load = properties.get("LoadState")
        if load not in ("not-found", "loaded"):
            return False
        if properties.get("ActiveState") not in ("inactive", "failed"):
            return False
        if properties.get("SubState") not in ("dead", "failed"):
            return False
        if properties.get("MainPID") != "0" or properties.get("ControlPID") != "0":
            return False
        if properties.get("TasksCurrent") not in ("0", "[not set]", "18446744073709551615"):
            return False
        expected = f"/system.slice/{job.unit}"
        group = properties.get("ControlGroup")
        if group not in ("", expected):
            return False
        path = _CGROUP_ROOT / expected.lstrip("/")
        try:
            if not path.exists():
                return group == "" and load == "not-found"
            events = dict(
                line.split() for line in (path / "cgroup.events").read_text().splitlines()
            )
            return events.get("populated") == "0"
        except (OSError, ValueError):
            return False

    async def terminate(self, execution_id: str) -> None:
        from agent_hub.skills.sandbox.base import validate_execution_id

        validate_execution_id(execution_id)
        job = self.executions.get(execution_id)
        if job is None:
            if self._retained(execution_id):
                raise ProcessCleanupIncomplete("project_termination_cleanup_incomplete")
            # Cancellation arriving before its run request reserves a tombstone.
            self._cancelled_ids.add(execution_id)
            return
        if job.task is not None and not job.finished.is_set():
            job.task.cancel()
            async with asyncio.timeout(_CLEANUP_SECONDS + 3):
                await job.finished.wait()
        if not job.quiet:
            raise ProcessCleanupIncomplete("project_termination_cleanup_incomplete")


async def request_project_broker(
    request: ProjectBrokerRequest | ContractProjectBrokerRequest | ProjectTerminateRequest,
    *,
    socket_path: Path = Path("/run/agent-hub/skill-broker.sock"),
) -> BrokerResponse:
    from agent_hub.skills.sandbox.broker import (
        _MAX_RESPONSE_BYTES,
        BrokerResponse,
        _read_frame,
        _write_frame,
    )

    connector = getattr(asyncio, "open_unix_connection", None)
    if connector is None:
        raise OSError("project_systemd_linux_unsupported")
    reader, writer = await connector(socket_path.as_posix())
    try:
        await _write_frame(writer, request.model_dump_json().encode("utf-8"))
        return BrokerResponse.model_validate_json(
            await _read_frame(reader, _MAX_RESPONSE_BYTES),
            strict=True,
        )
    finally:
        writer.close()
        await writer.wait_closed()


class ProjectSystemdSandbox:
    def __init__(self, settings: object | None = None, *, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path or cast(
            Path,
            getattr(settings, "socket_path", Path("/run/agent-hub/skill-broker.sock")),
        )

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        if not _systemd_supported():
            raise OSError("project_systemd_linux_unsupported")
        try:
            async with asyncio.timeout(invocation.timeout_seconds + _CLEANUP_SECONDS + 3):
                response = await request_project_broker(
                    ContractProjectBrokerRequest(invocation=invocation)
                    if isinstance(invocation, ContractProjectInvocation)
                    else ProjectBrokerRequest(invocation=invocation),
                    socket_path=self.socket_path,
                )
            if not response.ok or response.result is None:
                if not response.ok and response.error == "project_termination_cleanup_incomplete":
                    raise ProcessCleanupIncomplete("project_termination_cleanup_incomplete")
                raise OSError(response.error or "project broker rejected execution")
            return response.result
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(self.terminate(invocation.execution_id))
            raise

    async def terminate(self, execution_id: str) -> None:
        if not _systemd_supported():
            raise OSError("project_systemd_linux_unsupported")
        async with asyncio.timeout(_CLEANUP_SECONDS + 3):
            response = await request_project_broker(
                ProjectTerminateRequest(execution_id=execution_id),
                socket_path=self.socket_path,
            )
        if not response.ok:
            raise ProcessCleanupIncomplete(
                response.error or "project_termination_cleanup_incomplete"
            )
