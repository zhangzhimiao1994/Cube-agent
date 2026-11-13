"""v10 Root preparation with shared slots, one-use executions and two-unit drain."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

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
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionFailurePhase,
    ResolutionRequest,
    ResolutionStatus,
)
from agent_hub.skills.sandbox.python_resolution_store import ResolutionLeaseWitness
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
    from agent_hub.skills.sandbox.python_project_backend import RootConfig


_LOGGER = logging.getLogger(__name__)


class _MetadataPreparationExecution(_Execution):
    """Identify live v10 work without attributing another protocol's unit to it."""


@dataclass
class _ResolutionPreparationExecution(_Execution):
    """v11 uses the same constrained Root/write and non-Root fetch lifetime."""

    original_request: ResolutionRequest | None = None
    current_claim: ResolutionClaim | None = None
    lease_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    witness: ResolutionLeaseWitness | None = None
    lease_closed: bool = False
    lease_timeout: asyncio.Timeout | None = None
    task_bound: float | None = None

    def __post_init__(self) -> None:
        if self.original_request is not None:
            self.current_claim = self.original_request.claim


async def _renew_resolution(
    broker: ProjectExecutionBroker, request: ResolutionRequest,
) -> ResolutionStatus:
    """Update one already-live job, never admission, slots, intent or execution."""
    denied = ResolutionStatus(state="unknown", key=request.key)
    job = broker.executions.get(request.invocation.execution_id)
    if type(job) is not _ResolutionPreparationExecution:
        return denied
    async with job.lease_lock:
        original, previous = job.original_request, job.current_claim
        if (original is None or previous is None or job.lease_closed
            or job.finished.is_set() or job.task is None or job.task.done()
            or job.task.cancelling()
            or broker.executions.get(request.invocation.execution_id) is not job
            or request.invocation.execution_id in broker._cancelled_ids):
            return denied
        # The renewal action is the only normalized field. Even planning transport
        # IDs and timeouts must equal the admitted immutable request exactly.
        if request.model_copy(update={"action": original.action, "claim": original.claim}) != original:
            return denied
        ResolutionLeaseWitness.require_renewal(previous, request.claim)
        if request.claim.planning != original.claim.planning:
            return denied
        timer = job.lease_timeout
        if timer is not None:
            # Rescheduling the existing timeout verifies that its original owner
            # scope is still entered; a finished/expiring scope cannot restart.
            if timer.expired() or job.task_bound is None:
                return denied
            when = timer.when()
            if when is None or asyncio.get_running_loop().time() >= when:
                return denied
            timer.reschedule(when)
        if job.stage is not None:
            if job.witness is None:
                return denied
            job.witness.renew(previous, request.claim)
        if timer is not None:
            assert job.task_bound is not None
            ResolutionLeaseWitness.require_renewal(previous, request.claim)
            timer.reschedule(min(job.task_bound, request.claim.deadline))
        job.current_claim = request.claim
        return ResolutionStatus(state="lease_renewed", key=original.key)


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


def _write_request(stage: Path, request: PythonMetadataPreparationRequest | ResolutionRequest) -> None:
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


