from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayRejectedOutput,
    GatewayResponseCancelled,
)
from agent_hub.models.types import (
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    StructuredResponseSchema,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RecordingFactory,
    RecordingGeneration,
    SequenceGateway,
    _context,
    _reviewed_step_plan,
    _structured_dependent_final_plan,
)
from tests.unit.runtime.crew.test_instruction_context import task


@pytest.mark.parametrize(
    "output",
    [
        '{"summary":"ok","summary":"changed","findings":[],"risks":[]}',
        '{"summary":"ok","findings":[],"risks":[],"extra":true}',
        '{"summary":"ok","findings":[],"risks":[],"nested":{"key":1,"key":2}}',
        '{"summary":"ok","findings":[],"risks":[],"n":NaN}',
        '{"summary":"ok","findings":[],"risks":[],"n":Infinity}',
        '{"summary":"ok","findings":[],"risks":[],"n":-Infinity}',
        '{"summary":"ok","findings":[],"risks":[],"n":1e999}',
    ],
)
def test_strict_worker_rejects_duplicate_nonfinite_and_extra_fields(output: str) -> None:
    plan = _structured_dependent_final_plan()
    with pytest.raises(RuntimeExecutionError):
        adapter._validate_structured_role_output(plan, plan.steps[0], plan.agents[0], output)


@pytest.mark.parametrize(
    "output", ['{"score":NaN}', '{"score":Infinity}', '{"score":-Infinity}', '{"score":1e999}']
)
def test_declared_number_field_cannot_hide_nonfinite_values(output: str) -> None:
    plan = _structured_dependent_final_plan()
    agent = plan.agents[0].model_copy(update={"output_schema": {"score": "number"}})
    with pytest.raises(RuntimeExecutionError):
        adapter._validate_structured_role_output(plan, plan.steps[0], agent, output)


def test_legitimate_whitespace_key_order_and_numbers_remain_valid() -> None:
    plan = _structured_dependent_final_plan()
    adapter._validate_structured_role_output(
        plan,
        plan.steps[0],
        plan.agents[0],
        '\n { "risks": [], "findings": ["ok"], "summary": "ok" } ',
    )
    agent = plan.agents[0].model_copy(update={"output_schema": {"score": "number", "ok": "bool"}})
    adapter._validate_structured_role_output(plan, plan.steps[0], agent, '{"score":1.25,"ok":true}')


def test_schema_property_names_are_not_interpreted_as_keywords() -> None:
    plan = _structured_dependent_final_plan()
    agent = plan.agents[0].model_copy(
        update={"output_schema": {"format": "string", "$ref": "string"}}
    )
    assert adapter._parse_structured_role_output(agent, '{"format":"json","$ref":"text"}') == {
        "format": "json",
        "$ref": "text",
    }


def test_nested_duplicate_is_rejected_in_schema_valid_object() -> None:
    schema = StructuredResponseSchema(
        name="Nested",
        schema={
            "type": "object",
            "required": ("nested",),
            "additionalProperties": False,
            "properties": {
                "nested": {"type": "object", "additionalProperties": {"type": "integer"}}
            },
        },
    )
    assert adapter._parse_structured_output(
        schema,
        '{"nested":{"key":1}}',
        prefix="test output",
        max_bytes=1024,
    ) == {"nested": {"key": 1}}
    with pytest.raises(RuntimeExecutionError, match="not valid json"):
        adapter._parse_structured_output(
            schema,
            '{"nested":{"key":1,"key":2}}',
            prefix="test output",
            max_bytes=1024,
        )


@pytest.mark.parametrize("array_depth", [19, 20, 65])
def test_output_depth_uses_existing_runtime_bound(array_depth: int) -> None:
    schema = StructuredResponseSchema(name="Object", schema={"type": "object"})
    text = '{"value":' + "[" * array_depth + "0" + "]" * array_depth + "}"
    if array_depth == 19:
        adapter._parse_structured_output(schema, text, prefix="test", max_bytes=65536)
    else:
        with pytest.raises(RuntimeExecutionError, match="not valid json"):
            adapter._parse_structured_output(schema, text, prefix="test", max_bytes=65536)


