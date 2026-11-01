"""Offline real Crew generation/parser and capability delivery report regressions."""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import DispatchStep
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.runtime.crew import test_framework_parser_boundary as parser_fixtures
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
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
from tests.unit.runtime.delivery_report_typed_fixture import json_map, mapping, mutable, string

installed_parser = parser_fixtures.installed_parser
offline_only = parser_fixtures.offline_only

REPORT = "已交付 ZIP 附件；安装、构建和测试通过。请解压使用，并反馈实际体验。"


async def test_report_uses_causal_final_writer_instead_of_plan_array_order(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    first = responses()[:-1]
    second = (
        _workspace_delivery_write_response("index.js", "module.exports = 43;", "write2"),
        verify_response(),
        _workspace_delivery_bundle_response(),
        FINAL,
    )
    model = WorkspaceDeliverySequenceGateway((*first, *second, responses()[-1]))
    runner = subject(
        model,
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
    )
    original, final = runner._plan.steps
    later = original.model_copy(update={"id": "later_writer", "depends_on": (original.id,)})
    final = final.model_copy(update={"depends_on": (later.id,)})
    runner._plan = runner._plan.model_copy(update={"steps": (later, original, final)})
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 9


def responses(text: str = REPORT) -> tuple[ModelResponse, ...]:
    return (
        _workspace_delivery_write_response("index.js", "module.exports = 42;", "write"),
        verify_response(),
        _workspace_delivery_bundle_response(),
        FINAL,
        ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
    )


def subject(
    model: WorkspaceDeliverySequenceGateway,
    harness: WorkspaceDeliverySequenceHarness,
    repository: InMemoryArtifactRepository,
    parser: types.ModuleType,
    *,
    altered: str | None = None,
    engineering: bool = True,
    repeat_final: bool = False,
    strip_prompt: bool = False,
    preflight: bool = False,
) -> adapter.CrewDispatchRuntime:
    runner = engineering_runtime(model, harness, repository)
    worker = runner._plan.steps[0].model_copy(
        update={
            "final_synthesizer": False,
            "engineering_delivery_contract_version": 1 if engineering else 0,
            "token_budget": 9000,
        }
    )
    worker_agent = runner._plan.agents[0].model_copy(
        update={"output_schema": {"summary": "string"}}
    )
    reporter = runner._plan.agents[0].model_copy(update={"id": "reporter", "allowed_tools": ()})
    runner._plan = runner._plan.model_copy(
        update={
            "agents": (worker_agent, reporter),
            "steps": (
                worker,
                DispatchStep(
                    id="report",
                    agent="reporter",
                    task="Explain the delivery",
                    depends_on=(worker.id,),
                    final_synthesizer=True,
                    token_budget=1000,
                ),
            ),
        }
    )
    if preflight:
        preparation = DispatchStep(
            id="preflight",
            agent=worker.agent,
            task="Prepare a design note",
            tools=("workspace.write_text",),
            token_budget=500,
        )
        runner._plan = runner._plan.model_copy(
            update={
                "steps": (
                    preparation,
                    worker.model_copy(update={"depends_on": (preparation.id,)}),
                    runner._plan.steps[-1],
                )
            }
        )
    objects = ParserCrewObjects(parser)

    class Generation(adapter._CrewAIGeneration):
        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: adapter.CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            if step_id == "report" and strip_prompt:
                prompt = "Summarize without upstream context"
            raw = await super().execute(
                step_id, prompt, bridge, agent_id=agent_id, storage_scope=storage_scope
            )
            if step_id == "report" and repeat_final:
                raw = await super().execute(
                    step_id, prompt, bridge, agent_id=agent_id, storage_scope=storage_scope
                )
            return altered if step_id == "report" and altered is not None else raw

    class Factory:
        def build(
            self,
            agents: tuple[adapter.CrewAgentDefinition, ...],
            tasks: tuple[adapter.CrewTaskDefinition, ...],
            *,
            share_crew: bool,
            telemetry_disabled: bool,
        ) -> adapter.CrewStepGeneration:
            return Generation(objects.module, agents, tasks, Path(".unused-report-fixture"))

    runner._factory = Factory()
    return runner


def context() -> TaskContext:
    base = _workspace_delivery_sequence_context()
    return base.model_copy(
        update={
            "tenant_id": TENANT,
            "run_id": RUN,
            "routing_decision": {**base.routing_decision, "engineering_dispatch_report_version": 1},
        }
    )


async def test_report_reads_verified_bundle_and_replays_without_rebilling(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    model = WorkspaceDeliverySequenceGateway(responses())
    runner = subject(model, harness, repository, installed_parser)
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    report = next(
        event.artifact
        for event in events
        if event.artifact is not None
        and event.artifact.producer == "reporter"
        and event.artifact.type == "text"
    )
    assert report.content["text"] == REPORT
    assert set(report.content) == {"text", "delivery_report_binding"}
    from agent_hub.runtime.crew.delivery_report import final_evidence

    current = tuple(event.artifact for event in events if event.artifact is not None)
    facts = final_evidence(report, context(), current)
    assert mapping(mapping(facts["verification"])["verification"])["status"] == "passed"
    assert mapping(facts["bundle"])["presentation"] == "final_attachment"
    binding = report.content["delivery_report_binding"]
    assert isinstance(binding, Mapping)
    with pytest.raises(ValueError, match="delivery report"):
        final_evidence(
            report,
            context(),
            tuple(item for item in current if str(item.id) != binding["verification_artifact_id"]),
        )
    with pytest.raises(ValueError, match="delivery report"):
        final_evidence(report, context().model_copy(update={"run_id": UUID(int=3)}), current)
    assert len(model.requests) == 5 and not model.requests[-1].tools
    prompt = "\n".join(str(message.content) for message in model.requests[-1].messages)
    assert "verification_receipt_sha256" in prompt and "source_sha256" in prompt
    assert '"filename"' in prompt and ".zip" in prompt and "install" in prompt and "test" in prompt
    saved = await runner.save_checkpoint()
    checkpoint = RuntimeCheckpoint.from_payload(json.loads(json.dumps(saved.to_payload())))
    empty = WorkspaceDeliverySequenceGateway(())
    restored = subject(empty, harness, repository, installed_parser)
    await restored.restore_checkpoint(checkpoint)
    replay = [
        event
        async for event in restored.run(context().model_copy(update={"checkpoint": checkpoint}))
    ]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not empty.requests and len(harness.calls) == 3
    assert (await restored.save_checkpoint()).state["usage"] == saved.state["usage"]


@pytest.mark.parametrize("altered", ("框架伪造的完成报告", '{"summary":"internal JSON"}'))
async def test_report_cannot_substitute_native_text(
    tmp_path: Path,
    installed_parser: types.ModuleType,
    altered: str,
) -> None:
    model = WorkspaceDeliverySequenceGateway(responses())
    runner = subject(
        model,
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
        altered=altered,
    )
    events = []
    with pytest.raises(adapter.RuntimeExecutionError, match="delivery report"):
        async for event in runner.run(context()):
            events.append(event)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(
        event.artifact is not None
        and event.artifact.producer == "reporter"
        and event.artifact.type == "text"
        for event in events
    )


@pytest.mark.parametrize("mutation", ("text", "bundle", "verification"))
async def test_completed_report_rejects_rehashed_evidence_substitution(
    tmp_path: Path,
    installed_parser: types.ModuleType,
    mutation: str,
) -> None:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runner = subject(
        WorkspaceDeliverySequenceGateway(responses()), harness, repository, installed_parser
    )
    _events = [event async for event in runner.run(context())]
    saved = await runner.save_checkpoint()
    row = mapping(mapping(saved.state["artifact_refs"])["report"])
    assert isinstance(row, Mapping)
    if mutation != "text":
        row = next(
            mapping(item)
            for item in mapping(saved.state["tools"]).values()
            if mapping(item)["name"]
            == ("workspace.bundle" if mutation == "bundle" else "project.verify")
        )
        row = {"id": row["artifact_id"], "sha256": row["sha256"]}
    artifact = repository._artifacts[TENANT, RUN, UUID(string(row["id"]))]
    content = (
        {**artifact.content, "text": "伪造"}
        if mutation == "text"
        else {
            **artifact.content,
            "result": {**mapping(artifact.content["result"]), "summary": "changed native outcome"},
        }
    )
    changed = Artifact(
        id=artifact.id,
        version=artifact.version,
        type=artifact.type,
        producer=artifact.producer,
        source_ids=artifact.source_ids,
        provenance=artifact.provenance,
        content=json_map(content),
    )
    # Simulate internally consistent corrupted durable storage, bypassing its write-once guard.
    repository._artifacts[TENANT, RUN, changed.id] = changed
    payload = saved.to_payload()
    if mutation == "text":
        mutable(mutable(mutable(payload["state"])["artifact_refs"])["report"])["sha256"] = (
            changed.content_sha256
        )
    else:
        for tool in mutable(mutable(payload["state"])["tools"]).values():
            if mutable(tool)["artifact_id"] == str(changed.id):
                mutable(tool)["sha256"] = changed.content_sha256
    mutable(mutable(payload["state"])["artifact_registry"])[str(changed.id)] = (
        changed.content_sha256
    )
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    empty = WorkspaceDeliverySequenceGateway(())
    restored = subject(empty, harness, repository, installed_parser)
    with pytest.raises(adapter.RuntimeExecutionError, match="delivery report"):
        await restored.restore_checkpoint(checkpoint)
        _events = [
            event
            async for event in restored.run(context().model_copy(update={"checkpoint": checkpoint}))
        ]
    assert not empty.requests and len(harness.calls) == 3


async def test_final_empty_model_fails_without_retry_or_internal_fallback(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    model = WorkspaceDeliverySequenceGateway(responses(""))
    runner = subject(
        model,
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
    )
    events = []
    with pytest.raises(adapter.RuntimeExecutionError):
        async for event in runner.run(context()):
            events.append(event)
    assert len(model.requests) == 5
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(
        event.artifact is not None
        and event.artifact.type == "text"
        and event.artifact.producer == "reporter"
        for event in events
    )


async def test_native_final_request_retains_evidence_when_framework_drops_prompt(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    model = WorkspaceDeliverySequenceGateway(responses())
    runner = subject(
        model,
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
        strip_prompt=True,
    )
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    native_prompt = "\n".join(str(message.content) for message in model.requests[-1].messages)
    assert "verification_receipt_sha256" in native_prompt and "source_sha256" in native_prompt


async def test_unknown_final_model_is_never_replayed_or_given_fresh_budget(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    class CancelFinal(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 4:
                self.requests.append(request)
                raise asyncio.CancelledError
            return await super().complete_with_context(request)

    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runner = subject(CancelFinal(responses()), harness, repository, installed_parser)
    with pytest.raises(asyncio.CancelledError):
        _events = [event async for event in runner.run(context())]
    saved = await runner.save_checkpoint()
    empty = WorkspaceDeliverySequenceGateway(())
    restored = subject(empty, harness, repository, installed_parser)
    await restored.restore_checkpoint(saved)
    with pytest.raises(adapter.ModelOutcomeUncertain):
        _events = [
            event
            async for event in restored.run(context().model_copy(update={"checkpoint": saved}))
        ]
    assert not empty.requests and len(harness.calls) == 3
    assert (await restored.save_checkpoint()).state["usage"] == saved.state["usage"]


async def test_legacy_checkpoint_is_not_opted_in_by_a_refreshed_route(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    repository = InMemoryArtifactRepository()
    harness = ActualCapabilityHarness(tmp_path, Executor())
    model = WorkspaceDeliverySequenceGateway(responses())
    runner = subject(
        model, harness, repository, installed_parser, altered="Legacy framework result"
    )
    old_context = context().model_copy(
        update={
            "routing_decision": {
                key: value
                for key, value in context().routing_decision.items()
                if key != "engineering_dispatch_report_version"
            }
        }
    )
    events = [event async for event in runner.run(old_context)]
    report = next(
        event.artifact
        for event in events
        if event.artifact is not None
        and event.artifact.type == "text"
        and event.artifact.producer == "reporter"
    )
    assert report.content == {"text": "Legacy framework result"}
    saved = await runner.save_checkpoint()
    assert "engineering_dispatch_report_version" not in saved.state
    empty = WorkspaceDeliverySequenceGateway(())
    restored = subject(empty, harness, repository, installed_parser)
    await restored.restore_checkpoint(saved)
    replay = [
        event async for event in restored.run(context().model_copy(update={"checkpoint": saved}))
    ]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED and not empty.requests


@pytest.mark.parametrize("version", (True, "1", 2))
async def test_report_policy_is_typed_before_any_model_or_tool(
    tmp_path: Path,
    installed_parser: types.ModuleType,
    version: object,
) -> None:
    model = WorkspaceDeliverySequenceGateway(())
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runner = subject(model, harness, InMemoryArtifactRepository(), installed_parser)
    task = context().model_copy(
        update={
            "routing_decision": {
                **context().routing_decision,
                "engineering_dispatch_report_version": version,
            }
        }
    )
    with pytest.raises(adapter.RuntimeExecutionError, match="delivery report version"):
        _events = [event async for event in runner.run(task)]
    assert not model.requests and not harness.calls


async def test_framework_cannot_make_a_second_final_model_call(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    model = WorkspaceDeliverySequenceGateway((*responses(), responses()[-1]))
    runner = subject(
        model,
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
        repeat_final=True,
    )
    with pytest.raises(adapter.RuntimeExecutionError, match="delivery report"):
        _events = [event async for event in runner.run(context())]
    assert len(model.requests) == 5


async def test_verified_delivery_can_cover_an_ancestor_preflight_write(
    tmp_path: Path,
    installed_parser: types.ModuleType,
) -> None:
    model = WorkspaceDeliverySequenceGateway(
        (
            _workspace_delivery_write_response("NOTES.txt", "Design note", "preflight-write"),
            ModelResponse(text='{"summary":"Design note prepared"}', usage=TokenUsage(1, 1, 2)),
            *responses(),
        )
    )
    runner = subject(
        model,
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
        preflight=True,
    )
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 7
