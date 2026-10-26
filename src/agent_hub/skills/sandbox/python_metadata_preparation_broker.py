"""v10 Root preparation with shared slots, one-use executions and two-unit drain."""

from __future__ import annotations

import asyncio
import json
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
from agent_hub.skills.sandbox.project_contract_broker import _encoded, _object, _write_all
from agent_hub.skills.sandbox.python_input_supply_broker import build_input_supply_root_command
from agent_hub.skills.sandbox.python_input_supply_catalog import read_supply_configuration
from agent_hub.skills.sandbox.python_input_supply_worker import validate_configuration
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    MetadataPreparationError,
    PythonMetadataPreparationRequest,
    PythonMetadataPreparationResponse,
    PythonMetadataPreparationStatus,
)
from agent_hub.skills.sandbox.python_metadata_preparation_worker import error_code
from agent_hub.skills.sandbox.python_project_backend import read_root_configuration
from agent_hub.skills.sandbox.python_project_broker import PythonRootExecution, _admit_unit
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
    from agent_hub.skills.sandbox.python_project_backend import RootConfig


class _MetadataPreparationExecution(_Execution):
    """Identify live v10 work without attributing another protocol's unit to it."""


def build_metadata_preparation_root_command(
    invocation: ProjectInvocation, policy: BrokerPolicy, *, configuration: RootConfig,
    supply: RootSupplyConfig, stage: Path,
) -> tuple[str, ...]:
    command = build_input_supply_root_command(invocation, policy,
        configuration=configuration, supply=supply, stage=stage)
    module = "agent_hub.skills.sandbox.python_input_supply_worker"
    if command.count(module) != 1:
        raise ValueError("metadata_prepare_request_invalid")
    return tuple("agent_hub.skills.sandbox.python_metadata_preparation_worker"
        if item == module else item for item in command)


def _write_request(stage: Path, request: PythonMetadataPreparationRequest) -> None:
    if sys.platform != "linux":
        raise ValueError("metadata_prepare_unavailable")
    raw = request.model_dump_json().encode("utf-8")
    if not 0 < len(raw) <= 64 * 1024:
        raise ValueError("metadata_prepare_request_invalid")
    fd = os.open(stage / "request.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _inspection_state(broker: ProjectExecutionBroker, request: PythonMetadataPreparationRequest,
    response: PythonMetadataPreparationResponse) -> PythonMetadataPreparationResponse:
    status = response.status
    if status is None or status.state not in {"unknown", "in_progress"}:
        return response
    assert request.preparation_execution_id is not None
    active = broker.executions.get(request.preparation_execution_id)
    running = (type(active) is _MetadataPreparationExecution and active.launched and active.task is not None
        and not active.task.done() and not active.finished.is_set()
        and all(getattr(active.invocation, field) == getattr(request.invocation, field)
            for field in ("tenant_id", "run_id", "project_id", "session_id")))
    checked = PythonMetadataPreparationStatus.model_validate(
        vars(status) | {"state": "in_progress" if running else "unknown"}, strict=True)
    response = PythonMetadataPreparationResponse.model_validate(
        vars(response) | {"status": checked}, strict=True)
    response.require_for(request)
    return response


async def handle_metadata_preparation(
    broker: ProjectExecutionBroker, request: PythonMetadataPreparationRequest, *, peer_uid: int,
) -> PythonMetadataPreparationResponse:
    def failure(code: MetadataPreparationError) -> PythonMetadataPreparationResponse:
        return PythonMetadataPreparationResponse(action=request.action,
            scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=False, error=code)

    if type(peer_uid) is not int or peer_uid != broker.policy.allowed_uid:
        return failure("metadata_prepare_request_invalid")
    if not _systemd_supported():
        return failure("metadata_prepare_unavailable")
    try:
        if type(request) is not PythonMetadataPreparationRequest:
            return failure("metadata_prepare_request_invalid")
        try:
            request = PythonMetadataPreparationRequest.model_validate(request, strict=True)
        except (ValueError, TypeError, AttributeError, RecursionError):
            return failure("metadata_prepare_request_invalid")
        invocation = ProjectInvocation.model_validate(vars(request.invocation), strict=True)
        key = invocation.execution_id
        if key in broker.executions or key in broker._cancelled_ids or broker._retained(key):
            return failure("metadata_prepare_consumed")
        configuration, supply = read_root_configuration(), read_supply_configuration()
        validate_configuration(broker.policy, configuration, supply)
        job = _MetadataPreparationExecution(invocation, asyncio.current_task())
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
                        except BaseException as error:  # noqa: BLE001 - always attempt child drain.
                            problem = error
                        finally:
                            if job.launched:
                                await broker._kill_and_confirm(job)
                        if problem is not None:
                            raise problem
                except BaseException:  # noqa: BLE001 - uncertain drain retains stage and slot.
                    raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete") from None

            if stopping is None:
                stopping = asyncio.create_task(stop())
            await asyncio.shield(stopping)

        async def finish() -> None:
            try:
                await stop_units()
                if native_uncertain:
                    raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete")
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
                command = build_metadata_preparation_root_command(invocation, broker.policy,
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
                    raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete")
                if result.exit_code != 0 or result.timed_out:
                    return failure("metadata_prepare_unavailable")
                payload = json.loads(result.stdout, object_pairs_hook=_object)
                response = PythonMetadataPreparationResponse.model_validate_json(_encoded(payload), strict=True)
                response.require_for(request)
                return _inspection_state(broker, request, response)
        finally:
            await _uninterrupted(finish())
    except ProcessCleanupIncomplete:
        return failure("metadata_prepare_cleanup_incomplete")
    except (ValueError, RuntimeError, OSError, TimeoutError, TypeError, AttributeError) as error:
        return failure(error_code(error))
