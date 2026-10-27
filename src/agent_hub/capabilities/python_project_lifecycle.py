"""Trusted terminal cache cleanup, independent of model tool arguments."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_project_service import PythonOriginalContext
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.db.models import RunApprovalRow, RunCheckpointRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.skills.sandbox.python_project_client import PythonProjectSystemdSandbox
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
    PythonProjectReleaseEnvironmentRequest,
    _reference_matches,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.python_generated_service import PythonGeneratedEnvironmentContext

_LOGGER = logging.getLogger(__name__)
_RELEASE_MARKER = "python_project_cache_release"
_CANCEL_VERSION = 'python_project_cancel_cleanup_version'


class _Repository(Protocol):
    async def run_transaction(self) -> AsyncSession: ...


class _ContextReader(Protocol):
    async def python_original_context(
        self,
        tenant_id: UUID,
        run_id: UUID,
    ) -> PythonOriginalContext | None: ...


class _GeneratedContextReader(Protocol):
    async def environment_context(
        self, tenant: UUID, run: UUID,
    ) -> PythonGeneratedEnvironmentContext | None: ...


class _Releaser(Protocol):
    async def release_completed(
        self,
        invocation: PythonProjectInvocation,
        *,
        expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference: ...


def _uncertain(value: object) -> bool:
    # Conservative, bounded metadata walk. Never interpret a model terminal flag.
    pending = [(value, 0)]
    remaining = 100_000
    while pending:
        current, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 64:
            return True
        if isinstance(current, str):
            if current.lower() in {
                "unknown",
                "uncertain",
                "prepared",
                "running",
                "executing",
                "inflight",
                "in_flight",
                "partial",
                "outcome_uncertain",
                "tool_outcome_uncertain",
                "model_outcome_uncertain",
                "python_project_cleanup_incomplete",
            }:
                return True
        elif isinstance(current, Mapping):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((item, depth + 1) for item in current)
    return False


def _cancelled_model_history_only(value: object, run_id: UUID) -> bool:
    """Recognize only the cancelled historical model paths, never rewrite them.

    Caller already holds the terminal run lock and checks cancellation policy,
    leases, route and approvals. Root independently fences native consumers.
    """
    if (type(value) is not dict or value.get('phase') != 'running'
            or type(value.get('models')) is not dict or not value['models']
            or type(value.get('tools')) is not dict
            or len(value['models']) + len(value['tools']) > 100_000):
        return False
    required = {'status', 'step_id', 'attempt', 'purpose', 'actor', 'call_index',
                'request_sha256', 'artifact_id', 'sha256', 'provenance'}
    optional = {'failure_reason', 'reserved_max_output_tokens', 'initial_incomplete_chain',
                'post_tool_growth', 'provider_continuation'}
    allowed_paths: set[tuple[str, ...]] = {('phase',)}
    for key, row in value['models'].items():
        if (type(key) is not str or re.fullmatch('[a-f0-9]{64}', key) is None
                or type(row) is not dict or len(row) > len(required) + len(optional)
                or not required <= row.keys()
                or row.keys() - required - optional
                or type(row['status']) is not str
                or row['status'] not in {'running', 'succeeded', 'rejected'}):
            return False
        if (any(type(row[name]) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', row[name]) is None
                for name in ('actor', 'step_id'))
                or type(row['purpose']) is not str or row['purpose'] not in {'step', 'review'}
                or any(type(row[name]) is not int or not 0 <= row[name] <= 2**31 - 1
                       for name in ('attempt', 'call_index'))
                or type(row['request_sha256']) is not str
                or re.fullmatch('[a-f0-9]{64}', row['request_sha256']) is None):
            return False
        material = f"{run_id}:{row['step_id']}:{row['attempt']}:{row['purpose']}:{row['actor']}:{row['call_index']}"
        if hashlib.sha256(material.encode()).hexdigest() != key:
            return False
        if row['status'] == 'running':
            if any(row[name] is not None for name in ('artifact_id', 'sha256', 'provenance')):
                return False
            allowed_paths.add(('models', key, 'status'))
    if len(allowed_paths) == 1:
        return False
    for key, row in value['tools'].items():
        if (type(key) is not str or re.fullmatch('[a-f0-9]{64}', key) is None
                or type(row) is not dict or type(row.get('status')) is not str
                or row['status'] not in {'succeeded', 'rejected'}
                or not {'step_id', 'attempt', 'round', 'tool_index', 'name', 'arguments_sha256',
                        'trigger_model_artifact_id', 'replay_safe', 'artifact_id', 'sha256'} <= row.keys()):
            return False
    pending: list[tuple[object, tuple[str, ...], int]] = [(value, (), 0)]
    remaining = 100_000
    while pending:
        current, path, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 64:
            return False
        if type(current) is str:
            if not (current == 'running' and path in allowed_paths) and _uncertain(current):
                return False
        elif type(current) is dict:
            if any(type(key) is not str for key in current):
                return False
            pending.extend((item, (*path, key), depth + 1) for key, item in current.items())
        elif type(current) in (list, tuple):
            items = cast(list[object] | tuple[object, ...], current)
            pending.extend((item, (*path, str(index)), depth + 1) for index, item in enumerate(items))
        elif ((current is not None and type(current) not in (bool, int, float))
              or type(current) is float and not math.isfinite(current)):
            return False
    return True


def _generated_preparation(route: Mapping[str, object]) -> Mapping[str, object] | None:
    from agent_hub.capabilities.generated_project_service import validate_generated_preparation

    try:
        if (type(route.get('generated_project_contract_version')) is not int
                or route['generated_project_contract_version'] != 1
                or {'project_input', 'project_input_contract_version', 'project_input_preparation'} & route.keys()):
            return None
        state = validate_generated_preparation(route.get('generated_project_preparation'))
        if state['stack'] != 'python' or state['phase'] != 'python_ready':
            return None
        supply = state.get('python_input_supply')
        if supply is not None and (not isinstance(supply, Mapping) or supply.get('phase') != 'supplied'):
            return None
        return state
    except (RuntimeError, ValueError, TypeError, KeyError):
        return None


class PythonProjectTerminalHook:
    def __init__(
        self,
        repository: _Repository,
        verifier: _ContextReader,
        executor: _Releaser,
        *,
        generated_reader: _GeneratedContextReader | None = None,
    ) -> None:
        self.repository = repository
        self.verifier = verifier
        self.executor = executor
        self.generated_reader = generated_reader
        self._retry_cursor: UUID | None = None

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None,
        run_id: UUID,
        status: RunStatus,
        mode: TaskMode | None,
        routing_decision: dict[str, object],
    ) -> None:
        if status is not RunStatus.COMPLETED and status is not RunStatus.CANCELLED:
            return
        async with await self.repository.run_transaction() as session, session.begin():
            # Retain the run lock through the RPC, fencing worker/approval writers
            # which lock this same row before changing durable run state.
            row = await session.scalar(
                select(RunRow)
                .where(
                    RunRow.tenant_id == tenant_id,
                    RunRow.id == run_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or (row.tenant_id, row.id, row.status)
                != (tenant_id, run_id, status.value)
                or any(
                    value is not None
                    for value in (
                        row.worker_id,
                        row.worker_lease_token,
                        row.worker_lease_expires_at,
                        row.worker_heartbeat_at,
                    )
                )
            ):
                return
            route = row.routing_decision
            if not isinstance(route, dict) or _RELEASE_MARKER in route or _uncertain(route):
                return
            if status is RunStatus.CANCELLED and (
                type(route.get(_CANCEL_VERSION)) is not int or route[_CANCEL_VERSION] != 1
                or route.get('source') == 'self_repair'
                or any(not isinstance(key, str) or key.startswith('self_repair_') for key in route)
            ):
                return
            generated = ('generated_project_contract_version' in route
                         or 'generated_project_preparation' in route)
            if generated:
                preparation = _generated_preparation(route)
                if preparation is None or self.generated_reader is None:
                    return
            else:
                original_preparation = route.get("project_input_preparation")
                if not isinstance(original_preparation, dict) or (
                    type(original_preparation.get("schema_version")) is not int
                    or original_preparation["schema_version"] != 2
                    or original_preparation.get("phase") != "observed"
                ):
                    return
                preparation = original_preparation
            if (
                await session.scalar(
                    select(RunApprovalRow.id)
                    .where(
                        RunApprovalRow.tenant_id == tenant_id,
                        RunApprovalRow.run_id == run_id,
                        RunApprovalRow.status.not_in(("approved", "rejected", "expired")),
                    )
                    .limit(1)
                )
                is not None
            ):
                return
            states = (
                await session.scalars(
                    select(RunCheckpointRow.state)
                    .where(
                        RunCheckpointRow.tenant_id == tenant_id,
                        RunCheckpointRow.run_id == run_id,
                    )
                    .order_by(RunCheckpointRow.sequence.desc())
                    .limit(1)
                )
            ).all()
            if not states or any(
                not isinstance(state, dict) or (_uncertain(state) and not (
                    status is RunStatus.CANCELLED and _cancelled_model_history_only(state, run_id)
                )) for state in states
            ):
                return
            project, workspace_session = authorized_workspace_scope(
                row,
                tenant_id=tenant_id,
                run_id=run_id,
            )
            context: PythonOriginalContext | PythonGeneratedEnvironmentContext | None
            if generated:
                assert self.generated_reader is not None
                context = await self.generated_reader.environment_context(tenant_id, run_id)
            else:
                context = await self.verifier.python_original_context(tenant_id, run_id)
            if context is None:
                return
            # One scope has one current cache entry. Conflicting historical refs
            # cannot select which live object to erase; preserve them for audit.
            references = tuple(
                dict.fromkeys(ref.model_dump_json() for ref in context.environment_references)
            )
            if len(references) != 1:
                return
            expected = PythonEnvironmentReference.model_validate_json(references[0], strict=True)
            invocation = PythonProjectInvocation.model_validate(
                context.invocation.model_dump()
                | {
                    "execution_id": "python_release_" + uuid4().hex,
                    "install_allowed": False,
                    "timeout_seconds": 30,
                },
                strict=True,
            )
            if (invocation.tenant_id, invocation.run_id) != (tenant_id, run_id):
                return
            if (invocation.project_id, invocation.session_id) != (project, workspace_session):
                return
            if generated:
                marker = preparation.get('python_recipe')
                if (not isinstance(marker, Mapping)
                        or preparation['reference'] != invocation.original_reference.model_dump(mode='json')
                        or marker['recipe_sha256'] != invocation.recipe_sha256
                        or marker['environment_input_sha256'] != invocation.environment_input_sha256):
                    return
            if not _reference_matches(invocation, expected):
                return
            request = PythonProjectReleaseEnvironmentRequest(
                invocation=invocation,
                expected_environment=expected,
            )
            released = await self.executor.release_completed(
                request.invocation,
                expected_environment=request.expected_environment,
            )
            if released != expected:
                raise ValueError("python_project_response_invalid")
            row.routing_decision = {
                **route,
                _RELEASE_MARKER: {
                    "schema_version": 1,
                    "environment_reference": expected.model_dump(mode="json"),
                },
            }
            row.version += 1

    async def retry_completed(self, limit: int = 8) -> int:
        """Retry eligible terminal cleanup independently of RunService's once marker.

        The cursor is pagination only. Every candidate re-enters the locked DB
        gate; neither a remembered ID nor this query authorizes cache release.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid Python cleanup retry limit")
        query = (
            select(RunRow)
            .where(
                or_(
                    RunRow.status == RunStatus.COMPLETED.value,
                    and_(RunRow.status == RunStatus.CANCELLED.value,
                         RunRow.routing_decision.contains({_CANCEL_VERSION: 1})),
                ),
                or_(
                    RunRow.routing_decision.contains(
                        {"project_input_preparation": {"schema_version": 2, "phase": "observed"}}
                    ),
                    RunRow.routing_decision.contains(
                        {'generated_project_contract_version': 1,
                         'generated_project_preparation': {
                             'schema_version': 1, 'phase': 'python_ready', 'stack': 'python'}}
                    ),
                ),
                ~RunRow.routing_decision.has_key(_RELEASE_MARKER),
            )
            .order_by(RunRow.id)
            .limit(limit)
        )
        if self._retry_cursor is not None:
            query = query.where(RunRow.id > self._retry_cursor)
        async with await self.repository.run_transaction() as session:
            rows = (await session.scalars(query)).all()
            candidates = tuple((row.tenant_id, row.id, RunStatus(row.status)) for row in rows)
        if not candidates:
            self._retry_cursor = None
        for tenant_id, run_id, status in candidates:
            try:
                await self(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    actor_id=None,
                    status=status,
                    mode=None,
                    routing_decision={},
                )
            except Exception as error:  # noqa: BLE001 - retry later, never relax the gate.
                _LOGGER.warning(
                    "python_project_cleanup_retry_failed run_id=%s error_type=%s",
                    run_id,
                    type(error).__name__,
                )
            self._retry_cursor = run_id
        return len(candidates)

    async def retry_loop(self, stop: asyncio.Event, interval_seconds: float = 60.0) -> None:
        while not stop.is_set():
            try:
                await self.retry_completed()
            except Exception as error:  # noqa: BLE001 - DB outage is not cleanup authority.
                _LOGGER.warning(
                    "python_project_cleanup_scan_failed error_type=%s", type(error).__name__
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass


def build_python_project_terminal_hook(
    repository: object,
    verifier: object,
) -> PythonProjectTerminalHook | None:
    if not isinstance(repository, RunRepository) or not isinstance(
        verifier, ProjectVerificationService
    ):
        return None
    return PythonProjectTerminalHook(repository, verifier, PythonProjectSystemdSandbox(),
                                     generated_reader=verifier._python_generated)
