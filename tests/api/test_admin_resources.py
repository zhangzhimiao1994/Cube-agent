import asyncio
import base64
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers import admin as admin_router
from agent_hub.api.routers.admin import (
    AgentResourceRequest,
    AgentResourceResponse,
    AuditEventResponse,
    InMemoryAdminResourceService,
    MainAgentConfigRequest,
    MainAgentConfigResponse,
    MainAgentModelConfig,
    McpServerRequest,
    McpServerResponse,
    ModelDeploymentRequest,
    ModelDeploymentResponse,
    PersistentAdminResourceService,
    PluginArchiveManifest,
    PluginCapabilityRequest,
    PluginPackageMetadata,
    PluginResourceRequest,
    PluginResourceResponse,
    PluginSigningKeyRequest,
    RunArtifactResponse,
    RunDetailResponse,
    RunEventResponse,
    SecretCreateRequest,
    SecretReferenceResponse,
    SystemSettingsResponse,
    _admin_run_artifact,
    _admin_run_event,
    _mode_error_log_from_run,
    _model_check_failure_details,
    _openclaw_proposal,
    _orchestration_protocol_summary_from_run_events,
    _plugin_signature_payload,
    _repair_proposal,
    _routing_details,
    _run_debug_from_detail,
)
from agent_hub.app import _submit_scheduled_task, create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.evolution import EvolutionNextRoundExecutionRequest, EvolutionRunRequest
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from agent_hub.multimodal.generation import (
    InMemoryMultimediaGenerationJobStore,
    MultimediaDailyLimitExceeded,
    MultimediaGenerationExecutor,
    MultimediaGenerationJob,
    MultimediaGenerationKind,
    MultimediaGenerationResult,
)
from agent_hub.multimodal.video_providers import VideoProviderGenerationError
from agent_hub.plugins.runtime import PluginInvocationContext, build_runtime_plugin_service
from agent_hub.runs.repository import RunRecord, _event_with_failure_diagnostic
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from agent_hub.scheduler.service import SchedulerService
from agent_hub.scheduler.types import TaskRequest
from agent_hub.security.secrets import SecretReference
from agent_hub.settings import Settings


class FakeConfigService:
    def __init__(self) -> None:
        self.current: ConfigRevision | None = None
        self.drafts: list[dict[str, object]] = []

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
        assert tenant_id == TENANT_ID
        return self.current

    async def create_draft(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        document: object,
    ) -> ConfigRevision:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        assert isinstance(document, dict)
        self.drafts.append(document)
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=len(self.drafts),
            status=ConfigStatus.DRAFT,
            document=document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )

    async def publish(
        self,
        tenant_id: UUID,
        version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        document = self.drafts[version - 1]
        self.current = ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=version,
            status=ConfigStatus.PUBLISHED,
            document=document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )
        return self.current


class FakeSecretService:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.resolved: list[tuple[UUID, str]] = []

    async def create_or_get(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        plaintext: str,
    ) -> SecretReference:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        self.values.append(plaintext)
        return SecretReference(tenant_id=tenant_id, secret_id=SECRET_ID)

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == TENANT_ID
        assert isinstance(reference, str)
        self.resolved.append((tenant_id, reference))
        return "sk-live"


class FakeModelTransport:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Deployment, ModelRequest, str]] = []

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        self.calls.append((deployment, request, api_key))
        if self.error is not None:
            raise self.error
        return ModelResponse(
            text="agent-hub-model-check-ok",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


class FakeGenerationGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GatewayCompletion(
            response=ModelResponse(text="artifact://generated-media"),
            deployment_id="media_primary_1",
            logical_model=request.logical_model,
            provider_id="minimax",
            provider_model="minimax/MiniMax-Hailuo-02",
        )


class TenantAwareMultimediaExecutor:
    def __init__(
        self,
        tenant_id: UUID | None = None,
        *,
        root: "TenantAwareMultimediaExecutor | None" = None,
    ) -> None:
        self.tenant_id = TENANT_ID if tenant_id is None else tenant_id
        self.root = root or self
        self._jobs = InMemoryMultimediaGenerationJobStore()
        if root is None:
            self.calls: list[tuple[str, UUID, str]] = []
            self.scopes: dict[UUID, TenantAwareMultimediaExecutor] = {self.tenant_id: self}

    def for_tenant(self, tenant_id: UUID) -> "TenantAwareMultimediaExecutor":
        self.root.calls.append(("for_tenant", tenant_id, ""))
        if tenant_id not in self.root.scopes:
            self.root.scopes[tenant_id] = TenantAwareMultimediaExecutor(
                tenant_id,
                root=self.root,
            )
        return self.root.scopes[tenant_id]

    def submit(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationJob:
        self.root.calls.append(("submit", self.tenant_id, logical_model))
        return self._jobs.create(kind=kind, logical_model=logical_model, prompt=prompt)

    def get_job(self, job_id: str) -> MultimediaGenerationJob:
        self.root.calls.append(("get_job", self.tenant_id, job_id))
        return self._jobs.get(job_id)

    async def run_job(
        self,
        job_id: str,
        *,
        executor_id: str,
    ) -> MultimediaGenerationJob:
        self.root.calls.append(("run_job", self.tenant_id, executor_id))
        running = self._jobs.start(job_id, executor_id=executor_id)
        return self._jobs.succeed(
            running.id,
            artifacts=(),
        )

    async def generate(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationResult:
        del prompt
        self.root.calls.append(("generate", self.tenant_id, logical_model))
        return MultimediaGenerationResult(
            kind=kind,
            logical_model=logical_model,
            deployment_id=f"{self.tenant_id}:media",
            text="artifact://tenant-media",
        )


def test_system_settings_default_openclaw_is_disabled() -> None:
    settings = SystemSettingsResponse()

    assert settings.vibe_coding_enabled is False
    assert settings.openclaw_enabled is False
    assert settings.plugin_package_subprocess_registration_status is None
    assert settings.tool_approval_mode == "auto_review"
    assert settings.openclaw_mode == "ask"
    assert settings.openclaw_allowed_commands == []
    assert settings.model_dump()["vibe_coding_enabled"] is False
    assert settings.model_dump()["openclaw_enabled"] is False
    assert settings.model_dump()["plugin_package_subprocess_registration_status"] is None
    assert settings.model_dump()["tool_approval_mode"] == "auto_review"
    assert settings.model_dump()["openclaw_mode"] == "ask"
    assert settings.model_dump()["openclaw_allowed_commands"] == []


def test_settings_response_projects_plugin_package_subprocess_registration_status() -> None:
    api = client()
    cast(Any, api.app).state.plugin_package_subprocess_registration_status = (
        "launcher_not_found"
    )

    payload = api.get("/api/v1/admin/settings", headers=headers()).json()

    assert payload["plugin_package_subprocess_registration_status"] == "launcher_not_found"


def test_settings_update_accepts_projected_plugin_package_subprocess_registration_status() -> None:
    api = client()
    cast(Any, api.app).state.plugin_package_subprocess_registration_status = (
        "launcher_not_found"
    )
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()

    response = api.put("/api/v1/admin/settings", headers=headers(), json=payload)

    assert response.status_code == 200
    assert (
        response.json()["plugin_package_subprocess_registration_status"]
        == "launcher_not_found"
    )


@pytest.mark.asyncio
async def test_persistent_settings_missing_tool_approval_mode_migrates_to_ask() -> None:
    class StoredPersistentService(PersistentAdminResourceService):
        def __init__(self) -> None:
            super().__init__(
                config_service=FakeConfigService(),  # type: ignore[arg-type]
                secret_service=FakeSecretService(),  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
                run_repository=object(),  # type: ignore[arg-type]
            )
            self.payload = SystemSettingsResponse().model_dump(mode="json")
            self.payload.pop("tool_approval_mode")

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            del tenant_id
            assert (kind, resource_id) == ("setting", "system")
            return self.payload

    settings = await StoredPersistentService().get_settings()

    assert settings.require_approval_for_tools is True
    assert settings.tool_approval_mode == "ask"


@pytest.mark.asyncio
async def test_persistent_settings_invalid_tool_approval_mode_migrates_to_ask() -> None:
    class StoredPersistentService(PersistentAdminResourceService):
        def __init__(self) -> None:
            super().__init__(
                config_service=FakeConfigService(),  # type: ignore[arg-type]
                secret_service=FakeSecretService(),  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
                run_repository=object(),  # type: ignore[arg-type]
            )
            self.payload = {
                **SystemSettingsResponse().model_dump(mode="json"),
                "tool_approval_mode": "trusted_auto",
            }

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            del tenant_id
            assert (kind, resource_id) == ("setting", "system")
            return self.payload

    settings = await StoredPersistentService().get_settings()

    assert settings.tool_approval_mode == "ask"


def test_openclaw_operation_requires_feature_switch() -> None:
    response = client().post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "smoke test OpenClaw approval path",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_disabled"


def test_openclaw_adapters_expose_multisystem_execution_boundary() -> None:
    response = client().get("/api/v1/admin/openclaw/adapters", headers=headers())

    assert response.status_code == 200
    adapters = {(adapter["platform"], adapter["kind"]): adapter for adapter in response.json()}
    assert adapters[("linux", "server_command")] == {
        "platform": "linux",
        "kind": "server_command",
        "target_type": "server",
        "status": "available",
        "execution_host": "agent-hub-server",
        "requires_user_approval": True,
        "supports_read_only": False,
        "description": "Runs exact allowlisted argv commands on the 魔方 agent Linux server after approval.",
    }
    assert adapters[("windows", "server_command")]["status"] == "adapter_unavailable"
    assert adapters[("windows", "server_command")]["execution_host"] == "remote-windows-host"
    assert adapters[("macos", "desktop_action")]["status"] == "adapter_unavailable"
    assert adapters[("linux", "screen_read")]["supports_read_only"] is True
    assert adapters[("windows", "file_read")]["requires_user_approval"] is True


def test_openclaw_operation_can_be_created_from_chat_proposal() -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    run_id = uuid4()
    now = datetime.now(UTC)
    service.runs[run_id] = RunDetailResponse(
        id=run_id,
        status="waiting_approval",
        mode="dispatch",
        conversation_id="conv-openclaw-api-test",
        request="请用 OpenClaw 在 Linux 服务器执行 python --version",
        created_at=now,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        events=[
            RunEventResponse(sequence=1, kind="queued", message="waiting approval", created_at=now)
        ],
        artifacts=[],
        explicit_details={"conversation_id": "conv-openclaw-api-test"},
        openclaw_proposal={
            "kind": "server_command",
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "operation_text": "python --version",
            "source_conversation_id": "conv-openclaw-api-test",
            "summary": "主 Agent 检测到 OpenClaw 服务器操作请求。",
            "metadata": {"source": "chat_openclaw_proposal"},
        },
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/from-run/{run_id}",
        headers=headers(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_user_approval"
    assert body["platform"] == "linux"
    assert body["kind"] == "server_command"
    assert body["operation"]["target"] == "agent-hub-server"
    assert body["operation"]["argv"] == ["python", "--version"]
    assert body["operation"]["risk_level"] == "medium"
    assert "conv-openclaw-api-test" in body["operation"]["reason"]


def test_admin_run_detail_serializes_model_outcome_summary_without_capacity_internals() -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    run_id = uuid4()
    now = datetime.now(UTC)
    service.runs[run_id] = RunDetailResponse(
        id=run_id,
        status="completed",
        mode="dispatch",
        request="route and execute models",
        created_at=now,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "model.completed",
                    "message": "model.completed",
                    "created_at": now,
                    "actor": "main_agent",
                    "payload": {
                        "requested_logical_model": "main",
                        "logical_model": "backup",
                        "provider_id": "openai",
                        "fallback_used": True,
                        "attempted_logical_models": ("main", "backup"),
                        "fallback_attempt_count": 1,
                        "quota_scope_id": "tenant-private-quota",
                        "lease_id": "lease-private",
                    },
                }
            )
        ],
        artifacts=[],
        explicit_details={},
    )

    response = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert body["model_outcome_summary"] == {
        "completion_count": 1,
        "fallback_used": True,
        "fallback_attempt_count": 1,
        "requested_logical_models": ["main"],
        "actual_logical_models": ["backup"],
        "attempted_logical_models": ["main", "backup"],
        "provider_ids": ["openai"],
        "last_requested_logical_model": "main",
        "last_logical_model": "backup",
        "last_provider_id": "openai",
    }
    serialized = json.dumps(body, ensure_ascii=False)
    assert "lease-private" not in serialized
    assert "tenant-private-quota" not in serialized


def test_admin_run_detail_serializes_runtime_recovery_summary_without_internals() -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    run_id = uuid4()
    now = datetime.now(UTC)
    checkpoint_id = str(uuid4())
    service.runs[run_id] = RunDetailResponse(
        id=run_id,
        status="running",
        mode="dispatch",
        request="resume a long running dispatch",
        created_at=now,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        events=[
            _admin_run_event(
                {
                    "sequence": 3,
                    "kind": "runtime.recovered",
                    "message": "runtime.recovered",
                    "created_at": now,
                    "payload": {
                        "checkpoint_id": checkpoint_id,
                        "checkpoint_phase": "running",
                        "completed_steps": 2,
                        "total_steps": 5,
                        "model_status_counts": {
                            "failed": 1,
                            "running": 1,
                            "succeeded": 2,
                        },
                        "tool_status_counts": {
                            "running": 1,
                            "succeeded": 3,
                        },
                        "review_artifacts": 1,
                        "lease_id": "lease-private",
                        "quota_scope_id": "tenant-private-quota",
                        "credential_ref": "credential-private",
                    },
                }
            )
        ],
        artifacts=[],
        explicit_details={},
    )

    response = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_recovery_summary"] == {
        "recovery_count": 1,
        "last_completed_steps": 2,
        "last_total_steps": 5,
        "model_status_counts": {
            "failed": 1,
            "running": 1,
            "succeeded": 2,
        },
        "tool_status_counts": {
            "running": 1,
            "succeeded": 3,
        },
        "review_artifacts": 1,
    }
    assert "checkpoint_id" not in body["events"][0]["payload"]
    assert "checkpoint_phase" not in body["events"][0]["payload"]
    serialized = json.dumps(body, ensure_ascii=False)
    assert checkpoint_id not in serialized
    assert "lease-private" not in serialized
    assert "tenant-private-quota" not in serialized
    assert "credential-private" not in serialized


def test_admin_run_detail_keeps_safe_orchestration_handoffs_without_internals() -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    run_id = uuid4()
    now = datetime.now(UTC)
    service.runs[run_id] = RunDetailResponse(
        id=run_id,
        status="completed",
        mode="dispatch",
        request="orchestrate roles",
        created_at=now,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "step.started",
                    "message": "main_agent_plan",
                    "created_at": now,
                    "actor": "main_agent",
                    "step_id": "main_agent_plan",
                    "payload": {
                        "model_execution_plan": {
                            "schema_version": 1,
                            "orchestration_handoffs": {
                                "schema_version": 1,
                                "items": (
                                    {
                                        "source_step_id": "copywriter_step",
                                        "target_step_id": "final_response_step",
                                        "source_role_id": "copywriter",
                                        "target_role_id": "final_synthesizer",
                                        "source_purpose": "execute",
                                        "target_purpose": "synthesize",
                                        "source_logical_model": "creative",
                                        "target_logical_model": "main",
                                        "handoff_kind": "step_dependency",
                                        "lease_id": "lease-private",
                                    },
                                ),
                                "truncated": False,
                            },
                            "orchestration_contracts": {
                                "schema_version": 1,
                                "items": (
                                    {
                                        "contract_id": "copywriter_step-to-final_response_step",
                                        "source_step_id": "copywriter_step",
                                        "target_step_id": "final_response_step",
                                        "source_role_id": "copywriter",
                                        "target_role_id": "final_synthesizer",
                                        "handoff_kind": "step_dependency",
                                        "status": "planned",
                                        "required_output_fields": (
                                            "status",
                                            "summary",
                                            "evidence",
                                        ),
                                        "ready_status": "done",
                                        "blocking_statuses": ("blocked", "needs_user"),
                                        "recovery_hint": "retry_blocked_contract_chain",
                                        "api_base": "https://contract-internal.example.invalid",
                                        "capacity_pool": "capacity-private",
                                        "lease": "lease-private-short",
                                        "quota_scope": "quota-private-short",
                                        "release_channel": "stable",
                                        "quota_scope_id": "contract-private-quota",
                                    },
                                ),
                                "truncated": False,
                            },
                            "orchestration_protocol": {
                                "schema_version": 1,
                                "protocol": "role_handoff_contract_v1",
                                "mode": "dispatch",
                                "role_count": 2,
                                "handoff_count": 1,
                                "contract_count": 1,
                                "structured_output_schema": "dispatch_output_v1",
                                "required_output_fields": (
                                    "status",
                                    "summary",
                                    "evidence",
                                ),
                                "ready_status": "done",
                                "blocking_statuses": ("blocked", "needs_user"),
                                "recovery_hints": ("retry_blocked_contract_chain",),
                                "truncated": False,
                                "lease_id": "lease-private",
                            },
                            "model_capability_negotiation": {
                                "schema_version": 1,
                                "items": (
                                    {
                                        "role_id": "copywriter",
                                        "logical_model": "creative",
                                        "required_capabilities": ("text", "structured_output"),
                                        "matched_capabilities": ("text",),
                                        "missing_capabilities": ("structured_output",),
                                        "status": "missing_capability",
                                        "credential_ref": "credential-private",
                                    },
                                    {
                                        "role_id": "final_synthesizer",
                                        "logical_model": "main",
                                        "required_capabilities": ("text", "structured_output"),
                                        "matched_capabilities": ("text", "structured_output"),
                                        "missing_capabilities": (),
                                        "status": "satisfied",
                                        "api_base": "https://model-internal.example.invalid",
                                    },
                                    {
                                        "role_id": "token_leak",
                                        "logical_model": "sk_secret",
                                        "required_capabilities": ("tool_calling",),
                                        "matched_capabilities": (),
                                        "missing_capabilities": ("tool_calling",),
                                        "status": "missing_capability",
                                    },
                                ),
                                "role_count": 3,
                                "satisfied_count": 1,
                                "missing_count": 2,
                                "unknown_count": 0,
                                "truncated": True,
                            },
                            "quota_scope_id": "tenant-private-quota",
                            "credential_ref": "credential-private",
                            "api_base": "https://internal.example.invalid",
                        }
                    },
                }
            )
        ],
        artifacts=[],
        explicit_details={},
    )

    response = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())

    assert response.status_code == 200
    body = response.json()
    model_execution_plan = body["events"][0]["payload"]["model_execution_plan"]
    assert model_execution_plan["orchestration_handoffs"] == {
        "schema_version": 1,
        "items": [
            {
                "source_step_id": "copywriter_step",
                "target_step_id": "final_response_step",
                "source_role_id": "copywriter",
                "target_role_id": "final_synthesizer",
                "source_purpose": "execute",
                "target_purpose": "synthesize",
                "source_logical_model": "creative",
                "target_logical_model": "main",
                "handoff_kind": "step_dependency",
                "lease_id": "[redacted]",
            }
        ],
        "truncated": False,
    }
    assert model_execution_plan["orchestration_contracts"] == {
        "schema_version": 1,
        "items": [
            {
                "contract_id": "copywriter_step-to-final_response_step",
                "source_step_id": "copywriter_step",
                "target_step_id": "final_response_step",
                "source_role_id": "copywriter",
                "target_role_id": "final_synthesizer",
                "handoff_kind": "step_dependency",
                "status": "planned",
                "required_output_fields": ["status", "summary", "evidence"],
                "ready_status": "done",
                "blocking_statuses": ["blocked", "needs_user"],
                "recovery_hint": "retry_blocked_contract_chain",
                "api_base": "[redacted]",
                "capacity_pool": "[redacted]",
                "lease": "[redacted]",
                "quota_scope": "[redacted]",
                "release_channel": "stable",
                "quota_scope_id": "[redacted]",
            }
        ],
        "truncated": False,
    }
    assert body["orchestration_protocol_summary"] == {
        "protocol": "role_handoff_contract_v1",
        "status": "planned",
        "role_count": 2,
        "handoff_count": 1,
        "contract_count": 1,
        "blocked_contract_count": 0,
        "truncated": False,
    }
    assert body["model_capability_negotiation_summary"] == {
        "role_count": 3,
        "satisfied_count": 1,
        "missing_count": 2,
        "unknown_count": 0,
        "truncated": True,
    }
    serialized = json.dumps(body, ensure_ascii=False)
    assert "lease-private" not in serialized
    assert "tenant-private-quota" not in serialized
    assert "contract-private-quota" not in serialized
    assert "capacity-private" not in serialized
    assert "lease-private-short" not in serialized
    assert "quota-private-short" not in serialized
    assert "contract-internal.example.invalid" not in serialized
    assert "credential-private" not in serialized
    assert "internal.example.invalid" not in serialized
    assert "model-internal.example.invalid" not in serialized
    assert "sk_secret" not in serialized
    assert "token_leak" not in serialized


def test_orchestration_protocol_summary_reports_blocked_and_completed_statuses() -> None:
    now = datetime.now(UTC)
    protocol_plan = {
        "schema_version": 1,
        "orchestration_protocol": {
            "schema_version": 1,
            "protocol": "role_handoff_contract_v1",
            "mode": "dispatch",
            "role_count": 2,
            "handoff_count": 1,
            "contract_count": 1,
            "structured_output_schema": "dispatch_output_v1",
            "required_output_fields": ("status", "summary"),
            "ready_status": "done",
            "blocking_statuses": ("blocked", "needs_user"),
            "recovery_hints": ("retry_blocked_contract_chain",),
            "truncated": False,
        },
        "orchestration_contracts": {
            "schema_version": 1,
            "items": (
                {
                    "contract_id": "writer_step-to-final_response_step",
                    "source_step_id": "writer_step",
                    "target_step_id": "final_response_step",
                    "source_role_id": "writer",
                    "target_role_id": "final_synthesizer",
                    "handoff_kind": "step_dependency",
                    "status": "planned",
                    "blocking_statuses": ("blocked", "needs_user"),
                },
            ),
            "truncated": False,
        },
    }
    planned_event = RunEventResponse(
        sequence=1,
        kind="step.started",
        message="main_agent_plan",
        created_at=now,
        actor="main_agent",
        step_id="main_agent_plan",
        payload={"model_execution_plan": cast(JsonValue, protocol_plan)},
    )

    blocked = _orchestration_protocol_summary_from_run_events(
        (
            planned_event,
            RunEventResponse(
                sequence=2,
                kind="step.failed",
                message="writer failed",
                created_at=now,
                actor="writer",
                step_id="writer_step",
            ),
        )
    )
    completed = _orchestration_protocol_summary_from_run_events(
        (
            planned_event,
            RunEventResponse(
                sequence=2,
                kind="step.completed",
                message="writer done",
                created_at=now,
                actor="writer",
                step_id="writer_step",
            ),
            RunEventResponse(
                sequence=3,
                kind="step.completed",
                message="final done",
                created_at=now,
                actor="final_synthesizer",
                step_id="final_response_step",
            ),
        )
    )

    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.blocked_contract_count == 1
    assert completed is not None
    assert completed.status == "completed"
    assert completed.blocked_contract_count == 0


def test_orchestration_protocol_summary_uses_step_contract_event_ids() -> None:
    now = datetime.now(UTC)
    protocol_plan = {
        "schema_version": 1,
        "orchestration_protocol": {
            "schema_version": 1,
            "protocol": "role_handoff_contract_v1",
            "mode": "dispatch",
            "role_count": 2,
            "handoff_count": 1,
            "contract_count": 1,
            "structured_output_schema": "dispatch_output_v1",
            "required_output_fields": ("status", "summary"),
            "ready_status": "done",
            "blocking_statuses": ("blocked", "needs_user"),
            "recovery_hints": ("retry_blocked_contract_chain",),
            "truncated": False,
        },
    }
    planned_event = RunEventResponse(
        sequence=1,
        kind="step.started",
        message="main_agent_plan",
        created_at=now,
        actor="main_agent",
        step_id="main_agent_plan",
        payload={"model_execution_plan": cast(JsonValue, protocol_plan)},
    )

    blocked = _orchestration_protocol_summary_from_run_events(
        (
            planned_event,
            RunEventResponse(
                sequence=2,
                kind="step.failed",
                message="writer failed",
                created_at=now,
                actor="writer",
                step_id="writer_step",
                payload={"blocked_contract_ids": ("writer_step-to-final_response_step",)},
            ),
        )
    )
    completed = _orchestration_protocol_summary_from_run_events(
        (
            planned_event,
            RunEventResponse(
                sequence=2,
                kind="step.completed",
                message="final done",
                created_at=now,
                actor="final_synthesizer",
                step_id="final_response_step",
                payload={"completed_contract_ids": ("writer_step-to-final_response_step",)},
            ),
        )
    )

    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.blocked_contract_count == 1
    assert completed is not None
    assert completed.status == "completed"
    assert completed.blocked_contract_count == 0


def test_openclaw_operation_from_run_rejects_non_openclaw_proposal() -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["openclaw_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    run_id = uuid4()
    now = datetime.now(UTC)
    service.runs[run_id] = RunDetailResponse(
        id=run_id,
        status="completed",
        mode="dispatch",
        conversation_id="conv-normal-api-test",
        request="写一个普通方案",
        created_at=now,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        events=[RunEventResponse(sequence=1, kind="completed", message="done", created_at=now)],
        artifacts=[],
        explicit_details={"conversation_id": "conv-normal-api-test"},
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/from-run/{run_id}",
        headers=headers(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_proposal_missing"


def test_openclaw_operation_creates_approval_request_when_enabled() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    response = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "smoke test OpenClaw approval path",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_user_approval"
    assert body["platform"] == "linux"
    assert body["kind"] == "server_command"
    assert body["approval_id"].startswith("openclaw_")
    assert body["requires_user_approval"] is True
    assert body["operation"]["argv"] == ["python", "--version"]
    assert "agent-hub-server" in body["approval_summary"]

    fetched = api.get(f"/api/v1/admin/openclaw/operations/{body['id']}", headers=headers())
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]
    assert fetched.json()["status"] == "waiting_user_approval"

    approved = api.patch(
        f"/api/v1/admin/openclaw/operations/{body['id']}",
        headers=headers(),
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["requires_user_approval"] is False

    repeated = api.patch(
        f"/api/v1/admin/openclaw/operations/{body['id']}",
        headers=headers(),
        json={"decision": "reject"},
    )
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "openclaw_already_resolved"


def test_openclaw_read_only_mode_rejects_write_operations() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "read_only"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    response = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "read-only mode should block command execution plans",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_read_only"


def test_openclaw_execute_requires_approved_operation() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": [sys.executable, "-c", "print('openclaw-api-exec-ok')"],
            "risk_level": "low",
            "reason": "approved execution should be required",
        },
    )
    assert created.status_code == 202

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{created.json()['id']}/execute",
        headers=headers(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_not_approved"


def test_openclaw_auto_review_approves_allowlisted_low_risk_linux_command() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-auto-review-ok')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "auto_review"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "auto review should approve only an allowlisted low-risk probe",
        },
    )

    assert created.status_code == 202
    operation = created.json()
    assert operation["status"] == "approved"
    assert operation["requires_user_approval"] is False

    executed = api.post(
        f"/api/v1/admin/openclaw/operations/{operation['id']}/execute", headers=headers()
    )
    assert executed.status_code == 200
    assert executed.json()["stdout"].strip() == "openclaw-auto-review-ok"


def test_openclaw_auto_review_keeps_unlisted_command_waiting_for_user_approval() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-auto-review-denied')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "auto_review"
    payload["openclaw_allowed_commands"] = []
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "unlisted command still needs a human approval",
        },
    )

    assert created.status_code == 202
    operation = created.json()
    assert operation["status"] == "waiting_user_approval"
    assert operation["requires_user_approval"] is True

    executed = api.post(
        f"/api/v1/admin/openclaw/operations/{operation['id']}/execute", headers=headers()
    )
    assert executed.status_code == 409
    assert executed.json()["error"]["code"] == "openclaw_not_approved"


def test_openclaw_execute_denies_approved_unlisted_command() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = []
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": [sys.executable, "-c", "print('openclaw-api-exec-ok')"],
            "risk_level": "low",
            "reason": "approved command should still require an allowlist match",
        },
    )
    operation_id = created.json()["id"]
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/operations/{operation_id}",
            headers=headers(),
            json={"decision": "approve"},
        ).status_code
        == 200
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers()
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_command_denied"


def test_openclaw_execute_runs_allowlisted_linux_command() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-api-exec-ok')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "run a bounded smoke command after approval",
        },
    )
    operation_id = created.json()["id"]
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/operations/{operation_id}",
            headers=headers(),
            json={"decision": "approve"},
        ).status_code
        == 200
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["operation"]["status"] == "executed"
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "openclaw-api-exec-ok"
    assert body["stderr"] == ""
    assert body["truncated"] is False

    fetched = api.get(f"/api/v1/admin/openclaw/operations/{operation_id}", headers=headers())
    assert fetched.json()["status"] == "executed"
    assert fetched.json()["execution"]["exit_code"] == 0


def test_openclaw_execute_denies_shell_even_when_allowlisted() -> None:
    api = client()
    command = ["bash", "-c", "echo unsafe"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "shell execution must stay blocked",
        },
    )
    operation_id = created.json()["id"]
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/operations/{operation_id}",
            headers=headers(),
            json={"decision": "approve"},
        ).status_code
        == 200
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers()
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_command_denied"


def test_openclaw_execute_returns_adapter_unavailable_for_windows_command() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [["cmd", "/c", "echo", "ok"]]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "windows",
            "kind": "server_command",
            "target": "desktop",
            "argv": ["cmd", "/c", "echo", "ok"],
            "risk_level": "low",
            "reason": "windows adapter must not be treated as linux execution",
        },
    )
    operation_id = created.json()["id"]
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/operations/{operation_id}",
            headers=headers(),
            json={"decision": "approve"},
        ).status_code
        == 200
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers()
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_adapter_unavailable"


def test_openclaw_execute_uses_configured_remote_windows_adapter() -> None:
    adapter_calls: list[dict[str, object]] = []

    class AdapterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/openclaw/health":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(
                {
                    "status": "ok",
                    "platform": "windows",
                    "capabilities": ["server_command"],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            adapter_calls.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            payload = json.dumps(
                {
                    "exit_code": 0,
                    "stdout": "windows-adapter-ok\n",
                    "stderr": "",
                    "truncated": False,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), AdapterHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = client()
        secret = api.post(
            "/api/v1/admin/secrets",
            headers=headers(),
            json={"label": "openclaw-windows-adapter", "value": "sk-live"},
        )
        assert secret.status_code == 200
        payload = api.get("/api/v1/admin/settings", headers=headers()).json()
        payload["openclaw_enabled"] = True
        payload["openclaw_mode"] = "ask"
        payload["openclaw_allowed_commands"] = [["whoami"]]
        payload["openclaw_remote_adapters"] = [
            {
                "platform": "windows",
                "target_type": "server",
                "target": "desktop",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "credential_ref": secret.json()["ref"],
            }
        ]
        assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

        adapters = api.get("/api/v1/admin/openclaw/adapters", headers=headers()).json()
        windows_command = next(
            item
            for item in adapters
            if item["platform"] == "windows" and item["kind"] == "server_command"
        )
        assert windows_command["status"] == "available"

        session = api.post(
            "/api/v1/admin/openclaw/sessions",
            headers=headers(),
            json={
                "platform": "windows",
                "target_type": "server",
                "target": "desktop",
                "purpose": "keep the configured Windows adapter bounded to this host",
            },
        )
        assert session.status_code == 201
        assert session.json()["status"] == "active"

        created = api.post(
            "/api/v1/admin/openclaw/operations",
            headers=headers(),
            json={
                "platform": "windows",
                "kind": "server_command",
                "target": "desktop",
                "argv": ["whoami"],
                "risk_level": "low",
                "reason": "execute through the configured Windows adapter",
                "session_id": session.json()["id"],
            },
        )
        assert created.status_code == 202
        operation_id = created.json()["id"]
        assert (
            api.patch(
                f"/api/v1/admin/openclaw/operations/{operation_id}",
                headers=headers(),
                json={"decision": "approve"},
            ).status_code
            == 200
        )

        executed = api.post(
            f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers()
        )

        assert executed.status_code == 200
        assert executed.json()["stdout"] == "windows-adapter-ok\n"
        assert adapter_calls == [
            {
                "path": "/v1/openclaw/execute",
                "authorization": "Bearer sk-live",
                "body": {
                    "operation_id": operation_id,
                    "platform": "windows",
                    "kind": "server_command",
                    "target": "desktop",
                    "argv": ["whoami"],
                    "risk_level": "low",
                    "reason": "execute through the configured Windows adapter",
                    "session_id": session.json()["id"],
                },
            }
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_openclaw_operation_can_bind_to_active_session() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created_session = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "keep server operations inside an approved control session",
        },
    )
    assert created_session.status_code == 201
    session_id = created_session.json()["id"]

    created_operation = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "bind this command to the active OpenClaw session",
            "session_id": session_id,
        },
    )

    assert created_operation.status_code == 202
    operation = created_operation.json()
    assert operation["operation"]["session_id"] == session_id

    sessions = api.get("/api/v1/admin/openclaw/sessions", headers=headers())
    assert sessions.status_code == 200
    stored = next(item for item in sessions.json() if item["id"] == session_id)
    assert stored["operation_ids"] == [operation["id"]]


def test_openclaw_operation_rejects_inactive_session_binding() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created_session = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "pause this session before operation binding",
        },
    )
    session_id = created_session.json()["id"]
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/sessions/{session_id}",
            headers=headers(),
            json={"action": "pause"},
        ).status_code
        == 200
    )

    created_operation = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "paused sessions cannot accept new operations",
            "session_id": session_id,
        },
    )

    assert created_operation.status_code == 409
    assert created_operation.json()["error"]["code"] == "openclaw_session_not_active"


def test_openclaw_execute_rechecks_bound_session_is_active() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-paused-session-should-not-run')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created_session = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "pause this control session before executing a bound operation",
        },
    )
    assert created_session.status_code == 201
    session_id = created_session.json()["id"]

    created_operation = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "bound operation must respect session pause at execute time",
            "session_id": session_id,
        },
    )
    assert created_operation.status_code == 202
    operation_id = created_operation.json()["id"]
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/operations/{operation_id}",
            headers=headers(),
            json={"decision": "approve"},
        ).status_code
        == 200
    )
    assert (
        api.patch(
            f"/api/v1/admin/openclaw/sessions/{session_id}",
            headers=headers(),
            json={"action": "pause"},
        ).status_code
        == 200
    )

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers()
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_session_not_active"


def test_openclaw_session_requires_feature_switch() -> None:
    response = client().post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "keep a bounded OpenClaw control session for server maintenance",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_disabled"


def test_openclaw_session_lifecycle_tracks_pause_resume_and_stop() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "keep a bounded OpenClaw control session for server maintenance",
        },
    )
    assert created.status_code == 201
    session = created.json()
    assert session["status"] == "active"
    assert session["adapter_status"] == "available"
    assert session["mode"] == "ask"
    assert session["platform"] == "linux"
    assert session["target_type"] == "server"
    assert session["operation_ids"] == []

    listed = api.get("/api/v1/admin/openclaw/sessions", headers=headers())
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [session["id"]]

    paused = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "pause"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "resume"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"

    stopped = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "stop"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"

    repeated = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "resume"},
    )
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "openclaw_session_closed"


def test_openclaw_windows_session_is_managed_but_adapter_unavailable() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "windows",
            "target_type": "computer",
            "target": "office-windows-pc",
            "purpose": "prepare a future local Windows OpenClaw adapter session",
        },
    )

    assert created.status_code == 201
    session = created.json()
    assert session["status"] == "adapter_unavailable"
    assert session["adapter_status"] == "adapter_unavailable"
    assert session["execution_host"] == "remote-windows-host"


