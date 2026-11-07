"""Trusted terminal cache cleanup, independent of model tool arguments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sys
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities.project_baselines import ProjectBaselineError
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_project_service import (
    PythonOriginalContext,
    _validate_expired_original_observation,
)
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.db.models import (
    ProviderContinuationRow,
    RunApprovalRow,
    RunCheckpointRow,
    RunEventRow,
    RunOutboxRow,
    RunRow,
    RuntimeArtifactRow,
    RuntimeArtifactWriteRow,
    RunUsageRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository, _database_uuid
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepository
from agent_hub.runtime.contracts import Artifact, JsonValue, RuntimeCheckpoint, _freeze_object
from agent_hub.skills.sandbox.python_premodel_retirement import PythonStoppedPremodelSnapshot
from agent_hub.skills.sandbox.python_project_client import PythonProjectSystemdSandbox
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonFailedMaintenanceInvocation,
    PythonFailedReservationSnapshot,
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
_DIAGNOSTIC_ROOT = Path(__file__).parent.parent
_DIAGNOSTIC_ERROR_TYPES = (
    ProjectVerificationError, ProjectBaselineError, ValueError, TypeError, RuntimeError,
    OSError, PermissionError, FileNotFoundError, TimeoutError, KeyError, AttributeError,
)


def _retry_error_type(error: BaseException) -> str:
    # Unknown exception class names can themselves contain private text/newlines.
    for known in _DIAGNOSTIC_ERROR_TYPES:
        if type(error) is known:
            return known.__name__
    return "Exception"


def _retry_failure_frame(tb: TracebackType) -> dict[str, str | int] | None:
    frame = tb.tb_frame
    name = frame.f_globals.get("__name__")
    if not isinstance(name, str) or not name.startswith("agent_hub."):
        return None
    module = sys.modules.get(name)
    if module is None or module.__dict__ is not frame.f_globals:
        return None
    filename = Path(frame.f_code.co_filename)
    if ".." in filename.parts:
        return None
    try:
        relative = filename.relative_to(_DIAGNOSTIC_ROOT).as_posix()
    except ValueError:
        return None
    function = frame.f_code.co_name
    if (len(relative) > 240
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*\.py", relative)
            is None
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}|<(?:module|lambda|listcomp|dictcomp|setcomp|genexpr)>",
                            function) is None):
        return None
    return {"module": relative, "function": function, "line": tb.tb_lineno}


def _retry_failure_diagnosis(error: BaseException) -> str:
    """Bounded location metadata only, including context hidden by `from None`."""
    exceptions: list[dict[str, object]] = []
    frames: deque[tuple[int, dict[str, str | int]]] = deque(maxlen=12)
    seen: set[int] = set()
    current: BaseException | None = error
    truncated = False
    while current is not None and len(exceptions) < 4 and id(current) not in seen:
        seen.add(id(current))
        index = len(exceptions)
        exceptions.append({"type": _retry_error_type(current), "frames": []})
        tb = current.__traceback__
        # Bound scanning as well as output, even with a large external traceback.
        for _ in range(128):
            if tb is None:
                break
            item = _retry_failure_frame(tb)
            if item is not None:
                truncated |= len(frames) == 12
                frames.append((index, item))
            tb = tb.tb_next
        truncated |= tb is not None
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    truncated |= current is not None
    # Keep the innermost cause's frames when the global output budget is full.
    for index, item in frames:
        cast(list[dict[str, str | int]], exceptions[index]["frames"]).append(item)
    return json.dumps({"schema_version": 1, "exceptions": exceptions, "truncated": truncated},
                      ensure_ascii=True, separators=(",", ":"))


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


class _LegacyReleaser(Protocol):
    async def release_expired(self, invocation: PythonProjectInvocation) -> bool: ...

    async def inspect_failed(self, invocation: PythonFailedMaintenanceInvocation) -> PythonFailedReservationSnapshot | None: ...

    async def commit_failed(
        self, invocation: PythonFailedMaintenanceInvocation, snapshot: PythonFailedReservationSnapshot,
    ) -> bool: ...

    async def release_completed(
        self,
        invocation: PythonProjectInvocation,
        *,
        expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference: ...



class _Releaser(_LegacyReleaser, Protocol):
    async def inspect_stopped_premodel(
        self, invocation: PythonFailedMaintenanceInvocation,
    ) -> PythonStoppedPremodelSnapshot | None: ...

    async def commit_stopped_premodel(
        self, invocation: PythonFailedMaintenanceInvocation, snapshot: PythonStoppedPremodelSnapshot,
    ) -> bool: ...


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


async def _historical_nonblocking_repair_suggestion(
    session: AsyncSession, row: RunRow, route: dict[str, object],
) -> bool:
    """Classify one persisted suggestion for cleanup, never approve or resume it.

    record_self_repair_decision persists repair.classified and the proposal/token
    under this run's lock in one transaction. The event does not contain the
    token: its authority remains the unchanged locked RunRow, not an invented
    approval-row/token relationship. Caller retains every native release fence.
    """
    from agent_hub.runs.self_repair import (
        SelfRepairDecision,
        _failure_category,
        _fingerprint,
        _kind_text,
    )
    from agent_hub.runtime.contracts import RunEvent, _mutable_json

    def encoded(value: object) -> str:
        bounded = _freeze_object(value, name='historical repair comparison')
        return json.dumps(_mutable_json(bounded), sort_keys=True, separators=(',', ':'), allow_nan=False)

    try:
        proposal = route.get('repair_proposal')
        token = route.get('decision_token')
        if (row.status != 'failed' or type(route) is not dict
                or route.get('approval_kind') != 'self_repair'
                or type(token) is not str or re.fullmatch(r'decision-[a-f0-9]{64}', token) is None
                or route.get('source') == 'self_repair'
                or any(not isinstance(key, str) or key.startswith('self_repair_') for key in route)
                or _uncertain(route) or type(proposal) is not dict
                or proposal.get('kind') != 'self_repair'
                or proposal.get('source_run_id') != str(row.id)
                or proposal.get('pauses_run') is not False
                or proposal.get('requires_approval') is not True
                or proposal.get('automatic_execution') is not False):
            return False
        original = encoded(route)
        events = (await session.scalars(select(RunEventRow).where(
            RunEventRow.tenant_id == row.tenant_id, RunEventRow.run_id == row.id,
            RunEventRow.kind.in_(('repair.classified', 'repair.skipped', 'repair.started',
                                 'repair.completed', 'repair.failed')),
        ).order_by(RunEventRow.sequence.desc()).limit(2))).all()
        if len(events) != 1:
            return False
        saved = events[0]
        if ((saved.tenant_id, saved.run_id, saved.kind) != (row.tenant_id, row.id, 'repair.classified')
                or type(saved.sequence) is not int or saved.sequence < 1):
            return False
        event = RunEvent.from_payload(saved.payload)
        payload = event.payload
        source_sequence = proposal.get('source_event_sequence')
        attempt = payload.get('attempt')
        max_attempts = payload.get('max_attempts')
        if (event.run_id != row.id or event.sequence != saved.sequence
                or _kind_text(event.kind) != saved.kind
                or type(source_sequence) is not int or not 0 < source_sequence < saved.sequence
                or payload.get('schema_version') != 1 or type(payload.get('schema_version')) is not int
                or payload.get('status') != 'failed' or payload.get('mode') != row.mode
                or payload.get('source_sequence') != source_sequence
                or type(payload.get('source_sequence')) is not int
                or payload.get('requires_approval') is not True
                or payload.get('automatic_execution') is not False
                or type(attempt) is not int or type(max_attempts) is not int
                or not 1 <= attempt <= max_attempts <= 3):
            return False
        trigger = payload.get('trigger')
        action = payload.get('action')
        severity = payload.get('severity')
        failure_category = payload.get('failure_category')
        mode_text = row.mode
        if (not isinstance(trigger, str) or not isinstance(action, str)
                or not isinstance(severity, str) or not isinstance(failure_category, str)
                or not isinstance(mode_text, str)):
            return False
        recovery_strategy = payload.get('recovery_strategy')
        orchestration_recovery_hint = payload.get('orchestration_recovery_hint')
        error_code = payload.get('error_code')
        suggested_action = payload.get('suggested_action')
        if ((recovery_strategy is not None and not isinstance(recovery_strategy, str))
                or (orchestration_recovery_hint is not None and not isinstance(orchestration_recovery_hint, str))
                or (error_code is not None and not isinstance(error_code, str))
                or (suggested_action is not None and not isinstance(suggested_action, str))):
            return False
        roles = payload.get('role_capability_requirements', ())
        if not isinstance(roles, tuple):
            return False
        role_requirements: list[Mapping[str, JsonValue]] = []
        for role in roles:
            if not isinstance(role, Mapping):
                return False
            role_requirements.append(role)
        source = await session.scalar(select(RunEventRow).where(
            RunEventRow.tenant_id == row.tenant_id, RunEventRow.run_id == row.id,
            RunEventRow.sequence == source_sequence,
        ).limit(1))
        if (source is None or (source.tenant_id, source.run_id, source.sequence)
                != (row.tenant_id, row.id, source_sequence)
                or source.kind not in ('runtime.failed', 'step.failed', 'tool.failed')):
            return False
        failure = RunEvent.from_payload(source.payload)
        if (failure.run_id != row.id or failure.sequence != source.sequence
                or _kind_text(failure.kind) != source.kind or payload.get('source_kind') != source.kind
                or _failure_category(failure) != payload.get('failure_category')):
            return False
        mode = TaskMode(mode_text)
        fingerprint = _fingerprint(status=RunStatus.FAILED, mode=mode, source_kind=source.kind,
                                   failure_category=failure_category, failure=failure)
        if payload.get('fingerprint') != fingerprint or proposal.get('fingerprint') != fingerprint:
            return False
        decision = SelfRepairDecision(
            kind='repair.classified', trigger=trigger, action=action,
            severity=severity, status=RunStatus.FAILED, mode=mode,
            source_kind=source.kind, source_sequence=source_sequence, fingerprint=fingerprint,
            failure_category=failure_category, requires_approval=True,
            automatic_execution=False, attempt=attempt, max_attempts=max_attempts,
            recovery_strategy=recovery_strategy,
            orchestration_recovery_hint=orchestration_recovery_hint,
            error_code=error_code, suggested_action=suggested_action,
            role_capability_requirements=tuple(role_requirements),
        )
        if (decision.pauses_run is not False
                or encoded(decision.to_event(run_id=row.id, sequence=saved.sequence).to_payload()) != encoded(saved.payload)
                or encoded(decision.to_proposal(run_id=row.id)) != encoded(proposal)):
            return False
        child = await session.scalar(select(RunRow.id).where(
            RunRow.tenant_id == row.tenant_id, RunRow.id != row.id,
            or_(RunRow.blocked_by_run_id == row.id,
                RunRow.routing_decision.contains({'source_run_id': str(row.id)}),
                RunRow.routing_decision.contains({'self_repair_source_run_id': str(row.id)})),
        ).limit(1))
        return child is None and encoded(row.routing_decision) == original
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return False


class PythonProjectTerminalHook:
    def __init__(
        self,
        repository: _Repository,
        verifier: _ContextReader,
        executor: _LegacyReleaser,
        *,
        generated_reader: _GeneratedContextReader | None = None,
    ) -> None:
        self.repository = repository
        self.verifier = verifier
        self.executor = executor
        self.generated_reader = generated_reader
        self._retry_cursor: UUID | None = None
        self._capacity_cursor: UUID | None = None
        self._failed_empty_cursor: UUID | None = None
        self._capacity_lock = asyncio.Lock()
        self._stopped_unknown_owners: set[tuple[UUID, UUID]] = set()
        self._failure_archive_repository: ArtifactRepository | None = (
            PostgresArtifactRepository(repository._session_factory) if isinstance(repository, RunRepository) else None
        )

    async def _failed_empty_owner(
        self, session: AsyncSession, tenant: UUID, run: UUID, cutoff: datetime,
    ) -> tuple[int, str, str] | None:
        row = await session.scalar(select(RunRow).where(
            RunRow.tenant_id == tenant, RunRow.id == run).with_for_update())
        if (row is None or (row.tenant_id, row.id, row.status) != (tenant, run, 'failed')
                or type(row.version) is not int or not isinstance(row.updated_at, datetime)
                or row.updated_at.tzinfo is None or row.updated_at > cutoff
                or any(value is not None for value in (row.worker_id, row.worker_lease_token,
                    row.worker_lease_expires_at, row.worker_heartbeat_at))
                or type(row.routing_decision) is not dict or _uncertain(row.routing_decision)):
            return None
        if row.routing_decision.get('approval_kind') is not None or row.routing_decision.get('decision_token') is not None:
            return None
        generated = row.routing_decision.get('generated_project_preparation')
        original = row.routing_decision.get('project_input_preparation')
        generated_python = (
            type(row.routing_decision.get('generated_project_contract_version')) is int
            and row.routing_decision['generated_project_contract_version'] == 1
            and isinstance(generated, dict) and type(generated.get('schema_version')) is int
            and generated['schema_version'] == 1 and generated.get('phase') == 'python_ready'
            and generated.get('stack') == 'python')
        original_python = (
            isinstance(original, dict) and type(original.get('schema_version')) is int
            and original['schema_version'] == 2 and isinstance(original.get('python_recipe'), dict)
            and type(original['python_recipe'].get('schema_version')) is int
            and original['python_recipe']['schema_version'] == 1)
        if not generated_python and not original_python:
            return None
        # Historical self_repair/provider bytes do not enqueue a consumer. Actual
        # acceptance changes this locked row's status/version and creates outbox.
        for query in (
            select(RunApprovalRow.id).where(RunApprovalRow.tenant_id == tenant, RunApprovalRow.run_id == run,
                RunApprovalRow.status.not_in(('approved', 'rejected', 'expired'))),
            select(RunOutboxRow.id).where(RunOutboxRow.tenant_id == tenant, RunOutboxRow.run_id == run,
                RunOutboxRow.delivered.is_(False)),
        ):
            if await session.scalar(query.limit(1)) is not None:
                return None
        latest = await session.scalar(select(RunCheckpointRow).where(RunCheckpointRow.tenant_id == tenant,
            RunCheckpointRow.run_id == run).order_by(RunCheckpointRow.sequence.desc()).limit(1))
        if latest is not None:
            checkpoint = _bound_checkpoint(latest.payload, tenant, run)
            if (latest.state_sha256 != checkpoint.state_sha256 or latest.state != checkpoint.to_payload()['state']
                    or (latest.id, latest.runtime_type, latest.runtime_version, latest.mode) != (
                        checkpoint.id, checkpoint.runtime_type, checkpoint.runtime_version, checkpoint.mode.value)):
                return None
            body_path: tuple[str, ...] = ()
            if (checkpoint.runtime_type, checkpoint.runtime_version, checkpoint.mode) == ('hybrid', '3', TaskMode.HYBRID):
                native = _bound_checkpoint(latest.state.get('child_checkpoint'), tenant, run)
                body_path = ('child_checkpoint', 'state')
            else:
                native = checkpoint
            if (native.runtime_type, native.runtime_version, native.mode) != ('crew', '11', TaskMode.DISPATCH):
                return None
            body = native.to_payload()['state']
            assert isinstance(body, dict)
            for name in ('models', 'tools'):
                ledger = body.get(name)
                if (type(ledger) is not dict or (name == 'models' and not ledger)
                        or any(type(entry) is not dict or type(entry.get('status')) is not str
                               or entry['status'] not in {'succeeded', 'failed', 'rejected'}
                               for entry in ledger.values())):
                    return None
            pending: list[tuple[object, tuple[str, ...], int]] = [(latest.state, (), 0)]
            for _ in range(100_000):
                if not pending:
                    break
                value, path, depth = pending.pop()
                if depth > 64:
                    return None
                if isinstance(value, dict):
                    for key, item in value.items():
                        # Only this bound native CP body has a historical runtime
                        # phase. A provider object cannot acquire the exemption.
                        if key == 'phase' and item == 'running' and path == body_path:
                            continue
                        pending.append((item, (*path, key), depth + 1))
                elif isinstance(value, (list, tuple)):
                    pending.extend((item, (*path, str(index)), depth + 1) for index, item in enumerate(value))
                elif _uncertain(value):
                    return None
            if pending:
                return None
        project, workspace = authorized_workspace_scope(row, tenant_id=tenant, run_id=run)
        return row.version, project, workspace

    async def _release_expired_empty_failure(self, tenant: UUID, run: UUID, cutoff: datetime) -> bool:
        archive_repository = self._failure_archive_repository
        if archive_repository is None:
            return False
        async with asyncio.timeout(_CAPACITY_TIMEOUT_SECONDS):
            async with await self.repository.run_transaction() as session, session.begin():
                owner = await self._failed_empty_owner(session, tenant, run, cutoff)
                if owner is None:
                    return False
                version, project, workspace = owner
                invocation = PythonFailedMaintenanceInvocation(execution_id='python_release_expired_' + uuid4().hex,
                    tenant_id=tenant, run_id=run, project_id=project, session_id=workspace)
                snapshot = await self.executor.inspect_failed(invocation)
                if snapshot is None:
                    return False
                snapshot = PythonFailedReservationSnapshot.model_validate_json(snapshot.model_dump_json(), strict=True)
                if any(getattr(snapshot.scope, key) != getattr(invocation, key)
                       for key in ('tenant_id', 'run_id', 'project_id', 'session_id')):
                    return False
            # ArtifactRepo owns a separate RunRow-locked transaction. Never call
            # put/get_many inside the owner transaction above or below.
            archive = snapshot.archive()
            reference = ArtifactReference(id=archive.id, sha256=archive.content_sha256)
            await archive_repository.put(tenant, run, archive)
            readback = await archive_repository.get_many(tenant, run, (reference,))
            if len(readback) != 1 or readback[0].to_payload() != archive.to_payload():
                return False
            async with await self.repository.run_transaction() as session, session.begin():
                if await self._failed_empty_owner(session, tenant, run, cutoff) != (version, project, workspace):
                    return False
                stored = await session.scalar(select(RuntimeArtifactRow).where(RuntimeArtifactRow.tenant_id == tenant,
                    RuntimeArtifactRow.run_id == run, RuntimeArtifactRow.id == archive.id))
                if (stored is None or stored.permanent is not True or (stored.tenant_id, stored.run_id) != (tenant, run)
                        or PostgresArtifactRepository._hydrate(stored).to_payload() != archive.to_payload()):
                    return False
                commit = invocation.model_copy(update={'execution_id': 'python_release_expired_' + uuid4().hex})
                result = await self.executor.commit_failed(commit, snapshot)
                if type(result) is not bool:
                    raise ValueError('invalid failed reservation maintenance result')
                return result

    async def _pre_model_invocation(self, session: AsyncSession, tenant: UUID, run: UUID, cutoff: datetime) -> tuple[RunRow, PythonProjectInvocation] | None:
        row = await session.scalar(select(RunRow).where(RunRow.tenant_id == tenant, RunRow.id == run).with_for_update())
        if row is None or (row.tenant_id, row.id, row.status) != (tenant, run, 'failed') or (not isinstance(row.updated_at, datetime)) or (row.updated_at.tzinfo is None) or (row.updated_at > cutoff) or any(value is not None for value in (row.worker_id, row.worker_lease_token, row.worker_lease_expires_at, row.worker_heartbeat_at)):
            return None
        route = row.routing_decision
        if type(route) is not dict or _RELEASE_MARKER in route or route.get('source') == 'self_repair' or any(not isinstance(key, str) or key.startswith('self_repair_') for key in route):
            return None
        generated = bool({'generated_project_contract_version', 'generated_project_preparation'} & route.keys())
        state: Mapping[str, object] | None
        if generated:
            state = _generated_preparation(route)
            if state is None or row.mode != 'direct':
                return None
        else:
            original_state = route.get('project_input_preparation')
            if not isinstance(original_state, dict):
                return None
            state = original_state
            if type(state) is not dict or set(state) != {'schema_version', 'phase', 'execution_id', 'reference', 'python_recipe'} or type(state['schema_version']) is not int or (state['schema_version'] != 2) or (state['phase'] != 'executing'):
                return None
            check_route = {**route, 'project_input_preparation': {**state, 'phase': 'captured'}}
            if _uncertain(check_route):
                return None
        assert state is not None
        probes = [select(ProviderContinuationRow.id).where(ProviderContinuationRow.tenant_id == tenant, ProviderContinuationRow.run_id == run), select(RunApprovalRow.id).where(RunApprovalRow.tenant_id == tenant, RunApprovalRow.run_id == run, RunApprovalRow.status.not_in(('approved', 'rejected', 'expired'))), select(RunOutboxRow.id).where(RunOutboxRow.tenant_id == tenant, RunOutboxRow.run_id == run, RunOutboxRow.delivered.is_(False))]
        if not generated:
            probes += [select(RunCheckpointRow.id).where(RunCheckpointRow.tenant_id == tenant, RunCheckpointRow.run_id == run), select(RunUsageRow.id).where(RunUsageRow.tenant_id == tenant, RunUsageRow.run_id == run), select(RunEventRow.id).where(RunEventRow.tenant_id == tenant, RunEventRow.run_id == run, RunEventRow.kind.not_in(_PRE_MODEL_EVENT_KINDS))]
        for query in probes:
            if await session.scalar(query.limit(1)) is not None:
                return None
        if generated:
            from agent_hub.capabilities.python_direct_expiry import verified_direct_expiry
            if not await verified_direct_expiry(session, row):
                return None
            check_route = route
            receipt = route.get('project_verification_receipt')
            if isinstance(receipt, Mapping) and receipt.get('status') == 'running':
                check_route = {**route, 'project_verification_receipt': {**receipt, 'status': 'expiry_liveness_proven'}}
            if _uncertain(check_route):
                return None
        project, workspace = authorized_workspace_scope(row, tenant_id=tenant, run_id=run)
        item = await self.verifier.python_expired_environment_invocation(tenant, run)
        if item is None:
            return None
        item = PythonProjectInvocation.model_validate(item.model_dump(), strict=True)
        recipe = state['python_recipe']
        if item.install_allowed is not False or item.source_kind != ('candidate' if generated else 'original') or (item.tenant_id, item.run_id, item.project_id, item.session_id) != (tenant, run, project, workspace) or (item.original_reference.model_dump(mode='json') != state['reference']) or (not isinstance(recipe, Mapping)) or (set(recipe) != {'schema_version', 'recipe_sha256', 'environment_input_sha256'}) or (type(recipe['schema_version']) is not int) or (recipe['schema_version'] != 1) or ((item.recipe_sha256, item.environment_input_sha256) != (recipe['recipe_sha256'], recipe['environment_input_sha256'])):
            return None
        return (row, item)

    async def _release_expired_pre_model(self, tenant: UUID, run: UUID, cutoff: datetime) -> bool:
        async with asyncio.timeout(_CAPACITY_TIMEOUT_SECONDS), await self.repository.run_transaction() as session, session.begin():
            selected = await self._pre_model_invocation(session, tenant, run, cutoff)
            if selected is None:
                return False
            _row, item = selected
            released = await self.executor.release_expired(item)
            if type(released) is not bool:
                raise ValueError('invalid Python expired cleanup response')
            return released


    async def _stopped_pre_model_owner(
        self, session: AsyncSession, tenant: UUID, run: UUID, cutoff: datetime,
        *, expected_intent: Artifact | None = None,
    ) -> tuple[str, PythonProjectInvocation] | None:
        selected = await self._pre_model_invocation(session, tenant, run, cutoff)
        if selected is None:
            return None
        row, item = selected
        route = row.routing_decision
        if (type(route) is not dict
                or {'generated_project_contract_version', 'generated_project_preparation'} & route.keys()
                or item.source_kind != 'original'
                or type(row.version) is not int or row.version < 0
                or row.blocked_by_run_id is not None
                or route.get('approval_kind') is not None or route.get('decision_token') is not None):
            return None
        intent_id = self._stopped_intent_id(item)
        intent = await session.scalar(select(RuntimeArtifactRow).where(
            RuntimeArtifactRow.tenant_id == tenant, RuntimeArtifactRow.run_id == run,
            RuntimeArtifactRow.id == intent_id))
        if expected_intent is None:
            if intent is not None:
                return None  # A prior/private partial attempt is never automatic retry authority.
        elif (intent is None or intent.permanent is not True
                or (intent.tenant_id, intent.run_id, intent.id) != (tenant, run, expected_intent.id)
                or PostgresArtifactRepository._hydrate(intent).to_payload() != expected_intent.to_payload()):
            return None
        child = await session.scalar(select(RunRow.id).where(
            RunRow.tenant_id == tenant, RunRow.id != run,
            or_(RunRow.blocked_by_run_id == run,
                RunRow.routing_decision.contains({'source_run_id': str(run)}),
                RunRow.routing_decision.contains({'self_repair_source_run_id': str(run)})),
        ).limit(1))
        if child is not None or await session.scalar(select(RuntimeArtifactWriteRow.write_id).where(
                RuntimeArtifactWriteRow.tenant_id == tenant, RuntimeArtifactWriteRow.run_id == run,
                RuntimeArtifactWriteRow.status == 'reserved').limit(1)) is not None:
            return None
        # Freeze even permitted pre-model events, not merely absence of model kinds.
        # The real private ArtifactRepository publishes no event or version update.
        events = (await session.scalars(select(RunEventRow).where(
            RunEventRow.tenant_id == tenant, RunEventRow.run_id == run,
        ).order_by(RunEventRow.sequence).limit(1025))).all()
        if len(events) > 1024:
            return None
        history = []
        previous = -1
        for event in events:
            if ((event.tenant_id, event.run_id) != (tenant, run)
                    or type(event.sequence) is not int or event.sequence <= previous
                    or event.kind not in _PRE_MODEL_EVENT_KINDS or type(event.payload) is not dict):
                return None
            previous = event.sequence
            history.append({'id': str(event.id), 'sequence': event.sequence,
                'kind': event.kind, 'payload': event.payload})
        frozen_item = item.model_dump(mode='json')
        frozen_item.pop('execution_id')  # The readonly selector mints no grant, only a fresh probe ID.
        identity = {'tenant_id': str(tenant), 'run_id': str(run), 'version': row.version,
            'status': row.status, 'updated_at': row.updated_at.isoformat(),
            'actor_id': str(row.actor_id) if row.actor_id is not None else None,
            'actor_role': row.actor_role, 'request': row.request, 'mode': row.mode,
            'route': route, 'invocation': frozen_item, 'events': history}
        _freeze_object(identity, name='stopped pre-model owner')
        encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(encoded) > 1024**2:
            return None
        return hashlib.sha256(encoded).hexdigest(), item

    @staticmethod
    def _stopped_intent_id(item: PythonProjectInvocation) -> UUID:
        owner = ':'.join(str(getattr(item, key)) for key in (
            'tenant_id', 'run_id', 'project_id', 'session_id'))
        return uuid5(NAMESPACE_URL, 'agent-hub:stopped-premodel-commit-intent:' + owner)

    @staticmethod
    def _stopped_snapshot_matches(
        snapshot: PythonStoppedPremodelSnapshot, item: PythonProjectInvocation,
        maintenance: PythonFailedMaintenanceInvocation,
    ) -> bool:
        reference = PythonEnvironmentReference.model_validate_json(snapshot.reference, strict=True)
        return (snapshot.scope == reference.scope
            and snapshot.scope.original_reference == item.original_reference
            and _reference_matches(item, reference)
            and all(getattr(snapshot.scope, key) == getattr(maintenance, key)
                    for key in ('tenant_id', 'run_id', 'project_id', 'session_id'))
            and maintenance.execution_id not in {snapshot.origin_execution_id,
                *(stage.execution_id for stage in snapshot.stages)})

    async def _retire_stopped_pre_model(
        self, tenant: UUID, run: UUID, cutoff: datetime,
    ) -> bool:
        archive_repository = self._failure_archive_repository
        owner = tenant, run
        # Bounded conservative circuit: a new worker must inspect durable Root
        # state, never automatically resume an uncertain commit in this hook.
        if (archive_repository is None or owner in self._stopped_unknown_owners
                or len(self._stopped_unknown_owners) >= 1024):
            return False
        executor = cast(_Releaser, self.executor)
        try:
            async with asyncio.timeout(_CAPACITY_TIMEOUT_SECONDS):
                async with await self.repository.run_transaction() as session, session.begin():
                    selected = await self._stopped_pre_model_owner(session, tenant, run, cutoff)
                    if selected is None:
                        return False
                    identity, item = selected
                    invocation = PythonFailedMaintenanceInvocation(
                        execution_id='python_release_expired_' + uuid4().hex,
                        tenant_id=tenant, run_id=run, project_id=item.project_id, session_id=item.session_id)
                    snapshot = await executor.inspect_stopped_premodel(invocation)
                    if snapshot is None:
                        # Only this known, non-exception outcome permits normal TTL
                        # release, still under the original lock and a full recheck.
                        current = await self._stopped_pre_model_owner(session, tenant, run, cutoff)
                        if current is None or current[0] != identity:
                            return False
                        result = await self.executor.release_expired(current[1])
                        if type(result) is not bool:
                            raise ValueError('invalid stopped fallback release response')
                        return result
                    if type(snapshot) is not PythonStoppedPremodelSnapshot:
                        raise ValueError('invalid stopped pre-model snapshot type')
                    snapshot = PythonStoppedPremodelSnapshot.model_validate_json(
                        snapshot.model_dump_json(), strict=True)
                    if not self._stopped_snapshot_matches(snapshot, item, invocation):
                        return False
                # ArtifactRepo owns its own RunRow-locked transaction. Never nest it.
                archive = snapshot.archive()
                await archive_repository.put(tenant, run, archive)
                readback = await archive_repository.get_many(tenant, run, (
                    ArtifactReference(id=archive.id, sha256=archive.content_sha256),))
                if len(readback) != 1 or readback[0].to_payload() != archive.to_payload():
                    return False
                # Fixed owner ID + unique inspector identity makes concurrent or
                # restarted claimants conflict in the immutable private repository.
                # This is an irreversible attempt fence, not execution authority.
                intent = Artifact(id=self._stopped_intent_id(item),
                    type='python_stopped_premodel_commit_intent', producer='python_terminal_maintenance',
                    content={'schema_version': 1, 'owner_sha256': identity,
                        'inspect_execution_id': invocation.execution_id,
                        'snapshot_archive_id': str(archive.id),
                        'snapshot_archive_sha256': archive.content_sha256})
                await archive_repository.put(tenant, run, intent)
                intent_readback = await archive_repository.get_many(tenant, run, (
                    ArtifactReference(id=intent.id, sha256=intent.content_sha256),))
                if len(intent_readback) != 1 or intent_readback[0].to_payload() != intent.to_payload():
                    return False
                async with await self.repository.run_transaction() as session, session.begin():
                    current = await self._stopped_pre_model_owner(
                        session, tenant, run, cutoff, expected_intent=intent)
                    if current is None or current[0] != identity:
                        return False
                    stored = await session.scalar(select(RuntimeArtifactRow).where(
                        RuntimeArtifactRow.tenant_id == tenant, RuntimeArtifactRow.run_id == run,
                        RuntimeArtifactRow.id == archive.id))
                    if (stored is None or stored.permanent is not True
                            or (stored.tenant_id, stored.run_id, stored.id) != (tenant, run, archive.id)
                            or PostgresArtifactRepository._hydrate(stored).to_payload() != archive.to_payload()):
                        return False
                    commit = invocation.model_copy(update={
                        'execution_id': 'python_release_expired_' + uuid4().hex})
                    if (commit.execution_id == invocation.execution_id
                            or not self._stopped_snapshot_matches(snapshot, current[1], commit)):
                        return False
                    result = await executor.commit_stopped_premodel(commit, snapshot)
                    if type(result) is not bool:
                        raise ValueError('invalid stopped pre-model commit response')
                    return result
        except BaseException:
            self._stopped_unknown_owners.add(owner)
            raise

    async def _release_expired_terminal_observed(
        self, tenant: UUID, run: UUID, cutoff: datetime,
    ) -> bool:
        """Legacy observed READY only; no opt-in backfill or run-state transition."""
        async with (
            asyncio.timeout(_CAPACITY_TIMEOUT_SECONDS),
            await self.repository.run_transaction() as session,
            session.begin(),
        ):
            row = await session.scalar(select(RunRow).where(
                RunRow.tenant_id == tenant, RunRow.id == run).with_for_update())
            if (row is None or (row.tenant_id, row.id, row.status) != (tenant, run, 'failed')
                    or type(row.version) is not int or row.version < 0
                    or not isinstance(row.updated_at, datetime) or row.updated_at.tzinfo is None
                    or row.updated_at > cutoff or row.blocked_by_run_id is not None
                    or any(value is not None for value in (row.worker_id, row.worker_lease_token,
                        row.worker_lease_expires_at, row.worker_heartbeat_at))):
                return False
            route = row.routing_decision
            if (type(route) is not dict or _uncertain(route) or _RELEASE_MARKER in route
                    or route.get('source') == 'self_repair'
                    or any(not isinstance(key, str) or key.startswith('self_repair_') for key in route)
                    or {'generated_project_contract_version', 'generated_project_preparation'} & route.keys()):
                return False
            before = _freeze_object(route, name='expired observed owner')
            version = row.version
            if route.get('approval_kind') is not None or route.get('decision_token') is not None:
                if not await _historical_nonblocking_repair_suggestion(session, row, route):
                    return False
                # Private classification only; never assign this view to the row.
                approval_view = {**route, 'approval_kind': None, 'decision_token': None}
                if _uncertain(approval_view):
                    return False
            state = route.get('project_input_preparation')
            if (not isinstance(state, Mapping) or type(state.get('schema_version')) is not int
                    or state['schema_version'] != 2 or state.get('phase') != 'observed'):
                return False
            for query in (
                select(ProviderContinuationRow.id).where(ProviderContinuationRow.tenant_id == tenant,
                    ProviderContinuationRow.run_id == run),
                select(RunApprovalRow.id).where(RunApprovalRow.tenant_id == tenant, RunApprovalRow.run_id == run,
                    RunApprovalRow.status.not_in(('approved', 'rejected', 'expired'))),
                select(RunOutboxRow.id).where(RunOutboxRow.tenant_id == tenant, RunOutboxRow.run_id == run,
                    RunOutboxRow.delivered.is_(False)),
                select(RuntimeArtifactWriteRow.write_id).where(RuntimeArtifactWriteRow.tenant_id == tenant,
                    RuntimeArtifactWriteRow.run_id == run, RuntimeArtifactWriteRow.status == 'reserved'),
            ):
                if await session.scalar(query.limit(1)) is not None:
                    return False
            # Only the existing exact hybrid3 -> crew11 receipt reconciliation
            # can explain a nonterminal historical checkpoint here. Direct action
            # waits and every other unproved checkpoint remain retained.
            latest = await session.scalar(select(RunCheckpointRow).where(
                RunCheckpointRow.tenant_id == tenant, RunCheckpointRow.run_id == run,
            ).order_by(RunCheckpointRow.sequence.desc()).limit(1))
            if latest is not None and (row.mode != 'hybrid'
                    or (latest.runtime_type, latest.runtime_version, latest.mode) != ('hybrid', '3', 'hybrid')
                    or type(latest.state) is not dict
                    or not await _verified_failed_history(session, latest.state, tenant, run)):
                return False
            project, workspace = authorized_workspace_scope(row, tenant_id=tenant, run_id=run)
            item = await self.verifier.python_expired_environment_invocation(tenant, run)
            if item is None:
                return False
            item = PythonProjectInvocation.model_validate(item.model_dump(), strict=True)
            if (item.install_allowed is not False or item.source_kind != 'original'
                    or (item.tenant_id, item.run_id, item.project_id, item.session_id)
                    != (tenant, run, project, workspace)):
                return False
            _validate_expired_original_observation(cast(Mapping[str, JsonValue], state), item)
            if row.version != version or _freeze_object(row.routing_decision, name='expired observed recheck') != before:
                return False
            # DB owner lock is held through native admission, Root TTL/READY
            # measurement, and retained/unit/stage fences. A refusal stays false.
            released = await self.executor.release_expired(item)
            if type(released) is not bool:
                raise ValueError('invalid Python expired observed cleanup response')
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
                    RunRow.routing_decision.contains({
                        'project_input_preparation': {'schema_version': 2, 'phase': 'observed'}}),
                    and_(RunRow.mode == 'direct', RunRow.routing_decision.contains({
                        'generated_project_contract_version': 1, 'generated_project_preparation': {
                            'schema_version': 1, 'phase': 'python_ready', 'stack': 'python'}}))),
            ).order_by(RunRow.id).limit(8)
            if self._capacity_cursor is not None:
                query = query.where(RunRow.id > self._capacity_cursor)
            async with await self.repository.run_transaction() as session:
                rows = (await session.scalars(query)).all()
                candidates = tuple(
                    (_database_uuid(row.tenant_id), _database_uuid(row.id),
                     isinstance((row.routing_decision or {}).get('project_input_preparation'), Mapping)
                     and cast(Mapping[str, object], (row.routing_decision or {})['project_input_preparation'])
                         .get('phase') == 'observed',
                     not ({'generated_project_contract_version', 'generated_project_preparation'}
                          & (row.routing_decision or {}).keys())) for row in rows
                )
            if not candidates:
                self._capacity_cursor = None
            released = 0
            stopped_attempts: set[tuple[UUID, UUID]] = set()
            for tenant, run, observed, original in candidates:
                if (tenant, run) in self._stopped_unknown_owners:
                    self._capacity_cursor = run
                    continue
                try:
                    if not observed and original and self._failure_archive_repository is not None:
                        stopped_attempts.add((tenant, run))
                        released += int(await self._retire_stopped_pre_model(tenant, run, cutoff))
                    else:
                        released += int(await (self._release_expired_terminal_observed(tenant, run, cutoff)
                            if observed else self._release_expired_pre_model(tenant, run, cutoff)))
                except Exception:  # noqa: BLE001 - UNKNOWN is not false/absent and never grants deletion.
                    _LOGGER.warning('python_expired_environment_maintenance_blocked run_id=%s', run)
                self._capacity_cursor = run
            return released + await self._maintain_empty_failures(
                cutoff, excluded=frozenset(stopped_attempts | self._stopped_unknown_owners))

    async def _maintain_empty_failures(
        self, cutoff: datetime, *, excluded: frozenset[tuple[UUID, UUID]] = frozenset(),
    ) -> int:
        if self._failure_archive_repository is None:
            return 0
        query = select(RunRow).where(RunRow.status == 'failed', RunRow.updated_at <= cutoff,
            or_(RunRow.routing_decision.contains({'generated_project_contract_version': 1,
                'generated_project_preparation': {'schema_version': 1, 'phase': 'python_ready', 'stack': 'python'}}),
                RunRow.routing_decision.contains({'project_input_preparation': {
                    'schema_version': 2, 'python_recipe': {'schema_version': 1}}})),
        ).order_by(RunRow.id).limit(8)
        if self._failed_empty_cursor is not None:
            query = query.where(RunRow.id > self._failed_empty_cursor)
        async with await self.repository.run_transaction() as session:
            rows = (await session.scalars(query)).all()
            candidates = tuple((_database_uuid(row.tenant_id), _database_uuid(row.id)) for row in rows)
        if not candidates:
            self._failed_empty_cursor = None
        released = 0
        for tenant, run in candidates:
            if (tenant, run) in excluded:
                self._failed_empty_cursor = run
                continue
            try:
                released += int(await self._release_expired_empty_failure(tenant, run, cutoff))
            except Exception:  # noqa: BLE001 - one blocked owner never grants cleanup or stalls the bounded cursor.
                _LOGGER.warning('python_failed_empty_maintenance_blocked run_id=%s', run)
            self._failed_empty_cursor = run
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
            candidates = tuple(
                (_database_uuid(row.tenant_id), _database_uuid(row.id), RunStatus(row.status))
                for row in rows
            )
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
                    "python_project_cleanup_retry_failed run_id=%s error_type=%s diagnosis=%s",
                    run_id,
                    _retry_error_type(error),
                    _retry_failure_diagnosis(error),
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
