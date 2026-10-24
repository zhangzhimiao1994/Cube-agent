"""Native PG correction/ACK boundaries with real service, AutoGen and artifacts.

Only provider replies and harness outcomes are offline doubles. No network,
paid model, real project commands, deployment or user acceptance is exercised.
Worker loss rolls back a real checkpoint transaction; recovery only expires the
owned worker lease and never fabricates checkpoints or resets a run's status.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import Role
from agent_hub.db.models import RunArtifactRow, RunCheckpointRow, RunEventRow
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelRequest, RejectedOutputEvidence, TokenUsage
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime, _DiscussionDurability
from agent_hub.runtime.autogen.delivery_report import FINAL_REPORT_ACTOR
from agent_hub.runtime.autogen.model_rejection import validate_rejection
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.registry import RuntimeRegistry
from tests.integration.runs.test_action_service_native_recovery import RecordingQueue
from tests.integration.runs.test_discussion_final_report_native import (
    TOOL_CALLS,
    OfflineCapabilities,
    OfflineReportGateway,
    Scenario,
    SimulatedWorkerLoss,
    Stack,
    assert_public_report,
    ledger,
    native_plan,
)
from tests.integration.runs.test_discussion_final_report_native import (
    scenario as scenario,  # noqa: PLC0414 - explicitly re-export the shared pytest fixture
)

pytestmark = pytest.mark.integration
POLICY = "engineering_tool_correction_version"
ACK = "engineering_checkpoint_ack_version"
Boundary = Literal["correction_intent", "accepted_correction", "inspection_intent"]


def inspection_hash(request: ModelRequest) -> str:
    return _DiscussionDurability.request_hash(
        request, tool_names={"project.inspect": "project.inspect", "project_inspect": "project.inspect"},
    )


class CorrectInspectionGateway(OfflineReportGateway):
    """Reject the original inspection, then preserve the base's strict response."""

    def __init__(
        self, *, always_reject: bool = False, rejected: bool = False,
        forbid_original_request_hash: str | None = None,
        forbid_inspection: bool = False, forbidden: bool = False,
    ) -> None:
        super().__init__(forbidden=forbidden)
        self.always_reject = always_reject
        self.rejected = rejected
        self.forbid_original_request_hash = forbid_original_request_hash
        self.forbid_inspection = forbid_inspection
        self.inspection_requests: list[ModelRequest] = []
        self.accepted_inspections: list[GatewayCompletion] = []
        self.observed_tokens = 0
        self.observed_cost = Decimal(0)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        inspecting = request.required_tool == "project_inspect"
        if inspecting:
            self.inspection_requests.append(request)
            assert not self.forbid_inspection, "cold recovery repeated accepted inspection model"
            assert inspection_hash(request) != self.forbid_original_request_hash, (
                "cold recovery sent the original rejected request to the provider"
            )
            if self.always_reject or not self.rejected:
                self.requests.append(request)
                self.rejected = True
                usage = TokenUsage(7, 3, 10)
                self.observed_tokens += usage.total_tokens
                raise GatewayRejectedOutput(
                    evidence=RejectedOutputEvidence(
                        final_text="PRIVATE_REJECTED_INSPECTION_BODY", usage=usage,
                        usage_status="known", status="completed", reason="invalid_tool",
                    ), logical_model=request.logical_model, deployment_id="shared",
                    provider_id="test", provider_model="test/model", cost_usd=None,
                )
        result = await super().complete_with_context(request)
        assert result.response.usage is not None
        self.observed_tokens += result.response.usage.total_tokens
        if result.cost_usd is not None:
            self.observed_cost += result.cost_usd
        if inspecting:
            self.accepted_inspections.append(result)
        return result