def test_qwen_dashscope_unauthorized_model_check_returns_provider_specific_hint() -> None:
    deployment = Deployment(
        id="qwen_1",
        logical_model="qwen",
        provider_model="qwen/qwen-max",
        request_model="qwen-max",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    details = _model_check_failure_details(
        deployment,
        "provider returned status=401",
        status_code="401",
    )

    assert details["provider"] == "qwen"
    assert details["upstream_model"] == "qwen-max"
    assert "DashScope" in details["hint"]
    assert "AccessKey" in details["hint"]
    assert "Bearer" in details["hint"]


def test_openclaw_proposal_helper_preserves_safe_operation_details() -> None:
    proposal = _openclaw_proposal(
        {
            "openclaw_proposal": {
                "kind": "server_command",
                "platform": "linux",
                "target_type": "server",
                "target": "linux-server",
                "operation_text": "Use OpenClaw to execute date on the Linux server after approval.",
                "source_conversation_id": "conv-openclaw-admin",
                "summary": "Confirm before execution.",
                "metadata": {
                    "source": "chat_openclaw_proposal",
                    "requires_user_confirmation": "true",
                },
                "unsafe": object(),
            }
        }
    )

    assert proposal is not None
    assert proposal["kind"] == "server_command"
    assert proposal["platform"] == "linux"
    assert proposal["target_type"] == "server"
    assert proposal["source_conversation_id"] == "conv-openclaw-admin"
    assert proposal["metadata"] == {
        "source": "chat_openclaw_proposal",
        "requires_user_confirmation": "true",
    }
    assert "unsafe" not in proposal


def test_run_detail_response_can_expose_mode_decision_token() -> None:
    token = "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234"

    response = RunDetailResponse(
        id=uuid4(),
        status="waiting_user_mode",
        mode="auto",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="ambiguous task",
        events=[],
        artifacts=[],
        explicit_details={"version": "1"},
        decision_token=token,
    )

    assert response.decision_token == token


def test_run_detail_response_summarizes_model_outcomes_without_capacity_internals() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="completed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="use multiple models",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "model.completed",
                    "message": "model.completed",
                    "created_at": datetime.now(UTC),
                    "actor": "planner",
                    "payload": {
                        "requested_logical_model": "planner",
                        "logical_model": "planner",
                        "provider_id": "deepseek",
                        "fallback_used": False,
                        "attempted_logical_models": ("planner",),
                        "fallback_attempt_count": 0,
                        "quota_scope_id": "tenant-private-quota",
                        "lease_id": "lease-private",
                    },
                }
            ),
            _admin_run_event(
                {
                    "sequence": 2,
                    "kind": "model.completed",
                    "message": "model.completed",
                    "created_at": datetime.now(UTC),
                    "actor": "writer",
                    "payload": {
                        "requested_logical_model": "main",
                        "logical_model": "backup",
                        "provider_id": "openai",
                        "fallback_used": True,
                        "fallback_from_logical_model": "main",
                        "fallback_reason": "capacity_unavailable",
                        "attempted_logical_models": ("main", "backup"),
                        "fallback_attempt_count": 1,
                    },
                }
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    summary = response.model_outcome_summary

    assert summary.completion_count == 2
    assert summary.fallback_used is True
    assert summary.fallback_attempt_count == 1
    assert summary.requested_logical_models == ["planner", "main"]
    assert summary.actual_logical_models == ["planner", "backup"]
    assert summary.attempted_logical_models == ["planner", "main", "backup"]
    assert summary.provider_ids == ["deepseek", "openai"]
    assert summary.last_requested_logical_model == "main"
    assert summary.last_logical_model == "backup"
    assert summary.last_provider_id == "openai"
    assert "lease-private" not in summary.model_dump_json()
    assert "tenant-private-quota" not in summary.model_dump_json()


def test_run_detail_response_summarizes_direct_runtime_outcome_once() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="completed",
        mode="direct",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="direct answer",
        events=[
            RunEventResponse(
                sequence=1,
                kind="artifact.created",
                message="模型已返回直连回答。",
                created_at=datetime.now(UTC),
                actor="main_agent",
                payload={
                    "requested_logical_model": "main",
                    "logical_model": "backup",
                    "provider": "openai",
                    "fallback_used": True,
                    "fallback_from_logical_model": "main",
                    "fallback_reason": "capacity_unavailable",
                    "attempted_logical_models": ("main", "backup"),
                    "fallback_attempt_count": 1,
                },
            ),
            RunEventResponse(
                sequence=2,
                kind="runtime.completed",
                message="本次直连对话已完成。",
                created_at=datetime.now(UTC),
                actor="main_agent",
                payload={
                    "requested_logical_model": "main",
                    "logical_model": "backup",
                    "provider": "openai",
                    "fallback_used": True,
                    "fallback_from_logical_model": "main",
                    "fallback_reason": "capacity_unavailable",
                    "attempted_logical_models": ("main", "backup"),
                    "fallback_attempt_count": 1,
                },
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    summary = response.model_outcome_summary

    assert summary.completion_count == 1
    assert summary.fallback_used is True
    assert summary.fallback_attempt_count == 1
    assert summary.requested_logical_models == ["main"]
    assert summary.actual_logical_models == ["backup"]
    assert summary.provider_ids == ["openai"]


def test_run_detail_response_prefers_model_completed_for_mixed_model_events() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="completed",
        mode="direct",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="mixed legacy and model events",
        events=[
            RunEventResponse(
                sequence=1,
                kind="model.completed",
                message="model.completed",
                created_at=datetime.now(UTC),
                actor="main_agent",
                payload={
                    "requested_logical_model": "main",
                    "logical_model": "backup",
                    "provider_id": "openai",
                    "fallback_used": True,
                    "attempted_logical_models": ("main", "backup"),
                    "fallback_attempt_count": 1,
                },
            ),
            RunEventResponse(
                sequence=2,
                kind="artifact.created",
                message="artifact.created",
                created_at=datetime.now(UTC),
                actor="main_agent",
                payload={
                    "requested_logical_model": "ignored",
                    "logical_model": "ignored",
                    "provider": "ignored",
                    "fallback_used": True,
                    "attempted_logical_models": ("ignored",),
                    "fallback_attempt_count": 9,
                },
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    summary = response.model_outcome_summary

    assert summary.completion_count == 1
    assert summary.fallback_attempt_count == 1
    assert summary.requested_logical_models == ["main"]
    assert summary.actual_logical_models == ["backup"]
    assert summary.provider_ids == ["openai"]


def test_admin_run_event_exposes_safe_process_details_without_secrets() -> None:
    response = _admin_run_event(
        {
            "sequence": 7,
            "kind": "tool.completed",
            "message": "cat secret-token.txt and print private output",
            "actor": "reviewer",
            "participants": ["reviewer", "security-reviewer"],
            "tool_call_id": "call_1",
            "tool_name": "github_reader",
            "step_id": "collect-context",
            "action": "inspect",
            "payload": {
                "command": "git diff --stat",
                "api_key": "sk-should-not-leak",
                "nested": {"token": "secret", "result": "ok"},
            },
        }
    )

    serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    assert response.actor == "reviewer"
    assert response.participants == ["reviewer", "security-reviewer"]
    assert response.tool_call_id == "call_1"
    assert response.tool_name == "github_reader"
    assert response.step_id == "collect-context"
    assert response.action == "inspect"
    assert response.message == "tool.completed"
    assert response.payload == {}
    assert "git diff --stat" not in serialized
    assert "sk-should-not-leak" not in serialized
    assert "secret-token" not in serialized
    assert "result" not in serialized


def test_admin_run_event_preserves_tool_payload_approval_id_without_raw_details() -> None:
    response = _admin_run_event(
        {
            "sequence": 8,
            "kind": "tool.failed",
            "message": "tool.failed",
            "tool_name": "run_safe_command",
            "payload": {
                "approval_id": "approval_terminal_1",
                "failure_kind": "waiting_approval",
                "command": "cat private-token.txt",
                "stdout": "private output",
            },
        }
    )

    serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    assert response.payload == {
        "approval_id": "approval_terminal_1",
        "failure_kind": "waiting_approval",
    }
    assert "private-token" not in serialized
    assert "private output" not in serialized


def test_admin_run_event_redacts_capacity_internals_without_redacting_release() -> None:
    response = _admin_run_event(
        {
            "sequence": 9,
            "kind": "model.completed",
            "message": "model.completed",
            "payload": {
                "release": "20260909134530-gateway-outcome-22940cf",
                "release_id": "release-22940cf",
                "lease_id": "lease-private",
                "quota_scope_id": "tenant-private-quota",
            },
        }
    )

    assert response.payload["release"] == "20260909134530-gateway-outcome-22940cf"
    assert response.payload["release_id"] == "release-22940cf"
    assert response.payload["lease_id"] == "[redacted]"
    assert response.payload["quota_scope_id"] == "[redacted]"


def test_run_detail_response_exposes_structured_failure_diagnostics_without_raw_details() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "tool.failed",
                    "message": "cat private-token.txt failed with private output",
                    "created_at": datetime.now(UTC),
                    "actor": "engineer",
                    "tool_name": "run_safe_command",
                    "tool_call_id": "call_terminal",
                    "step_id": "engineer_step",
                    "payload": {
                        "operation_kind": "terminal",
                        "failure_kind": "capability_failed",
                        "exit_code": 127,
                        "output_bytes": 256,
                        "command": "cat private-token.txt",
                        "stdout": "private output",
                    },
                },
            ),
            RunEventResponse(
                sequence=2,
                kind="step.failed",
                message="terminal command failed",
                created_at=datetime.now(UTC),
                actor="engineer",
                step_id="engineer_step",
            ),
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="model gateway failed: model transport failed (status=401)",
                created_at=datetime.now(UTC),
                actor="reviewer",
                step_id="review_step",
                payload={"logical_model": "qwen-max", "provider": "litellm"},
            ),
            RunEventResponse(
                sequence=4,
                kind="approval.requested",
                message="approval.requested",
                created_at=datetime.now(UTC),
                actor="main_agent",
                approval_id="approval_retry_terminal",
                action="retry_terminal",
                payload={"requires_approval": True, "replay_safe": False},
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    diagnostics = response.failure_diagnostics

    assert [item.category for item in diagnostics] == ["tool", "model", "approval"]
    assert diagnostics[0].stage == "tool.failed"
    assert diagnostics[0].tool_name == "run_safe_command"
    assert diagnostics[0].failure_kind == "capability_failed"
    assert diagnostics[0].wrapped_by == 2
    assert diagnostics[1].status_code == "401"
    assert diagnostics[1].logical_model == "qwen-max"
    assert diagnostics[2].approval_id == "approval_retry_terminal"
    assert "private-token" not in serialized
    assert "private output" not in serialized


def test_run_detail_response_classifies_tool_failure_without_runtime_noise() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="tool.failed",
                message="capability execution failed",
                created_at=datetime.now(UTC),
                actor="engineer",
                tool_name="write_project_file",
                tool_call_id="call_write",
                step_id="engineer_step",
                payload={"failure_kind": "capability_failed"},
            ),
            RunEventResponse(
                sequence=2,
                kind="step.failed",
                message="step execution failed",
                created_at=datetime.now(UTC),
                actor="engineer",
                step_id="engineer_step",
            ),
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="runtime failed",
                created_at=datetime.now(UTC),
                actor="engineer",
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    diagnostics = response.failure_diagnostics

    assert len(diagnostics) == 1
    assert diagnostics[0].category == "tool"
    assert diagnostics[0].failure_kind == "capability_failed"
    assert diagnostics[0].error_code == "capability.execution_failed"
    assert diagnostics[0].retryable is True
    assert diagnostics[0].wrapped_by == 2


def test_run_detail_response_exposes_empty_response_code_and_retryability() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="hybrid",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="runtime.failed",
                message="hybrid dispatch failed: model response text is empty",
                created_at=datetime.now(UTC),
                actor="writer",
                step_id="draft",
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    diagnostic = response.failure_diagnostics[0]
    assert diagnostic.category == "model"
    assert diagnostic.error_stage == "model_response"
    assert diagnostic.error_category == "empty_response"
    assert diagnostic.error_code == "model.empty_response"
    assert diagnostic.retryable is True
    assert "压缩输入" in diagnostic.recommendation


def test_run_detail_response_prefers_specific_empty_response_over_stale_payload() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="hybrid",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="runtime.failed",
                message="hybrid dispatch failed: model response text is empty",
                created_at=datetime.now(UTC),
                actor="writer",
                step_id="draft",
                payload={
                    "error_stage": "runtime",
                    "error_category": "internal",
                    "error_code": "runtime.failed",
                    "retryable": False,
                    "suggested_action": "查看运行详情中的上一条失败事件。",
                },
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    diagnostic = response.failure_diagnostics[0]
    assert diagnostic.category == "model"
    assert diagnostic.error_stage == "model_response"
    assert diagnostic.error_category == "empty_response"
    assert diagnostic.error_code == "model.empty_response"
    assert diagnostic.retryable is True
    assert "压缩输入" in diagnostic.recommendation


def test_repository_backfills_public_diagnostic_for_failed_event_reason() -> None:
    run_id = uuid4()
    event = RunEvent(
        kind=EventKind.RUNTIME_FAILED,
        sequence=1,
        run_id=run_id,
        reason="capability failed: raw terminal output with private path",
    )

    updated = _event_with_failure_diagnostic(event)

    payload = dict(updated.payload)
    assert payload["error_summary"] == "capability failed"
    assert payload["error_stage"] == "capability"
    assert payload["error_category"] == "execution_failed"
    assert payload["error_code"] == "capability.execution_failed"
    assert "raw terminal output" not in str(payload)
    assert "private path" not in str(payload)


def test_repository_preserves_existing_failed_event_diagnostic_payload() -> None:
    run_id = uuid4()
    event = RunEvent(
        kind=EventKind.STEP_FAILED,
        sequence=1,
        run_id=run_id,
        step_id="review_step",
        actor="reviewer",
        reason="capability failed: raw terminal output",
        payload={
            "error_summary": "existing public summary",
            "error_stage": "custom_stage",
            "error_category": "custom_category",
            "error_code": "custom.failure",
            "retryable": True,
            "suggested_action": "existing action",
        },
    )

    updated = _event_with_failure_diagnostic(event)

    assert dict(updated.payload) == {
        "error_summary": "existing public summary",
        "error_stage": "custom_stage",
        "error_category": "custom_category",
        "error_code": "custom.failure",
        "retryable": True,
        "suggested_action": "existing action",
    }


def test_repair_proposal_projects_allowlisted_safe_metadata_only() -> None:
    proposal = _repair_proposal(
        {
            "repair_proposal": {
                "kind": "self_repair",
                "title": "controlled repair",
                "summary": "failed run was classified",
                "repair_action": "draft_repair_proposal",
                "failure_kind": "runtime_failure",
                "source_run_id": "run_1",
                "source_event_sequence": 2,
                "attempt": 1,
                "max_attempts": 1,
                "instruction": "只执行一次受控修复。",
                "recovery_strategy": "switch_to_available_model_and_retry",
                "orchestration_recovery_hint": "retry_blocked_contract_chain",
                "requires_approval": True,
                "replay_safe": False,
                "automatic_execution": False,
                "fingerprint": "a" * 64,
                "command": "cat private-token.txt",
                "stdout": "private output",
                "prompt": "hidden prompt",
            }
        }
    )

    serialized = json.dumps(proposal, ensure_ascii=False)
    assert proposal == {
        "kind": "self_repair",
        "title": "controlled repair",
        "summary": "failed run was classified",
        "repair_action": "draft_repair_proposal",
        "failure_kind": "runtime_failure",
        "source_run_id": "run_1",
        "source_event_sequence": 2,
        "attempt": 1,
        "max_attempts": 1,
        "instruction": "只执行一次受控修复。",
        "recovery_strategy": "switch_to_available_model_and_retry",
        "orchestration_recovery_hint": "retry_blocked_contract_chain",
        "requires_approval": True,
        "replay_safe": False,
        "automatic_execution": False,
        "fingerprint": "a" * 64,
    }
    assert "private-token" not in serialized
    assert "private output" not in serialized
    assert "hidden prompt" not in serialized


def test_repair_proposal_drops_unknown_recovery_metadata_values() -> None:
    proposal = _repair_proposal(
        {
            "repair_proposal": {
                "kind": "self_repair",
                "title": "controlled repair",
                "summary": "failed run was classified",
                "repair_action": "draft_repair_proposal",
                "failure_kind": "runtime_failure",
                "source_run_id": "run_1",
                "source_event_sequence": 2,
                "attempt": 1,
                "max_attempts": 1,
                "instruction": "只执行一次受控修复。",
                "recovery_strategy": "secret://model-provider-token",
                "orchestration_recovery_hint": "dump_private_context",
                "requires_approval": True,
                "replay_safe": False,
                "automatic_execution": False,
                "fingerprint": "a" * 64,
            }
        }
    )

    serialized = json.dumps(proposal, ensure_ascii=False)
    assert proposal is not None
    assert "recovery_strategy" not in proposal
    assert "orchestration_recovery_hint" not in proposal
    assert "secret://model-provider-token" not in serialized
    assert "dump_private_context" not in serialized


def test_run_detail_response_exposes_tool_lifecycle_without_raw_payloads() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "tool.started",
                    "message": "tool.started",
                    "created_at": datetime.now(UTC),
                    "actor": "engineer",
                    "tool_name": "run_safe_command",
                    "tool_call_id": "call_terminal",
                    "step_id": "engineer_step",
                    "payload": {
                        "operation_kind": "terminal",
                        "status": "started",
                        "argument_bytes": 42,
                        "replay_safe": False,
                        "command": "cat private-token.txt",
                    },
                },
            ),
            _admin_run_event(
                {
                    "sequence": 2,
                    "kind": "approval.requested",
                    "message": "approval.requested",
                    "created_at": datetime.now(UTC),
                    "actor": "main_agent",
                    "approval_id": "approval_terminal",
                    "step_id": "engineer_step",
                    "action": "retry_terminal",
                    "payload": {"requires_approval": True, "replay_safe": False},
                },
            ),
            _admin_run_event(
                {
                    "sequence": 3,
                    "kind": "approval.resolved",
                    "message": "approval.resolved",
                    "created_at": datetime.now(UTC),
                    "actor": "main_agent",
                    "approval_id": "approval_terminal",
                    "step_id": "engineer_step",
                    "decision": "approved",
                },
            ),
            _admin_run_event(
                {
                    "sequence": 4,
                    "kind": "tool.failed",
                    "message": "tool.failed",
                    "created_at": datetime.now(UTC),
                    "actor": "engineer",
                    "tool_name": "run_safe_command",
                    "tool_call_id": "call_terminal",
                    "step_id": "engineer_step",
                    "payload": {
                        "operation_kind": "terminal",
                        "status": "failed",
                        "exit_code": 1,
                        "output_bytes": 128,
                        "artifact_id": "artifact_terminal",
                        "failure_kind": "nonzero_exit",
                        "stdout": "private output",
                    },
                },
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)

    assert len(response.tool_lifecycle) == 1
    lifecycle = response.tool_lifecycle[0]
    assert lifecycle.tool_call_id == "call_terminal"
    assert lifecycle.tool_name == "run_safe_command"
    assert lifecycle.status == "failed"
    assert lifecycle.operation_kind == "terminal"
    assert lifecycle.actor == "engineer"
    assert lifecycle.step_id == "engineer_step"
    assert lifecycle.started_sequence == 1
    assert lifecycle.terminal_sequence == 4
    assert lifecycle.sequences == [1, 2, 3, 4]
    assert lifecycle.approval_id == "approval_terminal"
    assert lifecycle.replay_safe is False
    assert lifecycle.argument_bytes == 42
    assert lifecycle.output_bytes == 128
    assert lifecycle.exit_code == 1
    assert lifecycle.artifact_id == "artifact_terminal"
    assert lifecycle.failure_kind == "nonzero_exit"
    assert "private-token" not in serialized
    assert "private output" not in serialized


def test_tool_lifecycle_does_not_attach_unscoped_approval_to_every_tool() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="waiting_approval",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="tool.started",
                message="tool.started",
                created_at=datetime.now(UTC),
                actor="engineer",
                tool_name="first_tool",
                tool_call_id="call_first",
                step_id="first_step",
                payload={"status": "started", "operation_kind": "terminal"},
            ),
            RunEventResponse(
                sequence=2,
                kind="tool.started",
                message="tool.started",
                created_at=datetime.now(UTC),
                actor="reviewer",
                tool_name="second_tool",
                tool_call_id="call_second",
                step_id="second_step",
                payload={"status": "started", "operation_kind": "file_read"},
            ),
            RunEventResponse(
                sequence=3,
                kind="approval.requested",
                message="approval.requested",
                created_at=datetime.now(UTC),
                actor="main_agent",
                approval_id="approval_global",
                action="retry_terminal",
                payload={"requires_approval": True},
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    assert [item.approval_id for item in response.tool_lifecycle] == [None, None]
    assert [item.sequences for item in response.tool_lifecycle] == [[1], [2]]


def test_failure_diagnostics_redact_sensitive_approval_action() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="waiting_approval",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "approval.requested",
                    "message": "approval.requested",
                    "created_at": datetime.now(UTC),
                    "actor": "main_agent",
                    "approval_id": "approval_secret_action",
                    "action": "cat private-token.txt",
                }
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)

    assert response.failure_diagnostics[0].action is None
    assert response.failure_diagnostics[0].reason == "approval_required"
    assert "private-token" not in serialized


def test_failure_diagnostics_redact_sensitive_runtime_message() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            _admin_run_event(
                {
                    "sequence": 1,
                    "kind": "runtime.failed",
                    "message": "retry failed: cat private-token.txt printed private output",
                    "created_at": datetime.now(UTC),
                    "actor": "main_agent",
                    "payload": {"failure_kind": "runtime_error"},
                }
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)

    assert response.events[0].message == "redacted"
    assert response.failure_diagnostics[0].reason == "runtime failure was redacted"
    assert "private-token" not in serialized
    assert "private output" not in serialized


def test_failure_diagnostics_keep_later_independent_failures_after_tool_failure() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="tool.failed",
                message="tool.failed",
                created_at=datetime.now(UTC),
                actor="engineer",
                tool_name="run_safe_command",
                tool_call_id="call_terminal",
                step_id="engineer_step",
                payload={"failure_kind": "capability_failed"},
            ),
            RunEventResponse(
                sequence=2,
                kind="runtime.failed",
                message="model gateway failed: independent reviewer failed (status=503)",
                created_at=datetime.now(UTC),
                actor="reviewer",
                step_id="review_step",
                payload={"logical_model": "qwen-max"},
            ),
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="scheduler cleanup failed",
                created_at=datetime.now(UTC),
                actor="scheduler",
                step_id="cleanup_step",
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    assert [item.category for item in response.failure_diagnostics] == [
        "tool",
        "model",
        "runtime",
    ]
    assert response.failure_diagnostics[2].stage == "runtime.failed"
    assert response.failure_diagnostics[2].actor == "scheduler"


def test_failure_diagnostics_respect_approval_resolution_order() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="waiting_approval",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="approval.requested",
                message="approval.requested",
                created_at=datetime.now(UTC),
                actor="main_agent",
                approval_id="approval_retry",
                action="retry_terminal",
            ),
            RunEventResponse(
                sequence=2,
                kind="approval.resolved",
                message="approval.resolved",
                created_at=datetime.now(UTC),
                actor="main_agent",
                approval_id="approval_retry",
                decision="approved",
            ),
            RunEventResponse(
                sequence=3,
                kind="approval.requested",
                message="approval.requested",
                created_at=datetime.now(UTC),
                actor="main_agent",
                approval_id="approval_retry",
                action="retry_terminal",
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    assert len(response.failure_diagnostics) == 1
    assert response.failure_diagnostics[0].sequence == 3
    assert response.failure_diagnostics[0].approval_id == "approval_retry"


def test_admin_run_event_projects_safe_timeline_summary_without_raw_payload_text() -> None:
    event = _admin_run_event(
        {
            "sequence": 1,
            "kind": "review.completed",
            "message": "review completed: cat private-token.txt printed private output",
            "created_at": datetime.now(UTC),
            "actor": "reviewer",
            "payload": {
                "result": "private output should not be copied into timeline",
                "traceback": "Traceback includes private-token",
                "summary": "safe review summary",
                "logical_model": "qwen-max",
            },
        }
    )

    serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)

    assert event.summary == "reviewer completed review"
    assert event.message == "redacted"
    assert event.payload["result"] == "[redacted]"
    assert event.payload["traceback"] == "[redacted]"
    assert event.payload["summary"] == "safe review summary"
    assert "private-token" not in serialized
    assert "private output" not in serialized


def test_admin_run_event_preserves_repository_created_at() -> None:
    created_at = datetime(2026, 8, 7, 0, 0, 3, tzinfo=UTC)

    event = _admin_run_event(
        {
            "sequence": 1,
            "kind": "tool.completed",
            "message": "tool.completed",
            "summary": "terminal check completed",
            "created_at": created_at,
            "actor": "engineer",
            "tool_name": "run_safe_command",
            "payload": {"status": "completed", "operation_kind": "terminal"},
        }
    )

    assert event.created_at == created_at
    assert event.summary == "terminal check completed"


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("direct", "model gateway failed"),
        ("dispatch", "step execution failed"),
        ("discuss", "discussion_failed"),
        ("hybrid", "hybrid dispatch failed: model gateway failed"),
    ],
)
def test_mode_error_log_includes_runtime_failed_reason_from_events(
    mode: str,
    reason: str,
) -> None:
    run_id = uuid4()
    response = RunDetailResponse(
        id=run_id,
        status="failed",
        mode=mode,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="model.started",
                message="model request started",
                created_at=datetime.now(UTC),
            ),
            RunEventResponse(
                sequence=2,
                kind="runtime.failed",
                message=reason,
                created_at=datetime.now(UTC),
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert log.message == f"{mode} run failed: {reason}"
    assert log.details["reason"] == reason


def test_mode_error_log_explains_missing_legacy_failure_reason() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="direct",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert log.message == "direct run failed: failure reason was not recorded"
    assert log.details["reason"] == "failure reason was not recorded"
    assert "older runs" in log.details["diagnosis"]


def test_mode_error_log_explains_legacy_generic_gateway_reason() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="direct",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="runtime.failed",
                message="model gateway failed",
                created_at=datetime.now(UTC),
            )
        ],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert log.message == "direct run failed: model gateway failed"
    assert log.details["reason"] == "model gateway failed"
    assert "rerun" in log.details["diagnosis"].lower()


def test_mode_error_log_prefers_specific_step_reason_over_generic_terminal() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="dispatch execution failed",
                created_at=datetime.now(UTC),
            ),
            RunEventResponse(
                sequence=2,
                kind="step.failed",
                message="CrewAI step execution failed: agent identifier must be a safe identifier",
                created_at=datetime.now(UTC),
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert (
        log.message
        == "dispatch run failed: CrewAI step execution failed: agent identifier must be a safe identifier"
    )
    assert (
        log.details["reason"]
        == "CrewAI step execution failed: agent identifier must be a safe identifier"
    )
    assert "diagnosis" not in log.details


def test_run_debug_snapshot_preserves_partial_output_and_failure_context() -> None:
    run_id = uuid4()
    response = RunDetailResponse(
        id=run_id,
        status="failed",
        mode="hybrid",
        queue_wait_ms=12,
        capacity_wait_ms=3,
        cost_usd="0.042",
        request="生成代码审查报告",
        events=[
            RunEventResponse(
                sequence=1,
                kind="model.started",
                message="主 Agent 开始规划",
                created_at=datetime.now(UTC),
                actor="main_agent",
                payload={"credential_ref": "secret://main"},
            ),
            RunEventResponse(
                sequence=2,
                kind="artifact.created",
                message="已生成安全审查摘要",
                created_at=datetime.now(UTC),
                actor="security_reviewer",
            ),
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="hybrid discuss failed: model gateway failed: model transport failed",
                created_at=datetime.now(UTC),
            ),
        ],
        artifacts=[
            RunArtifactResponse(
                id="artifact_1",
                kind="text",
                title="安全审查摘要",
                text="已经完成静态审查，发现 2 个高风险问题。",
            )
        ],
        explicit_details={"routing_reason": "cross_domain_task"},
    )

    debug = _run_debug_from_detail(response)

    assert debug.run_id == run_id
    assert debug.failed_stage == "runtime.failed"
    assert (
        debug.failure_reason
        == "hybrid discuss failed: model gateway failed: model transport failed"
    )
    assert debug.partial_output_available is True
    assert debug.artifacts[0].text_preview == "已经完成静态审查，发现 2 个高风险问题。"
    assert debug.events[0].payload["credential_ref"] == "[redacted]"
    assert "模型服务" in debug.recommendation


