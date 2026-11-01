"""Local receipt and capture fixtures; real DTOs/DAOs, no native Root transport."""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict, Unpack
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select, Table, event
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.engine.result import IteratorResult
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction
from sqlalchemy.util import await_only

from agent_hub.auth.models import Role
from agent_hub.capabilities import python_generated_service
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectService,
    validate_generated_preparation,
    validate_generated_transition,
)
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_project_service import PythonProjectExecutor
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.project_verification_receipts import (
    RECEIPT_KEY,
    ProjectVerificationReceiptRepository,
    VerificationAuthority,
    VerificationRecord,
)
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.skills.sandbox import python_dependency_protocol as dependency
from agent_hub.skills.sandbox import python_project_profile as profile
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectInvocation,
    PythonProjectProbeInvocation,
)
from tests.unit.capabilities import test_python_generated_service as generated_helpers
from tests.unit.capabilities import test_python_project_service as original_helpers
from tests.unit.capabilities.dynamic_resolution_typed_fixture import (
    CASArgs,
    draft_case,
    mapping,
    mutable,
    observation,
    present,
    strings,
)
from tests.unit.harness.review_typed_fixture import result_rows
from tests.unit.runs.project_auto_repair_typed_fixture import json_map, sequence, string

__all__ = ["CASArgs", "json_map", "mapping", "mutable", "present", "sequence", "string"]


class PrivateVerifyArgs(TypedDict):
    tenant_id: UUID
    run_id: UUID
    actor: str


class VerifyArgs(PrivateVerifyArgs):
    action_key: str


class CompleteArgs(TypedDict):
    expected: VerificationRecord
    status: Literal["passed", "failed"]
    artifact_references: NotRequired[tuple[ArtifactReference, ...]]


class WriteArgs(TypedDict, total=False):
    write_id: UUID | None


class FailureExecutor(PythonProjectExecutor, Protocol):
    failure_index: int | None
    calls: list[PythonProjectInvocation]
    probes: list[PythonProjectProbeInvocation]
    inspections: list[PythonProjectInvocation]


class ReceiptSession(Session):
    pass


def database(session: Session) -> ReceiptDatabase:
    value: object = session.info["receipt_database"]
    assert isinstance(value, ReceiptDatabase)
    return value


