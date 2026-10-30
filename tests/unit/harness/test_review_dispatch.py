"""Concrete worker/Harness integration; fixed review seam, no provider/PG claims."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.model_review import ModelReviewOutcome
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.models.physical_budget import _bind_run_budget
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import TaskContext
from tests.unit.models.test_physical_budget import configured


class Runs:
    def __init__(self, record, evidence):
        self.record, self.evidence = record, evidence
        self.waits = []
        self.broad = self.exact = False

    async def get(self, *args):
        return self.record

    async def load_authenticated_input(self, *args):
        return self.evidence

    async def is_capability_approval_approved(self, *args, **kwargs):
        return self.exact

    async def is_capability_approval_scope_approved(self, *args):
        return self.broad

    async def is_conversation_capability_approved(self, *args, **kwargs):
        return self.broad

    async def begin_capability_approval(self, *args, **kwargs):
        self.waits.append(kwargs)

    async def record_capability_approval_review(self, *args, **kwargs):
        pytest.fail("model review must never write an ordinary approved grant")


class Permits:
    """Sequencing seam only; actual storage composition has a separate test."""

    def __init__(self):
        self.consumes = []
        self.effects = {}
        self.ok = True
        self.hook = None

    async def lookup_effect(self, tenant, run, turn, effect):
        return self.effects.get((turn, effect))

    async def consume_permit(self, **kwargs):
        self.consumes.append(kwargs)
        if self.hook:
            self.hook()
        key = kwargs["review_id"]
        if not self.ok or key in self.effects:
            return False
        self.effects[key] = True
        return True


class Reviewer:
    def __init__(self, permits):
        self.permits, self.calls, self.hook = permits, [], None
        self.status = "allow"

    async def review(self, snapshot, live):
        self.calls.append((snapshot, live))
        if self.hook:
            await self.hook()
        outcome = ModelReviewOutcome(
            self.status,
            "low",
            "authorized_action",
            snapshot.action_digest,
            snapshot.effect_digest,
            review_id=uuid4(),
        )
        self.permits.effects[(live.budget.evidence.turn_id, snapshot.effect_digest)] = outcome
        return outcome


def setup(tmp_path):
    from agent_hub.capabilities.review_context import (
        ConcreteModelReviewGate,
        TrustedReviewContextLoader,
    )

    budget, _, _, _ = configured()
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
        routing_decision=route,
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
    calls = []
    real_execute = backend.execute

    async def execute(**kwargs):
        calls.append(kwargs)
        return await real_execute(**kwargs)

    backend.execute = execute
    settings = SimpleNamespace(config=budget.config)

    async def config(tenant):
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
    return SimpleNamespace(**locals())


async def invoke(s, request=None):
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        return await s.harness.invoke(
            s.task.tenant_id, request or s.request, user_id=s.task.actor_id, role=s.task.actor_role
        )


async def test_allow_executes_owned_full_envelope_once(tmp_path):
    s = setup(tmp_path)

    async def mutate():
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
async def test_equivalent_effect_never_reissues_paid_review(tmp_path, status):
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
    assert s.calls == s.permits.consumes == []


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
async def test_changed_live_fences_never_execute(tmp_path, change):
    s = setup(tmp_path)

    async def mutate():
        if change == "policy":
            s.policy._plugin_policy_rules = SimpleNamespace(
                capability_policy_rules=lambda _: (
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
            s.backend._project_verification.root = changed
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
                    **s.runs.record.routing_decision,
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


async def test_static_discovery_never_waits_reviews_or_consumes(tmp_path):
    s = setup(tmp_path)
    # Discovery intentionally lacks worker/budget; it must not request concrete consent.
    result = await s.harness.authorize(
        s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
    )
    assert result.status == "succeeded", result
    assert s.runs.waits == s.reviewer.calls == s.permits.consumes == s.calls == []


@pytest.mark.parametrize("tool", ["workspace.write_text", "workspace.bundle"])
async def test_actual_inbound_zero_review_without_root_or_budget(tmp_path, tool):
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

    async def forbidden_root(_):
        pytest.fail("inbound authorization must not consult Root")

    s.loader._preparation_reader = forbidden_root
    route = {
        **s.runs.record.routing_decision,
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
    assert s.runs.waits == s.reviewer.calls == s.permits.consumes == []


async def test_consume_failure_cannot_downgrade_to_broad_grant(tmp_path):
    s = setup(tmp_path)
    s.runs.broad = True
    s.permits.ok = False
    assert (await invoke(s)).status == "failed"
    assert len(s.permits.consumes) == 1
    assert s.calls == []


async def test_post_consume_revocation_blocks_dispatch(tmp_path):
    s = setup(tmp_path)
    s.permits.hook = s.budget.revoke
    assert (await invoke(s)).status == "failed"
    assert len(s.permits.consumes) == 1
    assert s.calls == []


async def test_missing_worker_context_uses_human_even_with_broad_grant(tmp_path):
    s = setup(tmp_path)
    s.runs.broad = True
    result = await s.harness.invoke(
        s.task.tenant_id, s.request, user_id=s.task.actor_id, role=s.task.actor_role
    )
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert s.reviewer.calls == s.permits.consumes == s.calls == []


@pytest.mark.parametrize("effect", [PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL])
async def test_current_human_only_and_deny_beat_all_broad_shortcuts(tmp_path, effect):
    s = setup(tmp_path)
    route = {**s.runs.record.routing_decision, "conversation_id": "conversation"}
    s.runs.record = replace(s.runs.record, routing_decision=route)
    s.task = s.task.model_copy(update={"routing_decision": route})
    s.runs.broad = True
    s.policy._plugin_policy_rules = SimpleNamespace(
        capability_policy_rules=lambda _: (
            CapabilityRule(
                s.task.tenant_id, Role.OPERATOR, None, "file", "write", "workspace/current", effect
            ),
        )
    )
    assert (await invoke(s)).status == "failed"
    assert len(s.runs.waits) == (effect is PolicyEffect.REQUIRE_APPROVAL)
    assert s.calls == s.permits.consumes == s.reviewer.calls == []


async def test_exact_human_permission_can_override_model_deny(tmp_path):
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
async def test_incomplete_material_requires_human_without_model(tmp_path, missing):
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
    assert s.calls == s.permits.consumes == s.reviewer.calls == []


async def test_cancellation_during_model_cannot_revive_dispatch(tmp_path):
    s = setup(tmp_path)

    async def cancel():
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    s.reviewer.hook = cancel
    with pytest.raises(asyncio.CancelledError):
        await invoke(s)
    assert len(s.reviewer.calls) == 1
    assert s.calls == s.permits.consumes == []


async def test_worker_stream_owns_actual_harness_call(tmp_path):
    from agent_hub.runtime.contracts import RunEvent
    from agent_hub.runtime.worker_execution import worker_runtime_events

    s = setup(tmp_path)
    results = []

    class Runtime:
        async def run(self, context):
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


async def test_revocation_in_final_policy_await_cannot_execute(tmp_path):
    s = setup(tmp_path)
    original = s.policy._current_review_policy
    reads = 0

    async def policy_reader(*args):
        nonlocal reads
        reads += 1
        result = await original(*args)
        # initial + before consume (2 reads) + before dispatch (2 reads).
        if reads == 5:
            s.budget.revoke()
        return result

    s.policy._current_review_policy = policy_reader
    assert (await invoke(s)).status == "failed"
    assert reads == 5
    assert len(s.permits.consumes) == 1
    assert s.calls == []


@pytest.mark.parametrize("authorization", ["model", "exact"])
@pytest.mark.parametrize("change", ["root", "store", "source", "backend"])
async def test_final_authorization_await_cannot_dispatch_stale_source(
    tmp_path, authorization, change
):
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

    async def record_other_entry(**kwargs):
        s.calls.append(kwargs)
        return await other_execute(**kwargs)

    other.execute = record_other_entry

    def mutate():
        nonlocal mutations
        mutations += 1
        if change == "root":
            # Change all real backend Root owners together; this is a genuinely
            # different target, not just a loader-seam digest or mismatched config.
            s.backend._project_verification.root = changed_root
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

    async def policy_reader(*args):
        nonlocal policy_reads
        policy_reads += 1
        result = await original_policy(*args)
        if authorization == "model" and policy_reads == 5:
            await asyncio.sleep(0)
            mutate()
        return result

    async def exact_reader(_request):
        nonlocal exact_reads
        exact_reads += 1
        if exact_reads == 2:
            await asyncio.sleep(0)
            mutate()
        return True

    s.policy._current_review_policy = policy_reader
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


def _point_directory_alias(alias, target):
    """Real OS alias, not a patched resolver or scanner."""
    if alias.is_symlink():
        alias.unlink()
    elif alias.is_junction():
        alias.rmdir()  # Remove only the junction, never recursively remove its target.
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(alias))
        assert alias.is_junction()
    else:
        alias.symlink_to(target, target_is_directory=True)
        assert alias.is_symlink()
    assert alias.resolve(strict=True) == target.resolve(strict=True)


def _alias_workspace(tmp_path):
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


def _workspace_manifest(s, store):
    return tuple(
        (f.path, f.sha256, f.size_bytes)
        for f in store.list_files(s.task.tenant_id, "demo", "session")
    )


@pytest.mark.parametrize("authorization", ["model", "exact"])
@pytest.mark.parametrize("target", ["other", "credentials", "node_modules"])
async def test_final_await_alias_target_change_cannot_dispatch(tmp_path, authorization, target):
    s, store, root, alias = _alias_workspace(tmp_path)
    before = _workspace_manifest(s, store)
    assert [row[0] for row in before] == ["other/target.txt", "public/target.txt"]
    contents = {
        directory: (root / directory / "target.txt").read_bytes()
        for directory in ("public", "other", "credentials", "node_modules")
    }
    original_policy = s.policy._current_review_policy
    policy_reads = exact_reads = mutations = 0

    def retarget():
        nonlocal mutations
        _point_directory_alias(alias, root / target)
        mutations += 1
        assert _workspace_manifest(s, store) == before

    async def policy_reader(*args):
        nonlocal policy_reads
        policy_reads += 1
        result = await original_policy(*args)
        if authorization == "model" and policy_reads == 5:
            await asyncio.sleep(0)
            retarget()
        return result

    async def exact_reader(_request):
        nonlocal exact_reads
        exact_reads += 1
        if exact_reads == 2:
            await asyncio.sleep(0)
            retarget()
        return True

    s.policy._current_review_policy = policy_reader
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
        assert action.to_payload()["normalized_effect"]["target"] == "public/target.txt"
    assert s.runs.waits == []
    assert s.calls == [], result
    assert result.status == "failed"
    assert _workspace_manifest(s, store) == before
    assert {
        directory: (root / directory / "target.txt").read_bytes() for directory in contents
    } == contents


@pytest.mark.parametrize("authorization", ["model", "exact"])
async def test_unchanged_alias_target_uses_owned_path(tmp_path, authorization):
    s, _store, root, _alias = _alias_workspace(tmp_path)

    def mutate_original():
        object.__setattr__(
            s.request, "arguments", {"path": "credentials/target.txt", "content": "changed"}
        )

    if authorization == "model":

        async def reviewed(_snapshot=None):
            mutate_original()

        s.reviewer.hook = reviewed
    else:
        s.runs.exact = True
        reads = 0

        async def exact_reader(_request):
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


async def test_exact_explicit_private_target_is_not_model_authority(tmp_path):
    s, _store, root, _alias = _alias_workspace(tmp_path)
    s.request = replace(
        s.request, arguments={"path": "credentials/target.txt", "content": "manual content"}
    )
    s.runs.exact = True
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert len(s.calls) == 1
    assert (root / "credentials" / "target.txt").read_bytes() == b"manual content"
    assert s.reviewer.calls == s.permits.consumes == s.runs.waits == []


@pytest.mark.parametrize("target", ["credentials", "node_modules"])
async def test_initial_private_alias_still_requires_human(tmp_path, target):
    s, _store, root, alias = _alias_workspace(tmp_path)
    _point_directory_alias(alias, root / target)
    result = await invoke(s)
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert s.calls == s.reviewer.calls == s.permits.consumes == []


@pytest.mark.parametrize("authorization", ["model", "exact"])
@pytest.mark.parametrize("callback_result", ["none", "allow", "deny", "retarget"])
async def test_dispatch_check_await_precedes_final_target_guard(
    tmp_path, authorization, callback_result
):
    s, store, root, alias = _alias_workspace(tmp_path)
    before = _workspace_manifest(s, store)
    callback_calls = 0

    async def dispatch_check():
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
async def test_unknown_external_effect_is_human_only_even_with_broad(tmp_path, tool, sandbox):
    s = setup(tmp_path)
    s.runs.broad = True

    class External:
        def is_available(self, *args):
            return True

        async def invoke(self, **kwargs):
            pytest.fail("uninterpretable external action executed")

    s.harness._mcp_backend = External()
    request = replace(s.request, tool_name=tool, sandbox=sandbox, arguments={"command": "unknown"})
    assert (await invoke(s, request)).status == "failed"
    assert len(s.runs.waits) == 1
    assert s.calls == s.permits.consumes == s.reviewer.calls == []


async def test_root_reader_must_belong_to_the_actual_dispatch_backend(tmp_path):
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
    assert s.reviewer.calls == s.permits.consumes == s.calls == []


async def test_fresh_human_fallback_never_reuses_blanket_pending_scope(tmp_path):
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
    mode, tenant_mode, expected
):
    from tests.unit.capabilities.test_defaults import RecordingApprovalRepository, request
    from tests.unit.capabilities.test_permission_modes import ModeRepository

    runs = ModeRepository(mode)
    assert isinstance(runs, RecordingApprovalRepository)
    runs.record.status = RunStatus.RUNNING

    async def configured_mode(_):
        return tenant_mode

    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), runs, tool_approval_mode=configured_mode
    )
    result = await policy.invoke(
        request("file", "create", "generated/document.generate_docx"), role=Role.OPERATOR
    )
    assert result.status.value == expected


async def test_exact_human_permission_cannot_revive_revoked_run_budget_scope(tmp_path):
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
    assert s.calls == s.reviewer.calls == s.permits.consumes == []
