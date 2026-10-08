from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.capacity import CapacityUnavailable
from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput, ModelGatewayError
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    RejectedOutputEvidence,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    GatewayProvenance,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew import adapter as crew_adapter
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewRunStream,
    CrewTaskDefinition,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _artifact_final_synthesis_payload,
    _artifact_prompt_payload,
    _artifact_review_packet_payload,
    _checkpoint_can_skip_forbidden_tool_placeholders,
    _correctable_tool_argument_rejection,
    _crew_content_limits,
    _has_matching_tool_argument_rejection,
    _recovery_status_after_attempts,
    _scope_project_workspace_tool_call,
    _should_check_framework_raw,
    _step_timeout_recovery_window_seconds,
    _tool_definitions,
    _tool_round_budget,
    _tool_sandbox,
    _ToolLedger,
    _workspace_assistant_tool_history,
    _workspace_delivery_progress,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = UUID("00000000-0000-4000-8000-000000000002")


@pytest.mark.parametrize("status_code", [None, 405, 422])
def test_network_recovery_closure_does_not_mislabel_unknown_http_status(
    status_code: int | None,
) -> None:
    status = _recovery_status_after_attempts(
        {"error_code": "model.provider_transport_failed", "status_code": status_code},
        recovery_attempts=1, max_recovery_attempts=1,
    )
    assert status == (
        "failed_after_compact_retry" if status_code is None else "failed_without_compact_retry"
    )


