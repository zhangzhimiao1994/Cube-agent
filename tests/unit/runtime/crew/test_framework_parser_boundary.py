"""Offline incremental contract runtime/generation boundary regression.

Only owned fixture data is used. CrewAI parser functions are loaded from the
installed distribution without importing its telemetry/storage package main.
No actual provider, generated project or external resource is invoked.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import http.client
import importlib.metadata
import json
import re
import socket
import sys
import types
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

import pytest
from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, StructuredResponseSchema, TokenUsage
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import DispatchStep

SCOPE = (
    UUID("00000000-0000-4000-8000-000000000001"),
    UUID("85355427-8c08-45d2-9ac5-95637d4cadf5"),
)
SCHEMA = StructuredResponseSchema(name="OwnedParserBoundary", schema={"type": "object"})


def _receipt_tokens(receipt: GatewayCompletion) -> int:
    usage = receipt.response.usage
    assert usage is not None
    return usage.total_tokens


def _receipt_cost(receipt: GatewayCompletion) -> Decimal:
    cost = receipt.cost_usd
    assert cost is not None
    return cost


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch, _function_scoped_runner: asyncio.Runner) -> None:
    # Windows asyncio creates its self-wakeup socketpair before traffic is blocked.
    _function_scoped_runner.get_loop()

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("parser fixture must never dispatch network traffic")

    for name in (
        "CREWAI_DISABLE_TELEMETRY",
        "CREWAI_DISABLE_TRACKING",
        "CREWAI_TESTING",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", denied)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", denied)

    @contextmanager
    def memory_scope(path: Path) -> Iterator[None]:
        storage_token = adapter._CREWAI_STORAGE_CONTEXT.set(path)
        trace_token = adapter._CREWAI_TRACE_DISABLED.set(True)
        telemetry_token = adapter._CREWAI_TELEMETRY_DISABLED.set(True)
        try:
            yield
        finally:
            adapter._CREWAI_TELEMETRY_DISABLED.reset(telemetry_token)
            adapter._CREWAI_TRACE_DISABLED.reset(trace_token)
            adapter._CREWAI_STORAGE_CONTEXT.reset(storage_token)

    monkeypatch.setattr(adapter, "_active_crewai_scope", memory_scope)


@pytest.fixture
def installed_parser(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    distribution = importlib.metadata.distribution("crewai")
    root = Path(str(distribution.locate_file("crewai")))
    module = types.ModuleType("_owned_installed_parser_boundary")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    module.__dict__.update(
        {
            "dataclass": dataclasses.dataclass,
            "BaseModel": BaseModel,
            "repair_json": repair_json,
            "re": re,
            "Final": Final,
            # Only parser exception prose uses i18n; format_answer catches that prose.
            "_I18N": types.SimpleNamespace(slice=lambda name: "owned parser format error"),
        }
    )
    constants_path = root / "agents/constants.py"
    constants = ast.parse(constants_path.read_bytes())
    constants.body = [node for node in constants.body if isinstance(node, ast.AnnAssign)]
    exec(compile(constants, str(constants_path), "exec"), module.__dict__)  # noqa: S102 - Installed local definitions only.
    parser_path = root / "agents/parser.py"
    parser = ast.parse(parser_path.read_bytes())
    parser.body = [node for node in parser.body if isinstance(node, ast.ClassDef | ast.FunctionDef)]
    exec(compile(parser, str(parser_path), "exec"), module.__dict__)  # noqa: S102 - Installed local definitions only.
    utilities_path = root / "utilities/agent_utils.py"
    utilities = ast.parse(utilities_path.read_bytes())
    utilities.body = [
        node
        for node in utilities.body
        if isinstance(node, ast.FunctionDef) and node.name == "format_answer"
    ]
    assert len(utilities.body) == 1, "installed format_answer is required, never a stub fallback"
    exec(compile(utilities, str(utilities_path), "exec"), module.__dict__)  # noqa: S102 - Installed local format_answer only.
    return module


class FixtureLLM(Protocol):
    async def acall(self, messages: list[dict[str, str]], *, tools: None) -> str: ...


class ParserCrewObjects:
    """Fake Crew objects; actual private generation and installed parser execute."""

    def __init__(self, parser: types.ModuleType, framework_raw: str | None = None) -> None:
        self.parser_calls = 0
        fixture = self

        class BaseLLM:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class Agent:
            tools: list[object]
            llm: FixtureLLM

            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class Task:
            tools: list[object]
            description: str

            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        class Crew:
            agents: list[Agent]
            tasks: list[Task]

            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

            async def akickoff(self, inputs: dict[str, object]) -> object:
                assert inputs == {}
                agent, task = self.agents[0], self.tasks[0]
                assert agent.tools == [] and task.tools == []
                # Calls the GatewayOnlyLLM.acall implementation in _CrewAIGeneration.
                answer = await agent.llm.acall(
                    [{"role": "user", "content": task.description}],
                    tools=None,
                )
                response_model = (
                    getattr(task, "response_model", None)
                    or getattr(task, "output_pydantic", None)
                    or getattr(task, "output_json", None)
                )
                formatted = None
                if response_model is not None:
                    try:
                        if isinstance(answer, BaseModel):
                            formatted = parser.AgentFinish("", answer, answer.model_dump_json())
                        else:
                            response_model.model_validate_json(answer)
                            formatted = parser.AgentFinish("", answer, answer)
                    except ValidationError:
                        formatted = None
                if formatted is None:
                    fixture.parser_calls += 1
                    formatted = parser.format_answer(answer)
                if not isinstance(formatted, parser.AgentFinish):
                    raise adapter.RuntimeExecutionError(
                        "owned JSON was interpreted as a framework action"
                    )
                raw = formatted.output
                if isinstance(raw, BaseModel):
                    raw = raw.model_dump_json()
                return types.SimpleNamespace(raw=raw if framework_raw is None else framework_raw)

        self.module = types.SimpleNamespace(
            BaseLLM=BaseLLM,
            Agent=Agent,
            Task=Task,
            Crew=Crew,
            Process=types.SimpleNamespace(sequential="sequential"),
        )


async def _incremental_case(
    installed_parser: types.ModuleType,
) -> None:
    from agent_hub.models.types import ToolCall
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
    from agent_hub.runtime.crew.plan import DispatchPlan, DispatchStep
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        FakeCapabilities,
        WorkspaceDeliverySequenceHarness,
        _structured_dependent_final_plan,
        _workspace_delivery_sequence_context,
    )

    tools = ("workspace.write_text", "workspace.bundle")
    agent = (
        _structured_dependent_final_plan()
        .agents[0]
        .model_copy(
            update={
                "id": "implementer",
                "allowed_tools": tools,
            }
        )
    )
    plan = DispatchPlan(
        agents=(agent,),
        steps=(
            DispatchStep(
                id="implementer_step",
                agent="implementer",
                task=(
                    "Build the owned project incrementally.\n"
                    "Project workspace delivery contract: produce complete workspace files "
                    "and a downloadable bundle."
                ),
                tools=tools,
                final_synthesizer=True,
                token_budget=10_000,
                cost_budget_usd=Decimal(1),
                tool_argument_budget_bytes={"workspace.write_text": 512_000},
            ),
        ),
        allowed_tools=tools,
        total_token_budget=10_000,
        total_cost_usd=Decimal(1),
    )
    assert agent.output_schema
    assert adapter._is_incremental_workspace_contract_step(plan.steps[0])
    assert not adapter._is_project_scale_tool_contract_step(plan.steps[0])

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []
            self.receipts: list[GatewayCompletion] = []
            self.responses = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="owned-write",
                            name="workspace.write_text",
                            arguments={"path": "owned.txt", "content": "owned fixture content"},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="owned-bundle",
                            name="workspace.bundle",
                            arguments={"title": "Owned fixture"},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(text="Workspace bundle delivered.", usage=TokenUsage(118, 11, 129)),
            )

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            assert len(self.requests) <= 3, "handoff/restore must not rebill"
            assert request.response_schema is None
            receipt = GatewayCompletion(
                response=self.responses[len(self.requests) - 1],
                deployment_id="owned-deployment",
                logical_model=request.logical_model,
                provider_id="owned-provider",
                provider_model="owned-provider/owned-model",
                cost_usd=Decimal("0.001"),
            )
            self.receipts.append(receipt)
            return receipt

    class Capabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name in tools

    objects = ParserCrewObjects(installed_parser)

    class Factory(adapter.CrewObjectFactory):
        def build(
            self,
            agents: tuple[adapter.CrewAgentDefinition, ...],
            tasks: tuple[adapter.CrewTaskDefinition, ...],
            *,
            share_crew: bool,
            telemetry_disabled: bool,
        ) -> adapter.CrewStepGeneration:
            assert telemetry_disabled
            return adapter._CrewAIGeneration(
                objects.module,
                agents,
                tasks,
                Path.cwd() / ".owned-parser-fixture-not-created",
            )

    gateway, harness = Gateway(), WorkspaceDeliverySequenceHarness()
    repository = InMemoryArtifactRepository()

    def make_runtime() -> adapter.CrewDispatchRuntime:
        return adapter.CrewDispatchRuntime(
            gateway,
            plan,
            capability_gateway=Capabilities(),
            harness_tool_gateway=harness,
            crew_factory=Factory(),
            artifact_repository=repository,
        )

    context = _workspace_delivery_sequence_context()
    runtime = make_runtime()
    events = []
    failure = None
    try:
        async for event in runtime.run(context):
            events.append(event)
    except adapter.RuntimeExecutionError as exc:
        failure = exc
    checkpoint = await runtime.save_checkpoint()
    assert [call.tool_name for call in harness.calls] == list(tools), str(failure)
    assert harness.files == {"owned.txt": "owned fixture content"}
    assert len(gateway.requests) == 3
    assert objects.parser_calls == 1
    assert gateway.receipts[-1].response is gateway.responses[-1]
    assert sum(_receipt_tokens(receipt) for receipt in gateway.receipts) == 133
    assert gateway.receipts[-1].response.text == "Workspace bundle delivered."
    assert checkpoint.state["usage"] == {"tokens": 133, "cost_usd": "0.003"}
    model_artifacts = [
        event.artifact
        for event in events
        if event.artifact is not None and event.artifact.type == "model_response"
    ]
    assert len(model_artifacts) == 3
    for artifact in model_artifacts:
        assert artifact.provenance is not None
        assert artifact.provenance.deployment_id == "owned-deployment"
        assert artifact.provenance.provider_id == "owned-provider"
        assert artifact.provenance.provider_model == "owned-provider/owned-model"
    assert failure is None, f"incremental plaintext must reach structured handoff: {failure}"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"
    serialized = json.loads(json.dumps(checkpoint.to_payload()))
    restored_checkpoint = RuntimeCheckpoint.from_payload(serialized)
    restored = make_runtime()
    await restored.restore_checkpoint(restored_checkpoint)
    resumed = [
        event
        async for event in restored.run(
            context.model_copy(update={"checkpoint": restored_checkpoint}),
        )
    ]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3 and len(harness.calls) == 2
    assert objects.parser_calls == 1
    assert not any(
        event.kind in {EventKind.MODEL_STARTED, EventKind.TOOL_STARTED} for event in resumed
    )
    assert sum(_receipt_tokens(receipt) for receipt in gateway.receipts) == 133
    assert sum((_receipt_cost(receipt) for receipt in gateway.receipts), Decimal(0)) == Decimal(
        "0.003"
    )
    assert restored_checkpoint.state["usage"] == checkpoint.state["usage"]


@pytest.mark.asyncio
async def test_incremental_plaintext_completion_survives_structured_handoff_and_restore(
    installed_parser: types.ModuleType,
) -> None:
    await _incremental_case(installed_parser)


@pytest.mark.asyncio
async def test_owned_old_predicate_reproduces_incremental_framework_mismatch(
    installed_parser: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def old_check(step: DispatchStep, completion: GatewayCompletion) -> bool:
        return not adapter._is_project_scale_recovery_completion(
            completion
        ) and not adapter._is_project_scale_tool_contract_step(step)

    monkeypatch.setattr(adapter, "_should_check_framework_raw", old_check)
    with pytest.raises(AssertionError, match="incremental plaintext.*framework output mismatch"):
        await _incremental_case(installed_parser)


@pytest.mark.parametrize(
    "actual,altered",
    [
        ('{"summary":"provider facts"}', '{"summary":"changed facts"}'),
        ('{"value":true}', '{"value":1}'),
        ('{"value":1}', '{"value":1.0}'),
    ],
)
def test_ordinary_framework_semantic_and_type_mismatch_remains_rejected(
    actual: str,
    altered: str,
) -> None:
    with pytest.raises(adapter.RuntimeExecutionError, match="framework output mismatch"):
        adapter._check_framework_raw(SCHEMA, actual, altered)
