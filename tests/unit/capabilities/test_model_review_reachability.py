"""Legal full-scope submissions at the real Harness/Root/ledger boundary.

Fixed reviewer and focused ORM DB only: no provider, PG or product-readiness claim.
"""

import asyncio
import copy
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities import review_context as review_module
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.permission_modes import submission_capability_allowed
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.review_repository import ReviewRepository, review_config_sha256
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.db.models import RunReviewIntentRow
from agent_hub.domain.runs import RunStatus
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.physical_budget import _bind_run_budget
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.runs.authorization import _capture_authenticated_input
from tests.unit.capabilities.test_concrete_model_review import FixedLedgerReview, FocusedDB
from tests.unit.harness.test_review_dispatch import _point_directory_alias, invoke, setup


def full_scope(tmp_path, *, preauthorized=False, mode="auto_review", profile="workspace_write"):
    s = setup(tmp_path)
    permissions = ("workspace.read", "command.run")
    if preauthorized:
        permissions = ("workspace.read", "workspace.write", "command.run")
    if profile == "read_only":
        permissions = ("workspace.read",)
    if profile == "none":
        permissions = ()
    body = CreateRunRequest(
        message="Write exactly 'original content' to hello.txt in this project workspace.",
        mode=s.task.mode,
        conversation_id="conversation-reachability",
        project_id="demo",
        workspace_session_id="session",
        sandbox_profile=profile,
        permission_mode=mode,
        requested_permissions=permissions,
        execution_backend="systemd",
        runtime_timeout_seconds=60,
    )
    route = {
        **s.task.routing_decision,
        "conversation_id": body.conversation_id,
        "project_id": body.project_id,
        "workspace_session_id": body.workspace_session_id,
        "permission_mode": body.permission_mode,
        "sandbox_profile": body.sandbox_profile,
        "requested_permissions": list(body.requested_permissions),
        "approval_review_profile": {
            "version": 1,
            "config_sha256": review_config_sha256(s.budget.config),
            "catalog_version": 1,
            "catalog_sha256": "a" * 64,
            "timeout_seconds": 60,
        },
    }
    evidence = _capture_authenticated_input(
        AuthenticatedPrincipal(
            tenant_id=s.task.tenant_id, user_id=s.task.actor_id, role=s.task.actor_role
        ),
        body.message,
        {
            "schema_version": 1,
            "evidence_kind": "submitted_selection_not_grant",
            "operation": "create_run",
            "conversation_id": body.conversation_id,
            "selection": body.model_dump(mode="json", exclude={"message"}),
            "resolved_selection": {
                "sandbox_profile": body.sandbox_profile,
                "permission_mode": body.permission_mode,
                "requested_permissions": list(body.requested_permissions),
                "execution_backend": body.execution_backend,
                "runtime_timeout_seconds": body.runtime_timeout_seconds,
            },
        },
    )
    old = s.budget
    s.budget = type(old)(
        repository=old.repository,
        pool=replace(old._pool, authorization_turn_id=evidence.turn_id),
        evidence=evidence,
        lease=old.lease,
        config=old.config,
        deadline=old._deadline,
        trusted_contexts=old._trusted_contexts,
    )
    s.task = s.task.model_copy(update={"request": body.message, "routing_decision": route})
    s.runs.record = replace(s.runs.record, request=body.message, routing_decision=route)
    s.runs.evidence = evidence
    s.db = FocusedDB(s)
    s.budget.repository = RunAdmissionRepository(s.db)
    s.ledger = ReviewRepository(s.db)
    s.fixed = FixedLedgerReview(s.ledger)
    s.gate.repository, s.gate.service = s.ledger, s.fixed
    s.body = body
    return s


def action(s):
    return CapabilityRequest(
        tenant_id=s.task.tenant_id,
        run_id=s.task.run_id,
        user_id=s.task.actor_id,
        agent_id="main_agent",
        capability="file",
        operation="write",
        resource="workspace/current",
        arguments=dict(s.request.arguments),
        idempotency_key=s.request.idempotency_key,
    )


