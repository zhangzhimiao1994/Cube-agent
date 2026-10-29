"""Read-only liveness proof for expired Direct action caches; never resume authority.

Caller holds the FAILED run row lock and independently checks age, leases,
approvals, outbox and provider continuations. Root still fences every native
consumer and UNKNOWN stage. Pending verify must be demonstrably unstarted.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities.generated_project_service import validate_generated_preparation
from agent_hub.db.models import (
    RunCheckpointRow,
    RunEventRow,
    RunRow,
    RuntimeArtifactRow,
    RunUsageRow,
)
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
from agent_hub.runtime.action_continuation import read_action_continuation
from agent_hub.runtime.action_direct import _prepared_context
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepository
from agent_hub.runtime.continuation_artifacts import _encode, load_continuation
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct_completion_journal import _response
from agent_hub.runtime.model_scope import validate_model_scope_artifact, validate_model_scope_parts

_MAX_EVENTS = 4096
_MAX_BYTES = 64 * 1024 * 1024
_PROVENANCE = ('logical_model', 'deployment_id', 'provider_id', 'provider_model')


def _need(condition: object) -> None:
    if not condition:
        raise ValueError('unproven expired Direct liveness')


def _sha(value: object) -> bool:
    return type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None


def _db_uuid(value: UUID) -> UUID:
    # asyncpg's UUID subclass is a valid ORM value, but the native codec rightly
    # demands an exact uuid.UUID. Normalize locally, never mutate stored scope.
    _need(isinstance(value, UUID))
    parsed = UUID(str(value))
    _need(parsed == value and str(parsed) == str(value))
    return parsed


def _amount(value: object) -> Decimal:
    _need(type(value) in (str, Decimal))
    amount = Decimal(str(value))
    _need(amount.is_finite() and amount >= 0)
    return amount


def _identity(event: RunEvent) -> tuple[str, str]:
    call, digest = event.payload.get('call_id'), event.payload.get('request_sha256')
    _need(type(call) is str and str(UUID(call)) == call and _sha(digest))
    return cast(str, call), cast(str, digest)


async def direct_action_expiry_liveness(
    *, record: RunRow, checkpoint: RuntimeCheckpoint, checkpoint_sequence: int,
    repository: ArtifactRepository, events: tuple[RunEvent, ...], usage: Sequence[RunUsageRow],
) -> bool:
    """Validate independent native domains, then ordered provenance/known costs.

    Scope UUIDs and journal timeout-normalized request hashes intentionally are
    NOT event UUIDs/request hashes. No synthetic bridge between them is claimed.
    """
    try:
        route = record.routing_decision
        _need(isinstance(route, dict))
        assert isinstance(route, dict)
        tenant, run = _db_uuid(record.tenant_id), _db_uuid(record.id)
        _need(record.status == 'failed' and record.mode == 'direct'
              and action_checkpoint_valid(checkpoint)
              and (checkpoint.tenant_id, checkpoint.run_id) == (record.tenant_id, record.id)
              and checkpoint.state['tool_name'] == 'project.verify'
              and type(checkpoint_sequence) is int and checkpoint_sequence > 0
              and 0 < len(events) <= _MAX_EVENTS and len(usage) <= _MAX_EVENTS)
        raw = await load_continuation(repository, tenant_id=tenant, run_id=run,
            reference=cast(Mapping[str, JsonValue], checkpoint.state['reference']))
        _need(isinstance(raw, Mapping))
        assert isinstance(raw, Mapping)
        snapshot = TaskContext.from_payload(json.loads(_encode(raw['context'])))
        _need((snapshot.tenant_id, snapshot.run_id, snapshot.actor_id, snapshot.request,
               str(snapshot.actor_role)) ==
              (record.tenant_id, record.id, record.actor_id, record.request, str(record.actor_role)))
        for field in ('project_id', 'workspace_session_id', 'sandbox_profile', 'requested_permissions'):
            _need(_encode(snapshot.routing_decision.get(field)) == _encode(route.get(field)))
        sources = {item.id: item for item in snapshot.artifacts}
        if raw['inspection'] is not None:
            inspection = Artifact.from_payload(json.loads(_encode(raw['inspection'])))
            _need(inspection.type == 'tool_result' and inspection.producer == 'main_agent'
                  and inspection.content.get('tool_name') == 'project.inspect')
            publications = [event for event in events if str(event.kind) == 'artifact.created'
                and event.artifact is not None and event.artifact.id == inspection.id]
            _need(len(publications) == 1)
            publication = publications[0]
            _need(publication.run_id == record.id and publication.sequence < checkpoint_sequence
                  and publication.actor == 'main_agent' and publication.artifact is not None
                  and _encode(publication.artifact.to_payload()) == _encode(inspection.to_payload())
                  and inspection.content_sha256 == inspection.recompute_content_sha256())
            _need(inspection.id not in sources or
                  _encode(sources[inspection.id].to_payload()) == _encode(inspection.to_payload()))
            sources[inspection.id] = inspection
        # Only the read-only decoder gets the proven later inspection. Preserve
        # the original snapshot when checking the stored preparation identity.
        decode_context = snapshot.model_copy(update={'checkpoint': checkpoint, 'artifacts':tuple(sources.values())})
        frame = await read_action_continuation(repository, decode_context)
        preparation = cast(Mapping[str, JsonValue], frame['preparation'])
        prepared = _prepared_context(snapshot, preparation)
        state = validate_generated_preparation(prepared.routing_decision.get('generated_project_preparation'))
        current = validate_generated_preparation(route.get('generated_project_preparation'))
        _need(state == current and state['phase'] == 'python_ready' and state['stack'] == 'python'
              and state['user_id'] == str(record.actor_id)
              and state['request_sha256'] == hashlib.sha256(record.request.encode()).hexdigest())
        _need(frame.actions and all(a.result is not None and a.result.status == 'succeeded'
              and a.result.truncated is False for a in frame.actions))
        actions = {a.request.call_id: a for a in frame.actions}
        _need(len(actions) == len(frame.actions))
        pending = cast(Mapping[str, JsonValue], frame['pending'])
        pending_request = cast(Mapping[str, JsonValue], pending['request'])
        _need(pending_request['tool_name'] == 'project.verify'
              and pending_request['call_id'] not in actions)

        starts: dict[tuple[str, str], RunEvent] = {}
        completed: list[RunEvent] = []
        costs: list[RunEvent] = []
        tool_starts: dict[str, RunEvent] = {}
        tool_ends: set[str] = set()
        parts: list[dict[str, object]] = []
        part_ids: set[str] = set()
        receipt_ids: set[str] = set()
        previous = 0
        failed = 0
        for event in events:
            _need(event.run_id == record.id and previous < event.sequence)
            previous = event.sequence
            kind = str(event.kind)
            if kind == 'model.started':
                key = _identity(event)
                _need(key not in starts and all(k[0] != key[0] for k in starts))
                starts[key] = event
            elif kind == 'model.completed':
                key = _identity(event)
                _need(key in starts and starts[key].sequence < event.sequence
                      and not any(_identity(e) == key for e in completed))
                completed.append(event)
            elif kind == 'cost.recorded':
                costs.append(event)
            elif kind in ('tool.started', 'tool.completed'):
                call = event.tool_call_id
                _need(event.sequence < checkpoint_sequence and call in actions
                      and event.tool_name == actions[call].request.tool_name
                      and event.actor == 'main_agent')
                assert call is not None
                if kind == 'tool.started':
                    _need(call not in tool_starts)
                    tool_starts[call] = event
                else:
                    _need(call in tool_starts and tool_starts[call].sequence < event.sequence
                          and call not in tool_ends and event.payload.get('status') == 'succeeded')
                    tool_ends.add(call)
            elif kind == 'artifact.created' and event.artifact is not None:
                artifact = event.artifact
                if artifact.type == 'model_attempt':
                    _need(artifact.content_sha256 == artifact.recompute_content_sha256()
                          and str(artifact.id) not in part_ids)
                    part = validate_model_scope_artifact(artifact, str(record.id))
                    _need(part['tenant_id'] == str(record.tenant_id))
                    parts.append(part)
                    part_ids.add(str(artifact.id))
            elif kind == 'model.failure_receipt':
                identity = event.payload.get('artifact_id')
                _need(type(identity) is str and identity in part_ids and identity not in receipt_ids)
                receipt_ids.add(cast(str, identity))
            elif kind == 'runtime.failed':
                _need(event.sequence > checkpoint_sequence)
                failed += 1
            elif kind.startswith(('model.', 'tool.', 'runtime.')):
                raise ValueError('nonterminal history')
        _need(failed == 1 and tool_starts.keys() == tool_ends and starts
              and len(starts) == len(completed) == len(costs) == len(usage)
              and part_ids and part_ids == receipt_ids)
        validate_model_scope_parts(parts)
        _need(len({part['scope_id'] for part in parts}) == 1)
        parts.sort(key=lambda part: cast(int, part['part_index']))
        scope_calls = [call for part in parts for call in cast(list[dict[str, object]], part['calls'])]
        _need(len(scope_calls) == len(completed))
        model = parts[0]['requested_logical_model']
        _need(type(model) is str)
        # The resume fingerprint also hashes in-memory InstructionContext, which
        # TaskContext.to_payload deliberately excludes. Do not reconstruct it
        # from current files or mistake expiry liveness for resume validation.
        # action_checkpoint_valid still checks the exact persisted CP/state hash
        # and reference; the scoped DB loader binds that CP to this FAILED run.
        for scope_call, end, cost, saved in zip(scope_calls, completed, costs, usage, strict=True):
            _need(scope_call['outcome'] == 'received')
            receipt = cast(Mapping[str, object], scope_call['receipt'])
            provenance = cast(Mapping[str, object], receipt['provenance'])
            _need(tuple(provenance.get(k) for k in _PROVENANCE) == tuple(end.payload.get(k) for k in _PROVENANCE)
                  and starts[_identity(end)].payload.get('logical_model') == model
                  and end.payload.get('requested_logical_model') == model
                  and _identity(cost) == _identity(end) and cost.sequence > end.sequence
                  and cost.payload.get('usage_known') is True
                  and cost.provider_id == end.payload.get('provider_id')
                  and _amount(cost.cost_usd) == _amount(end.payload.get('cost_usd'))
                  and (saved.tenant_id, saved.run_id, saved.sequence, saved.provider_id) ==
                      (record.tenant_id, record.id, cost.sequence, cost.provider_id)
                  and _amount(saved.cost_usd) == _amount(cost.cost_usd)
                  and _encode(saved.payload) == _encode(cost.to_payload()))
            for token_field in ('input_tokens', 'output_tokens', 'total_tokens'):
                _need(type(end.payload.get(token_field)) is int and cast(int, end.payload[token_field]) >= 0
                      and end.payload[token_field] == cost.payload.get(token_field))
        references = cast(Sequence[Mapping[str, JsonValue]], frame['models'])
        _need(0 < len(references) <= len(completed))
        for index, reference in enumerate(references):
            value = await load_continuation(repository, tenant_id=tenant,
                run_id=run, reference=reference)
            _need(isinstance(value, Mapping))
            assert isinstance(value, Mapping)
            _need(set(value) == {'position', 'request_sha256', 'response'}
                  and type(value['position']) is int and value['position'] == index and _sha(value['request_sha256']))
            response = _response(value['response'])
            end = completed[index]
            tokens = response.response.usage
            _need(tokens is not None and response.cost_usd is not None
                  and (response.logical_model, response.deployment_id, response.provider_id, response.provider_model)
                  == tuple(end.payload.get(k) for k in _PROVENANCE)
                  and _amount(response.cost_usd) == _amount(end.payload.get('cost_usd')))
            assert tokens is not None
            _need((tokens.prompt_tokens, tokens.completion_tokens, tokens.total_tokens) ==
                  tuple(end.payload.get(k) for k in ('input_tokens', 'output_tokens', 'total_tokens')))
        return True
    except Exception:  # noqa: BLE001 - invalid/private evidence never escapes; cancellation propagates.
        return False


class _ReadonlyArtifacts:
    """Same locked session, scoped SELECT + native hydration, never get_many's row-lock path."""

    def __init__(self, session: AsyncSession, tenant: UUID, run: UUID) -> None:
        self.session, self.tenant, self.run = session, tenant, run
        self.cache: dict[UUID, Artifact] = {}
        self.total = 0

    async def get_many(self, tenant_id: UUID, run_id: UUID,
                       references: tuple[ArtifactReference, ...]) -> tuple[Artifact, ...]:
        _need((tenant_id, run_id) == (self.tenant, self.run) and len(references) <= 16384)
        wanted = {ref.id for ref in references if ref.id not in self.cache}
        _need(len(self.cache) + len(wanted) <= 16384)
        identities = tuple(wanted)
        for offset in range(0, len(identities), 64):
            batch = identities[offset:offset + 64]
            rows = (await self.session.scalars(select(RuntimeArtifactRow).where(
                RuntimeArtifactRow.tenant_id == tenant_id, RuntimeArtifactRow.run_id == run_id,
                RuntimeArtifactRow.id.in_(batch)).limit(len(batch) + 1))).all()
            _need(len(rows) == len(batch))
            for row in rows:
                _need((row.tenant_id, row.run_id) == (tenant_id, run_id) and row.id in batch
                      and 0 < row.byte_size <= 1024 * 1024 and row.id not in self.cache)
                self.total += row.byte_size
                _need(self.total <= _MAX_BYTES)
                self.cache[row.id] = PostgresArtifactRepository._hydrate(row)
        result = tuple(self.cache[ref.id] for ref in references)
        _need(all(item.content_sha256 == ref.sha256 for item, ref in zip(result, references, strict=True)))
        return result


