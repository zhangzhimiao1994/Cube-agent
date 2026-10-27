"""Freeze reviewed conversation artifacts before any model, without granting authority."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.continuation_artifacts import (
    _checkpoint,
    _encode,
    load_continuation,
    save_continuation,
)
from agent_hub.runtime.contracts import Artifact, JsonValue, RuntimeCheckpoint

VERSION = 'conversation_input_snapshot_version'
STATE = 'conversation_input_snapshot'
ERROR = 'conversation inputs are unavailable or changed'
_ROUTE = ('conversation_id', 'project_id', 'workspace_session_id', 'sandbox_profile',
          'permission_mode', 'execution_backend', 'requested_permissions', 'replace_workspace_files')
_ARTIFACT_FIELDS = {'id', 'version', 'type', 'producer', 'content', 'source_ids', 'content_sha256', 'provenance'}
_UNSET = object()


@dataclass(frozen=True, slots=True)
class ConversationInputs:
    artifacts: tuple[Artifact, ...]
    snapshot: Mapping[str, JsonValue] | None


def _need(value: object) -> None:
    if not value:
        raise RunConflict(ERROR)


def _plain(value: object) -> object:
    # Continuation decoding returns MappingProxy/tuples; strict contract parsing
    # requires ordinary JSON containers. Keep the codec's bounded canonical form.
    return json.loads(_encode(value))


def _route_scope(route: Mapping[str, object]) -> dict[str, object]:
    for key in _ROUTE:
        value = route.get(key)
        if key == 'requested_permissions':
            _need(isinstance(value, (list, tuple))
                  and all(type(p) is str for p in value) and len(value) == len(set(value))
                  and 'workspace.read' in value)
        elif key == 'replace_workspace_files':
            _need(value is None or type(value) is bool)
        else:
            _need(value is None or type(value) is str)
    return cast(dict[str, object], _plain({key: route.get(key) for key in _ROUTE}))


def _artifacts(value: object) -> tuple[Artifact, ...]:
    _need(type(value) is list)
    assert isinstance(value, list)
    result: list[Artifact] = []
    seen: set[UUID] = set()
    for payload in value:
        _need(type(payload) is dict and set(payload) == _ARTIFACT_FIELDS)
        _need(type(payload['id']) is str and type(payload['content_sha256']) is str
              and re.fullmatch('[a-f0-9]{64}', payload['content_sha256']) is not None)
        artifact = Artifact.from_payload(payload)
        _need(str(artifact.id) == payload['id'] and artifact.id not in seen
              and artifact.recompute_content_sha256() == payload['content_sha256']
              and _encode(artifact.to_payload()) == _encode(payload))
        seen.add(artifact.id)
        result.append(artifact)
    return tuple(result)


def _saved_state(value: object, scope_sha256: str) -> Mapping[str, JsonValue] | None:
    if value is None:
        return None
    _need(isinstance(value, Mapping)
          and set(value) == {'schema_version', 'scope_sha256', 'reference'})
    assert isinstance(value, Mapping)
    _need(type(value['schema_version']) is int and value['schema_version'] == 1
          and type(value['scope_sha256']) is str and value['scope_sha256'] == scope_sha256
          and isinstance(value['reference'], Mapping))
    _checkpoint(value['reference'])
    return cast(Mapping[str, JsonValue], _plain(value))


async def conversation_inputs(
    repository: RunRepository,
    artifacts: ArtifactRepository,
    *,
    tenant_id: UUID,
    run_id: UUID,
    actor_id: UUID | None,
    actor_role: Role | None,
    mode: TaskMode,
    worker_id: str,
    execution_token: UUID | None,
    current_request: str,
    routing_decision: Mapping[str, object],
    checkpoint: RuntimeCheckpoint | None,
    builder: Callable[[], Awaitable[tuple[Artifact, ...]]],
) -> ConversationInputs:
    """Caller guarantees the first capture precedes every model and accounts for time.

    Persist only existing, reviewed history through the private continuation
    repository, never a public artifact event or a user workspace write. The
    returned snapshot must be merged into the first TaskContext by the caller.
    Worker identity/lease are checked afresh, never frozen as resume authority.
    """
    if VERSION not in routing_decision:
        return ConversationInputs(await builder(), None)
    try:
        _need(type(routing_decision[VERSION]) is int and routing_decision[VERSION] == 1
              and type(tenant_id) is UUID and type(run_id) is UUID and type(actor_id) is UUID
              and type(actor_role) is Role and type(mode) is TaskMode and mode is not TaskMode.AUTO
              and type(execution_token) is UUID and type(worker_id) is str and bool(worker_id)
              and type(current_request) is str)
        assert actor_role is not None
        route_scope = _route_scope(routing_decision)
        binding = {'tenant_id': str(tenant_id), 'run_id': str(run_id), 'actor_id': str(actor_id),
            'actor_role': actor_role.value, 'mode': mode.value,
            'request_sha256': hashlib.sha256(current_request.encode('utf-8')).hexdigest(),
            'routing': route_scope}
        scope_sha256 = hashlib.sha256(_encode(binding)).hexdigest()
        supplied_state = _plain(routing_decision.get(STATE))
        if checkpoint is not None:
            _need(type(checkpoint) is RuntimeCheckpoint)
            checked = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
            _need(checked.tenant_id == tenant_id and checked.run_id == run_id and checked.mode is mode)

        async def state_io(
            *, expected: object = _UNSET, replacement: Mapping[str, JsonValue] | None = None,
        ) -> Mapping[str, JsonValue] | None:
            async with await repository.run_transaction() as session, session.begin():
                row = await repository.get_for_update(session, run_id)
                record = RunRepository._record(row)
                route = record.routing_decision or {}
                _need(record.id == run_id and record.tenant_id == tenant_id and record.actor_id == actor_id
                    and record.actor_role is actor_role and record.mode is mode
                    and record.request == current_request and record.status is RunStatus.RUNNING
                    and record.worker_id == worker_id and record.worker_lease_token == execution_token
                    and record.worker_lease_expires_at is not None
                    and record.worker_lease_expires_at > datetime.now(UTC)
                    and type(route.get(VERSION)) is int and route[VERSION] == 1
                    and _encode(_route_scope(route)) == _encode(route_scope))
                state = _saved_state(route.get(STATE), scope_sha256)
                if supplied_state is not None:
                    _need(_encode(supplied_state) == _encode(state))
                if expected is not _UNSET:
                    _need(_encode(state) == _encode(expected))
                if replacement is not None:
                    _need(state is None)
                    validated = _saved_state(replacement, scope_sha256)
                    row.routing_decision = {**route, STATE: _plain(validated)}
                    row.version += 1
                    return validated
                return state

        async def load(state: Mapping[str, JsonValue]) -> tuple[Artifact, ...]:
            value = _plain(await load_continuation(artifacts, tenant_id=tenant_id, run_id=run_id,
                reference=cast(Mapping[str, JsonValue], state['reference'])))
            _need(type(value) is dict and set(value) == {'schema_version', 'kind', 'scope', 'artifacts'})
            assert isinstance(value, dict)
            _need(type(value['schema_version']) is int and value['schema_version'] == 1
                  and value['kind'] == 'runtime_conversation_inputs'
                  and _encode(value['scope']) == _encode(binding))
            return _artifacts(value['artifacts'])

        state = await state_io()
        if state is not None:
            restored = await load(state)
            state = await state_io(expected=state)  # Loads can outlive the active lease.
            return ConversationInputs(restored, state)
        _need(checkpoint is None)
        built = await builder()
        _need(type(built) is tuple and all(type(item) is Artifact and type(item.id) is UUID for item in built))
        captured = _artifacts(_plain([item.to_payload() for item in built]))
        await state_io(expected=None)
        reference = await save_continuation(artifacts, tenant_id=tenant_id, run_id=run_id,
            value=cast(JsonValue, {'schema_version': 1, 'kind': 'runtime_conversation_inputs',
                                  'scope': binding, 'artifacts': [a.to_payload() for a in captured]}))
        replacement = _saved_state({'schema_version': 1, 'scope_sha256': scope_sha256,
                                    'reference': reference}, scope_sha256)
        assert replacement is not None
        captured = await load(replacement)
        saved = await state_io(expected=None, replacement=replacement)
        return ConversationInputs(captured, saved)
    except Exception as error:  # noqa: BLE001 - discard private builder, codec and parse errors.
        error.__traceback__ = None
        error.__context__ = None
        error.__cause__ = None
        del error
    # Raise outside the handler: private source exceptions must not survive as context.
    raise RunConflict(ERROR) from None


__all__ = ['STATE', 'VERSION', 'ConversationInputs', 'conversation_inputs']
