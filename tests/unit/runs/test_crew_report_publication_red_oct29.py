"""Native Crew publication/recovery regressions; no provider or server is called.

The original twelve RED cases are preserved as expected-behavior assertions.
Additional guards cover native binding, owned runtime scope, SQL rollback,
publication ordering and recovery from independently persisted review receipts.
"""
from __future__ import annotations

import asyncio
import json
import types
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table, UniqueConstraint, func, select
from sqlalchemy import event as sql_event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_hub.db.models import RunArtifactRow, RunCheckpointRow, RunEventRow, RunRow, RunStepRow
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import RunRepository, _public_event_payload
from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    ExecutionRuntime,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.delivery_report import final_evidence
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.runs.final_report_boundaries_typed_fixture import (
    SQLCase,
    context,
    responses,
    subject,
)
from tests.unit.runs.project_auto_repair_typed_fixture import json_map, present, sequence
from tests.unit.runs.reviewed_report_revision_typed_fixture import (
    object_map,
    read_map,
    read_rows,
    string_value,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
from tests.unit.runtime.crew.test_engineering_delivery_execution import ActualCapabilityHarness
from tests.unit.runtime.crew.test_framework_parser_boundary import (
    installed_parser,  # noqa: F401
    offline_only,  # noqa: F401
)

REPORT = "OWNED_CREW_FINAL_REPORT_BODY: verified ZIP is ready for the user."


class ReviewGateway(WorkspaceDeliverySequenceGateway):
    """Only the provider boundary is fake; native receipt/ledger code runs."""

    def __init__(self, outcome: str) -> None:
        review = ModelResponse(
            text=json.dumps({"verdict": outcome, "feedback": "Owned review feedback"}),
            usage=TokenUsage(1, 1, 2),
        )
        super().__init__((*responses(REPORT), review))
        self.outcome = outcome
        self.completions: list[GatewayCompletion] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model == "review" and self.outcome == "unknown":
            self.requests.append(request)
            # No response/usage receipt: native checkpoint must retain uncertainty.
            raise asyncio.CancelledError("owned reviewer call interrupted without a receipt")
        completion = await super().complete_with_context(request)
        self.completions.append(completion)
        return completion


def reviewed_runtime(
    gateway: WorkspaceDeliverySequenceGateway, harness: ActualCapabilityHarness,
    repository: InMemoryArtifactRepository, parser: types.ModuleType, *, final_id: str = "report",
) -> adapter.CrewDispatchRuntime:
    runtime = subject(gateway, harness, repository, parser)
    worker, report = runtime._plan.steps
    reporter = runtime._plan.agents[-1]
    reviewer = reporter.model_copy(update={"id": "reviewer", "logical_model": "review"})
    runtime._plan = runtime._plan.model_copy(update={
        "agents": (*runtime._plan.agents, reviewer),
        "steps": (worker, report.model_copy(update={"id": final_id, "reviewer": "reviewer"})),
    })
    return runtime


@dataclass
class NativeCase:
    runtime: adapter.CrewDispatchRuntime
    context: TaskContext
    events: tuple[RunEvent, ...]
    repository: InMemoryArtifactRepository
    gateway: ReviewGateway
    harness: ActualCapabilityHarness
    error: adapter.RuntimeExecutionError | asyncio.CancelledError | None

    @property
    def candidate(self) -> Artifact:
        return next(item.artifact for item in self.events if item.artifact is not None
                    and item.artifact.producer == "reporter" and item.artifact.type == "text")

    def reviewer_checkpoint(self, status: str) -> tuple[int, RuntimeCheckpoint]:
        for index, event in enumerate(self.events):
            checkpoint = event.checkpoint
            if checkpoint is not None and any(
                row["step_id"] == self.runtime._plan.final_step.id and row["purpose"] == "review"
                and row["status"] == status for row in read_rows(checkpoint.state["models"]).values()
            ):
                return index, checkpoint
        raise AssertionError(f"native fixture did not produce reviewer {status}")


async def make_case(
    tmp_path: Path, parser: types.ModuleType, outcome: str = "approve", *, final_id: str = "report",
) -> NativeCase:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = ReviewGateway(outcome)
    runtime = reviewed_runtime(gateway, harness, repository, parser, final_id=final_id)
    task = context()
    events: list[RunEvent] = []
    error = None
    try:
        async for event in runtime.run(task):
            events.append(event)
    except (adapter.RuntimeExecutionError, asyncio.CancelledError) as caught:
        error = caught
    case = NativeCase(runtime, task, tuple(events), repository, gateway, harness, error)
    assert case.candidate.content["text"] == REPORT
    assert set(case.candidate.content) == {"text", "delivery_report_binding"}
    # Independent native binding/causal verification is a prerequisite, not the
    # expected publication result. A fake text-only candidate cannot satisfy it.
    proof = final_evidence(case.candidate, task, tuple(
        event.artifact for event in events if event.artifact is not None
    ))
    assert read_map(read_map(proof["verification"])["verification"])["status"] == "passed"
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "project.verify", "workspace.bundle",
    ]
    assert len(gateway.requests) == 6
    if outcome == "approve":
        assert error is None
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    else:
        assert error is not None
        assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    return case


