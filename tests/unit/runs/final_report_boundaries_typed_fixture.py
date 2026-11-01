"""Typed copies of the existing final-report fixture factories, using real runtimes.

Provider responses and the capability executor remain the original offline doubles.
Checkpoint publication, validation, artifact storage and SQL transactions are real.
"""
from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import DispatchStep
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.runtime.autogen.project_repair_phase_typed_fixture import Evidence
from tests.unit.runtime.autogen.test_engineering_completion import Capabilities, Gateway, plan
from tests.unit.runtime.autogen.test_engineering_completion import context as discuss_context
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
    _workspace_delivery_write_response,
)
from tests.unit.runtime.crew.test_engineering_delivery_execution import (
    FINAL,
    ActualCapabilityHarness,
    engineering_runtime,
    verify_response,
)
from tests.unit.runtime.crew.test_framework_parser_boundary import ParserCrewObjects
from tests.unit.runtime.test_direct_generated_entry import Harness
from tests.unit.runtime.test_direct_project_verification import context as direct_context

SQLCase = tuple[RunRepository, async_sessionmaker[AsyncSession], AsyncEngine]
REPORT = "已交付可运行的项目及 ZIP。\n\n验证：实际运行检查已通过。使用方法见 README；未验证部署。"
CREW_REPORT = "已交付 ZIP 附件；安装、构建和测试通过。请解压使用，并反馈实际体验。"
DISCUSS_REPORT = "OWNED_DISCUSS_NATIVE_REPORT: The verified archive is attached."
OWNER = "delivery_main_agent"


class GeneratedHarness:
    """Delegate the existing harness unchanged, adapting its legacy keyword name."""

    def __init__(self, original: Harness) -> None:
        self.original = original

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        return await self.original.invoke(tenant_id, request, user_id=user_id, role=role)


def responses(text: str = CREW_REPORT) -> tuple[ModelResponse, ...]:
    return (_workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
            verify_response(), _workspace_delivery_bundle_response(), FINAL,
            ModelResponse(text=text, usage=TokenUsage(1, 1, 2)))

def subject(model: WorkspaceDeliverySequenceGateway, harness: ActualCapabilityHarness, repository: InMemoryArtifactRepository, parser: types.ModuleType,
            *, altered: str | None = None, engineering: bool = True,
            repeat_final: bool = False, strip_prompt: bool = False,
            preflight: bool = False) -> adapter.CrewDispatchRuntime:
    runner = engineering_runtime(model, harness, repository)
    worker = runner._plan.steps[0].model_copy(update={
        "final_synthesizer": False,
        "engineering_delivery_contract_version": 1 if engineering else 0,
        "token_budget": 9000,
    })
    worker_agent = runner._plan.agents[0].model_copy(update={"output_schema": {"summary": "string"}})
    reporter = runner._plan.agents[0].model_copy(update={"id": "reporter", "allowed_tools": ()})
    runner._plan = runner._plan.model_copy(update={
        "agents": (worker_agent, reporter),
        "steps": (worker, DispatchStep(id="report", agent="reporter", task="Explain the delivery",
                                      depends_on=(worker.id,), final_synthesizer=True,
                                      token_budget=1000)),
    })
    if preflight:
        preparation = DispatchStep(id="preflight", agent=worker.agent, task="Prepare a design note",
                                   tools=("workspace.write_text",), token_budget=500)
        runner._plan = runner._plan.model_copy(update={"steps": (
            preparation, worker.model_copy(update={"depends_on": (preparation.id,)}),
            runner._plan.steps[-1],
        )})
    objects = ParserCrewObjects(parser)

    class Generation(adapter._CrewAIGeneration):
        async def execute(self, step_id: str, prompt: str, bridge: adapter.CrewLLMBridge, *,
                          agent_id: str | None = None, storage_scope: tuple[UUID, UUID]) -> str:
            if step_id == "report" and strip_prompt:
                prompt = "Summarize without upstream context"
            raw = await super().execute(step_id, prompt, bridge, agent_id=agent_id, storage_scope=storage_scope)
            if step_id == "report" and repeat_final:
                raw = await super().execute(step_id, prompt, bridge, agent_id=agent_id, storage_scope=storage_scope)
            return altered if step_id == "report" and altered is not None else raw

    class Factory:
        def build(self, agents: tuple[adapter.CrewAgentDefinition, ...],
                  tasks: tuple[adapter.CrewTaskDefinition, ...], *,
                  share_crew: bool, telemetry_disabled: bool) -> adapter.CrewStepGeneration:
            return Generation(objects.module, agents, tasks, Path(".unused-report-fixture"))

    runner._factory = Factory()
    return runner

def context() -> TaskContext:
    base = _workspace_delivery_sequence_context()
    return base.model_copy(update={"tenant_id": TENANT, "run_id": RUN,
        "routing_decision": {**base.routing_decision, "engineering_dispatch_report_version": 1}})

def report_context() -> TaskContext:
    ctx = direct_context()
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "engineering_direct_report_version": 1,
    }})

def replace_model(evidence: Evidence, changes: Mapping[str, JsonValue] | None = None,
                  *, producer: str = "agent_hub", kind: str = "model_result") -> None:
    old = evidence.artifacts[-1]
    model = Artifact(id=old.id, type=kind, producer=producer,
                     content={**old.content, **(changes or {})})
    evidence.artifacts[-1] = model
    evidence.models[-1]["artifact_sha256"] = model.content_sha256

@dataclass
class DiscussCase:
    task: TaskContext
    runtime: AutoGenDiscussionRuntime
    backend: Capabilities
    gateway: Gateway
    events: tuple[RunEvent, ...]

    @property
    def report_index(self) -> int:
        return next(i for i, event in enumerate(self.events)
                    if event.artifact is not None and event.artifact.producer == OWNER)

    @property
    def main_checkpoint(self) -> RuntimeCheckpoint:
        checkpoint = self.events[self.report_index + 1].checkpoint
        assert checkpoint is not None and checkpoint.state["terminal"] is False
        return checkpoint

async def make_case() -> DiscussCase:
    gateway = Gateway(["[COMPLETE] premature", "workspace.write_text", "written",
                       "project.verify", "checked", "workspace.bundle", DISCUSS_REPORT])
    backend = Capabilities()
    runtime = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=backend)
    task = discuss_context()
    task = task.model_copy(update={"routing_decision": {
        **task.routing_decision, "engineering_final_report_version": 1,
    }})
    events = tuple([event async for event in runtime.run(task)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == ["workspace.write_text", "project.verify", "workspace.bundle"]
    return DiscussCase(task, runtime, backend, gateway, events)
