from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion, ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewDispatchRuntime,
    RuntimeExecutionError,
)
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    ManifestCapabilities,
    _context,
    _manifest_tool_plan,
)

NAME = "calendar.create_event"
GOOD: Mapping[str, JsonValue] = {"title": "Review", "date": "2026-10-15"}
SECRET = "PRIVATE-ARGUMENT-OR-CONST"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


class Capabilities(ManifestCapabilities):
    def __init__(self, schema: object = "default", *, kind: str = "plugin") -> None:
        super().__init__()
        self.schema = schema
        self.kind = kind

    def is_replay_safe(self, name: str) -> bool:
        return False

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, object]:
        result = super().capability_manifest(tenant_id)
        item = dict(cast(tuple[Mapping[str, object], ...], result["capabilities"])[0])
        item["kind"] = self.kind
        if self.schema is None:
            item.pop("input_schema")
        elif self.schema != "default":
            item["input_schema"] = self.schema
        return {"schema_version": 1, "capabilities": (item,)}


class Gateway:
    def __init__(self, *arguments: Mapping[str, JsonValue], structured: bool = False,
                 gate_after: int | None = None) -> None:
        self.arguments = arguments
        self.structured = structured
        self.gate_after = gate_after
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        index = len(self.requests)
        self.requests.append(request)
        if index == self.gate_after:
            await asyncio.Event().wait()
        if index < len(self.arguments):
            response = ModelResponse(text=None, tool_calls=(ToolCall(
                id=f"model-call-{index}", name=request.tools[0].name,
                arguments=self.arguments[index],
            ),), usage=TokenUsage(1, 1, 2))
        else:
            response = ModelResponse(
                text='{"summary":"done"}' if self.structured else "done",
                usage=TokenUsage(1, 1, 2),
            )
        return GatewayCompletion(
            response=response, deployment_id="fixture", logical_model=request.logical_model,
            provider_id="fixture", provider_model="fixture/model", cost_usd=Decimal(0),
        )


class Harness:
    def __init__(self, failure: str | None = None, *, raises: bool = False,
                 gate: bool = False) -> None:
        self.failure = failure
        self.raises = raises
        self.gate = gate
        self.calls: list[HarnessToolCallRequest] = []
        self.identities: list[tuple[UUID, UUID | None, Role | None]] = []

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None,
                     ) -> HarnessToolCallResult:
        self.calls.append(request)
        self.identities.append((tenant_id, user_id, role))
        if self.gate:
            await asyncio.Event().wait()
        if self.raises:
            raise RuntimeCapabilityError(self.failure or "backend failed")
        return HarnessToolCallResult(
            call_id=request.call_id, tool_name=request.tool_name,
            status="failed" if self.failure else "succeeded", payload={} if self.failure else
            {"created": True}, failure_reason=self.failure,
        )


def subject(gateway: Gateway, harness: Harness, capabilities: Capabilities | None = None,
            repository: InMemoryArtifactRepository | None = None) -> CrewDispatchRuntime:
    plan = _manifest_tool_plan()
    if gateway.structured:
        plan = plan.model_copy(update={"agents": (
            plan.agents[0].model_copy(update={"output_schema": {"summary": "string"}}),
        )})
    return CrewDispatchRuntime(
        gateway, plan, capability_gateway=capabilities or Capabilities(),
        harness_tool_gateway=harness, crew_factory=FastFactory(), artifact_repository=repository,
    )


def states(checkpoint: RuntimeCheckpoint) -> list[Mapping[str, Any]]:
    return list(cast(Mapping[str, Mapping[str, Any]], checkpoint.state["tools"]).values())


async def collect(runtime: CrewDispatchRuntime, checkpoint: RuntimeCheckpoint | None = None,
                  events: list[RunEvent] | None = None) -> list[RunEvent]:
    result = events if events is not None else []
    async for event in runtime.run(_context(checkpoint=checkpoint)):
        result.append(event)
    return result


@pytest.mark.parametrize("structured", (False, True))
@pytest.mark.parametrize("bad,keyword", (({}, "required"),
                                       ({"title": 7, "date": SECRET}, "type")))
