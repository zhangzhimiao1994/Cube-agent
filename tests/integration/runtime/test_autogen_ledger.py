from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse, TokenUsage
from agent_hub.models.types import ToolCall as GatewayToolCall
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)


def discussion_plan(*, tools: tuple[str, ...] = ()) -> DiscussionPlan:
    return DiscussionPlan(
        participants=(
            DiscussionParticipant(
                id="analyst",
                role="Analyst",
                goal="Analyze",
                logical_model="shared",
                allowed_tools=tools,
            ),
            DiscussionParticipant(
                id="critic", role="Critic", goal="Review", logical_model="shared"
            ),
        ),
        selector_model="shared",
        max_turns=4,
    )


def discussion_context() -> TaskContext:
    return TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="Analyze the evidence.",
    )


def resumed_context(context: TaskContext, checkpoint: Any) -> TaskContext:
    return TaskContext(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=context.mode,
        request=context.request,
        checkpoint=checkpoint,
    )


def model_request_hash(request: ModelRequest) -> str:
    payload = {
        "logical_model": request.logical_model,
        "messages": [
            {"role": message.role, "content": message.content} for message in request.messages
        ],
        "capabilities": sorted(item.value for item in request.required_capabilities),
        "max_output_tokens": request.max_output_tokens,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def collect(runtime: AutoGenDiscussionRuntime, context: TaskContext) -> list[RunEvent]:
    return [event async for event in runtime.run(context)]


class BlockingModelGateway:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FailingIfCalledGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        raise AssertionError("durable model result must prevent a repeated paid call")


async def test_model_running_resume_fails_uncertain_without_gateway_replay() -> None:
    context = discussion_context()
    gateway = BlockingModelGateway()
    runtime = AutoGenDiscussionRuntime(gateway, discussion_plan())
    stream = runtime.run(context)
    assert (await anext(stream)).kind is EventKind.DISCUSSION_STARTED
    pending: asyncio.Future[RunEvent] = asyncio.ensure_future(anext(stream))
    await gateway.started.wait()
    checkpoint = await runtime.save_checkpoint()
    model_ledger = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state["model_ledger"])
    assert model_ledger[0]["status"] == "running"
    assert len(cast(str, model_ledger[0]["request_hash"])) == 64
    await runtime.cancel()
    await pending

    recovered_gateway = FailingIfCalledGateway()
    recovered = AutoGenDiscussionRuntime(recovered_gateway, discussion_plan())
    await recovered.restore_checkpoint(checkpoint)
    events = await collect(recovered, resumed_context(context, checkpoint))
    assert recovered_gateway.requests == []
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "model_outcome_uncertain"

    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    entries = cast(list[dict[str, object]], state["model_ledger"])
    entries[0]["request_hash"] = "0" * 64
    payload["state_sha256"] = ""
    mismatched = RuntimeCheckpoint.from_payload(payload)
    mismatch_gateway = FailingIfCalledGateway()
    mismatch_runtime = AutoGenDiscussionRuntime(mismatch_gateway, discussion_plan())
    await mismatch_runtime.restore_checkpoint(mismatched)
    mismatch_events = await collect(mismatch_runtime, resumed_context(context, mismatched))
    assert mismatch_gateway.requests == []
    assert mismatch_events[-1].kind is EventKind.RUNTIME_FAILED

    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    entry = cast(list[dict[str, object]], state["model_ledger"])[0]
    state["model_ledger"] = [dict(entry) for _ in range(65)]
    payload["state_sha256"] = ""
    oversized = RuntimeCheckpoint.from_payload(payload)
    oversized_gateway = FailingIfCalledGateway()
    oversized_runtime = AutoGenDiscussionRuntime(oversized_gateway, discussion_plan())
    await oversized_runtime.restore_checkpoint(oversized)
    oversized_events = await collect(oversized_runtime, resumed_context(context, oversized))
    assert oversized_gateway.requests == []
    assert oversized_events[-1].kind is EventKind.RUNTIME_FAILED


