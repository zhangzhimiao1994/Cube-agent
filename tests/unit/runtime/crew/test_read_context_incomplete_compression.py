"""Offline compression parity for an accounted, single read_context recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, StructuredResponseSchema
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.runtime.crew.test_adapter_failure_reason import RecordingHarnessToolGateway
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect, plan
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
)


def _dispatch(with_tool: bool) -> DispatchPlan:
    base = plan()
    tools = ("read_context",) if with_tool else ()
    return base.model_copy(update={
        "allowed_tools": tools,
        "agents": (base.agents[0].model_copy(update={"allowed_tools": tools}),
                   *base.agents[1:]),
        "steps": (base.steps[0].model_copy(update={"tools": tools}), *base.steps[1:]),
    })


def _instruction(request: ModelRequest) -> str:
    instructions: list[str] = []
    for message in request.messages:
        content = str(message.content)
        if content.lstrip().startswith("{"):
            payload = json.loads(content)
            if "recovery" in payload:
                instructions.append(payload["recovery"]["instruction"])
    assert len(instructions) == 1
    return instructions[0]


def _assert_compression(request: ModelRequest) -> None:
    instruction = _instruction(request)
    for required in (
        "complete compact JSON object satisfying the unchanged schema",
        "Do not continue or reconstruct the discarded incomplete response",
        "Omit optional elaboration",
        "approximately 512 characters",
        "approximately four items",
        "only when compatible with the unchanged schema and required facts",
        "Schema minima and mandatory entries take priority",
        "Never mechanically truncate",
        "Preserve required fields and facts",
        "without inventing evidence",
    ):
        assert required in instruction


@pytest.mark.parametrize("with_tool", [False, True], ids=["no_tools", "read_context"])
async def test_compression_parity_keeps_model_tools_schema_and_cap(with_tool: bool) -> None:
    gateway = ToolIncompleteGateway("incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, _dispatch(with_tool), InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    original, recovered, _ = gateway.requests
    _assert_compression(recovered)
    assert recovered.logical_model == "selected"
    assert recovered.response_schema == original.response_schema
    assert recovered.response_schema is not None
    assert recovered.tools == original.tools
    assert tuple(tool.name for tool in recovered.tools) == (("read_context",) if with_tool else ())
    assert recovered.max_output_tokens == original.max_output_tokens == 6144
    assert not recovered.allow_fallback
    if with_tool:
        assert "authorized tool if needed" in _instruction(recovered)
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.calls == []
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["structured_repairs"] == {}
    assert mapping(checkpoint.state["usage"])["tokens"] == 8406


class _MinimumGateway(ToolIncompleteGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        outcome = self.outcomes[len(self.requests)]
        completion = await super().complete_with_context(request)
        if outcome == "success":
            completion = replace(completion, response=replace(completion.response, text=json.dumps({
                "summary": "x" * 600, "items": ["required"] * 5,
            })))
        return completion


@pytest.mark.parametrize("with_tool", [False, True], ids=["no_tools", "read_context"])
async def test_schema_minima_override_compression_preferences(
    with_tool: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch(with_tool)
    dispatch = dispatch.model_copy(update={
        "agents": (dispatch.agents[0].model_copy(update={
            "output_schema": {"summary": "string", "items": "string[]"},
        }), *dispatch.agents[1:]),
    })
    original_schema = adapter._agent_response_schema

    def minimum_schema(agent: AgentSpec) -> StructuredResponseSchema | None:
        schema = original_schema(agent)
        if schema is None or agent.id != dispatch.agents[0].id:
            return schema
        properties = dict(mapping(schema.schema["properties"]))
        properties["summary"] = {**mapping(properties["summary"]), "minLength": 600}
        properties["items"] = {**mapping(properties["items"]), "minItems": 5}
        return StructuredResponseSchema(
            name=schema.name, schema={**schema.schema, "properties": properties},
        )

    monkeypatch.setattr(adapter, "_agent_response_schema", minimum_schema)
    gateway = _MinimumGateway("incomplete", "success", "final")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, dispatch, InMemoryArtifactRepository(), harness)
    events = await collect(runtime)
    original, recovered, _ = gateway.requests
    _assert_compression(recovered)
    assert recovered.response_schema == original.response_schema
    assert recovered.response_schema is not None
    properties = mapping(recovered.response_schema.schema["properties"])
    assert mapping(properties["summary"])["minLength"] == 600
    assert mapping(properties["items"])["minItems"] == 5
    assert recovered.max_output_tokens == original.max_output_tokens == 6144
    assert not recovered.allow_fallback and recovered.tools == original.tools
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.calls == []


async def test_second_incomplete_is_terminal_without_tools_or_third_call() -> None:
    gateway = ToolIncompleteGateway("incomplete", "incomplete")
    harness = RecordingHarnessToolGateway()
    runtime = subject(gateway, _dispatch(True), InMemoryArtifactRepository(), harness)
    events = await collect(runtime, fail="structured output invalid")
    assert len(gateway.requests) == 2
    _assert_compression(gateway.requests[1])
    assert sum(event.kind is EventKind.STEP_RETRYING for event in events) == 1
    checkpoint = await runtime.save_checkpoint()
    assert [(row["attempt"], row["call_index"], row["status"])
            for row in draft_models(checkpoint)] == [(0, 0, "rejected"), (1, 0, "rejected")]
    assert checkpoint.state["structured_repairs"] == {} and checkpoint.state["tools"] == {}
    assert mapping(checkpoint.state["usage"])["tokens"] == 16756
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.02"
    assert harness.calls == []


@pytest.mark.parametrize("phase", ["source", "prepared", "running", "succeeded", "rejected"])
async def test_natural_checkpoint_recovery_keeps_compression_and_never_replays_tools(
    phase: str,
) -> None:
    dispatch = _dispatch(True)
    repository = InMemoryArtifactRepository()
    source_gateway = ToolIncompleteGateway(
        "incomplete", "incomplete" if phase == "rejected" else "success", "final",
    )
    source_harness = RecordingHarnessToolGateway()
    source = subject(source_gateway, dispatch, repository, source_harness)
    events = await collect(source, fail="structured output invalid" if phase == "rejected" else None)

    def matches(checkpoint: RuntimeCheckpoint) -> bool:
        rows = draft_models(checkpoint)
        if checkpoint.state["phase"] != "running" or not rows:
            return False
        if phase == "source":
            return len(rows) == 1 and rows[0]["status"] == "rejected"
        return len(rows) == 2 and rows[-1]["status"] == phase

    checkpoint = next(event.checkpoint for event in events
                      if event.checkpoint is not None and matches(event.checkpoint))
    before = checkpoint.to_payload()
    outcomes = ("success", "final") if phase in {"source", "prepared"} else (
        ("final",) if phase == "succeeded" else ()
    )
    gateway = ToolIncompleteGateway(*outcomes)
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    await collect(restored, checkpoint=checkpoint, fail=(
        "outcome requires confirmation" if phase == "running" else (
            "structured output invalid" if phase == "rejected" else None
        )
    ))
    assert len(gateway.requests) == len(outcomes)
    assert harness.calls == source_harness.calls == []
    assert checkpoint.to_payload() == before
    saved = await restored.save_checkpoint()
    if phase == "running":
        assert saved.to_payload() == before
    elif phase != "rejected":
        assert mapping(saved.state["usage"])["tokens"] == 8406
        assert Decimal(str(mapping(saved.state["usage"])["cost_usd"])) == Decimal("0.03")
        assert saved.state["structured_repairs"] == {} and saved.state["tools"] == {}
    if phase in {"source", "prepared"}:
        recovered = gateway.requests[0]
        original_recovered = source_gateway.requests[1]
        _assert_compression(recovered)
        timeout_cap = min(
            dispatch.steps[0].timeout_seconds,
            float(str(checkpoint.state["remaining_timeout_seconds"])),
        )
        assert 0 < recovered.timeout_seconds <= timeout_cap
        assert replace(
            recovered, timeout_seconds=original_recovered.timeout_seconds,
        ) == original_recovered
    for row in draft_models(checkpoint):
        if row["status"] in {"succeeded", "rejected"}:
            assert row in draft_models(saved)


async def test_natural_legacy_instruction_prepared_checkpoint_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_instruction = (
        "Regenerate the first response using the unchanged role, schema and authorized tools. "
        "Do not continue or reconstruct the discarded incomplete response. "
        "Use only the original task and available source evidence. "
        "Use an authorized tool if needed, otherwise return complete compact JSON. "
        "Preserve mandatory fields and facts; never mechanically truncate or invent evidence."
    )
    execute = adapter.CrewDispatchRuntime._execute_model_request

    async def legacy_execute(
        self: adapter.CrewDispatchRuntime, context: TaskContext, step: DispatchStep,
        actor: str, request: ModelRequest, **kwargs: Any,
    ) -> tuple[GatewayCompletion, Artifact]:
        if step.tools and kwargs["purpose"] == "step" and kwargs["attempt"] == 1:
            messages = list(request.messages)
            replaced = 0
            for index, message in enumerate(messages):
                content = str(message.content)
                if content.lstrip().startswith("{"):
                    payload = json.loads(content)
                    if "recovery" in payload:
                        payload["recovery"]["instruction"] = legacy_instruction
                        messages[index] = replace(message, content=json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                        ))
                        replaced += 1
            assert replaced == 1
            request = replace(request, messages=tuple(messages))
        return await execute(self, context, step, actor, request, **kwargs)

    dispatch = _dispatch(True)
    repository = InMemoryArtifactRepository()
    source_gateway = ToolIncompleteGateway("incomplete", "success", "final")
    source_harness = RecordingHarnessToolGateway()
    source = subject(source_gateway, dispatch, repository, source_harness)
    with monkeypatch.context() as legacy:
        # Generate the old request before the real ledger binds its SHA; do not edit the CP.
        legacy.setattr(adapter.CrewDispatchRuntime, "_execute_model_request", legacy_execute)
        events = await collect(source)
    assert _instruction(source_gateway.requests[1]) == legacy_instruction
    checkpoint = next(
        event.checkpoint for event in events
        if event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
        and len(draft_models(event.checkpoint)) == 2
        and draft_models(event.checkpoint)[-1]["status"] == "prepared"
    )
    before = checkpoint.to_payload()
    assert draft_models(checkpoint)[1]["request_sha256"] == source._model_request_sha256(
        source_gateway.requests[1],
    )
    assert mapping(checkpoint.state["usage"])["tokens"] == 8378
    assert mapping(checkpoint.state["usage"])["cost_usd"] == "0.01"
    gateway = ToolIncompleteGateway("success", "final")
    harness = RecordingHarnessToolGateway()
    restored = subject(gateway, dispatch, repository, harness)
    await restored.restore_checkpoint(checkpoint)
    await collect(restored, checkpoint=checkpoint, fail="model request changed after checkpoint")
    assert gateway.requests == [] and harness.calls == source_harness.calls == []
    assert checkpoint.to_payload() == before
    assert (await restored.save_checkpoint()).to_payload() == before
