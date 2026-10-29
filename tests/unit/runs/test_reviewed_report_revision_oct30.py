"""Native report revisions: private history, cold receipts and final publication."""
from __future__ import annotations

import json
import types
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter, delivery_report
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.runs.test_crew_report_publication_red_oct29 import (
    REPORT,
    NativeCase,
    ReviewGateway,
    public_json,
    reviewed_runtime,
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


def runtime(gateway, harness, repository, parser):
    runner = reviewed_runtime(gateway, harness, repository, parser)
    runner._plan = runner._plan.model_copy(update={"steps": (
        runner._plan.steps[0], runner._plan.final_step.model_copy(update={"reviewer_retries": 1}),
    )})
    return runner


@pytest.fixture
async def revision_case(tmp_path: Path, installed_parser: types.ModuleType) -> NativeCase:  # noqa: F811
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = ReviewGateway("approve")
    gateway.responses = (*responses(OLD), ModelResponse(
        text=json.dumps({"verdict": "revise", "feedback": "Replace this draft: " + OLD}),
        usage=TokenUsage(1, 1, 2)), SECOND, APPROVE)
    runner = runtime(gateway, harness, repository, installed_parser)
    task = context()  # Real plan opt-in, no test-only routing marker.
    events = tuple([event async for event in runner.run(task)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 8 and len(harness.calls) == 3
    return NativeCase(runner, task, events, repository, gateway, harness, None)


def boundary(case: NativeCase, phase: str) -> RuntimeCheckpoint:
    for event in case.events:
        cp = event.checkpoint
        if cp is None or cp.state["retries"].get("report") != 1:
            continue
        rows = [row for row in cp.state["models"].values()
                if row["step_id"] == "report" and row["purpose"] == "step" and row["attempt"] > 0]
        if (phase == "feedback" and not rows) or (rows and rows[-1]["status"] == phase):
            return RuntimeCheckpoint.from_payload(cp.to_payload())
    raise AssertionError(f"missing real native revision checkpoint: {phase}")


async def private_prefix(case: NativeCase, cp: RuntimeCheckpoint) -> tuple[Artifact, ...]:
    refs = tuple(ArtifactReference(UUID(key), digest)
                 for key, digest in cp.state["artifact_registry"].items())
    return await case.repository.get_many(case.context.tenant_id, case.context.run_id, refs)


async def repository_from(case: NativeCase, artifacts: tuple[Artifact, ...]) -> InMemoryArtifactRepository:
    repository = InMemoryArtifactRepository()
    for artifact in artifacts:
        await repository.put(case.context.tenant_id, case.context.run_id, artifact)
    return repository


def changed_checkpoint(cp: RuntimeCheckpoint, state: dict) -> RuntimeCheckpoint:
    payload = cp.to_payload()
    payload["state"] = state
    payload.pop("state_sha256")
    for key in ("id", "run_id", "tenant_id"):
        payload[key] = UUID(payload[key])
    payload["mode"] = type(cp.mode)(payload["mode"])
    return RuntimeCheckpoint(**payload)


async def test_fresh_reviewed_plan_publishes_only_approved_revision(revision_case: NativeCase) -> None:
    case = revision_case
    assert delivery_report.REVISION_POLICY not in case.context.routing_decision
    groups = [group async for group in _delivery_publication_batches(
        stream(case.events), case.context, runtime=case.runtime)]
    public = [event for group in groups for event in group]
    assert not any(OLD in public_json(event) for event in public)
    assert sum(event.kind is EventKind.RUNTIME_COMPLETED for event in public) == 1
    saved = await case.runtime.save_checkpoint()
    assert saved.state[delivery_report.REVISION_POLICY] == 1
    artifacts = await private_prefix(case, saved)
    assert any(item.content.get("text") == OLD for item in artifacts)
    assert any(item.type == "review_feedback" and OLD in str(item.content) for item in artifacts)
    refs = saved.state["artifact_refs"]["report"]
    final = next(item for item in artifacts if str(item.id) == refs["id"])
    assert final.version == 2 and final.content["text"] == REPORT
    terminal_group = next(group for group in groups if any(
        event.checkpoint == saved for event in group))
    assert any(event.artifact == final for event in terminal_group)


@pytest.mark.parametrize("phase", ["feedback", "prepared", "running", "succeeded", "completed"])
async def test_revision_cold_restore_keeps_receipts_budget_and_unknown_stop(
    revision_case: NativeCase, installed_parser: types.ModuleType, phase: str,  # noqa: F811
) -> None:
    case = revision_case
    final = await case.runtime.save_checkpoint()
    cp = final if phase == "completed" else boundary(case, phase)
    artifacts = await private_prefix(case, cp)
    remaining = {"feedback": (SECOND, APPROVE), "prepared": (SECOND, APPROVE),
                 "running": (), "succeeded": (APPROVE,), "completed": ()}[phase]
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
        public = [event for group in [group async for group in _delivery_publication_batches(
            stream(events), task, runtime=cold)] for event in group]
        assert not any(OLD in public_json(event) for event in public)
    assert len(case.harness.calls) == 3


@pytest.mark.parametrize("defect", ["candidate", "model", "reviewer", "feedback"])
async def test_cross_revision_receipt_substitution_rejected_before_calls(
    revision_case: NativeCase, installed_parser: types.ModuleType, defect: str,  # noqa: F811
) -> None:
    case = revision_case
    cp = await case.runtime.save_checkpoint()
    artifacts = await private_prefix(case, cp)
    drafts = sorted((a for a in artifacts if a.type == "text" and a.producer == "reporter"),
                    key=lambda a: a.version)
    first, second = drafts
    target = second
    payload = target.to_payload()
    if defect == "candidate":
        payload["content"][delivery_report.BINDING] = first.to_payload()["content"][delivery_report.BINDING]
    elif defect == "model":
        target = next(a for a in artifacts if str(a.id) == second.content[delivery_report.BINDING]["model_artifact_id"])
        payload = target.to_payload()
        payload["source_ids"] = list(first.source_ids[:1])
    elif defect == "reviewer":
        target = next(a for a in artifacts if a.producer == "reviewer" and a.type == "model_response"
                      and a.source_ids[0] == str(second.id))
        payload = target.to_payload()
        payload["source_ids"] = [str(first.id)]
    else:
        target = next(a for a in artifacts if a.type == "review_feedback")
        payload = target.to_payload()
        payload["source_ids"][0] = str(second.id)
    payload.pop("content_sha256")
    changed = Artifact.from_payload(payload)
    state = cp.to_payload()["state"]
    state["artifact_registry"][str(changed.id)] = changed.content_sha256
    for row in state["models"].values():
        if row["artifact_id"] == str(changed.id):
            row["sha256"] = changed.content_sha256
    for rows in (state["artifact_refs"], state["review_refs"]):
        for row in rows.values():
            if row["id"] == str(changed.id):
                row["sha256"] = changed.content_sha256
    forged = changed_checkpoint(cp, state)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = runtime(gateway, case.harness, await repository_from(case, tuple(
        changed if a.id == changed.id else a for a in artifacts)), installed_parser)
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(forged)
        _ = [event async for event in cold.run(case.context.model_copy(update={"checkpoint": forged}))]
    assert not gateway.requests and len(case.harness.calls) == 3


async def test_revision_restore_still_requires_matching_context_checkpoint(
    revision_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = revision_case
    cp = boundary(case, "feedback")
    gateway = WorkspaceDeliverySequenceGateway((SECOND, APPROVE))
    cold = runtime(gateway, case.harness, await repository_from(case, await private_prefix(case, cp)), installed_parser)
    await cold.restore_checkpoint(cp)
    with pytest.raises(adapter.RuntimeExecutionError, match="runtime checkpoint mismatch"):
        _ = [event async for event in cold.run(case.context)]
    assert not gateway.requests and len(case.harness.calls) == 3


async def test_legacy_feedback_checkpoint_cannot_be_upgraded_by_plan_or_route(
    revision_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = revision_case
    cp = boundary(case, "feedback")
    state = cp.to_payload()["state"]
    state.pop(delivery_report.REVISION_POLICY)
    legacy = changed_checkpoint(cp, state)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = runtime(gateway, case.harness, await repository_from(case, await private_prefix(case, cp)), installed_parser)
    await cold.restore_checkpoint(legacy)
    refreshed = case.context.model_copy(update={"checkpoint": legacy, "routing_decision": {
        **case.context.routing_decision, delivery_report.REVISION_POLICY: 1,
    }})
    observed = []
    with pytest.raises(adapter.RuntimeExecutionError, match="single native model call"):
        async for event in cold.run(refreshed):
            observed.append(event)
    assert not gateway.requests and len(case.harness.calls) == 3
    assert delivery_report.REVISION_POLICY not in legacy.state
    assert all(delivery_report.REVISION_POLICY not in event.checkpoint.state
               for event in observed if event.checkpoint is not None)


async def test_reviewed_plan_still_rejects_same_round_second_call(
    tmp_path: Path, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = WorkspaceDeliverySequenceGateway((*responses(), APPROVE))
    runner = runtime(gateway, harness, repository, installed_parser)
    runner._factory = subject(gateway, harness, repository, installed_parser, repeat_final=True)._factory
    with pytest.raises(adapter.RuntimeExecutionError, match="one native model call"):
        _ = [event async for event in runner.run(context())]
    assert len(gateway.requests) == 5 and len(harness.calls) == 3


@pytest.mark.parametrize("field", ["text", "producer", "sources"])
@pytest.mark.parametrize("rehash", [False, True])
async def test_final_revision_model_carrier_cannot_replace_native_receipt(
    revision_case: NativeCase, field: str, rehash: bool,
) -> None:
    case = revision_case
    cp = await case.runtime.save_checkpoint()
    artifacts = await private_prefix(case, cp)
    final = next(a for a in artifacts if str(a.id) == cp.state["artifact_refs"]["report"]["id"])
    model_id = final.content[delivery_report.BINDING]["model_artifact_id"]
    events = list(case.events)
    index = next(i for i, event in enumerate(events)
                 if event.artifact is not None and str(event.artifact.id) == model_id)
    original = events[index].artifact
    assert original is not None
    changes = {"text": {"content": {**original.content, "text": "UNREVIEWED_MODEL_BODY"}},
               "producer": {"producer": "foreign_reporter"}, "sources": {"source_ids": ()}}[field]
    changed = original.model_copy(update=changes)
    if rehash:
        payload = changed.to_payload()
        payload.pop("content_sha256")
        changed = Artifact.from_payload(payload)
    events[index] = events[index].model_copy(update={"artifact": changed})
    with pytest.raises(RuntimeError, match="crew report publication .*invalid"):
        _ = [group async for group in _delivery_publication_batches(
            stream(tuple(events)), case.context, runtime=case.runtime)]


async def test_revision_checkpoint_cannot_relabel_call_as_implicit_recovery(
    revision_case: NativeCase, installed_parser: types.ModuleType,  # noqa: F811
) -> None:
    case = revision_case
    cp = await case.runtime.save_checkpoint()
    state = cp.to_payload()["state"]
    key, row = next((k, v) for k, v in state["models"].items()
                    if v["step_id"] == "report" and v["purpose"] == "step" and v["attempt"] == 0)
    row["attempt"] = 1
    state["models"].pop(key)
    state["models"][case.runtime._model_call_key(case.context.run_id, "report", 1, "step", "reporter", 0)] = row
    forged = changed_checkpoint(cp, state)
    gateway = WorkspaceDeliverySequenceGateway(())
    cold = runtime(gateway, case.harness, case.repository, installed_parser)
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(forged)
    assert not gateway.requests


@pytest.mark.parametrize("version", [True, "1", 2])
async def test_revision_checkpoint_policy_is_strictly_typed(
    revision_case: NativeCase, installed_parser: types.ModuleType, version: object,  # noqa: F811
) -> None:
    case = revision_case
    cp = boundary(case, "feedback")
    state = cp.to_payload()["state"]
    state[delivery_report.REVISION_POLICY] = version
    cold = runtime(WorkspaceDeliverySequenceGateway(()), case.harness, case.repository, installed_parser)
    with pytest.raises(adapter.RuntimeExecutionError):
        await cold.restore_checkpoint(changed_checkpoint(cp, state))
