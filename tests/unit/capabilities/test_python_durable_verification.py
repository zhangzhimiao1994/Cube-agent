"""Python services through the real coordinator, private Artifacts and lease DAO.

Only the SQL session and Root transport are doubles. Native Python never runs.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import (
    validate_generated_preparation,
    validate_generated_transition,
)
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_generated_service import PythonGeneratedRecipeApprovalRequired
from agent_hub.capabilities.python_project_reports import PythonProjectFailure
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY, VerificationRecord
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities import test_python_generated_service as generated_helpers
from tests.unit.capabilities import test_python_project_service as original_helpers
from tests.unit.remaining_test_contract_typed_fixture import caches
from tests.unit.runs.test_project_verification_receipts import Database


class Runs:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.approvals: set[str] = set()
        self.updates = 0

    @property
    def record(self) -> Any:
        # Compatibility with the real original-observation fixture helper.
        return self.db.row

    async def get(self, tenant: UUID, run: UUID) -> Any:
        assert (tenant, run) == (self.db.row.tenant_id, self.db.row.id)
        return RunRepository._record(self.db.row)

    async def is_capability_approval_scope_approved(
        self, tenant: UUID, run: UUID, scope: str,
    ) -> bool:
        assert (tenant, run) == (self.db.row.tenant_id, self.db.row.id)
        return scope in self.approvals

    async def compare_and_set_generated_preparation(
        self, tenant: UUID, run: UUID, **kw: Any,
    ) -> Any:
        row = self.db.row
        assert (tenant, run, kw["actor_id"], kw["lease_token"]) == (
            row.tenant_id, row.id, row.actor_id, row.worker_lease_token)
        before = row.routing_decision.get("generated_project_preparation")
        assert (None if before is None else validate_generated_preparation(before)) == kw["expected"]
        after = validate_generated_transition(before, kw["replacement"])
        row.routing_decision = {**row.routing_decision,
            "generated_project_preparation": json.loads(json.dumps(after, default=dict))}
        row.version += 1
        self.updates += 1
        return RunRepository._record(row)


@dataclass
class Case:
    kind: str
    root: Path
    db: Database
    runs: Runs
    executor: Any
    artifacts: InMemoryArtifactRepository
    supply: Any = None

    def owner(self, *, executor: Any = None) -> ProjectVerificationService:
        owner = ProjectVerificationService(
            root=self.root, repository=self.runs, executor=None,
            python_executor=self.executor if executor is None else executor,
            receipt_repository=self.db.repository(), artifact_repository=self.artifacts)
        if self.supply is not None:
            owner._python_generated.supply_client = self.supply
        return owner

    def bind(self) -> Any:
        row = self.db.row
        context = TaskContext(
            tenant_id=row.tenant_id, run_id=row.id, actor_id=row.actor_id,
            actor_role=Role(row.actor_role), execution_token=row.worker_lease_token,
            request=row.request, mode=TaskMode(row.mode), routing_decision=row.routing_decision)
        # The worker's actor differs from the approved verifier actor on purpose.
        return bind_execution_authority(context, worker_id=row.worker_id)

    def arguments(self, *, actor: str = "tester", action: str = "python-check-one") -> dict[str, Any]:
        return {"tenant_id": self.db.row.tenant_id, "run_id": self.db.row.id,
                "actor": actor, "action_key": action}

    def index(self) -> VerificationRecord:
        return VerificationRecord.from_payload(self.db.row.routing_decision[RECEIPT_KEY])

    def assert_no_receipts(self, owner: ProjectVerificationService) -> None:
        key = (self.db.row.tenant_id, self.db.row.id)
        # environments retain historical Root issuance, never delivery authority.
        assert all(key not in cache for cache in (
            owner._receipts, owner._original_receipts, owner._generated_receipts,
            owner._python.receipts, owner._python_generated.receipts))


@pytest.fixture(params=["python_original", "python_generated"])
async def case(request: Any, tmp_path: Path) -> Case:
    db = Database()
    if request.param == "python_original":
        warm, previous, executor, base, _ = await original_helpers.configured(tmp_path)
        row = previous.record
        root = warm.root
        supply = None
    else:
        api = importlib.import_module("agent_hub.capabilities.python_generated_service")
        warm_service, _, previous, executor, supply, context = (
            await generated_helpers.configured(api, tmp_path))
        row, base, root = previous.row, context.invocation, warm_service.owner.root
    db.row.tenant_id, db.row.id, db.row.actor_id = base.tenant_id, base.run_id, row.actor_id
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
        # The original callback must never reserve a candidate verification attempt.
        await original_helpers.observed(owner, runs, base)
        db.row.routing_decision = json.loads(json.dumps(db.row.routing_decision, default=dict))
        assert RECEIPT_KEY not in db.row.routing_decision
        assert not value.artifacts._artifacts
        executor.failure_index = None
    else:
        assert supply is not None
        approval = supply.inputs.approval_request(user_id=db.row.actor_id, agent_id="tester",
                                                 idempotency_key="fixture-exact-recipe")
        scope = capability_approval_scope(approval)
        assert scope is not None
        runs.approvals.add(scope)
        assert db.row.routing_decision["generated_project_preparation"]["phase"] == "captured"
    executor.calls.clear()
    executor.probes.clear()
    executor.inspections.clear()
    return value


async def issued(case: Case) -> Mapping[str, Any]:
    with case.bind():
        return await case.owner().verify(**case.arguments())


def forbid_restore_side_effects(
    case: Case, owner: ProjectVerificationService, executor: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("durable restore attempted execution, supply, preparation or persistence")

    for target, methods in [
        (owner, ("verify", "_begin_verification")),
        (owner._python, ("verify", "prepare", "_run", "_persist_reference")),
        (owner._python_generated, ("verify", "_prepare", "_supply", "_plan", "_persist_reference")),
        (owner.generated, ("prepare", "update")),
        (owner._python_generated.supply_client, ("plan", "supply")),
        (executor, ("run", "probe")),
        (case.artifacts, ("reserve_write", "put", "abort_write")),
        (case.runs, ("compare_and_set_generated_preparation",)),
    ]:
        for method in methods:
            monkeypatch.setattr(target, method, forbidden)


async def test_real_callbacks_issue_fresh_preparation_scope_and_actual_actor_execution(
    case: Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_scope = case.db.authority(actor="tester").scope_sha256
    original_run = case.executor.run
    dispatched = []

    async def run(invocation: Any) -> Any:
        index = case.index()
        assert index.status == "running"
        assert index.execution_id == invocation.execution_id
        assert index.authority.actor == "tester"
        assert index.authority.scope_sha256 == case.db.authority(actor="tester").scope_sha256
        assert not case.artifacts._artifacts
        dispatched.append(invocation)
        return await original_run(invocation)

    monkeypatch.setattr(case.executor, "run", run)
    result = await issued(case)
    index = case.index()
    assert index.status == "passed" and index.generation == 1
    assert len(dispatched) == 1 and dispatched[0].source_kind == "candidate"
    assert result["verification"]["execution_id"] == index.execution_id == dispatched[0].execution_id
    assert result["verification"]["source_sha256"] == index.source_sha256
    if case.kind == "python_generated":
        assert index.authority.scope_sha256 != before_scope
        assert case.db.row.routing_decision["generated_project_preparation"]["phase"] == "python_ready"
    artifacts = await case.artifacts.get_many(case.db.row.tenant_id, case.db.row.id,
                                              index.artifact_references)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert (artifact.type, artifact.producer) == (
        "project_verification_receipt", "trusted_project_verifier")
    assert artifact.content["kind"] == case.kind
    assert artifact.content["execution_id"] == dispatched[0].execution_id
    scope = artifact.content["scope"]
    assert isinstance(scope, Mapping) and scope["actor"] == "tester"
    assert artifact.content["result"] == result


async def test_verify_then_fresh_owner_lease_restores_private_receipt_without_execution(
    case: Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await issued(case)
    case.db.new_lease()
    executor = type(case.executor)(case.root)
    owner = case.owner(executor=executor)
    forbid_restore_side_effects(case, owner, executor, monkeypatch)
    route = json.dumps(case.db.row.routing_decision, default=dict, sort_keys=True)
    with case.bind():
        assert await owner.restore(**case.arguments()) == result
        assert await owner.require_current_receipt(case.db.row.tenant_id, case.db.row.id) == (
            result["verification"]["source_sha256"])
    assert len(case.executor.calls) == 1
    assert not executor.calls and not executor.probes
    assert executor.inspections and all(not call.install_allowed for call in executor.inspections)
    assert json.dumps(case.db.row.routing_decision, default=dict, sort_keys=True) == route


@pytest.mark.parametrize("fault", ["actor", "missing_artifact", "revoked_grant", "action"])
async def test_python_restore_requires_private_issuance_current_actor_and_grant(
    case: Case, fault: str,
) -> None:
    await issued(case)
    case.db.new_lease()
    if fault == "missing_artifact":
        case.artifacts = InMemoryArtifactRepository()
    elif fault == "revoked_grant":
        case.runs.approvals.clear()
    executor = type(case.executor)(case.root)
    owner = case.owner(executor=executor)
    arguments = case.arguments(actor="engineer" if fault == "actor" else "tester",
                               action="other-action" if fault == "action" else "python-check-one")
    with case.bind(), pytest.raises(ProjectVerificationError):
        await owner.restore(**arguments)
    case.assert_no_receipts(owner)
    assert not executor.calls and not executor.probes


@pytest.mark.parametrize("commit_failure", [False, True])
async def test_python_failed_report_is_typed_and_failed_cas_uncertainty_stays_unknown(
    case: Case, monkeypatch: pytest.MonkeyPatch, commit_failure: bool,
) -> None:
    case.executor.failure_index = 2
    owner = case.owner()
    run = case.executor.run

    async def fail(invocation: Any) -> Any:
        result = await run(invocation)
        if commit_failure:
            case.db.failure = "commit"
        return result

    monkeypatch.setattr(case.executor, "run", fail)
    with case.bind(), pytest.raises(ProjectVerificationError) as caught:
        await owner.verify(**case.arguments())
    error = caught.value
    assert error.project_failure is None and error.project_prerequisite is None
    if commit_failure:
        assert error.python_project_failure is None and error.diagnostic is None
    else:
        assert type(error.python_project_failure) is PythonProjectFailure
        assert error.diagnostic is error.python_project_failure.diagnostic
        assert "secret" not in repr(error.python_project_failure.to_payload())
    assert case.index().status == ("running" if commit_failure else "failed")
    artifacts = tuple(case.artifacts._artifacts.values())
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == "project_verification_failure"
    assert artifact.producer == "trusted_project_failure_verifier"
    assert artifact.content["status"] == "failed"
    assert artifact.content["purpose"] == "failure_diagnostics_only"
    if commit_failure:
        # A CAS error cannot establish whether the index committed; keep its body.
        assert not case.index().artifact_references
    else:
        assert case.index().artifact_references == (
            ArtifactReference(artifact.id, artifact.content_sha256),
        )
    case.assert_no_receipts(owner)


@pytest.mark.parametrize("fault", ["malformed_report", "cleanup", "cancel"])
async def test_uncertain_python_execution_keeps_running_and_cannot_be_replayed(
    case: Case, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    run = case.executor.run
    cleanup = ProcessCleanupIncomplete("python_project_cleanup_incomplete")
    cancellation = asyncio.CancelledError()

    async def uncertain(invocation: Any) -> Any:
        result = await run(invocation)
        if fault == "cleanup":
            raise cleanup
        if fault == "cancel":
            raise cancellation
        assert result.result is not None
        return result.model_copy(update={"result": result.result.model_copy(update={"stdout": "{}"})})

    monkeypatch.setattr(case.executor, "run", uncertain)
    owner = case.owner()
    expected = {"cleanup": ProcessCleanupIncomplete, "cancel": asyncio.CancelledError}.get(
        fault, ProjectVerificationError)
    with case.bind(), pytest.raises(expected) as caught:
        await owner.verify(**case.arguments())
    if fault == "cleanup":
        assert caught.value is cleanup
    elif fault == "cancel":
        assert caught.value is cancellation
    else:
        assert isinstance(caught.value, ProjectVerificationError)
        assert caught.value.python_project_failure is None
    assert case.index().status == "running" and not case.artifacts._artifacts
    case.assert_no_receipts(owner)
    with case.bind(), pytest.raises(ProjectVerificationError):
        await case.owner().verify(**case.arguments(action="cannot-repeat-unknown"))
    assert len(case.executor.calls) == 1


@pytest.mark.parametrize("failure", [ProcessCleanupIncomplete, asyncio.CancelledError])
async def test_cold_environment_inspection_preserves_cleanup_and_cancellation(
    case: Case, monkeypatch: pytest.MonkeyPatch, failure: Any,
) -> None:
    await issued(case)
    case.db.new_lease()
    executor = type(case.executor)(case.root)
    owner = case.owner(executor=executor)
    error = failure("cold environment inspection interrupted")

    async def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(executor, "inspect_environment", interrupted)
    with case.bind(), pytest.raises(failure) as caught:
        await owner.restore(**case.arguments())
    assert caught.value is error
    assert all(not cache for cache in caches(owner))
    assert not executor.calls and not executor.probes


async def test_missing_trusted_binding_stops_python_before_dispatch(case: Case) -> None:
    owner = case.owner()
    with pytest.raises(ProjectVerificationError):
        await owner.verify(**case.arguments())
    assert RECEIPT_KEY not in case.db.row.routing_decision
    assert not case.executor.calls and not case.artifacts._artifacts


async def test_missing_recipe_grant_remains_a_typed_approval_before_begin(case: Case) -> None:
    case.runs.approvals.clear()
    owner = case.owner()
    with case.bind(), pytest.raises(ProjectVerificationError) as caught:
        await owner.verify(**case.arguments())
    assert caught.value.python_project_failure is None
    if case.kind == "python_generated":
        assert type(caught.value) is PythonGeneratedRecipeApprovalRequired
        assert caught.value.approval_request.agent_id == "tester"
    assert RECEIPT_KEY not in case.db.row.routing_decision
    assert not case.executor.calls and not case.artifacts._artifacts


async def test_preparation_refresh_never_adopts_a_replaced_worker_lease(
    case: Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = case.owner()
    begin = owner._begin_verification

    async def replaced(**arguments: Any) -> None:
        case.db.new_lease()
        await begin(**arguments)

    monkeypatch.setattr(owner, "_begin_verification", replaced)
    with case.bind(), pytest.raises(ProjectVerificationError) as caught:
        await owner.verify(**case.arguments())
    assert caught.value.python_project_failure is None
    assert RECEIPT_KEY not in case.db.row.routing_decision
    assert not case.executor.calls and not case.artifacts._artifacts
    case.assert_no_receipts(owner)


@pytest.mark.parametrize("case", ["python_generated"], indirect=True)
async def test_generated_supply_refreshes_bound_scope_before_real_dispatch(
    case: Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = case.owner()
    case.executor.probe_error = "python_project_wheel_missing"
    context = await owner.generated.load(case.db.row.tenant_id, case.db.row.id, "engineer")
    assert context is not None
    approval = await owner._python_generated.authorization_request(generated_helpers.request(), context)
    scope = capability_approval_scope(approval)
    assert scope is not None
    case.runs.approvals.add(scope)
    before = case.db.authority(actor="engineer").scope_sha256
    run = case.executor.run

    async def dispatch(invocation: Any) -> Any:
        state = case.db.row.routing_decision["generated_project_preparation"]
        assert state["phase"] == "python_ready"
        assert state["python_input_supply"]["phase"] == "supplied"
        assert case.index().authority.scope_sha256 != before
        assert case.index().authority.scope_sha256 == case.db.authority(actor="engineer").scope_sha256
        assert case.index().execution_id == invocation.execution_id
        return await run(invocation)

    monkeypatch.setattr(case.executor, "run", dispatch)
    with case.bind():
        await owner.verify(**case.arguments(actor="engineer"))
    assert case.index().status == "passed" and case.index().authority.actor == "engineer"
    assert len(case.supply.supplies) == 1 and len(case.executor.calls) == 1
