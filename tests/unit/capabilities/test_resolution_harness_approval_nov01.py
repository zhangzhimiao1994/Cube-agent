"""Real Harness/policy/DAO composition; only SQL and Root transports are local doubles."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
    capability_approval_scope,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.gateway import (
    ApprovalReviewDecision,
    ApprovalReviewOutcome,
    CapabilityResult,
    CapabilityStatus,
)
from agent_hub.capabilities.generated_project_service import GeneratedProjectService
from agent_hub.capabilities.python_input_preparation import dynamic_python_pins
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import RunApprovalRow, RunCheckpointRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import RuntimeCheckpoint, TaskContext
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

_fixtures = importlib.import_module("tests.unit.capabilities.test_dynamic_python_resolution_flow")
offline_network = _fixtures.offline_network


class RecordingPolicy(DefaultRuntimeCapabilityPolicyGateway):
    requests: list[CapabilityRequest]

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        self.requests.append(request)
        return await super().invoke(request, role=role)


@pytest.fixture
def dao() -> Iterator[Any]:
    module = importlib.import_module("tests.unit.runs.test_python_project_approval_projection")
    yield from module.dao.__wrapped__()


@dataclass
class Case:
    dao: Any
    service: GeneratedProjectService
    context: TaskContext
    harness: HarnessToolGateway
    policy: RecordingPolicy
    approvals: ApprovalService
    calls: list[str]
    native: dict[str, Any]
    request: HarnessToolCallRequest


async def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
                     *, mode: TaskMode = TaskMode.DIRECT, permission: str = "default") -> Case:
    service, _old_repo, context, _ = _fixtures.generated_dynamic_case(tmp_path, monkeypatch)
    assert isinstance(service, GeneratedProjectService) and isinstance(context, TaskContext)
    route = dict(context.routing_decision)
    route.update(permission_mode=permission, conversation_id="resolution-conversation")
    if permission == "legacy_auto_review":
        route.pop("permission_mode")
    row = dao.run
    for name, value in (("id", context.run_id), ("tenant_id", context.tenant_id),
                        ("actor_id", context.actor_id), ("actor_role", "operator"),
                        ("mode", mode.value), ("status", "running"), ("request", context.request),
                        ("routing_decision", json.loads(json.dumps(route, default=dict))),
                        ("worker_id", "resolution-worker"), ("worker_lease_token", context.execution_token),
                        ("worker_lease_expires_at", datetime.now(UTC) + timedelta(minutes=3)),
                        ("created_at", datetime.now(UTC))):
        setattr(row, name, value)
    context = context.model_copy(update={"mode": mode, "routing_decision": route})
    factory = dao.repository._session_factory

    class Session:
        """Reuse the existing SQL boundary and hydrate its real checkpoint query."""

        def __init__(self) -> None:
            self.session = factory()

        async def __aenter__(self) -> Self:
            await self.session.__aenter__()
            return self

        async def __aexit__(self, *args: object) -> None:
            await self.session.__aexit__(*args)

        def begin(self) -> Session:
            return self

        def __getattr__(self, key: str) -> Any:
            return getattr(self.session, key)

        async def scalar(self, statement: Any) -> Any:
            if statement.column_descriptions[0].get("expr") is RunCheckpointRow:
                record = dao.connection.execute(statement).mappings().first()
                return None if record is None else RunCheckpointRow(payload=record["payload"])
            if {table.name for table in statement.get_final_froms()} == {"agent_hub_run_approvals"}:
                params = statement.compile().params
                for approval in dao.approvals:
                    if all(getattr(approval, key.rsplit("_", 1)[0]) == value for key, value in params.items()):
                        return (approval if statement.column_descriptions[0].get("expr") is RunApprovalRow
                                else approval.id)
                return None
            return await self.session.scalar(statement)

        async def scalars(self, statement: Any) -> Any:
            assert {table.name for table in statement.get_final_froms()} == {'agent_hub_run_approvals'}
            params = statement.compile().params
            rows = [approval for approval in dao.approvals
                    if all(getattr(approval, key.rsplit('_', 1)[0]) == value for key, value in params.items())]
            return SimpleNamespace(all=lambda: rows)

        async def execute(self, statement: Any) -> Any:
            if getattr(statement, 'table', None) is not None and statement.table.name == 'agent_hub_run_approvals':
                params = statement.compile().params
                if statement.is_insert:
                    existing = next((item for item in dao.approvals if item.approval_id == params['approval_id']), None)
                    if existing is None:
                        existing = RunApprovalRow(**{key: params[key] for key in (
                            'id', 'tenant_id', 'run_id', 'approval_id', 'action', 'status', 'payload')},
                            created_at=datetime.now(UTC))
                        dao.approvals.append(existing)
                    else:
                        assert existing.approval_id.startswith('auto_review_')
                        existing.payload, existing.status = params['payload'], params['status']
                else:
                    resolution = next(value for value in params.values()
                        if isinstance(value, dict) and 'public_resolution' in value)
                    existing = next(item for item in dao.approvals
                                    if item.approval_id == resolution['public_resolution']['approval_id'])
                    assert existing.status == 'pending'
                    existing.status = resolution['status']
                    existing.payload = {**existing.payload, **resolution}
                return SimpleNamespace(scalar_one_or_none=lambda: existing.id)
            return await self.session.execute(statement)

    dao.repository._session_factory = Session
    service.repository = dao.repository
    native: dict[str, Any] = {"descriptor": await _fixtures.descriptor_with_build_inputs(), "saved": None,
                              "original": None, "plan": None, "published": False}
    calls: list[str] = []

    async def transport(request: Any) -> ResolutionStatus:
        from agent_hub.skills.sandbox.project import project_source_fingerprint

        assert await project_source_fingerprint(service.root, request.invocation) == request.claim.planning.draft.source_sha256
        calls.append(request.action)
        saved = native["saved"]
        if request.action == "inspect_python_resolution":
            if request.expected_plan_sha256 is not None:
                plan = native['plan']
                assert plan is not None and request.expected_plan_sha256 == plan.sha256
                if native.get('publication_outcome_unknown'):
                    return ResolutionStatus(state='unknown', key=request.key)
                return ResolutionStatus(state='published' if native['published'] else 'frozen',
                    key=request.key, descriptor=saved.to_payload(), plan=plan)
            return ResolutionStatus(state="absent" if saved is None else "resolved", key=request.key,
                                    descriptor=None if saved is None else saved.to_payload())
        assert "generated_project_preparation" not in row.routing_decision
        if request.action == 'freeze_python_resolution':
            from agent_hub.skills.sandbox.python_resolution_store import freeze_plan

            native['plan'] = freeze_plan(saved, original=native['original'], current=request.claim,
                                         overlay_sha256='e' * 64)
            return ResolutionStatus(state='frozen', key=request.key, descriptor=saved.to_payload(), plan=native['plan'])
        if request.action == 'publish_python_resolution':
            assert not native['published'] and request.expected_plan_sha256 == native['plan'].sha256
            if native.get('lose_publication_response'):
                native['publication_outcome_unknown'] = True
                raise TimeoutError('fixture lost publication response')
            native['published'] = True
            return ResolutionStatus(state='published', key=request.key, descriptor=saved.to_payload(), plan=native['plan'])
        assert request.action == "resolve_python_inputs" and saved is None
        native['original'] = request.claim
        native["saved"] = replace(native["descriptor"], scope=request.claim.resolution_scope(), needs=request.needs)
        return ResolutionStatus(state="resolved", key=request.key, descriptor=native["saved"].to_payload())

    wire = importlib.import_module("agent_hub.skills.sandbox.python_resolution_worker")
    monkeypatch.setattr(wire, "request_resolution", transport)
    runtime = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", project_workspace_dir=service.root,
        run_repository=dao.repository, project_executor=cast(Any, object()))
    assert runtime._project_verification is not None
    runtime._project_verification.generated = service
    approvals = ApprovalService(InMemoryApprovalStore())

    async def approval_mode(_tenant: UUID) -> str:
        return "auto_review"

    class LocalReviewer:
        async def review(self, request: CapabilityRequest) -> ApprovalReviewDecision:
            assert request.resource.startswith('workspace/current/project-resolution/python/')
            return ApprovalReviewDecision(ApprovalReviewOutcome.ALLOW, 'auto_review', 'fixture concrete policy review')

    policy = RecordingPolicy(approvals, dao.repository, tool_approval_mode=approval_mode,
        approval_reviewer=LocalReviewer() if permission == 'legacy_auto_review' else None)
    policy.requests = []
    harness = HarnessToolGateway(runtime, policy_gateway=policy)
    request = HarnessToolCallRequest(run_id=context.run_id, actor="engineer", tool_name="project.verify",
        arguments={}, sandbox="workspace_write", approval_required=False, idempotency_key="resolution-verify-once")
    return Case(dao, service, context, harness, policy, approvals, calls, native, request)


async def invoke(case: Case) -> HarnessToolCallResult:
    with bind_execution_authority(case.context, worker_id="resolution-worker", actor="engineer"):
        return await case.harness.invoke(case.context.tenant_id, case.request,
                                         user_id=case.context.actor_id, role=Role.OPERATOR)


@pytest.mark.parametrize("mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize("permission", ["default", "auto_review"])
async def test_resolution_reaches_real_policy_as_specific_pending_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any, mode: TaskMode, permission: str,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao, mode=mode, permission=permission)
    result = await invoke(case)
    assert result.failure_reason == "capability requires approval", result
    assert isinstance(result.payload.get("approval_id"), str)
    assert dao.run.status == "waiting_approval" and len(dao.approvals) == 1
    assert dao.approvals[0].payload["approval_scope"].endswith(
        "resource=workspace/current/project-resolution/python/" + dao.approvals[0].payload["approval_scope"].split("/")[-1])
    assert "generated_project_preparation" not in dao.run.routing_decision
    assert "python_metadata_preparation" not in dao.run.routing_decision
    assert case.calls == ["inspect_python_resolution"]
    assert case.native["saved"] is None


def save_checkpoint(case: Case, approval_id: str) -> None:
    # Real checkpoint gate and SQL recovery query remain enabled. This is an
    # approval-boundary test, not a claim that a model continuation was replayed.
    checkpoint = RuntimeCheckpoint(id=uuid4(), tenant_id=case.context.tenant_id, run_id=case.context.run_id,
        mode=TaskMode.DIRECT, runtime_type="direct_action", runtime_version="2",
        state={"phase": "concrete_action", "completed": False, "approval_id": approval_id,
            "tool_name": "project.verify", "context_sha256": "a" * 64, "elapsed_seconds": 1,
            "timeout_seconds": 120, "reference": {"version": 1, "content_sha256": "b" * 64,
                "total_bytes": 1, "segments": ({"id": str(uuid4()), "sha256": "c" * 64},)}})
    case.dao.connection.execute(text("INSERT INTO agent_hub_run_checkpoints "
        "(run_id,sequence,id,tenant_id,runtime_type,runtime_version,mode,state,state_sha256,payload) "
        "VALUES (:run,:sequence,:id,:tenant,:kind,:version,:mode,:state,:sha,:payload)"),
        {"run": case.context.run_id.hex, "sequence": 100 + case.dao.run.version,
            "id": checkpoint.id.hex, "tenant": case.context.tenant_id.hex,
            "kind": checkpoint.runtime_type, "version": checkpoint.runtime_version, "mode": checkpoint.mode.value,
            "state": json.dumps(checkpoint.to_payload()["state"]), "sha": checkpoint.state_sha256,
            "payload": json.dumps(checkpoint.to_payload())})


@pytest.mark.parametrize("permission", ["default", "auto_review"])
async def test_once_approval_is_found_by_resolution_and_same_run_reaches_typed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any, permission: str,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao, permission=permission)
    result = await invoke(case)
    assert result.failure_reason == "capability requires approval", result
    approval_id = cast(str, result.payload["approval_id"])
    save_checkpoint(case, approval_id)
    before_permissions = list(dao.run.routing_decision["requested_permissions"])
    version = dao.run.version
    assert await dao.repository.capability_approval_ready(case.context.tenant_id, case.context.run_id,
                                                        actor_id=case.context.actor_id)
    approved = await dao.repository.approve_capability_and_enqueue(tenant_id=case.context.tenant_id,
        run_id=case.context.run_id, actor_id=case.context.actor_id, approval_id=approval_id, version=version)
    assert approved.status is RunStatus.QUEUED and len(dao.outbox) == 1
    # A duplicate API receipt must never enqueue a second consumption.
    await dao.repository.approve_capability_and_enqueue(tenant_id=case.context.tenant_id,
        run_id=case.context.run_id, actor_id=case.context.actor_id, approval_id=approval_id, version=version)
    assert len(dao.outbox) == 1
    dao.run.status = "running"
    dao.run.worker_id, dao.run.worker_lease_token = "resolution-worker", case.context.execution_token
    dao.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=3)
    scope = dao.approvals[0].payload["approval_scope"]
    assert await dao.repository.is_capability_approval_scope_approved(
        case.context.tenant_id, case.context.run_id, scope)
    original_route = dao.run.routing_decision
    for key, value in [('workspace_session_id', 'another-session'), ('project_id', 'another-project'),
                       ('requested_permissions', ['workspace.read']), ('sandbox_profile', 'read_only')]:
        dao.run.routing_decision = {**original_route, key: value}
        assert not await dao.repository.is_capability_approval_scope_approved(
            case.context.tenant_id, case.context.run_id, scope)
    dao.run.routing_decision = original_route
    for tenant, run in [(uuid4(), case.context.run_id), (case.context.tenant_id, uuid4())]:
        assert not await dao.repository.is_capability_approval_scope_approved(tenant, run, scope)
    actor = dao.run.actor_id
    dao.run.actor_id = uuid4()
    assert not await dao.repository.is_capability_approval_scope_approved(case.context.tenant_id, case.context.run_id, scope)
    dao.run.actor_id = actor
    assert not await dao.repository.is_capability_approval_scope_approved(
        case.context.tenant_id, case.context.run_id, scope[:-1] + ("0" if scope[-1] != "0" else "1"))
    from agent_hub.skills.sandbox.python_registry_protocol import digest

    selected = case.policy.requests[0]
    for key in ('source_sha256', 'root_sha256', 'catalog_sha256'):
        changed_arguments = {**selected.arguments, key: 'f' * 64}
        changed = selected.model_copy(update={'arguments': changed_arguments,
            'resource': 'workspace/current/project-resolution/python/' + digest(changed_arguments)})
        changed_scope = capability_approval_scope(changed)
        assert changed_scope is not None
        assert not await dao.repository.is_capability_approval_scope_approved(
            case.context.tenant_id, case.context.run_id, changed_scope)
    with bind_execution_authority(case.context, worker_id="resolution-worker", actor="engineer"):
        decision = await case.policy.invoke(case.policy.requests[0], role=Role.OPERATOR)
    assert decision.status is CapabilityStatus.ALLOWED
    result = await invoke(case)
    assert result.failure_reason == "generated project draft requires correction", result
    rejection = cast(Mapping[str, Any], result.payload["generated_draft_rejection"])
    assert rejection["execution_started"] is False
    assert case.calls.count("resolve_python_inputs") == 1 and case.native["saved"] is not None
    assert "generated_project_preparation" not in dao.run.routing_decision
    assert dao.run.routing_decision["requested_permissions"] == before_permissions
    assert len(dao.approvals) == 1 and len(dao.outbox) == 1
    # Re-observation can inspect registered data, never resolve a second time.
    await invoke(case)
    assert case.calls.count("resolve_python_inputs") == 1


@pytest.mark.parametrize("change", ["hash", "actor", "run", "tenant", "unknown", "install", "extra"])
async def test_scope_rejects_unbound_resolution_proposals(change: str) -> None:
    fixture = importlib.import_module("tests.unit.capabilities.test_python_resolution_preparation")
    from agent_hub.capabilities.python_resolution_preparation import resolution_action
    from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, digest
    environment = importlib.import_module("tests.unit.skills.test_python_registry_read").environment

    live = fixture.live.__wrapped__()
    original = importlib.import_module("tests.unit.skills.test_python_resolution_store").claim()
    request = resolution_action(original.model_copy(update={"planning": live.planning}),
                                (DependencyNeed("demo>=1", "a" * 64),), environment())
    good = capability_approval_scope(request)
    assert good is not None and request.resource in good
    if change == "hash":
        changed = request.model_copy(update={"resource": request.resource[:-1] + ("f" if request.resource[-1] != 'f' else '0')})
    elif change in {"actor", "run", "tenant"}:
        key, value = {"actor": ("agent_id", "neighbor"), "run": ("run_id", uuid4()),
                      "tenant": ("tenant_id", uuid4())}[change]
        changed = request.model_copy(update={key: value})
        assert capability_approval_scope(changed) != good
        return
    else:
        arguments = dict(request.arguments)
        if change == "install":
            arguments["install_allowed"] = True
        elif change == "extra":
            arguments["worker_lease_token"] = "PRIVATE"
        changed = request.model_copy(update={"arguments": arguments,
            "resource": ("workspace/current/unknown/python/" if change == "unknown" else
                         "workspace/current/project-resolution/python/") + digest(arguments)})
    assert capability_approval_scope(changed) is None


@pytest.mark.parametrize('defect', ['install', 'actor', 'run', 'idempotency', 'source', 'hash', 'unknown_scope'])
async def test_malformed_resolution_has_typed_pre_dispatch_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any, defect: str,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao)
    assert (await invoke(case)).failure_reason == "capability requires approval"
    selected = case.policy.requests[0]
    malformed = selected.model_copy(update={"arguments": {**selected.arguments, "install_allowed": True}})
    if defect in {'actor', 'run', 'idempotency'}:
        field, value = {'actor': ('agent_id', 'neighbor'), 'run': ('run_id', uuid4()),
                        'idempotency': ('idempotency_key', 'unrelated-call')}[defect]
        malformed = selected.model_copy(update={field: value})
    elif defect == 'source':
        malformed = selected.model_copy(update={'arguments': {**selected.arguments, 'source_sha256': 'f' * 64}})
    elif defect in {'hash', 'unknown_scope'}:
        malformed = selected.model_copy(update={'resource': ('workspace/current/project-resolution/unknown/'
            if defect == 'unknown_scope' else 'workspace/current/project-resolution/python/') + 'f' * 64})

    async def invalid(_request: CapabilityRequest) -> CapabilityRequest:
        return malformed

    backend = cast(RuntimeCapabilityGateway, case.harness._backend)
    monkeypatch.setattr(backend, "project_verification_authorization", invalid)
    before = list(case.calls)
    result = await invoke(case)
    assert result.failure_reason == "project verification scope is not authorized"
    assert result.payload["verification_authorization_failure"] == {
        "schema_version": 1, "stage": "before_dispatch", "code": "resolution_proposal_invalid",
        "action_dispatched": False, "retryable": False,
    }
    assert case.calls == before and case.calls.count("resolve_python_inputs") == 0


async def test_real_automatic_review_allow_is_readable_by_resolution_without_manual_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao, permission='legacy_auto_review')
    result = await invoke(case)
    assert result.failure_reason == 'generated project draft requires correction', result
    assert case.calls.count('resolve_python_inputs') == 1
    assert len(dao.approvals) == 1 and dao.approvals[0].payload['reviewer'] == 'auto_review'
    assert 'decision_scope' not in dao.approvals[0].payload
    assert not dao.outbox and dao.run.status == 'running'
    assert 'generated_project_preparation' not in dao.run.routing_decision


async def approve_pending(case: Case, result: HarnessToolCallResult) -> None:
    assert result.failure_reason == 'capability requires approval', result
    approval = cast(str, result.payload['approval_id'])
    save_checkpoint(case, approval)
    await case.dao.repository.approve_capability_and_enqueue(tenant_id=case.context.tenant_id,
        run_id=case.context.run_id, actor_id=case.context.actor_id,
        approval_id=approval, version=case.dao.run.version)
    case.dao.run.status = 'running'
    case.dao.run.worker_id, case.dao.run.worker_lease_token = 'resolution-worker', case.context.execution_token
    case.dao.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=3)


@pytest.mark.parametrize('permission', ['default', 'legacy_auto_review'])
@pytest.mark.parametrize('unknown', [False, True], ids=['confirmed', 'unknown_no_replay'])
async def test_resolution_publication_and_real_prepare_share_one_run_without_early_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any, permission: str, unknown: bool,
) -> None:
    from agent_hub.capabilities.python_dependency_closure import check_python_dependency_closure
    from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
    from agent_hub.skills.sandbox.project import project_source_fingerprint
    from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
    from agent_hub.skills.sandbox.python_dependency_protocol import (
        PythonDependencyClosureObservation,
    )
    from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyResponse

    case = await configured(tmp_path, monkeypatch, dao, permission=permission)
    original_inspector = case.service._dependency_inspector
    project_path = importlib.import_module('tests.unit.capabilities.test_scoped_read').project_path()
    work = case.service.root / project_path

    async def inspect(invocation: Any, source: str) -> PythonDependencyClosureObservation:
        if not case.native['published']:
            return await original_inspector(invocation, source)
        assert await project_source_fingerprint(case.service.root, invocation) == source
        manifest, lock = (work / 'pyproject.toml').read_bytes(), (work / 'requirements.lock').read_bytes()
        declaration = parse_python_project_declaration(manifest, lock, max_control_bytes=256 * 1024,
                                                       max_requirements=128)
        records = tuple(w.metadata for w in case.native['saved'].wheels)
        data = importlib.import_module('tests.unit.skills.test_python_dependency_protocol').observation()
        index = PythonDependencyMetadataIndex(data['catalog_sha256'], records)
        data.update(source_sha256=source, manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            lock_sha256=hashlib.sha256(lock).hexdigest(), metadata_index_sha256=index.sha256,
            closure=check_python_dependency_closure(declaration.requirements, index.records,
                                                    data['target_environment']).to_payload())
        return PythonDependencyClosureObservation.model_validate(data, strict=True)

    class OfflineSupply:
        async def plan(self, invocation: Any) -> PythonInputSupplyResponse:
            # Stop at the NEXT independent recipe/input gate, after real capture.
            # No native executor, installer or verification is simulated as passed.
            assert 'generated_project_preparation' in dao.run.routing_decision
            return PythonInputSupplyResponse(action='plan_python_project_inputs',
                execution_id=invocation.execution_id, ok=False, error='input_supply_unavailable')

    case.service._dependency_inspector = inspect
    backend = cast(RuntimeCapabilityGateway, case.harness._backend)
    assert backend._project_verification is not None
    backend._project_verification._python_generated.supply_client = cast(Any, OfflineSupply())
    first = await invoke(case)
    if permission == 'default':
        assert case.calls.count('resolve_python_inputs') == 0
        await approve_pending(case, first)
        first = await invoke(case)
    assert first.failure_reason == 'generated project draft requires correction', first
    assert case.native['saved'] is not None and 'generated_project_preparation' not in dao.run.routing_decision
    # Model correction uses the actual resolved distribution pins; test project only.
    (work / 'pyproject.toml').write_bytes((work / 'pyproject.toml').read_bytes().replace(b'demo-dep>=1', b'demo-dep==1.0'))
    (work / 'requirements.lock').write_bytes(dynamic_python_pins(
        case.native['saved'].to_payload()).encode())
    case.native['lose_publication_response'] = unknown
    second = await invoke(case)
    if permission == 'default':
        assert case.calls.count('publish_python_resolution') == 0
        assert 'generated_project_preparation' not in dao.run.routing_decision
        assert len(dao.approvals) == 2
        assert dao.approvals[0].payload['approval_scope'] != dao.approvals[1].payload['approval_scope']
        await approve_pending(case, second)
        second = await invoke(case)
    assert case.calls.count('resolve_python_inputs') == case.calls.count('publish_python_resolution') == 1
    if unknown:
        assert second.status == 'failed' and second.failure_reason != 'generated project draft requires correction'
        assert 'generated_project_preparation' not in dao.run.routing_decision
        repeated = await invoke(case)
        assert repeated.status == 'failed' and case.calls.count('publish_python_resolution') == 1
        assert case.calls.count('resolve_python_inputs') == 1
        assert 'generated_project_preparation' not in dao.run.routing_decision
        return
    assert second.failure_reason == 'input_supply_unavailable', second
    assert dao.run.routing_decision['generated_project_preparation']['phase'] == 'captured'
    assert dao.run.id == case.context.run_id and dao.run.status == 'running'
    assert len(dao.approvals) == 2
    assert not backend._project_delivery_receipts


async def test_direct_caller_keeps_pre_dispatch_scope_failure_out_of_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
) -> None:
    from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
    from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic

    case = await configured(tmp_path, monkeypatch, dao)
    assert (await invoke(case)).failure_reason == 'capability requires approval'
    selected = case.policy.requests[0]

    async def invalid(_request: CapabilityRequest) -> CapabilityRequest:
        return selected.model_copy(update={'arguments': {**selected.arguments, 'install_allowed': True}})

    monkeypatch.setattr(case.harness._backend, 'project_verification_authorization', invalid)
    runtime = DirectRuntime(cast(Any, object()), logical_model='fixture', harness_tool_gateway=case.harness)
    with pytest.raises(RuntimeExecutionError) as caught:
        await runtime._verify_workspace(case.context, known_files={}, attempt=1,
                                       deadline=asyncio.get_running_loop().time() + 30)
    assert getattr(caught.value, 'failure_code', None) == 'scope_denied'
    diagnostic = safe_runtime_failure_diagnostic(caught.value)
    assert diagnostic['error_code'] == 'project_verification.scope_denied'
    assert diagnostic['retryable'] is False and 'outcome_unconfirmed' not in str(diagnostic)
    assert case.calls.count('resolve_python_inputs') == 0


async def test_sdk_manual_once_cannot_be_consumed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
) -> None:
    from agent_hub.capabilities.approvals import ApprovalMismatch

    case = await configured(tmp_path, monkeypatch, dao)
    pending = await invoke(case)
    assert pending.failure_reason == 'capability requires approval'
    approval_id = cast(str, pending.payload['approval_id'])
    save_checkpoint(case, approval_id)
    request = case.policy.requests[0]
    assert case.context.actor_id is not None
    approved = await case.approvals.approve(approval_id, request, actor_id=case.context.actor_id)
    assert approved.status == 'approved' and dao.approvals[0].payload['decision_scope'] == 'once'
    with pytest.raises(ApprovalMismatch):
        await case.approvals.approve(approval_id, request, actor_id=case.context.actor_id)
    assert not dao.outbox
    dao.run.worker_id, dao.run.worker_lease_token = 'resolution-worker', case.context.execution_token
    dao.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=3)
    assert (await invoke(case)).failure_reason == 'generated project draft requires correction'
    assert case.calls.count('resolve_python_inputs') == 1


def deny_exact(case: Case, resource: str) -> None:
    from agent_hub.capabilities.policy import CapabilityRule
    from agent_hub.capabilities.types import PolicyEffect

    rule = CapabilityRule(case.context.tenant_id, Role.OPERATOR, 'engineer', 'command', 'run',
                          resource, PolicyEffect.DENY)
    case.policy._plugin_policy_rules = SimpleNamespace(capability_policy_rules=lambda _tenant: (rule,))


async def test_current_exact_deny_after_resolution_approval_precedes_root_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao)
    pending = await invoke(case)
    resource = case.policy.requests[0].resource
    await approve_pending(case, pending)
    deny_exact(case, resource)
    result = await invoke(case)
    assert case.calls.count('resolve_python_inputs') == 0, result
    assert result.failure_reason == 'capability denied'
    assert 'generated_project_preparation' not in dao.run.routing_decision


async def test_current_exact_deny_after_publication_approval_precedes_root_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao)
    await approve_pending(case, await invoke(case))
    assert (await invoke(case)).failure_reason == 'generated project draft requires correction'
    project_path = importlib.import_module('tests.unit.capabilities.test_scoped_read').project_path()
    work = case.service.root / project_path
    (work / 'pyproject.toml').write_bytes((work / 'pyproject.toml').read_bytes().replace(b'demo-dep>=1', b'demo-dep==1.0'))
    (work / 'requirements.lock').write_bytes(dynamic_python_pins(
        case.native['saved'].to_payload()).encode())
    pending = await invoke(case)
    assert case.calls.count('publish_python_resolution') == 0
    resource = case.policy.requests[-1].resource
    await approve_pending(case, pending)
    deny_exact(case, resource)
    result = await invoke(case)
    assert case.calls.count('publish_python_resolution') == 0, result
    assert result.failure_reason == 'capability denied'
    assert 'generated_project_preparation' not in dao.run.routing_decision


async def test_policy_binding_covers_backend_prepare_and_rechecks_after_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao: Any,
) -> None:
    case = await configured(tmp_path, monkeypatch, dao)
    pending = await invoke(case)
    selected = case.policy.requests[0]
    await approve_pending(case, pending)

    async def projection_only(_request: CapabilityRequest) -> CapabilityRequest:
        return selected

    backend = cast(RuntimeCapabilityGateway, case.harness._backend)
    monkeypatch.setattr(backend, 'project_verification_authorization', projection_only)
    original_policy = case.policy.invoke
    revoked = False

    async def revoke_after_allowed(request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        nonlocal revoked
        result = await original_policy(request, role=role)
        if result.status is CapabilityStatus.ALLOWED and not revoked:
            revoked = True
            deny_exact(case, selected.resource)
        return result

    monkeypatch.setattr(case.policy, 'invoke', revoke_after_allowed)
    result = await invoke(case)
    assert revoked
    assert case.calls.count('resolve_python_inputs') == 0, result
    assert result.failure_reason == 'capability denied'


@pytest.mark.parametrize('revoked', [False, True], ids=['inherited_task', 'scope_exited'])
async def test_resolution_policy_callback_cannot_be_borrowed_or_outlive_call(revoked: bool) -> None:
    from agent_hub.capabilities.python_resolution_authorization import (
        InvalidResolutionAuthorization,
        bind_resolution_policy_authorizer,
        resolution_policy_allowed,
        resolution_policy_scope,
    )
    from agent_hub.capabilities.python_resolution_preparation import resolution_action
    from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed

    original = importlib.import_module('tests.unit.skills.test_python_resolution_store').claim()
    environment = importlib.import_module('tests.unit.skills.test_python_registry_read').environment()
    action = resolution_action(original, (DependencyNeed('demo>=1', 'a' * 64),), environment)
    expected = action.model_copy(update={'idempotency_key': 'owned-harness-call'})
    called = 0
    ready = asyncio.Event()

    async def authorize(_request: CapabilityRequest) -> bool:
        nonlocal called
        called += 1
        return True

    async def borrowed() -> None:
        await ready.wait()
        with pytest.raises(InvalidResolutionAuthorization):
            await resolution_policy_allowed(action)

    with resolution_policy_scope(expected):
        bind_resolution_policy_authorizer(expected, authorize)
        assert await resolution_policy_allowed(action) is True
        child = asyncio.create_task(borrowed())
        if not revoked:
            ready.set()
            await child
    if revoked:
        ready.set()
        await child
    assert called == 1 and await resolution_policy_allowed(action) is None
