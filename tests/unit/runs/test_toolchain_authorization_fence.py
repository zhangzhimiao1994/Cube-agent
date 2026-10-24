"""Deterministic row-lock interleavings; these doubles do not prove PostgreSQL locks."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import Any, Self, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewOutcome,
    CapabilityGateway,
    CapabilityStatus,
)
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.toolchain_preparation import toolchain_supply_binding
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.db.models import RunApprovalRow
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414
from tests.unit.capabilities.test_toolchain_preparation import state_for
from tests.unit.harness.test_toolchain_supply_gateway import NoRuntime, envelope
from tests.unit.runs.test_toolchain_supply_repository import SupplyDB, object_dict


class ApprovalDB(SupplyDB):
    def __init__(self, case: Any) -> None:
        super().__init__(case)
        self.route['toolchain_supply'] = state_for(case)
        self.before_lock: Any = None
        self.reads = 0
        self.locks: list[str] = []
        self.repository = RunRepository(cast(Any, lambda: Session(self)))

    def takeover(self) -> None:
        self.row.worker_id, self.row.worker_lease_token = 'worker-B', uuid4()


class Session:
    def __init__(self, db: ApprovalDB) -> None:
        self.db, self.active, self.locked = db, False, False
        self.row = deepcopy(db.row)
        self.approvals = deepcopy(db.approvals)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, error: object, *_: object) -> None:
        if self.active and error is None:
            self.db.row = self.row
            self.db.approvals = self.approvals

    def begin(self) -> Transaction:
        return Transaction(self)

    async def scalar(self, statement: Any) -> Any:
        table = next(iter(statement.get_final_froms())).name
        locked = statement._for_update_arg is not None
        values = set(statement.compile().params.values())
        if table == 'agent_hub_users':
            assert self.active and self.locked and locked
            self.db.locks.append('user')
            user = self.db.user
            return user if not self.db.user_missing and {user.id, user.tenant_id} == values else None
        assert table == 'agent_hub_runs'
        if locked:
            assert self.active
            if not self.locked:
                if self.db.before_lock is not None:
                    hook, self.db.before_lock = self.db.before_lock, None
                    hook(self.db)
                self.row = deepcopy(self.db.row)
                self.approvals = deepcopy(self.db.approvals)
                self.locked = True
            self.db.locks.append('run')
            return self.row if {self.row.id, self.row.tenant_id} == values else None
        self.db.reads += 1
        return self.db.row if {self.db.row.id, self.db.row.tenant_id} == values else None

    async def scalars(self, statement: Any) -> Any:
        values = statement.compile().params.values()
        items = [row for row in self.db.approvals if row.tenant_id in values and row.run_id in values
                 and ('approved' not in values or row.status == 'approved')]
        return type('Rows', (), {'all': lambda _: items})()

    async def execute(self, statement: Any) -> Any:
        assert self.active and self.locked and statement.is_insert
        params = statement.compile().params
        row = RunApprovalRow(**{key: params[key] for key in (
            'id', 'tenant_id', 'run_id', 'approval_id', 'action', 'status', 'payload')})
        self.approvals = [item for item in self.approvals if item.approval_id != row.approval_id] + [row]
        return type('Result', (), {'scalar_one_or_none': lambda _: row.id})()

    async def flush(self) -> None:
        assert self.active


class Transaction:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        self.session.active = True

    async def __aexit__(self, error: object, *_: object) -> None:
        if error is not None:
            self.session.active = False


class Reviewer:
    def __init__(self, outcome: ApprovalReviewOutcome) -> None:
        self.outcome = outcome

    async def review(self, request: Any) -> ApprovalReviewDecision:
        return ApprovalReviewDecision(self.outcome, 'test-reviewer', 'controlled SDK review')


def policy(db: ApprovalDB, outcome: ApprovalReviewOutcome | None = None) -> DefaultRuntimeCapabilityPolicyGateway:
    return DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), db.repository,
        approval_reviewer=None if outcome is None else Reviewer(outcome))


async def admit(db: ApprovalDB, authority: ToolchainSupplyAuthority | None,
                gateway: DefaultRuntimeCapabilityPolicyGateway | None = None, *,
                key: str = 'sdk-admission') -> Any:
    harness = HarnessToolGateway(NoRuntime(), policy_gateway=policy(db) if gateway is None else gateway)
    assert db.row.actor_id is not None
    return await harness.authorize_project_toolchain(db.row.tenant_id, replace(envelope(), idempotency_key=key),
        user_id=db.row.actor_id, role=Role.OPERATOR, plan=db.case.plan, original_reference=db.case.ref,
        authority=authority)


def snapshot(db: ApprovalDB) -> tuple[object, ...]:
    row = db.row
    return (row.status, row.worker_id, row.worker_lease_token, row.worker_lease_expires_at,
            row.version, deepcopy(row.routing_decision),
            [(item.approval_id, item.status, deepcopy(item.payload)) for item in db.approvals])


@pytest.mark.parametrize('outcome', [None, ApprovalReviewOutcome.ALLOW, ApprovalReviewOutcome.DENY])
async def test_takeover_after_preflight_before_approval_lock_preserves_B(case: Any, outcome: Any) -> None:
    db = ApprovalDB(case)
    await db.read()  # A has already completed state_IO.
    db.reads = 0
    expected: list[tuple[object, ...]] = []

    def takeover(live: ApprovalDB) -> None:
        assert live.reads > 0, 'interleaving must follow DefaultPolicy preflight reads'
        live.takeover()
        expected.append(snapshot(live))

    db.before_lock = takeover
    gateway = policy(db, outcome)
    result = await admit(db, db.bound, gateway)
    assert result.status == 'failed'
    assert len(expected) == 1 and snapshot(db) == expected[0]
    assert db.row.status == 'running' and db.row.worker_id == 'worker-B' and not db.approvals
    assert not await gateway._approvals._store.list_pending()


@pytest.mark.parametrize('defect', ['disabled', 'role', 'missing'])
async def test_current_user_change_at_lock_cannot_create_approval(case: Any, defect: str) -> None:
    db = ApprovalDB(case)
    before = snapshot(db)

    def changed(live: ApprovalDB) -> None:
        if defect == 'disabled':
            live.user.disabled = True
        elif defect == 'role':
            live.user.role = 'admin'
        else:
            live.user_missing = True

    db.before_lock = changed
    result = await admit(db, db.bound)
    assert result.status == 'failed' and snapshot(db) == before


async def test_public_default_invoke_cannot_infer_private_authority(case: Any) -> None:
    db = ApprovalDB(case)
    before = snapshot(db)
    result = await policy(db).invoke(toolchain_supply_binding(db.row, planned=True), role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED and snapshot(db) == before


async def test_repository_go_begin_without_authority_fails_closed(case: Any) -> None:
    db = ApprovalDB(case)
    request = toolchain_supply_binding(db.row, planned=True)
    before = snapshot(db)
    with pytest.raises(RunConflict, match='toolchain'):
        await db.repository.begin_capability_approval(db.row.tenant_id, db.row.id,
            actor_id=db.row.actor_id, approval_id='unbound', approval_fingerprint='unbound',
            approval_scope=capability_approval_scope(request))
    assert snapshot(db) == before


def test_authority_repr_does_not_disclose_lease_token(case: Any) -> None:
    db = ApprovalDB(case)
    assert str(db.bound.lease_token) not in repr(db.bound)


async def test_valid_authority_waits_and_persists_exact_binding(case: Any) -> None:
    db = ApprovalDB(case)
    result = await admit(db, db.bound)
    assert result.status == 'failed' and result.payload.get('approval_id') is not None
    assert db.row.status == 'waiting_approval' and db.row.worker_lease_token is None
    assert object_dict(db.approvals[0].payload['toolchain_input_binding'])['plan_sha256'] == case.plan.sha256
    assert db.locks == ['run', 'user']


@pytest.mark.parametrize('outcome', [ApprovalReviewOutcome.ALLOW, ApprovalReviewOutcome.DENY])
async def test_current_authority_auto_review_is_bound_and_keeps_lease(case: Any, outcome: Any) -> None:
    db = ApprovalDB(case)
    before = snapshot(db)[:-1]
    result = await admit(db, db.bound, policy(db, outcome))
    assert result.status == ('succeeded' if outcome is ApprovalReviewOutcome.ALLOW else 'failed')
    assert snapshot(db)[:-1] == before and db.locks == ['run', 'user']
    approval = db.approvals[0]
    expected = toolchain_supply_binding(db.row, planned=True)
    assert approval.payload['toolchain_input_binding'] == expected.arguments
    assert approval.payload['approval_scope'] == capability_approval_scope(expected)
    assert approval.status == ('approved' if outcome is ApprovalReviewOutcome.ALLOW else 'denied')
    assert str(db.bound.lease_token) not in str(approval.payload)
    if outcome is ApprovalReviewOutcome.ALLOW:
        retried = await admit(db, db.bound, key='retry-with-same-binding')
        assert retried.status == 'succeeded' and len(db.approvals) == 1


async def test_shared_policy_concurrent_calls_keep_their_own_authority(case: Any) -> None:
    db = ApprovalDB(case)
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedReviewer:
        async def review(self, request: Any) -> ApprovalReviewDecision:
            if request.idempotency_key == 'from-A':
                entered.set()
                await release.wait()
            return ApprovalReviewDecision(ApprovalReviewOutcome.ALLOW, 'test-reviewer', 'SDK review')

    approvals = ApprovalService(InMemoryApprovalStore())
    shared = DefaultRuntimeCapabilityPolicyGateway(approvals, db.repository, approval_reviewer=PausedReviewer())
    original = db.bound
    task = asyncio.create_task(admit(db, original, shared, key='from-A'))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        db.takeover()
        current = ToolchainSupplyAuthority.from_context(db.context(), worker_id='worker-B')
        current_result = await admit(db, current, shared, key='from-B')
        assert current_result.status == 'succeeded'
        after_B = snapshot(db)
    finally:
        release.set()
        stale_result = await asyncio.wait_for(task, timeout=5)
    assert stale_result.status == 'failed' and snapshot(db) == after_B
    assert len(db.approvals) == 1 and db.row.worker_lease_token == current.lease_token
    assert approvals._run_repository is db.repository
    assert not await approvals._store.list_pending()


async def test_missing_authority_fails_closed_at_both_trusted_entries(case: Any) -> None:
    db = ApprovalDB(case)
    before = snapshot(db)
    assert (await admit(db, None)).status == 'failed'
    request = toolchain_supply_binding(db.row, planned=True)
    result = await policy(db).authorize_project_toolchain(request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED and snapshot(db) == before


async def test_repository_go_review_without_authority_fails_closed(case: Any) -> None:
    db = ApprovalDB(case)
    request = toolchain_supply_binding(db.row, planned=True)
    before = snapshot(db)
    with pytest.raises(RunConflict, match='toolchain'):
        await db.repository.record_capability_approval_review(db.row.tenant_id, db.row.id,
            approval_id='unbound-review', approval_fingerprint=fingerprint_capability_request(request),
            approval_scope=capability_approval_scope(request), reviewer='test', reason='review', status='approved')
    assert snapshot(db) == before


async def test_generic_fingerprint_audit_cannot_be_reused_as_sdk_grant(case: Any) -> None:
    db = ApprovalDB(case)
    request = toolchain_supply_binding(db.row, planned=True).model_copy(update={'idempotency_key': 'sdk-admission'})
    await db.repository.record_capability_approval_review(db.row.tenant_id, db.row.id,
        approval_id='generic-review', approval_fingerprint=fingerprint_capability_request(request),
        reviewer='test', reason='generic review', status='approved')
    result = await admit(db, db.bound)
    assert result.status == 'failed' and result.payload.get('approval_id') is not None
    assert db.row.status == 'waiting_approval' and len(db.approvals) == 2


@pytest.mark.parametrize('effect', [PolicyEffect.ALLOW, PolicyEffect.REQUIRE_APPROVAL])
async def test_lower_gateway_without_private_context_cannot_review_go(case: Any, effect: PolicyEffect) -> None:
    db = ApprovalDB(case)
    request = toolchain_supply_binding(db.row, planned=True)
    rules = (CapabilityRule(request.tenant_id, Role.OPERATOR, 'main_agent', 'command', 'run',
                            request.resource, effect),)
    gateway = CapabilityGateway(CapabilityPolicy(rules), ApprovalService(InMemoryApprovalStore()),
        db.repository, approval_reviewer=Reviewer(ApprovalReviewOutcome.ALLOW))
    before = snapshot(db)
    result = await gateway.invoke(request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.DENIED and snapshot(db) == before
