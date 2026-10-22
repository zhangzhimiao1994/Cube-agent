"""Private Go policy entry and row-lock interleavings, without a native executor."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalRejected,
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewOutcome,
    CapabilityGateway,
    CapabilityStatus,
)
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.runs.go_project_admission import go_recipe_binding
from agent_hub.runs.repository import RunConflict
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414
from tests.unit.runs.test_go_project_admission import (
    GoDB,
    bind,
    seed_grant,
    snapshot,
)
from tests.unit.runs.test_go_project_admission import api as api  # noqa: PLC0414
from tests.unit.runs.test_go_project_admission import db as db  # noqa: PLC0414
from tests.unit.runs.test_toolchain_authorization_fence import Reviewer


def policy(db: GoDB, outcome: Any = None) -> DefaultRuntimeCapabilityPolicyGateway:
    return DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), db.repository,
        approval_reviewer=None if outcome is None else Reviewer(outcome))


async def admit(db: GoDB, gateway: Any, authority: Any, **patch: Any) -> Any:
    request = go_recipe_binding(db.row, idempotency_key=patch.pop('key', 'original-Go'))
    return await gateway.authorize_project_go_recipe(request.model_copy(update=patch),
                                                     role=Role.OPERATOR, authority=authority)


@pytest.mark.parametrize('outcome', [None, ApprovalReviewOutcome.ALLOW, ApprovalReviewOutcome.DENY])
async def test_preflight_then_takeover_before_approval_row_lock(db: GoDB, outcome: Any) -> None:
    await bind(db)
    db.reads = 0
    expected: list[Any] = []

    def takeover(live: GoDB) -> None:
        assert live.reads > 0
        live.takeover()
        expected.append(snapshot(live))

    db.before_lock = takeover
    gateway = policy(db, outcome)
    result = await admit(db, gateway, db.current_authority())
    assert result.status is CapabilityStatus.DENIED
    assert len(expected) == 1 and snapshot(db) == expected[0]
    assert db.row.status == 'running' and db.row.worker_id == 'worker-B' and not db.approvals
    assert not await gateway._approvals._store.list_pending()


@pytest.mark.parametrize('operation', ['waiting', 'review'])
async def test_repository_go_scope_requires_private_authority(db: GoDB, operation: str) -> None:
    await bind(db)
    before = snapshot(db)
    options = {'approval_id': 'generic', 'approval_fingerprint': 'generic',
               'approval_scope': capability_approval_scope(go_recipe_binding(db.row))}
    with pytest.raises(RunConflict):
        if operation == 'waiting':
            await db.repository.begin_capability_approval(db.row.tenant_id, db.row.id,
                                                         actor_id=db.row.actor_id, **options)
        else:
            await db.repository.record_capability_approval_review(db.row.tenant_id, db.row.id,
                reviewer='controlled', reason='Go review', status='approved', **options)
    assert snapshot(db) == before


@pytest.mark.parametrize('effect', [PolicyEffect.ALLOW, PolicyEffect.REQUIRE_APPROVAL])
async def test_public_gateways_cannot_infer_authority_even_with_grant(db: GoDB, api: Any, effect: Any) -> None:
    await seed_grant(db, api)
    request = go_recipe_binding(db.row)
    before = snapshot(db)
    assert (await policy(db).invoke(request, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    core = CapabilityGateway(CapabilityPolicy((CapabilityRule(request.tenant_id, Role.OPERATOR,
        'main_agent', 'command', 'run', request.resource, effect),)), ApprovalService(InMemoryApprovalStore()),
        db.repository, approval_reviewer=Reviewer(ApprovalReviewOutcome.ALLOW))
    assert (await core.invoke(request, role=Role.OPERATOR)).status is CapabilityStatus.DENIED
    assert snapshot(db) == before


async def test_missing_authority_or_model_arguments_fail_closed(db: GoDB) -> None:
    await bind(db)
    before = snapshot(db)
    gateway = policy(db, ApprovalReviewOutcome.ALLOW)
    assert (await admit(db, gateway, None)).status is CapabilityStatus.DENIED
    arguments = {**go_recipe_binding(db.row).arguments, 'approved': True}
    assert (await admit(db, gateway, db.current_authority(), arguments=arguments)).status is CapabilityStatus.DENIED
    assert snapshot(db) == before


@pytest.mark.parametrize('outcome', [None, ApprovalReviewOutcome.ALLOW, ApprovalReviewOutcome.DENY])
async def test_valid_authority_waits_or_persists_separate_review(db: GoDB, outcome: Any) -> None:
    await bind(db)
    authority = db.current_authority()
    gateway = policy(db, outcome)
    result = await admit(db, gateway, authority)
    assert result.status is (CapabilityStatus.ALLOWED if outcome is ApprovalReviewOutcome.ALLOW
                             else CapabilityStatus.WAITING_APPROVAL if outcome is None else CapabilityStatus.DENIED)
    assert len(db.approvals) == 1
    assert db.approvals[0].payload['go_recipe_binding'] == db.row.routing_decision['go_project_recipe']
    assert 'toolchain_input_binding' not in db.approvals[0].payload
    assert str(authority.lease_token) not in str(db.approvals[0].payload)
    if outcome is None:
        assert db.row.status == 'waiting_approval' and db.row.worker_lease_token is None
        assert len(await gateway._approvals._store.list_pending()) == 1
    else:
        assert db.row.status == 'running' and db.row.worker_lease_token == authority.lease_token
    if outcome is ApprovalReviewOutcome.ALLOW:
        assert (await admit(db, gateway, authority, key='retry')).status is CapabilityStatus.ALLOWED
        assert len(db.approvals) == 1


@pytest.mark.parametrize('outcome', [None, ApprovalReviewOutcome.ALLOW])
async def test_db_failure_cannot_publish_memory_pending_or_grant(db: GoDB, outcome: Any) -> None:
    await bind(db)
    db.fail_write = True
    before = snapshot(db)
    gateway = policy(db, outcome)
    assert (await admit(db, gateway, db.current_authority())).status is CapabilityStatus.DENIED
    assert snapshot(db) == before and not await gateway._approvals._store.list_pending()


async def test_concurrent_requests_do_not_share_mutable_authority(db: GoDB) -> None:
    await bind(db)
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedReviewer:
        async def review(self, request: Any) -> ApprovalReviewDecision:
            if request.idempotency_key == 'from-A':
                entered.set()
                await release.wait()
            return ApprovalReviewDecision(ApprovalReviewOutcome.ALLOW, 'controlled', 'Go recipe review')

    gateway = policy(db)
    gateway._approval_reviewer = PausedReviewer()
    task = asyncio.create_task(admit(db, gateway, db.current_authority(), key='from-A'))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        db.takeover()
        current = db.current_authority()
        assert (await admit(db, gateway, current, key='from-B')).status is CapabilityStatus.ALLOWED
        after = snapshot(db)
    finally:
        release.set()
        result = await asyncio.wait_for(task, timeout=3)
    assert result.status is CapabilityStatus.DENIED and snapshot(db) == after
    assert len(db.approvals) == 1 and db.row.worker_lease_token == current.lease_token


@pytest.mark.parametrize('defect', ['disabled', 'role', 'capture', 'supply', 'marker'])
async def test_new_revalidation_after_preflight(db: GoDB, defect: str) -> None:
    await bind(db)
    before: list[Any] = []

    def change(live: GoDB) -> None:
        if defect == 'disabled':
            live.user.disabled = True
        elif defect == 'role':
            live.user.role = 'admin'
        elif defect == 'capture':
            live.row.routing_decision['project_input_preparation']['schema_version'] = True
        elif defect == 'supply':
            live.row.routing_decision['toolchain_supply']['receipt']['environment']['tree_sha256'] = '0' * 64
        else:
            live.row.routing_decision['go_project_recipe']['file_limit'] += 1
        before.append(snapshot(live))

    db.before_lock = change
    gateway = policy(db, ApprovalReviewOutcome.ALLOW)
    result = await admit(db, gateway, db.current_authority())
    # The recipe marker's limits must also be protected by the per-request context.
    assert result.status is CapabilityStatus.DENIED
    assert len(before) == 1 and snapshot(db) == before[0]


@pytest.mark.parametrize('effect', [PolicyEffect.ALLOW, PolicyEffect.REQUIRE_APPROVAL])
async def test_lower_gateway_allow_still_requires_independent_go_grant(db: GoDB, effect: Any) -> None:
    await bind(db)
    request = go_recipe_binding(db.row)
    core = CapabilityGateway(CapabilityPolicy((CapabilityRule(request.tenant_id, Role.OPERATOR,
        'main_agent', 'command', 'run', request.resource, effect),)), ApprovalService(InMemoryApprovalStore()),
        db.repository, go_recipe_authority=db.current_authority())
    result = await core.invoke(request, role=Role.OPERATOR)
    assert result.status is CapabilityStatus.WAITING_APPROVAL
    assert db.approvals[0].status == 'pending'
    assert 'go_recipe_binding' in db.approvals[0].payload


@pytest.mark.parametrize('branch', ['grant', 'review', 'waiting'])
async def test_expiry_while_awaiting_approval_lock_rolls_back(db: GoDB, api: Any, branch: str) -> None:
    from datetime import UTC, datetime

    if branch == 'grant':
        await seed_grant(db, api)
    else:
        await bind(db)
    before = snapshot(db)
    authority = db.current_authority()

    def expire(session: Any) -> None:
        session.row.worker_lease_expires_at = datetime.now(UTC)

    if branch == 'grant':
        db.during_approval_lock = expire
    else:
        method = '_upsert_capability_approval' if branch == 'waiting' else '_upsert_capability_approval_review'
        original_flush = getattr(db.repository, method)

        async def delayed(*args: Any, **kwargs: Any) -> Any:
            result = await original_flush(*args, **kwargs)
            expire(args[0])
            return result

        setattr(db.repository, method, delayed)
    result = await admit(db, policy(db, None if branch == 'waiting' else ApprovalReviewOutcome.ALLOW), authority)
    assert result.status is CapabilityStatus.DENIED and snapshot(db) == before
    assert db.locks[-3:] == ['run', 'user', 'approval' if branch == 'grant' else 'approval-write']


@pytest.mark.parametrize('outcome', [ApprovalReviewOutcome.ALLOW, ApprovalReviewOutcome.REQUIRE_USER_APPROVAL])
async def test_marker_changes_after_grant_read_before_approval_write(db: GoDB, outcome: Any) -> None:
    await bind(db)
    expected: list[Any] = []

    class MarkerChangingReviewer:
        async def review(self, request: Any) -> ApprovalReviewDecision:
            assert db.locks[-3:] == ['run', 'user', 'approval']
            db.row.routing_decision['go_project_recipe']['file_limit'] += 1
            expected.append(snapshot(db))
            return ApprovalReviewDecision(outcome, 'controlled', 'Go recipe review')

    gateway = policy(db)
    gateway._approval_reviewer = MarkerChangingReviewer()
    result = await admit(db, gateway, db.current_authority())
    assert result.status is CapabilityStatus.DENIED and snapshot(db) == expected[0]
    assert not db.approvals and not await gateway._approvals._store.list_pending()


@pytest.mark.parametrize('change', ['disabled', 'role'])
@pytest.mark.parametrize('action', ['reject', 'expire'])
async def test_service_can_reject_or_expire_after_user_change(db: GoDB, change: str, action: str) -> None:
    from datetime import timedelta

    await bind(db)
    gateway = policy(db)
    waiting = await admit(db, gateway, db.current_authority())
    assert waiting.status is CapabilityStatus.WAITING_APPROVAL
    approvals = gateway._approvals
    record = await approvals.get(waiting.approval_id)
    assert record is not None
    db.user.disabled = change == 'disabled'
    if change == 'role':
        db.user.role = 'admin'
    if action == 'reject':
        with pytest.raises(ApprovalRejected, match='approval rejected'):
            await approvals.reject(record.id, actor_id=record.user_id)
    else:
        approvals._now = lambda: record.expires_at + timedelta(seconds=1)
        assert await approvals.expire_due() == 1
    resolved = await approvals.get(record.id)
    assert resolved is not None and resolved.status == ('rejected' if action == 'reject' else 'expired')
    assert db.row.status == 'cancelled' and db.approvals[0].status == 'rejected'
    assert not await approvals._store.list_pending()
    assert 'go_project_execution' not in db.row.routing_decision
