from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.gateway import ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelCapability
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FakeCapabilities,
    FastFactory,
    RecordingHarnessToolGateway,
    _context,
)


@pytest.mark.parametrize("usage_status", ["known", "missing", "invalid"])
@pytest.mark.parametrize(("output_kind", "safe_text"), [
    ("invalid_tool", None), ("mixed_tool", None), ("invalid_output", None),
    ("invalid_tool", '{"summary":"PRIVATE_SAFE_REJECTED_TEXT"}'),
    ("mixed_tool", '{"summary":"PRIVATE_SAFE_REJECTED_TEXT"}'),
    ("sdk_validation", None),
])
@pytest.mark.parametrize("contract", ["zip", "incremental", "structured"])
async def test_chat_rejected_receipt_accounts_without_execution_or_paid_replay(
    usage_status: str, output_kind: str, safe_text: str | None, contract: str,
) -> None:
    sdk_validation = output_kind == "sdk_validation"
    accounted = usage_status == "known" and not sdk_validation
    private = "PRIVATE_MALFORMED_TOOL_ARGUMENT"
    malformed = {
        "id": "broken", "type": "function",
        "function": {"name": "project_generate_zip", "arguments": "{" + private},
    }
    tools = ("project.generate_zip",) if contract == "zip" else (
        "workspace.bundle", "workspace.write_text",
    ) if contract == "incremental" else ()
    calls = [malformed]
    if output_kind == "mixed_tool":
        calls.insert(0, {
            "id": "valid", "type": "function",
            "function": {
                "name": "project_generate_zip" if contract == "zip" else "workspace_write_text",
                "arguments": json.dumps(
                    {"title": "Own fixture", "files": {"test.js": ""}}
                    if contract == "zip" else {"path": "test.js", "text": ""},
                ),
            },
        })
    raw_usage: dict[str, int] | None = {
        "prompt_tokens": 12, "completion_tokens": 9,
        "total_tokens": 99 if usage_status == "invalid" else 21,
    } if usage_status != "missing" else None
    wire_calls: list[dict[str, Any]] = []
    clients: list[AsyncOpenAI] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://own-chat.example/v1/chat/completions"
        wire_calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat-own", "object": "chat.completion", "created": 1,
            "model": "own-model", "usage": raw_usage,
            "choices": private if sdk_validation else [] if output_kind == "invalid_output" else [{
                "index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": safe_text, "tool_calls": calls},
            }],
        })

    def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
        assert max_retries == 0
        client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=max_retries,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            _strict_response_validation=sdk_validation,
        )
        clients.append(client)
        return client

    deployment = Deployment(
        id="own-chat", logical_model="general", provider_model="openai/own-model",
        request_model="own-model", api_base="https://own-chat.example/v1",
        secret_ref="own-fixture-secret", quota_scope_id="scope-own-chat",
        capabilities=frozenset({
            ModelCapability.TEXT, ModelCapability.TOOL_CALLING,
            ModelCapability.STRUCTURED_OUTPUT,
        }),
        input_per_million_usd=Decimal(1), output_per_million_usd=Decimal(2),
    )
    capacity = CapacityStub([lease("own-chat") for _ in range(8)])
    gateway = ModelGateway(
        ModelRegistry([deployment]), capacity, SecretStub([]),
        LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
    )
    await capacity.initialize()
    plan = DispatchPlan(
        agents=(AgentSpec(
            id="writer", role="writer", goal="Write", logical_model="general",
            output_schema={"summary": "string"}, allowed_tools=tools,
        ),),
        steps=(DispatchStep(
            id="final", agent="writer",
            task="Project workspace delivery contract: produce the actual project bundle.",
            tools=tools, final_synthesizer=True,
            token_budget=100_000, cost_budget_usd=Decimal(1),
        ),),
        allowed_tools=tools, total_token_budget=100_000,
        total_cost_usd=Decimal(1),
    )
    repository = InMemoryArtifactRepository()
    capabilities, harness = FakeCapabilities(), RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
        capability_gateway=capabilities, harness_tool_gateway=harness,
    )
    events: list[RunEvent] = []
    try:
        with pytest.raises(RuntimeExecutionError) as failure:
            async for event in runtime.run(_context(token_budget=100_000)):
                events.append(event)
        assert wire_calls, str(failure.value)
        checkpoint = await runtime.save_checkpoint()
        expected_usage = {
            "tokens": 21 if accounted else 0,
            "cost_usd": "0.000030" if accounted else "0",
        }
        assert checkpoint.state["usage"] == expected_usage
        assert len(wire_calls) == 1
        assert len(capacity.releases) == 1
        assert capabilities.calls == []
        assert harness.calls == []
        assert checkpoint.state["tools"] == {}
        rejected = cast(Mapping[str, Mapping[str, Any]], checkpoint.state["rejected_outputs"])
        assert len(rejected) == 1
        receipt = next(iter(rejected.values()))
        assert receipt["final_text"] == (None if usage_status == "invalid" else safe_text)
        assert receipt["usage_status"] == ("missing" if sdk_validation else usage_status)
        reason = "invalid_output" if output_kind in {"invalid_output", "sdk_validation"} else "invalid_tool"
        assert receipt["reason"] == (
            "usage_invalid" if usage_status == "invalid" and not sdk_validation else reason
        )
        assert receipt["usage"] == (raw_usage if accounted else None)
        assert receipt["cost_usd"] == ("0.000030" if accounted else None)
        if sdk_validation:
            assert receipt["output_status"] == "unknown"
            assert receipt["text_sha256"] is None
        assert checkpoint.state["structured_repairs"] == {}
        if not accounted:
            assert checkpoint.state["phase"] == "unaccounted"
            assert str(failure.value) == "dispatch usage unaccounted"
        assert not any(event.kind in {EventKind.STEP_RETRYING, EventKind.RUNTIME_COMPLETED}
                       for event in events)
        public = str([_public_event_payload(event.to_payload()) for event in events])
        assert private not in public
        assert "PRIVATE_SAFE_REJECTED_TEXT" not in public
        models = cast(Mapping[str, Mapping[str, Any]], checkpoint.state["models"])
        assert [model["status"] for model in models.values()] == ["rejected"]
        before = deepcopy(checkpoint.to_payload())
        before_state = cast(Mapping[str, object], before["state"])
        accounting_before = {
            field: before_state[field]
            for field in ("usage", "step_usage", "models", "rejected_outputs")
        }
        replay = CrewDispatchRuntime(
            gateway, plan, crew_factory=FastFactory(), artifact_repository=repository,
            capability_gateway=capabilities, harness_tool_gateway=harness,
        )
        await replay.restore_checkpoint(checkpoint)
        restored = replay._restored_checkpoint
        assert restored is not None
        restored_state = cast(Mapping[str, object], restored.to_payload()["state"])
        assert {field: restored_state[field] for field in accounting_before} == accounting_before
        replay_events: list[RunEvent] = []
        with pytest.raises(RuntimeExecutionError) as replay_failure:
            async for event in replay.run(_context(checkpoint=checkpoint, token_budget=100_000)):
                replay_events.append(event)
        assert len(wire_calls) == 1
        assert len(capacity.releases) == 1
        assert capabilities.calls == []
        assert harness.calls == []
        assert str(replay_failure.value) == (
            "structured output invalid" if accounted else "dispatch usage unaccounted"
        )
        assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
        assert checkpoint.to_payload() == before
        # This cached rejection stops before publishing a new checkpoint boundary.
        if usage_status == "known" and output_kind == "invalid_output":
            with pytest.raises(
                RuntimeExecutionError, match="^runtime has no completed checkpoint boundary$",
            ):
                await replay.save_checkpoint()
            retained = checkpoint
        else:
            retained = await replay.save_checkpoint()
        retained_state = cast(Mapping[str, object], retained.to_payload()["state"])
        assert {field: retained_state[field] for field in accounting_before} == accounting_before
    finally:
        for client in clients:
            await client.close()
