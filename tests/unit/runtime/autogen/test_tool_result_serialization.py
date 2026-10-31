"""Frozen harness evidence must remain JSON-readable at the AutoGen boundary."""
from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from autogen_core import CancellationToken
from autogen_core.tools import BaseTool

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.autogen.adapter import (
    GatewayCapabilityTool,
    _DiscussionDurability,
    _ToolRecord,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue
from agent_hub.runtime.generated_file_recovery import generated_file_arguments_sha256


class MemoryHarness:
    def __init__(self, payload: Mapping[str, JsonValue], *, failed: bool = False) -> None:
        self.payload, self.failed = payload, failed
        self.results: list[HarnessToolCallResult] = []

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        result = HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status="failed" if self.failed else "succeeded", payload=self.payload,
            failure_reason="duplicate attachment" if self.failed else None)
        self.results.append(result)
        return result


class ReplayPolicy:
    async def execute(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        raise AssertionError("tool execution must pass through the harness")

    def is_replay_safe(self, name: str) -> bool:
        return True


def tool_for(
    name: str, harness: MemoryHarness, *, artifacts: tuple[Artifact, ...] = (),
) -> tuple[GatewayCapabilityTool, _DiscussionDurability, list[_ToolRecord], list[Artifact], list[tuple[str, ...]]]:
    saved: list[Artifact] = []
    snapshots: list[tuple[str, ...]] = []

    async def publish(
        artifacts: tuple[Artifact, ...], models: tuple[dict[str, JsonValue], ...],
        tools: tuple[dict[str, JsonValue], ...],
    ) -> None:
        statuses: list[str] = []
        for row in tools:
            status = row["status"]
            assert isinstance(status, str)
            statuses.append(status)
        snapshots.append(tuple(statuses))

    async def store(artifact: Artifact) -> UUID:
        saved.append(artifact)
        return uuid4()

    async def abort(write_id: UUID) -> bool:
        raise AssertionError("no storage abort expected")

    def finalize(write_id: UUID) -> None:
        pass

    run, tenant = uuid4(), uuid4()
    durability = _DiscussionDurability(publish, store, abort, finalize,
        run_id=run, tenant_id=tenant, artifacts=artifacts)
    records: list[_ToolRecord] = []
    tool = GatewayCapabilityTool(ReplayPolicy(), tenant_id=tenant, run_id=run,
        actor="implementer", name=name, records=records, durability=durability,
        harness_tool_gateway=harness, user_id=uuid4(), role=Role.OPERATOR)
    return tool, durability, records, saved, snapshots


@pytest.mark.parametrize("name", ("project.inspect", "project.verify"))
async def test_fresh_committed_result_formats_nested_frozen_evidence(name: str) -> None:
    payload: dict[str, JsonValue] = {"verification": {"status": "passed", "checks": (
        {"phase": "test", "execution_diagnostic": {"exit_code": 0, "timed_out": False}},
    )}, "scope": {"project_id": "unit-project"}, "optional": None}
    harness = MemoryHarness(payload)
    tool, durability, records, saved, snapshots = tool_for(name, harness)
    result = await tool.run_json({}, CancellationToken(), call_id="fresh-result")

    # Reproduce formatting AFTER the durable success, not an alternate encoder.
    assert durability.tool_entries[0]["status"] == "succeeded"
    assert snapshots == [("running",), ("succeeded",)]
    assert [record.kind for record in records] == [EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED]
    assert len(harness.results) == len(saved) == 1
    assert isinstance(harness.results[0].payload["verification"], MappingProxyType)
    before = saved[0].content_sha256
    text = BaseTool.return_value_as_string(tool, result)
    assert json.loads(text) == {"result": {
        "verification": {"status": "passed", "checks": [
            {"phase": "test", "execution_diagnostic": {"exit_code": 0, "timed_out": False}},
        ]}, "scope": {"project_id": "unit-project"}, "optional": None,
    }}
    assert saved[0].content_sha256 == saved[0].recompute_content_sha256() == before
    saved_result = saved[0].content["result"]
    assert isinstance(saved_result, MappingProxyType)
    assert isinstance(saved_result["verification"], MappingProxyType)


async def test_reusable_attachment_formats_without_mutating_frozen_artifact() -> None:
    aid = str(uuid4())
    payload: dict[str, JsonValue] = {"artifact_id": aid, "presentation": "final_attachment", "file": {
        "artifact_id": aid, "filename": "unit.zip", "mime_type": "application/zip",
        "download_url": "/unit/download", "sha256": "0" * 64, "size_bytes": 128,
    }}
    arguments: dict[str, JsonValue] = {"title": "Unit", "files": {"unit.cjs": "unit data"}}
    previous = Artifact(id=uuid4(), type="tool_result", producer="implementer",
        content={"tool_name": "project.generate_zip",
            "arguments_sha256": generated_file_arguments_sha256(arguments), "result": payload})
    before = previous.content_sha256
    harness = MemoryHarness({}, failed=True)
    tool, durability, records, saved, snapshots = tool_for("project.generate_zip", harness, artifacts=(previous,))
    result = await tool.run_json(arguments, CancellationToken(), call_id="reuse-result")

    assert durability.tool_entries[0]["status"] == "succeeded"
    assert snapshots == [("running",), ("succeeded",)]
    assert [record.kind for record in records] == [EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED]
    assert len(harness.results) == len(saved) == 1
    assert harness.results[0].status == "failed"
    assert json.loads(BaseTool.return_value_as_string(tool, result)) == {"result": payload}
    assert previous.content_sha256 == previous.recompute_content_sha256() == before
    previous_result = previous.content["result"]
    assert isinstance(previous_result, MappingProxyType)
    assert isinstance(previous_result["file"], MappingProxyType)


@pytest.mark.parametrize("name", ("read_context", "workspace.read", "workspace_read"))
async def test_not_found_formats_as_negative_observation_without_empty_text(name: str) -> None:
    payload: dict[str, JsonValue] = {"path": "src/expense_report/core.py", "status": "not_found",
               "error_code": "workspace.file_not_found", "truncated": False}
    harness = MemoryHarness(payload)
    tool, durability, records, saved, _ = tool_for(name, harness)
    result = await tool.run_json({"path": payload["path"]}, CancellationToken(), call_id="missing-read")
    assert json.loads(BaseTool.return_value_as_string(tool, result)) == {"result": payload}
    assert saved[0].content["result"] == payload
    assert "text" not in result.result
    assert durability.tool_entries[0]["status"] == "succeeded"
    assert records[-1].kind is EventKind.TOOL_COMPLETED
