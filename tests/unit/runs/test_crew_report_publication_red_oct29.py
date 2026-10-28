"""RED-only native Crew publication evidence; no provider or server is called.

These assertions describe the required second-slice behavior, not the baseline's
early publication. Do not xfail them: the intentionally failing assertions are
the handoff to the implementation agent.
"""
from __future__ import annotations

import asyncio
import json
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.delivery_report import final_evidence
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
from tests.unit.runtime.crew.test_delivery_report import context, responses, subject
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
    repository: InMemoryArtifactRepository, parser: types.ModuleType,
) -> adapter.CrewDispatchRuntime:
    runtime = subject(gateway, harness, repository, parser)
    worker, report = runtime._plan.steps
    reporter = runtime._plan.agents[-1]
    reviewer = reporter.model_copy(update={"id": "reviewer", "logical_model": "review"})
    runtime._plan = runtime._plan.model_copy(update={
        "agents": (*runtime._plan.agents, reviewer),
        "steps": (worker, report.model_copy(update={"reviewer": "reviewer"})),
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
                row["step_id"] == "report" and row["purpose"] == "review"
                and row["status"] == status for row in checkpoint.state["models"].values()
            ):
                return index, checkpoint
        raise AssertionError(f"native fixture did not produce reviewer {status}")


async def make_case(tmp_path: Path, parser: types.ModuleType, outcome: str = "approve") -> NativeCase:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    gateway = ReviewGateway(outcome)
    runtime = reviewed_runtime(gateway, harness, repository, parser)
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
    assert proof["verification"]["verification"]["status"] == "passed"
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


async def stream(events: tuple[RunEvent, ...]) -> AsyncIterator[RunEvent]:
    for event in events:
        yield event


async def published(case: NativeCase, events: tuple[RunEvent, ...]) -> list[tuple[RunEvent, ...]]:
    return [group async for group in _delivery_publication_batches(stream(events), case.context)]


def public_json(event: RunEvent) -> str:
    return json.dumps(_public_event_payload(event.to_payload()), sort_keys=True, ensure_ascii=True)


def terminal_index(case: NativeCase) -> int:
    return next(index for index, event in enumerate(case.events)
                if event.checkpoint is not None and event.checkpoint.state["terminal"] is True
                and event.checkpoint.state["phase"] == "completed"
                and "report" in event.checkpoint.state["completed"])


def test_fixture_has_native_report_review_and_completed_checkpoint(native_case: NativeCase) -> None:
    case = native_case
    terminal = case.events[terminal_index(case)].checkpoint
    assert terminal is not None
    assert terminal.state["artifact_refs"]["report"] == {
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
    selected = {
        "model_response": lambda item: item.artifact is not None
        and item.artifact.type == "model_response" and item.artifact.producer == "reporter",
        "candidate": lambda item: item.artifact == case.candidate,
        "review_inputs": lambda item: item.kind is EventKind.REVIEW_COMPLETED
        and case.candidate in item.inputs,
        "step_inputs": lambda item: item.kind is EventKind.STEP_COMPLETED
        and item.step_id == "report" and case.candidate in item.inputs,
    }[carrier]
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
    assert checkpoint.state["terminal"] is False and "report" not in checkpoint.state["completed"]
    assert len(checkpoint.state["tools"]) == 3
    assert all(row["status"] == "succeeded" for row in checkpoint.state["tools"].values())
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
        reviews = [row for row in checkpoint.state["models"].values() if row["purpose"] == "review"]
        assert len(reviews) == 1 and reviews[0]["status"] == "running"
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
    references = tuple(ArtifactReference(UUID(key), digest)
                       for key, digest in checkpoint.state["artifact_registry"].items())
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
    assert saved.state["artifact_refs"]["report"] == {
        "id": str(case.candidate.id), "sha256": case.candidate.content_sha256,
    }
