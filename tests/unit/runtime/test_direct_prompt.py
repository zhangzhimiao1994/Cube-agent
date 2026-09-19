import zipfile
from io import BytesIO
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.project_scale_runner import (
    _embedded_workspace_bundle_from_text,
    _workspace_bundle_agent_standard_reasons,
    _workspace_bundle_project_quality_reasons,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.project_scale_artifact import project_scale_artifact_zip_files


class UnusedGateway:
    pass


def test_project_scale_fixture_files_include_agent_standard_reading_evidence() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in project_scale_artifact_zip_files(
            "Project-scale acceptance fixture: build a small project for scale=small "
            "and flow=artifact_production."
        ).items():
            archive.writestr(path, content)

    assert _workspace_bundle_agent_standard_reasons(buffer.getvalue()) == ()


def test_direct_prompt_truncates_large_artifact_text_for_capacity_estimation() -> None:
    original_text = "长文本" * 1_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": original_text},
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Synthesize the artifacts.",
        artifacts=(artifact,),
        token_budget=1_000_000,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    request = runtime._build_request(context).request

    assert request is not None
    user_content = request.messages[-1].content
    assert isinstance(user_content, str)
    assert "[truncated:" in user_content
    assert request.max_output_tokens <= 8192
    assert len(user_content.encode("utf-8")) < len(original_text.encode("utf-8"))
    assert artifact.content["text"] == original_text


def test_direct_prompt_includes_bounded_hermes_memory_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "hermes": {
            "injected_memories": (
                {
                    "summary": "reviewer 超时时先压缩上下文再分块审查。",
                    "memory_type": "error_handling",
                    "target": "reviewer",
                    "reason": "命中 reviewer 超时经验",
                },
            )
        }
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "HERMES_MEMORY_CONTEXT" in serialized
    assert "reviewer 超时时先压缩上下文再分块审查" in serialized
    assert "Current user instructions override them" in serialized


def test_direct_prompt_includes_bounded_self_repair_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "source": "self_repair",
        "self_repair_context": {
            "schema_version": 1,
            "source": "self_repair",
            "source_run_id": "run_1",
            "source_event_sequence": 2,
            "failure_kind": "runtime_failure",
            "repair_action": "draft_repair_proposal",
            "attempt": 1,
            "max_attempts": 1,
            "instruction": "只执行一次受控修复。",
            "automatic_execution": False,
            "requires_approval": True,
        },
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="修复失败运行",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "SELF_REPAIR_CONTEXT" in serialized
    assert "只执行一次受控修复" in serialized
    assert "do not bypass approvals" in serialized


def test_direct_prompt_includes_approved_project_preflight_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "project_preflight_approved": True,
        "project_preflight_proposal": {
            "kind": "project_architecture_preflight",
            "capability": "project.preflight_architecture",
            "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
            "graph_path": "architecture-map.html",
            "requires_constraints_and_skills_reading": True,
        },
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="构建大型项目",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "PROJECT_PREFLIGHT_CONTEXT" in serialized
    assert "project.preflight_architecture" in serialized
    assert "staged implementation" in serialized
    assert "stage_status" in serialized
    assert "verification_evidence" in serialized
    assert "acceptance_review" in serialized


@pytest.mark.asyncio
async def test_direct_project_scale_preflight_emits_verified_artifact_without_gateway() -> None:
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]
    request = (
        "Project-scale acceptance fixture: build a large project for scale=large "
        "and flow=direct."
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=request,
                routing_decision={
                    "project_preflight_approved": True,
                    "project_preflight_proposal": {
                        "kind": "project_architecture_preflight",
                        "capability": "project.preflight_architecture",
                        "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
                        "graph_path": "architecture-map.html",
                        "summary": "Approved large-project architecture preflight.",
                    },
                },
            )
        )
    ]

    assert all(event.kind is not EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    text = artifact_event.artifact.content["text"]
    assert isinstance(text, str)
    assert "### `README.md`" in text
    assert "### `VERIFICATION.md`" in text
    assert artifact_event.payload["deliverable_quality"] == {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }
    assert artifact_event.payload["agent_standard_verification"] == {
        "constraints_read": True,
        "constraint_sources": (
            "AGENTS.md workspace rules; HANDOFF current-state index; PROJECT_REQUIREMENTS.md"
        ),
        "skill_rule_sources": (
            "AGENTS.md workspace rules; applicable SKILL.md inventory; "
            "project-scale agent-standard rules"
        ),
        "read_before_implementation": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    assert artifact_event.payload["workspace_bundle"] == {
        "files": project_scale_artifact_zip_files(request)
    }


@pytest.mark.asyncio
async def test_direct_project_scale_fixture_emits_verified_artifact_without_preflight() -> None:
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=direct."
                ),
                routing_decision={
                    "project_id": "project-scale-acceptance",
                    "workspace_session_id": "project-scale-small-direct",
                },
            )
        )
    ]

    assert all(event.kind is not EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    text = artifact_event.artifact.content["text"]
    assert isinstance(text, str)
    assert "### `VERIFICATION.md`" in text
    assert "- npm run build: passed" in text
    assert "- npm test: passed" in text
    bundle = _embedded_workspace_bundle_from_text(text)
    assert bundle is not None
    assert _workspace_bundle_project_quality_reasons(bundle) == ()
    assert artifact_event.payload["deliverable_quality"] == {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }
    assert artifact_event.payload["agent_standard_verification"] == {
        "constraints_read": True,
        "constraint_sources": (
            "AGENTS.md workspace rules; HANDOFF current-state index; PROJECT_REQUIREMENTS.md"
        ),
        "skill_rule_sources": (
            "AGENTS.md workspace rules; applicable SKILL.md inventory; "
            "project-scale agent-standard rules"
        ),
        "read_before_implementation": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    assert artifact_event.payload["workspace_bundle"] == {
        "files": project_scale_artifact_zip_files(
            "Project-scale acceptance fixture: build a small project for scale=small "
            "and flow=direct."
        )
    }