def test_run_debug_snapshot_filters_tool_event_payloads() -> None:
    run_id = uuid4()
    response = RunDetailResponse(
        id=run_id,
        status="completed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="Run a private command",
        events=[
            RunEventResponse(
                sequence=1,
                kind="tool.completed",
                message="tool.completed",
                created_at=datetime.now(UTC),
                actor="executor",
                tool_call_id="call_1",
                tool_name="run_safe_command",
                payload={
                    "command": "echo private-token",
                    "stdout": "private terminal output",
                    "result": {"private": "secret"},
                    "exit_code": 0,
                    "output_bytes": 23,
                },
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    debug = _run_debug_from_detail(response)
    serialized = debug.model_dump_json()

    assert debug.events[0].payload == {"exit_code": 0, "output_bytes": 23}
    assert "private-token" not in serialized
    assert "private terminal output" not in serialized


def test_run_debug_endpoint_exposes_safe_failure_snapshot() -> None:
    api = client()
    run_id = "22222222-2222-4222-8222-222222222222"

    response = api.get(f"/api/v1/admin/runs/{run_id}/debug", headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["events"][0]["kind"] == "queued"
    assert body["partial_output_available"] is False
    assert body["artifacts"][0]["title"] == "Readiness report"
    assert body["artifacts"][0]["has_text"] is False


def test_routing_details_exposes_channel_directive_context() -> None:
    details = _routing_details(
        {
            "requested_channel_features": "vibe_coding",
            "requested_skills": "deep-research",
            "requested_mcp_servers": "filesystem",
            "requested_plugins": "github",
            "source": "evolution",
        }
    )

    assert details["requested_channel_features"] == "vibe_coding"
    assert details["requested_skills"] == "deep-research"
    assert details["requested_mcp_servers"] == "filesystem"
    assert details["requested_plugins"] == "github"
    assert details["source"] == "evolution"


def test_routing_details_exposes_harness_execution_profile() -> None:
    details = _routing_details(
        {
            "vibe_coding": True,
            "capability": "vibe_coding",
            "harness_decision": {
                "selected_provider": "deepseek",
                "selected_model": "deepseek-chat",
                "selected_logical_model": "main",
                "requires_approval": True,
                "capability_reasons": [
                    "supports_reasoning_delta",
                    "supports_streamed_tool_call_delta",
                    "supports_parallel_tool_calls",
                ],
                "policy_reasons": ["provider_allowed:deepseek"],
                "context_reasons": ["hermes_context_match"],
                "fallbacks_considered": ["openai"],
            },
        }
    )

    assert details["vibe_coding"] == "enabled"
    assert details["capability"] == "vibe_coding"
    assert details["harness_provider"] == "deepseek"
    assert details["harness_model"] == "deepseek-chat"
    assert details["harness_logical_model"] == "main"
    assert details["harness_requires_approval"] == "true"
    assert (
        details["harness_capabilities"]
        == "supports_reasoning_delta, supports_streamed_tool_call_delta, supports_parallel_tool_calls"
    )
    assert details["harness_policy"] == "provider_allowed:deepseek"
    assert details["harness_context"] == "hermes_context_match"
    assert details["harness_fallbacks"] == "openai"


def test_routing_details_exposes_capability_approval_id_without_fingerprint() -> None:
    details = _routing_details(
        {
            "approval_kind": "capability_tool",
            "approval_id": "approval_123",
            "approval_fingerprint": "f" * 64,
        }
    )

    assert details["approval_kind"] == "capability_tool"
    assert details["approval_id"] == "approval_123"
    assert "approval_fingerprint" not in details
    assert "f" * 64 not in repr(details)


def test_routing_details_redacts_sensitive_harness_profile_values() -> None:
    details = _routing_details(
        {
            "harness_decision": {
                "selected_provider": "sk-secret-provider",
                "selected_model": "deepseek-chat",
                "selected_logical_model": "main",
                "capability_reasons": ["supports_reasoning_delta", "bearer leaked-token"],
                "policy_reasons": ["provider_allowed:deepseek", "secret_ref:abc"],
            },
        }
    )

    assert "harness_provider" not in details
    assert details["harness_model"] == "deepseek-chat"
    assert details["harness_capabilities"] == "supports_reasoning_delta"
    assert details["harness_policy"] == "provider_allowed:deepseek"
    assert "secret" not in repr(details).lower()
    assert "bearer" not in repr(details).lower()
    assert "sk-" not in repr(details).lower()


TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_TENANT_ID = UUID("00000000-0000-4000-8000-000000000002")
ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
SECRET_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class StubAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.SUPER_ADMIN)


class OtherTenantAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(USER_ID, OTHER_TENANT_ID, Role.SUPER_ADMIN)


def client() -> TestClient:
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.settings = Settings.model_construct(
        plugin_package_store_dir=Path(tempfile.gettempdir())
        / f"agent-hub-test-plugin-packages-{uuid4()}"
    )
    return TestClient(app)


def client_with_settings(settings: Settings) -> TestClient:
    app = create_app(
        settings=settings,
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.settings = settings
    return TestClient(app)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


class TenantScopedAdminResourceService(InMemoryAdminResourceService):
    def __init__(
        self,
        tenant_id: UUID = TENANT_ID,
        actor_id: UUID = ACTOR_ID,
        *,
        root: "TenantScopedAdminResourceService | None" = None,
    ) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.root = root or self
        if root is None:
            self.scopes: dict[tuple[UUID, UUID], TenantScopedAdminResourceService] = {
                (tenant_id, actor_id): self
            }
            self.calls: list[tuple[str, str, UUID, UUID]] = []

    def for_principal(
        self, tenant_id: UUID, actor_id: UUID
    ) -> "TenantScopedAdminResourceService":
        root = self.root
        root.calls.append(("for_principal", "", tenant_id, actor_id))
        key = (tenant_id, actor_id)
        if key not in root.scopes:
            root.scopes[key] = TenantScopedAdminResourceService(
                tenant_id,
                actor_id,
                root=root,
            )
        return root.scopes[key]

    async def list_models(self) -> tuple[ModelDeploymentResponse, ...]:
        self.root.calls.append(("list_models", "", self.tenant_id, self.actor_id))
        return await super().list_models()

    async def create_model(self, request: ModelDeploymentRequest) -> ModelDeploymentResponse:
        self.root.calls.append(
            ("create_model", request.logical_model, self.tenant_id, self.actor_id)
        )
        return await super().create_model(request)

    async def create_secret(self, request: SecretCreateRequest) -> SecretReferenceResponse:
        self.root.calls.append(("create_secret", request.label, self.tenant_id, self.actor_id))
        return await super().create_secret(request)

    async def get_secret(self, ref: str) -> SecretReferenceResponse:
        self.root.calls.append(("get_secret", ref, self.tenant_id, self.actor_id))
        return await super().get_secret(ref)

    async def list_mcp_servers(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[McpServerResponse, ...]:
        target_tenant_id = self.tenant_id if tenant_id is None else tenant_id
        self.root.calls.append(("list_mcp", "", target_tenant_id, self.actor_id))
        if target_tenant_id != self.tenant_id:
            return ()
        return await super().list_mcp_servers(tenant_id=tenant_id)

    async def upsert_mcp_server(self, request: McpServerRequest) -> McpServerResponse:
        self.root.calls.append(("upsert_mcp", request.id, self.tenant_id, self.actor_id))
        response = await super().upsert_mcp_server(request)
        await self.record_audit_event(
            actor=str(self.actor_id),
            action="mcp.upsert",
            resource=f"mcp:{request.id}",
            details={"id": request.id},
            tenant_id=self.tenant_id,
        )
        return response

    async def delete_mcp_server(self, server_id: str) -> None:
        self.root.calls.append(("delete_mcp", server_id, self.tenant_id, self.actor_id))
        await super().delete_mcp_server(server_id)
        await self.record_audit_event(
            actor=str(self.actor_id),
            action="mcp.delete",
            resource=f"mcp:{server_id}",
            details={"id": server_id},
            tenant_id=self.tenant_id,
        )

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
        tenant_id: UUID | None = None,
    ) -> AuditEventResponse:
        target_tenant_id = self.tenant_id if tenant_id is None else tenant_id
        self.root.calls.append(("audit", action, target_tenant_id, UUID(actor)))
        return await super().record_audit_event(
            actor=actor,
            action=action,
            resource=resource,
            details=details,
            tenant_id=tenant_id,
        )

    async def list_audit_events(self, action: str | None = None) -> tuple[AuditEventResponse, ...]:
        self.root.calls.append(("list_audit", action or "", self.tenant_id, self.actor_id))
        return await super().list_audit_events(action)

    async def get_settings(self) -> SystemSettingsResponse:
        self.root.calls.append(("get_settings", "", self.tenant_id, self.actor_id))
        return await super().get_settings()

    async def update_settings(
        self, request: admin_router.SystemSettingsRequest
    ) -> SystemSettingsResponse:
        self.root.calls.append(("update_settings", request.default_mode, self.tenant_id, self.actor_id))
        return await super().update_settings(request)

    async def get_main_agent_config(self) -> MainAgentConfigResponse:
        self.root.calls.append(("get_main_agent", "", self.tenant_id, self.actor_id))
        return await super().get_main_agent_config()

    async def update_main_agent_config(
        self, request: MainAgentConfigRequest
    ) -> MainAgentConfigResponse:
        self.root.calls.append(
            ("update_main_agent", request.control_mode, self.tenant_id, self.actor_id)
        )
        return await super().update_main_agent_config(request)

    async def list_memory(self) -> tuple[admin_router.MemoryRecordResponse, ...]:
        self.root.calls.append(("list_memory", "", self.tenant_id, self.actor_id))
        return await super().list_memory()

    async def create_memory(
        self, request: admin_router.MemoryCreateRequest
    ) -> admin_router.MemoryRecordResponse:
        self.root.calls.append(("create_memory", request.id, self.tenant_id, self.actor_id))
        return await super().create_memory(request)

    async def update_memory(
        self, memory_id: str, request: admin_router.MemoryRecordRequest
    ) -> admin_router.MemoryRecordResponse:
        self.root.calls.append(("update_memory", memory_id, self.tenant_id, self.actor_id))
        return await super().update_memory(memory_id, request)

    async def forget_memory(self, memory_id: str) -> None:
        self.root.calls.append(("forget_memory", memory_id, self.tenant_id, self.actor_id))
        await super().forget_memory(memory_id)

    async def list_logs(self, category: str | None = None) -> tuple[admin_router.LogEntryResponse, ...]:
        self.root.calls.append(("list_logs", category or "", self.tenant_id, self.actor_id))
        return await super().list_logs(category)

    async def record_hermes_feedback(
        self, request: admin_router.HermesFeedbackRequest
    ) -> admin_router.HermesInsightResponse:
        self.root.calls.append(("hermes_feedback", request.outcome, self.tenant_id, self.actor_id))
        return await super().record_hermes_feedback(request)

    async def list_hermes_insights(self) -> tuple[admin_router.HermesInsightResponse, ...]:
        self.root.calls.append(("list_hermes", "", self.tenant_id, self.actor_id))
        return await super().list_hermes_insights()

    async def get_hermes_insight(self, insight_id: str) -> admin_router.HermesInsightResponse:
        self.root.calls.append(("get_hermes", insight_id, self.tenant_id, self.actor_id))
        return await super().get_hermes_insight(insight_id)

    async def confirm_hermes_insight(self, insight_id: str) -> admin_router.HermesInsightResponse:
        self.root.calls.append(("confirm_hermes", insight_id, self.tenant_id, self.actor_id))
        return await super().confirm_hermes_insight(insight_id)

    async def delete_hermes_insight(self, insight_id: str) -> None:
        self.root.calls.append(("delete_hermes", insight_id, self.tenant_id, self.actor_id))
        await super().delete_hermes_insight(insight_id)

    async def recommend_with_hermes(
        self, request: admin_router.HermesRecommendationRequest
    ) -> admin_router.HermesRecommendationResponse:
        self.root.calls.append(("recommend_hermes", request.task, self.tenant_id, self.actor_id))
        return await super().recommend_with_hermes(request)

    async def list_runs(self) -> tuple[admin_router.RunListItem, ...]:
        self.root.calls.append(("list_runs", "", self.tenant_id, self.actor_id))
        return await super().list_runs()

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        self.root.calls.append(("get_run", str(run_id), self.tenant_id, self.actor_id))
        return await super().get_run(run_id)

    async def download_run_artifact(
        self, run_id: UUID, artifact_id: UUID, *, tenant_id: UUID | None = None
    ) -> admin_router.GeneratedArtifactDownload:
        self.root.calls.append(
            (
                "download_artifact",
                f"{run_id}:{tenant_id}",
                self.tenant_id,
                self.actor_id,
            )
        )
        return await super().download_run_artifact(run_id, artifact_id, tenant_id=tenant_id)

    async def pause_run(self, run_id: UUID) -> RunDetailResponse:
        self.root.calls.append(("pause_run", str(run_id), self.tenant_id, self.actor_id))
        return await super().pause_run(run_id)

    async def resume_run(self, run_id: UUID) -> RunDetailResponse:
        self.root.calls.append(("resume_run", str(run_id), self.tenant_id, self.actor_id))
        return await super().resume_run(run_id)

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse:
        self.root.calls.append(("cancel_run", str(run_id), self.tenant_id, self.actor_id))
        return await super().cancel_run(run_id)

    async def delete_run(self, run_id: UUID) -> admin_router.RunDeleteResponse:
        self.root.calls.append(("delete_run", str(run_id), self.tenant_id, self.actor_id))
        return await super().delete_run(run_id)

    async def create_openclaw_session(
        self,
        request: admin_router.OpenClawSessionRequest,
        *,
        actor: str,
        mode: str,
        settings: SystemSettingsResponse,
    ) -> admin_router.OpenClawSessionResponse:
        self.root.calls.append(("create_openclaw_session", request.target, self.tenant_id, UUID(actor)))
        return await super().create_openclaw_session(
            request,
            actor=actor,
            mode=mode,
            settings=settings,
        )

    async def list_openclaw_sessions(self) -> tuple[admin_router.OpenClawSessionResponse, ...]:
        self.root.calls.append(("list_openclaw_sessions", "", self.tenant_id, self.actor_id))
        return await super().list_openclaw_sessions()

    async def update_openclaw_session(
        self,
        session_id: str,
        request: admin_router.OpenClawSessionActionRequest,
        *,
        actor: str,
    ) -> admin_router.OpenClawSessionResponse:
        self.root.calls.append(("update_openclaw_session", session_id, self.tenant_id, UUID(actor)))
        return await super().update_openclaw_session(session_id, request, actor=actor)

    async def create_openclaw_operation(
        self,
        request: admin_router.OpenClawOperationRequest,
        *,
        actor: str,
        mode: str,
    ) -> admin_router.OpenClawOperationResponse:
        self.root.calls.append(("create_openclaw_operation", request.target, self.tenant_id, UUID(actor)))
        return await super().create_openclaw_operation(request, actor=actor, mode=mode)

    async def get_openclaw_operation(self, operation_id: str) -> admin_router.OpenClawOperationResponse:
        self.root.calls.append(("get_openclaw_operation", operation_id, self.tenant_id, self.actor_id))
        return await super().get_openclaw_operation(operation_id)

    async def resolve_openclaw_operation(
        self,
        operation_id: str,
        request: admin_router.OpenClawResolveRequest,
        *,
        actor: str,
    ) -> admin_router.OpenClawOperationResponse:
        self.root.calls.append(("resolve_openclaw_operation", operation_id, self.tenant_id, UUID(actor)))
        return await super().resolve_openclaw_operation(operation_id, request, actor=actor)

    async def attach_openclaw_operation_to_session(
        self,
        session_id: str,
        operation_id: str,
        request: admin_router.OpenClawOperationRequest,
        *,
        actor: str,
    ) -> admin_router.OpenClawSessionResponse:
        self.root.calls.append(("attach_openclaw_operation", session_id, self.tenant_id, UUID(actor)))
        return await super().attach_openclaw_operation_to_session(
            session_id,
            operation_id,
            request,
            actor=actor,
        )


def test_admin_service_dependency_scopes_service_to_principal_tenant() -> None:
    class ScopedAdminService(InMemoryAdminResourceService):
        def __init__(self, tenant_id: UUID) -> None:
            super().__init__()
            self.tenant_id = tenant_id
            self.scope_calls: list[tuple[UUID, UUID]] = []

        def for_principal(
            self, tenant_id: UUID, actor_id: UUID
        ) -> "ScopedAdminService":
            self.scope_calls.append((tenant_id, actor_id))
            return ScopedAdminService(tenant_id)

        async def list_agents(self) -> tuple[AgentResourceResponse, ...]:
            return (
                AgentResourceResponse(
                    id=f"agent-{self.tenant_id}",
                    name=f"Agent {self.tenant_id}",
                    enabled=True,
                    role="assistant",
                    prompt="help",
                    model="main",
                    skills=[],
                ),
            )

    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = ScopedAdminService(TENANT_ID)
    cast(Any, api).state.admin_resource_service = service

    response = TestClient(api).get("/api/v1/admin/agents", headers=headers())

    assert response.status_code == 200
    assert service.scope_calls == [(OTHER_TENANT_ID, USER_ID)]
    assert response.json()[0]["id"] == f"agent-{OTHER_TENANT_ID}"


def test_admin_models_and_secrets_scope_to_principal_tenant_and_actor() -> None:
    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = TenantScopedAdminResourceService()
    cast(Any, api).state.admin_resource_service = service
    test_client = TestClient(api)

    created_model = test_client.post(
        "/api/v1/admin/models",
        headers=headers(),
        json=model_payload(),
    )
    listed_models = test_client.get("/api/v1/admin/models", headers=headers())
    created_secret = test_client.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "deepseek", "value": "sk-other-tenant"},
    )
    secret_ref = created_secret.json()["ref"]
    fetched_secret = test_client.get(f"/api/v1/admin/secrets/{secret_ref}", headers=headers())

    assert created_model.status_code == 200
    assert listed_models.status_code == 200
    assert [item["logical_model"] for item in listed_models.json()] == ["planner"]
    assert created_secret.status_code == 200
    assert fetched_secret.status_code == 200
    assert "sk-other-tenant" not in created_secret.text + fetched_secret.text
    assert service.scopes[(TENANT_ID, ACTOR_ID)].models == {}
    assert service.scopes[(TENANT_ID, ACTOR_ID)].secrets == {}
    assert service.calls == [
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("create_model", "planner", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("list_models", "", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("create_secret", "deepseek", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("get_secret", secret_ref, OTHER_TENANT_ID, USER_ID),
    ]


def test_admin_settings_memory_logs_and_hermes_scope_to_principal_tenant_and_actor() -> None:
    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = TenantScopedAdminResourceService()
    bootstrap_service = service.scopes[(TENANT_ID, ACTOR_ID)]
    bootstrap_service.memory["bootstrap-only"] = admin_router.MemoryRecordResponse(
        id="bootstrap-only",
        scope="tenant",
        value="bootstrap memory",
    )
    bootstrap_service.audit_events.append(
        AuditEventResponse(
            id="audit-bootstrap",
            actor=str(ACTOR_ID),
            action="bootstrap.only",
            resource="bootstrap",
            created_at=datetime.now(UTC),
        )
    )
    bootstrap_service.hermes_insights["hermes-bootstrap"] = admin_router.HermesInsightResponse(
        id="hermes-bootstrap",
        category="conversation",
        outcome="success",
        lesson="bootstrap lesson",
        summary="bootstrap summary",
        user_summary="bootstrap user summary",
        run_id=None,
        conversation_id=None,
        confirmed_at=None,
        tags=["bootstrap"],
        weight=1,
        created_at=datetime.now(UTC),
    )
    service.for_principal(OTHER_TENANT_ID, USER_ID)
    service.calls.clear()
    cast(Any, api).state.admin_resource_service = service
    test_client = TestClient(api)

    settings_payload = SystemSettingsResponse(default_mode="hybrid").model_dump(mode="json")
    updated_settings = test_client.put(
        "/api/v1/admin/settings",
        headers=headers(),
        json=settings_payload,
    )
    fetched_settings = test_client.get("/api/v1/admin/settings", headers=headers())
    updated_main_agent = test_client.put(
        "/api/v1/admin/main-agent",
        headers=headers(),
        json={"control_mode": "planner"},
    )
    fetched_main_agent = test_client.get("/api/v1/admin/main-agent", headers=headers())
    created_memory = test_client.post(
        "/api/v1/admin/memory",
        headers=headers(),
        json={"id": "tenant-memory", "value": "tenant memory"},
    )
    listed_memory = test_client.get("/api/v1/admin/memory", headers=headers())
    updated_memory = test_client.patch(
        "/api/v1/admin/memory/tenant-memory",
        headers=headers(),
        json={"value": "tenant memory updated"},
    )
    logs = test_client.get("/api/v1/admin/logs?category=audit", headers=headers())
    created_hermes = test_client.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "tenant lesson",
            "tags": ["tenant"],
            "weight": 2,
        },
    )
    hermes_id = created_hermes.json()["id"]
    listed_hermes = test_client.get("/api/v1/admin/hermes", headers=headers())
    fetched_hermes = test_client.get(f"/api/v1/admin/hermes/{hermes_id}", headers=headers())
    confirmed_hermes = test_client.post(
        f"/api/v1/admin/hermes/{hermes_id}/confirm",
        headers=headers(),
    )
    hermes_recommendation = test_client.post(
        "/api/v1/admin/hermes/recommend",
        headers=headers(),
        json={"task": "tenant planning review", "mode_candidates": ["dispatch"]},
    )
    missing_bootstrap_hermes = test_client.get(
        "/api/v1/admin/hermes/hermes-bootstrap",
        headers=headers(),
    )
    deleted_hermes = test_client.delete(f"/api/v1/admin/hermes/{hermes_id}", headers=headers())
    forgotten_memory = test_client.delete("/api/v1/admin/memory/tenant-memory", headers=headers())

    assert updated_settings.status_code == 200
    assert fetched_settings.status_code == 200
    assert fetched_settings.json()["default_mode"] == "hybrid"
    assert updated_main_agent.status_code == 200
    assert fetched_main_agent.status_code == 200
    assert fetched_main_agent.json()["control_mode"] == "planner"
    assert created_memory.status_code == 200
    assert listed_memory.status_code == 200
    listed_memory_ids = {item["id"] for item in listed_memory.json()}
    assert "tenant-memory" in listed_memory_ids
    assert "bootstrap-only" not in listed_memory_ids
    assert updated_memory.status_code == 200
    assert updated_memory.json()["value"] == "tenant memory updated"
    assert logs.status_code == 200
    assert "bootstrap.only" not in {item["message"] for item in logs.json()}
    assert created_hermes.status_code == 200
    assert listed_hermes.status_code == 200
    listed_hermes_ids = {item["id"] for item in listed_hermes.json()}
    assert hermes_id in listed_hermes_ids
    assert "hermes-bootstrap" not in listed_hermes_ids
    assert fetched_hermes.status_code == 200
    assert confirmed_hermes.status_code == 200
    assert hermes_recommendation.status_code == 200
    assert missing_bootstrap_hermes.status_code == 404
    assert deleted_hermes.status_code == 200
    assert forgotten_memory.status_code == 200
    assert bootstrap_service.settings.default_mode == "auto"
    assert bootstrap_service.main_agent_config.control_mode == "supervisor"
    assert "tenant-memory" not in bootstrap_service.memory
    assert all(call[2] == OTHER_TENANT_ID and call[3] == USER_ID for call in service.calls)


def test_admin_run_artifact_debug_and_openclaw_scope_to_principal_tenant_and_actor(
    tmp_path: Path,
) -> None:
    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = TenantScopedAdminResourceService()
    other_service = service.for_principal(OTHER_TENANT_ID, USER_ID)
    run_id = next(iter(other_service.runs))
    artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    artifact_path = tmp_path / "tenant-artifact.zip"
    artifact_path.write_bytes(b"tenant zip")
    other_service.generated_artifacts[(run_id, artifact_id)] = (
        artifact_path,
        "tenant-artifact.zip",
        "application/zip",
    )
    other_service.settings = SystemSettingsResponse(openclaw_enabled=True)
    other_service.runs[run_id] = other_service.runs[run_id].model_copy(
        update={
            "status": "waiting_approval",
            "openclaw_proposal": {
                "platform": "linux",
                "kind": "server_command",
                "target": "linux-server",
                "operation_text": "date",
                "source_conversation_id": "conv-tenant",
            },
        }
    )
    service.calls.clear()
    cast(Any, api).state.admin_resource_service = service
    test_client = TestClient(api)

    listed_runs = test_client.get("/api/v1/admin/runs", headers=headers())
    run_detail = test_client.get(f"/api/v1/admin/runs/{run_id}", headers=headers())
    run_debug = test_client.get(f"/api/v1/admin/runs/{run_id}/debug", headers=headers())
    downloaded = test_client.get(
        f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download",
        headers=headers(),
    )
    operation_from_run = test_client.post(
        f"/api/v1/admin/openclaw/operations/from-run/{run_id}",
        headers=headers(),
    )
    operation_id = operation_from_run.json()["id"]
    fetched_operation = test_client.get(
        f"/api/v1/admin/openclaw/operations/{operation_id}",
        headers=headers(),
    )
    created_session = test_client.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "tenant session",
        },
    )
    listed_sessions = test_client.get("/api/v1/admin/openclaw/sessions", headers=headers())

    assert listed_runs.status_code == 200
    assert {item["id"] for item in listed_runs.json()} == {str(run_id)}
    assert run_detail.status_code == 200
    assert run_debug.status_code == 200
    assert downloaded.status_code == 200
    assert downloaded.content == b"tenant zip"
    assert operation_from_run.status_code == 202
    assert fetched_operation.status_code == 200
    assert created_session.status_code == 201
    assert listed_sessions.status_code == 200
    assert listed_sessions.json()[0]["id"] == created_session.json()["id"]
    assert all(call[2] == OTHER_TENANT_ID and call[3] == USER_ID for call in service.calls)
    assert (
        "download_artifact",
        f"{run_id}:{OTHER_TENANT_ID}",
        OTHER_TENANT_ID,
        USER_ID,
    ) in service.calls


class FakeRuntimeCapabilityGateway:
    def __init__(self) -> None:
        self.tenant_ids: list[UUID] = []

    def capability_manifest(
        self,
        tenant_id: UUID,
        *,
        extra_sources: tuple[object, ...] = (),
    ) -> dict[str, object]:
        self.tenant_ids.append(tenant_id)
        capabilities: list[object] = []
        for source in extra_sources:
            source_manifest = cast(Any, source).manifests()
            raw_capabilities = source_manifest["capabilities"]
            assert isinstance(raw_capabilities, tuple)
            capabilities.extend(raw_capabilities)
        capabilities.append(
            {
                "id": "mcp.search",
                "kind": "mcp",
                "adapter": "mcp_server",
                "permission_class": "mcp.call",
                "sandbox_profile": "remote_connector",
                "available": True,
                "availability_reason": None,
                "replay_safe": False,
                "aliases": ("search_web",),
            }
        )
        return {
            "schema_version": 1,
            "capabilities": tuple(capabilities),
        }


def test_capability_manifest_endpoint_exposes_runtime_gateway_manifest() -> None:
    api = client()
    gateway = FakeRuntimeCapabilityGateway()
    cast(Any, api.app).state.runtime_capability_gateway = gateway

    response = api.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert response.status_code == 200
    assert gateway.tenant_ids == [TENANT_ID]
    body = response.json()
    capabilities = {
        item["id"]: item
        for item in body["capabilities"]
    }
    assert body["schema_version"] == 1
    assert "filesystem.list_directory" in capabilities
    assert "filesystem.read_file" in capabilities
    assert capabilities["mcp.search"] == {
        "id": "mcp.search",
        "kind": "mcp",
        "adapter": "mcp_server",
        "permission_class": "mcp.call",
        "sandbox_profile": "remote_connector",
        "policy_effect": "inherit",
        "available": True,
        "availability_reason": None,
        "replay_safe": False,
        "aliases": ["search_web"],
        "input_schema": None,
        "output_schema": None,
    }


def test_capability_manifest_endpoint_includes_saved_mcp_config_tools() -> None:
    api = client()
    gateway = FakeRuntimeCapabilityGateway()
    cast(Any, api.app).state.runtime_capability_gateway = gateway
    response = api.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={
            "id": "filesystem",
            "name": "Filesystem MCP",
            "allowed_tools": ["read_file", "list_directory"],
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-filesystem"],
            "executable_allowlist": ["uvx"],
            "timeout_seconds": 10,
        },
    )
    assert response.status_code == 200

    manifest_response = api.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert manifest_response.status_code == 200
    capabilities = {
        item["id"]: item
        for item in manifest_response.json()["capabilities"]
    }
    assert capabilities["filesystem.list_directory"] == {
        "id": "filesystem.list_directory",
        "kind": "mcp",
        "adapter": "mcp_server",
        "permission_class": "mcp.invoke",
        "sandbox_profile": "mcp_stdio",
        "policy_effect": "inherit",
        "available": False,
        "availability_reason": "mcp_server_not_discovered",
        "replay_safe": False,
        "aliases": [],
        "input_schema": None,
        "output_schema": None,
    }
    assert capabilities["filesystem.read_file"]["sandbox_profile"] == "mcp_stdio"


def test_capability_manifest_endpoint_reads_mcp_config_for_principal_tenant() -> None:
    class OtherTenantAuthService:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            if token != "valid-token":
                raise InvalidCredentials("bad token")
            return AuthenticatedPrincipal(USER_ID, OTHER_TENANT_ID, Role.SUPER_ADMIN)

    class RecordingMcpService:
        def __init__(self) -> None:
            self.tenant_ids: list[UUID | None] = []

        async def list_mcp_servers(
            self,
            *,
            tenant_id: UUID | None = None,
        ) -> tuple[McpServerResponse, ...]:
            self.tenant_ids.append(tenant_id)
            return ()

        async def list_plugins(
            self,
            *,
            tenant_id: UUID | None = None,
        ) -> tuple[object, ...]:
            del tenant_id
            return ()

    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = RecordingMcpService()
    gateway = FakeRuntimeCapabilityGateway()
    cast(Any, api).state.admin_resource_service = service
    cast(Any, api).state.runtime_capability_gateway = gateway

    response = TestClient(api).get(
        "/api/v1/admin/capabilities/manifest",
        headers=headers(),
    )

    assert response.status_code == 200
    assert service.tenant_ids == [OTHER_TENANT_ID]
    assert gateway.tenant_ids == [OTHER_TENANT_ID]


def test_plugin_admin_endpoints_read_plugin_config_for_principal_tenant() -> None:
    class OtherTenantAuthService:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            if token != "valid-token":
                raise InvalidCredentials("bad token")
            return AuthenticatedPrincipal(USER_ID, OTHER_TENANT_ID, Role.SUPER_ADMIN)

    class TenantScopedPluginService:
        def __init__(self) -> None:
            self.tenant_ids: list[UUID | None] = []

        async def list_plugins(
            self,
            *,
            tenant_id: UUID | None = None,
        ) -> tuple[PluginResourceResponse, ...]:
            self.tenant_ids.append(tenant_id)
            if tenant_id == OTHER_TENANT_ID:
                return (
                    PluginResourceResponse(
                        id="tenant-search",
                        name="Tenant Search Plugin",
                        status="running",
                        health="healthy",
                        capabilities=[
                            PluginCapabilityRequest(
                                id="tenant_search.web",
                                permission_class="network.read",
                                sandbox_profile="remote_connector",
                            )
                        ],
                    ),
                )
            return (
                PluginResourceResponse(
                    id="bootstrap-search",
                    name="Bootstrap Search Plugin",
                    status="running",
                    health="healthy",
                    capabilities=[
                        PluginCapabilityRequest(
                            id="bootstrap_search.web",
                            permission_class="network.read",
                            sandbox_profile="remote_connector",
                        )
                    ],
                ),
            )

        async def list_mcp_servers(
            self,
            *,
            tenant_id: UUID | None = None,
        ) -> tuple[McpServerResponse, ...]:
            del tenant_id
            return ()

        async def record_audit_event(
            self,
            *,
            actor: str,
            action: str,
            resource: str,
            details: dict[str, object] | None = None,
            tenant_id: UUID | None = None,
        ) -> AuditEventResponse:
            self.tenant_ids.append(tenant_id)
            return AuditEventResponse(
                id="audit-policy-review",
                actor=actor,
                action=action,
                resource=resource,
                details={
                    key: str(value)
                    for key, value in (details or {}).items()
                },
                created_at=datetime.now(UTC),
            )

    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = TenantScopedPluginService()
    cast(Any, api).state.admin_resource_service = service
    cast(Any, api).state.runtime_capability_gateway = FakeRuntimeCapabilityGateway()
    test_client = TestClient(api)

    plugins = test_client.get("/api/v1/admin/plugins", headers=headers())
    summary = test_client.get("/api/v1/admin/plugins/policy-summary", headers=headers())
    review = test_client.post("/api/v1/admin/plugins/policy-review", headers=headers())
    manifest = test_client.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert plugins.status_code == 200
    assert summary.status_code == 200
    assert review.status_code == 200
    assert manifest.status_code == 200
    assert service.tenant_ids == [
        OTHER_TENANT_ID,
        OTHER_TENANT_ID,
        OTHER_TENANT_ID,
        OTHER_TENANT_ID,
        OTHER_TENANT_ID,
    ]
    assert [item["id"] for item in plugins.json()] == ["tenant-search"]
    assert [item["id"] for item in summary.json()] == ["tenant-search"]
    assert review.json()["plugin_count"] == 1
    assert review.json()["policy_effect_counts"] == {"inherit": 1}
    capabilities = {item["id"]: item for item in manifest.json()["capabilities"]}
    assert "tenant_search.web" in capabilities
    assert "bootstrap_search.web" not in capabilities


def test_plugin_admin_write_endpoints_scope_writes_to_principal_tenant_and_actor() -> None:
    class OtherTenantAuthService:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            if token != "valid-token":
                raise InvalidCredentials("bad token")
            return AuthenticatedPrincipal(USER_ID, OTHER_TENANT_ID, Role.SUPER_ADMIN)

    class RecordingPluginWriteService(InMemoryAdminResourceService):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str, UUID | None, UUID | None]] = []

        async def upsert_plugin(
            self,
            request: PluginResourceRequest,
            *,
            tenant_id: UUID | None = None,
            actor_id: UUID | None = None,
            source_filename: str | None = None,
            content_sha256: str | None = None,
            package_metadata: PluginPackageMetadata | None = None,
        ) -> PluginResourceResponse:
            self.calls.append(("upsert", request.id, tenant_id, actor_id))
            return await super().upsert_plugin(
                request,
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_filename=source_filename,
                content_sha256=content_sha256,
                package_metadata=package_metadata,
            )

        async def start_plugin(
            self,
            plugin_id: str,
            *,
            tenant_id: UUID | None = None,
            actor_id: UUID | None = None,
        ) -> PluginResourceResponse:
            self.calls.append(("start", plugin_id, tenant_id, actor_id))
            return await super().start_plugin(plugin_id, tenant_id=tenant_id, actor_id=actor_id)

        async def stop_plugin(
            self,
            plugin_id: str,
            *,
            tenant_id: UUID | None = None,
            actor_id: UUID | None = None,
        ) -> PluginResourceResponse:
            self.calls.append(("stop", plugin_id, tenant_id, actor_id))
            return await super().stop_plugin(plugin_id, tenant_id=tenant_id, actor_id=actor_id)

        async def reload_plugin(
            self,
            plugin_id: str,
            *,
            tenant_id: UUID | None = None,
            actor_id: UUID | None = None,
        ) -> PluginResourceResponse:
            self.calls.append(("reload", plugin_id, tenant_id, actor_id))
            return await super().reload_plugin(plugin_id, tenant_id=tenant_id, actor_id=actor_id)

        async def delete_plugin(
            self,
            plugin_id: str,
            *,
            tenant_id: UUID | None = None,
            actor_id: UUID | None = None,
        ) -> None:
            self.calls.append(("delete", plugin_id, tenant_id, actor_id))
            await super().delete_plugin(plugin_id, tenant_id=tenant_id, actor_id=actor_id)

    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = RecordingPluginWriteService()
    cast(Any, api).state.admin_resource_service = service
    test_client = TestClient(api)

    created = test_client.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={"id": "search", "name": "Search Plugin"},
    )
    started = test_client.post("/api/v1/admin/plugins/search/start", headers=headers())
    stopped = test_client.post("/api/v1/admin/plugins/search/stop", headers=headers())
    reloaded = test_client.post("/api/v1/admin/plugins/search/reload", headers=headers())
    deleted = test_client.delete("/api/v1/admin/plugins/search", headers=headers())

    assert created.status_code == 200
    assert started.status_code == 200
    assert stopped.status_code == 200
    assert reloaded.status_code == 200
    assert deleted.status_code == 200
    assert service.calls == [
        ("upsert", "search", OTHER_TENANT_ID, USER_ID),
        ("start", "search", OTHER_TENANT_ID, USER_ID),
        ("stop", "search", OTHER_TENANT_ID, USER_ID),
        ("reload", "search", OTHER_TENANT_ID, USER_ID),
        ("delete", "search", OTHER_TENANT_ID, USER_ID),
    ]


def test_admin_mcp_write_delete_and_audit_scope_to_principal_tenant_and_actor() -> None:
    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = TenantScopedAdminResourceService()
    bootstrap_service = service.scopes[(TENANT_ID, ACTOR_ID)]
    bootstrap_service.mcp_servers["filesystem"] = McpServerResponse(
        id="filesystem",
        name="Bootstrap Filesystem MCP",
        health="configured",
        allowed_tools=["list_directory"],
        transport="stdio",
        command="uvx",
        args=["bootstrap-filesystem"],
        executable_allowlist=["uvx"],
    )
    reloaded: list[UUID] = []

    async def reload_mcp_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api).state.admin_resource_service = service
    cast(Any, api).state.reload_mcp_runtime_config = reload_mcp_runtime_config
    test_client = TestClient(api)

    created = test_client.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={
            "id": "filesystem",
            "name": "Tenant Filesystem MCP",
            "allowed_tools": ["read_file"],
            "transport": "stdio",
            "command": "uvx",
            "args": ["tenant-filesystem"],
            "executable_allowlist": ["uvx"],
            "timeout_seconds": 10,
        },
    )
    listed = test_client.get("/api/v1/admin/mcp", headers=headers())
    upsert_audit = test_client.get(
        "/api/v1/admin/audit?action=mcp.upsert",
        headers=headers(),
    )
    deleted = test_client.delete("/api/v1/admin/mcp/filesystem", headers=headers())
    listed_after_delete = test_client.get("/api/v1/admin/mcp", headers=headers())
    delete_audit = test_client.get(
        "/api/v1/admin/audit?action=mcp.delete",
        headers=headers(),
    )

    assert created.status_code == 200
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["Tenant Filesystem MCP"]
    assert upsert_audit.status_code == 200
    assert upsert_audit.json()[0]["actor"] == str(USER_ID)
    assert deleted.status_code == 200
    assert listed_after_delete.status_code == 200
    assert listed_after_delete.json() == []
    assert delete_audit.status_code == 200
    assert delete_audit.json()[0]["actor"] == str(USER_ID)
    assert bootstrap_service.mcp_servers["filesystem"].name == "Bootstrap Filesystem MCP"
    assert reloaded == [OTHER_TENANT_ID, OTHER_TENANT_ID]
    assert service.calls == [
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("upsert_mcp", "filesystem", OTHER_TENANT_ID, USER_ID),
        ("audit", "mcp.upsert", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("list_mcp", "", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("list_audit", "mcp.upsert", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("delete_mcp", "filesystem", OTHER_TENANT_ID, USER_ID),
        ("audit", "mcp.delete", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("list_mcp", "", OTHER_TENANT_ID, USER_ID),
        ("for_principal", "", OTHER_TENANT_ID, USER_ID),
        ("list_audit", "mcp.delete", OTHER_TENANT_ID, USER_ID),
    ]


def test_mcp_upsert_and_delete_trigger_runtime_reload_callback() -> None:
    api = client()
    reloaded: list[UUID] = []

    async def reload_mcp_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api.app).state.reload_mcp_runtime_config = reload_mcp_runtime_config

    created = api.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={
            "id": "filesystem",
            "name": "Filesystem MCP",
            "allowed_tools": ["read_file"],
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-filesystem"],
            "executable_allowlist": ["uvx"],
            "timeout_seconds": 10,
        },
    )
    deleted = api.delete("/api/v1/admin/mcp/filesystem", headers=headers())

    assert created.status_code == 200
    assert deleted.status_code == 200
    assert reloaded == [TENANT_ID, TENANT_ID]


def test_mcp_reload_callback_failure_does_not_fail_saved_config() -> None:
    api = client()

    async def reload_mcp_runtime_config(tenant_id: UUID) -> None:
        assert tenant_id == TENANT_ID
        raise RuntimeError("reload failed with secret token")

    cast(Any, api.app).state.reload_mcp_runtime_config = reload_mcp_runtime_config

    response = api.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={
            "id": "filesystem",
            "name": "Filesystem MCP",
            "allowed_tools": ["read_file"],
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-filesystem"],
            "executable_allowlist": ["uvx"],
            "timeout_seconds": 10,
        },
    )

    assert response.status_code == 200
    assert "secret token" not in response.text


def test_plugin_upsert_lifecycle_and_delete_trigger_runtime_reload_callback() -> None:
    api = client()
    reloaded: list[UUID] = []

    async def reload_plugin_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api.app).state.reload_plugin_runtime_config = reload_plugin_runtime_config

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "search",
            "name": "Search Plugin",
            "capabilities": [
                {
                    "id": "search.web",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                }
            ],
        },
    )
    uninstall_created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={"id": "docs", "name": "Docs Plugin"},
    )
    disabled = api.post("/api/v1/admin/plugins/search/disable", headers=headers())
    enabled = api.post("/api/v1/admin/plugins/search/enable", headers=headers())
    started = api.post("/api/v1/admin/plugins/search/start", headers=headers())
    stopped = api.post("/api/v1/admin/plugins/search/stop", headers=headers())
    reloaded_response = api.post("/api/v1/admin/plugins/search/reload", headers=headers())
    uninstalled = api.post("/api/v1/admin/plugins/docs/uninstall", headers=headers())
    deleted = api.delete("/api/v1/admin/plugins/search", headers=headers())

    assert created.status_code == 200
    assert uninstall_created.status_code == 200
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["health"] == "disabled"
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["status"] == "stopped"
    assert enabled.json()["health"] == "stopped"
    assert started.status_code == 200
    assert stopped.status_code == 200
    assert reloaded_response.status_code == 200
    assert uninstalled.status_code == 200
    assert uninstalled.json() == {"status": "uninstalled"}
    assert deleted.status_code == 200
    assert reloaded == [
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
        TENANT_ID,
    ]


def plugin_archive(
    manifest: Mapping[str, object],
    *,
    files: Mapping[str, str] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr("README.md", "Plugin package.\n")
        for path, content in (files or {}).items():
            archive.writestr(path, content)
    return buffer.getvalue()


VALID_PLUGIN_SIGNATURE = "A" * 86


def plugin_signature_value(private_key: ed25519.Ed25519PrivateKey, payload: bytes) -> str:
    return base64.urlsafe_b64encode(private_key.sign(payload)).rstrip(b"=").decode("ascii")


def plugin_public_key_value(private_key: ed25519.Ed25519PrivateKey) -> str:
    return (
        base64.urlsafe_b64encode(
            private_key.public_key().public_bytes_raw(),
        )
        .rstrip(b"=")
        .decode("ascii")
    )


def plugin_public_key_sha256(private_key: ed25519.Ed25519PrivateKey) -> str:
    return hashlib.sha256(private_key.public_key().public_bytes_raw()).hexdigest()


def signed_plugin_archive(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    key_id: str = "calendar-prod",
    files: dict[str, str] | None = None,
    package_overrides: Mapping[str, object] | None = None,
    capabilities: list[Mapping[str, object]] | None = None,
) -> bytes:
    package: dict[str, object] = {
        "schema_version": 1,
        "kind": "adapter_package",
        "package_version": "1.2.3",
        "adapter_id": "calendar_python",
        "sdk_api_version": "1.0",
        "signature": {
            "algorithm": "ed25519",
            "key_id": key_id,
        },
        "runtime": "python",
        "entrypoint": "adapter/main.py",
        "isolation": "local_process",
        "install_mode": "scan_only",
    }
    package = {**package, **(package_overrides or {})}
    manifest: dict[str, object] = {
        "id": "calendar",
        "name": "Calendar HTTP",
        "package": package,
    }
    if capabilities is not None:
        manifest["capabilities"] = capabilities
    package_files = files or {"adapter/main.py": "def invoke():\n    return {}\n"}
    unsigned_archive = plugin_archive(
        {
            **manifest,
            "package": {
                **package,
                "signature": {
                    **cast(dict[str, object], package["signature"]),
                    "value": VALID_PLUGIN_SIGNATURE,
                },
            },
        },
        files=package_files,
    )
    signature = plugin_signature_value(
        private_key,
        _plugin_signature_payload(
            PluginArchiveManifest.model_validate(
                {
                    **manifest,
                    "package": {
                        **package,
                        "signature": {
                            **cast(dict[str, object], package["signature"]),
                            "value": VALID_PLUGIN_SIGNATURE,
                        },
                    },
                }
            ),
            unsigned_archive,
        ),
    )
    signed_manifest = {
        **manifest,
        "package": {
            **package,
            "signature": {
                **cast(dict[str, object], package["signature"]),
                "value": signature,
            },
        },
    }
    return plugin_archive(signed_manifest, files=package_files)


def signed_plugin_archive_parts(archive_bytes: bytes) -> tuple[dict[str, object], dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        manifest = json.loads(archive.read("plugin.json").decode())
        files = {
            info.filename: archive.read(info.filename).decode()
            for info in archive.infolist()
            if not info.is_dir() and info.filename not in {"plugin.json", "README.md"}
        }
    return cast(dict[str, object], manifest), files


def test_plugin_signature_payload_excludes_server_controlled_activation_fields() -> None:
    archive_bytes = plugin_archive(
        {
            "id": "calendar",
            "name": "Calendar HTTP",
            "package": {
                "kind": "adapter_package",
                "package_version": "1.2.3",
                "adapter_id": "calendar_python",
                "sdk_api_version": "1.0",
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": "calendar-prod",
                    "value": VALID_PLUGIN_SIGNATURE,
                },
                "approval_state": "approved",
                "approval_reason": "approved by admin",
                "approved_by": str(USER_ID),
                "approved_at": "2026-09-09T00:00:00Z",
                "runtime": "python",
                "entrypoint": "adapter/main.py",
                "isolation": "local_process",
                "install_mode": "scan_only",
            },
        },
        files={"adapter/main.py": "def invoke():\n    return {}\n"},
    )
    manifest = PluginArchiveManifest.model_validate(
        {
            "id": "calendar",
            "name": "Calendar HTTP",
            "package": {
                "kind": "adapter_package",
                "package_version": "1.2.3",
                "adapter_id": "calendar_python",
                "sdk_api_version": "1.0",
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": "calendar-prod",
                    "value": VALID_PLUGIN_SIGNATURE,
                },
                "approval_state": "approved",
                "approval_reason": "approved by admin",
                "approved_by": str(USER_ID),
                "approved_at": "2026-09-09T00:00:00Z",
                "activation_state": "eligible",
                "activation_reason": "approved",
                "signature_trust_expires_at": "2026-10-09T00:00:00Z",
                "signature_verification": "verified",
                "verified_public_key_sha256": "a" * 64,
                "runtime": "python",
                "entrypoint": "adapter/main.py",
                "isolation": "local_process",
                "install_mode": "scan_only",
            },
        },
    )

    payload = json.loads(
        _plugin_signature_payload(
            manifest,
            archive_bytes,
        ).decode(),
    )

    signed_package = payload["manifest"]["package"]
    assert "activation_state" not in signed_package
    assert "activation_reason" not in signed_package
    assert "approval_state" not in signed_package
    assert "approval_reason" not in signed_package
    assert "approved_by" not in signed_package
    assert "approved_at" not in signed_package
    assert "signature_trust_expires_at" not in signed_package
    assert "signature_verification" not in signed_package
    assert "verified_public_key_sha256" not in signed_package
    assert signed_package["signature"] == {
        "algorithm": "ed25519",
        "key_id": "calendar-prod",
    }


def test_plugin_archive_install_scans_manifest_and_triggers_runtime_reload() -> None:
    api = client()
    reloaded: list[UUID] = []

    async def reload_plugin_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api.app).state.reload_plugin_runtime_config = reload_plugin_runtime_config

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": quote("calendar-plugin.zip"),
            "X-Agent-Hub-Plugin-Filename-Encoding": "percent",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "version": "1.0.0",
                "endpoint_url": "https://plugins.example/invoke",
                "domain_allowlist": ["plugins.example"],
                "capabilities": [
                    {
                        "id": "calendar.create_event",
                        "adapter": "http_json",
                        "permission_class": "calendar.write",
                        "sandbox_profile": "remote_connector",
                    }
                ],
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "calendar-plugin.zip"
    assert body["content_sha256"]
    assert body["plugin"]["id"] == "calendar"
    assert body["plugin"]["source_filename"] == "calendar-plugin.zip"
    assert body["plugin"]["content_sha256"] == body["content_sha256"]
    assert body["plugin"]["version"] == "1.0.0"
    assert body["plugin"]["capabilities"][0]["id"] == "calendar.create_event"
    assert reloaded == [TENANT_ID]
    listed = api.get("/api/v1/admin/plugins", headers=headers()).json()[0]
    assert listed["id"] == "calendar"
    assert listed["source_filename"] == "calendar-plugin.zip"
    assert listed["content_sha256"] == body["content_sha256"]