async def test_plugin_prevalidation_corrects_before_one_execution(
    structured: bool, bad: Mapping[str, JsonValue], keyword: str,
) -> None:
    gateway = Gateway(bad, GOOD, structured=structured)
    harness = Harness()
    runtime = subject(gateway, harness)
    events = await collect(runtime)
    checkpoint = await runtime.save_checkpoint()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [dict(call.arguments) for call in harness.calls] == [GOOD]
    assert len(gateway.requests) == 3
    assert len([e for e in events if e.kind is EventKind.TOOL_STARTED]) == 1
    rejected = next(s for s in states(checkpoint) if s["status"] == "rejected")
    assert rejected["arguments_sha256"] == digest(bad)
    # Rejection must be durable before the next model request, never marked running.
    first_tool_states = [s for e in events if e.checkpoint for s in states(e.checkpoint)
                         if s["arguments_sha256"] == digest(bad)]
    assert first_tool_states and {s["status"] for s in first_tool_states} == {"rejected"}
    packet = next(json.loads(str(m.content).split("=", 1)[1])[0]["result"]
                  for m in gateway.requests[1].messages
                  if str(m.content).startswith("UNTRUSTED_CAPABILITY_REJECTIONS_JSON="))
    assert packet["phase"] == "pre_invocation" and packet["execution_started"] is False
    assert packet["tool_name"] == NAME
    assert packet["arguments_sha256"] == digest(bad)
    assert packet["validation_keyword"] == keyword
    assert len(packet["schema_sha256"]) == 64
    if not bad:
        assert set(packet["missing_fields"]) == {"title", "date"}
    assert SECRET not in json.dumps(packet)
    assert harness.calls[0].actor == "writer"
    assert harness.calls[0].sandbox == "remote_connector"
    assert harness.calls[0].approval_required is False


@pytest.mark.parametrize("args,expected_calls", ((({}, {}), 2),
    (({}, {"title": "a"}, {"date": "b"}), 3)))
async def test_plugin_correction_is_bounded(
    args: tuple[Mapping[str, JsonValue], ...], expected_calls: int,
) -> None:
    gateway, harness = Gateway(*args), Harness()
    runtime = subject(gateway, harness)
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime)
    assert len(gateway.requests) == expected_calls
    assert harness.calls == []
    assert len([s for s in states(await runtime.save_checkpoint())
                if s["status"] == "rejected"]) <= 2


@pytest.mark.parametrize("failure", (
    "Plugin arguments do not match input schema", "plugin.invalid_arguments",
    "workspace path must not contain hidden files", "permission denied", "invalid output",
))
@pytest.mark.parametrize("raises", (False, True))
async def test_backend_failure_strings_never_prove_preinvocation(
    failure: str, raises: bool,
) -> None:
    gateway, harness = Gateway(GOOD), Harness(failure, raises=raises)
    runtime = subject(gateway, harness)
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime)
    assert len(gateway.requests) == len(harness.calls) == 1
    assert all(s["status"] != "rejected" for s in states(await runtime.save_checkpoint()))


@pytest.mark.parametrize("schema", (None, {"type": "object", "required": 7},
    {"type": "array", "items": {"type": "string"}}, {"type": "string"},
    {"type": "object", "properties": {"title": {"type": "not-a-type"}}},
    {"type": "object", "$schema": "https://unreachable.invalid/schema", "required": ["title"]},
    {"type": "object", "$ref": "https://unreachable.invalid/schema"}))
async def test_missing_or_broken_schema_never_grants_correction(schema: object) -> None:
    gateway, harness = Gateway({}), Harness("plugin.invalid_arguments")
    runtime = subject(gateway, harness, Capabilities(schema))
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime)
    assert len(gateway.requests) == 1
    assert all(s["status"] != "rejected" for s in states(await runtime.save_checkpoint()))


async def test_nonplugin_schema_does_not_grant_correction() -> None:
    gateway, harness = Gateway({}), Harness("plugin.invalid_arguments")
    runtime = subject(gateway, harness, Capabilities(kind="builtin"))
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime)
    assert len(gateway.requests) == len(harness.calls) == 1


