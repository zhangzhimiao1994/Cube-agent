"""Dedicated typed contracts for the integration repair slice; no external IO."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.engine.result import IteratorResult
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.sql import Select, operators
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Grouping,
    UnaryExpression,
)
from sqlalchemy.sql.selectable import Exists

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.config.schema import PlatformConfig
from agent_hub.db.base import Base
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunArtifactRow,
    RunAuthenticatedInputRow,
    RunCheckpointRow,
    RunEventRow,
    RunInputAuthorizationRow,
    RunOutboxRow,
    RunRow,
    RuntimeArtifactRow,
    RuntimeArtifactWriteRow,
    RunUsageRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.run_admission_repository import (
    DeploymentIdentity,
    RunAdmissionRepository,
    _ReserveArguments,
)
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.authorization import _AuthenticatedInput, _capture_authenticated_input
from agent_hub.runs.repository import RunRepository, _database_uuid
from agent_hub.runs.self_repair import (
    SelfRepairDecision,
    SelfRepairPolicy,
    classify_terminal_run,
    project_repair_scope_sha256,
)
from agent_hub.runs.service import RunService, _with_main_agent_context_window
from agent_hub.runtime.autogen import project_repair_phase as phase
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.review_assembly import ReviewExecution
from agent_hub.security.secrets import SecretService
from tests.unit.harness.review_typed_fixture import ReviewSession, StreamRuntimeStub, result_rows
from tests.unit.runs.project_auto_repair_typed_fixture import (
    UnexpectedTaskQueue,
    integer,
    json_map,
    ledger,
    mutable,
    present,
    sequence,
)
from tests.unit.runtime.test_review_assembly import AssemblyDB, AssemblyFixture, fixture


def matches(condition: object, row: Base) -> bool:
    if isinstance(condition, Grouping):
        return matches(condition.element, row)
    if isinstance(condition, BooleanClauseList):
        values = (matches(item, row) for item in condition.clauses)
        return all(values) if condition.operator is operators.and_ else any(values)
    if isinstance(condition, UnaryExpression):
        assert condition.operator is operators.inv
        return not matches(condition.element, row)
    if isinstance(condition, Exists):
        raise TypeError("correlated artifact lookup requires the repair store")
    assert isinstance(condition, BinaryExpression), condition
    key = condition.left.key
    assert isinstance(key, str)
    left: object = getattr(row, key)
    right: object = (
        condition.right.value
        if isinstance(condition.right, BindParameter)
        else condition.right.__visit_name__ == "true"
    )
    if condition.operator is operators.is_:
        return left is right
    if condition.operator is operators.in_op:
        return left in sequence(right)
    if condition.operator is operators.not_in_op:
        return left not in sequence(right)
    if condition.operator is operators.eq:
        return left == right
    if condition.operator is operators.ne:
        return left != right
    if condition.operator is operators.le:
        return integer(left) <= integer(right)
    if condition.operator is operators.lt:
        return integer(left) < integer(right)
    if condition.operator is operators.ge:
        return integer(left) >= integer(right)
    if condition.operator is operators.gt:
        return integer(left) > integer(right)
    raise AssertionError(f"unsupported predicate: {condition}")


class RepairDB(AssemblyDB):
    service_sql = False

    def select(self, statement: Select[tuple[object, ...]]) -> list[Base]:
        entity = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if type(row) is entity]
        rows = [row for row in rows if all(self.matches(c, row) for c in statement._where_criteria)]
        if statement._order_by_clauses and rows and hasattr(rows[0], "sequence"):
            rows.sort(key=lambda row: integer(getattr(row, "sequence", None)), reverse=True)
        return rows

    def matches(self, condition: object, row: Base) -> bool:
        if isinstance(condition, Grouping):
            return self.matches(condition.element, row)
        if isinstance(condition, BooleanClauseList):
            values = (self.matches(item, row) for item in condition.clauses)
            return all(values) if condition.operator is operators.and_ else any(values)
        if isinstance(condition, Exists):
            assert isinstance(row, RuntimeArtifactRow)
            return any(
                isinstance(write, RuntimeArtifactWriteRow)
                and write.status == "written"
                and (write.tenant_id, write.run_id, write.artifact_id, write.content_sha256)
                == (row.tenant_id, row.run_id, row.id, row.content_sha256)
                for write in self.rows
            )
        return matches(condition, row)

    def execute_rows(
        self, statement: Select[tuple[object, ...]]
    ) -> IteratorResult[tuple[object, ...]]:
        expr = statement.column_descriptions[0]["expr"]
        name: object = getattr(expr, "name", None)
        if name == "max":
            column = next(iter(expr.clauses))
            table = column.table.name
            values = [
                integer(getattr(row, column.key))
                for row in self.rows
                if type(row).__tablename__ == table
                and all(self.matches(c, row) for c in statement._where_criteria)
            ]
            return result_rows([(max(values, default=None),)])
        if name == "count":
            if len(statement.column_descriptions) > 1:
                artifacts = [row for row in self.rows if isinstance(row, RuntimeArtifactRow)]
                return result_rows([(len(artifacts), sum(row.byte_size for row in artifacts))])
            table = statement.get_final_froms()[0].description
            return result_rows([(sum(type(row).__tablename__ == table for row in self.rows),)])
        entity = statement.column_descriptions[0].get("entity")
        if entity is RunRow and getattr(expr, "key", None) == "created_at":
            rows = self.select(statement)
            return result_rows([(row.created_at,) for row in rows if isinstance(row, RunRow)])
        if entity is RunRow and len(statement.column_descriptions) == 3:
            selected = self.select(statement)
            if not selected:
                return result_rows([])
            row = selected[0]
            assert isinstance(row, RunRow)
            link = next(
                (
                    item
                    for item in self.rows
                    if isinstance(item, RunInputAuthorizationRow) and item.run_id == row.id
                ),
                None,
            )
            turn = next(
                (
                    item
                    for item in self.rows
                    if isinstance(item, RunAuthenticatedInputRow)
                    and link is not None
                    and item.turn_id == link.turn_id
                ),
                None,
            )
            return result_rows([(row, link, turn)])
        return super().execute_rows(statement)

    def inserted(self, statement: Insert) -> UUID | None:
        tables: dict[str, type[Base]] = {
            cls.__tablename__: cls
            for cls in (RunEventRow, RunCheckpointRow, RunArtifactRow, RunUsageRow, RunApprovalRow)
        }
        cls = tables[statement.table.name]
        params = statement.compile().params
        assert params is not None
        keys = ("run_id", "sequence") if cls in (RunEventRow, RunCheckpointRow) else ("id",)
        if any(
            type(row) is cls and all(getattr(row, key) == params[key] for key in keys)
            for row in self.rows
        ):
            return None
        row = cls(**params)
        self.add(row)
        identity: object = getattr(row, "id", None)
        assert isinstance(identity, UUID)
        return identity


@event.listens_for(ReviewSession, "do_orm_execute", insert=True, retval=True)
def repair_insert(state: ORMExecuteState) -> IteratorResult[tuple[object, ...]] | None:
    owner = state.session.info.get("review_fixture")
    if isinstance(owner, RepairDB) and isinstance(state.statement, Insert):
        assert owner.service_sql
        return result_rows([(owner.inserted(state.statement),)])
    return None


@dataclass
class RepairFixture:
    f: AssemblyFixture
    db: RepairDB
    runs: RunRepository
    original_task: TaskContext
    original: ReviewExecution
    cp: RuntimeCheckpoint
    before: dict[str, object]
    old_token: UUID | None


@dataclass
class ProjectionFixture(RepairFixture):
    context: TaskContext
    private: dict[str, str]


def checkpoint_row(cp: RuntimeCheckpoint, sequence: int) -> RunCheckpointRow:
    return RunCheckpointRow(
        id=cp.id,
        run_id=cp.run_id,
        tenant_id=cp.tenant_id,
        sequence=sequence,
        mode=cp.mode.value,
        runtime_type=cp.runtime_type,
        runtime_version=cp.runtime_version,
        state=cp.to_payload()["state"],
        state_sha256=cp.state_sha256,
        payload=cp.to_payload(),
    )


def pool_values(db: RepairDB) -> dict[str, object]:
    pool = next(row for row in db.rows if type(row) is RunAdmissionPoolRow)
    return {column.key: deepcopy(getattr(pool, column.key)) for column in pool.__table__.columns}


async def repair_fixture(
    tmp_path: Path, *, accept: bool = True, route_extras: Mapping[str, object] | None = None
) -> RepairFixture:
    f = await fixture(tmp_path, mode=TaskMode.DISCUSS)
    # Retain the real assembly configuration, replace only the SQL/session boundary.
    db = RepairDB(f.s)
    db.rows.remove(db.pool)
    db.run.created_at = f.s.runs.record.created_at
    db.run.version = 4
    route = {
        **f.s.task.routing_decision,
        "engineering_auto_repair_version": 1,
        "engineering_delivery_contract_version": 1,
        "engineering_checkpoint_ack_version": 1,
        **(route_extras or {}),
    }
    db.run.routing_decision = route
    runs = RunRepository(db)
    f.broker._runs = runs
    f.broker._sessions = db
    original_task = f.s.task.model_copy(update={"routing_decision": route})
    original = present(await f.broker.prepare(original_task, worker_id=present(db.run.worker_id)))
    admission = RunAdmissionRepository(db)
    hold: _ReserveArguments = {
        "tenant_id": original_task.tenant_id,
        "run_id": original_task.run_id,
        "authorization_turn_id": original.budget.evidence.turn_id,
        "lease": original.budget.lease,
        "config_sha256": original.budget._pool.config_sha256,
        "attempt_id": uuid4(),
        "purpose": "review",
        "request_sha256": "a" * 64,
        "deployment": DeploymentIdentity("fixed", "reviewer", "reviewer"),
        "token_hold": 18000,
        "review_cost_hold": Decimal("0.01"),
    }
    await admission.reserve(**hold)
    physical = uuid4()
    await admission.mark_issued(
        tenant_id=hold["tenant_id"],
        run_id=hold["run_id"],
        authorization_turn_id=hold["authorization_turn_id"],
        lease=hold["lease"],
        config_sha256=hold["config_sha256"],
        attempt_id=hold["attempt_id"],
        physical_transport_id=physical,
    )
    await admission.finish(
        tenant_id=hold["tenant_id"],
        run_id=hold["run_id"],
        attempt_id=hold["attempt_id"],
        lease=hold["lease"],
        deployment=hold["deployment"],
        outcome="unknown",
        usage=None,
    )
    _, template = evidence()
    cp = RuntimeCheckpoint(
        id=uuid4(),
        run_id=original_task.run_id,
        tenant_id=original_task.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state={
            **json_map(template.to_payload()["state"]),
            "remaining_timeout_seconds": 30,
            "remaining_absolute_timeout_seconds": 60,
        },
    )
    proposal = replace(
        decision(cp),
        requires_approval=False,
        automatic_execution=True,
        automatic_checkpoint_sha256=cp.state_sha256,
        automatic_scope_sha256=project_repair_scope_sha256(route),
    ).to_proposal(run_id=cp.run_id)
    db.run.routing_decision = {
        **route,
        "approval_kind": "self_repair",
        "decision_token": "token",
        "repair_proposal": proposal,
    }
    db.run.status = "failed"
    runs.clear_worker_lease(db.run)
    db.add(checkpoint_row(cp, 1))
    db.add(
        RunEventRow(
            run_id=cp.run_id, tenant_id=cp.tenant_id, sequence=2, kind="runtime.failed", payload={}
        )
    )
    before = pool_values(db)
    old_token = original_task.execution_token
    if accept:
        accepted = await runs.accept_self_repair_and_enqueue(
            tenant_id=cp.tenant_id,
            run_id=cp.run_id,
            version=4,
            decision_token="token",
            automatic_checkpoint_sha256=cp.state_sha256,
        )
        assert accepted.status is RunStatus.QUEUED
        assert [row.run_id for row in db.rows if type(row) is RunOutboxRow] == [cp.run_id]
    return RepairFixture(f, db, runs, original_task, original, cp, before, old_token)


async def claim(f: RepairFixture) -> TaskContext:
    token = uuid4()
    async with f.db() as session, session.begin():
        result = await f.runs.claim_for_execution(
            session,
            f.cp.run_id,
            allow_running_recovery=False,
            worker_id="repair-worker",
            worker_lease_token=token,
            worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
        )
    assert isinstance(result, tuple)
    row, checkpoint = result
    return f.original_task.model_copy(
        update={
            "execution_token": token,
            "checkpoint": checkpoint,
            "routing_decision": row.routing_decision,
        }
    )


async def productive_checkpoint(f: RepairFixture, context: TaskContext) -> RuntimeCheckpoint:
    state = mutable(f.cp.to_payload()["state"])
    state[phase.PHASE_KEY] = phase.prepare(
        context, f.cp, ledger(f.cp.state["model_ledger"]), ledger(f.cp.state["tool_ledger"])
    )
    artifact = Artifact(
        id=uuid4(),
        type="tool_result",
        producer="implementer",
        content=json_map(
            {
                "tool_name": "workspace.write_text",
                "arguments_sha256": "b" * 64,
                "result": {
                    "workspace_files": [{"path": "fixed.txt", "sha256": "c" * 64, "size_bytes": 7}]
                },
            }
        ),
    )
    sequence(state["tool_ledger"])
    tools = state["tool_ledger"]
    assert isinstance(tools, list)
    tools.append(
        {
            "name": "workspace.write_text",
            "actor": "implementer",
            "position": 1,
            "status": "succeeded",
            "artifact_id": str(artifact.id),
            "artifact_sha256": artifact.content_sha256,
            "arguments_hash": "b" * 64,
            "idempotency_key": hashlib.sha256(
                f"{context.run_id}:implementer:1:workspace.write_text:{'b' * 64}".encode()
            ).hexdigest(),
        }
    )
    mutable(state["artifact_registry"])[str(artifact.id)] = artifact.content_sha256
    state["engineering_tool_prefix"] = 2
    state["remaining_timeout_seconds"] = 25
    state["remaining_absolute_timeout_seconds"] = 55
    state["usage"] = {"tokens": 120, "cost_usd": "0.2"}
    payload = PostgresArtifactRepository._encode_payload(artifact.to_payload())
    f.db.add(
        RuntimeArtifactRow(
            id=artifact.id,
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            content_sha256=artifact.content_sha256,
            payload=payload,
            byte_size=len(payload.encode()),
            permanent=True,
        )
    )
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=json_map(state),
    )
    f.db.add(checkpoint_row(checkpoint, 3))
    return checkpoint


async def service_runtime_context(
    f: RepairFixture, context: TaskContext, *, window: int = 10000
) -> TaskContext:
    async def trusted_window() -> int:
        return window

    service = RunService(
        f.runs,
        runtime_registry=RuntimeRegistry((StreamRuntimeStub(),)),
        router=None,
        task_queue=UnexpectedTaskQueue(),
        main_agent_context_window_getter=trusted_window,
    )
    selected = await service._main_agent_context_window_tokens(context.routing_decision)
    return context.model_copy(
        update={
            "routing_decision": _with_main_agent_context_window(context.routing_decision, selected)
        }
    )


def submitted_input_scope(body: CreateRunRequest) -> dict[str, object]:
    """Mirror the authenticated create boundary's scope; selection is not a grant."""
    return {
        "schema_version": 1,
        "evidence_kind": "submitted_selection_not_grant",
        "operation": "create_run",
        "conversation_id": body.conversation_id,
        "selection": body.model_dump(mode="json", exclude={"message"}),
        "resolved_selection": {
            "sandbox_profile": body.sandbox_profile,
            "permission_mode": body.permission_mode,
            "requested_permissions": list(body.requested_permissions),
            "execution_backend": body.execution_backend,
        },
    }


