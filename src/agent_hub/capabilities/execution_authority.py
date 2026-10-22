"""Internal worker identity binding, not a tool API or a durable authorization grant.

Only the trusted worker driver may bind its own TaskContext and worker_id. Never
bind from model/tool arguments or a token read from the current database row.
An optional RunRecord must come from the caller's trusted repository read. Its
preparation state can refresh stale routing, but never its worker/token identity.
The receipt DAO must still fence the bound lease against the locked RunRow, and
verification code must still check policy, approvals, source and execution proof.

Preparation/supply must stabilize before obtaining authority for DAO.begin.
Await child tasks inside the binding: exit revokes inherited access as well as
resetting the local ContextVar. RunService's worker stream owns that lifecycle;
the binding alone never proves a successful project verification.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.project_verification_receipts import VerificationAuthority
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import TaskContext, _canonical_json_bytes, _freeze_object

_ERROR = "execution_authority_unavailable"
_ACTOR = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_STABLE_ROUTE_KEYS = (
    "project_id", "workspace_session_id", "sandbox_profile", "permission_mode", "execution_backend",
    "requested_permissions", "runtime_timeout_seconds", "project_input_contract_version",
    "project_input", "generated_project_contract_version",
)


class ExecutionAuthorityError(RuntimeError):
    """No matching trusted worker binding is available to this execution context."""


@dataclass(slots=True)
class _Binding:
    context: TaskContext = field(repr=False)
    worker_id: str
    actor: str
    active: bool = True


_BINDING: ContextVar[_Binding | None] = ContextVar("trusted_execution_authority", default=None)


def _stable_route(route: Mapping[str, object]) -> bytes:
    return _canonical_json_bytes(_freeze_object(
        {key: route[key] for key in _STABLE_ROUTE_KEYS if key in route},
        name="execution authority project scope",
    ))


@contextmanager
def bind_execution_authority(
    context: TaskContext, *, worker_id: str, actor: str = "main_agent",
) -> Iterator[None]:
    """Snapshot trusted worker input; nest safely and revoke inherited access on exit."""
    try:
        if type(context) is not TaskContext:
            raise TypeError(_ERROR)
        snapshot = context.validated_internal_clone()
        if (not all(type(value) is UUID for value in (
                snapshot.tenant_id, snapshot.run_id, snapshot.actor_id, snapshot.execution_token))
                or type(snapshot.actor_role) is not Role or snapshot.actor_role is Role.VIEWER
                or type(worker_id) is not str or not 0 < len(worker_id) <= 128
                or worker_id != worker_id.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in worker_id)
                or type(actor) is not str or _ACTOR.fullmatch(actor) is None):
            raise ValueError(_ERROR)
    except (ValueError, TypeError, RuntimeError, AttributeError, KeyError):
        raise ExecutionAuthorityError(_ERROR) from None
    binding = _Binding(snapshot, worker_id, actor)
    token = _BINDING.set(binding)
    try:
        yield
    finally:
        binding.active = False
        _BINDING.reset(token)


def get_verification_authority(
    *, tenant_id: UUID, run_id: UUID, user_id: UUID, role: Role,
    current_record: RunRecord | None = None,
) -> VerificationAuthority:
    """Resolve exact caller identity, optionally refreshing only trusted preparation routing.

    Actor, request, mode, worker_id and execution_token always originate from the
    binding. Even a newer RunRecord lease is never adopted; the DAO will reject
    stale ownership. No binding means failure, including when a record is given.
    """
    binding = _BINDING.get()
    if binding is None or not binding.active:
        raise ExecutionAuthorityError(_ERROR)
    context = binding.context
    try:
        if (not all(type(value) is UUID for value in (tenant_id, run_id, user_id))
                or type(role) is not Role or role is not context.actor_role
                or (tenant_id, run_id, user_id) != (
                    context.tenant_id, context.run_id, context.actor_id)):
            raise ValueError(_ERROR)
        if current_record is not None:
            if (type(current_record) is not RunRecord
                    or (current_record.tenant_id, current_record.id, current_record.actor_id)
                        != (context.tenant_id, context.run_id, context.actor_id)
                    or type(current_record.actor_role) is not Role
                    or current_record.actor_role is not context.actor_role
                    or type(current_record.mode) is not TaskMode or current_record.mode is not context.mode
                    or type(current_record.request) is not str or current_record.request != context.request
                    or current_record.status is not RunStatus.RUNNING):
                raise ValueError(_ERROR)
            route = _freeze_object(current_record.routing_decision, name="current execution routing")
            if _stable_route(route) != _stable_route(context.routing_decision):
                raise ValueError(_ERROR)
            context = context.model_copy(update={"routing_decision": route})
        return VerificationAuthority.from_context(
            context, worker_id=binding.worker_id, actor=binding.actor,
        )
    except (ValueError, TypeError, RuntimeError, AttributeError, KeyError):
        raise ExecutionAuthorityError(_ERROR) from None
