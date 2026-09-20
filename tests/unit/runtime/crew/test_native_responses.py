from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient, ModelResponseError, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelCapability, ModelRequest, ModelResponse
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CrewAIObjectFactory,
    CrewDispatchRuntime,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.instruction_context import model_request_sha256
from tests.unit.harness.test_tool_gateway import FakePolicyGateway, FakeRuntimeCapabilityGateway
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease
from tests.unit.runtime.crew.test_instruction_context import injections, task


class CapabilityBackend(FakeRuntimeCapabilityGateway):
    def is_replay_safe(self, name: str) -> bool:
        return name == "web.search"


class RecordingHarness(HarnessToolGateway):
    def __init__(self, backend: CapabilityBackend, policy: FakePolicyGateway) -> None:
        super().__init__(backend, policy_gateway=policy, require_actor_identity=True)
        self.calls: list[tuple[UUID, HarnessToolCallRequest, UUID | None, Role | None]] = []

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.calls.append((tenant_id, request, user_id, role))
        return await super().invoke(tenant_id, request, user_id=user_id, role=role)


class RecordingTransport(LiteLLMClient):
    def __init__(self, factory: OpenAIClientFactory) -> None:
        super().__init__(client_factory=factory)
        self.requests: list[ModelRequest] = []
        self.failures: list[Exception] = []

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str,
    ) -> ModelResponse:
        self.requests.append(request)
        try:
            return await super().complete(deployment, request, api_key)
        except Exception as error:
            self.failures.append(error)
            raise


def tool_item(name: str = "web_search") -> dict[str, Any]:
    return {
        "type": "function_call", "id": "fc_item_not_the_call_id",
        "call_id": "call_provider_search", "name": name,
        "arguments": '{"q":"safe"}', "status": "completed",
    }


def message_item(field: str = "result") -> dict[str, Any]:
    return {
        "type": "message", "id": "msg_final", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "annotations": [], "text": json.dumps({
            field: "14", "evidence": ["web.search"],
        })}],
    }


def response(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_native", "object": "response", "created_at": 1_700_000_000,
        "model": "native-model", "status": "completed", "output": output,
        "error": None, "incomplete_details": None,
        "usage": {"input_tokens": 12, "output_tokens": 9, "total_tokens": 21},
    }


class NativeRun:
    """Only capacity, secrets, policy decisions and HTTP are faked, not orchestration."""

    def __init__(
        self,
        root: Path,
        outputs: list[list[dict[str, Any]]],
        *,
        field: str = "result",
        step_tools: tuple[str, ...] = ("web.search",),
    ) -> None:
        self.backend = CapabilityBackend()
        self.policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
        self.harness = RecordingHarness(self.backend, self.policy)
        self.wire: list[tuple[str, dict[str, Any]]] = []
        self.clients: list[AsyncOpenAI] = []
        self.closed_by_transport: list[bool] = []
        self.plan = DispatchPlan(
            agents=(AgentSpec(
                id="writer", role="writer", goal="Write", logical_model="general",
                output_schema={field: "string", "evidence": "string[]"},
                allowed_tools=("web.search",),
            ),),
            steps=(DispatchStep(
                id="final", agent="writer", task="Search once and report the result",
                tools=step_tools, final_synthesizer=True, token_budget=8192,
            ),),
            allowed_tools=("web.search",), total_token_budget=20_000,
        )

        def handle(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url) == "https://native.example/v1/responses"
            index = len(self.wire)
            assert index < len(outputs), "Unexpected extra provider call"
            if index:
                assert len(self.executions()) == 1, "Continuation must follow tool execution"
            self.wire.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json=response(outputs[index]))

        def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
            assert max_retries == 0
            client = AsyncOpenAI(
                api_key=api_key, base_url=base_url, max_retries=max_retries,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            )
            self.clients.append(client)
            return client

        self.transport = RecordingTransport(cast(OpenAIClientFactory, factory))
        deployment = Deployment(
            id="native", logical_model="general", provider_model="openai/native-model",
            request_model="native-model", api_base="https://native.example/v1",
            secret_ref="native-test-secret", quota_scope_id="scope-native",
            structured_output_api="responses",
            capabilities=frozenset({
                ModelCapability.TEXT, ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT,
            }),
            input_per_million_usd=Decimal(0), output_per_million_usd=Decimal(0),
        )
        self.capacity = CapacityStub([lease("native") for _ in outputs])
        self.gateway = ModelGateway(
            ModelRegistry([deployment]), self.capacity, SecretStub([]), self.transport,
        )
        self.runtime = CrewDispatchRuntime(
            self.gateway, self.plan, capability_gateway=self.backend,
            harness_tool_gateway=self.harness,
            crew_factory=CrewAIObjectFactory(storage_dir=root / "crew"),
        )

    def executions(self) -> list[tuple[str, Mapping[str, object], str]]:
        return [call for call in self.backend.calls if call[0] == "execute"]

    async def run(self, context: TaskContext, events: list[RunEvent]) -> None:
        try:
            async with asyncio.timeout(30):
                async for event in self.runtime.run(context):
                    events.append(event)
        finally:
            # Close resources even when an assertion or production contract rejects the response.
            await self.runtime.cancel()
            self.closed_by_transport = [client.is_closed() for client in self.clients]
            for client in self.clients:
                await client.close()


