"""v5 Root orchestration with the existing execution slots and two-unit drain."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
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
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    SupplyError,
)
from agent_hub.skills.sandbox.python_project_broker import (
    PythonRootExecution,
    _admit_unit,
    build_python_root_worker_command,
)
from agent_hub.skills.sandbox.python_project_command import _escaped
from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
    from agent_hub.skills.sandbox.python_project_backend import RootConfig


def build_input_supply_root_command(
    invocation: ProjectInvocation, policy: BrokerPolicy, *, configuration: RootConfig,
    supply: RootSupplyConfig, stage: Path,
) -> tuple[str, ...]:
    command = build_python_root_worker_command(invocation, policy,
                                               configuration=configuration, stage=stage)
    original_module = "agent_hub.skills.sandbox.python_project_backend"
    if command.count(original_module) != 1 or sum(p.startswith("ReadWritePaths=") for p in command) != 1:
        raise ValueError("input_supply_request_invalid")
    writable = "ReadWritePaths=" + " ".join(_escaped(path) for path in (
        configuration.stage_root, configuration.wheel_root, supply.state_root))
    return tuple("agent_hub.skills.sandbox.python_input_supply_worker" if part == original_module
                 else writable if part.startswith("ReadWritePaths=") else part for part in command)


def _write_request(stage: Path, request: PythonInputSupplyRequest) -> None:
    if sys.platform != "linux":
        raise ValueError("input_supply_unsupported")
    from agent_hub.skills.sandbox.project_contract_broker import _write_all

    raw = request.model_dump_json().encode("utf-8")
    if not 0 < len(raw) <= 64 * 1024:
        raise ValueError("input_supply_request_invalid")
    fd = os.open(stage / "request.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


async def handle_input_supply(
    broker: ProjectExecutionBroker, request: PythonInputSupplyRequest, *, peer_uid: int,
) -> PythonInputSupplyResponse:
    def failure(code: SupplyError) -> PythonInputSupplyResponse:
        return PythonInputSupplyResponse(action=request.action,
            execution_id=request.invocation.execution_id, ok=False, error=code)

    if peer_uid != broker.policy.allowed_uid:
        return failure("input_supply_request_invalid")
    if not _systemd_supported():
        return failure("input_supply_unsupported")
    try:
        if type(request) is not PythonInputSupplyRequest:
            return failure("input_supply_request_invalid")
        request = PythonInputSupplyRequest.model_validate_json(request.model_dump_json(), strict=True)
        invocation = base_python_invocation(request.invocation)
        key = invocation.execution_id
        if key in broker.executions or key in broker._cancelled_ids or broker._retained(key):
            return failure("input_supply_consumed")
        from agent_hub.skills.sandbox.python_input_supply_catalog import (
            InputSupplyError,
            read_supply_configuration,
        )
        from agent_hub.skills.sandbox.python_input_supply_worker import validate_configuration
        from agent_hub.skills.sandbox.python_project_backend import read_root_configuration

        configuration = read_root_configuration()
        supply = read_supply_configuration()
        validate_configuration(broker.policy, configuration, supply)
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
                    async with asyncio.timeout(8):
                        try:
                            if helper.launched:
                                await broker._kill_and_confirm(helper)
                        except BaseException as error:  # noqa: BLE001 - always stop the network child too.
                            problem = error
                        finally:
                            if job.launched:
                                await broker._kill_and_confirm(job)
                        if problem is not None:
                            raise problem
                except BaseException:  # noqa: BLE001 - failed drain must retain UNKNOWN even on cancellation.
                    raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete") from None

            if stopping is None:
                stopping = asyncio.create_task(stop())
            await asyncio.shield(stopping)

        async def finish() -> None:
            try:
                await stop_units()
                if native_uncertain:
                    raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")
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
                command = build_input_supply_root_command(invocation, broker.policy,
                    configuration=configuration, supply=supply, stage=job.stage)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                helper.launched = job.launched = True
                broker._metadata(job, "RUNNING")
                result = await run_subprocess_with_limits(command, _Limits(remaining, 2_000_000),
                    stdin_payload=b"", process_started=lambda process: setattr(helper, "process", process),
                    on_forced_terminate=stop_units)
                if result.exit_code == 3:
                    native_uncertain = True
                    raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")
                if result.exit_code != 0 or result.timed_out:
                    return failure("input_supply_unavailable")
                response = PythonInputSupplyResponse.model_validate_json(result.stdout, strict=True)
                response.require_for(request)
                return response
        finally:
            await _uninterrupted(finish())
    except ProcessCleanupIncomplete:
        return failure("input_supply_cleanup_incomplete")
    except (ValueError, RuntimeError, OSError, TimeoutError) as error:
        from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError

        return failure(error.code if isinstance(error, InputSupplyError) else "input_supply_unavailable")
