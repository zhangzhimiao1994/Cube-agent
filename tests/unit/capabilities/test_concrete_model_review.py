"""Private context contracts and actual storage seam, without PG claims."""

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import (
    ExecutionAuthorityError,
    _trusted_review_context,
    bind_execution_authority,
)
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.model_review import ModelReviewOutcome
from agent_hub.capabilities.review_contracts import ModelReviewVerdict
from agent_hub.capabilities.review_repository import ReviewRepository, _ReviewReservation
from agent_hub.db.models import (
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.harness.tool_gateway import _capability_request
from agent_hub.models.physical_budget import _bind_run_budget
from agent_hub.models.run_admission_repository import DeploymentIdentity, UsageObservation
from agent_hub.runs.repository import RunRepository
from tests.unit.harness.test_review_dispatch import setup


async def test_loader_reads_real_worker_and_root(tmp_path):
    s = setup(tmp_path)
    request = _capability_request(s.task.tenant_id, s.task.actor_id, s.request)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        policy, inbound = await s.policy._current_review_policy(request, s.task.actor_role)
        assert not inbound
        assert policy.evaluate(request, s.task.actor_role).route == "model_required"
        snapshot, live, fence = await s.loader.load(
            request, s.task.actor_role, policy, tool_name=s.request.tool_name
        )
        assert snapshot.request() == request
        assert live.budget is s.budget
        assert fence.context is not s.task


async def test_effect_ignores_actor_key_and_run_while_action_binds_them(tmp_path):
    s = setup(tmp_path)
    request = _capability_request(s.task.tenant_id, s.task.actor_id, s.request)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        policy, _ = await s.policy._current_review_policy(request, s.task.actor_role)
        first, _, _ = await s.loader.load(
            request, s.task.actor_role, policy, tool_name=s.request.tool_name
        )
        other = request.model_copy(update={"agent_id": "other", "idempotency_key": "other"})
        second, _, _ = await s.loader.load(
            other, s.task.actor_role, policy, tool_name=s.request.tool_name
        )
        effect = first.to_payload()["normalized_effect"]
        assert (
            "run_id" not in str(effect)
            and "actor" not in str(effect)
            and "request_sha256" not in str(effect)
        )
        assert first.effect_digest == second.effect_digest
        assert first.action_digest != second.action_digest


async def test_file_effect_uses_actual_platform_case_semantics(tmp_path):
    s = setup(tmp_path)
    request = _capability_request(s.task.tenant_id, s.task.actor_id, s.request)
    other = request.model_copy(update={"arguments": {**request.arguments, "path": "HELLO.txt"}})
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        policy, _ = await s.policy._current_review_policy(request, s.task.actor_role)
        first, _, _ = await s.loader.load(
            request, s.task.actor_role, policy, tool_name=s.request.tool_name
        )
        second, _, _ = await s.loader.load(
            other, s.task.actor_role, policy, tool_name=s.request.tool_name
        )
        assert (first.effect_digest == second.effect_digest) == (
            os.path.normcase("HELLO.txt") == "hello.txt"
        )


async def test_accessor_is_independent_and_inherited_binding_revokes(tmp_path):
    s = setup(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    def read():
        return _trusted_review_context(
            tenant_id=s.task.tenant_id,
            run_id=s.task.run_id,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )

    async def child():
        started.set()
        await release.wait()
        with pytest.raises(ExecutionAuthorityError):
            read()

    with bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id):
        context, worker = read()
        object.__setattr__(context, "execution_token", uuid4())
        assert read()[0].execution_token == s.task.execution_token
        assert worker == s.budget.lease.worker_id
        with pytest.raises(ExecutionAuthorityError):
            _trusted_review_context(
                tenant_id=s.task.tenant_id,
                run_id=s.task.run_id,
                user_id=s.task.actor_id,
                role=Role.ADMIN,
            )
        pending = asyncio.create_task(child())
        await started.wait()
    release.set()
    await pending


def test_public_capability_result_has_no_private_review_fields():
    result = CapabilityResult(CapabilityStatus.ALLOWED, uuid4())
    assert set(asdict(result)) == {"status", "run_id", "approval_id", "reason", "review"}
    assert result.review is None


class FocusedDB:
    """ORM/query boundary double. Production ledger, fence and receipts run unchanged.

    Serializes transactions locally; this does NOT test PG locks/SQL/schema.
    """

    def __init__(self, s):
        self.s, self.rows, self.active = s, [], False
        self.lock = asyncio.Lock()
        route = {
            **s.runs.record.routing_decision,
            "runtime_token_absolute_tokens": 40000,
            "runtime_plan_token_budget": 40000,
        }
        s.runs.record = replace(s.runs.record, routing_decision=route)
        s.task = s.task.model_copy(update={"routing_decision": route})
        e, pool = s.budget.evidence, s.budget._pool
        self.run = RunRow(
            id=s.task.run_id,
            tenant_id=s.task.tenant_id,
            actor_id=e.actor_id,
            actor_role=e.actor_role.value,
            request=e.message,
            mode=s.task.mode.value,
            status="running",
            routing_decision=route,
            worker_id=s.budget.lease.worker_id,
            worker_lease_token=s.budget.lease.token,
            worker_lease_expires_at=s.runs.record.worker_lease_expires_at,
        )
        self.add(self.run)
        self.add(
            RunInputAuthorizationRow(run_id=pool.run_id, tenant_id=e.tenant_id, turn_id=e.turn_id)
        )
        self.add(
            RunAuthenticatedInputRow(
                turn_id=e.turn_id,
                run_id=pool.run_id,
                tenant_id=e.tenant_id,
                actor_id=e.actor_id,
                actor_role=e.actor_role.value,
                message=e.message,
                message_sha256=e.message_sha256,
                input_scope=e.input_scope,
                scope_sha256=e.scope_sha256,
            )
        )
        self.pool = RunAdmissionPoolRow(
            **asdict(pool),
            actor_id=e.actor_id,
            actor_role=e.actor_role.value,
            message_sha256=e.message_sha256,
            scope_sha256=e.scope_sha256,
        )
        self.add(self.pool)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @asynccontextmanager
    async def begin(self):
        async with self.lock:
            self.active = True
            try:
                yield
            finally:
                self.active = False

    def in_transaction(self):
        return self.active

    def add(self, row):
        if isinstance(row, RunReviewEventRow):
            row.sequence = len(self.rows)
        self.rows.append(row)

    async def flush(self):
        pass

    def select(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if type(row) is entity]
        for condition in statement._where_criteria:
            # These production calls use equality predicates only.
            name, value = condition.left.key, condition.right.value
            rows = [row for row in rows if getattr(row, name) == value]
        return rows

    async def scalar(self, statement):
        rows = self.select(statement)
        return rows[0] if rows else None

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: self.select(statement))

    async def execute(self, statement):
        if statement.column_descriptions[0].get("entity") is RunInputAuthorizationRow:
            link = next(r for r in self.rows if type(r) is RunInputAuthorizationRow)
            turn = next(r for r in self.rows if type(r) is RunAuthenticatedInputRow)
            return SimpleNamespace(one_or_none=lambda: (link, turn))
        assert "clock_timestamp" in str(statement)
        return SimpleNamespace(scalar_one=lambda: datetime.now(UTC))