def test_plugin_archive_install_rejects_archives_without_plugin_manifest() -> None:
    api = client()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.md", "No manifest.\n")

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "broken.zip",
        },
        content=buffer.getvalue(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "plugin archive is missing plugin.json"


def test_plugin_archive_install_persists_scan_only_package_metadata() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "version": "1.0.0",
                "package": {
                    "schema_version": 1,
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
                "endpoint_url": "https://plugins.example/invoke",
                "domain_allowlist": ["plugins.example"],
                "capabilities": [
                    {
                        "id": "calendar.create_event",
                        "adapter": "http_json",
                        "permission_class": "calendar.write",
                        "sandbox_profile": "remote_connector",
                    }
                ],
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["plugin"]["package_metadata"] == {
        "schema_version": 1,
        "kind": "adapter_package",
        "package_version": "1.2.3",
        "adapter_id": "calendar_python",
        "sdk_api_version": "1.0",
        "signature": {
            "algorithm": "ed25519",
            "key_id": "calendar-prod",
            "value": VALID_PLUGIN_SIGNATURE,
        },
        "signature_verification": "untrusted_key",
        "verified_public_key_sha256": None,
        "signature_trust_expires_at": None,
        "approval_state": "pending",
        "approval_reason": "adapter package requires plugin approval before activation",
        "approved_by": None,
        "approved_at": None,
        "activation_state": "blocked_untrusted_key",
        "activation_reason": "package signature key is not trusted for this tenant",
        "runtime": "python",
        "entrypoint": "adapter/main.py",
        "isolation": "local_process",
        "install_mode": "scan_only",
        "dependencies": [],
        "artifact": None,
    }
    assert api.get("/api/v1/admin/plugins", headers=headers()).json()[0]["package_metadata"] == body[
        "plugin"
    ]["package_metadata"]


def test_plugin_archive_install_persists_empty_package_dependencies() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "version": "1.0.0",
                "package": {
                    "schema_version": 1,
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                    "dependencies": [],
                },
                "endpoint_url": "https://plugins.example/invoke",
                "domain_allowlist": ["plugins.example"],
                "capabilities": [
                    {
                        "id": "calendar.create_event",
                        "adapter": "http_json",
                        "permission_class": "calendar.write",
                        "sandbox_profile": "remote_connector",
                    }
                ],
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 200
    assert response.json()["plugin"]["package_metadata"]["dependencies"] == []


def test_plugin_archive_install_rejects_package_dependencies() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "version": "1.0.0",
                "package": {
                    "schema_version": 1,
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                    "dependencies": [
                        {
                            "kind": "python",
                            "source": "pypi",
                            "name": "requests",
                            "version": "2.31.0",
                        }
                    ],
                },
                "endpoint_url": "https://plugins.example/invoke",
                "domain_allowlist": ["plugins.example"],
                "capabilities": [
                    {
                        "id": "calendar.create_event",
                        "adapter": "http_json",
                        "permission_class": "calendar.write",
                        "sandbox_profile": "remote_connector",
                    }
                ],
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == (
        "plugin package dependencies are not supported by this runtime"
    )


def test_plugin_archive_install_rejects_malformed_package_dependencies_with_stable_reason() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "version": "1.0.0",
                "package": {
                    "schema_version": 1,
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                    "dependencies": [{"kind": "python", "source": "pypi", "name": "requests"}],
                },
                "endpoint_url": "https://plugins.example/invoke",
                "domain_allowlist": ["plugins.example"],
                "capabilities": [
                    {
                        "id": "calendar.create_event",
                        "adapter": "http_json",
                        "permission_class": "calendar.write",
                        "sandbox_profile": "remote_connector",
                    }
                ],
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == (
        "plugin package dependencies are not supported by this runtime"
    )


def test_plugin_archive_install_stores_verified_adapter_package_artifact(
    tmp_path: Path,
) -> None:
    settings = Settings.model_construct(plugin_package_store_dir=tmp_path)
    api = client_with_settings(settings)
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    archive_bytes = signed_plugin_archive(
        private_key,
        files={"adapter/main.py": "def invoke():\n    return {'ok': True}\n"},
    )
    content_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=archive_bytes,
    )

    assert response.status_code == 200
    metadata = response.json()["plugin"]["package_metadata"]
    artifact = metadata["artifact"]
    assert artifact["storage_key"] == f"{TENANT_ID}/calendar/{content_sha256}"
    assert artifact["content_sha256"] == content_sha256
    assert artifact["file_count"] == 2
    assert artifact["total_size_bytes"] == len(
        "Plugin package.\n" + "def invoke():\n    return {'ok': True}\n"
    )
    assert artifact["quarantine_state"] == "stored"
    assert datetime.fromisoformat(artifact["stored_at"]).tzinfo is not None
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    assert (artifact_root / "adapter" / "main.py").read_text() == (
        "def invoke():\n    return {'ok': True}\n"
    )
    assert (artifact_root / "README.md").read_text() == "Plugin package.\n"
    assert not (artifact_root / "plugin.json").exists()


def test_plugin_archive_install_does_not_store_untrusted_adapter_package_artifact(
    tmp_path: Path,
) -> None:
    settings = Settings.model_construct(plugin_package_store_dir=tmp_path)
    api = client_with_settings(settings)

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "schema_version": 1,
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 200
    assert response.json()["plugin"]["package_metadata"]["artifact"] is None
    assert not tmp_path.exists() or not any(tmp_path.rglob("*"))


def test_plugin_delete_removes_verified_adapter_package_artifact(
    tmp_path: Path,
) -> None:
    settings = Settings.model_construct(plugin_package_store_dir=tmp_path)
    api = client_with_settings(settings)
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    archive_bytes = signed_plugin_archive(private_key)
    content_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=archive_bytes,
    )
    artifact_root = tmp_path / str(TENANT_ID) / "calendar" / content_sha256
    assert install.status_code == 200
    assert artifact_root.exists()

    delete_response = api.delete("/api/v1/admin/plugins/calendar", headers=headers())

    assert delete_response.status_code == 200
    assert not artifact_root.exists()


def test_plugin_archive_install_rejects_unsafe_package_entrypoint() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "runtime": "python",
                    "entrypoint": "../adapter.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            }
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "plugin package entrypoint is unsafe"


def test_plugin_archive_install_rejects_missing_package_entrypoint_file() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            }
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package entrypoint is missing"
    )


def test_plugin_archive_install_rejects_manifest_only_package_with_runtime() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "manifest_only",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "none",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "manifest-only plugin package cannot declare runtime execution"


def test_plugin_archive_install_rejects_manifest_only_package_with_sdk_contract() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "manifest_only",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "install_mode": "scan_only",
                },
            },
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "manifest-only plugin package cannot declare runtime execution"


def test_plugin_archive_install_rejects_manifest_only_package_with_version_or_signature() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "manifest_only",
                    "package_version": "1.2.3",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "install_mode": "scan_only",
                },
            },
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "manifest-only plugin package cannot declare runtime execution"


def test_plugin_archive_install_rejects_manifest_only_package_with_explicit_execution_defaults() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "manifest_only",
                    "package_version": None,
                    "adapter_id": None,
                    "sdk_api_version": None,
                    "signature": None,
                    "runtime": "none",
                    "entrypoint": None,
                    "isolation": "none",
                    "install_mode": "scan_only",
                },
            },
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "manifest-only plugin package cannot declare runtime execution"


def test_plugin_archive_install_rejects_adapter_package_without_sdk_contract() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "adapter plugin package must declare sdk contract"


def test_plugin_archive_install_rejects_invalid_package_sdk_contract() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    reason = response.json()["error"]["details"]["reason"]
    assert "package.sdk_api_version" in reason


def test_plugin_archive_install_rejects_adapter_package_without_package_version() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == "adapter plugin package must declare package version"


def test_plugin_archive_install_rejects_invalid_package_version_prerelease_zero() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3-01",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert "package.package_version" in response.json()["error"]["details"]["reason"]


def test_plugin_archive_install_rejects_forged_signature_verification() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "signature_verification": "verified",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package signature verification is server-controlled"
    )


def test_plugin_archive_install_rejects_forged_verified_public_key() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "verified_public_key_sha256": "a" * 64,
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package verified public key is server-controlled"
    )


def test_plugin_archive_install_rejects_forged_signature_trust_expiry() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "signature_trust_expires_at": (
                        datetime.now(UTC) + timedelta(days=1)
                    ).isoformat(),
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package signature trust expiry is server-controlled"
    )


def test_plugin_archive_install_rejects_forged_package_artifact() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "artifact": {
                        "storage_key": f"{TENANT_ID}/calendar/{'a' * 64}",
                        "content_sha256": "a" * 64,
                        "file_count": 1,
                        "total_size_bytes": 10,
                        "stored_at": "2026-09-09T04:00:00Z",
                        "quarantine_state": "stored",
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package artifact is server-controlled"
    )


def test_plugin_archive_install_rejects_invalid_package_signature() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "rsa-pss-sha256",
                        "key_id": "calendar-prod",
                        "value": "bad-signature",
                    },
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    reason = response.json()["error"]["details"]["reason"]
    assert "package.signature.algorithm" in reason
    assert "package.signature.value" in reason


def test_plugin_signing_keys_are_tenant_scoped() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = plugin_public_key_value(private_key)
    not_before = datetime.now(UTC) - timedelta(hours=1)
    not_after = datetime.now(UTC) + timedelta(days=30)

    response = api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": public_key,
            "not_before": not_before.isoformat(),
            "not_after": not_after.isoformat(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "key_id": "calendar-prod",
        "algorithm": "ed25519",
        "public_key": public_key,
        "trusted": True,
        "not_before": body["not_before"],
        "not_after": body["not_after"],
    }
    assert datetime.fromisoformat(body["not_before"]) == not_before
    assert datetime.fromisoformat(body["not_after"]) == not_after

    listed = api.get("/api/v1/admin/plugins/signing-keys", headers=headers())
    assert listed.status_code == 200
    assert listed.json() == [body]

    other_app = create_app(
        auth_service=OtherTenantAuthService(),
        rate_limiter=object(),
    )
    other_app.state.admin_resource_service = cast(Any, api.app).state.admin_resource_service
    other_tenant = TestClient(other_app).get(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
    )
    assert other_tenant.status_code == 200
    assert other_tenant.json() == []


def test_plugin_archive_install_records_signature_trust_expiry() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    not_after = datetime.now(UTC) + timedelta(days=30)
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
            "not_after": not_after.isoformat(),
        },
    )

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )

    assert response.status_code == 200
    metadata = response.json()["plugin"]["package_metadata"]
    assert metadata["signature_verification"] == "verified"
    assert datetime.fromisoformat(metadata["signature_trust_expires_at"]) == not_after


def test_plugin_signing_key_upsert_and_delete_trigger_runtime_reload_callback() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    reloaded: list[UUID] = []

    async def reload_plugin_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api.app).state.reload_plugin_runtime_config = reload_plugin_runtime_config

    upsert = api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    deleted = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    assert upsert.status_code == 200
    assert deleted.status_code == 200
    assert reloaded == [TENANT_ID, TENANT_ID]


def test_plugin_signing_key_mutations_require_plugin_approval_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_permissions: list[str] = []

    class RecordingAuthorizer:
        def require(
            self,
            principal: AuthenticatedPrincipal,
            permission: str,
        ) -> AuthenticatedPrincipal:
            required_permissions.append(permission)
            return principal

    monkeypatch.setattr(admin_router, "Authorizer", RecordingAuthorizer)
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()

    upsert = api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    deleted = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    assert upsert.status_code == 200
    assert deleted.status_code == 200
    assert required_permissions == ["plugin:approve", "plugin:approve"]


@pytest.mark.parametrize(
    ("key_id", "window"),
    [
        ("calendar-future", {"not_before": (datetime.now(UTC) + timedelta(days=1)).isoformat()}),
        ("calendar-expired", {"not_after": (datetime.now(UTC) - timedelta(days=1)).isoformat()}),
    ],
)
def test_plugin_archive_install_does_not_verify_inactive_signing_key(
    key_id: str,
    window: dict[str, str],
) -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": key_id,
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
            **window,
        },
    )

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key, key_id=key_id),
    )

    assert response.status_code == 200
    metadata = response.json()["plugin"]["package_metadata"]
    assert metadata["signature_verification"] == "untrusted_key"
    assert metadata["activation_state"] == "blocked_untrusted_key"


def test_plugin_signing_key_rejects_invalid_activation_window() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    not_before = datetime.now(UTC) + timedelta(days=1)
    not_after = datetime.now(UTC) - timedelta(days=1)

    response = client().post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
            "not_before": not_before.isoformat(),
            "not_after": not_after.isoformat(),
        },
    )

    assert response.status_code == 422


def test_plugin_signing_key_delete_revokes_future_package_trust() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = plugin_public_key_value(private_key)
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": public_key,
        },
    )

    response = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "deleted"}
    assert api.get("/api/v1/admin/plugins/signing-keys", headers=headers()).json() == []
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert install.status_code == 200
    assert install.json()["plugin"]["package_metadata"]["signature_verification"] == "untrusted_key"


def test_plugin_listing_downgrades_verified_package_after_signing_key_delete() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert install.status_code == 200
    assert install.json()["plugin"]["package_metadata"]["signature_verification"] == "verified"

    delete_response = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    assert delete_response.status_code == 200
    listed = api.get("/api/v1/admin/plugins", headers=headers())
    metadata = listed.json()[0]["package_metadata"]
    assert metadata["signature_verification"] == "untrusted_key"
    assert metadata["activation_state"] == "blocked_untrusted_key"
    assert metadata["activation_reason"] == "package signature key is not trusted for this tenant"


def test_plugin_listing_downgrades_verified_package_after_signing_key_expiry() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = plugin_public_key_value(private_key)
    active_window = {
        "not_before": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "not_after": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": public_key,
            **active_window,
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert install.status_code == 200
    assert install.json()["plugin"]["package_metadata"]["signature_verification"] == "verified"

    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": public_key,
            "not_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
    )

    listed = api.get("/api/v1/admin/plugins", headers=headers())
    metadata = listed.json()[0]["package_metadata"]
    assert metadata["signature_verification"] == "untrusted_key"
    assert metadata["activation_state"] == "blocked_untrusted_key"
    assert metadata["activation_reason"] == "package signature key is not trusted for this tenant"


def test_plugin_listing_downgrades_verified_package_when_signing_key_expires_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        current = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)

        @classmethod
        def now(cls, tz: object = None) -> Self:
            if tz is UTC:
                return cls.fromtimestamp(cls.current.timestamp(), UTC)
            return cls.fromtimestamp(cls.current.timestamp())

    monkeypatch.setattr(admin_router, "datetime", FrozenDateTime)
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = plugin_public_key_value(private_key)
    not_after = FrozenDateTime.current + timedelta(seconds=10)
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": public_key,
            "not_after": not_after.isoformat(),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert install.status_code == 200
    metadata = install.json()["plugin"]["package_metadata"]
    assert metadata["signature_verification"] == "verified"
    assert datetime.fromisoformat(metadata["signature_trust_expires_at"]) == not_after

    FrozenDateTime.current = not_after + timedelta(seconds=1)

    listed = api.get("/api/v1/admin/plugins", headers=headers())
    metadata = listed.json()[0]["package_metadata"]
    assert metadata["signature_verification"] == "untrusted_key"
    assert metadata["signature_trust_expires_at"] is None
    assert metadata["activation_state"] == "blocked_untrusted_key"
    assert metadata["activation_reason"] == "package signature key is not trusted for this tenant"


def test_scan_only_adapter_package_lifecycle_fails_closed() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert install.status_code == 200
    assert (
        install.json()["plugin"]["package_metadata"]["activation_state"]
        == "blocked_pending_approval"
    )
    assert install.json()["plugin"]["package_metadata"]["activation_reason"] == (
        "adapter package requires plugin approval before activation"
    )

    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())
    reloaded = api.post("/api/v1/admin/plugins/calendar/reload", headers=headers())
    disabled = api.post("/api/v1/admin/plugins/calendar/disable", headers=headers())
    enabled = api.post("/api/v1/admin/plugins/calendar/enable", headers=headers())

    assert started.status_code == 409
    assert started.json()["error"]["code"] == "plugin_package_not_eligible"
    assert reloaded.status_code == 409
    assert reloaded.json()["error"]["code"] == "plugin_package_not_eligible"
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert enabled.status_code == 409
    assert enabled.json()["error"]["code"] == "plugin_package_not_eligible"


def test_scan_only_adapter_package_install_does_not_inherit_running_status() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={"id": "calendar", "name": "Calendar HTTP"},
    )
    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())

    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )

    assert created.status_code == 200
    assert started.status_code == 200
    assert install.status_code == 200
    assert (
        install.json()["plugin"]["package_metadata"]["activation_state"]
        == "blocked_pending_approval"
    )
    assert install.json()["plugin"]["status"] == "stopped"
    assert install.json()["plugin"]["health"] == "stopped"


def test_plugin_package_approval_updates_metadata_and_keeps_scan_only_closed() -> None:
    api = client()
    reloaded: list[UUID] = []

    async def reload_plugin_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api.app).state.reload_plugin_runtime_config = reload_plugin_runtime_config
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )

    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={"reason": "reviewed by security"},
    )
    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())
    rejected = api.post(
        "/api/v1/admin/plugins/calendar/package/reject",
        headers=headers(),
        json={"reason": "requires isolation review"},
    )

    assert install.status_code == 200
    assert approved.status_code == 200
    approved_metadata = approved.json()["package_metadata"]
    assert approved_metadata["approval_state"] == "approved"
    assert approved_metadata["approval_reason"] == "reviewed by security"
    assert approved_metadata["approved_by"] == str(USER_ID)
    assert approved_metadata["approved_at"] is not None
    assert approved_metadata["activation_state"] == "verified_scan_only"
    assert approved_metadata["activation_reason"] == (
        "package signature is verified, but install_mode=scan_only prevents activation"
    )
    assert started.status_code == 409
    assert started.json()["error"]["code"] == "plugin_package_not_eligible"
    assert rejected.status_code == 200
    rejected_metadata = rejected.json()["package_metadata"]
    assert rejected_metadata["approval_state"] == "rejected"
    assert rejected_metadata["approval_reason"] == "requires isolation review"
    assert rejected_metadata["approved_by"] == str(USER_ID)
    assert rejected_metadata["approved_at"] is not None
    assert rejected_metadata["activation_state"] == "blocked_rejected_approval"
    assert rejected_metadata["activation_reason"] == "requires isolation review"
    assert reloaded == [TENANT_ID, TENANT_ID, TENANT_ID, TENANT_ID]


def test_runtime_registered_adapter_package_requires_known_adapter() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == (
        "runtime-registered adapter package requires a registered adapter descriptor"
    )


def test_runtime_registered_adapter_package_requires_capabilities_to_use_package_adapter() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "other_adapter",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == (
        "runtime-registered adapter packages must route capabilities through package adapter_id"
    )


@pytest.mark.parametrize(
    ("package_overrides", "capabilities", "expected_reason"),
    (
        (
            {
                "install_mode": "runtime_registered",
                "sdk_api_version": "9.9",
                "isolation": "local_process",
            },
            [
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
            "plugin package SDK API version is not supported",
        ),
        (
            {
                "install_mode": "runtime_registered",
                "runtime": "node",
                "isolation": "local_process",
            },
            [
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
            "runtime-registered plugin package runtime is not supported",
        ),
        (
            {"install_mode": "runtime_registered", "isolation": "in_process"},
            [
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
            "runtime-registered plugin package isolation is not supported",
        ),
        (
            {"install_mode": "runtime_registered", "isolation": "local_process"},
            [],
            "runtime-registered adapter packages must declare at least one capability",
        ),
        (
            {"install_mode": "runtime_registered", "isolation": "local_process"},
            [
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "remote_connector",
                }
            ],
            "runtime-registered adapter package capabilities must use package isolation",
        ),
    ),
)
def test_runtime_registered_adapter_package_rejects_unsupported_activation_contract(
    package_overrides: Mapping[str, object],
    capabilities: list[Mapping[str, object]],
    expected_reason: str,
) -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides=package_overrides,
            capabilities=capabilities,
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert response.json()["error"]["details"]["reason"] == expected_reason


def test_runtime_registered_adapter_package_can_be_approved_and_started() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())

    assert install.status_code == 200
    assert (
        install.json()["plugin"]["package_metadata"]["activation_state"]
        == "blocked_pending_approval"
    )
    assert approved.status_code == 200
    assert approved.json()["package_metadata"]["activation_state"] == "eligible"
    assert approved.json()["package_metadata"]["activation_reason"] == (
        "package signature, approval, SDK, adapter, and isolation policy allow execution"
    )
    assert started.status_code == 200
    assert started.json()["status"] == "running"


def test_runtime_registered_adapter_package_approval_rechecks_registered_adapter() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    cast(Any, api.app).state.plugin_service = object()

    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )

    assert install.status_code == 200
    assert approved.status_code == 409
    assert approved.json()["error"]["code"] == "plugin_package_not_eligible"
    assert approved.json()["error"]["message"] == (
        "runtime-registered adapter package requires a registered adapter descriptor"
    )


def test_runtime_registered_adapter_package_approval_rechecks_effective_trust() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    deleted_key = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )

    assert install.status_code == 200
    assert deleted_key.status_code == 200
    assert approved.status_code == 409
    assert approved.json()["error"]["code"] == "plugin_package_not_eligible"
    assert approved.json()["error"]["message"] == "package signature key is not trusted for this tenant"


def test_runtime_registered_adapter_package_start_rechecks_registered_adapter() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    cast(Any, api.app).state.plugin_service = object()

    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())

    assert install.status_code == 200
    assert approved.status_code == 200
    assert started.status_code == 409
    assert started.json()["error"]["code"] == "plugin_package_not_eligible"
    assert started.json()["error"]["message"] == (
        "runtime-registered adapter package requires a registered adapter descriptor"
    )


def test_runtime_registered_adapter_package_start_rechecks_effective_trust() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    deleted_key = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())

    assert install.status_code == 200
    assert approved.status_code == 200
    assert approved.json()["package_metadata"]["activation_state"] == "eligible"
    assert deleted_key.status_code == 200
    assert started.status_code == 409
    assert started.json()["error"]["code"] == "plugin_package_not_eligible"
    assert started.json()["error"]["message"] == "package signature key is not trusted for this tenant"


@pytest.mark.parametrize("endpoint", ("enable", "reload"))
def test_runtime_registered_adapter_package_lifecycle_rechecks_registered_adapter(
    endpoint: str,
) -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    cast(Any, api.app).state.plugin_service = object()

    response = api.post(f"/api/v1/admin/plugins/calendar/{endpoint}", headers=headers())

    assert install.status_code == 200
    assert approved.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plugin_package_not_eligible"
    assert response.json()["error"]["message"] == (
        "runtime-registered adapter package requires a registered adapter descriptor"
    )


@pytest.mark.parametrize("endpoint", ("enable", "reload"))
def test_runtime_registered_adapter_package_lifecycle_rechecks_effective_trust(
    endpoint: str,
) -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    deleted_key = api.delete(
        "/api/v1/admin/plugins/signing-keys/calendar-prod",
        headers=headers(),
    )

    response = api.post(f"/api/v1/admin/plugins/calendar/{endpoint}", headers=headers())

    assert install.status_code == 200
    assert approved.status_code == 200
    assert approved.json()["package_metadata"]["activation_state"] == "eligible"
    assert deleted_key.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plugin_package_not_eligible"
    assert response.json()["error"]["message"] == (
        "package signature key is not trusted for this tenant"
    )


def test_capability_manifest_rechecks_runtime_registered_adapter_descriptor() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.runtime_capability_gateway = FakeRuntimeCapabilityGateway()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    cast(Any, api.app).state.plugin_service = object()

    manifest = api.get("/api/v1/admin/capabilities/manifest", headers=headers())
    capabilities = {item["id"]: item for item in manifest.json()["capabilities"]}

    assert install.status_code == 200
    assert approved.status_code == 200
    assert approved.json()["package_metadata"]["activation_state"] == "eligible"
    assert manifest.status_code == 200
    assert capabilities["calendar.create_event"]["available"] is False
    assert capabilities["calendar.create_event"]["availability_reason"] == (
        "plugin_package_not_eligible"
    )


def test_plugin_listing_rechecks_runtime_registered_adapter_descriptor() -> None:
    class CalendarPluginService:
        def adapter_descriptors(self) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "id": "calendar_python",
                    "name": "Calendar Python",
                    "description": None,
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {"type": "string", "enum": ("local_process",)}
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    api = client()
    cast(Any, api.app).state.plugin_service = CalendarPluginService()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(
            private_key,
            package_overrides={"install_mode": "runtime_registered", "isolation": "local_process"},
            capabilities=[
                {
                    "id": "calendar.create_event",
                    "adapter": "calendar_python",
                    "sandbox_profile": "local_process",
                }
            ],
        ),
    )
    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={},
    )
    cast(Any, api.app).state.plugin_service = object()

    listed = api.get("/api/v1/admin/plugins", headers=headers())
    metadata = listed.json()[0]["package_metadata"]

    assert install.status_code == 200
    assert approved.status_code == 200
    assert approved.json()["package_metadata"]["activation_state"] == "eligible"
    assert metadata["activation_state"] == "blocked_unsupported_runtime"
    assert metadata["activation_reason"] == (
        "runtime-registered adapter package requires a registered adapter descriptor"
    )


def test_plugin_package_approval_mutations_require_plugin_approval_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_permissions: list[str] = []

    class RecordingAuthorizer:
        def require(
            self,
            principal: AuthenticatedPrincipal,
            permission: str,
        ) -> AuthenticatedPrincipal:
            required_permissions.append(permission)
            return principal

    monkeypatch.setattr(admin_router, "Authorizer", RecordingAuthorizer)
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )

    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={"reason": "reviewed"},
    )
    rejected = api.post(
        "/api/v1/admin/plugins/calendar/package/reject",
        headers=headers(),
        json={"reason": "rejected"},
    )

    assert approved.status_code == 200
    assert rejected.status_code == 200
    assert required_permissions[-2:] == ["plugin:approve", "plugin:approve"]


def test_plugin_package_approval_mutations_forbid_non_approver() -> None:
    class OperatorAuthService:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            if token != "valid-token":
                raise InvalidCredentials("bad token")
            return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.OPERATOR)

    app = create_app(auth_service=OperatorAuthService(), rate_limiter=object())
    app.state.admin_resource_service = InMemoryAdminResourceService()
    api = TestClient(app)

    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={"reason": "reviewed"},
    )
    rejected = api.post(
        "/api/v1/admin/plugins/calendar/package/reject",
        headers=headers(),
        json={"reason": "rejected"},
    )

    assert approved.status_code == 403
    assert approved.json()["error"]["code"] == "permission_denied"
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "permission_denied"


def test_plugin_package_approval_returns_not_found_for_unknown_plugin() -> None:
    api = client()

    approved = api.post(
        "/api/v1/admin/plugins/missing/package/approve",
        headers=headers(),
        json={"reason": "reviewed"},
    )
    rejected = api.post(
        "/api/v1/admin/plugins/missing/package/reject",
        headers=headers(),
        json={"reason": "rejected"},
    )

    assert approved.status_code == 404
    assert approved.json()["error"]["code"] == "not_found"
    assert rejected.status_code == 404
    assert rejected.json()["error"]["code"] == "not_found"


def test_plugin_package_approval_rejects_manifest_only_plugin() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={"id": "calendar", "name": "Calendar HTTP"},
    )

    approved = api.post(
        "/api/v1/admin/plugins/calendar/package/approve",
        headers=headers(),
        json={"reason": "reviewed"},
    )
    rejected = api.post(
        "/api/v1/admin/plugins/calendar/package/reject",
        headers=headers(),
        json={"reason": "rejected"},
    )

    assert created.status_code == 200
    assert approved.status_code == 409
    assert approved.json()["error"]["code"] == "plugin_package_not_approvable"
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "plugin_package_not_approvable"


@pytest.mark.asyncio
async def test_persistent_plugin_package_approval_is_tenant_scoped_and_audited() -> None:
    class StoredPersistentPluginService(PersistentAdminResourceService):
        def __init__(
            self,
            payloads: dict[tuple[UUID, str, str], dict[str, object]] | None = None,
        ) -> None:
            super().__init__(
                config_service=cast(Any, object()),
                secret_service=cast(Any, object()),
                tenant_id=TENANT_ID,
                actor_id=USER_ID,
            )
            self._session_factory = cast(Any, object())
            self.payloads = payloads if payloads is not None else {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return self.payloads.get((target_tenant_id, kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            self.payloads[(target_tenant_id, kind, resource_id)] = payload
            return True

        async def _list_admin_payloads(
            self,
            kind: str,
            *,
            tenant_id: UUID | None = None,
        ) -> list[dict[str, object]] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return [
                payload
                for (payload_tenant_id, payload_kind, _resource_id), payload in self.payloads.items()
                if payload_tenant_id == target_tenant_id and payload_kind == kind
            ]

    service = StoredPersistentPluginService()
    package = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": VALID_PLUGIN_SIGNATURE,
            },
            "signature_verification": "verified",
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "scan_only",
        }
    )
    await service.upsert_plugin(
        PluginResourceRequest(id="calendar", name="Calendar"),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
        package_metadata=package,
    )

    with pytest.raises(KeyError):
        await service.approve_plugin_package(
            "calendar",
            admin_router.PluginPackageApprovalRequest(reason="wrong tenant"),
            tenant_id=OTHER_TENANT_ID,
            actor_id=USER_ID,
        )

    approved = await service.approve_plugin_package(
        "calendar",
        admin_router.PluginPackageApprovalRequest(reason="reviewed by security"),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
    )
    rejected = await service.reject_plugin_package(
        "calendar",
        admin_router.PluginPackageApprovalRequest(reason="requires isolation review"),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
    )
    approve_audits = await service.list_audit_events("plugin.package.approved")
    reject_audits = await service.list_audit_events("plugin.package.rejected")
    other_tenant_audits = [
        payload
        for (tenant_id, kind, _resource_id), payload in service.payloads.items()
        if tenant_id == OTHER_TENANT_ID and kind == "audit"
    ]

    assert approved.package_metadata is not None
    assert approved.package_metadata.approval_state == "approved"
    assert approved.package_metadata.activation_state == "verified_scan_only"
    assert rejected.package_metadata is not None
    assert rejected.package_metadata.approval_state == "rejected"
    assert len(approve_audits) == 1
    assert approve_audits[0].actor == str(USER_ID)
    assert approve_audits[0].resource == "plugin:calendar"
    assert approve_audits[0].details == {
        "id": "calendar",
        "approval_state": "approved",
        "activation_state": "verified_scan_only",
        "approval_reason": "reviewed by security",
    }
    assert len(reject_audits) == 1
    assert reject_audits[0].details["approval_reason"] == "requires isolation review"
    assert other_tenant_audits == []


def test_plugin_listing_downgrades_verified_package_after_signing_key_rotation() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    rotated_private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    install = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )
    assert install.status_code == 200
    assert install.json()["plugin"]["package_metadata"]["signature_verification"] == "verified"

    rotate_key = api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(rotated_private_key),
        },
    )

    assert rotate_key.status_code == 200
    listed = api.get("/api/v1/admin/plugins", headers=headers())
    metadata = listed.json()[0]["package_metadata"]
    assert metadata["signature_verification"] == "untrusted_key"
    assert metadata["activation_state"] == "blocked_untrusted_key"
    assert metadata["activation_reason"] == "package signature key is not trusted for this tenant"


def test_plugin_signing_key_delete_missing_key_returns_not_found() -> None:
    response = client().delete(
        "/api/v1/admin/plugins/signing-keys/missing-key",
        headers=headers(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_plugin_archive_install_verifies_trusted_ed25519_signature() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )

    assert response.status_code == 200
    metadata = response.json()["plugin"]["package_metadata"]
    assert metadata["signature_verification"] == "verified"
    assert metadata["activation_state"] == "blocked_pending_approval"
    assert metadata["activation_reason"] == (
        "adapter package requires plugin approval before activation"
    )


def test_plugin_archive_install_records_untrusted_signature_key() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(private_key),
    )

    assert response.status_code == 200
    assert (
        response.json()["plugin"]["package_metadata"]["signature_verification"]
        == "untrusted_key"
    )
    assert response.json()["plugin"]["package_metadata"]["activation_state"] == "blocked_untrusted_key"
    assert response.json()["plugin"]["package_metadata"]["activation_reason"] == (
        "package signature key is not trusted for this tenant"
    )


def test_plugin_archive_install_rejects_forged_activation_state() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    "signature_verification": "verified",
                    "activation_state": "eligible",
                    "activation_reason": "trusted by manifest",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package activation state is server-controlled"
    )


@pytest.mark.parametrize(
    ("approval_field", "approval_value"),
    [
        ("approval_state", "approved"),
        ("approval_reason", "trusted by manifest"),
        ("approved_by", str(USER_ID)),
        ("approved_at", "2026-09-09T00:00:00Z"),
    ],
)
def test_plugin_archive_install_rejects_forged_package_approval_state(
    approval_field: str,
    approval_value: str,
) -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": "calendar-prod",
                        "value": VALID_PLUGIN_SIGNATURE,
                    },
                    approval_field: approval_value,
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package approval state is server-controlled"
    )


async def test_persistent_plugin_signing_keys_and_verification_status_survive_reload() -> None:
    class StoredPersistentPluginService(PersistentAdminResourceService):
        def __init__(
            self,
            payloads: dict[tuple[UUID, str, str], dict[str, object]] | None = None,
        ) -> None:
            super().__init__(
                config_service=cast(Any, object()),
                secret_service=cast(Any, object()),
                tenant_id=TENANT_ID,
                actor_id=USER_ID,
            )
            self._session_factory = cast(Any, object())
            self.payloads = payloads if payloads is not None else {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return self.payloads.get((target_tenant_id, kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            self.payloads[(target_tenant_id, kind, resource_id)] = payload
            return True

        async def _list_admin_payloads(
            self,
            kind: str,
            *,
            tenant_id: UUID | None = None,
        ) -> list[dict[str, object]] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return [
                payload
                for (payload_tenant_id, payload_kind, _resource_id), payload in self.payloads.items()
                if payload_tenant_id == target_tenant_id and payload_kind == kind
            ]

    private_key = ed25519.Ed25519PrivateKey.generate()
    writer = StoredPersistentPluginService()
    await writer.upsert_plugin_signing_key(
        PluginSigningKeyRequest(
            key_id="calendar-prod",
            algorithm="ed25519",
            public_key=plugin_public_key_value(private_key),
        ),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
    )
    signed_package = PluginPackageMetadata.model_validate(
        {
            "kind": "adapter_package",
            "package_version": "1.2.3",
            "adapter_id": "calendar_python",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": "calendar-prod",
                "value": VALID_PLUGIN_SIGNATURE,
            },
            "signature_verification": "verified",
            "verified_public_key_sha256": plugin_public_key_sha256(private_key),
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "scan_only",
        }
    )
    untrusted_package = signed_package.model_copy(
        update={"signature_verification": "untrusted_key"}
    )
    await writer.upsert_plugin(
        PluginResourceRequest(id="calendar", name="Calendar"),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
        package_metadata=signed_package,
    )
    await writer.upsert_plugin(
        PluginResourceRequest(id="search", name="Search"),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
        package_metadata=untrusted_package,
    )

    reader = StoredPersistentPluginService(writer.payloads)
    signing_keys = await reader.list_plugin_signing_keys(tenant_id=TENANT_ID)
    plugins = {plugin.id: plugin for plugin in await reader.list_plugins(tenant_id=TENANT_ID)}

    assert [key.key_id for key in signing_keys] == ["calendar-prod"]
    assert plugins["calendar"].package_metadata is not None
    assert plugins["calendar"].package_metadata.signature_verification == "verified"
    assert plugins["search"].package_metadata is not None
    assert plugins["search"].package_metadata.signature_verification == "untrusted_key"

    revoked_payloads = dict(writer.payloads)
    revoked_payloads.pop((TENANT_ID, "plugin_signing_key", "calendar-prod"))
    revoked_reader = StoredPersistentPluginService(revoked_payloads)
    revoked_plugins = {
        plugin.id: plugin for plugin in await revoked_reader.list_plugins(tenant_id=TENANT_ID)
    }

    assert revoked_plugins["calendar"].package_metadata is not None
    assert revoked_plugins["calendar"].package_metadata.signature_verification == "untrusted_key"
    assert revoked_plugins["calendar"].package_metadata.activation_state == "blocked_untrusted_key"


@pytest.mark.asyncio
async def test_persistent_plugin_signing_key_delete_is_tenant_scoped_and_audited() -> None:
    class StoredPersistentPluginService(PersistentAdminResourceService):
        def __init__(
            self,
            payloads: dict[tuple[UUID, str, str], dict[str, object]] | None = None,
        ) -> None:
            super().__init__(
                config_service=cast(Any, object()),
                secret_service=cast(Any, object()),
                tenant_id=TENANT_ID,
                actor_id=USER_ID,
            )
            self._session_factory = cast(Any, object())
            self.payloads = payloads if payloads is not None else {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return self.payloads.get((target_tenant_id, kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            self.payloads[(target_tenant_id, kind, resource_id)] = payload
            return True

        async def _delete_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> bool | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            key = (target_tenant_id, kind, resource_id)
            if key not in self.payloads:
                return False
            del self.payloads[key]
            return True

        async def _list_admin_payloads(
            self,
            kind: str,
            *,
            tenant_id: UUID | None = None,
        ) -> list[dict[str, object]] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return [
                payload
                for (payload_tenant_id, payload_kind, _resource_id), payload in self.payloads.items()
                if payload_tenant_id == target_tenant_id and payload_kind == kind
            ]

    private_key = ed25519.Ed25519PrivateKey.generate()
    other_private_key = ed25519.Ed25519PrivateKey.generate()
    service = StoredPersistentPluginService()
    await service.upsert_plugin_signing_key(
        PluginSigningKeyRequest(
            key_id="calendar-prod",
            algorithm="ed25519",
            public_key=plugin_public_key_value(private_key),
        ),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
    )
    await service.upsert_plugin_signing_key(
        PluginSigningKeyRequest(
            key_id="calendar-prod",
            algorithm="ed25519",
            public_key=plugin_public_key_value(other_private_key),
        ),
        tenant_id=OTHER_TENANT_ID,
        actor_id=USER_ID,
    )

    await service.delete_plugin_signing_key(
        "calendar-prod",
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
    )

    assert await service.list_plugin_signing_keys(tenant_id=TENANT_ID) == ()
    assert [
        key.key_id for key in await service.list_plugin_signing_keys(tenant_id=OTHER_TENANT_ID)
    ] == ["calendar-prod"]
    audits = await service.list_audit_events("plugin.signing_key.delete")
    assert len(audits) == 1
    assert audits[0].actor == str(USER_ID)
    assert audits[0].resource == "plugin_signing_key:calendar-prod"
    assert audits[0].details == {
        "key_id": "calendar-prod",
        "algorithm": "ed25519",
        "trusted": "False",
    }


def test_plugin_archive_install_rejects_invalid_trusted_signature() -> None:
    api = client()
    reloaded: list[UUID] = []

    async def reload_plugin_runtime_config(tenant_id: UUID) -> None:
        reloaded.append(tenant_id)

    cast(Any, api.app).state.reload_plugin_runtime_config = reload_plugin_runtime_config
    private_key = ed25519.Ed25519PrivateKey.generate()
    wrong_private_key = ed25519.Ed25519PrivateKey.generate()
    signing_key = api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    assert signing_key.status_code == 200
    reloaded.clear()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=signed_plugin_archive(wrong_private_key),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_package"
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package signature verification failed"
    )
    assert api.get("/api/v1/admin/plugins", headers=headers()).json() == []
    assert reloaded == []


def test_plugin_archive_install_rejects_signed_package_manifest_tampering() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    manifest, files = signed_plugin_archive_parts(signed_plugin_archive(private_key))
    package = cast(dict[str, object], manifest["package"])
    package["package_version"] = "1.2.4"

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(manifest, files=files),
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package signature verification failed"
    )


def test_plugin_archive_install_rejects_signed_package_file_tampering() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    manifest, files = signed_plugin_archive_parts(signed_plugin_archive(private_key))
    files["adapter/main.py"] = "def invoke():\n    return {'changed': True}\n"

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(manifest, files=files),
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package signature verification failed"
    )


def test_plugin_archive_install_rejects_signed_package_extra_file_tampering() -> None:
    api = client()
    private_key = ed25519.Ed25519PrivateKey.generate()
    api.post(
        "/api/v1/admin/plugins/signing-keys",
        headers=headers(),
        json={
            "key_id": "calendar-prod",
            "algorithm": "ed25519",
            "public_key": plugin_public_key_value(private_key),
        },
    )
    manifest, files = signed_plugin_archive_parts(signed_plugin_archive(private_key))
    files["adapter/extra.py"] = "EXTRA = True\n"

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(manifest, files=files),
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["details"]["reason"]
        == "plugin package signature verification failed"
    )


def test_plugin_archive_install_records_unsigned_package_as_not_provided() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins/install",
        headers={
            **headers(),
            "Content-Type": "application/zip",
            "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
        },
        content=plugin_archive(
            {
                "id": "calendar",
                "name": "Calendar HTTP",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.2.3",
                    "adapter_id": "calendar_python",
                    "sdk_api_version": "1.0",
                    "runtime": "python",
                    "entrypoint": "adapter/main.py",
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                },
            },
            files={"adapter/main.py": "def invoke():\n    return {}\n"},
        ),
    )

    assert response.status_code == 200
    assert (
        response.json()["plugin"]["package_metadata"]["signature_verification"]
        == "not_provided"
    )


