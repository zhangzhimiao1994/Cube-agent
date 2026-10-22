"""Database repository for durable run execution state."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import PluginApprovalIdentity
from agent_hub.db.models import (
    ConversationQueueItemRow,
    ConversationRow,
    RunApprovalRow,
    RunArtifactRow,
    RunCheckpointRow,
    RunEventRow,
    RunOutboxRow,
    RunRow,
    RunStepRow,
    RunUsageRow,
    UserRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.context_evidence import CONTEXT_EVENT_KINDS, context_event_projection
from agent_hub.runs.self_repair import repair_context_from_proposal
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.failure_reason import (
    RECOVERY_BLOCKED_FAILURE_REASON,
    is_public_project_prerequisite_text,
    runtime_failure_diagnostic_from_reason,
)

if TYPE_CHECKING:
    from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority


def _database_uuid(value: UUID) -> UUID:
    # asyncpg's UUID subclass must not cross strict runtime identity boundaries.
    if not isinstance(value, UUID):
        raise TypeError('database identity must be a UUID')
    return value if type(value) is UUID else UUID(str(value))


def _optional_database_uuid(value: UUID | None) -> UUID | None:
    return None if value is None else _database_uuid(value)


class _PythonApprovalOptions(TypedDict, total=False):
    python_binding: dict[str, object]
    python_input_binding: dict[str, object]
    toolchain_input_binding: dict[str, object]


class _ToolchainReviewOptions(TypedDict, total=False):
    approval_scope: str
    toolchain_input_binding: dict[str, object]


def _has_generated_preparation(route: Mapping[str, object]) -> bool:
    # Even malformed or mixed markers must not acquire original-input credit.
    return bool({'generated_project_contract_version', 'generated_project_preparation'} & route.keys())


def _pre_model_original_capture(route: Mapping[str, object]) -> bool:
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.project_contracts import ManagedProjectInput

    state = route.get('project_input_preparation')
    if (_has_generated_preparation(route)
            or type(route.get('project_input_contract_version')) is not int
            or route.get('project_input_contract_version') != 1
            or not isinstance(state, Mapping)
            or type(state.get('schema_version')) is not int or state['schema_version'] not in {1, 2}
            or state.get('phase') != 'captured'):
        return False
    expected_keys = {'schema_version', 'phase', 'reference'}
    if state['schema_version'] == 2:
        expected_keys.add('python_recipe')
        selection = route.get('project_input')
        if (_python_recipe_marker(state.get('python_recipe')) is None
                or not isinstance(selection, Mapping)
                or set(selection) != {'schema_version', 'kind', 'editable_paths'}):
            return False
    if set(state) != expected_keys:
        return False
    try:
        ProjectBaselineReference.from_payload(state['reference'])
        ManagedProjectInput.from_payload(route.get('project_input'))
    except (ValueError, TypeError):
        return False
    return True


def _python_recipe_marker(value: object) -> dict[str, object] | None:
    if (not isinstance(value, Mapping)
            or set(value) != {'schema_version', 'recipe_sha256', 'environment_input_sha256'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or any(type(value.get(key)) is not str
                   or re.fullmatch(r'[a-f0-9]{64}', value[key]) is None
                   for key in ('recipe_sha256', 'environment_input_sha256'))):
        return None
    return dict(value)


def _python_project_approval_binding(run: RunRow, approval_scope: object) -> dict[str, object] | None:
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.project_contracts import ManagedProjectInput
    from agent_hub.capabilities.scoped_read import ScopedReadError, authorized_workspace_scope

    route = run.routing_decision or {}
    if 'generated_project_contract_version' in route or 'generated_project_preparation' in route:
        return _generated_python_approval_binding(run, approval_scope, supply=False)
    state = route.get('project_input_preparation')
    if (not isinstance(run.actor_id, UUID)
            or type(route.get('project_input_contract_version')) is not int
            or route['project_input_contract_version'] != 1
            or not isinstance(state, Mapping)
            or type(state.get('schema_version')) is not int or state['schema_version'] != 2
            or type(state.get('phase')) is not str
            or state.get('phase') not in {'captured', 'executing', 'observed'}):
        return None
    keys = {'schema_version', 'phase', 'reference', 'python_recipe'}
    if state['phase'] in {'executing', 'observed'}:
        keys.add('execution_id')
        if state.get('execution_id') != 'original_' + run.id.hex:
            return None
    if state['phase'] == 'observed':
        keys.update({'observation', 'observation_sha256'})
        if (not isinstance(state.get('observation'), Mapping)
                or type(state.get('observation_sha256')) is not str
                or re.fullmatch(r'[a-f0-9]{64}', state['observation_sha256']) is None):
            return None
    selection = route.get('project_input')
    marker = _python_recipe_marker(state.get('python_recipe'))
    if (set(state) != keys or marker is None
            or not isinstance(selection, Mapping)
            or set(selection) != {'schema_version', 'kind', 'editable_paths'}
            or route.get('sandbox_profile') != 'workspace_write'
            or route.get('execution_backend', 'systemd') != 'systemd'):
        return None
    try:
        ProjectBaselineReference.from_payload(state['reference'])
        ManagedProjectInput.from_payload(selection)
        project, workspace_session = authorized_workspace_scope(
            run, tenant_id=run.tenant_id, run_id=run.id,
        )
    except (ValueError, TypeError, ScopedReadError):
        return None
    permissions = route.get('requested_permissions')
    if not isinstance(permissions, (list, tuple)) or not {'command.run', 'workspace.write'}.issubset(permissions):
        return None
    resource = 'workspace/current/project-verification/python/' + cast(str, marker['recipe_sha256'])
    expected_scope = (
        f'capability-scope:v1:tenant={run.tenant_id}:user={run.actor_id}:run={run.id}:'
        f'capability=command:operation=run:resource={resource}'
    )
    if type(approval_scope) is not str or approval_scope != expected_scope:
        return None
    return {**marker, 'project_id': project, 'workspace_session_id': workspace_session}


def _python_input_supply_approval_binding(run: RunRow, approval_scope: object) -> dict[str, object] | None:
    if (type(approval_scope) is not str
            or ':capability=command:operation=run:resource=workspace/current/project-inputs/python/'
                not in approval_scope):
        return None
    try:
        from agent_hub.capabilities.approvals import capability_approval_scope
        from agent_hub.capabilities.python_input_preparation import input_supply_binding

        route = run.routing_decision or {}
        if 'generated_project_contract_version' in route or 'generated_project_preparation' in route:
            return _generated_python_approval_binding(run, approval_scope, supply=True)
        request = input_supply_binding(run)
        if capability_approval_scope(request) != approval_scope:
            return None
        return request.arguments
    except (ValueError, TypeError, RuntimeError, LookupError):
        return None


def _is_toolchain_supply_scope(scope: object) -> bool:
    return isinstance(scope, str) and ':resource=workspace/current/project-inputs/toolchain/' in scope


def _toolchain_supply_approval_binding(
    run: RunRow, approval_scope: object, *, planned: bool = False,
) -> dict[str, object] | None:
    if not _is_toolchain_supply_scope(approval_scope):
        return None
    try:
        from agent_hub.capabilities.approvals import capability_approval_scope
        from agent_hub.capabilities.toolchain_preparation import toolchain_supply_binding

        request = toolchain_supply_binding(run, planned=planned)
        return request.arguments if approval_scope == capability_approval_scope(request) else None
    except (ValueError, TypeError, RuntimeError, LookupError):
        return None


def _toolchain_approval_matches(run: RunRow, approval: RunApprovalRow) -> bool:
    """Exact SDK binding is rederived from DB state, never a receipt or Python grant."""
    payload = approval.payload
    expected = _toolchain_supply_approval_binding(run, payload.get('approval_scope'))
    stored = payload.get('toolchain_input_binding')
    if expected is None or type(stored) is not dict or {
        'python_binding', 'python_input_binding', 'public_binding',
    } & payload.keys():
        return False
    try:
        return json.dumps(stored, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True)
    except (ValueError, TypeError, RecursionError):
        return False


def _generated_python_approval_binding(run: RunRow, scope: object, *, supply: bool) -> dict[str, object] | None:
    from agent_hub.capabilities.approvals import capability_approval_scope
    from agent_hub.capabilities.python_generated_service import generated_python_approval_request

    try:
        state = (run.routing_decision or {}).get('generated_project_preparation')
        if not isinstance(state, Mapping) or not isinstance(state.get('proposer_actor'), str):
            return None
        request = generated_python_approval_request(run, agent_id=state['proposer_actor'],
            idempotency_key='generated-python-binding')
        prefix = 'workspace/current/project-inputs/python/' if supply else 'workspace/current/project-verification/python/'
        if request is None or not request.resource.startswith(prefix) or capability_approval_scope(request) != scope:
            return None
        return dict(request.arguments)
    except (ValueError, TypeError, RuntimeError, LookupError):
        return None


_RECOVERY_REPLAYABLE_EVENT_KINDS = frozenset(
    {
        EventKind.APPROVAL_REQUESTED.value,
        EventKind.APPROVAL_RESOLVED.value,
        "harness.started",
        "observer.notice",
        "repair.classified",
        "repair.started",
        "runtime.recovered",
    }
)
_SELF_REPAIR_RECOVERY_BASELINE_EVENT_KINDS = frozenset(
    {
        EventKind.RUNTIME_FAILED.value,
        EventKind.STEP_FAILED.value,
        "observer.notice",
        "repair.classified",
        "terminal.notified",
    }
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    request: str
    mode: TaskMode | None
    status: RunStatus
    version: int
    created_at: datetime
    routing_decision: dict[str, object] | None
    blocked_by_run_id: UUID | None = None
    actor_role: Role | None = None
    worker_id: str | None = None
    worker_lease_token: UUID | None = None
    worker_lease_expires_at: datetime | None = None
    worker_heartbeat_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PendingOutbox:
    id: UUID
    run_id: UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ConversationContextItem:
    run_id: UUID
    request: str
    artifacts: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class ConversationQuestionSearchItem:
    run_id: UUID
    conversation_id: str
    project_id: str
    question: str
    created_at: datetime
    conversation_title: str


@dataclass(frozen=True, slots=True)
class ConversationQuestionSearchPage:
    items: tuple[ConversationQuestionSearchItem, ...]
    next_cursor: str | None


class RunNotFound(RuntimeError):
    """Stable missing-run error."""


class RunConflict(RuntimeError):
    """Stable run-state conflict error."""


class RunAlreadyActive(RuntimeError):
    """The run is already owned by another worker."""


_SENSITIVE_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "secret",
        "secret_ref",
        "credential_ref",
        "storage_key",
        "hidden_reasoning",
        "chain_of_thought",
        "reasoning_content",
        "provider_continuation",
        "native_assistant",
    }
)
_SENSITIVE_PUBLIC_TEXT = re.compile(
    r"(?:api[_ -]?key|(?<![a-z0-9])sk-[a-z0-9_-]{8,}|bearer\s+|authorization|password|secret|token"
    r"|\b(?:set[-_]cookie|cookie)\s*:|\b(?:sessionid|session_id)\s*=)",
    re.IGNORECASE,
)
_SAFE_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _safe_temporary_agent_model(proposal: dict[object, object]) -> str:
    for key in ("model", "recommended_model"):
        value = proposal.get(key)
        if isinstance(value, str) and _SAFE_MODEL_ID.fullmatch(value):
            return value
    raise RunConflict("temporary agent proposal has no safe model")


def _temporary_agent_approval_already_applied(
    routing_decision: dict[str, object],
    decision_token: str,
) -> bool:
    return (
        routing_decision.get("approval_kind") == "temporary_agent_creation"
        and routing_decision.get("decision_token") == decision_token
        and routing_decision.get("temporary_agent_approved") is True
    )


def _temporary_agent_revision_already_applied(
    routing_decision: dict[str, object],
    *,
    decision_token: str,
    feedback: str,
) -> bool:
    return (
        routing_decision.get("approval_kind") == "temporary_agent_creation"
        and routing_decision.get("decision_token") == decision_token
        and routing_decision.get("temporary_agent_rejected") is True
        and routing_decision.get("temporary_agent_feedback") == feedback
    )


def _project_preflight_approval_already_applied(
    routing_decision: dict[str, object],
    decision_token: str,
) -> bool:
    return (
        routing_decision.get("approval_kind") == "project_preflight"
        and routing_decision.get("decision_token") == decision_token
        and routing_decision.get("project_preflight_approved") is True
    )


def _self_repair_decision_token_hash(decision_token: str) -> str:
    return hashlib.sha256(decision_token.encode("utf-8")).hexdigest()


def _self_repair_acceptance_already_applied(
    routing_decision: dict[str, object],
    decision_token: str,
) -> bool:
    return (
        routing_decision.get("source") == "self_repair"
        and routing_decision.get("self_repair_accepted") is True
        and routing_decision.get("self_repair_decision_token_hash")
        == _self_repair_decision_token_hash(decision_token)
    )


def _retained_self_repair_contexts(
    decision: dict[str, object], *, run_id: UUID, next_attempt: int, next_sequence: object,
) -> list[dict[str, object]]:
    if decision.get("source") != "self_repair" or decision.get("self_repair_accepted") is not True:
        return []
    previous = decision.get("self_repair_context")
    if previous is None:
        return []
    history = decision.get("self_repair_context_history", [])
    invalid = "self repair context history is invalid"
    previous_attempt = decision.get("self_repair_attempt")
    if (type(previous_attempt) is not int or not 1 <= previous_attempt < next_attempt <= 3
            or next_attempt != previous_attempt + 1 or type(next_sequence) is not int
            or not 0 < next_sequence <= 2**63 - 1 or not isinstance(history, list)
            or len(history) != previous_attempt - 1):
        raise RunConflict(invalid)
    retained: list[dict[str, object]] = []
    last_sequence = 0
    for expected_attempt, context in enumerate([*history, previous], start=1):
        if not isinstance(context, dict):
            raise RunConflict(invalid)
        sequence, attempt, maximum = (context.get(key) for key in
                                      ("source_event_sequence", "attempt", "max_attempts"))
        if (context.get("source") != "self_repair" or context.get("source_run_id") != str(run_id)
                or type(sequence) is not int or not last_sequence < sequence < next_sequence
                or type(attempt) is not int or attempt != expected_attempt
                or type(maximum) is not int or not attempt <= maximum <= 3):
            raise RunConflict(invalid)
        bounded = repair_context_from_proposal(context)
        try:
            canonical = json.dumps(bounded, sort_keys=True, allow_nan=False)
            unchanged = canonical == json.dumps(context, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            unchanged = False
        if not unchanged:
            raise RunConflict(invalid)
        retained.append(bounded)
        last_sequence = sequence
    if (not isinstance(previous, dict)
            or decision.get("self_repair_source_run_id") != str(run_id)
            or type(decision.get("self_repair_source_event_sequence")) is not int
            or decision.get("self_repair_source_event_sequence") != last_sequence
            or type(decision.get("self_repair_max_attempts")) is not int
            or decision.get("self_repair_max_attempts") != previous.get("max_attempts")):
        raise RunConflict(invalid)
    return retained


def _mode_choice_already_applied(
    routing_decision: dict[str, object],
    *,
    decision_token: str,
    mode: TaskMode,
    operator_note: str | None,
) -> bool:
    if (
        routing_decision.get("decision_token") != decision_token
        or routing_decision.get("selected_mode") != mode.value
    ):
        return False
    if operator_note:
        return routing_decision.get("operator_note") == operator_note
    return "operator_note" not in routing_decision


class RunRepository:
    """Persist runs and normalized runtime events behind tenant boundaries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_run(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_role: Role | None = None,
        request: str,
        mode: TaskMode | None,
        status: RunStatus,
        idempotency_key: str | None,
        routing_decision: dict[str, object] | None = None,
        blocked_by_run_id: UUID | None = None,
        enqueue: bool,
    ) -> RunRecord:
        if blocked_by_run_id is None and routing_decision is not None:
            raw_blocked_by_run_id = routing_decision.get("blocked_by_run_id")
            if isinstance(raw_blocked_by_run_id, str):
                try:
                    blocked_by_run_id = UUID(raw_blocked_by_run_id)
                except ValueError:
                    raise ValueError("blocked_by_run_id must be a UUID") from None
        run_id = uuid4()
        outbox_id = uuid4()
        outbox_key = f"{tenant_id}:{idempotency_key or run_id}"
        async with self._session_factory() as session, session.begin():
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(RunRow).where(
                        RunRow.tenant_id == tenant_id,
                        RunRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return self._record(existing)
            row = RunRow(
                id=run_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                actor_role=None if actor_role is None else actor_role.value,
                request=request,
                mode=None if mode is None else mode.value,
                status=status.value,
                idempotency_key=idempotency_key,
                routing_decision=routing_decision,
                blocked_by_run_id=blocked_by_run_id,
                version=1,
            )
            session.add(row)
            await session.flush()
            if enqueue and blocked_by_run_id is None:
                session.add(
                    RunOutboxRow(
                        id=outbox_id,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_name="agent_hub.runs.execute",
                        idempotency_key=outbox_key,
                        payload={"run_id": str(run_id)},
                    )
                )
            await session.flush()
            return self._record(row)

    async def get(
        self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session:
            row = await session.scalar(self._run_select(tenant_id, run_id))
            if row is None:
                raise RunNotFound("run was not found")
            if actor_id is not None and row.actor_id != actor_id:
                raise RunNotFound("run was not found")
            return self._record(row)

    async def latest_waiting_choice_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
    ) -> RunRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(RunRow)
                .where(RunRow.tenant_id == tenant_id)
                .where(RunRow.actor_id == actor_id)
                .where(RunRow.status == RunStatus.WAITING_USER_MODE.value)
                .where(RunRow.routing_decision["conversation_id"].astext == conversation_id)
                .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                .limit(1)
            )
            return None if row is None else self._record(row)

    async def latest_waiting_mode_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
    ) -> RunRecord | None:
        return await self.latest_waiting_choice_for_conversation(
            tenant_id=tenant_id,
            actor_id=actor_id,
            conversation_id=conversation_id,
        )

    async def latest_resolved_mode_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
    ) -> TaskMode | None:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(RunRow.mode)
                .where(RunRow.tenant_id == tenant_id)
                .where(RunRow.actor_id == actor_id)
                .where(RunRow.mode.is_not(None))
                .where(RunRow.routing_decision["conversation_id"].astext == conversation_id)
                .where(RunRow.status != RunStatus.WAITING_USER_MODE.value)
                .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                .limit(1)
            )
        if not isinstance(value, str):
            return None
        try:
            mode = TaskMode(value)
        except ValueError:
            return None
        return None if mode is TaskMode.AUTO else mode

    async def list_recent(self, tenant_id: UUID, *, limit: int = 100) -> tuple[RunRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("run list limit must be between 1 and 500")
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunRow)
                    .where(RunRow.tenant_id == tenant_id)
                    .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(self._record(row) for row in rows)

    async def search_conversation_questions(
        self,
        tenant_id: UUID,
        *,
        q: str,
        project_id: str | None = None,
        archived: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ConversationQuestionSearchPage:
        normalized_query = q.strip()
        if not normalized_query:
            raise ValueError("question search query must not be empty")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("question search limit must be between 1 and 50")
        normalized_project_id = None if project_id is None else project_id.strip()
        if project_id is not None and not normalized_project_id:
            raise ValueError("project_id must not be empty")

        escaped_query = (
            normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        statement = (
            select(
                RunRow.id.label("run_id"),
                ConversationRow.conversation_id,
                ConversationRow.project_id,
                RunRow.request.label("question"),
                RunRow.created_at,
                ConversationRow.title.label("conversation_title"),
            )
            .join(
                ConversationRow,
                and_(
                    ConversationRow.tenant_id == RunRow.tenant_id,
                    ConversationRow.conversation_id
                    == RunRow.routing_decision["conversation_id"].astext,
                ),
            )
            .where(RunRow.tenant_id == tenant_id)
            .where(RunRow.request.ilike(f"%{escaped_query}%", escape="\\"))
            .where(
                ConversationRow.archived_at.is_not(None)
                if archived
                else ConversationRow.archived_at.is_(None)
            )
        )
        if normalized_project_id is not None:
            statement = statement.where(ConversationRow.project_id == normalized_project_id)
        if cursor is not None:
            cursor_created_at, cursor_run_id = _decode_question_search_cursor(cursor)
            statement = statement.where(
                or_(
                    RunRow.created_at < cursor_created_at,
                    and_(
                        RunRow.created_at == cursor_created_at,
                        RunRow.id < cursor_run_id,
                    ),
                )
            )
        statement = statement.order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(limit + 1)

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).mappings().all()

        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = tuple(
            ConversationQuestionSearchItem(
                run_id=row["run_id"],
                conversation_id=row["conversation_id"],
                project_id=row["project_id"],
                question=_question_search_excerpt(row["question"], normalized_query),
                created_at=row["created_at"],
                conversation_title=row["conversation_title"],
            )
            for row in visible_rows
        )
        next_cursor = None
        if has_more and items:
            last_item = items[-1]
            next_cursor = _encode_question_search_cursor(last_item.created_at, last_item.run_id)
        return ConversationQuestionSearchPage(items=items, next_cursor=next_cursor)

    async def list_conversation(
        self,
        tenant_id: UUID,
        conversation_id: str,
    ) -> tuple[RunRecord, ...]:
        normalized_conversation_id = conversation_id.strip()
        if not normalized_conversation_id:
            return ()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunRow)
                    .where(RunRow.tenant_id == tenant_id)
                    .where(
                        RunRow.routing_decision["conversation_id"].astext
                        == normalized_conversation_id
                    )
                    .order_by(RunRow.created_at.asc(), RunRow.id.asc())
                )
            ).all()
            return tuple(self._record(row) for row in rows)

    async def delete_run(self, tenant_id: UUID, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            status = RunStatus(row.status)
            if status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                raise RunConflict("run must be completed, failed, or cancelled before deletion")
            if self.has_worker_execution(row):
                raise RunConflict("run worker execution has not been released")

            blocked_successor = await session.scalar(
                select(RunRow.id)
                .where(RunRow.tenant_id == tenant_id, RunRow.blocked_by_run_id == run_id)
                .limit(1)
            )
            if blocked_successor is not None:
                raise RunConflict("run still has a blocked conversation successor")

            await session.execute(
                delete(ConversationQueueItemRow).where(
                    ConversationQueueItemRow.tenant_id == tenant_id,
                    (
                        (ConversationQueueItemRow.predecessor_run_id == run_id)
                        | (ConversationQueueItemRow.successor_run_id == run_id)
                    ),
                )
            )

            for table in (
                RunOutboxRow,
                RunApprovalRow,
                RunUsageRow,
                RunCheckpointRow,
                RunArtifactRow,
                RunStepRow,
                RunEventRow,
            ):
                await session.execute(
                    delete(table).where(table.tenant_id == tenant_id, table.run_id == run_id)
                )
            await session.execute(
                delete(RunRow).where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)
            )

    async def get_for_update(self, session: AsyncSession, run_id: UUID) -> RunRow:
        row = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
        if row is None:
            raise RunNotFound("run was not found")
        return row

    async def _locked_toolchain_supply(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID,
        authority: ToolchainSupplyAuthority,
    ) -> RunRow:
        from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority

        if type(authority) is not ToolchainSupplyAuthority:
            raise RunConflict('toolchain_scope_changed')
        row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
        if row is None:
            raise RunNotFound('run was not found')
        try:
            record = self._record(row)
        except (ValueError, TypeError):
            raise RunConflict('toolchain_scope_changed') from None
        authority.require_current(record, tenant_id, run_id)
        user = await session.scalar(select(UserRow).where(
            UserRow.tenant_id == tenant_id, UserRow.id == authority.actor_id).with_for_update())
        if (user is None or user.tenant_id != tenant_id or user.id != authority.actor_id
                or user.disabled is not False or user.role != authority.actor_role.value):
            raise RunConflict('toolchain_scope_changed')
        authority.require_current(self._record(row), tenant_id, run_id)
        return row

    @staticmethod
    async def _require_toolchain_supply_grant(session: AsyncSession, row: RunRow) -> None:
        rows = (await session.scalars(select(RunApprovalRow).where(
            RunApprovalRow.tenant_id == row.tenant_id,
            RunApprovalRow.run_id == row.id,
            RunApprovalRow.status == 'approved',
        ).with_for_update())).all()
        if not any(approval.tenant_id == row.tenant_id and approval.run_id == row.id
                   and approval.status == 'approved' and _toolchain_approval_matches(row, approval)
                   for approval in rows):
            raise RunConflict('toolchain_supply_approval_required')

    async def get_toolchain_supply_state(
        self, tenant_id: UUID, run_id: UUID, *, authority: ToolchainSupplyAuthority,
    ) -> Mapping[str, JsonValue] | None:
        from agent_hub.runs.toolchain_supply import supply_state

        async with self._session_factory() as session, session.begin():
            row = await self._locked_toolchain_supply(session, tenant_id, run_id, authority)
            state = supply_state(self._record(row))
            if state is not None and state['phase'] in {'executing', 'supplied'}:
                await self._require_toolchain_supply_grant(session, row)
            authority.require_current(self._record(row), tenant_id, run_id)
        return state

    async def compare_and_set_toolchain_supply(
        self, tenant_id: UUID, run_id: UUID, *, authority: ToolchainSupplyAuthority,
        expected: Mapping[str, JsonValue] | None, replacement: Mapping[str, JsonValue],
    ) -> RunRecord:
        from agent_hub.runs.toolchain_supply import supply_transition
        from agent_hub.runtime.contracts import _freeze_object

        expected = None if expected is None else _freeze_object(expected, name='expected supply')
        replacement = _freeze_object(replacement, name='replacement supply')
        async with self._session_factory() as session, session.begin():
            row = await self._locked_toolchain_supply(session, tenant_id, run_id, authority)
            after = supply_transition(self._record(row), expected, replacement)
            if after['phase'] in {'executing', 'supplied'}:
                await self._require_toolchain_supply_grant(session, row)
            authority.require_current(self._record(row), tenant_id, run_id)
            row.routing_decision = {**(row.routing_decision or {}),
                                    'toolchain_supply': json.loads(json.dumps(after, default=dict))}
            row.version += 1
            await session.flush()
            result = self._record(row)
        return result

    async def compare_and_set_generated_preparation(
        self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID, actor_role: Role,
        lease_token: UUID, request_sha256: str, project_id: str, session_id: str,
        expected: Mapping[str, JsonValue] | None, replacement: Mapping[str, JsonValue],
    ) -> RunRecord:
        from agent_hub.capabilities.generated_project_service import (
            STATE,
            generated_authority,
            validate_generated_preparation,
            validate_generated_transition,
        )

        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound('run was not found')
            record = self._record(row)
            authority = generated_authority(record, tenant_id, run_id)
            if (authority.user_id != actor_id or authority.role is not actor_role
                    or authority.lease_token != lease_token or authority.request_sha256 != request_sha256
                    or (authority.project_id, authority.session_id) != (project_id, session_id)):
                raise RunConflict('generated_acceptance_scope_changed')
            route = dict(record.routing_decision or {})
            previous = route.get(STATE)
            normalized = None if previous is None else validate_generated_preparation(previous)
            if normalized != expected:
                raise RunConflict('generated_acceptance_state_changed')
            after = validate_generated_transition(previous, replacement)
            row.routing_decision = {**route, STATE: json.loads(json.dumps(after, default=dict))}
            row.version += 1
            await session.flush()
            return self._record(row)

    async def update_status(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        mode: TaskMode | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            row.status = status.value
            if mode is not None:
                row.mode = mode.value
            row.version += 1
            await session.flush()
            return self._record(row)

    async def choose_mode_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        mode: TaskMode,
        decision_token: str,
        version: int,
        operator_note: str | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if RunStatus(row.status) is not RunStatus.WAITING_USER_MODE:
                if _mode_choice_already_applied(
                    routing_decision,
                    decision_token=decision_token,
                    mode=mode,
                    operator_note=operator_note,
                ):
                    return self._record(row)
                raise RunConflict("run is not waiting for a mode choice")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("mode choice token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")

            row.mode = mode.value
            updated_routing_decision = {
                **routing_decision,
                "selected_mode": mode.value,
                "effective_mode": mode.value,
                "mode_source": "user_mode_choice",
                **({"operator_note": operator_note} if operator_note else {}),
            }
            project_scale = updated_routing_decision.get("project_scale")
            if isinstance(project_scale, str) and project_scale in {
                "small",
                "medium",
                "large",
                "ultra",
            }:
                updated_routing_decision["effective_scale"] = project_scale
            elif "project_scale" in updated_routing_decision:
                updated_routing_decision.pop("effective_scale", None)
            route_reason = updated_routing_decision.get("reason")
            if isinstance(route_reason, str) and route_reason.strip():
                updated_routing_decision["route_reason"] = route_reason
            elif "reason" in updated_routing_decision:
                updated_routing_decision.pop("route_reason", None)
            row.routing_decision = updated_routing_decision
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:choose-mode:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def approve_temporary_agent_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                if _temporary_agent_approval_already_applied(
                    routing_decision,
                    decision_token,
                ):
                    return self._record(row)
                raise RunConflict("run is not waiting for temporary agent approval")
            if routing_decision.get("approval_kind") != "temporary_agent_creation":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("temporary agent approval token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")
            proposal = routing_decision.get("temporary_agent_proposal")
            if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str):
                raise RunConflict("temporary agent proposal is invalid")
            selected_model = _safe_temporary_agent_model(proposal)
            proposal = {**proposal, "model": selected_model}
            selected = routing_decision.get("selected_agent_ids")
            selected_agent_ids = (
                [item for item in selected if isinstance(item, str)]
                if isinstance(selected, list)
                else []
            )
            proposal_id = proposal["id"]
            if proposal_id not in selected_agent_ids:
                selected_agent_ids.append(proposal_id)
            row.routing_decision = {
                **routing_decision,
                "selected_agent_ids": selected_agent_ids,
                "temporary_agents": [proposal],
                "temporary_agent_approved": True,
            }
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:temporary-agent:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def revise_temporary_agent_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
        feedback: str,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                if _temporary_agent_revision_already_applied(
                    routing_decision,
                    decision_token=decision_token,
                    feedback=feedback,
                ):
                    return self._record(row)
                raise RunConflict("run is not waiting for temporary agent approval")
            if routing_decision.get("approval_kind") != "temporary_agent_creation":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("temporary agent revision token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")
            row.request = f"{row.request}\n\nUser feedback for temporary agent proposal: {feedback}"
            row.routing_decision = {
                **routing_decision,
                "temporary_agent_rejected": True,
                "temporary_agent_feedback": feedback,
                "temporary_agents": [],
                "workflow_adjustment_policy": "ask_before_apply",
            }
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:temporary-agent-revision:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def approve_project_preflight_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                if _project_preflight_approval_already_applied(routing_decision, decision_token):
                    return self._record(row)
                raise RunConflict("run is not waiting for project preflight approval")
            if routing_decision.get("approval_kind") != "project_preflight":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("project preflight approval token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")
            proposal = routing_decision.get("project_preflight_proposal")
            if not isinstance(proposal, dict) or proposal.get("kind") != "project_architecture_preflight":
                raise RunConflict("project preflight proposal is invalid")
            row.routing_decision = {
                **routing_decision,
                "project_preflight_approved": True,
            }
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:project-preflight:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def accept_self_repair_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if RunStatus(row.status) not in {
                RunStatus.FAILED,
                RunStatus.WAITING_APPROVAL,
            }:
                if _self_repair_acceptance_already_applied(routing_decision, decision_token):
                    return self._record(row)
                raise RunConflict("run is not waiting for self repair")
            if routing_decision.get("approval_kind") != "self_repair":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("self repair approval token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")
            proposal = routing_decision.get("repair_proposal")
            if not isinstance(proposal, dict) or proposal.get("kind") != "self_repair":
                raise RunConflict("self repair proposal is invalid")
            attempt = proposal.get("attempt", 1)
            max_attempts = proposal.get("max_attempts", 1)
            if type(attempt) is not int or type(max_attempts) is not int or attempt > max_attempts:
                raise RunConflict("self repair proposal exceeds retry budget")
            retained_contexts = _retained_self_repair_contexts(
                routing_decision, run_id=run_id, next_attempt=attempt,
                next_sequence=proposal.get("source_event_sequence"),
            )
            recovery_baseline_sequence = await self.next_event_sequence(session, row.id) - 1
            next_decision = {
                key: value
                for key, value in routing_decision.items()
                if key not in {"approval_kind", "decision_token"}
            }
            next_decision.update(
                {
                    "source": "self_repair",
                    "self_repair_accepted": True,
                    "self_repair_attempt": attempt,
                    "self_repair_max_attempts": max_attempts,
                    "self_repair_source_run_id": str(run_id),
                    "self_repair_source_event_sequence": proposal.get("source_event_sequence"),
                    "self_repair_recovery_baseline_sequence": recovery_baseline_sequence,
                    "self_repair_fingerprint": proposal.get("fingerprint"),
                    "self_repair_decision_token_hash": _self_repair_decision_token_hash(
                        decision_token
                    ),
                    "self_repair_context": repair_context_from_proposal(proposal),
                }
            )
            if retained_contexts:
                next_decision["self_repair_context_history"] = retained_contexts
            else:
                next_decision.pop("self_repair_context_history", None)
            row.routing_decision = next_decision
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:self-repair:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def retry_self_repair_recovery_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        version: int,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None or row.tenant_id != tenant_id or row.id != run_id or row.actor_id != actor_id:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.FAILED or row.version != version:
                raise RunConflict("run is not awaiting a recovery retry at this version")
            if self.has_worker_execution(row) or row.blocked_by_run_id is not None:
                raise RunConflict("run recovery retry is not quiescent")
            routing = dict(row.routing_decision or {})
            baseline = _self_repair_recovery_baseline_sequence(routing)
            if baseline <= 0 or routing.get("self_repair_source_run_id") != str(run_id):
                raise RunConflict("run has no approved self repair recovery baseline")
            checkpoint = await self.latest_checkpoint(session, tenant_id=tenant_id, run_id=run_id)
            if checkpoint is None or checkpoint.tenant_id != tenant_id or checkpoint.run_id != run_id:
                raise RunConflict("run recovery checkpoint is unavailable")
            tail = (await session.execute(
                select(RunEventRow.sequence, RunEventRow.kind, RunEventRow.payload)
                .where(
                    RunEventRow.tenant_id == tenant_id,
                    RunEventRow.run_id == run_id,
                    RunEventRow.sequence > baseline,
                )
                .order_by(RunEventRow.sequence)
            )).all()
            if not tail or any(
                kind != EventKind.RUNTIME_FAILED.value
                or not isinstance(payload, dict)
                or payload.get("reason") != RECOVERY_BLOCKED_FAILURE_REASON
                for _, kind, payload in tail
            ):
                raise RunConflict("run recovery retry requires only preexecution guard failures")
            last_sequence = tail[-1][0]
            if await self._recovery_blocked_after_checkpoint(
                session, run_id=run_id, minimum_event_sequence=last_sequence,
            ):
                raise RunConflict("run recovery retry has non-replayable execution after checkpoint")
            row.routing_decision = {**routing, "self_repair_recovery_baseline_sequence": last_sequence}
            row.status = RunStatus.QUEUED.value
            row.version += 1
            session.add(RunOutboxRow(
                id=uuid4(), tenant_id=tenant_id, run_id=run_id,
                task_name="agent_hub.runs.execute",
                idempotency_key=f"{tenant_id}:{run_id}:recovery-retry:{version}",
                payload={"run_id": str(run_id)},
            ))
            await session.flush()
            return self._record(row)

    async def enqueue_existing_run(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        from_status: RunStatus,
        to_status: RunStatus = RunStatus.QUEUED,
        idempotency_suffix: str,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            current_status = RunStatus(row.status)
            if current_status is to_status:
                return self._record(row)
            if current_status is not from_status:
                raise RunConflict("run state conflict")
            if from_status is RunStatus.PAUSED and self.has_worker_execution(row):
                raise RunConflict("run worker execution has not been released")
            row.status = to_status.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:{idempotency_suffix}:{row.version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def claim_for_execution(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
        worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
        worker_lease_expires_at: datetime | None = None,
    ) -> tuple[RunRow, RuntimeCheckpoint | None] | RunRecord:
        row = await self.get_for_update(session, run_id)
        status = RunStatus(row.status)
        mode = TaskMode(row.mode) if row.mode is not None else None
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return self._record(row)
        if mode is None:
            return self._record(row)
        if row.blocked_by_run_id is not None:
            raise RunConflict("run is blocked by an active conversation run")
        if status is RunStatus.RUNNING and not allow_running_recovery:
            raise RunAlreadyActive("run is already active")
        if (
            status is RunStatus.RUNNING
            and allow_running_recovery
            and row.worker_lease_expires_at is not None
            and row.worker_lease_expires_at > datetime.now(UTC)
        ):
            raise RunAlreadyActive("run is already active")
        if status not in {RunStatus.QUEUED, RunStatus.RETRYING, RunStatus.RUNNING}:
            raise RunConflict("run cannot be executed from its current state")
        row.status = RunStatus.RUNNING.value
        row.version += 1
        checkpoint = await self.latest_checkpoint(session, tenant_id=row.tenant_id, run_id=row.id)
        should_guard_recovery = (allow_running_recovery and status is RunStatus.RUNNING) or (
            status in {RunStatus.QUEUED, RunStatus.RETRYING} and checkpoint is not None
        )
        if should_guard_recovery:
            minimum_event_sequence = _self_repair_recovery_baseline_sequence(
                row.routing_decision
            )
            recovery_blocked = await self._recovery_blocked_after_checkpoint(
                session,
                run_id=row.id,
                minimum_event_sequence=minimum_event_sequence,
            )
            if recovery_blocked:
                sequence = await self.next_event_sequence(session, row.id)
                await self.persist_event(
                    session,
                    tenant_id=row.tenant_id,
                    run_id=row.id,
                    event=RunEvent(
                        kind=EventKind.RUNTIME_FAILED,
                        sequence=sequence,
                        run_id=row.id,
                        reason=RECOVERY_BLOCKED_FAILURE_REASON,
                    ),
                )
                row.status = RunStatus.FAILED.value
                self.clear_worker_lease(row)
                row.version += 1
                await session.flush()
                return self._record(row)
        self.record_worker_lease(
            row,
            worker_id=worker_id,
            worker_lease_token=worker_lease_token,
            worker_lease_expires_at=worker_lease_expires_at,
        )
        return row, checkpoint

    async def running_for_recovery(self, limit: int, *, now: datetime) -> tuple[UUID, ...]:
        if limit <= 0:
            return ()
        async with self._session_factory() as session:
            rows = await session.scalars(
                self._running_for_recovery_select(limit=limit, now=now)
            )
            return tuple(_database_uuid(run_id) for run_id in rows.all())

    async def terminal_worker_executions_for_recovery(
        self,
        limit: int,
        *,
        now: datetime,
    ) -> tuple[RunRecord, ...]:
        if limit <= 0:
            return ()
        async with self._session_factory() as session:
            rows = await session.scalars(
                self._terminal_worker_executions_for_recovery_select(limit=limit, now=now)
            )
            return tuple(self._record(row) for row in rows.all())

    async def renew_active_worker_lease(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        worker_id: str,
        worker_lease_token: UUID,
        worker_lease_expires_at: datetime,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None or RunStatus(row.status) is not RunStatus.RUNNING:
                return False
            if not self.renew_worker_lease(
                row,
                worker_id=worker_id,
                worker_lease_token=worker_lease_token,
                worker_lease_expires_at=worker_lease_expires_at,
            ):
                return False
            await session.flush()
            return True

    async def release_worker_execution(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        complete_if_running: bool = False,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if row.worker_id != worker_id or row.worker_lease_token != worker_lease_token:
                raise RunConflict("run worker execution ownership changed")
            if complete_if_running and RunStatus(row.status) is RunStatus.RUNNING:
                row.status = RunStatus.COMPLETED.value
            self.clear_worker_lease(row)
            row.version += 1
            await session.flush()
            return self._record(row)

    async def release_expired_worker_execution(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        now: datetime,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if row.worker_id != worker_id or row.worker_lease_token != worker_lease_token:
                raise RunConflict("run worker execution ownership changed")
            if RunStatus(row.status) not in {
                RunStatus.PAUSED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                raise RunConflict("run worker execution is not releasable")
            if row.worker_lease_expires_at is None or row.worker_lease_expires_at > now:
                return self._record(row)
            self.clear_worker_lease(row)
            row.version += 1
            await session.flush()
            return self._record(row)

    async def mark_worker_execution_exited(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        exited_at: datetime,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if row.worker_id != worker_id or row.worker_lease_token != worker_lease_token:
                raise RunConflict("run worker execution ownership changed")
            row.worker_lease_expires_at = exited_at
            row.worker_heartbeat_at = None
            row.version += 1
            await session.flush()
            return self._record(row)

    def _running_for_recovery_select(self, *, limit: int, now: datetime) -> Select[tuple[UUID]]:
        return (
            select(RunRow.id)
            .where(
                RunRow.status == RunStatus.RUNNING.value,
                RunRow.mode.is_not(None),
                RunRow.worker_lease_expires_at.is_not(None),
                RunRow.worker_lease_expires_at <= now,
            )
            .order_by(RunRow.worker_lease_expires_at, RunRow.updated_at, RunRow.id)
            .limit(limit)
        )

    def _terminal_worker_executions_for_recovery_select(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> Select[tuple[RunRow]]:
        return (
            select(RunRow)
            .where(
                RunRow.status.in_(
                    (
                        RunStatus.PAUSED.value,
                        RunStatus.COMPLETED.value,
                        RunStatus.FAILED.value,
                        RunStatus.CANCELLED.value,
                    )
                ),
                RunRow.mode.is_not(None),
                RunRow.worker_id.is_not(None),
                RunRow.worker_lease_token.is_not(None),
                RunRow.worker_lease_expires_at.is_not(None),
                RunRow.worker_lease_expires_at <= now,
            )
            .order_by(RunRow.worker_lease_expires_at, RunRow.updated_at, RunRow.id)
            .limit(limit)
        )

    async def _recovery_blocked_after_checkpoint(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        minimum_event_sequence: int = 0,
        pre_model_original_capture: bool = False,
    ) -> bool:
        checkpoint_sequence = (
            await session.scalar(
                select(func.max(RunCheckpointRow.sequence)).where(
                    RunCheckpointRow.run_id == run_id
                )
            )
            or 0
        )
        self_repair_baseline_event = and_(
            RunEventRow.sequence <= minimum_event_sequence,
            RunEventRow.kind.in_(_SELF_REPAIR_RECOVERY_BASELINE_EVENT_KINDS),
        )
        latest_event_sequence = await session.scalar(
            select(func.max(RunEventRow.sequence)).where(
                RunEventRow.run_id == run_id,
                ~RunEventRow.kind.in_(
                    _RECOVERY_REPLAYABLE_EVENT_KINDS | {'context.loaded'}
                    if pre_model_original_capture else _RECOVERY_REPLAYABLE_EVENT_KINDS
                ),
                ~self_repair_baseline_event,
            )
        )
        return latest_event_sequence is not None and latest_event_sequence > checkpoint_sequence

    async def update_control_status(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            current = RunStatus(row.status)
            if current is status:
                return self._record(row)
            if current in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                raise RunConflict("terminal run state is immutable")
            if row.blocked_by_run_id is not None:
                raise RunConflict("run is blocked by an active conversation run")
            if status is RunStatus.PAUSED and current not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                raise RunConflict("run cannot be paused from its current state")
            if status is RunStatus.CANCELLED and current is RunStatus.CANCELLED:
                raise RunConflict("run state conflict")
            if status is RunStatus.CANCELLED and isinstance(row.routing_decision, dict):
                row.routing_decision = {**row.routing_decision, 'python_project_cancel_cleanup_version': 1}
            row.status = status.value
            row.version += 1
            await session.flush()
            return self._record(row)

    async def _require_capability_approval_checkpoint(
        self,
        session: AsyncSession,
        row: RunRow,
        routing_decision: dict[str, object],
        *,
        approval_id: str,
    ) -> None:
        if (_has_generated_preparation(routing_decision)
                and routing_decision.get('approval_checkpoint_required') is not True):
            raise RunConflict("capability approval is waiting for a durable recovery checkpoint")
        if routing_decision.get("approval_checkpoint_required") is True:
            checkpoint = await self.latest_checkpoint(
                session, tenant_id=row.tenant_id, run_id=row.id,
            )
            if (
                checkpoint is None
                or checkpoint.run_id != row.id
                or checkpoint.tenant_id != row.tenant_id
                or checkpoint.mode.value != row.mode
                or not checkpoint_waits_for_approval(checkpoint, approval_id)
            ):
                raise RunConflict("capability approval is waiting for a durable recovery checkpoint")
        # Loading guidance is read-only. Ignore it only for the durable captured
        # original, never injected prompts or runtime/tool events without proof.
        original_capture = (
            routing_decision.get('approval_checkpoint_required') is False
            and _pre_model_original_capture(routing_decision)
            and (
                cast(Mapping[str, object], routing_decision['project_input_preparation'])['schema_version'] == 1
                or _python_project_approval_binding(row, routing_decision.get('approval_scope')) is not None
            )
        )
        recovery_options = {'pre_model_original_capture': True} if original_capture else {}
        if await self._recovery_blocked_after_checkpoint(
            session,
            run_id=row.id,
            minimum_event_sequence=_self_repair_recovery_baseline_sequence(routing_decision),
            **recovery_options,
        ):
            raise RunConflict("capability approval is waiting for a durable recovery checkpoint")

    async def begin_capability_approval(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        approval_scope: str | None = None,
        actor_id: UUID | None = None,
        plugin_identity: PluginApprovalIdentity | None = None,
        toolchain_authority: ToolchainSupplyAuthority | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row: RunRow | None
            toolchain_scope = _is_toolchain_supply_scope(approval_scope)
            if toolchain_scope or toolchain_authority is not None:
                if not toolchain_scope or toolchain_authority is None:
                    raise RunConflict('toolchain authority is required')
                row = await self._locked_toolchain_supply(session, tenant_id, run_id, toolchain_authority)
            else:
                row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if (actor_id is not None and row.actor_id != actor_id) or (
                plugin_identity is not None and actor_id is None
            ):
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if (
                RunStatus(row.status) is RunStatus.WAITING_APPROVAL
                and routing_decision.get("approval_id") == approval_id
                and routing_decision.get("approval_fingerprint") == approval_fingerprint
            ):
                return self._record(row)
            if RunStatus(row.status) is not RunStatus.RUNNING:
                raise RunConflict("run is not running")
            approval_worker_token = row.worker_lease_token
            toolchain_binding = None
            if _is_toolchain_supply_scope(approval_scope):
                toolchain_binding = _toolchain_supply_approval_binding(row, approval_scope, planned=True)
                if (plugin_identity is not None or toolchain_binding is None
                        or actor_id is None or row.worker_id is None or approval_worker_token is None
                        or row.worker_lease_expires_at is None
                        or row.worker_lease_expires_at <= datetime.now(UTC)):
                    raise RunConflict('toolchain approval binding is invalid or stale')
            python_binding = (
                _python_project_approval_binding(row, approval_scope)
                if plugin_identity is None else None
            )
            # Captured input has no native command or model side effect yet. Its
            # durable original reference resumes admission, not a runtime checkpoint.
            original_capture = _pre_model_original_capture(routing_decision) and (
                cast(Mapping[str, object], routing_decision['project_input_preparation'])['schema_version'] == 1
                or python_binding is not None
            )
            checkpoint_required = _has_generated_preparation(routing_decision) or (
                approval_worker_token is not None and not original_capture)
            python_options: _PythonApprovalOptions = {}
            if python_binding is not None:
                python_options['python_binding'] = python_binding
            input_binding = (
                _python_input_supply_approval_binding(row, approval_scope)
                if plugin_identity is None else None
            )
            if input_binding is not None:
                python_options['python_input_binding'] = input_binding
            if toolchain_binding is not None:
                python_options['toolchain_input_binding'] = toolchain_binding
            if toolchain_authority is not None:
                toolchain_authority.require_current(self._record(row), tenant_id, run_id)
            row.status = RunStatus.WAITING_APPROVAL.value
            self.clear_worker_lease(row)
            row.routing_decision = {
                **routing_decision,
                "approval_kind": "capability_tool",
                "approval_id": approval_id,
                "approval_fingerprint": approval_fingerprint,
                "approval_checkpoint_required": checkpoint_required,
                **({"approval_checkpoint_worker_token": str(approval_worker_token)}
                   if checkpoint_required and approval_worker_token is not None else {}),
                "reason": "capability requires approval",
                **({"approval_scope": approval_scope} if approval_scope else {}),
            }
            row.version += 1
            await self._upsert_capability_approval(
                session,
                tenant_id,
                run_id,
                approval_id=approval_id,
                approval_fingerprint=approval_fingerprint,
                approval_scope=approval_scope,
                status="pending",
                request_version=row.version,
                public_binding=(
                    {"plugin_id": plugin_identity.plugin_id,
                     "capability_id": plugin_identity.capability_id,
                     "run_id": str(row.id), "version": row.version}
                    if plugin_identity is not None else None
                ),
                **python_options,
            )
            await session.flush()
            return self._record(row)

    async def resolve_capability_approval(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        approval_id: str,
        approval_fingerprint: str,
    ) -> RunRecord:
        if status not in {RunStatus.RUNNING, RunStatus.CANCELLED}:
            raise RunConflict("approval resolution status is invalid")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                if await self._capability_approval_has_status(
                    session,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    approval_id=approval_id,
                    approval_fingerprint=approval_fingerprint,
                    status="approved" if status is RunStatus.RUNNING else "rejected",
                ):
                    return self._record(row)
                raise RunConflict("run is not waiting for approval")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if (
                routing_decision.get("approval_id") != approval_id
                or routing_decision.get("approval_fingerprint") != approval_fingerprint
            ):
                raise RunConflict("run is waiting for a different approval")
            if (
                status is RunStatus.RUNNING
                and (_has_generated_preparation(routing_decision)
                     or routing_decision.get("approval_checkpoint_required") is True
                     or (routing_decision.get("approval_checkpoint_required") is False
                         and _pre_model_original_capture(routing_decision)))
            ):
                await self._require_capability_approval_checkpoint(
                    session, row, routing_decision, approval_id=approval_id,
                )
            row.status = status.value
            self.clear_worker_lease(row)
            row.routing_decision = {
                key: value
                for key, value in routing_decision.items()
                if key
                not in {
                    "approval_kind", "approval_id", "approval_fingerprint", "approval_scope",
                    "approval_checkpoint_required", "approval_checkpoint_worker_token",
                }
            }
            row.version += 1
            await self._upsert_capability_approval(
                session,
                tenant_id,
                run_id,
                approval_id=approval_id,
                approval_fingerprint=approval_fingerprint,
                approval_scope=_optional_string(routing_decision.get("approval_scope")),
                status="approved" if status is RunStatus.RUNNING else "rejected",
                resolution_version=row.version,
            )
            await session.flush()
            return self._record(row)

    async def record_capability_approval_review(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        reviewer: str,
        reason: str,
        status: str,
        toolchain_authority: ToolchainSupplyAuthority | None = None,
        approval_scope: str | None = None,
    ) -> RunRecord:
        if status not in {"approved", "denied"}:
            raise RunConflict("approval review status is invalid")
        async with self._session_factory() as session, session.begin():
            row: RunRow | None
            toolchain_binding = None
            if _is_toolchain_supply_scope(approval_scope) or toolchain_authority is not None:
                if not _is_toolchain_supply_scope(approval_scope) or toolchain_authority is None:
                    raise RunConflict('toolchain authority is required')
                row = await self._locked_toolchain_supply(session, tenant_id, run_id, toolchain_authority)
                toolchain_binding = _toolchain_supply_approval_binding(row, approval_scope, planned=True)
                if toolchain_binding is None:
                    raise RunConflict('toolchain approval binding is invalid or stale')
            else:
                row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            toolchain_options: _ToolchainReviewOptions = {}
            if toolchain_binding is not None:
                assert isinstance(approval_scope, str)
                toolchain_options['approval_scope'] = approval_scope
                toolchain_options['toolchain_input_binding'] = toolchain_binding
            await self._upsert_capability_approval_review(
                session,
                tenant_id,
                run_id,
                approval_id=approval_id,
                approval_fingerprint=approval_fingerprint,
                reviewer=reviewer,
                reason=reason,
                status=status,
                **toolchain_options,
            )
            if toolchain_authority is not None:
                toolchain_authority.require_current(self._record(row), tenant_id, run_id)
            await session.flush()
            return self._record(row)

    async def is_capability_approval_approved(
        self,
        tenant_id: UUID,
        run_id: UUID,
        approval_fingerprint: str,
    ) -> bool:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunApprovalRow).where(
                        RunApprovalRow.tenant_id == tenant_id,
                        RunApprovalRow.run_id == run_id,
                        RunApprovalRow.status == "approved",
                    )
                )
            ).all()
            for row in rows:
                if row.payload.get('approval_fingerprint') != approval_fingerprint:
                    continue
                if (_is_toolchain_supply_scope(row.payload.get('approval_scope'))
                        or 'toolchain_input_binding' in row.payload):
                    run = await session.scalar(self._run_select(tenant_id, run_id))
                    if run is not None and _toolchain_approval_matches(run, row):
                        return True
                else:
                    return True
            return False

    async def is_capability_approval_scope_approved(
        self,
        tenant_id: UUID,
        run_id: UUID,
        approval_scope: str,
    ) -> bool:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunApprovalRow).where(
                        RunApprovalRow.tenant_id == tenant_id,
                        RunApprovalRow.run_id == run_id,
                        RunApprovalRow.status == "approved",
                    )
                )
            ).all()
            for row in rows:
                if row.payload.get('approval_scope') != approval_scope:
                    continue
                if _is_toolchain_supply_scope(approval_scope) or 'toolchain_input_binding' in row.payload:
                    run = await session.scalar(self._run_select(tenant_id, run_id))
                    if run is not None and _toolchain_approval_matches(run, row):
                        return True
                else:
                    return True
            return False

    @staticmethod
    async def _capability_approval_id_has_status(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        approval_id: str,
        status: str,
    ) -> bool:
        resolved_id = await session.scalar(
            select(RunApprovalRow.id).where(
                RunApprovalRow.tenant_id == tenant_id,
                RunApprovalRow.run_id == run_id,
                RunApprovalRow.approval_id == approval_id,
                RunApprovalRow.status == status,
            )
        )
        return resolved_id is not None

    @staticmethod
    async def _capability_approval_has_status(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        approval_id: str,
        approval_fingerprint: str,
        status: str,
    ) -> bool:
        resolved_id = await session.scalar(
            select(RunApprovalRow.id).where(
                RunApprovalRow.tenant_id == tenant_id,
                RunApprovalRow.run_id == run_id,
                RunApprovalRow.approval_id == approval_id,
                RunApprovalRow.payload["approval_fingerprint"].astext == approval_fingerprint,
                RunApprovalRow.status == status,
            )
        )
        return resolved_id is not None

    @staticmethod
    async def _capability_approval_id_is_approved(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        approval_id: str,
    ) -> bool:
        return await RunRepository._capability_approval_id_has_status(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            approval_id=approval_id,
            status="approved",
        )

    async def approve_capability_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        approval_id: str,
        version: int,
        actor_id: UUID | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if actor_id is not None and row.actor_id != actor_id:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                if await self._capability_approval_id_is_approved(
                    session,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    approval_id=approval_id,
                ):
                    return self._record(row)
                raise RunConflict("run is not waiting for capability approval")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if routing_decision.get("approval_kind") != "capability_tool":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("approval_id") != approval_id:
                raise RunConflict("capability approval token is invalid")
            approval_fingerprint = routing_decision.get("approval_fingerprint")
            if type(approval_fingerprint) is not str or not approval_fingerprint:
                raise RunConflict("capability approval fingerprint is missing")
            approval_scope = _optional_string(routing_decision.get("approval_scope"))
            if row.version != version:
                raise RunConflict("run version is stale")
            await self._require_capability_approval_checkpoint(
                session, row, routing_decision, approval_id=approval_id,
            )
            row.routing_decision = {
                key: value
                for key, value in routing_decision.items()
                if key
                not in {
                    "approval_kind", "approval_id", "approval_fingerprint", "approval_scope",
                    "approval_checkpoint_required", "approval_checkpoint_worker_token",
                }
            }
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await self._upsert_capability_approval(
                session,
                tenant_id,
                run_id,
                approval_id=approval_id,
                approval_fingerprint=approval_fingerprint,
                approval_scope=approval_scope,
                status="approved",
                resolution_version=row.version,
            )
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:capability-approval:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def reject_capability_approval(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        approval_id: str,
        version: int,
        actor_id: UUID | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if actor_id is not None and row.actor_id != actor_id:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                if await self._capability_approval_id_has_status(
                    session,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    approval_id=approval_id,
                    status="rejected",
                ):
                    return self._record(row)
                raise RunConflict("run is not waiting for capability approval")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if routing_decision.get("approval_kind") != "capability_tool":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("approval_id") != approval_id:
                raise RunConflict("capability approval token is invalid")
            approval_fingerprint = routing_decision.get("approval_fingerprint")
            if type(approval_fingerprint) is not str or not approval_fingerprint:
                raise RunConflict("capability approval fingerprint is missing")
            approval_scope = _optional_string(routing_decision.get("approval_scope"))
            if row.version != version:
                raise RunConflict("run version is stale")
            row.routing_decision = {
                key: value
                for key, value in routing_decision.items()
                if key
                not in {
                    "approval_kind", "approval_id", "approval_fingerprint", "approval_scope",
                    "approval_checkpoint_required", "approval_checkpoint_worker_token",
                }
            }
            row.status = RunStatus.CANCELLED.value
            self.clear_worker_lease(row)
            row.version += 1
            await self._upsert_capability_approval(
                session,
                tenant_id,
                run_id,
                approval_id=approval_id,
                approval_fingerprint=approval_fingerprint,
                approval_scope=approval_scope,
                status="rejected",
                resolution_version=row.version,
            )
            await session.flush()
            return self._record(row)

    async def fail_run(
        self,
        run_id: UUID,
        *,
        reason: str,
        worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await self.get_for_update(session, run_id)
            if (
                (worker_id is not None or worker_lease_token is not None)
                and (row.worker_id != worker_id or row.worker_lease_token != worker_lease_token)
            ):
                raise RunConflict("run worker execution ownership changed")
            current = RunStatus(row.status)
            if current in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.PAUSED,
                RunStatus.WAITING_APPROVAL,
            }:
                return self._record(row)
            sequence = (
                await session.scalar(
                    select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == run_id)
                )
                or 0
            ) + 1
            await self.persist_event(
                session,
                tenant_id=row.tenant_id,
                run_id=run_id,
                event=RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=sequence,
                    run_id=run_id,
                    reason=reason,
                ),
            )
            row.status = RunStatus.FAILED.value
            row.version += 1
            await session.flush()
            return self._record(row)

    async def pending_outbox(self, limit: int = 100) -> tuple[PendingOutbox, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunOutboxRow)
                    .where(RunOutboxRow.delivered.is_(False))
                    .order_by(RunOutboxRow.created_at, RunOutboxRow.id)
                    .limit(limit)
                )
            ).all()
            return tuple(
                PendingOutbox(
                    id=_database_uuid(row.id),
                    run_id=_database_uuid(row.run_id),
                    idempotency_key=row.idempotency_key,
                )
                for row in rows
            )

    async def deliver_outbox(
        self,
        outbox_id: UUID,
        deliver: Callable[[UUID, str], Awaitable[None]],
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(RunOutboxRow)
                .where(RunOutboxRow.id == outbox_id)
                .with_for_update(skip_locked=True)
            )
            if row is None or row.delivered:
                return False
            await deliver(_database_uuid(row.run_id), row.idempotency_key)
            row.delivered = True
            row.delivered_at = func.now()
            await session.flush()
            return True

    async def events(
        self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None,
    ) -> tuple[dict[str, object], ...]:
        async with self._session_factory() as session:
            run = await session.scalar(self._run_select(tenant_id, run_id))
            if run is None or (actor_id is not None and run.actor_id != actor_id):
                raise RunNotFound("run was not found")
            rows = (
                await session.scalars(
                    select(RunEventRow)
                    .where(RunEventRow.tenant_id == tenant_id, RunEventRow.run_id == run_id)
                    .order_by(RunEventRow.sequence)
                )
            ).all()
            events: list[dict[str, object]] = []
            for row in rows:
                payload = _public_event_payload(dict(row.payload))
                payload["created_at"] = row.created_at
                events.append(payload)
            return tuple(events)

    async def capability_approval_events(
        self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Independent durable approval sequence; never a worker event/SSE cursor."""
        async with self._session_factory() as session:
            run = await session.scalar(self._run_select(tenant_id, run_id))
            if run is None or (actor_id is not None and run.actor_id != actor_id):
                raise RunNotFound("run was not found")
            approvals = (await session.scalars(
                select(RunApprovalRow)
                .where(RunApprovalRow.tenant_id == tenant_id, RunApprovalRow.run_id == run_id)
                .order_by(RunApprovalRow.created_at, RunApprovalRow.approval_id)
            )).all()
            return _public_plugin_approval_events(approvals, run)

    async def capability_approval_ready(
        self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID,
    ) -> bool:
        """Read the same recovery gate enforced by approval without changing its receipt."""
        async with self._session_factory() as session:
            run = await session.scalar(self._run_select(tenant_id, run_id))
            if run is None or run.actor_id != actor_id:
                raise RunNotFound("run was not found")
            routing = dict(run.routing_decision or {})
            approval_id = routing.get("approval_id")
            fingerprint = routing.get("approval_fingerprint")
            if (
                run.status != RunStatus.WAITING_APPROVAL.value
                or routing.get("approval_kind") != "capability_tool"
                or not isinstance(approval_id, str) or not approval_id
                or not isinstance(fingerprint, str) or not fingerprint
            ):
                return False
            try:
                await self._require_capability_approval_checkpoint(
                    session, run, routing, approval_id=approval_id,
                )
            except RunConflict:
                return False
            return True

    async def raw_events(self, tenant_id: UUID, run_id: UUID) -> tuple[RunEvent, ...]:
        """Internal reconstruction only; public consumers must use events()."""
        async with self._session_factory() as session:
            await self._assert_run(session, tenant_id, run_id)
            rows = (
                await session.scalars(
                    select(RunEventRow)
                    .where(RunEventRow.tenant_id == tenant_id, RunEventRow.run_id == run_id)
                    .order_by(RunEventRow.sequence)
                )
            ).all()
            events: list[RunEvent] = []
            for row in rows:
                if row.tenant_id != tenant_id or row.run_id != run_id:
                    raise RunConflict("stored run event scope mismatch")
                try:
                    event = RunEvent.from_payload(dict(row.payload))
                except (TypeError, ValueError):
                    raise RunConflict("stored run event is invalid") from None
                if event.run_id != run_id or (
                    event.checkpoint is not None and event.checkpoint.tenant_id != tenant_id
                ):
                    raise RunConflict("stored run event scope mismatch")
                events.append(event)
            return tuple(events)

    async def completed_step_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[str, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunStepRow.step_id)
                    .where(
                        RunStepRow.tenant_id == tenant_id,
                        RunStepRow.run_id == run_id,
                        RunStepRow.status == "completed",
                    )
                    .order_by(RunStepRow.created_at, RunStepRow.step_id)
                )
            ).all()
            return tuple(rows)

    async def artifact_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[UUID, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunArtifactRow.id)
                    .where(RunArtifactRow.tenant_id == tenant_id, RunArtifactRow.run_id == run_id)
                    .order_by(RunArtifactRow.created_at, RunArtifactRow.id)
                )
            ).all()
            return tuple(rows)

    async def artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunArtifactRow)
                    .where(RunArtifactRow.tenant_id == tenant_id, RunArtifactRow.run_id == run_id)
                    .order_by(RunArtifactRow.created_at, RunArtifactRow.id)
                )
            ).all()
            return tuple(_public_artifact_payload(dict(row.payload)) for row in rows)

    async def raw_artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunArtifactRow)
                    .where(RunArtifactRow.tenant_id == tenant_id, RunArtifactRow.run_id == run_id)
                    .order_by(RunArtifactRow.created_at, RunArtifactRow.id)
                )
            ).all()
            return tuple(dict(row.payload) for row in rows)

    async def conversation_context(
        self,
        tenant_id: UUID,
        conversation_id: str,
        *,
        before_run_id: UUID,
        limit: int | None = 32,
        query: str | None = None,
        candidate_limit: int | None = None,
    ) -> tuple[ConversationContextItem, ...]:
        effective_limit = candidate_limit if candidate_limit is not None else limit
        if effective_limit is not None and (type(effective_limit) is not int or effective_limit < 1):
            raise ValueError("conversation context candidate limit must be positive")
        async with self._session_factory() as session:
            current = await session.scalar(
                select(RunRow.created_at).where(
                    RunRow.tenant_id == tenant_id, RunRow.id == before_run_id
                )
            )
            if current is None:
                raise RunNotFound("run was not found")
            base_filter = (
                select(RunRow)
                .where(RunRow.tenant_id == tenant_id)
                .where(RunRow.id != before_run_id)
                .where(RunRow.routing_decision["conversation_id"].astext == conversation_id)
                .where(RunRow.created_at <= current)
            )
            if effective_limit is None:
                ordered_rows = tuple(
                    (
                        await session.scalars(
                            base_filter.order_by(RunRow.created_at.asc(), RunRow.id.asc())
                        )
                    ).all()
                )
            else:
                origin = await session.scalar(
                    base_filter.order_by(RunRow.created_at.asc(), RunRow.id.asc()).limit(1)
                )
                recent_rows = (
                    await session.scalars(
                        base_filter.order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(
                            effective_limit
                        )
                    )
                ).all()
                relevant_rows: list[RunRow] = []
                query_terms = _conversation_context_query_terms(query)
                if query_terms:
                    relevant_filter = or_(
                        *(
                            RunRow.request.ilike(
                                f"%{_escape_ilike(term)}%",
                                escape="\\",
                            )
                            for term in query_terms
                        )
                    )
                    relevant_rows = list(
                        (
                            await session.scalars(
                                base_filter.where(relevant_filter)
                                .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                                .limit(effective_limit)
                            )
                        ).all()
                    )
                selected: dict[UUID, RunRow] = {}
                if origin is not None:
                    selected[origin.id] = origin
                relevance_slots = max(0, (effective_limit - len(selected)) // 2)
                for row in relevant_rows:
                    if relevance_slots <= 0 or len(selected) >= effective_limit:
                        break
                    if row.id not in selected:
                        selected[row.id] = row
                        relevance_slots -= 1
                for row in recent_rows:
                    if len(selected) >= effective_limit:
                        break
                    selected.setdefault(row.id, row)
                ordered_rows = tuple(
                    sorted(selected.values(), key=lambda row: (row.created_at, row.id))
                )
            if not ordered_rows:
                return ()
            run_ids = [row.id for row in ordered_rows]
            artifact_rows = (
                await session.scalars(
                    select(RunArtifactRow)
                    .where(RunArtifactRow.tenant_id == tenant_id)
                    .where(RunArtifactRow.run_id.in_(run_ids))
                    .order_by(RunArtifactRow.created_at, RunArtifactRow.id)
                )
            ).all()
            artifacts_by_run: dict[UUID, list[dict[str, object]]] = {
                run_id: [] for run_id in run_ids
            }
            for artifact in artifact_rows:
                artifacts_by_run.setdefault(artifact.run_id, []).append(
                    _public_artifact_payload(dict(artifact.payload))
                )
            return tuple(
                ConversationContextItem(
                    run_id=row.id,
                    request=row.request,
                    artifacts=tuple(artifacts_by_run.get(row.id, ())),
                )
                for row in ordered_rows
            )

    async def usage_cost(self, tenant_id: UUID, run_id: UUID) -> Decimal:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunUsageRow.cost_usd).where(
                        RunUsageRow.tenant_id == tenant_id,
                        RunUsageRow.run_id == run_id,
                    )
                )
            ).all()
            return sum((Decimal(value) for value in rows), Decimal("0.00"))

    async def latest_checkpoint(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
    ) -> RuntimeCheckpoint | None:
        row = await session.scalar(
            select(RunCheckpointRow)
            .where(RunCheckpointRow.tenant_id == tenant_id, RunCheckpointRow.run_id == run_id)
            .order_by(RunCheckpointRow.sequence.desc())
            .limit(1)
        )
        if row is None:
            return None
        return RuntimeCheckpoint.from_payload(dict(row.payload))

    async def next_event_sequence(self, session: AsyncSession, run_id: UUID) -> int:
        return (
            await session.scalar(
                select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == run_id)
            )
            or 0
        ) + 1

    async def persist_event(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        event = _event_with_failure_diagnostic(event)
        if event.kind in {EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED, EventKind.TOOL_FAILED}:
            event = RunEvent.model_validate(event)
        payload = event.to_payload()
        inserted_event_id = await session.scalar(
            insert(RunEventRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=event.sequence,
                kind=payload["kind"],
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=[RunEventRow.run_id, RunEventRow.sequence])
            .returning(RunEventRow.id)
        )
        if inserted_event_id is None:
            return
        if event.step_id is not None and event.kind is EventKind.STEP_COMPLETED:
            await self._persist_step(session, tenant_id, run_id, event)
        if event.artifact is not None:
            await self._persist_artifact(session, tenant_id, run_id, event.artifact)
        if event.checkpoint is not None:
            await self._persist_checkpoint(session, tenant_id, run_id, event)
        if event.kind in {EventKind.APPROVAL_REQUESTED, EventKind.APPROVAL_RESOLVED}:
            await self._persist_approval(session, tenant_id, run_id, event)
        if event.kind is EventKind.COST_RECORDED:
            await self._persist_usage(session, tenant_id, run_id, event)

    async def record_self_repair_decision(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
        proposal: dict[str, object] | None,
        decision_token: str | None,
    ) -> None:
        row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
        if row is None:
            raise RunNotFound("run was not found")
        await self.persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)
        if proposal is None:
            return
        if RunStatus(row.status) is not RunStatus.FAILED:
            return
        if decision_token is None:
            raise ValueError("self repair decision token is required")
        routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
        row.routing_decision = {
            **routing_decision,
            "approval_kind": "self_repair",
            "decision_token": decision_token,
            "repair_proposal": proposal,
        }
        if proposal.get("pauses_run") is True:
            row.status = RunStatus.WAITING_APPROVAL.value
        row.version += 1

    async def run_transaction(self) -> AsyncSession:
        return self._session_factory()

    @staticmethod
    def record_worker_lease(
        row: RunRow,
        *,
        worker_id: str | None,
        worker_lease_token: UUID | None,
        worker_lease_expires_at: datetime | None,
    ) -> None:
        if worker_id is None or worker_lease_token is None or worker_lease_expires_at is None:
            return
        row.worker_id = worker_id
        row.worker_lease_token = worker_lease_token
        row.worker_lease_expires_at = worker_lease_expires_at
        row.worker_heartbeat_at = datetime.now(UTC)

    @classmethod
    def renew_worker_lease(
        cls,
        row: RunRow,
        *,
        worker_id: str,
        worker_lease_token: UUID,
        worker_lease_expires_at: datetime,
    ) -> bool:
        if row.worker_id != worker_id or row.worker_lease_token != worker_lease_token:
            return False
        cls.record_worker_lease(
            row,
            worker_id=worker_id,
            worker_lease_token=worker_lease_token,
            worker_lease_expires_at=worker_lease_expires_at,
        )
        return True

    @staticmethod
    def clear_worker_lease(row: RunRow) -> None:
        row.worker_id = None
        row.worker_lease_token = None
        row.worker_lease_expires_at = None
        row.worker_heartbeat_at = None

    @staticmethod
    def has_worker_execution(row: RunRow) -> bool:
        return any(
            value is not None
            for value in (
                row.worker_id,
                row.worker_lease_token,
                row.worker_lease_expires_at,
                row.worker_heartbeat_at,
            )
        )

    @staticmethod
    def _record(row: RunRow) -> RunRecord:
        return RunRecord(
            id=_database_uuid(row.id),
            tenant_id=_database_uuid(row.tenant_id),
            actor_id=_optional_database_uuid(row.actor_id),
            actor_role=None if row.actor_role is None else Role(row.actor_role),
            request=row.request,
            mode=None if row.mode is None else TaskMode(row.mode),
            status=RunStatus(row.status),
            version=row.version,
            created_at=row.created_at,
            routing_decision=None if row.routing_decision is None else dict(row.routing_decision),
            blocked_by_run_id=_optional_database_uuid(getattr(row, 'blocked_by_run_id', None)),
            worker_id=getattr(row, "worker_id", None),
            worker_lease_token=_optional_database_uuid(getattr(row, 'worker_lease_token', None)),
            worker_lease_expires_at=getattr(row, "worker_lease_expires_at", None),
            worker_heartbeat_at=getattr(row, "worker_heartbeat_at", None),
        )

    @staticmethod
    def _run_select(tenant_id: UUID, run_id: UUID) -> Select[tuple[RunRow]]:
        return select(RunRow).where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)

    @staticmethod
    async def _assert_run(session: AsyncSession, tenant_id: UUID, run_id: UUID) -> None:
        exists = await session.scalar(
            select(RunRow.id).where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)
        )
        if exists is None:
            raise RunNotFound("run was not found")

    @staticmethod
    async def _persist_step(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.step_id is not None
        await session.execute(
            insert(RunStepRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                step_id=event.step_id,
                actor=event.actor or "runtime",
                status="completed",
                payload=event.to_payload(),
            )
            .on_conflict_do_nothing(index_elements=[RunStepRow.run_id, RunStepRow.step_id])
        )

    @staticmethod
    async def _persist_artifact(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        artifact: Artifact,
    ) -> None:
        await session.execute(
            insert(RunArtifactRow)
            .values(
                id=artifact.id,
                tenant_id=tenant_id,
                run_id=run_id,
                type=artifact.type,
                producer=artifact.producer,
                content_sha256=artifact.content_sha256,
                payload=artifact.to_payload(),
            )
            .on_conflict_do_nothing()
        )

    @staticmethod
    async def _persist_checkpoint(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.checkpoint is not None
        checkpoint = event.checkpoint
        await session.execute(
            insert(RunCheckpointRow)
            .values(
                id=checkpoint.id,
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=event.sequence,
                runtime_type=checkpoint.runtime_type,
                runtime_version=checkpoint.runtime_version,
                mode=checkpoint.mode.value,
                state=checkpoint.to_payload()["state"],
                state_sha256=checkpoint.state_sha256,
                payload=checkpoint.to_payload(),
            )
            .on_conflict_do_nothing(
                index_elements=[RunCheckpointRow.run_id, RunCheckpointRow.sequence]
            )
        )

    @staticmethod
    async def _upsert_capability_approval(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        approval_scope: str | None = None,
        status: str,
        public_binding: dict[str, object] | None = None,
        python_binding: dict[str, object] | None = None,
        python_input_binding: dict[str, object] | None = None,
        toolchain_input_binding: dict[str, object] | None = None,
        request_version: int | None = None,
        resolution_version: int | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "approval_id": approval_id,
            "approval_fingerprint": approval_fingerprint,
            "approval_kind": "capability_tool",
            "status": status,
            **({"approval_scope": approval_scope} if approval_scope else {}),
            **({"public_binding": public_binding} if public_binding is not None else {}),
            **({'python_binding': python_binding} if python_binding is not None else {}),
            **({'python_input_binding': python_input_binding} if python_input_binding is not None else {}),
            **({'toolchain_input_binding': toolchain_input_binding} if toolchain_input_binding is not None else {}),
        }
        if request_version is not None:
            payload["public_request"] = {
                "kind": "approval.requested", "sequence": request_version,
                "run_id": str(run_id), "approval_id": approval_id,
                "actor": "capability_gateway", "action": "capability_tool",
                "reason": "capability requires approval",
                "payload": public_binding or {"run_id": str(run_id), "version": request_version},
            }
        if resolution_version is not None:
            payload["public_resolution"] = {
                "kind": "approval.resolved", "sequence": resolution_version,
                "run_id": str(run_id), "approval_id": approval_id,
                "actor": "capability_gateway", "decision": status, "payload": {},
            }
        if status == "pending":
            statement = insert(RunApprovalRow).values(
                id=uuid4(), tenant_id=tenant_id, run_id=run_id, approval_id=approval_id,
                action="capability_tool", status=status, payload=payload,
            ).on_conflict_do_nothing(
                index_elements=[RunApprovalRow.run_id, RunApprovalRow.approval_id],
            )
            result = await session.execute(statement.returning(RunApprovalRow.id))
        else:
            resolution = {key: value for key, value in payload.items()
                          if key in {"status", "public_resolution"}}
            result = await session.execute(
                update(RunApprovalRow).where(
                    RunApprovalRow.tenant_id == tenant_id,
                    RunApprovalRow.run_id == run_id,
                    RunApprovalRow.approval_id == approval_id,
                    RunApprovalRow.status == "pending",
                    RunApprovalRow.payload["approval_fingerprint"].astext == approval_fingerprint,
                ).values(action="capability_tool", status=status,
                         payload=RunApprovalRow.payload.op("||")(resolution))
                .returning(RunApprovalRow.id)
            )
        if result.scalar_one_or_none() is None:
            raise RunConflict("capability approval identity collision or stale resolution")

    @staticmethod
    async def _upsert_capability_approval_review(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
        reviewer: str,
        reason: str,
        status: str,
        approval_scope: str | None = None,
        toolchain_input_binding: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "approval_id": approval_id,
            "approval_fingerprint": approval_fingerprint,
            "approval_kind": "capability_tool",
            "reviewer": reviewer,
            "reason": reason,
            "status": status,
            **({'approval_scope': approval_scope, 'toolchain_input_binding': toolchain_input_binding}
               if toolchain_input_binding is not None else {}),
        }
        result = await session.execute(
            insert(RunApprovalRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                approval_id=approval_id,
                action="capability_tool.auto_review",
                status=status,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=[RunApprovalRow.run_id, RunApprovalRow.approval_id],
                set_={
                    "action": "capability_tool.auto_review",
                    "status": status,
                    "payload": payload,
                },
                where=~RunApprovalRow.payload.has_key("public_request"),
            )
            .returning(RunApprovalRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise RunConflict("approval review cannot replace a persisted request")

    @staticmethod
    async def _persist_approval(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.approval_id is not None
        payload = event.to_payload()
        status = "pending" if event.kind is EventKind.APPROVAL_REQUESTED else str(event.decision)
        action = event.action if event.action is not None else "resolved"
        await session.execute(
            insert(RunApprovalRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                approval_id=event.approval_id,
                action=action,
                status=status,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=[RunApprovalRow.run_id, RunApprovalRow.approval_id],
                set_={"action": action, "status": status, "payload": payload},
                where=~RunApprovalRow.payload.has_key("public_request"),
            )
        )

    @staticmethod
    async def _persist_usage(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.provider_id is not None
        assert event.cost_usd is not None
        await session.execute(
            insert(RunUsageRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=event.sequence,
                provider_id=event.provider_id,
                cost_usd=str(event.cost_usd),
                payload=event.to_payload(),
            )
            .on_conflict_do_nothing(index_elements=[RunUsageRow.run_id, RunUsageRow.sequence])
            )


def _encode_question_search_cursor(created_at: datetime, run_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "run_id": str(run_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _conversation_context_query_terms(query: str | None) -> tuple[str, ...]:
    if query is None:
        return ()
    normalized = re.sub(r"\s+", " ", query.casefold()).strip()
    if not normalized:
        return ()
    candidates: list[str] = []
    candidates.extend(re.findall(r"[a-z0-9][a-z0-9_.:-]{1,63}", normalized))
    for chunk in re.findall(r"[\u3400-\u9fff]{2,}", normalized):
        if len(chunk) <= 6:
            candidates.append(chunk)
            continue
        for width in (6, 5, 4, 3, 2):
            candidates.extend(
                chunk[index : index + width] for index in range(len(chunk) - width + 1)
            )
    ignored = {"继续", "当前", "之前", "这个", "那个", "方案", "项目", "任务"}
    unique = {candidate for candidate in candidates if candidate not in ignored}
    return tuple(sorted(unique, key=lambda value: (-len(value), value))[:24])


def _escape_ilike(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _question_search_excerpt(question: str, query: str, *, max_chars: int = 600) -> str:
    if len(question) <= max_chars:
        return question
    match_index = max(question.lower().find(query.lower()), 0)
    start = max(0, match_index - max_chars // 3)
    end = min(len(question), start + max_chars)
    start = max(0, end - max_chars)
    excerpt = question[start:end]
    return f"{'...' if start else ''}{excerpt}{'...' if end < len(question) else ''}"


def _decode_question_search_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        payload = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        created_at = datetime.fromisoformat(payload["created_at"])
        run_id = UUID(payload["run_id"])
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError("invalid question search cursor") from error
    if created_at.tzinfo is None:
        raise ValueError("invalid question search cursor")
    return created_at, run_id


def _is_recovery_replayable_event_kind(kind: str) -> bool:
    return kind in _RECOVERY_REPLAYABLE_EVENT_KINDS


def _is_self_repair_recovery_baseline_event_kind(kind: str) -> bool:
    return kind in _SELF_REPAIR_RECOVERY_BASELINE_EVENT_KINDS


def _self_repair_recovery_baseline_sequence(
    routing_decision: dict[str, object] | None,
) -> int:
    if routing_decision is None:
        return 0
    if (
        routing_decision.get("source") != "self_repair"
        or routing_decision.get("self_repair_accepted") is not True
    ):
        return 0
    value = routing_decision.get("self_repair_recovery_baseline_sequence")
    return value if type(value) is int and value >= 0 else 0


def _public_plugin_approval_events(
    approvals: Sequence[RunApprovalRow], run: RunRow,
) -> tuple[dict[str, object], ...]:
    # Run versions serialize this independent stream under the existing run row lock.
    routing = run.routing_decision or {}
    public: list[dict[str, object]] = []
    for approval in approvals:
        if approval.tenant_id != run.tenant_id or approval.run_id != run.id:
            raise RunConflict("capability approval evidence scope is invalid")
        requested = approval.payload.get("public_request")
        if requested is None:
            if approval.status == "pending":
                raise RunConflict("pending capability approval evidence is unavailable")
            continue
        if (
            approval.action != "capability_tool" or not isinstance(requested, dict)
            or requested.get("kind") != "approval.requested"
            or requested.get("run_id") != str(run.id)
            or requested.get("approval_id") != approval.approval_id
            or type(requested.get("sequence")) is not int or requested["sequence"] <= 0
            or approval.status not in {"pending", "approved", "rejected"}
        ):
            raise RunConflict("capability approval evidence is invalid")
        binding = requested.get("payload")
        if not isinstance(binding, dict) or binding.get("run_id") != str(run.id) or (
            type(binding.get("version")) is not int
            or binding["version"] != requested["sequence"]
        ):
            raise RunConflict("capability approval evidence is invalid")
        if "public_binding" in approval.payload:
            if set(binding) != {"plugin_id", "capability_id", "run_id", "version"} or (
                binding != approval.payload["public_binding"]
            ):
                raise RunConflict("capability approval identity is invalid")
            try:
                PluginApprovalIdentity(binding["plugin_id"], binding["capability_id"])
            except (TypeError, ValueError):
                raise RunConflict("capability approval identity is invalid") from None
        elif set(binding) != {"run_id", "version"}:
            raise RunConflict("capability approval identity is unproven")
        if approval.status == "pending" and (
            run.status != RunStatus.WAITING_APPROVAL.value
            or routing.get("approval_kind") != "capability_tool"
            or routing.get("approval_id") != approval.approval_id
            or not isinstance(approval.payload.get("approval_fingerprint"), str)
            or routing.get("approval_fingerprint") != approval.payload.get("approval_fingerprint")
            or routing.get("approval_scope") != approval.payload.get("approval_scope")
            or binding["version"] != run.version
            or "public_resolution" in approval.payload
        ):
            raise RunConflict("pending capability approval evidence is stale")
        public_binding = dict(binding)
        python_binding = None
        if 'public_binding' not in approval.payload and 'toolchain_input_binding' in approval.payload:
            if _toolchain_approval_matches(run, approval):
                public_binding.update({
                    'capability': 'command', 'operation': 'run',
                    'resource': 'workspace/current/project-inputs/toolchain/'
                    + cast(str, cast(dict[str, object], approval.payload['toolchain_input_binding'])['plan_sha256']),
                    'approval_scope': approval.payload['approval_scope'],
                })
        elif 'public_binding' not in approval.payload and 'python_input_binding' in approval.payload:
            expected_supply = _python_input_supply_approval_binding(run, approval.payload.get('approval_scope'))
            stored_supply = approval.payload['python_input_binding']
            if expected_supply is not None and type(stored_supply) is dict and stored_supply == expected_supply:
                public_binding.update({
                    'capability': 'command', 'operation': 'run',
                    'resource': 'workspace/current/project-inputs/python/' + cast(str, expected_supply['plan_sha256']),
                    'approval_scope': approval.payload['approval_scope'],
                })
        elif 'public_binding' not in approval.payload and 'python_binding' in approval.payload:
            expected_python = _python_project_approval_binding(run, approval.payload.get('approval_scope'))
            stored_python = approval.payload['python_binding']
            if (expected_python is not None and type(stored_python) is dict
                    and type(stored_python.get('schema_version')) is int
                    and stored_python == expected_python):
                python_binding = expected_python
                public_binding.update({
                    'capability': 'command', 'operation': 'run',
                    'resource': 'workspace/current/project-verification/python/'
                    + cast(str, python_binding['recipe_sha256']),
                    'approval_scope': approval.payload['approval_scope'],
                })
        elif "public_binding" not in approval.payload and isinstance(run.actor_id, UUID):
            for capability, operation, resource in (
                ("file", "write", "workspace/current"),
                ("command", "run", "workspace/current/project-verification"),
                ("file", "create", "workspace/bundle"),
            ):
                scope = (
                    f"capability-scope:v1:tenant={run.tenant_id}:user={run.actor_id}:"
                    f"run={run.id}:capability={capability}:operation={operation}:resource={resource}"
                )
                if approval.payload.get("approval_scope") == scope:
                    public_binding.update({
                        "capability": capability, "operation": operation,
                        "resource": resource, "approval_scope": scope,
                    })
                    break
        # Construct a whitelist view; even stored envelope extensions cannot leak arguments/source.
        public.append({
            "kind": "approval.requested", "sequence": requested["sequence"], "run_id": str(run.id),
            "approval_id": approval.approval_id, "actor": "capability_gateway",
            "action": "capability_tool", "reason": "capability requires approval",
            "payload": public_binding, "created_at": approval.created_at,
        })
        if approval.status != "pending":
            resolved = approval.payload.get("public_resolution")
            if (
                not isinstance(resolved, dict) or resolved.get("kind") != "approval.resolved"
                or resolved.get("run_id") != str(run.id)
                or resolved.get("approval_id") != approval.approval_id
                or resolved.get("decision") != approval.status
                or type(resolved.get("sequence")) is not int
                or resolved["sequence"] <= requested["sequence"]
            ):
                raise RunConflict("capability approval resolution evidence is invalid")
            public.append({
                "kind": "approval.resolved", "sequence": resolved["sequence"], "run_id": str(run.id),
                "approval_id": approval.approval_id, "actor": "capability_gateway",
                "decision": approval.status, "payload": {}, "created_at": approval.created_at,
            })
    public.sort(key=lambda event: cast(int, event["sequence"]))
    if len({event["sequence"] for event in public}) != len(public):
        raise RunConflict("capability approval evidence sequence is duplicated")
    return tuple(public)


def _public_event_payload(payload: dict[str, object]) -> dict[str, object]:
    kind = payload.get("kind")
    if type(kind) is str and kind in CONTEXT_EVENT_KINDS:
        return context_event_projection(payload)
    public = {
        key: _public_artifact_payload(value)
        if key == "artifact" and isinstance(value, dict)
        else _sanitize_public_json(value, project_artifacts=True)
        for key, value in payload.items()
        if _is_public_key(key) and key not in {"checkpoint", "checkpoint_summary"}
    }
    if "checkpoint" in payload or kind == EventKind.CHECKPOINT_SAVED.value:
        public["checkpoint"] = None
    if kind == EventKind.CHECKPOINT_SAVED.value:
        checkpoint = payload.get("checkpoint")
        public["checkpoint_summary"] = _public_checkpoint_summary(
            payload.get("checkpoint_summary") if checkpoint is None else checkpoint
        )
    return public


def _public_checkpoint_summary(value: object) -> dict[str, str]:
    # Never manufacture a redacted RuntimeCheckpoint with a misleading resume hash.
    if not isinstance(value, dict):
        return {}
    patterns = {
        "id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
        "runtime_type": r"[a-z0-9][a-z0-9_.-]{0,127}",
        "runtime_version": r"[0-9]+(?:\.[0-9]+){0,2}",
        "mode": r"direct|discuss|dispatch|hybrid",
        "state_sha256": r"[0-9a-f]{64}",
    }
    return {
        key: item
        for key, pattern in patterns.items()
        if type(item := value.get(key)) is str
        and len(item) <= 128
        and re.fullmatch(pattern, item) is not None
    }


def _event_with_failure_diagnostic(event: RunEvent) -> RunEvent:
    if event.kind not in {EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED, EventKind.TOOL_FAILED}:
        return event
    if not event.reason:
        return event
    payload = dict(event.payload)
    if "error_code" in payload:
        return event
    payload = {
        **runtime_failure_diagnostic_from_reason(event.reason),
        **payload,
    }
    return event.model_copy(update={"payload": payload})


def _public_artifact_payload(payload: dict[str, object]) -> dict[str, object]:
    try:
        original = Artifact.from_payload(payload)
    except (TypeError, ValueError):
        original = None
    public = {
        key: _sanitize_public_json(value)
        for key, value in payload.items()
        if _is_public_key(key) and key not in {"public_content_sha256", "content_redacted"}
    }
    if original is None or payload.get("content_sha256") != original.content_sha256:
        return public
    try:
        # The original digest belongs to private storage; validate a separate public envelope.
        projected = Artifact.from_payload({**public, "content_sha256": ""})
    except (TypeError, ValueError):
        return public
    public["public_content_sha256"] = projected.content_sha256
    public["content_redacted"] = projected.content_sha256 != original.content_sha256
    return public


def _sanitize_public_json(value: object, *, project_artifacts: bool = False) -> object:
    if isinstance(value, dict):
        return {
            str(key): _public_artifact_payload(item)
            if project_artifacts and key == "artifact" and isinstance(item, dict)
            else _sanitize_public_json(item, project_artifacts=project_artifacts)
            for key, item in value.items()
            if _is_public_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_public_json(item, project_artifacts=project_artifacts) for item in value]
    if (type(value) is str and _SENSITIVE_PUBLIC_TEXT.search(value)
        and not is_public_project_prerequisite_text(value)):
        return "[redacted]"
    return value


def _optional_string(value: object) -> str | None:
    return value if type(value) is str else None


def _is_public_key(key: str) -> bool:
    lowered = key.lower()
    return not any(sensitive in lowered for sensitive in _SENSITIVE_PUBLIC_KEYS)
