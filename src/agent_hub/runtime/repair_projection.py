"""Revocable worker-only correspondence with an accepted persistent repair scope.

This is projection data, never a grant. It is neither a TaskContext field nor
serializable model material, and cannot replace the normal budget/Root fences.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus
from agent_hub.runtime.contracts import (
    EventKind,
    RunEvent,
    TaskContext,
    _canonical_json_bytes,
    _freeze_object,
)

if TYPE_CHECKING:
    from agent_hub.runs.repository import RunRecord, RunRepository

_SEAL = object()
_ERROR = "repair_projection_unavailable"


def _route_hash(route: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(_freeze_object(route, name="repair routing"))
    ).hexdigest()


def _identity(context: TaskContext) -> tuple[object, ...]:
    checkpoint = context.checkpoint
    return (
        context.tenant_id,
        context.run_id,
        context.execution_token,
        context.actor_id,
        context.actor_role,
        context.mode,
        hashlib.sha256(context.request.encode()).hexdigest(),
        None if checkpoint is None else checkpoint.id,
        None if checkpoint is None else checkpoint.state_sha256,
    )


def _record_current(record: RunRecord, context: TaskContext, worker: str) -> bool:
    expiry = record.worker_lease_expires_at
    return (
        record.id == context.run_id
        and record.tenant_id == context.tenant_id
        and record.status is RunStatus.RUNNING
        and record.mode is context.mode
        and record.blocked_by_run_id is None
        and record.request == context.request
        and record.actor_id == context.actor_id
        and record.actor_role is context.actor_role
        and record.worker_id == worker
        and record.worker_lease_token == context.execution_token
        and expiry is not None
        and expiry > datetime.now(UTC)
    )


@dataclass(frozen=True, slots=True)
class _Proof:
    identity: tuple[object, ...]
    worker: str
    scope: str
    persistent_route: str
    runtime_route: str
    expires_at: datetime
    seal: object
    frame_actor: str | None = None


@dataclass(slots=True)
class _Projection:
    proof: _Proof
    repository: RunRepository
    context: TaskContext
    active: bool = True
    finished: bool = False

    async def refresh(self) -> None:
        if self.finished:
            return  # Permit natural terminal stream cleanup, never new phase use.
        try:
            record = await self.repository.get(self.context.tenant_id, self.context.run_id)
            if (
                not self.active
                or not _record_current(record, self.context, self.proof.worker)
                or _route_hash(record.routing_decision or {}) != self.proof.persistent_route
            ):
                raise ValueError(_ERROR)
            assert record.worker_lease_expires_at is not None
            # Heartbeat renewal can refresh only this SAME persisted owner/token;
            # it never changes an original pool deadline, route or accepted scope.
            self.proof = replace(self.proof, expires_at=record.worker_lease_expires_at)
        except (LookupError, TypeError, ValueError):
            self.active = False
            raise ValueError(_ERROR) from None

    def observe(self, event: RunEvent) -> None:
        if event.kind in {
            EventKind.RUNTIME_COMPLETED,
            EventKind.RUNTIME_FAILED,
            EventKind.RUNTIME_CANCELLED,
        }:
            self.finished = True


_CURRENT: ContextVar[_Projection | None] = ContextVar("private_repair_projection", default=None)


async def _prepare_projection(
    repository: RunRepository | None,
    context: TaskContext,
    worker: str | None,
) -> _Projection | None:
    # Deferred: runs.__init__ imports RunService/worker_execution.
    from agent_hub.runs.repository import RunRepository
    from agent_hub.runtime.autogen.project_repair_phase import authority_digest, selected

    if not selected(context) or not isinstance(repository, RunRepository) or worker is None:
        return None
    if (
        type(context.execution_token) is not UUID
        or type(context.actor_id) is not UUID
        or type(context.actor_role) is not Role
        or context.actor_role is Role.VIEWER
    ):
        raise ValueError(_ERROR)
    checked = await repository.load_same_run_repair_continuity(context.tenant_id, context.run_id)
    # Read current identity/lease AFTER the accepted-checkpoint proof read.
    record = await repository.get(context.tenant_id, context.run_id)
    route = record.routing_decision or {}
    proposal = route.get("repair_proposal")
    scope = proposal.get("automatic_scope_sha256") if isinstance(proposal, Mapping) else None
    if (
        not _record_current(record, context, worker)
        or checked is None
        or not checked.matches_routing(route)
        or not checked.matches(context)
        or type(scope) is not str
        or authority_digest(route) != scope
    ):
        raise ValueError(_ERROR)
    assert record.worker_lease_expires_at is not None
    return _Projection(
        _Proof(
            _identity(context),
            worker,
            scope,
            _route_hash(route),
            _route_hash(context.routing_decision),
            record.worker_lease_expires_at,
            _SEAL,
        ),
        repository,
        context.validated_internal_clone(),
    )


@contextmanager
def _bind_projection(projection: _Projection | None) -> Iterator[None]:
    from agent_hub.capabilities.execution_authority import get_verification_authority

    if projection is not None:
        context = projection.context
        assert context.actor_id is not None and context.actor_role is not None
        authority = get_verification_authority(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            user_id=context.actor_id,
            role=context.actor_role,
        )
        projection.proof = replace(projection.proof, frame_actor=authority.actor)
    token = _CURRENT.set(projection)  # Clear inherited data even when this run has none.
    try:
        yield
    finally:
        if projection is not None:
            projection.active = False  # Escaped child Contexts share this revocation.
        _CURRENT.reset(token)


def _matches_projection(context: TaskContext, expected: object) -> bool:
    from agent_hub.capabilities.execution_authority import (
        ExecutionAuthorityError,
        _trusted_review_context,
        get_verification_authority,
    )

    projection = _CURRENT.get()
    if (
        type(projection) is not _Projection
        or type(projection.proof) is not _Proof
        or not projection.active
        or projection.finished
        or projection.proof.seal is not _SEAL
        or projection.proof.scope != expected
        or projection.proof.expires_at <= datetime.now(UTC)
        or _identity(context) != projection.proof.identity
        or _route_hash(context.routing_decision) != projection.proof.runtime_route
        or context.actor_id is None
        or context.actor_role is None
    ):
        return False
    try:
        trusted, worker = _trusted_review_context(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            user_id=context.actor_id,
            role=context.actor_role,
        )
        authority = get_verification_authority(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            user_id=context.actor_id,
            role=context.actor_role,
        )
        return (
            authority.actor == projection.proof.frame_actor
            and worker == projection.proof.worker
            and _identity(trusted) == projection.proof.identity
            and _route_hash(trusted.routing_decision) == projection.proof.runtime_route
        )
    except (ExecutionAuthorityError, TypeError, ValueError):
        return False


async def _projection_events(
    stream: AsyncIterator[RunEvent],
    projection: _Projection,
) -> AsyncIterator[RunEvent]:
    try:
        while True:
            await projection.refresh()
            try:
                event = await anext(stream)
            except StopAsyncIteration:
                return
            projection.observe(event)
            yield event
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
