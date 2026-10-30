"""Private evidence of authenticated API input, never a permission grant.

Only the authenticated create/queue/edit boundaries capture live principals.
Internal callers default to no evidence. Persisted originals stay separate from
mutable run requests; later admission must intersect current identity and policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TypedDict
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import RunAuthenticatedInputRow, RunInputAuthorizationRow, RunRow

_SEAL = object()
_INVALID = 'authenticated input binding is invalid'


@dataclass(frozen=True, slots=True)
class _AuthenticatedInput:
    tenant_id: UUID
    actor_id: UUID
    actor_role: Role
    turn_id: UUID
    message: str = field(repr=False)
    message_sha256: str
    input_scope: str = field(repr=False)
    scope_sha256: str
    _seal: object = field(repr=False, compare=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _canonical_scope(scope: dict[str, object]) -> str:
    return json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _capture_authenticated_input(
    principal: AuthenticatedPrincipal, message: str, scope: dict[str, object],
) -> _AuthenticatedInput:
    try:
        if (type(principal) is not AuthenticatedPrincipal
                or type(principal.tenant_id) is not UUID or type(principal.user_id) is not UUID
                or type(principal.role) is not Role or type(message) is not str
                or not message.strip() or type(scope) is not dict):
            raise ValueError
        canonical = _canonical_scope(scope)
        return _AuthenticatedInput(principal.tenant_id, principal.user_id, principal.role,
                                   uuid4(), message, _sha256(message), canonical,
                                   _sha256(canonical), _SEAL)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError(_INVALID) from None


def _validate_authenticated_input(
    evidence: _AuthenticatedInput, tenant_id: UUID, actor_id: UUID | None,
    actor_role: Role | None, message: str,
) -> None:
    try:
        if (type(evidence) is not _AuthenticatedInput or evidence._seal is not _SEAL
                or type(evidence.turn_id) is not UUID
                or type(evidence.tenant_id) is not UUID or type(evidence.actor_id) is not UUID
                or type(evidence.actor_role) is not Role
                or (evidence.tenant_id, evidence.actor_id, evidence.actor_role, evidence.message)
                != (tenant_id, actor_id, actor_role, message)
                or not evidence.message.strip()
                or evidence.message_sha256 != _sha256(evidence.message)
                or evidence.scope_sha256 != _sha256(evidence.input_scope)):
            raise ValueError
        scope = json.loads(evidence.input_scope)
        if type(scope) is not dict or _canonical_scope(scope) != evidence.input_scope:
            raise ValueError
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise ValueError(_INVALID) from None


async def _persist_authenticated_input(
    session: AsyncSession, run: RunRow, evidence: _AuthenticatedInput,
    *, submitted_message: str | None = None,
) -> None:
    _validate_authenticated_input(evidence, run.tenant_id, run.actor_id,
                                  None if run.actor_role is None else Role(run.actor_role),
                                  run.request if submitted_message is None else submitted_message)
    session.add(RunAuthenticatedInputRow(
        turn_id=evidence.turn_id, tenant_id=evidence.tenant_id, run_id=run.id,
        actor_id=evidence.actor_id, actor_role=evidence.actor_role.value,
        message=evidence.message, message_sha256=evidence.message_sha256,
        input_scope=evidence.input_scope, scope_sha256=evidence.scope_sha256))
    await session.flush()
    link = await session.get(RunInputAuthorizationRow, run.id)
    if link is None:
        session.add(RunInputAuthorizationRow(run_id=run.id, tenant_id=run.tenant_id,
                                             turn_id=evidence.turn_id))
    else:
        link.turn_id = evidence.turn_id
    await session.flush()


def _validated_persisted_input(
    run: RunRow, link: RunInputAuthorizationRow | None, turn: RunAuthenticatedInputRow | None,
) -> _AuthenticatedInput | None:
    if link is None or turn is None or run.actor_role is None:
        return None
    try:
        if ((link.run_id, link.tenant_id, link.turn_id)
                != (run.id, run.tenant_id, turn.turn_id)
                or (turn.run_id, turn.tenant_id, turn.actor_id, turn.actor_role)
                != (run.id, run.tenant_id, run.actor_id, run.actor_role)):
            return None
        # Normalize driver UUID subclasses only at the database boundary.
        evidence = _AuthenticatedInput(
            UUID(str(turn.tenant_id)), UUID(str(turn.actor_id)), Role(turn.actor_role),
            UUID(str(turn.turn_id)), turn.message, turn.message_sha256,
            turn.input_scope, turn.scope_sha256, _SEAL)
        _validate_authenticated_input(evidence, run.tenant_id, run.actor_id,
                                      Role(run.actor_role), turn.message)
        return evidence
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        return None


class _AuthenticatedInputOptions(TypedDict, total=False):
    authenticated_input: _AuthenticatedInput


def _authenticated_input_options(evidence: _AuthenticatedInput | None) -> _AuthenticatedInputOptions:
    # Preserve legacy call signatures for evidence-free internal callers.
    return {} if evidence is None else {'authenticated_input': evidence}
