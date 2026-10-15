from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.plugins.contracts import plugin_package_generation_guidance
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.crew.adapter import RuntimeExecutionError as CrewRuntimeExecutionError
from agent_hub.runtime.direct import DirectRuntime, Gateway, RuntimeExecutionError
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.instruction_context import InstructionContext, model_request_sha256
from tests.contracts.test_runtime_contract import FakeGateway
from tests.integration.runtime.test_crew_adapter import (
    FakeCapabilities,
    FastFactory,
    FastGeneration,
    ToolGateway,
    one_step_plan,
)
from tests.unit.runtime.crew.test_instruction_context import task as guidance_task
from tests.unit.runtime.test_direct_prompt import (
    RecordingCapabilityGateway,
    SequencedDirectGateway,
)
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime

_MARKER = "PLUGIN_PACKAGE_STDIO_CONTRACT"
_CREATION_REQUESTS = (
    "\u751f\u6210\u4e00\u4e2a\u63d2\u4ef6",
    "\u751f\u6210\u4e00\u4e2a\u80fd\u529b",
    "\u5e2e\u6211\u521b\u5efa\u4e00\u4e2a\u6587\u672c\u7edf\u8ba1\u63d2\u4ef6",
    "\u5199\u4e00\u4e2a\u53ef\u5b89\u88c5\u7684\u80fd\u529b",
    "Generate a plugin",
    "Create a capability",
    "Build a text statistics plugin",
    "Write a new capability",
    "\u751f\u6210\u4e00\u4e2a Python \u63d2\u4ef6",
    "\u65b0\u589e PDF \u89e3\u6790\u80fd\u529b",
    "\u6dfb\u52a0\u4e00\u4e2a CSV \u5bfc\u51fa\u63d2\u4ef6",
    "\u7f16\u5199\u4ee3\u7801\uff0c\u5b9e\u73b0\u4e00\u4e2a Python \u63d2\u4ef6\u5c0f\u578b\u9879\u76ee",
    "\u4e0d\u8981\u89e3\u91ca\uff0c\u76f4\u63a5\u751f\u6210\u4e00\u4e2a\u63d2\u4ef6",
    "\u4e0d\u8981\u4f7f\u7528\u5916\u90e8\u4f9d\u8d56\uff0c\u751f\u6210\u4e00\u4e2a\u63d2\u4ef6",
    "\u5e2e\u6211\u521b\u5efa\u4e00\u4e2a Python plugin",
    "\u5e2e\u6211\u521b\u5efa\u4e00\u4e2a Python Plugin",
    "Create a \u6587\u672c\u7edf\u8ba1 plugin",
    "\u4e0d\u8981\u89e3\u91ca\u76f4\u63a5\u751f\u6210\u4e00\u4e2a\u63d2\u4ef6",
    "\u4e0d\u8981\u4f7f\u7528\u5916\u90e8\u4f9d\u8d56\u76f4\u63a5\u751f\u6210\u4e00\u4e2a\u63d2\u4ef6",
    "\u628a CSV \u5bfc\u51fa\u529f\u80fd\u63d2\u4ef6\u5316",
    "Pluginize the CSV export feature",
)
_ORDINARY_REQUESTS = (
    "\u8c03\u7528\u6587\u672c\u7edf\u8ba1\u80fd\u529b",
    "Use the text statistics plugin",
    "\u7528\u73b0\u6709\u63d2\u4ef6\u751f\u6210\u62a5\u544a",
    "Generate a report using the calendar plugin",
    "\u5b89\u88c5\u73b0\u6709\u63d2\u4ef6",
    "Install the existing capability",
    "Explain how to create a plugin",
    "Generate plugin documentation",
    "Create a report with the statistics capability",
    "Do not generate a plugin",
    "\u8bf7\u89e3\u91ca\u5982\u4f55\u521b\u5efa\u63d2\u4ef6",
    "\u751f\u6210\u63d2\u4ef6\u6587\u6863",
    "\u4e0d\u8981\u521b\u5efa\u63d2\u4ef6\uff0c\u53ea\u8c03\u7528\u73b0\u6709\u80fd\u529b",
    "\u4e0d\u7528\u521b\u5efa\u63d2\u4ef6\uff0c\u53ea\u8c03\u7528\u73b0\u6709\u80fd\u529b",
    "\u4e0d\u8981\u628a\u73b0\u6709\u529f\u80fd\u63d2\u4ef6\u5316",
)


