"""Real PG/RunService public report persistence, not Root or user-project acceptance.

Submit/execute, AutoGen, worker fencing, checkpoint commits, private artifacts,
and public RunArtifactRow/EventRow writes are real. Model replies and capability
outcomes are explicit offline doubles; no paid model, real commands, project
build, Root execution, or real-user acceptance is exercised. Usage assertions
cover native model artifacts and checkpoint accounting, not provider billing.
The crash test never fabricates or manually persists a runtime checkpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import Role
from agent_hub.db.models import RunArtifactRow, RunCheckpointRow, RunEventRow, RunRow
from agent_hub.db.session import Database, build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.delivery_report import FINAL_REPORT_ACTOR, engineering_report_valid
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.integration.runs.test_action_service_native_recovery import RecordingQueue
from tests.unit.runtime.autogen.test_engineering_completion import Capabilities, plan
from tests.unit.runtime.autogen.test_final_delivery_runtime import ReportGateway

pytestmark = pytest.mark.integration
REPORT = "## Delivery\n\nArchive attached. Fixture checks passed; deployment was not tested.\n"
MESSAGE = "Discuss the next milestone and give a short conclusion."
TOOL_CALLS = ["project.inspect", "workspace.write_text", "project.verify", "workspace.bundle"]


def native_plan() -> DiscussionPlan:
    original = plan()
    engineer = replace(original.participants[0], allowed_tools=(
        "project.inspect", *original.participants[0].allowed_tools,
    ))
    return replace(original, participants=(engineer, original.participants[1]),
                   max_turns=24, token_budget=5000, wall_time_seconds=30)


class OfflineReportGateway(ReportGateway):
    """Reuse the final-report double while honoring service-required inspection."""

    def __init__(self, *, report: str = REPORT, forbidden: bool = False) -> None:
        super().__init__(report=report)
        self.forbidden = forbidden
        # The real driver selects required milestones. A second scripted tool
        # sequence would request verification twice rather than test that path.
        self.actions.clear()

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.forbidden:
            self.requests.append(request)
            raise AssertionError("cold recovery repeated an offline model call")
        if request.required_tool is None:
            return await super().complete_with_context(request)
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(text=None, usage=TokenUsage(1, 1, 2), tool_calls=(
                ToolCall(id=f"offline_{len(self.requests)}", name=request.required_tool, arguments={}),
            )), deployment_id="shared", logical_model=request.logical_model,
            provider_id="test", provider_model="test/model", cost_usd=Decimal(0),
        )


class OfflineCapabilities(Capabilities):
    """Offline outcomes at the real runtime harness entrance, with OPERATOR scope."""

    def __init__(self, database: Database, scenario: Scenario, *, forbidden: bool = False) -> None:
        super().__init__()
        self.database, self.scenario, self.forbidden = database, scenario, forbidden

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        assert tenant_id == self.scenario.tenant and request.run_id == self.scenario.run_id
        assert user_id == self.scenario.actor and role is Role.OPERATOR
        assert request.actor == "engineer" and request.tool_name in TOOL_CALLS
        if self.forbidden:
            self.calls.append(request.tool_name)
            raise AssertionError("cold recovery repeated an offline capability")
        record = await RunRepository(self.database.session_factory).get(
            tenant_id, request.run_id, actor_id=self.scenario.actor,
        )
        assert record.status is RunStatus.RUNNING and record.worker_lease_token is not None
        assert record.actor_role is Role.OPERATOR
        if request.tool_name == "project.inspect":
            self.calls.append(request.tool_name)
            result: Mapping[str, JsonValue] = {
                "schema_version": 1, "scope": {
                    "tenant_id": str(tenant_id), "run_id": str(request.run_id),
                    "project_id": self.scenario.project, "workspace_session_id": "main",
                }, "manifests": (), "operations": {}, "issues": (),
            }
        else:
            result = await super().execute(
                tenant_id=tenant_id, run_id=request.run_id, actor=request.actor,
                name=request.tool_name, arguments=request.arguments,
                idempotency_key=request.idempotency_key,
            )
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status="succeeded", payload=result)


class SimulatedWorkerLoss(BaseException):
    """Abrupt process loss must escape RunService's normal Exception-to-failed path."""


class CrashBeforeReportPublication(RunRepository):
    report: Artifact | None = None

    async def persist_event(
        self, session: AsyncSession, *, tenant_id: UUID, run_id: UUID, event: RunEvent,
    ) -> None:
        if (event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
                and event.artifact.producer == FINAL_REPORT_ACTOR):
            self.report = event.artifact
            raise SimulatedWorkerLoss("offline worker lost before public report transaction")
        await super().persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)