@pytest.mark.parametrize("total_nodes", [4096, 4097, 16385])
def test_output_node_count_uses_existing_runtime_bound(total_nodes: int) -> None:
    schema = StructuredResponseSchema(name="Object", schema={"type": "object"})
    # The root, property key and array itself contribute three nodes.
    text = json.dumps({"values": [0] * (total_nodes - 3)})
    if total_nodes == 4096:
        adapter._parse_structured_output(schema, text, prefix="test", max_bytes=65536)
    else:
        with pytest.raises(RuntimeExecutionError, match="not valid json"):
            adapter._parse_structured_output(schema, text, prefix="test", max_bytes=65536)


@pytest.mark.parametrize("array_depth", [19, 20, 65])
def test_schema_depth_uses_existing_runtime_bound(array_depth: int) -> None:
    payload: Any = 0
    for _ in range(array_depth):
        payload = [payload]
    schema = StructuredResponseSchema(name="Object", schema={"type": "object", "default": payload})
    if array_depth == 19:
        adapter._structured_validator(schema)
    else:
        with pytest.raises(RuntimeExecutionError, match="schema exceeds limits"):
            adapter._structured_validator(schema)


@pytest.mark.parametrize("total_nodes", [4096, 4097, 16385])
def test_schema_node_count_uses_existing_runtime_bound(total_nodes: int) -> None:
    # Root, two keys, type and default array contribute five nodes.
    schema = StructuredResponseSchema(
        name="Object",
        schema={
            "type": "object",
            "default": (0,) * (total_nodes - 5),
        },
    )
    if total_nodes == 4096:
        adapter._structured_validator(schema)
    else:
        with pytest.raises(RuntimeExecutionError, match="schema exceeds limits"):
            adapter._structured_validator(schema)


@pytest.mark.parametrize(
    "output",
    [
        '{"summary":"' + "x" * 65_536 + '","findings":[],"risks":[]}',
        '{"summary":"ok","findings":[],"risks":[],"deep":' + "[" * 40 + "0" + "]" * 40 + "}",
        '{"summary":"ok","findings":' + json.dumps(["x"] * 5000) + ',"risks":[]}',
    ],
    ids=["bytes", "depth", "nodes"],
)
def test_structured_output_resource_limits(output: str) -> None:
    plan = _structured_dependent_final_plan()
    with pytest.raises(RuntimeExecutionError):
        adapter._validate_structured_role_output(plan, plan.steps[0], plan.agents[0], output)


@pytest.mark.parametrize(
    "review",
    [
        '{"verdict":"reject","verdict":"approve"}',
        '{"verdict":"approve","extra":true}',
        '{"verdict":true}',
        "{}",
        "prose",
        '{"verdict":"approve","feedback":null}',
        '{"verdict":"approve","extra":{"key":1,"key":2}}',
    ],
)
async def test_invalid_reviewer_never_approves_or_unlocks_dependents(review: str) -> None:
    gateway = SequenceGateway('{"summary":"actual candidate"}', review)
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 3
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    assert any(event.artifact and event.artifact.type == "model_response" for event in events)
    failed_review = next(event for event in events if event.kind == "review.failed")
    assert failed_review.payload["review_status"] == "unverified"
    assert "verdict" not in failed_review.payload
    assert review not in str(failed_review.to_payload())


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "invalid"},
        {"$ref": "https://example.com/private-schema"},
        {"properties": {"format": {"$ref": "https://example.com/private-schema"}}},
    ],
)
async def test_bad_schema_configuration_fails_before_model_submission(
    monkeypatch: pytest.MonkeyPatch,
    schema: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        adapter,
        "_agent_response_schema",
        lambda _: StructuredResponseSchema(name="Bad", schema=schema),
    )
    gateway = SequenceGateway('{"summary":"ok"}')
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(_context())]
    assert gateway.requests == []


class RepairCaptureGateway:
    def __init__(self, *outputs: tuple[str | None, int | None, bool]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert len(self.requests) <= len(self.outputs), "unexpected extra model call"
        text, total, rejected = self.outputs[len(self.requests) - 1]
        usage = None if total is None else TokenUsage(1, total - 1, total)
        if rejected:
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=text,
                    usage=usage,
                    usage_status="missing" if usage is None else "known",
                    status="completed",
                    reason="invalid_json",
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="provider",
                provider_model="provider/model",
                cost_usd=Decimal(0),
            )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=usage),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="provider",
            provider_model="provider/model",
            cost_usd=Decimal(0),
        )