class CrashAtCorrectionCheckpoint(RunRepository):
    boundary: Boundary
    blocked_checkpoint: RuntimeCheckpoint | None = None
    blocked_sequence: int | None = None

    async def persist_event(
        self, session: AsyncSession, *, tenant_id: UUID, run_id: UUID, event: RunEvent,
    ) -> None:
        checkpoint = event.checkpoint
        hit = False
        if event.kind is EventKind.CHECKPOINT_SAVED and checkpoint is not None:
            models, tools = ledger(checkpoint, "model_ledger"), ledger(checkpoint, "tool_ledger")
            last = models[-1] if models else {}
            correcting = "correction_for" in last
            hit = (
                self.boundary == "correction_intent" and correcting and last["status"] == "running"
                or self.boundary == "accepted_correction" and correcting
                and last["status"] == "succeeded"
                or self.boundary == "inspection_intent" and correcting
                and last["status"] == "succeeded" and bool(tools)
                and tools[-1]["name"] == "project.inspect" and tools[-1]["status"] == "running"
            )
        await super().persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)
        if hit:
            assert self.blocked_checkpoint is None and checkpoint is not None
            self.blocked_checkpoint, self.blocked_sequence = checkpoint, event.sequence
            # The insert/promotion has run, but the service's atomic transaction
            # has not committed. The previous ACK must already be in PostgreSQL.
            raise SimulatedWorkerLoss(f"worker lost before {self.boundary} commit")


def open_stack(
    scoped: Scenario, *, boundary: Boundary | None = None, always_reject: bool = False,
    rejected: bool = False, forbid_original_request_hash: str | None = None,
    forbid_inspection: bool = False, forbidden: bool = False,
) -> Stack:
    database = build_database(scoped.database_url)
    assert database.engine.url.get_backend_name() == "postgresql"
    runs: RunRepository
    if boundary is None:
        runs = RunRepository(database.session_factory)
    else:
        runs = CrashAtCorrectionCheckpoint(database.session_factory)
        runs.boundary = boundary
    gateway = CorrectInspectionGateway(
        always_reject=always_reject, rejected=rejected,
        forbid_original_request_hash=forbid_original_request_hash,
        forbid_inspection=forbid_inspection, forbidden=forbidden,
    )
    tools = OfflineCapabilities(database, scoped, forbidden=forbidden)
    configured = native_plan()
    assert len(configured.participants) == 8
    runtime = AutoGenDiscussionRuntime(
        gateway, configured, capability_gateway=tools, harness_tool_gateway=tools,
        artifact_repository=PostgresArtifactRepository(database.session_factory),
    )
    assert runtime.mode is TaskMode.DISCUSS
    registry = RuntimeRegistry((runtime,))
    assert registry.get(TaskMode.DISCUSS) is runtime
    service = RunService(
        runs, runtime_registry=registry, router=None, task_queue=RecordingQueue(),
        worker_id=f"correction-native-{uuid4().hex}", runtime_timeout_seconds=30,
        runtime_token_budget=5000,
    )
    stack = Stack(database, runs, service, runtime, gateway, tools)
    scoped.stacks.append(stack)
    return stack


def provider(stack: Stack) -> CorrectInspectionGateway:
    assert isinstance(stack.gateway, CorrectInspectionGateway)
    return stack.gateway


async def latest(stack: Stack, scoped: Scenario) -> RuntimeCheckpoint:
    assert scoped.run_id is not None
    async with stack.database.session_factory() as session:
        saved = await stack.runs.latest_checkpoint(
            session, tenant_id=scoped.tenant, run_id=scoped.run_id,
        )
        assert saved is not None
        row = await session.get(RunCheckpointRow, saved.id)
        assert row is not None and row.tenant_id == scoped.tenant and row.run_id == scoped.run_id
        assert saved.run_id == scoped.run_id and saved.tenant_id == scoped.tenant
        assert saved.mode is TaskMode.DISCUSS
        assert saved.runtime_type == "autogen" and saved.runtime_version == "2"
        assert saved.state["autogen_version"] == "0.7.5"
        assert saved.state[POLICY] == 1 and saved.state[ACK] == 1
        return saved


def assert_usage(saved: RuntimeCheckpoint, *, tokens: int, cost: Decimal) -> None:
    usage = saved.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == tokens
    assert Decimal(str(usage["cost_usd"])) == cost