@dataclass
class Stack:
    database: Database
    runs: RunRepository
    service: RunService
    runtime: AutoGenDiscussionRuntime
    gateway: OfflineReportGateway
    tools: OfflineCapabilities


@dataclass
class Scenario:
    database_url: str
    tenant: UUID = field(default_factory=uuid4)
    actor: UUID = field(default_factory=uuid4)
    project: str = field(default_factory=lambda: f"report-native-{uuid4().hex}")
    run_id: UUID | None = None
    stacks: list[Stack] = field(default_factory=list)

    def open(self, *, report: str = REPORT, crash: bool = False, forbidden: bool = False) -> Stack:
        database = build_database(self.database_url)
        assert database.engine.url.get_backend_name() == "postgresql"
        runs = (CrashBeforeReportPublication(database.session_factory) if crash
                else RunRepository(database.session_factory))
        gateway = OfflineReportGateway(report=report, forbidden=forbidden)
        tools = OfflineCapabilities(database, self, forbidden=forbidden)
        runtime = AutoGenDiscussionRuntime(
            gateway, native_plan(), capability_gateway=tools, harness_tool_gateway=tools,
            artifact_repository=PostgresArtifactRepository(database.session_factory),
        )
        service = RunService(
            runs, runtime_registry=RuntimeRegistry((runtime,)), router=None,
            task_queue=RecordingQueue(), worker_id=f"report-native-{uuid4().hex}",
            runtime_timeout_seconds=30, runtime_token_budget=5000,
        )
        stack = Stack(database, runs, service, runtime, gateway, tools)
        self.stacks.append(stack)
        return stack

    async def submit(self, stack: Stack) -> UUID:
        assert self.run_id is None
        submitted = await stack.service.submit(
            tenant_id=self.tenant, actor_id=self.actor, actor_role=Role.OPERATOR,
            mode=TaskMode.DISCUSS, message=MESSAGE, agent_ids=("engineer", "reviewer"),
            allow_scale_mode_upgrade=False, project_id=self.project, workspace_session_id="main",
            sandbox_profile="workspace_write", permission_mode="default",
            requested_permissions=("workspace.read", "workspace.write"),
        )
        self.run_id = submitted.id
        record = await stack.runs.get(self.tenant, submitted.id, actor_id=self.actor)
        assert record.routing_decision is not None
        assert "generated_project_contract_version" not in record.routing_decision
        assert "project_delivery" not in record.routing_decision
        return submitted.id


@pytest.fixture
async def scenario(database_url: str) -> AsyncIterator[Scenario]:
    scoped = Scenario(database_url)
    try:
        yield scoped
    finally:
        try:
            for stack in scoped.stacks:
                await stack.runtime.cancel()
        finally:
            try:
                if scoped.run_id is not None:
                    cleanup = build_database(database_url)
                    try:
                        async with cleanup.session_factory() as session, session.begin():
                            await session.execute(delete(RunRow).where(
                                RunRow.tenant_id == scoped.tenant, RunRow.id == scoped.run_id,
                            ))
                    finally:
                        await cleanup.dispose()
            finally:
                for stack in scoped.stacks:
                    await stack.database.dispose()


def ledger(checkpoint: RuntimeCheckpoint, name: str) -> tuple[Mapping[str, JsonValue], ...]:
    values = checkpoint.state[name]
    assert isinstance(values, tuple | list)
    rows: list[Mapping[str, JsonValue]] = []
    for value in values:
        assert isinstance(value, Mapping)
        rows.append(value)
    return tuple(rows)