async def test_absent_schema_keeps_gateway_compatibility_without_local_rejection() -> None:
    gateway, harness = Gateway({}), Harness()
    runtime = subject(gateway, harness, Capabilities(None))
    await collect(runtime)
    assert [dict(call.arguments) for call in harness.calls] == [{}]
    assert {s["status"] for s in states(await runtime.save_checkpoint())} == {"succeeded"}
    assert not any("CAPABILITY_ARGUMENT_CORRECTION" in str(m.content)
                   for request in gateway.requests for m in request.messages)


@pytest.mark.parametrize("correct_first", (False, True))
async def test_implicit_object_schema_preserves_plugin_validation_semantics(
    correct_first: bool,
) -> None:
    schema = {"properties": {"text": {"type": "string"}}, "required": ["text"]}
    valid: Mapping[str, JsonValue] = {"text": "review"}
    gateway, harness = Gateway(*(({}, valid) if correct_first else (valid,))), Harness()
    runtime = subject(gateway, harness, Capabilities(schema))
    events = await collect(runtime)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [dict(call.arguments) for call in harness.calls] == [valid]
    assert len(gateway.requests) == (3 if correct_first else 2)
    assert len([s for s in states(await runtime.save_checkpoint())
                if s["status"] == "rejected"]) == int(correct_first)


@pytest.mark.parametrize("structured", (False, True))
async def test_large_valid_schema_keeps_authoritative_gateway_path(structured: bool) -> None:
    choices = [f"value{index}-" + "x" * 128 for index in range(16)]
    schema = {"type": "object", "properties": {
        f"field{index}": {"type": "string", "enum": choices} for index in range(8)
    }}
    assert len(json.dumps(schema).encode()) > 8192
    arguments: Mapping[str, JsonValue] = {"field0": choices[0]}
    gateway, harness = Gateway(arguments, structured=structured), Harness()
    runtime = subject(gateway, harness, Capabilities(schema))
    try:
        events = await collect(runtime)
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert [dict(call.arguments) for call in harness.calls] == [dict(arguments)]
        assert len(gateway.requests) == 2
        assert {state["status"] for state in states(await runtime.save_checkpoint())} == {"succeeded"}
    finally:
        await runtime.cancel()


async def test_required_only_feedback_contains_no_raw_error_or_instance_fields() -> None:
    schema = {"required": ["text"]}
    gateway, harness = Gateway({"private": SECRET}, {"text": "review"}), Harness()
    runtime = subject(gateway, harness, Capabilities(schema))
    await collect(runtime)
    packet = next(json.loads(str(m.content).split("=", 1)[1])[0]["result"]
                  for m in gateway.requests[1].messages
                  if str(m.content).startswith("UNTRUSTED_CAPABILITY_REJECTIONS_JSON="))
    assert packet["validation_keyword"] == "required" and packet["missing_fields"] == []
    assert SECRET not in json.dumps(packet) and "private" not in json.dumps(packet)
    assert len(harness.calls) == 1


async def test_const_secret_is_not_disclosed_by_validation_feedback() -> None:
    schema = {"type": "object", "properties": {"title": {"const": SECRET}}}
    gateway, harness = Gateway({"title": "wrong"}, {"title": SECRET}), Harness()
    await collect(subject(gateway, harness, Capabilities(schema)))
    feedback = " ".join(str(m.content) for m in gateway.requests[1].messages)
    assert SECRET not in feedback and "wrong" not in feedback
    assert len(harness.calls) == 1


async def partial(runtime: CrewDispatchRuntime, status: str) -> RuntimeCheckpoint:
    checkpoint = None
    try:
        async with asyncio.timeout(10):
            async for event in runtime.run(_context()):
                if event.checkpoint and any(s["status"] == status for s in states(event.checkpoint)):
                    checkpoint = event.checkpoint
                    break
    finally:
        await runtime.cancel()
    assert checkpoint is not None
    return checkpoint