def test_plugin_resource_upsert_rejects_client_supplied_archive_provenance() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "calendar",
            "name": "Calendar HTTP",
            "source_filename": "forged.zip",
            "content_sha256": "a" * 64,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation"


def test_plugin_resource_upsert_rejects_client_supplied_package_metadata() -> None:
    api = client()

    response = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "calendar",
            "name": "Calendar HTTP",
            "package_metadata": {
                "schema_version": 1,
                "kind": "adapter_package",
                "package_version": "1.2.3",
                "adapter_id": "calendar_python",
                "sdk_api_version": "1.0",
                "runtime": "python",
                "entrypoint": "adapter/main.py",
                "isolation": "local_process",
                "install_mode": "scan_only",
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation"


def test_plugin_reload_callback_failure_does_not_fail_saved_config() -> None:
    api = client()

    async def reload_plugin_runtime_config(tenant_id: UUID) -> None:
        assert tenant_id == TENANT_ID
        raise RuntimeError("plugin reload failed with secret token")

    cast(Any, api.app).state.reload_plugin_runtime_config = reload_plugin_runtime_config

    response = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "search",
            "name": "Search Plugin",
            "capabilities": [
                {
                    "id": "search.web",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert "secret token" not in response.text


def test_plugin_capability_request_defaults_to_remote_connector_sandbox_profile() -> None:
    capability = PluginCapabilityRequest(id="search.web")

    assert capability.sandbox_profile == "remote_connector"


def test_plugin_capability_request_defaults_to_inherited_policy_effect() -> None:
    capability = PluginCapabilityRequest(id="search.web")

    assert capability.policy_effect == "inherit"


def test_plugin_resource_request_preserves_descriptor_resource_config() -> None:
    plugin = PluginResourceRequest(
        id="workflow-plugin",
        name="Workflow Plugin",
        resource_config={"workflow_id": "daily_report"},
    )

    assert plugin.resource_config == {"workflow_id": "daily_report"}


def test_plugin_capability_request_preserves_descriptor_capability_config() -> None:
    capability = PluginCapabilityRequest(
        id="workflow.daily",
        adapter="workflow",
        capability_config={"workflow_stage": "daily", "parallelism": 2},
    )

    assert capability.capability_config == {"workflow_stage": "daily", "parallelism": 2}


@pytest.mark.asyncio
async def test_admin_plugin_lifecycle_updates_status_and_health() -> None:
    service = InMemoryAdminResourceService()

    created = await service.upsert_plugin(
        PluginResourceRequest(
            id="search",
            name="Search Plugin",
            capabilities=[
                PluginCapabilityRequest(
                    id="search.web",
                    permission_class="network.read",
                    sandbox_profile="remote_connector",
                    aliases=["search_web"],
                )
            ],
        )
    )
    disabled = await service.disable_plugin("search")
    enabled = await service.enable_plugin("search")
    started = await service.start_plugin("search")
    stopped = await service.stop_plugin("search")
    reloaded = await service.reload_plugin("search")

    assert created.status == "stopped"
    assert created.health == "stopped"
    assert disabled.enabled is False
    assert disabled.status == "disabled"
    assert disabled.health == "disabled"
    assert enabled.enabled is True
    assert enabled.status == "stopped"
    assert enabled.health == "stopped"
    assert started.status == "running"
    assert started.health == "healthy"
    assert stopped.status == "stopped"
    assert stopped.health == "stopped"
    assert reloaded.status == "running"
    assert reloaded.health == "healthy"


@pytest.mark.asyncio
async def test_persistent_admin_plugin_lifecycle_persists_status() -> None:
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    await service.upsert_plugin(
        PluginResourceRequest(
            id="search",
            name="Search Plugin",
            resource_config={"workflow_id": "daily_report"},
        )
    )
    disabled = await service.disable_plugin("search")
    enabled = await service.enable_plugin("search")
    started = await service.start_plugin("search")
    listed = await service.list_plugins()

    assert disabled.enabled is False
    assert disabled.status == "disabled"
    assert disabled.health == "disabled"
    assert enabled.enabled is True
    assert enabled.status == "stopped"
    assert enabled.health == "stopped"
    assert started.status == "running"
    assert started.health == "healthy"
    assert started.resource_config == {"workflow_id": "daily_report"}
    assert listed == (started,)


@pytest.mark.asyncio
async def test_persistent_admin_plugin_lifecycle_is_scoped_to_requested_tenant() -> None:
    class TenantScopedPersistentService(PersistentAdminResourceService):
        def __init__(self) -> None:
            super().__init__(
                config_service=FakeConfigService(),  # type: ignore[arg-type]
                secret_service=FakeSecretService(),  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
            )
            self._session_factory = cast(Any, object())
            self.payloads: dict[tuple[UUID, str, str], dict[str, object]] = {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return self.payloads.get((target_tenant_id, kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            self.payloads[(target_tenant_id, kind, resource_id)] = payload
            return True

        async def _list_admin_payloads(
            self,
            kind: str,
            *,
            tenant_id: UUID | None = None,
        ) -> list[dict[str, object]] | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            return [
                payload
                for (payload_tenant_id, payload_kind, _resource_id), payload in self.payloads.items()
                if payload_tenant_id == target_tenant_id and payload_kind == kind
            ]

        async def _delete_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> bool | None:
            target_tenant_id = TENANT_ID if tenant_id is None else tenant_id
            key = (target_tenant_id, kind, resource_id)
            if key not in self.payloads:
                return False
            del self.payloads[key]
            return True

        async def _record_audit(
            self,
            action: str,
            resource: str,
            payload: dict[str, object] | None = None,
            *,
            actor_id: UUID | None = None,
            tenant_id: UUID | None = None,
        ) -> None:
            target_actor_id = ACTOR_ID if actor_id is None else actor_id
            await super()._record_audit(
                action,
                resource,
                payload,
                actor_id=target_actor_id,
                tenant_id=tenant_id,
            )

    service = TenantScopedPersistentService()

    await service.upsert_plugin(
        PluginResourceRequest(id="search", name="Bootstrap Search Plugin"),
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )
    await service.upsert_plugin(
        PluginResourceRequest(id="search", name="Tenant Search Plugin"),
        tenant_id=OTHER_TENANT_ID,
        actor_id=USER_ID,
    )
    disabled = await service.disable_plugin("search", tenant_id=OTHER_TENANT_ID, actor_id=USER_ID)
    enabled = await service.enable_plugin("search", tenant_id=OTHER_TENANT_ID, actor_id=USER_ID)
    started = await service.start_plugin("search", tenant_id=OTHER_TENANT_ID, actor_id=USER_ID)
    bootstrap_plugins = await service.list_plugins(tenant_id=TENANT_ID)
    tenant_plugins = await service.list_plugins(tenant_id=OTHER_TENANT_ID)
    await service.delete_plugin("search", tenant_id=OTHER_TENANT_ID, actor_id=USER_ID)
    bootstrap_after_delete = await service.list_plugins(tenant_id=TENANT_ID)
    tenant_after_delete = await service.list_plugins(tenant_id=OTHER_TENANT_ID)
    tenant_start_audits = [
        payload
        for (tenant_id, kind, _resource_id), payload in service.payloads.items()
        if tenant_id == OTHER_TENANT_ID
        and kind == "audit"
        and payload["action"] == "plugin.start"
    ]
    tenant_enable_audits = [
        payload
        for (tenant_id, kind, _resource_id), payload in service.payloads.items()
        if tenant_id == OTHER_TENANT_ID
        and kind == "audit"
        and payload["action"] == "plugin.enable"
    ]
    tenant_disable_audits = [
        payload
        for (tenant_id, kind, _resource_id), payload in service.payloads.items()
        if tenant_id == OTHER_TENANT_ID
        and kind == "audit"
        and payload["action"] == "plugin.disable"
    ]

    assert disabled.enabled is False
    assert disabled.status == "disabled"
    assert enabled.enabled is True
    assert enabled.status == "stopped"
    assert started.name == "Tenant Search Plugin"
    assert started.status == "running"
    assert [plugin.name for plugin in bootstrap_plugins] == ["Bootstrap Search Plugin"]
    assert [plugin.name for plugin in tenant_plugins] == ["Tenant Search Plugin"]
    assert [plugin.name for plugin in bootstrap_after_delete] == ["Bootstrap Search Plugin"]
    assert tenant_after_delete == ()
    assert tenant_start_audits[0]["actor"] == str(USER_ID)
    assert tenant_enable_audits[0]["actor"] == str(USER_ID)
    assert tenant_disable_audits[0]["actor"] == str(USER_ID)


@pytest.mark.asyncio
async def test_persistent_admin_plugin_uninstall_deletes_plugin_and_records_uninstall_audit() -> None:
    class StoredPersistentService(PersistentAdminResourceService):
        def __init__(self) -> None:
            super().__init__(
                config_service=FakeConfigService(),  # type: ignore[arg-type]
                secret_service=FakeSecretService(),  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
            )
            self._session_factory = cast(Any, object())
            self.payloads: dict[tuple[str, str], dict[str, object]] = {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            del tenant_id
            return self.payloads.get((kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            del tenant_id
            self.payloads[(kind, resource_id)] = payload
            return True

        async def _delete_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> bool | None:
            del tenant_id
            key = (kind, resource_id)
            if key not in self.payloads:
                return False
            del self.payloads[key]
            return True

        async def _list_admin_payloads(
            self,
            kind: str,
            *,
            tenant_id: UUID | None = None,
        ) -> list[dict[str, object]] | None:
            del tenant_id
            return [
                payload
                for (payload_kind, _resource_id), payload in self.payloads.items()
                if payload_kind == kind
            ]

    service = StoredPersistentService()

    await service.upsert_plugin(
        PluginResourceRequest(id="search", name="Search Plugin"),
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
    )
    await service.uninstall_plugin("search", tenant_id=TENANT_ID, actor_id=USER_ID)

    assert await service.list_plugins(tenant_id=TENANT_ID) == ()
    uninstall_audits = await service.list_audit_events("plugin.uninstall")
    delete_audits = await service.list_audit_events("plugin.delete")
    assert len(uninstall_audits) == 1
    assert uninstall_audits[0].actor == str(USER_ID)
    assert uninstall_audits[0].resource == "plugin:search"
    assert uninstall_audits[0].details == {"id": "search"}
    assert delete_audits == ()


@pytest.mark.asyncio
async def test_persistent_admin_plugin_upsert_audit_records_safe_policy_summary() -> None:
    class StoredPersistentService(PersistentAdminResourceService):
        def __init__(self) -> None:
            super().__init__(
                config_service=FakeConfigService(),  # type: ignore[arg-type]
                secret_service=FakeSecretService(),  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
            )
            self._session_factory = cast(Any, object())
            self.payloads: dict[tuple[str, str], dict[str, object]] = {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            del tenant_id
            return self.payloads.get((kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            del tenant_id
            self.payloads[(kind, resource_id)] = payload
            return True

        async def _list_admin_payloads(
            self,
            kind: str,
            *,
            tenant_id: UUID | None = None,
        ) -> list[dict[str, object]] | None:
            del tenant_id
            return [
                payload
                for (payload_kind, _resource_id), payload in self.payloads.items()
                if payload_kind == kind
            ]

    service = StoredPersistentService()

    await service.upsert_plugin(
        PluginResourceRequest(
            id="search",
            name="Search Plugin",
            resource_config={
                "base_url": "https://search.internal",
                "secret_token": "do-not-leak",
                "token": "also-do-not-leak",
            },
            capabilities=[
                PluginCapabilityRequest(
                    id="search.web",
                    adapter="http_json",
                    permission_class="network.read",
                    sandbox_profile="remote_connector",
                    policy_effect="require_approval",
                    capability_config={
                        "credential": "capability-do-not-leak",
                        "mode": "semantic",
                        "api_key": "sk-do-not-leak",
                    },
                )
            ],
        )
    )

    audits = await service.list_audit_events("plugin.upsert")

    assert len(audits) == 1
    details = audits[0].details
    assert details["id"] == "search"
    assert details["capability_count"] == "1"
    assert details["capability_ids"] == "search.web"
    assert details["adapters"] == "http_json"
    assert details["permission_classes"] == "network.read"
    assert details["policy_effects"] == "require_approval"
    assert details["sandbox_profiles"] == "remote_connector"
    assert details["resource_config_key_count"] == "3"
    assert details["resource_config_keys"] == "base_url"
    assert details["redacted_resource_config_key_count"] == "2"
    assert details["capability_config_key_count"] == "3"
    assert details["capability_config_keys"] == "mode"
    assert details["redacted_capability_config_key_count"] == "2"
    assert "do-not-leak" not in repr(details)
    assert "sk-do-not-leak" not in repr(details)
    assert "also-do-not-leak" not in repr(details)
    assert "capability-do-not-leak" not in repr(details)
    assert "secret_token" not in repr(details)
    assert "api_key" not in repr(details)
    assert "token" not in repr(details)
    assert "credential" not in repr(details)


def test_plugin_admin_api_exposes_running_plugin_capabilities_in_manifest() -> None:
    api = client()
    cast(Any, api.app).state.runtime_capability_gateway = FakeRuntimeCapabilityGateway()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "search",
            "name": "Search Plugin",
            "capabilities": [
                {
                    "id": "search.web",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "require_approval",
                    "aliases": ["search_web"],
                }
            ],
        },
    )
    started = api.post("/api/v1/admin/plugins/search/start", headers=headers())
    manifest = api.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert created.status_code == 200
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    capabilities = {item["id"]: item for item in manifest.json()["capabilities"]}
    assert capabilities["search.web"] == {
        "id": "search.web",
        "kind": "plugin",
        "adapter": "plugin_runtime",
        "permission_class": "network.read",
        "sandbox_profile": "remote_connector",
        "policy_effect": "require_approval",
        "available": True,
        "availability_reason": None,
        "replay_safe": False,
        "aliases": ["search_web"],
        "input_schema": None,
        "output_schema": None,
    }


def test_plugin_admin_api_exposes_safe_plugin_policy_summary() -> None:
    api = client()

    class PluginServiceWithPolicyDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "http_json",
                    "name": "HTTP JSON",
                    "description": "Accepts descriptor-owned policy test config.",
                    "resource_schema": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "capability_schema": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithPolicyDescriptor()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "search",
            "name": "Search Plugin",
            "version": "2026.09",
            "resource_config": {
                "base_url": "https://search.internal",
                "secret_token": "do-not-leak",
                "token": "also-do-not-leak",
            },
            "capabilities": [
                {
                    "id": "search.web",
                    "adapter": "http_json",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "require_approval",
                    "replay_safe": True,
                    "aliases": ["search_web"],
                    "capability_config": {
                        "credential": "capability-do-not-leak",
                        "mode": "semantic",
                        "api_key": "sk-do-not-leak",
                    },
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"results": {"type": "array"}},
                    },
                }
            ],
        },
    )

    summary = api.get("/api/v1/admin/plugins/policy-summary", headers=headers())

    assert created.status_code == 200
    assert summary.status_code == 200
    assert summary.json() == [
        {
            "id": "search",
            "name": "Search Plugin",
            "version": "2026.09",
            "enabled": True,
            "status": "stopped",
            "health": "stopped",
            "capability_count": 1,
            "adapters": ["http_json"],
            "permission_classes": ["network.read"],
            "policy_effects": ["require_approval"],
            "sandbox_profiles": ["remote_connector"],
            "resource_config_key_count": 3,
            "resource_config_keys": ["base_url"],
            "redacted_resource_config_key_count": 2,
            "capabilities": [
                {
                    "id": "search.web",
                    "adapter": "http_json",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "require_approval",
                    "replay_safe": True,
                    "aliases": ["search_web"],
                    "input_schema_declared": True,
                    "output_schema_declared": True,
                    "capability_config_key_count": 3,
                    "capability_config_keys": ["mode"],
                    "redacted_capability_config_key_count": 2,
                }
            ],
        }
    ]
    assert "do-not-leak" not in summary.text
    assert "sk-do-not-leak" not in summary.text
    assert "also-do-not-leak" not in summary.text
    assert "capability-do-not-leak" not in summary.text
    assert "secret_token" not in summary.text
    assert "api_key" not in summary.text
    assert "token" not in summary.text
    assert "credential" not in summary.text


def test_plugin_admin_api_records_safe_plugin_policy_review_audit() -> None:
    api = client()

    class PluginServiceWithPolicyDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "http_json",
                    "name": "HTTP JSON",
                    "description": "Accepts descriptor-owned policy test config.",
                    "resource_schema": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "capability_schema": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithPolicyDescriptor()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "search",
            "name": "Search Plugin",
            "endpoint_url": "https://search.internal/secret-path",
            "credential_ref": "credential-do-not-leak",
            "resource_config": {
                "base_url": "https://search.internal",
                "secret_token": "do-not-leak",
                "mode": "semantic",
            },
            "capabilities": [
                {
                    "id": "search.web",
                    "adapter": "http_json",
                    "permission_class": "network.read",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "require_approval",
                    "replay_safe": True,
                    "capability_config": {
                        "credential": "capability-do-not-leak",
                        "mode": "semantic",
                    },
                },
                {
                    "id": "search.delete",
                    "adapter": "http_json",
                    "permission_class": "network.write",
                    "sandbox_profile": "remote_connector",
                    "policy_effect": "deny",
                    "capability_config": {"mode": "delete"},
                },
            ],
        },
    )

    review = api.post("/api/v1/admin/plugins/policy-review", headers=headers())
    audit = api.get("/api/v1/admin/audit?action=plugin.policy_review", headers=headers())

    assert created.status_code == 200
    assert review.status_code == 200
    assert review.json() == {
        "plugin_count": 1,
        "capability_count": 2,
        "policy_effect_counts": {"deny": 1, "require_approval": 1},
        "permission_class_counts": {"network.read": 1, "network.write": 1},
        "sandbox_profile_counts": {"remote_connector": 2},
        "plugins": [
            {
                "id": "search",
                "name": "Search Plugin",
                "version": "local",
                "enabled": True,
                "status": "stopped",
                "health": "stopped",
                "capability_count": 2,
                "adapters": ["http_json"],
                "permission_classes": ["network.read", "network.write"],
                "policy_effects": ["deny", "require_approval"],
                "sandbox_profiles": ["remote_connector"],
                "resource_config_key_count": 3,
                "resource_config_keys": ["base_url", "mode"],
                "redacted_resource_config_key_count": 1,
                "capabilities": [
                    {
                        "id": "search.web",
                        "adapter": "http_json",
                        "permission_class": "network.read",
                        "sandbox_profile": "remote_connector",
                        "policy_effect": "require_approval",
                        "replay_safe": True,
                        "aliases": [],
                        "input_schema_declared": False,
                        "output_schema_declared": False,
                        "capability_config_key_count": 2,
                        "capability_config_keys": ["mode"],
                        "redacted_capability_config_key_count": 1,
                    },
                    {
                        "id": "search.delete",
                        "adapter": "http_json",
                        "permission_class": "network.write",
                        "sandbox_profile": "remote_connector",
                        "policy_effect": "deny",
                        "replay_safe": False,
                        "aliases": [],
                        "input_schema_declared": False,
                        "output_schema_declared": False,
                        "capability_config_key_count": 1,
                        "capability_config_keys": ["mode"],
                        "redacted_capability_config_key_count": 0,
                    },
                ],
            }
        ],
    }
    assert audit.status_code == 200
    assert audit.json()[0]["actor"] == str(USER_ID)
    assert audit.json()[0]["details"] == {
        "capability_count": "2",
        "deny_count": "1",
        "permission_classes": "network.read,network.write",
        "plugin_count": "1",
        "policy_effects": "deny,require_approval",
        "require_approval_count": "1",
        "sandbox_profiles": "remote_connector",
    }
    serialized = review.text + audit.text
    assert "do-not-leak" not in serialized
    assert "credential-do-not-leak" not in serialized
    assert "capability-do-not-leak" not in serialized
    assert "secret-path" not in serialized
    assert "secret_token" not in serialized
    assert "credential" not in serialized


def test_plugin_admin_api_validates_descriptor_resource_config() -> None:
    api = client()

    class PluginServiceWithWorkflowDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "workflow",
                    "name": "Workflow",
                    "description": "Runs a workflow.",
                    "resource_schema": {
                        "type": "object",
                        "required": ("workflow_id", "retry_limit"),
                        "properties": {
                            "workflow_id": {"type": "string"},
                            "retry_limit": {"type": "integer", "minimum": 1, "maximum": 5},
                        },
                        "additionalProperties": False,
                    },
                    "capability_schema": {
                        "type": "object",
                        "required": ("id",),
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithWorkflowDescriptor()

    missing = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "workflow-plugin",
            "name": "Workflow Plugin",
            "capabilities": [{"id": "workflow.daily", "adapter": "workflow"}],
            "resource_config": {"workflow_id": "daily_report"},
        },
    )
    invalid = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "workflow-plugin",
            "name": "Workflow Plugin",
            "capabilities": [{"id": "workflow.daily", "adapter": "workflow"}],
            "resource_config": {"workflow_id": "daily_report", "retry_limit": "nope"},
        },
    )
    valid = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "workflow-plugin",
            "name": "Workflow Plugin",
            "capabilities": [{"id": "workflow.daily", "adapter": "workflow"}],
            "resource_config": {"workflow_id": "daily_report", "retry_limit": 3},
        },
    )

    assert missing.status_code == 422
    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert valid.json()["resource_config"] == {"workflow_id": "daily_report", "retry_limit": 3}


def test_plugin_admin_api_validates_descriptor_capability_config() -> None:
    api = client()

    class PluginServiceWithWorkflowDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "workflow",
                    "name": "Workflow",
                    "description": "Runs a workflow.",
                    "resource_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "capability_schema": {
                        "type": "object",
                        "required": ("id", "workflow_stage", "parallelism"),
                        "properties": {
                            "id": {"type": "string"},
                            "workflow_stage": {"type": "string"},
                            "parallelism": {"type": "integer", "minimum": 1, "maximum": 4},
                        },
                        "additionalProperties": False,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithWorkflowDescriptor()

    missing = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "workflow-plugin",
            "name": "Workflow Plugin",
            "capabilities": [{"id": "workflow.daily", "adapter": "workflow"}],
        },
    )
    invalid = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "workflow-plugin",
            "name": "Workflow Plugin",
            "capabilities": [
                {
                    "id": "workflow.daily",
                    "adapter": "workflow",
                    "capability_config": {"workflow_stage": "daily", "parallelism": 8},
                }
            ],
        },
    )
    valid = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "workflow-plugin",
            "name": "Workflow Plugin",
            "capabilities": [
                {
                    "id": "workflow.daily",
                    "adapter": "workflow",
                    "capability_config": {"workflow_stage": "daily", "parallelism": 2},
                }
            ],
        },
    )

    assert missing.status_code == 422
    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert valid.json()["capabilities"][0]["capability_config"] == {
        "workflow_stage": "daily",
        "parallelism": 2,
    }


def test_plugin_admin_api_allows_descriptor_capability_additional_properties() -> None:
    api = client()

    class PluginServiceWithOpaqueCapabilityDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "opaque",
                    "name": "Opaque",
                    "description": "Accepts adapter-owned capability config.",
                    "resource_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "capability_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithOpaqueCapabilityDescriptor()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "opaque-plugin",
            "name": "Opaque Plugin",
            "capabilities": [
                {
                    "id": "opaque.run",
                    "adapter": "opaque",
                    "capability_config": {
                        "routing": {"mode": "fanout", "max_children": 3},
                        "labels": ["daily", "parallel"],
                    },
                }
            ],
        },
    )

    assert created.status_code == 200
    assert created.json()["capabilities"][0]["capability_config"] == {
        "routing": {"mode": "fanout", "max_children": 3},
        "labels": ["daily", "parallel"],
    }


def test_plugin_admin_api_allows_descriptor_resource_additional_properties() -> None:
    api = client()

    class PluginServiceWithOpaqueResourceDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "opaque",
                    "name": "Opaque",
                    "description": "Accepts adapter-owned resource config.",
                    "resource_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True,
                    },
                    "capability_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
                {
                    "id": "closed",
                    "name": "Closed",
                    "description": "Rejects adapter-owned resource config.",
                    "resource_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "capability_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithOpaqueResourceDescriptor()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "opaque-resource-plugin",
            "name": "Opaque Resource Plugin",
            "capabilities": [{"id": "opaque.run", "adapter": "opaque"}],
            "resource_config": {
                "routing": {"mode": "fanout", "max_children": 3},
                "labels": ["daily", "parallel"],
            },
        },
    )
    rejected = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "closed-resource-plugin",
            "name": "Closed Resource Plugin",
            "capabilities": [{"id": "closed.run", "adapter": "closed"}],
            "resource_config": {"routing": {"mode": "fanout"}},
        },
    )

    assert created.status_code == 200
    assert created.json()["resource_config"] == {
        "routing": {"mode": "fanout", "max_children": 3},
        "labels": ["daily", "parallel"],
    }
    assert rejected.status_code == 422


def test_plugin_admin_api_rejects_unknown_resource_config_when_any_descriptor_is_closed() -> None:
    api = client()

    class PluginServiceWithMixedResourceDescriptors:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "opaque",
                    "name": "Opaque",
                    "description": "Accepts adapter-owned resource config.",
                    "resource_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True,
                    },
                    "capability_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
                {
                    "id": "closed",
                    "name": "Closed",
                    "description": "Rejects adapter-owned resource config.",
                    "resource_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "capability_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithMixedResourceDescriptors()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "mixed-resource-plugin",
            "name": "Mixed Resource Plugin",
            "capabilities": [
                {"id": "opaque.run", "adapter": "opaque"},
                {"id": "closed.run", "adapter": "closed"},
            ],
            "resource_config": {"routing": {"mode": "fanout"}},
        },
    )

    assert created.status_code == 422


def test_plugin_admin_api_preserves_capability_schemas_in_manifest() -> None:
    api = client()
    cast(Any, api.app).state.runtime_capability_gateway = FakeRuntimeCapabilityGateway()

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "calendar",
            "name": "Calendar Plugin",
            "capabilities": [
                {
                    "id": "calendar.create_event",
                    "adapter": "http_json",
                    "permission_class": "calendar.write",
                    "sandbox_profile": "remote_connector",
                    "input_schema": {
                        "type": "object",
                        "required": ["title"],
                        "properties": {"title": {"type": "string"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"remote_id": {"type": "string"}},
                    },
                }
            ],
        },
    )
    started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())
    manifest = api.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert created.status_code == 200
    assert started.status_code == 200
    capability = {
        item["id"]: item
        for item in manifest.json()["capabilities"]
    }["calendar.create_event"]
    assert capability["input_schema"] == {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    assert capability["output_schema"] == {
        "type": "object",
        "properties": {"remote_id": {"type": "string"}},
    }


@pytest.mark.asyncio
async def test_plugin_admin_runtime_http_json_acceptance_records_safe_audit() -> None:
    adapter_calls: list[dict[str, object]] = []

    class AdapterHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            adapter_calls.append(
                {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "body": body,
                }
            )
            payload = json.dumps({"remote_id": "evt_123"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), AdapterHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = client()
        admin_service = cast(
            InMemoryAdminResourceService,
            cast(Any, api.app).state.admin_resource_service,
        )
        runtime_plugin_service = await build_runtime_plugin_service(
            tenant_id=TENANT_ID,
            admin_service=admin_service,
        )
        cast(Any, api.app).state.plugin_service = runtime_plugin_service
        cast(Any, api.app).state.reload_plugin_runtime_config = runtime_plugin_service.reload

        created = api.post(
            "/api/v1/admin/plugins",
            headers=headers(),
            json={
                "id": "calendar",
                "name": "Calendar Plugin",
                "endpoint_url": f"http://127.0.0.1:{server.server_port}/invoke",
                "domain_allowlist": ["127.0.0.1"],
                "capabilities": [
                    {
                        "id": "calendar.create_event",
                        "adapter": "http_json",
                        "permission_class": "calendar.write",
                        "sandbox_profile": "remote_connector",
                        "policy_effect": "require_approval",
                        "input_schema": {
                            "type": "object",
                            "required": ["title"],
                            "properties": {"title": {"type": "string"}},
                        },
                        "output_schema": {
                            "type": "object",
                            "required": ["remote_id"],
                            "properties": {"remote_id": {"type": "string"}},
                        },
                    }
                ],
            },
        )
        started = api.post("/api/v1/admin/plugins/calendar/start", headers=headers())

        assert created.status_code == 200
        assert started.status_code == 200
        result = await runtime_plugin_service.invoke(
            tenant_id=TENANT_ID,
            user_id=TENANT_ID,
            run_id=TENANT_ID,
            actor="acceptance",
            name="calendar.create_event",
            arguments={"title": "Planning"},
            idempotency_key="acceptance-1",
        )
        audit = api.get(
            "/api/v1/admin/audit?action=plugin.invoke.succeeded",
            headers=headers(),
        )

        assert result == {"remote_id": "evt_123"}
        assert len(adapter_calls) == 1
        call = adapter_calls[0]
        assert call["path"] == "/invoke"
        assert call["content_type"] == "application/json"
        body = cast(dict[str, object], call["body"])
        assert body["plugin_id"] == "calendar"
        assert body["capability_id"] == "calendar.create_event"
        assert body["arguments"] == {"title": "Planning"}
        assert body["resource_config"] == {}
        assert body["capability_config"] == {}
        assert cast(dict[str, object], body["context"])["idempotency_key"] == "acceptance-1"
        assert audit.status_code == 200
        event = audit.json()[0]
        assert event["resource"] == "plugin:calendar:calendar.create_event"
        assert event["details"]["adapter"] == "http_json"
        assert event["details"]["permission_class"] == "calendar.write"
        assert event["details"]["sandbox_profile"] == "remote_connector"
        assert "Planning" not in audit.text
        assert "evt_123" not in audit.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_plugin_admin_runtime_in_process_adapter_acceptance_records_safe_audit() -> None:
    adapter_calls: list[dict[str, object]] = []

    class InProcessAdapter:
        async def invoke(
            self,
            *,
            plugin: PluginResourceResponse,
            capability: PluginCapabilityRequest,
            arguments: Mapping[str, JsonValue],
            context: PluginInvocationContext,
        ) -> Mapping[str, JsonValue]:
            adapter_calls.append(
                {
                    "plugin_id": plugin.id,
                    "capability_id": capability.id,
                    "arguments": dict(arguments),
                    "idempotency_key": context.idempotency_key,
                }
            )
            return {"ok": True, "handled_by": "in_process"}

        def descriptor(self) -> Mapping[str, JsonValue]:
            return {
                "id": "local_tool",
                "name": "Local Tool",
                "description": "Runs a trusted local test adapter.",
                "resource_schema": {"type": "object", "additionalProperties": False},
                "capability_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "sandbox_profile": {"type": "string", "enum": ("in_process",)},
                    },
                    "additionalProperties": True,
                },
                "argument_schema": {
                    "type": "object",
                    "required": ("command",),
                    "properties": {"command": {"type": "string"}},
                    "additionalProperties": False,
                },
            }

    api = client()
    admin_service = cast(
        InMemoryAdminResourceService,
        cast(Any, api.app).state.admin_resource_service,
    )
    runtime_plugin_service = await build_runtime_plugin_service(
        tenant_id=TENANT_ID,
        admin_service=admin_service,
        adapters={"local_tool": InProcessAdapter()},
    )
    cast(Any, api.app).state.plugin_service = runtime_plugin_service
    cast(Any, api.app).state.reload_plugin_runtime_config = runtime_plugin_service.reload

    created = api.post(
        "/api/v1/admin/plugins",
        headers=headers(),
        json={
            "id": "local",
            "name": "Local Plugin",
            "capabilities": [
                {
                    "id": "local.run",
                    "adapter": "local_tool",
                    "permission_class": "local.execute",
                    "sandbox_profile": "in_process",
                    "policy_effect": "require_approval",
                }
            ],
        },
    )
    started = api.post("/api/v1/admin/plugins/local/start", headers=headers())

    assert created.status_code == 200
    assert started.status_code == 200
    result = await runtime_plugin_service.invoke(
        tenant_id=TENANT_ID,
        user_id=TENANT_ID,
        run_id=TENANT_ID,
        actor="acceptance",
        name="local.run",
        arguments={"command": "status"},
        idempotency_key="in-process-1",
    )
    audit = api.get(
        "/api/v1/admin/audit?action=plugin.invoke.succeeded",
        headers=headers(),
    )

    assert result == {"ok": True, "handled_by": "in_process"}
    assert adapter_calls == [
        {
            "plugin_id": "local",
            "capability_id": "local.run",
            "arguments": {"command": "status"},
            "idempotency_key": "in-process-1",
        }
    ]
    assert audit.status_code == 200
    event = audit.json()[0]
    assert event["resource"] == "plugin:local:local.run"
    assert event["details"]["adapter"] == "local_tool"
    assert event["details"]["permission_class"] == "local.execute"
    assert event["details"]["sandbox_profile"] == "in_process"
    assert "status" not in audit.text


def test_plugin_adapter_catalog_endpoint_exposes_safe_http_json_descriptor() -> None:
    api = client()

    response = api.get("/api/v1/admin/plugins/adapters", headers=headers())

    assert response.status_code == 200
    descriptors = {item["id"]: item for item in response.json()}
    assert "http_json" in descriptors
    assert descriptors["http_json"]["resource_schema"]["required"] == [
        "endpoint_url",
        "domain_allowlist",
    ]
    assert descriptors["http_json"]["capability_contract"] == {
        "schema_version": 1,
        "declared_sandbox_profiles": [],
        "runtime_sandbox_profiles": ["remote_connector"],
    }
    assert "credential_ref" in descriptors["http_json"]["resource_schema"]["properties"]
    assert "Authorization" not in response.text


def test_plugin_adapter_catalog_exposes_runtime_contract_for_declared_sandboxes() -> None:
    api = client()

    class PluginServiceWithSandboxDescriptor:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "browser",
                    "name": "Browser",
                    "description": "Reads allowlisted web resources.",
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {
                            "sandbox_profile": {
                                "type": "string",
                                "enum": ("http_read", "local_process"),
                            }
                        },
                        "additionalProperties": True,
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithSandboxDescriptor()

    response = api.get("/api/v1/admin/plugins/adapters", headers=headers())

    assert response.status_code == 200
    descriptor = response.json()[0]
    assert descriptor["id"] == "browser"
    assert descriptor["capability_contract"] == {
        "schema_version": 1,
        "declared_sandbox_profiles": ["http_read", "local_process"],
        "runtime_sandbox_profiles": ["http_read", "local_process", "remote_connector"],
    }