@pytest.fixture
async def native_case(tmp_path: Path, installed_parser: types.ModuleType) -> NativeCase:  # noqa: F811
    return await make_case(tmp_path, installed_parser)


async def stream(events: Sequence[RunEvent]) -> AsyncIterator[RunEvent]:
    for event in events:
        yield event


async def published(case: NativeCase, events: tuple[RunEvent, ...]) -> list[tuple[RunEvent, ...]]:
    groups = []
    try:
        async for group in _delivery_publication_batches(stream(events), case.context, runtime=case.runtime):
            groups.append(group)
    except RuntimeError as error:
        # Prefix observations inspect already committable groups. The separate
        # EOF regression requires the actual publisher to raise for truncation.
        if str(error) != "crew report publication incomplete stream":
            raise
    return groups


def public_json(event: RunEvent) -> str:
    return json.dumps(_public_event_payload(event.to_payload()), sort_keys=True, ensure_ascii=True)


def terminal_index(case: NativeCase) -> int:
    return next(index for index, event in enumerate(case.events)
                if event.checkpoint is not None and event.checkpoint.state["terminal"] is True
                and event.checkpoint.state["phase"] == "completed"
                and "report" in sequence(event.checkpoint.state["completed"]))


def test_fixture_has_native_report_review_and_completed_checkpoint(native_case: NativeCase) -> None:
    case = native_case
    terminal = case.events[terminal_index(case)].checkpoint
    assert terminal is not None
    assert read_map(terminal.state["artifact_refs"])["report"] == {
        "id": str(case.candidate.id), "sha256": case.candidate.content_sha256,
    }
    assert any(event.kind is EventKind.REVIEW_COMPLETED and event.payload["verdict"] == "approve"
               for event in case.events)
    assert case.reviewer_checkpoint("prepared")[0] < case.reviewer_checkpoint("running")[0]


@pytest.mark.parametrize("carrier", ["model_response", "candidate", "review_inputs", "step_inputs"])
async def test_report_body_carriers_are_not_public_before_final_checkpoint(
    native_case: NativeCase, carrier: str,
) -> None:
    case = native_case
    prefix = case.events[:terminal_index(case)]
    selectors: dict[str, Callable[[RunEvent], bool]] = {
        "model_response": lambda item: item.artifact is not None
        and item.artifact.type == "model_response" and item.artifact.producer == "reporter",
        "candidate": lambda item: item.artifact == case.candidate,
        "review_inputs": lambda item: item.kind is EventKind.REVIEW_COMPLETED
        and case.candidate in item.inputs,
        "step_inputs": lambda item: item.kind is EventKind.STEP_COMPLETED
        and item.step_id == "report" and case.candidate in item.inputs,
    }
    selected = selectors[carrier]
    original = next(item for item in prefix if selected(item))
    assert REPORT in json.dumps(original.to_payload()), "native fixture must contain the body carrier"
    groups = await published(case, prefix)
    leaks = [item.sequence for group in groups for item in group
             if selected(item) and REPORT in public_json(item)]
    assert leaks == [], f"{carrier} exposed report body before native completed checkpoint: {leaks}"