async def test_durable_rejection_restores_without_reexecuting_bad_arguments() -> None:
    repository = InMemoryArtifactRepository()
    first_harness = Harness()
    first = subject(Gateway({}, gate_after=1), first_harness, repository=repository)
    checkpoint = await partial(first, "rejected")
    assert first_harness.calls == []
    gateway, harness = Gateway(GOOD), Harness()
    resumed = subject(gateway, harness, repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(resumed, checkpoint)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(harness.calls) == 1 and len(gateway.requests) == 2
    assert "pre_invocation" in " ".join(str(m.content) for m in gateway.requests[0].messages)
    assert (await resumed.save_checkpoint()).state["usage"] == {"tokens": 6, "cost_usd": "0"}


@pytest.mark.parametrize("status", ("running", "uncertain"))
@pytest.mark.parametrize("schema", (None, {"type": "object", "required": ["new"]},
                                     {"type": "object", "required": 7}))
async def test_changed_schema_cannot_release_unknown_execution(status: str, schema: object) -> None:
    repository = InMemoryArtifactRepository()
    first = subject(Gateway(GOOD), Harness(gate=True), repository=repository)
    checkpoint = await partial(first, "running")
    if status == "uncertain":
        checkpoint = await first.save_checkpoint()
        assert {s["status"] for s in states(checkpoint)} == {"uncertain"}
    gateway, harness = Gateway({}), Harness()
    resumed = subject(gateway, harness, Capabilities(schema), repository)
    with pytest.raises(CapabilityOutcomeUncertain):
        await resumed.restore_checkpoint(checkpoint)
        await collect(resumed, checkpoint)
    assert gateway.requests == [] and harness.calls == []
    assert {s["status"] for s in states(checkpoint)} == {status}


async def test_running_plugin_precedes_semantic_reuse_even_if_declared_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReplayCapabilities(Capabilities):
        def is_replay_safe(self, name: str) -> bool:
            return True

    repository = InMemoryArtifactRepository()
    first = subject(Gateway(GOOD), Harness(gate=True), ReplayCapabilities(), repository)
    checkpoint = await partial(first, "running")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("UNKNOWN must precede semantic reuse and input validation")

    monkeypatch.setattr(adapter, "_succeeded_semantic_tool_result", forbidden)
    monkeypatch.setattr(adapter, "_plugin_argument_prevalidation", forbidden)
    gateway, harness = Gateway(GOOD), Harness()
    resumed = subject(gateway, harness, ReplayCapabilities(), repository)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(CapabilityOutcomeUncertain):
        await collect(resumed, checkpoint)
    assert gateway.requests == [] and harness.calls == []


async def test_existing_backend_failure_cannot_be_relabelled_as_preinvocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryArtifactRepository()
    first = subject(Gateway(GOOD), Harness("plugin.invalid_arguments"), repository=repository)
    with pytest.raises(RuntimeExecutionError):
        await collect(first)
    checkpoint = await first.save_checkpoint()
    assert {s["status"] for s in states(checkpoint)} == {"failed"}

    def forbidden(*args: object) -> None:
        pytest.fail("An executed failure is not a new pre-invocation request")

    monkeypatch.setattr(adapter, "_plugin_argument_prevalidation", forbidden)
    gateway, harness = Gateway(), Harness("plugin.invalid_arguments")
    resumed = subject(gateway, harness, repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError):
        await collect(resumed, checkpoint)
    assert len(harness.calls) == 1 and gateway.requests == []
    assert {s["status"] for s in states(await resumed.save_checkpoint())} == {"failed"}


async def test_argument_rejection_does_not_extend_round_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(1, 1, 4))
    gateway, harness = Gateway({}, GOOD), Harness()
    runtime = subject(gateway, harness)
    with pytest.raises(RuntimeExecutionError, match="round limit exceeded"):
        await collect(runtime)
    assert len(gateway.requests) == 2 and harness.calls == []