def assert_correction_requests(original: ModelRequest, corrected: ModelRequest) -> None:
    assert original.required_tool == corrected.required_tool == "project_inspect"
    assert len(original.tools) == 1 and corrected.tools == original.tools
    assert corrected.required_capabilities == original.required_capabilities
    assert corrected.logical_model == original.logical_model
    assert corrected.allow_fallback is original.allow_fallback
    assert corrected.response_schema is original.response_schema is None
    assert not corrected.native_tool_continuation and not original.native_tool_continuation
    assert corrected.messages[:-1] == original.messages
    assert len(corrected.messages) == len(original.messages) + 1
    assert 0 < corrected.max_output_tokens <= original.max_output_tokens
    assert 0 < corrected.timeout_seconds <= original.timeout_seconds
    assert inspection_hash(corrected) != inspection_hash(original)


def assert_receipt(saved: RuntimeCheckpoint, scoped: Scenario, *, position: int) -> None:
    assert scoped.run_id is not None
    row = ledger(saved, "model_ledger")[position]
    value = row["rejection"]
    assert row["status"] == "rejected" and isinstance(value, Mapping)
    receipt = validate_rejection(
        value, run_id=scoped.run_id, tenant_id=scoped.tenant,
        position=position, request_hash=row["request_hash"],
    )
    assert receipt.reason == "invalid_tool" and receipt.output_status == "completed"
    assert receipt.usage_status == "known" and receipt.usage is not None
    assert receipt.usage.model_dump() == {
        "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
    }
    assert receipt.cost_usd is None and value["cost_usd"] is None
    assert receipt.requested_logical_model == "shared"
    assert receipt.provenance.deployment_id == "shared"
    assert receipt.provenance.provider_id == "test"
    assert receipt.provenance.provider_model == "test/model"
    assert "PRIVATE_REJECTED_INSPECTION_BODY" not in str(saved.to_payload())


async def assert_scope(stack: Stack, scoped: Scenario) -> None:
    assert scoped.run_id is not None
    record = await stack.runs.get(scoped.tenant, scoped.run_id, actor_id=scoped.actor)
    assert record.tenant_id == scoped.tenant and record.id == scoped.run_id
    assert record.actor_id == scoped.actor and record.actor_role is Role.OPERATOR
    assert record.mode is TaskMode.DISCUSS
    assert record.version >= 1
    route = record.routing_decision
    assert route is not None
    for key in (POLICY, ACK, "engineering_delivery_contract_version",
                "engineering_action_version", "engineering_final_report_version"):
        assert type(route[key]) is int and route[key] == 1
    assert route["sandbox_profile"] == "workspace_write"
    assert route["permission_mode"] == "default"
    assert route["project_id"] == scoped.project and route["workspace_session_id"] == "main"
    permissions = route["requested_permissions"]
    assert isinstance(permissions, tuple | list)
    assert set(permissions) == {"workspace.read", "workspace.write"}
    assert "generated_project_contract_version" not in route
    assert "project_delivery" not in route
    assert stack.runtime.participant_ids == (
        "engineer", "reviewer", "reviewer2", "reviewer3", "reviewer4", "reviewer5",
        "reviewer6", "reviewer7",
    )


async def crash_and_read(stack: Stack, scoped: Scenario) -> RuntimeCheckpoint:
    assert scoped.run_id is not None
    with pytest.raises(SimulatedWorkerLoss):
        await asyncio.wait_for(stack.service.execute(scoped.run_id), timeout=30)
    assert isinstance(stack.runs, CrashAtCorrectionCheckpoint)
    blocked = stack.runs.blocked_checkpoint
    assert blocked is not None and stack.runs.blocked_sequence is not None
    saved = await latest(stack, scoped)
    assert saved.id != blocked.id and saved.state["terminal"] is False
    # Independent session proves both rollback and the predecessor's actual commit.
    async with stack.database.session_factory() as session:
        assert await session.get(RunCheckpointRow, blocked.id) is None
        assert await session.scalar(select(RunEventRow.id).where(
            RunEventRow.tenant_id == scoped.tenant, RunEventRow.run_id == scoped.run_id,
            RunEventRow.sequence == stack.runs.blocked_sequence,
        )) is None
        assert await session.scalar(select(RunEventRow.id).where(
            RunEventRow.tenant_id == scoped.tenant, RunEventRow.run_id == scoped.run_id,
            RunEventRow.kind == EventKind.CHECKPOINT_SAVED.value,
            RunEventRow.sequence < stack.runs.blocked_sequence,
        ).limit(1)) is not None
    assert stack.tools.calls == []
    await assert_scope(stack, scoped)
    return saved


