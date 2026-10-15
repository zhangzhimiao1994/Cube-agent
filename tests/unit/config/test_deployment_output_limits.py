"""Unknown provider limits must not override bounded request output budgets."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.config.schema import DeploymentDefinition
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.responses import response_create_kwargs
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from tests.unit.config.test_schema import deployment
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import draft_models, subject, tool_plan


@pytest.mark.parametrize("configured,request_cap,context,input_tokens,expected", [
    ({}, 12288, 65536, 1000, 12288),
    ({"max_output_tokens": None}, 12288, 65536, 1000, 12288),
    ({"max_output_tokens": 8192}, 12288, 65536, 1000, 8192),
    ({"max_output_tokens": 16384}, 12288, 65536, 1000, 12288),
    ({}, 4096, 65536, 1000, 4096),
    ({}, 12288, 9000, 1000, 8000),
    ({}, 4096, 2048, 1000, 1048),
    ({}, 80000, None, 1000, 64536),
])
def test_config_round_trip_preserves_unknown_or_explicit_limit_at_wire_boundary(
    configured: dict[str, int | None], request_cap: int, context: int | None,
    input_tokens: int, expected: int,
) -> None:
    definition = deployment(context_window_tokens=context, **configured)
    restored = DeploymentDefinition.model_validate_json(definition.model_dump_json())
    runtime = restored.to_deployment(deployment_id="primary", logical_model="primary")
    request = ModelRequest(
        logical_model="primary", messages=(ModelMessage(role="user", content="fixed task"),),
        max_output_tokens=request_cap,
        required_capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
        response_schema=StructuredResponseSchema(name="Result", schema={"type": "object"}),
    )
    selected = ModelGateway._request_for_deployment(request, runtime, input_tokens)
    wire = response_create_kwargs(runtime, selected)
    assert restored.max_output_tokens == configured.get("max_output_tokens")
    assert runtime.max_output_tokens == configured.get("max_output_tokens")
    assert runtime.context_window_tokens == (65536 if context is None else context)
    assert selected.max_output_tokens == wire["max_output_tokens"] == expected
    assert request.max_output_tokens == request_cap


@pytest.mark.parametrize("hard_limit", [None, 8192])
async def test_configured_gateway_preserves_post_tool_reservation_unless_limit_is_explicit(
    hard_limit: int | None,
) -> None:
    wire: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire.append(body)
        index = len(wire)
        assert index <= 4 and request.method == "POST"
        if index == 4:
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(200, json={
                "id": "final", "object": "chat.completion", "created": 1, "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "actual final answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 16, "total_tokens": 17},
            })
        assert request.url.path == "/v1/responses"
        incomplete = index == 2 or (index == 3 and body["max_output_tokens"] < 12288)
        completion = (6144 if index == 2 else 8192) if incomplete else 16
        prompt = 2234 if incomplete else 1
        output: list[dict[str, object]] = [{
            "id": "tool", "type": "function_call", "status": "completed", "call_id": "approved",
            "name": "web_search", "arguments": '{"q":"safe"}',
        }] if index == 1 else [{
            "id": f"msg-{index}", "type": "message", "role": "assistant",
            "status": "incomplete" if incomplete else "completed",
            "content": [{"type": "output_text", "annotations": [],
                         "text": "PRIVATE_PARTIAL" if incomplete else '{"summary":"complete handoff"}'}],
        }]
        return httpx.Response(200, json={
            "id": f"resp-{index}", "object": "response", "created_at": 1, "model": "fixture",
            "status": "incomplete" if incomplete else "completed", "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            "output": output,
            "usage": {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion},
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    configured = deployment(
        structured_output_api="responses", api_base="https://fixture.invalid/v1",
        capabilities={"text", "structured_output", "tool_calling"},
        **({} if hard_limit is None else {"max_output_tokens": hard_limit}),
    ).to_deployment(deployment_id="primary", logical_model="general")
    capacity = CapacityStub([lease("primary", configured.quota_scope_id) for _ in range(4)])
    await capacity.initialize()
    gateway = ModelGateway(
        ModelRegistry([configured]), capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
    )
    repository = InMemoryArtifactRepository()
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, tool_plan(), repository, harness)
    try:
        failure = None
        try:
            await collect(runtime)
        except RuntimeExecutionError as error:
            failure = error
        saved = await runtime.save_checkpoint()
        repair = mapping(mapping(saved.state["structured_repairs"])["draft"])
        assert repair["max_output_tokens"] == 12288
        assert wire[1]["max_output_tokens"] == 6144
        assert wire[2]["max_output_tokens"] == (8192 if hard_limit is not None else 12288)
        assert not wire[2].get("tools")
        assert len(harness.calls) == 1
        assert all(row["attempt"] == 0 for row in draft_models(saved))
        assert mapping(saved.state["usage"])["tokens"] == (18821 if hard_limit is not None else 8429)
        assert "PRIVATE_PARTIAL" not in str(saved.to_payload())
        if hard_limit is None:
            assert failure is None
            before = len(wire)
            replay_harness = RecordingHarnessToolGateway()
            replay = subject(gateway, tool_plan(), repository, replay_harness)
            await replay.restore_checkpoint(saved)
            await collect(replay, checkpoint=saved)
            assert len(wire) == before == 4 and replay_harness.calls == []
            assert (await replay.save_checkpoint()).to_payload() == saved.to_payload()
        else:
            assert isinstance(failure, RuntimeExecutionError) and str(failure) == "structured output invalid"
            assert len(wire) == 3 and repair["status"] == "rejected"
    finally:
        for client in clients:
            await client.close()