class UnusedGateway:
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        return GatewayCompletion(
            response=ModelResponse(text="unused", usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ToolGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web_search", arguments={"q": "safe"}),
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
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ProjectPreflightToolGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id="preflight-call",
                        name="project_preflight_architecture",
                        arguments={
                            "title": "Large Project",
                            "summary": "model supplied but unsupported",
                        },
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="preflight ready", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ToolResultAwareGateway:
    """Models a provider that needs a trusted continuation contract after a tool call."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        has_trusted_continuation = any(
            message.role == "system"
            and isinstance(message.content, str)
            and "CAPABILITY_RESULT_CONTINUATION" in message.content
            for message in request.messages
        )
        response = (
            ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
            if has_trusted_continuation
            else ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web_search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            )
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class DuplicateUntilToolsDisabledGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id=f"provider-call-{len(self.requests)}",
                        name="web_search",
                        arguments={"q": "safe"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if request.tools
            else ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ReadContextToolGateway:
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
                        name="read_context",
                        arguments={"path": "missing/generated-project.zip"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="review can continue", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class RepeatingReadContextToolGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id=f"provider-call-{len(self.requests)}",
                        name="read_context",
                        arguments={"query": "generated project verification evidence"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ReadContextThenForbiddenToolGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id="provider-call-read-context",
                        name="read_context",
                        arguments={"query": "generated project verification evidence"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id="provider-call-forbidden",
                        name="project.generate_zip",
                        arguments={"title": "tester should not write"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ManifestToolGateway:
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
                        name="calendar_create_event",
                        arguments={"title": "Review", "date": "2026-09-16"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="calendar event ready", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ContractToolGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        has_tool_results = any(
            isinstance(message.content, str)
            and message.content.startswith("UNTRUSTED_CAPABILITY_RESULTS_JSON=")
            for message in request.messages
        )
        if ModelCapability.TOOL_CALLING in request.required_capabilities and not has_tool_results:
            response = ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web_search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            )
        else:
            response = ModelResponse(text=_role_output_text(request), usage=TokenUsage(1, 1, 2))
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class ProjectZipWorkspaceGateway:
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
                        arguments={
                            "title": "Hello Workspace",
                            "project_id": "project-main",
                            "workspace_session_id": "session-main",
                            "files": {"main.py": "print('hello')\n"},
                        },
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="workspace zip ready", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class FakeCapabilities:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

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
        del tenant_id, run_id, arguments, idempotency_key
        self.calls.append((actor, name))
        return {"items": ("legacy result",)}

    def is_replay_safe(self, name: str) -> bool:
        return name == "web.search"


class ProjectPreflightCapabilities(FakeCapabilities):
    def __init__(self) -> None:
        super().__init__()
        self.arguments: list[Mapping[str, JsonValue]] = []

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
        del tenant_id, run_id, idempotency_key
        self.calls.append((actor, name))
        self.arguments.append(dict(arguments))
        return {"summary": "preflight ready"}

    def is_replay_safe(self, name: str) -> bool:
        return name == "project.preflight_architecture"

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, object]:
        del tenant_id
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "project.preflight_architecture",
                    "kind": "builtin",
                    "adapter": "runtime_builtin",
                    "available": True,
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 96,
                            },
                        },
                    },
                },
            ),
        }


class ManifestCapabilities(FakeCapabilities):
    def is_replay_safe(self, name: str) -> bool:
        return name == "calendar.create_event"

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, object]:
        del tenant_id
        return {
            "schema_version": 1,
            "capabilities": (
                {
                    "id": "calendar.create_event",
                    "kind": "plugin",
                    "adapter": "plugin_runtime",
                    "available": True,
                    "description": "Create a calendar event through the approved plugin.",
                    "failure_codes": ("plugin.timeout", "plugin.invalid_arguments"),
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ("title", "date"),
                        "properties": {
                            "title": {"type": "string"},
                            "date": {"type": "string"},
                        },
                    },
                    "sandbox_profile": "remote_connector",
                    "replay_safe": True,
                },
            ),
        }


class UnavailableCapabilities(FakeCapabilities):
    def __init__(self) -> None:
        super().__init__()
        self.availability_checks: list[tuple[UUID, str]] = []

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        self.availability_checks.append((tenant_id, name))
        return False


class RaisingCapabilities(FakeCapabilities):
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
        raise RuntimeError("commit status unavailable")

    def is_replay_safe(self, name: str) -> bool:
        del name
        return False


class ReplaySafeRaisingCapabilities(RaisingCapabilities):
    def is_replay_safe(self, name: str) -> bool:
        del name
        return True


class ScopedReadUnavailableCapabilities(FakeCapabilities):
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
        del tenant_id, run_id, idempotency_key
        self.calls.append((actor, name))
        assert arguments == {"path": "missing/generated-project.zip"}
        raise RuntimeCapabilityError("workspace read denied or scoped file unavailable")

    def is_replay_safe(self, name: str) -> bool:
        return name == "read_context"


class ReadContextCapabilities(FakeCapabilities):
    def is_replay_safe(self, name: str) -> bool:
        return name == "read_context"


class RecordingHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, HarnessToolCallRequest]] = []

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        assert user_id is None
        assert role is None
        self.calls.append((tenant_id, request))
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload={"items": ("harness result",)},
        )


class FailingHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []

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
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            payload={},
            failure_reason="tool unavailable",
        )


class DeterministicErrorHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []

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
        raise RuntimeCapabilityError("files must be an object")


class WaitingApprovalHarnessToolGateway:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []

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
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            payload={"approval_id": "approval_project_zip"},
            failure_reason="capability requires approval",
        )


class IdentityRecordingHarnessToolGateway:
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
            payload={"items": ("identity result",)},
        )


class FailingGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, bridge, agent_id, storage_scope
        raise ValueError("agent identifier must be a safe identifier")


class FailingFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FailingGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return FailingGeneration()


class BuildFailingFactory(CrewObjectFactory):
    def __init__(self, message: str = "private crew runtime refused storage path") -> None:
        self.message = message

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FailingGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        raise ValueError(self.message)


class TimeoutGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, bridge, agent_id, storage_scope
        raise TimeoutError


class TimeoutFactory(CrewObjectFactory):
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> TimeoutGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return TimeoutGeneration()


class RecordingGeneration:
    def __init__(self, *, reviewer_timeouts: int = 0, agent_timeouts: int = 0) -> None:
        self.reviewer_timeouts = reviewer_timeouts
        self.agent_timeouts = agent_timeouts
        self.prompts: list[tuple[str, str | None, str]] = []

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del storage_scope
        self.prompts.append((step_id, agent_id, prompt))
        if self.reviewer_timeouts > 0 and agent_id == "reviewer":
            self.reviewer_timeouts -= 1
            raise TimeoutError
        if self.agent_timeouts > 0 and agent_id != "reviewer":
            self.agent_timeouts -= 1
            raise TimeoutError
        return await bridge.complete([{"role": "system", "content": prompt}])


class DeadlineThenSuccessGeneration(RecordingGeneration):
    def __init__(self, *, first_delay_seconds: float, target_agent_id: str) -> None:
        super().__init__()
        self.first_delay_seconds = first_delay_seconds
        self.target_agent_id = target_agent_id
        self.target_attempts = 0

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        if agent_id == self.target_agent_id:
            self.target_attempts += 1
        if agent_id == self.target_agent_id and self.target_attempts == 1:
            self.prompts.append((step_id, agent_id, prompt))
            await asyncio.sleep(self.first_delay_seconds)
        return await super().execute(
            step_id,
            prompt,
            bridge,
            agent_id=agent_id,
            storage_scope=storage_scope,
        )


class RecordingFactory(CrewObjectFactory):
    def __init__(self, generation: RecordingGeneration) -> None:
        self.generation = generation

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> RecordingGeneration:
        del agents, tasks, share_crew, telemetry_disabled
        return self.generation


class RoleAwareGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == "review":
            text = '{"verdict":"approve"}'
        else:
            text = _role_output_text(request, fallback="role output " + ("内容" * 600))
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


def _role_output_text(request: ModelRequest, *, fallback: str = "role output") -> str:
    if request.response_schema is not None:
        properties = cast(Mapping[str, object], request.response_schema.schema["properties"])
        values = {"summary": "role output", "findings": ["ok"], "risks": []}
        return json.dumps({key: values[key] for key in properties})
    return fallback


class SequenceGateway:
    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text = self._texts.pop(0) if self._texts else "done"
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class FallbackSequenceGateway(SequenceGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text = self._texts.pop(0) if self._texts else "done"
        fallback_used = len(self.requests) == 1
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="fallback" if fallback_used else "primary",
            logical_model="fallback" if fallback_used else request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
            fallback_used=fallback_used,
            fallback_from_logical_model=request.logical_model if fallback_used else None,
            fallback_reason="capacity" if fallback_used else None,
            attempted_logical_models=(
                (request.logical_model, "fallback") if fallback_used else (request.logical_model,)
            ),
        )


class LogicalFallbackSequenceGateway(SequenceGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text = self._texts.pop(0) if self._texts else "done"
        logical_model = "glm" if len(self.requests) == 1 else request.logical_model
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="glm_1" if logical_model == "glm" else "primary",
            logical_model=logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
            attempted_logical_models=(logical_model,),
        )


class EmptyThenRoleAwareGateway(RoleAwareGateway):
    def __init__(self, *, empty_logical_model: str) -> None:
        super().__init__()
        self.empty_logical_model = empty_logical_model
        self._empty_returned = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == self.empty_logical_model and not self._empty_returned:
            self._empty_returned = True
            text = ""
        elif request.logical_model == "review":
            text = '{"verdict":"approve"}'
        else:
            text = _role_output_text(request, fallback="role output " + ("内容" * 600))
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class CapacityUnavailableThenRoleAwareGateway(RoleAwareGateway):
    def __init__(self, *, unavailable_logical_model: str) -> None:
        super().__init__()
        self.unavailable_logical_model = unavailable_logical_model
        self._unavailable_returned = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == self.unavailable_logical_model and not self._unavailable_returned:
            self._unavailable_returned = True
            raise CapacityUnavailable("model capacity unavailable")
        text = (
            '{"verdict":"approve"}'
            if request.logical_model == "review"
            else _role_output_text(request)
        )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class CapacityThenBadRequestThenRoleAwareGateway(RoleAwareGateway):
    def __init__(
        self,
        *,
        unavailable_logical_model: str,
        bad_request_logical_model: str,
    ) -> None:
        super().__init__()
        self.unavailable_logical_model = unavailable_logical_model
        self.bad_request_logical_model = bad_request_logical_model
        self._unavailable_returned = False
        self._bad_request_returned = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if (
            request.logical_model == self.unavailable_logical_model
            and not self._unavailable_returned
        ):
            self._unavailable_returned = True
            raise CapacityUnavailable("model capacity unavailable")
        if (
            request.logical_model == self.bad_request_logical_model
            and not self._bad_request_returned
        ):
            self._bad_request_returned = True
            raise ModelTransportError("model transport failed", status_code=400)
        text = (
            '{"verdict":"approve"}'
            if request.logical_model == "review"
            else _role_output_text(request)
        )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class SlowCapacityRecoveryGateway(RoleAwareGateway):
    def __init__(self) -> None:
        super().__init__()
        self._failed_once = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if not self._failed_once:
            self._failed_once = True
            await asyncio.sleep(0.8)
            raise CapacityUnavailable("model capacity unavailable")
        await asyncio.sleep(1.5)
        return GatewayCompletion(
            response=ModelResponse(
                text=_role_output_text(request),
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class RepeatedCapacityUnavailableGateway(RoleAwareGateway):
    def __init__(self, *, unavailable_logical_model: str, failures: int) -> None:
        super().__init__()
        self.unavailable_logical_model = unavailable_logical_model
        self.failures = failures

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == self.unavailable_logical_model and self.failures > 0:
            self.failures -= 1
            raise CapacityUnavailable("model capacity unavailable")
        text = (
            '{"verdict":"approve"}'
            if request.logical_model == "review"
            else _role_output_text(request)
        )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class EmptyThenReviewingGateway(EmptyThenRoleAwareGateway):
    def __init__(self, *, empty_logical_model: str, reviews: tuple[str, ...]) -> None:
        super().__init__(empty_logical_model=empty_logical_model)
        self.reviews = list(reviews)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model != "review":
            return await super().complete_with_context(request)
        self.requests.append(request)
        if request.logical_model == self.empty_logical_model and not self._empty_returned:
            self._empty_returned = True
            text = ""
        else:
            text = self.reviews.pop(0) if self.reviews else '{"verdict":"approve"}'
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


class CapacityThenReviewingGateway(EmptyThenReviewingGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model == self.empty_logical_model and not self._empty_returned:
            self._empty_returned = True
            self.requests.append(request)
            raise CapacityUnavailable("model capacity unavailable")
        return await super().complete_with_context(request)


class FailingModelGateway(RoleAwareGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        raise RuntimeError("provider detail must be redacted")


class FailingAfterToolGateway(ToolGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.requests:
            raise RuntimeError("provider detail must be redacted")
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web.search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


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
        del agents, tasks, share_crew, telemetry_disabled
        return FastGeneration()


def _one_step_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )


def _one_step_plan_with_model_fallback() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="primary",
                fallback_models=("backup",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )


def _one_step_plan_with_two_model_fallbacks() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="primary",
                fallback_models=("backup", "final"),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )


def _short_timeout_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
                timeout_seconds=2.1,
            ),
        ),
        total_token_budget=100,
    )


def _tool_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )


def _project_preflight_tool_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="project_preflight_architect",
                role="Project Preflight Architect",
                goal="Create the architecture preflight",
                logical_model="general",
                allowed_tools=("project.preflight_architecture",),
            ),
        ),
        steps=(
            DispatchStep(
                id="project_preflight_step",
                agent="project_preflight_architect",
                task="Create the architecture preflight",
                tools=("project.preflight_architecture",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("project.preflight_architecture",),
        total_token_budget=100,
    )


def _read_context_tool_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="security_reviewer",
                role="security_reviewer",
                goal="Review generated project evidence",
                logical_model="general",
                allowed_tools=("read_context",),
            ),
        ),
        steps=(
            DispatchStep(
                id="security_reviewer_step",
                agent="security_reviewer",
                task="Review generated project evidence",
                tools=("read_context",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("read_context",),
        total_token_budget=100,
    )


def _project_scale_repeating_read_context_plan() -> DispatchPlan:
    task = (
        "Role mission: verify generated project evidence.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="tester",
                role="tester",
                goal="Verify generated project evidence",
                logical_model="general",
                allowed_tools=("read_context",),
                output_schema={
                    "status": "string",
                    "summary": "string",
                    "evidence": "string[]",
                    "risks": "string[]",
                    "artifacts": "string[]",
                    "verification": "string[]",
                },
            ),
        ),
        steps=(
            DispatchStep(
                id="tester_step",
                agent="tester",
                task=task,
                tools=("read_context",),
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(10),
            ),
        ),
        allowed_tools=("read_context",),
        total_token_budget=100_000,
        total_cost_usd=Decimal(10),
    )


def _manifest_tool_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("calendar.create_event",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("calendar.create_event",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("calendar.create_event",),
        total_token_budget=100,
    )


def _project_zip_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("project.generate_zip",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("project.generate_zip",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("project.generate_zip",),
        total_token_budget=100,
    )


def _project_scale_artifact_plan() -> DispatchPlan:
    task = (
        "Project-scale acceptance fixture: build a small project for scale=small "
        "and flow=artifact_production."
    )
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="implementer",
                role="implementer",
                goal="Produce a verified project bundle",
                logical_model="general",
                allowed_tools=("project.generate_zip",),
            ),
        ),
        steps=(
            DispatchStep(
                id="implement",
                agent="implementer",
                task=task,
                tools=("project.generate_zip",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("project.generate_zip",),
        total_token_budget=100,
    )


def _reviewed_step_plan(*, reviewer_retries: int = 0) -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string"},
            ),
            AgentSpec(id="reviewer", role="reviewer", goal="Review", logical_model="review"),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="draft",
                agent="writer",
                task="Draft",
                reviewer="reviewer",
                reviewer_retries=reviewer_retries,
                token_budget=1000,
            ),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )


def _dependent_final_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string"},
            ),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(id="draft", agent="writer", task="Draft", token_budget=1000),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )


def _branched_contract_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string"},
            ),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(id="source_a", agent="writer", task="Source A", token_budget=1000),
            DispatchStep(
                id="blocked_target",
                agent="writer",
                task="Blocked target",
                depends_on=("source_a",),
                token_budget=1000,
            ),
            DispatchStep(id="side_note", agent="writer", task="Side note", token_budget=1000),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("blocked_target", "side_note"),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )


def _tool_contract_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
                output_schema={"summary": "string"},
            ),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(id="source_a", agent="writer", task="Source A", token_budget=1000),
            DispatchStep(
                id="tool_target",
                agent="writer",
                task="Use tool",
                depends_on=("source_a",),
                tools=("web.search",),
                token_budget=1000,
            ),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("tool_target",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=1000,
    )


def _structured_dependent_final_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={
                    "summary": "string",
                    "findings": "string[]",
                    "risks": "string[]",
                },
            ),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(id="draft", agent="writer", task="Draft", token_budget=1000),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )


def _context(**changes: object) -> TaskContext:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "tenant_id": TENANT_ID,
        "mode": TaskMode.DISPATCH,
        "request": "Write a short answer",
        "token_budget": 1000,
    }
    values.update(changes)
    return TaskContext.model_validate(values, strict=True)


def test_openai_tool_call_response_can_have_empty_text() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(
            text="",
            tool_calls=(ToolCall(id="call_1", name="read_context", arguments={"query": "x"}),),
            usage=TokenUsage(10, 1, 11),
        ),
        deployment_id="primary",
        logical_model="general",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-v4-flash",
        cost_usd=Decimal(0),
    )

    response = CrewDispatchRuntime._valid_response(completion)

    assert response.tool_calls[0].name == "read_context"


def test_text_only_empty_model_response_still_fails() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(text="", usage=TokenUsage(10, 0, 10)),
        deployment_id="primary",
        logical_model="general",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-v4-flash",
        cost_usd=Decimal(0),
    )

    with pytest.raises(RuntimeExecutionError, match="model response text is empty"):
        CrewDispatchRuntime._valid_response(completion)


def test_model_response_artifact_preserves_gateway_fallback_metadata() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(text="fallback answer", usage=TokenUsage(10, 2, 12)),
        deployment_id="backup",
        logical_model="backup",
        provider_id="openai",
        provider_model="openai/gpt-5",
        cost_usd=Decimal("0.000123"),
        fallback_used=True,
        fallback_from_logical_model="primary",
        fallback_reason="capacity_unavailable",
        attempted_logical_models=("primary", "backup"),
    )

    artifact = CrewDispatchRuntime._model_artifact(
        actor="writer",
        completion=completion,
        sources=(),
    )
    restored = CrewDispatchRuntime._completion_from_model_artifact(artifact)

    assert artifact.content["fallback_used"] is True
    assert restored.fallback_used is True
    assert restored.fallback_from_logical_model == "primary"
    assert restored.fallback_reason == "capacity_unavailable"
    assert restored.attempted_logical_models == ("primary", "backup")


def test_legacy_model_response_artifact_defaults_to_no_gateway_fallback() -> None:
    artifact = Artifact(
        id=uuid4(),
        type="model_response",
        producer="writer",
        content={
            "text": "legacy answer",
            "tool_calls": (),
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
            "cost_usd": "0",
        },
        provenance=GatewayProvenance(
            logical_model="primary",
            deployment_id="primary",
            provider_id="deepseek",
            provider_model="deepseek/deepseek-chat",
        ),
    )

    restored = CrewDispatchRuntime._completion_from_model_artifact(artifact)

    assert restored.fallback_used is False
    assert restored.fallback_from_logical_model is None
    assert restored.fallback_reason is None
    assert restored.attempted_logical_models == ()


async def _collect(runtime: CrewDispatchRuntime) -> list[RunEvent]:
    return [event async for event in runtime.run(_context())]


async def test_adaptive_runtime_allows_soft_context_budget_below_hard_plan_envelope() -> None:
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(
                token_budget=50,
                routing_decision={
                    "runtime_plan_token_budget": 100,
                    "runtime_token_absolute_tokens": 100,
                },
            )
        )
    ]

    assert any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_step_events_include_orchestration_contract_context() -> None:
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _dependent_final_plan(),
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    final_started = next(
        event
        for event in events
        if event.kind is EventKind.STEP_STARTED and event.step_id == "final_response"
    )
    assert final_started.payload["orchestration_protocol"] == "role_handoff_contract_v1"
    assert final_started.payload["depends_on"] == ("draft",)
    assert final_started.payload["incoming_contract_ids"] == ("draft-to-final_response",)

    draft_started = next(
        event for event in events if event.kind is EventKind.STEP_STARTED and event.step_id == "draft"
    )
    assert draft_started.payload["dependent_step_ids"] == ("final_response",)
    assert draft_started.payload["outgoing_contract_ids"] == ("draft-to-final_response",)

    final_completed = next(
        event
        for event in events
        if event.kind is EventKind.STEP_COMPLETED and event.step_id == "final_response"
    )
    assert final_completed.payload["completed_contract_ids"] == ("draft-to-final_response",)

    single_step_runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        crew_factory=FastFactory(),
    )
    single_step_events = await _collect(single_step_runtime)
    single_step_started = next(
        event for event in single_step_events if event.kind is EventKind.STEP_STARTED
    )
    assert "orchestration_protocol" not in single_step_started.payload


async def test_real_multi_agent_events_include_normalized_agent_identity() -> None:
    class MultiAgentGateway:
        def __init__(self) -> None:
            self.implementer_calls = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if request.logical_model == "implementer_model":
                self.implementer_calls += 1
                response = (
                    ModelResponse(
                        text=None,
                        tool_calls=(
                            ToolCall(
                                id="implementation-search",
                                name="web_search",
                                arguments={"q": "implementation contract"},
                            ),
                        ),
                        usage=TokenUsage(1, 1, 2),
                    )
                    if self.implementer_calls == 1
                    else ModelResponse(
                        text=_role_output_text(request, fallback="implemented"),
                        usage=TokenUsage(1, 1, 2),
                    )
                )
            else:
                response = ModelResponse(
                    text=_role_output_text(
                        request,
                        fallback=f"completed by {request.logical_model}",
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="architect",
                role="Architecture Owner",
                goal="Define the contract",
                logical_model="architect_model",
                output_schema={"summary": "string"},
            ),
            AgentSpec(
                id="implementer",
                role="Implementation Owner",
                goal="Implement the contract",
                logical_model="implementer_model",
                allowed_tools=("web.search",),
                output_schema={"summary": "string"},
            ),
            AgentSpec(
                id="tester",
                role="Independent Test Owner",
                goal="Verify the implementation",
                logical_model="tester_model",
                output_schema={"summary": "string"},
            ),
            AgentSpec(
                id="synthesizer",
                role="Final Delivery Owner",
                goal="Synthesize the delivery",
                logical_model="synthesizer_model",
            ),
        ),
        steps=(
            DispatchStep(
                id="architecture",
                agent="architect",
                task="Define architecture",
                token_budget=100,
            ),
            DispatchStep(
                id="implementation",
                agent="implementer",
                task="Implement architecture",
                depends_on=("architecture",),
                tools=("web.search",),
                token_budget=100,
            ),
            DispatchStep(
                id="verification",
                agent="tester",
                task="Verify implementation",
                depends_on=("implementation",),
                token_budget=100,
            ),
            DispatchStep(
                id="final_response",
                agent="synthesizer",
                task="Synthesize verified delivery",
                depends_on=("architecture", "implementation", "verification"),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=400,
    )
    runtime = CrewDispatchRuntime(
        MultiAgentGateway(),
        plan,
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=RecordingHarnessToolGateway(),
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    expected_by_step = {
        "architecture": "architect",
        "implementation": "implementer",
        "verification": "tester",
        "final_response": "synthesizer",
    }
    for step_id, agent_id in expected_by_step.items():
        owned = [
            event
            for event in events
            if event.step_id == step_id
            and event.kind in {EventKind.STEP_STARTED, EventKind.STEP_COMPLETED}
        ]
        assert {event.kind for event in owned} == {
            EventKind.STEP_STARTED,
            EventKind.STEP_COMPLETED,
        }
        assert all(event.payload["agent_id"] == agent_id for event in owned)

        artifact_events = [
            event
            for event in events
            if event.kind is EventKind.ARTIFACT_CREATED
            and event.artifact is not None
            and event.artifact.producer == agent_id
        ]
        assert artifact_events
        assert all(event.payload["agent_id"] == agent_id for event in artifact_events)
        role_event = next(event for event in artifact_events if "role" in event.payload)
        assert role_event.payload["role"] != agent_id

    model_events = [event for event in events if event.kind is EventKind.MODEL_STARTED]
    assert {event.payload["agent_id"] for event in model_events} == set(
        expected_by_step.values()
    )
    implementer_tool_events = [
        event
        for event in events
        if event.kind
        in {
            EventKind.TOOL_REQUESTED,
            EventKind.TOOL_STARTED,
            EventKind.TOOL_COMPLETED,
        }
    ]
    assert implementer_tool_events
    assert all(event.actor == "implementer" for event in implementer_tool_events)
    completed_tool = next(
        event
        for event in implementer_tool_events
        if event.kind is EventKind.TOOL_COMPLETED
    )
    assert completed_tool.artifact is not None
    assert completed_tool.artifact.content["agent_id"] == "implementer"


async def test_orchestration_checkpoint_frontier_mismatch_reports_recovery_reason() -> None:
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _dependent_final_plan(),
        crew_factory=FastFactory(),
    )
    events = await _collect(runtime)
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    state["frontier"] = ["final_response"]
    payload["state_sha256"] = ""
    corrupted = RuntimeCheckpoint.from_payload(payload)
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _dependent_final_plan(),
        crew_factory=FastFactory(),
    )

    with pytest.raises(RuntimeExecutionError, match="orchestration checkpoint is incompatible"):
        await restored.restore_checkpoint(corrupted)


async def test_step_failed_reports_blocked_orchestration_contracts() -> None:
    runtime = CrewDispatchRuntime(
        FailingModelGateway(),
        _dependent_final_plan(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)

    draft_failed = next(
        event for event in events if event.kind is EventKind.STEP_FAILED and event.step_id == "draft"
    )
    assert draft_failed.payload["orchestration_protocol"] == "role_handoff_contract_v1"
    assert draft_failed.payload["blocked_contract_ids"] == ("draft-to-final_response",)
    assert draft_failed.payload["orchestration_recovery_hint"] == "retry_blocked_contract_chain"


async def test_unavailable_planned_capability_fails_before_model_call() -> None:
    gateway = RoleAwareGateway()
    capabilities = UnavailableCapabilities()
    runtime = CrewDispatchRuntime(
        gateway,
        _tool_plan(),
        capability_gateway=capabilities,
        crew_factory=FastFactory(),
    )

    with pytest.raises(RuntimeExecutionError, match="planned capability is unavailable"):
        await _collect(runtime)

    assert capabilities.availability_checks == [(TENANT_ID, "web.search")]
    assert gateway.requests == []
    assert capabilities.calls == []


async def test_dependent_structured_role_output_must_match_handoff_schema() -> None:
    gateway = SequenceGateway(
        '{"summary":"done","findings":["ok"],"risks":[]}',
        "final answer",
    )
    runtime = CrewDispatchRuntime(
        gateway,
        _structured_dependent_final_plan(),
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert len(gateway.requests) == 2
    draft_completed = next(
        event for event in events if event.kind is EventKind.STEP_COMPLETED and event.step_id == "draft"
    )
    assert draft_completed.payload["outgoing_contract_ids"] == ("draft-to-final_response",)
    final_completed = next(
        event
        for event in events
        if event.kind is EventKind.STEP_COMPLETED and event.step_id == "final_response"
    )
    assert final_completed.payload["completed_contract_ids"] == ("draft-to-final_response",)


@pytest.mark.parametrize(
    ("writer_output", "reason"),
    (
        ('{"summary":"done","findings":["ok"]}', "structured handoff output missing field"),
        (
            '{"summary":"done","findings":"ok","risks":[]}',
            "structured handoff output field type mismatch",
        ),
        ("plain text", "structured handoff output is not valid json"),
    ),
)
async def test_invalid_dependent_structured_role_output_blocks_handoff(
    writer_output: str,
    reason: str,
) -> None:
    gateway = SequenceGateway(writer_output, "final answer")
    runtime = CrewDispatchRuntime(
        gateway,
        _structured_dependent_final_plan(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match=reason):
        from agent_hub.runtime.crew.adapter import _validate_structured_role_output
        plan = _structured_dependent_final_plan()
        _validate_structured_role_output(plan, plan.steps[0], plan.agents[0], writer_output)
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert len(gateway.requests) == 2
    draft_failed = next(
        event for event in events if event.kind is EventKind.STEP_FAILED and event.step_id == "draft"
    )
    assert draft_failed.reason == "structured output invalid"
    assert draft_failed.payload["blocked_contract_ids"] == ("draft-to-final_response",)
    assert draft_failed.payload["orchestration_recovery_hint"] == "retry_blocked_contract_chain"
    assert not any(event.step_id == "final_response" for event in events)


async def test_fallback_dependent_plain_text_output_cannot_fabricate_handoff() -> None:
    gateway = FallbackSequenceGateway("plain fallback evidence", "final answer")
    runtime = CrewDispatchRuntime(
        gateway,
        _structured_dependent_final_plan(),
        crew_factory=FastFactory(),
    )

    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)


async def test_logical_fallback_plain_text_output_cannot_fabricate_handoff() -> None:
    gateway = LogicalFallbackSequenceGateway("plain logical fallback evidence", "final answer")
    runtime = CrewDispatchRuntime(
        gateway,
        _structured_dependent_final_plan(),
        crew_factory=FastFactory(),
    )

    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)


async def test_project_scale_artifact_plain_text_output_cannot_fabricate_handoff() -> None:
    gateway = SequenceGateway("plain project-scale evidence", "final answer")
    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={
                    "summary": "string",
                    "findings": "string[]",
                    "risks": "string[]",
                },
            ),
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="draft",
                agent="writer",
                task=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=artifact_production. Return only the role-specific result."
                ),
                token_budget=1000,
            ),
            DispatchStep(
                id="final_response",
                agent="final_synthesizer",
                task="Synthesize",
                depends_on=("draft",),
                final_synthesizer=True,
                token_budget=1000,
            ),
        ),
        total_token_budget=1000,
    )
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())

    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)


async def test_agent_output_schema_becomes_structured_model_request() -> None:
    gateway = SequenceGateway('{"summary":"done","risks":[]}')
    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string", "risks": "string[]"},
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())

    await _collect(runtime)

    request = gateway.requests[0]
    assert ModelCapability.STRUCTURED_OUTPUT in request.required_capabilities
    assert request.response_schema is not None
    assert request.response_schema.name == "DispatchRoleOutput"
    assert request.response_schema.schema == {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "string"},
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "string[]",
            },
        },
        "required": ("summary", "risks"),
        "additionalProperties": False,
    }


async def test_final_structured_role_output_must_match_schema() -> None:
    gateway = SequenceGateway("plain text")
    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string", "risks": "string[]"},
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context()):
            events.append(event)

    failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert failed.step_id == "final"
    assert failed.reason == "structured output invalid"
    assert "blocked_contract_ids" not in failed.payload


async def test_invalid_structured_output_without_provider_cost_is_not_usage_unaccounted() -> None:
    class MissingCostInvalidStructuredGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(text="plain text", usage=TokenUsage(1, 1, 2)),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=None,
            )

    gateway = MissingCostInvalidStructuredGateway()
    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                output_schema={"summary": "string", "risks": "string[]"},
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="structured output invalid"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert not any(
        event.payload.get("error_code") == "runtime.dispatch_usage_unaccounted"
        for event in events
    )
    failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert failed.reason == "structured output invalid"


async def test_reviewer_verdict_uses_structured_model_request() -> None:
    gateway = RoleAwareGateway()
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())

    await _collect(runtime)

    request = next(item for item in gateway.requests if item.logical_model == "review")
    assert ModelCapability.STRUCTURED_OUTPUT in request.required_capabilities
    assert request.response_schema is not None
    assert request.response_schema.name == "DispatchReviewVerdict"
    assert request.response_schema.schema == {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ("approve", "revise", "reject")},
            "feedback": {"type": "string"},
        },
        "required": ("verdict",),
        "additionalProperties": False,
    }


async def test_tool_calls_cross_the_harness_tool_gateway_envelope() -> None:
    capabilities = FakeCapabilities()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert capabilities.calls == []
    assert len(harness.calls) == 1
    tenant_id, request = harness.calls[0]
    assert tenant_id == TENANT_ID
    assert request.run_id == RUN_ID
    assert request.actor == "writer"
    assert request.tool_name == "web.search"
    assert request.arguments == {"q": "safe"}
    assert request.approval_required is True
    assert request.sandbox == "restricted"
    assert request.call_id == f"call-{request.idempotency_key[:32]}"
    tool_started = next(event for event in events if event.kind is EventKind.TOOL_STARTED)
    assert tool_started.payload["schema_version"] == 1
    assert tool_started.payload["status"] == "running"
    assert tool_started.payload["operation_kind"] == "generic"
    assert tool_started.payload["sandbox"] == "restricted"
    assert tool_started.payload["argument_keys"] == ("q",)
    assert tool_started.payload["argument_key_count"] == 1
    argument_bytes = tool_started.payload["argument_bytes"]
    assert isinstance(argument_bytes, int)
    assert argument_bytes > 0
    assert "arguments" not in tool_started.payload
    assert '"safe"' not in json.dumps(dict(tool_started.payload))
    tool_artifact = next(
        event.artifact for event in events if event.artifact and event.artifact.type == "tool_result"
    )
    tool_completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert tool_completed.payload["schema_version"] == 1
    assert tool_completed.payload["status"] == "succeeded"
    result_bytes = tool_completed.payload["result_bytes"]
    assert isinstance(result_bytes, int)
    assert result_bytes > 0
    assert tool_completed.payload["artifact_id"] == str(tool_artifact.id)
    assert "result" not in tool_completed.payload
    assert "harness result" not in json.dumps(dict(tool_completed.payload))
    result = tool_artifact.content["result"]
    assert isinstance(result, Mapping)
    assert result["items"] == ("harness result",)


async def test_successful_tool_result_adds_trusted_continuation_contract() -> None:
    gateway = ToolResultAwareGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert len(harness.calls) == 1
    assert len(gateway.requests) == 2
    continuation = gateway.requests[1]
    assert any(
        message.role == "system"
        and isinstance(message.content, str)
        and "CAPABILITY_RESULT_CONTINUATION" in message.content
        for message in continuation.messages
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_identical_tool_call_is_reused_and_forces_result_synthesis() -> None:
    gateway = DuplicateUntilToolsDisabledGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert len(harness.calls) == 1
    assert len(gateway.requests) == 3
    assert gateway.requests[2].tools == ()
    assert ModelCapability.TOOL_CALLING not in gateway.requests[2].required_capabilities
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_mixed_reused_results_do_not_extend_tool_round_budget() -> None:
    class MixedReuseGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if not request.tools:
                return GatewayCompletion(
                    response=ModelResponse(
                        text="Reused results synthesized.",
                        usage=TokenUsage(1, 1, 2),
                    ),
                    deployment_id="primary",
                    logical_model=request.logical_model,
                    provider_id="deepseek",
                    provider_model="deepseek/deepseek-v4-flash",
                    cost_usd=Decimal(0),
                )
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id=f"zip-{len(self.requests)}",
                            name="project_generate_zip",
                            arguments={
                                "title": "Mixed reuse",
                                "files": {"main.py": "print('ready')\n"},
                                "presentation": "final_attachment",
                            },
                        ),
                        ToolCall(
                            id=f"search-{len(self.requests)}",
                            name="web_search",
                            arguments={"q": "same query"},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class MixedReuseCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name in {"project.generate_zip", "web.search"}

    class MixedReuseHarness:
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
            payload: Mapping[str, JsonValue]
            if request.tool_name == "project.generate_zip":
                payload = {
                    "artifact_id": self.artifact_id,
                    "file": {
                        "artifact_id": self.artifact_id,
                        "filename": "mixed-reuse.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "download_url": (
                            f"/api/v1/runs/{RUN_ID}/artifacts/{self.artifact_id}/download"
                        ),
                    },
                    "presentation": "final_attachment",
                    "summary": "Generated mixed-reuse.zip.",
                }
            else:
                payload = {"items": ("same result",)}
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload=payload,
            )

    tools = ("project.generate_zip", "web.search")
    plan = DispatchPlan(
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
    gateway = MixedReuseGateway()
    harness = MixedReuseHarness()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        capability_gateway=MixedReuseCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(routing_decision={"project_scale": "medium"})
        )
    ]

    assert len(harness.calls) == 2
    assert len(gateway.requests) == 3
    assert gateway.requests[-1].tools == ()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize("reviewed", [False, True])
async def test_tool_progress_crosses_initial_step_deadline_without_retry(
    reviewed: bool,
) -> None:
    class TimedProgressGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []
            self.started_at: float | None = None

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            loop = asyncio.get_running_loop()
            if self.started_at is None:
                self.started_at = loop.time()
            self.requests.append(request)
            index = len(self.requests)
            targets = (0.10, 0.25, 0.45, 0.60, 0.65)
            await asyncio.sleep(max(0, self.started_at + targets[min(index, 5) - 1] - loop.time()))
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id=f"progress-{index}", name="web_search",
                        arguments={"q": f"unique-{index}"},
                    ),),
                    usage=TokenUsage(1, 1, 2),
                )
                if index <= 3 else ModelResponse(
                    text='{"verdict":"approve"}' if request.logical_model == "review" else "done",
                    usage=TokenUsage(1, 1, 2),
                )
            )
            return GatewayCompletion(
                response=response, deployment_id="primary", logical_model=request.logical_model,
                provider_id="deepseek", provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    plan = _tool_plan()
    step = plan.steps[0].model_copy(update={"timeout_seconds": 0.4})
    if reviewed:
        plan = plan.model_copy(update={"agents": (*plan.agents, AgentSpec(
            id="reviewer", role="reviewer", goal="Review", logical_model="review",
        ))})
        step = step.model_copy(update={"reviewer": "reviewer"})
    plan = plan.model_copy(update={"steps": (step,)})
    gateway = TimedProgressGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    completions_at: list[float] = []
    async for event in runtime.run(_context(
        timeout_seconds=5, routing_decision={"project_scale": "medium"},
    )):
        events.append(event)
        if event.kind is EventKind.TOOL_COMPLETED:
            completions_at.append(asyncio.get_running_loop().time())

    assert gateway.started_at is not None
    assert completions_at[-1] > gateway.started_at + 0.4
    assert len(harness.calls) == 3
    assert len(gateway.requests) == (5 if reviewed else 4)
    assert not any(event.kind in {EventKind.STEP_RETRYING, EventKind.STEP_FAILED} for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize("tool_progress", [False, True])
async def test_tool_progress_never_extends_the_run_deadline(tool_progress: bool) -> None:
    class StalledGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if tool_progress and not self.requests:
                return await super().complete_with_context(request)
            self.requests.append(request)
            await asyncio.Event().wait()
            raise AssertionError("stalled model unexpectedly resumed")

    plan = _tool_plan()
    plan = plan.model_copy(update={"steps": (
        plan.steps[0].model_copy(update={"timeout_seconds": 0.15}),
    )})
    gateway = StalledGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    # An independent watchdog catches any accidental removal of the run fuse.
    async with asyncio.timeout(2):
        with pytest.raises(RuntimeExecutionError):
            async for event in runtime.run(_context(timeout_seconds=0.6)):
                events.append(event)
    assert len(harness.calls) == int(tool_progress)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert any(event.kind is EventKind.RUNTIME_FAILED for event in events)


async def test_first_durable_tool_extends_timer_before_the_batch_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.crew import adapter

    class BatchGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if len(self.requests) == 1:
                return GatewayCompletion(
                    response=ModelResponse(
                        text=None,
                        tool_calls=tuple(ToolCall(
                            id=f"batch-{index}", name="web_search", arguments={"q": str(index)},
                        ) for index in range(3)),
                        usage=TokenUsage(1, 1, 2),
                    ),
                    deployment_id="primary", logical_model=request.logical_model,
                    provider_id="deepseek", provider_model="deepseek/deepseek-v4-flash",
                    cost_usd=Decimal(0),
                )
            return completion

    class DelayedSecondTool(RecordingHarnessToolGateway):
        async def invoke(
            self, tenant_id: UUID, request: HarnessToolCallRequest, *,
            user_id: UUID | None = None, role: Role | None = None,
        ) -> HarnessToolCallResult:
            if len(self.calls) == 1:
                await asyncio.sleep(0.55)
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)

    extensions: list[float] = []
    extend = adapter._tool_progress_step_deadline

    def record_extension(context: TaskContext, *, step_deadline: float, run_deadline: float) -> float:
        extensions.append(step_deadline)
        return extend(context, step_deadline=step_deadline, run_deadline=run_deadline)

    monkeypatch.setattr(adapter, "_tool_progress_step_deadline", record_extension)
    plan = _tool_plan()
    plan = plan.model_copy(update={"steps": (
        plan.steps[0].model_copy(update={"timeout_seconds": 0.4}),
    )})
    gateway = BatchGateway()
    harness = DelayedSecondTool()
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness, crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context(timeout_seconds=5))]
    assert len(harness.calls) == 3
    assert len(gateway.requests) == 2
    assert len(extensions) == 1
    assert not any(event.kind in {EventKind.STEP_RETRYING, EventKind.STEP_FAILED} for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize(
    ("project_scale", "hard_limit"),
    [
        (None, 50),
        ("small", 50),
        ("medium", 50),
        ("large", 50),
        ("ultra", 50),
    ],
)
def test_tool_round_budget_uses_dynamic_resource_cap_for_projects(
    project_scale: str | None,
    hard_limit: int,
) -> None:
    routing_decision = {} if project_scale is None else {"project_scale": project_scale}

    budget = _tool_round_budget(
        _context(routing_decision=routing_decision),
        _tool_plan().steps[0],
    )

    assert budget.initial_limit == 8
    assert budget.extension_size == 8
    assert budget.hard_limit == hard_limit


def test_tool_round_budget_does_not_depend_on_scale_metadata_shape() -> None:
    budget = _tool_round_budget(
        _context(routing_decision={"project_scale": {"unexpected": "mapping"}}),
        _tool_plan().steps[0],
    )

    assert budget.initial_limit == 8
    assert budget.extension_size == 8
    assert budget.hard_limit == 50


@pytest.mark.parametrize(
    ("token_budget", "timeout_seconds", "hard_limit"),
    [
        (10, 60.0, 8),
        (100, 60.0, 50),
        (1_000, 200.0, 200),
        (1_000_000, 3_600.0, 3_600),
    ],
)
def test_tool_round_budget_dynamic_cap_follows_step_resources(
    token_budget: int,
    timeout_seconds: float,
    hard_limit: int,
) -> None:
    step = _tool_plan().steps[0].model_copy(
        update={"token_budget": token_budget, "timeout_seconds": timeout_seconds}
    )

    budget = _tool_round_budget(
        _context(
            routing_decision={"project_scale": "small"},
            token_budget=token_budget,
            timeout_seconds=timeout_seconds,
        ),
        step,
    )

    assert budget.initial_limit == 8
    assert budget.extension_size == 8
    assert budget.hard_limit == hard_limit


@pytest.mark.parametrize(
    ("project_scale", "tool_rounds"),
    [("medium", 9), ("large", 17), ("ultra", 25)],
)
async def test_tool_round_budget_extends_only_while_new_results_arrive(
    project_scale: str,
    tool_rounds: int,
) -> None:
    class ProgressGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            request_index = len(self.requests)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id=f"progress-{request_index}",
                            name="web_search",
                            arguments={"q": f"unique-{request_index}"},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
                if request_index <= tool_rounds
                else ModelResponse(text="done", usage=TokenUsage(1, 1, 2))
            )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    gateway = ProgressGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(routing_decision={"project_scale": project_scale})
        )
    ]

    assert len(harness.calls) == tool_rounds
    assert len(gateway.requests) == tool_rounds + 1
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize(
    ("project_scale", "hard_limit"),
    [("small", 50), ("medium", 50), ("large", 50), ("ultra", 50)],
)
async def test_tool_round_budget_stops_at_audited_resource_fuse(
    project_scale: str,
    hard_limit: int,
) -> None:
    class EndlessProgressGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            request_index = len(self.requests)
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id=f"progress-{request_index}",
                            name="web_search",
                            arguments={"q": f"unique-{request_index}"},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    gateway = EndlessProgressGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    context = _context(routing_decision={"project_scale": project_scale})
    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        _ = [
            event
            async for event in runtime.run(context)
        ]

    assert len(harness.calls) == hard_limit
    assert len(gateway.requests) == hard_limit + 1
    limits = _crew_content_limits(
        context,
        max_output_tokens=max(request.max_output_tokens for request in gateway.requests),
        source_count=1,
    )
    assert max(len(request.messages) for request in gateway.requests) <= (
        limits.interaction_message_limit
    )
    assert any(
        "EARLIER_INTERACTION_WINDOW_COMPRESSED" in message.content
        for request in gateway.requests
        for message in request.messages
    )
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        EndlessProgressGateway(),
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=RecordingHarnessToolGateway(),
        crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)


async def test_crew_runtime_uses_manifest_sandbox_for_plugin_tool_facade() -> None:
    gateway = ManifestToolGateway()
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _manifest_tool_plan(),
        capability_gateway=ManifestCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(routing_decision={"sandbox_profile": "restricted"})
        )
    ]

    assert len(harness.calls) == 1
    (model_tool,) = gateway.requests[0].tools
    assert model_tool.description == (
        "Create a calendar event through the approved plugin. "
        "Failure codes: plugin.timeout, plugin.invalid_arguments."
    )
    assert model_tool.parameters["type"] == "object"
    assert model_tool.parameters["additionalProperties"] is False
    assert model_tool.parameters["required"] == ("title", "date")
    properties = model_tool.parameters["properties"]
    assert isinstance(properties, Mapping)
    assert properties["title"] == {"type": "string"}
    assert properties["date"] == {"type": "string"}
    _tenant_id, request = harness.calls[0]
    assert request.tool_name == "calendar.create_event"
    assert request.sandbox == "remote_connector"
    tool_started = next(event for event in events if event.kind is EventKind.TOOL_STARTED)
    assert tool_started.payload["sandbox"] == "remote_connector"


async def test_project_zip_failure_after_final_zip_reuses_existing_artifact() -> None:
    class RepeatedZipGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id=f"provider-{len(self.requests)}",
                            name="project_generate_zip",
                            arguments={
                                "title": "Hello World Python",
                                "files": {"main.py": "print('hello world')\n"},
                            },
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
                if len(self.requests) <= 2
                else ModelResponse(text="done", usage=TokenUsage(1, 1, 2))
            )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class ZipCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

    class FlakyZipHarnessToolGateway:
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
                            "download_url": (
                                f"/api/v1/admin/runs/{RUN_ID}/artifacts/"
                                f"{self.artifact_id}/download"
                            ),
                        },
                        "metadata": {
                            "artifact_id": self.artifact_id,
                            "filename": "hello-world-python.zip",
                            "mime_type": "application/zip",
                            "size_bytes": 128,
                            "sha256": "0" * 64,
                            "storage_key": (
                                f"{TENANT_ID}/{RUN_ID}/{self.artifact_id}/"
                                "hello-world-python.zip"
                            ),
                            "download_url": (
                                f"/api/v1/admin/runs/{RUN_ID}/artifacts/"
                                f"{self.artifact_id}/download"
                            ),
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

    harness = FlakyZipHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        RepeatedZipGateway(),
        _project_zip_plan(),
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert len(harness.calls) == 1
    assert [event.kind for event in events if event.kind is EventKind.TOOL_FAILED] == []
    assert [event.kind for event in events if event.kind is EventKind.TOOL_COMPLETED] == [
        EventKind.TOOL_COMPLETED,
        EventKind.TOOL_COMPLETED,
    ]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_project_zip_round_limit_reuses_existing_final_artifact() -> None:
    class EndlessZipGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id=f"provider-{len(self.requests)}",
                            name="project_generate_zip",
                            arguments={
                                "title": "Hello Mofang",
                                "files": {"main.py": "print('hello mofang')\n"},
                                "presentation": "final_attachment",
                            },
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class ZipCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

    class ZipHarnessToolGateway:
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
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={
                    "artifact_id": self.artifact_id,
                    "file": {
                        "artifact_id": self.artifact_id,
                        "filename": "hello-mofang.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "download_url": f"/api/v1/runs/{RUN_ID}/artifacts/{self.artifact_id}/download",
                    },
                    "presentation": "final_attachment",
                    "summary": "Generated project ZIP artifact hello-mofang.zip.",
                },
            )

    gateway = EndlessZipGateway()
    harness = ZipHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _project_zip_plan(),
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    assert len(harness.calls) == 1
    assert len(gateway.requests) == 2
    assert [event.kind for event in events if event.kind is EventKind.TOOL_FAILED] == []
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    completed = next(event for event in events if event.kind is EventKind.STEP_COMPLETED)
    assert completed.payload["artifact_id"]


async def test_final_project_zip_attachment_overrides_conflicting_denial_text() -> None:
    class ConflictingZipGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-1",
                            name="project_generate_zip",
                            arguments={
                                "title": "Main Py Only",
                                "files": {"main.py": 'print("hello sandbox button")'},
                                "presentation": "final_attachment",
                            },
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
                if len(self.requests) == 1
                else ModelResponse(
                    text="无法直接生成 zip 文件，因为当前没有暴露 harness 工具。",
                    usage=TokenUsage(1, 1, 2),
                )
            )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class ZipHarnessToolGateway:
        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            artifact_id = str(uuid4())
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={
                    "artifact_id": artifact_id,
                    "file": {
                        "artifact_id": artifact_id,
                        "filename": "main-py-only.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "download_url": (
                            f"/api/v1/runs/{RUN_ID}/artifacts/{artifact_id}/download"
                        ),
                    },
                    "presentation": "final_attachment",
                    "summary": "Generated project ZIP artifact main-py-only.zip.",
                },
            )

    class ZipCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

    runtime = CrewDispatchRuntime(
        ConflictingZipGateway(),
        _project_zip_plan(),
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=ZipHarnessToolGateway(),
        crew_factory=FastFactory(),
    )

    events = await _collect(runtime)

    completed = next(event for event in events if event.kind is EventKind.RUNTIME_COMPLETED)
    final = completed.inputs[0]
    assert final.content["text"] == "已生成可下载项目 ZIP：main-py-only.zip。"


async def test_tool_calls_forward_trusted_actor_identity_to_harness_gateway() -> None:
    user_id = UUID("00000000-0000-4000-8000-000000000003")
    harness = IdentityRecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(actor_id=user_id, actor_role=Role.OPERATOR)
        )
    ]

    assert len(harness.calls) == 1
    tenant_id, request, recorded_user_id, recorded_role = harness.calls[0]
    assert tenant_id == TENANT_ID
    assert request.actor == "writer"
    assert recorded_user_id == user_id
    assert recorded_role is Role.OPERATOR
    result = next(
        event.artifact.content["result"]
        for event in events
        if event.artifact and event.artifact.type == "tool_result"
    )
    assert result == {"items": ("identity result",)}


async def test_project_zip_workspace_write_uses_run_sandbox_for_harness_request() -> None:
    harness = RecordingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ProjectZipWorkspaceGateway(),
        _project_zip_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(routing_decision={"sandbox_profile": "workspace_write"})
        )
    ]

    assert len(harness.calls) == 1
    _tenant_id, request = harness.calls[0]
    assert request.tool_name == "project.generate_zip"
    assert request.sandbox == "workspace_write"
    started = next(event for event in events if event.kind is EventKind.TOOL_STARTED)
    assert started.payload["sandbox"] == "workspace_write"


async def test_approval_pending_project_zip_does_not_leave_started_lifecycle() -> None:
    harness = WaitingApprovalHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ProjectZipWorkspaceGateway(),
        _project_zip_plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(
            _context(routing_decision={"sandbox_profile": "workspace_write"})
        ):
            events.append(event)

    assert len(harness.calls) == 1
    assert [event.kind for event in events if event.kind == "tool.requested"] == [
        "tool.requested"
    ]
    assert [event.kind for event in events if event.kind is EventKind.TOOL_STARTED] == []
    failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed.payload["status"] == "waiting_approval"
    assert failed.payload["failure_kind"] == "waiting_approval"
    assert failed.payload["approval_id"] == "approval_project_zip"
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "waiting_approval"


@pytest.mark.parametrize("receipt_mutation", [
    None, "missing_approval_id", "blank_approval_id", "non_string_approval_id",
    "unknown_field", "approval_id_on_prepared",
])
async def test_waiting_approval_checkpoint_survives_service_stop_and_resumes_once(
    receipt_mutation: str | None,
) -> None:
    request_persisted = asyncio.Event()

    class WorkspaceGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id="write-file", name="workspace.write_text",
                        arguments={"path": "answer.txt", "content": "approved answer"},
                    ),),
                    usage=TokenUsage(1, 1, 2),
                )
                if len(self.requests) == 1
                else ModelResponse(text="Workspace written.", usage=TokenUsage(1, 1, 2))
            )
            return GatewayCompletion(
                response=response, deployment_id="primary", logical_model=request.logical_model,
                provider_id="deepseek", provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class WorkspaceCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "workspace.write_text"

    class ApprovalGateway(WaitingApprovalHarnessToolGateway):
        waiting = False
        approved = False
        executions = 0

        async def invoke(
            self, tenant_id: UUID, request: HarnessToolCallRequest, *,
            user_id: UUID | None = None, role: Role | None = None,
        ) -> HarnessToolCallResult:
            assert request.approval_required is True
            assert request.tool_name == "workspace.write_text"
            if not self.approved:
                await request_persisted.wait()
                self.waiting = True
                return await super().invoke(tenant_id, request, user_id=user_id, role=role)
            assert len(gateway.requests) == 1
            self.calls.append(request)
            self.executions += 1
            return HarnessToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name, status="succeeded",
                payload={"summary": "Workspace written."},
            )

    tools = ("workspace.write_text",)
    plan = DispatchPlan(
        agents=(AgentSpec(
            id="implementer", role="Implementer", goal="Write", logical_model="general",
            allowed_tools=tools,
        ),),
        steps=(DispatchStep(
            id="final", agent="implementer", task="Write an answer file", tools=tools,
            final_synthesizer=True, token_budget=100,
        ),),
        allowed_tools=tools, total_token_budget=100,
    )
    routing: dict[str, JsonValue] = {
        "sandbox_profile": "workspace_write",
        "project_id": "approval-probe",
        "workspace_session_id": "approval-session",
    }
    gateway = WorkspaceGateway()
    harness = ApprovalGateway()
    capabilities = WorkspaceCapabilities()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=capabilities, harness_tool_gateway=harness,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    persisted: list[RunEvent] = []
    stream = cast(CrewRunStream, runtime.run(_context(routing_decision=routing)))
    try:
        async for event in stream:
            # Match RunService: stop at the first event observed while waiting;
            # only a checkpoint is persisted at that boundary.
            if harness.waiting:
                if event.kind is EventKind.CHECKPOINT_SAVED:
                    persisted.append(event)
                break
            persisted.append(event)
            if event.kind is EventKind.TOOL_REQUESTED:
                request_persisted.set()
    finally:
        await stream.aclose()

    requested = next(event for event in persisted if event.kind is EventKind.TOOL_REQUESTED)
    latest = persisted[-1]
    assert latest.kind is EventKind.CHECKPOINT_SAVED
    assert latest.sequence > requested.sequence
    checkpoint = latest.checkpoint
    assert checkpoint is not None
    tool_states = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    (waiting,) = tool_states.values()
    assert waiting["status"] == "waiting_approval"
    assert waiting["approval_id"] == "approval_project_zip"
    assert waiting["replay_safe"] is True
    assert len(gateway.requests) == 1
    assert harness.executions == 0
    assert capabilities.calls == []

    harness.approved = True
    harness.waiting = False
    resumed = CrewDispatchRuntime(
        gateway, plan, capability_gateway=capabilities, harness_tool_gateway=harness,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    if receipt_mutation is not None:
        payload = checkpoint.to_payload()
        state = cast(dict[str, object], payload["state"])
        receipts = cast(dict[str, dict[str, object]], state["tools"])
        receipt = next(iter(receipts.values()))
        if receipt_mutation == "missing_approval_id":
            receipt.pop("approval_id")
        elif receipt_mutation == "blank_approval_id":
            receipt["approval_id"] = " "
        elif receipt_mutation == "non_string_approval_id":
            receipt["approval_id"] = True
        elif receipt_mutation == "unknown_field":
            receipt["unknown"] = True
        else:
            receipt["status"] = "prepared"
        payload["state_sha256"] = ""
        corrupted = RuntimeCheckpoint.from_payload(payload)
        with pytest.raises(RuntimeExecutionError, match="runtime checkpoint is incompatible"):
            await resumed.restore_checkpoint(corrupted)
        assert harness.executions == 0
        assert len(gateway.requests) == 1
        return
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(
        _context(checkpoint=checkpoint, routing_decision=routing),
    )]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert harness.executions == 1
    assert len(harness.calls) == 2
    assert harness.calls[0].idempotency_key == harness.calls[1].idempotency_key
    assert len(gateway.requests) == 2  # Initial request plus the new tool-result continuation.
    assert capabilities.calls == []


async def test_project_scale_artifact_text_response_synthesizes_workspace_zip() -> None:
    class TextOnlyGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text="workspace package ready",
                    usage=TokenUsage(1, 1, 2),
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class ZipCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

    class ZipHarnessToolGateway:
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
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={
                    "artifact_id": self.artifact_id,
                    "file": {
                        "artifact_id": self.artifact_id,
                        "filename": "project-scale-artifact-production.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 2048,
                        "sha256": "0" * 64,
                        "download_url": (
                            f"/api/v1/runs/{RUN_ID}/artifacts/{self.artifact_id}/download"
                        ),
                    },
                    "presentation": "final_attachment",
                    "summary": "Generated project ZIP artifact.",
                    "workspace_files": (),
                    "deliverable_quality": {
                        "requirements_satisfied": True,
                        "build_passed": True,
                        "tests_passed": True,
                        "interactive_checks_passed": True,
                        "no_placeholders": True,
                        "artifact_integrity": True,
                    },
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    },
                },
            )

    gateway = TextOnlyGateway()
    harness = ZipHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _project_scale_artifact_plan(),
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(
                request=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=dispatch."
                ),
                routing_decision={
                    "project_id": "project-scale-acceptance",
                    "workspace_session_id": "project-scale-small-dispatch",
                    "sandbox_profile": "workspace_write",
                },
            )
        )
    ]

    assert len(harness.calls) == 1
    call = harness.calls[0]
    assert call.tool_name == "project.generate_zip"
    assert call.sandbox == "workspace_write"
    assert call.arguments["project_id"] == "project-scale-acceptance"
    assert call.arguments["workspace_session_id"] == "project-scale-small-dispatch"
    files = call.arguments["files"]
    assert isinstance(files, Mapping)
    assert set(files) >= {
        "README.md",
        "PROJECT_REQUIREMENTS.md",
        "IMPLEMENTATION_PLAN.md",
        "VERIFICATION.md",
        "package.json",
        "src/main.js",
        "tests/app.test.js",
    }
    assert len(gateway.requests) == 2
    assert any(
        isinstance(message.content, str)
        and message.content.startswith("UNTRUSTED_CAPABILITY_RESULTS_JSON=")
        for message in gateway.requests[1].messages
    )
    completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert completed.artifact is not None
    assert completed.artifact.content["artifact_origin"] == "builtin_fixture"
    assert completed.payload["deliverable_quality"] == {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }
    assert completed.payload["agent_standard_verification"] == {
        "constraints_read": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_natural_large_project_writes_workspace_incrementally_before_bundle() -> None:
    class IncrementalGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.calls += 1
            responses = (
                ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id="write-preview",
                        name="workspace.write_text",
                        arguments={
                            "path": "preview.html",
                            "content": "<!doctype html><title>网盘</title>",
                        },
                    ),),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id="list-files", name="workspace.list", arguments={}
                    ),),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id="bundle",
                        name="workspace.bundle",
                        arguments={"title": "Cloud Drive"},
                    ),),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(text="Workspace bundle delivered.", usage=TokenUsage(1, 1, 2)),
            )
            return GatewayCompletion(
                response=responses[min(self.calls - 1, len(responses) - 1)],
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=Decimal(0),
            )

    class IncrementalCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name in {"workspace.write_text", "workspace.list", "workspace.bundle"}

    class IncrementalHarness:
        def __init__(self) -> None:
            self.calls: list[HarnessToolCallRequest] = []

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
            payload: Mapping[str, JsonValue] = {
                "summary": f"{request.tool_name} completed",
            }
            if request.tool_name == "workspace.bundle":
                payload = {
                    **payload,
                    "artifact_id": str(uuid4()),
                    "presentation": "final_attachment",
                    "file": {"filename": "cloud-drive.zip"},
                }
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload=payload,
            )

    task = (
        "Role mission: implement.\n"
        "User task: 编写一个网盘网站\n"
        "Project workspace delivery contract: produce complete workspace files and a downloadable bundle."
    )
    tools = ("workspace.write_text", "workspace.list", "workspace.bundle")
    plan = DispatchPlan(
        agents=(AgentSpec(
            id="implementer",
            role="Implementer",
            goal="Build the project incrementally.",
            logical_model="general",
            allowed_tools=tools,
        ),),
        steps=(DispatchStep(
            id="implementer_step",
            agent="implementer",
            task=task,
            tools=tools,
            final_synthesizer=True,
            token_budget=10_000,
            tool_argument_budget_bytes={"workspace.write_text": 512_000},
        ),),
        allowed_tools=tools,
        total_token_budget=10_000,
    )
    harness = IncrementalHarness()
    runtime = CrewDispatchRuntime(
        IncrementalGateway(),
        plan,
        capability_gateway=IncrementalCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(_context(
            actor_id=uuid4(),
            actor_role=Role.OPERATOR,
            routing_decision={
                "project_id": "cloud-drive",
                "workspace_session_id": "cloud-drive-session",
                "sandbox_profile": "workspace_write",
                "project_scale": "large",
                "project_delivery": "workspace",
                "artifact_strategy": "workspace_bundle",
            },
            token_budget=10_000,
        ))
    ]

    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text",
        "workspace.list",
        "workspace.bundle",
    ]
    assert [call.approval_required for call in harness.calls] == [True, False, True]
    workspace_events = [
        (event.kind, event.tool_name)
        for event in events
        if event.kind in {EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED}
    ]
    assert workspace_events == [
        (EventKind.TOOL_STARTED, "workspace.write_text"),
        (EventKind.TOOL_COMPLETED, "workspace.write_text"),
        (EventKind.TOOL_STARTED, "workspace.list"),
        (EventKind.TOOL_COMPLETED, "workspace.list"),
        (EventKind.TOOL_STARTED, "workspace.bundle"),
        (EventKind.TOOL_COMPLETED, "workspace.bundle"),
    ]
    bundle_completed = next(
        event
        for event in events
        if event.kind is EventKind.TOOL_COMPLETED
        and event.tool_name == "workspace.bundle"
    )
    assert bundle_completed.artifact is not None
    assert bundle_completed.artifact.content["artifact_origin"] == (
        "incremental_workspace_delivery"
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


class WorkspaceDeliverySequenceGateway:
    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert len(self.requests) <= len(self.responses), "workspace continuation exceeded its bound"
        return GatewayCompletion(
            response=self.responses[len(self.requests) - 1],
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class WorkspaceDeliverySequenceHarness:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []
        self.files: dict[str, str] = {}

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        assert tenant_id == TENANT_ID
        assert user_id is not None and role is Role.OPERATOR
        self.calls.append(request)
        payload: Mapping[str, JsonValue]
        if request.tool_name == "workspace.write_text":
            path = request.arguments["path"]
            content = request.arguments["content"]
            assert isinstance(path, str) and isinstance(content, str)
            self.files[path] = content
            payload = {
                "path": path,
                "content_bytes": len(content.encode("utf-8")),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "summary": "Workspace file written.",
            }
        else:
            assert request.tool_name == "workspace.bundle"
            payload = {
                "artifact_id": str(uuid4()),
                "presentation": "final_attachment",
                "file": {"filename": "incremental-progress.zip"},
            }
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded",
            payload=payload,
        )


def _workspace_delivery_sequence_runtime(
    gateway: WorkspaceDeliverySequenceGateway,
    harness: WorkspaceDeliverySequenceHarness,
    *,
    artifact_repository: InMemoryArtifactRepository | None = None,
    tools: tuple[str, ...] = ("workspace.write_text", "workspace.list", "workspace.bundle"),
) -> CrewDispatchRuntime:
    class WorkspaceCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name in tools

    plan = DispatchPlan(
        agents=(AgentSpec(
            id="implementer",
            role="Implementer",
            goal="Complete the business sources and tests before bundling.",
            logical_model="general",
            allowed_tools=tools,
        ),),
        steps=(DispatchStep(
            id="implementer_step",
            agent="implementer",
            task=(
                "Build the project incrementally.\n"
                "Project workspace delivery contract: produce complete workspace files "
                "and a downloadable bundle."
            ),
            tools=tools,
            final_synthesizer=True,
            token_budget=10_000,
            tool_argument_budget_bytes={"workspace.write_text": 512_000},
        ),),
        allowed_tools=tools,
        total_token_budget=10_000,
    )
    return CrewDispatchRuntime(
        gateway,
        plan,
        capability_gateway=WorkspaceCapabilities(),
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
        artifact_repository=artifact_repository,
    )


def _workspace_delivery_sequence_context() -> TaskContext:
    return _context(
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
        routing_decision={
            "project_id": "incremental-progress-project",
            "workspace_session_id": "incremental-progress-session",
            "sandbox_profile": "workspace_write",
            "project_scale": "medium",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
        token_budget=10_000,
    )


def _workspace_delivery_write_response(path: str, content: str, call_id: str) -> ModelResponse:
    return ModelResponse(
        text=None,
        tool_calls=(ToolCall(
            id=call_id,
            name="workspace.write_text",
            arguments={"path": path, "content": content},
        ),),
        usage=TokenUsage(1, 1, 2),
    )


def _workspace_delivery_bundle_response() -> ModelResponse:
    return ModelResponse(
        text=None,
        tool_calls=(ToolCall(
            id="deliver-workspace",
            name="workspace.bundle",
            arguments={"title": "Incremental business project"},
        ),),
        usage=TokenUsage(1, 1, 2),
    )


def _workspace_delivery_request_packet(
    request: ModelRequest, marker: str, *, role: str | None = None,
) -> str:
    packets = [
        message.content.partition(marker)[2]
        for message in request.messages
        if isinstance(message.content, str)
        and marker in message.content
        and (role is None or message.role == role)
    ]
    assert packets, f"missing {marker} in the next model request"
    payload, _end = json.JSONDecoder().raw_decode(packets[-1].lstrip())
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _workspace_delivery_final_attachments(events: list[RunEvent]) -> list[RunEvent]:
    attachments: list[RunEvent] = []
    for event in events:
        if event.artifact is None:
            continue
        result = event.artifact.content.get("result")
        if isinstance(result, Mapping) and result.get("presentation") == "final_attachment":
            attachments.append(event)
    return attachments


async def test_workspace_delivery_rounds_keep_assistant_history_and_cumulative_progress() -> None:
    business = "export const business = 'SOURCE_BODY_SENTINEL_A';\n" * 1000
    tests = "const expected = 'SOURCE_BODY_SENTINEL_B';\n" * 1000
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", business, "write-business"),
        _workspace_delivery_write_response("tests/business.test.ts", tests, "write-tests"),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)

    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    request = gateway.requests[2]
    history = _workspace_delivery_request_packet(
        request, "UNTRUSTED_ASSISTANT_TOOL_CALLS_JSON=", role="assistant",
    )
    progress = _workspace_delivery_request_packet(request, "WORKSPACE_DELIVERY_PROGRESS_JSON=")
    latest_results = _workspace_delivery_request_packet(request, "UNTRUSTED_CAPABILITY_RESULTS_JSON=")
    assert "workspace.write_text" in history
    assert "tests/business.test.ts" in history
    assert hashlib.sha256(tests.encode("utf-8")).hexdigest() in history
    assert f'"content_bytes": {len(tests.encode("utf-8"))}' in history
    assert "src/business.ts" in progress
    assert "tests/business.test.ts" in progress
    assert "tests/business.test.ts" in latest_results
    serialized = json.dumps([message.content for message in request.messages], ensure_ascii=False)
    assert "SOURCE_BODY_SENTINEL_A" not in serialized
    assert "SOURCE_BODY_SENTINEL_B" not in serialized
    assert len((history + progress).encode("utf-8")) <= 16_384
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text", "workspace.bundle",
    ]
    attachments = _workspace_delivery_final_attachments(events)
    assert len(attachments) == 1
    assert attachments[0].tool_name == "workspace.bundle"


async def test_workspace_delivery_early_stop_continues_once_to_missing_files_and_bundle() -> None:
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_write_response("tests/business.test.ts", "const testReady = true;\n", "write-tests"),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)

    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 5
    assert set(harness.files) == {"src/business.ts", "tests/business.test.ts"}
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text", "workspace.bundle",
    ]
    assert all(call.approval_required for call in harness.calls)
    assert len({call.idempotency_key for call in harness.calls}) == 3
    progress = _workspace_delivery_request_packet(
        gateway.requests[2], "WORKSPACE_DELIVERY_PROGRESS_JSON=",
    )
    assert "src/business.ts" in progress
    assert "tests/business.test.ts" not in progress
    attachments = _workspace_delivery_final_attachments(events)
    assert len(attachments) == 1
    assert attachments[0].tool_name == "workspace.bundle"
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 10, "cost_usd": "0"}


@pytest.mark.parametrize("path", ["tests/business.test.ts", "src/business.ts"])
async def test_workspace_delivery_new_file_progress_allows_another_delivery_correction(
    path: str,
) -> None:
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const ready = 1;\n", "first-write"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_write_response(path, "export const ready = 2;\n", "progress-write"),
        ModelResponse(text="The additional work is finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)

    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 6
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text", "workspace.bundle",
    ]
    assert harness.files[path] == "export const ready = 2;\n"
    assert all(call.approval_required for call in harness.calls)
    assert len({call.idempotency_key for call in harness.calls}) == 3
    assert len(_workspace_delivery_final_attachments(events)) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 12, "cost_usd": "0"}


async def test_workspace_delivery_same_content_rewrite_does_not_renew_delivery_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_lookup = crew_adapter._succeeded_semantic_tool_result

    def lookup_without_write_reuse(
        ledger: _ToolLedger, *, step_id: str, name: str, arguments_sha256: str,
    ) -> Artifact | None:
        # Both identical writes must execute successfully, rather than reuse a receipt.
        if name == "workspace.write_text":
            return None
        return original_lookup(
            ledger, step_id=step_id, name=name, arguments_sha256=arguments_sha256,
        )

    monkeypatch.setattr(crew_adapter, "_succeeded_semantic_tool_result", lookup_without_write_reuse)
    path = "src/business.ts"
    content = "export const ready = 1;\n"
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response(path, content, "first-write"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_write_response(path, content, "same-content-rewrite"),
        ModelResponse(text="The unchanged work is finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="project workspace bundle is missing"):
        async for event in runtime.run(_workspace_delivery_sequence_context()):
            events.append(event)

    assert len(gateway.requests) == 4
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text",
    ]
    assert all(call.arguments["path"] == path and call.arguments["content"] == content
               and call.approval_required for call in harness.calls)
    assert len({call.idempotency_key for call in harness.calls}) == 2
    assert harness.files == {path: content}
    writes = [event for event in events
              if event.kind is EventKind.TOOL_COMPLETED and event.tool_name == "workspace.write_text"]
    assert len(writes) == 2
    for event in writes:
        assert event.artifact is not None
        result = event.artifact.content["result"]
        assert isinstance(result, Mapping)
        assert result["path"] == path
        assert result["content_bytes"] == len(content.encode("utf-8"))
        assert result["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert _workspace_delivery_final_attachments(events) == []
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 8, "cost_usd": "0"}
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping) and len(tools) == 2
    assert all(isinstance(state, Mapping) and state["status"] == "succeeded"
               and state["name"] == "workspace.write_text" for state in tools.values())


async def test_workspace_delivery_progress_corrections_restore_without_rebilling() -> None:
    initial = (
        _workspace_delivery_write_response("src/business.ts", "export const ready = 1;\n", "first-write"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_write_response("tests/business.test.ts", "export const ready = 2;\n", "progress-write"),
        ModelResponse(text="The additional work is finished.", usage=TokenUsage(1, 1, 2)),
    )
    checkpoint, context, repository, harness, first_gateway = await _workspace_delivery_partial_checkpoint(
        responses=initial, expected_tools=2,
    )
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(checkpoint)

    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first_gateway.requests) == 4 and len(gateway.requests) == 2
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text", "workspace.bundle",
    ]
    restored = await runtime.save_checkpoint()
    assert restored.state["usage"] == {"tokens": 12, "cost_usd": "0"}
    assert len(_workspace_delivery_final_attachments(events)) == 1


async def test_workspace_delivery_repeated_early_stop_fails_bounded_without_fake_artifact() -> None:
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        ModelResponse(text="Everything is already done.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="workspace (bundle|delivery)"):
        async for event in runtime.run(_workspace_delivery_sequence_context()):
            events.append(event)

    assert len(gateway.requests) == 3
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text"]
    assert set(harness.files) == {"src/business.ts"}
    assert harness.calls[0].approval_required is True
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert _workspace_delivery_final_attachments(events) == []
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 6, "cost_usd": "0"}
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping) and len(tools) == 1
    assert all(isinstance(state, Mapping) and state["status"] == "succeeded" for state in tools.values())


async def _workspace_delivery_partial_checkpoint(
    *, legacy: bool = False, responses: tuple[ModelResponse, ...] | None = None,
    harness: WorkspaceDeliverySequenceHarness | None = None,
    context: TaskContext | None = None,
    tools: tuple[str, ...] = ("workspace.write_text", "workspace.list", "workspace.bundle"),
    expected_tools: int = 2,
) -> tuple[
    RuntimeCheckpoint, TaskContext, InMemoryArtifactRepository,
    WorkspaceDeliverySequenceHarness, WorkspaceDeliverySequenceGateway,
]:
    paused = asyncio.Event()
    checkpoint_ready = asyncio.Event()
    if responses is None:
        responses = (
            _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
            _workspace_delivery_write_response("tests/business.test.ts", "const testReady = true;\n", "write-tests"),
        )
    expected_models = len(responses)

    class PausingGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == expected_models:
                paused.set()
                await asyncio.Event().wait()
                raise AssertionError("cancelled unpaid request must not complete")
            return await super().complete_with_context(request)

    gateway = PausingGateway(responses)
    repository = InMemoryArtifactRepository()
    harness = harness or WorkspaceDeliverySequenceHarness()
    context = context or _workspace_delivery_sequence_context()
    runtime = _workspace_delivery_sequence_runtime(
        gateway, harness, artifact_repository=repository, tools=tools,
    )
    stream = runtime.run(context)
    assert isinstance(stream, CrewRunStream)
    # Build genuine legacy request hashes before stripping the new serialized marker.
    if legacy:
        stream._state.workspace_delivery_continuation = False
    checkpoints: list[RuntimeCheckpoint] = []

    async def consume() -> None:
        async for event in stream:
            checkpoint = event.checkpoint
            if checkpoint is None:
                continue
            models = checkpoint.state.get("models")
            tools = checkpoint.state.get("tools")
            if (
                isinstance(models, Mapping) and len(models) == expected_models
                and isinstance(tools, Mapping) and len(tools) == expected_tools
                and all(isinstance(value, Mapping) and value.get("status") == "succeeded"
                        for value in (*models.values(), *tools.values()))
            ):
                checkpoints.append(checkpoint)
                checkpoint_ready.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            await paused.wait()
            await checkpoint_ready.wait()
    finally:
        await runtime.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert len(gateway.requests) == expected_models
    return checkpoints[-1], context, repository, harness, gateway


class WorkspaceObservationHarness(WorkspaceDeliverySequenceHarness):
    def __init__(self, root: Path, context: TaskContext) -> None:
        super().__init__()
        self.results: list[HarnessToolCallResult] = []
        self.routing: dict[str, JsonValue] = {
            **context.routing_decision,
            "requested_permissions": ("workspace.read", "workspace.write"),
        }

        class ScopedRunRepository:
            async def get(inner_self, tenant_id: UUID, run_id: UUID) -> object:
                assert tenant_id == context.tenant_id and run_id == context.run_id
                return SimpleNamespace(
                    tenant_id=tenant_id, id=run_id, actor_id=context.actor_id,
                    routing_decision=self.routing,
                )

        repository = ScopedRunRepository()
        self.backend = RuntimeCapabilityGateway(
            skill_store_dir=root / "skills", project_workspace_dir=root / "projects",
            generated_artifact_dir=root / "artifacts", run_repository=repository,
            skill_sandboxes={},
        )
        self.authorized = HarnessToolGateway(
            self.backend,
            policy_gateway=DefaultRuntimeCapabilityPolicyGateway(
                ApprovalService(InMemoryApprovalStore()), repository,
            ),
            require_actor_identity=True,
        )

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.calls.append(request)
        result = await self.authorized.invoke(tenant_id, request, user_id=user_id, role=role)
        self.results.append(result)
        return result


_WORKSPACE_OBSERVATION_TOOLS = (
    "workspace.write_text", "workspace.read", "workspace.list", "workspace.bundle",
)
_WORKSPACE_OBSERVATION_BEFORE = "export type OwnTest = { before: boolean };\n"
_WORKSPACE_OBSERVATION_AFTER = "export type OwnTest = { after: number };\n"


def _workspace_observation_call(name: str, call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id, name=name,
        arguments={"path": "src/types.ts"} if name == "workspace.read" else {},
    )


def _workspace_observation_responses(name: str) -> tuple[ModelResponse, ...]:
    changed_path = "src/types.ts" if name == "workspace.read" else "src/added.ts"
    return (
        _workspace_delivery_write_response("src/types.ts", _WORKSPACE_OBSERVATION_BEFORE, "write-before"),
        ModelResponse(text=None, tool_calls=(_workspace_observation_call(name, "observe-before"),), usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_write_response(changed_path, _WORKSPACE_OBSERVATION_AFTER, "write-after"),
        ModelResponse(text=None, tool_calls=(_workspace_observation_call(name, "observe-after"),), usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    )


def _workspace_observation_value(result: Mapping[str, JsonValue], name: str) -> JsonValue:
    if name == "workspace.read":
        return result["text"]
    rows = result["workspace_files"]
    assert isinstance(rows, tuple)
    paths = []
    for row in rows:
        assert isinstance(row, Mapping) and isinstance(row["path"], str)
        paths.append(row["path"])
    return tuple(sorted(paths))


@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
async def test_workspace_delivery_observation_does_not_renew_delivery_correction(
    tmp_path: Path, name: str,
) -> None:
    context = _workspace_delivery_sequence_context()
    harness = WorkspaceObservationHarness(tmp_path, context)
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/types.ts", _WORKSPACE_OBSERVATION_BEFORE, "write-before"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        ModelResponse(text=None, tool_calls=(_workspace_observation_call(name, "observe-only"),), usage=TokenUsage(1, 1, 2)),
        ModelResponse(text="Everything is already finished.", usage=TokenUsage(1, 1, 2)),
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=_WORKSPACE_OBSERVATION_TOOLS)
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="workspace (bundle|delivery)"):
        async for event in runtime.run(context):
            events.append(event)

    assert len(gateway.requests) == 4
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", name]
    assert all(result.status == "succeeded" for result in harness.results)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert _workspace_delivery_final_attachments(events) == []


@pytest.mark.parametrize("legacy", (False, True))
@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
async def test_workspace_delivery_observation_refreshes_after_changed_write(
    tmp_path: Path, name: str, legacy: bool,
) -> None:
    context = _workspace_delivery_sequence_context()
    harness = WorkspaceObservationHarness(tmp_path, context)
    gateway = WorkspaceDeliverySequenceGateway(_workspace_observation_responses(name))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=_WORKSPACE_OBSERVATION_TOOLS)
    stream = runtime.run(context)
    assert isinstance(stream, CrewRunStream)
    stream._state.workspace_delivery_continuation = not legacy

    events: list[RunEvent] = []
    if legacy:
        with pytest.raises(RuntimeExecutionError, match="step repeated an identical capability after result synthesis"):
            async for event in stream:
                events.append(event)
    else:
        events = [event async for event in stream]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    observations = [result for result in harness.results if result.tool_name == name]
    assert len(observations) == (1 if legacy else 2)
    assert all(result.status == "succeeded" for result in harness.results)
    before: JsonValue = _WORKSPACE_OBSERVATION_BEFORE if name == "workspace.read" else ("src/types.ts",)
    after: JsonValue = _WORKSPACE_OBSERVATION_AFTER if name == "workspace.read" else ("src/added.ts", "src/types.ts")
    assert _workspace_observation_value(observations[0].payload, name) == before
    if not legacy:
        assert _workspace_observation_value(observations[1].payload, name) == after
    marker = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
    result_message = next(message for message in reversed(gateway.requests[4].messages) if isinstance(message.content, str) and message.content.startswith(marker))
    assert isinstance(result_message.content, str)
    packet = json.loads(result_message.content[len(marker):])
    result = packet[0]["result"]
    if name == "workspace.read":
        assert result["text"] == (before if legacy else after)
    else:
        assert tuple(sorted(row["path"] for row in result["workspace_files"])) == (before if legacy else after)
    assert bool(gateway.requests[4].tools) is (not legacy)


@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
async def test_workspace_delivery_observation_refreshes_at_distinct_tool_index(
    tmp_path: Path, name: str,
) -> None:
    context = _workspace_delivery_sequence_context()
    harness = WorkspaceObservationHarness(tmp_path, context)
    responses = _workspace_observation_responses(name)
    changed_write = responses[2].tool_calls[0]
    gateway = WorkspaceDeliverySequenceGateway((
        responses[0],
        ModelResponse(text=None, tool_calls=(
            _workspace_observation_call(name, "observe-before"), changed_write,
            _workspace_observation_call(name, "observe-after"),
        ), usage=TokenUsage(1, 1, 2)),
        responses[4], responses[5],
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=_WORKSPACE_OBSERVATION_TOOLS)

    events = [event async for event in runtime.run(context)]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    observations = [result for result in harness.results if result.tool_name == name]
    assert len(observations) == 2
    expected: list[JsonValue] = (
        [_WORKSPACE_OBSERVATION_BEFORE, _WORKSPACE_OBSERVATION_AFTER]
        if name == "workspace.read" else [("src/types.ts",), ("src/added.ts", "src/types.ts")]
    )
    assert [_workspace_observation_value(result.payload, name) for result in observations] == expected
    checkpoint = await runtime.save_checkpoint()
    tools = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    observation_states = [state for state in tools.values() if state["name"] == name]
    assert {state["tool_index"] for state in observation_states} == {0, 2}
    assert len({state["trigger_model_artifact_id"] for state in observation_states}) == 1


@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
@pytest.mark.parametrize("same_response", (False, True))
async def test_workspace_delivery_observation_restore_preserves_same_model_snapshot(
    tmp_path: Path, name: str, same_response: bool,
) -> None:
    context = _workspace_delivery_sequence_context()
    harness = WorkspaceObservationHarness(tmp_path, context)
    responses = _workspace_observation_responses(name)
    seeded = responses[:2]
    remainder = responses[2:]
    if same_response:
        seeded = (
            responses[0],
            ModelResponse(text=None, tool_calls=(
                _workspace_observation_call(name, "observe-before"), responses[2].tool_calls[0],
                _workspace_observation_call(name, "observe-after"),
            ), usage=TokenUsage(1, 1, 2)),
        )
        remainder = responses[4:]
    checkpoint, context, repository, _, first_gateway = await _workspace_delivery_partial_checkpoint(
        responses=seeded, harness=harness, context=context,
        tools=_WORKSPACE_OBSERVATION_TOOLS, expected_tools=4 if same_response else 2,
    )
    assert len(harness.calls) == (4 if same_response else 2)
    original_ids = {str(state["artifact_id"]) for state in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"]).values()}
    gateway = WorkspaceDeliverySequenceGateway(remainder)
    runtime = _workspace_delivery_sequence_runtime(
        gateway, harness, artifact_repository=repository, tools=_WORKSPACE_OBSERVATION_TOOLS,
    )
    await runtime.restore_checkpoint(checkpoint)

    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first_gateway.requests) == 2
    assert len(gateway.requests) == (2 if same_response else 4)
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", name, "workspace.write_text", name, "workspace.bundle"]
    restored = await runtime.save_checkpoint()
    assert restored.state["usage"] == {"tokens": 8 if same_response else 12, "cost_usd": "0"}
    restored_ids = {str(state["artifact_id"]) for state in cast(Mapping[str, Mapping[str, JsonValue]], restored.state["tools"]).values()}
    assert original_ids.issubset(restored_ids)
    observations = [result for result in harness.results if result.tool_name == name]
    expected_after: JsonValue = _WORKSPACE_OBSERVATION_AFTER if name == "workspace.read" else ("src/added.ts", "src/types.ts")
    assert _workspace_observation_value(observations[-1].payload, name) == expected_after
    next_request = gateway.requests[0 if same_response else 2]
    assert next_request.tools
    marker = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
    result_message = next(message for message in reversed(next_request.messages) if isinstance(message.content, str) and message.content.startswith(marker))
    assert isinstance(result_message.content, str)
    packet = json.loads(result_message.content[len(marker):])
    observed = [item["result"] for item in packet if item["name"] == name]
    if name == "workspace.read":
        assert [item["text"] for item in observed] == (
            [_WORKSPACE_OBSERVATION_BEFORE, _WORKSPACE_OBSERVATION_AFTER]
            if same_response else [_WORKSPACE_OBSERVATION_AFTER]
        )
    else:
        assert [tuple(sorted(row["path"] for row in item["workspace_files"])) for item in observed] == (
            [("src/types.ts",), ("src/added.ts", "src/types.ts")]
            if same_response else [("src/added.ts", "src/types.ts")]
        )


@pytest.mark.parametrize("name", ("workspace.read", "workspace.list"))
async def test_workspace_delivery_observation_reauthorizes_after_permission_revoked(
    tmp_path: Path, name: str,
) -> None:
    context = _workspace_delivery_sequence_context()
    harness = WorkspaceObservationHarness(tmp_path, context)

    class RevokingGateway(WorkspaceDeliverySequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 3:
                harness.routing["requested_permissions"] = ("workspace.write",)
            return await super().complete_with_context(request)

    gateway = RevokingGateway(_workspace_observation_responses(name))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, tools=_WORKSPACE_OBSERVATION_TOOLS)
    events: list[RunEvent] = []
    if name == "workspace.read":
        events = [event async for event in runtime.run(context)]
        failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
        assert failed.reason == "workspace read denied or scoped file unavailable"
        assert failed.payload["status"] == "rejected"
        assert not any(
            event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == failed.tool_call_id
            for event in events
        )
        feedback = json.loads(_workspace_delivery_request_packet(
            gateway.requests[4], "UNTRUSTED_CAPABILITY_REJECTIONS_JSON=",
        ))
        assert feedback[0]["result"] == {
            "status": "rejected",
            "error_code": "workspace_read_unavailable",
            "message": "workspace read denied or scoped file unavailable",
            "tool_name": "workspace.read",
        }
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    else:
        with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
            _ = [event async for event in runtime.run(context)]
    observations = [result for result in harness.results if result.tool_name == name]
    assert len(observations) == 2
    assert observations[0].status == "succeeded" and observations[1].status == "failed"
    assert observations[1].payload == {}
    if name == "workspace.list":
        assert "workspace.bundle" not in [call.tool_name for call in harness.calls]


async def test_workspace_delivery_checkpoint_keeps_new_history_without_replaying_success() -> None:
    checkpoint, context, repository, harness, first_gateway = await _workspace_delivery_partial_checkpoint()
    assert checkpoint.state.get("workspace_delivery_continuation") is True
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(checkpoint)

    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first_gateway.requests) == 2 and len(gateway.requests) == 2
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text", "workspace.bundle",
    ]
    history = _workspace_delivery_request_packet(
        gateway.requests[0], "UNTRUSTED_ASSISTANT_TOOL_CALLS_JSON=", role="assistant",
    )
    assert "tests/business.test.ts" in history
    progress = _workspace_delivery_request_packet(gateway.requests[0], "WORKSPACE_DELIVERY_PROGRESS_JSON=")
    assert "src/business.ts" in progress and "tests/business.test.ts" in progress
    restored = await runtime.save_checkpoint()
    assert restored.state["usage"] == {"tokens": 8, "cost_usd": "0"}


async def test_workspace_delivery_legacy_checkpoint_keeps_old_request_hashes_without_replay() -> None:
    checkpoint, context, repository, harness, first_gateway = await _workspace_delivery_partial_checkpoint(legacy=True)
    payload = checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    state.pop("workspace_delivery_continuation", None)
    payload["state_sha256"] = ""
    legacy_checkpoint = RuntimeCheckpoint.from_payload(payload)
    assert "workspace_delivery_continuation" not in legacy_checkpoint.state
    assert legacy_checkpoint.state["models"] == checkpoint.state["models"]
    assert legacy_checkpoint.runtime_version == checkpoint.runtime_version == "11"
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(legacy_checkpoint)

    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": legacy_checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first_gateway.requests) == 2 and len(gateway.requests) == 2
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text", "workspace.write_text", "workspace.bundle",
    ]
    for request in gateway.requests:
        serialized = json.dumps([message.content for message in request.messages])
        assert "UNTRUSTED_ASSISTANT_TOOL_CALLS_JSON=" not in serialized
        assert "WORKSPACE_DELIVERY_PROGRESS_JSON=" not in serialized
    restored = await runtime.save_checkpoint()
    assert restored.state.get("workspace_delivery_continuation") is False
    assert restored.state["usage"] == {"tokens": 8, "cost_usd": "0"}


@pytest.mark.parametrize("marker", [None, 0, 1, "true", [], {}])
async def test_workspace_delivery_checkpoint_rejects_nonbool_marker(marker: JsonValue) -> None:
    checkpoint, _context_value, repository, harness, _gateway = await _workspace_delivery_partial_checkpoint()
    payload = checkpoint.to_payload()
    state = cast(dict[str, JsonValue], payload["state"])
    state["workspace_delivery_continuation"] = marker
    payload["state_sha256"] = ""
    invalid = RuntimeCheckpoint.from_payload(payload)
    assert invalid.state_sha256 == invalid.recompute_state_sha256()
    assert type(invalid.state["workspace_delivery_continuation"]) is not bool
    gateway = WorkspaceDeliverySequenceGateway(())
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    calls_before = len(harness.calls)

    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await runtime.restore_checkpoint(invalid)

    assert gateway.requests == []
    assert len(harness.calls) == calls_before


async def test_workspace_delivery_checkpoint_replays_early_stop_before_future_bundle() -> None:
    checkpoint, context, repository, harness, first_gateway = await _workspace_delivery_partial_checkpoint(
        responses=(
            _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
            ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
            _workspace_delivery_bundle_response(),
        ),
    )
    gateway = WorkspaceDeliverySequenceGateway((
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(checkpoint)

    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first_gateway.requests) == 3 and len(gateway.requests) == 1
    results = _workspace_delivery_request_packet(gateway.requests[0], "UNTRUSTED_CAPABILITY_RESULTS_JSON=")
    assert "workspace.bundle" in results and "incremental-progress.zip" in results
    bundle_states = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    bundle_id = next(value["artifact_id"] for value in bundle_states.values() if value["name"] == "workspace.bundle")
    assert any(event.artifact is not None and bundle_id in event.artifact.source_ids for event in events)
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    restored = await runtime.save_checkpoint()
    assert restored.state["usage"] == {"tokens": 8, "cost_usd": "0"}


@pytest.mark.parametrize("legacy", [False, True])
async def test_workspace_delivery_checkpoint_accepts_current_attempt_prior_bundle(legacy: bool) -> None:
    checkpoint, context, repository, harness, first_gateway = await _workspace_delivery_partial_checkpoint(
        legacy=legacy,
        responses=(
            _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
            _workspace_delivery_bundle_response(),
        ),
    )
    gateway = WorkspaceDeliverySequenceGateway((
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    runtime = _workspace_delivery_sequence_runtime(gateway, harness, artifact_repository=repository)
    await runtime.restore_checkpoint(checkpoint)

    events = [event async for event in runtime.run(context.model_copy(update={"checkpoint": checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(first_gateway.requests) == 2 and len(gateway.requests) == 1
    results = _workspace_delivery_request_packet(gateway.requests[0], "UNTRUSTED_CAPABILITY_RESULTS_JSON=")
    assert "workspace.bundle" in results and "incremental-progress.zip" in results
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    serialized = json.dumps([message.content for message in gateway.requests[0].messages])
    assert ("WORKSPACE_DELIVERY_PROGRESS_JSON=" in serialized) is (not legacy)
    assert ("UNTRUSTED_ASSISTANT_TOOL_CALLS_JSON=" in serialized) is (not legacy)


@pytest.mark.parametrize("legacy", [False, True])
async def test_workspace_delivery_compact_retry_requests_fresh_bundle(legacy: bool) -> None:
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)
    stream = runtime.run(_workspace_delivery_sequence_context())
    assert isinstance(stream, CrewRunStream)
    stream._state.workspace_delivery_continuation = not legacy

    events = [event async for event in stream]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 5
    retries = [event for event in events if event.kind is EventKind.STEP_RETRYING]
    assert len(retries) == 1 and retries[0].reason == "model response text is empty"
    assert [call.tool_name for call in harness.calls] == (
        ["workspace.write_text", "workspace.bundle"] if legacy
        else ["workspace.write_text", "workspace.bundle", "workspace.bundle"]
    )
    checkpoint = await runtime.save_checkpoint()
    tools = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    bundles = sorted(
        (value for value in tools.values() if value["name"] == "workspace.bundle"),
        key=lambda value: (cast(int, value["attempt"]), cast(int, value["round"])),
    )
    assert [value["attempt"] for value in bundles] == ([0] if legacy else [0, 1])
    assert all(value["status"] == "succeeded" for value in bundles)
    assert len({value["artifact_id"] for value in bundles}) == (1 if legacy else 2)
    results = _workspace_delivery_request_packet(gateway.requests[-1], "UNTRUSTED_CAPABILITY_RESULTS_JSON=")
    assert "workspace.bundle" in results and "incremental-progress.zip" in results
    final_artifacts = [
        event.artifact for event in events
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None and event.artifact.type == "text"
    ]
    assert bundles[-1]["artifact_id"] in final_artifacts[-1].source_ids
    assert checkpoint.state["usage"] == {"tokens": 8, "cost_usd": "0"}


@pytest.mark.parametrize("legacy", [False, True])
async def test_workspace_delivery_changed_write_requests_fresh_bundle(legacy: bool) -> None:
    class SnapshotHarness(WorkspaceDeliverySequenceHarness):
        def __init__(self) -> None:
            super().__init__()
            self.bundle_snapshots: list[dict[str, str]] = []

        async def invoke(
            self, tenant_id: UUID, request: HarnessToolCallRequest, *,
            user_id: UUID | None = None, role: Role | None = None,
        ) -> HarnessToolCallResult:
            result = await super().invoke(tenant_id, request, user_id=user_id, role=role)
            if request.tool_name == "workspace.bundle":
                self.bundle_snapshots.append(dict(self.files))
            return result

    before = "export const revision = 1;\n"
    after = "export const revision = 2;\n"
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", before, "write-before"),
        _workspace_delivery_bundle_response(),
        _workspace_delivery_write_response("src/business.ts", after, "write-after"),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = SnapshotHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)
    stream = runtime.run(_workspace_delivery_sequence_context())
    assert isinstance(stream, CrewRunStream)
    stream._state.workspace_delivery_continuation = not legacy

    events = [event async for event in stream]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 5
    assert [call.tool_name for call in harness.calls] == (
        ["workspace.write_text", "workspace.bundle", "workspace.write_text"] if legacy
        else ["workspace.write_text", "workspace.bundle", "workspace.write_text", "workspace.bundle"]
    )
    assert harness.files == {"src/business.ts": after}
    assert harness.bundle_snapshots == (
        [{"src/business.ts": before}] if legacy
        else [{"src/business.ts": before}, {"src/business.ts": after}]
    )
    assert (harness.bundle_snapshots[-1] == harness.files) is (not legacy)
    checkpoint = await runtime.save_checkpoint()
    tools = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    bundles = sorted(
        (value for value in tools.values() if value["name"] == "workspace.bundle"),
        key=lambda value: (cast(int, value["attempt"]), cast(int, value["round"])),
    )
    assert [value["round"] for value in bundles] == ([1] if legacy else [1, 3])
    assert all(value["attempt"] == 0 and value["status"] == "succeeded" for value in bundles)
    assert len({value["artifact_id"] for value in bundles}) == (1 if legacy else 2)
    attachments = _workspace_delivery_final_attachments(events)
    assert len(attachments) == (1 if legacy else 2)
    assert attachments[-1].artifact is not None
    assert str(attachments[-1].artifact.id) == bundles[-1]["artifact_id"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    ("bundle_attempt", "bundle_round"),
    [(0, 1), (0, 2), (1, 0)],
    ids=["current-round", "future-round", "other-attempt"],
)
async def test_workspace_delivery_bundle_causality_boundary(
    monkeypatch: pytest.MonkeyPatch, legacy: bool, bundle_attempt: int, bundle_round: int,
) -> None:
    candidate = Artifact(
        id=uuid4(), type="tool_result", producer="implementer",
        content={"result": {
            "artifact_id": str(uuid4()), "presentation": "final_attachment",
            "file": {"filename": "out-of-scope.zip"},
        }},
        source_ids=(str(uuid4()),),
    )
    original_lookup = crew_adapter._succeeded_semantic_tool_result

    def lookup_with_out_of_scope_bundle(
        ledger: _ToolLedger, *, step_id: str, name: str, arguments_sha256: str,
    ) -> Artifact | None:
        # Inject at the ledger boundary; keep actual semantic lookup and delivery gates.
        ledger.states["out-of-scope-bundle"] = {
            "status": "succeeded", "step_id": step_id, "attempt": bundle_attempt,
            "round": bundle_round, "tool_index": 0, "name": "workspace.bundle",
            "arguments_sha256": "a" * 64,
        }
        ledger.artifacts["out-of-scope-bundle"] = candidate
        return original_lookup(ledger, step_id=step_id, name=name, arguments_sha256=arguments_sha256)

    monkeypatch.setattr(crew_adapter, "_succeeded_semantic_tool_result", lookup_with_out_of_scope_bundle)
    gateway = WorkspaceDeliverySequenceGateway((
        _workspace_delivery_write_response("src/business.ts", "export const ready = true;\n", "write-business"),
        ModelResponse(text="Implementation finished.", usage=TokenUsage(1, 1, 2)),
        _workspace_delivery_bundle_response(),
        ModelResponse(text="Workspace delivered.", usage=TokenUsage(1, 1, 2)),
    ))
    harness = WorkspaceDeliverySequenceHarness()
    runtime = _workspace_delivery_sequence_runtime(gateway, harness)
    stream = runtime.run(_workspace_delivery_sequence_context())
    assert isinstance(stream, CrewRunStream)
    stream._state.workspace_delivery_continuation = not legacy

    events = [event async for event in stream]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == (2 if legacy else 4), "out-of-scope bundle must not satisfy new delivery contract"
    assert [call.tool_name for call in harness.calls] == (
        ["workspace.write_text"] if legacy
        else ["workspace.write_text", "workspace.bundle"]
    )
    attachments = _workspace_delivery_final_attachments(events)
    assert len(attachments) == (0 if legacy else 1)
    assert all(event.artifact is not None and event.artifact.id != candidate.id for event in attachments)
    serialized = json.dumps([message.content for request in gateway.requests for message in request.messages])
    assert ("WORKSPACE_DELIVERY_PROGRESS_JSON=" in serialized) is (not legacy)
    assert ("WORKSPACE_DELIVERY_CONTINUATION:" in serialized) is (not legacy)


@pytest.mark.parametrize(("round_index", "expected_key"), [(1, "z-old"), (2, "a-latest")])
def test_workspace_delivery_progress_uses_latest_coordinates_after_sorted_ledger_restore(
    round_index: int, expected_key: str,
) -> None:
    writes = (
        ("z-old", 0, 9, "export const revision = 'old';\n"),
        ("b-middle", 1, 0, "export const revision = 'middle';\n"),
        ("a-latest", 1, 1, "export const revision = 'latest';\n"),
        ("c-current", 2, 0, "export const revision = 'current';\n"),
        ("d-future", 3, 0, "export const revision = 'future';\n"),
    )
    ledger = _ToolLedger()
    metadata: dict[str, dict[str, JsonValue]] = {}
    for key, tool_round, tool_index, content in writes:
        ledger.states[key] = {
            "status": "succeeded",
            "step_id": "implementer_step",
            "attempt": 0,
            "round": tool_round,
            "tool_index": tool_index,
            "name": "workspace.write_text",
        }
        metadata[key] = {
            "path": "src/business.ts",
            "size_bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        ledger.artifacts[key] = Artifact(
            id=uuid4(),
            type="tool_result",
            producer="implementer",
            content={"result": metadata[key]},
        )
    restored = _ToolLedger(
        states=cast(dict[str, Mapping[str, JsonValue]], json.loads(json.dumps(ledger.states, sort_keys=True))),
        artifacts=dict(ledger.artifacts),
    )
    expected = "WORKSPACE_DELIVERY_PROGRESS_JSON=" + json.dumps(
        {"files": [metadata[expected_key]], "file_count": 1, "omitted_files": 0},
        sort_keys=True, separators=(",", ":"),
    )
    original_message = _workspace_delivery_progress(
        ledger, step_id="implementer_step", attempt=0, round_index=round_index, max_bytes=4096,
    )
    restored_message = _workspace_delivery_progress(
        restored, step_id="implementer_step", attempt=0, round_index=round_index, max_bytes=4096,
    )

    assert original_message.content == expected
    assert restored_message.content == expected
    assert original_message.role == restored_message.role == "user"
    assert original_message.content == restored_message.content


@pytest.mark.parametrize("call_count", [1, 16])
def test_workspace_delivery_assistant_history_512_budget_includes_prefix_without_source(
    call_count: int,
) -> None:
    content = "export const privateBody = 'HISTORY_SOURCE_BODY_SENTINEL';\n" * 1000
    calls = tuple(
        ToolCall(
            id=f"write-{index}",
            name="workspace.write_text",
            arguments={
                "path": "src/" + "business_component/" * 8 + f"module_{index}.ts",
                "content": content,
            },
        )
        for index in range(call_count)
    )

    message = _workspace_assistant_tool_history(calls, max_bytes=512)

    assert message.role == "assistant"
    assert isinstance(message.content, str)
    prefix = "UNTRUSTED_ASSISTANT_TOOL_CALLS_JSON="
    assert message.content.startswith(prefix)
    assert len(message.content.encode("utf-8")) <= 512
    assert "HISTORY_SOURCE_BODY_SENTINEL" not in message.content
    payload = json.loads(message.content.removeprefix(prefix))
    if call_count == 16:
        assert isinstance(payload, dict)
        assert set(payload) == {"call_count", "details_omitted", "sha256"}
        assert payload["call_count"] == 16 and payload["details_omitted"] is True
        assert isinstance(payload["sha256"], str) and len(payload["sha256"]) == 64
        assert set(payload["sha256"]) <= set("0123456789abcdef")
    else:
        assert isinstance(payload, list) and len(payload) == 1
        assert payload[0]["name"] == "workspace.write_text"
        assert payload[0]["arguments"] == {
            "path": calls[0].arguments["path"],
            "content_bytes": len(content.encode("utf-8")),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }


def test_workspace_delivery_progress_512_budget_includes_prefix_and_omitted_count() -> None:
    source = "export const privateBody = 'PROGRESS_SOURCE_BODY_SENTINEL';\n" * 1000
    ledger = _ToolLedger()
    for index in range(16):
        key = f"write-{index:02d}"
        ledger.states[key] = {
            "status": "succeeded", "step_id": "implementer_step", "attempt": 0,
            "round": 0, "tool_index": index, "name": "workspace.write_text",
        }
        ledger.artifacts[key] = Artifact(
            id=uuid4(), type="tool_result", producer="implementer",
            content={"result": {
                "path": "src/" + "business_component/" * 8 + f"module_{index}.ts",
                "size_bytes": len(source.encode("utf-8")),
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "content": source,
            }},
        )

    message = _workspace_delivery_progress(
        ledger, step_id="implementer_step", attempt=0, round_index=1, max_bytes=512,
    )

    assert message.role == "user"
    assert isinstance(message.content, str)
    prefix = "WORKSPACE_DELIVERY_PROGRESS_JSON="
    assert message.content.startswith(prefix)
    assert len(message.content.encode("utf-8")) <= 512
    assert "PROGRESS_SOURCE_BODY_SENTINEL" not in message.content
    payload = json.loads(message.content.removeprefix(prefix))
    assert payload["file_count"] == 16
    assert 0 < len(payload["files"]) < 16
    assert payload["omitted_files"] == 16 - len(payload["files"])
    assert payload["omitted_files"] >= 10
    assert all(set(item) == {"path", "size_bytes", "sha256"} for item in payload["files"])


def test_workspace_delivery_progress_filters_failed_other_step_and_other_attempt() -> None:
    ledger = _ToolLedger()
    for key, status, step_id, attempt in (
        ("valid", "succeeded", "implementer_step", 0),
        ("failed", "failed", "implementer_step", 0),
        ("other-step", "succeeded", "reviewer_step", 0),
        ("other-attempt", "succeeded", "implementer_step", 1),
    ):
        ledger.states[key] = {
            "status": status, "step_id": step_id, "attempt": attempt,
            "round": 0, "tool_index": 0, "name": "workspace.write_text",
        }
        ledger.artifacts[key] = Artifact(
            id=uuid4(), type="tool_result", producer="implementer",
            content={"result": {"path": f"src/{key}.ts", "size_bytes": 7, "sha256": "a" * 64}},
        )

    message = _workspace_delivery_progress(
        ledger, step_id="implementer_step", attempt=0, round_index=1, max_bytes=512,
    )

    assert isinstance(message.content, str)
    payload = json.loads(message.content.removeprefix("WORKSPACE_DELIVERY_PROGRESS_JSON="))
    assert payload == {
        "files": [{"path": "src/valid.ts", "size_bytes": 7, "sha256": "a" * 64}],
        "file_count": 1,
        "omitted_files": 0,
    }


async def test_workspace_repair_prunes_obsolete_files_before_bundle() -> None:
    order: list[tuple[str, object]] = []

    class RepairGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.calls += 1
            files = {
                "package.json": '{"scripts":{"test":"node --test"}}',
                "README.md": "# Repaired project\n",
                "IMPLEMENTATION_PLAN.md": "# Plan\n",
                "VERIFICATION.md": "# Verification\n",
                "src/main.js": "export const ready = true;\n",
                "tests/app.test.js": "import assert from 'node:assert';\nassert.ok(true);\n",
            }
            responses = (
                ModelResponse(
                    text=None,
                    tool_calls=tuple(
                        ToolCall(
                            id=f"write-{index}",
                            name="workspace.write_text",
                            arguments={"path": path, "content": content},
                        )
                        for index, (path, content) in enumerate(files.items())
                    ),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id="bundle",
                        name="workspace.bundle",
                        arguments={"title": "Repaired project"},
                    ),),
                    usage=TokenUsage(1, 1, 2),
                ),
                ModelResponse(text="Workspace repair delivered.", usage=TokenUsage(1, 1, 2)),
            )
            return GatewayCompletion(
                response=responses[min(self.calls - 1, len(responses) - 1)],
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=Decimal(0),
            )

    class RepairCapabilities(FakeCapabilities):
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
            del tenant_id, run_id, actor, idempotency_key
            order.append((name, dict(arguments)))
            return {"removed_paths": ("src/domain/validation.ts",)}

        def is_replay_safe(self, name: str) -> bool:
            return name in {"workspace.write_text", "workspace.bundle", "workspace.prune"}

    class RepairHarness:
        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            order.append((request.tool_name, dict(request.arguments)))
            payload: Mapping[str, JsonValue] = {"summary": "completed"}
            if request.tool_name == "workspace.bundle":
                payload = {
                    "artifact_id": str(uuid4()),
                    "presentation": "final_attachment",
                    "file": {"filename": "repaired-project.zip"},
                }
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload=payload,
            )

    task = (
        "Role mission: repair.\n"
        "User task: repair the project\n"
        "Project workspace delivery contract: produce complete workspace files and a downloadable bundle."
    )
    tools = ("workspace.write_text", "workspace.bundle")
    plan = DispatchPlan(
        agents=(AgentSpec(
            id="implementer",
            role="Implementer",
            goal="Repair the project incrementally.",
            logical_model="general",
            allowed_tools=tools,
        ),),
        steps=(DispatchStep(
            id="implementer_step",
            agent="implementer",
            task=task,
            tools=tools,
            final_synthesizer=True,
            token_budget=10_000,
            tool_argument_budget_bytes={"workspace.write_text": 512_000},
        ),),
        allowed_tools=tools,
        total_token_budget=10_000,
    )
    runtime = CrewDispatchRuntime(
        RepairGateway(),
        plan,
        capability_gateway=RepairCapabilities(),
        harness_tool_gateway=RepairHarness(),
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(_context(
            actor_id=uuid4(),
            actor_role=Role.OPERATOR,
            routing_decision={
                "project_id": "repaired-project",
                "workspace_session_id": "repaired-project-session",
                "sandbox_profile": "workspace_write",
                "project_scale": "large",
                "project_delivery": "workspace",
                "artifact_strategy": "workspace_bundle",
                "replace_workspace_files": True,
            },
            token_budget=10_000,
        ))
    ]

    names = [name for name, _arguments in order]
    assert names == [
        *("workspace.write_text" for _index in range(6)),
        "workspace.prune",
        "workspace.bundle",
    ]
    prune_arguments = order[-2][1]
    assert isinstance(prune_arguments, Mapping)
    assert prune_arguments["keep_paths"] == (
        "IMPLEMENTATION_PLAN.md",
        "README.md",
        "VERIFICATION.md",
        "package.json",
        "src/main.js",
        "tests/app.test.js",
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_failed_harness_tool_result_records_failed_not_uncertain() -> None:
    capabilities = FakeCapabilities()
    harness = FailingHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert capabilities.calls == []
    assert len(harness.calls) == 1
    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "tool unavailable"
    ]
    failed_event = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed_event.payload["schema_version"] == 1
    assert failed_event.payload["status"] == "failed"
    assert failed_event.payload["failure_kind"] == "capability_failed"
    assert failed_event.payload["argument_keys"] == ("q",)
    assert "arguments" not in failed_event.payload
    assert '"safe"' not in json.dumps(dict(failed_event.payload))
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "failed"
    assert state["artifact_id"] is None
    assert state["sha256"] is None
    resumed = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    await resumed.restore_checkpoint(checkpoint)


async def test_workspace_path_rejection_is_returned_for_bounded_model_correction() -> None:
    rejected_content = "TOP-SECRET-WORKFLOW-CONTENT"

    class CorrectingWorkspaceGateway:
        def __init__(self, correction_gate: asyncio.Event | None = None) -> None:
            self.requests: list[ModelRequest] = []
            self.correction_gate = correction_gate

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            has_argument_correction = any(
                message.role == "system"
                and isinstance(message.content, str)
                and "CAPABILITY_ARGUMENT_CORRECTION" in message.content
                for message in request.messages
            )
            has_successful_continuation = any(
                message.role == "system"
                and isinstance(message.content, str)
                and "CAPABILITY_RESULT_CONTINUATION" in message.content
                for message in request.messages
            )
            if has_successful_continuation:
                response = ModelResponse(
                    text="workspace updated",
                    usage=TokenUsage(1, 1, 2),
                )
            elif has_argument_correction:
                if self.correction_gate is not None:
                    await self.correction_gate.wait()
                response = ModelResponse(
                    text=None,
                    tool_calls=(ToolCall(
                        id="safe-path",
                        name="workspace.write_text",
                        arguments={
                            "path": "docs/ci.yml",
                            "content": "safe workflow guidance",
                        },
                    ),),
                    usage=TokenUsage(1, 1, 2),
                )
            else:
                response = ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="hidden-path",
                            name="workspace.write_text",
                            arguments={
                                "path": ".github/workflows/ci.yml",
                                "content": rejected_content,
                            },
                        ),
                        ToolCall(
                            id="must-not-run-after-rejection",
                            name="workspace.write_text",
                            arguments={
                                "path": "should-not-run.txt",
                                "content": "must not run",
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

    class WorkspacePathHarness:
        def __init__(self) -> None:
            self.calls: list[HarnessToolCallRequest] = []

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
            if request.arguments["path"] == ".github/workflows/ci.yml":
                return HarnessToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    status="failed",
                    payload={},
                    failure_reason="workspace path must not contain hidden files",
                )
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={"path": request.arguments["path"]},
            )

    plan = DispatchPlan(
        agents=(AgentSpec(
            id="writer",
            role="Writer",
            goal="Write the requested workspace file.",
            logical_model="general",
            allowed_tools=("workspace.write_text",),
        ),),
        steps=(DispatchStep(
            id="write_step",
            agent="writer",
            task="Write one workspace file.",
            tools=("workspace.write_text",),
            final_synthesizer=True,
            token_budget=1_000,
        ),),
        allowed_tools=("workspace.write_text",),
        total_token_budget=1_000,
    )
    gateway = CorrectingWorkspaceGateway(asyncio.Event())
    harness = WorkspacePathHarness()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )

    stream = runtime.run(_context())
    events: list[RunEvent] = []
    rejection_checkpoint: RuntimeCheckpoint | None = None
    async for event in stream:
        events.append(event)
        if event.kind is not EventKind.CHECKPOINT_SAVED or event.checkpoint is None:
            continue
        checkpoint_tools = event.checkpoint.state["tools"]
        if isinstance(checkpoint_tools, Mapping) and any(
            isinstance(state, Mapping) and state.get("status") == "rejected"
            for state in checkpoint_tools.values()
        ):
            rejection_checkpoint = event.checkpoint
            break
    await runtime.cancel()
    assert rejection_checkpoint is not None

    resumed_gateway = CorrectingWorkspaceGateway()
    resumed_harness = WorkspacePathHarness()
    resumed = CrewDispatchRuntime(
        resumed_gateway,
        plan,
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=resumed_harness,
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await resumed.restore_checkpoint(rejection_checkpoint)
    resumed_events = [
        event
        async for event in resumed.run(_context(checkpoint=rejection_checkpoint))
    ]

    assert [call.arguments["path"] for call in (*harness.calls, *resumed_harness.calls)] == [
        ".github/workflows/ci.yml",
        "docs/ci.yml",
    ]
    assert len(gateway.requests) == 2
    assert len(resumed_gateway.requests) == 2
    correction_messages = resumed_gateway.requests[0].messages
    serialized_messages = json.dumps(
        [
            {"role": message.role, "content": message.content}
            for message in correction_messages
        ],
        ensure_ascii=False,
    )
    assert "CAPABILITY_ARGUMENT_CORRECTION" in serialized_messages
    assert "invalid_workspace_path" in serialized_messages
    assert "workspace path must not contain hidden files" in serialized_messages
    assert rejected_content not in serialized_messages
    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "workspace path must not contain hidden files"
    ]
    rejected_event = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert rejected_event.payload["status"] == "rejected"
    assert rejected_event.payload["failure_kind"] == "invalid_arguments"
    assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await resumed.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    rejected_states = [
        state
        for state in tool_states.values()
        if isinstance(state, Mapping) and state.get("status") == "rejected"
    ]
    assert len(rejected_states) == 1

    completed_gateway = CorrectingWorkspaceGateway()
    completed_harness = WorkspacePathHarness()
    completed = CrewDispatchRuntime(
        completed_gateway,
        plan,
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=completed_harness,
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await completed.restore_checkpoint(checkpoint)
    completed_events = [
        event
        async for event in completed.run(_context(checkpoint=checkpoint))
    ]
    assert completed_harness.calls == []
    assert completed_gateway.requests == []
    assert [event.kind for event in completed_events] == [EventKind.RUNTIME_COMPLETED]


async def test_default_harness_wrapper_fails_closed_without_actor_identity() -> None:
    capabilities = FakeCapabilities()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert capabilities.calls == []
    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "capability identity unavailable"
    ]


async def test_default_harness_wrapper_preserves_backend_uncertainty() -> None:
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=RaisingCapabilities(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(CapabilityOutcomeUncertain):
        async for event in runtime.run(
            _context(actor_id=uuid4(), actor_role=Role.OPERATOR)
        ):
            events.append(event)

    assert [event.reason for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "capability execution failed"
    ]
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "uncertain"


async def test_replay_safe_harness_backend_error_records_failed_not_uncertain() -> None:
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=ReplaySafeRaisingCapabilities(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    async for event in runtime.run(_context(actor_id=uuid4(), actor_role=Role.OPERATOR)):
        events.append(event)

    failed_event = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed_event.reason == "capability transient execution failed"
    assert failed_event.payload["replay_safe"] is True
    assert failed_event.payload["failure_kind"] == "capability_failed"
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.payload["error_code"] == "capability.transient_execution_failed"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "failed"


_WORKSPACE_READ_UNAVAILABLE = "workspace read denied or scoped file unavailable"
_SECRET_READ_PATH = "private/TOP-SECRET-READ-PATH/token-sensitive.txt"
_SECRET_READ_CONTENT = "SYNTHETIC-SECRET-READ-CONTENT"
_SECRET_READ_OUTPUT = "SYNTHETIC-SECRET-FAILED-READ-OUTPUT"


class WorkspaceReadCorrectionGateway:
    def __init__(
        self,
        paths: tuple[str, ...],
        *,
        name: str = "workspace.read",
        correction_gate: asyncio.Event | None = None,
        gate_after: int = 1,
        cost_usd: Decimal = Decimal(0),
    ) -> None:
        self.paths = paths
        self.name = name
        self.correction_gate = correction_gate
        self.gate_after = gate_after
        self.cost_usd = cost_usd
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if len(self.requests) > self.gate_after and self.correction_gate is not None:
            await self.correction_gate.wait()
        index = len(self.requests) - 1
        response = (
            ModelResponse(
                text=None,
                tool_calls=(ToolCall(
                    id=f"read-{index}",
                    name=self.name.replace(".", "_"),
                    arguments={
                        "path": self.paths[index],
                        **({"query": _SECRET_READ_CONTENT}
                           if self.paths[index] == _SECRET_READ_PATH else {}),
                    },
                ),),
                usage=TokenUsage(1, 1, 2),
            )
            if index < len(self.paths)
            else ModelResponse(text="review complete", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=self.cost_usd,
        )


class WorkspaceReadRejectionHarness:
    def __init__(
        self, backend: str, *, reason: str = _WORKSPACE_READ_UNAVAILABLE,
    ) -> None:
        self.backend = backend
        self.reason = reason
        self.calls: list[HarnessToolCallRequest] = []

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
        if request.arguments["path"] == "src/available.txt":
            return HarnessToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name,
                status="succeeded", payload={"items": ()},
            )
        if self.backend == "exception":
            raise RuntimeCapabilityError(self.reason)
        return HarnessToolCallResult(
            call_id=request.call_id, tool_name=request.tool_name,
            status="failed",
            payload={"approval_id": str(RUN_ID)}
            if self.reason == "capability requires approval" else {
                "path": _SECRET_READ_PATH,
                "text": _SECRET_READ_CONTENT,
                "stderr": _SECRET_READ_OUTPUT,
            },
            failure_reason=self.reason,
        )


def _workspace_read_correction_plan(name: str = "workspace.read") -> DispatchPlan:
    base = _read_context_tool_plan()
    return base.model_copy(update={
        "agents": (base.agents[0].model_copy(update={"allowed_tools": (name,)}),),
        "steps": (base.steps[0].model_copy(update={"tools": (name,)}),),
        "allowed_tools": (name,),
    })


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
@pytest.mark.parametrize("name", ("workspace.read", "workspace_read"))
async def test_workspace_read_unavailable_is_honest_redacted_argument_rejection(
    backend: str, name: str,
) -> None:
    gateway = WorkspaceReadCorrectionGateway((_SECRET_READ_PATH, "src/available.txt"), name=name)
    harness = WorkspaceReadRejectionHarness(backend)
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway, _workspace_read_correction_plan(name),
        capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context())]

    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failed) == 1
    assert failed[0].payload["status"] == "rejected"
    assert failed[0].payload["failure_kind"] == "invalid_arguments"
    completed = [event for event in events if event.kind is EventKind.TOOL_COMPLETED]
    assert len(completed) == 1
    assert completed[0].tool_call_id != failed[0].tool_call_id
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    assert len(harness.calls) == 2
    assert all(call.sandbox == "read_only" for call in harness.calls)
    assert len({call.idempotency_key for call in harness.calls}) == 2
    feedback = next(
        message.content for message in gateway.requests[1].messages
        if isinstance(message.content, str)
        and message.content.startswith("UNTRUSTED_CAPABILITY_REJECTIONS_JSON=")
    )
    result = json.loads(feedback.split("=", 1)[1])[0]["result"]
    assert result == {
        "status": "rejected",
        "error_code": "workspace_read_unavailable",
        "message": _WORKSPACE_READ_UNAVAILABLE,
        "tool_name": name,
    }
    serialized_feedback = json.dumps([
        {"role": message.role, "content": message.content}
        for message in gateway.requests[1].messages
    ])
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 6, "cost_usd": "0"}
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping)
    rejected_state = next(
        state for state in tools.values()
        if isinstance(state, Mapping) and state["status"] == "rejected"
    )
    artifact_id = rejected_state["artifact_id"]
    artifact_sha = rejected_state["sha256"]
    assert isinstance(artifact_id, str) and isinstance(artifact_sha, str)
    rejected_artifact, = await repository.get_many(TENANT_ID, RUN_ID, (
        ArtifactReference(id=UUID(artifact_id), sha256=artifact_sha),
    ))
    assert rejected_artifact.type == "tool_result"
    assert rejected_artifact.content["result"] == result
    serialized_failure = json.dumps(failed[0].model_dump(mode="json"))
    serialized_artifact = json.dumps(rejected_artifact.to_payload())
    for secret in (_SECRET_READ_PATH, _SECRET_READ_CONTENT, _SECRET_READ_OUTPUT):
        assert secret not in serialized_feedback
        assert secret not in serialized_failure
        assert secret not in serialized_artifact
    original_model_artifact = next(
        event.artifact for event in events
        if event.artifact is not None and event.artifact.type == "model_response"
    )
    original_model_payload = json.dumps(original_model_artifact.to_payload())
    assert _SECRET_READ_PATH in original_model_payload
    assert _SECRET_READ_CONTENT in original_model_payload


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
@pytest.mark.parametrize("name", ("workspace.read", "workspace_read"))
async def test_workspace_read_repeated_rejection_does_not_invoke_again(
    backend: str, name: str,
) -> None:
    gateway = WorkspaceReadCorrectionGateway((_SECRET_READ_PATH, _SECRET_READ_PATH), name=name)
    harness = WorkspaceReadRejectionHarness(backend)
    runtime = CrewDispatchRuntime(
        gateway, _workspace_read_correction_plan(name),
        capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="repeated rejected request"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(harness.calls) == 1
    assert len(gateway.requests) == 2
    assert not any(event.kind is EventKind.TOOL_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 4, "cost_usd": "0"}


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
@pytest.mark.parametrize("name", ("workspace.read", "workspace_read"))
async def test_workspace_read_corrections_stop_after_two_rejections(
    backend: str, name: str,
) -> None:
    gateway = WorkspaceReadCorrectionGateway(("missing/a", "missing/b", "missing/c"), name=name)
    harness = WorkspaceReadRejectionHarness(backend)
    runtime = CrewDispatchRuntime(
        gateway, _workspace_read_correction_plan(name),
        capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == len(harness.calls) == 3
    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert [event.payload["status"] for event in failed] == ["rejected", "rejected", "failed"]
    assert not any(event.kind is EventKind.TOOL_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 6, "cost_usd": "0"}


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
@pytest.mark.parametrize(("name", "reason"), (
    ("workspace.read", "capability denied"),
    ("workspace.read", "workspace access is not authorized"),
    ("workspace_read", "workspace scope could not be resolved"),
    ("workspace.read", "workspace read denied or scoped file unavailable: other error"),
    ("workspace.read", "workspace path must be relative"),
    ("workspace.list", _WORKSPACE_READ_UNAVAILABLE),
))
async def test_workspace_read_other_errors_and_tools_are_not_correctable(
    backend: str, name: str, reason: str,
) -> None:
    gateway = WorkspaceReadCorrectionGateway((_SECRET_READ_PATH,), name=name)
    harness = WorkspaceReadRejectionHarness(backend, reason=reason)
    runtime = CrewDispatchRuntime(
        gateway, _workspace_read_correction_plan(name),
        capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == len(harness.calls) == 1
    assert [event.payload["status"] for event in events if event.kind is EventKind.TOOL_FAILED] == [
        "failed",
    ]
    assert not any(event.kind is EventKind.TOOL_COMPLETED for event in events)


async def test_workspace_read_approval_is_not_argument_correction() -> None:
    gateway = WorkspaceReadCorrectionGateway((_SECRET_READ_PATH,))
    harness = WorkspaceReadRejectionHarness("failed_result", reason="capability requires approval")
    runtime = CrewDispatchRuntime(
        gateway, _workspace_read_correction_plan(),
        capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == len(harness.calls) == 1
    failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed.payload["status"] == failed.payload["failure_kind"] == "waiting_approval"
    checkpoint = await runtime.save_checkpoint()
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping)
    assert any(
        isinstance(state, Mapping) and state["status"] == "waiting_approval"
        for state in tools.values()
    )


async def test_workspace_read_missing_identity_does_not_invoke_or_correct() -> None:
    gateway = WorkspaceReadCorrectionGateway((_SECRET_READ_PATH,))
    capabilities = FakeCapabilities()
    runtime = CrewDispatchRuntime(
        gateway, _workspace_read_correction_plan(),
        capability_gateway=capabilities, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert capabilities.calls == []
    assert len(gateway.requests) == 1
    failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed.reason == "capability identity unavailable"
    assert failed.payload["status"] == "failed"


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
@pytest.mark.parametrize(("prior_rejections", "outcome", "expected_cost"), (
    (1, "success", "0.375"), (1, "repeat", "0.250"),
    (2, "success", "0.500"), (2, "repeat", "0.375"), (2, "exhausted", "0.375"),
))
async def test_workspace_read_rejection_checkpoint_keeps_feedback_and_replay_guard(
    backend: str, prior_rejections: int, outcome: str, expected_cost: str,
) -> None:
    gate = asyncio.Event()
    gateway = WorkspaceReadCorrectionGateway(
        (_SECRET_READ_PATH, "missing/second")[:prior_rejections],
        correction_gate=gate, gate_after=prior_rejections, cost_usd=Decimal("0.125"),
    )
    harness = WorkspaceReadRejectionHarness(backend)
    repository = InMemoryArtifactRepository()
    plan = _workspace_read_correction_plan()
    plan = plan.model_copy(update={
        "steps": (plan.steps[0].model_copy(update={"cost_budget_usd": Decimal(1)}),),
        "total_cost_usd": Decimal(1),
    })
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    checkpoint: RuntimeCheckpoint | None = None
    async for event in runtime.run(_context()):
        if event.kind is not EventKind.CHECKPOINT_SAVED or event.checkpoint is None:
            continue
        tools = event.checkpoint.state["tools"]
        assert isinstance(tools, Mapping)
        if sum(
            isinstance(state, Mapping) and state["status"] == "rejected"
            for state in tools.values()
        ) == prior_rejections:
            checkpoint = event.checkpoint
            break
    await runtime.cancel()
    assert checkpoint is not None
    assert checkpoint.state["usage"] == {
        "tokens": 2 * prior_rejections,
        "cost_usd": "0.125" if prior_rejections == 1 else "0.250",
    }

    resumed_gateway = WorkspaceReadCorrectionGateway((
        _SECRET_READ_PATH if outcome == "repeat"
        else "missing/third" if outcome == "exhausted"
        else "src/available.txt",
    ), cost_usd=Decimal("0.125"))
    resumed_harness = WorkspaceReadRejectionHarness(backend)
    resumed = CrewDispatchRuntime(
        resumed_gateway, plan, capability_gateway=FakeCapabilities(),
        harness_tool_gateway=resumed_harness, artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await resumed.restore_checkpoint(checkpoint)
    events: list[RunEvent] = []
    if outcome != "success":
        reason = "repeated rejected request" if outcome == "repeat" else "capability execution failed"
        with pytest.raises(RuntimeExecutionError, match=reason):
            async for event in resumed.run(_context(checkpoint=checkpoint)):
                events.append(event)
        assert len(resumed_harness.calls) == (0 if outcome == "repeat" else 1)
        assert len(resumed_gateway.requests) == 1
        if outcome == "exhausted":
            failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
            assert failed.payload["status"] == "failed"
    else:
        events = [event async for event in resumed.run(_context(checkpoint=checkpoint))]
        assert len(resumed_harness.calls) == 1
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(resumed_gateway.requests) == 2
    feedback = json.dumps([
        {"role": message.role, "content": message.content}
        for message in resumed_gateway.requests[0].messages
    ])
    assert "workspace_read_unavailable" in feedback
    for secret in (_SECRET_READ_PATH, _SECRET_READ_CONTENT, _SECRET_READ_OUTPUT):
        assert secret not in feedback
    restored = await resumed.save_checkpoint()
    assert restored.state["usage"] == {
        "tokens": 2 * prior_rejections + (4 if outcome == "success" else 2),
        "cost_usd": expected_cost,
    }
    assert len(harness.calls) == prior_rejections
    if outcome == "success":
        completed_gateway = WorkspaceReadCorrectionGateway((), cost_usd=Decimal("0.125"))
        completed_harness = WorkspaceReadRejectionHarness(backend)
        completed = CrewDispatchRuntime(
            completed_gateway, plan, capability_gateway=FakeCapabilities(),
            harness_tool_gateway=completed_harness, artifact_repository=repository,
            crew_factory=FastFactory(),
        )
        await completed.restore_checkpoint(restored)
        completed_events = [
            event async for event in completed.run(_context(checkpoint=restored))
        ]
        assert [event.kind for event in completed_events] == [EventKind.RUNTIME_COMPLETED]
        assert completed_gateway.requests == []
        assert completed_harness.calls == []
        assert restored.state["usage"] == {
            "tokens": 2 * prior_rejections + 4, "cost_usd": expected_cost,
        }


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
async def test_workspace_read_argument_correction_does_not_extend_token_budget(backend: str) -> None:
    gateway = WorkspaceReadCorrectionGateway((_SECRET_READ_PATH, "src/available.txt"))
    harness = WorkspaceReadRejectionHarness(backend)
    plan = _workspace_read_correction_plan()
    plan = plan.model_copy(update={
        "steps": (plan.steps[0].model_copy(update={"token_budget": 2}),),
        "total_token_budget": 2,
    })
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=FakeCapabilities(), harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError, match="dispatch budget exhausted"):
        async for _event in runtime.run(_context(token_budget=2)):
            pass
    assert len(gateway.requests) == 2
    assert len(harness.calls) == 1
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 4, "cost_usd": "0"}
    assert checkpoint.state["phase"] == "budget_exhausted"


async def test_read_context_scoped_unavailable_does_not_fail_dispatch_step() -> None:
    capabilities = ScopedReadUnavailableCapabilities()
    runtime = CrewDispatchRuntime(
        ReadContextToolGateway(),
        _read_context_tool_plan(),
        capability_gateway=capabilities,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(actor_id=uuid4(), actor_role=Role.OPERATOR)
        )
    ]

    assert capabilities.calls == [("security_reviewer", "read_context")]
    assert not any(event.kind is EventKind.TOOL_FAILED for event in events)
    completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert completed.payload["status"] == "succeeded"
    artifact = completed.artifact
    assert artifact is not None
    result = artifact.content["result"]
    assert isinstance(result, Mapping)
    assert result["unavailable"] is True
    assert result["matches"] == ()
    assert result["path"] == "missing/generated-project.zip"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_project_scale_repeating_read_context_round_limit_completes_from_tool_evidence() -> None:
    capabilities = ReadContextCapabilities()
    gateway = RepeatingReadContextToolGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _project_scale_repeating_read_context_plan(),
        capability_gateway=capabilities,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(
                actor_id=uuid4(),
                actor_role=Role.OPERATOR,
                token_budget=100_000,
            )
        )
    ]
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) > 1
    assert any(event.kind is EventKind.TOOL_COMPLETED for event in events)
    assert not any(event.kind is EventKind.STEP_FAILED for event in events)
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"


async def test_project_scale_tester_forbidden_tool_after_evidence_completes_from_tool_evidence() -> None:
    capabilities = ReadContextCapabilities()
    gateway = ReadContextThenForbiddenToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        _project_scale_repeating_read_context_plan(),
        capability_gateway=capabilities,
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(
                actor_id=uuid4(),
                actor_role=Role.OPERATOR,
                token_budget=100_000,
            )
        )
    ]
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) == 2
    assert any(event.kind is EventKind.TOOL_COMPLETED for event in events)
    assert not any(event.kind is EventKind.STEP_FAILED for event in events)
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"

    restored = CrewDispatchRuntime(
        ReadContextThenForbiddenToolGateway(),
        _project_scale_repeating_read_context_plan(),
        capability_gateway=ReadContextCapabilities(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [
        event
        async for event in restored.run(
            _context(
                actor_id=uuid4(),
                actor_role=Role.OPERATOR,
                token_budget=100_000,
                checkpoint=checkpoint,
            )
        )
    ]

    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]


def test_checkpoint_forbidden_tool_skip_requires_every_prior_allowed_tool() -> None:
    step = _project_scale_repeating_read_context_plan().steps[0]
    ledger = _ToolLedger(
        states={
            "prior": {
                "step_id": step.id,
                "status": "succeeded",
            }
        }
    )
    calls = (
        ToolCall(
            id="allowed",
            name="read_context",
            arguments={"query": "generated project verification evidence"},
        ),
        ToolCall(
            id="forbidden",
            name="project.generate_zip",
            arguments={"title": "tester should not write"},
        ),
    )

    assert not _checkpoint_can_skip_forbidden_tool_placeholders(
        step,
        ledger,
        calls,
        {},
        is_last_model_call=True,
    )
    assert _checkpoint_can_skip_forbidden_tool_placeholders(
        step,
        ledger,
        calls,
        {0: ({}, None)},
        is_last_model_call=True,
    )
    assert not _checkpoint_can_skip_forbidden_tool_placeholders(
        step,
        ledger,
        calls,
        {0: ({}, None)},
        is_last_model_call=False,
    )


async def test_deterministic_harness_errors_record_failed_not_uncertain() -> None:
    capabilities = FakeCapabilities()
    harness = DeterministicErrorHarnessToolGateway()
    runtime = CrewDispatchRuntime(
        ToolGateway(),
        _tool_plan(),
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert capabilities.calls == []
    assert len(harness.calls) == 1
    failed_event = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    assert failed_event.reason == "files must be an object"
    assert failed_event.payload["failure_kind"] == "capability_failed"
    checkpoint = await runtime.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    state = next(iter(tool_states.values()))
    assert isinstance(state, Mapping)
    assert state["status"] == "failed"


def test_tool_sandbox_accepts_dot_and_underscore_builtin_names() -> None:
    assert _tool_sandbox("workspace.read") == "read_only"
    assert _tool_sandbox("workspace_read") == "read_only"
    assert _tool_sandbox("calculator.evaluate") == "none"
    assert _tool_sandbox("calculator_evaluate") == "none"
    assert _tool_sandbox("web.search") == "restricted"


def test_project_zip_tool_definition_exposes_required_file_schema() -> None:
    (definition,) = _tool_definitions(("project.generate_zip",))

    assert definition.name == "project_generate_zip"
    assert definition.parameters["required"] == ("title", "files")
    properties = definition.parameters["properties"]
    assert isinstance(properties, Mapping)
    files = properties["files"]
    assert isinstance(files, Mapping)
    assert files["type"] == "object"
    assert files["additionalProperties"] == {"type": "string"}


def test_workspace_write_tool_definition_documents_safe_relative_paths() -> None:
    (definition,) = _tool_definitions(("workspace.write_text",))

    assert "POSIX relative path" in definition.description
    assert "hidden path segments" in definition.description
    properties = definition.parameters["properties"]
    assert isinstance(properties, Mapping)
    path = properties["path"]
    assert isinstance(path, Mapping)
    description = path["description"]
    assert isinstance(description, str)
    assert "Allowed root metadata files" in description
    assert definition.parameters["additionalProperties"] is False


def test_workspace_argument_correction_guards_are_bounded_and_exact() -> None:
    repeated_sha = "a" * 64
    ledger = _ToolLedger(states={
        "first": {
            "status": "rejected",
            "step_id": "write_step",
            "name": "workspace.write_text",
            "arguments_sha256": repeated_sha,
        },
        "second": {
            "status": "rejected",
            "step_id": "write_step",
            "name": "workspace.write_text",
            "arguments_sha256": "b" * 64,
        },
    })
    call = ToolCall(
        id="hidden-path",
        name="workspace.write_text",
        arguments={"path": ".github/workflows/ci.yml", "content": "secret"},
    )

    assert _has_matching_tool_argument_rejection(
        ledger,
        step_id="write_step",
        name="workspace.write_text",
        arguments_sha256=repeated_sha,
    )
    assert _correctable_tool_argument_rejection(
        call,
        "workspace path must not contain hidden files",
        ledger,
        step_id="write_step",
        producer="writer",
        source_id=str(uuid4()),
        arguments_sha256="c" * 64,
    ) is None
    assert _correctable_tool_argument_rejection(
        call,
        "capability requires approval",
        _ToolLedger(),
        step_id="write_step",
        producer="writer",
        source_id=str(uuid4()),
        arguments_sha256="d" * 64,
    ) is None


def test_project_zip_tool_call_gets_server_owned_workspace_scope() -> None:
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="编写一个网盘网站",
        routing_decision={
            "project_id": "cloud-drive",
            "workspace_session_id": "conv-cloud-drive",
            "project_delivery": "workspace",
        },
    )
    original = ToolCall(
        id="zip-call",
        name="project.generate_zip",
        arguments={"title": "网盘", "files": {"preview.html": "<html></html>"}},
    )

    scoped = _scope_project_workspace_tool_call(context, original)

    assert "project_id" not in original.arguments
    assert scoped.arguments["project_id"] == "cloud-drive"
    assert scoped.arguments["workspace_session_id"] == "conv-cloud-drive"


def test_project_preflight_tool_call_gets_server_owned_workspace_scope() -> None:
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        request="创建大型项目架构预检",
        routing_decision={
            "project_id": "large-project",
            "workspace_session_id": "conv-large-project",
            "project_preflight_approved": True,
        },
    )
    original = ToolCall(
        id="preflight-call",
        name="project.preflight_architecture",
        arguments={
            "title": "大型项目",
            "request": "生成架构计划",
            "project_id": "model-supplied-project",
            "workspace_session_id": "model-supplied-session",
        },
    )

    scoped = _scope_project_workspace_tool_call(context, original)

    assert original.arguments["project_id"] == "model-supplied-project"
    assert scoped.arguments["project_id"] == "large-project"
    assert scoped.arguments["workspace_session_id"] == "conv-large-project"
    assert scoped.arguments["request"] == "创建大型项目架构预检"
    assert set(scoped.arguments) == {
        "title",
        "request",
        "project_id",
        "workspace_session_id",
    }


def test_project_preflight_tool_call_restores_missing_request_from_context() -> None:
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        request="创建大型项目架构预检",
        routing_decision={
            "project_id": "large-project",
            "workspace_session_id": "conv-large-project",
        },
    )
    original = ToolCall(
        id="preflight-call",
        name="project.preflight_architecture",
        arguments={"title": "大型项目", "summary": "模型生成的摘要"},
    )

    scoped = _scope_project_workspace_tool_call(context, original)

    assert "request" not in original.arguments
    assert scoped.arguments["request"] == context.request
    assert "summary" not in scoped.arguments


def test_project_preflight_tool_call_bounds_server_owned_request_and_title() -> None:
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        request="需" * 2_000,
        routing_decision={
            "project_id": "large-project",
            "workspace_session_id": "conv-large-project",
        },
    )
    original = ToolCall(
        id="preflight-call",
        name="project.preflight_architecture",
        arguments={"title": " 标题 " + ("长" * 200)},
    )

    scoped = _scope_project_workspace_tool_call(context, original)

    assert scoped.arguments["request"] == "需" * 1_200
    assert scoped.arguments["title"] == ("标题 " + ("长" * 200))[:96]


def test_project_preflight_tool_call_normalizes_whitespace_before_bounding() -> None:
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        request=(" " * 1_300) + "保留真实需求",
        routing_decision={
            "project_id": "large-project",
            "workspace_session_id": "conv-large-project",
        },
    )

    scoped = _scope_project_workspace_tool_call(
        context,
        ToolCall(
            id="preflight-call",
            name="project.preflight_architecture",
            arguments={},
        ),
    )

    assert scoped.arguments["request"] == "保留真实需求"


@pytest.mark.parametrize(
    "routing_decision",
    (
        {},
        {"project_id": "large-project"},
        {"workspace_session_id": "conv-large-project"},
    ),
)
def test_project_preflight_tool_call_rejects_missing_server_scope(
    routing_decision: Mapping[str, JsonValue],
) -> None:
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.HYBRID,
        request="创建大型项目架构预检",
        routing_decision=routing_decision,
    )
    original = ToolCall(
        id="preflight-call",
        name="project.preflight_architecture",
        arguments={
            "title": "大型项目",
            "request": "生成架构计划",
            "project_id": "model-supplied-project",
            "workspace_session_id": "model-supplied-session",
        },
    )

    with pytest.raises(
        RuntimeExecutionError,
        match="project preflight workspace scope is not configured",
    ):
        _scope_project_workspace_tool_call(context, original)


async def test_project_preflight_runtime_injects_server_scope_before_capability_execution() -> None:
    capabilities = ProjectPreflightCapabilities()
    model_gateway = ProjectPreflightToolGateway()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        model_gateway,
        _project_preflight_tool_plan(),
        capability_gateway=capabilities,
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )

    events = [
        event
        async for event in runtime.run(
            _context(
                actor_id=uuid4(),
                actor_role=Role.OPERATOR,
                mode=TaskMode.DISPATCH,
                routing_decision={
                    "project_id": "large-project",
                    "workspace_session_id": "conv-large-project",
                    "project_preflight_approved": True,
                },
            )
        )
    ]

    assert capabilities.calls == [
        ("project_preflight_architect", "project.preflight_architecture")
    ]
    assert capabilities.arguments == [
        {
            "title": "Large Project",
            "request": "Write a short answer",
            "project_id": "large-project",
            "workspace_session_id": "conv-large-project",
        }
    ]
    (tool_definition,) = model_gateway.requests[0].tools
    assert tool_definition.name == "project_preflight_architecture"
    assert tool_definition.parameters == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 96},
        },
    }
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED

    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        ProjectPreflightToolGateway(),
        _project_preflight_tool_plan(),
        capability_gateway=ProjectPreflightCapabilities(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [
        event
        async for event in restored.run(
            _context(
                actor_id=uuid4(),
                actor_role=Role.OPERATOR,
                mode=TaskMode.DISPATCH,
                checkpoint=checkpoint,
                routing_decision={
                    "project_id": "large-project",
                    "workspace_session_id": "conv-large-project",
                    "project_preflight_approved": True,
                },
            )
        )
    ]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]

    changed_request = CrewDispatchRuntime(
        ProjectPreflightToolGateway(),
        _project_preflight_tool_plan(),
        capability_gateway=ProjectPreflightCapabilities(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await changed_request.restore_checkpoint(checkpoint)
    with pytest.raises(
        RuntimeExecutionError,
        match="runtime checkpoint capability artifact lineage is invalid",
    ):
        [
            event
            async for event in changed_request.run(
                _context(
                    actor_id=uuid4(),
                    actor_role=Role.OPERATOR,
                    mode=TaskMode.DISPATCH,
                    request="不同的用户需求",
                    checkpoint=checkpoint,
                    routing_decision={
                        "project_id": "large-project",
                        "workspace_session_id": "conv-large-project",
                        "project_preflight_approved": True,
                    },
                )
            )
        ]


async def test_dispatch_framework_failure_records_safe_root_cause() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=FailingFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    expected = "CrewAI step execution failed: agent identifier must be a safe identifier"
    assert str(caught.value) == expected
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == expected
    assert any(
        event.kind is EventKind.STEP_FAILED and event.reason == expected for event in events
    )


async def test_dispatch_generation_build_failure_records_safe_root_cause() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=BuildFailingFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    expected = "CrewAI generation failed: private crew runtime refused storage path"
    assert str(caught.value) == expected
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == expected


async def test_dispatch_generation_build_failure_records_error_type_for_sensitive_cause() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=BuildFailingFactory("api_key credential lookup failed"),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    expected = "CrewAI generation failed: ValueError"
    assert str(caught.value) == expected
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == expected


async def test_dispatch_framework_timeout_names_the_step_and_actor() -> None:
    runtime = CrewDispatchRuntime(
        UnusedGateway(),
        _one_step_plan(),
        crew_factory=TimeoutFactory(),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    expected = "CrewAI step timed out: step=final actor=writer"
    assert str(caught.value) == expected
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == expected
    assert any(
        event.kind is EventKind.STEP_FAILED and event.reason == expected for event in events
    )
    step_failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert step_failed.payload["error_code"] == "crew.step_timeout"
    assert step_failed.payload["step_id"] == "final"
    assert step_failed.payload["actor"] == "writer"


async def test_agent_timeout_compact_retries_before_failing_step() -> None:
    generation = RecordingGeneration(agent_timeouts=1)
    plan = _one_step_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(writer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert retrying.payload["model_fallback"] == "not_available_in_crewai_bridge"
    assert "Keep the retry concise" in writer_prompts[1]
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)


async def test_expired_step_deadline_uses_remaining_run_budget_for_compact_retry() -> None:
    generation = DeadlineThenSuccessGeneration(
        first_delay_seconds=0.1,
        target_agent_id="writer",
    )
    plan = _one_step_plan()
    plan = plan.model_copy(update={
        "steps": (
            plan.steps[0].model_copy(update={"timeout_seconds": 0.05}),
        ),
    })
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(generation),
    )

    events = [event async for event in runtime.run(_context(timeout_seconds=2.0))]

    assert generation.target_attempts == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.reason == "CrewAI step timed out: step=final actor=writer"
    assert retrying.payload["error_code"] == "crew.step_timeout"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_expired_review_deadline_uses_remaining_run_budget_for_compact_retry() -> None:
    generation = DeadlineThenSuccessGeneration(
        first_delay_seconds=0.1,
        target_agent_id="reviewer",
    )
    plan = _reviewed_step_plan()
    plan = plan.model_copy(update={
        "steps": (
            plan.steps[0].model_copy(update={"timeout_seconds": 0.05}),
            plan.steps[1],
        ),
    })
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(generation),
    )

    events = [event async for event in runtime.run(_context(timeout_seconds=2.0))]

    assert generation.target_attempts == 2
    retrying = next(
        event
        for event in events
        if event.kind is EventKind.STEP_RETRYING and event.actor == "reviewer"
    )
    assert retrying.reason == "CrewAI step timed out: step=draft.review actor=reviewer"
    assert retrying.payload["error_code"] == "crew.step_timeout"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_agent_timeout_reports_recovery_closure_after_retry_exhausted() -> None:
    generation = RecordingGeneration(agent_timeouts=2)
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        crew_factory=RecordingFactory(generation),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError) as caught:
        async for event in runtime.run(_context()):
            events.append(event)

    assert str(caught.value) == "CrewAI step timed out: step=final actor=writer"
    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(writer_prompts) == 2
    retry_events = [event for event in events if event.kind is EventKind.STEP_RETRYING]
    assert len(retry_events) == 1
    step_failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    assert step_failed.payload["error_code"] == "crew.step_timeout"
    assert step_failed.payload["recovery_status"] == "failed_after_compact_retry"
    assert step_failed.payload["recovery_attempts"] == 1
    assert step_failed.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )


async def test_agent_empty_model_response_compact_retries_before_completing() -> None:
    gateway = EmptyThenRoleAwareGateway(empty_logical_model="general")
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    assert len(gateway.requests) == 2
    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(writer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert "Keep the retry concise" in writer_prompts[1]
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    model_states = checkpoint.state["models"]
    assert isinstance(model_states, Mapping)
    assert {state["status"] for state in model_states.values() if isinstance(state, Mapping)} == {
        "failed",
        "succeeded",
    }
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [event async for event in restored.run(_context(checkpoint=checkpoint))]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]


async def test_agent_empty_model_response_retries_with_agent_fallback_model() -> None:
    gateway = EmptyThenRoleAwareGateway(empty_logical_model="primary")
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan_with_model_fallback(),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    assert [request.logical_model for request in gateway.requests] == ["primary", "backup"]
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["model_fallback"] == "backup"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_final_synthesizer_empty_response_falls_back_to_dependency_evidence() -> None:
    class FinalSynthesizerAlwaysEmptyGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if request.response_schema is not None:
                text = _role_output_text(request)
            else:
                text = ""
            return GatewayCompletion(
                response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    gateway = FinalSynthesizerAlwaysEmptyGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _structured_dependent_final_plan(),
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    events = await _collect(runtime)
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) == 3
    retrying = next(
        event
        for event in events
        if event.kind is EventKind.STEP_RETRYING and event.actor == "final_synthesizer"
    )
    assert retrying.payload["error_code"] == "model.empty_response"
    fallback_artifact = next(
        event.artifact
        for event in events
        if (
            event.kind is EventKind.ARTIFACT_CREATED
            and event.actor == "final_synthesizer"
            and event.artifact is not None
        )
    )
    assert fallback_artifact.content["recovery_status"] == "internal_final_synthesis_fallback"
    assert fallback_artifact.source_ids
    assert not any(event.kind is EventKind.STEP_FAILED for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"


async def test_missing_usage_after_empty_response_retry_is_estimated() -> None:
    class EmptyThenMissingUsageGateway(RoleAwareGateway):
        def __init__(self) -> None:
            super().__init__()
            self._empty_returned = False

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if request.logical_model == "primary" and not self._empty_returned:
                self._empty_returned = True
                response = ModelResponse(text="", usage=TokenUsage(1, 1, 2))
            else:
                response = ModelResponse(text=_role_output_text(request), usage=None)
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="primary",
                fallback_models=("backup",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=1_000,
            ),
        ),
        total_token_budget=1_000,
    )
    gateway = EmptyThenMissingUsageGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    events = await _collect(runtime)
    checkpoint = await runtime.save_checkpoint()

    assert [request.logical_model for request in gateway.requests] == ["primary", "backup"]
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["model_fallback"] == "backup"
    assert not any(
        event.payload.get("error_code") == "runtime.dispatch_usage_unaccounted"
        for event in events
    )
    assert checkpoint.state["phase"] == "completed"
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    tokens = usage.get("tokens")
    assert type(tokens) is int and tokens > 0
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_missing_usage_after_capacity_retry_project_zip_tool_call_is_estimated() -> None:
    class CapacityThenZipMissingUsageGateway(RoleAwareGateway):
        def __init__(self) -> None:
            super().__init__()
            self._capacity_returned = False

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if not self._capacity_returned:
                self._capacity_returned = True
                raise CapacityUnavailable("model capacity unavailable")
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-zip",
                            name="project_generate_zip",
                            arguments={
                                "title": "Task API",
                                "files": {"package.json": "{}\n"},
                            },
                        ),
                    ),
                    usage=None,
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class ZipCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

    class ZipHarnessToolGateway:
        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            artifact_id = str(uuid4())
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={
                    "artifact_id": artifact_id,
                    "file": {
                        "artifact_id": artifact_id,
                        "filename": "task-api.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "download_url": f"/api/v1/admin/runs/{RUN_ID}/artifacts/{artifact_id}/download",
                    },
                    "metadata": {
                        "artifact_id": artifact_id,
                        "filename": "task-api.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "storage_key": f"{TENANT_ID}/{RUN_ID}/{artifact_id}/task-api.zip",
                        "download_url": f"/api/v1/admin/runs/{RUN_ID}/artifacts/{artifact_id}/download",
                    },
                    "presentation": "final_attachment",
                    "summary": "Generated project ZIP artifact task-api.zip.",
                },
            )

    gateway = CapacityThenZipMissingUsageGateway()
    plan = DispatchPlan(
        agents=_project_zip_plan().agents,
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("project.generate_zip",),
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(100),
            ),
        ),
        allowed_tools=("project.generate_zip",),
        total_token_budget=100_000,
        total_cost_usd=Decimal(100),
    )
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=ZipHarnessToolGateway(),
        crew_factory=FastFactory(),
    )

    events = [event async for event in runtime.run(_context(token_budget=100_000))]
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) >= 2
    assert any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert not any(
        event.payload.get("error_code") == "runtime.dispatch_usage_unaccounted"
        for event in events
    )
    assert checkpoint.state["phase"] == "completed"
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    tokens = usage.get("tokens")
    assert type(tokens) is int and tokens > 0
    completed = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    assert completed.artifact is not None
    assert completed.artifact.content["artifact_origin"] == "tool_workspace_write"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_project_scale_tool_contract_rejected_without_text_retries_instead_of_unaccounted() -> None:
    task = (
        "Role mission: implement the project.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )

    class RejectedThenZipMissingUsageGateway(RoleAwareGateway):
        def __init__(self) -> None:
            super().__init__()
            self._rejected = False

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if not self._rejected:
                self._rejected = True
                raise GatewayRejectedOutput(
                    evidence=RejectedOutputEvidence(
                        final_text=None,
                        usage=None,
                        usage_status="missing",
                        status="completed",
                        reason="invalid_output",
                    ),
                    deployment_id="primary",
                    logical_model=request.logical_model,
                    provider_id="deepseek",
                    provider_model="deepseek/deepseek-v4-flash",
                    cost_usd=Decimal(0),
                )
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-zip",
                            name="project_generate_zip",
                            arguments={
                                "title": "Task API",
                                "files": {"package.json": "{}\n"},
                            },
                        ),
                    ),
                    usage=None,
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class ZipCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return name == "project.generate_zip"

    class ZipHarnessToolGateway:
        async def invoke(
            self,
            tenant_id: UUID,
            request: HarnessToolCallRequest,
            *,
            user_id: UUID | None = None,
            role: Role | None = None,
        ) -> HarnessToolCallResult:
            del tenant_id, user_id, role
            artifact_id = str(uuid4())
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={
                    "artifact_id": artifact_id,
                    "file": {
                        "artifact_id": artifact_id,
                        "filename": "task-api.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "download_url": f"/api/v1/admin/runs/{RUN_ID}/artifacts/{artifact_id}/download",
                    },
                    "metadata": {
                        "artifact_id": artifact_id,
                        "filename": "task-api.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 128,
                        "sha256": "0" * 64,
                        "storage_key": f"{TENANT_ID}/{RUN_ID}/{artifact_id}/task-api.zip",
                        "download_url": f"/api/v1/admin/runs/{RUN_ID}/artifacts/{artifact_id}/download",
                    },
                    "presentation": "final_attachment",
                    "summary": "Generated project ZIP artifact task-api.zip.",
                },
            )

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="implementer",
                role="Implementer",
                goal="Build the project.",
                logical_model="qwen",
                allowed_tools=("project.generate_zip",),
            ),
        ),
        steps=(
            DispatchStep(
                id="implementer_step",
                agent="implementer",
                task=task,
                tools=("project.generate_zip",),
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(100),
            ),
        ),
        allowed_tools=("project.generate_zip",),
        total_token_budget=100_000,
        total_cost_usd=Decimal(100),
    )
    gateway = RejectedThenZipMissingUsageGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=ZipHarnessToolGateway(),
        crew_factory=FastFactory(),
    )

    events = [event async for event in runtime.run(_context(token_budget=100_000))]
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) >= 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.payload["error_code"] == "model.empty_response"
    assert not any(
        event.payload.get("error_code") == "runtime.dispatch_usage_unaccounted"
        for event in events
    )
    assert checkpoint.state["phase"] == "completed"
    restored = CrewDispatchRuntime(
        RejectedThenZipMissingUsageGateway(),
        plan,
        capability_gateway=ZipCapabilities(),
        harness_tool_gateway=ZipHarnessToolGateway(),
        crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)
    model_states = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"])
    assert {state["status"] for state in model_states.values()} == {"failed", "succeeded"}
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    tokens = usage.get("tokens")
    assert type(tokens) is int and tokens > 0
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_project_scale_rejected_structured_output_missing_usage_is_estimated() -> None:
    task = (
        "Role mission: implement the project.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )

    class RejectedProjectScaleGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=(
                        "Created package.json, src/server.ts, tests, README, "
                        "and verification notes for the requested task API."
                    ),
                    usage=None,
                    usage_status="missing",
                    status="completed",
                    reason="schema_mismatch",
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="implementer",
                role="Implementer",
                goal="Build the project.",
                logical_model="qwen",
                output_schema={
                    "status": "string",
                    "summary": "string",
                    "evidence": "string[]",
                    "risks": "string[]",
                    "artifacts": "string[]",
                    "verification": "string[]",
                },
            ),
        ),
        steps=(
            DispatchStep(
                id="implementer_step",
                agent="implementer",
                task=task,
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(10),
            ),
        ),
        total_token_budget=100_000,
        total_cost_usd=Decimal(10),
    )
    runtime = CrewDispatchRuntime(
        RejectedProjectScaleGateway(),
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    events = [
        event async for event in runtime.run(_context(token_budget=100_000))
    ]
    checkpoint = await runtime.save_checkpoint()

    assert not any(
        event.payload.get("error_code") == "runtime.dispatch_usage_unaccounted"
        for event in events
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    tokens = usage.get("tokens")
    assert type(tokens) is int and tokens > 0


async def test_project_scale_empty_rejected_structured_output_uses_internal_fallback() -> None:
    task = (
        "Role mission: synthesize the project.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )

    class EmptyRejectedProjectScaleGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=None,
                    usage=None,
                    usage_status="missing",
                    status="completed",
                    reason="invalid_output",
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="qwen",
                provider_model="qwen/qwen3-coder",
                cost_usd=Decimal(0),
            )

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize the verified project result.",
                logical_model="qwen",
                output_schema={
                    "status": "string",
                    "summary": "string",
                    "evidence": "string[]",
                    "risks": "string[]",
                    "artifacts": "string[]",
                    "verification": "string[]",
                },
            ),
        ),
        steps=(
            DispatchStep(
                id="final_response_step",
                agent="final_synthesizer",
                task=task,
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(10),
            ),
        ),
        total_token_budget=100_000,
        total_cost_usd=Decimal(10),
    )
    gateway = EmptyRejectedProjectScaleGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    events = [
        event async for event in runtime.run(_context(token_budget=100_000))
    ]
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) == 1
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    assert not any(event.kind is EventKind.STEP_FAILED for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"


async def test_project_scale_finalizer_gateway_empty_text_uses_internal_fallback() -> None:
    task = (
        "Role mission: synthesize the project.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )

    class EmptyErrorProjectScaleGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise ModelGatewayError("model response text is empty")

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="final_synthesizer",
                role="Final Synthesizer",
                goal="Synthesize the verified project result.",
                logical_model="qwen",
                output_schema={
                    "status": "string",
                    "summary": "string",
                    "evidence": "string[]",
                    "risks": "string[]",
                    "artifacts": "string[]",
                    "verification": "string[]",
                },
            ),
        ),
        steps=(
            DispatchStep(
                id="final_response_step",
                agent="final_synthesizer",
                task=task,
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(10),
            ),
        ),
        total_token_budget=100_000,
        total_cost_usd=Decimal(10),
    )
    gateway = EmptyErrorProjectScaleGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    events = [
        event async for event in runtime.run(_context(token_budget=100_000))
    ]
    checkpoint = await runtime.save_checkpoint()

    assert len(gateway.requests) == 1
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    assert not any(event.kind is EventKind.STEP_FAILED for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert checkpoint.state["phase"] == "completed"


async def test_project_scale_rejected_structured_recovery_tolerates_framework_raw() -> None:
    task = (
        "Role mission: implement the project.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )

    class RewriteRecoveredRaw(RecordingGeneration):
        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )
            return "Created the requested task API files, tests, and verification notes."

    class RejectedProjectScaleGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(
                    final_text=(
                        "Created package.json, src/server.ts, tests, README, "
                        "and verification notes for the requested task API."
                    ),
                    usage=TokenUsage(10, 5, 15),
                    usage_status="known",
                    status="completed",
                    reason="schema_mismatch",
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="implementer",
                role="Implementer",
                goal="Build the project.",
                logical_model="qwen",
                output_schema={
                    "status": "string",
                    "summary": "string",
                    "evidence": "string[]",
                    "risks": "string[]",
                    "artifacts": "string[]",
                    "verification": "string[]",
                },
            ),
        ),
        steps=(
            DispatchStep(
                id="implementer_step",
                agent="implementer",
                task=task,
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(10),
            ),
        ),
        total_token_budget=100_000,
        total_cost_usd=Decimal(10),
    )
    runtime = CrewDispatchRuntime(
        RejectedProjectScaleGateway(),
        plan,
        crew_factory=RecordingFactory(RewriteRecoveredRaw()),
    )

    events = [event async for event in runtime.run(_context(token_budget=100_000))]

    assert not any(
        event.payload.get("error_summary") == "framework output mismatch"
        for event in events
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_project_scale_recovered_fallback_model_preserves_checkpoint_lineage() -> None:
    task = (
        "Role mission: plan the project.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )

    class CapacityThenTransportFallbackGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if request.logical_model == "qwen":
                raise CapacityUnavailable("model capacity unavailable")
            raise ModelTransportError("model transport failed", status_code=500)

    plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="architect",
                role="Architect",
                goal="Plan the project.",
                logical_model="qwen",
                fallback_models=("sonnet5",),
                output_schema={
                    "status": "string",
                    "summary": "string",
                    "evidence": "string[]",
                    "risks": "string[]",
                    "artifacts": "string[]",
                    "verification": "string[]",
                },
            ),
        ),
        steps=(
            DispatchStep(
                id="architect_step",
                agent="architect",
                task=task,
                final_synthesizer=True,
                token_budget=100_000,
                cost_budget_usd=Decimal(10),
            ),
        ),
        total_token_budget=100_000,
        total_cost_usd=Decimal(10),
    )
    gateway = CapacityThenTransportFallbackGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    events = [event async for event in runtime.run(_context(token_budget=100_000))]
    checkpoint = await runtime.save_checkpoint()

    assert [request.logical_model for request in gateway.requests] == ["qwen", "sonnet5"]
    assert not any(
        event.payload.get("error_summary") == "runtime checkpoint model artifact lineage is invalid"
        for event in events
    )
    assert checkpoint.state["phase"] == "completed"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_agent_fallback_provider_bad_request_retries_next_fallback_model() -> None:
    gateway = CapacityThenBadRequestThenRoleAwareGateway(
        unavailable_logical_model="primary",
        bad_request_logical_model="backup",
    )
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan_with_two_model_fallbacks(),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    assert [request.logical_model for request in gateway.requests] == [
        "primary",
        "backup",
        "final",
    ]
    retrying = [event for event in events if event.kind is EventKind.STEP_RETRYING]
    assert [event.payload["model_fallback"] for event in retrying] == ["backup", "final"]
    assert retrying[0].payload["error_code"] == "model.capacity_unavailable"
    assert retrying[1].payload["error_code"] == "model.provider_bad_request"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan_with_two_model_fallbacks(),
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)


async def test_failed_fallback_bad_request_checkpoint_resumes_next_candidate() -> None:
    repository = InMemoryArtifactRepository()
    plan = _one_step_plan_with_two_model_fallbacks()
    gateway = CapacityThenBadRequestThenRoleAwareGateway(
        unavailable_logical_model="primary", bad_request_logical_model="backup",
    )
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    checkpoints = [
        event.checkpoint async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    failed_checkpoint = next(
        checkpoint for checkpoint in checkpoints
        if any(
            state["status"] == "failed" and state["attempt"] == 1
            for state in cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"]).values()
        )
    )
    restored_gateway = RoleAwareGateway()
    restored = CrewDispatchRuntime(
        restored_gateway, plan, artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(failed_checkpoint)
    events = [event async for event in restored.run(_context(checkpoint=failed_checkpoint))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in restored_gateway.requests] == ["final"]


async def test_agent_capacity_unavailable_compact_retries_before_completing() -> None:
    gateway = CapacityUnavailableThenRoleAwareGateway(unavailable_logical_model="general")
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan(),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    writer_prompts = [prompt for step_id, agent_id, prompt in generation.prompts if step_id == "final"]
    assert len(gateway.requests) == 2
    assert len(writer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["error_code"] == "model.capacity_unavailable"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert retrying.payload["model_fallback"] == "not_available_in_crewai_bridge"
    assert "Keep the retry concise" in writer_prompts[1]
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)


@pytest.mark.parametrize("status_code", [None, 408, 429, 503])
async def test_agent_transient_provider_failure_uses_existing_fallback(status_code: int | None) -> None:
    class ProviderFailureGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if request.logical_model == "primary":
                self.requests.append(request)
                raise ModelTransportError("model transport failed", status_code=status_code)
            return await super().complete_with_context(request)

    gateway = ProviderFailureGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan_with_model_fallback(),
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    events = await _collect(runtime)

    assert [request.logical_model for request in gateway.requests] == ["primary", "backup"]
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.payload["model_fallback"] == "backup"
    assert retrying.payload["error_code"] == {
        None: "model.provider_transport_failed",
        408: "model.provider_transient_failed",
        429: "model.provider_rate_limited",
        503: "model.provider_unavailable",
    }[status_code]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan_with_model_fallback(),
        crew_factory=FastFactory(),
    )
    await restored.restore_checkpoint(checkpoint)


@pytest.mark.parametrize("status_code", [None, 400, 401, 402, 403, 404, 405, 413, 422, 429])
async def test_provider_failure_recovery_is_bounded_and_preserves_auth_errors(
    status_code: int | None,
) -> None:
    class UnavailableGateway(RoleAwareGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise ModelTransportError("model transport failed", status_code=status_code)

    gateway = UnavailableGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _one_step_plan_with_two_model_fallbacks(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="model transport failed"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert [request.logical_model for request in gateway.requests] == (
        ["primary", "backup", "final"] if status_code in {None, 429} else ["primary"]
    )
    failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
    if status_code in {None, 429}:
        assert failed.payload["recovery_status"] == "failed_after_compact_retry"


@pytest.mark.parametrize("status_code", [None, 408, 429, 503])
@pytest.mark.parametrize("checkpoint_phase", ["provider_failed", "running", "completed"])
@pytest.mark.parametrize("repeat_tool", [False, True])
@pytest.mark.parametrize("tamper_receipt", [False, True])
async def test_provider_recovery_after_tool_success_checkpoint_hydration_preserves_receipts(
    status_code: int | None, checkpoint_phase: str, repeat_tool: bool, tamper_receipt: bool,
) -> None:
    class ProviderFailureAfterToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if len(self.requests) == 2:
                raise ModelTransportError("model transport failed", status_code=status_code)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(id="provider-call", name="web_search", arguments={"q": "safe"}),
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
                if len(self.requests) == 1 or (repeat_tool and len(self.requests) == 3)
                else ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
            )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-v4-flash",
                cost_usd=Decimal(0),
            )

    class SideEffectCapabilities(FakeCapabilities):
        def is_replay_safe(self, name: str) -> bool:
            return False

    base_plan = _tool_plan()
    plan = base_plan.model_copy(update={
        "agents": (base_plan.agents[0].model_copy(update={
            "logical_model": "primary", "fallback_models": ("backup",),
        }),),
    })
    repository = InMemoryArtifactRepository()
    capabilities = SideEffectCapabilities()
    gateway = ProviderFailureAfterToolGateway()
    actor_id = uuid4()
    runtime = CrewDispatchRuntime(
        gateway, plan, capability_gateway=capabilities,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(
        _context(actor_id=actor_id, actor_role=Role.OPERATOR),
    )]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in gateway.requests] == [
        "primary", "primary", "backup", *(["backup"] if repeat_tool else []),
    ]
    assert capabilities.calls == [("writer", "web.search")]
    checkpoint = await runtime.save_checkpoint()
    if checkpoint_phase != "completed":
        expected_model_count = 2 if checkpoint_phase == "provider_failed" else 4 if repeat_tool else 3
        expected_success_count = expected_model_count - 1
        checkpoint = next(
            event.checkpoint for event in events
            if event.checkpoint is not None
            and not event.checkpoint.state["completed"]
            and isinstance(models := event.checkpoint.state["models"], Mapping)
            and len(models) == expected_model_count
            and sum(
                isinstance(state, Mapping) and state["status"] == "succeeded"
                for state in models.values()
            ) == expected_success_count
            and any(
                isinstance(state, Mapping) and state["status"] == "failed"
                for state in models.values()
            )
        )
    if tamper_receipt:
        payload = checkpoint.to_payload()
        state = cast(dict[str, object], payload["state"])
        tools = cast(dict[str, dict[str, object]], state["tools"])
        key, receipt = next(iter(tools.items()))
        arguments_sha256 = hashlib.sha256(b'{"q":"other"}').hexdigest()
        receipt["arguments_sha256"] = arguments_sha256
        tools.pop(key)
        forged_key = hashlib.sha256(
            f"{RUN_ID}:final:0:0:0:web.search:{arguments_sha256}".encode(),
        ).hexdigest()
        tools[forged_key] = receipt
        # Recompute integrity hashes so graph validation, rather than stale hashes, rejects it.
        payload["state_sha256"] = ""
        checkpoint = RuntimeCheckpoint.from_payload(payload)
    resumed_gateway = RoleAwareGateway()
    resumed = CrewDispatchRuntime(
        resumed_gateway, plan, capability_gateway=capabilities,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    await resumed.restore_checkpoint(checkpoint)
    if tamper_receipt:
        with pytest.raises(RuntimeExecutionError, match="checkpoint capability artifact lineage"):
            _ = [event async for event in resumed.run(
                _context(checkpoint=checkpoint, actor_id=actor_id, actor_role=Role.OPERATOR),
            )]
        assert resumed_gateway.requests == []
        assert capabilities.calls == [("writer", "web.search")]
        return
    resumed_events = [event async for event in resumed.run(
        _context(checkpoint=checkpoint, actor_id=actor_id, actor_role=Role.OPERATOR),
    )]
    assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in resumed_gateway.requests] == (
        ["backup"] if checkpoint_phase == "provider_failed" else []
    )
    assert capabilities.calls == [("writer", "web.search")]
    assert not any(event.kind is EventKind.TOOL_STARTED for event in resumed_events)


async def test_agent_capacity_recovery_gets_bounded_step_deadline_window() -> None:
    gateway = SlowCapacityRecoveryGateway()
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        gateway,
        _short_timeout_plan(),
        crew_factory=RecordingFactory(generation),
    )

    events = [event async for event in runtime.run(_context(timeout_seconds=20.0))]

    assert len(gateway.requests) == 2
    assert any(event.kind is EventKind.STEP_RETRYING for event in events)
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


def test_project_scale_tool_step_gets_extended_recovery_window() -> None:
    step = DispatchStep(
        id="implementer_step",
        agent="implementer",
        task=(
            "Build a real small business project for flow=dispatch. "
            "Return strict JSON workspace_bundle.files."
        ),
        tools=("project.generate_zip",),
        token_budget=1000,
    )
    ordinary = DispatchStep(
        id="writer_step",
        agent="writer",
        task="Write a short answer",
        token_budget=1000,
    )

    assert _step_timeout_recovery_window_seconds(step) > _step_timeout_recovery_window_seconds(
        ordinary
    )


def test_project_scale_tool_step_skips_framework_raw_check() -> None:
    step = DispatchStep(
        id="implementer_step",
        agent="implementer",
        task=(
            "Build a real small business project for flow=dispatch. "
            "Return strict JSON workspace_bundle.files."
        ),
        tools=("project.generate_zip",),
        token_budget=1000,
    )
    ordinary = DispatchStep(
        id="writer_step",
        agent="writer",
        task="Return strict JSON.",
        token_budget=1000,
    )
    completion = GatewayCompletion(
        response=ModelResponse(text='{"status":"done"}', usage=TokenUsage(1, 1, 2)),
        deployment_id="primary",
        logical_model="qwen",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-v4-flash",
        cost_usd=Decimal(0),
    )

    assert not _should_check_framework_raw(step, completion)
    assert _should_check_framework_raw(ordinary, completion)


async def test_failed_model_checkpoint_resumes_through_generic_compact_retry() -> None:
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        EmptyThenRoleAwareGateway(empty_logical_model="general"),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    failed_checkpoint = None
    for checkpoint in checkpoints:
        model_states = checkpoint.state["models"]
        assert isinstance(model_states, Mapping)
        if {
            state["status"] for state in model_states.values() if isinstance(state, Mapping)
        } == {"failed"}:
            failed_checkpoint = checkpoint
            break
    assert failed_checkpoint is not None
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(failed_checkpoint)

    events = [event async for event in restored.run(_context(checkpoint=failed_checkpoint))]

    recovered = next(event for event in events if event.kind == "runtime.recovered")
    assert events[0].kind == "runtime.recovered"
    assert recovered.payload["checkpoint_id"] == str(failed_checkpoint.id)
    assert recovered.payload["checkpoint_phase"] == "running"
    assert recovered.payload["completed_steps"] == 0
    assert recovered.payload["total_steps"] == 1
    assert recovered.payload["model_status_counts"] == {"failed": 1}
    assert recovered.payload["tool_status_counts"] == {}
    assert recovered.payload["review_artifacts"] == 0
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "writer"
    assert retrying.payload["error_code"] == "model.empty_response"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert len(restored_generation.prompts) == 2
    assert "Keep the retry concise" in restored_generation.prompts[1][2]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_fallback_model_checkpoint_lineage_uses_actual_provenance() -> None:
    repository = InMemoryArtifactRepository()
    plan = _one_step_plan_with_model_fallback()
    runtime = CrewDispatchRuntime(
        CapacityUnavailableThenRoleAwareGateway(unavailable_logical_model="primary"),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )

    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    completed_checkpoint = next(
        checkpoint
        for checkpoint in reversed(checkpoints)
        if checkpoint.state["phase"] == "completed"
    )
    model_states = cast(Mapping[str, Mapping[str, JsonValue]], completed_checkpoint.state["models"])
    assert any(
        state["status"] == "succeeded"
        and cast(Mapping[str, JsonValue], state["provenance"])["logical_model"] == "backup"
        for state in model_states.values()
    )

    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(completed_checkpoint)

    events = [event async for event in restored.run(_context(checkpoint=completed_checkpoint))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_blocked_contract_self_repair_uses_checkpoint_frontier_and_repair_policy() -> None:
    repository = InMemoryArtifactRepository()
    plan = _dependent_final_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    draft_checkpoint = next(
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.state["phase"] == "running"
        and checkpoint.state["completed"] == ("draft",)
    )
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(draft_checkpoint)

    events = [
        event
        async for event in restored.run(
            _context(
                checkpoint=draft_checkpoint,
                routing_decision={
                    "source": "self_repair",
                    "self_repair_accepted": True,
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "step_failure",
                        "repair_action": "draft_repair_proposal",
                        "attempt": 1,
                        "max_attempts": 1,
                        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                        "orchestration_recovery_hint": "retry_blocked_contract_chain",
                        "blocked_contract_ids": ("draft-to-final_response",),
                        "instruction": "重规划角色交接契约链。",
                        "automatic_execution": False,
                        "requires_approval": True,
                    },
                },
            )
        )
    ]

    assert [item[0] for item in restored_generation.prompts] == ["final_response"]
    prompt = restored_generation.prompts[0][2]
    assert "SELF_REPAIR_CONTEXT" in prompt
    assert "orchestration_repair" in prompt
    assert "retry_blocked_contract_chain_after_replanning" in prompt
    assert "draft-to-final_response" in prompt
    started_steps = [
        event.step_id
        for event in events
        if event.kind is EventKind.STEP_STARTED and event.step_id is not None
    ]
    assert started_steps == ["final_response"]
    final_completed = next(
        event
        for event in events
        if event.kind is EventKind.STEP_COMPLETED and event.step_id == "final_response"
    )
    assert final_completed.payload["completed_contract_ids"] == ("draft-to-final_response",)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_blocked_contract_self_repair_reopens_completed_target_step() -> None:
    repository = InMemoryArtifactRepository()
    plan = _dependent_final_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    completed_checkpoint = next(
        checkpoint
        for checkpoint in reversed(checkpoints)
        if checkpoint.state["phase"] == "completed"
    )
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(completed_checkpoint)

    events = [
        event
        async for event in restored.run(
            _context(
                checkpoint=completed_checkpoint,
                routing_decision={
                    "source": "self_repair",
                    "self_repair_accepted": True,
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "step_failure",
                        "repair_action": "draft_repair_proposal",
                        "attempt": 1,
                        "max_attempts": 1,
                        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                        "orchestration_recovery_hint": "retry_blocked_contract_chain",
                        "blocked_contract_ids": ("draft-to-final_response",),
                        "instruction": "重规划角色交接契约链。",
                        "automatic_execution": False,
                        "requires_approval": True,
                    },
                },
            )
        )
    ]

    assert [item[0] for item in restored_generation.prompts] == ["final_response"]
    prompt = restored_generation.prompts[0][2]
    assert "orchestration_repair" in prompt
    assert "draft-to-final_response" in prompt
    started_steps = [
        event.step_id
        for event in events
        if event.kind is EventKind.STEP_STARTED and event.step_id is not None
    ]
    assert started_steps == ["final_response"]
    recovered = next(event for event in events if event.kind == "runtime.recovered")
    assert recovered.payload["checkpoint_phase"] == "completed"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    repaired_checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    )
    restored_again_generation = RecordingGeneration()
    restored_again = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_again_generation),
    )
    await restored_again.restore_checkpoint(repaired_checkpoint)

    restored_again_events = [
        event
        async for event in restored_again.run(
            _context(
                checkpoint=repaired_checkpoint,
                routing_decision={
                    "source": "self_repair",
                    "self_repair_accepted": True,
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "step_failure",
                        "repair_action": "draft_repair_proposal",
                        "attempt": 1,
                        "max_attempts": 1,
                        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                        "orchestration_recovery_hint": "retry_blocked_contract_chain",
                        "blocked_contract_ids": ("draft-to-final_response",),
                        "instruction": "重规划角色交接契约链。",
                        "automatic_execution": False,
                        "requires_approval": True,
                    },
                },
            )
        )
    ]

    assert restored_again_generation.prompts == []
    assert [event.kind for event in restored_again_events] == [EventKind.RUNTIME_COMPLETED]


async def test_blocked_contract_self_repair_preserves_unrelated_completed_branch() -> None:
    repository = InMemoryArtifactRepository()
    plan = _branched_contract_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    completed_checkpoint = next(
        checkpoint
        for checkpoint in reversed(checkpoints)
        if checkpoint.state["phase"] == "completed"
    )
    checkpoint_refs = cast(Mapping[str, Mapping[str, str]], completed_checkpoint.state["artifact_refs"])
    preserved_side_note_id = checkpoint_refs["side_note"]["id"]
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(completed_checkpoint)

    events = [
        event
        async for event in restored.run(
            _context(
                checkpoint=completed_checkpoint,
                routing_decision={
                    "source": "self_repair",
                    "self_repair_accepted": True,
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "step_failure",
                        "repair_action": "draft_repair_proposal",
                        "attempt": 1,
                        "max_attempts": 1,
                        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                        "orchestration_recovery_hint": "retry_blocked_contract_chain",
                        "blocked_contract_ids": ("source_a-to-blocked_target",),
                        "instruction": "Replan the blocked contract chain.",
                        "automatic_execution": False,
                        "requires_approval": True,
                    },
                },
            )
        )
    ]

    assert [item[0] for item in restored_generation.prompts] == [
        "blocked_target",
        "final_response",
    ]
    final_prompt = json.loads(restored_generation.prompts[-1][2])
    final_sources = final_prompt["untrusted_source_artifacts"]
    assert isinstance(final_sources, list)
    assert {source["id"] for source in final_sources} >= {preserved_side_note_id}
    assert all(source["synthesis_input"]["mode"] == "summary" for source in final_sources)
    started_steps = [
        event.step_id
        for event in events
        if event.kind is EventKind.STEP_STARTED and event.step_id is not None
    ]
    assert started_steps == ["blocked_target", "final_response"]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    final_checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    )
    restored_again = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored_again.restore_checkpoint(final_checkpoint)


async def test_blocked_contract_self_repair_requires_accepted_marker_to_reopen() -> None:
    repository = InMemoryArtifactRepository()
    plan = _dependent_final_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    completed_checkpoint = next(
        checkpoint
        for checkpoint in reversed(checkpoints)
        if checkpoint.state["phase"] == "completed"
    )
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(completed_checkpoint)

    events = [
        event
        async for event in restored.run(
            _context(
                checkpoint=completed_checkpoint,
                routing_decision={
                    "source": "self_repair",
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "step_failure",
                        "repair_action": "draft_repair_proposal",
                        "attempt": 1,
                        "max_attempts": 1,
                        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                        "orchestration_recovery_hint": "retry_blocked_contract_chain",
                        "blocked_contract_ids": ("draft-to-final_response",),
                        "instruction": "Replan the blocked contract chain.",
                        "automatic_execution": False,
                        "requires_approval": True,
                    },
                },
            )
        )
    ]

    assert restored_generation.prompts == []
    assert [event.kind for event in events] == [EventKind.RUNTIME_COMPLETED]


async def test_blocked_contract_self_repair_namespaces_reopened_tool_calls() -> None:
    repository = InMemoryArtifactRepository()
    harness = RecordingHarnessToolGateway()
    capabilities = FakeCapabilities()
    plan = _tool_contract_plan()
    runtime = CrewDispatchRuntime(
        ContractToolGateway(),
        plan,
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    checkpoints = [
        event.checkpoint
        async for event in runtime.run(_context())
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    ]
    completed_checkpoint = next(
        checkpoint
        for checkpoint in reversed(checkpoints)
        if checkpoint.state["phase"] == "completed"
    )
    assert len(harness.calls) == 1
    first_key = harness.calls[0][1].idempotency_key

    restored = CrewDispatchRuntime(
        ContractToolGateway(),
        plan,
        capability_gateway=capabilities,
        harness_tool_gateway=harness,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(completed_checkpoint)

    events = [
        event
        async for event in restored.run(
            _context(
                checkpoint=completed_checkpoint,
                routing_decision={
                    "source": "self_repair",
                    "self_repair_accepted": True,
                    "self_repair_context": {
                        "source": "self_repair",
                        "failure_kind": "step_failure",
                        "repair_action": "draft_repair_proposal",
                        "attempt": 1,
                        "max_attempts": 1,
                        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
                        "orchestration_recovery_hint": "retry_blocked_contract_chain",
                        "blocked_contract_ids": ("source_a-to-tool_target",),
                        "instruction": "Replan the blocked contract chain.",
                        "automatic_execution": False,
                        "requires_approval": True,
                    },
                },
            )
        )
    ]

    assert len(harness.calls) == 2
    second_key = harness.calls[1][1].idempotency_key
    assert second_key != first_key
    started_steps = [
        event.step_id
        for event in events
        if event.kind is EventKind.STEP_STARTED and event.step_id is not None
    ]
    assert started_steps == ["tool_target", "final_response"]


async def test_non_retryable_failed_model_checkpoint_requires_confirmation() -> None:
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        FailingModelGateway(),
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context(actor_id=uuid4(), actor_role=Role.OPERATOR)):
            events.append(event)

    def has_failed_model(event: RunEvent) -> bool:
        checkpoint = event.checkpoint
        if checkpoint is None:
            return False
        model_states = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"])
        return any(state["status"] == "failed" for state in model_states.values())

    failed_checkpoint = next(
        event.checkpoint
        for event in reversed(events)
        if event.kind is EventKind.CHECKPOINT_SAVED
        and has_failed_model(event)
    )
    assert failed_checkpoint is not None
    restored_gateway = RoleAwareGateway()
    restored = CrewDispatchRuntime(
        restored_gateway,
        _one_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(failed_checkpoint)

    with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
        [event async for event in restored.run(_context(checkpoint=failed_checkpoint))]

    assert restored_gateway.requests == []


async def test_succeeded_tool_survives_non_retryable_followup_model_failure() -> None:
    repository = InMemoryArtifactRepository()
    tool_plan = _tool_plan()
    first_capabilities = FakeCapabilities()
    runtime = CrewDispatchRuntime(
        FailingAfterToolGateway(),
        tool_plan,
        capability_gateway=first_capabilities,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    events: list[RunEvent] = []

    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context(actor_id=uuid4(), actor_role=Role.OPERATOR)):
            events.append(event)

    checkpoint = await runtime.save_checkpoint()
    persisted_artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    tool_artifacts = tuple(
        artifact for artifact in persisted_artifacts if artifact.type == "tool_result"
    )
    assert len(tool_artifacts) == 1
    assert first_capabilities.calls == [("writer", "web.search")]

    second_capabilities = FakeCapabilities()
    restored_gateway = ToolGateway()
    restored = CrewDispatchRuntime(
        restored_gateway,
        tool_plan,
        capability_gateway=second_capabilities,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)

    with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
        [
            event
            async for event in restored.run(
                _context(checkpoint=checkpoint, artifacts=persisted_artifacts)
            )
        ]

    assert restored_gateway.requests == []
    assert second_capabilities.calls == []


async def test_reviewer_timeout_uses_generic_recovery_before_soft_skip() -> None:
    generation = RecordingGeneration(reviewer_timeouts=1)
    plan = _reviewed_step_plan()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "reviewer"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert retrying.payload["recovery_layers"] == (
        "input_compression",
        "prompt_decomposition",
        "model_fallback_marked",
        "failure_closure",
    )
    assert retrying.payload["model_fallback"] == "not_available_in_crewai_bridge"
    assert "Keep the retry concise" in reviewer_prompts[1][2]
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert "review_status" not in review_completed.payload
    assert "error_code" not in review_completed.payload
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)


async def test_reviewer_empty_model_response_uses_shared_format_correction() -> None:
    gateway = EmptyThenRoleAwareGateway(empty_logical_model="review")
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 1
    assert len([request for request in gateway.requests if request.logical_model == "review"]) == 2
    assert not any(event.kind is EventKind.STEP_RETRYING for event in events)
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 8, "cost_usd": "0"}
    repairs = checkpoint.state["structured_repairs"]
    assert isinstance(repairs, Mapping) and set(repairs) == {"draft"}
    repair = repairs["draft"]
    assert isinstance(repair, Mapping)
    assert repair["actor"] == "reviewer" and repair["status"] == "succeeded"
    model_states = checkpoint.state["models"]
    assert isinstance(model_states, Mapping)
    assert {state["status"] for state in model_states.values() if isinstance(state, Mapping)} == {
        "rejected",
        "succeeded",
    }
    replay_gateway = RoleAwareGateway()
    restored = CrewDispatchRuntime(
        replay_gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [event async for event in restored.run(_context(checkpoint=checkpoint))]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]
    assert replay_gateway.requests == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in resumed_events)


async def test_reviewer_capacity_unavailable_uses_generic_recovery_before_completing() -> None:
    gateway = CapacityUnavailableThenRoleAwareGateway(unavailable_logical_model="review")
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    retrying = next(event for event in events if event.kind is EventKind.STEP_RETRYING)
    assert retrying.actor == "reviewer"
    assert retrying.payload["error_code"] == "model.capacity_unavailable"
    assert retrying.payload["recovery_strategy"] == "compact_retry"
    assert retrying.payload["recovery_attempt"] == 1
    assert "Keep the retry concise" in reviewer_prompts[1][2]
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert "review_status" not in review_completed.payload
    assert any(event.kind is EventKind.STEP_COMPLETED for event in events)


async def test_reviewer_capacity_unavailable_fails_closed_after_compact_retry() -> None:
    gateway = RepeatedCapacityUnavailableGateway(unavailable_logical_model="review", failures=2)
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        crew_factory=RecordingFactory(generation),
    )

    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    review_failed = next(event for event in events if event.kind == "review.failed")
    assert review_failed.payload["actor"] == "reviewer"
    assert review_failed.payload["review_status"] == "unverified"
    assert "verdict" not in review_failed.payload
    assert review_failed.payload["error_code"] == "model.capacity_unavailable"
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    assert len([item for item in generation.prompts if item[1] != "reviewer"]) == 1


async def test_reviewer_revise_after_compact_recovery_has_distinct_ledger_coordinates() -> None:
    gateway = CapacityThenReviewingGateway(
        empty_logical_model="review",
        reviews=('{"verdict":"revise","feedback":"tighten"}', '{"verdict":"approve"}'),
    )
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    plan = _reviewed_step_plan(reviewer_retries=1)
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    initial_events: list[RunEvent] = []
    async for event in runtime.run(_context()):
        initial_events.append(event)
    assert len(gateway.requests) == 6
    assert initial_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [
        event.payload["verdict"]
        for event in initial_events
        if event.kind is EventKind.REVIEW_COMPLETED
    ] == ["revise", "approve"]
    revise_checkpoint = next(
        event.checkpoint
        for event in initial_events
        if event.checkpoint is not None and event.checkpoint.state["review_refs"]
    )
    restored_generation = RecordingGeneration()
    restored = CrewDispatchRuntime(
        RoleAwareGateway(),
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(restored_generation),
    )
    await restored.restore_checkpoint(revise_checkpoint)

    events: list[RunEvent] = []
    async for event in restored.run(_context(checkpoint=revise_checkpoint)):
        events.append(event)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [event.payload["verdict"] for event in events
            if event.kind is EventKind.REVIEW_COMPLETED] == ["approve"]


@pytest.mark.parametrize("recovery", ["capacity", "empty"])
async def test_recovered_agent_attempt_can_still_survive_reviewer_revision_resume(
    recovery: str,
) -> None:
    factory = CapacityThenReviewingGateway if recovery == "capacity" else EmptyThenReviewingGateway
    gateway = factory(
        empty_logical_model="general",
        reviews=('{"verdict":"revise","feedback":"tighten"}', '{"verdict":"approve"}'),
    )
    generation = RecordingGeneration()
    repository = InMemoryArtifactRepository()
    plan = _reviewed_step_plan(reviewer_retries=1)
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == (
        2 if recovery == "capacity" else 1
    )
    assert len([request for request in gateway.requests if request.logical_model == "review"]) == 2
    assert [event.payload["verdict"] for event in events
            if event.kind is EventKind.REVIEW_COMPLETED] == ["revise", "approve"]
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["usage"] == {
        "tokens": 10 if recovery == "capacity" else 12, "cost_usd": "0",
    }
    repairs = checkpoint.state["structured_repairs"]
    assert isinstance(repairs, Mapping)
    assert set(repairs) == (set() if recovery == "capacity" else {"draft"})
    model_states = checkpoint.state["models"]
    assert isinstance(model_states, Mapping)
    draft_step_attempts = {
        state["attempt"]
        for state in model_states.values()
        if isinstance(state, Mapping)
        and state["step_id"] == "draft"
        and state["purpose"] == "step"
    }
    assert draft_step_attempts == ({0, 1, 3} if recovery == "capacity" else {0, 2})
    replay_gateway = RoleAwareGateway()
    restored = CrewDispatchRuntime(
        replay_gateway,
        plan,
        artifact_repository=repository,
        crew_factory=RecordingFactory(RecordingGeneration()),
    )
    await restored.restore_checkpoint(checkpoint)
    resumed_events = [event async for event in restored.run(_context(checkpoint=checkpoint))]
    assert [event.kind for event in resumed_events] == [EventKind.RUNTIME_COMPLETED]
    assert replay_gateway.requests == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in resumed_events)


async def test_reviewer_timeout_retries_before_soft_skip() -> None:
    generation = RecordingGeneration(reviewer_timeouts=1)
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _reviewed_step_plan(reviewer_retries=1),
        crew_factory=RecordingFactory(generation),
    )

    events = await _collect(runtime)

    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    review_completed = next(event for event in events if event.kind is EventKind.REVIEW_COMPLETED)
    assert review_completed.payload["verdict"] == "approve"
    assert "review_status" not in review_completed.payload
    assert "error_code" not in review_completed.payload


async def test_reviewer_timeout_fails_closed_after_retry_budget_is_exhausted() -> None:
    generation = RecordingGeneration(reviewer_timeouts=2)
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _reviewed_step_plan(),
        crew_factory=RecordingFactory(generation),
    )

    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(_context()):
            events.append(event)
    reviewer_prompts = [item for item in generation.prompts if item[1] == "reviewer"]
    assert len(reviewer_prompts) == 2
    review_failed = next(event for event in events if event.kind == "review.failed")
    assert review_failed.payload["review_status"] == "unverified"
    assert review_failed.payload["error_code"] == "crew.step_timeout"
    assert "verdict" not in review_failed.payload
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    assert len([item for item in generation.prompts if item[1] != "reviewer"]) == 1


async def test_dependent_final_step_receives_bounded_synthesis_sources() -> None:
    generation = RecordingGeneration()
    runtime = CrewDispatchRuntime(
        RoleAwareGateway(),
        _dependent_final_plan(),
        crew_factory=RecordingFactory(generation),
    )

    await _collect(runtime)

    final_prompt = next(prompt for step_id, _, prompt in generation.prompts if step_id == "final_response")
    assert '"synthesis_input":{"mode":"summary"' in final_prompt
    assert '"content":{"text"' in final_prompt
    assert '"artifact_review_packet"' not in final_prompt


def test_artifact_review_packet_payload_exposes_bounded_preview_without_full_content() -> None:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": "long text " * 1_000, "metadata": {"unsafe": "kept out of prompt"}},
        source_ids=(str(uuid4()),),
    )

    payload = _artifact_review_packet_payload(artifact)

    packet = payload["artifact_review_packet"]
    assert isinstance(packet, Mapping)
    assert packet["id"] == str(artifact.id)
    assert packet["type"] == "text"
    assert packet["producer"] == "writer"
    assert isinstance(packet["source_ids"], tuple)
    assert len(packet["source_ids"]) == 1
    assert packet["content_sha256"] == artifact.content_sha256
    assert packet["content_keys"] == ("metadata", "text")
    preview = packet["preview"]
    assert isinstance(preview, str)
    assert len(preview.encode("utf-8")) <= 1200
    assert "metadata" not in packet


def test_artifact_review_packet_payload_extracts_staged_preflight_fields() -> None:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="builder",
        content={
            "text": json.dumps(
                {
                    "summary": "done",
                    "stage_status": ["stage 1 complete"],
                    "verification_evidence": ["unit test passed"],
                    "remaining_risks": ["needs live token probe"],
                    "acceptance_review": ["approved"],
                    "stage_repair_actions": ["fixed failing build stage"],
                },
                ensure_ascii=False,
            )
        },
    )

    payload = _artifact_review_packet_payload(artifact)

    packet = payload["artifact_review_packet"]
    assert isinstance(packet, Mapping)
    assert packet["staged_preflight_fields"] == {
        "stage_status": ("stage 1 complete",),
        "verification_evidence": ("unit test passed",),
        "remaining_risks": ("needs live token probe",),
        "acceptance_review": ("approved",),
        "stage_repair_actions": ("fixed failing build stage",),
    }


def test_artifact_prompt_payload_truncates_large_text_without_mutating_artifact() -> None:
    original_text = "长文本" * 1_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="writer",
        content={"text": original_text},
    )

    payload = _artifact_prompt_payload(artifact, max_text_bytes=256)

    content = payload["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    assert len(text.encode("utf-8")) <= 256
    assert "[truncated:" in text
    assert artifact.content["text"] == original_text


def test_workspace_write_model_evidence_replaces_content_with_metadata_summary() -> None:
    content = "大型项目源码" * 20_000
    completion = GatewayCompletion(
        response=ModelResponse(
            text=None,
            tool_calls=(
                ToolCall(
                    id="write-large-file",
                    name="workspace.write_text",
                    arguments={"path": "src/large.ts", "content": content},
                ),
            ),
            usage=TokenUsage(100, 100, 200),
        ),
        deployment_id="primary",
        logical_model="general",
        provider_id="deepseek",
        provider_model="deepseek/chat",
        cost_usd=Decimal(0),
    )
    artifact = CrewDispatchRuntime._model_artifact(
        actor="implementer",
        completion=completion,
        sources=(),
    )

    payload = _artifact_prompt_payload(artifact)

    payload_content = payload["content"]
    assert isinstance(payload_content, Mapping)
    tool_calls = payload_content["tool_calls"]
    assert isinstance(tool_calls, list)
    tool_arguments = tool_calls[0]["arguments"]
    assert tool_arguments == {
        "path": "src/large.ts",
        "content_bytes": len(content.encode("utf-8")),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    assert content not in json.dumps(payload, ensure_ascii=False)
    artifact_tool_calls = artifact.content["tool_calls"]
    assert isinstance(artifact_tool_calls, tuple)
    artifact_tool_call = artifact_tool_calls[0]
    assert isinstance(artifact_tool_call, Mapping)
    artifact_arguments = artifact_tool_call["arguments"]
    assert isinstance(artifact_arguments, Mapping)
    assert artifact_arguments["content"] == content


def test_final_synthesis_payload_uses_smaller_summary_without_mutating_artifact() -> None:
    original_text = "final synthesis source " * 2_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": original_text},
    )

    payload = _artifact_final_synthesis_payload(artifact)

    content = payload["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    assert len(text.encode("utf-8")) <= 2_048
    assert "[truncated:" in text
    assert payload["synthesis_input"] == {
        "mode": "summary",
        "note": "Full artifact is stored separately; this final synthesis input is bounded to keep production model calls reliable.",
    }
    assert artifact.content["text"] == original_text