async def expire_owned_worker(stack: Stack, scoped: Scenario) -> None:
    assert scoped.run_id is not None
    record = await stack.runs.get(scoped.tenant, scoped.run_id, actor_id=scoped.actor)
    assert record.status is RunStatus.RUNNING and record.worker_lease_token is not None
    assert record.worker_id == stack.service._worker_id
    async with stack.database.session_factory() as session, session.begin():
        row = await stack.runs.get_for_update(session, scoped.run_id)
        assert row.tenant_id == scoped.tenant and row.actor_id == scoped.actor
        assert row.status == RunStatus.RUNNING.value
        assert row.worker_id == record.worker_id
        assert row.worker_lease_token == record.worker_lease_token
        # Only this worker's lease expires; an unknown provider outcome stays unknown.
        row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await stack.runtime.cancel()
    await stack.database.dispose()


async def assert_delivery(stack: Stack, scoped: Scenario, *, tokens: int, cost: Decimal) -> None:
    assert scoped.run_id is not None
    await assert_scope(stack, scoped)
    saved = await latest(stack, scoped)
    assert_usage(saved, tokens=tokens, cost=cost)
    gateway = provider(stack)
    assert len(gateway.final_requests) == 1
    # The existing report helper expresses cumulative usage in two-token units.
    # Derive its argument from observed receipts, never from ledger/call counts.
    assert tokens % 2 == 0
    await assert_public_report(
        stack, scoped, final_request=gateway.final_requests[0], request_count=tokens // 2,
    )
    # Message compaction may remove acknowledged rows from the latest ledger.
    # Inspect real historical checkpoints rather than treating it as full history.
    async with stack.database.session_factory() as session:
        checkpoints = (await session.scalars(select(RunCheckpointRow).where(
            RunCheckpointRow.tenant_id == scoped.tenant, RunCheckpointRow.run_id == scoped.run_id,
        ).order_by(RunCheckpointRow.sequence))).all()
    for row in checkpoints:
        committed = RuntimeCheckpoint.from_payload(dict(row.payload))
        models = ledger(committed, "model_ledger")
        if not models or "correction_for" not in models[-1] or models[-1]["status"] != "succeeded":
            continue
        previous, corrected = models[-2:]
        assert corrected["correction_for"] == len(models) - 2
        assert_receipt(committed, scoped, position=len(models) - 2)
        assert previous["required_tool"] == corrected["required_tool"] == "project_inspect"
        assert previous["request_hash"] != corrected["request_hash"]
        previous_tokens, corrected_tokens = previous["max_output_tokens"], corrected["max_output_tokens"]
        previous_time, corrected_time = previous["timeout_seconds"], corrected["timeout_seconds"]
        assert isinstance(previous_tokens, int) and isinstance(corrected_tokens, int)
        assert 0 < corrected_tokens <= previous_tokens
        assert isinstance(previous_time, int | float) and isinstance(corrected_time, int | float)
        assert 0 < corrected_time <= previous_time
        break
    else:
        pytest.fail("no committed rejected receipt with a successful linked correction")


