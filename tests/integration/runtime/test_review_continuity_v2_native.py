"""Prepared real-PG/v2/four-native-mode continuity; collection is not execution.

The protocol qualification file and provider replies are synthetic fixtures.
PG transactions, activation reader, pool seal, Redis capacity, native adapters,
SDK physical accounting and UNKNOWN storage are production implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Unpack
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.review_context import _ConcreteReviewCall, _Fence
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.service import ConfigService
from agent_hub.db.models import RunAdmissionAttemptRow, RunApprovalRow, RunReviewIntentRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.tool_gateway import _capability_request
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_budget import _identity
from agent_hub.models.run_admission_repository import (
    AttemptSnapshot,
    PoolSnapshot,
    RunAdmissionRepository,
    _WorkerLease,
)
from agent_hub.models.types import Deployment
from agent_hub.runs.authorization import _persist_authenticated_input
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime import review_assembly as assembly
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    _canonical_json_bytes,
    _freeze_object,
)
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime, _deployments
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414 - dedicated explicit PG fixture
)
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import prepare
from tests.integration.models.test_run_admission import (
    storage as storage,  # noqa: PLC0414 - real scoped PG fixture/cleanup
)
from tests.integration.runtime.review_actions_typed_fixture import InvokeArguments
from tests.integration.runtime.review_mode_import_closure_typed_fixture import (
    NativeCase,
    NativeRuntime,
    RuntimeArguments,
    TokenPool,
    WireAttempt,
)
from tests.integration.runtime.test_model_review_action import (
    _assert_accounting_receipt,
    _DirectWriteAudit,
    _install_note_wire,
)
from tests.integration.runtime.test_model_review_modes import (
    own_redis as own_redis,  # noqa: PLC0414 - owned Unix socket Redis, no production service
)
from tests.integration.runtime.test_model_review_modes import (
    owned_crewai_storage as owned_crewai_storage,  # noqa: PLC0414 - preserve per-test native storage
)
from tests.integration.runtime.test_model_review_modes_helper import (
    FACTORIES,
    FINAL_TEXT,
    FixedWire,
    FixtureSecrets,
    assert_wire_receipts,
    build_task_context,
    capture_submission_input,
    fixture_platform,
    submission_request,
)
from tests.unit.harness.review_typed_fixture import present
from tests.unit.runs.project_auto_repair_typed_fixture import mapping
from tests.unit.runtime.test_plain_file_action_helper import plain_note_submission

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope='module')]


@dataclass
class V2Native:
    case: NativeCase
    path: Path
    body: bytes
    expires_at: datetime
    pool: PoolSnapshot
    seal: tuple[str, datetime]
    unknown: AttemptSnapshot
    note: tuple[str, str] | None


def assert_same_routing_json(actual: Mapping[str, object], expected: Mapping[str, JsonValue]) -> None:
    """Compare every JSON field, permitting only the contract's container thaw.

    No default serializer/string coercion or ignored route fields: freeze rejects
    non-JSON values and canonical bytes still distinguish bool/int and int/float.
    """
    frozen = _freeze_object(actual, name='persisted routing snapshot')
    assert set(frozen) == set(expected)
    for key in expected:
        assert _canonical_json_bytes({key: frozen[key]}) == _canonical_json_bytes({key: expected[key]}), key
    assert _canonical_json_bytes(frozen) == _canonical_json_bytes(expected)


def assert_action_wire_receipts(
    completed: Sequence[RunAdmissionAttemptRow], wire: Mapping[UUID, WireAttempt],
    pool: PoolSnapshot, unknown: AttemptSnapshot, reviews: Sequence[RunReviewIntentRow],
) -> None:
    """Account primary and reviewer IO separately; retain the UNKNOWN debit."""
    assert unknown.state == 'unknown' and unknown.token_hold == 11
    assert unknown.attempt_id not in wire
    assert len(completed) == len(wire) and {r.attempt_id for r in completed} == set(wire)
    assert all(r.purpose in {'primary', 'review'} for r in completed)
    primary = [r for r in completed if r.purpose == 'primary']
    reviewer = [r for r in completed if r.purpose == 'review']
    primary_wire = {key: row for key, row in wire.items() if row['purpose'] == 'primary'}
    review_wire = {key: row for key, row in wire.items() if row['purpose'] == 'review'}
    assert len(primary_wire) + len(review_wire) == len(wire)
    primary_tokens = sum(row['usage']['total_tokens'] for row in primary_wire.values())
    # Retain the chat helper's full primary identity/receipt/hash assertions,
    # but never pass a genuine review attempt into its primary-only contract.
    assert_wire_receipts(primary, primary_wire, TokenPool(token_charged=primary_tokens))
    assert {r.attempt_id for r in reviewer} == set(review_wire) == {r.attempt_id for r in reviews}
    review_tokens = 0
    review_cost = Decimal(0)
    for row in reviewer:
        sent = review_wire[row.attempt_id]
        assert row.purpose == sent['purpose'] == 'review'
        assert row.state == 'completed' and row.physical_transport_id is not None
        assert row.request_sha256 == sent['request_sha256'] and row.deployment == sent['deployment']
        assert str(row.physical_transport_id) == sent['physical_transport_id']
        _assert_accounting_receipt(row, sent)
        cost = Decimal(sent['usage']['cost_usd']['decimal'])
        assert sent['usage']['total_tokens'] == 5 and cost == Decimal('0.000008')
        review_tokens += sent['usage']['total_tokens']
        review_cost += cost
        intent = next(r for r in reviews if r.attempt_id == row.attempt_id)
        assert (intent.tenant_id, intent.run_id, intent.authorization_turn_id) == (
            pool.tenant_id, pool.run_id, pool.authorization_turn_id)
        assert intent.request_sha256 == row.request_sha256
        assert intent.physical_identity == row.physical_transport_id
    assert pool.review_token_charged == review_tokens
    assert pool.review_count_charged == len(reviewer)
    assert pool.review_cost_charged == review_cost
    assert pool.token_charged == 11 + primary_tokens + review_tokens


async def fresh_v2_native_case(storage: Storage, tmp_path: Path,
                               mode: TaskMode, redis: Redis, *, action: bool = False) -> V2Native:
    factory = storage[0]
    _, binding, _ = await prepare(storage, open_now=False)
    repository = RunRepository(factory)
    platform = fixture_platform()
    config = ApprovalReviewConfig(engine='rules_then_model', reviewer_logical_model='reviewer',
        max_input_tokens=1000000, max_output_tokens=512, max_cost_usd_per_run='100.00')

    class Configs(ConfigService):
        def __init__(self) -> None:
            pass

        async def get_current(self, tenant_id: UUID) -> ConfigRevision:
            assert tenant_id == binding['tenant_id']
            return ConfigRevision(uuid4(), tenant_id, 1, ConfigStatus.PUBLISHED,
                platform.model_dump(mode='json'), None, datetime.now(UTC))

    async def current_config(tenant_id: UUID) -> ApprovalReviewConfig:
        assert tenant_id == binding['tenant_id']
        return config

    capacities: list[CapacityPool] = []
    namespace = 'continuity-v2:' + uuid4().hex + ':'

    async def capacity(tenant_id: UUID, deployments: Sequence[Deployment]) -> CapacityPool:
        async def fingerprint(reference: str) -> str:
            return await FixtureSecrets().fingerprint(tenant_id, reference)

        controller = CapacityPool(redis, deployments=deployments,
            fingerprint_resolver=fingerprint, key_prefix=namespace)
        capacities.append(controller)
        return controller

    expiry = datetime.now(UTC) + timedelta(minutes=10)
    document = {'schema_version': 1, 'valid_until': expiry.isoformat(),
        'billing_contracts': [{'tenant_id': str(binding['tenant_id']),
            'catalog_sha256': assembly.catalog_fingerprint(platform), 'deployment_id': name,
            'evidence_reference': 'fixture://v2-native-contract-not-production',
            'contract': 'single_physical_all_billed_tokens_v1'} for name in ('primary_1', 'reviewer_1')],
        'worker_verification': {'profile_version': 1,
            'source_sha256': assembly.runtime_source_fingerprint(),
            'modes': ['direct', 'dispatch', 'discuss', 'hybrid'],
            'checks': sorted(assembly.REQUIRED_VERIFICATION_CHECKS),
            'evidence_reference': 'fixture://v2-native-composition-not-production'}}
    body = json.dumps(document).encode()
    path = tmp_path / 'original-activation.json'
    path.write_bytes(body)
    path.chmod(0o600)
    digest = hashlib.sha256(body).hexdigest()
    wire = FixedWire()
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))

    def new_broker() -> assembly.ReviewAssemblyBroker:
        # No tuple loader, static contract fallback or in-memory prior snapshot.
        return assembly.ReviewAssemblyBroker(config_service=Configs(), config_getter=current_config,
            run_repository=repository, session_factory=factory, secret_service=FixtureSecrets(),
            capacity_factory=capacity, transport=transport,
            evidence_source=assembly.ServerReviewEvidenceSource(path, expected_sha256=digest))

    broker = new_broker()
    admission = RunAdmissionRepository(factory)
    assert await admission.get_pool(binding['tenant_id'], binding['run_id']) is None
    assert await admission.get_continuity_seal(binding['tenant_id'], binding['run_id']) is None
    profile = await broker.submission_profile(binding['tenant_id'], timeout_seconds=3600)
    assert mapping(profile['approval_review_profile'])['version'] == 2
    assert mapping(profile['approval_review_profile'])['activation'] == (
        assembly.ServerReviewEvidenceSource(path, expected_sha256=digest).current().binding())
    note: tuple[str, str] | None = None
    if action:
        assert mode is TaskMode.DIRECT
        request, path_name, content = plain_note_submission()
        note = (path_name, content)
        assert request.requested_permissions == ('workspace.read', 'command.run')
    else:
        request = submission_request(mode)
    selection = workspace_selection(project_id=request.project_id, session_id=request.workspace_session_id,
        sandbox_profile=request.sandbox_profile, requested_permissions=request.requested_permissions,
        permission_mode=request.permission_mode)
    route = {**selection.routing_payload(), 'conversation_id': request.conversation_id,
        'execution_backend': request.execution_backend, 'selected_agent_ids': list(request.agent_ids),
        'runtime_token_absolute_tokens': 10000000, 'runtime_plan_token_budget': 10000000,
        'runtime_timeout_seconds': 3600, **profile}
    async with factory() as session, session.begin():
        row = present(await session.get(RunRow, binding['run_id']))
        row.request, row.mode, row.routing_decision = request.message, mode.value, route
        evidence = capture_submission_input(row, request)
        await _persist_authenticated_input(session, row, evidence)
        # Do not treat fixture-populated lease columns as a worker claim. Cross
        # the real claim transaction before admission/any physical invocation.
        row.status = 'queued'
        repository.clear_worker_lease(row)
        await session.flush()
        lease = _WorkerLease('native-v2-worker', uuid4())
        claimed = await repository.claim_for_execution(session, binding['run_id'],
            allow_running_recovery=False, worker_id=lease.worker_id,
            worker_lease_token=lease.token,
            worker_lease_expires_at=datetime.now(UTC) + timedelta(hours=1))
        assert isinstance(claimed, tuple)
        claimed_row, checkpoint = claimed
        assert checkpoint is None and claimed_row.status == 'running'
        assert claimed_row.worker_id == lease.worker_id and claimed_row.worker_lease_token == lease.token
        binding = {**binding, 'lease': lease}
    record = await repository.get(binding['tenant_id'], binding['run_id'])
    context = build_task_context(record, binding, request, route).model_copy(update={'timeout_seconds': 3600})
    execution = present(await broker.prepare(context, worker_id=binding['lease'].worker_id))
    seal = present(await admission.get_continuity_seal(context.tenant_id, context.run_id))
    assert datetime.fromisoformat(str(mapping(profile['approval_review_profile'])['prepared_at'])) <= seal[1] < expiry
    primary = next(d for d in _deployments(platform) if d.logical_model == 'primary')
    deployment = _identity(primary)
    unknown_id = uuid4()
    await admission.reserve(tenant_id=context.tenant_id, run_id=context.run_id,
        authorization_turn_id=execution.budget.evidence.turn_id, lease=execution.budget.lease,
        config_sha256=execution.budget._pool.config_sha256, attempt_id=unknown_id,
        purpose='primary', request_sha256='a' * 64, deployment=deployment,
        token_hold=11, review_cost_hold=None)
    assert await admission.mark_issued(tenant_id=context.tenant_id, run_id=context.run_id,
        authorization_turn_id=execution.budget.evidence.turn_id, lease=execution.budget.lease,
        config_sha256=execution.budget._pool.config_sha256, attempt_id=unknown_id,
        physical_transport_id=uuid4())
    unknown = await admission.finish(tenant_id=context.tenant_id, run_id=context.run_id,
        attempt_id=unknown_id, lease=execution.budget.lease, deployment=deployment, outcome='unknown', usage=None)
    assert unknown.state == 'unknown'
    pool = present(await admission.get_pool(context.tenant_id, context.run_id))
    assert pool.token_charged == 11 and pool.original_token_grant == 10000000
    assert pool.original_deadline > expiry
    execution.budget.revoke()

    # Cold assembly reads the persisted v2 and original PG seal, not that budget.
    broker = new_broker()
    artifacts = InMemoryArtifactRepository()
    stack = assembly.assemble_review_capability_stack(broker, build_runtime_capability_stack,
        tenant_id=context.tenant_id, run_repository=repository, skill_store_dir=tmp_path / 'skills',
        workspace_root=tmp_path, generated_artifact_dir=tmp_path / 'generated',
        project_workspace_dir=tmp_path, artifact_repository=artifacts)
    arguments: RuntimeArguments = {'config_service': Configs(), 'secret_service': FixtureSecrets(),
        'capacity_factory': capacity, 'transport': transport, 'capability_gateway': stack.runtime_gateway,
        'harness_tool_gateway': stack.harness_tool_gateway, 'artifact_repository': artifacts}
    runtime: NativeRuntime
    if mode is TaskMode.DIRECT:
        runtime = ConfigBackedDirectRuntime(**arguments, action_capability_gateway=stack.runtime_gateway,
                                           action_harness_tool_gateway=stack.harness_tool_gateway)
    else:
        runtime = FACTORIES[mode](**arguments)
    wire.runtime = runtime
    case = NativeCase(factory, repository, broker, context, binding, wire, runtime, stack,
                      capacities, tmp_path, config, redis, request)
    return V2Native(case, path, body, expiry, pool, seal, unknown, note)


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_real_pg_v2_native_modes_continue_original_balance_and_unknown_after_review_expiry(
    storage: Storage, own_redis: Redis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    prepared = await fresh_v2_native_case(storage, tmp_path, mode, own_redis)
    case = prepared.case
    routing_snapshot = _freeze_object(case.context.routing_decision, name='original routing snapshot')
    expired = prepared.expires_at + timedelta(seconds=1)

    class ExpiredQualification(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ExpiredQualification:
            return cls.fromtimestamp(expired.timestamp(), tz=tz)

    monkeypatch.setattr(assembly, 'datetime', ExpiredQualification)
    events = []
    try:
        async with asyncio.timeout(90):
            async with worker_runtime_events(case.runtime, case.context,
                worker_id=case.binding['lease'].worker_id, review_broker=case.broker) as stream:
                events = [event async for event in stream]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(e.kind, e.reason) for e in events]
        assert any(e.artifact is not None and FINAL_TEXT in json.dumps(dict(e.artifact.content), default=dict)
                   for e in events if e.kind is EventKind.ARTIFACT_CREATED)
        required = {TaskMode.DIRECT: {'DirectRuntime'}, TaskMode.DISPATCH: {'CrewDispatchRuntime'},
            TaskMode.DISCUSS: {'AutoGenDiscussionRuntime'},
            TaskMode.HYBRID: {'HybridRuntime', 'DirectRuntime', 'CrewDispatchRuntime', 'AutoGenDiscussionRuntime'}}[mode]
        assert required <= case.wire.classes
        admission = RunAdmissionRepository(case.factory)
        pool = present(await admission.get_pool(case.context.tenant_id, case.context.run_id))
        assert await admission.get_continuity_seal(case.context.tenant_id, case.context.run_id) == prepared.seal
        assert pool.original_deadline == prepared.pool.original_deadline
        assert pool.authorization_turn_id == prepared.pool.authorization_turn_id
        assert pool.original_token_grant == prepared.pool.original_token_grant
        assert pool.token_charged == 11 + len(case.wire.calls) * 5
        assert pool.tokens_remaining == 10000000 - 11 - len(case.wire.calls) * 5
        assert pool.review_count_charged == 0 and case.wire.reviewer_calls == 0
        assert await admission.get_attempt(case.context.tenant_id, case.context.run_id,
                                           prepared.unknown.attempt_id) == prepared.unknown
        async with case.factory() as session:
            attempts = list(await session.scalars(select(RunAdmissionAttemptRow).where(
                RunAdmissionAttemptRow.run_id == case.context.run_id)))
            reviews = list(await session.scalars(select(RunReviewIntentRow).where(
                RunReviewIntentRow.run_id == case.context.run_id)))
        completed = [r for r in attempts if r.attempt_id != prepared.unknown.attempt_id]
        assert len(attempts) == len(case.wire.calls) + 1 and all(r.state == 'completed' for r in completed)
        assert_wire_receipts(completed, case.wire.attempts, TokenPool(token_charged=pool.token_charged - 11))
        assert not reviews and prepared.path.read_bytes() == prepared.body
        persisted = await case.repository.get(case.context.tenant_id, case.context.run_id)
        assert mapping(present(persisted.routing_decision)['approval_review_profile'])['version'] == 2
        assert case.wire.calls and all(budget is case.wire.budgets[0] for budget in case.wire.budgets)
        assert not case.wire.budgets[0]._active and all(body.closed for body in case.wire.bodies)
        for capacity in case.capacities:
            for scope in ('fixture-primary', 'fixture-reviewer'):
                assert await case.redis.zcard(capacity._keys(scope)['leases']) == 0
        assert_same_routing_json(present(persisted.routing_decision), routing_snapshot)
    finally:
        case.wire.release.set()
        await case.runtime.cancel_run_owned(case.context.run_id, present(case.context.execution_token))


def expire_qualification(prepared: V2Native, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance qualification time only; never the PG/task/lease budget clocks."""
    expired = prepared.expires_at + timedelta(seconds=1)

    class ExpiredQualification(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ExpiredQualification:
            return cls.fromtimestamp(expired.timestamp(), tz=tz)

    monkeypatch.setattr(assembly, 'datetime', ExpiredQualification)


@pytest.mark.parametrize('authorization', ['manual_expired', 'model_fresh', 'model_expires_during_reply'])
async def test_real_pg_v2_native_direct_root_write_preserves_exact_vs_model_freshness(
    storage: Storage, own_redis: Redis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    authorization: str,
) -> None:
    """Actual Direct/worker/Harness/Root/PG; manual decision and provider are fixtures.

    The manual fixture persists one exact decision at the observed request, not
    a broad grant or a replacement authorization getter. This is not an HTTP
    approval-endpoint test. Model cases use the real review service/consume_once.
    """
    prepared = await fresh_v2_native_case(storage, tmp_path, TaskMode.DIRECT, own_redis, action=True)
    case = prepared.case
    path, content = present(prepared.note)
    assert case.body.requested_permissions == ('workspace.read', 'command.run')
    proposals = _install_note_wire(case, case.body, path, content)
    text_for = case.wire.text_for

    def expire_after_actual_review_reply(payload: Mapping[str, object]) -> str:
        before = case.wire.reviewer_calls
        text = text_for(payload)
        if authorization == 'model_expires_during_reply' and case.wire.reviewer_calls > before:
            # The SDK has reached the real reviewer response while fresh. Every
            # later final dispatch check must reject this now-expired evidence.
            expire_qualification(prepared, monkeypatch)
        return text

    monkeypatch.setattr(case.wire, 'text_for', expire_after_actual_review_reply)
    harness = case.stack.harness_tool_gateway
    assert isinstance(harness, assembly._ProfiledHarness)
    gate = present(harness._fresh._review_gate)
    model_check = present(gate.model_dispatch_check)
    model_checks = 0

    async def observed_model_check() -> None:
        nonlocal model_checks
        model_checks += 1
        await model_check()

    monkeypatch.setattr(gate, 'model_dispatch_check', observed_model_check)
    prepared_writes: list[_ConcreteReviewCall] = []
    prepare_write = _ConcreteReviewCall._prepare_write_dispatch

    def observed_prepare_write(self: _ConcreteReviewCall, request: CapabilityRequest, fence: _Fence) -> None:
        prepare_write(self, request, fence)
        prepared_writes.append(self)

    monkeypatch.setattr(_ConcreteReviewCall, '_prepare_write_dispatch', observed_prepare_write)
    results: list[HarnessToolCallResult] = []
    original_invoke = harness.invoke
    manual_fingerprints: list[str] = []

    async def invoke(tenant_id: UUID, request: HarnessToolCallRequest,
                     **kwargs: Unpack[InvokeArguments]) -> HarnessToolCallResult:
        if request.tool_name == 'workspace.write_text':
            assert not results, 'native Direct must not replay a refused or completed write'
            assert dict(request.arguments) == {'path': path, 'content': content}
            if authorization == 'manual_expired':
                capability = _capability_request(tenant_id, present(case.context.actor_id), request)
                fingerprint = fingerprint_capability_request(capability)
                async with case.factory() as session, session.begin():
                    row = present(await session.get(RunRow, case.context.run_id))
                    assert row.worker_id == case.binding['lease'].worker_id
                    assert row.worker_lease_token == case.context.execution_token
                    session.add(RunApprovalRow(id=uuid4(), tenant_id=tenant_id,
                        run_id=case.context.run_id, action='capability_tool', status='approved',
                        approval_id='fixture_manual_exact', created_at=datetime.now(UTC), payload={
                            'approval_fingerprint': fingerprint,
                            'approval_scope': capability_approval_scope(capability),
                            'approval_origin': 'builtin', 'decision_scope': 'once'}))
                manual_fingerprints.append(fingerprint)
                expire_qualification(prepared, monkeypatch)
        result = await original_invoke(tenant_id, request, **kwargs)
        if request.tool_name == 'workspace.write_text':
            results.append(result)
        return result

    monkeypatch.setattr(harness, 'invoke', invoke)
    root = ProjectWorkspaceStore(tmp_path).session_root(case.context.tenant_id,
        present(case.body.project_id), present(case.body.workspace_session_id))
    audit = _DirectWriteAudit(root / path)
    sys.addaudithook(audit)
    events: list[RunEvent] = []
    failure: RuntimeExecutionError | None = None
    try:
        try:
            async with asyncio.timeout(90):
                async with worker_runtime_events(case.runtime, case.context,
                    worker_id=case.binding['lease'].worker_id, review_broker=case.broker) as stream:
                    events = [event async for event in stream]
        except RuntimeExecutionError as error:
            failure = error
        assert len(proposals) == len(results) == 1
        if authorization == 'model_expires_during_reply':
            assert results[0].status != 'succeeded'
            assert audit.count == 0 and not (root / path).exists()
            assert case.wire.reviewer_calls == 1
        else:
            assert failure is None and events[-1].kind is EventKind.RUNTIME_COMPLETED
            assert results[0].status == 'succeeded' and results[0].failure_reason is None
            assert audit.count == 1 and (root / path).read_bytes() == content.encode()
            assert mapping(results[0].payload['file'])['sha256'] == hashlib.sha256(content.encode()).hexdigest()
            assert len(prepared_writes) == 1 and prepared_writes[0]._budget is not None
            assert prepared_writes[0]._model_permit_consumed is (authorization == 'model_fresh')
            if authorization == 'manual_expired':
                assert model_checks == 0 and len(manual_fingerprints) == 1
                assert case.wire.reviewer_calls == 0
            else:
                assert model_checks > 0 and case.wire.reviewer_calls == 1
        assert 'ActionApprovedDirectRuntime' in case.wire.classes
        admission = RunAdmissionRepository(case.factory)
        pool = present(await admission.get_pool(case.context.tenant_id, case.context.run_id))
        assert pool.original_deadline == prepared.pool.original_deadline
        assert pool.original_token_grant == prepared.pool.original_token_grant
        assert await admission.get_continuity_seal(case.context.tenant_id, case.context.run_id) == prepared.seal
        assert await admission.get_attempt(case.context.tenant_id, case.context.run_id,
                                           prepared.unknown.attempt_id) == prepared.unknown
        assert pool.token_charged == 11 + 5 * len(case.wire.calls)
        async with case.factory() as session:
            attempts = list(await session.scalars(select(RunAdmissionAttemptRow).where(
                RunAdmissionAttemptRow.run_id == case.context.run_id)))
            reviews = list(await session.scalars(select(RunReviewIntentRow).where(
                RunReviewIntentRow.run_id == case.context.run_id)))
            approvals = list(await session.scalars(select(RunApprovalRow).where(
                RunApprovalRow.run_id == case.context.run_id)))
        completed = [r for r in attempts if r.attempt_id != prepared.unknown.attempt_id]
        assert_action_wire_receipts(completed, case.wire.attempts, pool, prepared.unknown, reviews)
        assert all(r.worker_id == case.binding['lease'].worker_id
                   and r.worker_lease_token == case.context.execution_token for r in completed)
        if authorization == 'manual_expired':
            assert not reviews and len(approvals) == 1
            assert approvals[0].payload['approval_fingerprint'] == manual_fingerprints[0]
            assert approvals[0].payload['decision_scope'] == 'once'
        elif authorization == 'model_fresh':
            assert len(reviews) == 1 and reviews[0].status == 'allow' and reviews[0].consumed
            assert pool.review_count_charged == 1 and pool.review_token_charged == 5
        else:
            assert all(not r.consumed for r in reviews)
        assert prepared.path.read_bytes() == prepared.body
        assert all(body.closed for body in case.wire.bodies)
    finally:
        audit.active = False
        case.wire.release.set()
        await case.runtime.cancel_run_owned(case.context.run_id, present(case.context.execution_token))


@pytest.mark.parametrize('defect', ['removed', 'replaced'])
async def test_real_pg_v2_native_proof_revocation_stops_original_worker_before_any_send(
    storage: Storage, own_redis: Redis, tmp_path: Path, defect: str,
) -> None:
    prepared = await fresh_v2_native_case(storage, tmp_path, TaskMode.DIRECT, own_redis)
    case = prepared.case
    if defect == 'removed':
        prepared.path.unlink()
    else:
        replacement = prepared.path.with_suffix('.replacement')
        replacement.write_bytes(prepared.body + b'\n')
        replacement.chmod(0o600)
        replacement.replace(prepared.path)
    admission = RunAdmissionRepository(case.factory)
    before = present(await admission.get_pool(case.context.tenant_id, case.context.run_id))
    try:
        with pytest.raises(assembly.ReviewAssemblyUnavailable):
            async with asyncio.timeout(30):
                async with worker_runtime_events(case.runtime, case.context,
                    worker_id=case.binding['lease'].worker_id, review_broker=case.broker) as stream:
                    await anext(stream)
        assert not case.wire.calls and not case.wire.attempts
        assert await admission.get_pool(case.context.tenant_id, case.context.run_id) == before
        assert await admission.get_continuity_seal(case.context.tenant_id, case.context.run_id) == prepared.seal
        assert await admission.get_attempt(case.context.tenant_id, case.context.run_id,
                                           prepared.unknown.attempt_id) == prepared.unknown
    finally:
        case.wire.release.set()
        await case.runtime.cancel_run_owned(case.context.run_id, present(case.context.execution_token))
