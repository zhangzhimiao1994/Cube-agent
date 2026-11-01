"""Native report revisions: private history, cold receipts and final publication."""

from __future__ import annotations

import json
import types
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    _freeze_object,
)
from agent_hub.runtime.crew import adapter, delivery_report
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.runs.reviewed_report_revision_typed_fixture import (
    integer_value,
    mutable_rows,
    object_array,
    object_map,
    read_map,
    read_rows,
    string_value,
)
from tests.unit.runs.test_crew_report_publication_red_oct29 import (
    REPORT,
    NativeCase,
    ReviewGateway,
    persist_group,
    public_json,
    reviewed_runtime,
    sql_repository,
    stream,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
from tests.unit.runtime.crew.test_delivery_report import context, responses, subject
from tests.unit.runtime.crew.test_engineering_delivery_execution import ActualCapabilityHarness
from tests.unit.runtime.crew.test_framework_parser_boundary import (
    installed_parser,  # noqa: F401
    offline_only,  # noqa: F401
)

OLD = "PRIVATE_FIRST_DRAFT_REPLACED_BY_NATIVE_REVIEW"
APPROVE = ModelResponse(text='{"verdict":"approve"}', usage=TokenUsage(1, 1, 2))
SECOND = ModelResponse(text=REPORT, usage=TokenUsage(1, 1, 2))


def runtime(
    gateway: WorkspaceDeliverySequenceGateway,
    harness: ActualCapabilityHarness,
    repository: InMemoryArtifactRepository,
    parser: types.ModuleType,
) -> adapter.CrewDispatchRuntime:
    runner = reviewed_runtime(gateway, harness, repository, parser)
    runner._plan = runner._plan.model_copy(
        update={
            "steps": (
                runner._plan.steps[0],
                runner._plan.final_step.model_copy(update={"reviewer_retries": 1}),
            )
        }
    )
    return runner


@pytest.fixture
async def revision_case(tmp_path: Path, installed_parser: types.ModuleType) -> NativeCase:  # noqa: F811
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = ReviewGateway("approve")
    gateway.responses = (
        *responses(OLD),
        ModelResponse(
            text=json.dumps({"verdict": "revise", "feedback": "Replace this draft: " + OLD}),
            usage=TokenUsage(1, 1, 2),
        ),
        SECOND,
        APPROVE,
    )
    runner = runtime(gateway, harness, repository, installed_parser)
    task = context()  # Real plan opt-in, no test-only routing marker.
    events = tuple([event async for event in runner.run(task)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 8 and len(harness.calls) == 3
    return NativeCase(runner, task, events, repository, gateway, harness, None)


def boundary(case: NativeCase, phase: str) -> RuntimeCheckpoint:
    for event in case.events:
        cp = event.checkpoint
        if cp is None or read_map(cp.state["retries"]).get("report") != 1:
            continue
        rows = [
            row
            for row in read_rows(cp.state["models"]).values()
            if row["step_id"] == "report"
            and row["purpose"] == "step"
            and integer_value(row["attempt"]) > 0
        ]
        if (phase == "feedback" and not rows) or (rows and rows[-1]["status"] == phase):
            return RuntimeCheckpoint.from_payload(cp.to_payload())
    raise AssertionError(f"missing real native revision checkpoint: {phase}")


async def private_prefix(case: NativeCase, cp: RuntimeCheckpoint) -> tuple[Artifact, ...]:
    refs = tuple(
        ArtifactReference(UUID(key), string_value(digest))
        for key, digest in read_map(cp.state["artifact_registry"]).items()
    )
    return await case.repository.get_many(case.context.tenant_id, case.context.run_id, refs)


async def repository_from(
    case: NativeCase, artifacts: tuple[Artifact, ...]
) -> InMemoryArtifactRepository:
    repository = InMemoryArtifactRepository()
    for artifact in artifacts:
        await repository.put(case.context.tenant_id, case.context.run_id, artifact)
    return repository


def changed_checkpoint(cp: RuntimeCheckpoint, state: dict[str, object]) -> RuntimeCheckpoint:
    # Keep every original envelope field; rebuild only the changed state's hash,
    # with the same state limit/validator as RuntimeCheckpoint.
    return RuntimeCheckpoint(
        id=cp.id,
        runtime_type=cp.runtime_type,
        runtime_version=cp.runtime_version,
        run_id=cp.run_id,
        tenant_id=cp.tenant_id,
        mode=cp.mode,
        state=_freeze_object(state, name="checkpoint state", max_nodes=2_000_000),
    )


async def test_fresh_reviewed_plan_publishes_only_approved_revision(
    revision_case: NativeCase,
) -> None:
    case = revision_case
    assert delivery_report.REVISION_POLICY not in case.context.routing_decision
    groups = [
        group
        async for group in _delivery_publication_batches(
            stream(case.events), case.context, runtime=case.runtime
        )
    ]
    public = [event for group in groups for event in group]
    assert not any(OLD in public_json(event) for event in public)
    assert sum(event.kind is EventKind.RUNTIME_COMPLETED for event in public) == 1
    saved = await case.runtime.save_checkpoint()
    assert saved.state[delivery_report.REVISION_POLICY] == 1
    artifacts = await private_prefix(case, saved)
    assert any(item.content.get("text") == OLD for item in artifacts)
    assert any(item.type == "review_feedback" and OLD in str(item.content) for item in artifacts)
    refs = read_map(read_map(saved.state["artifact_refs"])["report"])
    final = next(item for item in artifacts if str(item.id) == refs["id"])
    assert final.version == 2 and final.content["text"] == REPORT
    terminal_group = next(
        group for group in groups if any(event.checkpoint == saved for event in group)
    )
    assert any(event.artifact == final for event in terminal_group)


async def test_public_revision_keeps_review_and_retry_metadata_without_private_bodies(
    revision_case: NativeCase,
) -> None:
    case = revision_case
    groups = [
        group
        async for group in _delivery_publication_batches(
            stream(case.events), case.context, runtime=case.runtime
        )
    ]
    public = [event for group in groups for event in group]
    reviews = [
        event for event in public if event.payload.get("source_event_kind") == "review.completed"
    ]
    retries = [
        event
        for event in public
        if event.kind is EventKind.STEP_RETRYING and event.actor == "reporter"
    ]
    assert [event.payload["verdict"] for event in reviews] == ["revise", "approve"]
    assert len(retries) == 1 and retries[0].payload["attempt"] == 2
    terminal = await case.runtime.save_checkpoint()
    original_reviews = [event for event in case.events if event.kind is EventKind.REVIEW_COMPLETED]
    for projected, original in zip(reviews, original_reviews, strict=True):
        candidate = original.inputs[0]
        assert projected.kind == "decision.report_review"
        assert projected.run_id == original.run_id
        assert projected.payload["actor"] == original.actor == "reviewer"
        assert projected.payload["step_id"] == "report"
        assert projected.actor is None and projected.step_id is None  # Extension-event contract.
        assert not projected.inputs and projected.artifact is None
        assert projected.payload["source_event_sequence"] == original.sequence
        assert projected.payload["candidate_artifact_id"] == str(candidate.id)
        assert projected.payload["candidate_sha256"] == candidate.content_sha256
        assert projected.payload["attempt"] == candidate.version
        row = read_rows(terminal.state["models"])[string_value(projected.payload["model_call_key"])]
        assert row["purpose"] == "review" and row["actor"] == original.actor
        assert row["artifact_id"] == projected.payload["review_model_artifact_id"]
        assert row["sha256"] == projected.payload["review_model_sha256"]
        assert RunEvent.from_payload(projected.to_payload()) == projected
    retry = retries[0]
    original_retry = next(
        event
        for event in case.events
        if event.kind is EventKind.STEP_RETRYING and event.actor == "reporter"
    )
    assert retry.payload["source_event_kind"] == "step.retrying"
    assert retry.payload["source_event_sequence"] == original_retry.sequence
    assert retry.step_id == original_retry.step_id == "report"
    assert RunEvent.from_payload(retry.to_payload()) == retry
    assert not any(
        OLD in public_json(event) or '"feedback"' in public_json(event) for event in public
    )
    publication = next(
        group for group in groups if any(event.checkpoint == terminal for event in group)
    )
    assert all(event not in publication for event in (*reviews, *retries))
    for projected in (*reviews, *retries):
        earlier = public[: public.index(projected)]
        receipt = next(
            event.checkpoint for event in reversed(earlier) if event.checkpoint is not None
        )
        assert receipt is not None
        row = read_rows(receipt.state["models"])[string_value(projected.payload["model_call_key"])]
        assert row["status"] == "succeeded" and row["actor"] == "reviewer"
        assert row["sha256"] == projected.payload["review_model_sha256"]
    assert all(event.inputs for event in original_reviews), "native input contract was rewritten"
    assert len(case.gateway.requests) == 8 and len(case.harness.calls) == 3


@pytest.mark.parametrize("crash_after", ["review", "retry", "prepared"])
async def test_real_db_prefix_crash_restore_preserves_merged_review_retry_history(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
    crash_after: str,
) -> None:
    case = revision_case
    if crash_after == "prepared":
        stop = next(
            i
            for i, event in enumerate(case.events)
            if event.checkpoint == boundary(case, "prepared")
        )
    else:
        stop = next(
            i
            for i, event in enumerate(case.events)
            if (
                event.kind is EventKind.REVIEW_COMPLETED
                if crash_after == "review"
                else event.kind is EventKind.STEP_RETRYING and event.actor == "reporter"
            )
        )

    class WorkerDied(Exception):
        pass

    async def interrupted() -> AsyncIterator[RunEvent]:
        for event in case.events[: stop + 1]:
            yield event
        raise WorkerDied

    async with sql_repository(case.context) as (repository, factory, _):
        with pytest.raises(WorkerDied):
            async for group in _delivery_publication_batches(
                interrupted(), case.context, runtime=case.runtime
            ):
                await persist_group(repository, case.context, group)
        before = await repository.events(case.context.tenant_id, case.context.run_id)
        initial_reviews = [
            event
            for event in before
            if read_map(event["payload"]).get("source_event_kind") == "review.completed"
        ]
        initial_retries = [event for event in before if event["kind"] == "step.retrying"]
        assert [read_map(event["payload"])["verdict"] for event in initial_reviews] == ["revise"]
        assert len(initial_retries) == (0 if crash_after == "review" else 1)
        assert OLD not in json.dumps(before, default=str) and REPORT not in json.dumps(
            before, default=str
        )
        async with factory() as session:
            cp = await repository.latest_checkpoint(
                session, tenant_id=case.context.tenant_id, run_id=case.context.run_id
            )
        assert cp is not None and cp.state["terminal"] is False
        prefix = await repository.raw_events(case.context.tenant_id, case.context.run_id)
        gateway = WorkspaceDeliverySequenceGateway((SECOND, APPROVE))
        cold = runtime(
            gateway,
            case.harness,
            await repository_from(case, await private_prefix(case, cp)),
            installed_parser,
        )
        await cold.restore_checkpoint(cp)
        task = case.context.model_copy(update={"checkpoint": cp})
        native = tuple([event async for event in cold.run(task)])
        for group in [
            group
            async for group in _delivery_publication_batches(
                stream(native), task, runtime=cold, recovery_events=prefix
            )
        ]:
            await persist_group(repository, task, group)
        merged = await repository.events(task.tenant_id, task.run_id)
        reviews = [
            event
            for event in merged
            if read_map(event["payload"]).get("source_event_kind") == "review.completed"
        ]
        retries = [event for event in merged if event["kind"] == "step.retrying"]
        assert [read_map(event["payload"])["verdict"] for event in reviews] == ["revise", "approve"]
        assert len(retries) == 1 and read_map(retries[0]["payload"])["attempt"] == 2
        identities = [
            (read_map(event["payload"])["source_event_kind"], read_map(event["payload"])["model_call_key"])
            for event in (*reviews, *retries)
        ]
        assert len(identities) == len(set(identities)) == 3
        assert reviews[0] == initial_reviews[0], (
            "cold recovery rewrote the persisted native source coordinates"
        )
        assert OLD not in json.dumps(merged, default=str)
        assert '"feedback"' not in json.dumps(merged, default=str)
        assert sum(event["kind"] == "runtime.completed" for event in merged) == 1
        assert [event["sequence"] for event in merged] == list(range(1, len(merged) + 1))
        assert len(gateway.requests) == 2 and len(case.harness.calls) == 3
        final = await cold.save_checkpoint()
        async with factory() as session:
            stored = await repository.latest_checkpoint(
                session, tenant_id=task.tenant_id, run_id=task.run_id
            )
        assert stored is not None
        assert stored == final and stored.state_sha256 == final.recompute_state_sha256()


@pytest.mark.parametrize("phase", ["feedback", "prepared", "running", "succeeded", "completed"])
async def test_revision_cold_restore_keeps_receipts_budget_and_unknown_stop(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
    phase: str,
) -> None:
    case = revision_case
    final = await case.runtime.save_checkpoint()
    cp = final if phase == "completed" else boundary(case, phase)
    artifacts = await private_prefix(case, cp)
    remaining = {
        "feedback": (SECOND, APPROVE),
        "prepared": (SECOND, APPROVE),
        "running": (),
        "succeeded": (APPROVE,),
        "completed": (),
    }[phase]
    gateway = WorkspaceDeliverySequenceGateway(remaining)
    cold = runtime(gateway, case.harness, await repository_from(case, artifacts), installed_parser)
    await cold.restore_checkpoint(cp)
    task = case.context.model_copy(update={"checkpoint": cp})
    if phase == "running":
        with pytest.raises(adapter.ModelOutcomeUncertain):
            _ = [event async for event in cold.run(task)]
        assert not gateway.requests
        assert (await cold.save_checkpoint()).state["usage"] == cp.state["usage"]
    else:
        events = tuple([event async for event in cold.run(task)])
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == len(remaining)
        saved = await cold.save_checkpoint()
        assert saved.state["usage"] == final.state["usage"]
        assert saved.state["step_usage"] == final.state["step_usage"]
        if phase == "completed":
            assert saved.state["artifact_refs"] == final.state["artifact_refs"]
        public = [
            event
            for group in [
                group
                async for group in _delivery_publication_batches(stream(events), task, runtime=cold)
            ]
            for event in group
        ]
        assert not any(OLD in public_json(event) for event in public)
        traces = [
            event
            for event in public
            if event.payload.get("source_event_kind") == "review.completed"
        ]
        # This checks only the resumed segment. Durable merged history is tested
        # with an actual persisted SQL prefix above, not inferred from this list.
        assert [event.payload["verdict"] for event in traces] == [
            event.payload["verdict"] for event in events if event.kind is EventKind.REVIEW_COMPLETED
        ]
        assert not any(event.kind is EventKind.STEP_RETRYING for event in public)
        if traces:
            native_review = next(
                event for event in events if event.kind is EventKind.REVIEW_COMPLETED
            )
            assert traces[0].payload["candidate_artifact_id"] == str(native_review.inputs[0].id)
            assert traces[0].payload["source_event_sequence"] == native_review.sequence
            assert traces[0].payload["actor"] == "reviewer" and traces[0].payload["attempt"] == 2
    assert len(case.harness.calls) == 3


async def test_review_trace_waits_for_delivered_succeeded_receipt_checkpoint(
    revision_case: NativeCase,
) -> None:
    case = revision_case
    review_index = next(
        i for i, event in enumerate(case.events) if event.kind is EventKind.REVIEW_COMPLETED
    )
    cp_index = next(
        i
        for i, event in enumerate(case.events[:review_index])
        if event.checkpoint is not None
        and any(
            row["purpose"] == "review" and row["status"] == "succeeded"
            for row in read_rows(event.checkpoint.state["models"]).values()
        )
    )
    delivered: list[RunEvent] = []

    class WorkerDied(Exception):
        pass

    async def delayed_receipt() -> AsyncIterator[RunEvent]:
        for event in case.events[: review_index + 1]:
            if event.checkpoint is None or not any(
                row["purpose"] == "review" and row["status"] == "succeeded"
                for row in read_rows(event.checkpoint.state["models"]).values()
            ):
                yield event
        assert not any(
            event.payload.get("projection") == "crew_report_trace_v1" for event in delivered
        )
        yield case.events[cp_index]
        raise WorkerDied

    groups: list[tuple[RunEvent, ...]] = []
    with pytest.raises(WorkerDied):
        async for group in _delivery_publication_batches(
            delayed_receipt(), case.context, runtime=case.runtime
        ):
            groups.append(group)
            delivered.extend(group)
    trace = next(
        event for event in delivered if event.payload.get("projection") == "crew_report_trace_v1"
    )
    assert trace.payload["verdict"] == "revise"
    group = next(group for group in groups if trace in group)
    assert group[0] == case.events[cp_index], (
        "trace was not committed with its delayed receipt proof"
    )
    assert OLD not in "".join(public_json(event) for event in delivered)


@pytest.mark.parametrize("field", ["verdict", "candidate_sha256"])
async def test_conflicting_persisted_review_trace_is_not_silently_deduplicated(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
    field: str,
) -> None:
    case = revision_case
    groups: list[RunEvent] = []
    iterator = _delivery_publication_batches(
        stream(case.events), case.context, runtime=case.runtime
    )
    try:
        async for group in iterator:
            groups.extend(group)
            if any(event.payload.get("source_event_kind") == "review.completed" for event in group):
                break
    finally:
        await iterator.aclose()
    cp = next(event.checkpoint for event in reversed(groups) if event.checkpoint is not None)
    assert cp is not None
    index = next(
        i
        for i, event in enumerate(groups)
        if event.payload.get("projection") == "crew_report_trace_v1"
    )
    item = groups[index]
    groups[index] = item.model_copy(
        update={
            "payload": {
                **item.payload,
                field: "approve" if field == "verdict" else "f" * 64,
            }
        }
    )
    gateway = WorkspaceDeliverySequenceGateway((SECOND, APPROVE))
    cold = runtime(
        gateway,
        case.harness,
        await repository_from(case, await private_prefix(case, cp)),
        installed_parser,
    )
    await cold.restore_checkpoint(cp)
    task = case.context.model_copy(update={"checkpoint": cp})
    with pytest.raises(RuntimeError, match="recovered trace is invalid"):
        _ = [
            group
            async for group in _delivery_publication_batches(
                cold.run(task), task, runtime=cold, recovery_events=tuple(groups)
            )
        ]
    assert len(case.harness.calls) == 3


@pytest.mark.parametrize("phase", ["prepared", "succeeded"])
async def test_cold_review_trace_keeps_native_candidate_and_call_identity_without_replay(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
    phase: str,
) -> None:
    case = revision_case
    cp = next(
        event.checkpoint
        for event in case.events
        if event.checkpoint is not None
        and any(
            row["step_id"] == "report"
            and row["purpose"] == "review"
            and integer_value(row["attempt"]) > 0
            and row["status"] == phase
            for row in read_rows(event.checkpoint.state["models"]).values()
        )
    )
    key, row = next(
        (key, row)
        for key, row in read_rows(cp.state["models"]).items()
        if row["step_id"] == "report"
        and row["purpose"] == "review"
        and integer_value(row["attempt"]) > 0
    )
    expected_candidate = next(
        event.artifact
        for event in case.events
        if event.artifact is not None
        and event.artifact.producer == "reporter"
        and event.artifact.type == "text"
        and event.artifact.version == 2
    )
    gateway = WorkspaceDeliverySequenceGateway((APPROVE,) if phase == "prepared" else ())
    cold = runtime(
        gateway,
        case.harness,
        await repository_from(case, await private_prefix(case, cp)),
        installed_parser,
    )
    await cold.restore_checkpoint(cp)
    task = case.context.model_copy(update={"checkpoint": cp})
    events = tuple([event async for event in cold.run(task)])
    public = [
        event
        for group in [
            group
            async for group in _delivery_publication_batches(stream(events), task, runtime=cold)
        ]
        for event in group
    ]
    traces = [
        event for event in public if event.payload.get("source_event_kind") == "review.completed"
    ]
    assert len(traces) == 1 and traces[0].payload["verdict"] == "approve"
    assert traces[0].payload["model_call_key"] == key
    assert expected_candidate is not None
    assert traces[0].payload["candidate_artifact_id"] == str(expected_candidate.id)
    assert traces[0].payload["candidate_sha256"] == expected_candidate.content_sha256
    if phase == "succeeded":
        assert traces[0].payload["review_model_artifact_id"] == row["artifact_id"]
    assert len(gateway.requests) == (1 if phase == "prepared" else 0)
    assert len(case.harness.calls) == 3
    assert not any(OLD in public_json(event) for event in public)


@pytest.mark.parametrize("field", ["verdict", "reviewer", "retry_attempt"])
async def test_public_trace_cannot_relabel_native_verdict_actor_or_retry(
    revision_case: NativeCase,
    field: str,
) -> None:
    case = revision_case
    events = list(case.events)
    index = next(
        i
        for i, event in enumerate(events)
        if (
            event.kind is EventKind.REVIEW_COMPLETED
            if field != "retry_attempt"
            else event.kind is EventKind.STEP_RETRYING and event.actor == "reporter"
        )
    )
    event = events[index]
    if field == "reviewer":
        updates: dict[str, object] = {"actor": "foreign_reviewer"}
    else:
        updates = {
            "payload": {
                **event.payload,
                **({"verdict": "approve"} if field == "verdict" else {"attempt": 3}),
            }
        }
    events[index] = event.model_copy(update=updates)
    with pytest.raises(RuntimeError, match="crew report publication review trace is invalid"):
        _ = [
            group
            async for group in _delivery_publication_batches(
                stream(tuple(events)), case.context, runtime=case.runtime
            )
        ]


@pytest.mark.parametrize("defect", ["candidate", "model", "reviewer", "feedback"])
async def test_cross_revision_receipt_substitution_rejected_before_calls(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
    defect: str,
) -> None:
    case = revision_case
    cp = await case.runtime.save_checkpoint()
    artifacts = await private_prefix(case, cp)
    drafts = sorted(
        (a for a in artifacts if a.type == "text" and a.producer == "reporter"),
        key=lambda a: a.version,
    )
    first, second = drafts
    target = second
    payload = target.to_payload()
    if defect == "candidate":
        object_map(payload["content"])[delivery_report.BINDING] = read_map(
            first.to_payload()["content"]
        )[delivery_report.BINDING]
    elif defect == "model":
        target = next(
            a
            for a in artifacts
            if str(a.id) == read_map(second.content[delivery_report.BINDING])["model_artifact_id"]
        )
        payload = target.to_payload()
        payload["source_ids"] = list(first.source_ids[:1])
    elif defect == "reviewer":
        target = next(
            a
            for a in artifacts
            if a.producer == "reviewer"
            and a.type == "model_response"
            and a.source_ids[0] == str(second.id)
        )
        payload = target.to_payload()
        payload["source_ids"] = [str(first.id)]
    else:
        target = next(a for a in artifacts if a.type == "review_feedback")
        payload = target.to_payload()
        object_array(payload["source_ids"])[0] = str(second.id)
    payload.pop("content_sha256")
    changed = Artifact.from_payload(payload)
    state = object_map(cp.to_payload()["state"])
    object_map(state["artifact_registry"])[str(changed.id)] = changed.content_sha256
    for row in mutable_rows(state["models"]).values():
        if row["artifact_id"] == str(changed.id):
            row["sha256"] = changed.content_sha256
    for rows in (state["artifact_refs"], state["review_refs"]):
        for row in mutable_rows(rows).values():
            if row["id"] == str(changed.id):
                row["sha256"] = changed.content_sha256
    forged = changed_checkpoint(cp, state)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = runtime(
        gateway,
        case.harness,
        await repository_from(case, tuple(changed if a.id == changed.id else a for a in artifacts)),
        installed_parser,
    )
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(forged)
        _ = [
            event
            async for event in cold.run(case.context.model_copy(update={"checkpoint": forged}))
        ]
    assert not gateway.requests and len(case.harness.calls) == 3


async def test_revision_restore_still_requires_matching_context_checkpoint(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = revision_case
    cp = boundary(case, "feedback")
    gateway = WorkspaceDeliverySequenceGateway((SECOND, APPROVE))
    cold = runtime(
        gateway,
        case.harness,
        await repository_from(case, await private_prefix(case, cp)),
        installed_parser,
    )
    await cold.restore_checkpoint(cp)
    with pytest.raises(adapter.RuntimeExecutionError, match="runtime checkpoint mismatch"):
        _ = [event async for event in cold.run(case.context)]
    assert not gateway.requests and len(case.harness.calls) == 3


async def test_legacy_feedback_checkpoint_cannot_be_upgraded_by_plan_or_route(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = revision_case
    cp = boundary(case, "feedback")
    state = object_map(cp.to_payload()["state"])
    state.pop(delivery_report.REVISION_POLICY)
    legacy = changed_checkpoint(cp, state)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = runtime(
        gateway,
        case.harness,
        await repository_from(case, await private_prefix(case, cp)),
        installed_parser,
    )
    await cold.restore_checkpoint(legacy)
    refreshed = case.context.model_copy(
        update={
            "checkpoint": legacy,
            "routing_decision": {
                **case.context.routing_decision,
                delivery_report.REVISION_POLICY: 1,
            },
        }
    )
    observed = []
    with pytest.raises(adapter.RuntimeExecutionError, match="single native model call"):
        async for event in cold.run(refreshed):
            observed.append(event)
    assert not gateway.requests and len(case.harness.calls) == 3
    assert delivery_report.REVISION_POLICY not in legacy.state
    assert all(
        delivery_report.REVISION_POLICY not in event.checkpoint.state
        for event in observed
        if event.checkpoint is not None
    )


async def test_reviewed_plan_still_rejects_same_round_second_call(
    tmp_path: Path,
    installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = WorkspaceDeliverySequenceGateway((*responses(), APPROVE))
    runner = runtime(gateway, harness, repository, installed_parser)
    runner._factory = subject(
        gateway, harness, repository, installed_parser, repeat_final=True
    )._factory
    with pytest.raises(adapter.RuntimeExecutionError, match="one native model call"):
        _ = [event async for event in runner.run(context())]
    assert len(gateway.requests) == 5 and len(harness.calls) == 3


@pytest.mark.parametrize("field", ["text", "producer", "sources"])
@pytest.mark.parametrize("rehash", [False, True])
async def test_final_revision_model_carrier_cannot_replace_native_receipt(
    revision_case: NativeCase,
    field: str,
    rehash: bool,
) -> None:
    case = revision_case
    cp = await case.runtime.save_checkpoint()
    artifacts = await private_prefix(case, cp)
    final = next(
        a
        for a in artifacts
        if str(a.id) == read_map(read_map(cp.state["artifact_refs"])["report"])["id"]
    )
    model_id = read_map(final.content[delivery_report.BINDING])["model_artifact_id"]
    events = list(case.events)
    index = next(
        i
        for i, event in enumerate(events)
        if event.artifact is not None and str(event.artifact.id) == model_id
    )
    original = events[index].artifact
    assert original is not None
    changes_by_field: dict[str, dict[str, object]] = {
        "text": {"content": {**original.content, "text": "UNREVIEWED_MODEL_BODY"}},
        "producer": {"producer": "foreign_reporter"},
        "sources": {"source_ids": ()},
    }
    changes = changes_by_field[field]
    changed = original.model_copy(update=changes)
    if rehash:
        payload = changed.to_payload()
        payload.pop("content_sha256")
        changed = Artifact.from_payload(payload)
    events[index] = events[index].model_copy(update={"artifact": changed})
    with pytest.raises(RuntimeError, match="crew report publication .*invalid"):
        _ = [
            group
            async for group in _delivery_publication_batches(
                stream(tuple(events)), case.context, runtime=case.runtime
            )
        ]


async def test_revision_checkpoint_cannot_relabel_call_as_implicit_recovery(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = revision_case
    cp = await case.runtime.save_checkpoint()
    state = object_map(cp.to_payload()["state"])
    key, row = next(
        (k, v)
        for k, v in mutable_rows(state["models"]).items()
        if v["step_id"] == "report" and v["purpose"] == "step" and v["attempt"] == 0
    )
    row["attempt"] = 1
    object_map(state["models"]).pop(key)
    object_map(state["models"])[
        case.runtime._model_call_key(case.context.run_id, "report", 1, "step", "reporter", 0)
    ] = row
    forged = changed_checkpoint(cp, state)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = runtime(gateway, case.harness, case.repository, installed_parser)
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(forged)
    assert not gateway.requests


@pytest.mark.parametrize("version", [True, "1", 2])
async def test_revision_checkpoint_policy_is_strictly_typed(
    revision_case: NativeCase,
    installed_parser: types.ModuleType,  # noqa: F811
    version: object,
) -> None:
    case = revision_case
    cp = boundary(case, "feedback")
    state = object_map(cp.to_payload()["state"])
    state[delivery_report.REVISION_POLICY] = version
    cold = runtime(
        WorkspaceDeliverySequenceGateway(()), case.harness, case.repository, installed_parser
    )
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(changed_checkpoint(cp, state))