@pytest.mark.parametrize("phase", ["prepared", "running"])
async def test_reviewer_intent_checkpoint_is_durable_without_releasing_report(
    native_case: NativeCase, phase: str,
) -> None:
    case = native_case
    index, checkpoint = case.reviewer_checkpoint(phase)
    groups = await published(case, case.events[:index + 1])
    emitted = [item for group in groups for item in group]
    assert any(item.checkpoint == checkpoint for item in emitted), "do not block reviewer intent durability"
    assert checkpoint.state["terminal"] is False and "report" not in sequence(checkpoint.state["completed"])
    assert len(read_map(checkpoint.state["tools"])) == 3
    assert all(row["status"] == "succeeded" for row in read_rows(checkpoint.state["tools"]).values())
    assert not any(REPORT in public_json(item) for item in emitted), f"report released at reviewer {phase}"


@pytest.mark.parametrize("outcome", ["reject", "revise", "unknown"])
async def test_unsuccessful_reviewer_never_releases_final_report(
    tmp_path: Path, installed_parser: types.ModuleType, outcome: str,  # noqa: F811
) -> None:
    case = await make_case(tmp_path, installed_parser, outcome)
    if outcome == "unknown":
        # The gateway was entered without a receipt. No invented unknown row or
        # synthetic event is needed: the real native ledger remains running.
        case.reviewer_checkpoint("running")
        assert not any(item.kind is EventKind.REVIEW_COMPLETED for item in case.events)
        checkpoint = await case.runtime.save_checkpoint()
        reviews = [row for row in read_rows(checkpoint.state["models"]).values() if row["purpose"] == "review"]
        assert len(reviews) == 1 and read_map(reviews[0])["status"] == "running"
    else:
        assert any(item.kind is EventKind.REVIEW_COMPLETED and item.payload["verdict"] == outcome
                   for item in case.events)
    groups = await published(case, case.events)
    assert not any(REPORT in public_json(item) for group in groups for item in group), (
        f"native reviewer {outcome} still exposed a final report body"
    )


async def test_approve_and_step_completed_wait_for_one_final_checkpoint_commit(native_case: NativeCase) -> None:
    case = native_case
    boundary = terminal_index(case)
    groups = await published(case, case.events[:boundary + 1])
    body_groups = [group for group in groups if any(REPORT in public_json(item) for item in group)]
    assert len(body_groups) == 1, "all native report body carriers need one completed publication boundary"
    assert case.events[boundary] in body_groups[0]
    assert sum(item.artifact == case.candidate for item in body_groups[0]) == 1


def test_bound_native_review_candidate_matches_its_receipt(native_case: NativeCase) -> None:
    case = native_case
    completion = next(item for item in case.gateway.completions if item.response.text == REPORT)
    candidate = case.candidate
    assert adapter.CrewDispatchRuntime._review_candidate_matches(
        candidate, case.runtime._plan.steps[-1], completion, candidate.source_ids, candidate.version,
    ), "legitimate delivery_report_binding makes the native review candidate unmatchable"