def assert_single_known_rejected_usage(checkpoint_state: Mapping[str, object]) -> None:
    usage = cast(Mapping[str, object], checkpoint_state["usage"])
    assert usage["tokens"] == 21
    rejected = cast(Mapping[str, Mapping[str, object]], checkpoint_state["rejected_outputs"])
    assert len(rejected) == 1
    receipt = next(iter(rejected.values()))
    assert receipt["usage_status"] == "known"
    assert receipt["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 9,
        "total_tokens": 21,
    }


@pytest.mark.parametrize("field", ["result", "format"])
async def test_native_responses_real_crew_tool_continuation(tmp_path: Path, field: str) -> None:
    context = await task(tmp_path)
    case = NativeRun(tmp_path, [[tool_item()], [message_item(field)]], field=field)
    before = case.plan.model_dump_json()
    events: list[RunEvent] = []
    await case.run(context, events)

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert case.closed_by_transport == [True, True]
    assert case.transport.failures == []
    assert case.plan.model_dump_json() == before
    assert len(case.wire) == len(case.transport.requests) == len(case.capacity.releases) == 2
    assert len(case.executions()) == len(case.harness.calls) == len(case.policy.requests) == 1
    tenant, envelope, user, role = case.harness.calls[0]
    assert (tenant, envelope.run_id, user, role) == (
        context.tenant_id, context.run_id, context.actor_id, Role.OPERATOR,
    )
    assert (envelope.actor, envelope.tool_name, envelope.arguments) == (
        "writer", "web.search", {"q": "safe"},
    )
    assert envelope.approval_required is True and envelope.sandbox == "restricted"
    policy_request, policy_role = case.policy.requests[0]
    assert (policy_request.tenant_id, policy_request.user_id, policy_request.run_id,
            policy_request.agent_id, policy_role) == (
        context.tenant_id, context.actor_id, context.run_id, "writer", Role.OPERATOR,
    )
    assert case.executions()[0][1] == {
        "tenant_id": str(context.tenant_id), "run_id": str(context.run_id),
        "actor": "writer", "name": "web.search", "arguments": {"q": "safe"},
    }
    assert policy_request.idempotency_key == case.executions()[0][2] == envelope.idempotency_key

    bodies = [body for _, body in case.wire]
    for body, request in zip(bodies, case.transport.requests, strict=True):
        assert request.response_schema is not None
        schema = json.loads(json.dumps(request.response_schema.schema, default=dict))
        assert body["text"]["format"] == {
            "type": "json_schema", "name": request.response_schema.name,
            "schema": schema, "strict": True,
        }
        assert schema["properties"][field]["type"] == "string"
        assert schema["additionalProperties"] is False
        assert body["model"] == "native-model" and body["stream"] is False
        assert body["store"] is False
        assert body["max_output_tokens"] == request.max_output_tokens
        assert "messages" not in body and "response_format" not in body
        assert body["input"] == [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
        assert body["tools"] == [{
            "type": "function", "name": tool.name, "description": tool.description,
            "parameters": json.loads(json.dumps(tool.parameters, default=dict)),
        } for tool in request.tools]
        assert [tool["name"] for tool in body["tools"]] == ["web_search"]
        assert body["tool_choice"] == "auto"
        messages = [item["content"] for item in body["input"]]
        assert sum(text.count("<PROJECT_GUIDANCE_JSON>") for text in messages) == 1
        assert "PRIVATE-GUIDANCE" in "\n".join(messages)
        marker = "INTERNAL_RESPONSE_SCHEMA_JSON="
        assert sum(text.count(marker) for text in messages) == 1
        contract = next(text for text in messages if marker in text)
        assert json.loads(contract.split(marker, 1)[1]) == schema
    assert bodies[0]["tools"] == bodies[1]["tools"]
    continuation = next(item["content"] for item in bodies[1]["input"]
                        if "UNTRUSTED_CAPABILITY_RESULTS_JSON=" in item["content"])
    assert "fc_item_not_the_call_id" not in continuation
    assert json.loads(continuation.split("UNTRUSTED_CAPABILITY_RESULTS_JSON=", 1)[1]) == [
        {"name": "web.search", "result": {"value": "14"}},
    ]

    artifacts = [event.artifact for event in events if event.artifact is not None]
    models = [artifact for artifact in artifacts if artifact.type == "model_response"]
    assert len(models) == 2
    calls = cast(list[Mapping[str, object]], models[0].content["tool_calls"])
    assert calls[0]["id"] == "call_provider_search"
    assert calls[0]["name"] == "web.search"
    assert models[1].content["text"] == json.dumps({field: "14", "evidence": ["web.search"]})
    for artifact in models:
        assert artifact.content["usage"] == {
            "prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21,
        }
    checkpoint = await case.runtime.save_checkpoint()
    assert cast(Mapping[str, object], checkpoint.state["usage"])["tokens"] == 42
    tools = cast(Mapping[str, Mapping[str, object]], checkpoint.state["tools"])
    assert set(tools) == {envelope.idempotency_key}
    assert tools[envelope.idempotency_key]["status"] == "succeeded"
    ledger = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert len(ledger) == 2
    injected = injections(events)
    assert [event.payload["call_index"] for event in injected] == [0, 1]
    assert context.instruction_context is not None
    for event, request in zip(injected, case.transport.requests, strict=True):
        state = ledger[cast(str, event.payload["ledger_key"])]
        assert state["status"] == "succeeded"
        assert event.payload["ledger_request_sha256"] == state["request_sha256"]
        assert state["request_sha256"] == case.runtime._model_request_sha256(request)
        assert event.payload["request_sha256"] == model_request_sha256(request)
        assert event.payload["load_id"] == str(context.instruction_context.load_id)
        assert event.payload["actor"] == state["actor"] == "writer"
        assert "PRIVATE-GUIDANCE" not in json.dumps(event.to_payload())


@pytest.mark.parametrize("tool_name,step_tools", [
    ("unknown_tool", ("web.search",)),
    ("web_search", ()),
])
async def test_native_responses_unknown_or_unoffered_tool_never_executes(
    tmp_path: Path, tool_name: str, step_tools: tuple[str, ...],
) -> None:
    context = await task(tmp_path)
    case = NativeRun(tmp_path, [[tool_item(tool_name)]], step_tools=step_tools)
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        await case.run(context, events)
    assert len(case.wire) == 1
    assert case.closed_by_transport == [True]
    assert len(case.transport.failures) == 1
    assert isinstance(case.transport.failures[0], ModelResponseError)
    assert [tool["name"] for tool in case.wire[0][1].get("tools", [])] == (
        ["web_search"] if step_tools else []
    )
    assert case.harness.calls == []
    assert case.policy.requests == []
    assert case.executions() == []
    assert all(event.kind is not EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    checkpoint = await case.runtime.save_checkpoint()
    assert checkpoint.state["tools"] == {}
    assert_single_known_rejected_usage(checkpoint.state)


@pytest.mark.parametrize("boundary", ["incomplete", "failed", "refusal", "unsupported"])
async def test_native_responses_mixed_output_cannot_execute_a_valid_tool(
    tmp_path: Path, boundary: str,
) -> None:
    context = await task(tmp_path)
    item = message_item()
    if boundary in {"incomplete", "failed"}:
        item["status"] = boundary
    elif boundary == "refusal":
        item["content"].append({"type": "refusal", "refusal": "PRIVATE-REFUSAL"})
    else:
        item = {"type": "web_search_call", "id": "builtin", "status": "completed"}
    case = NativeRun(tmp_path, [[tool_item(), item]])
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured output invalid") as caught:
        await case.run(context, events)
    assert "PRIVATE-REFUSAL" not in str(caught.value)
    assert len(case.wire) == 1
    assert case.closed_by_transport == [True]
    assert len(case.transport.failures) == 1
    assert isinstance(case.transport.failures[0], ModelResponseError)
    assert case.harness.calls == []
    assert case.policy.requests == []
    assert case.executions() == []
    assert all(event.kind is not EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    checkpoint = await case.runtime.save_checkpoint()
    assert checkpoint.state["tools"] == {}
    assert_single_known_rejected_usage(checkpoint.state)