def test_plugin_adapter_catalog_skips_invalid_provider_descriptors() -> None:
    api = client()

    class PluginServiceWithMixedDescriptors:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "workflow",
                    "name": "Workflow",
                    "description": "Runs workflows.",
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {"type": "object", "additionalProperties": True},
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
                {
                    "id": "broken",
                    "name": "Broken",
                    "resource_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithMixedDescriptors()

    response = api.get("/api/v1/admin/plugins/adapters", headers=headers())

    assert response.status_code == 200
    descriptors = {item["id"]: item for item in response.json()}
    assert "workflow" in descriptors
    assert "broken" not in descriptors
    assert "http_json" not in descriptors


def test_plugin_adapter_catalog_skips_descriptors_with_invalid_schemas() -> None:
    api = client()

    class PluginServiceWithInvalidSchemaDescriptors:
        def adapter_descriptors(self) -> tuple[dict[str, object], ...]:
            return (
                {
                    "id": "workflow",
                    "name": "Workflow",
                    "description": "Runs workflows.",
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {"type": "object", "additionalProperties": True},
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
                {
                    "id": "bad-resource",
                    "name": "Bad Resource",
                    "description": "Has an invalid resource schema.",
                    "resource_schema": {"type": "not-a-json-schema-type"},
                    "capability_schema": {"type": "object", "additionalProperties": True},
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
                {
                    "id": "array-resource",
                    "name": "Array Resource",
                    "description": "Has a non-object resource schema.",
                    "resource_schema": {"type": "array"},
                    "capability_schema": {"type": "object", "additionalProperties": True},
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
                {
                    "id": "referenced-capability",
                    "name": "Referenced Capability",
                    "description": "Has an unsupported capability schema reference.",
                    "resource_schema": {"type": "object", "additionalProperties": True},
                    "capability_schema": {
                        "type": "object",
                        "properties": {"stage": {"$ref": "#/$defs/stage"}},
                    },
                    "argument_schema": {"type": "object", "additionalProperties": True},
                },
            )

    cast(Any, api.app).state.plugin_service = PluginServiceWithInvalidSchemaDescriptors()

    response = api.get("/api/v1/admin/plugins/adapters", headers=headers())

    assert response.status_code == 200
    descriptors = {item["id"]: item for item in response.json()}
    assert set(descriptors) == {"workflow"}


def test_capability_manifest_endpoint_requires_plugin_and_mcp_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_permissions: list[str] = []

    class RecordingAuthorizer:
        def require(
            self,
            principal: AuthenticatedPrincipal,
            permission: str,
        ) -> AuthenticatedPrincipal:
            required_permissions.append(permission)
            return principal

    monkeypatch.setattr(admin_router, "Authorizer", RecordingAuthorizer)
    api = client()
    cast(Any, api.app).state.runtime_capability_gateway = FakeRuntimeCapabilityGateway()

    response = api.get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert response.status_code == 200
    assert required_permissions == ["plugin:read", "mcp:read"]


def test_capability_manifest_endpoint_fails_when_runtime_gateway_is_unavailable() -> None:
    response = client().get("/api/v1/admin/capabilities/manifest", headers=headers())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "capability_manifest_unavailable"


@dataclass(frozen=True, slots=True)
class SubmittedScheduleRun:
    id: UUID


class RecordingScheduleSubmitter:
    def __init__(self) -> None:
        self.calls: list[TaskRequest] = []

    async def submit(self, request: TaskRequest) -> SubmittedScheduleRun:
        self.calls.append(request)
        return SubmittedScheduleRun(uuid4())


@dataclass(frozen=True, slots=True)
class SubmittedTaskRun:
    id: UUID


class RecordingScheduledRunService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_role: Role | None = None,
        message: str,
        mode: TaskMode,
        workflow_id: str | None = None,
        channel_context: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> SubmittedTaskRun:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "actor_role": actor_role,
                "message": message,
                "mode": mode,
                "workflow_id": workflow_id,
                "channel_context": channel_context,
                "idempotency_key": idempotency_key,
            }
        )
        return SubmittedTaskRun(uuid4())


class PersistentScheduleResourceService(InMemoryAdminResourceService):
    def __init__(
        self,
        tenant_id: UUID = TENANT_ID,
        actor_id: UUID = ACTOR_ID,
        *,
        root: "PersistentScheduleResourceService | None" = None,
    ) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.root = root or self
        if root is None:
            self.payloads: dict[tuple[UUID, str, str], dict[str, object]] = {}
            self.scopes: dict[tuple[UUID, UUID], PersistentScheduleResourceService] = {
                (tenant_id, actor_id): self
            }
            self.calls: list[tuple[str, str, UUID, UUID]] = []

    def for_principal(
        self,
        tenant_id: UUID,
        actor_id: UUID,
    ) -> "PersistentScheduleResourceService":
        root = self.root
        root.calls.append(("for_principal", "", tenant_id, actor_id))
        key = (tenant_id, actor_id)
        if key not in root.scopes:
            root.scopes[key] = PersistentScheduleResourceService(
                tenant_id,
                actor_id,
                root=root,
            )
        return root.scopes[key]

    async def _list_admin_payloads(
        self,
        kind: str,
        *,
        tenant_id: UUID | None = None,
    ) -> list[dict[str, object]]:
        target_tenant_id = self.tenant_id if tenant_id is None else tenant_id
        self.root.calls.append(("list_payloads", kind, target_tenant_id, self.actor_id))
        return [
            payload
            for (stored_tenant_id, stored_kind, _resource_id), payload in sorted(
                self.root.payloads.items()
            )
            if stored_tenant_id == target_tenant_id and stored_kind == kind
        ]

    async def _upsert_admin_payload(
        self,
        kind: str,
        resource_id: str,
        payload: dict[str, object],
        *,
        tenant_id: UUID | None = None,
    ) -> bool:
        target_tenant_id = self.tenant_id if tenant_id is None else tenant_id
        self.root.calls.append(("upsert_payload", kind, target_tenant_id, self.actor_id))
        self.root.payloads[(target_tenant_id, kind, resource_id)] = payload
        return True

    async def _delete_admin_payload(
        self,
        kind: str,
        resource_id: str,
        *,
        tenant_id: UUID | None = None,
    ) -> bool:
        target_tenant_id = self.tenant_id if tenant_id is None else tenant_id
        self.root.calls.append(("delete_payload", kind, target_tenant_id, self.actor_id))
        return self.root.payloads.pop((target_tenant_id, kind, resource_id), None) is not None

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
        tenant_id: UUID | None = None,
    ) -> AuditEventResponse:
        target_tenant_id = self.tenant_id if tenant_id is None else tenant_id
        self.root.calls.append(("audit", action, target_tenant_id, UUID(actor)))
        return await super().record_audit_event(
            actor=actor,
            action=action,
            resource=resource,
            details=details,
            tenant_id=tenant_id,
        )


def scheduler_client(
    submitter: RecordingScheduleSubmitter,
    *,
    resource_service: InMemoryAdminResourceService | None = None,
    auth_service: object | None = None,
) -> TestClient:
    app = create_app(
        auth_service=auth_service or StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = resource_service or InMemoryAdminResourceService()
    app.state.schedule_service = SchedulerService(submitter.submit)
    return TestClient(app)


@pytest.mark.asyncio
async def test_scheduled_task_submission_records_operator_role_snapshot() -> None:
    app = FastAPI()
    run_service = RecordingScheduledRunService()
    app.state.run_service = run_service
    request = TaskRequest(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        message="execute scheduled workflow",
        mode=TaskMode.DISPATCH,
        workflow="nightly_check",
        budget=16_384,
        idempotency_key="schedule:test",
        metadata={"source": "scheduler"},
    )

    submitted = await _submit_scheduled_task(app, request)

    assert isinstance(submitted, SubmittedTaskRun)
    assert run_service.calls[0]["actor_role"] is Role.OPERATOR
    assert run_service.calls[0]["workflow_id"] == "nightly_check"
    assert run_service.calls[0]["channel_context"] == {"source": "scheduler"}


def model_payload() -> dict[str, object]:
    return {
        "provider": "deepseek",
        "api_base": "https://api.deepseek.example/v1",
        "upstream_model": "deepseek-chat",
        "logical_model": "planner",
        "capabilities": ["text", "tool_calling"],
        "credential_ref": "secret_1",
        "quota_scope": "deepseek_account_1",
        "max_concurrency": 1,
        "target_utilization": 0.8,
        "reserved_capacity": 0,
        "rpm": 60,
        "tpm": 100000,
        "queue_timeout_seconds": 60,
        "fallback": "planner_backup",
        "weight": 100,
    }


def test_schedule_api_creates_lists_and_ticks_user_visible_tasks() -> None:
    submitter = RecordingScheduleSubmitter()
    api = scheduler_client(submitter)
    run_at = "2026-08-13T09:00:00+08:00"

    created = api.post(
        "/api/v1/admin/schedules",
        headers=headers(),
        json={
            "name": "daily-report-fill",
            "message": "Open the report system and fill today's report",
            "mode": "dispatch",
            "workflow_id": "daily_report",
            "kind": "one_time",
            "run_at": run_at,
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once",
            "budget": 4096,
            "metadata": {"openclaw": "windows_desktop_report"},
        },
    )

    assert created.status_code == 201
    schedule = created.json()
    assert schedule["name"] == "daily-report-fill"
    assert schedule["status"] == "active"
    assert schedule["kind"] == "one_time"
    assert schedule["next_fire_at"] == "2026-08-13T01:00:00Z"

    listed = api.get("/api/v1/admin/schedules", headers=headers())
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [schedule["id"]]

    ticked = api.post(
        "/api/v1/admin/schedules/tick",
        headers=headers(),
        json={"now": run_at},
    )

    assert ticked.status_code == 200
    assert ticked.json() == {"fired": [schedule["id"]]}
    assert len(submitter.calls) == 1
    request = submitter.calls[0]
    assert request.tenant_id == TENANT_ID
    assert request.actor_id == USER_ID
    assert request.message == "Open the report system and fill today's report"
    assert request.mode.value == "dispatch"
    assert request.workflow == "daily_report"
    assert request.budget == 4096
    assert request.metadata["schedule_id"] == schedule["id"]
    assert request.metadata["openclaw"] == "windows_desktop_report"


def test_schedule_api_persists_restores_and_deletes_tasks() -> None:
    resource_service = PersistentScheduleResourceService()
    api = scheduler_client(RecordingScheduleSubmitter(), resource_service=resource_service)
    run_at = "2026-08-14T09:00:00+08:00"

    created = api.post(
        "/api/v1/admin/schedules",
        headers=headers(),
        json={
            "name": "restart-safe-report-fill",
            "message": "Open the report system after restart",
            "mode": "dispatch",
            "workflow_id": "daily_report",
            "kind": "one_time",
            "run_at": run_at,
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once",
            "budget": 4096,
            "metadata": {"openclaw": "windows_desktop_report"},
        },
    )

    assert created.status_code == 201
    schedule_id = created.json()["id"]
    assert (TENANT_ID, "schedule", schedule_id) in resource_service.payloads

    restarted_api = scheduler_client(
        RecordingScheduleSubmitter(),
        resource_service=resource_service,
    )
    restored = restarted_api.get("/api/v1/admin/schedules", headers=headers())
    assert restored.status_code == 200
    assert [item["id"] for item in restored.json()] == [schedule_id]

    deleted = restarted_api.delete(f"/api/v1/admin/schedules/{schedule_id}", headers=headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"id": schedule_id, "deleted": True}
    assert (TENANT_ID, "schedule", schedule_id) not in resource_service.payloads

    listed_after_delete = restarted_api.get("/api/v1/admin/schedules", headers=headers())
    assert listed_after_delete.status_code == 200
    assert listed_after_delete.json() == []


def test_schedule_api_persists_restores_and_deletes_only_principal_tenant() -> None:
    resource_service = PersistentScheduleResourceService()
    bootstrap_api = scheduler_client(
        RecordingScheduleSubmitter(),
        resource_service=resource_service,
    )
    other_api = scheduler_client(
        RecordingScheduleSubmitter(),
        resource_service=resource_service,
        auth_service=OtherTenantAuthService(),
    )

    bootstrap = bootstrap_api.post(
        "/api/v1/admin/schedules",
        headers=headers(),
        json={
            "name": "bootstrap-report",
            "message": "Run bootstrap report",
            "mode": "dispatch",
            "workflow_id": "daily_report",
            "kind": "one_time",
            "run_at": "2026-08-14T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )
    other = other_api.post(
        "/api/v1/admin/schedules",
        headers=headers(),
        json={
            "name": "tenant-report",
            "message": "Run tenant report",
            "mode": "dispatch",
            "workflow_id": "daily_report",
            "kind": "one_time",
            "run_at": "2026-08-15T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )

    assert bootstrap.status_code == 201
    assert other.status_code == 201
    bootstrap_id = bootstrap.json()["id"]
    other_id = other.json()["id"]
    assert (TENANT_ID, "schedule", bootstrap_id) in resource_service.payloads
    assert (OTHER_TENANT_ID, "schedule", other_id) in resource_service.payloads

    restarted_other_api = scheduler_client(
        RecordingScheduleSubmitter(),
        resource_service=resource_service,
        auth_service=OtherTenantAuthService(),
    )
    restored_other = restarted_other_api.get("/api/v1/admin/schedules", headers=headers())
    missing_bootstrap_delete = restarted_other_api.delete(
        f"/api/v1/admin/schedules/{bootstrap_id}",
        headers=headers(),
    )
    deleted_other = restarted_other_api.delete(
        f"/api/v1/admin/schedules/{other_id}",
        headers=headers(),
    )
    restored_bootstrap = scheduler_client(
        RecordingScheduleSubmitter(),
        resource_service=resource_service,
    ).get("/api/v1/admin/schedules", headers=headers())

    assert restored_other.status_code == 200
    assert [item["id"] for item in restored_other.json()] == [other_id]
    assert missing_bootstrap_delete.status_code == 404
    assert deleted_other.status_code == 200
    assert (OTHER_TENANT_ID, "schedule", other_id) not in resource_service.payloads
    assert (TENANT_ID, "schedule", bootstrap_id) in resource_service.payloads
    assert restored_bootstrap.status_code == 200
    assert [item["id"] for item in restored_bootstrap.json()] == [bootstrap_id]
    assert ("audit", "schedule.create", OTHER_TENANT_ID, USER_ID) in resource_service.calls
    assert ("audit", "schedule.delete", OTHER_TENANT_ID, USER_ID) in resource_service.calls


def skill_archive() -> bytes:
    manifest = (
        "name: safe_skill\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3.12\n"
        "declared_tools:\n"
        "  - filesystem.read\n"
        "dependency_lock_hash: "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.yaml", manifest)
        archive.writestr("main.py", "print('ok')\n")
    return buffer.getvalue()


def skill_archive_variant(*, entry_body: str) -> bytes:
    manifest = (
        "name: safe_skill\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3.12\n"
        "declared_tools:\n"
        "  - filesystem.read\n"
        "dependency_lock_hash: "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.yaml", manifest)
        archive.writestr("main.py", entry_body)
    return buffer.getvalue()


def skill_tar_archive() -> bytes:
    manifest = (
        "name: safe_tar_skill\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3.12\n"
        "declared_tools: []\n"
        "dependency_lock_hash: "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in {
            "skill.yaml": manifest.encode("utf-8"),
            "main.py": b"print('ok')\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def skill_bundle_archive() -> bytes:
    buffer = io.BytesIO()
    skill_manifests = {
        "writer": (
            "name: writer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools:\n"
            "  - filesystem.read\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
        "reviewer": (
            "name: reviewer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools: []\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
    }
    with zipfile.ZipFile(buffer, "w") as archive:
        for folder, manifest in skill_manifests.items():
            archive.writestr(f"{folder}/skill.yaml", manifest)
            archive.writestr(f"{folder}/main.py", "print('ok')\n")
    return buffer.getvalue()


def wrapped_skill_tar_bundle_archive() -> bytes:
    skill_manifests = {
        "writer": (
            "name: wrapped_writer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools:\n"
            "  - filesystem.read\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
        "reviewer": (
            "name: wrapped_reviewer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools: []\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder, manifest in skill_manifests.items():
            for name, content in {
                f"all-skills/{folder}/skill.yaml": manifest.encode("utf-8"),
                f"all-skills/{folder}/main.py": b"print('ok')\n",
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def instruction_skill_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: codex-writer\ndescription: Draft structured research notes.\n---\n\nWrite concise notes.\n",
        )
    return buffer.getvalue()


def instruction_skill_archive_with_reference(reference_body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: codex-writer\ndescription: Draft structured research notes.\n---\n\nWrite concise notes.\n",
        )
        archive.writestr("references/guide.md", reference_body)
    return buffer.getvalue()


def instruction_skill_bundle_archive() -> bytes:
    skill_docs = {
        "research": "---\nname: research-writer\ndescription: Research writing.\n---\n\nWrite research notes.\n",
        "reviewer": "---\nname: reviewer-checklist\ndescription: Review checklist.\n---\n\nReview outputs.\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder, content in skill_docs.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"all-skills/{folder}/SKILL.md")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def large_nested_instruction_skill_bundle_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(99):
            skill_name = f"nested-instruction-skill-{index:03d}"
            content = (
                "---\n"
                f"name: {skill_name}\n"
                "description: Nested bundle regression.\n"
                "---\n\n"
                "Use this instruction skill from a wrapped all-skills archive.\n"
            ).encode()
            info = tarfile.TarInfo(f"all-skills_1/skills/{skill_name}/SKILL.md")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def large_flat_instruction_skill_bundle_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(99):
            skill_name = f"flat-instruction-skill-{index:03d}"
            archive.writestr(
                f"{skill_name}/SKILL.md",
                "---\n"
                f"name: {skill_name}\n"
                "description: Flat bundle regression.\n"
                "---\n\n"
                "Use this instruction skill from a flat skills.zip archive.\n",
            )
    return buffer.getvalue()


def instruction_bundle_with_rich_skill_directory_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "rich-skill/SKILL.md",
            "---\nname: rich-skill\ndescription: Skill with reference files.\n---\n\nUse this skill.\n",
        )
        for index in range(80):
            archive.writestr(
                f"rich-skill/references/note-{index:03d}.md",
                f"Reference note {index}.\n",
            )
        archive.writestr(
            "compact-skill/SKILL.md",
            "---\nname: compact-skill\ndescription: Compact bundled skill.\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def instruction_bundle_with_very_large_skill_directory_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "large-research-skill/SKILL.md",
            "---\nname: large-research-skill\ndescription: Skill with many reference files.\n---\n\nUse this skill.\n",
        )
        for index in range(320):
            archive.writestr(
                f"large-research-skill/references/source-{index:03d}.md",
                f"Reference source {index}.\n",
            )
        archive.writestr(
            "compact-neighbor-skill/SKILL.md",
            "---\nname: compact-neighbor-skill\ndescription: Neighbor skill.\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def instruction_bundle_with_non_slug_frontmatter_name_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "bianzheng-pingheng/SKILL.md",
            "---\nname: 辩证平衡\ndescription: 中文名称的 Skill。\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def instruction_bundle_with_hidden_nested_skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "aibiandao/SKILL.md",
            "---\nname: aibiandao\ndescription: Parent skill.\n---\n\nUse this skill.\n",
        )
        archive.writestr(
            "aibiandao/.worktrees/draft/SKILL.md",
            "---\nname: should-not-install\ndescription: Hidden worktree.\n---\n\nIgnore this worktree.\n",
        )
        archive.writestr("aibiandao/.worktrees/draft/notes.md", "temporary worktree note\n")
        archive.writestr("aibiandao/__pycache__/cached.cpython-314.pyc", b"cached")
        archive.writestr(
            "other-skill/SKILL.md",
            "---\nname: other-skill\ndescription: Other skill.\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def instruction_bundle_with_nested_example_skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "skills/nuwa/SKILL.md",
            "---\nname: nuwa\ndescription: Parent skill with examples.\n---\n\nUse this skill.\n",
        )
        archive.writestr(
            "skills/nuwa/examples/example-persona/SKILL.md",
            "---\nname: example-persona\ndescription: Nested example skill.\n---\n\nReference example.\n",
        )
        archive.writestr(
            "skills/nuwa/references/notes.md",
            "Reference notes for the parent skill.\n",
        )
        archive.writestr(
            "skills/other-skill/SKILL.md",
            "---\nname: other-skill\ndescription: Other skill.\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def large_phone_wrapped_instruction_skill_bundle_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(99):
            skill_name = f"phone-wrapped-skill-{index:03d}"
            files = {
                f"phone-export/all-skills_1/skills/{skill_name}/SKILL.md": (
                    "---\n"
                    f"name: {skill_name}\n"
                    "description: Phone wrapped bundle regression.\n"
                    "---\n\n"
                    "Use this instruction skill from a multi-layer phone archive.\n"
                ).encode(),
                f"phone-export/all-skills_1/skills/{skill_name}/references/note.md": (
                    f"Reference note {index}.\n"
                ).encode(),
            }
            for path, content in files.items():
                info = tarfile.TarInfo(path)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def phone_wrapped_instruction_bundle_with_tar_metadata_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        pax = tarfile.TarInfo("pax_global_header")
        pax.type = tarfile.XGLTYPE
        pax_data = b"24 comment=phone export\n"
        pax.size = len(pax_data)
        archive.addfile(pax, io.BytesIO(pax_data))
        for index in range(3):
            skill_name = f"phone-metadata-skill-{index:03d}"
            content = (
                "---\n"
                f"name: {skill_name}\n"
                "description: Phone export with tar metadata.\n"
                "---\n\n"
                "Use this instruction skill from a phone archive with tar metadata.\n"
            ).encode()
            info = tarfile.TarInfo(f"./all-skills_1/skills/{skill_name}/SKILL.md")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def partially_invalid_instruction_skill_bundle_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "valid-skill/SKILL.md",
            "---\nname: valid-bundle-skill\ndescription: Valid bundled skill.\n---\n\nUse this skill.\n",
        )
        archive.writestr(
            "invalid-skill/SKILL.md",
            "---\nname: invalid-bundle-skill\ndescription: Invalid bundled skill.\n---\n\nUse this skill.\n",
        )
        archive.writestr("invalid-skill/nested.zip", b"PK\x03\x04")
    return buffer.getvalue()


def test_model_pool_reports_serial_slot_and_queue_policy() -> None:
    response = client().post("/api/v1/admin/models", headers=headers(), json=model_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["upstream_model"] == "deepseek-chat"
    assert body["effective_slots"] == 1
    assert body["saturation_policy"] == "queue_first_then_fallback"


def test_model_effective_slots_apply_target_utilization() -> None:
    payload = {**model_payload(), "max_concurrency": 2, "target_utilization": 0.8}

    response = client().post("/api/v1/admin/models", headers=headers(), json=payload)

    assert response.status_code == 200
    assert response.json()["effective_slots"] == 1


def test_model_create_auto_infers_known_video_generation_capability() -> None:
    payload = {
        **model_payload(),
        "provider": "minimax",
        "upstream_model": "MiniMax-Hailuo-02",
        "logical_model": "video_primary",
        "capabilities": ["text"],
    }

    response = client().post("/api/v1/admin/models", headers=headers(), json=payload)

    assert response.status_code == 200
    assert response.json()["capabilities"] == ["text", "video_generation"]


def test_model_create_accepts_input_understanding_capabilities() -> None:
    payload = {
        **model_payload(),
        "capabilities": ["text", "vision", "audio", "tool_calling"],
    }

    response = client().post("/api/v1/admin/models", headers=headers(), json=payload)

    assert response.status_code == 200
    assert response.json()["capabilities"] == ["audio", "text", "tool_calling", "vision"]


def test_multimedia_generation_requires_feature_switch() -> None:
    response = client().post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "multimedia_generation_disabled"


def test_multimedia_video_generation_requires_video_capable_model() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    gateway = FakeGenerationGateway(
        error=NoCapableDeployment(
            "no capable deployment for logical model 'video_primary': video_generation"
        )
    )
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(gateway)

    response = api.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "model_capability_unavailable"
    assert gateway.requests[0].required_capabilities == frozenset(
        {ModelCapability.VIDEO_GENERATION}
    )


def test_multimedia_generation_daily_limit_returns_429() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    gateway = FakeGenerationGateway(
        error=MultimediaDailyLimitExceeded("daily multimedia generation limit exceeded")
    )
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(gateway)

    response = api.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "multimedia_daily_limit_exceeded"


def test_multimedia_generation_provider_failure_returns_502() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    gateway = FakeGenerationGateway(
        error=VideoProviderGenerationError(
            "MiniMax video submit failed: invalid api key", provider_code="2049"
        )
    )
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(gateway)

    response = api.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "multimedia_provider_failed"
    assert response.json()["error"]["details"] == {
        "provider_code": "2049",
        "reason": "MiniMax video submit failed: invalid api key",
    }


def test_admin_multimedia_routes_use_principal_tenant_executor_scope() -> None:
    api = create_app(auth_service=OtherTenantAuthService(), rate_limiter=object())
    service = TenantScopedAdminResourceService()
    tenant_service = service.for_principal(OTHER_TENANT_ID, USER_ID)
    tenant_service.settings = SystemSettingsResponse(multimedia_generation_enabled=True)
    executor = TenantAwareMultimediaExecutor()
    cast(Any, api).state.admin_resource_service = service
    cast(Any, api).state.multimedia_generation_executor = executor
    test_client = TestClient(api)

    submitted = test_client.post(
        "/api/v1/admin/multimedia/jobs",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a tenant video",
        },
    )
    job_id = submitted.json()["id"]
    fetched = test_client.get(f"/api/v1/admin/multimedia/jobs/{job_id}", headers=headers())
    ran = test_client.post(
        f"/api/v1/admin/multimedia/jobs/{job_id}/run",
        headers=headers(),
        json={"executor_id": "tenant_media_executor"},
    )
    generated = test_client.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "generate a tenant video",
        },
    )

    assert submitted.status_code == 202
    assert fetched.status_code == 200
    assert ran.status_code == 202
    assert generated.status_code == 202
    assert executor.calls == [
        ("for_tenant", OTHER_TENANT_ID, ""),
        ("submit", OTHER_TENANT_ID, "video_primary"),
        ("for_tenant", OTHER_TENANT_ID, ""),
        ("get_job", OTHER_TENANT_ID, job_id),
        ("for_tenant", OTHER_TENANT_ID, ""),
        ("run_job", OTHER_TENANT_ID, "tenant_media_executor"),
        ("for_tenant", OTHER_TENANT_ID, ""),
        ("generate", OTHER_TENANT_ID, "video_primary"),
    ]


def test_multimedia_generation_job_can_be_run_by_executor_agent_and_read_by_main_agent() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(
        FakeGenerationGateway()
    )

    submitted = api.post(
        "/api/v1/admin/multimedia/jobs",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert submitted.status_code == 202
    queued = submitted.json()
    assert queued["id"].startswith("media_")
    assert queued["status"] == "queued"
    assert queued["executor_id"] is None
    assert queued["artifacts"] == []

    completed = api.post(
        f"/api/v1/admin/multimedia/jobs/{queued['id']}/run",
        headers=headers(),
        json={"executor_id": "multimedia_generator"},
    )

    assert completed.status_code == 202
    body = completed.json()
    assert body["status"] == "succeeded"
    assert body["executor_id"] == "multimedia_generator"
    assert body["artifacts"] == [
        {
            "kind": "video",
            "uri": "artifact://generated-media",
            "text": "artifact://generated-media",
        }
    ]

    readable = api.get(f"/api/v1/admin/multimedia/jobs/{queued['id']}", headers=headers())

    assert readable.status_code == 200
    assert readable.json() == body


def test_main_agent_config_saves_dedicated_model_api_and_control_policy() -> None:
    api = client()

    updated = api.put(
        "/api/v1/admin/main-agent",
        headers=headers(),
        json={
            "model": {
                "provider": "openai-compatible",
                "api_base": "https://gsykj.com",
                "api_protocol": "openai_compatible",
                "upstream_model": "deepseek-chat",
                "credential_ref": "secret://main-agent",
                "capabilities": ["text", "tool_calling"],
                "max_concurrency": 3,
            },
            "control_mode": "supervisor",
            "decision_policy": "choose mode first, then roles; main agent makes the final decision",
            "hermes_policy": "confirm_before_apply",
            "max_review_rounds": 3,
        },
    )
    fetched = api.get("/api/v1/admin/main-agent", headers=headers())

    assert updated.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["model"]["provider"] == "openai-compatible"
    assert fetched.json()["model"]["api_base"] == "https://gsykj.com/v1"
    assert fetched.json()["model"]["api_protocol"] == "openai_compatible"
    assert fetched.json()["model"]["max_concurrency"] == 3
    assert fetched.json()["control_mode"] == "supervisor"
    assert fetched.json()["hermes_policy"] == "confirm_before_apply"


def test_main_agent_config_rejects_missing_dedicated_model_key() -> None:
    response = client().put(
        "/api/v1/admin/main-agent",
        headers=headers(),
        json={
            "model": {
                "provider": "openai-compatible",
                "api_base": "https://gsykj.com",
                "api_protocol": "openai_compatible",
                "upstream_model": "deepseek-chat",
                "credential_ref": "",
                "capabilities": ["text"],
            },
            "control_mode": "supervisor",
            "decision_policy": "use a dedicated main agent model",
            "hermes_policy": "observe",
            "max_review_rounds": 2,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation"


def test_secret_create_and_get_never_return_value_or_fingerprint() -> None:
    api = client()

    created = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "deepseek", "value": "sk-secret-value"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["last_four"] == "alue"
    assert "sk-secret-value" not in created.text
    assert "fingerprint" not in body

    fetched = api.get(f"/api/v1/admin/secrets/{body['ref']}", headers=headers())
    assert fetched.status_code == 200
    assert "sk-secret-value" not in fetched.text
    assert "fingerprint" not in fetched.json()


def test_duplicate_secret_is_rejected_by_fingerprint_without_disclosure() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "one", "value": "same-secret"},
    )
    second = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "two", "value": "same-secret"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "same-secret" not in second.text


def test_probe_returns_non_saturating_recommendation() -> None:
    response = client().post(
        "/api/v1/admin/models/probe",
        headers=headers(),
        json={"quota_scope": "deepseek_account_1", "desired_concurrency": 32},
    )

    assert response.status_code == 200
    assert response.json()["recommended_concurrency"] == 8
    assert "explicitly" in response.json()["warning"]


def test_draft_diff_publish_conflict_and_rollback() -> None:
    api = client()

    draft = api.put(
        "/api/v1/admin/config/draft",
        headers=headers(),
        json={"yaml": "models:\n  - planner\n"},
    )
    diff = api.post(
        "/api/v1/admin/config/diff",
        headers=headers(),
        json={"yaml": "models:\n  - planner\n"},
    )
    publish = api.post(
        "/api/v1/admin/config/publish",
        headers=headers(),
        json={"expected_version": 0},
    )
    conflict = api.post(
        "/api/v1/admin/config/publish",
        headers=headers(),
        json={"expected_version": 0},
    )
    rollback = api.post("/api/v1/admin/config/rollback/0", headers=headers())

    assert draft.json() == {"version": 0, "status": "draft"}
    assert diff.json()["changed"] == ["configuration"]
    assert publish.json() == {"version": 1, "status": "published"}
    assert conflict.status_code == 409
    assert rollback.json() == {"version": 0, "status": "rolled_back"}


def test_agent_and_workflow_crud() -> None:
    api = client()

    agent = api.post(
        "/api/v1/admin/agents",
        headers=headers(),
        json={"id": "planner", "name": "Planner", "enabled": True},
    )
    workflow = api.post(
        "/api/v1/admin/workflows",
        headers=headers(),
        json={"id": "dispatch", "name": "Dispatch", "enabled": True},
    )

    assert agent.status_code == 200
    assert workflow.status_code == 200
    assert api.get("/api/v1/admin/agents", headers=headers()).json()[0]["id"] == "planner"
    assert api.get("/api/v1/admin/workflows", headers=headers()).json()[0]["id"] == "dispatch"


def test_channel_status_exposes_feishu_setup_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_live")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret-live")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verify-live")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "encrypt-live")
    monkeypatch.setenv("FEISHU_TRANSPORT", "webhook")
    monkeypatch.setenv("FEISHU_COMMAND_ALIASES", "方案=//派单, 代码=//vi")
    monkeypatch.setenv("AGENT_HUB_PUBLIC_URL", "https://agent.example.com")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    by_id = {item["id"]: item for item in payload}
    assert by_id["feishu"]["status"] == "configured"
    assert (
        by_id["feishu"]["public_webhook_url"] == "https://agent.example.com/channels/feishu/events"
    )
    assert by_id["feishu"]["missing"] == []
    assert by_id["feishu"]["command_aliases"] == {}
    assert {
        "feishu",
        "dingtalk",
        "wecom_bot",
        "wecom_app",
        "wechat_official",
        "wechat_customer_service",
        "telegram",
        "slack",
        "qq",
        "custom_webhook",
    }.issubset(by_id)
    assert by_id["wechat_official"]["status"] == "missing_config"
    assert by_id["custom_webhook"]["status"] == "missing_config"
    assert by_id["wecom_app"]["name"] == "企业微信 Agent"
    serialized = response.text
    assert "自建应用" not in serialized
    assert "secret-live" not in serialized
    assert "verify-live" not in serialized
    assert "encrypt-live" not in serialized


def test_channel_status_supports_feishu_bot_template_app_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
        "FEISHU_TRANSPORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FEISHU_APP_TYPE", "bot_template")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_template")
    monkeypatch.setenv("FEISHU_APP_SECRET", "template-secret")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert by_id["feishu"]["status"] == "configured"
    assert by_id["feishu"]["missing"] == []
    assert by_id["feishu"]["public_webhook_url"] is None
    assert any("长连接" in note for note in by_id["feishu"]["notes"])
    assert "template-secret" not in response.text


def test_channel_status_defaults_feishu_to_websocket_two_parameter_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_TYPE",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
        "FEISHU_TRANSPORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_default")
    monkeypatch.setenv("FEISHU_APP_SECRET", "default-secret")
    monkeypatch.setenv("AGENT_HUB_PUBLIC_URL", "https://agent.example.com")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert by_id["feishu"]["status"] == "configured"
    assert by_id["feishu"]["missing"] == []
    assert by_id["feishu"]["transports"] == ["websocket"]
    assert by_id["feishu"]["configured"] == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert "AGENT_HUB_PUBLIC_URL" not in by_id["feishu"]["configured"]
    assert "FEISHU_VERIFICATION_TOKEN" not in by_id["feishu"]["configured"]
    assert any("长连接" in note for note in by_id["feishu"]["notes"])


def test_channel_status_treats_feishu_custom_app_token_as_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_TYPE",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_custom")
    monkeypatch.setenv("FEISHU_APP_SECRET", "custom-secret")
    monkeypatch.setenv("FEISHU_TRANSPORT", "webhook")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert by_id["feishu"]["status"] == "missing_config"
    assert by_id["feishu"]["missing"] == ["FEISHU_VERIFICATION_TOKEN", "AGENT_HUB_PUBLIC_URL"]
    assert any("Webhook" in note for note in by_id["feishu"]["notes"])


def test_channel_config_accepts_feishu_bot_template_app_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_APP_TYPE",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    api = client()
    response = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers=headers(),
        json={
            "values": {
                "FEISHU_APP_TYPE": "bot_template",
                "FEISHU_APP_ID": "cli_template",
                "FEISHU_APP_SECRET": "template-secret",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["saved"] == ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TYPE"]
    assert response.json()["status"]["status"] == "configured"
    assert response.json()["status"]["missing"] == []
    assert "template-secret" not in response.text


def test_channel_config_can_be_saved_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DINGTALK_APP_KEY", raising=False)
    monkeypatch.delenv("DINGTALK_APP_SECRET", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_TOKEN", raising=False)

    api = client()
    response = api.post(
        "/api/v1/admin/channels/dingtalk/config",
        headers=headers(),
        json={
            "values": {
                "DINGTALK_APP_KEY": "ding-app-key",
                "DINGTALK_APP_SECRET": "ding-secret",
                "DINGTALK_WEBHOOK_TOKEN": "ding-token",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["saved"] == [
        "DINGTALK_APP_KEY",
        "DINGTALK_APP_SECRET",
        "DINGTALK_WEBHOOK_TOKEN",
    ]
    assert response.json()["status"]["status"] == "configured"
    assert response.json()["status"]["missing"] == []
    assert "ding-secret" not in response.text
    channels = api.get("/api/v1/admin/channels", headers=headers())
    by_id = {item["id"]: item for item in channels.json()}
    assert by_id["dingtalk"]["status"] == "configured"
    assert by_id["dingtalk"]["missing"] == []


def test_channel_status_reports_configured_sources_after_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_WEBHOOK_TOKEN", "env-token")

    api = client()
    env_only = api.get("/api/v1/admin/channels", headers=headers())
    by_id = {item["id"]: item for item in env_only.json()}
    assert by_id["custom_webhook"]["configured"] == ["CUSTOM_WEBHOOK_TOKEN"]
    assert by_id["custom_webhook"]["configured_sources"] == {"CUSTOM_WEBHOOK_TOKEN": "environment"}

    saved = api.post(
        "/api/v1/admin/channels/custom_webhook/config",
        headers=headers(),
        json={"values": {"CUSTOM_WEBHOOK_TOKEN": "saved-token"}},
    )
    assert saved.status_code == 200
    assert saved.json()["status"]["configured_sources"] == {"CUSTOM_WEBHOOK_TOKEN": "saved"}

    cleared = api.delete("/api/v1/admin/channels/custom_webhook/config", headers=headers())

    assert cleared.status_code == 200
    assert cleared.json()["saved"] == []
    assert cleared.json()["status"]["status"] == "configured"
    assert cleared.json()["status"]["configured_sources"] == {"CUSTOM_WEBHOOK_TOKEN": "environment"}


def test_channel_config_can_be_cleared_after_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CUSTOM_WEBHOOK_TOKEN", raising=False)

    api = client()
    saved = api.post(
        "/api/v1/admin/channels/custom_webhook/config",
        headers=headers(),
        json={"values": {"CUSTOM_WEBHOOK_TOKEN": "saved-token"}},
    )
    assert saved.status_code == 200
    assert saved.json()["status"]["status"] == "configured"

    cleared = api.delete(
        "/api/v1/admin/channels/custom_webhook/config",
        headers=headers(),
    )

    assert cleared.status_code == 200
    assert cleared.json()["id"] == "custom_webhook"
    assert cleared.json()["saved"] == []
    assert cleared.json()["status"]["status"] == "missing_config"
    assert cleared.json()["status"]["missing"] == ["CUSTOM_WEBHOOK_TOKEN"]

    channels = api.get("/api/v1/admin/channels", headers=headers())
    by_id = {item["id"]: item for item in channels.json()}
    assert by_id["custom_webhook"]["status"] == "missing_config"
    assert by_id["custom_webhook"]["missing"] == ["CUSTOM_WEBHOOK_TOKEN"]

    audit = api.get("/api/v1/admin/audit?action=channel.clear", headers=headers())
    assert audit.status_code == 200
    assert audit.json()[0]["resource"] == "channel:custom_webhook"


def test_channel_config_save_and_clear_refresh_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    application = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    application.state.admin_resource_service = InMemoryAdminResourceService()
    refreshes: list[dict[str, str]] = []

    async def refresh_channel_runtime_config(config: dict[str, str]) -> None:
        refreshes.append(dict(config))

    application.state.refresh_channel_runtime_config = refresh_channel_runtime_config
    api = TestClient(application)

    saved = api.post(
        "/api/v1/admin/channels/feishu/config",
        headers=headers(),
        json={
            "values": {
                "FEISHU_TRANSPORT": "websocket",
                "FEISHU_APP_ID": "cli_live",
                "FEISHU_APP_SECRET": "live-secret",
            }
        },
    )

    assert saved.status_code == 200
    assert refreshes[-1] == {
        "FEISHU_TRANSPORT": "websocket",
        "FEISHU_APP_ID": "cli_live",
        "FEISHU_APP_SECRET": "live-secret",
    }

    cleared = api.delete("/api/v1/admin/channels/feishu/config", headers=headers())

    assert cleared.status_code == 200
    assert refreshes[-1] == {}


def test_all_channel_statuses_are_configured_when_required_env_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_APP_TYPE",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
        "DINGTALK_APP_KEY",
        "DINGTALK_APP_SECRET",
        "DINGTALK_WEBHOOK_TOKEN",
        "WECOM_BOT_WEBHOOK_KEY",
        "WECOM_BOT_WEBHOOK_TOKEN",
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_SECRET",
        "WECOM_TOKEN",
        "WECHATMP_APP_ID",
        "WECHATMP_APP_SECRET",
        "WECHATMP_TOKEN",
        "WECHAT_KF_CORP_ID",
        "WECHAT_KF_SECRET",
        "WECHAT_KF_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "QQ_BOT_APP_ID",
        "QQ_BOT_TOKEN",
        "QQ_WEBHOOK_TOKEN",
        "CUSTOM_WEBHOOK_TOKEN",
    ):
        monkeypatch.setenv(name, "configured")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert {item["status"] for item in payload} == {"configured"}
    assert all(item["missing"] == [] for item in payload)


def test_operational_run_listing_details_and_controls() -> None:
    api = client()

    runs = api.get("/api/v1/admin/runs", headers=headers())
    assert runs.status_code == 200
    run_id = runs.json()[0]["id"]
    assert runs.json()[0]["queue_wait_ms"] >= 0
    assert runs.json()[0]["capacity_wait_ms"] >= 0
    assert runs.json()[0]["cost_usd"] == "0.0132"
    assert runs.json()[0]["request"] == "Summarize current deployment readiness."
    assert runs.json()[0]["version"] == 1

    detail = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())
    pause = api.post(f"/api/v1/admin/runs/{run_id}/pause", headers=headers())
    resume = api.post(f"/api/v1/admin/runs/{run_id}/resume", headers=headers())
    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())

    assert detail.status_code == 200
    assert detail.json()["mode"] == "dispatch"
    assert detail.json()["version"] == 1
    assert detail.json()["events"][0]["kind"] == "queued"
    assert detail.json()["artifacts"][0]["title"] == "Readiness report"
    assert pause.json()["status"] == "paused"
    assert resume.json()["status"] == "running"
    assert cancel.json()["status"] == "cancelled"


def test_operational_run_delete_removes_cancelled_conversation() -> None:
    api = client()
    run_id = api.get("/api/v1/admin/runs", headers=headers()).json()[0]["id"]

    active_delete = api.delete(f"/api/v1/admin/runs/{run_id}", headers=headers())
    assert active_delete.status_code == 409
    assert active_delete.json()["error"]["code"] == "run_conflict"

    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())
    assert cancel.status_code == 200

    deleted = api.delete(f"/api/v1/admin/runs/{run_id}", headers=headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"id": run_id, "deleted": True}

    missing_detail = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())
    assert missing_detail.status_code == 404
    remaining = api.get("/api/v1/admin/runs", headers=headers())
    assert all(item["id"] != run_id for item in remaining.json())


def test_operational_run_bulk_delete_uses_existing_delete_rules() -> None:
    api = client()
    run_id = api.get("/api/v1/admin/runs", headers=headers()).json()[0]["id"]

    blocked = api.post(
        "/api/v1/admin/runs/bulk-delete",
        headers=headers(),
        json={"ids": [run_id]},
    )
    assert blocked.status_code == 200
    assert blocked.json()["deleted"] == []
    assert blocked.json()["failed"][0]["id"] == run_id
    assert blocked.json()["failed"][0]["code"] == "run_conflict"

    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())
    assert cancel.status_code == 200
    deleted = api.post(
        "/api/v1/admin/runs/bulk-delete",
        headers=headers(),
        json={"ids": [run_id]},
    )

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == [{"id": run_id, "deleted": True}]
    assert deleted.json()["failed"] == []


def test_operational_run_bulk_delete_accepts_large_selection() -> None:
    api = client()
    ids = [str(uuid4()) for _ in range(101)]

    response = api.post(
        "/api/v1/admin/runs/bulk-delete",
        headers=headers(),
        json={"ids": ids},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == []
    assert [item["id"] for item in response.json()["failed"]] == ids
    assert {item["code"] for item in response.json()["failed"]} == {"not_found"}


def test_admin_run_artifact_exposes_safe_text_for_chat_reply() -> None:
    artifact = _admin_run_artifact(
        {
            "id": "artifact-1",
            "type": "text",
            "producer": "final_synthesizer",
            "content": {"text": "这是可以直接显示在对话里的最终回答。"},
        }
    )

    assert artifact.kind == "text"
    assert artifact.title == "final_synthesizer"
    assert artifact.text == "这是可以直接显示在对话里的最终回答。"


def test_admin_run_artifact_does_not_expose_sensitive_text() -> None:
    artifact = _admin_run_artifact(
        {
            "id": "artifact-2",
            "type": "text",
            "producer": "final_synthesizer",
            "content": {"text": "api_key=sk-secret-value"},
        }
    )

    assert artifact.text is None


def test_admin_run_artifact_exposes_safe_generated_file_metadata() -> None:
    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    artifact = _admin_run_artifact(
        {
            "id": str(artifact_id),
            "type": "tool_result",
            "producer": "document_writer",
            "content": {
                "result": {
                    "file": {
                        "artifact_id": str(artifact_id),
                        "filename": "delivery-plan.docx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "size_bytes": 123,
                        "sha256": "a" * 64,
                        "download_url": f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download",
                    },
                    "metadata": {
                        "artifact_id": str(artifact_id),
                        "filename": "delivery-plan.docx",
                        "storage_key": "tenant/run/artifact/delivery-plan.docx",
                    },
                }
            },
        },
        run_id=run_id,
    )

    assert artifact.filename == "delivery-plan.docx"
    assert artifact.mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert artifact.size_bytes == 123
    assert artifact.sha256 == "a" * 64
    assert artifact.download_url == f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download"
    assert "storage_key" not in artifact.model_dump_json()


def test_admin_run_artifact_defaults_step_generated_file_to_step_detail() -> None:
    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    artifact = _admin_run_artifact(
        {
            "id": str(artifact_id),
            "type": "tool_result",
            "producer": "document_writer",
            "content": {
                "result": {
                    "file": {
                        "artifact_id": str(artifact_id),
                        "filename": "draft-plan.docx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    }
                },
                "step_id": "draft-doc",
            },
        },
        run_id=run_id,
    )

    assert artifact.presentation == "step_detail"


def test_admin_run_artifact_keeps_explicit_final_attachment_presentation() -> None:
    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    artifact = _admin_run_artifact(
        {
            "id": str(artifact_id),
            "type": "tool_result",
            "producer": "final_synthesizer",
            "content": {
                "result": {
                    "file": {
                        "artifact_id": str(artifact_id),
                        "filename": "delivery-plan.docx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    }
                },
                "presentation": "final_attachment",
            },
        },
        run_id=run_id,
    )

    assert artifact.presentation == "final_attachment"


def test_admin_run_artifacts_deduplicate_reused_generated_downloads() -> None:
    from agent_hub.api.routers.admin import _admin_run_artifacts

    run_id = UUID("33333333-3333-4333-8333-333333333333")
    stored_artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    download_url = f"/api/v1/runs/{run_id}/artifacts/{stored_artifact_id}/download"
    artifacts: list[dict[str, object]] = [
        {
            "id": "55555555-5555-4555-8555-555555555555",
            "type": "tool_result",
            "producer": "implementer",
            "content": {
                "result": {
                    "file": {
                        "artifact_id": str(stored_artifact_id),
                        "filename": "mofang-main.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 135,
                        "download_url": download_url,
                    },
                    "presentation": "final_attachment",
                }
            },
        },
        {
            "id": "66666666-6666-4666-8666-666666666666",
            "type": "tool_result",
            "producer": "implementer",
            "content": {
                "result": {
                    "file": {
                        "artifact_id": str(stored_artifact_id),
                        "filename": "mofang-main.zip",
                        "mime_type": "application/zip",
                        "size_bytes": 135,
                        "download_url": download_url,
                    },
                    "presentation": "final_attachment",
                }
            },
        },
    ]

    responses = _admin_run_artifacts(artifacts, run_id=run_id)

    assert len(responses) == 1
    assert responses[0].download_url == download_url


@pytest.mark.parametrize(
    ("filename", "mime_type", "data"),
    [
        (
            "delivery-plan.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"docx-bytes",
        ),
        (
            "launch-review.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            b"pptx-bytes",
        ),
        ("chart.png", "image/png", b"\x89PNG\r\n\x1a\n"),
        ("bundle.zip", "application/zip", b"PK\x03\x04"),
    ],
)
def test_download_run_artifact_returns_generated_file(
    tmp_path: Path, filename: str, mime_type: str, data: bytes
) -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    path = tmp_path / filename
    path.write_bytes(data)
    service.generated_artifacts[(run_id, artifact_id)] = (
        path,
        filename,
        mime_type,
    )

    response = api.get(
        f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download",
        headers=headers(),
    )

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"].startswith(mime_type)


def test_download_run_artifact_rejects_mime_extension_mismatch(tmp_path: Path) -> None:
    api = client()
    service = cast(InMemoryAdminResourceService, cast(Any, api.app).state.admin_resource_service)
    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444444")
    path = tmp_path / "installer.exe"
    path.write_bytes(b"not-an-image")
    service.generated_artifacts[(run_id, artifact_id)] = (
        path,
        "installer.exe",
        "image/png",
    )

    response = api.get(
        f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download",
        headers=headers(),
    )

    assert response.status_code == 404


def _completed_run_record(run_id: UUID) -> RunRecord:
    return RunRecord(
        id=run_id,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        request="Generate a file.",
        mode=TaskMode.DISPATCH,
        status=RunStatus.COMPLETED,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision={},
    )


class GeneratedArtifactRunRepository:
    def __init__(self, run_id: UUID, artifacts: tuple[dict[str, object], ...]) -> None:
        self.run_id = run_id
        self.artifacts_payload = artifacts
        self.deleted_run_id: UUID | None = None

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        assert tenant_id == TENANT_ID
        if run_id != self.run_id:
            raise KeyError(run_id)
        return _completed_run_record(run_id)

    async def raw_artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return self.artifacts_payload

    async def delete_run(self, tenant_id: UUID, run_id: UUID) -> None:
        assert tenant_id == TENANT_ID
        if run_id != self.run_id:
            raise KeyError(run_id)
        self.deleted_run_id = run_id


@pytest.mark.asyncio
async def test_persistent_download_matches_nested_generated_file_artifact_id(tmp_path: Path) -> None:
    from agent_hub.files.generated import DOCX_MIME_TYPE, GeneratedFileStore

    run_id = UUID("33333333-3333-4333-8333-333333333333")
    wrapper_artifact_id = UUID("44444444-4444-4444-8444-444444444441")
    file_artifact_id = UUID("44444444-4444-4444-8444-444444444442")
    metadata = GeneratedFileStore(tmp_path).store_bytes(
        TENANT_ID,
        run_id,
        file_artifact_id,
        "delivery-plan.docx",
        DOCX_MIME_TYPE,
        b"docx-bytes",
    )
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=GeneratedArtifactRunRepository(
            run_id,
            (
                {
                    "id": str(wrapper_artifact_id),
                    "type": "tool_result",
                    "producer": "writer",
                    "content": {
                        "result": {
                            "file": {
                                **metadata.to_public_dict(),
                                "artifact_id": str(file_artifact_id),
                            },
                            "metadata": {
                                **metadata.to_content_file(),
                                "artifact_id": str(file_artifact_id),
                            },
                        }
                    },
                },
            ),
        ),  # type: ignore[arg-type]
        generated_artifact_dir=tmp_path,
    )

    download = await service.download_run_artifact(run_id, file_artifact_id)

    assert download.filename == "delivery-plan.docx"
    assert download.mime_type == DOCX_MIME_TYPE
    assert download.path.read_bytes() == b"docx-bytes"


@pytest.mark.asyncio
async def test_persistent_delete_run_cleans_generated_artifact_files(tmp_path: Path) -> None:
    from agent_hub.files.generated import DOCX_MIME_TYPE, PPTX_MIME_TYPE, GeneratedFileStore

    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444442")
    sibling_run_id = UUID("33333333-3333-4333-8333-333333333334")
    sibling_artifact_id = UUID("44444444-4444-4444-8444-444444444443")
    store = GeneratedFileStore(tmp_path)
    metadata = store.store_bytes(
        TENANT_ID,
        run_id,
        artifact_id,
        "delivery-plan.docx",
        DOCX_MIME_TYPE,
        b"docx-bytes",
    )
    sibling_metadata = store.store_bytes(
        TENANT_ID,
        sibling_run_id,
        sibling_artifact_id,
        "launch-review.pptx",
        PPTX_MIME_TYPE,
        b"pptx-bytes",
    )
    repository = GeneratedArtifactRunRepository(run_id, ())
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=repository,  # type: ignore[arg-type]
        generated_artifact_dir=tmp_path,
    )

    deleted = await service.delete_run(run_id)

    assert deleted.id == run_id
    assert deleted.deleted is True
    assert repository.deleted_run_id == run_id
    with pytest.raises(FileNotFoundError):
        store.resolve(metadata.storage_key)
    assert store.resolve(sibling_metadata.storage_key).read_bytes() == b"pptx-bytes"


@pytest.mark.asyncio
async def test_persistent_download_rejects_non_generated_artifacts(tmp_path: Path) -> None:
    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444442")
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=GeneratedArtifactRunRepository(
            run_id,
            (
                {
                    "id": str(artifact_id),
                    "type": "text",
                    "producer": "writer",
                    "content": {"text": "not a generated file"},
                },
            ),
        ),  # type: ignore[arg-type]
        generated_artifact_dir=tmp_path,
    )

    with pytest.raises(KeyError):
        await service.download_run_artifact(run_id, artifact_id)


@pytest.mark.asyncio
async def test_persistent_download_rejects_generated_files_without_storage_key(
    tmp_path: Path,
) -> None:
    from agent_hub.files.generated import DOCX_MIME_TYPE

    run_id = UUID("33333333-3333-4333-8333-333333333333")
    artifact_id = UUID("44444444-4444-4444-8444-444444444442")
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=GeneratedArtifactRunRepository(
            run_id,
            (
                {
                    "id": str(artifact_id),
                    "type": "tool_result",
                    "producer": "writer",
                    "content": {
                        "result": {
                            "file": {
                                "artifact_id": str(artifact_id),
                                "filename": "delivery-plan.docx",
                                "mime_type": DOCX_MIME_TYPE,
                            },
                            "metadata": {
                                "artifact_id": str(artifact_id),
                                "filename": "delivery-plan.docx",
                            },
                        }
                    },
                },
            ),
        ),  # type: ignore[arg-type]
        generated_artifact_dir=tmp_path,
    )

    with pytest.raises(KeyError):
        await service.download_run_artifact(run_id, artifact_id)


@pytest.mark.asyncio
async def test_persistent_download_rejects_cross_context_storage_key(tmp_path: Path) -> None:
    from agent_hub.files.generated import DOCX_MIME_TYPE, GeneratedFileStore

    run_id = UUID("33333333-3333-4333-8333-333333333333")
    requested_artifact_id = UUID("44444444-4444-4444-8444-444444444442")
    stored_artifact_id = UUID("44444444-4444-4444-8444-444444444443")
    metadata = GeneratedFileStore(tmp_path).store_bytes(
        TENANT_ID,
        run_id,
        stored_artifact_id,
        "delivery-plan.docx",
        DOCX_MIME_TYPE,
        b"docx-bytes",
    )
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=GeneratedArtifactRunRepository(
            run_id,
            (
                {
                    "id": str(requested_artifact_id),
                    "type": "tool_result",
                    "producer": "writer",
                    "content": {
                        "result": {
                            "file": {
                                **metadata.to_public_dict(),
                                "artifact_id": str(requested_artifact_id),
                            },
                            "metadata": {
                                **metadata.to_content_file(),
                                "artifact_id": str(requested_artifact_id),
                            },
                        }
                    },
                },
            ),
        ),  # type: ignore[arg-type]
        generated_artifact_dir=tmp_path,
    )

    with pytest.raises(KeyError):
        await service.download_run_artifact(run_id, requested_artifact_id)


def test_conversation_can_be_loaded_by_session_id() -> None:
    api = client()

    response = api.get("/api/v1/admin/conversations/conv-readiness", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation_id"] == "conv-readiness"
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["request"] == "Summarize current deployment readiness."


@pytest.mark.asyncio
async def test_persistent_admin_conversation_keeps_chronological_messages() -> None:
    first_id = UUID("33333333-3333-4333-8333-333333333331")
    second_id = UUID("33333333-3333-4333-8333-333333333332")

    class FakeRunRepository:
        async def list_recent(self, tenant_id: UUID, *, limit: int = 100) -> tuple[RunRecord, ...]:
            assert tenant_id == TENANT_ID
            assert limit == 200
            return (
                RunRecord(
                    id=second_id,
                    tenant_id=TENANT_ID,
                    actor_id=ACTOR_ID,
                    request="第二轮：继续细化方案",
                    mode=TaskMode.DISPATCH,
                    status=RunStatus.COMPLETED,
                    version=1,
                    created_at=datetime.now(UTC),
                    routing_decision={"conversation_id": "conv-multi-turn"},
                ),
                RunRecord(
                    id=first_id,
                    tenant_id=TENANT_ID,
                    actor_id=ACTOR_ID,
                    request="第一轮：先做方案",
                    mode=TaskMode.DISPATCH,
                    status=RunStatus.COMPLETED,
                    version=1,
                    created_at=datetime.now(UTC),
                    routing_decision={"conversation_id": "conv-multi-turn"},
                ),
            )

        async def usage_cost(self, tenant_id: UUID, run_id: UUID) -> str:
            assert tenant_id == TENANT_ID
            assert run_id in {first_id, second_id}
            return "0"

        async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
            assert tenant_id == TENANT_ID
            assert run_id in {first_id, second_id}
            return ()

        async def artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
            assert tenant_id == TENANT_ID
            assert run_id in {first_id, second_id}
            return ()

    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=FakeRunRepository(),  # type: ignore[arg-type]
    )

    conversation = await service.get_conversation("conv-multi-turn")

    assert [run.request for run in conversation.runs] == [
        "第一轮：先做方案",
        "第二轮：继续细化方案",
    ]


def test_skill_upload_approve_mcp_memory_and_audit_are_safe() -> None:
    api = client()

    created_agent = api.post(
        "/api/v1/admin/agents",
        headers=headers(),
        json={
            "id": "smoke-agent",
            "name": "Smoke Agent",
            "role": "reviewer",
            "prompt": "Review safely.",
            "model": "planner",
            "skills": [],
        },
    )
    deleted_agent = api.delete("/api/v1/admin/agents/smoke-agent", headers=headers())
    agents = api.get("/api/v1/admin/agents", headers=headers())
    created_workflow = api.post(
        "/api/v1/admin/workflows",
        headers=headers(),
        json={
            "id": "smoke-workflow",
            "name": "Smoke Workflow",
            "mode": "dispatch",
            "agent_ids": ["planner"],
            "objective": "Smoke test workflow.",
        },
    )
    deleted_workflow = api.delete("/api/v1/admin/workflows/smoke-workflow", headers=headers())
    workflows = api.get("/api/v1/admin/workflows", headers=headers())
    uploaded = api.post(
        "/api/v1/admin/skills",
        headers=headers(),
        json={"filename": "safe-skill.zip"},
    )
    approved = api.post(
        f"/api/v1/admin/skills/{uploaded.json()['id']}/approve",
        headers=headers(),
    )
    deleted_skill = api.delete(
        f"/api/v1/admin/skills/{uploaded.json()['id']}",
        headers=headers(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())
    created_mcp = api.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={"id": "browser", "name": "Browser MCP", "allowed_tools": ["open_page"]},
    )
    deleted_mcp = api.delete("/api/v1/admin/mcp/browser", headers=headers())
    mcp = api.get("/api/v1/admin/mcp", headers=headers())
    created_memory = api.post(
        "/api/v1/admin/memory",
        headers=headers(),
        json={
            "id": "logging-policy",
            "scope": "tenant",
            "value": "Default production log collection level is warning.",
        },
    )
    memory = api.get("/api/v1/admin/memory", headers=headers())
    updated_memory = api.patch(
        f"/api/v1/admin/memory/{memory.json()[0]['id']}",
        headers=headers(),
        json={"value": "Updated non-dangerous operation policy."},
    )
    audit = api.get("/api/v1/admin/audit?action=config.publish", headers=headers())

    assert created_agent.status_code == 200
    assert deleted_agent.json()["status"] == "deleted"
    assert all(item["id"] != "smoke-agent" for item in agents.json())
    assert created_workflow.status_code == 200
    assert deleted_workflow.json()["status"] == "deleted"
    assert all(item["id"] != "smoke-workflow" for item in workflows.json())
    assert uploaded.json()["status"] == "quarantined"
    assert approved.json()["status"] == "enabled"
    assert deleted_skill.json()["status"] == "deleted"
    assert all(item["id"] != uploaded.json()["id"] for item in skills.json())
    assert created_mcp.json()["health"] == "configured"
    assert deleted_mcp.json()["status"] == "deleted"
    assert all(item["id"] != "browser" for item in mcp.json())
    assert created_memory.json()["id"] == "logging-policy"
    assert any(item["id"] == "logging-policy" for item in memory.json())
    assert updated_memory.json()["value"] == "Updated non-dangerous operation policy."
    assert audit.json()[0]["action"] == "config.publish"
    serialized = uploaded.text + approved.text + skills.text + mcp.text + memory.text + audit.text
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


def test_memory_api_exposes_hermes_plus_fields_and_lock_controls() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/memory",
        headers=headers(),
        json={
            "id": "hermes-plus-policy",
            "scope": "cube-agent",
            "value": "Hermes+ must finish before harness refactor.",
            "heat": 0.7,
            "project_id": "cube-agent",
            "conversation_id": "handoff",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["heat"] == 0.7
    assert body["locked"] is False
    assert body["summary_period"] == "none"

    locked = api.post("/api/v1/admin/memory/hermes-plus-policy/lock", headers=headers())
    assert locked.status_code == 200
    assert locked.json()["locked"] is True

    unlocked = api.post("/api/v1/admin/memory/hermes-plus-policy/unlock", headers=headers())
    assert unlocked.status_code == 200
    assert unlocked.json()["locked"] is False


def test_unified_logs_include_audit_model_mode_and_feature_errors() -> None:
    api = client()
    app = cast(Any, api.app)
    service = cast(
        InMemoryAdminResourceService,
        app.state.admin_resource_service,
    )

    service.logs.extend(
        [
            service.make_log(
                category="model_error",
                level="error",
                title="模型可用性测试失败",
                message="provider returned status=401",
                source="models.create",
                details={"provider": "deepseek", "status_code": "401"},
            ),
            service.make_log(
                category="mode_error",
                level="error",
                title="模式运行失败",
                message="dispatch runtime failed",
                source="runs.execute",
                details={"mode": "dispatch"},
            ),
            service.make_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={"feature": "skills"},
            ),
            service.make_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent model is required",
                source="agents.upsert",
                details={"agent_id": "director", "reason": "missing_model"},
            ),
        ]
    )
    asyncio.run(
        service.record_log(
            category="feature_error",
            level="info",
            title="正常运行流水",
            message="this normal trace must not be collected",
            source="feature.normal",
        )
    )

    all_logs = api.get("/api/v1/admin/logs", headers=headers())
    model_logs = api.get("/api/v1/admin/logs?category=model_error", headers=headers())
    channel_logs = api.get("/api/v1/admin/logs?category=channel_error", headers=headers())

    assert all_logs.status_code == 200
    categories = {item["category"] for item in all_logs.json()}
    assert {
        "audit",
        "model_error",
        "mode_error",
        "feature_error",
        "agent_error",
        "channel_error",
    } <= categories
    assert model_logs.status_code == 200
    assert [item["category"] for item in model_logs.json()] == ["model_error"]
    assert channel_logs.status_code == 200
    assert channel_logs.json()
    assert all(item["level"] == "warning" for item in channel_logs.json())
    serialized = all_logs.text
    assert "this normal trace must not be collected" not in serialized
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


def test_skill_archive_upload_scans_real_zip_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is False
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["name"] == "safe_skill"
    assert item["status"] == "scanned"
    assert item["requested_permissions"] == ["tool:filesystem.read"]
    assert any("content sha256" in entry for entry in item["scan_diff"])
    assert skills.json()[0]["id"] == item["id"]


def test_skill_archive_upload_requires_choice_for_same_name_new_content() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('one')\n"),
    )
    conflict = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('two')\n"),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert first.status_code == 200
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"]["code"] == "skill_version_choice_required"
    assert body["error"]["details"]["skill_name"] == "safe_skill"
    assert body["error"]["details"]["current_version_id"] == first.json()["items"][0]["id"]
    assert body["error"]["details"]["new_content_sha256"]
    assert len(skills.json()) == 1


def test_skill_archive_upload_overwrites_same_name_when_requested() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('one')\n"),
    )
    overwritten = api.post(
        "/api/v1/admin/skills/upload?strategy=overwrite",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('two')\n"),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert first.status_code == 200
    assert overwritten.status_code == 200
    first_item = first.json()["items"][0]
    overwritten_item = overwritten.json()["items"][0]
    assert overwritten_item["id"] == first_item["id"]
    assert overwritten_item["content_sha256"] != first_item["content_sha256"]
    listed = skills.json()
    assert len(listed) == 1
    assert listed[0]["id"] == first_item["id"]
    assert listed[0]["current_version_id"] == first_item["id"]
    assert len(listed[0]["versions"]) == 1


def test_skill_archive_upload_saves_same_name_as_new_version_when_requested() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('one')\n"),
    )
    second = api.post(
        "/api/v1/admin/skills/upload?strategy=new_version",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('two')\n"),
    )
    repeated = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill-copy.zip"},
        content=skill_archive_variant(entry_body="print('two')\n"),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert repeated.status_code == 200
    first_item = first.json()["items"][0]
    second_item = second.json()["items"][0]
    assert second_item["id"] != first_item["id"]
    assert repeated.json()["items"][0]["id"] == second_item["id"]
    listed = skills.json()
    assert len(listed) == 1
    assert listed[0]["id"] == second_item["id"]
    assert listed[0]["current_version_id"] == second_item["id"]
    assert [version["id"] for version in listed[0]["versions"]] == [
        second_item["id"],
        first_item["id"],
    ]


def test_skill_version_activation_switches_current_version() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('one')\n"),
    ).json()["items"][0]
    second = api.post(
        "/api/v1/admin/skills/upload?strategy=new_version",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('two')\n"),
    ).json()["items"][0]

    activated = api.post(
        f"/api/v1/admin/skills/{second['id']}/versions/{first['id']}/activate",
        headers=headers(),
    )

    assert activated.status_code == 200
    body = activated.json()
    assert body["id"] == first["id"]
    assert body["current_version_id"] == first["id"]
    assert [version["is_current"] for version in body["versions"]] == [False, True]


def test_skill_strategy_upload_and_activation_require_approval_permission() -> None:
    class OperatorAuthService:
        def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
            if token != "operator-token":
                raise InvalidCredentials("bad token")
            return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.OPERATOR)

    resource_service = InMemoryAdminResourceService()
    admin_app = create_app(auth_service=StubAuthService(), rate_limiter=object())
    admin_app.state.admin_resource_service = resource_service
    admin_api = TestClient(admin_app)
    app = create_app(auth_service=OperatorAuthService(), rate_limiter=object())
    app.state.admin_resource_service = resource_service
    api = TestClient(app)
    limited_headers = {"Authorization": "Bearer operator-token"}

    first = admin_api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('one')\n"),
    )
    overwrite = api.post(
        "/api/v1/admin/skills/upload?strategy=overwrite",
        headers={**limited_headers, "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive_variant(entry_body="print('two')\n"),
    )
    activate = api.post(
        f"/api/v1/admin/skills/{first.json()['items'][0]['id']}/versions/{first.json()['items'][0]['id']}/activate",
        headers=limited_headers,
    )

    assert first.status_code == 200
    assert overwrite.status_code == 403
    assert activate.status_code == 403


def test_skill_archive_upload_accepts_percent_encoded_filename_header() -> None:
    api = client()
    filename = "技能包.zip"

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={
            **headers(),
            "X-Agent-Hub-Skill-Filename": quote(filename, safe=""),
            "X-Agent-Hub-Skill-Filename-Encoding": "percent",
        },
        content=skill_archive(),
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["items"][0]["name"] == "safe_skill"


def test_skill_archive_upload_accepts_real_tar_gz_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-tar-skill.tar.gz"},
        content=skill_tar_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is False
    assert body["items"][0]["name"] == "safe_tar_skill"
    assert body["items"][0]["status"] == "scanned"
    assert any(item["id"] == body["items"][0]["id"] for item in skills.json())


def test_skill_archive_upload_scans_bundle_with_multiple_skill_directories() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.zip"},
        content=skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["writer_skill", "reviewer_skill"]
    assert body["items"][0]["requested_permissions"] == ["tool:filesystem.read"]
    assert {item["name"] for item in skills.json()} == {"writer_skill", "reviewer_skill"}


def test_skill_archive_upload_scans_wrapped_tar_gz_bundle_with_multiple_skill_directories() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.tar.gz"},
        content=wrapped_skill_tar_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == [
        "wrapped_writer_skill",
        "wrapped_reviewer_skill",
    ]
    assert body["items"][0]["requested_permissions"] == ["tool:filesystem.read"]
    assert {item["name"] for item in skills.json()} == {
        "wrapped_writer_skill",
        "wrapped_reviewer_skill",
    }


def test_skill_archive_upload_accepts_instruction_only_skill_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "codex-writer-skill.zip"},
        content=instruction_skill_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is False
    assert body["items"][0]["name"] == "codex-writer"
    assert body["items"][0]["requested_permissions"] == []
    assert "SKILL.md detected" in body["items"][0]["scan_diff"]
    assert any(item["name"] == "codex-writer" for item in skills.json())


def test_instruction_skill_hash_changes_when_reference_file_changes() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "codex-writer-skill.zip"},
        content=instruction_skill_archive_with_reference("Original reference.\n"),
    )
    second = api.post(
        "/api/v1/admin/skills/upload?strategy=overwrite",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "codex-writer-skill.zip"},
        content=instruction_skill_archive_with_reference("Updated reference.\n"),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert first.status_code == 200
    assert second.status_code == 200
    first_item = first.json()["items"][0]
    second_item = second.json()["items"][0]
    assert first_item["id"] == second_item["id"]
    assert first_item["content_sha256"] != second_item["content_sha256"]
    assert first_item["package_version_id"] != second_item["package_version_id"]
    assert second_item["source_filename"] == "codex-writer-skill.zip"
    listed = skills.json()
    assert len(listed) == 1
    assert listed[0]["content_sha256"] == second_item["content_sha256"]
    assert listed[0]["package_version_id"] == second_item["package_version_id"]


