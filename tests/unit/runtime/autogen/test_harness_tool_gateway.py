from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from autogen_core import CancellationToken

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.models.types import ToolCall as GatewayToolCall
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    GatewayCapabilityTool,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, TaskContext


async def test_gateway_capability_tool_routes_through_harness_with_actor_identity() -> None:
    class ReplayGateway:
        def is_replay_safe(self, name: str) -> bool:
            return name == "web.search"

        async def execute(
            self,
            *,
            tenant_id: UUID,
            run_id: UUID,
            actor: str,
            name: str,
            arguments: Mapping[str, JsonValue],
            idempotency_key: str,
        ) -> Mapping[str, JsonValue]:
            del tenant_id, run_id, actor, name, arguments, idempotency_key
            raise AssertionError("tool execution must be routed through harness")

    class HarnessGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, HarnessToolCallRequest, UUID | None, Role | None]] = []

        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            self.calls.append((tenant_id, request, user_id, role))
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={"items": ("source-a",)},
            )

    async def publish(
        durable_artifacts: tuple[Artifact, ...],
        model_entries: tuple[dict[str, JsonValue], ...],
        tool_entries: tuple[dict[str, JsonValue], ...],
    ) -> None:
        del durable_artifacts, model_entries, tool_entries

    async def store(artifact: Artifact) -> UUID:
        del artifact
        return uuid4()

    async def abort(write_id: UUID) -> bool:
        del write_id
        return True

    def finalize(write_id: UUID) -> None:
        del write_id

    actor_id = uuid4()
    ctx = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="Search evidence.",
        actor_id=actor_id,
        actor_role=Role.OPERATOR,
    )
    harness_gateway = HarnessGateway()
    durability = _DiscussionDurability(
        publish,
        store,
        abort,
        finalize,
        run_id=ctx.run_id,
    )
    records: list[Any] = []
    tool = GatewayCapabilityTool(
        ReplayGateway(),
        tenant_id=ctx.tenant_id,
        run_id=ctx.run_id,
        actor="analyst",
        name="web.search",
        records=records,
        durability=durability,
        harness_tool_gateway=harness_gateway,
        user_id=ctx.actor_id,
        role=ctx.actor_role,
    )

    result = await tool.run_json({"query": "evidence"}, CancellationToken(), call_id="call_1")

    assert result.result == {"items": ("source-a",)}
    assert len(harness_gateway.calls) == 1
    tenant_id, request, user_id, role = harness_gateway.calls[0]
    assert tenant_id == ctx.tenant_id
    assert request.run_id == ctx.run_id
    assert request.actor == "analyst"
    assert request.tool_name == "web.search"
    assert request.arguments == {"query": "evidence"}
    assert request.sandbox == "restricted"
    assert request.call_id == "call_1"
    assert user_id == actor_id
    assert role is Role.OPERATOR
    assert [record.kind for record in records] == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
    ]
    started = records[0]
    assert started.payload["schema_version"] == 1
    assert started.payload["status"] == "running"
    assert started.payload["operation_kind"] == "generic"
    assert started.payload["sandbox"] == "restricted"
    assert started.payload["argument_keys"] == ("query",)
    assert started.payload["argument_key_count"] == 1
    assert started.payload["argument_bytes"] > 0
    assert "evidence" not in json.dumps(dict(started.payload))
    completed = records[1]
    assert completed.payload["schema_version"] == 1
    assert completed.payload["status"] == "succeeded"
    assert completed.payload["result_bytes"] > 0
    assert completed.payload["artifact_id"] == str(completed.artifact.id)
    assert "source-a" not in json.dumps(dict(completed.payload))


async def test_gateway_capability_tool_preserves_deterministic_harness_failure() -> None:
    class ReplayGateway:
        def is_replay_safe(self, name: str) -> bool:
            return name == "web.search"

        async def execute(
            self,
            *,
            tenant_id: UUID,
            run_id: UUID,
            actor: str,
            name: str,
            arguments: Mapping[str, JsonValue],
            idempotency_key: str,
        ) -> Mapping[str, JsonValue]:
            del tenant_id, run_id, actor, name, arguments, idempotency_key
            raise AssertionError("tool execution must be routed through harness")

    class DenyingHarnessGateway:
        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="failed",
                payload={},
                failure_reason="capability denied",
            )

    async def publish(
        durable_artifacts: tuple[Artifact, ...],
        model_entries: tuple[dict[str, JsonValue], ...],
        tool_entries: tuple[dict[str, JsonValue], ...],
    ) -> None:
        del durable_artifacts, model_entries, tool_entries

    async def store(artifact: Artifact) -> UUID:
        del artifact
        return uuid4()

    async def abort(write_id: UUID) -> bool:
        del write_id
        return True

    def finalize(write_id: UUID) -> None:
        del write_id

    records: list[Any] = []
    run_id = uuid4()
    tool = GatewayCapabilityTool(
        ReplayGateway(),
        tenant_id=uuid4(),
        run_id=run_id,
        actor="analyst",
        name="web.search",
        records=records,
        durability=_DiscussionDurability(
            publish,
            store,
            abort,
            finalize,
            run_id=run_id,
        ),
        harness_tool_gateway=DenyingHarnessGateway(),
        user_id=uuid4(),
        role=Role.OPERATOR,
    )

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        await tool.run_json({"query": "evidence"}, CancellationToken(), call_id="call_1")

    assert [(record.kind, record.reason) for record in records] == [
        (EventKind.TOOL_STARTED, None),
        (EventKind.TOOL_FAILED, "capability denied"),
    ]
    failed = records[1]
    assert failed.payload["schema_version"] == 1
    assert failed.payload["status"] == "failed"
    assert failed.payload["failure_kind"] == "capability_failed"
    assert failed.payload["argument_keys"] == ("query",)
    assert "arguments" not in failed.payload
    assert '"evidence"' not in json.dumps(dict(failed.payload))


