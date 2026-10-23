"""SDK Root helper/fetch lifecycle over the existing shared broker admission pool."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox.base import run_subprocess_with_limits
from agent_hub.skills.sandbox.project import (
    ProjectExecutionBroker,
    _Execution,
    _Limits,
    _private_project_runtime,
    _systemd_supported,
    _uninterrupted,
)
from agent_hub.skills.sandbox.python_project_broker import _admit_unit
from agent_hub.skills.sandbox.python_project_command import _escaped
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog
from agent_hub.skills.sandbox.toolchain_protocol import (
    TOOLCHAIN_DRAIN_SECONDS,
    TOOLCHAIN_TERMINATION_SECONDS,
    ToolchainError,
    ToolchainInvocation,
    ToolchainRequest,
    ToolchainResponse,
    base_toolchain_invocation,
    parse_toolchain_request,
    parse_toolchain_response,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy


@dataclass
class ToolchainExecution(_Execution):
    @property
    def termination_grace_seconds(self) -> float:
        return TOOLCHAIN_TERMINATION_SECONDS


@dataclass
class ToolchainRootExecution(ToolchainExecution):
    @property
    def unit(self) -> str:
        return "agent-hub-toolchain-broker-" + self.invocation.execution_id + ".service"


def build_toolchain_root_command(
    invocation: ToolchainInvocation,
    policy: BrokerPolicy,
    *,
    catalog: ToolchainCatalog,
    stage: Path,
) -> tuple[str, ...]:
    _escaped(stage)
    _escaped(Path(policy.python))
    if stage.parent != Path(catalog.stage_root) or stage.parent != policy.toolchain_runtime_root:
        raise ValueError("toolchain_request_invalid")
    properties = (
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
        "ReadWritePaths="
        + " ".join(
            _escaped(p)
            for p in (
                Path(catalog.stage_root),
                Path(catalog.runtime_root),
                Path(catalog.state_root),
                policy.python_project_runtime_root,
            )
        ),
    )
    command = [
        policy.systemd_run,
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        "agent-hub-toolchain-broker-" + invocation.execution_id,
    ]
    for prop in properties:
        command.extend(("-p", prop))
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
            "agent_hub.skills.sandbox.toolchain_worker",
            "--stage",
            stage.as_posix().replace("%", "%%"),
        )
    )
    return tuple(command)


def _configuration() -> ToolchainCatalog:
    from agent_hub.skills.sandbox.toolchain_configuration import read_toolchain_configuration

    return read_toolchain_configuration(deadline=time.monotonic() + 10)


def toolchain_error(error: Exception) -> ToolchainError:
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalogError
    from agent_hub.skills.sandbox.toolchain_configuration import ToolchainConfigurationError
    from agent_hub.skills.sandbox.toolchain_store import ToolchainStoreError
    from agent_hub.skills.sandbox.toolchain_worker import ToolchainWorkerError

    if isinstance(error, ToolchainWorkerError):
        return error.code
    if isinstance(error, ToolchainConfigurationError):
        codes: dict[str, ToolchainError] = {
            "toolchain_configuration_timeout": "toolchain_timeout",
            "toolchain_configuration_unsupported": "toolchain_unsupported",
            "toolchain_configuration_missing": "toolchain_configuration_missing",
        }
        return codes.get(error.code, "toolchain_configuration_untrusted")
    if isinstance(error, ToolchainCatalogError):
        if error.code == "toolchain_capacity":
            return "toolchain_capacity"
        if error.code == "toolchain_catalog_no_match":
            return "toolchain_catalog_no_match"
        return "toolchain_configuration_untrusted"
    if isinstance(error, ToolchainStoreError):
        return (
            "toolchain_not_ready"
            if error.code == "toolchain_store_missing"
            else (
                "toolchain_timeout"
                if error.code == "toolchain_store_timeout"
                else "toolchain_artifact_integrity"
            )
        )
    return "toolchain_timeout" if isinstance(error, TimeoutError) else "toolchain_unavailable"


async def handle_toolchain_request(
    broker: ProjectExecutionBroker,
    request: ToolchainRequest,
    *,
    peer_uid: int,
) -> ToolchainResponse:
    def failure(code: ToolchainError) -> ToolchainResponse:
        return ToolchainResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            scope=request.invocation.scope,
            ok=False,
            error=code,
        )

    if peer_uid != broker.policy.allowed_uid:
        return failure("toolchain_request_invalid")
    if not _systemd_supported():
        return failure("toolchain_unsupported")
    try:
        request = parse_toolchain_request(request.model_dump_json().encode())
        from agent_hub.skills.sandbox.toolchain_worker import validate_toolchain_configuration

        catalog = _configuration()
        validate_toolchain_configuration(broker.policy, catalog)
        invocation = base_toolchain_invocation(request.invocation)
        key = invocation.execution_id
        if key in broker.executions or key in broker._cancelled_ids or broker._retained(key):
            return failure("toolchain_consumed")
        job = ToolchainExecution(invocation, asyncio.current_task())
        helper = ToolchainRootExecution(invocation, job.task)
        broker.executions[key] = job
        stopping: asyncio.Task[None] | None = None
        native_uncertain = False

        async def stop_units() -> None:
            nonlocal stopping

            async def stop() -> None:
                problem: BaseException | None = None
                try:
                    async with asyncio.timeout(TOOLCHAIN_DRAIN_SECONDS):
                        try:
                            if helper.launched:
                                await broker._kill_and_confirm(helper)
                        except BaseException as error:  # noqa: BLE001 - always drain both units.
                            problem = error
                        finally:
                            if job.launched:
                                await broker._kill_and_confirm(job)
                        if problem is not None:
                            raise problem
                except BaseException:  # noqa: BLE001 - every uncertain drain retains its stage.
                    raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete") from None

            if stopping is None:
                stopping = asyncio.create_task(stop())
            await asyncio.shield(stopping)

        async def finish() -> None:
            try:
                await stop_units()
                if native_uncertain:
                    raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete")
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
                _private_project_runtime(Path(catalog.stage_root))
                job.stage = Path(tempfile.mkdtemp(prefix=key + "-", dir=catalog.stage_root))
                job.stage.chmod(0o700)
                broker._metadata(job, "STAGED", request.invocation.scope.source_sha256)
                raw = request.model_dump_json().encode()
                fd = os.open(
                    job.stage / "request.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    from agent_hub.skills.sandbox.project_contract_broker import _write_all

                    _write_all(fd, raw)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                helper.launched = job.launched = True
                broker._metadata(job, "RUNNING")
                command = build_toolchain_root_command(
                    request.invocation, broker.policy, catalog=catalog, stage=job.stage
                )
                native_uncertain = True
                result = await run_subprocess_with_limits(
                    command,
                    _Limits(max(0.001, deadline - asyncio.get_running_loop().time()), 512 * 1024),
                    stdin_payload=b"",
                    process_started=lambda p: setattr(helper, "process", p),
                    on_forced_terminate=stop_units,
                    termination_grace_seconds=TOOLCHAIN_TERMINATION_SECONDS,
                )
                if result.exit_code == 3:
                    native_uncertain = True
                    raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete")
                if result.exit_code != 0 or result.timed_out:
                    return failure("toolchain_unavailable")
                response = parse_toolchain_response(result.stdout.encode())
                response.require_for(request)
                native_uncertain = False
                return response
        finally:
            await _uninterrupted(finish())
    except ProcessCleanupIncomplete:
        return failure("toolchain_cleanup_incomplete")
    except (ValueError, RuntimeError, OSError, TimeoutError) as error:
        return failure(toolchain_error(error))
