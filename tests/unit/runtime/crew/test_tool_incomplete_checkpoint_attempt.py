"""A qualified first-call receipt cannot skip its sole recovery attempt."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest

from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_known_incomplete_recovery import collect
from tests.unit.runtime.crew.test_structured_repair_output_budget import mapping
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    draft_models,
    subject,
    tool_plan,
)


@pytest.mark.parametrize("tool_status", ["prepared", "succeeded"])
async def test_natural_checkpoint_cannot_skip_qualified_recovery_attempt(
    tool_status: str,
) -> None:
    dispatch = tool_plan(9000)
    repository = InMemoryArtifactRepository()
    source_gateway = ToolIncompleteGateway()
    source_harness = RecordingHarnessToolGateway()
    source = subject(source_gateway, dispatch, repository, source_harness)
    events = await collect(source, tokens=9000)
    assert len(source_gateway.requests) == 4 and len(source_harness.calls) == 1
    checkpoint = next(
        event.checkpoint for event in events
        if event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
        and len(draft_models(event.checkpoint)) == 2
        and len(mapping(event.checkpoint.state["tools"])) == 1
        and mapping(next(iter(mapping(event.checkpoint.state["tools"]).values())))["status"] == tool_status
    )
    rows = draft_models(checkpoint)
    assert rows[0]["attempt"] == 0
    assert rows[0]["failure_reason"] == "model response incomplete"
    assert rows[1]["attempt"] == 1 and rows[1]["status"] == "succeeded"
    before = checkpoint.to_payload()
    payload = checkpoint.to_payload()
    state = cast(dict[str, Any], payload["state"])

    # Keep actual artifacts and receipts; rebind both coordinates, not just the tool key.
    rebound_models = {}
    for key, row in state["models"].items():
        if row["attempt"] == 1:
            row["attempt"] = 2
            key = source._model_call_key(
                checkpoint.run_id, row["step_id"], 2, row["purpose"],
                row["actor"], row["call_index"],
            )
        rebound_models[key] = row
    state["models"] = rebound_models
    rebound_tools = {}
    for row in state["tools"].values():
        assert row["attempt"] == 1
        row["attempt"] = 2
        key = source._tool_call_key(
            checkpoint.run_id, row["step_id"], 2, row["round"], row["tool_index"],
            row["name"], row["arguments_sha256"],
            trigger_model_artifact_id=row["trigger_model_artifact_id"],
        )
        rebound_tools[key] = row
    state["tools"] = rebound_tools
    payload["state_sha256"] = ""
    boundary = RuntimeCheckpoint.from_payload(payload)
    assert checkpoint.to_payload() == before
    assert boundary.state_sha256 != checkpoint.state_sha256
    assert all(row["attempt"] != 1 for row in draft_models(boundary))

    gateway = ToolIncompleteGateway("tool", "success", "final")
    harness = RecordingHarnessToolGateway()
    replay = subject(gateway, dispatch, repository, harness)
    failure: RuntimeExecutionError | None = None
    try:
        await replay.restore_checkpoint(boundary)
        async for _ in replay.run(_context(token_budget=9000, checkpoint=boundary)):
            pass
    except RuntimeExecutionError as error:
        failure = error

    assert not gateway.requests and not harness.calls, (
        f"skipped recovery attempt dispatched {len(gateway.requests)} model requests "
        f"and {len(harness.calls)} tool calls from {tool_status} checkpoint"
    )
    assert failure is not None, "skipped recovery attempt was not rejected"
    assert re.search(
        r"checkpoint.*(invalid|incompatible)|structured output invalid", str(failure),
    )
    assert checkpoint.to_payload() == before