async def test_unpreauthorized_auto_review_executes_reviewed_owned_action(tmp_path):
    s = full_scope(tmp_path)
    original_route = dict(s.runs.record.routing_decision)
    s.runs.broad = True  # Old broad grants cannot substitute for the specific review.
    assert not await submission_capability_allowed(s.runs, action(s), role=Role.OPERATOR)

    def mutate_caller():
        object.__setattr__(s.request, "arguments", {"path": "wrong.txt", "content": "wrong"})

    s.fixed.hook = mutate_caller
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert s.fixed.calls == len(s.calls) == 1
    assert s.calls[0]["arguments"] == {"path": "hello.txt", "content": "original content"}
    intents = [row for row in s.db.rows if type(row) is RunReviewIntentRow]
    assert len(intents) == 1 and intents[0].consumed
    store = ProjectWorkspaceStore(tmp_path)
    files = store.list_files(s.task.tenant_id, "demo", "session")
    assert [item.path for item in files] == ["hello.txt"]
    target = store.session_root(s.task.tenant_id, "demo", "session") / "hello.txt"
    assert target.read_text(encoding="utf-8") == "original content"
    assert not s.runs.waits
    assert dict(s.runs.record.routing_decision) == original_route
    assert s.db.run.routing_decision == original_route


async def test_unpreauthorized_explicit_ask_waits_without_review(tmp_path):
    s = full_scope(tmp_path)
    s.policy._plugin_policy_rules = SimpleNamespace(
        capability_policy_rules=lambda _: (
            CapabilityRule(
                s.task.tenant_id,
                Role.OPERATOR,
                None,
                "file",
                "write",
                "workspace/current",
                PolicyEffect.REQUIRE_APPROVAL,
            ),
        )
    )
    result = await invoke(s)
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert s.fixed.calls == 0 and not s.calls


@pytest.mark.parametrize("mode", ["default", "auto_review"])
async def test_full_preauthorization_executes_without_review_or_wait(tmp_path, mode):
    s = full_scope(tmp_path, preauthorized=True, mode=mode)
    assert await submission_capability_allowed(s.runs, action(s), role=Role.OPERATOR)
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert len(s.calls) == 1 and s.fixed.calls == 0 and not s.runs.waits
    assert not [row for row in s.db.rows if type(row) is RunReviewIntentRow]


async def test_direct_backend_without_specific_authority_still_rejects_missing_write(tmp_path):
    s = full_scope(tmp_path)
    with pytest.raises(RuntimeCapabilityError, match="workspace write is not authorized"):
        await s.backend.execute(
            tenant_id=s.task.tenant_id,
            run_id=s.task.run_id,
            actor=s.request.actor,
            name=s.request.tool_name,
            arguments=s.request.arguments,
            idempotency_key=s.request.idempotency_key,
        )
    assert s.fixed.calls == 0
    assert not ProjectWorkspaceStore(tmp_path).list_files(s.task.tenant_id, "demo", "session")


@pytest.mark.parametrize(
    "barrier",
    [
        "read_only",
        "restricted",
        "none",
        "deny",
        "actor",
        "role",
        "tenant",
        "status",
    ],
)
async def test_hard_barriers_never_reach_reviewer_or_backend(tmp_path, barrier):
    profile = barrier if barrier in {"read_only", "restricted", "none"} else "workspace_write"
    s = full_scope(
        tmp_path, profile=profile, mode="read_only" if barrier == "read_only" else "auto_review"
    )
    if barrier == "deny":
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
    elif barrier in {"actor", "role", "tenant", "status"}:
        field, value = {
            "actor": ("actor_id", uuid4()),
            "role": ("actor_role", Role.ADMIN),
            "tenant": ("tenant_id", uuid4()),
            "status": ("status", RunStatus.CANCELLED),
        }[barrier]
        s.runs.record = replace(s.runs.record, **{field: value})
    result = await invoke(s)
    assert result.status == "failed"
    assert s.fixed.calls == 0 and not s.calls and not s.runs.waits


@pytest.mark.parametrize("path", ["credentials.json", "node_modules/output.txt"])
async def test_unpreauthorized_sensitive_target_stays_human_only(tmp_path, path):
    s = full_scope(tmp_path)
    s.request = replace(s.request, arguments={"path": path, "content": "original content"})
    result = await invoke(s)
    assert result.status == "failed"
    assert len(s.runs.waits) == 1
    assert s.fixed.calls == 0 and not s.calls


def consumed(s):
    intents = [row for row in s.db.rows if type(row) is RunReviewIntentRow]
    assert len(intents) == 1 and intents[0].consumed