async def assert_failed(stack: Stack, scoped: Scenario, *, reason: str) -> None:
    assert scoped.run_id is not None
    await assert_scope(stack, scoped)
    record = await stack.runs.get(scoped.tenant, scoped.run_id, actor_id=scoped.actor)
    assert record.status is RunStatus.FAILED and record.worker_lease_token is None
    events = await stack.runs.raw_events(scoped.tenant, scoped.run_id)
    failed = [event for event in events if event.kind is EventKind.RUNTIME_FAILED]
    assert len(failed) == 1 and reason in (failed[0].reason or "")
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED or event.kind == "discussion.completed"
                   for event in events)
    assert not any(event.artifact is not None and event.artifact.producer == FINAL_REPORT_ACTOR
                   for event in events)
    assert provider(stack).final_requests == [] and stack.tools.calls == []
    async with stack.database.session_factory() as session:
        assert await session.scalar(select(RunArtifactRow.id).where(
            RunArtifactRow.tenant_id == scoped.tenant, RunArtifactRow.run_id == scoped.run_id,
            RunArtifactRow.producer == FINAL_REPORT_ACTOR,
        )) is None


async def test_fresh_discuss_corrects_inspection_once_and_publishes_bound_report(
    scenario: Scenario,
) -> None:
    stack = open_stack(scenario)
    run_id = await scenario.submit(stack)
    await asyncio.wait_for(stack.service.execute(run_id), timeout=30)
    gateway = provider(stack)
    assert len(gateway.inspection_requests) == 2 and len(gateway.accepted_inspections) == 1
    assert_correction_requests(*gateway.inspection_requests)
    assert stack.tools.calls == TOOL_CALLS
    await assert_delivery(stack, scenario, tokens=gateway.observed_tokens, cost=gateway.observed_cost)


async def test_cold_committed_rejection_can_send_first_different_hash_correction(
    scenario: Scenario,
) -> None:
    first = open_stack(scenario, boundary="correction_intent")
    run_id = await scenario.submit(first)
    saved = await crash_and_read(first, scenario)
    gateway = provider(first)
    assert len(gateway.inspection_requests) == 1 and gateway.accepted_inspections == []
    models = ledger(saved, "model_ledger")
    assert models[-1]["status"] == "rejected"
    assert all("correction_for" not in row for row in models)
    assert_receipt(saved, scenario, position=len(models) - 1)
    original = gateway.inspection_requests[0]
    assert models[-1]["request_hash"] == inspection_hash(original)
    assert_usage(saved, tokens=gateway.observed_tokens, cost=gateway.observed_cost)
    await expire_owned_worker(first, scenario)

    fresh = open_stack(scenario, rejected=True, forbid_original_request_hash=inspection_hash(original))
    assert fresh.runtime is not first.runtime and fresh.database is not first.database
    await asyncio.wait_for(fresh.service.execute(run_id, allow_running_recovery=True), timeout=30)
    recovered = provider(fresh)
    assert len(recovered.inspection_requests) == 1 and len(recovered.accepted_inspections) == 1
    assert_correction_requests(original, recovered.inspection_requests[0])
    assert fresh.tools.calls == TOOL_CALLS and first.tools.calls == []
    assert len(gateway.inspection_requests) == 1
    await assert_delivery(
        fresh, scenario, tokens=gateway.observed_tokens + recovered.observed_tokens,
        cost=gateway.observed_cost + recovered.observed_cost,
    )


async def test_cold_running_correction_stops_unknown_without_model_or_tools(
    scenario: Scenario,
) -> None:
    first = open_stack(scenario, boundary="accepted_correction")
    run_id = await scenario.submit(first)
    saved = await crash_and_read(first, scenario)
    gateway = provider(first)
    assert len(gateway.inspection_requests) == 2 and len(gateway.accepted_inspections) == 1
    assert_correction_requests(*gateway.inspection_requests)
    models = ledger(saved, "model_ledger")
    assert models[-1]["status"] == "running" and models[-1]["correction_for"] == len(models) - 2
    assert models[-1]["request_hash"] == inspection_hash(gateway.inspection_requests[1])
    assert_receipt(saved, scenario, position=len(models) - 2)
    accepted = gateway.accepted_inspections[0]
    assert accepted.response.usage is not None and accepted.cost_usd is not None
    assert_usage(
        saved, tokens=gateway.observed_tokens - accepted.response.usage.total_tokens,
        cost=gateway.observed_cost - accepted.cost_usd,
    )
    await expire_owned_worker(first, scenario)

    fresh = open_stack(scenario, forbidden=True, forbid_inspection=True)
    await asyncio.wait_for(fresh.service.execute(run_id, allow_running_recovery=True), timeout=30)
    await assert_failed(fresh, scenario, reason="model outcome is uncertain")
    assert provider(fresh).requests == [] and provider(fresh).inspection_requests == []
    assert provider(fresh).observed_tokens == 0
    assert (await latest(fresh, scenario)).state["usage"] == saved.state["usage"]