def assert_private_rejection(events: list[RunEvent], secret: str) -> None:
    for event in events:
        assert secret not in str(event.payload)
        assert secret not in str(event.message)
        if event.artifact:
            assert secret not in str(event.artifact.content)
    # Internal checkpoint state is private; public projection is covered separately.


@pytest.mark.parametrize("native_rejection", [False, True])
async def test_shared_correction_valid_result_accounts_and_replays(native_rejection: bool) -> None:
    secret = "PRIVATE_REJECTED_CANDIDATE_WORKER"
    accepted = '{"summary":"model corrected candidate"}'
    gateway = RepairCaptureGateway(
        (secret, 129, native_rejection),
        (accepted, 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("final accepted answer", 11, False),
    )
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    initial, correction = gateway.requests[:2]
    assert correction.logical_model == initial.logical_model
    assert correction.response_schema == initial.response_schema
    assert correction.tools == ()
    assert (
        sum(
            "INTERNAL_RESPONSE_SCHEMA_JSON=" in str(message.content)
            for message in correction.messages
        )
        == 1
    )
    assert any(
        event.artifact and event.artifact.content.get("text") == accepted for event in events
    )
    assert_private_rejection(events, secret)
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 165
    rejected = checkpoint.state["rejected_outputs"]
    assert isinstance(rejected, Mapping) and len(rejected) == 1
    assert secret in str(rejected)
    reservations = checkpoint.state["structured_repairs"]
    assert isinstance(reservations, Mapping) and set(reservations) == {"draft"}
    ledger = checkpoint.state["models"]
    assert isinstance(ledger, Mapping)
    assert (
        sum(
            isinstance(value, Mapping) and value["status"] == "rejected"
            for value in ledger.values()
        )
        == 1
    )
    replay_gateway = RepairCaptureGateway()
    replay = CrewDispatchRuntime(
        replay_gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events = [event async for event in replay.run(_context(checkpoint=checkpoint))]
    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert replay_gateway.requests == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert checkpoint.state["usage"] == usage


async def test_shared_correction_near_budget_response_checkpoint_replays_original_limit() -> None:
    plan = _reviewed_step_plan()
    plan = plan.model_copy(
        update={
            "steps": (
                plan.steps[0].model_copy(update={"token_budget": 160}),
                plan.steps[1],
            )
        }
    )
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        ("PRIVATE_NEAR_BUDGET", 129, True),
        ('{"summary":"corrected"}', 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory()
    )
    events = [event async for event in runtime.run(_context())]
    checkpoint = next(
        event.checkpoint
        for event in events
        if event.checkpoint is not None
        and isinstance(event.checkpoint.state["usage"], Mapping)
        and event.checkpoint.state["usage"]["tokens"] == 146
    )
    assert gateway.requests[1].max_output_tokens == 31
    assert checkpoint.state["completed"] == ()
    replay_gateway = RepairCaptureGateway(('{"verdict":"approve"}', 8, False), ("final", 11, False))
    replay = CrewDispatchRuntime(
        replay_gateway, plan, artifact_repository=repository, crew_factory=FastFactory()
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events = [event async for event in replay.run(_context(checkpoint=checkpoint))]
    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in replay_gateway.requests] == ["review", "general"]
    usage = (await replay.save_checkpoint()).state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 165


@pytest.mark.parametrize(
    "phase,changed", [("reserved", False), ("running", False), ("reserved", True)]
)
async def test_shared_correction_prepared_and_uncertain_replay(phase: str, changed: bool) -> None:
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        ("PRIVATE_REPLAY", 129, True),
        ('{"summary":"corrected"}', 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory()
    )
    events = [event async for event in runtime.run(_context())]
    checkpoints = [event.checkpoint for event in events if event.checkpoint is not None]
    checkpoint = next(
        item
        for item in checkpoints
        if isinstance(item.state["structured_repairs"], Mapping)
        and isinstance((repair_state := item.state["structured_repairs"].get("draft")), Mapping)
        and repair_state["status"] == phase
    )
    replay_gateway = RepairCaptureGateway(
        ('{"summary":"corrected"}', 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("final", 11, False),
    )
    replay = CrewDispatchRuntime(
        replay_gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    context = _context(
        checkpoint=checkpoint, request="Changed task" if changed else "Write a short answer"
    )
    if changed or phase == "running":
        with pytest.raises(RuntimeExecutionError):
            _ = [event async for event in replay.run(context)]
        assert replay_gateway.requests == []
    else:
        replay_events = [event async for event in replay.run(context)]
        assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(replay_gateway.requests) == 3
        usage = (await replay.save_checkpoint()).state["usage"]
        assert isinstance(usage, Mapping) and usage["tokens"] == 165


async def test_correction_transport_failure_never_restarts_worker() -> None:
    from agent_hub.models.litellm_client import ModelTransportError

    class FailedCorrection(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if self.requests:
                self.requests.append(request)
                raise ModelTransportError("model transport timed out")
            return await super().complete_with_context(request)

    gateway = FailedCorrection(("PRIVATE_TRANSPORT", 129, True))
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(reviewer_retries=1), crew_factory=FastFactory()
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    checkpoint = await runtime.save_checkpoint()
    repair = checkpoint.state["structured_repairs"]
    assert isinstance(repair, Mapping) and isinstance(repair["draft"], Mapping)
    assert repair["draft"]["status"] == "uncertain"


@pytest.mark.parametrize("native_rejection", [False, True])
async def test_shared_correction_invalid_result_stops_at_one(native_rejection: bool) -> None:
    secret = "PRIVATE_REJECTED_CANDIDATE_INVALID"
    gateway = RepairCaptureGateway((secret, 129, native_rejection), (secret, 17, native_rejection))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    assert_private_rejection(events, secret)
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 146
    rejected = checkpoint.state["rejected_outputs"]
    assert isinstance(rejected, Mapping) and len(rejected) == 2


async def test_shared_correction_worker_slot_blocks_reviewer_second_repair() -> None:
    gateway = RepairCaptureGateway(
        ("PRIVATE_WORKER_FAILURE", 129, True),
        ('{"summary":"real corrected candidate"}', 17, False),
        ("PRIVATE_REVIEW_FAILURE", 8, True),
    )
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 3
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    assert_private_rejection(events, "PRIVATE_WORKER_FAILURE")
    assert_private_rejection(events, "PRIVATE_REVIEW_FAILURE")
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 154
    reservations = checkpoint.state["structured_repairs"]
    assert isinstance(reservations, Mapping) and len(reservations) == 1


async def test_shared_correction_reviewer_uses_its_own_schema_and_actor() -> None:
    gateway = RepairCaptureGateway(
        ('{"summary":"actual candidate"}', 129, False),
        ("PRIVATE_REVIEW_INVALID", 8, True),
        ('{"verdict":"approve"}', 17, False),
        ("final approved", 11, False),
    )
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(_context())]
    assert len(gateway.requests) == 4
    review, correction = gateway.requests[1:3]
    assert correction.logical_model == review.logical_model == "review"
    assert correction.response_schema == review.response_schema == adapter._REVIEW_RESPONSE_SCHEMA
    assert correction.tools == ()
    assert [
        event.payload["verdict"] for event in events if event.kind is EventKind.REVIEW_COMPLETED
    ] == ["approve"]
    assert_private_rejection(events, "PRIVATE_REVIEW_INVALID")
    usage = (await runtime.save_checkpoint()).state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 165


async def test_shared_correction_unknown_usage_is_retained_without_paid_retry() -> None:
    secret = "PRIVATE_UNACCOUNTED_INVALID"
    gateway = RepairCaptureGateway((secret, None, True))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 1
    assert_private_rejection(events, secret)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] == "unaccounted"
    rejected = checkpoint.state["rejected_outputs"]
    assert isinstance(rejected, Mapping) and len(rejected) == 1
    assert checkpoint.state["structured_repairs"] == {}


async def test_shared_correction_chat_tool_receipt_is_accounted_without_execution() -> None:
    class ToolCorrection(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if not self.requests:
                return await super().complete_with_context(request)
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    usage=TokenUsage(1, 16, 17),
                    tool_calls=(
                        ToolCall(id="forbidden", name="read_context", arguments={"query": "x"}),
                    ),
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="provider",
                provider_model="provider/model",
                cost_usd=Decimal(0),
            )

    gateway = ToolCorrection(("PRIVATE_REJECTED_TOOL", 129, True))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    usage = (await runtime.save_checkpoint()).state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 146
    assert not any(str(event.kind).startswith("tool.") for event in events)


@pytest.mark.parametrize("reject", [False, True])
async def test_shared_correction_cancelled_receipt_accounts_without_repair(reject: bool) -> None:
    class CancelledGateway(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                receipt = await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                raise GatewayResponseCancelled(receipt=error) from None
            raise GatewayResponseCancelled(receipt=receipt)

    gateway = CancelledGateway(("PRIVATE_CANCELLED_RECEIPT", 129, reject))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(asyncio.CancelledError):
        async for event in runtime.run(_context()):
            events.append(event)
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 129
    assert len(gateway.requests) == 1
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert_private_rejection(events, "PRIVATE_CANCELLED_RECEIPT")


@pytest.mark.parametrize(
    "field,value",
    [
        ("fallback_used", "true"),
        ("fallback_reason", "reason\nsecret"),
        ("attempted_logical_models", (12,)),
        ("attempted_logical_models", ("unsafe\nmodel",)),
        ("fallback_from_logical_model", "general"),
    ],
)
def test_rejected_private_fallback_metadata_is_validated(field: str, value: Any) -> None:
    rejection = GatewayRejectedOutput(
        evidence=RejectedOutputEvidence(
            final_text="bad",
            usage=TokenUsage(1, 1, 2),
            usage_status="known",
            status="completed",
            reason="invalid_json",
        ),
        deployment_id="primary",
        logical_model="general",
        provider_id="provider",
        provider_model="provider/model",
        cost_usd=Decimal(0),
    )
    payload = dict(CrewDispatchRuntime._rejected_private_payload(rejection, ()))
    payload[field] = value
    with pytest.raises(RuntimeExecutionError):
        CrewDispatchRuntime._rejected_from_private(payload)


@pytest.mark.parametrize(
    "target,raw,equivalent",
    [
        ("writer", '{"summary":"framework altered facts"}', False),
        ("reviewer", '{"verdict":"approve"}', False),
        ("writer", ' { "summary" : "provider facts" } ', True),
    ],
)
async def test_framework_raw_cannot_replace_validated_provider_output(
    target: str,
    raw: str,
    equivalent: bool,
) -> None:
    class RewriteGeneration(RecordingGeneration):
        async def execute(self, *args: Any, **kwargs: Any) -> str:
            actual = await super().execute(*args, **kwargs)
            return raw if kwargs.get("agent_id") == target else actual

    gateway = RepairCaptureGateway(
        ('{"summary":"provider facts"}', 129, False),
        ('{"verdict":"reject"}' if target == "reviewer" else '{"verdict":"approve"}', 8, False),
        ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), crew_factory=RecordingFactory(RewriteGeneration())
    )
    events: list[RunEvent] = []
    if equivalent:
        events = [event async for event in runtime.run(_context())]
        candidate = next(
            event.artifact
            for event in events
            if event.artifact is not None
            and event.artifact.type == "text"
            and event.artifact.producer == "writer"
        )
        assert candidate.content["text"] == '{"summary":"provider facts"}'
    else:
        with pytest.raises(RuntimeExecutionError, match="framework output mismatch"):
            async for event in runtime.run(_context()):
                events.append(event)
        assert len(gateway.requests) == (1 if target == "writer" else 2)
        assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
        assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)


@pytest.mark.parametrize("rejected", [False, True])
async def test_guidance_emit_cancellation_keeps_completed_gateway_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejected: bool,
) -> None:
    context = await task(tmp_path)
    assert context.instruction_context is not None and context.instruction_context.render()
    started = asyncio.Event()
    original = adapter._Sequence.event

    async def paused_event(sequence: Any, **values: Any) -> RunEvent:
        if values.get("kind") == "context.injected":
            started.set()
            await asyncio.Event().wait()
        return await original(sequence, **values)

    monkeypatch.setattr(adapter._Sequence, "event", paused_event)
    gateway = RepairCaptureGateway(("PRIVATE_GUIDED_RECEIPT", 129, rejected))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []

    async def consume() -> None:
        async for event in runtime.run(context):
            events.append(event)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(runtime.cancel(), timeout=3)
        with pytest.raises(asyncio.CancelledError):
            await consumer
        checkpoint = await runtime.save_checkpoint()
        usage = checkpoint.state["usage"]
        assert isinstance(usage, Mapping) and usage["tokens"] == 129
        assert len(gateway.requests) == 1
        assert checkpoint.state["structured_repairs"] == {}
        assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
        assert_private_rejection(events, "PRIVATE_GUIDED_RECEIPT")
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.parametrize("rejected", [False, True])
async def test_cancelled_correction_receipt_is_accounted_without_acceptance(rejected: bool) -> None:
    class CancelCorrection(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                completion = await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                if len(self.requests) == 2:
                    raise GatewayResponseCancelled(receipt=error) from None
                raise
            raise GatewayResponseCancelled(receipt=completion)

    gateway = CancelCorrection(
        ("PRIVATE_ORIGINAL", 129, True),
        ('{"summary":"received but cancelled"}', 17, rejected),
    )
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(asyncio.CancelledError):
        async for event in runtime.run(_context()):
            events.append(event)
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 146
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)


async def test_correction_cannot_spend_when_known_positive_cost_exhausts_budget() -> None:
    class PricedGateway(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                error.cost_usd = Decimal("0.1")
                raise

    plan = _reviewed_step_plan()
    plan = plan.model_copy(
        update={
            "total_cost_usd": Decimal("0.2"),
            "steps": (
                plan.steps[0].model_copy(update={"cost_budget_usd": Decimal("0.1")}),
                plan.steps[1],
            ),
        }
    )
    gateway = PricedGateway(("PRIVATE_PRICED", 129, True))
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    with pytest.raises(RuntimeExecutionError, match="budget exhausted"):
        _ = [event async for event in runtime.run(_context())]
    assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    "actual,raw", [('{"value":true}', '{"value":1}'), ('{"value":1}', '{"value":1.0}')]
)
def test_framework_comparison_preserves_json_scalar_types(actual: str, raw: str) -> None:
    schema = StructuredResponseSchema(name="Scalar", schema={"type": "object"})
    with pytest.raises(RuntimeExecutionError, match="framework output mismatch"):
        adapter._check_framework_raw(schema, actual, raw)


@pytest.mark.parametrize(
    "invalid", ["\ud800", "x" * 70_000, None], ids=["invalid-utf8", "oversized", "absent"]
)
async def test_unretainable_response_still_accounts_without_paid_correction(invalid: str | None) -> None:
    gateway = RepairCaptureGateway((invalid, 129, False))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(_context())]
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 129
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("actor", ["worker", "reviewer"])
async def test_empty_structured_response_uses_shared_correction_and_accounts(actor: str) -> None:
    worker = '{"summary":"real worker result"}'
    review = '{"verdict":"approve"}'
    outputs = (
        (("", 2, False), (worker, 17, False), (review, 8, False))
        if actor == "worker"
        else ((worker, 8, False), ("", 2, False), (review, 17, False))
    )
    gateway = RepairCaptureGateway(*outputs, ("final answer", 11, False))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 38
    repairs = checkpoint.state["structured_repairs"]
    assert isinstance(repairs, Mapping) and set(repairs) == {"draft"}
    repair = repairs["draft"]
    assert isinstance(repair, Mapping) and repair["status"] == "succeeded"
    source, correction = gateway.requests[(0 if actor == "worker" else 1) :][:2]
    assert correction.logical_model == source.logical_model
    assert correction.response_schema == source.response_schema
    assert correction.allow_fallback is False
    assert not any(event.payload.get("error_code") == "model.empty_response" for event in events)