def capture_submission_input(row: RunRow, body: CreateRunRequest) -> _AuthenticatedInput:
    """Capture the fixture's original input at its persisted-principal boundary."""
    return _capture_authenticated_input(
        AuthenticatedPrincipal(
            _database_uuid(present(row.actor_id)),
            _database_uuid(row.tenant_id),
            Role(present(row.actor_role)),
        ),
        body.message,
        submitted_input_scope(body),
    )


def fixture_platform() -> PlatformConfig:
    definition = {
        "provider": "openai",
        "model": "fixture-primary",
        "credential_ref": "fixture-primary-secret",
        "quota_scope_id": "fixture-primary",
        "context_window_tokens": 131072,
        "max_output_tokens": 8192,
        "input_per_million_usd": "1",
        "output_per_million_usd": "2",
        "capabilities": ["text", "structured_output", "tool_calling"],
    }
    return PlatformConfig.model_validate(
        {
            "models": {
                "primary": {"deployments": [definition]},
                "reviewer": {
                    "deployments": [
                        {
                            **definition,
                            "model": "fixture-reviewer",
                            "credential_ref": "fixture-reviewer-secret",
                            "quota_scope_id": "fixture-reviewer",
                        }
                    ]
                },
            },
            "agents": [
                {
                    "id": name,
                    "role": name,
                    "prompt": "Compare the bounded observations and provide the final answer.",
                    "model": "primary",
                    "skills": [],
                }
                for name in ("analyst", "critic")
            ],
        }
    )