def _request_text(request: ModelRequest) -> str:
    return "\n".join(str(message.content) for message in request.messages)


def _assert_stdio_contract(request: ModelRequest) -> None:
    text = _request_text(request)
    assert text.count(_MARKER) == 1
    schema_line = next(
        line for line in text.splitlines() if line.startswith("PLUGIN_PACKAGE_STDIN_SCHEMA_JSON=")
    )
    schema = json.loads(schema_line.partition("=")[2])
    assert set(schema["required"]) == {
        "schema_version", "plugin_id", "capability_id", "arguments",
        "resource_config", "capability_config", "context",
    }
    assert schema["properties"]["schema_version"]["const"] == 1
    assert set(schema["properties"]["context"]["required"]) == {
        "tenant_id", "user_id", "run_id", "actor", "idempotency_key",
    }


async def _capture(
    request: str, mode: TaskMode, *, empty_project_guidance: bool = False,
) -> list[ModelRequest]:
    gateway = FakeGateway()
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=mode, request=request,
        token_budget=20_000,
    )
    assert context.instruction_context is None
    if empty_project_guidance:
        context = context.model_copy(update={"instruction_context": InstructionContext(
            load_id=uuid4(), tenant_id=context.tenant_id, run_id=context.run_id, sources=(),
        )})
    runtime = (
        DirectRuntime(gateway, logical_model="general")
        if mode is TaskMode.DIRECT
        else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert gateway.requests
    return gateway.requests


@pytest.mark.parametrize("task", _CREATION_REQUESTS)
@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH))
async def test_plugin_creation_gateway_request_has_stdio_contract_without_project_guidance(
    task: str, mode: TaskMode,
) -> None:
    for actual in await _capture(task, mode):
        _assert_stdio_contract(actual)


@pytest.mark.parametrize("task", _ORDINARY_REQUESTS)
@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH))
async def test_ordinary_capability_gateway_request_has_no_generation_guidance(
    task: str, mode: TaskMode,
) -> None:
    for actual in await _capture(task, mode):
        assert _MARKER not in _request_text(actual)


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH))
async def test_empty_instruction_context_still_supplies_sdk_knowledge(mode: TaskMode) -> None:
    for actual in await _capture(
        "\u751f\u6210\u4e00\u4e2a Python \u63d2\u4ef6", mode, empty_project_guidance=True,
    ):
        _assert_stdio_contract(actual)
        assert "<PROJECT_GUIDANCE_JSON>" not in _request_text(actual)


async def test_crew_tool_continuation_keeps_one_contract_and_hashes_submitted_requests() -> None:
    gateway, capabilities = ToolGateway(), FakeCapabilities()
    plan = one_step_plan(tools=("web.search",))
    original_plan = plan.model_dump_json()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), capability_gateway=capabilities,
    )
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH,
        request="Generate a plugin", token_budget=20_000,
        actor_id=uuid4(), actor_role=Role.OPERATOR,
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2
    checkpoint = await runtime.save_checkpoint()
    models = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    for actual in gateway.requests:
        _assert_stdio_contract(actual)
        assert any(
            state["request_sha256"] == runtime._model_request_sha256(actual)
            for state in models.values()
        )
    assert capabilities.calls == [("writer", "web.search")]
    assert plan.model_dump_json() == original_plan