@pytest.mark.parametrize("damage", (
    "missing", "legacy", "run_id", "arguments_sha256", "schema_sha256", "both_schema_hashes",
))
async def test_restore_refuses_missing_or_forged_prevalidation(damage: str) -> None:
    repository = InMemoryArtifactRepository()
    checkpoint = await partial(subject(Gateway({}, gate_after=1), Harness(),
                                       repository=repository), "rejected")
    payload = cast(dict[str, Any], checkpoint.to_payload())
    registry = payload["state"]["artifact_registry"]
    ctx = _context()
    stored = await repository.get_many(ctx.tenant_id, ctx.run_id, tuple(
        ArtifactReference(UUID(key), sha) for key, sha in registry.items()
    ))
    replacement = InMemoryArtifactRepository()
    rejected_state = next(s for s in payload["state"]["tools"].values()
                          if s["status"] == "rejected")
    for artifact in stored:
        if str(artifact.id) == rejected_state["artifact_id"]:
            data = cast(dict[str, Any], artifact.to_payload())
            if damage in {"missing", "legacy"}:
                data["content"].pop("prevalidation")
                if damage == "legacy":
                    data["content"]["result"]["error_code"] = "invalid_workspace_path"
            elif damage == "both_schema_hashes":
                data["content"]["prevalidation"]["schema_sha256"] = "0" * 64
                data["content"]["result"]["schema_sha256"] = "0" * 64
            else:
                data["content"]["prevalidation"][damage] = "0" * 64
            data["content_sha256"] = ""
            artifact = Artifact.from_payload(data)
            registry[str(artifact.id)] = rejected_state["sha256"] = artifact.content_sha256
        await replacement.put(ctx.tenant_id, ctx.run_id, artifact)
    payload["state_sha256"] = ""
    damaged = RuntimeCheckpoint.from_payload(payload)
    gateway, harness = Gateway(GOOD), Harness()
    resumed = subject(gateway, harness, repository=replacement)
    await resumed.restore_checkpoint(damaged)
    with pytest.raises(RuntimeExecutionError, match="plugin argument rejection evidence is invalid"):
        await collect(resumed, damaged)
    assert gateway.requests == [] and harness.calls == []


@pytest.mark.parametrize("api", ("chat_completions", "responses"))
async def test_real_structured_dispatch_transport_shares_prevalidation(api: str) -> None:
    wire: list[dict[str, Any]] = []
    harness = Harness()
    clients: list[AsyncOpenAI] = []

    def handle(request: httpx.Request) -> httpx.Response:
        index = len(wire)
        wire.append(json.loads(request.content))
        assert len(harness.calls) == (1 if index == 2 else 0)
        assert index < 3
        args = {} if index == 0 else GOOD
        if api == "responses":
            output = [{"type": "function_call", "id": f"fc-{index}",
                       "call_id": f"provider-{index}", "name": "calendar_create_event",
                       "arguments": json.dumps(args), "status": "completed"}] if index < 2 else [{
                           "type": "message", "id": "final", "role": "assistant",
                           "status": "completed", "content": [{"type": "output_text",
                               "annotations": [], "text": '{"summary":"done"}'}],
                       }]
            return httpx.Response(200, json={
                "id": f"response-{index}", "object": "response", "created_at": 1_700_000_000,
                "model": "fixture-model", "status": "completed", "output": output,
                "error": None, "incomplete_details": None,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            })
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"provider-{index}", "type": "function", "function": {
                "name": "calendar_create_event", "arguments": json.dumps(args),
            },
        }]} if index < 2 else {"role": "assistant", "content": '{"summary":"done"}'}
        return httpx.Response(200, json={
            "id": f"chat-{index}", "object": "chat.completion", "created": 1_700_000_000,
            "model": "fixture-model", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if index < 2 else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=max_retries,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        clients.append(client)
        return client

    gateway = ModelGateway(
        ModelRegistry([deployment("fixture", logical_model="general", capabilities=frozenset({
            ModelCapability.TEXT, ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT,
        }), structured_output_api=api, provider_model="openai/fixture-model",
            api_base="https://owned.invalid/v1")]),
        CapacityStub([lease("fixture") for _ in range(3)]), SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
    )
    plan = _manifest_tool_plan()
    plan = plan.model_copy(update={"agents": (plan.agents[0].model_copy(update={
        "output_schema": {"summary": "string"},
    }),)})
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=Capabilities(), harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    try:
        events = await collect(runtime)
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(wire) == 3 and [dict(call.arguments) for call in harness.calls] == [GOOD]
        assert "pre_invocation" in json.dumps(wire[1])
        assert {s["status"] for s in states(await runtime.save_checkpoint())} == {"rejected", "succeeded"}
    finally:
        await runtime.cancel()
        for client in clients:
            await client.close()
