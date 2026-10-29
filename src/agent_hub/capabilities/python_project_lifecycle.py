"""Trusted terminal cache cleanup, independent of model tool arguments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_project_service import PythonOriginalContext
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.db.models import (
    ProviderContinuationRow,
    RunApprovalRow,
    RunCheckpointRow,
    RunEventRow,
    RunOutboxRow,
    RunRow,
    RuntimeArtifactRow,
    RunUsageRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint
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
_FAILED_VERSION = 'terminal_failed_python_environment_cleanup_version'
_MAX_RETENTION_SECONDS = 86400
_CAPACITY_TIMEOUT_SECONDS = 30.0
_PRE_MODEL_EVENT_KINDS = (
    'context.loaded', 'harness.started', 'runtime.failed', 'repair.classified', 'terminal.notified',
)


class _Repository(Protocol):
    async def run_transaction(self) -> AsyncSession: ...


class _ContextReader(Protocol):
    async def python_expired_environment_invocation(
        self, tenant_id: UUID, run_id: UUID,
    ) -> PythonProjectInvocation | None: ...

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
    async def release_expired(self, invocation: PythonProjectInvocation) -> bool: ...

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


def _terminal_model_history_only(
    value: object, run_id: UUID, *, verified_failed: frozenset[str] = frozenset(),
    verified_terminal: bool = False,
) -> bool:
    """Recognize only the opted-in terminal model snapshot paths; never rewrite them.

    Caller already holds the terminal run lock and checks the persisted policy,
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
    if len(allowed_paths) == 1 and not verified_terminal:
        return False
    for key, row in value['tools'].items():
        if (type(key) is not str or re.fullmatch('[a-f0-9]{64}', key) is None
                or type(row) is not dict or type(row.get('status')) is not str
                or (row['status'] not in {'succeeded', 'rejected'}
                    and not (row['status'] == 'failed' and key in verified_failed))
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


def _bound_checkpoint(payload: object, tenant: UUID, run: UUID) -> RuntimeCheckpoint:
    # The DTO intentionally auto-fills an absent digest for builders. Cleanup must
    # instead demand the original persisted hash before invoking that constructor.
    if (type(payload) is not dict or type(payload.get('state_sha256')) is not str
            or re.fullmatch('[a-f0-9]{64}', payload['state_sha256']) is None):
        raise ValueError('unproven checkpoint')
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    if checkpoint.tenant_id != tenant or checkpoint.run_id != run:
        raise ValueError('foreign checkpoint')
    return checkpoint


async def _verified_failed_history(
    session: AsyncSession, state: dict[str, object], tenant: UUID, run: UUID,
) -> bool:
    """Metadata-only proof; never hydrate a runtime, restore it, or acquire retry authority."""
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, _execution_failure_result

    try:
        rows = (await session.scalars(select(RunCheckpointRow).where(
            RunCheckpointRow.tenant_id == tenant, RunCheckpointRow.run_id == run,
        ).order_by(RunCheckpointRow.sequence.desc()).limit(1))).all()
        if len(rows) != 1:
            return False
        saved = rows[0]
        outer = _bound_checkpoint(saved.payload, tenant, run)
        if ((saved.tenant_id, saved.run_id, saved.id, saved.runtime_type, saved.runtime_version, saved.mode)
                != (tenant, run, outer.id, outer.runtime_type, outer.runtime_version, outer.mode.value)
                or saved.state != state or outer.to_payload()['state'] != state
                or saved.state_sha256 != outer.state_sha256):
            return False
        child = outer
        if outer.runtime_type == 'hybrid':
            required = {
                'plan_digest', 'artifact_registry', 'output_artifact_registry', 'next_sequence',
                'next_stage', 'terminal', 'reason', 'remaining_token_budget', 'token_budget_grant',
                'token_budget_spent', 'remaining_timeout_seconds', 'remaining_absolute_timeout_seconds',
                'timeout_progress_units', 'last_child_progress_fingerprint', 'child_checkpoint',
            }
            if (outer.runtime_version != '3' or outer.mode is not TaskMode.HYBRID
                    or not required <= state.keys()
                    or state.keys() - required - {'terminal_diagnostic', 'engineering_hybrid_report_version'}
                    or ('engineering_hybrid_report_version' in state and (
                        type(state['engineering_hybrid_report_version']) is not int
                        or state['engineering_hybrid_report_version'] != 1))
                    or type(state['next_stage']) is not int or state['next_stage'] not in (0, 1)
                    or state['terminal'] is not False or state['reason'] is not None
                    or _uncertain({key: value for key, value in state.items() if key != 'child_checkpoint'})):
                return False
            child = _bound_checkpoint(state['child_checkpoint'], tenant, run)
        if child.runtime_type != 'crew' or child.runtime_version != '11' or child.mode is not TaskMode.DISPATCH:
            return False  # One supported wrapper only; never recursively discover a convenient ledger.
        body = cast(dict[str, object], child.to_payload()['state'])
        models, tools, registry = body.get('models'), body.get('tools'), body.get('artifact_registry')
        if (type(models) is not dict or not models or type(tools) is not dict
                or type(registry) is not dict or len(models) + len(tools) > 16_384):
            return False
        expected: dict[str, str] = {}
        model_rows: dict[str, Mapping[str, object]] = {}
        private = child.state.get('rejected_outputs')
        if not isinstance(private, Mapping):
            return False
        rejected_keys = set()
        for key, model in models.items():
            if type(model) is not dict or model.get('status') not in {'succeeded', 'rejected'}:
                return False
            if model['status'] == 'rejected':
                rejected_keys.add(key)
                receipt = private.get(key)
                if (not isinstance(receipt, Mapping) or receipt.get('disposition') != 'rejected'
                        or receipt.get('text_sha256') != model.get('sha256')
                        or receipt.get('provenance') != model.get('provenance')
                        or type(receipt.get('version')) is not int or receipt['version'] != 1
                        or model.get('artifact_id') is not None):
                    return False
                rejected = CrewDispatchRuntime._rejected_from_private(receipt)
                evidence = rejected.evidence
                if (evidence is None or evidence.usage_status != 'known' or evidence.usage is None):
                    return False
                if evidence.status == 'completed':
                    if model.get('failure_reason') != 'structured output rejected':
                        return False
                elif evidence.status == 'incomplete':
                    # A confirmed incomplete response has returned, unlike an
                    # uncertain transport/model outcome. Its rejected batch did
                    # not execute. The complete succeeded-call/tool receipt
                    # reconciliation below still fences every actual side effect.
                    if (evidence.reason != 'incomplete' or evidence.final_text is not None
                            or model.get('failure_reason') not in {
                                'structured output rejected', 'model response incomplete',
                                'model post-tool incomplete', 'model post-tool growth incomplete',
                                'model initial-chain incomplete',
                            }):
                        return False
                else:
                    return False
            else:
                artifact_id, digest = model.get('artifact_id'), model.get('sha256')
                if type(artifact_id) is not str or type(digest) is not str or artifact_id in expected:
                    return False
                expected[artifact_id] = digest
                model_rows[artifact_id] = model
        if set(private) != rejected_keys:
            return False
        tool_fields = {'status', 'step_id', 'attempt', 'round', 'tool_index', 'name', 'arguments_sha256',
                       'trigger_model_artifact_id', 'replay_safe', 'artifact_id', 'sha256'}
        for tool in tools.values():
            if (type(tool) is not dict or set(tool) != tool_fields
                    or tool['status'] not in {'succeeded', 'rejected', 'failed'}
                    or type(tool['artifact_id']) is not str or type(tool['sha256']) is not str
                    or tool['artifact_id'] in expected):
                return False
            expected[tool['artifact_id']] = tool['sha256']
        if any(registry.get(key) != digest for key, digest in expected.items()):
            return False
        ids = tuple(UUID(key) for key in expected)
        artifacts: dict[str, Artifact] = {}
        total = 0
        for offset in range(0, len(ids), 64):
            batch = ids[offset:offset + 64]
            stored = (await session.scalars(select(RuntimeArtifactRow).where(
                RuntimeArtifactRow.tenant_id == tenant, RuntimeArtifactRow.run_id == run,
                RuntimeArtifactRow.id.in_(batch),
            ).limit(len(batch) + 1))).all()
            if len(stored) != len(batch):
                return False
            for item in stored:
                if ((item.tenant_id, item.run_id) != (tenant, run) or item.id not in batch
                        or type(item.byte_size) is not int or not 0 < item.byte_size <= 1_048_576):
                    return False
                total += item.byte_size
                if total > 64 * 1024 * 1024 or str(item.id) in artifacts:
                    return False
                artifact = PostgresArtifactRepository._hydrate(item)
                if expected.get(str(artifact.id)) != artifact.content_sha256:
                    return False
                artifacts[str(artifact.id)] = artifact
        if set(artifacts) != set(expected):
            return False
        calls: dict[tuple[object, ...], tuple[object, str, str]] = {}
        for artifact_id, model in model_rows.items():
            artifact = artifacts[artifact_id]
            if (artifact.producer != model.get('actor') or artifact.provenance is None
                    or artifact.provenance.to_payload() != model.get('provenance')):
                return False
            completion = CrewDispatchRuntime._completion_from_model_artifact(artifact)
            if completion.response.usage is None:
                return False
            for index, call in enumerate(completion.response.tool_calls):
                if model['purpose'] != 'step':
                    return False
                arguments = json.dumps(dict(call.arguments), default=dict, ensure_ascii=False,
                    allow_nan=False, sort_keys=True, separators=(',', ':'))
                coordinate = (model['step_id'], model['attempt'], model['call_index'], index)
                if coordinate in calls:
                    return False
                calls[coordinate] = (call.name, hashlib.sha256(arguments.encode()).hexdigest(), artifact_id)
        failed_keys = set()
        consumed = set()
        for key, tool in tools.items():
            if (any(type(tool[name]) is not int or not 0 <= tool[name] <= 2**31 - 1
                    for name in ('attempt', 'round', 'tool_index')) or type(tool['replay_safe']) is not bool):
                return False
            coordinate = (tool['step_id'], tool['attempt'], tool['round'], tool['tool_index'])
            if coordinate in consumed or calls.get(coordinate) != (
                tool['name'], tool['arguments_sha256'], tool['trigger_model_artifact_id'],
            ):
                return False
            consumed.add(coordinate)
            base = f"{run}:{tool['step_id']}:{tool['attempt']}:{tool['round']}:{tool['tool_index']}:{tool['name']}:{tool['arguments_sha256']}"
            if key not in {hashlib.sha256(base.encode()).hexdigest(), hashlib.sha256(
                f"{base}:trigger:{tool['trigger_model_artifact_id']}".encode()).hexdigest()}:
                return False
            artifact = artifacts[tool['artifact_id']]
            trigger = artifacts[tool['trigger_model_artifact_id']]
            if (artifact.type != 'tool_result' or artifact.producer != trigger.producer
                    or artifact.source_ids != (str(trigger.id),)):
                return False
            if tool['status'] == 'failed':
                result = artifact.content.get('result')
                if (artifact.content.get('tool_name') != tool['name']
                        or artifact.content.get('arguments_sha256') != tool['arguments_sha256']
                        or not isinstance(result, Mapping)
                        or _execution_failure_result(result, tool_name=tool['name']) != result):
                    return False
                failed_keys.add(key)
        if set(calls) != consumed:
            return False  # Missing even one batch member is not a fully terminal tool history.
        return _terminal_model_history_only(body, run, verified_failed=frozenset(failed_keys),
                                            verified_terminal=True)
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError, RecursionError):
        return False


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
        self._capacity_cursor: UUID | None = None
        self._capacity_lock = asyncio.Lock()

    async def _release_expired_pre_model(
        self, tenant: UUID, run: UUID, cutoff: datetime,
    ) -> bool:
        async with (
            asyncio.timeout(_CAPACITY_TIMEOUT_SECONDS),
            await self.repository.run_transaction() as session,
            session.begin(),
        ):
            row = await session.scalar(select(RunRow).where(
                RunRow.tenant_id == tenant, RunRow.id == run,
            ).with_for_update())
            if (row is None or (row.tenant_id, row.id, row.status) != (tenant, run, 'failed')
                    or not isinstance(row.updated_at, datetime) or row.updated_at.tzinfo is None
                    or row.updated_at > cutoff or any(value is not None for value in (
                        row.worker_id, row.worker_lease_token,
                        row.worker_lease_expires_at, row.worker_heartbeat_at))):
                return False
            route = row.routing_decision
            if (type(route) is not dict or _RELEASE_MARKER in route
                    or route.get('source') == 'self_repair'
                    or any(not isinstance(key, str) or key.startswith('self_repair_') for key in route)):
                return False
            generated = bool({'generated_project_contract_version', 'generated_project_preparation'} & route.keys())
            state: Mapping[str, object] | None
            if generated:
                state = _generated_preparation(route)
                if state is None or row.mode != 'direct' or _uncertain(route):
                    return False
            else:
                original_state = route.get('project_input_preparation')
                if not isinstance(original_state, dict):
                    return False
                state = original_state
                if (type(state) is not dict or set(state) != {
                    'schema_version', 'phase', 'execution_id', 'reference', 'python_recipe'}
                    or type(state['schema_version']) is not int or state['schema_version'] != 2
                    or state['phase'] != 'executing'):
                    return False
                # This exact producer phase is not an execution proof. Any
                # other UNKNOWN/partial marker remains a veto.
                check_route = {**route, 'project_input_preparation': {**state, 'phase': 'captured'}}
                if _uncertain(check_route):
                    return False
            assert state is not None
            probes = [
                select(ProviderContinuationRow.id).where(ProviderContinuationRow.tenant_id == tenant,
                                                        ProviderContinuationRow.run_id == run),
                select(RunApprovalRow.id).where(RunApprovalRow.tenant_id == tenant,
                    RunApprovalRow.run_id == run, RunApprovalRow.status.not_in(('approved', 'rejected', 'expired'))),
                select(RunOutboxRow.id).where(RunOutboxRow.tenant_id == tenant,
                    RunOutboxRow.run_id == run, RunOutboxRow.delivered.is_(False)),
            ]
            if not generated:
                probes += [
                    select(RunCheckpointRow.id).where(RunCheckpointRow.tenant_id == tenant,
                                                     RunCheckpointRow.run_id == run),
                    select(RunUsageRow.id).where(RunUsageRow.tenant_id == tenant, RunUsageRow.run_id == run),
                    select(RunEventRow.id).where(RunEventRow.tenant_id == tenant, RunEventRow.run_id == run,
                                                 RunEventRow.kind.not_in(_PRE_MODEL_EVENT_KINDS)),
                ]
            for query in probes:
                if await session.scalar(query.limit(1)) is not None:
                    return False
            if generated:
                from agent_hub.capabilities.python_direct_expiry import verified_direct_expiry

                if not await verified_direct_expiry(session, row):
                    return False
            project, workspace = authorized_workspace_scope(row, tenant_id=tenant, run_id=run)
            item = await self.verifier.python_expired_environment_invocation(tenant, run)
            if item is None:
                return False
            item = PythonProjectInvocation.model_validate(item.model_dump(), strict=True)
            recipe = state['python_recipe']
            if (item.install_allowed is not False or item.source_kind != ('candidate' if generated else 'original')
                    or (item.tenant_id, item.run_id, item.project_id, item.session_id)
                    != (tenant, run, project, workspace)
                    or item.original_reference.model_dump(mode='json') != state['reference']
                    or not isinstance(recipe, Mapping) or set(recipe) != {
                        'schema_version', 'recipe_sha256', 'environment_input_sha256'}
                    or type(recipe['schema_version']) is not int or recipe['schema_version'] != 1
                    or (item.recipe_sha256, item.environment_input_sha256)
                    != (recipe['recipe_sha256'], recipe['environment_input_sha256'])):
                return False
            released = await self.executor.release_expired(item)
            if type(released) is not bool:
                raise ValueError('invalid Python expired cleanup response')
            # No run transition, marker, version, request or flag is backfilled.
            return released

    async def maintain_capacity(self) -> int:
        """Bounded internal maintenance; DB locks remain held through each RPC.

        Eligibility is not a root deletion proof. Root obtains the native READY
        ref itself and must independently enforce TTL/retained/unit/stage fences.
        """
        if self._capacity_lock.locked():
            # Prepare must not race an already-running periodic cleanup against
            # the same full cache. Wait for it without launching another batch.
            async with self._capacity_lock:
                return 0
        async with self._capacity_lock:
            cutoff = datetime.now(UTC) - timedelta(seconds=_MAX_RETENTION_SECONDS)
            query = select(RunRow).where(
                RunRow.status == RunStatus.FAILED.value, RunRow.updated_at <= cutoff,
                or_(RunRow.routing_decision.contains({
                        'project_input_preparation': {'schema_version': 2, 'phase': 'executing'}}),
                    and_(RunRow.mode == 'direct', RunRow.routing_decision.contains({
                        'generated_project_contract_version': 1, 'generated_project_preparation': {
                            'schema_version': 1, 'phase': 'python_ready', 'stack': 'python'}}))),
            ).order_by(RunRow.id).limit(8)
            if self._capacity_cursor is not None:
                query = query.where(RunRow.id > self._capacity_cursor)
            async with await self.repository.run_transaction() as session:
                rows = (await session.scalars(query)).all()
                candidates = tuple((row.tenant_id, row.id) for row in rows)
            if not candidates:
                self._capacity_cursor = None
            released = 0
            for tenant, run in candidates:
                try:
                    released += int(await self._release_expired_pre_model(tenant, run, cutoff))
                except Exception:  # noqa: BLE001 - UNKNOWN is not false/absent and never grants deletion.
                    _LOGGER.warning('python_expired_environment_maintenance_blocked run_id=%s', run)
                self._capacity_cursor = run
            return released

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
        if (status is not RunStatus.COMPLETED and status is not RunStatus.CANCELLED
                and status is not RunStatus.FAILED):
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
            if status in {RunStatus.CANCELLED, RunStatus.FAILED}:
                policy = _FAILED_VERSION if status is RunStatus.FAILED else _CANCEL_VERSION
                # Only the locked, service-persisted opt-in is authority. Never
                # backfill legacy FAILED rows from callback arguments or history.
                if (type(route.get(policy)) is not int or route[policy] != 1
                        or route.get('source') == 'self_repair'
                        or any(not isinstance(key, str) or key.startswith('self_repair_') for key in route)):
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
            if not states:
                return
            for state in states:
                if not isinstance(state, dict):
                    return
                tools = state.get('tools')
                extended_failed = status is RunStatus.FAILED and (
                    'child_checkpoint' in state or (isinstance(tools, dict) and any(
                        isinstance(tool, dict) and tool.get('status') == 'failed' for tool in tools.values()
                    ))
                )
                if extended_failed:
                    if not await _verified_failed_history(session, state, tenant_id, run_id):
                        return
                elif _uncertain(state) and not (
                    status in {RunStatus.CANCELLED, RunStatus.FAILED}
                    and _terminal_model_history_only(state, run_id)
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
                    and_(RunRow.status == RunStatus.FAILED.value,
                         RunRow.routing_decision.contains({_FAILED_VERSION: 1})),
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
                await self.maintain_capacity()
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