async def test_crew_compaction_preserves_plugin_sdk_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.models.types import ToolCall
    from agent_hub.runtime.crew.adapter import CrewLLMBridge

    class RepeatingGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            n = len(self.requests)
            response = (
                ModelResponse(text=None, tool_calls=(ToolCall(
                    id=f"call-{n}", name="web.search", arguments={"q": f"query-{n}"},
                ),), usage=TokenUsage(1, 1, 2))
                if n <= 3 else ModelResponse(text="done", usage=TokenUsage(1, 1, 2))
            )
            return replace(completion, response=response)

    class LargeCapabilities(FakeCapabilities):
        async def execute(self, **kwargs: object) -> dict[str, JsonValue]:
            self.calls.append((str(kwargs["actor"]), str(kwargs["name"])))
            return {"items": ("x" * 73_728,)}

    async def two_messages(
        self: FastGeneration, step_id: str, prompt: str, bridge: CrewLLMBridge,
        *, agent_id: str | None = None, storage_scope: tuple[UUID, UUID],
    ) -> str:
        return await bridge.complete([
            {"role": "system", "content": "Assigned writer role"},
            {"role": "user", "content": prompt},
        ])

    monkeypatch.setattr(FastGeneration, "execute", two_messages)
    gateway, capabilities = RepeatingGateway(), LargeCapabilities()
    runtime = CrewDispatchRuntime(
        gateway, one_step_plan(tools=("web.search",)), crew_factory=FastFactory(),
        capability_gateway=capabilities,
    )
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH,
        request="Generate a plugin", token_budget=20_000,
        actor_id=uuid4(), actor_role=Role.OPERATOR,
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(capabilities.calls) == 3
    assert len(gateway.requests) == 4
    assert "EARLIER_INTERACTION_WINDOW_COMPRESSED" in _request_text(gateway.requests[-1])
    for request in gateway.requests:
        _assert_stdio_contract(request)


