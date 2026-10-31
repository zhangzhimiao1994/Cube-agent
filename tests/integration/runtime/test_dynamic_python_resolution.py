"""Dedicated-PG dynamic resolution checkpoint integration; native setup FAILED, fix rerun PENDING.

Actual RunRecord/private execution binding, PostgresArtifactRepository lookup/hash
readback and original pool. Only the final Root inspect transport is deterministic.
This node is NOT registry/download/install/ordinary-user acceptance evidence.
"""
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.python_input_preparation import (
    DynamicPythonResolutionFlow,
    DynamicResolutionJournal,
)
from agent_hub.capabilities.python_metadata_preparation import metadata_scope
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.db.models import RunRow
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataDraft,
    PythonMetadataPlanningRequest,
)
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus, resolution_key
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414
)
from tests.integration.models.test_run_admission import prepare
from tests.integration.models.test_run_admission import storage as storage  # noqa: PLC0414
from tests.unit.skills.test_python_registry_read import environment

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


def _recovery_route():
    return {"generated_project_contract_version": 1, "python_dependency_preflight_version": 1,
             "python_dependency_preflight_submission_version": 1, "project_id": "generated-project",
             "workspace_session_id": "session", "sandbox_profile": "workspace_write",
             "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
             "execution_backend": "systemd", "runtime_timeout_seconds": 120,
             "runtime_token_absolute_tokens": 100, "runtime_plan_token_budget": 100}


async def _prepare_resolution_pool(storage, *, route):
    # Keep prepare's original grant/evidence/lease. Narrow its generic 30-minute
    # deadline to this fixture's existing 120-second runtime before the first open.
    repository, binding, opening = await prepare(storage, route=route, open_now=False)
    async with storage[0]() as session:
        original = await session.get(RunRow, binding["run_id"])
    opening = {**opening, "original_deadline": min(opening["original_deadline"],
        original.created_at + timedelta(seconds=route["runtime_timeout_seconds"]))}
    await repository.open_pool(**opening)
    return repository, binding, opening