async def assert_public_report(
    stack: Stack, scenario: Scenario, *, final_request: ModelRequest, request_count: int,
) -> Artifact:
    assert scenario.run_id is not None
    run_id = scenario.run_id
    record = await stack.runs.get(scenario.tenant, run_id, actor_id=scenario.actor)
    assert record.status is RunStatus.COMPLETED and record.worker_lease_token is None
    assert record.actor_id == scenario.actor and record.actor_role is Role.OPERATOR
    assert record.routing_decision is not None
    assert record.routing_decision["engineering_delivery_contract_version"] == 1
    assert record.routing_decision["engineering_final_report_version"] == 1
    assert "generated_project_contract_version" not in record.routing_decision
    events = await stack.runs.raw_events(scenario.tenant, run_id)
    completed = [event for event in events if event.kind is EventKind.RUNTIME_COMPLETED]
    assert len(completed) == 1
    report_id = UUID(str(completed[0].payload["final_answer_artifact_id"]))
    digest = completed[0].payload["final_answer_sha256"]
    async with stack.database.session_factory() as session:
        # Public storage is asserted independently of the private hydration table.
        public = await session.scalar(select(RunArtifactRow).where(
            RunArtifactRow.tenant_id == scenario.tenant, RunArtifactRow.run_id == run_id,
            RunArtifactRow.id == report_id,
        ))
        assert public is not None, "completed refers only to a private runtime artifact"
        report = Artifact.from_payload(dict(public.payload))
        assert public.content_sha256 == digest == report.content_sha256
        checkpoint = await stack.runs.latest_checkpoint(
            session, tenant_id=scenario.tenant, run_id=run_id,
        )
        assert checkpoint is not None and checkpoint.state["terminal"] is True
        checkpoint_row = await session.get(RunCheckpointRow, checkpoint.id)
        assert checkpoint_row is not None
        assert checkpoint_row.tenant_id == scenario.tenant and checkpoint_row.run_id == run_id
        completed_row = await session.scalar(select(RunEventRow).where(
            RunEventRow.tenant_id == scenario.tenant, RunEventRow.run_id == run_id,
            RunEventRow.sequence == completed[0].sequence,
        ))
        assert completed_row is not None and completed_row.kind == EventKind.RUNTIME_COMPLETED.value
        assert completed_row.payload["payload"] == dict(completed[0].payload)
    assert report.type == "text" and report.producer == FINAL_REPORT_ACTOR
    assert report.content["text"] == REPORT and report.content["presentation"] == "final_answer"
    publications = [event for event in events if event.kind is EventKind.ARTIFACT_CREATED
                    and event.artifact is not None and event.artifact.id == report_id]
    assert len(publications) == 1 and publications[0].sequence < completed[0].sequence
    assert publications[0].artifact is not None
    assert publications[0].artifact.to_payload() == report.to_payload()
    discussion = [event for event in events if event.kind == "discussion.completed"]
    assert len(discussion) == 1 and discussion[0].payload["summary"] == REPORT
    assert discussion[0].payload["final_answer_artifact_id"] == str(report_id)
    assert discussion[0].payload["final_answer_sha256"] == digest
    public_payloads = await stack.runs.artifacts(scenario.tenant, run_id)
    public_report = next(item for item in public_payloads if item["id"] == str(report_id))
    assert public_report["content_sha256"] == digest
    assert isinstance(public_report["content"], Mapping)
    assert public_report["content"]["text"] == REPORT

    registry = checkpoint.state["artifact_registry"]
    assert isinstance(registry, Mapping) and registry[str(report_id)] == digest
    refs: list[ArtifactReference] = []
    for key, value in registry.items():
        assert isinstance(value, str)
        refs.append(ArtifactReference(UUID(key), value))
    private = await PostgresArtifactRepository(stack.database.session_factory).get_many(
        scenario.tenant, run_id, tuple(refs),
    )
    by_id = {str(artifact.id): artifact for artifact in private}
    assert by_id[str(report_id)].to_payload() == report.to_payload()
    models, tools = ledger(checkpoint, "model_ledger"), ledger(checkpoint, "tool_ledger")
    metadata = report.content["engineering_report"]
    assert isinstance(metadata, Mapping)
    model = by_id[str(metadata["model_artifact_id"])]
    bundle = by_id[str(metadata["bundle_artifact_id"])]
    assert model.type == "model_result" and model.producer == "agent_hub"
    assert model.content["text"] == REPORT and model.content["tool_calls"] == ()
    assert model.content["logical_model"] == final_request.logical_model
    assert final_request.tools == () and final_request.required_tool is None
    assert model.content["usage"] == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    assert models[-1]["status"] == "succeeded"
    assert models[-1]["artifact_id"] == str(model.id)
    assert models[-1]["artifact_sha256"] == model.content_sha256
    assert models[-1]["request_hash"] == _DiscussionDurability.request_hash(final_request)
    assert tools[-1]["name"] == "workspace.bundle" and tools[-1]["status"] == "succeeded"
    assert tools[-1]["artifact_id"] == str(bundle.id)
    assert tools[-1]["artifact_sha256"] == bundle.content_sha256
    assert report.source_ids == (str(model.id), str(bundle.id))
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 2 * request_count
    assert Decimal(str(usage["cost_usd"])) == 0
    context = TaskContext(
        run_id=run_id, tenant_id=scenario.tenant, actor_id=scenario.actor, actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS, request=record.request,
        routing_decision=cast(Mapping[str, JsonValue], record.routing_decision),
    )
    assert engineering_report_valid(
        report, context=context, logical_model=final_request.logical_model,
        model_entries=models, tool_entries=tools, artifacts=private,
    )
    assert await stack.runs.artifacts(uuid4(), run_id) == ()
    assert await stack.runs.artifacts(scenario.tenant, uuid4()) == ()
    return report