class FixedLedgerReview:
    """Fixed verdict seam using the ACTUAL repositories for all durable changes."""

    def __init__(self, repo, status="allow"):
        self.repo, self.status, self.calls, self.hook = repo, status, 0, None

    async def review(self, snapshot, live):
        self.calls += 1
        req = snapshot.request()
        model = DeploymentIdentity("fixed", "review", "fixed-model")
        lease = live.budget.lease
        item = await self.repo.begin_intent(
            snapshot=snapshot,
            config=live.budget.config,
            reservation=_ReviewReservation("e" * 64, model, 100, Decimal("0.1"), lease),
            review_id=uuid4(),
        )
        assert item.created
        physical = uuid4()
        assert await self.repo.mark_issued(
            tenant_id=req.tenant_id,
            run_id=req.run_id,
            review_id=item.review_id,
            physical_identity=physical,
            lease=lease,
        )
        verdict = (
            None
            if self.status == "unknown"
            else ModelReviewVerdict(
                1,
                self.status,
                "low",
                "authorized_action",
                "Private fixed reasoning.",
                snapshot.action_digest,
            )
        )
        committed = await self.repo.finish(
            tenant_id=req.tenant_id,
            run_id=req.run_id,
            review_id=item.review_id,
            attempt_id=item.attempt_id,
            request_sha256="e" * 64,
            physical_identity=physical,
            lease=lease,
            actual_model_identity=model,
            outcome=self.status,
            verdict=verdict,
            usage=None if self.status == "unknown" else UsageObservation(1, 2, 3, Decimal("0.001")),
        )
        if self.hook:
            self.hook()
        return ModelReviewOutcome(
            committed.status,
            committed.risk,
            committed.reason_code,
            committed.action_digest,
            committed.effect_digest,
            review_id=committed.review_id,
        )