async def test_pg_dynamic_resolution_cold_restore_keeps_scope_and_original_pool(storage):
    route = _recovery_route()
    pool_repo, binding, _ = await _prepare_resolution_pool(storage, route=route)
    factory = storage[0]
    repository = RunRepository(factory)
    record = await repository.get(binding["tenant_id"], binding["run_id"])
    before = await pool_repo.get_pool(record.tenant_id, record.id)
    assert before.original_token_grant == 100
    assert before.original_deadline == record.created_at + timedelta(seconds=120)
    scope = metadata_scope(record, actor="engineer", live=True)
    deadline = record.created_at + timedelta(seconds=120)
    data_scope = {**scope.model_dump(mode="json"), "root_sha256": "8" * 64,
                  "catalog_sha256": "3" * 64, "original_deadline": deadline.isoformat()}
    artifacts = PostgresArtifactRepository(factory)
    journal = DynamicResolutionJournal(artifacts)
    needs = (DependencyNeed("synthetic-tool>=1", "a" * 64),)
    context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, actor_id=record.actor_id,
        actor_role=record.actor_role, execution_token=record.worker_lease_token, mode=record.mode, request=record.request,
        routing_decision=record.routing_decision, timeout_seconds=120)
    planning = PythonMetadataPlanningRequest(
        invocation=ProjectInvocation(execution_id="recover_" + uuid4().hex, tenant_id=record.tenant_id,
            run_id=record.id, project_id="generated-project", session_id="session",
            sandbox_profile="restricted", install_allowed=False, timeout_seconds=120),
        scope=metadata_scope(record, actor="engineer", live=True),
        draft=PythonMetadataDraft(source_sha256="c" * 64, acceptance_sha256="d" * 64,
                                  manifest_sha256="e" * 64, lock_sha256="f" * 64),
        expected_root_config_sha256="8" * 64, expected_catalog_sha256="3" * 64)
    # Derive the opaque data key through the actual private preparation contract,
    # using the original worker. No synthetic key may stand in for Root addressing.
    async def original_row():
        return record
    with bind_execution_authority(context, worker_id=record.worker_id, actor="engineer"):
        origin = await PythonResolutionPreparation(current=original_row, approved=None)._claim(planning)
    key = resolution_key(origin, needs, environment())
    ref = await journal.save(data_scope, needs=needs, environment=environment(),
                             resolution_key=key, pending_plan_sha256="b" * 64)
    stored, = await artifacts.get_many(record.tenant_id, record.id, (ref,))
    assert str(record.worker_lease_token) not in str(stored.to_payload())
    # Controlled DB lease handoff only after durable checkpoint publication.
    token = uuid4()
    async with factory() as session, session.begin():
        row = await session.get(RunRow, record.id)
        row.worker_id, row.worker_lease_token = "recovered-worker", token
    current = await repository.get(record.tenant_id, record.id)
    context = TaskContext(run_id=current.id, tenant_id=current.tenant_id, actor_id=current.actor_id,
        actor_role=current.actor_role, execution_token=token, mode=current.mode, request=current.request,
        routing_decision=current.routing_decision, timeout_seconds=120)
    loaded = await DynamicResolutionJournal(PostgresArtifactRepository(factory)).load(data_scope)
    assert loaded is not None
    calls = []
    async def now():
        return await repository.get(current.tenant_id, current.id)
    async def forbidden_approval(request):
        raise AssertionError("read-only recovery must not request a new effect approval")
    async def root_inspect(request):
        assert request.action == "inspect_python_resolution"
        assert request.expected_plan_sha256 == "b" * 64
        assert request.claim.lease_token == token and request.claim.original_deadline == deadline
        calls.append(request.action)
        return ResolutionStatus(state="unknown", key=request.key)
    fresh = DynamicPythonResolutionFlow(PythonResolutionPreparation(current=now,
        approved=forbidden_approval, transport=root_inspect))
    fresh.restore(loaded)
    with (bind_execution_authority(context, worker_id="recovered-worker", actor="engineer"),
          pytest.raises(ValueError, match="inspect_only")):
        await fresh.resolve_data(needs=(DependencyNeed("synthetic-tool==1.0", "0" * 64),),
                                 environment=environment(), planning=planning)
    assert calls == ["inspect_python_resolution"]
    after = await pool_repo.get_pool(current.tenant_id, current.id)
    assert after == before
    latest = await repository.get(current.tenant_id, current.id)
    assert latest.routing_decision == route and latest.worker_lease_token == token
    foreign = dict(data_scope, run_id=str(UUID(int=999)))
    with pytest.raises(ValueError):
        await DynamicResolutionJournal(PostgresArtifactRepository(factory)).load(foreign)


class _PreparationSession:
    """Only SQL storage IO is simulated; real prepare/auth/admission DAO code runs."""

    def __init__(self):
        from datetime import UTC, datetime
        self.now = datetime.now(UTC)
        self.rows = []
        self.active = False
        self.authorization_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def transaction():
            assert not self.active
            self.active = True
            try:
                yield
            finally:
                self.active = False
        return transaction()

    def in_transaction(self):
        return self.active

    def add(self, row):
        if isinstance(row, RunRow) and row.created_at is None:
            row.created_at = self.now
        self.rows.append(row)

    async def flush(self):
        pass

    async def get(self, kind, key):
        return next((row for row in self.rows if type(row) is kind
                     and (row.id if kind is RunRow else row.run_id) == key), None)

    async def scalar(self, statement):
        if "clock_timestamp()" in str(statement):
            return self.now
        entity = statement.column_descriptions[0]["entity"]
        parameters = tuple(statement.compile().params.values())
        candidates = [row for row in self.rows if type(row) is entity]
        return next((row for row in candidates if row.tenant_id in parameters
                     and (row.id if entity is RunRow else row.run_id) in parameters), None)

    async def execute(self, statement):
        from types import SimpleNamespace

        from agent_hub.db.models import RunAuthenticatedInputRow, RunInputAuthorizationRow
        if "clock_timestamp()" in str(statement):
            return SimpleNamespace(scalar_one=lambda: self.now)
        assert [column["entity"] for column in statement.column_descriptions] == [RunInputAuthorizationRow, RunAuthenticatedInputRow]
        self.authorization_reads += 1
        parameters = tuple(statement.compile().params.values())
        link = next((row for row in self.rows if type(row) is RunInputAuthorizationRow and row.run_id in parameters), None)
        turn = None if link is None else next((row for row in self.rows if type(row) is RunAuthenticatedInputRow and row.turn_id == link.turn_id), None)
        return SimpleNamespace(one_or_none=lambda: None if link is None or turn is None else (link, turn))