async def test_gateway_capability_tool_reuses_existing_final_project_zip_after_failure() -> None:
    class ReplayGateway:
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

        async def execute(
            self,
            *,
            tenant_id: UUID,
            run_id: UUID,
            actor: str,
            name: str,
            arguments: Mapping[str, JsonValue],
            idempotency_key: str,
        ) -> Mapping[str, JsonValue]:
            del tenant_id, run_id, actor, name, arguments, idempotency_key
            raise AssertionError("tool execution must be routed through harness")

    class FlakyZipHarnessGateway:
        def __init__(self) -> None:
            self.calls: list[HarnessToolCallRequest] = []
            self.artifact_id = str(uuid4())

        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            self.calls.append(request)
            if len(self.calls) == 1:
                return HarnessToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    status="succeeded",
                    payload={
                        "artifact_id": self.artifact_id,
                        "file": {
                            "artifact_id": self.artifact_id,
                            "filename": "hello-world-python.zip",
                            "mime_type": "application/zip",
                            "size_bytes": 128,
                            "sha256": "0" * 64,
                            "download_url": f"/api/v1/admin/runs/{request.run_id}/artifacts/{self.artifact_id}/download",
                        },
                        "presentation": "final_attachment",
                        "summary": "Generated project ZIP artifact hello-world-python.zip.",
                    },
                )
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="failed",
                payload={},
                failure_reason="generated artifact store rejected duplicate request",
            )

    async def publish(
        durable_artifacts: tuple[Artifact, ...],
        model_entries: tuple[dict[str, JsonValue], ...],
        tool_entries: tuple[dict[str, JsonValue], ...],
    ) -> None:
        del durable_artifacts, model_entries, tool_entries

    async def store(artifact: Artifact) -> UUID:
        del artifact
        return uuid4()

    async def abort(write_id: UUID) -> bool:
        del write_id
        return True

    def finalize(write_id: UUID) -> None:
        del write_id

    run_id = uuid4()
    records: list[Any] = []
    harness = FlakyZipHarnessGateway()
    tool = GatewayCapabilityTool(
        ReplayGateway(),
        tenant_id=uuid4(),
        run_id=run_id,
        actor="implementer",
        name="project.generate_zip",
        records=records,
        durability=_DiscussionDurability(
            publish,
            store,
            abort,
            finalize,
            run_id=run_id,
        ),
        harness_tool_gateway=harness,
        user_id=uuid4(),
        role=Role.OPERATOR,
    )

    first = await tool.run_json(
        {"title": "Hello", "files": {"hello.py": "print('hello')\n"}},
        CancellationToken(),
        call_id="call_1",
    )
    second = await tool.run_json(
        {"title": "Hello", "files": {"hello.py": "print('hello')\n"}},
        CancellationToken(),
        call_id="call_2",
    )

    assert len(harness.calls) == 2
    assert first.result["file"]["filename"] == "hello-world-python.zip"
    assert second.result["file"]["filename"] == "hello-world-python.zip"
    assert [record.kind for record in records] == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
    ]


async def test_autogen_runtime_yields_tool_failure_before_runtime_failure() -> None:
    class ToolCallingGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text="analyst",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
                if len(self.requests) == 1
                else ModelResponse(
                    text=None,
                    tool_calls=(
                        GatewayToolCall(
                            id="call_1", name="web.search", arguments={"query": "evidence"}
                        ),
                    ),
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            )
            return GatewayCompletion(
                response=response,
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    class ReplayGateway:
        def is_replay_safe(self, name: str) -> bool:
            return name == "web.search"

        async def execute(
            self,
            *,
            tenant_id: UUID,
            run_id: UUID,
            actor: str,
            name: str,
            arguments: Mapping[str, JsonValue],
            idempotency_key: str,
        ) -> Mapping[str, JsonValue]:
            del tenant_id, run_id, actor, name, arguments, idempotency_key
            raise AssertionError("tool execution must be routed through harness")

    class DenyingHarnessGateway:
        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="failed",
                payload={},
                failure_reason="capability denied",
            )

    participant = DiscussionParticipant(
        id="analyst",
        role="Analyst",
        goal="Search",
        logical_model="shared",
        allowed_tools=("web.search",),
    )
    runtime = AutoGenDiscussionRuntime(
        ToolCallingGateway(),
        DiscussionPlan(
            participants=(
                participant,
                DiscussionParticipant(
                    id="critic",
                    role="Critic",
                    goal="Review",
                    logical_model="shared",
                ),
            ),
            selector_model="shared",
            max_turns=2,
        ),
        capability_gateway=ReplayGateway(),
        harness_tool_gateway=DenyingHarnessGateway(),
    )
    ctx = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="Search evidence.",
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
    )

    events: list[Any] = [event async for event in runtime.run(ctx)]

    kinds = [event.kind for event in events]
    assert EventKind.TOOL_STARTED in kinds
    assert EventKind.TOOL_FAILED in kinds
    failed_index = kinds.index(EventKind.TOOL_FAILED)
    assert events[failed_index].reason == "capability denied"
    assert kinds[failed_index + 1] is EventKind.RUNTIME_FAILED