def test_skill_archive_upload_accepts_instruction_skill_tar_gz_bundle() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.tar.gz"},
        content=instruction_skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["research-writer", "reviewer-checklist"]
    assert all("SKILL.md detected" in item["scan_diff"] for item in body["items"])
    assert {item["name"] for item in skills.json()} == {"research-writer", "reviewer-checklist"}


def test_skill_archive_upload_accepts_large_flat_instruction_bundle_zip() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "skills.zip"},
        content=large_flat_instruction_skill_bundle_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert len(body["items"]) == 99
    assert body["items"][0]["name"] == "flat-instruction-skill-000"
    assert body["items"][-1]["name"] == "flat-instruction-skill-098"
    assert len(skills.json()) == 99


def test_skill_archive_upload_accepts_large_nested_instruction_bundle_tar_gz() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills_1.tar.gz"},
        content=large_nested_instruction_skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert len(body["items"]) == 99
    assert body["items"][0]["name"] == "nested-instruction-skill-000"
    assert body["items"][-1]["name"] == "nested-instruction-skill-098"
    assert len(skills.json()) == 99


def test_skill_bulk_delete_removes_selected_skills_and_reports_missing() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.tar.gz"},
        content=instruction_skill_bundle_archive(),
    )
    skill_ids = [item["id"] for item in uploaded.json()["items"]]

    deleted = api.post(
        "/api/v1/admin/skills/bulk-delete",
        headers=headers(),
        json={"ids": [skill_ids[0], skill_ids[1], skill_ids[0], "missing-skill"]},
    )
    remaining = api.get("/api/v1/admin/skills", headers=headers())

    assert deleted.status_code == 200
    assert deleted.json() == {
        "deleted": [skill_ids[0], skill_ids[1]],
        "failed": [
            {
                "id": "missing-skill",
                "code": "not_found",
                "message": "not found",
            }
        ],
    }
    assert remaining.json() == []


def test_skill_archive_upload_accepts_rich_instruction_skill_directory() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "rich-skills.zip"},
        content=instruction_bundle_with_rich_skill_directory_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["rich-skill", "compact-skill"]
    assert {item["name"] for item in skills.json()} == {"rich-skill", "compact-skill"}


def test_skill_archive_upload_accepts_large_instruction_skill_directory() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "large-skills.zip"},
        content=instruction_bundle_with_very_large_skill_directory_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == [
        "large-research-skill",
        "compact-neighbor-skill",
    ]
    assert {item["name"] for item in skills.json()} == {
        "large-research-skill",
        "compact-neighbor-skill",
    }


def test_skill_archive_upload_uses_directory_slug_when_frontmatter_name_has_no_slug() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "skills.zip"},
        content=instruction_bundle_with_non_slug_frontmatter_name_zip(),
    )

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["items"][0]["name"] == "bianzheng-pingheng"


def test_skill_archive_upload_ignores_hidden_nested_skill_directories() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "hidden-worktree-skills.zip"},
        content=instruction_bundle_with_hidden_nested_skill_zip(),
    )

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert [item["name"] for item in body["items"]] == ["aibiandao", "other-skill"]


def test_skill_archive_upload_keeps_parent_skill_with_nested_example_skill_files() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "skills-with-examples.zip"},
        content=instruction_bundle_with_nested_example_skill_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["nuwa", "other-skill"]
    assert body["skipped"] == []
    assert {item["name"] for item in skills.json()} == {"nuwa", "other-skill"}


def test_skill_archive_upload_accepts_phone_wrapped_large_instruction_bundle_with_assets() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills_1.tar.gz"},
        content=large_phone_wrapped_instruction_skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert len(body["items"]) == 99
    assert body["items"][0]["name"] == "phone-wrapped-skill-000"
    assert body["items"][-1]["name"] == "phone-wrapped-skill-098"
    assert len(skills.json()) == 99


def test_skill_archive_upload_accepts_phone_wrapped_tar_metadata_bundle() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills_1.tar.gz"},
        content=phone_wrapped_instruction_bundle_with_tar_metadata_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == [
        "phone-metadata-skill-000",
        "phone-metadata-skill-001",
        "phone-metadata-skill-002",
    ]
    assert [item["name"] for item in skills.json()] == [
        "phone-metadata-skill-000",
        "phone-metadata-skill-001",
        "phone-metadata-skill-002",
    ]


def test_skill_archive_upload_keeps_valid_bundle_items_when_one_item_is_invalid() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "mixed-skills.zip"},
        content=partially_invalid_instruction_skill_bundle_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["valid-bundle-skill"]
    assert body["skipped"] == [
        {
            "path": "invalid-skill",
            "reason": "instruction skill contains nested archives",
        }
    ]
    assert [item["name"] for item in skills.json()] == ["valid-bundle-skill"]


def test_skill_archive_upload_rejects_invalid_zip_without_saving_metadata() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "broken.zip"},
        content=b"not-a-zip",
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 422
    body = uploaded.json()
    assert body["error"]["code"] == "invalid_skill_package"
    assert body["error"]["details"]["reason"] == "skill archive must be a valid zip or tar archive"
    assert skills.json() == []


def test_memory_forget_removes_record() -> None:
    api = client()
    memory_id = api.get("/api/v1/admin/memory", headers=headers()).json()[0]["id"]

    forgotten = api.delete(f"/api/v1/admin/memory/{memory_id}", headers=headers())
    remaining = api.get("/api/v1/admin/memory", headers=headers())

    assert forgotten.status_code == 200
    assert forgotten.json() == {"status": "forgotten"}
    assert remaining.json() == []


def test_hermes_records_feedback_and_recommends_from_prior_lessons() -> None:
    api = client()

    feedback = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Use group chat when debate review is required.",
            "conversation_id": "conv-architecture-1",
            "tags": ["debate", "review"],
            "weight": 5,
        },
    )
    recommendation = api.post(
        "/api/v1/admin/hermes/recommend",
        headers=headers(),
        json={
            "task": "Run a debate review for this architecture.",
            "mode_candidates": ["dispatch", "group_chat"],
            "model_candidates": ["deepseek-chat", "gpt-4o"],
            "skill_candidates": ["architecture-review", "safe-shell"],
        },
    )
    insights = api.get("/api/v1/admin/hermes", headers=headers())

    assert feedback.status_code == 200
    insight_id = feedback.json()["id"]
    assert feedback.json()["category"] == "conversation"
    assert feedback.json()["conversation_id"] == "conv-architecture-1"
    assert feedback.json()["confirmed_at"] is None
    assert feedback.json()["summary"] == (
        "Learned success pattern: Use group chat when debate review is required. "
        "Tags: debate, review. Weight: 5."
    )
    detail = api.get(f"/api/v1/admin/hermes/{insight_id}", headers=headers())
    confirmed = api.post(f"/api/v1/admin/hermes/{insight_id}/confirm", headers=headers())
    assert recommendation.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["id"] == insight_id
    assert detail.json()["conversation_id"] == "conv-architecture-1"
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_at"] is not None
    assert recommendation.json()["recommended_mode"] == "group_chat"
    assert recommendation.json()["recommended_model"] == "deepseek-chat"
    assert recommendation.json()["confidence"] > 0.45
    assert any("Hermes lesson" in reason for reason in recommendation.json()["reasons"])
    assert any(
        insight["lesson"] == "Use group chat when debate review is required."
        and insight["summary"].startswith("Learned success pattern:")
        and insight["category"] == "conversation"
        for insight in insights.json()
    )


