from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.runtime.test_hybrid import (
    FailingRuntime,
    MultiArtifactRuntime,
    UnusedRuntime,
    artifact,
    final_zip_artifact,
)


class DiagnosticFailure(FailingRuntime):
    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        reason = "model gateway failed: model transport failed (status=503)"
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=context.run_id, reason=reason,
            payload={**runtime_failure_diagnostic_from_reason(reason),
                     "error_code": "model.transport_failed", "error_stage": "discussion_selector"},
        )


async def partial_run(
    *, failure_mode: TaskMode = TaskMode.DISCUSS, attachment: bool = False,
) -> tuple[
    TaskContext, RuntimeCheckpoint, InMemoryArtifactRepository, RunEvent,
]:
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
                          request="Terminal checkpoint diagnostic fixture.")
    repository = InMemoryArtifactRepository()
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (
            final_zip_artifact() if attachment else artifact("worker", "Executed result."),
        )),
        DiagnosticFailure(TaskMode.DISCUSS, "unused") if failure_mode is TaskMode.DISCUSS
        else MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("critic", "Reviewed result."),)),
        DiagnosticFailure(TaskMode.DIRECT, "unused") if failure_mode is TaskMode.DIRECT
        else UnusedRuntime(TaskMode.DIRECT, "unused"),
        artifact_repository=repository,
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].payload["error_stage"] == "discussion_selector"
    return context, await runtime.save_checkpoint(), repository, events[-1]


async def replay(
    context: TaskContext, checkpoint: RuntimeCheckpoint, repository: InMemoryArtifactRepository,
) -> list[RunEvent]:
    executions: list[TaskMode] = []

    class RejectExecution(UnusedRuntime):
        def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            executions.append(self.mode)
            return super().run(context)

    runtime = HybridRuntime(
        RejectExecution(TaskMode.DISPATCH, "terminal replay must not execute"),
        RejectExecution(TaskMode.DISCUSS, "terminal replay must not execute"),
        RejectExecution(TaskMode.DIRECT, "terminal replay must not execute"),
        artifact_repository=repository,
    )
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]
    assert executions == []
    return events


def rebuild(checkpoint: RuntimeCheckpoint, state: Mapping[str, JsonValue], version: str) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        id=uuid4(), runtime_type=checkpoint.runtime_type, runtime_version=version,
        run_id=checkpoint.run_id, tenant_id=checkpoint.tenant_id, mode=checkpoint.mode, state=state,
    )


@pytest.mark.parametrize("failure_mode", (TaskMode.DISCUSS, TaskMode.DIRECT))
@pytest.mark.parametrize("attachment", (False, True))
async def test_partial_terminal_replay_preserves_original_safe_diagnostic_and_checkpoint(
    failure_mode: TaskMode, attachment: bool,
) -> None:
    context, checkpoint, repository, terminal = await partial_run(
        failure_mode=failure_mode, attachment=attachment,
    )
    original = checkpoint.to_payload()
    restored = RuntimeCheckpoint.from_payload(original)
    events = await replay(context, restored, repository)
    assert len(events) == 1
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == terminal.reason
    assert events[-1].payload == terminal.payload
    diagnostic = checkpoint.state["terminal_diagnostic"]
    assert isinstance(diagnostic, Mapping)
    assert diagnostic["schema_version"] == 1
    assert diagnostic["reason"] == (
        f"hybrid {failure_mode.value} failed: model gateway failed: model transport failed (status=503)"
    )
    assert diagnostic["payload"] == terminal.payload
    assert restored.to_payload() == checkpoint.to_payload() == original


@pytest.mark.parametrize("version", ("1", "2", "3"))
async def test_legacy_terminal_without_diagnostic_does_not_invent_failure_or_rewrite(version: str) -> None:
    context, checkpoint, repository, terminal = await partial_run()
    state = dict(checkpoint.state)
    state.pop("terminal_diagnostic", None)
    if version != "3":
        state.pop("token_budget_grant")
        state.pop("token_budget_spent")
    if version == "1":
        for key in ("child_checkpoint", "remaining_absolute_timeout_seconds", "timeout_progress_units",
                    "last_child_progress_fingerprint"):
            state.pop(key)
    legacy = rebuild(checkpoint, state, version)
    before = legacy.to_payload()
    events = await replay(context, legacy, repository)
    assert len(events) == 1
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == terminal.reason
    assert dict(events[-1].payload) == {}
    assert legacy.to_payload() == before


@pytest.mark.parametrize("mutation", (
    "unknown_schema", "boolean_schema", "missing_code", "extra_envelope_key", "extra_payload_key",
    "unsafe_reason", "unsafe_stage", "private_summary", "nonterminal", "explicit_completion",
    "wrong_next_stage", "unknown_state_key", "diagnostic_on_v1", "diagnostic_on_v2",
))
async def test_terminal_diagnostic_rejects_invalid_envelope_without_execution(mutation: str) -> None:
    context, checkpoint, repository, terminal = await partial_run()
    state = deepcopy(cast(dict[str, JsonValue], checkpoint.to_payload()["state"]))
    diagnostic: dict[str, JsonValue] = {
        "schema_version": 1, "reason": terminal.payload["error_summary"],
        "payload": dict(terminal.payload),
    }
    payload = cast(dict[str, JsonValue], diagnostic["payload"])
    version = "3"
    if mutation == "unknown_schema":
        diagnostic["schema_version"] = 2
    elif mutation == "boolean_schema":
        diagnostic["schema_version"] = True
    elif mutation == "missing_code":
        payload.pop("error_code")
    elif mutation == "extra_envelope_key":
        diagnostic["unexpected"] = "not allowed"
    elif mutation == "extra_payload_key":
        payload["unexpected"] = "not allowed"
    elif mutation == "unsafe_reason":
        diagnostic["reason"] = "Authorization Bearer sk-private"
    elif mutation == "unsafe_stage":
        payload["error_stage"] = "password"
    elif mutation == "private_summary":
        payload["error_summary"] = "Private text unrelated to the recorded reason."
    elif mutation == "nonterminal":
        state["terminal"] = False
        state["next_stage"] = 1
    elif mutation == "explicit_completion":
        state["reason"] = "explicit_completion"
    elif mutation == "wrong_next_stage":
        state["next_stage"] = 1
    elif mutation == "unknown_state_key":
        state["unexpected"] = "not allowed"
    elif mutation in {"diagnostic_on_v1", "diagnostic_on_v2"}:
        version = "1" if mutation == "diagnostic_on_v1" else "2"
        state.pop("token_budget_grant")
        state.pop("token_budget_spent")
        if version == "1":
            for key in ("child_checkpoint", "remaining_absolute_timeout_seconds",
                        "timeout_progress_units", "last_child_progress_fingerprint"):
                state.pop(key)
    state["terminal_diagnostic"] = diagnostic
    changed = rebuild(checkpoint, state, version)
    before = changed.to_payload()
    events = await replay(context, changed, repository)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.artifact is not None for event in events)
    assert changed.to_payload() == before
    assert "sk-private" not in repr(events)


async def test_unknown_checkpoint_version_is_rejected_without_rewriting() -> None:
    context, checkpoint, repository, _ = await partial_run()
    unknown = rebuild(checkpoint, checkpoint.state, "99")
    before = unknown.to_payload()
    with pytest.raises(RuntimeError, match="checkpoint is incompatible"):
        await replay(context, unknown, repository)
    assert unknown.to_payload() == before