def no_owned_write(s, tmp_path):
    root = ProjectWorkspaceStore(tmp_path).session_root(s.task.tenant_id, "demo", "session")
    assert not (root / "hello.txt").exists()
    assert not (root / "second.txt").exists()


@pytest.mark.parametrize("attack", ["second_file", "content", "backend", "child", "copy", "bool"])
async def test_consumed_authority_cannot_be_transferred_or_forged(tmp_path, attack):
    s = full_scope(tmp_path)
    original = s.backend.execute

    async def intercept(**kwargs):
        if attack == "second_file":
            kwargs["arguments"] = {"path": "second.txt", "content": "original content"}
        elif attack == "content":
            kwargs["arguments"] = {"path": "hello.txt", "content": "unreviewed content"}
        elif attack == "backend":
            from agent_hub.capabilities.runtime import RuntimeCapabilityGateway

            other = RuntimeCapabilityGateway(
                skill_store_dir=tmp_path / "other-skills",
                project_workspace_dir=tmp_path,
                run_repository=s.runs,
                tenant_id=s.task.tenant_id,
            )
            return await other.execute(**kwargs)
        elif attack == "child":
            return await asyncio.create_task(original(**kwargs))
        elif attack in {"copy", "bool"}:
            binding = review_module._WRITE_DISPATCH.get()
            token = review_module._WRITE_DISPATCH.set(
                True if attack == "bool" else copy.copy(binding)
            )
            try:
                return await original(**kwargs)
            finally:
                review_module._WRITE_DISPATCH.reset(token)
        return await original(**kwargs)

    s.backend.execute = intercept
    result = await invoke(s)
    assert result.status == "failed", result
    consumed(s)
    assert s.fixed.calls == 1 and not s.runs.waits
    no_owned_write(s, tmp_path)
    assert "workspace.write" not in s.runs.record.routing_decision["requested_permissions"]


async def test_one_dispatch_cannot_execute_twice_or_survive_context_exit(tmp_path):
    s = full_scope(tmp_path)
    original = s.backend.execute
    captured = []
    rejected = []

    async def intercept(**kwargs):
        captured.append((copy_context(), dict(kwargs)))
        result = await original(**kwargs)
        with pytest.raises(RuntimeCapabilityError):
            await original(**kwargs)
        rejected.append(True)
        return result

    s.backend.execute = intercept
    result = await invoke(s)
    assert result.status == "succeeded", result
    assert rejected == [True] and s.fixed.calls == 1
    consumed(s)
    context, kwargs = captured[0]
    with pytest.raises(RuntimeCapabilityError):
        await context.run(asyncio.create_task, original(**kwargs))
    assert len(s.calls) == 3  # first successful entry, duplicate, copied/revoked context
    assert [
        item.path
        for item in ProjectWorkspaceStore(tmp_path).list_files(
            s.task.tenant_id,
            "demo",
            "session",
        )
    ] == ["hello.txt"]


@pytest.mark.parametrize(
    "change",
    [
        "root",
        "role",
        "lease",
        "auth",
        "revoke",
        "pool",
        "deny",
        "human_ask",
        "config",
    ],
)
async def test_after_consume_invalidation_never_writes_or_reissues(tmp_path, change):
    s = full_scope(tmp_path)
    consume = s.ledger.consume_permit

    async def consume_then_invalidate(**kwargs):
        result = await consume(**kwargs)
        assert result
        if change == "root":
            root = tmp_path / "changed-root"
            root.mkdir()
            s.backend._project_verification.root = root
        elif change == "role":
            s.runs.record = replace(s.runs.record, actor_role=Role.ADMIN)
            s.db.run.actor_role = Role.ADMIN.value
        elif change == "lease":
            s.runs.record = replace(s.runs.record, worker_lease_token=uuid4())
            s.db.run.worker_lease_token = s.runs.record.worker_lease_token
        elif change == "auth":
            s.runs.evidence = replace(s.runs.evidence, turn_id=uuid4())
        elif change == "revoke":
            s.budget._active = False
        elif change == "pool":
            s.db.pool.closed = True
        elif change in {"deny", "human_ask"}:
            effect = PolicyEffect.DENY if change == "deny" else PolicyEffect.REQUIRE_APPROVAL
            s.policy._plugin_policy_rules = SimpleNamespace(
                capability_policy_rules=lambda _: (
                    CapabilityRule(
                        s.task.tenant_id,
                        Role.OPERATOR,
                        None,
                        "file",
                        "write",
                        "workspace/current",
                        effect,
                    ),
                )
            )
        else:
            s.settings.config = replace(s.settings.config, max_reviews_per_run=1)
        return result

    s.ledger.consume_permit = consume_then_invalidate
    result = await invoke(s)
    assert result.status == "failed", result
    consumed(s)
    assert s.fixed.calls == 1 and not s.runs.waits
    no_owned_write(s, tmp_path)