def test_hermes_bulk_confirm_confirms_multiple_learning_records() -> None:
    api = client()
    first = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Use discussion mode for conflicting design opinions.",
            "conversation_id": "conv-bulk-1",
            "tags": ["discussion"],
            "weight": 4,
        },
    ).json()
    second = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "failure",
            "lesson": "Ask before adding temporary engineering agents.",
            "conversation_id": "conv-bulk-2",
            "tags": ["approval"],
            "weight": 5,
        },
    ).json()

    response = api.post(
        "/api/v1/admin/hermes/bulk-confirm",
        headers=headers(),
        json={"ids": [first["id"], second["id"]]},
    )

    assert response.status_code == 200
    confirmed = response.json()["confirmed"]
    assert [item["id"] for item in confirmed] == [first["id"], second["id"]]
    assert all(item["confirmed_at"] is not None for item in confirmed)
    assert response.json()["failed"] == []


def test_hermes_bulk_delete_removes_multiple_learning_records() -> None:
    api = client()
    first = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Delete old Hermes lessons in batches during cleanup.",
            "conversation_id": "conv-bulk-delete-1",
            "tags": ["cleanup"],
            "weight": 3,
        },
    ).json()
    second = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "neutral",
            "lesson": "Remove obsolete confirmed Hermes guidance as a batch.",
            "conversation_id": "conv-bulk-delete-2",
            "tags": ["cleanup"],
            "weight": 2,
        },
    ).json()
    api.post(f"/api/v1/admin/hermes/{second['id']}/confirm", headers=headers())

    response = api.post(
        "/api/v1/admin/hermes/bulk-delete",
        headers=headers(),
        json={"ids": [first["id"], second["id"], "hermes_deadbeef"]},
    )
    remaining = api.get("/api/v1/admin/hermes", headers=headers())

    assert response.status_code == 200
    assert response.json()["deleted"] == [first["id"], second["id"]]
    assert response.json()["failed"] == [
        {
            "id": "hermes_deadbeef",
            "code": "hermes_not_found",
            "message": "Hermes learning record was not found",
        }
    ]
    assert all(item["id"] not in {first["id"], second["id"]} for item in remaining.json())


def test_hermes_bulk_actions_accept_large_mobile_selection() -> None:
    api = client()
    created_ids: list[str] = []
    for index in range(289):
        response = api.post(
            "/api/v1/admin/hermes/feedback",
            headers=headers(),
            json={
                "outcome": "neutral",
                "lesson": f"Large mobile bulk selection regression lesson {index}.",
                "conversation_id": f"conv-large-bulk-{index}",
                "tags": ["bulk"],
                "weight": 1,
            },
        )
        assert response.status_code == 200
        created_ids.append(response.json()["id"])

    confirm = api.post(
        "/api/v1/admin/hermes/bulk-confirm",
        headers=headers(),
        json={"ids": created_ids},
    )
    delete = api.post(
        "/api/v1/admin/hermes/bulk-delete",
        headers=headers(),
        json={"ids": created_ids},
    )

    assert confirm.status_code == 200
    assert [item["id"] for item in confirm.json()["confirmed"]] == created_ids
    assert confirm.json()["failed"] == []
    assert delete.status_code == 200
    assert delete.json() == {"deleted": created_ids, "failed": []}


def test_hermes_bulk_actions_accept_runtime_learning_ids() -> None:
    api = client()
    runtime_id = "hermes_run_deadbeef0123456789abcdef01234567"

    confirm = api.post(
        "/api/v1/admin/hermes/bulk-confirm",
        headers=headers(),
        json={"ids": [runtime_id]},
    )
    delete = api.post(
        "/api/v1/admin/hermes/bulk-delete",
        headers=headers(),
        json={"ids": [runtime_id]},
    )

    assert confirm.status_code == 200
    assert confirm.json() == {
        "confirmed": [],
        "failed": [
            {
                "id": runtime_id,
                "code": "hermes_not_found",
                "message": "Hermes learning record was not found",
            }
        ],
    }
    assert delete.status_code == 200
    assert delete.json() == {
        "deleted": [],
        "failed": [
            {
                "id": runtime_id,
                "code": "hermes_not_found",
                "message": "Hermes learning record was not found",
            }
        ],
    }


def test_hermes_delete_removes_learning_record() -> None:
    api = client()
    insight = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "neutral",
            "lesson": "Delete stale Hermes lessons when they are no longer useful.",
            "conversation_id": "conv-delete-1",
            "tags": ["cleanup"],
            "weight": 2,
        },
    ).json()

    deleted = api.delete(f"/api/v1/admin/hermes/{insight['id']}", headers=headers())
    remaining = api.get("/api/v1/admin/hermes", headers=headers())
    missing = api.get(f"/api/v1/admin/hermes/{insight['id']}", headers=headers())

    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}
    assert all(item["id"] != insight["id"] for item in remaining.json())
    assert missing.status_code == 404


def test_hermes_feedback_rejects_sensitive_content_without_echoing_it() -> None:
    response = client().post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "failure",
            "lesson": "Do not store api_key sk-secret-value in memory.",
            "tags": ["security"],
            "weight": 10,
        },
    )

    assert response.status_code == 422
    assert "sk-secret-value" not in response.text


@pytest.mark.asyncio
async def test_persistent_hermes_missing_payload_does_not_fallback_to_in_memory_defaults() -> None:
    class MissingHermesPayloadService(PersistentAdminResourceService):
        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            del resource_id, tenant_id
            if kind == "hermes":
                return {}
            return None

        async def _delete_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> bool | None:
            del resource_id, tenant_id
            if kind == "hermes":
                return False
            return None

    service = MissingHermesPayloadService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    with pytest.raises(KeyError):
        await service.get_hermes_insight("hermes-1")
    with pytest.raises(KeyError):
        await service.confirm_hermes_insight("hermes-1")
    with pytest.raises(KeyError):
        await service.delete_hermes_insight("hermes-1")


@pytest.mark.asyncio
async def test_persistent_admin_models_write_to_published_config() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=4,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.logical_model == "main"
    assert created.upstream_model == "deepseek-chat"
    assert configs.current is not None
    deployment, request, api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert deployment.request_model == "deepseek-chat"
    assert request.logical_model == "main"
    assert api_key == "sk-live"
    assert secrets.resolved == [
        (TENANT_ID, f"secret://{SECRET_ID}"),
    ]
    assert configs.current.document == {
        "models": {
            "main": {
                "deployments": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_base": "https://api.deepseek.com/v1",
                        "credential_ref": f"secret://{SECRET_ID}",
                        "quota_scope_id": "deepseek-account",
                        "max_concurrency": 4,
                        "target_utilization": 0.8,
                        "reserved_slots": 0,
                        "rpm": 60,
                        "tpm": 100000,
                        "capabilities": ["text", "tool_calling"],
                    }
                ]
            },
        },
        "agents": [],
    }


@pytest.mark.asyncio
async def test_persistent_admin_service_for_principal_reads_principal_tenant_config() -> None:
    class TenantAwareConfigService:
        def __init__(self) -> None:
            self.tenant_ids: list[UUID] = []

        async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
            self.tenant_ids.append(tenant_id)
            return ConfigRevision(
                id=uuid4(),
                tenant_id=tenant_id,
                version=1,
                status=ConfigStatus.PUBLISHED,
                document={
                    "models": {
                        "main": {
                            "deployments": [
                                {
                                    "provider": "openai-compatible",
                                    "model": "model",
                                    "api_base": "https://example.test/v1",
                                    "credential_ref": f"secret://{SECRET_ID}",
                                    "quota_scope_id": "tenant-test",
                                    "max_concurrency": 1,
                                    "target_utilization": 0.8,
                                    "reserved_slots": 0,
                                    "rpm": 60,
                                    "tpm": 100000,
                                    "capabilities": ["text"],
                                }
                            ]
                        }
                    },
                    "agents": [
                        {
                            "id": f"agent-{tenant_id}",
                            "role": "assistant",
                            "prompt": "help",
                            "model": "main",
                            "skills": [],
                        }
                    ],
                },
                created_by=USER_ID,
                created_at=datetime.now(UTC),
            )

    configs = TenantAwareConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    scoped = service.for_principal(OTHER_TENANT_ID, USER_ID)
    agents = await scoped.list_agents()

    assert configs.tenant_ids == [OTHER_TENANT_ID]
    assert agents[0].id == f"agent-{OTHER_TENANT_ID}"


@pytest.mark.asyncio
async def test_persistent_admin_mcp_preserves_transport_connection_fields() -> None:
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    created = await service.upsert_mcp_server(
        McpServerRequest(
            id="local_mcp",
            name="Local MCP",
            transport="stdio",
            command="/usr/bin/python3",
            args=["/opt/mcp/server.py"],
            executable_allowlist=["/usr/bin/python3"],
            allowed_tools=["echo"],
            timeout_seconds=5,
        )
    )
    listed = await service.list_mcp_servers()

    assert created.transport == "stdio"
    assert created.command == "/usr/bin/python3"
    assert created.args == ["/opt/mcp/server.py"]
    assert created.executable_allowlist == ["/usr/bin/python3"]
    assert created.timeout_seconds == 5
    assert listed[0].transport == "stdio"


@pytest.mark.asyncio
async def test_persistent_admin_normalizes_openai_compatible_root_api_base() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="openai-compatible",
            api_base="https://gsykj.com",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="relay-account",
            max_concurrency=2,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://gsykj.com/v1"
    assert transport.calls[0][0].api_base == "https://gsykj.com/v1"
    assert configs.current is not None
    deployment = cast(
        dict[str, object],
        cast(dict[str, object], configs.current.document["models"])["main"],
    )["deployments"]
    assert cast(list[dict[str, object]], deployment)[0]["api_base"] == "https://gsykj.com/v1"


@pytest.mark.asyncio
async def test_persistent_admin_updates_existing_model_and_rechecks_availability() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=1,
            target_utilization=0.8,
        )
    )

    updated = await service.update_model(
        created.id,
        ModelDeploymentRequest(
            provider="openai-compatible",
            api_base="https://gsykj.com",
            upstream_model="deepseek-v4-flash",
            logical_model="planner",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="relay-account",
            max_concurrency=4,
            target_utilization=0.8,
            rpm=120,
            tpm=200000,
        ),
    )

    assert updated.logical_model == "planner"
    assert updated.provider == "openai-compatible"
    assert updated.api_base == "https://gsykj.com/v1"
    assert updated.max_concurrency == 4
    assert len(transport.calls) == 2
    assert configs.current is not None
    assert configs.current.document["models"] == {
        "planner": {
            "deployments": [
                {
                    "provider": "openai-compatible",
                    "model": "deepseek-v4-flash",
                    "api_base": "https://gsykj.com/v1",
                    "credential_ref": f"secret://{SECRET_ID}",
                    "quota_scope_id": "relay-account",
                    "max_concurrency": 4,
                    "target_utilization": 0.8,
                    "reserved_slots": 0,
                    "rpm": 120,
                    "tpm": 200000,
                    "capabilities": ["text", "tool_calling"],
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_persistent_admin_normalizes_anthropic_messages_api_base() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="claude-code-compatible",
            api_base="https://toapis.com/v1",
            api_protocol="anthropic_messages",
            upstream_model="claude-sonnet-4-6",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="anthropic-account",
            max_concurrency=2,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://toapis.com/v1/messages"
    assert created.api_protocol == "anthropic_messages"
    assert transport.calls[0][0].api_base == "https://toapis.com/v1/messages"
    assert transport.calls[0][0].max_concurrency == 2


@pytest.mark.asyncio
async def test_persistent_admin_verifies_dedicated_main_agent_model() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    response = await service.update_main_agent_config(
        MainAgentConfigRequest(
            model=MainAgentModelConfig(
                provider="claude-code-relay",
                api_base="https://toapis.com/v1",
                api_protocol="anthropic_messages",
                upstream_model="claude-sonnet-4-6",
                credential_ref=f"secret://{SECRET_ID}",
                capabilities=["text", "tool_calling"],
                max_concurrency=3,
            ),
            control_mode="supervisor",
            hermes_policy="confirm_before_apply",
            decision_policy="choose mode and role pool; ask before workflow changes",
            operating_style="control the room and ask before changing a chosen workflow",
            direct_answerer="main_agent",
            max_review_rounds=2,
        )
    )

    assert response.model is not None
    assert response.model.api_base == "https://toapis.com/v1/messages"
    assert transport.calls[0][0].logical_model == "main_agent"
    assert transport.calls[0][0].api_base == "https://toapis.com/v1/messages"
    assert transport.calls[0][0].max_concurrency == 3
    assert transport.calls[0][2] == "sk-live"


@pytest.mark.asyncio
async def test_persistent_admin_logs_dedicated_main_agent_model_failures() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(RuntimeError("provider returned status=401")),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.update_main_agent_config(
            MainAgentConfigRequest(
                model=MainAgentModelConfig(
                    provider="claude-code-relay",
                    api_base="https://bad-relay.example/v1",
                    api_protocol="anthropic_messages",
                    upstream_model="claude-sonnet-4-6",
                    credential_ref=f"secret://{SECRET_ID}",
                    capabilities=["text", "tool_calling"],
                ),
                control_mode="supervisor",
                hermes_policy="confirm_before_apply",
                decision_policy="choose mode and role pool; ask before workflow changes",
                operating_style="control the room and ask before changing a chosen workflow",
                direct_answerer="main_agent",
                max_review_rounds=2,
            )
        )

    assert error.value.code == "model_unavailable"
    assert error.value.details is not None
    assert error.value.details["provider"] == "claude-code-relay"
    assert error.value.details["logical_model"] == "main_agent"
    assert error.value.details["api_base"] == "https://bad-relay.example/v1/messages"
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].source == "main_agent.update"
    assert model_logs[0].message == "provider returned status=401"
    assert model_logs[0].details["provider"] == "claude-code-relay"
    assert model_logs[0].details["logical_model"] == "main_agent"
    assert model_logs[0].details["api_base"] == "https://bad-relay.example/v1/messages"
    serialized = model_logs[0].model_dump_json()
    assert "credential_ref" not in serialized
    assert "secret://" not in serialized


@pytest.mark.asyncio
async def test_persistent_admin_agents_write_to_published_config() -> None:
    configs = FakeConfigService()
    configs.current = ConfigRevision(
        id=uuid4(),
        tenant_id=TENANT_ID,
        version=1,
        status=ConfigStatus.PUBLISHED,
        document={
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "credential_ref": f"secret://{SECRET_ID}",
                            "quota_scope_id": "deepseek-account",
                        }
                    ]
                }
            },
            "agents": [],
        },
        created_by=ACTOR_ID,
        created_at=datetime.now(UTC),
    )
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    created = await service.upsert_agent(
        AgentResourceRequest(
            id="director",
            name="导演",
            enabled=True,
            role="短视频导演",
            prompt="负责拆解选题、镜头语言和成片节奏。",
            model="main",
            skills=["script_review"],
        )
    )
    listed = await service.list_agents()

    assert created.id == "director"
    assert created.role == "短视频导演"
    assert listed == (created,)
    assert configs.current is not None
    assert configs.current.version == 1
    assert configs.current.document["agents"] == [
        {
            "id": "director",
            "role": "短视频导演",
            "prompt": "负责拆解选题、镜头语言和成片节奏。",
            "model": "main",
            "skills": ["script_review"],
        }
    ]


@pytest.mark.asyncio
async def test_persistent_admin_model_is_not_published_when_availability_check_fails() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(RuntimeError("provider returned status=401")),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.create_model(
            ModelDeploymentRequest(
                provider="deepseek",
                api_base="https://api.deepseek.com/v1",
                upstream_model="deepseek-chat",
                logical_model="main",
                capabilities=["text"],
                credential_ref=f"secret://{SECRET_ID}",
                quota_scope="deepseek-account",
                max_concurrency=4,
                target_utilization=0.8,
                reserved_capacity=0,
                rpm=60,
                tpm=100000,
                queue_timeout_seconds=60,
                fallback=None,
                weight=100,
            )
        )

    assert error.value.code == "model_unavailable"
    assert "status=401" in error.value.public_message
    assert error.value.details == {
        "stage": "model_availability_check",
        "provider": "deepseek",
        "api_base": "https://api.deepseek.com/v1",
        "logical_model": "main",
        "upstream_model": "deepseek-chat",
        "status_code": "401",
        "reason": "provider returned status=401",
        "hint": "检查 API Key 是否有效、API Base 是否可从服务器访问、模型名是否属于该服务商账号。",
    }
    assert "sk-live" not in error.value.public_message
    assert "credential_ref" not in error.value.details
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].message == "provider returned status=401"
    assert model_logs[0].details["provider"] == "deepseek"
    assert model_logs[0].details["status_code"] == "401"
    assert configs.drafts == []
    assert configs.current is None


@pytest.mark.asyncio
async def test_persistent_admin_saves_multimedia_video_model_without_chat_probe() -> None:
    configs = FakeConfigService()
    transport = FakeModelTransport(RuntimeError("chat probe should not run"))
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="minimax",
            api_base="https://api.minimax.io",
            upstream_model="MiniMax-Hailuo-02",
            logical_model="video_primary",
            capabilities=["video_generation"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="minimax-video-account",
            max_concurrency=1,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=3,
            tpm=None,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://api.minimax.io/v1"
    assert created.capabilities == ["video_generation"]
    assert transport.calls == []
    assert configs.current is not None
    document = cast(dict[str, Any], configs.current.document)
    models = cast(dict[str, Any], document["models"])
    video_primary = cast(dict[str, Any], models["video_primary"])
    deployments = cast(list[dict[str, Any]], video_primary["deployments"])
    assert deployments[0]["model"] == "MiniMax-Hailuo-02"


@pytest.mark.asyncio
async def test_persistent_admin_saves_multimedia_audio_model_without_chat_probe() -> None:
    configs = FakeConfigService()
    transport = FakeModelTransport(RuntimeError("chat probe should not run"))
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="minimax",
            api_base="https://api.minimax.io",
            upstream_model="speech-2.8-turbo",
            logical_model="audio_primary",
            capabilities=["audio_generation"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="minimax-audio-account",
            max_concurrency=2,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=30,
            tpm=None,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://api.minimax.io/v1"
    assert created.capabilities == ["audio_generation"]
    assert transport.calls == []
    assert configs.current is not None
    document = cast(dict[str, Any], configs.current.document)
    models = cast(dict[str, Any], document["models"])
    audio_primary = cast(dict[str, Any], models["audio_primary"])
    deployments = cast(list[dict[str, Any]], audio_primary["deployments"])
    assert deployments[0]["model"] == "speech-2.8-turbo"


@pytest.mark.asyncio
async def test_persistent_admin_deletes_model_deployment_and_publishes_config() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(),
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=4,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    await service.delete_model(created.id)

    assert await service.list_models() == ()
    assert configs.current is not None
    assert configs.current.document["models"] == {}
    model_logs = await service.list_logs("model_error")
    serialized = "".join(item.model_dump_json() for item in model_logs)
    assert "secret://" not in serialized


@pytest.mark.asyncio
async def test_persistent_admin_model_logs_preflight_availability_failures() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.create_model(
            ModelDeploymentRequest(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                upstream_model="abab6.5s-chat",
                logical_model="vision_only",
                capabilities=["vision"],
                credential_ref=f"secret://{SECRET_ID}",
                quota_scope="minimax-account",
                max_concurrency=4,
                target_utilization=0.8,
                reserved_capacity=0,
                rpm=60,
                tpm=100000,
                queue_timeout_seconds=60,
                fallback=None,
                weight=100,
            )
        )

    assert error.value.code == "model_unavailable"
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].message == "model availability check requires text capability"
    assert model_logs[0].details["provider"] == "minimax"
    assert model_logs[0].details["logical_model"] == "vision_only"
    serialized = model_logs[0].model_dump_json()
    assert "credential_ref" not in serialized
    assert "secret://" not in serialized
    assert configs.drafts == []
    assert configs.current is None


@pytest.mark.asyncio
async def test_persistent_admin_secret_uses_sealed_secret_service() -> None:
    secrets = FakeSecretService()
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    reference = await service.create_secret(
        SecretCreateRequest(label="deepseek", value=SecretStr("sk-live-1234"))
    )

    assert reference.ref == f"secret://{SECRET_ID}"
    assert reference.last_four == "1234"
    assert secrets.values == ["sk-live-1234"]


def test_evolution_run_records_skill_optimization_rounds_and_audit() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "skill_optimization",
            "title": "优化 darwin-skill",
            "objective": "对 darwin-skill 做标准三轮优化，保留有测试收益的版本。",
            "mode": "hybrid",
            "source_skill_ids": ["darwin-skill"],
            "target_artifact_type": "skill",
            "baseline_agent_id": "agent-main-m3",
            "candidate_agent_ids": ["agent-coder", "agent-reviewer"],
            "evaluator_agent_id": "agent-evaluator",
            "approval_policy": "ask",
            "iteration_policy": "score_gated",
            "memory_policy": "summarize_between_rounds",
            "max_rounds": 3,
            "min_delta": 2.0,
            "rubric": ["结构评分", "实测表现", "反例黑名单"],
        },
    )

    assert created.status_code == 200
    run = created.json()
    assert run["id"].startswith("evolution_")
    assert run["status"] == "waiting_approval"
    assert run["kind"] == "skill_optimization"
    assert run["baseline_agent_id"] == "agent-main-m3"
    assert run["candidate_agent_ids"] == ["agent-coder", "agent-reviewer"]
    assert run["evaluator_agent_id"] == "agent-evaluator"
    assert run["approval_status"] == "pending"
    assert run["next_action"] == "request_approval"

    blocked = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "未审批测试",
            "candidate_summary": "未审批前不应记录候选版本。",
            "score_before": 70.0,
            "score_after": 71.0,
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "evolution_run_requires_approval"

    approved = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/approve",
        headers=headers(),
        json={"approved": True, "note": "人工确认基准 agent 和评测口径。"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "running"
    assert approved.json()["approval_status"] == "approved"
    assert approved.json()["next_action"] == "run_next_round"

    recorded = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "实测表现",
            "candidate_summary": "补充 test-prompts 并减少自评偏差。",
            "score_before": 72.0,
            "score_after": 76.5,
            "tests_passed": True,
            "regression_detected": False,
            "judge_summary": "两个测试 prompt 均优于基线。",
            "artifact_refs": ["artifact://generated-skill/darwin-v2"],
            "tokens_used": 12000,
            "elapsed_seconds": 180,
        },
    )

    assert recorded.status_code == 200
    body = recorded.json()
    assert body["status"] == "running"
    assert body["next_action"] == "run_next_round"
    assert body["rounds"][0]["delta"] == 4.5
    assert body["rounds"][0]["accepted"] is True
    assert body["rounds"][0]["recommendation"] == "continue"

    listed = api.get("/api/v1/admin/evolution-runs", headers=headers())
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == run["id"]

    audit = api.get("/api/v1/admin/audit?action=evolution.round_recorded", headers=headers())
    assert audit.status_code == 200
    event = audit.json()[0]
    assert event["resource"] == f"evolution:{run['id']}"
    assert event["details"]["recommendation"] == "continue"
    assert event["details"]["next_action"] == "run_next_round"

    approval_audit = api.get("/api/v1/admin/audit?action=evolution.approve", headers=headers())
    assert approval_audit.status_code == 200
    assert approval_audit.json()[0]["details"]["approval_status"] == "approved"


def test_evolution_next_round_plan_requires_approval_and_contains_execution_contract() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "skill_optimization",
            "title": "进化科研 Skill",
            "objective": "生成并迭代 AI 科研 Skill，必须用固定评测集比较基准和候选。",
            "mode": "hybrid",
            "source_skill_ids": ["darwin-skill", "zhengliu"],
            "target_artifact_type": "skill",
            "baseline_agent_id": "agent-main-m3",
            "candidate_agent_ids": ["agent-researcher", "agent-reviewer"],
            "evaluator_agent_id": "agent-evaluator",
            "approval_policy": "ask",
            "iteration_policy": "score_gated",
            "memory_policy": "summarize_between_rounds",
            "max_rounds": 4,
            "min_delta": 2.0,
            "rubric": ["科研可用性", "反例覆盖", "可复现评测"],
        },
    )
    run = created.json()

    blocked = api.get(
        f"/api/v1/admin/evolution-runs/{run['id']}/next-round-plan", headers=headers()
    )

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "evolution_run_requires_approval"

    approved = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/approve",
        headers=headers(),
        json={"approved": True, "note": "确认基准、候选和评测口径。"},
    )
    assert approved.status_code == 200

    planned = api.get(
        f"/api/v1/admin/evolution-runs/{run['id']}/next-round-plan", headers=headers()
    )

    assert planned.status_code == 200
    plan = planned.json()
    assert plan["run_id"] == run["id"]
    assert plan["round"] == 1
    assert plan["action"] == "run_next_round"
    assert plan["baseline_agent_id"] == "agent-main-m3"
    assert plan["candidate_agent_ids"] == ["agent-researcher", "agent-reviewer"]
    assert plan["evaluator_agent_id"] == "agent-evaluator"
    assert "固定评测集比较基准和候选" in plan["task_prompt"]
    assert "darwin-skill" in plan["task_prompt"]
    assert "score_before" in plan["required_output_schema"]
    assert plan["memory_policy"] == "summarize_between_rounds"


def test_evolution_next_round_execution_queues_real_run_with_metadata() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "skill_optimization",
            "title": "进化科研 Skill",
            "objective": "执行一轮候选 Skill 评测并返回结构化评分。",
            "mode": "hybrid",
            "source_skill_ids": ["darwin-skill"],
            "target_artifact_type": "skill",
            "baseline_agent_id": "agent-main-m3",
            "candidate_agent_ids": ["agent-researcher", "agent-reviewer"],
            "evaluator_agent_id": "agent-evaluator",
            "approval_policy": "ask",
        },
    )
    run = created.json()

    blocked = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/execute-next-round", headers=headers()
    )

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "evolution_run_requires_approval"

    approved = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/approve",
        headers=headers(),
        json={"approved": True, "note": "确认执行下一轮。"},
    )
    assert approved.status_code == 200

    executed = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/execute-next-round", headers=headers()
    )

    assert executed.status_code == 200
    execution = executed.json()
    assert execution["evolution_run_id"] == run["id"]
    assert execution["round"] == 1
    assert execution["status"] == "queued"
    assert "fixed evaluation set" in execution["task_prompt"]

    run_detail = api.get(f"/api/v1/admin/runs/{execution['execution_run_id']}", headers=headers())
    assert run_detail.status_code == 200
    detail = run_detail.json()
    assert detail["status"] == "queued"
    assert detail["conversation_id"] == execution["execution_conversation_id"]
    assert detail["explicit_details"]["source"] == "evolution"
    assert detail["explicit_details"]["evolution_run_id"] == run["id"]
    assert detail["explicit_details"]["evolution_round"] == "1"
    assert detail["explicit_details"]["candidate_agent_ids"] == "agent-researcher, agent-reviewer"

    audit = api.get(
        "/api/v1/admin/audit?action=evolution.round_execution_queued", headers=headers()
    )
    assert audit.status_code == 200
    assert audit.json()[0]["details"]["execution_run_id"] == execution["execution_run_id"]


def test_evolution_execution_result_ingest_records_round_from_artifact() -> None:
    api = client()
    app = cast(Any, api.app)
    service = cast(InMemoryAdminResourceService, app.state.admin_resource_service)
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "skill_optimization",
            "title": "进化科研 Skill",
            "objective": "执行一轮候选 Skill 评测并返回结构化评分。",
            "mode": "hybrid",
            "source_skill_ids": ["darwin-skill"],
            "target_artifact_type": "skill",
            "baseline_agent_id": "agent-main-m3",
            "candidate_agent_ids": ["agent-researcher"],
            "evaluator_agent_id": "agent-evaluator",
            "approval_policy": "auto",
        },
    )
    run = created.json()
    executed = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/execute-next-round", headers=headers()
    )
    execution = executed.json()
    execution_run_id = UUID(execution["execution_run_id"])
    queued = service.runs[execution_run_id]
    round_payload = {
        "changed_dimension": "可验证性",
        "candidate_summary": "补充固定评测集和失败样例。",
        "score_before": 62.0,
        "score_after": 71.5,
        "tests_passed": True,
        "regression_detected": False,
        "accepted": True,
        "judge_summary": "候选版本在边界用例上有稳定提升。",
        "artifact_refs": ["skill://candidate/research-v2"],
        "tokens_used": 1200,
        "elapsed_seconds": 45,
    }
    service.runs[execution_run_id] = queued.model_copy(
        update={
            "status": "completed",
            "artifacts": [
                RunArtifactResponse(
                    id="evolution-round-result",
                    kind="json",
                    title="Evolution round result",
                    text=json.dumps(round_payload, ensure_ascii=False),
                )
            ],
        }
    )

    ingested = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/execution-runs/{execution_run_id}/ingest",
        headers=headers(),
    )

    assert ingested.status_code == 200
    updated = ingested.json()
    assert updated["rounds"][0]["changed_dimension"] == "可验证性"
    assert updated["rounds"][0]["delta"] == 9.5
    assert updated["rounds"][0]["accepted"] is True
    assert f"run://{execution_run_id}" in updated["rounds"][0]["artifact_refs"]
    assert updated["next_action"] == "run_next_round"

    audit = api.get("/api/v1/admin/audit?action=evolution.round_ingested", headers=headers())
    assert audit.status_code == 200
    assert audit.json()[0]["details"]["execution_run_id"] == str(execution_run_id)


def test_evolution_execution_result_ingest_rejects_unlinked_run() -> None:
    api = client()
    app = cast(Any, api.app)
    service = cast(InMemoryAdminResourceService, app.state.admin_resource_service)
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "skill_optimization",
            "title": "进化科研 Skill",
            "objective": "执行一轮候选 Skill 评测并返回结构化评分。",
            "mode": "hybrid",
            "target_artifact_type": "skill",
            "approval_policy": "auto",
        },
    )
    run = created.json()
    unrelated_run_id = uuid4()
    now = datetime.now(UTC)
    service.runs[unrelated_run_id] = RunDetailResponse(
        id=unrelated_run_id,
        status="completed",
        mode="hybrid",
        conversation_id="manual-run",
        request="manual task",
        created_at=now,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        events=[RunEventResponse(sequence=1, kind="completed", message="done", created_at=now)],
        artifacts=[RunArtifactResponse(id="result", kind="json", title="result", text="{}")],
        explicit_details={"source": "manual"},
    )

    ingested = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/execution-runs/{unrelated_run_id}/ingest",
        headers=headers(),
    )

    assert ingested.status_code == 409
    assert ingested.json()["error"]["code"] == "evolution_execution_mismatch"


@pytest.mark.asyncio
async def test_persistent_evolution_next_round_execution_enqueues_run_repository() -> None:
    execution_run_id = UUID("33333333-3333-4333-8333-333333333333")

    class FakeRunRepository:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def create_run(
            self,
            *,
            tenant_id: UUID,
            actor_id: UUID,
            actor_role: Role | None = None,
            request: str,
            mode: TaskMode | None,
            status: RunStatus,
            idempotency_key: str | None,
            routing_decision: dict[str, object] | None = None,
            enqueue: bool,
        ) -> RunRecord:
            self.calls.append(
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "request": request,
                    "mode": mode,
                    "status": status,
                    "idempotency_key": idempotency_key,
                    "routing_decision": routing_decision,
                    "enqueue": enqueue,
                }
            )
            return RunRecord(
                id=execution_run_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                actor_role=actor_role,
                request=request,
                mode=mode,
                status=status,
                version=1,
                created_at=datetime.now(UTC),
                routing_decision=routing_decision,
            )

    repository = FakeRunRepository()
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=repository,  # type: ignore[arg-type]
    )
    evolution = await service.create_evolution_run(
        EvolutionRunRequest(
            kind="skill_optimization",
            title="进化科研 Skill",
            objective="执行一轮候选 Skill 评测并返回结构化评分。",
            mode="hybrid",
            source_skill_ids=["darwin-skill"],
            target_artifact_type="skill",
            baseline_agent_id="agent-main-m3",
            candidate_agent_ids=["agent-researcher"],
            evaluator_agent_id="agent-evaluator",
            approval_policy="auto",
        ),
        actor=str(USER_ID),
    )

    response = await service.execute_evolution_next_round(
        evolution.id,
        EvolutionNextRoundExecutionRequest(idempotency_key="evolution-test-key"),
        actor=str(USER_ID),
    )

    assert response.execution_run_id == str(execution_run_id)
    assert response.status == "queued"
    assert len(repository.calls) == 1
    call = repository.calls[0]
    assert call["tenant_id"] == TENANT_ID
    assert call["actor_id"] == USER_ID
    assert call["actor_role"] is Role.OPERATOR
    assert call["mode"] is TaskMode.HYBRID
    assert call["status"] is RunStatus.QUEUED
    assert call["idempotency_key"] == "evolution-test-key"
    assert call["enqueue"] is True
    routing = cast(dict[str, object], call["routing_decision"])
    assert routing["source"] == "evolution"
    assert routing["evolution_run_id"] == evolution.id
    assert routing["evolution_round"] == 1
    assert routing["candidate_agent_ids"] == ["agent-researcher"]
    assert routing["selected_agent_ids"] == ["agent-researcher", "agent-evaluator"]


@pytest.mark.asyncio
async def test_persistent_evolution_execution_result_ingest_uses_run_repository_artifacts() -> None:
    execution_run_id = UUID("44444444-4444-4444-8444-444444444444")
    routing_decision: dict[str, object] = {}
    artifact_payload = {
        "changed_dimension": "边界评测",
        "candidate_summary": "增加反例和压缩上下文策略。",
        "score_before": 70.0,
        "score_after": 75.0,
        "tests_passed": True,
        "regression_detected": False,
        "judge_summary": "多轮对话后的偏差下降。",
        "artifact_refs": [],
        "tokens_used": 2400,
        "elapsed_seconds": 90,
    }

    class FakeRunRepository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
            assert tenant_id == TENANT_ID
            assert run_id == execution_run_id
            return RunRecord(
                id=execution_run_id,
                tenant_id=tenant_id,
                actor_id=USER_ID,
                request="execute evolution round",
                mode=TaskMode.HYBRID,
                status=RunStatus.COMPLETED,
                version=1,
                created_at=datetime.now(UTC),
                routing_decision=routing_decision,
            )

        async def artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
            assert tenant_id == TENANT_ID
            assert run_id == execution_run_id
            return (
                {
                    "id": "round-result",
                    "type": "json",
                    "producer": "evaluator",
                    "content": {
                        "text": "result:\n```json\n"
                        + json.dumps(artifact_payload, ensure_ascii=False)
                        + "\n```"
                    },
                },
            )

    class StoredPersistentService(PersistentAdminResourceService):
        def __init__(self) -> None:
            super().__init__(
                config_service=FakeConfigService(),  # type: ignore[arg-type]
                secret_service=FakeSecretService(),  # type: ignore[arg-type]
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
                run_repository=FakeRunRepository(),  # type: ignore[arg-type]
            )
            self.payloads: dict[tuple[str, str], dict[str, object]] = {}

        async def _get_admin_payload(
            self,
            kind: str,
            resource_id: str,
            *,
            tenant_id: UUID | None = None,
        ) -> dict[str, object] | None:
            del tenant_id
            return self.payloads.get((kind, resource_id), {})

        async def _upsert_admin_payload(
            self,
            kind: str,
            resource_id: str,
            payload: dict[str, object],
            *,
            tenant_id: UUID | None = None,
        ) -> bool:
            del tenant_id
            self.payloads[(kind, resource_id)] = payload
            return True

    service = StoredPersistentService()
    evolution = await service.create_evolution_run(
        EvolutionRunRequest(
            kind="skill_optimization",
            title="进化科研 Skill",
            objective="执行一轮候选 Skill 评测并返回结构化评分。",
            mode="hybrid",
            source_skill_ids=["darwin-skill"],
            target_artifact_type="skill",
            baseline_agent_id="agent-main-m3",
            candidate_agent_ids=["agent-researcher"],
            evaluator_agent_id="agent-evaluator",
            approval_policy="auto",
        ),
        actor=str(USER_ID),
    )
    plan = await service.plan_evolution_next_round(evolution.id, actor=str(USER_ID))
    routing_decision.update(
        {
            "source": "evolution",
            "evolution_run_id": evolution.id,
            "evolution_round": plan.round,
        }
    )

    updated = await service.ingest_evolution_execution_run(
        evolution.id, execution_run_id, actor=str(USER_ID)
    )

    assert updated.rounds[0].changed_dimension == "边界评测"
    assert updated.rounds[0].delta == 5.0
    assert f"run://{execution_run_id}" in updated.rounds[0].artifact_refs
    assert updated.next_action == "run_next_round"


def test_evolution_run_stops_after_two_low_delta_rounds() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "academic_research",
            "title": "论文创新点发现",
            "objective": "迭代发现论文创新点并用反例筛选。",
            "mode": "discuss",
            "target_artifact_type": "research_gap",
            "approval_policy": "auto",
            "max_rounds": 5,
            "min_delta": 2.0,
        },
    )
    run_id = created.json()["id"]

    first = api.post(
        f"/api/v1/admin/evolution-runs/{run_id}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "可发表性",
            "candidate_summary": "提出一个小幅改进的研究 gap。",
            "score_before": 80.0,
            "score_after": 81.0,
            "tests_passed": True,
            "regression_detected": False,
        },
    )
    assert first.status_code == 200
    assert first.json()["status"] == "running"
    assert first.json()["rounds"][0]["recommendation"] == "observe_one_more_round"

    second = api.post(
        f"/api/v1/admin/evolution-runs/{run_id}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "反例验证",
            "candidate_summary": "反例检查后只有小幅提升。",
            "score_before": 81.0,
            "score_after": 81.7,
            "tests_passed": True,
            "regression_detected": False,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "stopped"
    assert body["rounds"][1]["recommendation"] == "stop"
    assert body["stop_reason"] == "two consecutive rounds below minimum delta"

    rejected = api.post(
        f"/api/v1/admin/evolution-runs/{run_id}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "继续迭代",
            "candidate_summary": "不应继续执行。",
            "score_before": 81.7,
            "score_after": 82.0,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "evolution_run_closed"