@event.listens_for(ReceiptSession, "after_transaction_create")
def begin_snapshot(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        db = database(session)
        await_only(db.lock.acquire())
        session.info["working_row"] = deepcopy(db.row)


@event.listens_for(ReceiptSession, "do_orm_execute")
def select_snapshot(state: ORMExecuteState) -> IteratorResult[tuple[object, ...]]:
    row: object = state.session.info["working_row"]
    assert isinstance(row, RunRow)
    assert state.session.in_transaction()
    statement = state.statement
    assert isinstance(statement, Select)
    dialect_type: type[Dialect] = PGDialect
    compiled = statement.compile(dialect=dialect_type())
    assert "FOR UPDATE" in str(compiled)
    tables = statement.get_final_froms()
    assert all(isinstance(table, Table) for table in tables)
    assert {table.name for table in tables if isinstance(table, Table)} == {"agent_hub_runs"}
    assert len(compiled.params) == 2
    return result_rows([(row,)] if set(compiled.params.values()) == {row.id, row.tenant_id} else [])


@event.listens_for(ReceiptSession, "after_commit")
def commit_snapshot(session: Session) -> None:
    row: object = session.info["working_row"]
    assert isinstance(row, RunRow)
    database(session).row = deepcopy(row)


@event.listens_for(ReceiptSession, "after_transaction_end")
def release_snapshot(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop("working_row", None)
        database(session).lock.release()


class ReceiptDatabase:
    def __init__(self) -> None:
        self.row = RunRow(
            id=uuid4(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            actor_role="operator",
            request="Repair the current project",
            mode="direct",
            status="running",
            version=4,
            created_at=datetime.now(UTC),
            worker_id="worker-one",
            worker_lease_token=uuid4(),
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            routing_decision={
                "project_id": "project-a",
                "workspace_session_id": "session-a",
                "sandbox_profile": "workspace_write",
                "execution_backend": "systemd",
                "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
                "runtime_timeout_seconds": 300,
                "unrelated": {"keep": [1, False]},
            },
        )
        self.lock = asyncio.Lock()
        self.factory = async_sessionmaker(
            expire_on_commit=False,
            sync_session_class=ReceiptSession,
            info={"receipt_database": self},
        )

    def repository(self) -> ProjectVerificationReceiptRepository:
        return ProjectVerificationReceiptRepository(self.factory)

    def authority(self, *, actor: str = "main_agent") -> VerificationAuthority:
        row = self.row
        context = TaskContext(
            tenant_id=row.tenant_id,
            run_id=row.id,
            actor_id=row.actor_id,
            actor_role=Role(present(row.actor_role)),
            execution_token=row.worker_lease_token,
            request=row.request,
            mode=TaskMode(present(row.mode)),
            routing_decision=json_map(present(row.routing_decision)),
        )
        return VerificationAuthority.from_context(
            context, worker_id=present(row.worker_id), actor=actor
        )

    def new_lease(self) -> None:
        self.row.worker_id = "worker-two"
        self.row.worker_lease_token = uuid4()
        self.row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)


class Runs:
    def __init__(self, db: ReceiptDatabase) -> None:
        self.db = db
        self.approvals: set[str] = set()
        self.updates = 0

    @property
    def record(self) -> RunRow:
        return self.db.row

    async def get(self, tenant: UUID, run: UUID) -> RunRecord:
        assert (tenant, run) == (self.db.row.tenant_id, self.db.row.id)
        return RunRepository._record(self.db.row)

    async def is_capability_approval_scope_approved(
        self, tenant: UUID, run: UUID, scope: str
    ) -> bool:
        assert (tenant, run) == (self.db.row.tenant_id, self.db.row.id)
        return scope in self.approvals

    async def compare_and_set_generated_preparation(
        self, tenant: UUID, run: UUID, **kw: Unpack[CASArgs]
    ) -> RunRecord:
        row = self.db.row
        assert (tenant, run, kw["actor_id"], kw["lease_token"]) == (
            row.tenant_id,
            row.id,
            row.actor_id,
            row.worker_lease_token,
        )
        guard = kw.get("before_reservation")
        if guard is not None:
            await guard()
        before = present(row.routing_decision).get("generated_project_preparation")
        assert (None if before is None else validate_generated_preparation(before)) == kw[
            "expected"
        ]
        after = validate_generated_transition(before, kw["replacement"])
        row.routing_decision = {
            **present(row.routing_decision),
            "generated_project_preparation": json.loads(json.dumps(after, default=dict)),
        }
        row.version += 1
        self.updates += 1
        return RunRepository._record(row)


@dataclass
class CaptureRow:
    tenant_id: UUID
    id: UUID
    actor_id: UUID
    actor_role: Role
    request: str
    status: RunStatus
    worker_id: str | None
    worker_lease_token: UUID | None
    worker_lease_expires_at: datetime | None
    routing_decision: dict[str, object]


class CaptureRepository:
    def __init__(self, row: CaptureRow) -> None:
        self.row = row
        self.updates = 0

    async def get(self, tenant_id: UUID, run_id: UUID) -> CaptureRow:
        assert (tenant_id, run_id) == (self.row.tenant_id, self.row.id)
        self._normalize()
        return self.row

    def _normalize(self) -> None:
        route = self.row.routing_decision
        if "generated_project_preparation" in route:
            self.row.routing_decision = {
                **route,
                "generated_project_preparation": validate_generated_preparation(
                    route["generated_project_preparation"]
                ),
            }

    async def compare_and_set_generated_preparation(
        self, tenant_id: UUID, run_id: UUID, **kw: Unpack[CASArgs]
    ) -> CaptureRow:
        import hashlib

        guard = kw.get("before_reservation")
        if guard is not None:
            await guard()
        self._normalize()
        assert (tenant_id, run_id) == (self.row.tenant_id, self.row.id)
        assert kw["actor_id"] == self.row.actor_id
        assert kw["lease_token"] == self.row.worker_lease_token
        assert kw["request_sha256"] == hashlib.sha256(self.row.request.encode()).hexdigest()
        assert self.row.routing_decision.get("generated_project_preparation") == kw["expected"]
        self.row.routing_decision = {
            **self.row.routing_decision,
            "generated_project_preparation": kw["replacement"],
        }
        self.updates += 1
        return self.row


type Draft = tuple[GeneratedProjectService, CaptureRepository, str]


@pytest.fixture
def draft(tmp_path: Path) -> Draft:
    service, previous, corrected = draft_case(tmp_path)
    row = previous.row
    repository = CaptureRepository(
        CaptureRow(
            row.tenant_id,
            row.id,
            present(row.actor_id),
            present(row.actor_role),
            row.request,
            row.status,
            row.worker_id,
            row.worker_lease_token,
            row.worker_lease_expires_at,
            dict(present(row.routing_decision)),
        )
    )
    return (
        GeneratedProjectService(
            root=service.root,
            repository=repository,
            dependency_inspector=service._dependency_inspector,
        ),
        repository,
        corrected,
    )


def assign_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def profile_environment() -> dict[str, str]:
    return dict(strings(observation()["target_environment"]))


def exchanges(
    calls: list[profile.PythonProjectProfileRequest | dependency.PythonDependencyClosureRequest],
) -> tuple[profile.PythonProjectProfileRequest, dependency.PythonDependencyClosureRequest]:
    discovery, closure = calls
    assert isinstance(discovery, profile.PythonProjectProfileRequest)
    assert isinstance(closure, dependency.PythonDependencyClosureRequest)
    return discovery, closure


def set_item(value: object, key: str, item: object) -> None:
    """Attempt the original item assignment, including immutable mapping refusal."""
    from operator import setitem

    setter: object = setitem
    assert callable(setter)
    setter(value, key, item)


@dataclass
class Case:
    kind: str
    root: Path
    db: ReceiptDatabase
    runs: Runs
    executor: FailureExecutor
    artifacts: InMemoryArtifactRepository
    supply: generated_helpers.Supply | None = None

    def owner(self, *, executor: FailureExecutor | None = None) -> ProjectVerificationService:
        owner = ProjectVerificationService(
            root=self.root,
            repository=self.runs,
            executor=None,
            python_executor=self.executor if executor is None else executor,
            receipt_repository=self.db.repository(),
            artifact_repository=self.artifacts,
        )
        if self.supply is not None:
            owner._python_generated.supply_client = self.supply
        return owner

    def bind(self) -> AbstractContextManager[object]:
        row = self.db.row
        context = TaskContext(
            tenant_id=row.tenant_id,
            run_id=row.id,
            actor_id=row.actor_id,
            actor_role=Role(present(row.actor_role)),
            execution_token=row.worker_lease_token,
            request=row.request,
            mode=TaskMode(present(row.mode)),
            routing_decision=json_map(present(row.routing_decision)),
        )
        return bind_execution_authority(context, worker_id=present(row.worker_id))

    def arguments(self, *, actor: str = "tester", action: str = "python-check-one") -> VerifyArgs:
        return {
            "tenant_id": self.db.row.tenant_id,
            "run_id": self.db.row.id,
            "actor": actor,
            "action_key": action,
        }

    def index(self) -> VerificationRecord:
        return VerificationRecord.from_payload(present(self.db.row.routing_decision)[RECEIPT_KEY])

    def assert_no_receipts(self, owner: ProjectVerificationService) -> None:
        key = (self.db.row.tenant_id, self.db.row.id)
        assert all(
            key not in cache
            for cache in (
                owner._receipts,
                owner._original_receipts,
                owner._generated_receipts,
                owner._python.receipts,
                owner._python_generated.receipts,
            )
        )


@pytest.fixture(params=["python_original", "python_generated"])
async def case(request: pytest.FixtureRequest, tmp_path: Path) -> Case:
    db = ReceiptDatabase()
    if request.param == "python_original":
        warm, previous, executor, base, _ = await original_helpers.configured(tmp_path)
        row = previous.record
        root = warm.root
        supply = None
    else:
        api = python_generated_service
        warm_service, _, previous, executor, supply, context = await generated_helpers.configured(
            api, tmp_path
        )
        row, base, root = (previous.row, context.invocation, warm_service.owner.root)
    db.row.tenant_id, db.row.id, db.row.actor_id = (base.tenant_id, base.run_id, row.actor_id)
    db.row.routing_decision = json.loads(json.dumps(row.routing_decision, default=dict))
    if request.param == "python_generated":
        db.row.request = row.request
    runs = Runs(db)
    value = Case(request.param, root, db, runs, executor, InMemoryArtifactRepository(), supply)
    owner = value.owner()
    if request.param == "python_original":
        exact = await owner.authorization_request(original_helpers.request(runs, base))
        assert exact is not None
        scope = capability_approval_scope(exact)
        assert scope is not None
        runs.approvals.add(scope)
        await original_helpers.observed(owner, runs, base)
        db.row.routing_decision = json.loads(json.dumps(db.row.routing_decision, default=dict))
        assert RECEIPT_KEY not in db.row.routing_decision
        assert not value.artifacts._artifacts
        executor.failure_index = None
    else:
        assert supply is not None
        approval = supply.inputs.approval_request(
            user_id=db.row.actor_id, agent_id="tester", idempotency_key="fixture-exact-recipe"
        )
        scope = capability_approval_scope(approval)
        assert scope is not None
        runs.approvals.add(scope)
        assert (
            mapping(present(db.row.routing_decision)["generated_project_preparation"])["phase"]
            == "captured"
        )
    executor.calls.clear()
    executor.probes.clear()
    executor.inspections.clear()
    return value
