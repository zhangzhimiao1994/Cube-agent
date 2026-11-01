from __future__ import annotations

import types
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.runtime.crew import test_framework_parser_boundary as parser_fixtures
from tests.unit.runtime.delivery_report_typed_fixture import json_map, mapping
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact, final_zip_artifact

installed_parser = parser_fixtures.installed_parser
offline_only = parser_fixtures.offline_only


@pytest.mark.parametrize("final_text", [None, "Done.", "Project files already exist."])
async def test_new_hybrid_cannot_complete_without_current_native_final_binding(
    final_text: str | None,
) -> None:
    bundle = final_zip_artifact()
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (bundle,)),
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("reviewer", "Reviewed."),)),
        MultiArtifactRuntime(
            TaskMode.DIRECT, () if final_text is None else (artifact("main", final_text),)
        ),
    )
    ctx = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Build and deliver the project.",
        routing_decision={
            "engineering_delivery_contract_version": 1,
            "engineering_hybrid_report_version": 1,
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
            "project_scale": "small",
        },
    )
    events = [event async for event in runtime.run(ctx)]
    assert any(event.artifact == bundle for event in events)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


@pytest.mark.parametrize("discussion_outputs", [1, 90])
async def test_hybrid_real_crew_delivery_reaches_bound_main_report_and_cold_restore(
    tmp_path: Path,
    installed_parser: types.ModuleType,
    discussion_outputs: int,
) -> None:
    from agent_hub.runtime import direct_delivery_report
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.direct import DirectRuntime
    from tests.unit.capabilities.test_project_verification import Executor
    from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
    from tests.unit.runtime.crew.test_delivery_report import context as dispatch_context
    from tests.unit.runtime.crew.test_delivery_report import responses, subject
    from tests.unit.runtime.crew.test_engineering_delivery_execution import ActualCapabilityHarness
    from tests.unit.runtime.test_direct_project_verification import Model
    from tests.unit.runtime.test_hybrid import UnusedRuntime

    repo = InMemoryArtifactRepository()
    ctx = dispatch_context()
    ctx = ctx.model_copy(
        update={
            "mode": TaskMode.HYBRID,
            "routing_decision": {
                **ctx.routing_decision,
                "engineering_delivery_contract_version": 1,
                "engineering_hybrid_report_version": 1,
            },
        }
    )
    reporter = Model("项目 ZIP 已交付。安装、构建和测试均已验证；请按 README 使用，部署尚未验证。")
    crew_model = WorkspaceDeliverySequenceGateway(responses())
    harness = ActualCapabilityHarness(tmp_path, Executor())
    runtime = HybridRuntime(
        subject(crew_model, harness, repo, installed_parser),
        MultiArtifactRuntime(
            TaskMode.DISCUSS,
            tuple(
                artifact("reviewer", f"Reviewed item {index}.")
                for index in range(discussion_outputs)
            ),
        ),
        DirectRuntime(reporter, logical_model="main"),
        artifact_repository=repo,
    )
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    report = next(
        event.artifact
        for event in reversed(events)
        if event.artifact is not None and event.artifact.producer == "main"
    )
    assert direct_delivery_report.valid(ctx, report)
    assert len(crew_model.requests) == 5 and len(reporter.requests) == 1
    assert not reporter.requests[0].tools
    assert "passed" in "\n".join(str(message.content) for message in reporter.requests[0].messages)
    checkpoint = await runtime.save_checkpoint()
    restored = HybridRuntime(
        UnusedRuntime(TaskMode.DISPATCH, "unused"),
        UnusedRuntime(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
        artifact_repository=repo,
    )
    await restored.restore_checkpoint(checkpoint)
    replay = [
        event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))
    ]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(reporter.requests) == 1
    state = {
        **checkpoint.state,
        "artifact_registry": {
            key: value
            for key, value in mapping(checkpoint.state["artifact_registry"]).items()
            if key != str(report.id)
        },
        "output_artifact_registry": {
            key: value
            for key, value in mapping(checkpoint.state["output_artifact_registry"]).items()
            if key != str(report.id)
        },
    }
    incomplete = RuntimeCheckpoint(
        id=uuid4(),
        runtime_type=checkpoint.runtime_type,
        runtime_version=checkpoint.runtime_version,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        mode=ctx.mode,
        state=json_map(state),
    )
    missing = HybridRuntime(
        UnusedRuntime(TaskMode.DISPATCH, "unused"),
        UnusedRuntime(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
        artifact_repository=repo,
    )
    await missing.restore_checkpoint(incomplete)
    denied = [
        event async for event in missing.run(ctx.model_copy(update={"checkpoint": incomplete}))
    ]
    assert denied[-1].kind is EventKind.RUNTIME_FAILED
    assert len(reporter.requests) == 1
    for downgraded in (0, None):
        route = dict(ctx.routing_decision)
        if downgraded is None:
            route.pop("engineering_hybrid_report_version")
        else:
            route["engineering_hybrid_report_version"] = downgraded
        stale = HybridRuntime(
            UnusedRuntime(TaskMode.DISPATCH, "unused"),
            UnusedRuntime(TaskMode.DISCUSS, "unused"),
            UnusedRuntime(TaskMode.DIRECT, "unused"),
            artifact_repository=repo,
        )
        await stale.restore_checkpoint(incomplete)
        events = [
            event
            async for event in stale.run(
                ctx.model_copy(
                    update={
                        "checkpoint": incomplete,
                        "routing_decision": route,
                    }
                )
            )
        ]
        assert events[-1].kind is EventKind.RUNTIME_FAILED


@pytest.mark.parametrize("failure", ["transport", "empty"])
async def test_hybrid_final_main_never_retries_unknown_or_empty_response(
    tmp_path: Path,
    installed_parser: types.ModuleType,
    failure: str,
) -> None:
    from agent_hub.models.litellm_client import ModelTransportError
    from agent_hub.runtime import hybrid_delivery_report
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
    from tests.unit.capabilities.test_project_verification import Executor
    from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
    from tests.unit.runtime.crew.test_delivery_report import context as dispatch_context
    from tests.unit.runtime.crew.test_delivery_report import responses, subject
    from tests.unit.runtime.crew.test_engineering_delivery_execution import ActualCapabilityHarness
    from tests.unit.runtime.test_direct_project_verification import Model

    ctx = dispatch_context()
    crew = subject(
        WorkspaceDeliverySequenceGateway(responses()),
        ActualCapabilityHarness(tmp_path, Executor()),
        InMemoryArtifactRepository(),
        installed_parser,
    )
    created = [event.artifact async for event in crew.run(ctx) if event.artifact is not None]
    _, required = hybrid_delivery_report.delivery(
        ctx, created, output_ids={item.id for item in created}
    )
    ctx = ctx.model_copy(
        update={
            "mode": TaskMode.DIRECT,
            "artifacts": required,
            "routing_decision": {
                **ctx.routing_decision,
                "engineering_hybrid_report_version": 1,
                hybrid_delivery_report.INPUT: {
                    str(item.id): item.content_sha256 for item in required
                },
            },
        }
    )

    class ReportModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if failure == "transport" and not self.requests:
                self.requests.append(request)
                raise ModelTransportError("uncertain provider transport")
            return await super().complete_with_context(request)

    model = (
        ReportModel("", "A report after retry.")
        if failure == "empty"
        else ReportModel("A report after retry.")
    )
    runtime = DirectRuntime(model, logical_model="main")
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 1
