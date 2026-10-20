"""Trusted completed-run cache cleanup, independent of model tool arguments."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
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

_LOGGER = logging.getLogger(__name__)
_RELEASE_MARKER = "python_project_cache_release"


class _Repository(Protocol):
    async def run_transaction(self) -> AsyncSession: ...


class _ContextReader(Protocol):
    async def python_original_context(
        self,
        tenant_id: UUID,
        run_id: UUID,
    ) -> PythonOriginalContext | None: ...


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


class PythonProjectTerminalHook:
    def __init__(
        self,
        repository: _Repository,
        verifier: _ContextReader,
        executor: _Releaser,
    ) -> None:
        self.repository = repository
        self.verifier = verifier
        self.executor = executor
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
        if status is not RunStatus.COMPLETED:
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
                != (tenant_id, run_id, RunStatus.COMPLETED.value)
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
            if not isinstance(route, dict) or _RELEASE_MARKER in route:
                return
            preparation = route.get("project_input_preparation")
            if not isinstance(preparation, dict) or (
                type(preparation.get("schema_version")) is not int
                or preparation["schema_version"] != 2
                or preparation.get("phase") != "observed"
                or _uncertain(route)
            ):
                return
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
                not isinstance(state, dict) or _uncertain(state) for state in states
            ):
                return
            project, workspace_session = authorized_workspace_scope(
                row,
                tenant_id=tenant_id,
                run_id=run_id,
            )
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
        """Retry new Python completions independently of RunService's once marker.

        The cursor is pagination only. Every candidate re-enters the locked DB
        gate; neither a remembered ID nor this query authorizes cache release.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid Python cleanup retry limit")
        query = (
            select(RunRow)
            .where(
                RunRow.status == RunStatus.COMPLETED.value,
                RunRow.routing_decision.contains(
                    {
                        "project_input_preparation": {"schema_version": 2, "phase": "observed"},
                    }
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
            candidates = tuple((row.tenant_id, row.id) for row in rows)
        if not candidates:
            self._retry_cursor = None
        for tenant_id, run_id in candidates:
            try:
                await self(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    actor_id=None,
                    status=RunStatus.COMPLETED,
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
    return PythonProjectTerminalHook(repository, verifier, PythonProjectSystemdSandbox())
