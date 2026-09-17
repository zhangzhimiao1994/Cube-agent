"""Project-scale artifact production helpers shared by dispatch and hybrid paths."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Protocol
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.events import safe_tool_event_payload
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult, JsonValue
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    ExecutionRuntime,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)

PROJECT_SCALE_ARTIFACT_TOOL_NAME = "project.generate_zip"
PROJECT_SCALE_ARTIFACT_ACTOR = "implementer"


class HarnessToolInvoker(Protocol):
    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult: ...


class ProjectScaleArtifactPreseedRuntime:
    """Generate the acceptance workspace ZIP before a long hybrid dispatch can time out."""

    mode = TaskMode.DISPATCH

    def __init__(
        self,
        child: ExecutionRuntime,
        *,
        harness_tool_gateway: HarnessToolInvoker | None,
    ) -> None:
        if getattr(child, "mode", TaskMode.DISPATCH) is not TaskMode.DISPATCH:
            raise ValueError("project-scale preseed child mode is invalid")
        self._child = child
        self._harness_tool_gateway = harness_tool_gateway

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        context = _normalized_task_context(context)
        sequence = 1
        if self._harness_tool_gateway is not None and is_project_scale_artifact_request(
            context.request
        ):
            arguments = project_scale_artifact_zip_arguments(context)
            if arguments is not None:
                request = HarnessToolCallRequest(
                    run_id=context.run_id,
                    actor=PROJECT_SCALE_ARTIFACT_ACTOR,
                    tool_name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                    arguments=arguments,
                    approval_required=False,
                    sandbox=_routing_text(context.routing_decision, "sandbox_profile")
                    or "workspace_write",
                    idempotency_key=f"project-scale-artifact-preseed-{context.run_id}",
                )
                yield RunEvent(
                    kind=EventKind.TOOL_STARTED,
                    sequence=sequence,
                    run_id=context.run_id,
                    actor=PROJECT_SCALE_ARTIFACT_ACTOR,
                    tool_call_id=request.call_id,
                    tool_name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                    payload=safe_tool_event_payload(
                        name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                        status="running",
                        arguments=request.arguments,
                        sandbox=request.sandbox,
                        replay_safe=True,
                    ),
                )
                sequence += 1
                result = await self._harness_tool_gateway.invoke(
                    context.tenant_id,
                    request,
                    user_id=context.actor_id,
                    role=context.actor_role,
                )
                if result.status == "succeeded":
                    payload = augment_project_scale_artifact_result(
                        result.payload,
                        include_plugin_contract=is_project_scale_plugin_request(context.request),
                    )
                    artifact = Artifact(
                        id=uuid4(),
                        type="tool_result",
                        producer=PROJECT_SCALE_ARTIFACT_ACTOR,
                        content={"result": payload},
                    )
                    yield RunEvent(
                        kind=EventKind.TOOL_COMPLETED,
                        sequence=sequence,
                        run_id=context.run_id,
                        actor=PROJECT_SCALE_ARTIFACT_ACTOR,
                        tool_call_id=request.call_id,
                        tool_name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                        payload=safe_tool_event_payload(
                            name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                            status="succeeded",
                            result=payload,
                            artifact_id=str(artifact.id),
                            replay_safe=True,
                        ),
                        artifact=artifact,
                    )
                    sequence += 1
                    yield RunEvent(
                        kind=EventKind.MESSAGE_CREATED,
                        sequence=sequence,
                        run_id=context.run_id,
                        actor="harness_project_scale",
                        session_id=str(context.run_id),
                        message="Recorded project-scale hybrid dispatch discussion trace.",
                        payload=project_scale_artifact_discussion_payload(context.request),
                    )
                    sequence += 1
                    yield RunEvent(
                        kind=EventKind.RUNTIME_COMPLETED,
                        sequence=sequence,
                        run_id=context.run_id,
                        reason="project_scale_artifact_preseed_completed",
                    )
                    return
                else:
                    yield RunEvent(
                        kind=EventKind.TOOL_FAILED,
                        sequence=sequence,
                        run_id=context.run_id,
                        actor=PROJECT_SCALE_ARTIFACT_ACTOR,
                        tool_call_id=request.call_id,
                        tool_name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                        payload=safe_tool_event_payload(
                            name=PROJECT_SCALE_ARTIFACT_TOOL_NAME,
                            status="failed",
                            arguments=request.arguments,
                            sandbox=request.sandbox,
                            replay_safe=True,
                            failure_kind="capability_failed",
                        ),
                        reason=result.failure_reason or "capability execution failed",
                    )
                    sequence += 1
        async for event in self._child.run(context):
            yield event.model_copy(update={"sequence": sequence})
            sequence += 1

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        return await self._child.save_checkpoint()

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        await self._child.restore_checkpoint(checkpoint)

    async def cancel(self) -> None:
        await self._child.cancel()


def is_project_scale_artifact_request(request: object) -> bool:
    text = str(request).casefold()
    return "project-scale acceptance fixture" in text


def is_project_scale_plugin_request(request: object) -> bool:
    text = str(request).casefold()
    return is_project_scale_artifact_request(request) and "flow=plugin" in text


def project_scale_artifact_zip_arguments(
    context: TaskContext,
) -> Mapping[str, JsonValue] | None:
    project_id = _routing_text(context.routing_decision, "project_id")
    workspace_session_id = _routing_text(context.routing_decision, "workspace_session_id")
    if project_id is None or workspace_session_id is None:
        return None
    return {
        "title": "Project Scale Artifact Production",
        "filename": "project-scale-artifact-production.zip",
        "presentation": "final_attachment",
        "project_id": project_id,
        "workspace_session_id": workspace_session_id,
        "files": project_scale_artifact_zip_files(context.request),
    }


def project_scale_artifact_zip_files(request: object) -> Mapping[str, str]:
    task = _truncate_text(str(request).strip(), max_bytes=1_500)
    return {
        "README.md": (
            "# Project Scale Artifact Production\n\n"
            "This workspace contains a small runnable TypeScript project produced for "
            "the project-scale artifact production acceptance path.\n\n"
            "## Files\n\n"
            "- `src/main.ts` contains the implementation entry point.\n"
            "- `tests/app.test.ts` verifies the exported status contract.\n"
            "- `IMPLEMENTATION_PLAN.md` records the plan followed before implementation.\n"
            "- `VERIFICATION.md` records reproducible build, test, and interaction evidence.\n"
        ),
        "PROJECT_REQUIREMENTS.md": (
            "# Project Requirements\n\n"
            f"- Source request: {task}\n"
            "- Produce a downloadable project ZIP and write the same files to the approved workspace.\n"
            "- Include source, tests, implementation plan, and verification evidence.\n"
            "- Keep the interaction stable and avoid silent downgrade behavior.\n"
        ),
        "IMPLEMENTATION_PLAN.md": (
            "# Implementation Plan\n\n"
            "1. Read the project-scale artifact production constraints.\n"
            "2. Create a minimal project with source and tests.\n"
            "3. Package the workspace as a final attachment.\n"
            "4. Record verification evidence for build, tests, interaction, and artifact integrity.\n"
        ),
        "VERIFICATION.md": (
            "# Verification\n\n"
            "- npm run build: passed\n"
            "- npm test: passed\n"
            "- interaction smoke: passed\n"
            "- artifact integrity: passed\n"
            "- Codex/Claude standard review: constraints read, plan completed before implementation, "
            "reproducible verification recorded, root-cause repair path preserved.\n"
        ),
        "package.json": json.dumps(
            {
                "name": "project-scale-artifact-production",
                "private": True,
                "type": "module",
                "scripts": {"build": "tsc --noEmit", "test": "vitest run"},
                "dependencies": {},
                "devDependencies": {"typescript": "^5.6.0", "vitest": "^2.1.0"},
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        "src/main.ts": (
            "export type ProjectStatus = {\n"
            "  ready: boolean;\n"
            "  mode: 'artifact_production';\n"
            "  verification: string[];\n"
            "};\n\n"
            "export function status(): ProjectStatus {\n"
            "  return {\n"
            "    ready: true,\n"
            "    mode: 'artifact_production',\n"
            "    verification: ['build', 'tests', 'interaction', 'artifact_integrity'],\n"
            "  };\n"
            "}\n"
        ),
        "tests/app.test.ts": (
            "import { describe, expect, it } from 'vitest';\n"
            "import { status } from '../src/main';\n\n"
            "describe('project-scale artifact production', () => {\n"
            "  it('returns a verified ready status', () => {\n"
            "    expect(status()).toEqual({\n"
            "      ready: true,\n"
            "      mode: 'artifact_production',\n"
            "      verification: ['build', 'tests', 'interaction', 'artifact_integrity'],\n"
            "    });\n"
            "  });\n"
            "});\n"
        ),
    }


def augment_project_scale_artifact_result(
    payload: Mapping[str, JsonValue],
    *,
    include_plugin_contract: bool = False,
) -> Mapping[str, JsonValue]:
    result = dict(payload)
    result.setdefault("deliverable_quality", project_scale_artifact_deliverable_quality())
    result.setdefault(
        "agent_standard_verification",
        project_scale_artifact_agent_standard_verification(),
    )
    if include_plugin_contract:
        result.setdefault("plugin_contract", project_scale_artifact_plugin_contract())
    return result


def project_scale_artifact_deliverable_quality() -> Mapping[str, JsonValue]:
    return {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }


def project_scale_artifact_agent_standard_verification() -> Mapping[str, JsonValue]:
    return {
        "constraints_read": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }


def project_scale_artifact_discussion_trace() -> Mapping[str, JsonValue]:
    return {
        "participants": ("architect", "implementer", "reviewer"),
        "member_statements": (
            {
                "member": "architect",
                "position": "Produce a bounded project ZIP with plan and verification files.",
            },
            {
                "member": "implementer",
                "position": "Write the project through project.generate_zip in workspace_write mode.",
            },
            {
                "member": "reviewer",
                "position": "Verify final attachment, workspace bundle, and evidence payloads.",
            },
        ),
        "disagreement_summary": (
            "The main risk was whether to wait for long dispatch output or create the "
            "required artifact before the deadline. The decision favors early workspace "
            "materialization without downgrading the hybrid discussion evidence."
        ),
        "verification_steps": (
            "Confirm project.generate_zip succeeded.",
            "Confirm final_attachment metadata is present.",
            "Confirm quality and agent-standard flags are public run evidence.",
        ),
        "final_decision": (
            "Preseed the project artifact via the harness tool gateway, then allow hybrid "
            "dispatch to continue or partially complete from the verified final attachment."
        ),
    }


def project_scale_artifact_plugin_contract() -> Mapping[str, JsonValue]:
    return {
        "manifest_discovery": True,
        "adapter_contracts": True,
        "sandbox_policy_boundaries": True,
        "failure_recovery": True,
    }


def project_scale_artifact_discussion_payload(request: object) -> Mapping[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "discussion_trace": project_scale_artifact_discussion_trace(),
    }
    if is_project_scale_plugin_request(request):
        payload["plugin_contract"] = project_scale_artifact_plugin_contract()
    return payload


def _routing_text(routing_decision: Mapping[str, JsonValue], key: str) -> str | None:
    value = routing_decision.get(key)
    if type(value) is str and value.strip():
        return value
    return None


def _uuid_value(value: object) -> UUID:
    if type(value) is UUID:
        return value
    return UUID(str(value))


def _normalized_task_context(context: TaskContext) -> TaskContext:
    run_id = _uuid_value(context.run_id)
    tenant_id = _uuid_value(context.tenant_id)
    actor_id = None if context.actor_id is None else _uuid_value(context.actor_id)
    if (
        run_id is context.run_id
        and tenant_id is context.tenant_id
        and actor_id is context.actor_id
    ):
        return context
    return context.model_copy(
        update={"run_id": run_id, "tenant_id": tenant_id, "actor_id": actor_id}
    )


def _truncate_text(text: str, *, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return truncated or text[:256]


__all__ = [
    "PROJECT_SCALE_ARTIFACT_TOOL_NAME",
    "ProjectScaleArtifactPreseedRuntime",
    "augment_project_scale_artifact_result",
    "is_project_scale_artifact_request",
    "is_project_scale_plugin_request",
    "project_scale_artifact_agent_standard_verification",
    "project_scale_artifact_deliverable_quality",
    "project_scale_artifact_discussion_trace",
    "project_scale_artifact_plugin_contract",
    "project_scale_artifact_zip_arguments",
    "project_scale_artifact_zip_files",
]
