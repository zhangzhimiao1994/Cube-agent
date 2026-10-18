from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, StructuredResponseSchema
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    RecordingFactory,
    RecordingGeneration,
    _context,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import RepairCaptureGateway


class PricedCaptureGateway(RepairCaptureGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        return replace(await super().complete_with_context(request), cost_usd=Decimal("0.001"))


def fenced(text: str, newline: str = "\n") -> str:
    return f"```json{newline}{text}{newline}```"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("reviewer", [False, True])
def test_whole_json_fence_uses_worker_and_reviewer_schema(newline: str, reviewer: bool) -> None:
    text = '{"verdict":"approve"}' if reviewer else '{"summary":"actual result"}'
    schema = (
        adapter._REVIEW_RESPONSE_SCHEMA
        if reviewer else adapter._agent_response_schema(_reviewed_step_plan().agents[0])
    )
    assert schema is not None
    assert adapter._parse_structured_output(
        schema, " \t\r\n" + fenced(text, newline) + "\r\n\t ", prefix="test", max_bytes=1024,
    ) == ({"verdict": "approve"} if reviewer else {"summary": "actual result"})


@pytest.mark.parametrize("text", [
    'before\n```json\n{}\n```', '```json\n{}\n```\nafter',
    '```json\n{}\n```\n```json\n{}\n```',
    '```\n{}\n```', '```JSON\n{}\n```', '```javascript\n{}\n```',
    '```json \n{}\n```', ' ```json\n{}\n``` extra',
    '```json\n{}', '{}\n```', '```json {}\n```', '```json\r{}\r```',
    '```json\n{}```', '````json\n{}\n````',
    '\u00a0```json\n{}\n```', '```json\n{}\n```\u00a0',
    '\ufeff```json\n{}\n```', '```json\n{} {}\n```',
])
def test_fence_exception_does_not_extract_from_other_wrappers(text: str) -> None:
    schema = StructuredResponseSchema(name="Object", schema={"type": "object"})
    with pytest.raises(RuntimeExecutionError, match="not valid json"):
        adapter._parse_structured_output(schema, text, prefix="test", max_bytes=1024)


@pytest.mark.parametrize("payload,reason", [
    ('{"value":1,"value":2}', "not valid json"),
    ('{"value":{"key":1,"key":2}}', "not valid json"),
    ('{"value":NaN}', "not valid json"),
    ('{"value":Infinity}', "not valid json"),
    ('{"value":-Infinity}', "not valid json"),
    ('{"value":1e999}', "not valid json"),
    ('{}', "missing field"),
    ('{"value":"1"}', "field type mismatch"),
    ('{"value":true}', "field type mismatch"),
    ('{"value":1,"extra":0}', "unexpected fields"),
    ('[]', "not an object"),
])
def test_fenced_payload_still_requires_strict_json_and_schema(payload: str, reason: str) -> None:
    schema = StructuredResponseSchema(name="Number", schema={
        "type": "object", "required": ("value",), "additionalProperties": False,
        "properties": {"value": {"type": "number"}},
    })
    with pytest.raises(RuntimeExecutionError, match=reason):
        adapter._parse_structured_output(schema, fenced(payload), prefix="test", max_bytes=1024)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_fence_byte_limit_counts_original_utf8_and_outer_whitespace(newline: str) -> None:
    schema = StructuredResponseSchema(name="Object", schema={"type": "object"})
    text = " \t" + fenced('{"value":"\u4e2d"}', newline) + "\r\n "
    size = len(text.encode("utf-8"))
    assert adapter._parse_structured_output(
        schema, text, prefix="test", max_bytes=size,
    ) == {"value": "\u4e2d"}
    for invalid in (text, "```json\ninvalid\n```" + " " * size):
        with pytest.raises(RuntimeExecutionError, match="exceeds output limit"):
            adapter._parse_structured_output(schema, invalid, prefix="test", max_bytes=size - 1)


@pytest.mark.parametrize("payload", [
    '{"value":' + "[" * 20 + "0" + "]" * 20 + "}",
    '{"value":[' + ",".join(["0"] * 4094) + "]}",
])
def test_fenced_payload_keeps_depth_and_node_limits(payload: str) -> None:
    schema = StructuredResponseSchema(name="Object", schema={"type": "object"})
    with pytest.raises(RuntimeExecutionError, match="not valid json"):
        adapter._parse_structured_output(schema, fenced(payload), prefix="test", max_bytes=65536)


@pytest.mark.parametrize("actual,raw,equivalent", [
    (fenced('{"value":1}'), '{ "value": 1 }', True),
    ('{"value":1}', fenced('{ "value": 1 }', "\r\n"), True),
    (fenced('{"value":true}'), '{"value":1}', False),
    (fenced('{"value":1}'), fenced('{"value":1.0}'), False),
    (fenced('{"value":1}'), fenced('{"value":2}'), False),
    (fenced('{"value":1}'), "prose " + fenced('{"value":1}'), False),
])
def test_framework_raw_uses_same_fence_and_type_rules(
    actual: str, raw: str, equivalent: bool,
) -> None:
    schema = StructuredResponseSchema(name="Object", schema={"type": "object"})
    if equivalent:
        adapter._check_framework_raw(schema, actual, raw)
    else:
        with pytest.raises(RuntimeExecutionError, match="framework output mismatch"):
            adapter._check_framework_raw(schema, actual, raw)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_initial_fenced_worker_and_reviewer_keep_raw_usage_provenance_and_replay(
    newline: str,
) -> None:
    worker = " \t" + fenced('{"summary":"provider facts"}', newline) + newline
    review = fenced('{"verdict":"approve"}', newline)
    gateway = RepairCaptureGateway((worker, 129, False), (review, 8, False), ("final", 11, False))
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    for text in (worker, review):
        artifact = next(event.artifact for event in events if event.artifact is not None
                        and event.artifact.type == "model_response"
                        and event.artifact.content.get("text") == text)
        assert artifact.provenance is not None
        assert artifact.provenance.deployment_id == "primary"
        assert artifact.provenance.provider_id == "provider"
        assert artifact.provenance.provider_model == "provider/model"
        assert artifact.content_sha256 == artifact.recompute_content_sha256()
    candidate = next(event.artifact for event in events if event.artifact is not None
                     and event.artifact.type == "text" and event.artifact.producer == "writer")
    assert candidate.content["text"] == worker
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["structured_repairs"] == {}
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 148
    replay_gateway = RepairCaptureGateway()
    replay = CrewDispatchRuntime(
        replay_gateway, _reviewed_step_plan(), artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events = [event async for event in replay.run(_context(checkpoint=checkpoint))]
    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert replay_gateway.requests == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert (await replay.save_checkpoint()).state["usage"] == usage


@pytest.mark.parametrize("actor", ["worker", "reviewer"])
@pytest.mark.parametrize("native_rejection", [False, True])
async def test_fenced_correction_keeps_shared_slot_schema_and_usage(
    actor: str, native_rejection: bool,
) -> None:
    worker, review = '{"summary":"provider facts"}', '{"verdict":"approve"}'
    outputs = (
        (("invalid", 129, native_rejection), (fenced(worker), 17, False), (review, 8, False))
        if actor == "worker" else
        ((worker, 129, False), ("invalid", 8, native_rejection), (fenced(review), 17, False))
    )
    gateway = RepairCaptureGateway(*outputs, ("final", 11, False))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    original, correction = gateway.requests[(0 if actor == "worker" else 1):][:2]
    assert correction.response_schema == original.response_schema
    assert correction.logical_model == original.logical_model
    assert correction.tools == () and correction.allow_fallback is False
    checkpoint = await runtime.save_checkpoint()
    repairs = checkpoint.state["structured_repairs"]
    assert isinstance(repairs, Mapping) and set(repairs) == {"draft"}
    repair = repairs["draft"]
    assert isinstance(repair, Mapping) and repair["status"] == "succeeded"
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 165


async def test_native_rejected_fence_still_requires_the_shared_correction() -> None:
    original = fenced('{"summary":"native rejected candidate"}')
    gateway = RepairCaptureGateway(
        (original, 129, True), ('{"summary":"actual correction"}', 17, False),
        ('{"verdict":"approve"}', 8, False), ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    checkpoint = await runtime.save_checkpoint()
    rejected = checkpoint.state["rejected_outputs"]
    assert isinstance(rejected, Mapping) and len(rejected) == 1
    receipt = next(iter(rejected.values()))
    assert isinstance(receipt, Mapping) and receipt["final_text"] == original
    assert receipt["reason"] == "invalid_json" and receipt["usage_status"] == "known"
    assert checkpoint.state["structured_repairs"]
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 165


async def test_fenced_worker_correction_cannot_grant_reviewer_another_slot() -> None:
    gateway = RepairCaptureGateway(
        ("invalid worker", 129, False), (fenced('{"summary":"actual"}'), 17, False),
        (fenced('{"verdict":"approve","verdict":"approve"}'), 8, False),
    )
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 3
    assert not any(event.kind in {EventKind.REVIEW_COMPLETED, EventKind.STEP_COMPLETED}
                   for event in events)
    usage = (await runtime.save_checkpoint()).state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 154


@pytest.mark.parametrize("valid", [False, True])
async def test_fenced_unknown_usage_fails_closed_without_correction(valid: bool) -> None:
    text = fenced('{"summary":"actual"}' if valid else '{"summary":NaN}')
    gateway = RepairCaptureGateway((text, None, False))
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["phase"] in {"unaccounted", "budget_exhausted"}
    assert checkpoint.state["structured_repairs"] == {}
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)


@pytest.mark.parametrize("verdict", ["reject", "revise"])
async def test_fenced_reviewer_preserves_reject_and_revise(verdict: str) -> None:
    outputs = [(fenced('{"summary":"candidate"}'), 129, False),
               (fenced(json.dumps({"verdict": verdict, "feedback": "needs changes"})), 8, False)]
    if verdict == "revise":
        outputs.extend([(fenced('{"summary":"revised"}'), 17, False),
                        (fenced('{"verdict":"approve"}'), 8, False), ("final", 11, False)])
    gateway = RepairCaptureGateway(*outputs)
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(reviewer_retries=1), crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    if verdict == "reject":
        with pytest.raises(RuntimeExecutionError, match="review rejected"):
            async for event in runtime.run(_context()):
                events.append(event)
        assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    else:
        events = [event async for event in runtime.run(_context())]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [event.payload["verdict"] for event in events
            if event.kind is EventKind.REVIEW_COMPLETED] == (
        ["reject"] if verdict == "reject" else ["revise", "approve"]
    )
    assert (await runtime.save_checkpoint()).state["structured_repairs"] == {}


async def test_framework_unfencing_preserves_provider_text() -> None:
    class UnfenceGeneration(RecordingGeneration):
        async def execute(self, *args: Any, **kwargs: Any) -> str:
            actual = await super().execute(*args, **kwargs)
            return actual.removeprefix("```json\n").removesuffix("\n```")

    worker = fenced('{"summary":"provider facts"}')
    gateway = RepairCaptureGateway(
        (worker, 129, False), (fenced('{"verdict":"approve"}'), 8, False), ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), crew_factory=RecordingFactory(UnfenceGeneration()),
    )
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any(event.artifact is not None and event.artifact.type == "text"
               and event.artifact.content.get("text") == worker for event in events)
    assert len(gateway.requests) == 3


async def test_old_rejected_fence_checkpoint_replays_correction_without_resend_or_recharge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse = adapter._parse_structured_output

    def legacy_parse(
        schema: StructuredResponseSchema, text: object, *, prefix: str, max_bytes: int,
    ) -> Mapping[str, JsonValue]:
        # Reproduce the pre-exception contract when creating the historical checkpoint.
        if isinstance(text, str) and text.startswith("```json\n"):
            raise RuntimeExecutionError(f"{prefix} is not valid json")
        return parse(schema, text, prefix=prefix, max_bytes=max_bytes)

    repository = InMemoryArtifactRepository()
    base_plan = _reviewed_step_plan()
    plan = base_plan.model_copy(update={
        "total_cost_usd": Decimal("0.02"),
        "steps": tuple(step.model_copy(update={"cost_budget_usd": Decimal("0.005")})
                       for step in base_plan.steps),
    })
    gateway = PricedCaptureGateway(
        (fenced('{"summary":"old rejected candidate"}'), 129, False),
        ('{"summary":"old corrected candidate"}', 17, False),
        ('{"verdict":"approve"}', 8, False), ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "_parse_structured_output", legacy_parse)
        events = [event async for event in runtime.run(_context())]
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None
                      and isinstance(event.checkpoint.state["usage"], Mapping)
                      and event.checkpoint.state["usage"]["tokens"] == 146)
    replay_gateway = PricedCaptureGateway(('{"verdict":"approve"}', 8, False), ("final", 11, False))
    replay = CrewDispatchRuntime(
        replay_gateway, plan, artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    replay_events = [event async for event in replay.run(_context(checkpoint=checkpoint))]
    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in replay_gateway.requests] == ["review", "general"]
    costs = [event for event in replay_events if event.kind is EventKind.COST_RECORDED]
    assert len(costs) == 2
    replay_checkpoint = await replay.save_checkpoint()
    assert replay_checkpoint.state["rejected_outputs"] == checkpoint.state["rejected_outputs"]
    assert replay_checkpoint.state["structured_repairs"] == checkpoint.state["structured_repairs"]
    usage = replay_checkpoint.state["usage"]
    assert isinstance(usage, Mapping) and usage["tokens"] == 165
    assert Decimal(str(usage["cost_usd"])) == Decimal("0.004")