class FixtureSecrets(SecretService):
    def __init__(self) -> None:
        pass

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        value = await self.resolve(tenant_id, reference)
        return hashlib.sha256(f"{tenant_id}:{value}".encode()).hexdigest()

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert isinstance(reference, str)
        return {
            "fixture-primary-secret": "synthetic-primary-credential",
            "fixture-reviewer-secret": "synthetic-reviewer-credential",
        }[reference]


def evidence() -> tuple[dict[str, object], RuntimeCheckpoint]:
    from agent_hub.runs.workspace import workspace_selection

    route: dict[str, object] = {
        **workspace_selection(
            project_id="project", session_id="main", sandbox_profile="workspace_write"
        ).routing_payload(),
        "engineering_auto_repair_version": 1,
        "engineering_delivery_contract_version": 1,
        "engineering_checkpoint_ack_version": 1,
        "runtime_plan_token_budget": 1000,
        "runtime_token_absolute_tokens": 1000,
        "runtime_timeout_absolute_seconds": 3600,
    }
    artifact_id = str(uuid4())
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        runtime_type="autogen",
        runtime_version="2",
        state=json_map(
            {
                "terminal": False,
                "engineering_checkpoint_ack_version": 1,
                "remaining_timeout_seconds": 100,
                "remaining_absolute_timeout_seconds": 500,
                "usage": {"tokens": 100, "cost_usd": "0.1"},
                "artifact_registry": {artifact_id: "a" * 64},
                "model_ledger": [],
                "tool_ledger": [
                    {"status": "failed", "artifact_id": artifact_id, "artifact_sha256": "a" * 64}
                ],
            }
        ),
    )
    return route, checkpoint


def decision(cp: RuntimeCheckpoint, reason: str = "project build failed") -> SelfRepairDecision:
    result = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=cp.mode,
        routing_decision={},
        policy=SelfRepairPolicy(),
        events=[
            RunEvent(
                run_id=cp.run_id,
                sequence=1,
                kind=EventKind.STEP_FAILED,
                reason=reason,
                step_id="build",
                actor="implementer",
            )
        ],
    )
    assert result is not None
    return result
