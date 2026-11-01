"""Real admission chain; only persistence and native execution are local doubles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select

from agent_hub.api.routers.admin import PersistentAdminResourceService
from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
)
from agent_hub.capabilities.defaults import (
    DefaultRuntimeCapabilityPolicyGateway,
    build_runtime_capability_stack,
)
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.tools.registry import CompositeCapabilityManifestSource
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import AdminResourceRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway, _python_recipe_authorization
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.mcp.runtime import RuntimeMcpService
from agent_hub.plugins.runtime import RuntimePluginService
from agent_hub.runs.project_inputs import OriginalProjectApprovalPending
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object
from agent_hub.runtime.worker import (
    _admin_settings_getter_for_tenant,
    _require_tool_approval_from_settings,
    _tool_approval_mode_from_settings,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.capabilities.test_python_project_input_store import python_controls
from tests.unit.capabilities.test_python_project_service import PythonExecutor
from tests.unit.plugins.test_runtime import plugin
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, FakeTransaction


class AdmissionRepository(ExecutableFakeRepository):
    def __init__(self, route: dict[str, object], *, frozen: bool) -> None:
        super().__init__(routing_decision=route)
        self.frozen = frozen
        self.intents: list[dict[str, Any]] = []
        self.approved_scopes: set[str] = set()
        self.pending_scopes: dict[str, str] = {}

    async def get(self, tenant: UUID, run: UUID) -> RunRecord:
        assert (tenant, run) == (self.row.tenant_id, self.row.id)
        record = RunRepository._record(cast(Any, self.row))
        if self.frozen:
            record = replace(record, routing_decision=cast(dict[str, object], _freeze_object(
                record.routing_decision, name="admission routing",
            )))
        return record

    async def is_capability_approval_scope_approved(
        self, tenant: UUID, run: UUID, scope: str,
    ) -> bool:
        assert (tenant, run) == (self.row.tenant_id, self.row.id)
        return scope in self.approved_scopes

    async def begin_capability_approval(self, tenant: UUID, run: UUID, **kwargs: Any) -> None:
        assert (tenant, run) == (self.row.tenant_id, self.row.id)
        self.intents.append(kwargs)
        self.pending_scopes[kwargs["approval_id"]] = kwargs["approval_scope"]
        self.row.status = RunStatus.WAITING_APPROVAL.value

    async def resolve_capability_approval(
        self, tenant: UUID, run: UUID, status: RunStatus, **kwargs: Any,
    ) -> None:
        assert (tenant, run) == (self.row.tenant_id, self.row.id)
        self.approved_scopes.add(self.pending_scopes.pop(kwargs["approval_id"]))
        self.row.status = status.value


class AdmissionSqlSession(ApprovalProjectionSession):
    """Only SQL I/O is replaced, including persisted settings/plugin inventory."""

    def __init__(self) -> None:
        super().__init__()
        self.admin_rows: list[AdminResourceRow] = []
        self.sql_phases: list[str] = []

    async def scalar(self, statement: Any) -> object:
        tables = {getattr(table, "name", None) for table in statement.get_final_froms()}
        if tables == {"agent_hub_runs"}:
            assert self.run.id in statement.compile().params.values()
            return self.run
        return await super().scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        params = statement.compile().params
        if {getattr(table, "name", None) for table in statement.get_final_froms()} == {"agent_hub_run_approvals"}:
            assert self.run.tenant_id in params.values() and self.run.id in params.values()
            self.sql_phases.append("approval_read")
            rows = [row for row in self.approvals.values() if row.status in params.values()]
            return SimpleNamespace(all=lambda: rows)
        return await super().scalars(statement)

    async def execute(self, statement: Any) -> Any:
        params = statement.compile().params
        if isinstance(statement, Select):
            assert {getattr(table, "name", None) for table in statement.get_final_froms()} == {"agent_hub_admin_resources"}
            self.sql_phases.append("admin_read")
            rows = [row for row in self.admin_rows if all(
                params[key] == getattr(row, key.rsplit("_", 1)[0]) for key in params
            )]
            return SimpleNamespace(scalars=lambda: rows,
                                   scalar_one_or_none=lambda: rows[0] if rows else None)
        self.sql_phases.append("approval_write")
        return await super().execute(statement)


async def admission(tmp_path: Path, *, frozen: bool = False) -> tuple[Any, ...]:
    root = tmp_path / "workspaces"
    invocation, source = source_tree(root)
    python_controls(source)
    repository = AdmissionRepository({
        "project_id": invocation.project_id,
        "workspace_session_id": invocation.session_id,
        "sandbox_profile": "workspace_write",
        "execution_backend": "systemd",
        "requested_permissions": [
            "workspace.read", "workspace.write", "network.read", "command.run",
        ],
        "project_input_contract_version": 1,
        "project_input": {
            "schema_version": 1, "kind": "managed_workspace", "editable_paths": ["src"],
        },
    }, frozen=frozen)
    repository.run_id = repository.row.id = invocation.run_id
    repository.row.tenant_id = invocation.tenant_id
    repository.row.actor_role = Role.OPERATOR.value
    token = uuid4()
    await repository.claim_for_execution(
        FakeTransaction(), invocation.run_id, allow_running_recovery=False,
        worker_id="admission-worker", worker_lease_token=token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    context = TaskContext(
        run_id=invocation.run_id, tenant_id=invocation.tenant_id,
        actor_id=repository.row.actor_id, actor_role=Role.OPERATOR, execution_token=token,
        mode=TaskMode.DISPATCH, request=repository.row.request,
        routing_decision=cast(Mapping[str, JsonValue], repository.row.routing_decision),
    )
    runtime = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=root,
        run_repository=repository, project_executor=cast(Any, object()),
    )
    verifier = runtime._project_verification
    assert verifier is not None
    executor = PythonExecutor(root)
    verifier._python.executor = executor
    store = InMemoryApprovalStore()
    approvals = ApprovalService(store)
    policy = DefaultRuntimeCapabilityPolicyGateway(approvals, repository)
    gateway = HarnessToolGateway(runtime, policy_gateway=policy, raise_backend_errors=True)
    preparer = runtime.original_project_preparer(gateway)
    assert preparer is not None
    return preparer, repository, context, executor, runtime, store, approvals


def admission_call(context: TaskContext) -> HarnessToolCallRequest:
    return HarnessToolCallRequest(
        run_id=context.run_id, actor="main_agent", tool_name="project.verify", arguments={},
        approval_required=False, sandbox="workspace_write",
        idempotency_key="original-project-admission-" + context.run_id.hex,
    )


def generic_request(context: TaskContext) -> CapabilityRequest:
    assert context.actor_id is not None
    return CapabilityRequest(
        tenant_id=context.tenant_id, user_id=context.actor_id, run_id=context.run_id,
        agent_id="main_agent", capability="command", operation="run",
        resource="workspace/current/project-verification", arguments={},
        idempotency_key=admission_call(context).idempotency_key,
    )


@pytest.mark.parametrize("frozen", [False, True])
async def test_captured_python_reaches_real_recipe_approval_before_any_original_execution(
    tmp_path: Path, frozen: bool,
) -> None:
    preparer, repository, context, executor, runtime, store, _ = await admission(
        tmp_path, frozen=frozen,
    )
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    state = repository.row.routing_decision["project_input_preparation"]
    assert state["phase"] == "captured" and state["schema_version"] == 2
    assert len(executor.probes) == 1 and not executor.calls
    pending = await store.list_pending()
    assert len(pending) == 1, "real resolver/specialization must reach ApprovalService"
    assert len(repository.intents) == 1
    assert repository.row.status == RunStatus.WAITING_APPROVAL.value
    verifier = runtime._project_verification
    assert verifier is not None
    selected = await verifier._python.select(
        context.tenant_id, context.run_id, "main_agent", phases=("captured",),
    )
    assert selected is not None
    exact = selected.inputs.approval_request(
        user_id=context.actor_id, agent_id="main_agent",
        idempotency_key="original-project-admission-" + context.run_id.hex,
    )
    assert pending[0].request_scope == capability_approval_scope(exact)
    assert repository.intents[0]["approval_scope"] == pending[0].request_scope


@pytest.mark.parametrize("frozen", [False, True])
async def test_actual_approval_resumes_captured_original_once_without_reprobe(
    tmp_path: Path, frozen: bool,
) -> None:
    preparer, repository, context, executor, runtime, store, approvals = await admission(
        tmp_path, frozen=frozen,
    )
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    request = generic_request(context)
    exact = await runtime.project_verification_authorization(request)
    assert exact is not None
    # Same strict reconstruction used by Harness and defaults; no self-reported scope.
    assert _python_recipe_authorization(exact, expected=request) == exact
    pending, = await store.list_pending()
    controls = preparer.root / ".project-contracts" / str(context.tenant_id) / str(context.run_id)
    before = {name: (controls / name).read_bytes() for name in ("baseline.json", "python-recipe.json")}
    await approvals.approve(pending.id, exact, actor_id=context.actor_id)
    resumed = context.model_copy(update={"routing_decision": repository.row.routing_decision})
    prepared = await preparer(resumed)
    state = prepared.routing_decision["project_input_preparation"]
    assert isinstance(state, Mapping) and state["phase"] == "observed"
    assert state["observation"]["status"] == "failed"
    assert len(executor.probes) == 1 and len(executor.calls) == 1
    assert executor.calls[0].install_allowed is True
    assert len(repository.intents) == 1
    assert before == {name: (controls / name).read_bytes() for name in before}
    await preparer(context.model_copy(update={"routing_decision": repository.row.routing_decision}))
    assert len(executor.probes) == 1 and len(executor.calls) == 1


async def test_old_generic_grant_cannot_bypass_actual_python_approval(tmp_path: Path) -> None:
    preparer, repository, context, executor, _, store, _ = await admission(tmp_path)
    repository.approved_scopes.add(capability_approval_scope(generic_request(context)))
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    pending, = await store.list_pending()
    assert pending.request_scope not in repository.approved_scopes
    assert not executor.calls


@pytest.mark.parametrize("bad", ["recipe", "environment", "schema", "extra"])
async def test_captured_marker_drift_fails_before_real_approval_without_generic_fallback(
    tmp_path: Path, bad: str,
) -> None:
    preparer, repository, context, executor, runtime, store, _ = await admission(tmp_path)
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    # Discard only test approval state, never baseline/recipe files or once fences.
    repository.intents.clear()
    state = repository.row.routing_decision["project_input_preparation"]
    marker = state["python_recipe"]
    if bad == "recipe":
        marker["recipe_sha256"] = "0" * 64
    elif bad == "environment":
        marker["environment_input_sha256"] = "0" * 64
    elif bad == "schema":
        marker["schema_version"] = True
    else:
        marker["authority"] = "install"
    pending_before = await store.list_pending()
    gateway = preparer.authorizer
    assert isinstance(gateway, HarnessToolGateway)
    denied = await gateway.authorize(
        context.tenant_id, admission_call(context), user_id=context.actor_id, role=context.actor_role,
    )
    assert denied.status == "failed"
    assert denied.failure_reason == "capability authorization unavailable"
    assert await store.list_pending() == pending_before
    assert not repository.intents and not executor.calls
    assert runtime.is_available(context.tenant_id, "project.verify") is True


async def test_actual_runtime_availability_failure_is_distinct_from_resolver_failure(
    tmp_path: Path,
) -> None:
    preparer, repository, context, executor, runtime, store, approvals = await admission(tmp_path)
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    request = generic_request(context)
    exact = await runtime.project_verification_authorization(request)
    assert exact is not None
    pending, = await store.list_pending()
    await approvals.approve(pending.id, exact, actor_id=context.actor_id)
    runtime._project_executor_configured = False
    denied = await preparer.authorizer.authorize(
        context.tenant_id, admission_call(context), user_id=context.actor_id, role=context.actor_role,
    )
    assert denied.status == "failed" and denied.failure_reason == "tool unavailable"
    assert len(repository.intents) == 1 and not executor.calls


async def test_unavailable_runtime_still_persists_first_exact_python_approval(tmp_path: Path) -> None:
    preparer, repository, context, executor, runtime, store, _ = await admission(tmp_path)
    runtime._project_executor_configured = False
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    pending, = await store.list_pending()
    assert pending.request_scope == repository.intents[0]["approval_scope"]
    assert repository.row.status == RunStatus.WAITING_APPROVAL.value
    assert not executor.calls and len(executor.probes) == 1


async def test_actual_repository_persists_python_admission_before_runtime_or_model(
    tmp_path: Path,
) -> None:
    preparer, memory, context, executor, runtime, _, _ = await admission(tmp_path)

    sql = AdmissionSqlSession()
    sql.run = cast(Any, memory.row)
    repository = RunRepository(cast(Any, sql))
    runtime._run_repository = repository
    verifier = runtime._project_verification
    assert verifier is not None
    verifier.repository = repository
    preparer.repository = repository
    approvals = ApprovalService(InMemoryApprovalStore())
    preparer.authorizer = HarnessToolGateway(
        runtime, policy_gateway=DefaultRuntimeCapabilityPolicyGateway(approvals, repository),
        raise_backend_errors=True,
    )
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    record = await repository.get(context.tenant_id, context.run_id)
    assert record.status is RunStatus.WAITING_APPROVAL
    assert len(sql.approvals) == 1
    pending, = sql.approvals.values()
    assert pending.status == "pending"
    scope = pending.payload["approval_scope"]
    public = pending.payload["public_request"]
    assert isinstance(scope, str) and "/project-verification/python/" in scope
    assert isinstance(public, dict) and public["kind"] == "approval.requested"
    assert not executor.calls and len(executor.probes) == 1
    assert record.worker_lease_token is None
    assert record.routing_decision is not None
    assert record.routing_decision["approval_checkpoint_required"] is False


@pytest.mark.parametrize("required,mode", [(False, "auto_review"), (True, "ask")])
async def test_production_stack_with_persisted_settings_and_plugin_rules_reaches_pending_sql(
    tmp_path: Path, required: bool, mode: str,
) -> None:
    preparer, memory, context, executor, _, _, _ = await admission(tmp_path)
    sql = AdmissionSqlSession()
    sql.run = cast(Any, memory.row)
    sql.admin_rows = [
        AdminResourceRow(tenant_id=context.tenant_id, kind="setting", resource_id="system",
                         payload={"require_approval_for_tools": required, "tool_approval_mode": mode}),
        AdminResourceRow(tenant_id=context.tenant_id, kind="plugin", resource_id="command-plugin",
                         payload=plugin("command-plugin", adapter="http_json",
                             endpoint_url="https://example.invalid", permission_class="command.run",
                             policy_effect="deny").model_dump(mode="json")),
    ]
    repository = RunRepository(cast(Any, sql))
    admin = PersistentAdminResourceService(
        config_service=cast(Any, object()), secret_service=cast(Any, object()),
        tenant_id=uuid4(), actor_id=uuid4(), session_factory=cast(Any, sql),
    )
    plugins = RuntimePluginService(tenant_id=context.tenant_id, admin_service=admin)
    mcp = RuntimeMcpService(tenant_id=context.tenant_id, admin_service=admin,
                           run_repository=repository)
    await plugins.start()
    await mcp.start()
    assert plugins.capability_policy_rules(context.tenant_id), "exercise nonempty real extra rules"
    composite = CompositeCapabilityManifestSource(
        (mcp.capability_manifest_source(), plugins.capability_manifest_source()),
    )
    stack = build_runtime_capability_stack(
        tenant_id=context.tenant_id, run_repository=repository,
        skill_store_dir=tmp_path / "production-skills", workspace_root=None,
        project_workspace_dir=preparer.root,
        require_approval_for_tools=lambda tenant: _require_tool_approval_from_settings(
            _admin_settings_getter_for_tenant(admin, tenant)),
        tool_approval_mode=lambda tenant: _tool_approval_mode_from_settings(
            _admin_settings_getter_for_tenant(admin, tenant)),
        tool_registry=composite, plugin_backend=plugins, mcp_backend=mcp,
    )
    runtime = stack.runtime_gateway
    # No native execution: expose the injected executor consistently on Windows too.
    runtime._project_executor_configured = True
    assert runtime._project_verification is not None
    runtime._project_verification._python.executor = executor
    production = runtime.original_project_preparer(stack.harness_tool_gateway)
    assert production is not None
    assert runtime.is_available(context.tenant_id, "project.verify") is True
    with pytest.raises(RuntimeError, match="authorization"):
        await production(context)
    assert sql.run.status == RunStatus.WAITING_APPROVAL.value
    assert sql.sql_phases.count("approval_read") == 2
    assert sql.sql_phases.count("approval_write") == 1
    assert sql.sql_phases.count("admin_read") >= 5
    pending, = sql.approvals.values()
    public = pending.payload["public_request"]
    assert pending.status == "pending"
    assert isinstance(public, dict) and public["kind"] == "approval.requested"
    assert len(executor.probes) == 1 and not executor.calls


@pytest.mark.parametrize("frozen", [False, True])
async def test_actual_install_unknown_keeps_write_ahead_state_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen: bool,
) -> None:
    preparer, repository, context, executor, runtime, store, approvals = await admission(
        tmp_path, frozen=frozen,
    )
    with pytest.raises(OriginalProjectApprovalPending):
        await preparer(context)
    assert repository.row.routing_decision["project_input_preparation"]["phase"] == "captured"
    assert not executor.calls
    pending, = await store.list_pending()
    exact = await runtime.project_verification_authorization(generic_request(context))
    assert exact is not None
    await approvals.approve(pending.id, exact, actor_id=context.actor_id)
    dispatched: list[PythonProjectInvocation] = []

    async def unknown_after_dispatch(invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse:
        state = repository.row.routing_decision["project_input_preparation"]
        assert state["phase"] == "executing" and state["execution_id"] == invocation.execution_id
        assert invocation.install_allowed is True
        dispatched.append(invocation)
        raise TimeoutError("native reply unavailable after dispatch")

    monkeypatch.setattr(executor, "run", unknown_after_dispatch)
    resumed = context.model_copy(update={"routing_decision": repository.row.routing_decision})
    with pytest.raises(ProjectVerificationError, match="^python_project_service_unavailable$"):
        await preparer(resumed)
    assert len(dispatched) == 1 and len(executor.probes) == 1
    assert repository.row.routing_decision["project_input_preparation"]["phase"] == "executing"
    with pytest.raises(RuntimeError, match="original project preparation is unavailable or changed"):
        await preparer(context.model_copy(update={"routing_decision": repository.row.routing_decision}))
    assert len(dispatched) == 1 and len(executor.probes) == 1
    assert len(repository.intents) == 1


@pytest.mark.parametrize("status", [CapabilityStatus.DENIED, CapabilityStatus.ALLOWED],
                         ids=["current-policy-denied", "allowed-without-persisted-exact-approval"])
async def test_install_cannot_leave_capture_without_policy_and_actual_exact_approval(
    tmp_path: Path, status: CapabilityStatus,
) -> None:
    preparer, repository, context, executor, _, store, _ = await admission(tmp_path)

    class DecisionWithoutReceipt(DefaultRuntimeCapabilityPolicyGateway):
        async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
            return CapabilityResult(status, request.run_id, reason="capability denied")

    preparer.authorizer._policy_gateway = DecisionWithoutReceipt(ApprovalService(store), repository)
    with pytest.raises(RuntimeError, match="authorization required or denied"):
        await preparer(context)
    assert repository.row.routing_decision["project_input_preparation"]["phase"] == "captured"
    assert not executor.calls and len(executor.probes) == 1
    assert not await store.list_pending() and not repository.intents
