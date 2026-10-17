"""Synthetic causal delivery opportunities; no production data or paid calls."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelResponse, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import DispatchStep
from agent_hub.runtime.crew.workspace_boundary import (
    productive_workspace_proposal,
    productive_workspace_success,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    _workspace_delivery_bundle_response,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_maintenance_initial_context_refresh import (
    TAIL,
    A,
    ExistingWorkspace,
    Observation,
    exercise,
    make_subject,
    partial,
    rows,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (  # noqa: F401
    observe,
    offline_only,
)

FLAG = "workspace_productive_boundary"
ROUND_LIMIT = "step capability round limit exceeded"
READS = (observe("workspace.read", A), observe("workspace.read", A))


@pytest.fixture(autouse=True)
def small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def budget(context: TaskContext, step: DispatchStep) -> adapter._ToolRoundBudget:
        return adapter._ToolRoundBudget(initial_limit=2, extension_size=2, hard_limit=8)

    monkeypatch.setattr(adapter, "_tool_round_budget", budget)


async def test_changed_write_after_observation_correction_gets_normal_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await exercise(monkeypatch, (
        observe("workspace.read", A), observe("workspace.read", A), *TAIL,
    ), version=1, pressure=False)
    assert result.reason is None, "soft boundary discarded the pending changed write before authorization"
    assert result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in result.tools.calls] == [
        "workspace.read", "workspace.read", "workspace.write_text", "workspace.bundle",
    ]
    assert result.checkpoint.state[FLAG] is True
    assert len(result.deadline_credits) == 3, "the proposal itself must not earn deadline credit"
    assert all(call.approval_required for call in result.tools.calls[2:])


async def drive(responses: tuple[ModelResponse, ...], *, prior: Observation | None = None,
                checkpoint: RuntimeCheckpoint | None = None,
                tools: ExistingWorkspace | None = None, enabled: bool = True) -> Observation:
    runtime, task, gateway, tools = make_subject(
        responses, version=1, pressure=False,
        task=prior.task if prior else None, tools=tools or (prior.tools if prior else None),
        repository=prior.repository if prior else None,
    )
    if checkpoint is not None:
        await runtime.restore_checkpoint(checkpoint)
        task = task.model_copy(update={"checkpoint": checkpoint})
    stream = runtime.run(task)
    assert isinstance(stream, adapter.CrewRunStream)
    stream._state.workspace_productive_boundary = enabled
    events, reason = [], None
    try:
        async for event in stream:
            events.append(event)
    except adapter.RuntimeExecutionError as error:
        reason = str(error)
    assert isinstance(runtime._artifact_repository, InMemoryArtifactRepository)
    saved = runtime._last_checkpoint or checkpoint
    assert saved is not None, reason
    return Observation(events, reason, saved, gateway, tools, [], runtime,
                       task, runtime._artifact_repository)


@pytest.mark.parametrize("kind", ("read", "noop", "unknown_path", "mixed", "old_bundle", "invalid"))
async def test_nonproductive_or_mixed_batch_cannot_cross_soft_boundary(
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    candidate = TAIL[0]
    if kind == "read":
        candidate = observe("workspace.read", A)
    elif kind == "noop":
        candidate = _workspace_delivery_write_response(A, "a", "noop")
    elif kind == "unknown_path":
        candidate = _workspace_delivery_write_response("src/unobserved.ts", "changed", "unknown")
    elif kind == "mixed":
        candidate = replace(candidate, tool_calls=(*candidate.tool_calls, *READS[0].tool_calls))
    elif kind == "old_bundle":
        candidate = _workspace_delivery_bundle_response()
    elif kind == "invalid":
        candidate = replace(candidate, tool_calls=(ToolCall(id="bad", name="workspace.write_text",
                            arguments={"path": A, "content": "changed", "extra": True}),))
    result = await exercise(monkeypatch, (*READS, candidate, *TAIL), version=1, pressure=False)
    assert result.reason == ROUND_LIMIT
    assert len(result.gateway.requests) == 3 and len(result.tools.calls) == 2
    assert len(result.deadline_credits) == 1 and result.tools.files[A] == "a"


async def test_hard_cap_is_never_extended(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(
        initial_limit=2, extension_size=2, hard_limit=2))
    result = await exercise(monkeypatch, (*READS, *TAIL), version=1, pressure=False)
    assert result.reason == ROUND_LIMIT and len(result.tools.calls) == 2


async def test_legacy_checkpoint_missing_flag_keeps_hashes_and_original_failure() -> None:
    original = await drive((*READS, *TAIL), enabled=False)
    assert original.reason == ROUND_LIMIT and FLAG not in original.checkpoint.state
    checkpoint = partial(original, models=3, tools=2)
    before, calls = checkpoint.to_payload(), tuple(original.tools.calls)
    resumed = await drive((), prior=original, checkpoint=checkpoint)
    assert resumed.reason == ROUND_LIMIT and not resumed.gateway.requests
    assert tuple(resumed.tools.calls) == calls and FLAG not in resumed.checkpoint.state
    assert checkpoint.to_payload() == before
    for field in ("models", "tools", "usage"):
        assert resumed.checkpoint.state[field] == checkpoint.state[field]


@pytest.mark.parametrize("point", ("before", "after", "completed"))
async def test_restore_uses_prefix_not_already_changed_live_workspace(point: str) -> None:
    original = await drive((*READS, *TAIL))
    assert original.reason is None
    checkpoint = (original.checkpoint if point == "completed" else
                  partial(original, models=3, tools=2 if point == "before" else 3))
    before, calls, files = checkpoint.to_payload(), tuple(original.tools.calls), dict(original.tools.files)
    responses = TAIL[1:] if point != "completed" else ()
    resumed = await drive(responses, prior=original, checkpoint=checkpoint)
    assert resumed.reason is None
    assert checkpoint.to_payload() == before and resumed.tools.files == files
    extra = resumed.tools.calls[len(calls):]
    assert [call.tool_name for call in extra] == (
        ["workspace.write_text", "workspace.bundle"] if point == "before" else
        ["workspace.bundle"] if point == "after" else [])
    assert len(resumed.gateway.requests) == len(responses)
    for field in ("models", "tools"):
        for key, row in rows(checkpoint, field).items():
            assert rows(resumed.checkpoint, field)[key] == row
    if point == "completed":
        assert resumed.checkpoint.state["usage"] == checkpoint.state["usage"]


class ApprovalTools(ExistingWorkspace):
    waiting = True

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if self.waiting and request.tool_name == "workspace.write_text":
            self.calls.append(request)
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status="failed", failure_reason="capability requires approval",
                payload={"approval_id": "approved-only-by-synthetic-owner"})
        return await super().invoke(tenant_id, request, user_id=user_id, role=role)


async def test_waiting_approval_restore_retains_original_slot_and_no_new_model() -> None:
    tools = ApprovalTools()
    original = await drive((*READS, *TAIL), tools=tools)
    assert original.reason == "capability execution failed"
    checkpoint = original.checkpoint
    pending = [row for row in rows(checkpoint, "tools").values() if row["status"] == "waiting_approval"]
    assert len(pending) == 1 and pending[0]["name"] == "workspace.write_text"
    before, count = checkpoint.to_payload(), len(tools.calls)
    tools.waiting = False
    resumed = await drive(TAIL[1:], prior=original, checkpoint=checkpoint)
    assert resumed.reason is None and len(resumed.gateway.requests) == 2
    assert [call.tool_name for call in tools.calls[count:]] == ["workspace.write_text", "workspace.bundle"]
    assert tools.calls[count].idempotency_key == tools.calls[count - 1].idempotency_key
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("status", ("running", "uncertain"))
async def test_unknown_write_is_never_reexecuted(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    original = await drive((*READS, *TAIL), tools=ApprovalTools())
    payload = original.checkpoint.to_payload()
    states = cast(dict[str, dict[str, JsonValue]], cast(dict[str, JsonValue], payload["state"])["tools"])
    pending = next(row for row in states.values() if row["status"] == "waiting_approval")
    pending.pop("approval_id")
    pending["status"] = status
    pending["replay_safe"] = False
    payload["state_sha256"] = ""
    checkpoint, calls = RuntimeCheckpoint.from_payload(payload), tuple(original.tools.calls)
    before = checkpoint.to_payload()
    runtime, task, gateway, tools = make_subject((), version=1, pressure=False,
        task=original.task, tools=original.tools, repository=original.repository)
    assert runtime._capabilities is not None
    monkeypatch.setattr(runtime._capabilities, "is_replay_safe", lambda _: False)
    await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(adapter.CapabilityOutcomeUncertain, match="requires confirmation"):
        async for _ in runtime.run(task.model_copy(update={"checkpoint": checkpoint})):
            pass
    assert not gateway.requests and tuple(tools.calls) == calls
    assert checkpoint.to_payload() == before


@pytest.mark.parametrize("value", (True, "true", 1, -1, None))
async def test_invalid_checkpoint_policy_fails_before_io(value: JsonValue) -> None:
    original = await drive((*READS, *TAIL), enabled=False)
    payload = original.checkpoint.to_payload()
    cast(dict[str, JsonValue], payload["state"])[FLAG] = value
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    if value is True:
        # Explicit true is supported, but not without its meaningful-progress prerequisite.
        payload = checkpoint.to_payload()
        cast(dict[str, JsonValue], payload["state"])["workspace_meaningful_progress"] = False
        payload["state_sha256"] = ""
        checkpoint = RuntimeCheckpoint.from_payload(payload)
    runtime, _, gateway, tools = make_subject((), version=1, pressure=False,
        task=original.task, tools=original.tools, repository=original.repository)
    calls = tuple(tools.calls)
    with pytest.raises(adapter.RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(checkpoint)
    assert not gateway.requests and tuple(tools.calls) == calls


def fact(name: str = "workspace.read", result: Mapping[str, JsonValue] | None = None,
         round_index: int = 0) -> tuple[dict[str, Mapping[str, JsonValue]], dict[str, Artifact]]:
    trigger = str(uuid4())
    artifact = Artifact(id=uuid4(), type="tool_result", producer="worker",
        content={"agent_id": "worker", "tool_name": name, "arguments_sha256": "a" * 64,
                 "result": result if result is not None else {"path": A, "text": "old", "truncated": False}},
        source_ids=(trigger,))
    state: dict[str, JsonValue] = {"step_id": "repair", "attempt": 0, "round": round_index,
        "tool_index": 0, "name": name, "status": "succeeded", "artifact_id": str(artifact.id),
        "sha256": artifact.content_sha256, "arguments_sha256": "a" * 64,
        "trigger_model_artifact_id": trigger}
    return {"key": state}, {"key": artifact}


def eligible(states: Mapping[str, Mapping[str, JsonValue]], artifacts: Mapping[str, Artifact],
             calls: tuple[ToolCall, ...] | None = None) -> bool:
    return productive_workspace_proposal(calls=calls if calls is not None else TAIL[0].tool_calls,
        planned_tools=("workspace.write_text", "workspace.bundle"), step_id="repair", actor="worker",
        attempt=0, round_index=2, states=states, artifacts=artifacts)


@pytest.mark.parametrize("name", ("workspace.read", "workspace_read", "workspace.list", "workspace.write_text"))
def test_read_or_written_file_metadata_can_prove_change(name: str) -> None:
    result: Mapping[str, JsonValue] | None = None
    if name in {"workspace.list", "workspace.write_text"}:
        result = {"workspace_files": ({"path": A, "sha256": hashlib.sha256(b"old").hexdigest(), "size_bytes": 3},)}
    assert eligible(*fact(name, result))


@pytest.mark.parametrize("field,value", (("status", "running"), ("status", "uncertain"),
    ("status", "rejected"), ("status", "waiting_approval"), ("sha256", "b" * 64),
    ("trigger_model_artifact_id", str(UUID(int=1))), ("arguments_sha256", "b" * 64),
    ("artifact_id", str(UUID(int=2))), ("attempt", 1), ("round", 2), ("step_id", "foreign")))
def test_unproved_or_future_facts_do_not_authorize_opportunity(field: str, value: JsonValue) -> None:
    states, artifacts = fact()
    states["key"] = {**states["key"], field: value}
    assert not eligible(states, artifacts)


def test_duplicate_coordinates_and_wrong_producer_are_not_evidence() -> None:
    states, artifacts = fact()
    assert not eligible({**states, "duplicate": states["key"]}, {**artifacts, "duplicate": artifacts["key"]})
    artifact = artifacts["key"].model_copy(update={"producer": "foreign"})
    assert not eligible(states, {"key": artifact})


def test_truncated_read_and_empty_write_metadata_cannot_prove_delivery() -> None:
    assert not eligible(*fact(result={"path": A, "text": "old", "truncated": True}))
    states, artifacts = fact("workspace.write_text", {"workspace_files": ()})
    assert not eligible(states, artifacts, _workspace_delivery_bundle_response().tool_calls)


def test_missing_fresh_bundle_can_qualify_but_completed_bundle_cannot() -> None:
    metadata: Mapping[str, JsonValue] = {"path": A, "sha256": hashlib.sha256(b"old").hexdigest(), "size_bytes": 3}
    states, artifacts = fact("workspace.write_text", metadata)
    call = _workspace_delivery_bundle_response().tool_calls
    assert eligible(states, artifacts, call)
    extra_states, extra_artifacts = fact("workspace.bundle", {
        "workspace_files": (metadata,), "artifact_id": str(uuid4()),
        "file": {"filename": "delivered.zip"}, "presentation": "final_attachment",
    }, round_index=1)
    states["bundle"], artifacts["bundle"] = extra_states["key"], extra_artifacts["key"]
    assert not eligible(states, artifacts, call)


async def test_new_fresh_bundle_gets_original_approval_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(
        initial_limit=3, extension_size=2, hard_limit=8))
    result = await exercise(monkeypatch, (
        TAIL[0], observe("workspace.list"), observe("workspace.list"), *TAIL[1:],
    ), version=1, pressure=False)
    assert result.reason is None
    assert result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert result.tools.calls[-1].tool_name == "workspace.bundle"
    assert result.tools.calls[-1].approval_required
    assert len(result.deadline_credits) == 3


async def test_only_one_opportunity_no_repeated_observation_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await exercise(monkeypatch, (
        *READS, TAIL[0], observe("workspace.read", A), observe("workspace.read", A),
        observe("workspace.read", A), *TAIL[1:],
    ), version=1, pressure=False)
    assert result.reason in {ROUND_LIMIT, "workspace delivery stalled after unchanged observations"}
    assert len(result.gateway.requests) <= 6
    assert not any(call.tool_name == "workspace.bundle" for call in result.tools.calls)
    assert len(result.deadline_credits) == 3


def test_same_path_changed_then_reverted_batch_cannot_earn_boundary() -> None:
    forward = TAIL[0].tool_calls[0]
    backward = ToolCall(id="revert", name="workspace.write_text", arguments={"path": A, "content": "old"})
    assert not eligible(*fact(), (forward, backward))


async def test_net_noop_batch_stops_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    forward = TAIL[0].tool_calls[0]
    backward = ToolCall(id="revert", name="workspace.write_text", arguments={"path": A, "content": "a"})
    candidate = replace(TAIL[0], tool_calls=(forward, backward))
    result = await exercise(monkeypatch, (*READS, candidate, *TAIL[1:]), version=1, pressure=False)
    assert result.reason == ROUND_LIMIT
    assert result.tools.files[A] == "a"
    assert [call.tool_name for call in result.tools.calls] == ["workspace.read", "workspace.read"]


async def test_soft8_waiting_approved_success_checkpoint_restores_bundle_without_new_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_tool_round_budget", lambda *_: adapter._ToolRoundBudget(
        initial_limit=8, extension_size=8, hard_limit=20))
    paths = (A, "src/dependency-b.ts", *(f"docs/retained-{index}.md" for index in range(5)), A)
    observations = tuple(observe("workspace.read", path) for path in paths)
    tools = ApprovalTools()
    waiting = await drive((*observations, *TAIL), tools=tools)
    assert waiting.reason == "capability execution failed"
    waiting_checkpoint = waiting.checkpoint
    pending = [row for row in rows(waiting_checkpoint, "tools").values() if row["status"] == "waiting_approval"]
    assert len(pending) == 1 and pending[0]["round"] == 8
    assert len(waiting.gateway.requests) == 9
    tools.waiting = False
    approved = await drive(TAIL[1:], prior=waiting, checkpoint=waiting_checkpoint)
    assert approved.reason is None and len(approved.gateway.requests) == 2
    succeeded = partial(approved, models=9, tools=9)
    before, calls, files = succeeded.to_payload(), tuple(tools.calls), dict(tools.files)
    assert len([row for row in rows(succeeded, "tools").values()
                if row["name"] == "workspace.write_text" and row["status"] == "succeeded"]) == 1
    credits: list[float] = []
    original_credit = adapter._tool_progress_step_deadline

    def credit(ctx: TaskContext, *, step_deadline: float, run_deadline: float) -> float:
        result = original_credit(ctx, step_deadline=step_deadline, run_deadline=run_deadline)
        credits.append(result)
        return result

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", credit)
    restored = await drive(TAIL[1:], prior=approved, checkpoint=succeeded)
    assert restored.reason is None and restored.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(restored.gateway.requests) == 2
    assert [call.tool_name for call in tools.calls[len(calls):]] == ["workspace.bundle"]
    assert len(credits) == 1, "only the fresh bundle, never replayed writes, may earn deadline credit"
    assert tools.files == files and succeeded.to_payload() == before
    for field in ("models", "tools"):
        for key, row in rows(succeeded, field).items():
            assert rows(restored.checkpoint, field)[key] == row
    for field in ("usage", "timeout_progress_units"):
        assert restored.checkpoint.state[field] == approved.checkpoint.state[field]


SECOND_PATH = "src/dependency-b.ts"
SECOND_WRITE = _workspace_delivery_write_response(SECOND_PATH, "export const second = true;", "second-repair")
TWO_WINDOWS = (*READS, TAIL[0], observe("workspace.read", SECOND_PATH),
               observe("workspace.read", SECOND_PATH), SECOND_WRITE, *TAIL[1:])


async def test_two_soft_windows_allow_two_independently_proven_changed_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await exercise(monkeypatch, TWO_WINDOWS, version=1, pressure=False)
    assert result.reason is None, "a successful first repair permanently spent the productive opportunity"
    assert result.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in result.tools.calls] == [
        "workspace.read", "workspace.read", "workspace.write_text",
        "workspace.read", "workspace.read", "workspace.write_text", "workspace.bundle",
    ]
    assert len(result.gateway.requests) == 8 and len(result.deadline_credits) == 5


@pytest.mark.parametrize("tool_count", (5, 6))
async def test_second_opportunity_reconstructs_after_partial_restore(tool_count: int) -> None:
    original = await drive(TWO_WINDOWS)
    assert original.reason is None
    checkpoint = partial(original, models=6, tools=tool_count)
    before, calls = checkpoint.to_payload(), tuple(original.tools.calls)
    restored = await drive(TAIL[1:], prior=original, checkpoint=checkpoint)
    assert restored.reason is None and restored.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in restored.tools.calls[len(calls):]] == (
        ["workspace.write_text", "workspace.bundle"] if tool_count == 5 else ["workspace.bundle"])
    assert len(restored.gateway.requests) == 2 and checkpoint.to_payload() == before
    for field in ("models", "tools"):
        for key, row in rows(checkpoint, field).items():
            assert rows(restored.checkpoint, field)[key] == row
    for field in ("usage", "timeout_progress_units"):
        assert restored.checkpoint.state[field] == original.checkpoint.state[field]


def success_fact() -> tuple[dict[str, Mapping[str, JsonValue]], dict[str, Artifact]]:
    states, artifacts = fact()
    call = TAIL[0].tool_calls[0]
    content = call.arguments["content"]
    assert isinstance(content, str)
    raw = content.encode()
    extra_states, extra_artifacts = fact("workspace.write_text", {
        "path": A, "content_sha256": hashlib.sha256(raw).hexdigest(), "content_bytes": len(raw),
    }, round_index=2)
    arguments_sha = hashlib.sha256(json.dumps(dict(call.arguments), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    old = extra_artifacts["key"]
    artifact = Artifact(id=old.id, type=old.type, producer=old.producer,
        content={**old.content, "arguments_sha256": arguments_sha}, source_ids=old.source_ids)
    states["success"] = {**extra_states["key"], "arguments_sha256": arguments_sha, "sha256": artifact.content_sha256}
    artifacts["success"] = artifact
    return states, artifacts


def success(states: Mapping[str, Mapping[str, JsonValue]], artifacts: Mapping[str, Artifact]) -> bool:
    return productive_workspace_success(calls=TAIL[0].tool_calls,
        planned_tools=("workspace.write_text", "workspace.bundle"), step_id="repair", actor="worker",
        attempt=0, round_index=2, states=states, artifacts=artifacts)


def test_reset_requires_exact_same_slot_success_receipts() -> None:
    states, artifacts = success_fact()
    assert success(states, artifacts)
    assert not success(*fact()), "the proposal alone must not reset the opportunity"
    assert not success({**states, "extra": states["success"]}, {**artifacts, "extra": artifacts["success"]})


@pytest.mark.parametrize("field,value", (("status", "running"), ("status", "uncertain"),
    ("status", "rejected"), ("status", "waiting_approval"), ("tool_index", 1),
    ("step_id", "foreign"), ("attempt", 1), ("round", 3), ("sha256", "b" * 64)))
def test_pending_foreign_or_extra_receipts_cannot_reset(field: str, value: JsonValue) -> None:
    states, artifacts = success_fact()
    states["success"] = {**states["success"], field: value}
    assert not success(states, artifacts)


@pytest.mark.parametrize("mutation", ("result", "arguments", "source", "actor"))
def test_self_consistent_receipt_still_must_match_original_changed_proposal(mutation: str) -> None:
    states, artifacts = success_fact()
    old = artifacts["success"]
    content = dict(old.content)
    if mutation == "result":
        content["result"] = {"path": A, "content_sha256": hashlib.sha256(b"wrong").hexdigest(), "content_bytes": 5}
    elif mutation == "arguments":
        content["arguments_sha256"] = "b" * 64
        states["success"] = {**states["success"], "arguments_sha256": "b" * 64}
    elif mutation == "actor":
        content["agent_id"] = "foreign"
    artifact = Artifact(id=old.id, type=old.type, producer="foreign" if mutation == "actor" else old.producer,
        content=content, source_ids=(str(UUID(int=7)),) if mutation == "source" else old.source_ids)
    states["success"] = {**states["success"], "sha256": artifact.content_sha256}
    artifacts["success"] = artifact
    assert not success(states, artifacts)


async def test_disabled_context_refresh_neither_uses_nor_saves_new_policy() -> None:
    runtime, task, gateway, tools = make_subject((*READS, *TAIL), version=1, pressure=False)
    stream = runtime.run(task)
    assert isinstance(stream, adapter.CrewRunStream)
    stream._state.workspace_context_refresh = False
    with pytest.raises(adapter.RuntimeExecutionError, match=ROUND_LIMIT):
        async for _ in stream:
            pass
    saved = await runtime.save_checkpoint()
    assert FLAG not in saved.state and len(gateway.requests) == 3 and len(tools.calls) == 2


@pytest.mark.parametrize("dependency", ("workspace_refresh_round_continuation", "workspace_context_refresh",
                                      "workspace_meaningful_progress", "workspace_delivery_continuation"))
async def test_true_policy_requires_all_enabled_checkpoint_prerequisites(dependency: str) -> None:
    original = await drive((*READS, *TAIL), enabled=False)
    payload = original.checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    state[FLAG] = True
    state[dependency] = False
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    runtime, _, gateway, tools = make_subject((), version=1, pressure=False,
        task=original.task, tools=original.tools, repository=original.repository)
    calls = tuple(tools.calls)
    with pytest.raises(adapter.RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(checkpoint)
    assert not gateway.requests and tuple(tools.calls) == calls


@pytest.mark.parametrize("empty", (False, True))
def test_complete_listing_can_prove_create_including_empty_workspace(empty: bool) -> None:
    entries: tuple[JsonValue, ...] = () if empty else ({
        "path": SECOND_PATH, "sha256": hashlib.sha256(b"other").hexdigest(), "size_bytes": 5,
    },)
    assert eligible(*fact("workspace.list", {"workspace_files": entries}))


@pytest.mark.parametrize("result", (
    {"workspace_files": (), "truncated": True},
    {"workspace_files": (), "has_more": True},
    {"workspace_files": (), "next_cursor": "more"},
    {"workspace_files": ({"path": SECOND_PATH},)},
    {"complete": True},
))
def test_missing_truncated_or_incomplete_listing_never_proves_create(result: JsonValue) -> None:
    assert isinstance(result, Mapping)
    assert not eligible(*fact("workspace.list", result))


def test_later_truncated_read_marks_path_existing_not_proven_absent() -> None:
    states, artifacts = fact("workspace.list", {"workspace_files": ()})
    later_states, later_artifacts = fact(result={"path": A, "text": "partial", "truncated": True}, round_index=1)
    states["later"], artifacts["later"] = later_states["key"], later_artifacts["key"]
    assert not eligible(states, artifacts)


async def test_create_from_complete_listing_and_partial_restore_are_causal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = _workspace_delivery_write_response("src/new-production.ts", "export const added = true;", "create")
    original = await exercise(monkeypatch, (
        observe("workspace.list"), observe("workspace.list"), create, *TAIL[1:],
    ), version=1, pressure=False)
    assert original.reason is None, "complete listing proved the new file absent before the boundary"
    assert original.events[-1].kind is EventKind.RUNTIME_COMPLETED and len(original.tools.files) == 31
    checkpoint = partial(original, models=3, tools=3)
    before, calls = checkpoint.to_payload(), tuple(original.tools.calls)
    restored = await drive(TAIL[1:], prior=original, checkpoint=checkpoint)
    assert restored.reason is None and restored.events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in restored.tools.calls[len(calls):]] == ["workspace.bundle"]
    assert len(restored.gateway.requests) == 2 and checkpoint.to_payload() == before
    assert restored.checkpoint.state["usage"] == original.checkpoint.state["usage"]