async def _handle_preparation(
    broker: ProjectExecutionBroker, request: PythonMetadataPreparationRequest | ResolutionRequest, *, peer_uid: int,
) -> PythonMetadataPreparationResponse | ResolutionStatus:
    resolution = type(request) is ResolutionRequest
    phase: ResolutionFailurePhase = "request"
    action: str | None = None
    correlation_key: str | None = None
    exit_code: int | None = None
    timed_out: bool | None = None

    def failure(code: MetadataPreparationError) -> PythonMetadataPreparationResponse | ResolutionStatus:
        _LOGGER.warning(
            "python_preparation_failed action=%s phase=%s key=%s code=%s exit_code=%s timed_out=%s",
            action, phase, correlation_key, code, exit_code, timed_out,
            extra={"action": action, "phase": phase, "key": correlation_key, "code": code,
                   "exit_code": exit_code, "timed_out": timed_out},
        )
        if isinstance(request, ResolutionRequest):
            from agent_hub.skills.sandbox.python_resolution_protocol import (
                ResolutionFailureDiagnostic,
            )

            return ResolutionStatus(state="unknown", key=request.key,
                diagnostic=ResolutionFailureDiagnostic(phase=phase, code=code,
                    exit_code=exit_code, timed_out=timed_out))
        return PythonMetadataPreparationResponse(action=request.action,
            scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=False, error=code)

    if type(peer_uid) is not int or peer_uid != broker.policy.allowed_uid:
        return failure("metadata_prepare_request_invalid")
    if not _systemd_supported():
        return failure("metadata_prepare_unavailable")
    try:
        if type(request) not in {PythonMetadataPreparationRequest, ResolutionRequest}:
            return failure("metadata_prepare_request_invalid")
        try:
            request = (ResolutionRequest.model_validate(request, strict=True) if resolution
                else PythonMetadataPreparationRequest.model_validate(request, strict=True))
        except (ValueError, TypeError, AttributeError, RecursionError):
            return failure("metadata_prepare_request_invalid")
        invocation = ProjectInvocation.model_validate(vars(request.invocation), strict=True)
        action = request.action
        if isinstance(request, ResolutionRequest):
            correlation_key = request.key
        if isinstance(request, ResolutionRequest) and request.action == "renew_python_resolution":
            return await _renew_resolution(broker, request)
        if isinstance(request, ResolutionRequest):
            _ = request.claim.deadline
        key = invocation.execution_id
        phase = "admission"
        if key in broker.executions or key in broker._cancelled_ids or broker._retained(key):
            return failure("metadata_prepare_consumed")
        phase = "configuration"
        configuration, supply = read_root_configuration(), read_supply_configuration()
        validate_configuration(broker.policy, configuration, supply)
        if isinstance(request, ResolutionRequest):
            _ = request.claim.deadline
        phase = "admission"
        job: _Execution = (_ResolutionPreparationExecution(invocation, asyncio.current_task(),
            original_request=request) if isinstance(request, ResolutionRequest)
            else _MetadataPreparationExecution(invocation, asyncio.current_task()))
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
                if isinstance(job, _ResolutionPreparationExecution):
                    async with job.lease_lock:
                        job.lease_closed = True
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
            if isinstance(request, ResolutionRequest):
                deadline = min(deadline, request.claim.task_deadline)
            async with asyncio.timeout_at(deadline), asyncio.timeout(None) as lease_timeout:
                if isinstance(job, _ResolutionPreparationExecution):
                    async with job.lease_lock:
                        assert job.current_claim is not None
                        job.task_bound = deadline
                        job.lease_timeout = lease_timeout
                        lease_timeout.reschedule(min(deadline, job.current_claim.deadline))
                await broker.slots.acquire()
                job.slot_acquired = True
                await _admit_unit(broker, helper)
                await _admit_unit(broker, job)
                phase = "stage"
                _private_project_runtime(configuration.stage_root)
                if isinstance(job, _ResolutionPreparationExecution):
                    async with job.lease_lock:
                        assert job.current_claim is not None and job.original_request is not None
                        _ = job.current_claim.deadline
                        job.stage = Path(tempfile.mkdtemp(prefix=key + "-", dir=configuration.stage_root))
                        job.stage.chmod(0o700)
                        broker._metadata(job, "STAGED")
                        _write_request(job.stage, request)
                        job.witness = ResolutionLeaseWitness(job.stage, job.original_request)
                        job.witness.initialize(job.current_claim)
                else:
                    job.stage = Path(tempfile.mkdtemp(prefix=key + "-", dir=configuration.stage_root))
                    job.stage.chmod(0o700)
                    broker._metadata(job, "STAGED")
                    _write_request(job.stage, request)
                command = build_metadata_preparation_root_command(invocation, broker.policy,
                    configuration=configuration, supply=supply, stage=job.stage)
                if resolution:
                    module = "agent_hub.skills.sandbox.python_metadata_preparation_worker"
                    if command.count(module) != 1:
                        raise ValueError("resolution_request_invalid")
                    command = tuple("agent_hub.skills.sandbox.python_resolution_worker" if item == module else item for item in command)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                phase = "execution"
                helper.launched = job.launched = True
                broker._metadata(job, "RUNNING")
                result = await run_subprocess_with_limits(command, _Limits(remaining, 2_000_000),
                    stdin_payload=b"", process_started=lambda process: setattr(helper, "process", process),
                    on_forced_terminate=stop_units)
                if result.exit_code != 0 or result.timed_out:
                    exit_code, timed_out = result.exit_code, result.timed_out
                if result.exit_code == 3:
                    native_uncertain = True
                    raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete")
                if result.exit_code != 0 or result.timed_out:
                    if resolution and result.exit_code == 2 and not result.timed_out:
                        # Only a bounded, exact-key UNKNOWN from the reviewed Root worker.
                        # Invalid/private stdout is never reflected, even on failure.
                        try:
                            if type(result.stdout) is not str or not 0 < len(result.stdout.encode("utf-8")) <= 4096:
                                raise ValueError
                            raw = json.loads(result.stdout, object_pairs_hook=_object)
                            observed = ResolutionStatus.model_validate_json(_encoded(raw), strict=True)
                            if observed.key != cast(ResolutionRequest, request).key or observed.state != "unknown" or observed.diagnostic is None:
                                raise ValueError
                        except (ValueError, TypeError, AttributeError):
                            pass
                        else:
                            failure("metadata_prepare_unavailable")
                            return observed
                    return failure("metadata_prepare_unavailable")
                phase = "response"
                payload = json.loads(result.stdout, object_pairs_hook=_object)
                if isinstance(request, ResolutionRequest):
                    resolution_response = ResolutionStatus.model_validate_json(_encoded(payload), strict=True)
                    if resolution_response.key != request.key:
                        raise ValueError("resolution_response_invalid")
                    return resolution_response
                response = PythonMetadataPreparationResponse.model_validate_json(_encoded(payload), strict=True)
                response.require_for(request)
                return _inspection_state(broker, request, response)
        finally:
            previous_phase, phase = phase, "cleanup"
            await _uninterrupted(finish())
            phase = previous_phase
    except ProcessCleanupIncomplete:
        return failure("metadata_prepare_cleanup_incomplete")
    except (ValueError, RuntimeError, OSError, TimeoutError, TypeError, AttributeError) as error:
        return failure(error_code(error))


async def handle_metadata_preparation(
    broker: ProjectExecutionBroker, request: PythonMetadataPreparationRequest, *, peer_uid: int,
) -> PythonMetadataPreparationResponse:
    result = await _handle_preparation(broker, request, peer_uid=peer_uid)
    if type(result) is not PythonMetadataPreparationResponse:
        raise ValueError("metadata_prepare_response_invalid")
    return result


async def handle_resolution_preparation(
    broker: ProjectExecutionBroker, request: ResolutionRequest, *, peer_uid: int,
) -> ResolutionStatus:
    result = await _handle_preparation(broker, request, peer_uid=peer_uid)
    if type(result) is not ResolutionStatus:
        raise ValueError("resolution_response_invalid")
    return result
