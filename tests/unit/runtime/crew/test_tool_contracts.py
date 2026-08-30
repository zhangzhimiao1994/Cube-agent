from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewTaskDefinition,
    _tool_definitions,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep

RUN_ID = UUID("00000000-0000-4000-8000-000000000121")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000122")


class BadProjectZipGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id="provider-call",
                        name="project_generate_zip",
                        arguments=cast(
                            Mapping[str, JsonValue],
                            {
                                "archive_name": "hello-world.zip",
                                "files": {"main": ["print('hello world')"]},
                            },
                        ),
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class RepeatingProjectZipGateway(BadProjectZipGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id=f"provider-call-{len(self.requests)}",
                        name="project_generate_zip",
                        arguments={
                            "title": "Hello World",
                            "filename": "hello-world.zip",
                            "presentation": "final_attachment",
                            "files": {"main.py": "print('hello world')\n"},
                        },
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class TextOnlyGateway(BadProjectZipGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(
                text="I prepared the requested file.",
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class TextThenToolGateway(BadProjectZipGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if len(self.requests) == 1:
            response = ModelResponse(
                text="I prepared the requested file.",
                usage=TokenUsage(1, 1, 2),
            )
        else:
            assert request.tools
            response = ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id="provider-call",
                        name=request.tools[0].name,
                        arguments={
                            "title": "Acceptance File",
                            "filename": "acceptance-file.zip",
                            "presentation": "final_attachment",
                            "files": {"main.py": "print('hello world')\n"},
                        },
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class FailingProjectZipCapabilities:
    async def execute(  # type: ignore[no-untyped-def]
        self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
    ):
        del tenant_id, run_id, actor, name, arguments, idempotency_key
        raise RuntimeError("file contents must be strings")

    def is_replay_safe(self, name: str) -> bool:
        return name == "project.generate_zip"


class SuccessfulProjectZipCapabilities:
    async def execute(  # type: ignore[no-untyped-def]
        self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
    ):
        del tenant_id, run_id, actor, name, arguments, idempotency_key
        return {
            "artifact_id": "00000000-0000-4000-8000-000000000123",
            "file": {
                "artifact_id": "00000000-0000-4000-8000-000000000123",
                "filename": "hello-world.zip",
                "mime_type": "application/zip",
                "size_bytes": 128,
                "sha256": "a" * 64,
                "storage_key": "tenant/run/artifact/hello-world.zip",
            },
            "summary": "Generated project ZIP artifact hello-world.zip.",
            "presentation": "final_attachment",
        }

    def is_replay_safe(self, name: str) -> bool:
        return name == "project.generate_zip"


class FastGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, agent_id, storage_scope
        return await bridge.complete([{"role": "system", "content": prompt}])


class FastFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FastGeneration:
        del agents, tasks
        assert share_crew is False
        assert telemetry_disabled is True
        return FastGeneration()


def _one_step_plan(*, tools: tuple[str, ...]) -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=tools,
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=tools,
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=tools,
        total_token_budget=100,
    )


def _context() -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Generate a downloadable hello-world Python project zip",
        token_budget=1000,
    )


def test_project_zip_tool_definition_exposes_strict_file_contract() -> None:
    tool = _tool_definitions(("project.generate_zip",))[0]

    assert tool.name == "project_generate_zip"
    assert "project.generate_zip" in tool.description
    assert "files" in tool.description
    assert tool.parameters["type"] == "object"
    required = tool.parameters["required"]
    assert isinstance(required, tuple)
    assert set(required) == {"title", "files"}
    properties = tool.parameters["properties"]
    assert isinstance(properties, Mapping)
    assert properties["files"] == {
        "type": "object",
        "description": (
            "Project files keyed by safe relative path. Each value is UTF-8 text "
            "content for that file."
        ),
        "additionalProperties": {"type": "string"},
        "minProperties": 1,
        "maxProperties": 64,
    }
    assert tool.parameters["additionalProperties"] is False


def test_document_tool_definition_exposes_strict_downloadable_contract() -> None:
    tool = _tool_definitions(("document.generate_docx",))[0]

    assert tool.name == "document_generate_docx"
    assert "document.generate_docx" in tool.description
    assert "downloadable DOCX" in tool.description
    assert tool.parameters["type"] == "object"
    assert tool.parameters["additionalProperties"] is False
    required = tool.parameters["required"]
    assert isinstance(required, tuple)
    assert required == ("title",)
    properties = tool.parameters["properties"]
    assert isinstance(properties, Mapping)
    assert set(properties) == {"title", "subtitle", "filename", "presentation", "sections"}
    assert properties["presentation"] == {
        "type": "string",
        "enum": ("step_detail", "final_attachment"),
        "description": "Use final_attachment when the DOCX is the final downloadable file.",
    }
    assert properties["sections"] == {
        "type": "array",
        "description": "Optional ordered document sections.",
        "items": {"type": "object", "additionalProperties": True},
    }


def test_presentation_tool_definition_exposes_strict_downloadable_contract() -> None:
    tool = _tool_definitions(("presentation.generate_pptx",))[0]

    assert tool.name == "presentation_generate_pptx"
    assert "presentation.generate_pptx" in tool.description
    assert "downloadable PPTX" in tool.description
    assert tool.parameters["type"] == "object"
    assert tool.parameters["additionalProperties"] is False
    required = tool.parameters["required"]
    assert isinstance(required, tuple)
    assert required == ("title",)
    properties = tool.parameters["properties"]
    assert isinstance(properties, Mapping)
    assert set(properties) == {
        "title",
        "subtitle",
        "filename",
        "template_id",
        "presentation",
        "slides",
    }
    assert properties["presentation"] == {
        "type": "string",
        "enum": ("step_detail", "final_attachment"),
        "description": "Use final_attachment when the PPTX is the final downloadable file.",
    }
    assert properties["slides"] == {
        "type": "array",
        "description": "Optional ordered slide definitions.",
        "items": {"type": "object", "additionalProperties": True},
    }


async def test_capability_failure_event_keeps_safe_tool_error_summary() -> None:
    runtime = CrewDispatchRuntime(
        BadProjectZipGateway(),
        _one_step_plan(tools=("project.generate_zip",)),
        capability_gateway=FailingProjectZipCapabilities(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    reason = "capability failed: file contents must be strings"

    with pytest.raises(CapabilityOutcomeUncertain, match="file contents must be strings"):
        async for event in runtime.run(_context()):
            events.append(event)

    failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed.reason == reason
    assert failed.payload["error_summary"] == reason
    assert failed.payload["error_stage"] == "capability"
    assert failed.payload["error_code"] == "capability.execution_failed"


async def test_final_attachment_tool_result_completes_without_extra_tool_rounds() -> None:
    gateway = RepeatingProjectZipGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan(tools=("project.generate_zip",)),
        capability_gateway=SuccessfulProjectZipCapabilities(),
        crew_factory=FastFactory(),
    )

    events = [event async for event in runtime.run(_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [event.kind for event in events if str(event.kind).startswith("tool.")] == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
    ]
    assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    "tool_name",
    ("document.generate_docx", "presentation.generate_pptx", "project.generate_zip"),
)
async def test_delivery_tool_step_rejects_text_only_model_response(tool_name: str) -> None:
    gateway = TextOnlyGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan(tools=(tool_name,)),
        capability_gateway=SuccessfulProjectZipCapabilities(),
        crew_factory=FastFactory(),
    )

    with pytest.raises(CapabilityOutcomeUncertain, match="required final attachment"):
        async for _event in runtime.run(_context()):
            pass

    assert len(gateway.requests) > 1


async def test_delivery_tool_step_recovers_after_text_only_response() -> None:
    gateway = TextThenToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan(tools=("presentation.generate_pptx",)),
        capability_gateway=SuccessfulProjectZipCapabilities(),
        crew_factory=FastFactory(),
    )

    events = [event async for event in runtime.run(_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2
    assert any(event.kind is EventKind.TOOL_COMPLETED for event in events)
