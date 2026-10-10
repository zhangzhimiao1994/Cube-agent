"""Finalization preserves completed capabilities without replaying their effects."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    ModelGateway,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    _GatewayFailureHistory,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelCapability, ModelRequest, RejectedUsageStatus, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_cost_boundary import cost_plan
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)


class PostToolGateway(ToolIncompleteGateway):
    def __init__(self, *outcomes: str, scope_entries: int = 1, trusted_scope: bool = True,
                 usage_status: RejectedUsageStatus = "known") -> None:
        super().__init__(*outcomes)
        self.scope_entries = scope_entries
        self.trusted_scope = trusted_scope
        self.usage_status = usage_status

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput as rejected:
            rejected.logical_model = request.logical_model
            history = _GatewayFailureHistory(entered_count=self.scope_entries)
            history.mark_incomplete(ScopeIncompletePhase.TRANSPORT, ScopeIncompleteReason.REJECTED_OUTPUT)
            if self.trusted_scope:
                history.attach_diagnostic(rejected)
            raise


async def test_completed_tool_then_known_incomplete_finalizes_once() -> None:
    gateway = PostToolGateway("tool", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4 and len(harness.calls) == 1
    first, source, finalization, _ = gateway.requests
    assert first.logical_model == source.logical_model == finalization.logical_model
    assert finalization.response_schema == source.response_schema
    assert finalization.tools == () and not finalization.allow_fallback
    assert ModelCapability.TOOL_CALLING not in finalization.required_capabilities
    assert finalization.max_output_tokens <= source.max_output_tokens
    prompt = " ".join(str(message.content) for message in finalization.messages)
    assert "UNTRUSTED_CAPABILITY_RESULTS_JSON" in prompt
    assert "POST_TOOL_FINALIZATION" in prompt
    assert "UNTRUSTED_REJECTED_OUTPUT_JSON" not in prompt
    checkpoint = await runtime.save_checkpoint()
    assert [(row["attempt"], row["call_index"], row["status"])
            for row in draft_models(checkpoint)] == [
        (0, 0, "succeeded"), (0, 1, "rejected"), (0, 2, "succeeded"),
    ]
    repair = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    assert repair["version"] == 2 and repair["mode"] == "post_tool_finalization"
    assert repair["status"] == "succeeded"
    assert draft_models(checkpoint)[1]["failure_reason"] == "model post-tool incomplete"
    assert mapping(checkpoint.state["usage"])["tokens"] == 8423
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.04"


@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded"])
async def test_natural_finalization_checkpoint_preserves_tools_and_known_receipts(phase: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    harness = RecordingHarnessToolGateway()
    runtime = subject(PostToolGateway("tool", "incomplete", "success", "final"),
                      dispatch, repository, harness)
    events = await collect(runtime)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == "running"
                      and len(draft_models(event.checkpoint)) == (2 if phase == "source" else 3)
                      and draft_models(event.checkpoint)[-1]["status"] == (
                          "rejected" if phase == "source" else phase
                      ))
    before = checkpoint.to_payload()
    for _ in range(2):
        outcomes = ("final",) if phase == "succeeded" else ("success", "final")
        gateway = PostToolGateway(*outcomes)
        restored_harness = RecordingHarnessToolGateway()
        restored = subject(gateway, dispatch, repository, restored_harness)
        await restored.restore_checkpoint(checkpoint)
        await collect(restored, checkpoint=checkpoint,
                      fail="outcome requires confirmation" if phase == "running" else None)
        assert restored_harness.calls == []
        if phase == "running":
            assert gateway.requests == []
            assert (await restored.save_checkpoint()).to_payload() == before
        else:
            assert len(gateway.requests) == len(outcomes)
            saved = await restored.save_checkpoint()
            assert mapping(saved.state["usage"])["tokens"] == 8423
            assert mapping(saved.state["usage"])["cost_usd"] == "0.04"
            for key, row in mapping(checkpoint.state["models"]).items():
                if mapping(row)["status"] in {"succeeded", "rejected"}:
                    assert mapping(saved.state["models"])[key] == row
        assert checkpoint.to_payload() == before


@pytest.mark.parametrize("last", ["incomplete", "invalid", "transport", "tool"])
async def test_finalization_failure_never_recovers_or_invokes_tools_again(last: str) -> None:
    gateway = PostToolGateway("tool", "incomplete", last)
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured|provider unavailable|forbidden")
    assert len(gateway.requests) == 3 and len(harness.calls) == 1
    saved = await runtime.save_checkpoint()
    repair = mapping(mapping(saved.state["structured_repairs"])["draft"])
    assert repair["status"] in {"rejected", "uncertain"}
    assert all(row["attempt"] == 0 for row in draft_models(saved))


class DistinctToolsGateway(PostToolGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        completion = await super().complete_with_context(request)
        if completion.response.tool_calls:
            completion = replace(completion, response=replace(completion.response, tool_calls=(
                ToolCall(id=f"tool-{len(self.requests)}", name="web_search",
                         arguments={"q": f"query-{len(self.requests)}"}),
            )))
        return completion


async def test_two_completed_tool_rounds_allow_call2_to_call3_finalization() -> None:
    gateway = DistinctToolsGateway("tool", "tool", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime)
    assert len(gateway.requests) == 5 and len(harness.calls) == 2
    assert gateway.requests[3].tools == ()
    assert [(row["attempt"], row["call_index"], row["status"])
            for row in draft_models(await runtime.save_checkpoint())] == [
        (0, 0, "succeeded"), (0, 1, "succeeded"), (0, 2, "rejected"), (0, 3, "succeeded"),
    ]


@pytest.mark.parametrize("scope", ["step", "global"])
async def test_equal_cost_after_source_receipt_never_purchases_finalization(
    scope: Literal["step", "global"],
) -> None:
    gateway = PostToolGateway("tool", "incomplete")
    gateway.source_cost = Decimal("0.99")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, cost_plan(scope), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="budget exhausted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal(1)
    assert checkpoint.state["structured_repairs"] == {}


@pytest.mark.parametrize("tokens", [8395, 9000])
async def test_finalization_respects_remaining_token_budget(tokens: int) -> None:
    gateway = PostToolGateway("tool", "incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(tokens), InMemoryArtifactRepository(), harness)
    await collect(runtime, tokens=tokens, fail="budget exhausted" if tokens == 8395 else None)
    assert len(harness.calls) == 1
    if tokens == 8395:
        assert len(gateway.requests) == 2
    else:
        assert len(gateway.requests) == 4 and gateway.requests[2].max_output_tokens == 605


@pytest.mark.parametrize("boundary", ["unknown_scope", "no_scope", "missing_usage"])
async def test_untrusted_or_unaccounted_source_cannot_finalize(boundary: str) -> None:
    gateway = PostToolGateway("tool", "incomplete", scope_entries=2 if boundary == "unknown_scope" else 1,
                             trusted_scope=boundary != "no_scope",
                             usage_status="missing" if boundary == "missing_usage" else "known")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    await collect(runtime, fail="structured output invalid|usage unaccounted")
    assert len(gateway.requests) == 2 and len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["structured_repairs"] == {}
    assert draft_models(checkpoint)[-1]["failure_reason"] != "model post-tool incomplete"


@pytest.mark.parametrize("change", [
    "source_hash", "correction_hash", "history_hash", "mode", "version", "actor", "step",
    "source_usage", "source_usage_coherent", "tool_status", "tool_attempt", "tool_name", "tool_sha",
    "trigger", "source_ids",
    "legacy", "first_call_marker",
])
async def test_checkpoint_qualification_and_reservation_mismatches_never_replay(change: str) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    original = subject(PostToolGateway("tool", "incomplete", "success", "final"), dispatch,
                       repository, RecordingHarnessToolGateway())
    events = await collect(original)
    phase = "source" if change in {"legacy", "first_call_marker"} else "prepared"
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == "running"
                      and len(draft_models(event.checkpoint)) == (2 if phase == "source" else 3)
                      and draft_models(event.checkpoint)[-1]["status"] == (
                          "rejected" if phase == "source" else phase
                      ))
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    models = state["models"]
    source_key = next(key for key, row in models.items() if row["status"] == "rejected")
    tool = next(iter(state["tools"].values()))
    repair = state["structured_repairs"].get("draft")
    if change in {"source_hash", "correction_hash", "history_hash"}:
        repair[{
            "source_hash": "source_request_sha256", "correction_hash": "correction_request_sha256",
            "history_hash": "tool_history_sha256",
        }[change]] = "0" * 64
    elif change in {"mode", "version"}:
        repair[change] = "format_repair" if change == "mode" else 1
    elif change in {"actor", "step"}:
        models[source_key]["actor" if change == "actor" else "step_id"] = "final"
    elif change == "source_usage":
        state["rejected_outputs"][source_key]["usage"]["total_tokens"] += 1
    elif change == "source_usage_coherent":
        usage = state["rejected_outputs"][source_key]["usage"]
        usage["total_tokens"] += 1
        usage["prompt_tokens"] += 1
        state["usage"]["tokens"] += 1
        state["step_usage"]["draft"]["tokens"] += 1
    elif change.startswith("tool_"):
        key = change.removeprefix("tool_")
        tool[key] = {"status": "running", "attempt": 1, "name": "workspace.write_text", "sha": "0" * 64}[
            key
        ]
        if key == "sha":
            tool["sha256"] = tool.pop("sha")
    elif change == "trigger":
        tool["trigger_model_artifact_id"] = "00000000-0000-4000-8000-000000000099"
    elif change == "source_ids":
        state["rejected_outputs"][source_key]["source_ids"] = []
    else:
        models[source_key]["failure_reason"] = (
            "structured output rejected" if change == "legacy" else "model response incomplete"
        )
    payload["state_sha256"] = ""
    altered = RuntimeCheckpoint.from_payload(payload)
    gateway = PostToolGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    try:
        await restored.restore_checkpoint(altered)
    except RuntimeExecutionError:
        pass
    else:
        await collect(restored, checkpoint=altered, fail="checkpoint|structured output invalid|qualification")
    assert gateway.requests == [] and harness.calls == []


class BoundaryCostGateway(PostToolGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        completion = await super().complete_with_context(request)
        return replace(completion, cost_usd=(Decimal(0) if self.outcomes[len(self.requests) - 1] == "final"
                                            else completion.cost_usd))


@pytest.mark.parametrize("scope", ["step", "global"])
async def test_cached_finalization_at_equal_cost_readback_is_not_a_paid_retry(
    scope: Literal["step", "global"],
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch = cost_plan(scope)
    gateway = BoundaryCostGateway("tool", "incomplete", "success", "final")
    gateway.source_cost = Decimal("0.98")
    original = subject(gateway, dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(original)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and event.checkpoint.state["phase"] == "running"
                      and len(draft_models(event.checkpoint)) == 3
                      and draft_models(event.checkpoint)[-1]["status"] == "succeeded")
    assert Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) == Decimal(1)
    for _ in range(2):
        replay_gateway = BoundaryCostGateway("final")
        harness = RecordingHarnessToolGateway()
        restored = subject(replay_gateway, dispatch, repository, harness)
        await restored.restore_checkpoint(checkpoint)
        await collect(restored, checkpoint=checkpoint)
        assert len(replay_gateway.requests) == 1 and replay_gateway.requests[0].response_schema is None
        assert harness.calls == []
        saved = await restored.save_checkpoint()
        assert Decimal(mapping(saved.state["usage"])["cost_usd"]) == Decimal(1)
        for key, row in mapping(checkpoint.state["models"]).items():
            assert mapping(saved.state["models"])[key] == row


async def test_finalization_never_extends_original_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_extension(*args: object) -> float:
        raise AssertionError("post-tool finalization extended the original step deadline")

    monkeypatch.setattr(CrewDispatchRuntime, "_recovery_step_deadline", forbid_extension)
    gateway = PostToolGateway("tool", "incomplete", "success", "final")
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), RecordingHarnessToolGateway())
    events = await collect(runtime)
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert gateway.requests[2].timeout_seconds <= gateway.requests[1].timeout_seconds


async def test_real_gateway_sdk_alias_tool_then_single_incomplete_finalization() -> None:
    wire: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []
    rejected_receipts: list[GatewayRejectedOutput] = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        index = len(wire)
        assert index <= 4 and request.method == "POST"
        assert request.url.path == ("/v1/chat/completions" if index == 4 else "/v1/responses")
        if index == 4:
            return httpx.Response(200, json={
                "id": "chat-final", "object": "chat.completion", "created": 1, "model": "native-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "actual final answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 16, "total_tokens": 17},
            })
        output: list[dict[str, object]] = [{
            "id": "tool-own", "type": "function_call", "status": "completed", "call_id": "approved",
            "name": "web_search", "arguments": '{"q":"safe"}',
        }] if index == 1 else [{
            "id": f"msg-{index}", "type": "message", "role": "assistant",
            "status": "incomplete" if index == 2 else "completed",
            "content": [{"type": "output_text", "annotations": [],
                         "text": "PRIVATE_PARTIAL" if index == 2 else '{"summary":"complete handoff"}'}],
        }]
        return httpx.Response(200, json={
            "id": f"resp-{index}", "object": "response", "created_at": 1, "model": "native-model",
            "status": "incomplete" if index == 2 else "completed", "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if index == 2 else None,
            "output": output,
            "usage": {"input_tokens": 2234, "output_tokens": 6144, "total_tokens": 8378}
            if index == 2 else {"input_tokens": 1, "output_tokens": 16, "total_tokens": 17},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        clients.append(client)
        return client

    class RecordingGateway(ModelGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as rejected:
                rejected_receipts.append(rejected)
                raise

    capabilities = frozenset({ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT,
                              ModelCapability.TOOL_CALLING})
    registry = ModelRegistry([deployment(
        "primary", logical_model="general", capabilities=capabilities, structured_output_api="responses",
        request_model="native-model", api_base="https://own-native.example/v1",
        input_per_million_usd=Decimal(1), output_per_million_usd=Decimal(2),
    )])
    capacity = CapacityStub([lease("primary") for _ in range(4)])
    await capacity.initialize()
    gateway = RecordingGateway(registry, capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), InMemoryArtifactRepository(), harness)
    try:
        await collect(runtime)
        assert len(wire) == 4 and len(harness.calls) == 1
        assert harness.calls[0][1].tool_name == "web.search"
        assert wire[0]["tools"][0]["name"] == "web_search"
        assert not wire[2].get("tools")
        scope = get_gateway_scope_diagnostic(rejected_receipts[0])
        assert scope is not None and scope.transport_entered_count == 1 and scope.failure_attempt_count == 0
        assert get_gateway_failure_receipt(rejected_receipts[0]) is None
        saved = await runtime.save_checkpoint()
        assert "PRIVATE_PARTIAL" not in str(saved.to_payload())
        assert draft_models(saved)[1]["failure_reason"] == "model post-tool incomplete"
        assert mapping(mapping(saved.state["structured_repairs"])["draft"])["version"] == 2
    finally:
        for client in clients:
            await client.close()


class ReviewRevisionGateway(DistinctToolsGateway):
    request_offset: int = 0

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        outcome = self.outcomes[len(self.requests)]
        completion = await super().complete_with_context(request)
        if completion.response.tool_calls:
            index = self.request_offset + len(self.requests)
            return replace(completion, response=replace(completion.response, tool_calls=(
                ToolCall(id=f"tool-{index}", name="web_search", arguments={"q": f"query-{index}"}),
            )))
        if outcome in {"revise", "approve"}:
            return replace(completion, response=replace(
                completion.response, text=json.dumps({"verdict": outcome, "feedback": "check facts"}),
            ))
        return completion


@pytest.mark.parametrize("boundary", ["completed", "later_running", "later_approved"])
@pytest.mark.parametrize("incomplete", [False, True])
async def test_closed_finalization_survives_independently_validated_reviewer_revision(
    boundary: str, incomplete: bool,
) -> None:
    base = tool_plan()
    dispatch = base.model_copy(update={"total_cost_usd": Decimal(5), "steps": (
        base.steps[0].model_copy(update={"reviewer": "reviewer", "reviewer_retries": 1}),
        base.steps[1],
    )})
    repository = InMemoryArtifactRepository()
    outcomes = ("tool", "incomplete", "success") if incomplete else ("tool", "success")
    gateway = ReviewRevisionGateway(*outcomes, "revise", "tool", "success", "approve", "final")
    harness = RecordingHarnessToolGateway()
    original = subject(gateway, dispatch, repository, harness)
    events = await collect(original)
    assert len(gateway.requests) == (8 if incomplete else 7) and len(harness.calls) == 2
    checkpoint = (await original.save_checkpoint()) if boundary == "completed" else next(
        event.checkpoint for event in events if event.checkpoint is not None
        and any(row["attempt"] > 0 and row["status"] == (
                    "succeeded" if boundary == "later_approved" else "running")
                and (boundary != "later_approved" or row["actor"] == "reviewer")
                for row in draft_models(event.checkpoint))
    )
    before = checkpoint.to_payload()
    replay_gateway = ReviewRevisionGateway("final")
    replay_harness = RecordingHarnessToolGateway()
    restored = subject(replay_gateway, dispatch, repository, replay_harness)
    await restored.restore_checkpoint(checkpoint)
    expected_failure = "outcome requires confirmation" if boundary == "later_running" else None
    resumed_events = await collect(restored, checkpoint=checkpoint, fail=expected_failure)
    assert replay_harness.calls == []
    saved = (checkpoint if boundary == "completed" and not incomplete else await restored.save_checkpoint())
    if boundary == "later_approved":
        assert len(replay_gateway.requests) == 1
        assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert mapping(saved.state["usage"])["tokens"] == mapping(checkpoint.state["usage"])["tokens"] + 11
        assert Decimal(mapping(saved.state["usage"])["cost_usd"]) == (
            Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) + Decimal("0.01")
        )
    else:
        assert replay_gateway.requests == []
        assert saved.to_payload() == before
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("incomplete", [False, True])
@pytest.mark.parametrize("review_round", [0, 1])
@pytest.mark.parametrize("phase", ["prepared", "running", "succeeded"])
async def test_natural_review_checkpoint_reuses_original_candidate(
    incomplete: bool, review_round: int, phase: str,
) -> None:
    base = tool_plan()
    dispatch = base.model_copy(update={"total_cost_usd": Decimal(5), "steps": (
        base.steps[0].model_copy(update={"reviewer": "reviewer", "reviewer_retries": 1}),
        base.steps[1],
    )})
    repository = InMemoryArtifactRepository()
    outcomes = ("tool", "incomplete", "success") if incomplete else ("tool", "success")
    original = subject(ReviewRevisionGateway(*outcomes, "revise", "tool", "success", "approve", "final"),
                       dispatch, repository, RecordingHarnessToolGateway())
    events = await collect(original)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len([row for row in draft_models(event.checkpoint) if row["purpose"] == "review"])
                      == review_round + 1
                      and [row for row in draft_models(event.checkpoint) if row["purpose"] == "review"][-1]
                      ["status"] == phase)
    candidate = next(event.artifact for event in events if event.artifact is not None
                     and event.artifact.type == "text" and event.artifact.producer == "writer"
                     and event.artifact.version == review_round + 1)
    before = checkpoint.to_payload()
    remaining: tuple[str, ...] = (("revise", "tool", "success", "approve", "final") if review_round == 0 else
                                  ("approve", "final"))
    if phase == "succeeded":
        remaining = remaining[1:]
    for _ in range(2):
        gateway = ReviewRevisionGateway(*remaining)
        gateway.request_offset = (8 if incomplete else 7) - len(remaining)
        harness = RecordingHarnessToolGateway()
        restored = subject(gateway, dispatch, repository, harness)
        await restored.restore_checkpoint(checkpoint)
        stored_before = dict(repository._artifacts)
        resumed = await collect(restored, checkpoint=checkpoint,
                                fail="checkpoint review artifact lineage is invalid" if phase == "running" else None)
        if phase == "running":
            assert gateway.requests == [] and harness.calls == []
            assert repository._artifacts == stored_before
        else:
            assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
            assert len(gateway.requests) == len(remaining)
            assert len(harness.calls) == int(review_round == 0)
            reused = next(event.artifact for event in resumed if event.artifact is not None
                          and event.artifact.type == "text" and event.artifact.producer == "writer")
            assert reused.to_payload() == candidate.to_payload()
            new_candidates = [artifact for key, artifact in repository._artifacts.items()
                              if key not in stored_before and artifact.type == "text"
                              and artifact.producer == "writer"]
            assert len(new_candidates) == int(review_round == 0)
            saved = await restored.save_checkpoint()
            assert mapping(saved.state["usage"])["tokens"] == (
                mapping(checkpoint.state["usage"])["tokens"]
                + sum(11 if outcome == "final" else 17 for outcome in remaining)
            )
            assert Decimal(mapping(saved.state["usage"])["cost_usd"]) == (
                Decimal(mapping(checkpoint.state["usage"])["cost_usd"]) + Decimal("0.01") * len(remaining)
            )
            for key, row in mapping(checkpoint.state["models"]).items():
                if mapping(row)["status"] == "succeeded":
                    assert mapping(saved.state["models"])[key] == row
        assert checkpoint.to_payload() == before


@pytest.mark.parametrize("change", [
    "id", "content", "sha", "producer", "type", "source_ids", "version", "duplicate",
    "review_actor", "review_step", "review_attempt", "request_sha",
])
async def test_prepared_review_candidate_mismatch_never_issues_model_or_tool(change: str) -> None:
    base = tool_plan()
    dispatch = base.model_copy(update={"steps": (
        base.steps[0].model_copy(update={"reviewer": "reviewer"}), base.steps[1],
    )})
    repository = InMemoryArtifactRepository()
    original = subject(ReviewRevisionGateway("tool", "success", "approve", "final"), dispatch,
                       repository, RecordingHarnessToolGateway())
    events = await collect(original)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and any(row["purpose"] == "review" and row["status"] == "prepared"
                              for row in draft_models(event.checkpoint)))
    candidate = next(event.artifact for event in events if event.artifact is not None
                     and event.artifact.type == "text" and event.artifact.producer == "writer")
    payload: dict[str, Any] = checkpoint.to_payload()
    state = payload["state"]
    review = next(row for row in state["models"].values() if row["purpose"] == "review")
    if change.startswith("review_"):
        review[change.removeprefix("review_")] = {"review_actor": "writer", "review_step": "final",
                                                  "review_attempt": 3}[change]
    elif change == "request_sha":
        review["request_sha256"] = "0" * 64
    elif change == "sha":
        state["artifact_registry"][str(candidate.id)] = "0" * 64
    else:
        fields = candidate.to_payload()
        fields["id"] = candidate.id
        fields["content_sha256"] = ""
        if change in {"id", "duplicate"}:
            fields["id"] = UUID("00000000-0000-4000-8000-000000000099")
        else:
            fields[change] = {"content": {"text": '{"summary":"different"}'}, "producer": "reviewer",
                              "type": "review_candidate", "source_ids": [], "version": 2}[change]
        changed = Artifact.model_validate(fields)
        if change != "duplicate":
            state["artifact_registry"].pop(str(candidate.id))
            repository._artifacts.pop((checkpoint.tenant_id, checkpoint.run_id, candidate.id))
        state["artifact_registry"][str(changed.id)] = changed.content_sha256
        await repository.put(checkpoint.tenant_id, checkpoint.run_id, changed)
    payload["state_sha256"] = ""
    altered = RuntimeCheckpoint.from_payload(payload)
    before = altered.to_payload()
    stored_before = dict(repository._artifacts)
    gateway = ReviewRevisionGateway("approve", "final")
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    expected = "model request changed after checkpoint" if change in {"id", "request_sha"} else "checkpoint"
    with pytest.raises(RuntimeExecutionError, match=expected):
        await restored.restore_checkpoint(altered)
        await collect(restored, checkpoint=altered)
    assert gateway.requests == [] and harness.calls == []
    assert repository._artifacts == stored_before
    assert altered.to_payload() == before


async def test_expired_prepared_finalization_checkpoint_never_issues_model_or_tool() -> None:
    repository = InMemoryArtifactRepository()
    dispatch = tool_plan()
    original = subject(PostToolGateway("tool", "incomplete", "success", "final"), dispatch,
                       repository, RecordingHarnessToolGateway())
    events = await collect(original)
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and len(draft_models(event.checkpoint)) == 3
                      and draft_models(event.checkpoint)[-1]["status"] == "prepared")
    payload: dict[str, Any] = checkpoint.to_payload()
    payload["state"]["remaining_timeout_seconds"] = 0.0
    payload["state"]["remaining_absolute_timeout_seconds"] = 0.0
    payload["state_sha256"] = ""
    expired = RuntimeCheckpoint.from_payload(payload)
    gateway = PostToolGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    with pytest.raises(RuntimeExecutionError, match="checkpoint|deadline|timeout"):
        await restored.restore_checkpoint(expired)
        await collect(restored, checkpoint=expired)
    assert gateway.requests == [] and harness.calls == []