async def test_backend_contract_await_root_change_cannot_use_consumed_authority(tmp_path):
    s = full_scope(tmp_path)
    original = s.backend._original_project_contract

    async def change_root(*args):
        result = await original(*args)
        await asyncio.sleep(0)
        root = tmp_path / "changed-root"
        root.mkdir()
        s.backend._project_verification.root = root
        return result

    s.backend._original_project_contract = change_root
    result = await invoke(s)
    assert result.status == "failed", result
    consumed(s)
    assert s.fixed.calls == 1 and len(s.calls) == 1 and not s.runs.waits
    no_owned_write(s, tmp_path)


@pytest.mark.parametrize("boundary", ["profile", "rules", "no_gate", "contract"])
async def test_pending_escalation_never_enables_old_or_unsupported_paths(tmp_path, boundary):
    s = full_scope(tmp_path)
    if boundary in {"profile", "contract"}:
        route = dict(s.runs.record.routing_decision)
        if boundary == "profile":
            route.pop("approval_review_profile")
        else:
            route["generated_project_contract_version"] = 1
        s.task = s.task.model_copy(update={"routing_decision": route})
        s.runs.record = replace(s.runs.record, routing_decision=route)
    elif boundary == "rules":
        s.settings.config = replace(s.settings.config, engine="rules")
    else:
        s.harness._review_gate = None
    result = await invoke(s)
    assert result.status == "failed"
    assert s.fixed.calls == 0 and not s.calls
    no_owned_write(s, tmp_path)


