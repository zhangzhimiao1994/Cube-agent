"""Async root-helper orchestration using the shared project execution lifecycle.

Project code remains in the separate unprivileged project unit. The helper is
trusted installed code: its own cgroup contains forked observers and root copies.
Stop/confirm BOTH units before deleting a stage or releasing the execution slot.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox.base import run_subprocess_with_limits
from agent_hub.skills.sandbox.project import (
    ProjectExecutionBroker,
    ProjectInvocation,
    _Execution,
    _Limits,
    _private_project_runtime,
    _systemd_supported,
    _uninterrupted,
)
from agent_hub.skills.sandbox.python_project_command import _escaped
from agent_hub.skills.sandbox.python_project_process import python_root_response_limit
from agent_hub.skills.sandbox.python_project_protocol import (
    PYTHON_PROJECT_NATIVE_UNCERTAIN_EXIT_CODE,
    PythonProductDescriptor,
    PythonProjectBrokerError,
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInspectEnvironmentRequest,
    PythonProjectInvocation,
    PythonProjectProbeRequest,
    PythonProjectReleaseEnvironmentRequest,
    PythonProjectReleaseExpiredEnvironmentRequest,
    base_python_invocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_project_backend import RootConfig

type _Request = (
    PythonProjectBrokerRequest | PythonProjectProbeRequest | PythonProjectInspectEnvironmentRequest
    | PythonProjectReleaseEnvironmentRequest
    | PythonProjectReleaseExpiredEnvironmentRequest
)
type ProductSink = Callable[[Path, PythonProjectBrokerResponse, PythonProjectInvocation],
                           Awaitable[PythonProductDescriptor | None]]


@dataclass
class PythonRootExecution(_Execution):
    @property
    def unit(self) -> str:
        return f"agent-hub-python-broker-{self.invocation.execution_id}.service"


def build_python_root_worker_command(
    invocation: ProjectInvocation,
    policy: BrokerPolicy,
    *,
    configuration: RootConfig,
    stage: Path,
) -> tuple[str, ...]:
    invocation = ProjectInvocation.model_validate(invocation.model_dump(), strict=True)
    if stage.parent != configuration.stage_root:
        raise ValueError("python_project_request_invalid")
    _escaped(stage)
    _escaped(Path(policy.python))
    properties = [
        "User=root",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateNetwork=yes",
        "IPAddressDeny=any",
        "RestrictAddressFamilies=AF_UNIX",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "KillMode=control-group",
        "TimeoutStopSec=2s",
        "TasksMax=64",
        "Slice=system.slice",
        f"MemoryMax={invocation.memory_limit_bytes}",
        f"CPUQuota={invocation.cpu_quota_percent}%",
        f"RuntimeMaxSec={invocation.timeout_seconds}s",
        f"ReadWritePaths={_escaped(configuration.stage_root)} {_escaped(configuration.environment_root)}",
    ]
    command = [
        policy.systemd_run,
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        f"agent-hub-python-broker-{invocation.execution_id}",
    ]
    for value in properties:
        command.extend(("-p", value))
    command.extend(
        (
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            Path(policy.python).as_posix().replace("%", "%%"),
            "-I",
            "-B",
            "-m",
            "agent_hub.skills.sandbox.python_project_backend",
            "--stage",
            stage.as_posix().replace("%", "%%"),
        )
    )
    return tuple(command)


def _configuration() -> RootConfig:
    from agent_hub.skills.sandbox.python_project_backend import read_root_configuration

    return read_root_configuration()


async def _admit_unit(broker: ProjectExecutionBroker, job: _Execution) -> None:
    result = await broker._helper(
        (
            broker.policy.systemctl,
            "show",
            job.unit,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,TasksCurrent",
        ),
        2.0,
    )
    if not broker._unit_quiet(job, result):
        raise ValueError("python_project_execution_unavailable")


def _write_request(stage: Path, request: _Request) -> None:
    from agent_hub.skills.sandbox.project_contract_broker import _write_all

    raw = request.model_dump_json().encode("utf-8")
    if not 0 < len(raw) <= 2_000_000:
        raise ValueError("python_project_request_invalid")
    descriptor = os.open(
        stage / "request.json",
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.chmod(stage / "request.json", 0o600)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def handle_python_project(
    broker: ProjectExecutionBroker,
    request: _Request,
    *,
    peer_uid: int,
    product_sink: ProductSink | None = None,
) -> PythonProjectBrokerResponse:
    def failure(code: PythonProjectBrokerError) -> PythonProjectBrokerResponse:
        return PythonProjectBrokerResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            ok=False,
            error=code,
        )

    if peer_uid != broker.policy.allowed_uid:
        return failure("python_project_request_invalid")
    if not _systemd_supported():
        return failure("python_project_unsupported")
    try:
        if type(request) not in {
            PythonProjectBrokerRequest,
            PythonProjectProbeRequest,
            PythonProjectInspectEnvironmentRequest,
            PythonProjectReleaseEnvironmentRequest,
            PythonProjectReleaseExpiredEnvironmentRequest,
        }:
            raise ValueError("python_project_request_invalid")
        request = type(request).model_validate_json(request.model_dump_json(), strict=True)
        invocation = base_python_invocation(request.invocation)
        key = invocation.execution_id
        if key in broker.executions or key in broker._cancelled_ids or broker._retained(key):
            return failure("python_project_request_invalid")
        configuration = _configuration()
        if configuration.stage_root != broker.policy.python_project_runtime_root:
            return failure("python_project_runtime_untrusted")
        job = _Execution(invocation, asyncio.current_task())
        helper = PythonRootExecution(invocation, job.task)
        broker.executions[key] = job
        stopping: asyncio.Task[None] | None = None
        native_uncertain = False

        async def stop_units() -> None:
            nonlocal stopping

            async def stop() -> None:
                problem: BaseException | None = None
                try:
                    # Once the helper is dead it cannot start a new project unit.
                    # Even an uncertain helper stop must attempt the child kill.
                    async with asyncio.timeout(8.0):
                        try:
                            if helper.launched:
                                await broker._kill_and_confirm(helper)
                        except BaseException as error:  # noqa: BLE001 - preserve cleanup uncertainty.
                            problem = error
                        finally:
                            if job.launched:
                                await broker._kill_and_confirm(job)
                    if problem is not None:
                        raise problem
                except BaseException:  # noqa: BLE001 - no UNKNOWN stage may be removed.
                    raise ProcessCleanupIncomplete("python_project_cleanup_incomplete") from None

            if stopping is None:
                stopping = asyncio.create_task(stop())
            await asyncio.shield(stopping)

        async def finish() -> None:
            try:
                await stop_units()
                if native_uncertain:
                    # Quiescence cannot establish whether a native publication
                    # completed. Keep its durable fence across broker restarts.
                    raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
                # Both cgroups are now empty. The common owned-stage cleanup is
                # safe only after launcher IO has also been drained by its owner.
                job.launched = False
                await broker._cleanup(job)
            except BaseException:
                job.quiet = False
                if job.stage is not None:
                    broker._metadata(job, "UNKNOWN")
                raise
            finally:
                if job.slot_acquired and job.quiet:
                    broker.slots.release()
                    job.slot_acquired = False
                job.task = None
                helper.process = None
                job.finished.set()

        try:
            deadline = asyncio.get_running_loop().time() + invocation.timeout_seconds
            async with asyncio.timeout_at(deadline):
                await broker.slots.acquire()
                job.slot_acquired = True
                await _admit_unit(broker, helper)
                await _admit_unit(broker, job)
                _private_project_runtime(configuration.stage_root)
                job.stage = Path(tempfile.mkdtemp(prefix=key + "-", dir=configuration.stage_root))
                job.stage.chmod(0o700)
                broker._metadata(job, "STAGED")
                _write_request(job.stage, request)
                command = build_python_root_worker_command(
                    invocation, broker.policy, configuration=configuration, stage=job.stage
                )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                helper.launched = True
                job.launched = True
                broker._metadata(job, "RUNNING")
                result = await run_subprocess_with_limits(
                    command,
                    # Phase capture is already bounded. Allow nested JSON's
                    # escaping overhead without killing a valid verbose build.
                    _Limits(remaining, python_root_response_limit(invocation.output_limit_bytes)),
                    stdin_payload=b"",
                    process_started=lambda process: setattr(helper, "process", process),
                    on_forced_terminate=stop_units,
                )
                if result.exit_code == PYTHON_PROJECT_NATIVE_UNCERTAIN_EXIT_CODE:
                    native_uncertain = True
                    raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
                if result.exit_code != 0 or result.timed_out:
                    return failure("python_project_execution_unavailable")
                response = PythonProjectBrokerResponse.model_validate_json(
                    result.stdout, strict=True
                )
                response.require_for(request)
                if isinstance(request, PythonProjectBrokerRequest) and request.product_transfer_version == 1:
                    if product_sink is None:
                        return failure("python_project_execution_unavailable")
                    # Drain both cgroups before reading an output; keep stage and
                    # slot until the transfer and normal cleanup have completed.
                    await stop_units()
                    product = await product_sink(job.stage, response, request.invocation)
                    response = response.model_copy(update={"product": product})
                    response.require_for(request)
                return response
        finally:
            await _uninterrupted(finish())
    except ProcessCleanupIncomplete:
        return failure("python_project_cleanup_incomplete")
    except (ValueError, RuntimeError, OSError, TimeoutError) as error:
        from agent_hub.skills.sandbox.python_project_backend import PythonProjectBackendError

        if isinstance(error, PythonProjectBackendError):
            return failure(error.code)
        return failure("python_project_execution_unavailable")
