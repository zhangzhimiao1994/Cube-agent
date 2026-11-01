"""Concrete worker/Harness integration; fixed review seam, no provider/PG claims."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Never, TypedDict, Unpack
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.model_review import ModelReviewOutcome, _LiveReviewContext
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.review_context import (
    ConcreteModelReviewGate,
    TrustedReviewContextLoader,
)
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig, FrozenReviewAction
from agent_hub.capabilities.review_repository import ReviewIntent, _CurrentReviewBinding
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.physical_budget import _bind_run_budget, _RunBudgetContext
from agent_hub.models.run_admission_repository import DeploymentIdentity
from agent_hub.runs.authorization import _AuthenticatedInput
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runtime.contracts import JsonValue, RunEvent, TaskContext
from tests.unit.harness.review_typed_fixture import (
    FixedPolicyRules,
    ReviewMemoryDB,
    StreamRuntimeStub,
    configured_budget,
    mapping,
    mutable,
    present,
)


class Runs(RunRepository):
    def __init__(self, record: RunRecord, evidence: _AuthenticatedInput) -> None:
        super().__init__(ReviewMemoryDB())
        self.record, self.evidence = record, evidence
        self.waits: list[dict[str, object]] = []
        self.broad = self.exact = False

    async def get(self, tenant_id: UUID, run_id: UUID, *, actor_id: UUID | None = None) -> RunRecord:
        return self.record

    async def load_authenticated_input(self, *args: object) -> _AuthenticatedInput:
        return self.evidence

    async def is_capability_approval_approved(self, *args: object, **kwargs: object) -> bool:
        return self.exact

    async def is_capability_approval_scope_approved(self, *args: object) -> bool:
        return self.broad

    async def is_conversation_capability_approved(self, *args: object, **kwargs: object) -> bool:
        return self.broad

    async def begin_capability_approval(self, *args: object, **kwargs: object) -> RunRecord:
        self.waits.append(kwargs)
        return self.record

    async def record_capability_approval_review(self, *args: object, **kwargs: object) -> Never:
        pytest.fail('model review must never write an ordinary approved grant')


class Permits:
    """Sequencing seam only; actual storage composition has a separate test."""
    def __init__(self) -> None:
        self.consumes: list[dict[str, object]] = []
        self.effects: dict[tuple[UUID, str], ReviewIntent] = {}
        self.used: set[UUID] = set()
        self.ok = True
        self.hook: Callable[[], None] | None = None

    async def lookup_effect(self, tenant_id: UUID, run_id: UUID, authorization_turn_id: UUID, effect_digest: str) -> ReviewIntent | None:
        return self.effects.get((authorization_turn_id, effect_digest))

    async def consume_permit(self, *, tenant_id: UUID, run_id: UUID, review_id: UUID, current_binding: _CurrentReviewBinding) -> bool:
        self.consumes.append({"tenant_id": tenant_id, "run_id": run_id, "review_id": review_id, "current_binding": current_binding})
        if self.hook:
            self.hook()
        if not self.ok or review_id in self.used:
            return False
        self.used.add(review_id)
        return True


class Reviewer:
    def __init__(self, permits: Permits) -> None:
        self.permits = permits
        self.calls: list[tuple[FrozenReviewAction, _LiveReviewContext]] = []
        self.hook: Callable[[], Awaitable[None]] | None = None
        self.status = 'allow'

    async def review(self, snapshot: FrozenReviewAction, live_context: _LiveReviewContext) -> ModelReviewOutcome:
        live = live_context
        self.calls.append((snapshot, live))
        if self.hook:
            await self.hook()
        outcome = ModelReviewOutcome(self.status, 'low', 'authorized_action', snapshot.action_digest,
            snapshot.effect_digest, review_id=uuid4())
        req = snapshot.request()
        # Synthetic sequencing record, not a durable repository/PG receipt.
        self.permits.effects[(live.budget.evidence.turn_id, snapshot.effect_digest)] = ReviewIntent(
            present(outcome.review_id), req.tenant_id, present(req.run_id), uuid4(),
            snapshot.action_digest, snapshot.effect_digest, '0'*64, '0'*64, '0'*64, '0'*64, '0'*64,
            DeploymentIdentity('fixed','review','fixed-model'), None,
            self.status, outcome.risk, outcome.reason_code, False)
        return outcome


class ExecuteCall(TypedDict):
    tenant_id: UUID
    run_id: UUID
    actor: str
    name: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str


@dataclass
class ReviewSettings:
    config: ApprovalReviewConfig


@dataclass
class ReviewSetup:
    task: TaskContext
    budget: _RunBudgetContext
    runs: Runs
    backend: RuntimeCapabilityGateway
    calls: list[ExecuteCall]
    settings: ReviewSettings
    policy: DefaultRuntimeCapabilityPolicyGateway
    permits: Permits
    reviewer: Reviewer
    loader: TrustedReviewContextLoader
    gate: ConcreteModelReviewGate
    harness: HarnessToolGateway
    request: HarnessToolCallRequest


def setup(tmp_path: Path) -> ReviewSetup:
    from agent_hub.capabilities.review_context import (
        ConcreteModelReviewGate,
        TrustedReviewContextLoader,
    )

    budget = configured_budget()
    route = {
        "project_id": "demo",
        "workspace_session_id": "session",
        "sandbox_profile": "workspace_write",
        "permission_mode": "auto_review",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
        "execution_backend": "systemd",
        "runtime_timeout_seconds": 60,
    }
    task = TaskContext(
        tenant_id=budget.tenant_id,
        run_id=budget.run_id,
        actor_id=budget.evidence.actor_id,
        actor_role=Role.OPERATOR,
        mode=TaskMode.DIRECT,
        execution_token=budget.lease.token,
        request=budget.evidence.message,
        routing_decision=mapping(route),
    )
    record = RunRecord(
        task.run_id,
        task.tenant_id,
        task.actor_id,
        task.request,
        task.mode,
        RunStatus.RUNNING,
        1,
        datetime.now(UTC),
        route,
        actor_role=task.actor_role,
        worker_id=budget.lease.worker_id,
        worker_lease_token=budget.lease.token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
    )
    runs = Runs(record, budget.evidence)
    backend = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        tenant_id=task.tenant_id,
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
        run_repository=runs,
    )
    calls: list[ExecuteCall] = []
    real_execute = backend.execute

    async def execute(**kwargs: Unpack[ExecuteCall]) -> Mapping[str, JsonValue]:
        calls.append(kwargs)
        return await real_execute(**kwargs)

    pytest.MonkeyPatch().setattr(backend, "execute", execute)
    settings = ReviewSettings(budget.config)

    async def config(tenant: UUID) -> ApprovalReviewConfig:
        return settings.config

    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        runs,
        exact_human_approval_getter=lambda _: runs.is_capability_approval_approved(),
    )
    permits = Permits()
    reviewer = Reviewer(permits)
    loader = TrustedReviewContextLoader(
        runs, preparation_reader=backend.action_preparation_snapshot, config_getter=config
    )
    gate = ConcreteModelReviewGate(loader, reviewer, permits)
    harness = HarnessToolGateway(backend, policy_gateway=policy, review_gate=gate)
    request = HarnessToolCallRequest(
        task.run_id,
        "main_agent",
        "workspace.write_text",
        {"path": "hello.txt", "content": "original content"},
        True,
        "workspace_write",
        "first",
    )
    return ReviewSetup(task, budget, runs, backend, calls, settings, policy, permits, reviewer, loader, gate, harness, request)


async def invoke(s: ReviewSetup, request: HarnessToolCallRequest | None = None) -> HarnessToolCallResult:
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        return await s.harness.invoke(
            s.task.tenant_id, request or s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )


async def test_allow_executes_owned_full_envelope_once(tmp_path: Path) -> None:
    s = setup(tmp_path)

    async def mutate() -> None:
        object.__setattr__(s.request, "arguments", {"path": "bad.txt", "content": "changed"})
        object.__setattr__(s.request, "idempotency_key", "changed")

    s.reviewer.hook = mutate
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert len(s.calls) == len(s.permits.consumes) == len(s.reviewer.calls) == 1
    assert s.calls[0]["arguments"] == {"path": "hello.txt", "content": "original content"}
    assert s.calls[0]["idempotency_key"] == "first"
    assert s.reviewer.calls[0][0].request().arguments == dict(s.calls[0]["arguments"])
    files = ProjectWorkspaceStore(tmp_path).list_files(s.task.tenant_id, "demo", "session")
    assert [f.path for f in files] == ["hello.txt"]
    assert not s.runs.waits


@pytest.mark.parametrize("status", ["deny", "unknown", "needs_human"])
async def test_equivalent_effect_never_reissues_paid_review(tmp_path: Path, status: Literal["allow", "deny", "unknown", "needs_human"]) -> None:
    s = setup(tmp_path)
    s.reviewer.status = status
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        assert (
            await s.harness.invoke(
                s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
            )
        ).status == "failed"
        second = replace(s.request, actor="repair_agent", idempotency_key="second")
        assert (
            await s.harness.invoke(
                s.task.tenant_id, second, user_id=s.task.actor_id, role=s.task.actor_role
            )
        ).status == "failed"
    assert len(s.reviewer.calls) == 1
    assert not (s.calls or s.permits.consumes)


@pytest.mark.parametrize(
    "change",
    [
        "policy",
        "config",
        "source",
        "root",
        "store_root",
        "role",
        "lease",
        "auth",
        "scope",
        "status",
        "revoke",
    ],
)
async def test_changed_live_fences_never_execute(tmp_path: Path, change: str) -> None:
    s = setup(tmp_path)

    async def mutate() -> None:
        if change == "policy":
            s.policy._plugin_policy_rules = FixedPolicyRules(
                (
                    CapabilityRule(
                        s.task.tenant_id,
                        Role.OPERATOR,
                        None,
                        "file",
                        "write",
                        "workspace/current",
                        PolicyEffect.DENY,
                    ),
                )
            )
        elif change == "config":
            s.settings.config = replace(s.settings.config, max_reviews_per_run=1)
        elif change == "source":
            ProjectWorkspaceStore(tmp_path).write_bytes(
                tenant_id=s.task.tenant_id,
                project_id="demo",
                session_id="session",
                relative_path="changed.txt",
                data=b"changed",
                mime_type="text/plain",
            )
        elif change == "root":
            changed = tmp_path / "another-root"
            changed.mkdir()
            present(s.backend._project_verification).root = changed
        elif change == "store_root":
            changed = tmp_path / "another-store"
            changed.mkdir()
            s.backend._project_workspace_store = ProjectWorkspaceStore(changed)
        elif change == "role":
            s.runs.record = replace(s.runs.record, actor_role=Role.VIEWER)
        elif change == "lease":
            s.runs.record = replace(s.runs.record, worker_lease_token=uuid4())
        elif change == "auth":
            s.runs.evidence = replace(s.runs.evidence, turn_id=uuid4())
        elif change == "scope":
            s.runs.record = replace(
                s.runs.record,
                routing_decision={
                    **present(s.runs.record.routing_decision),
                    "requested_permissions": ["workspace.read"],
                },
            )
        elif change == "status":
            s.runs.record = replace(s.runs.record, status=RunStatus.CANCELLED)
        else:
            s.budget.revoke()

    s.reviewer.hook = mutate
    assert (await invoke(s)).status == "failed"
    assert len(s.reviewer.calls) == 1
    assert s.calls == []


async def test_static_discovery_never_waits_reviews_or_consumes(tmp_path: Path) -> None:
    s = setup(tmp_path)
    # Discovery intentionally lacks worker/budget; it must not request concrete consent.
    result = await s.harness.authorize(
        s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
    )
    assert result.status == "succeeded", result
    assert not (s.runs.waits or s.reviewer.calls or s.permits.consumes or s.calls)


@pytest.mark.parametrize("tool", ["workspace.write_text", "workspace.bundle"])
async def test_actual_inbound_zero_review_without_root_or_budget(tmp_path: Path, tool: str) -> None:
    s = setup(tmp_path)
    if tool == "workspace.bundle":
        ProjectWorkspaceStore(tmp_path).write_bytes(
            tenant_id=s.task.tenant_id,
            project_id="demo",
            session_id="session",
            relative_path="hello.txt",
            data=b"hello",
            mime_type="text/plain",
        )
        s.request = replace(s.request, tool_name=tool, arguments={})

    async def forbidden_root(_: object) -> Never:
        pytest.fail("inbound authorization must not consult Root")

    s.loader._preparation_reader = forbidden_root
    route = {
        **present(s.runs.record.routing_decision),
        "permission_mode": "auto_review",
        "conversation_id": "conversation",
    }
    s.task = s.task.model_copy(update={"routing_decision": route})
    s.runs.record = replace(s.runs.record, routing_decision=route)
    with bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id):
        result = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
    assert result.status == "succeeded", result
    assert len(s.calls) == 1
    assert not (s.runs.waits or s.reviewer.calls or s.permits.consumes)


async def test_consume_failure_cannot_downgrade_to_broad_grant(tmp_path: Path) -> None:
    s = setup(tmp_path)
    s.runs.broad = True
    s.permits.ok = False
    assert (await invoke(s)).status == "failed"
    assert len(s.permits.consumes) == 1
    assert s.calls == []


async def test_post_consume_revocation_blocks_dispatch(tmp_path: Path) -> None:
    s = setup(tmp_path)
    s.permits.hook = s.budget.revoke
    assert (await invoke(s)).status == "failed"
    assert len(s.permits.consumes) == 1
    assert s.calls == []


async def test_missing_worker_context_uses_human_even_with_broad_grant(tmp_path: Path) -> None:
    s = setup(tmp_path)
    s.runs.broad = True
    result = await s.harness.invoke(
        s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
    )
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert not (s.reviewer.calls or s.permits.consumes or s.calls)


@pytest.mark.parametrize("effect", [PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL])
async def test_current_human_only_and_deny_beat_all_broad_shortcuts(tmp_path: Path, effect: PolicyEffect) -> None:
    s = setup(tmp_path)
    route = {**present(s.runs.record.routing_decision), "conversation_id": "conversation"}
    s.runs.record = replace(s.runs.record, routing_decision=route)
    s.task = s.task.model_copy(update={"routing_decision": route})
    s.runs.broad = True
    s.policy._plugin_policy_rules = FixedPolicyRules(
        (
            CapabilityRule(
                s.task.tenant_id, Role.OPERATOR, None, "file", "write", "workspace/current", effect
            ),
        )
    )
    assert (await invoke(s)).status == "failed"
    assert len(s.runs.waits) == (effect is PolicyEffect.REQUIRE_APPROVAL)
    assert not (s.calls or s.permits.consumes or s.reviewer.calls)


async def test_exact_human_permission_can_override_model_deny(tmp_path: Path) -> None:
    s = setup(tmp_path)
    s.reviewer.status = "deny"
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        first = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
        assert first.status == "failed"
        s.runs.exact = True
        second = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
    assert second.status == "succeeded"
    assert len(s.calls) == len(s.reviewer.calls) == 1
    assert s.permits.consumes == []


@pytest.mark.parametrize(
    "missing", ["budget", "root", "sensitive", "reference", "private_target", "excluded_target"]
)
async def test_incomplete_material_requires_human_without_model(tmp_path: Path, missing: str) -> None:
    s = setup(tmp_path)
    if missing == "root":
        s.backend._project_verification = None
    elif missing == "sensitive":
        s.request = replace(
            s.request, arguments={"path": "hello.txt", "content": "password=private"}
        )
    elif missing == "reference":
        s.request = replace(
            s.request, arguments={"path": "hello.txt", "content_reference": {"digest": "a" * 64}}
        )
    elif missing == "private_target":
        s.request = replace(
            s.request, arguments={"path": "credentials.json", "content": "ordinary text"}
        )
    elif missing == "excluded_target":
        s.request = replace(
            s.request, arguments={"path": "node_modules/file.txt", "content": "ordinary text"}
        )
    if missing == "budget":
        with bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id):
            result = await s.harness.invoke(
                s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
            )
    else:
        result = await invoke(s)
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert not (s.calls or s.permits.consumes or s.reviewer.calls)


async def test_cancellation_during_model_cannot_revive_dispatch(tmp_path: Path) -> None:
    s = setup(tmp_path)

    async def cancel() -> None:
        present(asyncio.current_task()).cancel()
        await asyncio.sleep(0)

    s.reviewer.hook = cancel
    with pytest.raises(asyncio.CancelledError):
        await invoke(s)
    assert len(s.reviewer.calls) == 1
    assert not (s.calls or s.permits.consumes)


async def test_worker_stream_owns_actual_harness_call(tmp_path: Path) -> None:
    from agent_hub.runtime.contracts import RunEvent
    from agent_hub.runtime.worker_execution import worker_runtime_events

    s = setup(tmp_path)
    results = []

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            results.append(
                await s.harness.invoke(
                    context.tenant_id, s.request, user_id=context.actor_id, role=context.actor_role
                )
            )
            yield RunEvent(kind="model.started", sequence=1, run_id=context.run_id)

    with _bind_run_budget(s.budget):
        async with worker_runtime_events(
            Runtime(), s.task, worker_id=s.budget.lease.worker_id
        ) as stream:
            async for _ in stream:
                pass
    assert results[0].status == "succeeded"
    assert len(s.calls) == 1


async def test_revocation_in_final_policy_await_cannot_execute(tmp_path: Path) -> None:
    s = setup(tmp_path)
    original = s.policy._current_review_policy
    reads = 0

    async def policy_reader(request: CapabilityRequest, role: Role) -> tuple[CapabilityPolicy, bool]:
        nonlocal reads
        reads += 1
        result = await original(request, role)
        # initial + before consume (2 reads) + before dispatch (2 reads).
        if reads == 5:
            s.budget.revoke()
        return result

    pytest.MonkeyPatch().setattr(s.policy, "_current_review_policy", policy_reader)
    assert (await invoke(s)).status == "failed"
    assert reads == 5
    assert len(s.permits.consumes) == 1
    assert s.calls == []


@pytest.mark.parametrize("authorization", ["model", "exact"])
@pytest.mark.parametrize("change", ["root", "store", "source", "backend"])
async def test_final_authorization_await_cannot_dispatch_stale_source(
    tmp_path: Path, authorization: str, change: str
) -> None:
    """Use actual Root/files/backend at the last query, after async source validation."""
    s = setup(tmp_path)
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(
        tenant_id=s.task.tenant_id,
        project_id="demo",
        session_id="session",
        relative_path="baseline.txt",
        data=b"before",
        mime_type="text/plain",
    )
    changed_root = tmp_path / "changed-root"
    changed_root.mkdir()
    policy_reads = exact_reads = mutations = 0
    original_policy = s.policy._current_review_policy
    other = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "other-skills",
        tenant_id=s.task.tenant_id,
        project_workspace_dir=changed_root,
        run_repository=s.runs,
    )
    other_execute = other.execute

    async def record_other_entry(**kwargs: Unpack[ExecuteCall]) -> Mapping[str, JsonValue]:
        s.calls.append(kwargs)
        return await other_execute(**kwargs)

    pytest.MonkeyPatch().setattr(other, "execute", record_other_entry)

    def mutate() -> None:
        nonlocal mutations
        mutations += 1
        if change == "root":
            # Change all real backend Root owners together; this is a genuinely
            # different target, not just a loader-seam digest or mismatched config.
            present(s.backend._project_verification).root = changed_root
            s.backend._project_workspace_dir = changed_root
            s.backend._project_workspace_store = ProjectWorkspaceStore(changed_root)
        elif change == "store":
            s.backend._project_workspace_store = ProjectWorkspaceStore(changed_root)
        elif change == "backend":
            s.harness._backend = other
        else:
            # Same name/size, different bytes: metadata-only checks are insufficient.
            store.write_bytes(
                tenant_id=s.task.tenant_id,
                project_id="demo",
                session_id="session",
                relative_path="baseline.txt",
                data=b"after!",
                mime_type="text/plain",
            )

    async def policy_reader(request: CapabilityRequest, role: Role) -> tuple[CapabilityPolicy, bool]:
        nonlocal policy_reads
        policy_reads += 1
        result = await original_policy(request, role)
        if authorization == "model" and policy_reads == 5:
            await asyncio.sleep(0)
            mutate()
        return result

    async def exact_reader(_request: CapabilityRequest) -> bool:
        nonlocal exact_reads
        exact_reads += 1
        if exact_reads == 2:
            await asyncio.sleep(0)
            mutate()
        return True

    pytest.MonkeyPatch().setattr(s.policy, "_current_review_policy", policy_reader)
    if authorization == "exact":
        s.runs.exact = True
        s.policy._exact_human_approval_getter = exact_reader
    result = await invoke(s)
    assert mutations == 1
    assert len(s.reviewer.calls) == len(s.permits.consumes) == (authorization == "model")
    assert s.runs.waits == []  # failure must not request another grant/review.
    if authorization == "model":
        assert policy_reads == 5
    else:
        assert exact_reads == 2
    assert s.calls == [], result  # count actual backend ENTRY, not only its final result.
    assert result.status == "failed"
    assert [item.path for item in store.list_files(s.task.tenant_id, "demo", "session")] == [
        "baseline.txt"
    ]
    assert not ProjectWorkspaceStore(changed_root).list_files(s.task.tenant_id, "demo", "session")


def _point_directory_alias(alias: Path, target: Path) -> None:
    """Real OS alias, not a patched resolver or scanner."""
    if alias.is_symlink():
        alias.unlink()
    elif alias.is_junction():
        alias.rmdir()  # Remove only the junction, never recursively remove its target.
    if os.name == "nt":
        import _winapi

        junction_name = "CreateJunction"
        junction = getattr(_winapi, junction_name)
        assert callable(junction)
        junction(str(target), str(alias))
        assert alias.is_junction()
    else:
        alias.symlink_to(target, target_is_directory=True)
        assert alias.is_symlink()
    assert alias.resolve(strict=True) == target.resolve(strict=True)


def _alias_workspace(tmp_path: Path) -> tuple[ReviewSetup, ProjectWorkspaceStore, Path, Path]:
    s = setup(tmp_path)
    store = ProjectWorkspaceStore(tmp_path)
    for directory in ("public", "other", "credentials", "node_modules"):
        store.write_bytes(
            tenant_id=s.task.tenant_id,
            project_id="demo",
            session_id="session",
            relative_path=f"{directory}/target.txt",
            data=f"{directory} before".encode(),
            mime_type="text/plain",
        )
    root = store.session_root(s.task.tenant_id, "demo", "session")
    alias = root / "alias"
    _point_directory_alias(alias, root / "public")
    s.request = replace(
        s.request, arguments={"path": "alias/target.txt", "content": "original content"}
    )
    return s, store, root, alias


def _workspace_manifest(s: ReviewSetup, store: ProjectWorkspaceStore) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (f.path, f.sha256, f.size_bytes)
        for f in store.list_files(s.task.tenant_id, "demo", "session")
    )


@pytest.mark.parametrize("authorization", ["model", "exact"])
@pytest.mark.parametrize("target", ["other", "credentials", "node_modules"])
async def test_final_await_alias_target_change_cannot_dispatch(tmp_path: Path, authorization: str, target: str) -> None:
    s, store, root, alias = _alias_workspace(tmp_path)
    before = _workspace_manifest(s, store)
    assert [row[0] for row in before] == ["other/target.txt", "public/target.txt"]
    contents = {
        directory: (root / directory / "target.txt").read_bytes()
        for directory in ("public", "other", "credentials", "node_modules")
    }
    original_policy = s.policy._current_review_policy
    policy_reads = exact_reads = mutations = 0

    def retarget() -> None:
        nonlocal mutations
        _point_directory_alias(alias, root / target)
        mutations += 1
        assert _workspace_manifest(s, store) == before

    async def policy_reader(request: CapabilityRequest, role: Role) -> tuple[CapabilityPolicy, bool]:
        nonlocal policy_reads
        policy_reads += 1
        result = await original_policy(request, role)
        if authorization == "model" and policy_reads == 5:
            await asyncio.sleep(0)
            retarget()
        return result

    async def exact_reader(_request: CapabilityRequest) -> bool:
        nonlocal exact_reads
        exact_reads += 1
        if exact_reads == 2:
            await asyncio.sleep(0)
            retarget()
        return True

    pytest.MonkeyPatch().setattr(s.policy, "_current_review_policy", policy_reader)
    if authorization == "exact":
        s.runs.exact = True
        s.policy._exact_human_approval_getter = exact_reader
    result = await invoke(s)
    assert mutations == 1
    assert (policy_reads if authorization == "model" else exact_reads) == (
        5 if authorization == "model" else 2
    )
    assert len(s.reviewer.calls) == len(s.permits.consumes) == (authorization == "model")
    if authorization == "model":
        action = s.reviewer.calls[0][0]
        assert action.request().arguments["path"] == "alias/target.txt"
        assert mutable(action.to_payload()["normalized_effect"])["target"] == "public/target.txt"
    assert s.runs.waits == []
    assert s.calls == [], result
    assert result.status == "failed"
    assert _workspace_manifest(s, store) == before
    assert {
        directory: (root / directory / "target.txt").read_bytes() for directory in contents
    } == contents


@pytest.mark.parametrize("authorization", ["model", "exact"])
async def test_unchanged_alias_target_uses_owned_path(tmp_path: Path, authorization: str) -> None:
    s, _store, root, _alias = _alias_workspace(tmp_path)

    def mutate_original() -> None:
        object.__setattr__(
            s.request, "arguments", {"path": "credentials/target.txt", "content": "changed"}
        )

    if authorization == "model":

        async def reviewed(_snapshot: FrozenReviewAction | None=None) -> None:
            mutate_original()

        s.reviewer.hook = reviewed
    else:
        s.runs.exact = True
        reads = 0

        async def exact_reader(_request: CapabilityRequest) -> bool:
            nonlocal reads
            reads += 1
            if reads == 2:
                mutate_original()
            return True

        s.policy._exact_human_approval_getter = exact_reader
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert len(s.calls) == 1
    assert dict(s.calls[0]["arguments"]) == {
        "path": "alias/target.txt",
        "content": "original content",
    }
    assert (root / "public" / "target.txt").read_bytes() == b"original content"
    assert (root / "credentials" / "target.txt").read_bytes() == b"credentials before"
    assert len(s.reviewer.calls) == len(s.permits.consumes) == (authorization == "model")


async def test_exact_explicit_private_target_is_not_model_authority(tmp_path: Path) -> None:
    s, _store, root, _alias = _alias_workspace(tmp_path)
    s.request = replace(
        s.request, arguments={"path": "credentials/target.txt", "content": "manual content"}
    )
    s.runs.exact = True
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert len(s.calls) == 1
    assert (root / "credentials" / "target.txt").read_bytes() == b"manual content"
    assert not (s.reviewer.calls or s.permits.consumes or s.runs.waits)


@pytest.mark.parametrize("target", ["credentials", "node_modules"])
async def test_initial_private_alias_still_requires_human(tmp_path: Path, target: str) -> None:
    s, _store, root, alias = _alias_workspace(tmp_path)
    _point_directory_alias(alias, root / target)
    result = await invoke(s)
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert not (s.calls or s.reviewer.calls or s.permits.consumes)


@pytest.mark.parametrize("authorization", ["model", "exact"])
@pytest.mark.parametrize("callback_result", ["none", "allow", "deny", "retarget"])
async def test_dispatch_check_await_precedes_final_target_guard(
    tmp_path: Path, authorization: str, callback_result: str
) -> None:
    s, store, root, alias = _alias_workspace(tmp_path)
    before = _workspace_manifest(s, store)
    callback_calls = 0

    async def dispatch_check() -> None:
        nonlocal callback_calls
        callback_calls += 1
        # This hook is AFTER authorization/one-use consumption; it cannot mint
        # another review or execution grant, and failures leave the permit spent.
        assert len(s.reviewer.calls) == len(s.permits.consumes) == (authorization == "model")
        assert s.calls == []
        await asyncio.sleep(0)
        if callback_result == "deny":
            raise RuntimeError("private callback details")
        if callback_result == "retarget":
            _point_directory_alias(alias, root / "credentials")
            assert _workspace_manifest(s, store) == before

    s.harness._review_gate = type(s.gate)(
        s.loader,
        s.reviewer,
        s.permits,
        dispatch_check=None if callback_result == "none" else dispatch_check,
    )
    s.runs.exact = authorization == "exact"
    result = await invoke(s)
    allowed = callback_result in {"none", "allow"}
    assert callback_calls == (callback_result != "none")
    assert result.status == ("succeeded" if allowed else "failed"), result
    assert len(s.calls) == allowed
    assert len(s.reviewer.calls) == len(s.permits.consumes) == (authorization == "model")
    assert s.runs.waits == []
    assert (root / "credentials" / "target.txt").read_bytes() == b"credentials before"
    assert (root / "public" / "target.txt").read_bytes() == (
        b"original content" if allowed else b"public before"
    )
    if not allowed:
        assert _workspace_manifest(s, store) == before
    assert "private callback details" not in str(result.to_payload())


@pytest.mark.parametrize("tool,sandbox", [("unknown.command", "systemd"), ("vendor.echo", "mcp")])
async def test_unknown_external_effect_is_human_only_even_with_broad(tmp_path: Path, tool: str, sandbox: str) -> None:
    s = setup(tmp_path)
    s.runs.broad = True

    class External:
        def is_available(self, *args: object) -> bool:
            return True

        async def invoke(self, **kwargs: object) -> Never:
            pytest.fail("uninterpretable external action executed")

    s.harness._mcp_backend = External()
    request = replace(s.request, tool_name=tool, sandbox=sandbox, arguments={"command": "unknown"})
    assert (await invoke(s, request)).status == "failed"
    assert len(s.runs.waits) == 1
    assert not (s.calls or s.permits.consumes or s.reviewer.calls)


async def test_root_reader_must_belong_to_the_actual_dispatch_backend(tmp_path: Path) -> None:
    s = setup(tmp_path)
    other = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "other-skills",
        tenant_id=s.task.tenant_id,
        project_workspace_dir=tmp_path / "other-root",
        run_repository=s.runs,
    )
    (tmp_path / "other-root").mkdir()
    s.loader._preparation_reader = other.action_preparation_snapshot
    assert (await invoke(s)).status == "failed"
    assert len(s.runs.waits) == 1
    assert not (s.reviewer.calls or s.permits.consumes or s.calls)


async def test_fresh_human_fallback_never_reuses_blanket_pending_scope(tmp_path: Path) -> None:
    s = setup(tmp_path)
    s.backend._project_verification = None
    with bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id):
        first = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
        second = await s.harness.invoke(
            s.task.tenant_id,
            replace(s.request, arguments={"path": "other.txt", "content": "other content"}),
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
    assert first.status == second.status == "failed"
    assert first.payload["approval_id"] != second.payload["approval_id"]


@pytest.mark.parametrize(
    "mode,tenant_mode,expected",
    [
        ("default", "auto_review", "waiting_approval"),
        ("auto_review", "auto_review", "allow"),
        ("auto_review", "ask", "waiting_approval"),
    ],
)
async def test_legacy_mode_with_valid_current_status_preserves_behavior(
    mode: str, tenant_mode: str, expected: str
) -> None:
    from tests.unit.capabilities.test_defaults import RecordingApprovalRepository, request
    from tests.unit.harness.review_typed_fixture import ModeRepository

    runs = ModeRepository(mode)
    assert isinstance(runs, RecordingApprovalRepository)
    runs.record = replace(runs.record, status=RunStatus.RUNNING)

    async def configured_mode(_: object) -> str:
        return tenant_mode

    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), runs, tool_approval_mode=configured_mode
    )
    result = await policy.invoke(
        request("file", "create", "generated/document.generate_docx"), role=Role.OPERATOR
    )
    assert result.status.value == expected


async def test_exact_human_permission_cannot_revive_revoked_run_budget_scope(tmp_path: Path) -> None:
    s = setup(tmp_path)
    s.runs.exact = True
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        s.budget.revoke()
        result = await s.harness.invoke(
            s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )
    assert result.status == "failed"
    assert not (s.calls or s.reviewer.calls or s.permits.consumes)
