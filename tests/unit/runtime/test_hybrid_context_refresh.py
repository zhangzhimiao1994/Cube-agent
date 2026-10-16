"""A bounded context refresh must not inflate hybrid progress or deadlines."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, CrewRunStream
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_sequence_runtime,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (  # noqa: F401
    TOOLS,
    observe,
    offline_only,
)
from tests.unit.runtime.crew.test_workspace_context_refresh import (
    A,
    B,
    context,
    harness,
    prefix,
    tail,
    trace_deadline,
)
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime


@pytest.mark.parametrize("enabled", (False, True))
async def test_hybrid_context_refresh_keeps_semantic_progress_and_deadline_bounds(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    original_run = CrewDispatchRuntime.run

    def run(dispatch: CrewDispatchRuntime, task: TaskContext) -> CrewRunStream:
        stream = cast(CrewRunStream, original_run(dispatch, task))
        stream._state.workspace_context_refresh = enabled
        return stream

    monkeypatch.setattr(CrewDispatchRuntime, "run", run)
    credits = trace_deadline(monkeypatch)
    gateway = WorkspaceDeliverySequenceGateway((
        *prefix(), *(observe("workspace.read", path) for path in (A, B, A, B)), *tail(),
    ))
    tools = harness()
    dispatch = _workspace_delivery_sequence_runtime(gateway, tools, tools=TOOLS)
    subject = HybridRuntime(
        dispatch,
        MultiArtifactRuntime(TaskMode.DISCUSS, ()),
        MultiArtifactRuntime(TaskMode.DIRECT, ()),
    )
    task = context().model_copy(update={"mode": TaskMode.HYBRID})
    events = [event async for event in subject.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    children: list[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]] = [
        (event.checkpoint.state, cast(Mapping[str, JsonValue],
            cast(Mapping[str, JsonValue], event.checkpoint.state["child_checkpoint"])["state"]))
        for event in events if event.checkpoint is not None
        and event.checkpoint.state.get("child_checkpoint") is not None
    ]
    assert children
    assert all(child["workspace_context_refresh"] is enabled for _, child in children)
    assert all(parent["timeout_progress_units"] == child["timeout_progress_units"]
               for parent, child in children)
    assert max(cast(int, child["timeout_progress_units"]) for _, child in children) == 3
    assert len(credits) == 3
    assert len(gateway.requests) == (8 if enabled else 5)