async def test_operator_final_report_is_publicly_persisted_with_native_model_binding(
    scenario: Scenario,
) -> None:
    stack = scenario.open()
    run_id = await scenario.submit(stack)
    await asyncio.wait_for(stack.service.execute(run_id), timeout=30)
    assert stack.tools.calls == TOOL_CALLS
    assert len(stack.gateway.final_requests) == 1
    await assert_public_report(stack, scenario, final_request=stack.gateway.final_requests[0],
                               request_count=len(stack.gateway.requests))


async def test_cold_service_recovery_before_public_report_reuses_committed_native_outcomes(
    scenario: Scenario,
) -> None:
    first = scenario.open(crash=True)
    run_id = await scenario.submit(first)
    with pytest.raises(SimulatedWorkerLoss):
        await asyncio.wait_for(first.service.execute(run_id), timeout=30)
    assert isinstance(first.runs, CrashBeforeReportPublication)
    interrupted_report = first.runs.report
    assert interrupted_report is not None
    assert first.tools.calls == TOOL_CALLS and len(first.gateway.final_requests) == 1
    final_request = first.gateway.final_requests[0]
    request_count = len(first.gateway.requests)
    private = await PostgresArtifactRepository(first.database.session_factory).get_many(
        scenario.tenant, run_id,
        (ArtifactReference(interrupted_report.id, interrupted_report.content_sha256),),
    )
    assert len(private) == 1 and private[0].to_payload() == interrupted_report.to_payload()
    async with first.database.session_factory() as session, session.begin():
        assert await session.scalar(select(RunArtifactRow.id).where(
            RunArtifactRow.tenant_id == scenario.tenant, RunArtifactRow.run_id == run_id,
            RunArtifactRow.id == interrupted_report.id,
        )) is None
        saved = await first.runs.latest_checkpoint(session, tenant_id=scenario.tenant, run_id=run_id)
        assert saved is not None and saved.state["terminal"] is False
        row = await first.runs.get_for_update(session, run_id)
        assert row.tenant_id == scenario.tenant and row.status == RunStatus.RUNNING.value
        assert row.worker_lease_token is not None
        # Expire only this newly submitted test worker. No status/checkpoint edits.
        row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await first.runtime.cancel()
    await first.database.dispose()

    fresh = scenario.open(forbidden=True)
    assert fresh.runtime is not first.runtime and fresh.database is not first.database
    await asyncio.wait_for(fresh.service.execute(run_id, allow_running_recovery=True), timeout=30)
    assert not fresh.gateway.requests and not fresh.tools.calls
    record = await fresh.runs.get(scenario.tenant, run_id, actor_id=scenario.actor)
    recovered_events = await fresh.runs.raw_events(scenario.tenant, run_id)
    assert record.status is RunStatus.COMPLETED, (
        ("before-public recovery needs a committed checkpoint retaining the report; "
         "an in-memory save_checkpoint is insufficient"),
        [(event.kind, event.reason) for event in recovered_events
         if event.kind is EventKind.RUNTIME_FAILED],
    )
    recovered = await assert_public_report(fresh, scenario, final_request=final_request,
                                          request_count=request_count)
    # An unpublished text envelope may be rebuilt with a new UUID from the same
    # durable model outcome. The exact content/source binding must stay stable.
    assert recovered.content_sha256 == interrupted_report.content_sha256
    assert len(first.gateway.requests) == request_count and first.tools.calls == TOOL_CALLS


@pytest.mark.parametrize("text", ["", " \n\t"])
async def test_completed_tool_delivery_without_final_report_cannot_complete(
    scenario: Scenario, text: str,
) -> None:
    stack = scenario.open(report=text)
    run_id = await scenario.submit(stack)
    await asyncio.wait_for(stack.service.execute(run_id), timeout=30)
    record = await stack.runs.get(scenario.tenant, run_id, actor_id=scenario.actor)
    assert record.status is RunStatus.FAILED and record.worker_lease_token is None
    assert record.actor_role is Role.OPERATOR
    assert stack.tools.calls == TOOL_CALLS and len(stack.gateway.final_requests) == 1
    events = await stack.runs.raw_events(scenario.tenant, run_id)
    assert any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED or event.kind == "discussion.completed"
                   for event in events)
    async with stack.database.session_factory() as session:
        assert await session.scalar(select(RunArtifactRow.id).where(
            RunArtifactRow.tenant_id == scenario.tenant, RunArtifactRow.run_id == run_id,
            RunArtifactRow.producer == FINAL_REPORT_ACTOR,
        )) is None
