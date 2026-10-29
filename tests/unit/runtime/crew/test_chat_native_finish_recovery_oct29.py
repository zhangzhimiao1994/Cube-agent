"""Real SDK -> gateway -> native Crew recovery; HTTP stays in MockTransport."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.gateway import GatewayRejectedOutput, ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelCapability, ModelRequest
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import _workspace_delivery_sequence_context
from tests.unit.runtime.crew.test_meaningful_workspace_progress import Observations
from tests.unit.runtime.crew.test_native_tool_continuation import subject


@pytest.mark.parametrize("finish", ["tool_calls", "length"])
async def test_native_chat_rejects_whole_batch_and_finishes_real_bundle(finish: str) -> None:
    wire: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []
    receipts: list[GatewayRejectedOutput] = []
    requests: list[ModelRequest] = []

    def call(identifier: str, name: str, arguments: str) -> dict[str, Any]:
        return {"id": identifier, "type": "function",
                "function": {"name": name, "arguments": arguments}}

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        index = len(wire)
        assert index <= 5, "correction must not become an unbounded paid loop"
        message: dict[str, Any] = {"role": "assistant", "content": None}
        end = "tool_calls"
        if index == 1:
            message["tool_calls"] = [call("write-once", "workspace_write_text", json.dumps({
                "path": "src/business.ts", "content": "export const ready = true;\n",
            }))]
        elif index == 2:
            # Even the valid prefix must never execute when the batch is rejected.
            message["tool_calls"] = [
                call("never-prefix", "workspace_write_text", json.dumps({
                    "path": "src/never.ts", "content": "PRIVATE_REJECTED_PREFIX",
                })), call("never-tail", "workspace_bundle", '{"title":'),
            ]
            end = finish
        elif index == 3:
            message["content"] = "Source is present. A verified bundle is still required."
            end = "stop"
        elif index == 4:
            message["tool_calls"] = [call("actual-bundle", "workspace_bundle", json.dumps({
                "title": "Verified native continuation",
            }))]
        else:
            message["content"] = "Project delivered with its downloadable bundle."
            end = "stop"
        return httpx.Response(200, json={
            "id": f"chat-{index}", "object": "chat.completion", "created": 1,
            "model": "mock-model", "choices": [{"index": 0, "finish_reason": end,
                                                     "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 16, "total_tokens": 17},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        clients.append(client)
        return client

    class RecordingGateway(ModelGateway):
        async def complete_with_context(self, request: ModelRequest) -> Any:
            requests.append(request)
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                receipts.append(error)
                raise

    registry = ModelRegistry([deployment(
        "primary", logical_model="general", capabilities=frozenset({
            ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
        }), request_model="mock-model", api_base="https://offline.example/v1",
        input_per_million_usd=Decimal(1), output_per_million_usd=Decimal(2),
    )])
    capacity = CapacityStub([lease("primary") for _ in range(5)])
    await capacity.initialize()
    gateway = RecordingGateway(registry, capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)))
    harness = Observations()
    runtime = subject(gateway, harness, InMemoryProviderContinuationRepository())
    runtime._plan = runtime._plan.model_copy(update={
        "total_token_budget": 100_000, "total_cost_usd": Decimal("0.10"),
        "steps": (runtime._plan.steps[0].model_copy(update={
            "token_budget": 100_000, "cost_budget_usd": Decimal("0.10"),
        }),),
    })
    context = _workspace_delivery_sequence_context().model_copy(update={"token_budget": 100_000})
    try:
        events = [event async for event in runtime.run(context)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert [entry.tool_name for entry in harness.calls] == [
            "workspace.write_text", "workspace.bundle",
        ]
        assert "src/never.ts" not in harness.files
        assert len(wire) == 5 and len(receipts) == 1
        evidence = receipts[0].evidence
        assert evidence is not None and evidence.final_text is None
        assert (evidence.reason, evidence.status) == (
            ("incomplete", "incomplete") if finish == "length" else ("invalid_tool", "completed")
        )
        assert requests[2].tools == () and not requests[2].allow_fallback
        assert requests[2].response_schema is None
        assert requests[2].messages[:-1] == requests[1].messages
        correction = json.dumps(wire[2])
        assert "never-prefix" not in correction and "never-tail" not in correction
        assert "PRIVATE_REJECTED_PREFIX" not in correction
        assert any(message.get("tool_call_id") == "write-once" for message in wire[2]["messages"])
        checkpoint = await runtime.save_checkpoint()
        assert checkpoint.state["usage"]["tokens"] == 85
        assert len(capacity.records) == len(capacity.releases) == 5
        assert "PRIVATE_REJECTED_PREFIX" not in json.dumps(checkpoint.to_payload())
        assert "PRIVATE_REJECTED_PREFIX" not in json.dumps([event.to_payload() for event in events])
    finally:
        for client in clients:
            await client.close()