async def verified_direct_expiry(session: AsyncSession, record: RunRow) -> bool:
    """Load bounded, scoped native metadata only while caller holds the run lock."""
    try:
        tenant, run = _db_uuid(record.tenant_id), _db_uuid(record.id)
        checkpoints = (await session.scalars(select(RunCheckpointRow).where(
            RunCheckpointRow.tenant_id == tenant, RunCheckpointRow.run_id == run).limit(2))).all()
        _need(len(checkpoints) == 1)
        saved = checkpoints[0]
        _need(type(saved.payload) is dict and _sha(saved.payload.get('state_sha256')))
        cp = RuntimeCheckpoint.from_payload(saved.payload)
        _need((saved.tenant_id, saved.run_id, saved.id, saved.mode, saved.runtime_type,
               saved.runtime_version, saved.state_sha256) ==
              (tenant, run, cp.id, cp.mode.value, cp.runtime_type, cp.runtime_version, cp.state_sha256)
              and _encode(saved.state) == _encode(cp.to_payload()['state']))
        rows = (await session.scalars(select(RunEventRow).where(
            RunEventRow.tenant_id == tenant, RunEventRow.run_id == run)
            .order_by(RunEventRow.sequence).limit(_MAX_EVENTS + 1))).all()
        _need(len(rows) <= _MAX_EVENTS)
        events = []
        total = 0
        for row in rows:
            total += len(_encode(row.payload))
            _need(total <= _MAX_BYTES)
            event = RunEvent.from_payload(row.payload)
            _need((row.tenant_id, row.run_id, row.sequence, row.kind) ==
                  (tenant, run, event.sequence, str(event.kind)))
            events.append(event)
        usage = (await session.scalars(select(RunUsageRow).where(
            RunUsageRow.tenant_id == tenant, RunUsageRow.run_id == run)
            .order_by(RunUsageRow.sequence).limit(_MAX_EVENTS + 1))).all()
        return await direct_action_expiry_liveness(record=record, checkpoint=cp,
            checkpoint_sequence=saved.sequence,
            repository=cast(ArtifactRepository, _ReadonlyArtifacts(session, tenant, run)),
            events=tuple(events), usage=usage)
    except Exception:  # noqa: BLE001 - preserve cache on unavailable or inconsistent evidence.
        return False