async def test_cold_accepted_correction_reuses_model_then_executes_first_inspection(
    scenario: Scenario,
) -> None:
    first = open_stack(scenario, boundary="inspection_intent")
    run_id = await scenario.submit(first)
    saved = await crash_and_read(first, scenario)
    gateway = provider(first)
    assert len(gateway.inspection_requests) == 2 and len(gateway.accepted_inspections) == 1
    assert_correction_requests(*gateway.inspection_requests)
    models = ledger(saved, "model_ledger")
    corrected = models[-1]
    assert corrected["status"] == "succeeded" and corrected["correction_for"] == len(models) - 2
    assert corrected["request_hash"] == inspection_hash(gateway.inspection_requests[1])
    assert_receipt(saved, scenario, position=len(models) - 2)
    assert ledger(saved, "tool_ledger") == ()
    assert_usage(saved, tokens=gateway.observed_tokens, cost=gateway.observed_cost)
    artifacts = await PostgresArtifactRepository(first.database.session_factory).get_many(
        scenario.tenant, run_id,
        (ArtifactReference(UUID(str(corrected["artifact_id"])), str(corrected["artifact_sha256"])),),
    )
    assert len(artifacts) == 1
    artifact: Artifact = artifacts[0]
    response = gateway.accepted_inspections[0].response
    assert response.usage is not None
    assert artifact.type == "model_result" and artifact.content["text"] is None
    assert artifact.content["usage"] == {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }
    assert artifact.content["tool_calls"] == tuple(
        {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
        for call in response.tool_calls
    )
    await expire_owned_worker(first, scenario)

    fresh = open_stack(
        scenario, rejected=True, forbid_inspection=True,
        forbid_original_request_hash=inspection_hash(gateway.inspection_requests[0]),
    )
    await asyncio.wait_for(fresh.service.execute(run_id, allow_running_recovery=True), timeout=30)
    recovered = provider(fresh)
    assert recovered.inspection_requests == [] and recovered.accepted_inspections == []
    assert recovered.requests, "later task phases still need normal provider calls"
    assert fresh.tools.calls == TOOL_CALLS and first.tools.calls == []
    assert len(gateway.inspection_requests) == 2
    await assert_delivery(
        fresh, scenario, tokens=gateway.observed_tokens + recovered.observed_tokens,
        cost=gateway.observed_cost + recovered.observed_cost,
    )


async def test_second_inspection_rejection_fails_without_third_request_or_success(
    scenario: Scenario,
) -> None:
    stack = open_stack(scenario, always_reject=True)
    run_id = await scenario.submit(stack)
    await asyncio.wait_for(stack.service.execute(run_id), timeout=30)
    gateway = provider(stack)
    assert len(gateway.inspection_requests) == 2 and gateway.accepted_inspections == []
    assert_correction_requests(*gateway.inspection_requests)
    await assert_failed(stack, scenario, reason="invalid_tool")
    saved = await latest(stack, scenario)
    models = ledger(saved, "model_ledger")
    assert models[-1]["status"] == "rejected" and models[-1]["correction_for"] == len(models) - 2
    for position, request in zip(
        (len(models) - 2, len(models) - 1), gateway.inspection_requests, strict=True,
    ):
        assert_receipt(saved, scenario, position=position)
        assert models[position]["request_hash"] == inspection_hash(request)
    assert_usage(saved, tokens=gateway.observed_tokens, cost=gateway.observed_cost)