async def test_local_recovery_preparation_uses_real_authenticated_original_pool_contract():
    from agent_hub.db.models import RunAdmissionEventRow
    session = _PreparationSession()
    storage = (lambda: session, [])
    route = _recovery_route()
    repository, binding, opening = await _prepare_resolution_pool(storage, route=route)
    before = await repository.get_pool(binding["tenant_id"], binding["run_id"])
    row = await session.get(RunRow, binding["run_id"])
    assert before.original_token_grant == opening["original_token_grant"] == 100
    assert before.original_deadline == row.created_at + timedelta(seconds=120)
    assert before.authorization_turn_id == binding["authorization_turn_id"]
    assert row.routing_decision == route
    assert row.worker_lease_token == binding["lease"].token
    assert metadata_scope(row, actor="engineer", live=True).actor_role == "operator"
    await repository.require_current(**binding)
    assert await repository.open_pool(**opening) == before
    assert await repository.get_pool(binding["tenant_id"], binding["run_id"]) == before
    assert session.authorization_reads >= 3
    assert len([row for row in session.rows if type(row) is RunAdmissionEventRow]) == 1


@pytest.mark.parametrize("changed", ["lease", "authenticated_input"])
async def test_local_recovery_prepare_retains_original_identity_fences(changed):
    from agent_hub.db.models import RunAuthenticatedInputRow
    from agent_hub.models.run_admission_repository import AdmissionConflict
    session = _PreparationSession()
    repository, binding, _ = await _prepare_resolution_pool((lambda: session, []), route=_recovery_route())
    before = await repository.get_pool(binding["tenant_id"], binding["run_id"])
    if changed == "lease":
        row = await session.get(RunRow, binding["run_id"])
        row.worker_lease_token = uuid4()
    else:
        row = next(row for row in session.rows if type(row) is RunAuthenticatedInputRow)
        row.message = "altered authenticated original"
    with pytest.raises(AdmissionConflict):
        await repository.require_current(**binding)
    assert await repository.get_pool(binding["tenant_id"], binding["run_id"]) == before


@pytest.mark.parametrize("changed", ["grant", "deadline"])
async def test_local_recovery_prepare_cannot_extend_the_original_pool(changed):
    from agent_hub.models.run_admission_repository import AdmissionConflict
    session = _PreparationSession()
    repository, binding, opening = await _prepare_resolution_pool((lambda: session, []), route=_recovery_route())
    before = await repository.get_pool(binding["tenant_id"], binding["run_id"])
    updated = dict(opening)
    if changed == "grant":
        updated["original_token_grant"] += 1
    else:
        updated["original_deadline"] += timedelta(seconds=1)
    with pytest.raises(AdmissionConflict):
        await repository.open_pool(**updated)
    assert await repository.get_pool(binding["tenant_id"], binding["run_id"]) == before


async def test_local_generic_admission_deadline_cannot_bypass_recovery_timeout():
    from agent_hub.db.models import RunAdmissionPoolRow
    from agent_hub.models.run_admission_repository import AdmissionConflict
    session = _PreparationSession()
    # The shared helper's 30-minute default must still fail for this 120s route.
    with pytest.raises(AdmissionConflict):
        await prepare((lambda: session, []), route=_recovery_route())
    assert not any(type(row) is RunAdmissionPoolRow for row in session.rows)