async def test_pending_static_discovery_never_issues_review_wait_or_write(tmp_path):
    s = full_scope(tmp_path)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        result = await s.harness.authorize(
            s.task.tenant_id,
            s.request,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
        assert result.status == "succeeded"
    assert s.fixed.calls == 0 and not s.calls and not s.runs.waits


async def test_cancel_after_consume_never_writes_or_refunds(tmp_path):
    s = full_scope(tmp_path)
    consume = s.ledger.consume_permit

    async def consume_then_cancel(**kwargs):
        assert await consume(**kwargs)
        asyncio.current_task().cancel()
        return True

    s.ledger.consume_permit = consume_then_cancel
    task = asyncio.create_task(invoke(s))
    with pytest.raises(asyncio.CancelledError):
        await task
    consumed(s)
    assert s.fixed.calls == 1 and not s.calls and not s.runs.waits
    no_owned_write(s, tmp_path)


async def test_current_exact_human_can_override_model_deny_without_new_model_call(tmp_path):
    s = full_scope(tmp_path)
    s.fixed.status = "deny"
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        first = await s.harness.invoke(
            s.task.tenant_id,
            s.request,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
        assert first.status == "failed"
        assert s.fixed.calls == 1 and not s.calls
        s.runs.exact = True
        second = replace(s.request, idempotency_key="current-exact-human")
        result = await s.harness.invoke(
            s.task.tenant_id,
            second,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
    assert result.status == "succeeded", result
    assert s.fixed.calls == 1 and len(s.calls) == 1 and not s.runs.waits


@pytest.mark.parametrize("status", ["allow", "deny", "unknown"])
async def test_pending_effect_occupancy_survives_key_changes(tmp_path, status):
    s = full_scope(tmp_path)
    s.fixed.status = status
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        first = await s.harness.invoke(
            s.task.tenant_id,
            s.request,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
        second = await s.harness.invoke(
            s.task.tenant_id,
            replace(s.request, idempotency_key="same-effect-new-key"),
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
    assert first.status == ("succeeded" if status == "allow" else "failed")
    assert second.status == "failed" and s.fixed.calls == 1
    assert len(s.calls) == (1 if status == "allow" else 0)
    intents = [row for row in s.db.rows if type(row) is RunReviewIntentRow]
    assert len(intents) == 1 and intents[0].consumed is (status == "allow")


@pytest.mark.parametrize("change", ["path", "content"])
async def test_private_backend_write_entry_also_requires_exact_action(tmp_path, change):
    s = full_scope(tmp_path)
    original = s.backend._execute_workspace_write_text

    async def tamper(tenant, run, arguments, *, _dispatch=None):
        changed = dict(arguments)
        changed[change] = "second.txt" if change == "path" else "not reviewed"
        return await original(tenant, run, changed, _dispatch=_dispatch)

    s.backend._execute_workspace_write_text = tamper
    result = await invoke(s)
    assert result.status == "failed", result
    consumed(s)
    no_owned_write(s, tmp_path)


@pytest.mark.parametrize("destination", ["other", "credentials", "node_modules"])
async def test_backend_final_callback_cannot_retarget_manifest_invisible_alias(
    tmp_path, destination
):
    s = full_scope(tmp_path)
    store = s.backend._project_workspace_store
    for directory in ("public", "other", "credentials", "node_modules"):
        store.write_bytes(
            tenant_id=s.task.tenant_id,
            project_id="demo",
            session_id="session",
            relative_path=f"{directory}/target.txt",
            data=b"before",
            mime_type="text/plain",
        )
    root = store.session_root(s.task.tenant_id, "demo", "session")
    alias = root / "alias"
    _point_directory_alias(alias, root / "public")
    original_manifest = [
        (item.path, item.sha256, item.size_bytes)
        for item in store.list_files(
            s.task.tenant_id,
            "demo",
            "session",
        )
    ]
    s.request = replace(
        s.request, arguments={"path": "alias/target.txt", "content": "original content"}
    )
    checks = []

    async def dispatch_check():
        checks.append(True)
        if len(checks) == 2:
            await asyncio.sleep(0)
            _point_directory_alias(alias, root / destination)
            assert [
                (item.path, item.sha256, item.size_bytes)
                for item in store.list_files(
                    s.task.tenant_id,
                    "demo",
                    "session",
                )
            ] == original_manifest

    s.gate.dispatch_check = dispatch_check
    result = await invoke(s)
    assert result.status == "failed", result
    consumed(s)
    assert s.fixed.calls == len(s.calls) == 1 and len(checks) == 2 and not s.runs.waits
    for directory in ("public", "other", "credentials", "node_modules"):
        assert (root / directory / "target.txt").read_bytes() == b"before"


async def test_default_pending_waits_then_only_exact_human_can_dispatch(tmp_path):
    s = full_scope(tmp_path, mode="default")
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        first = await s.harness.invoke(
            s.task.tenant_id,
            s.request,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
        assert first.status == "failed"
        assert len(s.runs.waits) == 1 and s.fixed.calls == 0 and not s.calls
        s.runs.exact = True
        result = await s.harness.invoke(
            s.task.tenant_id,
            s.request,
            user_id=s.task.actor_id,
            role=s.task.actor_role,
        )
    assert result.status == "succeeded", result
    assert len(s.calls) == 1 and s.fixed.calls == 0 and len(s.runs.waits) == 1
    assert not [row for row in s.db.rows if type(row) is RunReviewIntentRow]
    assert "workspace.write" not in s.runs.record.routing_decision["requested_permissions"]


@pytest.mark.parametrize("preauthorized", [False, True])
async def test_explicit_model_eligible_ask_cannot_override_default_manual_choice(
    tmp_path, preauthorized
):
    s = full_scope(tmp_path, mode="default", preauthorized=preauthorized)
    s.policy._plugin_policy_rules = SimpleNamespace(
        capability_policy_rules=lambda _: (
            CapabilityRule(
                s.task.tenant_id,
                Role.OPERATOR,
                None,
                "file",
                "write",
                "workspace/current",
                PolicyEffect.REQUIRE_APPROVAL,
                review_disposition="model_eligible",
            ),
        )
    )
    result = await invoke(s)
    assert result.status == "failed", result
    assert len(s.runs.waits) == 1 and s.fixed.calls == 0 and not s.calls


async def test_loader_cannot_review_manual_choice_even_with_eligible_policy(tmp_path):
    from agent_hub.capabilities.defaults import default_capability_policy

    s = full_scope(tmp_path, mode="default")
    policy = default_capability_policy(
        s.task.tenant_id, require_approval_for_tools=True, concrete_model_review=True
    )
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
        pytest.raises(ValueError),
    ):
        await s.loader.load(action(s), s.task.actor_role, policy, tool_name="workspace.write_text")
    assert s.fixed.calls == 0 and not s.calls
