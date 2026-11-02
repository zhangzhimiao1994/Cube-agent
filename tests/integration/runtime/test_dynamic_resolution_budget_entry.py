"""Local admission SQL delivery plus real generated prepare and budget entry.

This is not PostgreSQL, Root IPC, model or product acceptance evidence.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Unpack
from uuid import uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectError,
    GeneratedProjectService,
    PythonMetadataPreparationApprovalRequired,
)
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.review_repository import review_config_sha256
from agent_hub.db.models import RunAdmissionPoolRow, RunRow
from agent_hub.models.physical_budget import _bind_run_budget, _run_budget, _RunBudgetContext
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    PoolSnapshot,
    RunAdmissionRepository,
    _ExecutionArguments,
    _WorkerLease,
)
from agent_hub.runs.authorization import _capture_authenticated_input, _persist_authenticated_input
from agent_hub.runtime.contracts import TaskContext
from agent_hub.skills.sandbox import python_resolution_worker
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionRequest, ResolutionStatus
from tests.integration.runtime.dynamic_physical_typed_fixture import (
    PreparationStorage,
    context_route,
    present,
)
from tests.unit.capabilities.dynamic_resolution_typed_fixture import FlowRepository
from tests.unit.capabilities.test_dynamic_python_resolution_flow import generated_dynamic_case

pytestmark = pytest.mark.asyncio


class ObservedAdmission(RunAdmissionRepository):
    """Observe the actual current fence without replacing its SQL or contract."""

    def __init__(self, storage: PreparationStorage, order: list[str]) -> None:
        super().__init__(storage)
        self.order = order
        self.current_tasks: list[object] = []
        self.refusals: list[AdmissionConflict] = []

    async def require_current(self, **arguments: Unpack[_ExecutionArguments]) -> None:
        self.current_tasks.append(asyncio.current_task())
        self.order.append("budget.current.before")
        try:
            await super().require_current(**arguments)
        except AdmissionConflict as error:
            self.refusals.append(error)
            raise
        self.order.append("budget.current.after")


@dataclass
class BudgetEntry:
    service: GeneratedProjectService
    repository: FlowRepository
    context: TaskContext
    storage: PreparationStorage
    admission: ObservedAdmission
    pool: PoolSnapshot
    budget: _RunBudgetContext
    order: list[str]
    root_calls: list[str]


async def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BudgetEntry:
    service, repository, context, _ = generated_dynamic_case(tmp_path, monkeypatch)
    original = repository.row
    route = {
        **present(original.routing_decision),
        "runtime_timeout_seconds": 120,
        "runtime_timeout_absolute_seconds": 120,
        "runtime_plan_timeout_seconds": 120,
        "runtime_token_absolute_tokens": 100,
        "runtime_plan_token_budget": 100,
    }
    repository.row = replace(original, routing_decision=route)
    context = context.model_copy(update={"routing_decision": context_route(route)})
    storage = PreparationStorage()
    storage.now = original.created_at
    row = RunRow(
        id=original.id,
        tenant_id=original.tenant_id,
        actor_id=original.actor_id,
        actor_role=present(original.actor_role).value,
        request=original.request,
        mode=present(original.mode).value,
        status=original.status.value,
        version=original.version,
        created_at=original.created_at,
        routing_decision=route,
        worker_id=original.worker_id,
        worker_lease_token=original.worker_lease_token,
        worker_lease_expires_at=original.worker_lease_expires_at,
    )
    principal = AuthenticatedPrincipal(
        present(original.actor_id), original.tenant_id, present(original.actor_role)
    )
    evidence = _capture_authenticated_input(principal, original.request, {"mode": "direct"})
    async with storage() as session, session.begin():
        session.add(row)
        await session.flush()
        await _persist_authenticated_input(session, row, evidence)

    config = ApprovalReviewConfig(
        engine="rules_then_model", reviewer_logical_model="reviewer", max_cost_usd_per_run="10.00"
    )
    order: list[str] = []
    admission = ObservedAdmission(storage, order)
    lease = _WorkerLease(present(original.worker_id), present(original.worker_lease_token))
    pool = await admission.open_pool(
        tenant_id=original.tenant_id,
        run_id=original.id,
        authorization_turn_id=evidence.turn_id,
        lease=lease,
        config_sha256=review_config_sha256(config),
        original_token_grant=100,
        original_deadline=original.created_at + timedelta(seconds=120),
        review_token_limit=100,
        review_count_limit=8,
        review_cost_limit=Decimal(10),
    )
    budget = _RunBudgetContext(
        repository=admission,
        pool=pool,
        evidence=evidence,
        lease=lease,
        config=config,
        deadline=time.monotonic() + 120,
    )
    root_calls: list[str] = []

    async def root_inspect(request: ResolutionRequest) -> ResolutionStatus:
        assert _run_budget.get() is budget
        budget.check()
        assert admission.current_tasks
        assert all(task is admission.current_tasks[0] for task in admission.current_tasks)
        # The SDK runs immutable IPC in a child; current fences remain in its caller.
        assert asyncio.current_task() is not admission.current_tasks[0]
        assert request.action == "inspect_python_resolution"
        assert request.claim.lease_token == context.execution_token
        assert request.claim.worker_id == lease.worker_id
        assert request.claim.original_deadline == pool.original_deadline
        journal = present(service._resolution_journal)
        assert not await journal.read_artifacts(
            original.tenant_id,
            original.id,
            artifact_type="python_resolution_checkpoint",
            producer="python_resolution",
        )
        root_calls.append(request.action)
        order.append("root.inspect.absent")
        return ResolutionStatus(state="absent", key=request.key)

    monkeypatch.setattr(python_resolution_worker, "request_resolution", root_inspect)
    return BudgetEntry(
        service, repository, context, storage, admission, pool, budget, order, root_calls
    )


async def test_generated_prepare_checks_bound_original_pool_before_first_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = await configured(tmp_path, monkeypatch)
    context = case.context
    reads_before = case.storage.authorization_reads
    assert _run_budget.get() is None
    with (
        bind_execution_authority(
            context, worker_id=present(case.repository.row.worker_id), actor="engineer"
        ),
        _bind_run_budget(case.budget),
    ):
        child = asyncio.create_task(
            case.service.prepare(
                context.tenant_id, context.run_id, "engineer", user_id=present(context.actor_id)
            )
        )
        with pytest.raises(PythonMetadataPreparationApprovalRequired) as pending:
            await child
        assert _run_budget.get() is case.budget
        case.budget.check()
        assert pending.value.approval_request.arguments["purpose"] == "python_dependency_resolution"
        assert case.root_calls == ["inspect_python_resolution"]
        assert case.order[:2] == ["budget.current.before", "budget.current.after"]
        assert case.order.index("root.inspect.absent") >= 4
        assert len(case.admission.current_tasks) >= 3
        assert all(task is child for task in case.admission.current_tasks)
        assert case.storage.authorization_reads - reads_before == len(case.admission.current_tasks)
        assert not case.admission.refusals
        assert await case.admission.get_pool(context.tenant_id, context.run_id) == case.pool
        checkpoints = await present(case.service._resolution_journal).read_artifacts(
            context.tenant_id,
            context.run_id,
            artifact_type="python_resolution_checkpoint",
            producer="python_resolution",
        )
        assert len(checkpoints) == 1
        assert "generated_project_preparation" not in present(case.repository.row.routing_decision)
        assert case.repository.updates == 0
    assert _run_budget.get() is None


@pytest.mark.parametrize("change", ["closed_pool", "replaced_lease"])
async def test_first_claim_rejects_changed_original_pool_before_root_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    case = await configured(tmp_path, monkeypatch)
    context = case.context
    async with case.storage() as session, session.begin():
        if change == "closed_pool":
            pool = present(await session.get(RunAdmissionPoolRow, context.run_id))
            pool.closed = True
        else:
            assert change == "replaced_lease"
            row = present(await session.get(RunRow, context.run_id))
            row.worker_lease_token = uuid4()
    with (
        bind_execution_authority(
            context, worker_id=present(case.repository.row.worker_id), actor="engineer"
        ),
        _bind_run_budget(case.budget),
    ):
        child = asyncio.create_task(
            case.service.prepare(
                context.tenant_id, context.run_id, "engineer", user_id=present(context.actor_id)
            )
        )
        with pytest.raises(GeneratedProjectError, match="generated_acceptance_unavailable_or_changed"):
            await child
        assert case.root_calls == []
        assert case.order == ["budget.current.before"]
        assert case.admission.current_tasks == [child]
        assert len(case.admission.refusals) == 1
        assert str(case.admission.refusals[0]) == "admission state conflict"
        assert not await present(case.service._resolution_journal).read_artifacts(
            context.tenant_id,
            context.run_id,
            artifact_type="python_resolution_checkpoint",
            producer="python_resolution",
        )
        assert "generated_project_preparation" not in present(case.repository.row.routing_decision)
    assert _run_budget.get() is None
