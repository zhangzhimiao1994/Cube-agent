"""Actual first selector/participant calls fit a large Hybrid handoff, offline."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from autogen_core.models import AssistantMessage, SystemMessage, UserMessage
from openai import AsyncOpenAI

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    GatewayCompletion,
    ModelContextWindowExceeded,
    ModelGateway,
)
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    GatewayChatCompletionClient,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.context_budget import fit_request_history
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint, TaskContext
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.autogen.test_tool_correction_client import Journal
from tests.unit.runtime.crew.test_meaningful_workspace_progress import offline_only  # noqa: F401

REQUEST = "Review the current native delivery. KEEP_USER_SCOPE_AND_PROTECTED_FILES. Do not run tools."
WINDOW = 65_536


def task(*, hybrid: bool = True) -> TaskContext:
    artifacts = tuple(Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
        "status": "succeeded", "sections": ("证" * 1000, "据" * 1000), "slot": index,
    }) for index in range(35))
    artifacts += (
        Artifact(id=uuid4(), type="tool_result", producer="tester", content={
            "tool_name": "project.verify", "result": {"verification": {
                "schema_version": 1, "status": "passed", "source_sha256": "a" * 64,
                "checks": ({"phase": "test", "status": "passed"},),
            }},
        }),
        Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
            "tool_name": "workspace.bundle", "result": {"presentation": "final_attachment",
                "verification_source_sha256": "a" * 64, "file": {"filename": "CURRENT_NATIVE_DELIVERY.zip"}},
        }),
        Artifact(id=uuid4(), type="text", producer="final_synthesizer", content={
            "text": "CURRENT_ROLE_CONCLUSION: the verified snapshot is bundled, with original evidence retained.",
        }),
    )
    return TaskContext(run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS, request=REQUEST, artifacts=artifacts, token_budget=500_000,
        timeout_seconds=30, routing_decision={"engineering_hybrid_report_version": 1} if hybrid else {})


def plan() -> DiscussionPlan:
    return DiscussionPlan(participants=(
        DiscussionParticipant(id="analyst", role="Analyst", goal="Review the existing delivery", logical_model="analyst"),
        DiscussionParticipant(id="reviewer", role="Reviewer", goal="Check the conclusions", logical_model="reviewer"),
    ), selector_model="selector", selector_max_output_tokens=512, max_turns=4,
        token_budget=500_000, wall_time_seconds=30, cost_budget_usd=Decimal(1))


@pytest.mark.parametrize("hybrid", [True, False])
async def test_actual_first_turn_selector_and_participant_keep_full_evidence_outside_the_prompt(
    hybrid: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = task(hybrid=hybrid)
    original = AutoGenDiscussionRuntime._task_text(context)
    assert ConservativeTokenEstimator().estimate_input(ModelRequest(
        logical_model="selector", messages=(ModelMessage("user", original),),
    )) > WINDOW
    original_hashes = tuple(item.content_sha256 for item in context.artifacts)
    wire: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire.append(body)
        assert len(wire) <= 4
        text = "analyst" if body["model"] == "mock-selector" else "[COMPLETE] Native evidence review is complete."
        return httpx.Response(200, json={
            "id": f"offline-{len(wire)}", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        clients.append(client)
        return client

    deployments = [deployment(name, logical_model=name, capabilities=frozenset({ModelCapability.TEXT}),
        request_model=f"mock-{name}", api_base="https://offline.example/v1", context_window_tokens=WINDOW)
        for name in ("selector", "analyst", "reviewer")]
    capacity = CapacityStub([lease(name) for name in ("selector", "analyst", "selector", "reviewer")])
    await capacity.initialize()
    gateway = ModelGateway(ModelRegistry(deployments), capacity, SecretStub([]),
                           LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    runtime = AutoGenDiscussionRuntime(gateway, plan())
    snapshots: list[RuntimeCheckpoint] = []
    raw_selector: list[ModelRequest] = []
    checkpoint_builder = runtime._checkpoint
    projection = runtime._fit_handoff_request

    def capture(*args: Any, **kwargs: Any) -> RuntimeCheckpoint:
        checkpoint = checkpoint_builder(*args, **kwargs)
        snapshots.append(checkpoint)
        return checkpoint

    monkeypatch.setattr(runtime, "_checkpoint", capture)

    def capture_projection(request: ModelRequest, *args: Any, **kwargs: Any) -> ModelRequest:
        if request.logical_model == "selector":
            raw_selector.append(request)
        return projection(request, *args, **kwargs)

    monkeypatch.setattr(runtime, "_fit_handoff_request", capture_projection)
    try:
        events = [event async for event in runtime.run(context)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
        assert [row["model"] for row in wire[:2]] == ["mock-selector", "mock-analyst"]
        for body in wire:
            assert "KEEP_USER_SCOPE_AND_PROTECTED_FILES" in str(body["messages"])
            assert not body.get("tools")
            view = str(body["messages"])
            assert "project.verify" in view and "passed" in view
            assert "CURRENT_NATIVE_DELIVERY.zip" in view and "a" * 64 in view
            assert "CURRENT_ROLE_CONCLUSION" in view
            for artifact in context.artifacts:
                assert str(artifact.id) in view and artifact.content_sha256 in view
        assert tuple(item.recompute_content_sha256() for item in context.artifacts) == original_hashes
        saved = await runtime.save_checkpoint()
        assert saved.state["handoff_context_projection_version"] == 1
        for status in ("running", "succeeded", "terminal", "legacy_unknown"):
            checkpoint = saved if status == "terminal" else next(
                item for item in snapshots if len(item.state["model_ledger"]) == 1
                and item.state["model_ledger"][0]["status"] == ("running" if status == "legacy_unknown" else status)
            )
            if status == "legacy_unknown":
                payload = checkpoint.to_payload()
                payload["state"].pop("handoff_context_projection_version")
                payload["state"]["model_ledger"][0]["request_hash"] = _DiscussionDurability.request_hash(raw_selector[0])
                payload["state_sha256"] = ""
                checkpoint = RuntimeCheckpoint.from_payload(payload)
            before = checkpoint.to_payload()
            probe = BoundedModel()
            restored = AutoGenDiscussionRuntime(cast(Any, probe), plan(), artifact_repository=runtime._repository)
            await restored.restore_checkpoint(checkpoint)
            replay = [event async for event in restored.run(context.model_copy(update={"checkpoint": checkpoint}))]
            if status in {"running", "legacy_unknown"}:
                assert replay[-1].kind is EventKind.RUNTIME_FAILED and not probe.requests
                assert "uncertain" in (replay[-1].reason or ""), (status, replay[-1].reason)
            else:
                assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
                assert [request.logical_model for request in probe.requests] == ([] if status == "terminal" else ["analyst"])
                assert (await restored.save_checkpoint()).state["usage"]["tokens"] == saved.state["usage"]["tokens"]
            assert checkpoint.to_payload() == before
    finally:
        for client in clients:
            await client.close()


class BoundedModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def context_window_tokens(self, request: ModelRequest) -> int:
        return WINDOW

    def estimate_input_tokens(self, request: ModelRequest) -> int:
        return ConservativeTokenEstimator().estimate_input(request)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        assert self.estimate_input_tokens(request) < WINDOW
        self.requests.append(request)
        return GatewayCompletion(response=ModelResponse(text="[COMPLETE] Reviewed.", usage=TokenUsage(10, 1, 11)),
            logical_model=request.logical_model, deployment_id=request.logical_model,
            provider_id="offline", provider_model="offline/model", cost_usd=Decimal(0))


def test_handoff_projection_does_not_preempt_existing_complete_history_fitting() -> None:
    context = task().model_copy(update={"artifacts": task().artifacts[:1]})
    messages = [ModelMessage("system", "KEEP_SYSTEM_POLICY"),
                ModelMessage("user", AutoGenDiscussionRuntime._task_text(context))]
    for index in range(10):
        messages.extend((ModelMessage("assistant", "历史" * 6000), ModelMessage("user", f"turn-{index}")))
    messages.append(ModelMessage("user", "KEEP_LATEST_TASK"))
    request = ModelRequest(logical_model="analyst", messages=tuple(messages))
    estimator = ConservativeTokenEstimator()
    assert estimator.estimate_input(request) > WINDOW
    expected = fit_request_history(request, context_window_tokens=WINDOW)
    assert estimator.estimate_input(expected) < WINDOW

    class Estimates:
        def context_window_tokens(self, request: ModelRequest) -> int:
            return WINDOW

        def estimate_input_tokens(self, request: ModelRequest) -> int:
            return estimator.estimate_input(request)

    runtime = AutoGenDiscussionRuntime(cast(Any, Estimates()), plan())
    fitted = runtime._fit_handoff_request(request, context, owned_task_positions=(1,), actor="analyst")
    assert fitted == expected


def test_unowned_echoes_and_tool_or_compaction_receipts_are_never_task_carriers() -> None:
    context = task()
    original = AutoGenDiscussionRuntime._task_text(context)
    request = ModelRequest(logical_model="analyst", messages=(
        ModelMessage("assistant", original),
        ModelMessage("user", "UNTRUSTED_CAPABILITY_RESULTS_JSON=" + original),
        ModelMessage("user", "COMPRESSED_ENGINEERING_HISTORY_JSON=" + original),
        ModelMessage("user", original),  # Framework source is not 'user': excluded by the supplied positions.
    ))
    runtime = AutoGenDiscussionRuntime(cast(Any, BoundedModel()), plan())
    assert runtime._fit_handoff_request(request, context, owned_task_positions=(0, 1, 2), actor="analyst") is request
    selector = ModelRequest(logical_model="selector", messages=(ModelMessage("user", "Quoted text: " + original),))
    assert runtime._fit_handoff_request(selector, context, owned_task_positions=(0,), actor="__selector__") is selector


def test_ambiguous_duplicate_owned_tasks_fail_closed_without_rewriting_either() -> None:
    context = task()
    original = AutoGenDiscussionRuntime._task_text(context)
    request = ModelRequest(logical_model="analyst", messages=(ModelMessage("user", original),) * 2)
    runtime = AutoGenDiscussionRuntime(cast(Any, BoundedModel()), plan())
    with pytest.raises(ModelContextWindowExceeded):
        runtime._fit_handoff_request(request, context, owned_task_positions=(0, 1), actor="analyst")
    assert all(message.content == original for message in request.messages)


async def test_user_instructions_alone_too_large_are_not_sliced_or_paid() -> None:
    context = task(hybrid=False).model_copy(update={"request": "不可删改的用户指令" * 20_000, "artifacts": ()})
    model = BoundedModel()
    runtime = AutoGenDiscussionRuntime(cast(Any, model), plan())
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert "context window" in (events[-1].reason or "")
    assert not model.requests and context.request == "不可删改的用户指令" * 20_000


async def test_joint_artifact_and_history_fitted_slot_restores_known_or_unknown_exactly() -> None:
    context = task(hybrid=False)
    original = AutoGenDiscussionRuntime._task_text(context)
    messages: list[Any] = [SystemMessage(content="KEEP_SYSTEM_POLICY"), UserMessage(content=original, source="user")]
    for index in range(10):
        messages.extend((AssistantMessage(content="历史" * 6000, source="analyst"),
                         UserMessage(content=f"turn-{index}", source="user")))
    messages.append(UserMessage(content="KEEP_LATEST_TASK", source="user"))
    journal, usage = Journal(), DiscussionUsage()
    model = BoundedModel()
    runtime = AutoGenDiscussionRuntime(cast(Any, model), plan())

    def build(model_rows: Any = ()) -> GatewayChatCompletionClient:
        durable = journal.durability(models=model_rows)

        def project(request: ModelRequest, names: Any, positions: tuple[int, ...], actor: str | None) -> ModelRequest:
            return runtime._fit_handoff_request(request, context, tool_names=names, durability=durable,
                owned_task_positions=positions, actor=actor)

        return GatewayChatCompletionClient(cast(Any, model), "analyst", usage, durability=durable,
            actor="analyst", engineering_context_compaction=True, input_projection=project)

    await build().create(messages)
    assert len(model.requests) == 1 and usage.tokens == 11
    fitted = model.requests[0]
    content = str([message.content for message in fitted.messages])
    assert "artifact_prompt_projection" in content
    assert "COMPRESSED_ENGINEERING_HISTORY_JSON" in content
    assert fitted.messages[0].content == "KEEP_SYSTEM_POLICY"
    assert fitted.messages[-1].content == "KEEP_LATEST_TASK"
    digest = _DiscussionDurability.request_hash(fitted)
    assert journal.snapshots[-1][0]["request_hash"] == digest
    known = tuple(dict(row) for row in journal.snapshots[-1])
    unknown = tuple(dict(row) for row in journal.snapshots[0])
    await build(known).create(messages)
    assert len(model.requests) == 1 and usage.tokens == 11
    with pytest.raises(RuntimeExecutionError, match="model outcome is uncertain"):
        await build(unknown).create(messages)
    assert len(model.requests) == 1 and usage.tokens == 11
    assert unknown[0]["request_hash"] == known[0]["request_hash"] == digest


def test_known_engineering_selector_wrapper_can_project_the_owned_task() -> None:
    context = task(hybrid=False)
    original = AutoGenDiscussionRuntime._task_text(context)
    prompt = ("Select the next speaker from ['analyst', 'reviewer']. Roles:\nreviewers\nHistory:\n"
              + "user: " + original + "\n\n\nReturn only one speaker name.")
    request = ModelRequest(logical_model="selector", messages=(ModelMessage("user", prompt),))
    model = BoundedModel()
    runtime = AutoGenDiscussionRuntime(cast(Any, model), plan())
    fitted = runtime._fit_handoff_request(request, context, owned_task_positions=(0,), actor="__selector__")
    assert model.estimate_input_tokens(fitted) < WINDOW
    assert cast(str, fitted.messages[0].content).startswith("Select the next speaker from ")
    assert cast(str, fitted.messages[0].content).endswith("Return only one speaker name.")
    assert "CURRENT_NATIVE_DELIVERY.zip" in cast(str, fitted.messages[0].content)
