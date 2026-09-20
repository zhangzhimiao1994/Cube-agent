from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models.gateway import ModelGateway
from agent_hub.models.litellm_client import LiteLLMClient, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelCapability
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CrewAIObjectFactory,
    CrewDispatchRuntime,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease
from tests.unit.runtime.crew.test_instruction_context import task

PRIVATE_INVALID = "PRIVATE_NATIVE_INVALID_JSON_USAGE_129"


def _structured_reviewed_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string"},
            ),
            AgentSpec(id="reviewer", role="reviewer", goal="Review", logical_model="review"),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
                output_schema={"answer": "string"},
            ),
        ),
        steps=(
            DispatchStep(
                id="draft",
                agent="writer",
                task="Draft a structured internal answer.",
                reviewer="reviewer",
                token_budget=1000,
            ),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize the accepted draft.",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )


def _message(text: str) -> dict[str, Any]:
    return {
        "id": "msg_native",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _tool_call() -> dict[str, Any]:
    return {
        "id": "fc_native",
        "type": "function_call",
        "call_id": "call_repair_must_not_execute",
        "name": "stage_repair_actions",
        "arguments": '{"action":"mutate"}',
        "status": "completed",
    }


def _response(output: list[dict[str, Any]], total_tokens: int) -> dict[str, Any]:
    return {
        "id": f"resp_{total_tokens}",
        "object": "response",
        "created_at": 1,
        "model": "native-model",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": output,
        "usage": {
            "input_tokens": 1,
            "output_tokens": total_tokens - 1,
            "total_tokens": total_tokens,
        },
    }


def _deployment(identifier: str, logical_model: str) -> Deployment:
    return Deployment(
        id=identifier,
        logical_model=logical_model,
        provider_model="openai/native-model",
        request_model="native-model",
        api_base="https://native.example/v1",
        secret_ref=f"secret://{identifier}",
        quota_scope_id=f"scope-{identifier}",
        structured_output_api="responses",
        capabilities=frozenset(
            {
                ModelCapability.TEXT,
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.TOOL_CALLING,
            }
        ),
        input_per_million_usd=Decimal(0),
        output_per_million_usd=Decimal(0),
    )


class NativeStructuredRepairRun:
    """Mocks only SDK HTTP, capacity, and secrets; Crew/Gateway/LiteLLM stay real."""

    def __init__(self, root: Path, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.wire: list[dict[str, Any]] = []
        self.clients: list[AsyncOpenAI] = []
        self.capacity = CapacityStub(
            [
                lease("native-general"),
                lease("native-general"),
                lease("native-review"),
                lease("native-general"),
            ]
        )

        async def handle(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url) == "https://native.example/v1/responses"
            index = len(self.wire)
            assert index < len(self.responses), "unexpected extra native Responses call"
            self.wire.append(json.loads(request.content))
            return httpx.Response(200, json=self.responses[index])

        def factory(*, api_key: str, base_url: str, max_retries: int) -> AsyncOpenAI:
            assert api_key.startswith("key-for-secret://")
            assert base_url == "https://native.example/v1"
            assert max_retries == 0
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            )
            self.clients.append(client)
            return client

        deployments = [
            _deployment("native-general", "general"),
            _deployment("native-review", "review"),
            _deployment("backup-general", "backup_general"),
            _deployment("backup-review", "backup_review"),
        ]
        self.gateway = ModelGateway(
            registry=ModelRegistry(deployments),
            capacity_pool=self.capacity,
            secret_resolver=SecretStub(self.capacity.events),
            transport=LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
            fallbacks={"general": "backup_general", "review": "backup_review"},
        )
        self.runtime = CrewDispatchRuntime(
            self.gateway,
            _structured_reviewed_plan(),
            crew_factory=CrewAIObjectFactory(storage_dir=root / "crew"),
        )

    async def run(self, context: TaskContext, events: list[RunEvent]) -> None:
        try:
            async with asyncio.timeout(30):
                async for event in self.runtime.run(context):
                    events.append(event)
        finally:
            await self.runtime.cancel()
            for client in self.clients:
                await client.close()


def _assert_no_public_leak(events: list[RunEvent], secret: str) -> None:
    for event in events:
        payload = _public_event_payload(event.to_payload())
        assert secret not in json.dumps(payload, ensure_ascii=False, default=str)
        assert secret not in str(event.message)
        if event.artifact is not None:
            assert secret not in json.dumps(
                event.artifact.content,
                ensure_ascii=False,
                default=str,
            )


def _native_bodies(case: NativeStructuredRepairRun) -> list[dict[str, Any]]:
    assert len(case.wire) == len(case.capacity.releases)
    assert all(
        event[0] != "acquire" or all(not item.startswith("backup-") for item in event[1])
        for event in case.capacity.events
        if isinstance(event, tuple) and event
    )
    return case.wire


async def test_native_structured_repair_accounts_usage_and_continues_without_fallback(
    tmp_path: Path,
) -> None:
    case = NativeStructuredRepairRun(
        tmp_path,
        [
            _response([_message(PRIVATE_INVALID)], 129),
            _response([_message('{"summary":"model corrected candidate"}')], 17),
            _response([_message('{"verdict":"approve"}')], 8),
            _response([_message('{"answer":"final accepted answer"}')], 11),
        ],
    )
    context = await task(tmp_path)
    events: list[RunEvent] = []

    await case.run(context, events)

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    bodies = _native_bodies(case)
    assert len(bodies) == 4
    original, correction, review, final = bodies
    assert correction["model"] == original["model"] == "native-model"
    assert correction["text"] == original["text"]
    assert correction["input"] != original["input"]
    assert correction.get("tools", []) == []
    assert "tool_choice" not in correction
    assert review["text"]["format"]["name"] == "DispatchReviewVerdict"
    assert final["text"]["format"]["name"] == "DispatchRoleOutput"
    assert all(body["store"] is False and body["stream"] is False for body in bodies)
    assert not any(event.kind == "model.fallback" for event in events)
    assert not any(release.deployment_id.startswith("backup-") for release in case.capacity.releases)
    _assert_no_public_leak(events, PRIVATE_INVALID)

    checkpoint = await case.runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == 129 + 17 + 8 + 11
    rejected = checkpoint.state["rejected_outputs"]
    assert isinstance(rejected, Mapping)
    assert len(rejected) == 1
    assert PRIVATE_INVALID in str(rejected)
    repairs = checkpoint.state["structured_repairs"]
    assert isinstance(repairs, Mapping)
    assert set(repairs) == {"draft"}
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    assert (
        sum(
            isinstance(value, Mapping) and value.get("status") == "rejected"
            for value in models.values()
        )
        == 1
    )


@pytest.mark.parametrize(
    "repair_output",
    [
        [_message(PRIVATE_INVALID)],
        [_tool_call()],
    ],
    ids=["invalid-json", "tool-call"],
)
async def test_native_structured_repair_invalid_correction_stops_before_review_final_or_tool(
    tmp_path: Path,
    repair_output: list[dict[str, Any]],
) -> None:
    case = NativeStructuredRepairRun(
        tmp_path,
        [
            _response([_message(PRIVATE_INVALID)], 129),
            _response(repair_output, 17),
            _response([_message('{"verdict":"approve"}')], 8),
            _response([_message('{"answer":"should not run"}')], 11),
        ],
    )
    context = await task(tmp_path)
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        await case.run(context, events)

    assert PRIVATE_INVALID not in str(caught.value)
    bodies = _native_bodies(case)
    assert len(bodies) == 2
    assert bodies[1].get("tools", []) == []
    assert "tool_choice" not in bodies[1]
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    assert not any(str(event.kind).startswith("tool.") for event in events)
    assert not any("summary" in event.payload or "verdict" in event.payload for event in events)
    _assert_no_public_leak(events, PRIVATE_INVALID)

    checkpoint = await case.runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == 129 + 17
    assert checkpoint.state["tools"] == {}
    rejected = checkpoint.state["rejected_outputs"]
    assert isinstance(rejected, Mapping)
    assert len(rejected) == 2
