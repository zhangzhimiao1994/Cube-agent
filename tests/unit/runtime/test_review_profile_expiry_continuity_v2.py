"""v2 original-pool continuity and revocation; v1 and auto-review stay strict.

Fresh admission uses the real submission_profile, never a fabricated v2 record.
Qualification expiry cannot renew a task, erase UNKNOWN or become model consent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Unpack
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities import execution_authority
from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.review_context import _ConcreteReviewCall, _Fence
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.config.repository import ConfigRevision
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionEventRow,
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunReviewEventRow,
    RunReviewIntentRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.tool_gateway import _capability_request
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models import physical_budget
from agent_hub.models.physical_admission import _PhysicalAdmissionStopped
from agent_hub.models.physical_budget import _review_admission, _run_budget
from agent_hub.models.run_admission_repository import (
    AdmissionConflict,
    AttemptSnapshot,
    DeploymentIdentity,
    RunAdmissionRepository,
    _ReserveArguments,
)
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime import review_assembly as assembly
from agent_hub.runtime.contracts import RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.defaults import _gateway_for_config
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.harness.review_typed_fixture import (
    FixedPolicyRules,
    StreamRuntimeStub,
    logical_request,
    mutable,
    present,
)
from tests.unit.runtime.test_review_assembly import AssemblyFixture, evidence_document, fixture


@dataclass
class Admission:
    f: AssemblyFixture
    path: Path
    body: bytes
    expired_at: datetime
    profile: object

    def expire_review_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instant = self.expired_at

        class ExpiredEvidenceClock(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> ExpiredEvidenceClock:
                return cls.fromtimestamp(instant.timestamp(), tz=tz)

        # Only the qualification clock advances. The original pool/deadline,
        # real DAO lease clock and monotonic task clock are neither reset nor extended.
        monkeypatch.setattr(assembly, 'datetime', ExpiredEvidenceClock)

    def assert_immutable(self) -> None:
        f = self.f
        assert self.path.read_bytes() == self.body
        assert f.s.task.routing_decision['approval_review_profile'] == self.profile
        assert present(f.db.run.routing_decision)['approval_review_profile'] == self.profile
        pools = [row for row in f.db.rows if isinstance(row, RunAdmissionPoolRow)]
        assert len(pools) == 1
        assert pools[0].original_deadline == f.db.run.created_at + timedelta(seconds=3600)
        assert pools[0].original_deadline > self.expired_at
        assert pools[0].original_token_grant == 40000 and not pools[0].closed
        assert f.db.run.worker_id == f.s.budget.lease.worker_id
        assert f.db.run.worker_lease_token == f.s.budget.lease.token


async def fresh_admission(tmp_path: Path, *, legacy_v1: bool = False,
                          mode: TaskMode = TaskMode.DIRECT) -> Admission:
    f = await fixture(tmp_path, mode=mode)
    repository = RunAdmissionRepository(f.db)
    assert await repository.get_pool(f.s.task.tenant_id, f.s.task.run_id) is None
    document = evidence_document(f)
    expiry = datetime.now(UTC) + timedelta(seconds=30)
    document['valid_until'] = expiry.isoformat()
    body = json.dumps(document).encode()
    path = tmp_path / 'review-evidence.json'
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    if legacy_v1:
        f.broker._evidence_source = None
        f.broker._evidence_loader = lambda: assembly.load_server_review_evidence(path, expected_sha256=digest)
    else:
        f.broker._evidence_source = assembly.ServerReviewEvidenceSource(path, expected_sha256=digest)
        f.broker._evidence_loader = None
    issued = await f.broker.submission_profile(f.s.task.tenant_id, timeout_seconds=3600)
    if legacy_v1:
        # The legacy tuple loader must issue only its real historical v1 shape.
        assert mutable(issued['approval_review_profile'])['version'] == 1
    route = {**f.s.task.routing_decision, **issued}
    f.s.task = f.s.task.model_copy(update={'routing_decision': route, 'timeout_seconds': 3600})
    f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    f.db.run.routing_decision = route
    assert await repository.get_pool(f.s.task.tenant_id, f.s.task.run_id) is None
    assert (await f.broker.readiness(f.s.task.tenant_id)).status == 'ready'
    return Admission(f, path, body, expiry + timedelta(seconds=1),
                     deepcopy(f.s.task.routing_decision['approval_review_profile']))


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_new_admission_continues_primary_billing_after_only_review_evidence_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    admission = await fresh_admission(tmp_path, mode=mode)
    f = admission.f

    class Coding(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budget = present(_run_budget.get())
            gateway, _, _ = await _gateway_for_config(f.state.platform,
                tenant_id=context.tenant_id, secret_service=f.secrets,
                capacity_factory=f.capacity, transport=f.broker._transport)
            await gateway.complete(logical_request())
            before = present(await RunAdmissionRepository(f.db).get_pool(context.tenant_id, context.run_id))
            assert before.token_charged == 5 and len(f.state.calls) == 1
            deadline, lease = budget.deadline, budget.lease
            admission.expire_review_only(monkeypatch)
            budget.check()  # Original wall/monotonic task deadline is still valid.
            try:
                await gateway.complete(logical_request())
            finally:
                admission.assert_immutable()
                assert budget.deadline == deadline and budget.lease == lease
            after = present(await RunAdmissionRepository(f.db).get_pool(context.tenant_id, context.run_id))
            assert after.original_deadline == before.original_deadline
            assert after.authorization_turn_id == before.authorization_turn_id
            assert after.config_sha256 == before.config_sha256
            assert after.token_charged == 10 and after.tokens_remaining == 39990
            assert after.review_count_charged == 0 and after.review_token_charged == 0
            assert len(f.state.calls) == 2
            assert mutable(admission.profile)['version'] == 2
            yield RunEvent(kind='runtime.checked', sequence=1, run_id=context.run_id, payload={'checked': True})

    async with worker_runtime_events(Coding(), f.s.task, worker_id=f.s.budget.lease.worker_id,
                                     review_broker=f.broker) as events:
        await anext(events)


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_new_admission_reentry_reuses_original_pool_after_only_review_evidence_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    admission = await fresh_admission(tmp_path, mode=mode)
    f = admission.f
    original = present(await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id))
    # Exercise the production reserve/issue/finish UNKNOWN accounting path;
    # do not manufacture a balance by assigning the row's token counter.
    repository = RunAdmissionRepository(f.db)
    attempt_id = uuid4()
    deployment = DeploymentIdentity('openai', 'primary_1', 'fixed')
    await repository.reserve(tenant_id=f.s.task.tenant_id, run_id=f.s.task.run_id,
        authorization_turn_id=original.budget.evidence.turn_id, lease=original.budget.lease,
        config_sha256=original.budget._pool.config_sha256, attempt_id=attempt_id,
        purpose='primary', request_sha256='f' * 64, deployment=deployment,
        token_hold=11, review_cost_hold=None)
    assert await repository.mark_issued(tenant_id=f.s.task.tenant_id, run_id=f.s.task.run_id,
        authorization_turn_id=original.budget.evidence.turn_id, lease=original.budget.lease,
        config_sha256=original.budget._pool.config_sha256, attempt_id=attempt_id,
        physical_transport_id=uuid4())
    await repository.finish(tenant_id=f.s.task.tenant_id, run_id=f.s.task.run_id,
        attempt_id=attempt_id, lease=original.budget.lease, deployment=deployment,
        outcome='unknown', usage=None)
    attempt = next(row for row in f.db.rows if isinstance(row, RunAdmissionAttemptRow)
                   and row.attempt_id == attempt_id)
    assert attempt.state == 'unknown'
    before = present(await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id))
    original.budget.revoke()
    admission.expire_review_only(monkeypatch)
    # Capture one exact clock pair for the NEW local budget construction. This
    # avoids comparing two Windows clock samples with different granularity;
    # the persisted UTC deadline and the DAO's live clock are untouched.
    monotonic_sample, wall_sample = time.monotonic(), datetime.now(UTC)

    class BudgetClock:
        @staticmethod
        def monotonic() -> float:
            return monotonic_sample

    class BudgetWall(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> BudgetWall:
            return cls.fromtimestamp(wall_sample.timestamp(), tz=tz)

    monkeypatch.setattr(physical_budget, 'time', BudgetClock)
    monkeypatch.setattr(physical_budget, 'datetime', BudgetWall)
    try:
        resumed = present(await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id))
    finally:
        admission.assert_immutable()
    assert resumed.budget._pool == before
    assert resumed.budget._pool.token_charged == 11
    assert resumed.budget._pool.tokens_remaining == 39989
    original_bound = monotonic_sample + (before.original_deadline - wall_sample).total_seconds()
    assert monotonic_sample < resumed.budget.deadline <= original_bound
    assert resumed.budget.lease == original.budget.lease
    assert mutable(admission.profile)['version'] == 2
    assert attempt.state == 'unknown' and attempt.token_hold == 11
    assert sum(isinstance(row, RunAdmissionAttemptRow) for row in f.db.rows) == 1
    assert not f.state.calls


async def test_legacy_v1_is_not_salvaged_or_upgraded_after_review_evidence_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = await fresh_admission(tmp_path, legacy_v1=True)
    f = admission.f
    original = present(await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id))
    pool = present(await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id))
    original.budget.revoke()
    admission.expire_review_only(monkeypatch)
    with pytest.raises(assembly.ReviewAssemblyUnavailable, match='evidence_invalid|review_profile_unsupported'):
        await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id)
    admission.assert_immutable()
    assert mutable(admission.profile)['version'] == 1
    assert await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id) == pool
    assert not f.state.calls


async def test_expired_evidence_never_authorizes_an_independent_model_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = await fresh_admission(tmp_path)
    f = admission.f
    f.state.review = True
    stack = assembly.assemble_review_capability_stack(f.broker, build_runtime_capability_stack,
        tenant_id=f.s.task.tenant_id, run_repository=f.s.runs, skill_store_dir=tmp_path / 'skills',
        workspace_root=tmp_path, generated_artifact_dir=tmp_path / 'generated', project_workspace_dir=tmp_path)

    class NeedsReview(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            admission.expire_review_only(monkeypatch)
            result = await stack.harness_tool_gateway.invoke(context.tenant_id, f.s.request,
                user_id=context.actor_id, role=context.actor_role)
            assert result.status in {'failed', 'waiting_approval'}
            assert not f.state.calls  # No provider review request or primary request.
            assert not ProjectWorkspaceStore(tmp_path).list_files(context.tenant_id, 'demo', 'session')
            pool = present(await RunAdmissionRepository(f.db).get_pool(context.tenant_id, context.run_id))
            assert pool.token_charged == 0 and pool.review_count_charged == 0
            admission.assert_immutable()
            yield RunEvent(kind='runtime.checked', sequence=1, run_id=context.run_id, payload={'checked': True})

    async with worker_runtime_events(NeedsReview(), f.s.task, worker_id=f.s.budget.lease.worker_id,
                                     review_broker=f.broker) as events:
        await anext(events)


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('phase', ['secret_wait', 'last_catalog_read'])
async def test_review_expiry_during_last_async_send_never_reaches_provider_or_consumes_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode, phase: str,
) -> None:
    admission = await fresh_admission(tmp_path, mode=mode)
    f = admission.f
    f.state.review = True
    stack = assembly.assemble_review_capability_stack(f.broker, build_runtime_capability_stack,
        tenant_id=f.s.task.tenant_id, run_repository=f.s.runs, skill_store_dir=tmp_path / 'skills',
        workspace_root=tmp_path, generated_artifact_dir=tmp_path / 'generated', project_workspace_dir=tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    paused = False

    async def pause() -> None:
        nonlocal paused
        if not paused:
            paused = True
            assert _review_admission.get() is not None
            assert not f.state.calls
            entered.set()
            await release.wait()

    if phase == 'secret_wait':
        resolve = f.broker._secrets.resolve

        async def held_resolve(tenant_id: UUID, reference: str) -> str:
            if _review_admission.get() is not None:
                await pause()
            return await resolve(tenant_id, reference)

        monkeypatch.setattr(f.broker._secrets, 'resolve', held_resolve)
    else:
        current = f.broker._configs.get_current

        async def held_catalog(tenant_id: UUID) -> ConfigRevision | None:
            value = await current(tenant_id)
            # The real mark_issued DAO has committed. Pause the catalog read
            # preceding _dispatch_check's final synchronous activation fence.
            if _review_admission.get() is not None and any(
                isinstance(row, RunReviewIntentRow) and row.status == 'issued' for row in f.db.rows
            ):
                await pause()
            return value

        monkeypatch.setattr(f.broker._configs, 'get_current', held_catalog)

    outcomes: list[HarnessToolCallResult] = []

    class AwaitingReview(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            outcomes.append(await stack.harness_tool_gateway.invoke(context.tenant_id, f.s.request,
                user_id=context.actor_id, role=context.actor_role))
            yield RunEvent(kind='runtime.checked', sequence=1, run_id=context.run_id, payload={'checked': True})

    async def drive() -> None:
        async with worker_runtime_events(AwaitingReview(), f.s.task,
            worker_id=f.s.budget.lease.worker_id, review_broker=f.broker) as events:
            await anext(events)

    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        admission.expire_review_only(monkeypatch)
        release.set()
        await asyncio.wait_for(task, timeout=10)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(outcomes) == 1 and outcomes[0].status in {'failed', 'waiting_approval'}
    assert not f.state.calls
    assert not ProjectWorkspaceStore(tmp_path).list_files(f.s.task.tenant_id, 'demo', 'session')
    assert not any(isinstance(row, RunReviewIntentRow) and row.status == 'allow' for row in f.db.rows)
    assert not any(isinstance(row, RunReviewEventRow) and row.kind == 'consumed' for row in f.db.rows)
    admission.assert_immutable()


async def retained_unknown(admission: Admission) -> UUID:
    """Seed one genuine issued/UNKNOWN ledger debit, without provider IO."""
    f = admission.f
    execution = present(await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id))
    budget = execution.budget
    repository = RunAdmissionRepository(f.db)
    attempt_id, deployment = uuid4(), DeploymentIdentity('openai', 'primary_1', 'fixed')
    await repository.reserve(tenant_id=budget.tenant_id, run_id=budget.run_id,
        authorization_turn_id=budget.evidence.turn_id, lease=budget.lease,
        config_sha256=budget._pool.config_sha256, attempt_id=attempt_id,
        purpose='primary', request_sha256='e' * 64, deployment=deployment,
        token_hold=11, review_cost_hold=None)
    assert await repository.mark_issued(tenant_id=budget.tenant_id, run_id=budget.run_id,
        authorization_turn_id=budget.evidence.turn_id, lease=budget.lease,
        config_sha256=budget._pool.config_sha256, attempt_id=attempt_id,
        physical_transport_id=uuid4())
    result = await repository.finish(tenant_id=budget.tenant_id, run_id=budget.run_id,
        attempt_id=attempt_id, lease=budget.lease, deployment=deployment, outcome='unknown', usage=None)
    assert result.state == 'unknown'
    budget.revoke()
    return attempt_id


@pytest.mark.parametrize('defect', [
    'missing_pool', 'missing_seal', 'seal_sha', 'seal_time', 'proof_removed', 'proof_replaced',
    'source_changed', 'contracts_binding', 'catalog_changed', 'actor_changed', 'worker_changed',
    'original_deadline_expired',
])
async def test_v2_negative_recovery_never_rebuilds_or_refunds_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    admission = await fresh_admission(tmp_path)
    f = admission.f
    attempt_id = await retained_unknown(admission)
    pool = next(row for row in f.db.rows if isinstance(row, RunAdmissionPoolRow))
    opened = next(row for row in f.db.rows if isinstance(row, RunAdmissionEventRow) and row.kind == 'opened')
    context = f.s.task
    if defect == 'missing_pool':
        # An already checkpointed recovery is never a fresh admission. The
        # marker is a refusal input, not a claim that any runtime was replayed.
        checkpoint = RuntimeCheckpoint(id=uuid4(), tenant_id=context.tenant_id, run_id=context.run_id,
            mode=context.mode, runtime_type='continuity_probe', runtime_version='1', state={'paused': True})
        context = context.model_copy(update={'checkpoint': checkpoint})
        f.db.rows.remove(pool)
    elif defect == 'missing_seal':
        opened.evidence = {key: value for key, value in opened.evidence.items()
                           if key not in {'continuity_profile_sha256', 'admitted_at'}}
    elif defect == 'seal_sha':
        opened.evidence = {**opened.evidence, 'continuity_profile_sha256': '0' * 64}
    elif defect == 'seal_time':
        opened.evidence = {**opened.evidence, 'admitted_at': 'not-a-timestamp'}
    elif defect == 'proof_removed':
        admission.path.unlink()
    elif defect == 'proof_replaced':
        admission.path.write_bytes(admission.body + b'\n')
    elif defect == 'source_changed':
        monkeypatch.setattr(assembly, 'runtime_source_fingerprint', lambda: '0' * 64)
    elif defect == 'contracts_binding':
        profile = mutable(context.routing_decision['approval_review_profile'])
        activation = mutable(profile['activation'])
        profile['activation'] = {**activation, 'contracts_sha256': '0' * 64}
        route: dict[str, object] = {**context.routing_decision, 'approval_review_profile': profile}
        context = context.model_copy(update={'routing_decision': route})
        f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
        f.db.run.routing_decision = route
    elif defect == 'catalog_changed':
        f.state.version += 1
    elif defect == 'actor_changed':
        changed = uuid4()
        f.db.run.actor_id = changed
        f.s.runs.record = replace(f.s.runs.record, actor_id=changed)
    elif defect == 'worker_changed':
        changed = uuid4()
        f.db.run.worker_lease_token = changed
        f.s.runs.record = replace(f.s.runs.record, worker_lease_token=changed)
    else:
        pool.original_deadline = datetime.now(UTC) - timedelta(seconds=1)
    admission.expire_review_only(monkeypatch)
    rows = tuple(id(row) for row in f.db.rows)
    counters = (pool.original_token_grant, pool.original_deadline, pool.token_charged,
                pool.review_token_charged, pool.review_count_charged, pool.review_cost_charged)
    evidence = deepcopy(opened.evidence)
    with pytest.raises((assembly.ReviewAssemblyUnavailable, AdmissionConflict)):
        await f.broker.prepare(context, worker_id=f.s.budget.lease.worker_id)
    assert tuple(id(row) for row in f.db.rows) == rows
    assert opened.evidence == evidence
    assert counters == (pool.original_token_grant, pool.original_deadline, pool.token_charged,
                        pool.review_token_charged, pool.review_count_charged, pool.review_cost_charged)
    attempt = next(row for row in f.db.rows if isinstance(row, RunAdmissionAttemptRow)
                   and row.attempt_id == attempt_id)
    assert attempt.state == 'unknown' and attempt.token_hold == 11 and pool.token_charged == 11
    assert sum(isinstance(row, RunAdmissionAttemptRow) for row in f.db.rows) == 1
    if defect == 'missing_pool':
        assert not any(isinstance(row, RunAdmissionPoolRow) for row in f.db.rows)
    assert not f.state.calls


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_v2_negative_role_revocation_during_reserve_stops_before_send_and_keeps_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    admission = await fresh_admission(tmp_path, mode=mode)
    f = admission.f
    attempt_id = await retained_unknown(admission)
    entered, release = asyncio.Event(), asyncio.Event()
    reserve = RunAdmissionRepository.reserve

    async def held_reserve(self: RunAdmissionRepository, **arguments: Unpack[_ReserveArguments]) -> AttemptSnapshot:
        assert arguments['purpose'] == 'primary' and arguments['attempt_id'] != attempt_id
        entered.set()
        await release.wait()
        # Keep the real transaction, locks and post-wait authorization fence.
        return await reserve(self, **arguments)

    monkeypatch.setattr(RunAdmissionRepository, 'reserve', held_reserve)

    class Coding(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            gateway, _, _ = await _gateway_for_config(f.state.platform,
                tenant_id=context.tenant_id, secret_service=f.secrets,
                capacity_factory=f.capacity, transport=f.broker._transport)
            admission.expire_review_only(monkeypatch)
            await gateway.complete(logical_request())
            yield RunEvent(kind='runtime.unexpected', sequence=1, run_id=context.run_id, payload={'sent': True})

    async def drive() -> None:
        async with worker_runtime_events(Coding(), f.s.task, worker_id=f.s.budget.lease.worker_id,
                                         review_broker=f.broker) as events:
            await anext(events)

    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        f.db.run.actor_role = Role.VIEWER.value
        f.s.runs.record = replace(f.s.runs.record, actor_role=Role.VIEWER)
        release.set()
        with pytest.raises(_PhysicalAdmissionStopped):
            await asyncio.wait_for(task, timeout=10)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    admission.assert_immutable()
    assert not f.state.calls
    pool = present(await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id))
    assert pool.token_charged == 11 and pool.tokens_remaining == 39989
    attempts = [row for row in f.db.rows if isinstance(row, RunAdmissionAttemptRow)]
    assert len(attempts) == 1 and attempts[0].attempt_id == attempt_id and attempts[0].state == 'unknown'


@pytest.mark.parametrize('reader', ['source', 'legacy'])
async def test_v2_hardening_expiry_during_source_hash_is_rechecked_before_reader_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str,
) -> None:
    admission = await fresh_admission(tmp_path)
    fingerprint = assembly.runtime_source_fingerprint

    def expires_during_hash() -> str:
        result = fingerprint()
        admission.expire_review_only(monkeypatch)
        return result

    monkeypatch.setattr(assembly, 'runtime_source_fingerprint', expires_during_hash)
    digest = hashlib.sha256(admission.body).hexdigest()
    with pytest.raises(ValueError, match='invalid server review evidence'):
        if reader == 'source':
            assembly.ServerReviewEvidenceSource(admission.path, expected_sha256=digest).current()
        else:
            assembly.load_server_review_evidence(admission.path, expected_sha256=digest)
    assert admission.path.read_bytes() == admission.body and not admission.f.state.calls


async def test_v2_hardening_final_activation_fence_rechecks_utc_after_source_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = await fresh_admission(tmp_path)
    f = admission.f
    execution = present(await f.broker.prepare(f.s.task, worker_id=f.s.budget.lease.worker_id))
    current = assembly.ServerReviewEvidenceSource.current

    def expires_after_reader(self: assembly.ServerReviewEvidenceSource) -> assembly.VerifiedReviewActivation:
        activation = current(self)
        admission.expire_review_only(monkeypatch)
        return activation

    monkeypatch.setattr(assembly.ServerReviewEvidenceSource, 'current', expires_after_reader)
    with pytest.raises(assembly.ReviewAssemblyUnavailable, match='review_activation_changed'):
        f.broker._require_activation_current(execution.snapshot)
    admission.assert_immutable()
    assert not f.state.calls


@pytest.mark.parametrize('seam', ['seal_return', 'catalog_return', 'source_hash'])
async def test_v2_hardening_worker_revocation_after_last_await_or_hash_is_not_borrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str,
) -> None:
    admission = await fresh_admission(tmp_path)
    f = admission.f
    armed = False
    revoked = False

    def revoke() -> None:
        nonlocal revoked
        binding = present(execution_authority._BINDING.get())
        assert binding.active
        binding.active = False
        revoked = True

    if seam == 'seal_return':
        seal = RunAdmissionRepository.get_continuity_seal

        async def revoke_after_seal(self: RunAdmissionRepository, tenant_id: UUID,
                                    run_id: UUID) -> tuple[str, datetime] | None:
            result = await seal(self, tenant_id, run_id)
            if armed:
                revoke()
            return result

        monkeypatch.setattr(RunAdmissionRepository, 'get_continuity_seal', revoke_after_seal)
    else:
        current = f.broker._configs.get_current

        async def revoke_after_catalog(tenant_id: UUID) -> ConfigRevision | None:
            result = await current(tenant_id)
            if armed and seam == 'catalog_return':
                revoke()
            return result

        monkeypatch.setattr(f.broker._configs, 'get_current', revoke_after_catalog)
        fingerprint = assembly.runtime_source_fingerprint

        def revoke_after_hash() -> str:
            result = fingerprint()
            if armed and seam == 'source_hash':
                revoke()
            return result

        monkeypatch.setattr(assembly, 'runtime_source_fingerprint', revoke_after_hash)

    class OriginalWorker(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            nonlocal armed
            budget = present(_run_budget.get())
            if seam == 'seal_return':
                armed = True
                operation = budget.require_current
            else:
                require = budget.require_current

                async def arm_after_budget() -> None:
                    nonlocal armed
                    await require()
                    armed = True

                monkeypatch.setattr(budget, 'require_current', arm_after_budget)
                operation = f.broker._dispatch_check
            with pytest.raises(execution_authority.ExecutionAuthorityError):
                await operation()
            assert revoked
            assert not present(execution_authority._BINDING.get()).active
            assert not f.state.calls
            yield RunEvent(kind='runtime.checked', sequence=1, run_id=context.run_id, payload={'revoked': True})

    async with worker_runtime_events(OriginalWorker(), f.s.task, worker_id=f.s.budget.lease.worker_id,
                                     review_broker=f.broker) as events:
        await anext(events)
    admission.assert_immutable()


@pytest.mark.parametrize('authorization', ['exact', 'rules'])
async def test_v2_hardening_expired_review_does_not_block_current_exact_or_rules_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorization: str,
) -> None:
    admission = await fresh_admission(tmp_path)
    f = admission.f
    exact_reads: list[bool] = []
    if authorization == 'exact':
        # No broad workspace.write: only the exact manual request may write.
        route: dict[str, object] = {**f.s.task.routing_decision,
            'conversation_id': 'fixture-conversation',
            'requested_permissions': ['workspace.read', 'command.run']}
        f.s.task = f.s.task.model_copy(update={'routing_decision': route})
        f.db.run.routing_decision = route
        f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    else:
        # A genuine inbound workspace selection suppresses only the default
        # blanket ASK. An explicit ALLOW must not override an explicit ASK.
        route = {**f.s.task.routing_decision, 'conversation_id': 'fixture-conversation'}
        f.s.task = f.s.task.model_copy(update={'routing_decision': route})
        f.db.run.routing_decision = route
        f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    stack = assembly.assemble_review_capability_stack(f.broker, build_runtime_capability_stack,
        tenant_id=f.s.task.tenant_id, run_repository=f.s.runs, skill_store_dir=tmp_path / 'skills',
        workspace_root=tmp_path, generated_artifact_dir=tmp_path / 'generated', project_workspace_dir=tmp_path)
    if authorization == 'exact':
        request = _capability_request(f.s.task.tenant_id, present(f.s.task.actor_id), f.s.request)
        f.db.add(RunApprovalRow(id=uuid4(), tenant_id=f.s.task.tenant_id, run_id=f.s.task.run_id,
            approval_id='manual_exact_write', action='capability_tool', status='approved',
            created_at=datetime.now(UTC), payload={
                'approval_fingerprint': fingerprint_capability_request(request),
                'approval_scope': capability_approval_scope(request), 'decision_scope': 'once'}))
        # Keep the real fingerprint lookup; do not replace it with an always-True fixture.
        exact_lookup = RunRepository(f.db).is_capability_approval_approved

        async def read_exact(tenant_id: UUID, run_id: UUID, approval_fingerprint: str,
                             *, tool_origin: str | None = None) -> bool:
            result = await exact_lookup(tenant_id, run_id, approval_fingerprint, tool_origin=tool_origin)
            exact_reads.append(result)
            return result

        monkeypatch.setattr(f.s.runs, 'is_capability_approval_approved', read_exact)
    else:
        stack.policy_gateway._plugin_policy_rules = FixedPolicyRules((CapabilityRule(
            f.s.task.tenant_id, Role.OPERATOR, None, 'file', 'write', 'workspace/current', PolicyEffect.ALLOW,
        ),))
    model_checks = 0
    harness = stack.harness_tool_gateway
    assert isinstance(harness, assembly._ProfiledHarness)
    callback = present(harness._fresh._review_gate).model_dispatch_check
    assert callback is not None

    async def model_check() -> None:
        nonlocal model_checks
        model_checks += 1
        await callback()

    gate = present(harness._fresh._review_gate)
    policy_trace: list[str] = []
    policy_reader = stack.policy_gateway._current_review_policy

    async def traced_policy(request: CapabilityRequest, role: Role) -> tuple[CapabilityPolicy, bool]:
        try:
            policy, inbound = await policy_reader(request, role)
        except Exception as error:
            policy_trace.append(type(error).__name__)
            raise
        policy_trace.append(policy.evaluate(request, role).route)
        return policy, inbound

    monkeypatch.setattr(stack.policy_gateway, '_current_review_policy', traced_policy)
    fence_reader = gate.loader.fence

    async def traced_fence(request: CapabilityRequest, role: Role) -> _Fence:
        try:
            return await fence_reader(request, role)
        except Exception as error:
            policy_trace.append('fence:' + type(error).__name__)
            raise

    monkeypatch.setattr(gate.loader, 'fence', traced_fence)
    monkeypatch.setattr(gate, 'model_dispatch_check', model_check)
    dispatch_errors: list[str] = []
    dispatch = present(gate.dispatch_check)

    async def generic_dispatch() -> None:
        try:
            await dispatch()
        except Exception as error:
            dispatch_errors.append(type(error).__name__)
            raise

    monkeypatch.setattr(gate, 'dispatch_check', generic_dispatch)
    prepared: list[_ConcreteReviewCall] = []
    prepare_write = _ConcreteReviewCall._prepare_write_dispatch

    def observed_write(self: _ConcreteReviewCall, request: CapabilityRequest, fence: _Fence) -> None:
        prepare_write(self, request, fence)
        prepared.append(self)

    monkeypatch.setattr(_ConcreteReviewCall, '_prepare_write_dispatch', observed_write)

    class AlreadyAuthorized(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            admission.expire_review_only(monkeypatch)
            result = await stack.harness_tool_gateway.invoke(context.tenant_id, f.s.request,
                user_id=context.actor_id, role=context.actor_role)
            assert result.status == 'succeeded', (result.failure_reason, exact_reads, model_checks,
                                                  dispatch_errors, len(prepared), policy_trace)
            if authorization == 'exact':
                permissions = context.routing_decision['requested_permissions']
                assert isinstance(permissions, (list, tuple))
                assert 'workspace.write' not in permissions
                assert not f.s.runs.broad
                assert len(prepared) == 1 and prepared[0]._budget is _run_budget.get()
                assert prepared[0]._budget is not None and not prepared[0]._model_permit_consumed
                unapproved = replace(f.s.request, idempotency_key='not-manually-approved')
                denied = await stack.harness_tool_gateway.invoke(context.tenant_id, unapproved,
                    user_id=context.actor_id, role=context.actor_role)
                assert denied.status in {'failed', 'waiting_approval'}
            assert model_checks == 0 and not f.state.calls
            if authorization == 'rules':
                assert policy_trace and all(route == 'allowed' for route in policy_trace)
            files = ProjectWorkspaceStore(tmp_path).list_files(context.tenant_id, 'demo', 'session')
            assert [file.path for file in files] == ['hello.txt']
            assert ProjectWorkspaceStore(tmp_path).resolve_file(context.tenant_id, 'demo', 'session', 'hello.txt').read_bytes() == b'original content'
            yield RunEvent(kind='runtime.checked', sequence=1, run_id=context.run_id, payload={'authorized': True})

    async with worker_runtime_events(AlreadyAuthorized(), f.s.task, worker_id=f.s.budget.lease.worker_id,
                                     review_broker=f.broker) as events:
        await anext(events)
    admission.assert_immutable()
