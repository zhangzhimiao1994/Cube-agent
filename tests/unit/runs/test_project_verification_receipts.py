"""DAO contracts with real RunRow values; doubles do not prove PostgreSQL locking."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from agent_hub.auth.models import Role
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import TaskContext

KEY = "project_verification_receipt"


def api() -> Any:
    name = "agent_hub.runs.project_verification_receipts"
    assert importlib.util.find_spec(name) is not None, "verification issuance DAO is missing"
    return importlib.import_module(name)


class Database:
    def __init__(self) -> None:
        self.row: Any = RunRow(
            id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role="operator",
            request="Repair the current project", mode="direct", status="running", version=4,
            created_at=datetime.now(UTC), worker_id="worker-one", worker_lease_token=uuid4(),
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            routing_decision={
                "project_id": "project-a", "workspace_session_id": "session-a",
                "sandbox_profile": "workspace_write", "execution_backend": "systemd",
                "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
                "runtime_timeout_seconds": 300, "unrelated": {"keep": [1, False]},
            },
        )
        self.lock = asyncio.Lock()
        self.missing = False
        self.failure: str | None = None
        self.expire_on_flush = False
        self.queries: list[str] = []
        self.commits = 0

    def factory(self) -> Session:
        return Session(self)

    def repository(self) -> Any:
        return api().ProjectVerificationReceiptRepository(cast(Any, self.factory))

    def authority(self, *, actor: str = "main_agent") -> Any:
        row = self.row
        context = TaskContext(
            tenant_id=row.tenant_id, run_id=row.id, actor_id=row.actor_id,
            actor_role=Role(row.actor_role), execution_token=row.worker_lease_token,
            request=row.request, mode=TaskMode(row.mode), routing_decision=row.routing_decision,
        )
        return api().VerificationAuthority.from_context(
            context, worker_id=row.worker_id, actor=actor,
        )

    def new_lease(self) -> None:
        self.row.worker_id = "worker-two"
        self.row.worker_lease_token = uuid4()
        self.row.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)


class Transaction:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        session = self.session
        await session.db.lock.acquire()
        session.row = deepcopy(session.db.row)
        session.active = True

    async def __aexit__(self, error: object, *_: object) -> None:
        session = self.session
        try:
            if error is None:
                if session.db.failure == "commit":
                    raise SQLAlchemyError("PRIVATE_SQL_COMMIT_SENTINEL")
                session.db.row = deepcopy(session.row)
                session.db.commits += 1
        finally:
            session.active = False
            session.db.lock.release()


class Session:
    def __init__(self, db: Database) -> None:
        self.db, self.active = db, False
        self.row = db.row

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        assert not self.active

    def begin(self) -> Transaction:
        return Transaction(self)

    async def scalar(self, statement: Any) -> RunRow | None:
        assert self.active
        if self.db.failure == "select":
            raise SQLAlchemyError("PRIVATE_SQL_SELECT_SENTINEL")
        if self.db.failure == "cancel":
            raise asyncio.CancelledError
        compiled = statement.compile(dialect=cast(Any, postgresql.dialect)())
        sql, params = str(compiled), compiled.params
        self.db.queries.append(sql)
        assert "FOR UPDATE" in sql
        assert {table.name for table in statement.get_final_froms()} == {"agent_hub_runs"}
        assert len(params) == 2
        return None if self.db.missing or set(params.values()) != {
            self.row.id, self.row.tenant_id,
        } else cast(RunRow, self.row)

    async def flush(self) -> None:
        assert self.active
        if self.db.failure == "flush":
            raise SQLAlchemyError("PRIVATE_SQL_FLUSH_SENTINEL")
        if self.db.expire_on_flush:
            self.row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


@pytest.fixture
def db() -> Database:
    return Database()


def refs() -> tuple[ArtifactReference, ...]:
    return (ArtifactReference(id=uuid4(), sha256="b" * 64),)


async def begin(db: Database, authority: Any = None, *, action_key: str = "verify-one") -> Any:
    return await db.repository().begin(
        authority or db.authority(), action_key=action_key,
        execution_id="native_once", source_sha256="a" * 64,
    )


async def passed(db: Database) -> Any:
    record = await begin(db)
    return await db.repository().complete(
        db.authority(), expected=record, status="passed", artifact_references=refs(),
    )


@pytest.mark.parametrize("initial,changed", [
    (None, "default"), ("default", "auto_review"),
    ("auto_review", "default"), ("default", None),
])
async def test_receipt_cannot_load_under_changed_permission_mode(
    db: Database, initial: str | None, changed: str | None,
) -> None:
    if initial is not None:
        db.row.routing_decision["permission_mode"] = initial
    issued = await passed(db)
    if changed is None:
        db.row.routing_decision.pop("permission_mode", None)
    else:
        db.row.routing_decision["permission_mode"] = changed
    with pytest.raises(RunConflict):
        await db.repository().load(db.authority(), generation=issued.generation)


@pytest.mark.parametrize("mode", ["read_only", "full_access", "unknown", True, {}, None])
def test_explicit_non_verifying_permission_mode_cannot_create_authority(db: Database, mode: Any) -> None:
    db.row.routing_decision["permission_mode"] = mode
    with pytest.raises(RunConflict):
        db.authority()


@pytest.mark.parametrize("mode", [None, "default", "auto_review"])
async def test_unchanged_permission_mode_retains_receipt_across_lease_change(
    db: Database, mode: str | None,
) -> None:
    if mode is not None:
        db.row.routing_decision["permission_mode"] = mode
    issued = await passed(db)
    db.new_lease()
    assert await db.repository().load(db.authority(), generation=issued.generation) == issued


async def test_begin_commit_and_cold_load_preserve_only_bounded_issuance_index(db: Database) -> None:
    authority = db.authority()
    before = deepcopy(db.row.routing_decision)
    running = await begin(db, authority)
    assert running.generation == 1 and running.status == "running"
    assert running.action_key == "verify-one" and running.execution_id == "native_once"
    assert running.source_sha256 == "a" * 64 and running.artifact_references == ()
    assert await db.repository().load(authority) is None
    body_refs = refs()
    issued = await db.repository().complete(
        authority, expected=running, status="passed", artifact_references=body_refs,
    )
    assert issued.status == "passed" and issued.artifact_references == body_refs
    assert await db.repository().load(authority, generation=1) == issued
    assert db.row.version == 6
    assert {key: value for key, value in db.row.routing_decision.items() if key != KEY} == before
    encoded = json.dumps(db.row.routing_decision[KEY])
    assert len(encoded.encode()) < 16 * 1024
    assert "Repair the current project" not in encoded
    assert "verification" not in db.row.routing_decision[KEY]
    with pytest.raises(FrozenInstanceError):
        issued.status = "failed"


async def test_new_begin_invalidates_pass_before_work_and_failed_never_falls_back(db: Database) -> None:
    old = await passed(db)
    running = await begin(db, action_key="verify-two")
    assert running.generation == 2 and running.artifact_references == ()
    assert await db.repository().load(db.authority()) is None
    assert await db.repository().load(db.authority(), generation=old.generation) is None
    failed = await db.repository().complete(db.authority(), expected=running, status="failed")
    assert failed.status == "failed"
    db.new_lease()
    assert await db.repository().load(db.authority()) is None
    assert db.row.routing_decision[KEY]["generation"] == 2


async def test_new_lease_can_load_pass_but_old_lease_cannot_access_it(db: Database) -> None:
    old_authority = db.authority()
    issued = await passed(db)
    db.new_lease()
    db.row.routing_decision["approval_id"] = "approval_resolved"
    version = db.row.version
    assert await db.repository().load(db.authority()) == issued
    assert await db.repository().load(db.authority(), generation=2) is None
    assert db.row.version == version
    with pytest.raises(RunConflict):
        await db.repository().load(old_authority)


async def test_new_lease_cannot_complete_old_running_attempt(db: Database) -> None:
    old_authority = db.authority()
    running = await begin(db, old_authority)
    db.new_lease()
    for authority in (old_authority, db.authority()):
        with pytest.raises(RunConflict):
            await db.repository().complete(
                authority, expected=running, status="passed", artifact_references=refs(),
            )
    assert await db.repository().load(db.authority()) is None
    assert db.row.routing_decision[KEY]["status"] == "running"


async def test_latest_generation_fences_late_completion_even_under_same_lease(db: Database) -> None:
    first = await begin(db)
    await db.repository().complete(db.authority(), expected=first, status="failed")
    second = await begin(db, action_key="verify-two")
    with pytest.raises(RunConflict):
        await db.repository().complete(
            db.authority(), expected=first, status="passed", artifact_references=refs(),
        )
    issued = await db.repository().complete(
        db.authority(), expected=second, status="passed", artifact_references=refs(),
    )
    assert issued.generation == 2


@pytest.mark.parametrize("second_key", ["verify-one", "verify-two"])
async def test_concurrent_begins_allow_exactly_one_running_attempt(
    db: Database, second_key: str,
) -> None:
    results = await asyncio.gather(
        begin(db), begin(db, action_key=second_key), return_exceptions=True,
    )
    records = [result for result in results if isinstance(result, api().VerificationRecord)]
    conflicts = [result for result in results if isinstance(result, RunConflict)]
    assert len(records) == 1 and len(conflicts) == 1
    assert records[0].generation == 1 and records[0].status == "running"
    assert db.row.routing_decision[KEY]["generation"] == 1 and db.row.version == 5


@pytest.mark.parametrize("action_key", ["verify-one", "verify-two"])
@pytest.mark.parametrize("new_lease", [False, True])
async def test_running_attempt_cannot_be_restarted_even_after_worker_lease_loss(
    db: Database, action_key: str, new_lease: bool,
) -> None:
    await begin(db)
    if new_lease:
        db.new_lease()
    before, version, commits = deepcopy(db.row.routing_decision), db.row.version, db.commits
    with pytest.raises(RunConflict):
        await begin(db, action_key=action_key)
    assert db.row.routing_decision == before and db.row.version == version
    assert db.commits == commits


@pytest.mark.parametrize("status", ["passed", "failed"])
@pytest.mark.parametrize("new_lease", [False, True])
async def test_terminal_action_key_cannot_be_reexecuted(
    db: Database, status: str, new_lease: bool,
) -> None:
    running = await begin(db)
    await db.repository().complete(
        db.authority(), expected=running, status=status,
        artifact_references=refs() if status == "passed" else (),
    )
    if new_lease:
        db.new_lease()
    before, version = deepcopy(db.row.routing_decision), db.row.version
    with pytest.raises(RunConflict):
        await db.repository().begin(
            db.authority(), action_key="verify-one", execution_id="different_execution",
            source_sha256="c" * 64,
        )
    assert db.row.routing_decision == before and db.row.version == version


@pytest.mark.parametrize("status", ["passed", "failed"])
async def test_only_terminal_attempt_with_different_key_allows_new_generation(
    db: Database, status: str,
) -> None:
    running = await begin(db)
    await db.repository().complete(
        db.authority(), expected=running, status=status,
        artifact_references=refs() if status == "passed" else (),
    )
    db.new_lease()
    next_attempt = await begin(db, action_key="verify-two")
    assert next_attempt.generation == 2 and next_attempt.status == "running"
    assert next_attempt.action_key == "verify-two" and next_attempt.artifact_references == ()
    assert await db.repository().load(db.authority()) is None


def mutate(db: Database, defect: str) -> None:
    row, route = db.row, db.row.routing_decision
    if defect in {"actor", "token"}:
        setattr(row, "actor_id" if defect == "actor" else "worker_lease_token", uuid4())
    elif defect in {"role", "worker", "status", "request", "mode"}:
        field = {"role": "actor_role", "worker": "worker_id"}.get(defect, defect)
        setattr(row, field, {"role": "admin", "worker": "other-worker", "status": "paused",
                            "request": "different request", "mode": "dispatch"}[defect])
    elif defect in {"expiry", "missing_expiry", "naive_expiry"}:
        row.worker_lease_expires_at = {
            "expiry": datetime.now(UTC) - timedelta(seconds=1), "missing_expiry": None,
            "naive_expiry": datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5),
        }[defect]
    elif defect in {"permissions", "added_permission"}:
        route["requested_permissions"] = ["workspace.read"] if defect == "permissions" else [
            *route["requested_permissions"], "network.read",
        ]
    elif defect in {"project", "session", "profile", "backend"}:
        route[{"project": "project_id", "session": "workspace_session_id",
               "profile": "sandbox_profile", "backend": "execution_backend"}[defect]] = "other"
    else:
        route[defect] = {"changed": True}


@pytest.mark.parametrize("operation", ["begin", "complete", "load"])
@pytest.mark.parametrize("defect", [
    "actor", "role", "worker", "token", "expiry", "missing_expiry", "naive_expiry",
    "status", "request", "mode", "permissions", "added_permission", "project", "session",
    "profile", "backend", "project_input", "project_input_preparation", "python_input_supply",
    "generated_project_preparation", "toolchain_supply", "go_project_recipe",
])
async def test_every_access_revalidates_current_run_authority(
    db: Database, operation: str, defect: str,
) -> None:
    authority = db.authority()
    running = await begin(db, authority)
    if operation == "load":
        await db.repository().complete(
            authority, expected=running, status="passed", artifact_references=refs(),
        )
    mutate(db, defect)
    before, version = deepcopy(db.row.routing_decision), db.row.version
    with pytest.raises(RunConflict):
        if operation == "begin":
            await begin(db, authority)
        elif operation == "load":
            await db.repository().load(authority)
        else:
            await db.repository().complete(authority, expected=running, status="failed")
    assert db.row.routing_decision == before and db.row.version == version


@pytest.mark.parametrize("defect", ["actor", "role", "request", "mode", "project", "session",
                                   "added_permission", "project_input_preparation"])
async def test_fresh_current_authority_cannot_load_changed_stable_scope(
    db: Database, defect: str,
) -> None:
    await passed(db)
    db.new_lease()
    mutate(db, defect)
    with pytest.raises(RunConflict):
        await db.repository().load(db.authority())


@pytest.mark.parametrize("role", [None, "viewer", "not-a-role"])
async def test_routing_role_cannot_override_trusted_runrow_role(db: Database, role: Any) -> None:
    authority = db.authority()
    db.row.actor_role = role
    db.row.routing_decision["actor_role"] = "operator"
    with pytest.raises(RunConflict):
        await begin(db, authority)
    assert KEY not in db.row.routing_decision


async def test_actor_string_is_part_of_issued_scope(db: Database) -> None:
    await passed(db)
    with pytest.raises(RunConflict):
        await db.repository().load(db.authority(actor="engineer"))


@pytest.mark.parametrize("operation", ["begin", "complete", "load"])
@pytest.mark.parametrize("corruption", ["null", "partial", "boolean_generation", "unknown_status",
                                       "unknown_outcome",
                                       "extra", "bad_reference", "oversized"])
async def test_corrupt_index_never_resets_generation_or_becomes_a_pass(
    db: Database, operation: str, corruption: str,
) -> None:
    running = await begin(db)
    item = db.row.routing_decision[KEY]
    if corruption == "null":
        db.row.routing_decision[KEY] = None
    elif corruption == "partial":
        item.pop("authority")
    elif corruption == "boolean_generation":
        item["generation"] = True
    elif corruption == "unknown_status":
        item["status"] = "approved"
    elif corruption == "unknown_outcome":
        item["status"] = "unknown"
    elif corruption == "extra":
        item["passed"] = True
    elif corruption == "bad_reference":
        item["artifact_references"] = [{"id": str(uuid4()), "sha256": "INVALID"}]
    else:
        item["action_key"] = "x" * 20_000
    before = deepcopy(db.row.routing_decision)
    with pytest.raises(RunConflict):
        if operation == "begin":
            await begin(db)
        elif operation == "load":
            await db.repository().load(db.authority())
        else:
            await db.repository().complete(db.authority(), expected=running, status="failed")
    assert db.row.routing_decision == before


@pytest.mark.parametrize("defect", ["generation", "source", "execution", "action", "actor"])
async def test_completion_compares_full_expected_record(db: Database, defect: str) -> None:
    running = await begin(db)
    updates: dict[str, dict[str, Any]] = {
        "generation": {"generation": 2}, "source": {"source_sha256": "c" * 64},
        "execution": {"execution_id": "other_execution"}, "action": {"action_key": "other"},
        "actor": {"authority": replace(running.authority, actor="engineer")},
    }
    expected = replace(running, **updates[defect])
    with pytest.raises(RunConflict):
        await db.repository().complete(db.authority(), expected=expected, status="failed")
    assert db.row.routing_decision[KEY]["status"] == "running"


@pytest.mark.parametrize("status", ["passed", "failed"])
async def test_completed_attempt_cannot_be_completed_twice(db: Database, status: str) -> None:
    running = await begin(db)
    await db.repository().complete(
        db.authority(), expected=running, status=status,
        artifact_references=refs() if status == "passed" else (),
    )
    with pytest.raises(RunConflict):
        await db.repository().complete(db.authority(), expected=running, status="failed")


@pytest.mark.parametrize("defect", ["empty_pass", "too_many", "duplicate", "raw_mapping", "status"])
async def test_completion_rejects_unbounded_or_untyped_index_inputs(db: Database, defect: str) -> None:
    running = await begin(db)
    body_refs: Any = refs()
    status = "passed"
    if defect == "empty_pass":
        body_refs = ()
    elif defect == "too_many":
        body_refs = tuple(ArtifactReference(id=uuid4(), sha256="b" * 64) for _ in range(65))
    elif defect == "duplicate":
        body_refs = (body_refs[0], body_refs[0])
    elif defect == "raw_mapping":
        body_refs = (body_refs[0].to_payload(),)
    else:
        status = "approved"
    with pytest.raises((RunConflict, ValueError, TypeError)):
        await db.repository().complete(
            db.authority(), expected=running, status=status, artifact_references=body_refs,
        )
    assert db.row.routing_decision[KEY]["status"] == "running"


@pytest.mark.parametrize("operation", ["begin", "complete", "load"])
async def test_missing_or_cross_tenant_run_is_unavailable(db: Database, operation: str) -> None:
    authority = db.authority()
    running = await begin(db)
    db.missing = True
    with pytest.raises(RunNotFound):
        if operation == "begin":
            await begin(db, authority)
        elif operation == "complete":
            await db.repository().complete(authority, expected=running, status="failed")
        else:
            await db.repository().load(authority)
    db.missing = False
    with pytest.raises(RunNotFound):
        await db.repository().load(replace(authority, tenant_id=uuid4()))


@pytest.mark.parametrize("failure", ["select", "flush", "commit", "cancel"])
@pytest.mark.parametrize("operation", ["begin", "complete"])
async def test_storage_failure_and_cancellation_never_commit_partial_issuance(
    db: Database, failure: str, operation: str,
) -> None:
    running = await begin(db)
    if operation == "begin":
        await db.repository().complete(db.authority(), expected=running, status="failed")
    before, version = deepcopy(db.row.routing_decision), db.row.version
    db.failure = failure
    error_type = asyncio.CancelledError if failure == "cancel" else RunConflict
    with pytest.raises(error_type) as caught:
        if operation == "begin":
            await begin(db, action_key="verify-two")
        else:
            await db.repository().complete(
                db.authority(), expected=running, status="passed", artifact_references=refs(),
            )
    assert "PRIVATE_SQL" not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert db.row.routing_decision == before and db.row.version == version
    assert not db.lock.locked()


@pytest.mark.parametrize("operation", ["begin", "complete"])
async def test_lease_expiring_during_flush_rolls_back(db: Database, operation: str) -> None:
    running = await begin(db)
    if operation == "begin":
        await db.repository().complete(db.authority(), expected=running, status="failed")
    before = deepcopy(db.row.routing_decision)
    db.expire_on_flush = True
    with pytest.raises(RunConflict):
        if operation == "begin":
            await begin(db, action_key="verify-two")
        else:
            await db.repository().complete(db.authority(), expected=running, status="failed")
    assert db.row.routing_decision == before