async def test_fixed_ledger_seam_commits_verifiable_allow(tmp_path):
    s = setup(tmp_path)
    db = FocusedDB(s)
    repo = ReviewRepository(db)
    fixed = FixedLedgerReview(repo)
    request = _capability_request(s.task.tenant_id, s.task.actor_id, s.request)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        policy, _ = await s.policy._current_review_policy(request, s.task.actor_role)
        snapshot, live, _ = await s.loader.load(
            request, s.task.actor_role, policy, tool_name=s.request.tool_name
        )
        outcome = await fixed.review(snapshot, live)
        assert outcome.status == "allow"
        assert (
            await repo.get(s.task.tenant_id, s.task.run_id, outcome.review_id)
        ).status == "allow"


@pytest.mark.parametrize("status", ["allow", "deny", "unknown"])
async def test_actual_repository_one_use_is_wired_to_real_backend(tmp_path, status):
    s = setup(tmp_path)
    db = FocusedDB(s)
    repo = ReviewRepository(db)
    fixed = FixedLedgerReview(repo, status)
    s.gate.repository, s.gate.service = repo, fixed
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        first = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
        second = await s.harness.invoke(
            s.task.tenant_id,
            replace(s.request, actor="repair_agent", idempotency_key="second"),
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
    assert first.status == ("succeeded" if status == "allow" else "failed"), first
    assert second.status == "failed"
    assert fixed.calls == 1
    assert len(s.calls) == (status == "allow")
    intents = [r for r in db.rows if type(r) is RunReviewIntentRow]
    assert len(intents) == 1
    assert intents[0].consumed == (status == "allow")
    assert [r.kind for r in db.rows if type(r) is RunReviewEventRow] == (
        ["prepared", "issued", "terminal", "consumed"]
        if status == "allow"
        else ["prepared", "issued", "terminal"]
    )


@pytest.mark.parametrize("change", ["lease", "role", "auth", "receipt"])
async def test_actual_repository_rejects_current_db_fence_or_bad_receipt(tmp_path, change):
    s = setup(tmp_path)
    db = FocusedDB(s)
    repo = ReviewRepository(db)
    fixed = FixedLedgerReview(repo)
    s.gate.repository, s.gate.service = repo, fixed

    def mutate():
        if change == "lease":
            db.run.worker_lease_token = uuid4()
        elif change == "role":
            db.run.actor_role = "viewer"
        elif change == "auth":
            next(r for r in db.rows if type(r) is RunInputAuthorizationRow).turn_id = uuid4()
        else:
            next(r for r in db.rows if type(r) is RunReviewIntentRow).receipt_sha256 = "0" * 64

    fixed.hook = mutate
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        result = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
    assert result.status == "failed"
    assert fixed.calls == 1
    assert s.calls == []
    assert not next(r for r in db.rows if type(r) is RunReviewIntentRow).consumed


@pytest.mark.parametrize(
    "action,expected", [("capability_tool.auto_review", False), ("capability_tool", True)]
)
async def test_fresh_exact_permission_distinguishes_human_from_legacy_model_upsert(
    tmp_path, action, expected
):
    from agent_hub.capabilities.approvals import fingerprint_capability_request
    from agent_hub.capabilities.defaults import _has_exact_human_approval

    s = setup(tmp_path)
    db = FocusedDB(s)
    request = _capability_request(s.task.tenant_id, s.task.actor_id, s.request)
    db.add(
        RunApprovalRow(
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            approval_id="approval_fixed",
            action=action,
            status="approved",
            payload={"approval_fingerprint": fingerprint_capability_request(request)},
        )
    )
    assert await _has_exact_human_approval(RunRepository(db), request) is expected