async def test_direct_workspace_continuation_keeps_one_contract() -> None:
    batches = (
        ({"plugin.json": '{"id":"example"}'}, False, "continue adapter source"),
        ({
            "adapter/main.py": "def main(): return 0\n",
            "tests/test_adapter.py": "def test_adapter(): assert True\n",
            "IMPLEMENTATION_PLAN.md": (
                "Read AGENTS.md, HANDOFF, PROJECT_REQUIREMENTS.md and SKILL.md agent-standard rules."
            ),
            "VERIFICATION.md": "Focused adapter tests passed.\n",
        }, True, ""),
    )
    gateway = SequencedDirectGateway(tuple(
        ModelResponse(text=json.dumps({"workspace_batch": {
            "files": files, "complete": complete, "continuation": continuation,
        }, "summary": "Plugin files."}), usage=TokenUsage(100, 80, 180))
        for files, complete, continuation in batches
    ))
    runtime = DirectRuntime(
        cast(Gateway, gateway), logical_model="main", capability_gateway=RecordingCapabilityGateway(),
    )
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
        request="Build a plugin", token_budget=50_000,
        routing_decision={
            "project_scale": "large", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2
    for actual in gateway.requests:
        _assert_stdio_contract(cast(ModelRequest, actual))
    assert "continue adapter source" in _request_text(cast(ModelRequest, gateway.requests[1]))


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH))
async def test_plugin_guidance_is_in_existing_request_hash_with_project_guidance(
    tmp_path: Path, mode: TaskMode,
) -> None:
    context = (await guidance_task(tmp_path)).model_copy(update={
        "mode": mode, "request": "Create a capability",
    })
    gateway = FakeGateway()
    runtime = (
        DirectRuntime(gateway, logical_model="general")
        if mode is TaskMode.DIRECT
        else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    actual = gateway.requests[0]
    _assert_stdio_contract(actual)
    assert _request_text(actual).count("<PROJECT_GUIDANCE_JSON>") == 1
    injection = next(event for event in events if event.kind == "context.injected")
    assert injection.payload["request_sha256"] == model_request_sha256(actual)
    guidance = plugin_package_generation_guidance(context.request)
    without_guidance = replace(actual, messages=tuple(
        replace(message, content=str(message.content).replace(guidance, ""))
        for message in actual.messages
    ))
    assert model_request_sha256(without_guidance) != injection.payload["request_sha256"]


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH))
async def test_plugin_guidance_counts_toward_prompt_limit_before_gateway_submission(
    monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    from agent_hub.runtime import direct
    from agent_hub.runtime.crew import adapter

    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=mode,
        request="Generate a plugin", token_budget=20_000,
    )
    if mode is TaskMode.DIRECT:
        monkeypatch.setattr(direct, "_context_byte_budget", lambda _: 1_024)
    else:
        monkeypatch.setattr(adapter, "_MAX_PROMPT_BYTES", 1_024)
        monkeypatch.setattr(adapter, "_ABSOLUTE_PROMPT_BYTES", 1_024)
    gateway = FakeGateway()
    runtime = (
        DirectRuntime(gateway, logical_model="general")
        if mode is TaskMode.DIRECT
        else CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    )
    with pytest.raises((RuntimeExecutionError, CrewRuntimeExecutionError), match="limit"):
        _ = [event async for event in runtime.run(context)]
    assert gateway.requests == []
    ordinary = context.model_copy(update={"request": "Use a plugin"})
    events = [event async for event in runtime.run(ordinary)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 1


async def test_hybrid_dispatch_and_direct_children_reuse_shared_stdio_contract() -> None:
    dispatch_gateway, direct_gateway = FakeGateway(), FakeGateway()
    runtime = HybridRuntime(
        CrewDispatchRuntime(dispatch_gateway, one_step_plan(), crew_factory=FastFactory()),
        MultiArtifactRuntime(TaskMode.DISCUSS, ()),
        DirectRuntime(direct_gateway, logical_model="general"),
    )
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
        request="Create a capability", token_budget=20_000,
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(dispatch_gateway.requests) == len(direct_gateway.requests) == 1
    expected = plugin_package_generation_guidance(context.request)
    for actual in (*dispatch_gateway.requests, *direct_gateway.requests):
        _assert_stdio_contract(actual)
        assert expected in _request_text(actual)


def test_crew_reentry_deduplicates_only_its_exact_system_contract() -> None:
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH,
        request="Generate a plugin",
    )
    guidance = plugin_package_generation_guidance(context.request)
    original = [
        {"role": "user", "content": "Quoted marker: " + _MARKER},
        {"role": "user", "content": guidance},
    ]
    first = CrewDispatchRuntime._guidance_messages(context, original)
    second = CrewDispatchRuntime._guidance_messages(context, [
        {"role": message.role, "content": message.content} for message in first
    ])
    assert sum(message.role == "system" and guidance in str(message.content) for message in first) == 1
    assert second == first
    assert sum(message.role == "user" and message.content == guidance for message in second) == 1


async def test_crew_bridge_reentry_preserves_user_quotes_and_one_trusted_system_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.crew.adapter import CrewLLMBridge

    guidance = plugin_package_generation_guidance("Generate a plugin")

    async def reenter(
        self: FastGeneration, step_id: str, prompt: str, bridge: CrewLLMBridge,
        *, agent_id: str | None = None, storage_scope: tuple[object, object],
    ) -> str:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "system", "content": guidance},
            {"role": "user", "content": guidance},
        ]
        await bridge.complete(messages)
        return await bridge.complete(messages)

    monkeypatch.setattr(FastGeneration, "execute", reenter)
    requests = await _capture("Generate a plugin. Quoted marker: " + _MARKER, TaskMode.DISPATCH)
    assert len(requests) == 2
    for actual in requests:
        assert sum(
            message.role == "system" and guidance in str(message.content)
            for message in actual.messages
        ) == 1
        assert sum(
            message.role == "user" and message.content == guidance
            for message in actual.messages
        ) == 1


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH))
async def test_user_quoted_marker_cannot_suppress_generation_knowledge(mode: TaskMode) -> None:
    guidance = plugin_package_generation_guidance("Create a capability")
    requests = await _capture("Create a capability. Quoted marker: " + _MARKER, mode)
    for actual in requests:
        assert _request_text(actual).count(guidance) == 1
