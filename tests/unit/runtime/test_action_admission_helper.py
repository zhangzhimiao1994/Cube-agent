"""Offline admission helpers migrated from main-root tests; no provider or SQL service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime import defaults
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeCapabilityAvailability,
    FakeConfigService,
    FakeSecretService,
    ImmediateCapacity,
)


def configured_document() -> dict[str, object]:
    return {
        "models": {
            "main": {
                "deployments": [
                    {
                        "provider": "openai",
                        "model": "offline-test",
                        "api_base": "http://127.0.0.1:1/v1",
                        "credential_ref": "secret://offline",
                        "quota_scope_id": "offline",
                        "max_concurrency": 2,
                        "target_utilization": 0.8,
                        "reserved_slots": 0,
                        "capabilities": ["text", "tool_calling", "structured_output"],
                    }
                ]
            }
        },
        "agents": [
            {
                "id": "analyst",
                "role": "Analyst",
                "prompt": "Compare facts.",
                "model": "main",
                "skills": ["calculator"],
            },
            {
                "id": "critic",
                "role": "Critic",
                "prompt": "Check claims.",
                "model": "main",
                "skills": [],
            },
        ],
    }


class OfflineCapacity(ImmediateCapacity):
    def __init__(
        self,
        redis_client: object,
        *,
        deployments: Sequence[Deployment],
        fingerprint_resolver: object,
    ) -> None:
        del redis_client, fingerprint_resolver
        super().__init__(tuple(deployments))


def response(text: str | None, *, tools: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=tools,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class OfflineTransport:
    def __init__(self, replies: list[ModelResponse]) -> None:
        self.replies = list(replies)
        self.requests: list[ModelRequest] = []
        self.completed: list[ModelRequest] = []

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str
    ) -> ModelResponse:
        del deployment, api_key
        self.requests.append(request)
        if not self.replies:
            raise ModelTransportError(
                "offline fixture stopped after durable message", status_code=400
            )
        self.completed.append(request)
        return self.replies.pop(0)


class NoExecutionBackend(FakeCapabilityAvailability):
    def __init__(self) -> None:
        super().__init__(
            {
                "calculator",
                "read_context",
                "workspace.write_text",
                "project.inspect",
                "workspace.list",
                "project.verify",
                "workspace.bundle",
            }
        )
        self.effects = 0

    async def execute(self, **kwargs: Any) -> Any:
        self.effects += 1
        raise AssertionError("legacy admission must not execute a project tool")


def task(db: ApprovalProjectionSession, mode: TaskMode, permission_mode: str) -> TaskContext:
    db.run.tenant_id = TENANT_ID
    db.run.mode = mode.value
    db.run.actor_role = Role.OPERATOR.value
    db.run.routing_decision = {
        **workspace_selection(
            project_id="project",
            session_id="main",
            permission_mode=cast(Any, permission_mode),
            requested_permissions=("workspace.read", "workspace.write", "command.run"),
        ).routing_payload(),
        "conversation_id": "fixed-legacy-admission",
        "selected_agent_ids": ("analyst", "critic"),
        "main_agent_model": "main",
    }
    return TaskContext(
        run_id=db.run.id,
        tenant_id=TENANT_ID,
        actor_id=db.run.actor_id,
        actor_role=Role.OPERATOR,
        execution_token=uuid4(),
        mode=mode,
        request="Compare two explicit project implementation views.",
        token_budget=4096,
        timeout_seconds=10,
        routing_decision=cast(Mapping[str, JsonValue], db.run.routing_decision),
    )


def factory(
    mode: TaskMode,
    db: ApprovalProjectionSession,
    transport: OfflineTransport,
    backend: NoExecutionBackend,
) -> tuple[Any, RunRepository]:
    repository = RunRepository(cast(Any, db))
    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()),
        repository,
    )
    harness = HarnessToolGateway(backend, policy_gateway=policy)
    registry = defaults.configured_runtime_registry(
        config_service=cast(Any, FakeConfigService(configured_document())),
        secret_service=cast(Any, FakeSecretService()),
        redis_client=object(),
        transport=transport,
        capability_gateway=backend,
        harness_tool_gateway=harness,
        artifact_repository=InMemoryArtifactRepository(),
    )
    return registry.get(mode), repository


class Policy:
    def __init__(self) -> None:
        self.allowed: set[str] = set()

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        if request.operation in self.allowed:
            return CapabilityResult(CapabilityStatus.ALLOWED, request.run_id)
        return CapabilityResult(
            CapabilityStatus.WAITING_APPROVAL,
            request.run_id,
            approval_id="approval_" + request.operation,
            reason="capability requires approval",
        )