async def test_prepared_bound_report_cold_recovers_without_replaying_writer_or_tools(
    native_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = native_case
    _, checkpoint = case.reviewer_checkpoint("prepared")
    # Reconstruct only artifacts in this exact historical CP, not later review
    # receipts from the completed fixture. Use the real repository API and hash.
    references = tuple(ArtifactReference(UUID(key), string_value(digest))
                       for key, digest in read_map(checkpoint.state["artifact_registry"]).items())
    artifacts = await case.repository.get_many(case.context.tenant_id, case.context.run_id, references)
    cold_repository = InMemoryArtifactRepository()
    for artifact in artifacts:
        await cold_repository.put(case.context.tenant_id, case.context.run_id, artifact)
    cold_gateway = WorkspaceDeliverySequenceGateway((ModelResponse(
        text='{"verdict":"approve","feedback":"Owned recovery approval"}', usage=TokenUsage(1, 1, 2),
    ),))
    cold = reviewed_runtime(cold_gateway, case.harness, cold_repository, installed_parser)
    recovered: list[RunEvent] = []
    await cold.restore_checkpoint(RuntimeCheckpoint.from_payload(checkpoint.to_payload()))
    async for event in cold.run(case.context.model_copy(update={"checkpoint": checkpoint})):
        recovered.append(event)
    assert recovered[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in cold_gateway.requests] == ["review"]
    assert len(case.harness.calls) == 3, "prepared review recovery replayed completed tools"
    saved = await cold.save_checkpoint()
    assert read_map(saved.state["artifact_refs"])["report"] == {
        "id": str(case.candidate.id), "sha256": case.candidate.content_sha256,
    }


@pytest.mark.parametrize("defect", ["extra_content", "run", "tenant", "model", "stripped_binding"])
async def test_forged_prepared_report_is_rejected_before_any_model_or_tool_replay(
    native_case: NativeCase, installed_parser: types.ModuleType, defect: str,  # noqa: F811
) -> None:
    from uuid import uuid4

    case = native_case
    _, checkpoint = case.reviewer_checkpoint("prepared")
    content = object_map(case.candidate.to_payload()["content"])
    if defect == "extra_content":
        content["arbitrary_private_payload"] = "not a native report field"
    elif defect == "stripped_binding":
        content.pop("delivery_report_binding")
    else:
        key = {"run": "run_id", "tenant": "tenant_id", "model": "model_artifact_id"}[defect]
        object_map(content["delivery_report_binding"])[key] = str(uuid4())
    candidate = Artifact(id=case.candidate.id, type=case.candidate.type,
        producer=case.candidate.producer, version=case.candidate.version,
        source_ids=case.candidate.source_ids, provenance=case.candidate.provenance, content=json_map(content))
    payload = object_map(checkpoint.to_payload())
    object_map(object_map(payload["state"])["artifact_registry"])[str(candidate.id)] = candidate.content_sha256
    payload.pop("state_sha256")
    for key in ("id", "run_id", "tenant_id"):
        payload[key] = UUID(string_value(payload[key]))
    payload["mode"] = type(checkpoint.mode)(string_value(payload["mode"]))
    changed = RuntimeCheckpoint.model_validate(payload)
    references = tuple(ArtifactReference(UUID(key), string_value(digest))
                       for key, digest in read_map(checkpoint.state["artifact_registry"]).items())
    artifacts = await case.repository.get_many(case.context.tenant_id, case.context.run_id, references)
    repository = InMemoryArtifactRepository()
    for artifact in artifacts:
        await repository.put(case.context.tenant_id, case.context.run_id,
                             candidate if artifact.id == candidate.id else artifact)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = reviewed_runtime(gateway, case.harness, repository, installed_parser)
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(changed)
        _ = [event async for event in cold.run(case.context.model_copy(update={"checkpoint": changed}))]
    assert not gateway.requests, "forged candidate reached the reviewer/model boundary"
    assert len(case.harness.calls) == 3


@asynccontextmanager
async def sql_repository(task: TaskContext) -> AsyncIterator[SQLCase]:
    """Actual repository SQL, with only PostgreSQL column types adapted to SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model in (RunRow, RunEventRow, RunArtifactRow, RunCheckpointRow, RunStepRow):
        table = model.__table__
        assert isinstance(table, Table)
        columns = [Column(c.name, JSON() if isinstance(c.type, JSONB)
                          else String(32) if isinstance(c.type, PGUUID) else c.type,
                          primary_key=c.primary_key, nullable=c.nullable,
                          server_default=c.server_default) for c in table.columns]
        constraints = [UniqueConstraint(*(c.name for c in item.columns))
                       for item in table.constraints if isinstance(item, UniqueConstraint)]
        Table(model.__tablename__, metadata, *columns, *constraints)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as session:
            session.add(RunRow(id=task.run_id, tenant_id=task.tenant_id, actor_id=task.actor_id,
                actor_role="operator", request=task.request, mode=task.mode.value,
                status="running", version=1, routing_decision=dict(task.routing_decision)))
        yield RunRepository(factory), factory, engine
    finally:
        await engine.dispose()


async def persist_group(repository: RunRepository, task: TaskContext, group: tuple[RunEvent, ...]) -> None:
    async with await repository.run_transaction() as session, session.begin():
        await repository.get_for_update(session, task.run_id)
        for item in group:
            sequence = await repository.next_event_sequence(session, task.run_id)
            await repository.persist_event(session, tenant_id=task.tenant_id, run_id=task.run_id,
                event=item.model_copy(update={"sequence": sequence}))


async def test_real_repository_rolls_back_only_final_publication_and_keeps_private_evidence(
    native_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = native_case
    async with sql_repository(case.context) as (repository, factory, engine):
        groups = await published(case, case.events[:terminal_index(case) + 1])

        class SaveFailed(RuntimeError):
            pass

        def fail_final(connection: Connection, cursor: object, statement: str, parameters: object,
                       execution_context: ExecutionContext, many: bool) -> None:
            if statement.startswith("INSERT INTO " + RunCheckpointRow.__tablename__):
                raise SaveFailed("owned final checkpoint insertion failure")

        for group in groups:
            if any(item.artifact == case.candidate for item in group):
                sql_event.listen(engine.sync_engine, "before_cursor_execute", fail_final)
                try:
                    with pytest.raises(SaveFailed):
                        await persist_group(repository, case.context, group)
                finally:
                    sql_event.remove(engine.sync_engine, "before_cursor_execute", fail_final)
            else:
                await persist_group(repository, case.context, group)
        raw = await repository.raw_events(case.context.tenant_id, case.context.run_id)
        assert raw and not any(REPORT in public_json(item) for item in raw)
        assert not any(item.checkpoint is not None and item.checkpoint.state["phase"] == "completed"
                       for item in raw)
        async with factory() as session:
            assert present(await session.scalar(select(func.count()).select_from(RunCheckpointRow))) > 0
            assert await session.scalar(select(func.count()).select_from(RunArtifactRow).where(
                RunArtifactRow.id == case.candidate.id,
            )) == 0
        reference = ArtifactReference(case.candidate.id, case.candidate.content_sha256)
        assert await case.repository.get_many(case.context.tenant_id, case.context.run_id,
                                             (reference,)) == (case.candidate,)
        async with factory() as session:
            checkpoint = await repository.latest_checkpoint(session, tenant_id=case.context.tenant_id,
                                                             run_id=case.context.run_id)
        assert checkpoint is not None and checkpoint.state["phase"] == "running"
        gateway = WorkspaceDeliverySequenceGateway(())
        cold = reviewed_runtime(gateway, case.harness, case.repository, installed_parser)
        await cold.restore_checkpoint(checkpoint)
        task = case.context.model_copy(update={"checkpoint": checkpoint})
        async for group in _delivery_publication_batches(cold.run(task), task, runtime=cold):
            await persist_group(repository, task, group)
        recovered = await repository.raw_events(task.tenant_id, task.run_id)
        assert sum(item.artifact == case.candidate for item in recovered) == 1
        assert sum(item.kind is EventKind.RUNTIME_COMPLETED for item in recovered) == 1
        assert not gateway.requests and len(case.harness.calls) == 3


async def test_real_repository_publication_sequences_and_cold_recovery_do_not_duplicate_report(
    native_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = native_case
    boundary = terminal_index(case)
    expected = case.events[boundary].checkpoint
    assert expected is not None
    async with sql_repository(case.context) as (repository, factory, _):
        for group in await published(case, case.events[:boundary + 1]):
            await persist_group(repository, case.context, group)
        cold_repository = RunRepository(factory)
        raw = await cold_repository.raw_events(case.context.tenant_id, case.context.run_id)
        assert [item.sequence for item in raw] == list(range(1, len(raw) + 1))
        assert sum(item.artifact == case.candidate for item in raw) == 1
        # CP native cursor/hash are unchanged even though its public predecessors
        # were delayed and the repository has reassigned every DB sequence.
        async with factory() as session:
            checkpoint = await cold_repository.latest_checkpoint(session, tenant_id=case.context.tenant_id,
                                                                  run_id=case.context.run_id)
        assert checkpoint == expected
        assert checkpoint is not None
        assert checkpoint.state["next_sequence"] == expected.state["next_sequence"]
        gateway = WorkspaceDeliverySequenceGateway(())
        cold = reviewed_runtime(gateway, case.harness, case.repository, installed_parser)
        await cold.restore_checkpoint(checkpoint)
        recovered = tuple([item async for item in cold.run(case.context.model_copy(update={"checkpoint": checkpoint}))])
        assert [item.kind for item in recovered] == [EventKind.RUNTIME_COMPLETED]
        for group in [group async for group in _delivery_publication_batches(
            stream(recovered), case.context.model_copy(update={"checkpoint": checkpoint}), runtime=cold,
        )]:
            await persist_group(cold_repository, case.context, group)
        final = await cold_repository.raw_events(case.context.tenant_id, case.context.run_id)
        assert [item.sequence for item in final] == list(range(1, len(raw) + 2))
        assert sum(item.artifact == case.candidate for item in final) == 1
        assert not gateway.requests and len(case.harness.calls) == 3


@pytest.mark.parametrize("final_id", ["report.v1", "1report"])
async def test_native_plan_identifiers_survive_bound_prepared_review_recovery(
    tmp_path: Path, installed_parser: types.ModuleType, final_id: str,  # noqa: F811
) -> None:
    case = await make_case(tmp_path, installed_parser, final_id=final_id)
    _, checkpoint = case.reviewer_checkpoint("prepared")
    gateway = WorkspaceDeliverySequenceGateway((ModelResponse(
        text='{"verdict":"approve"}', usage=TokenUsage(1, 1, 2),
    ),))
    cold = reviewed_runtime(gateway, case.harness, case.repository, installed_parser, final_id=final_id)
    await cold.restore_checkpoint(checkpoint)
    events = [event async for event in cold.run(case.context.model_copy(update={"checkpoint": checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 1 and len(case.harness.calls) == 3


async def test_native_report_revision_keeps_the_single_model_attempt_boundary(
    tmp_path: Path, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    old = "OWNED_REJECTED_REPORT_BODY_DO_NOT_PUBLISH"
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = ReviewGateway("approve")
    gateway.responses = (*responses(old),
        ModelResponse(text='{"verdict":"revise","feedback":"Please revise"}', usage=TokenUsage(1, 1, 2)),
        ModelResponse(text=REPORT, usage=TokenUsage(1, 1, 2)),
        ModelResponse(text='{"verdict":"approve"}', usage=TokenUsage(1, 1, 2)))
    runtime = reviewed_runtime(gateway, harness, repository, installed_parser)
    runtime._plan = runtime._plan.model_copy(update={"steps": (
        runtime._plan.steps[0], runtime._plan.final_step.model_copy(update={"reviewer_retries": 1}),
    )})
    task = context()
    events = []
    async for event in runtime.run(task):
        events.append(event)
    groups = [group async for group in _delivery_publication_batches(stream(events), task, runtime=runtime)]
    public = [item for group in groups for item in group]
    assert not any(old in public_json(item) for item in public)
    assert any(REPORT in public_json(item) for item in public)
    assert sum(item.kind is EventKind.RUNTIME_COMPLETED for item in public) == 1
    assert len(gateway.requests) == 8 and len(harness.calls) == 3
    saved = await runtime.save_checkpoint()
    assert len(read_map(saved.state["models"])) == 8


async def test_pending_without_terminal_checkpoint_fails_closed(native_case: NativeCase) -> None:
    case = native_case
    prefix = case.events[:case.reviewer_checkpoint("prepared")[0] + 1]
    with pytest.raises(RuntimeError, match="incomplete stream"):
        _ = [group async for group in _delivery_publication_batches(
            stream(prefix), case.context, runtime=case.runtime,
        )]


async def test_full_native_stream_delivers_exactly_one_validated_completed_event(native_case: NativeCase) -> None:
    case = native_case
    groups = await published(case, case.events)
    completed = [item for group in groups for item in group if item.kind is EventKind.RUNTIME_COMPLETED]
    assert len(completed) == 1 and completed[0].inputs == (case.candidate,)
    assert sum(item.artifact == case.candidate for group in groups for item in group) == 1


async def test_prepared_db_checkpoint_restarts_with_full_public_report_and_no_replay(
    native_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = native_case
    index, prepared = case.reviewer_checkpoint("prepared")
    async with sql_repository(case.context) as (repository, factory, _):
        iterator = _delivery_publication_batches(stream(case.events[:index + 1]), case.context, runtime=case.runtime)
        try:
            async for group in iterator:
                await persist_group(repository, case.context, group)
                if any(item.checkpoint == prepared for item in group):
                    break  # Simulated worker death: do not pretend this prefix is a normal EOF.
        finally:
            await iterator.aclose()
        cold_repository = RunRepository(factory)
        async with factory() as session:
            checkpoint = await cold_repository.latest_checkpoint(session, tenant_id=case.context.tenant_id,
                                                                  run_id=case.context.run_id)
        assert checkpoint == prepared
        assert checkpoint is not None
        before = await cold_repository.raw_events(case.context.tenant_id, case.context.run_id)
        assert not any(REPORT in public_json(item) for item in before)
        gateway = WorkspaceDeliverySequenceGateway((ModelResponse(
            text='{"verdict":"approve"}', usage=TokenUsage(1, 1, 2),
        ),))
        cold = reviewed_runtime(gateway, case.harness, case.repository, installed_parser)
        await cold.restore_checkpoint(checkpoint)
        task = case.context.model_copy(update={"checkpoint": checkpoint})
        async for group in _delivery_publication_batches(cold.run(task), task, runtime=cold):
            await persist_group(cold_repository, task, group)
        raw = await cold_repository.raw_events(task.tenant_id, task.run_id)
        assert sum(item.artifact == case.candidate for item in raw) == 1
        model_id = read_map(case.candidate.content["delivery_report_binding"])["model_artifact_id"]
        assert sum(item.artifact is not None and str(item.artifact.id) == model_id for item in raw) == 1
        assert sum(item.kind is EventKind.RUNTIME_COMPLETED for item in raw) == 1
        assert len(gateway.requests) == 1 and len(case.harness.calls) == 3


async def test_actual_run_service_completes_through_owned_configured_admission_chain(
    tmp_path: Path, installed_parser: types.ModuleType, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    from agent_hub.domain.runs import RunStatus, TaskMode
    from agent_hub.runs.service import RunService
    from agent_hub.runtime.defaults import ConfigBackedDispatchRuntime, _PlannedRuntime
    from agent_hub.runtime.engineering_admission import EngineeringAdmissionRuntime
    from agent_hub.runtime.project_scale_artifact import ProjectScaleArtifactPreseedRuntime
    from agent_hub.runtime.registry import RuntimeRegistry
    from tests.unit.runs import test_terminal_hooks as service_fixture

    task = context()
    monkeypatch.setattr(service_fixture, "TENANT_ID", task.tenant_id)
    monkeypatch.setattr(service_fixture, "ACTOR_ID", task.actor_id)
    repository = service_fixture.ExecutableFakeRepository(routing_decision=dict(task.routing_decision))
    repository.run_id = repository.row.id = task.run_id
    repository.row.request = task.request
    repository.row.actor_role = present(task.actor_role).value
    gateway = ReviewGateway("approve")
    harness = ActualCapabilityHarness(tmp_path, Executor())
    private = InMemoryArtifactRepository()
    native = reviewed_runtime(gateway, harness, private, installed_parser)
    wrapped = EngineeringAdmissionRuntime(_PlannedRuntime(
        ProjectScaleArtifactPreseedRuntime(native, harness_tool_gateway=None),
        mode=TaskMode.DISPATCH, main_agent_model="general", roles=(), steps=(),
    ), None)

    class Configured(ConfigBackedDispatchRuntime):
        builds = 0

        async def _runtime_for(self, observed: TaskContext) -> ExecutionRuntime:
            self.builds += 1
            assert observed.tenant_id == task.tenant_id and observed.run_id == task.run_id
            assert observed.execution_token is not None
            return wrapped

    configured = Configured(config_service=cast(Any, object()), secret_service=cast(Any, object()),
                            capacity_factory=cast(Any, object()), transport=cast(Any, object()))
    service = RunService(cast(RunRepository, repository), runtime_registry=RuntimeRegistry((configured,)),
        router=None, task_queue=cast(Any, object()), runtime_token_budget=10_000)
    result = await service.execute(task.run_id)
    assert result.status is RunStatus.COMPLETED
    assert sum(item.kind is EventKind.RUNTIME_COMPLETED for item in repository.event_log) == 1
    reports = [item.artifact for item in repository.event_log if item.artifact is not None
               and "delivery_report_binding" in item.artifact.content]
    assert len(reports) == 1 and reports[0].content["text"] == REPORT
    assert configured.builds == 1 and len(gateway.requests) == 6 and len(harness.calls) == 3


async def test_completed_event_without_native_completed_checkpoint_is_rejected(native_case: NativeCase) -> None:
    case = native_case
    with pytest.raises(RuntimeError, match="no validated publication checkpoint"):
        _ = [group async for group in _delivery_publication_batches(
            stream((case.events[-1],)), case.context, runtime=case.runtime,
        )]