async def test_model_succeeded_artifact_resumes_without_gateway_and_hash_tamper_fails() -> None:
    class BlockTextRepository(InMemoryArtifactRepository):
        def __init__(self) -> None:
            super().__init__()
            self.text_started = asyncio.Event()
            self.block_text = True

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            if artifact.type == "text" and self.block_text:
                self.text_started.set()
                await asyncio.Event().wait()

    class TwoRepliesGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text="analyst" if len(self.requests) == 1 else "[COMPLETE] durable",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    repository = BlockTextRepository()
    context = discussion_context()
    runtime = AutoGenDiscussionRuntime(
        TwoRepliesGateway(), discussion_plan(), artifact_repository=repository
    )
    stream = runtime.run(context)
    await anext(stream)
    pending: asyncio.Future[RunEvent] = asyncio.ensure_future(anext(stream))
    await repository.text_started.wait()
    checkpoint = await runtime.save_checkpoint()
    ledger = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state["model_ledger"])
    assert [entry["status"] for entry in ledger] == ["succeeded", "succeeded"]
    references = tuple(
        ArtifactReference(
            id=UUID(cast(str, entry["artifact_id"])),
            sha256=cast(str, entry["artifact_sha256"]),
        )
        for entry in ledger
    )
    stored = await repository.get_many(context.tenant_id, context.run_id, references)
    assert all(artifact.type == "model_result" for artifact in stored)
    await runtime.cancel()
    await pending

    repository.block_text = False
    recovered_gateway = FailingIfCalledGateway()
    recovered = AutoGenDiscussionRuntime(
        recovered_gateway, discussion_plan(), artifact_repository=repository
    )
    await recovered.restore_checkpoint(checkpoint)
    events = await collect(recovered, resumed_context(context, checkpoint))
    assert recovered_gateway.requests == []
    assert any(event.message == "[COMPLETE] durable" for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [
        (event.kind, event.reason, event.message) for event in events
    ]

    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    registry = cast(dict[str, str], state["artifact_registry"])
    registry[str(references[0].id)] = "0" * 64
    payload["state_sha256"] = ""
    mismatched = RuntimeCheckpoint.from_payload(payload)
    mismatch_gateway = FailingIfCalledGateway()
    mismatch_runtime = AutoGenDiscussionRuntime(
        mismatch_gateway, discussion_plan(), artifact_repository=repository
    )
    await mismatch_runtime.restore_checkpoint(mismatched)
    mismatch_events = await collect(mismatch_runtime, resumed_context(context, mismatched))
    assert mismatch_gateway.requests == []
    assert mismatch_events[-1].kind is EventKind.RUNTIME_FAILED


class ToolScenarioGateway:
    def __init__(self, *, final_reply: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.final_reply = final_reply

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        index = len(self.requests)
        if self.final_reply and ModelCapability.TOOL_CALLING in request.required_capabilities:
            response = ModelResponse(
                text="[COMPLETE] recovered",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        elif self.final_reply or index == 1:
            response = ModelResponse(
                text="analyst",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        else:
            response = ModelResponse(
                text=None,
                tool_calls=(
                    GatewayToolCall(
                        id="call_1", name="web.search", arguments={"query": "evidence"}
                    ),
                ),
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        return GatewayCompletion(
            response=response,
            deployment_id="shared",
            logical_model=request.logical_model,
            provider_id="openai",
            provider_model="openai/test",
            cost_usd=Decimal(0),
        )


class BlockingCapability:
    def __init__(self, *, replay_safe: bool) -> None:
        self.replay_safe = replay_safe
        self.started = asyncio.Event()
        self.calls: list[tuple[str, str]] = []

    def is_replay_safe(self, name: str) -> bool:
        return self.replay_safe

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor, name
        arguments_hash = hashlib.sha256(
            json.dumps(dict(arguments), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.calls.append((idempotency_key, arguments_hash))
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class RecoveryCapability(BlockingCapability):
    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor, name
        arguments_hash = hashlib.sha256(
            json.dumps(dict(arguments), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.calls.append((idempotency_key, arguments_hash))
        return {"items": ("source-a",)}


async def running_tool_checkpoint(
    replay_safe: bool,
) -> tuple[Any, TaskContext, BlockingCapability, InMemoryArtifactRepository]:
    context = discussion_context()
    capability = BlockingCapability(replay_safe=replay_safe)
    repository = InMemoryArtifactRepository()
    runtime = AutoGenDiscussionRuntime(
        ToolScenarioGateway(),
        discussion_plan(tools=("web.search",)),
        capability_gateway=capability,
        artifact_repository=repository,
    )
    stream: AsyncIterator[RunEvent] = runtime.run(context)
    await anext(stream)
    pending: asyncio.Future[RunEvent] = asyncio.ensure_future(anext(stream))
    await capability.started.wait()
    checkpoint = await runtime.save_checkpoint()
    await runtime.cancel()
    await pending
    return checkpoint, context, capability, repository


async def test_restricted_tool_running_resume_fails_uncertain_without_capability_call() -> None:
    checkpoint, context, first_capability, repository = await running_tool_checkpoint(False)
    ledger = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state["tool_ledger"])
    assert ledger[0]["status"] == "running"
    assert ledger[0]["replay_safe"] is False
    second_capability = RecoveryCapability(replay_safe=False)
    recovered = AutoGenDiscussionRuntime(
        FailingIfCalledGateway(),
        discussion_plan(tools=("web.search",)),
        capability_gateway=second_capability,
        artifact_repository=repository,
    )
    await recovered.restore_checkpoint(checkpoint)
    events = await collect(recovered, resumed_context(context, checkpoint))
    assert len(first_capability.calls) == 1
    assert second_capability.calls == []
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "tool_outcome_uncertain"

    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    entries = cast(list[dict[str, object]], state["tool_ledger"])
    entries[0]["arguments_hash"] = "0" * 64
    payload["state_sha256"] = ""
    mismatched = RuntimeCheckpoint.from_payload(payload)
    mismatch_capability = RecoveryCapability(replay_safe=False)
    mismatch_runtime = AutoGenDiscussionRuntime(
        FailingIfCalledGateway(),
        discussion_plan(tools=("web.search",)),
        capability_gateway=mismatch_capability,
        artifact_repository=repository,
    )
    await mismatch_runtime.restore_checkpoint(mismatched)
    mismatch_events = await collect(mismatch_runtime, resumed_context(context, mismatched))
    assert mismatch_capability.calls == []
    assert mismatch_events[-1].kind is EventKind.RUNTIME_FAILED


async def test_replay_safe_tool_running_resumes_once_with_same_identity_and_args_hash() -> None:
    checkpoint, context, first_capability, repository = await running_tool_checkpoint(True)
    ledger = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state["tool_ledger"])
    second_capability = RecoveryCapability(replay_safe=True)
    recovered_gateway = ToolScenarioGateway(final_reply=True)
    recovered = AutoGenDiscussionRuntime(
        recovered_gateway,
        discussion_plan(tools=("web.search",)),
        capability_gateway=second_capability,
        artifact_repository=repository,
    )
    await recovered.restore_checkpoint(checkpoint)
    events = await collect(recovered, resumed_context(context, checkpoint))
    assert len(second_capability.calls) == 1, (
        [(event.kind, event.reason) for event in events],
        len(recovered_gateway.requests),
        checkpoint.state,
    )
    assert second_capability.calls[0] == first_capability.calls[0]
    assert second_capability.calls[0][0] == ledger[0]["idempotency_key"]
    assert second_capability.calls[0][1] == ledger[0]["arguments_hash"]
    historical_hashes = {
        cast(str, entry["request_hash"]) for entry in checkpoint.state["model_ledger"]
    }
    assert recovered_gateway.requests
    assert all(
        model_request_hash(request) not in historical_hashes
        for request in recovered_gateway.requests
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
