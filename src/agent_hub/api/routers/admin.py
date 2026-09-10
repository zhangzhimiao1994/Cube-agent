from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import inspect
import io
import json
import logging
import os
import re
import shlex
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Protocol, cast, get_args
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import BASE_ERROR_RESPONSES, PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied, Role
from agent_hub.capabilities.tools.registry import PluginConfigCapabilityManifestSource
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService, ConfigValidationError
from agent_hub.db.models import AdminResourceRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.evolution import (
    EvolutionApprovalRequest,
    EvolutionNextRoundExecutionRequest,
    EvolutionNextRoundExecutionResponse,
    EvolutionNextRoundPlanResponse,
    EvolutionRoundRequest,
    EvolutionRunRequest,
    EvolutionRunResponse,
    append_evolution_round,
    approve_evolution_run_response,
    create_evolution_run_response,
    plan_evolution_next_round,
)
from agent_hub.files.generated import GeneratedFileStore, validate_generated_filename
from agent_hub.mcp.manifest import McpConfigCapabilityManifestSource
from agent_hub.models.capabilities import infer_model_capabilities
from agent_hub.models.capacity import safe_operational_limit
from agent_hub.models.gateway import ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient, ModelTransportError
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import Deployment, ModelCapability, ModelMessage, ModelRequest
from agent_hub.multimodal.generation import (
    MultimediaArtifact,
    MultimediaDailyLimitExceeded,
    MultimediaGenerationJob,
    MultimediaGenerationKind,
    MultimediaGenerationResult,
)
from agent_hub.multimodal.video_providers import VideoProviderGenerationError
from agent_hub.openclaw.executor import (
    OpenClawCommandResult,
    openclaw_command_allowed,
    run_openclaw_command,
)
from agent_hub.openclaw.remote_adapter import (
    OpenClawRemoteAdapter,
    OpenClawRemoteAdapterError,
    run_remote_openclaw_operation,
)
from agent_hub.plugins.contracts import (
    adapter_declared_sandbox_profiles,
    adapter_runtime_sandbox_profiles,
    http_json_adapter_descriptor,
)
from agent_hub.plugins.schemas import PluginSchemaError, plugin_schema_validator
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRecord, RunRepository
from agent_hub.runs.self_repair import repair_proposal_projection
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.failure_reason import (
    is_legacy_generic_failure_reason,
    runtime_failure_diagnostic_from_reason,
)
from agent_hub.scheduler.types import (
    CronScheduleSpec,
    OneTimeScheduleSpec,
    ScheduleDefinition,
    ScheduleMisfirePolicy,
    ScheduleStatus,
)
from agent_hub.security.secrets import SecretService, SecretValidationError
from agent_hub.skills.package import InvalidSkillPackage
from agent_hub.skills.scanner import SkillScanner, SkillScanReport

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], responses=BASE_ERROR_RESPONSES)
_LOGGER = logging.getLogger(__name__)
_MODEL_CHECK_STATUS_RE = re.compile(r"\bstatus[=_: ](?P<status>[1-5][0-9]{2})\b")
_MODEL_CHECK_HINT = (
    "检查 API Key 是否有效、API Base 是否可从服务器访问、模型名是否属于该服务商账号。"
)
_DASHSCOPE_AUTH_HINT = (
    "DashScope/Qwen 返回 401 通常表示鉴权失败：请使用阿里云百炼/DashScope API Key，"
    "不要使用阿里云 AccessKey/Secret；确认该 Key 已开通对应模型权限；API Base 使用 "
    "https://dashscope.aliyuncs.com/compatible-mode/v1；API Key 输入框只填写 sk-... 原文，"
    "不要带 Bearer 前缀。"
)
_OPENAI_COMPATIBLE_AUTH_HINT = (
    "OpenAI 兼容中转站返回 401/403 通常表示鉴权失败：请确认 API Key 属于该中转站账号，"
    "API Base 是否需要带 /v1，并且 API Key 输入框只填写 token 原文，不要带 Bearer 前缀。"
)


class ModelDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    api_base: str = Field(min_length=1, max_length=2048)
    api_protocol: str = Field(
        default="openai_compatible",
        pattern=r"^(openai_compatible|anthropic_messages)$",
    )
    upstream_model: str = Field(min_length=1, max_length=512)
    logical_model: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    capabilities: list[str] = Field(min_length=1, max_length=8)
    credential_ref: str = Field(min_length=1, max_length=128)
    quota_scope: str = Field(min_length=1, max_length=128)
    max_concurrency: int = Field(ge=1, le=1024)
    target_utilization: float = Field(ge=0.1, le=0.95)
    reserved_capacity: int = Field(default=0, ge=0, le=1024)
    rpm: int | None = Field(default=None, ge=1, le=1_000_000)
    tpm: int | None = Field(default=None, ge=1, le=1_000_000_000)
    queue_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    fallback: str | None = Field(default=None, max_length=128)
    weight: int = Field(default=100, ge=1, le=10_000)


class ModelDeploymentResponse(ModelDeploymentRequest):
    id: UUID
    effective_slots: int
    saturation_policy: str


def _model_effective_slots(
    max_concurrency: int,
    target_utilization: float,
    reserved_capacity: int,
) -> int:
    return safe_operational_limit(max_concurrency, target_utilization, reserved_capacity)


class SecretCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    value: SecretStr


class SecretReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    last_four: str


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quota_scope: str = Field(min_length=1, max_length=128)
    desired_concurrency: int = Field(ge=1, le=1024)


class ProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_concurrency: int
    warning: str


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(min_length=1, max_length=200_000)


class DiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added: list[str]
    removed: list[str]
    changed: list[str]


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)


class PublishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    status: str


class OperationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class NamedResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class NamedResourceResponse(NamedResourceRequest):
    pass


class WorkflowResourceRequest(NamedResourceRequest):
    mode: str | None = Field(
        default=None,
        pattern=r"^(auto|direct|dispatch|discuss|hybrid)$",
    )
    task_type: str | None = Field(default=None, max_length=256)
    allow_main_agent_override: bool = False
    allow_temporary_agents: bool = False
    temporary_agent_policy: str | None = Field(default=None, max_length=10_000)
    role_selection_policy: str | None = Field(default=None, max_length=10_000)
    agent_ids: list[str] = Field(default_factory=list, max_length=64)
    objective: str | None = Field(default=None, max_length=10_000)
    steps: list[str] = Field(default_factory=list, max_length=128)
    deliverables: list[str] = Field(default_factory=list, max_length=128)
    decision_policy: str | None = Field(default=None, max_length=10_000)


class WorkflowResourceResponse(WorkflowResourceRequest):
    pass


class AgentResourceRequest(NamedResourceRequest):
    role: str | None = Field(default=None, min_length=1, max_length=256)
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    skills: list[str] = Field(default_factory=list, max_length=128)


class AgentResourceResponse(AgentResourceRequest):
    pass


class RunListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: str
    mode: str
    version: int = Field(default=1, ge=1)
    conversation_id: str | None = None
    request: str = ""
    created_at: datetime | None = None
    queue_wait_ms: int = Field(ge=0)
    capacity_wait_ms: int = Field(ge=0)
    cost_usd: str


class RunArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    title: str
    text: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    download_url: str | None = None
    presentation: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedArtifactDownload:
    path: Path
    filename: str
    mime_type: str


class RunEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    kind: str
    message: str
    summary: str | None = None
    created_at: datetime
    actor: str | None = None
    participants: list[str] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    step_id: str | None = None
    approval_id: str | None = None
    action: str | None = None
    decision: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    artifact: RunArtifactResponse | None = None


class FailureDiagnosticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    stage: str
    reason: str
    recommendation: str
    sequence: int = Field(ge=1)
    actor: str | None = None
    step_id: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    failure_kind: str | None = None
    status_code: str | None = None
    logical_model: str | None = None
    error_stage: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    retryable: bool | None = None
    approval_id: str | None = None
    action: str | None = None
    wrapped_by: int | None = Field(default=None, ge=1)


class ToolLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    status: str
    operation_kind: str
    actor: str | None = None
    step_id: str | None = None
    started_sequence: int | None = Field(default=None, ge=1)
    terminal_sequence: int | None = Field(default=None, ge=1)
    sequences: list[int] = Field(default_factory=list)
    approval_id: str | None = None
    replay_safe: bool | None = None
    argument_bytes: int | None = Field(default=None, ge=0)
    output_bytes: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    artifact_id: str | None = None
    failure_kind: str | None = None


class ModelOutcomeSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_count: int = Field(default=0, ge=0)
    fallback_used: bool = False
    fallback_attempt_count: int = Field(default=0, ge=0)
    requested_logical_models: list[str] = Field(default_factory=list)
    actual_logical_models: list[str] = Field(default_factory=list)
    attempted_logical_models: list[str] = Field(default_factory=list)
    provider_ids: list[str] = Field(default_factory=list)
    last_requested_logical_model: str | None = None
    last_logical_model: str | None = None
    last_provider_id: str | None = None


class OrchestrationProtocolSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str
    status: Literal["planned", "active", "blocked", "completed", "unknown"] = "unknown"
    role_count: int = Field(default=0, ge=0)
    handoff_count: int = Field(default=0, ge=0)
    contract_count: int = Field(default=0, ge=0)
    blocked_contract_count: int = Field(default=0, ge=0)
    truncated: bool = False


class ModelCapabilityNegotiationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_count: int = Field(default=0, ge=0)
    satisfied_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    unknown_count: int = Field(default=0, ge=0)
    missing_capability_counts: dict[str, int] = Field(default_factory=dict, max_length=8)
    truncated: bool = False


class CapabilityExecutionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_boundary: Literal["runtime_capability_gateway"]
    role_count: int = Field(default=0, ge=0)
    capability_count: int = Field(default=0, ge=0)
    inventory_count: int = Field(default=0, ge=0)
    truncated: bool = False


class RuntimeRecoverySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_count: int = Field(default=0, ge=0)
    last_completed_steps: int = Field(default=0, ge=0)
    last_total_steps: int = Field(default=0, ge=0)
    model_status_counts: dict[str, int] = Field(default_factory=dict, max_length=8)
    tool_status_counts: dict[str, int] = Field(default_factory=dict, max_length=8)
    review_artifacts: int = Field(default=0, ge=0)


class RunDetailResponse(RunListItem):
    request: str
    events: list[RunEventResponse]
    artifacts: list[RunArtifactResponse]
    explicit_details: dict[str, str]
    failure_diagnostics: list[FailureDiagnosticResponse] = Field(default_factory=list)
    tool_lifecycle: list[ToolLifecycleResponse] = Field(default_factory=list)
    model_outcome_summary: ModelOutcomeSummaryResponse = Field(
        default_factory=ModelOutcomeSummaryResponse
    )
    orchestration_protocol_summary: OrchestrationProtocolSummaryResponse | None = None
    model_capability_negotiation_summary: (
        ModelCapabilityNegotiationSummaryResponse | None
    ) = None
    capability_execution_summary: CapabilityExecutionSummaryResponse | None = None
    runtime_recovery_summary: RuntimeRecoverySummaryResponse | None = None
    decision_token: str | None = None
    temporary_agent_proposal: dict[str, JsonValue] | None = None
    schedule_proposal: dict[str, JsonValue] | None = None
    evolution_proposal: dict[str, JsonValue] | None = None
    openclaw_proposal: dict[str, JsonValue] | None = None
    repair_proposal: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def populate_failure_diagnostics(self) -> RunDetailResponse:
        if not self.failure_diagnostics:
            self.failure_diagnostics = _failure_diagnostics_from_run_events(self.events)
        if not self.tool_lifecycle:
            self.tool_lifecycle = _tool_lifecycle_from_run_events(self.events)
        self.model_outcome_summary = _model_outcome_summary_from_run_events(self.events)
        self.orchestration_protocol_summary = _orchestration_protocol_summary_from_run_events(
            self.events
        )
        self.model_capability_negotiation_summary = (
            _model_capability_negotiation_summary_from_run_events(self.events)
        )
        self.capability_execution_summary = _capability_execution_summary_from_run_events(
            self.events
        )
        self.runtime_recovery_summary = _runtime_recovery_summary_from_run_events(
            self.events
        )
        return self


class RunDebugArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    title: str
    has_text: bool
    text_preview: str | None = None


class RunDebugResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: str
    mode: str
    failed_stage: str | None = None
    failure_reason: str
    partial_output_available: bool
    request_preview: str
    events: list[RunEventResponse]
    artifacts: list[RunDebugArtifactResponse]
    explicit_details: dict[str, str]
    recommendation: str
    generated_at: datetime


class RunDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    deleted: bool


class BulkFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    code: str
    message: str


class RunBulkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(min_length=1, max_length=1000)


class RunBulkDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: list[RunDeleteResponse]
    failed: list[BulkFailureResponse]


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    runs: list[RunDetailResponse]


class SkillUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)


class SkillVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    source_filename: str | None = None
    package_version_id: str | None = None
    content_sha256: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    is_current: bool = False


class SkillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: str
    scan_diff: list[str]
    requested_permissions: list[str]
    source_filename: str | None = None
    package_version_id: str | None = None
    content_sha256: str | None = None
    current_version_id: str | None = None
    versions: list[SkillVersionResponse] = Field(default_factory=list)


class SkillBulkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=1000)


class SkillBulkDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: list[str]
    failed: list[BulkFailureResponse]


class SkillArchiveSkippedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class SkillArchiveUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    bundle: bool
    items: list[SkillResponse]
    skipped: list[SkillArchiveSkippedResponse] = Field(default_factory=list)


class PluginCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    adapter: str = Field(
        default="plugin_runtime",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    permission_class: str = Field(
        default="plugin.use",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$|^[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$",
    )
    sandbox_profile: str = Field(
        default="remote_connector",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    policy_effect: Literal["inherit", "allow", "require_approval", "deny"] = "inherit"
    replay_safe: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=128)
    capability_config: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)
    input_schema: dict[str, JsonValue] | None = None
    output_schema: dict[str, JsonValue] | None = None

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        aliases: list[str] = []
        for alias in value:
            if (
                not isinstance(alias, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", alias) is None
            ):
                raise ValueError("plugin capability alias is invalid")
            aliases.append(alias)
        if len(set(aliases)) != len(aliases):
            raise ValueError("plugin capability aliases must be unique")
        return aliases

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_capability_schema(
        cls, value: dict[str, JsonValue] | None
    ) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        schema_type = value.get("type")
        if schema_type is not None and not isinstance(schema_type, str):
            raise ValueError("plugin capability schema type must be a string")
        return value


class PluginResourceRequest(NamedResourceRequest):
    description: str | None = Field(default=None, max_length=1000)
    version: str = Field(
        default="local",
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:+-]*$",
    )
    resource_config: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)
    endpoint_url: str | None = Field(default=None, max_length=2048)
    domain_allowlist: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    credential_ref: str | None = Field(default=None, max_length=128)
    credential_header: str = Field(
        default="X-Plugin-Credential",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9-]*$",
    )
    credential_scheme: str = Field(default="Bearer", max_length=32)
    capabilities: list[PluginCapabilityRequest] = Field(default_factory=list, max_length=256)

    @field_validator("capabilities")
    @classmethod
    def validate_capability_ids(
        cls, value: list[PluginCapabilityRequest]
    ) -> list[PluginCapabilityRequest]:
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("plugin capability ids must be unique")
        return value


class PluginPackageSignatureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["ed25519"]
    key_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$",
    )
    value: str = Field(
        min_length=86,
        max_length=86,
        pattern=r"^[A-Za-z0-9_-]{86}$",
    )


class PluginSigningKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$",
    )
    algorithm: Literal["ed25519"]
    public_key: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    not_before: datetime | None = None
    not_after: datetime | None = None

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        _decode_base64url_bytes(value, expected_length=32, label="public_key")
        return value

    @field_validator("not_before", "not_after")
    @classmethod
    def validate_window_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("signing key window timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_activation_window(self) -> PluginSigningKeyRequest:
        if (
            self.not_before is not None
            and self.not_after is not None
            and self.not_after <= self.not_before
        ):
            raise ValueError("not_after must be later than not_before")
        return self


class PluginSigningKeyResponse(PluginSigningKeyRequest):
    trusted: bool = True


PluginSignatureVerification = Literal[
    "not_provided",
    "not_verified",
    "untrusted_key",
    "verified",
    "failed",
]
PluginPackageInstallMode = Literal["scan_only", "runtime_registered"]

SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS = frozenset(("1.0",))
SUPPORTED_RUNTIME_REGISTERED_PACKAGE_RUNTIMES = frozenset(("python",))
SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS = frozenset(("local_process",))
PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON = (
    "plugin package dependencies are not supported by this runtime"
)


class PluginPackageArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_key: str = Field(
        min_length=103,
        max_length=512,
        pattern=r"^[0-9a-f-]{36}/[a-z0-9][a-z0-9_.-]{0,127}/[a-f0-9]{64}$",
    )
    content_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    file_count: int = Field(ge=1, le=512)
    total_size_bytes: int = Field(ge=0, le=2_000_000)
    stored_at: datetime
    quarantine_state: Literal["stored"] = "stored"


class PluginPackageDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["python"] = "python"
    source: Literal["pypi"] = "pypi"
    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9!+.,<=>~_-]{0,127}$",
    )


class PluginPackageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["manifest_only", "adapter_package"] = "manifest_only"
    package_version: str | None = Field(
        default=None,
        max_length=64,
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
            r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        ),
    )
    adapter_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$",
    )
    sdk_api_version: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^[1-9][0-9]*\.[0-9]+$",
    )
    signature: PluginPackageSignatureMetadata | None = None
    signature_verification: PluginSignatureVerification = "not_provided"
    verified_public_key_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    signature_trust_expires_at: datetime | None = None
    approval_state: Literal["not_required", "pending", "approved", "rejected"] = "not_required"
    approval_reason: str = Field(default="", max_length=256)
    approved_by: str | None = Field(default=None, max_length=128)
    approved_at: datetime | None = None
    activation_state: Literal[
        "not_applicable",
        "blocked_unsigned",
        "blocked_unverified_signature",
        "blocked_untrusted_key",
        "blocked_failed_signature",
        "blocked_pending_approval",
        "blocked_rejected_approval",
        "blocked_unsupported_runtime",
        "verified_scan_only",
        "eligible",
    ] = "not_applicable"
    activation_reason: str = Field(default="", max_length=256)
    runtime: Literal["none", "python", "node", "container", "mcp_remote"] = "none"
    entrypoint: str | None = Field(default=None, max_length=255)
    isolation: Literal[
        "none",
        "remote_connector",
        "in_process",
        "local_process",
        "container",
        "mcp_remote",
    ] = "none"
    install_mode: PluginPackageInstallMode = "scan_only"
    dependencies: tuple[PluginPackageDependency, ...] = Field(default_factory=tuple, max_length=32)
    artifact: PluginPackageArtifactMetadata | None = None

    @field_validator("signature_trust_expires_at")
    @classmethod
    def validate_signature_trust_expiry(cls, value: datetime | None) -> datetime | None:
        return PluginSigningKeyRequest.validate_window_timestamp(value)

    @model_validator(mode="after")
    def derive_server_controlled_state(self) -> PluginPackageMetadata:
        if self.signature is None:
            verification: PluginSignatureVerification = "not_provided"
        elif self.signature_verification == "not_provided":
            verification = "not_verified"
        else:
            verification = self.signature_verification
        if self.kind == "manifest_only":
            approval_state: Literal["not_required", "pending", "approved", "rejected"] = (
                "not_required"
            )
            approval_reason = "manifest-only package has no executable activation target"
        elif self.approval_state == "not_required":
            approval_state = "pending"
            approval_reason = "adapter package requires plugin approval before activation"
        else:
            approval_state = self.approval_state
            approval_reason = self.approval_reason
        self.signature_verification = verification
        self.approval_state = approval_state
        self.approval_reason = approval_reason
        if approval_state in {"not_required", "pending"}:
            self.approved_by = None
            self.approved_at = None
        state, reason = _plugin_package_activation_state(self, verification)
        self.activation_state = state
        self.activation_reason = reason
        return self


class PluginArchiveManifest(PluginResourceRequest):
    package: PluginPackageMetadata | None = None


class PluginResourceResponse(PluginResourceRequest):
    source_filename: str | None = Field(
        default=None,
        max_length=255,
        pattern=r"^[^/\\\x00]+$",
    )
    content_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    package_metadata: PluginPackageMetadata | None = None
    status: str = Field(pattern=r"^(disabled|stopped|running|failed)$")
    health: str = Field(pattern=r"^(disabled|stopped|healthy|unhealthy|failed|unknown)$")
    last_error_type: str | None = Field(default=None, max_length=128)


class PluginArchiveInstallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    content_sha256: str
    plugin: PluginResourceResponse


class PluginPackageApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=256)


PluginActivationState = Literal[
    "not_applicable",
    "blocked_unsigned",
    "blocked_unverified_signature",
    "blocked_untrusted_key",
    "blocked_failed_signature",
    "blocked_pending_approval",
    "blocked_rejected_approval",
    "blocked_unsupported_runtime",
    "verified_scan_only",
    "eligible",
]


def _plugin_package_activation_state(
    package: PluginPackageMetadata,
    signature_verification: PluginSignatureVerification,
) -> tuple[PluginActivationState, str]:
    if package.kind == "manifest_only":
        return (
            "not_applicable",
            "manifest-only package has no executable activation target",
        )
    if package.signature is None or signature_verification == "not_provided":
        return (
            "blocked_unsigned",
            "adapter packages must include a trusted verified signature before activation",
        )
    if signature_verification == "not_verified":
        return (
            "blocked_unverified_signature",
            "package signature has not been verified by the server",
        )
    if signature_verification == "untrusted_key":
        return (
            "blocked_untrusted_key",
            "package signature key is not trusted for this tenant",
        )
    if signature_verification == "failed":
        return (
            "blocked_failed_signature",
            "package signature verification failed",
        )
    if package.approval_state == "pending":
        return (
            "blocked_pending_approval",
            "adapter package requires plugin approval before activation",
        )
    if package.approval_state == "rejected":
        return (
            "blocked_rejected_approval",
            package.approval_reason or "adapter package approval was rejected",
        )
    if package.install_mode == "scan_only":
        return (
            "verified_scan_only",
            "package signature is verified, but install_mode=scan_only prevents activation",
        )
    if package.install_mode != "runtime_registered":
        return (
            "blocked_unsupported_runtime",
            "plugin package install mode is not supported for activation",
        )
    if package.sdk_api_version not in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS:
        return (
            "blocked_unsupported_runtime",
            "plugin package SDK API version is not supported for activation",
        )
    if package.runtime not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_RUNTIMES:
        return (
            "blocked_unsupported_runtime",
            "plugin package runtime is not supported for activation",
        )
    if package.isolation not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS:
        return (
            "blocked_unsupported_runtime",
            "plugin package isolation is not supported for activation",
        )
    if package.dependencies:
        return (
            "blocked_unsupported_runtime",
            PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON,
        )
    return (
        "eligible",
        "package signature, approval, SDK, adapter, and isolation policy allow execution",
    )


class PluginPolicyCapabilitySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    adapter: str
    permission_class: str
    sandbox_profile: str
    policy_effect: Literal["inherit", "allow", "require_approval", "deny"]
    replay_safe: bool
    aliases: list[str] = Field(default_factory=list, max_length=128)
    input_schema_declared: bool
    output_schema_declared: bool
    capability_config_key_count: int
    capability_config_keys: list[str] = Field(default_factory=list, max_length=128)
    redacted_capability_config_key_count: int


class PluginPolicySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: str
    enabled: bool
    status: str
    health: str
    capability_count: int
    adapters: list[str] = Field(default_factory=list, max_length=256)
    permission_classes: list[str] = Field(default_factory=list, max_length=256)
    policy_effects: list[Literal["inherit", "allow", "require_approval", "deny"]] = Field(
        default_factory=list,
        max_length=4,
    )
    sandbox_profiles: list[str] = Field(default_factory=list, max_length=256)
    resource_config_key_count: int
    resource_config_keys: list[str] = Field(default_factory=list, max_length=128)
    redacted_resource_config_key_count: int
    capabilities: list[PluginPolicyCapabilitySummaryResponse] = Field(
        default_factory=list,
        max_length=256,
    )


class PluginPolicyReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_count: int
    capability_count: int
    policy_effect_counts: dict[
        Literal["inherit", "allow", "require_approval", "deny"], int
    ] = Field(default_factory=dict, max_length=4)
    permission_class_counts: dict[str, int] = Field(default_factory=dict, max_length=256)
    sandbox_profile_counts: dict[str, int] = Field(default_factory=dict, max_length=256)
    plugins: list[PluginPolicySummaryResponse] = Field(default_factory=list, max_length=256)


class PluginAdapterCapabilityContractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    declared_sandbox_profiles: list[str] = Field(default_factory=list, max_length=32)
    runtime_sandbox_profiles: list[str] = Field(default_factory=list, max_length=32)


class PluginAdapterDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    resource_schema: dict[str, JsonValue]
    capability_schema: dict[str, JsonValue]
    argument_schema: dict[str, JsonValue]
    capability_contract: PluginAdapterCapabilityContractResponse = Field(
        default_factory=PluginAdapterCapabilityContractResponse,
    )

    @field_validator("resource_schema", "capability_schema", "argument_schema")
    @classmethod
    def validate_descriptor_schema(
        cls,
        value: dict[str, JsonValue],
        info: ValidationInfo,
    ) -> dict[str, JsonValue]:
        try:
            plugin_schema_validator(
                schema=value,
                invalid_schema_message=f"plugin adapter {info.field_name} is invalid",
                require_object_schema=True,
            )
        except PluginSchemaError as exc:
            raise ValueError(str(exc)) from None
        return value

    @model_validator(mode="after")
    def hydrate_capability_contract(self) -> PluginAdapterDescriptorResponse:
        descriptor: Mapping[str, JsonValue] = {
            "capability_schema": self.capability_schema,
        }
        self.capability_contract = PluginAdapterCapabilityContractResponse(
            declared_sandbox_profiles=sorted(
                adapter_declared_sandbox_profiles(descriptor),
            ),
            runtime_sandbox_profiles=list(
                adapter_runtime_sandbox_profiles(descriptor),
            ),
        )
        return self


class McpServerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    health: str
    allowed_tools: list[str]
    transport: str = Field(default="streamable_http", pattern=r"^(stdio|sse|streamable_http)$")
    command: str | None = Field(default=None, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=128)
    url: str | None = Field(default=None, max_length=2048)
    executable_allowlist: list[str] = Field(default_factory=list, max_length=64)
    domain_allowlist: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float = Field(default=10, gt=0, le=120)


class McpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    allowed_tools: list[str] = Field(default_factory=list, max_length=256)
    transport: str = Field(default="streamable_http", pattern=r"^(stdio|sse|streamable_http)$")
    command: str | None = Field(default=None, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=128)
    url: str | None = Field(default=None, max_length=2048)
    executable_allowlist: list[str] = Field(default_factory=list, max_length=64)
    domain_allowlist: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float = Field(default=10, gt=0, le=120)


class CapabilityManifestItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    adapter: str = Field(min_length=1, max_length=128)
    permission_class: str = Field(min_length=1, max_length=128)
    sandbox_profile: str = Field(min_length=1, max_length=128)
    policy_effect: Literal["inherit", "allow", "require_approval", "deny"] = "inherit"
    available: bool
    availability_reason: str | None = None
    replay_safe: bool
    aliases: list[str] = Field(default_factory=list, max_length=128)
    input_schema: dict[str, JsonValue] | None = None
    output_schema: dict[str, JsonValue] | None = None


class CapabilityManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    capabilities: list[CapabilityManifestItemResponse]


class ChannelRuntimeStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    ready: bool
    connection_attempts: int = Field(ge=0)
    reconnects: int = Field(ge=0)
    received_events: int = Field(ge=0)
    submitted_messages: int = Field(ge=0)
    ignored_events: int = Field(ge=0)
    failures: int = Field(ge=0)
    last_error_type: str | None = None
    last_error_message: str | None = None


class ChannelStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: str
    transports: list[str]
    webhook_path: str | None = None
    public_webhook_url: str | None = None
    missing: list[str] = Field(default_factory=list)
    configured: list[str] = Field(default_factory=list)
    configured_sources: dict[str, str] = Field(default_factory=dict)
    command_aliases: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    runtime: ChannelRuntimeStatusResponse | None = None


class ChannelConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, str] = Field(default_factory=dict, max_length=64)


class ChannelConfigSaveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    saved: list[str]
    status: ChannelStatusResponse


@dataclass(frozen=True, slots=True)
class ChannelDefinition:
    id: str
    name: str
    transports: tuple[str, ...]
    required_env: tuple[str, ...]
    webhook_path: str | None
    notes: tuple[str, ...]


CHANNEL_DEFINITIONS: tuple[ChannelDefinition, ...] = (
    ChannelDefinition(
        id="feishu",
        name="飞书",
        transports=("webhook", "websocket"),
        required_env=(
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
        ),
        webhook_path="/channels/feishu/events",
        notes=(
            "Webhook 已挂载在主 API 服务，不需要额外暴露 8001。",
            "飞书已接入运行提交链路。",
        ),
    ),
    ChannelDefinition(
        id="dingtalk",
        name="钉钉",
        transports=("webhook",),
        required_env=("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_WEBHOOK_TOKEN"),
        webhook_path="/channels/dingtalk/events",
        notes=("配置齐全后可通过该 Webhook 接收钉钉消息，并提交到主 Agent。",),
    ),
    ChannelDefinition(
        id="wecom_bot",
        name="企微智能机器人",
        transports=("webhook",),
        required_env=("WECOM_BOT_WEBHOOK_KEY", "WECOM_BOT_WEBHOOK_TOKEN"),
        webhook_path="/channels/wecom/bot/events",
        notes=("适合企业微信群机器人场景；配置齐全后可接收消息。",),
    ),
    ChannelDefinition(
        id="wecom_app",
        name="企业微信 Agent",
        transports=("callback",),
        required_env=("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN"),
        webhook_path="/channels/wecom/app/events",
        notes=("适合把企业微信私聊、审批入口和内部任务流接入主 Agent；配置齐全后可接收回调消息。",),
    ),
    ChannelDefinition(
        id="wechat_official",
        name="公众号",
        transports=("callback",),
        required_env=("WECHATMP_APP_ID", "WECHATMP_APP_SECRET", "WECHATMP_TOKEN"),
        webhook_path="/channels/wechatmp/events",
        notes=("适合公众号消息入口；配置齐全后可接收文本消息。",),
    ),
    ChannelDefinition(
        id="wechat_customer_service",
        name="微信客服",
        transports=("callback",),
        required_env=("WECHAT_KF_CORP_ID", "WECHAT_KF_SECRET", "WECHAT_KF_TOKEN"),
        webhook_path="/channels/wechat-kf/events",
        notes=("适合微信客服入口；配置齐全后可接收客服消息。",),
    ),
    ChannelDefinition(
        id="telegram",
        name="Telegram",
        transports=("webhook",),
        required_env=("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_TOKEN", "AGENT_HUB_PUBLIC_URL"),
        webhook_path="/channels/telegram/events",
        notes=("适合海外聊天机器人场景；配置齐全后可接收 Bot Webhook。",),
    ),
    ChannelDefinition(
        id="slack",
        name="Slack",
        transports=("events_api",),
        required_env=("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"),
        webhook_path="/channels/slack/events",
        notes=("适合团队协作空间；配置齐全后可接收事件消息。",),
    ),
    ChannelDefinition(
        id="qq",
        name="QQ 机器人",
        transports=("webhook",),
        required_env=("QQ_BOT_APP_ID", "QQ_BOT_TOKEN", "QQ_WEBHOOK_TOKEN"),
        webhook_path="/channels/qq/events",
        notes=("适合 QQ 频道或机器人入口；配置齐全后可接收事件消息。",),
    ),
    ChannelDefinition(
        id="custom_webhook",
        name="自定义 Webhook",
        transports=("webhook",),
        required_env=("CUSTOM_WEBHOOK_TOKEN",),
        webhook_path="/channels/custom/events",
        notes=("用于兼容其他支持 HTTP Webhook 的聊天软件；配置共享令牌后可接收 JSON 文本消息。",),
    ),
)


class MemoryRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=20_000)
    heat: float = Field(default=0.5, ge=0, le=1)
    locked: bool = False
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    summary_period: str = Field(default="none", pattern=r"^(none|day|week|month)$")
    recall_count: int = Field(default=0, ge=0)
    last_recalled_at: str | None = None


class MemoryCreateRequest(MemoryRecordRequest):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    scope: str = Field(default="tenant", min_length=1, max_length=128)


class MemoryRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    value: str
    heat: float = Field(default=0.5, ge=0, le=1)
    locked: bool = False
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    summary_period: str = Field(default="none", pattern=r"^(none|day|week|month)$")
    recall_count: int = Field(default=0, ge=0)
    last_recalled_at: str | None = None


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    actor: str
    action: str
    resource: str
    details: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


class LogEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str = Field(
        pattern=r"^(audit|model_error|mode_error|feature_error|agent_error|channel_error)$"
    )
    level: str = Field(pattern=r"^(info|warning|error)$")
    title: str
    message: str
    source: str
    details: dict[str, str]
    created_at: datetime


class OpenClawRemoteAdapterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(pattern=r"^(linux|windows|macos)$")
    target_type: str = Field(pattern=r"^(server|computer|desktop|filesystem|screen)$")
    target: str = Field(min_length=1, max_length=256)
    base_url: str = Field(min_length=1, max_length=2048)
    credential_ref: str = Field(min_length=1, max_length=128)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OpenClaw adapter URL must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "OpenClaw adapter URL must not contain credentials, query, or fragment"
            )
        normalized_path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


PluginPackageSubprocessRegistrationStatus = Literal[
    "disabled",
    "no_adapter_ids",
    "unsupported_isolation_backend",
    "missing_isolation_launcher",
    "launcher_path_not_absolute",
    "launcher_not_found",
    "launcher_not_executable",
    "ready",
]


class SystemSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_mode: str = Field(
        default="auto",
        pattern=r"^(auto|direct|dispatch|discuss|hybrid)$",
    )
    default_workflow_id: str | None = Field(default=None, max_length=128)
    default_agent_ids: list[str] = Field(default_factory=list, max_length=64)
    log_level: str = Field(default="warning", pattern=r"^(warning|error)$")
    hermes_enabled: bool = True
    safe_tools_enabled: bool = True
    require_approval_for_tools: bool = True
    tool_approval_mode: str = Field(default="auto_review", pattern=r"^(ask|auto_review)$")
    allow_main_agent_override: bool = False
    allow_temporary_agents: bool = False
    vibe_coding_enabled: bool = False
    multimedia_generation_enabled: bool = False
    openclaw_enabled: bool = False
    openclaw_mode: str = Field(default="ask", pattern=r"^(ask|read_only|auto_review|trusted_auto)$")
    openclaw_allowed_commands: list[list[str]] = Field(default_factory=list, max_length=64)
    openclaw_remote_adapters: list[OpenClawRemoteAdapterSettings] = Field(
        default_factory=list, max_length=32
    )
    temporary_agent_policy: str = Field(
        default="主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
        max_length=10_000,
    )
    channel_entry: str = Field(default="web", max_length=64)
    attachment_retention_days: int = Field(default=7, ge=1, le=365)
    attachment_max_mb: int = Field(default=25, ge=1, le=200)
    plugin_package_subprocess_registration_status: (
        PluginPackageSubprocessRegistrationStatus | None
    ) = None

    @field_validator("openclaw_allowed_commands")
    @classmethod
    def validate_openclaw_allowed_commands(cls, value: list[list[str]]) -> list[list[str]]:
        for argv in value:
            if not argv or len(argv) > 32:
                raise ValueError("OpenClaw allowed commands must be nonempty bounded argv lists")
            OpenClawOperationRequest.validate_argv(argv)
        return value


class SystemSettingsResponse(SystemSettingsRequest):
    pass


_PLUGIN_PACKAGE_SUBPROCESS_REGISTRATION_STATUSES = frozenset(
    get_args(PluginPackageSubprocessRegistrationStatus)
)


def _migrate_system_settings_payload(payload: Mapping[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    if migrated.get("tool_approval_mode") not in {"ask", "auto_review"}:
        migrated["tool_approval_mode"] = "ask"
    return migrated


def _settings_with_runtime_status(
    settings: SystemSettingsResponse,
    request: Request,
) -> SystemSettingsResponse:
    status = getattr(
        request.app.state,
        "plugin_package_subprocess_registration_status",
        None,
    )
    if status not in _PLUGIN_PACKAGE_SUBPROCESS_REGISTRATION_STATUSES:
        status = None
    return settings.model_copy(
        update={"plugin_package_subprocess_registration_status": status}
    )


def _persisted_system_settings_payload(
    settings: SystemSettingsRequest | SystemSettingsResponse,
) -> dict[str, Any]:
    return settings.model_dump(
        exclude={"plugin_package_subprocess_registration_status"}
    )


class OpenClawOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(pattern=r"^(linux|windows|macos)$")
    kind: str = Field(pattern=r"^(server_command|desktop_action|screen_read|file_read)$")
    target: str = Field(min_length=1, max_length=256)
    argv: list[str] = Field(default_factory=list, max_length=32)
    risk_level: str = Field(default="medium", pattern=r"^(low|medium|high)$")
    reason: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(
        default=None, max_length=128, pattern=r"^openclaw_session_[a-f0-9]+$"
    )

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 512:
                raise ValueError("argv items must be nonblank bounded strings")
            if item != item.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                raise ValueError("argv items must be printable and unpadded")
        return value


class OpenClawOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str = Field(pattern=r"^(waiting_user_approval|approved|rejected|executed)$")
    approval_id: str
    requires_user_approval: bool
    platform: str
    kind: str
    operation: dict[str, object]
    approval_summary: str
    requested_by: str
    created_at: datetime
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    execution: dict[str, object] | None = None


class OpenClawResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern=r"^(approve|reject)$")


class OpenClawExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: OpenClawOperationResponse
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


class OpenClawAdapterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(pattern=r"^(linux|windows|macos)$")
    kind: str = Field(pattern=r"^(server_command|desktop_action|screen_read|file_read)$")
    target_type: str = Field(pattern=r"^(server|computer|desktop|filesystem|screen)$")
    status: str = Field(pattern=r"^(available|adapter_unavailable)$")
    execution_host: str
    requires_user_approval: bool
    supports_read_only: bool
    description: str


class OpenClawSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(pattern=r"^(linux|windows|macos)$")
    target_type: str = Field(pattern=r"^(server|computer|desktop)$")
    target: str = Field(min_length=1, max_length=256)
    purpose: str = Field(min_length=1, max_length=2_000)


class OpenClawSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str = Field(pattern=r"^(active|paused|stopped|adapter_unavailable)$")
    adapter_status: str = Field(pattern=r"^(available|adapter_unavailable)$")
    mode: str = Field(pattern=r"^(ask|read_only|auto_review|trusted_auto)$")
    platform: str
    target_type: str
    target: str
    purpose: str
    execution_host: str
    requested_by: str
    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None = None
    operation_ids: list[str] = Field(default_factory=list)


class OpenClawSessionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern=r"^(pause|resume|stop)$")


class MultimediaGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(image|video|audio)$")
    logical_model: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    prompt: str = Field(min_length=1, max_length=20_000)


class MultimediaGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    logical_model: str
    deployment_id: str
    text: str | None


class MultimediaGenerationJobRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_id: str = Field(min_length=1, max_length=128)


class MultimediaArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    uri: str | None
    text: str | None


class MultimediaGenerationJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    logical_model: str
    prompt: str
    status: str
    artifacts: list[MultimediaArtifactResponse]
    executor_id: str | None
    error: str | None


class ScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=65_536)
    mode: str = Field(default="auto", pattern=r"^(auto|direct|dispatch|discuss|hybrid)$")
    workflow_id: str = Field(default="scheduled_task", pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    kind: str = Field(pattern=r"^(one_time|cron)$")
    run_at: datetime | None = None
    cron: str | None = Field(default=None, max_length=64)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    misfire_policy: str = Field(default="fire_once", pattern=r"^(fire_once|skip)$")
    budget: int = Field(default=16_384, ge=1, le=10_000_000)
    idempotency_key: str | None = Field(default=None, max_length=256)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _safe_log_details(value)


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    status: str
    kind: str
    mode: str
    workflow_id: str
    message: str
    timezone: str
    next_fire_at: datetime | None
    run_at: datetime | None
    cron: str | None
    misfire_policy: str
    budget: int
    metadata: dict[str, str]


class ScheduleTickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: datetime


class ScheduleTickResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fired: list[UUID]


class MultimediaGenerationExecutorProtocol(Protocol):
    def submit(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationJob: ...

    def get_job(self, job_id: str) -> MultimediaGenerationJob: ...

    async def run_job(
        self,
        job_id: str,
        *,
        executor_id: str,
    ) -> MultimediaGenerationJob: ...

    async def generate(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationResult: ...


class SchedulerServiceProtocol(Protocol):
    async def add_schedule(
        self,
        schedule: ScheduleDefinition,
        *,
        now: datetime,
    ) -> ScheduleDefinition: ...

    async def create_schedule(
        self,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        name: str,
        message: str,
        mode: TaskMode,
        workflow: str,
        budget: int,
        spec: OneTimeScheduleSpec | CronScheduleSpec,
        idempotency_key: str,
        now: datetime,
        user_visible: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> ScheduleDefinition: ...

    async def list_schedules(self, *, tenant_id: UUID) -> tuple[ScheduleDefinition, ...]: ...

    async def delete_schedule(self, *, tenant_id: UUID, schedule_id: UUID) -> None: ...

    async def tick(
        self,
        tenant_id: UUID | None = None,
        *,
        now: datetime,
    ) -> tuple[UUID, ...]: ...


def _openclaw_operation_response(
    *,
    operation_id: str,
    request: OpenClawOperationRequest,
    actor: str,
    mode: str,
) -> OpenClawOperationResponse:
    return OpenClawOperationResponse(
        id=operation_id,
        status="waiting_user_approval",
        approval_id=f"{operation_id}_approval",
        requires_user_approval=True,
        platform=request.platform,
        kind=request.kind,
        operation=request.model_dump(),
        approval_summary=(
            f"OpenClaw {request.platform} {request.kind} on {request.target}; "
            f"risk={request.risk_level}; mode={mode}; reason={request.reason}"
        ),
        requested_by=actor,
        created_at=datetime.now(UTC),
    )


def _openclaw_audit_details(request: OpenClawOperationRequest, mode: str) -> dict[str, object]:
    return {
        "platform": request.platform,
        "kind": request.kind,
        "target": request.target,
        "risk_level": request.risk_level,
        "mode": mode,
    }


def _openclaw_can_auto_approve(
    request: OpenClawOperationRequest, settings: SystemSettingsResponse
) -> bool:
    if settings.openclaw_mode not in {"auto_review", "trusted_auto"}:
        return False
    if request.risk_level != "low":
        return False
    if request.platform != "linux" or request.kind != "server_command":
        return False
    return openclaw_command_allowed(request.argv, settings.openclaw_allowed_commands)


def _openclaw_proposal_string(proposal: Mapping[str, JsonValue], key: str) -> str:
    value = proposal.get(key)
    return value if isinstance(value, str) else ""


def _openclaw_operation_request_from_run_detail(
    detail: RunDetailResponse,
) -> OpenClawOperationRequest:
    proposal = detail.openclaw_proposal
    if detail.status != "waiting_approval" or proposal is None:
        raise PublicAPIError(
            409,
            "openclaw_proposal_missing",
            "Run does not contain a pending OpenClaw proposal",
        )
    platform = _openclaw_proposal_string(proposal, "platform")
    kind = _openclaw_proposal_string(proposal, "kind")
    target = _openclaw_normalized_target(
        platform=platform,
        kind=kind,
        target=_openclaw_proposal_string(proposal, "target"),
    )
    operation_text = _openclaw_proposal_string(proposal, "operation_text") or detail.request
    source_conversation_id = (
        _openclaw_proposal_string(proposal, "source_conversation_id")
        or detail.conversation_id
        or ""
    )
    try:
        return OpenClawOperationRequest(
            platform=platform,
            kind=kind,
            target=target,
            argv=_openclaw_argv_from_operation_text(kind, operation_text),
            risk_level="medium",
            reason=_openclaw_reason_from_run_proposal(
                detail,
                operation_text=operation_text,
                source_conversation_id=source_conversation_id,
            ),
        )
    except ValidationError as exc:
        raise PublicAPIError(
            422,
            "openclaw_proposal_invalid",
            "OpenClaw proposal cannot be converted into a controlled operation",
        ) from exc


def _openclaw_normalized_target(*, platform: str, kind: str, target: str) -> str:
    if platform == "linux" and kind == "server_command" and target in {"", "linux-server"}:
        return "agent-hub-server"
    return target or "operator-selected"


def _openclaw_reason_from_run_proposal(
    detail: RunDetailResponse,
    *,
    operation_text: str,
    source_conversation_id: str,
) -> str:
    parts = [
        "Created from chat OpenClaw proposal",
        f"run_id={detail.id}",
    ]
    if source_conversation_id:
        parts.append(f"conversation_id={source_conversation_id}")
    if detail.request:
        parts.append(f"request={detail.request}")
    if operation_text and operation_text != detail.request:
        parts.append(f"operation_text={operation_text}")
    return "; ".join(parts)[:2000]


_OPENCLAW_COMMAND_PHRASE_RE = re.compile(
    r"(?:execute|run|\u6267\u884c|\u8fd0\u884c)\s+(?P<command>.+)",
    re.IGNORECASE,
)
_OPENCLAW_COMMAND_STOP_RE = re.compile(r"\s+(?:on|after|before|with|in)\b.*$", re.IGNORECASE)


def _openclaw_argv_from_operation_text(kind: str, operation_text: str) -> list[str]:
    if kind != "server_command":
        return []
    match = _OPENCLAW_COMMAND_PHRASE_RE.search(operation_text)
    if match is not None:
        command = _OPENCLAW_COMMAND_STOP_RE.sub("", match.group("command")).strip(" .。；;")
    else:
        command = operation_text.strip(" .。；;")
        if _looks_like_openclaw_natural_language_request(command):
            return []
    if not command:
        return []
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _looks_like_openclaw_natural_language_request(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in ("openclaw", "please", "server", "approval")) or any(
        marker in value for marker in ("请", "服务器", "审批", "确认")
    )


def _openclaw_operation_target_types(kind: str) -> frozenset[str]:
    if kind == "server_command":
        return frozenset({"server"})
    if kind == "desktop_action":
        return frozenset({"computer", "desktop"})
    if kind == "screen_read":
        return frozenset({"computer", "desktop", "screen"})
    if kind == "file_read":
        return frozenset({"computer", "filesystem", "server"})
    return frozenset()


def _openclaw_default_target_type(kind: str) -> str:
    return {
        "server_command": "server",
        "desktop_action": "desktop",
        "screen_read": "screen",
        "file_read": "filesystem",
    }[kind]


def _remote_adapter_host(adapter: OpenClawRemoteAdapterSettings) -> str:
    parsed = urlsplit(adapter.base_url)
    return parsed.netloc or f"remote-{adapter.platform}-host"


def _configured_openclaw_adapter_for_kind(
    settings: SystemSettingsResponse,
    *,
    platform: str,
    kind: str,
) -> OpenClawRemoteAdapterSettings | None:
    target_types = _openclaw_operation_target_types(kind)
    return next(
        (
            adapter
            for adapter in settings.openclaw_remote_adapters
            if adapter.platform == platform and adapter.target_type in target_types
        ),
        None,
    )


def _configured_openclaw_adapter_for_operation(
    settings: SystemSettingsResponse,
    request: OpenClawOperationRequest,
) -> OpenClawRemoteAdapterSettings | None:
    target_types = _openclaw_operation_target_types(request.kind)
    return next(
        (
            adapter
            for adapter in settings.openclaw_remote_adapters
            if adapter.platform == request.platform
            and adapter.target == request.target
            and adapter.target_type in target_types
        ),
        None,
    )


def _configured_openclaw_adapter_for_session(
    settings: SystemSettingsResponse,
    request: OpenClawSessionRequest,
) -> OpenClawRemoteAdapterSettings | None:
    return next(
        (
            adapter
            for adapter in settings.openclaw_remote_adapters
            if adapter.platform == request.platform
            and adapter.target == request.target
            and adapter.target_type == request.target_type
        ),
        None,
    )


def _openclaw_adapter_responses(
    settings: SystemSettingsResponse | None = None,
) -> tuple[OpenClawAdapterResponse, ...]:
    descriptions = {
        ("linux", "server_command"): (
            "Runs exact allowlisted argv commands on the 魔方 agent Linux server after approval."
        ),
        (
            "linux",
            "desktop_action",
        ): "Requires a connected Linux desktop OpenClaw adapter before execution.",
        (
            "linux",
            "screen_read",
        ): "Requires a connected Linux screen OpenClaw adapter before execution.",
        (
            "linux",
            "file_read",
        ): "Requires a connected Linux filesystem OpenClaw adapter before execution.",
        (
            "windows",
            "server_command",
        ): "Requires a connected Windows OpenClaw adapter before execution.",
        (
            "windows",
            "desktop_action",
        ): "Requires a connected Windows desktop OpenClaw adapter before execution.",
        (
            "windows",
            "screen_read",
        ): "Requires a connected Windows screen OpenClaw adapter before execution.",
        (
            "windows",
            "file_read",
        ): "Requires a connected Windows filesystem OpenClaw adapter before execution.",
        (
            "macos",
            "server_command",
        ): "Requires a connected macOS OpenClaw adapter before execution.",
        (
            "macos",
            "desktop_action",
        ): "Requires a connected macOS desktop OpenClaw adapter before execution.",
        (
            "macos",
            "screen_read",
        ): "Requires a connected macOS screen OpenClaw adapter before execution.",
        (
            "macos",
            "file_read",
        ): "Requires a connected macOS filesystem OpenClaw adapter before execution.",
    }
    adapters: list[OpenClawAdapterResponse] = []
    for platform in ("linux", "windows", "macos"):
        for kind in ("server_command", "desktop_action", "screen_read", "file_read"):
            configured = (
                None
                if settings is None
                else _configured_openclaw_adapter_for_kind(settings, platform=platform, kind=kind)
            )
            local_linux = platform == "linux" and kind == "server_command"
            available = local_linux or configured is not None
            if configured is not None:
                host = _remote_adapter_host(configured)
                description = (
                    f"Uses configured remote OpenClaw adapter for {configured.target} at {host}; "
                    "execution still requires policy approval and adapter authentication."
                )
            else:
                host = "agent-hub-server" if platform == "linux" else f"remote-{platform}-host"
                description = descriptions[(platform, kind)]
            adapters.append(
                OpenClawAdapterResponse(
                    platform=platform,
                    kind=kind,
                    target_type=_openclaw_default_target_type(kind),
                    status="available" if available else "adapter_unavailable",
                    execution_host=host,
                    requires_user_approval=True,
                    supports_read_only=kind in {"screen_read", "file_read"},
                    description=description,
                )
            )
    return tuple(adapters)


def _openclaw_session_host(
    request: OpenClawSessionRequest, settings: SystemSettingsResponse
) -> str:
    if request.platform == "linux" and request.target_type == "server":
        return "agent-hub-server"
    configured = _configured_openclaw_adapter_for_session(settings, request)
    if configured is not None:
        return _remote_adapter_host(configured)
    return f"remote-{request.platform}-host"


def _openclaw_session_adapter_status(
    request: OpenClawSessionRequest, settings: SystemSettingsResponse
) -> str:
    if request.platform == "linux" and request.target_type == "server":
        return "available"
    return (
        "available"
        if _configured_openclaw_adapter_for_session(settings, request) is not None
        else "adapter_unavailable"
    )


def _openclaw_session_response(
    *,
    session_id: str,
    request: OpenClawSessionRequest,
    actor: str,
    mode: str,
    settings: SystemSettingsResponse,
) -> OpenClawSessionResponse:
    now = datetime.now(UTC)
    adapter_status = _openclaw_session_adapter_status(request, settings)
    return OpenClawSessionResponse(
        id=session_id,
        status="active" if adapter_status == "available" else "adapter_unavailable",
        adapter_status=adapter_status,
        mode=mode,
        platform=request.platform,
        target_type=request.target_type,
        target=request.target,
        purpose=request.purpose,
        execution_host=_openclaw_session_host(request, settings),
        requested_by=actor,
        created_at=now,
        updated_at=now,
        operation_ids=[],
    )


def _openclaw_session_audit_details(
    session: OpenClawSessionResponse,
    *,
    action: str | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "platform": session.platform,
        "target_type": session.target_type,
        "target": session.target,
        "adapter_status": session.adapter_status,
        "status": session.status,
        "mode": session.mode,
    }
    if action is not None:
        details["action"] = action
    return details


def _updated_openclaw_session(
    session: OpenClawSessionResponse,
    request: OpenClawSessionActionRequest,
) -> OpenClawSessionResponse:
    now = datetime.now(UTC)
    if session.status == "stopped":
        raise PublicAPIError(
            409,
            "openclaw_session_closed",
            "OpenClaw session is already stopped",
        )
    if request.action == "stop":
        return session.model_copy(
            update={"status": "stopped", "updated_at": now, "stopped_at": now}
        )
    if session.status == "adapter_unavailable":
        raise PublicAPIError(
            409,
            "openclaw_adapter_unavailable",
            "OpenClaw adapter is not available for this session",
        )
    if request.action == "pause":
        return session.model_copy(update={"status": "paused", "updated_at": now})
    return session.model_copy(update={"status": "active", "updated_at": now})


def _validate_openclaw_session_for_operation(
    session: OpenClawSessionResponse,
    request: OpenClawOperationRequest,
) -> None:
    if session.status != "active":
        raise PublicAPIError(
            409,
            "openclaw_session_not_active",
            "OpenClaw operation requires an active control session",
        )
    if session.platform != request.platform or session.target != request.target:
        raise PublicAPIError(
            409,
            "openclaw_session_target_mismatch",
            "OpenClaw operation target does not match the control session",
        )
    if session.target_type not in _openclaw_operation_target_types(request.kind):
        raise PublicAPIError(
            409,
            "openclaw_session_target_mismatch",
            "OpenClaw operation kind does not match the control session type",
        )


def _attach_openclaw_operation_to_session_payload(
    session: OpenClawSessionResponse,
    operation_id: str,
    request: OpenClawOperationRequest,
) -> OpenClawSessionResponse:
    _validate_openclaw_session_for_operation(session, request)
    operation_ids = list(session.operation_ids)
    if operation_id not in operation_ids:
        operation_ids.append(operation_id)
    return session.model_copy(
        update={"operation_ids": operation_ids, "updated_at": datetime.now(UTC)}
    )


async def _execute_openclaw_operation(
    operation: OpenClawOperationResponse,
    settings: SystemSettingsResponse,
    *,
    actor: str,
    adapter_token_resolver: Callable[[str], Awaitable[str]] | None = None,
) -> tuple[OpenClawOperationResponse, OpenClawCommandResult]:
    if operation.status != "approved":
        raise PublicAPIError(
            409,
            "openclaw_not_approved",
            "OpenClaw operation must be approved before execution",
        )
    if not settings.openclaw_enabled:
        raise PublicAPIError(409, "openclaw_disabled", "OpenClaw is disabled")

    request = OpenClawOperationRequest.model_validate(operation.operation)
    if settings.openclaw_mode == "read_only" and request.kind not in {"screen_read", "file_read"}:
        raise PublicAPIError(
            403, "openclaw_read_only", "OpenClaw read-only mode blocks this operation"
        )

    if request.platform == "linux" and request.kind == "server_command":
        if not openclaw_command_allowed(request.argv, settings.openclaw_allowed_commands):
            raise PublicAPIError(
                403,
                "openclaw_command_denied",
                "OpenClaw command is not in the allowed command list",
            )
        result = await run_openclaw_command(request.argv)
    else:
        adapter = _configured_openclaw_adapter_for_operation(settings, request)
        if adapter is None:
            raise PublicAPIError(
                409,
                "openclaw_adapter_unavailable",
                "OpenClaw execution adapter is not available for this operation",
            )
        if request.kind == "server_command" and not openclaw_command_allowed(
            request.argv, settings.openclaw_allowed_commands
        ):
            raise PublicAPIError(
                403,
                "openclaw_command_denied",
                "OpenClaw command is not in the allowed command list",
            )
        if adapter_token_resolver is None:
            raise PublicAPIError(
                409,
                "openclaw_adapter_unavailable",
                "OpenClaw adapter credentials are not available",
            )
        try:
            adapter_token = await adapter_token_resolver(adapter.credential_ref)
            result = await run_remote_openclaw_operation(
                OpenClawRemoteAdapter(
                    platform=adapter.platform,
                    target_type=adapter.target_type,
                    target=adapter.target,
                    base_url=adapter.base_url,
                ),
                operation_id=operation.id,
                operation=request.model_dump(),
                bearer_token=adapter_token,
            )
        except (KeyError, SecretValidationError, OpenClawRemoteAdapterError):
            raise PublicAPIError(
                502,
                "openclaw_adapter_failed",
                "OpenClaw remote adapter execution failed",
            ) from None

    execution: dict[str, object] = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        "executed_by": actor,
        "executed_at": datetime.now(UTC),
    }
    return (
        operation.model_copy(update={"status": "executed", "execution": execution}),
        result,
    )


def _openclaw_execution_response(
    operation: OpenClawOperationResponse,
    result: OpenClawCommandResult,
) -> OpenClawExecutionResponse:
    return OpenClawExecutionResponse(
        operation=operation,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
    )


class MainAgentModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    api_base: str = Field(min_length=1, max_length=2048)
    api_protocol: str = Field(
        default="openai_compatible",
        pattern=r"^(openai_compatible|anthropic_messages)$",
    )
    upstream_model: str = Field(min_length=1, max_length=512)
    credential_ref: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=lambda: ["text"], min_length=1, max_length=8)
    max_concurrency: int = Field(default=1, ge=1, le=1024)


class MainAgentConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: MainAgentModelConfig | None = None
    control_mode: str = Field(
        default="supervisor",
        pattern=r"^(supervisor|planner|reviewer|autonomous)$",
    )
    decision_policy: str = Field(
        default="choose mode first, select the role pool, then let the main agent make the final decision",
        min_length=1,
        max_length=10_000,
    )
    operating_style: str = Field(
        default=(
            "control the room: clarify goals, choose the execution mode, select the direct "
            "answerer or role pool, resolve conflicts, and review failures before closing"
        ),
        min_length=1,
        max_length=10_000,
    )
    direct_answerer: str = Field(
        default="main_agent",
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$",
    )
    hermes_policy: str = Field(
        default="observe",
        pattern=r"^(off|observe|suggest|confirm_before_apply)$",
    )
    max_review_rounds: int = Field(default=2, ge=1, le=20)


class MainAgentConfigResponse(MainAgentConfigRequest):
    pass


class HermesFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID | None = None
    conversation_id: str | None = Field(default=None, max_length=128)
    category: str = Field(default="conversation", pattern=r"^(conversation|scheduler)$")
    outcome: str = Field(pattern=r"^(success|failure|neutral)$")
    lesson: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    weight: int = Field(default=1, ge=1, le=10)


class HermesInsightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str = Field(default="conversation", pattern=r"^(conversation|scheduler)$")
    outcome: str
    lesson: str
    summary: str
    user_summary: str | None = None
    run_id: UUID | None = None
    conversation_id: str | None = None
    confirmed_at: datetime | None = None
    tags: list[str]
    weight: int
    created_at: datetime


class HermesRecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    mode_candidates: list[str] = Field(default_factory=lambda: ["dispatch", "group_chat"])
    model_candidates: list[str] = Field(default_factory=list, max_length=20)
    skill_candidates: list[str] = Field(default_factory=list, max_length=50)


class HermesRecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_mode: str
    recommended_model: str | None
    recommended_skills: list[str]
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
    requires_approval: bool


_HERMES_BULK_ACTION_LIMIT = 1000


class HermesBulkConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=_HERMES_BULK_ACTION_LIMIT)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if re.fullmatch(r"hermes[-_][A-Za-z0-9][A-Za-z0-9_-]{0,120}", item) is None:
                raise ValueError("Hermes ids must be safe learning identifiers")
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


class HermesBulkConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: list[HermesInsightResponse]
    failed: list[BulkFailureResponse]


class HermesBulkDeleteRequest(HermesBulkConfirmRequest):
    pass


class HermesBulkDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: list[str]
    failed: list[BulkFailureResponse]


class AdminResourceService(Protocol):
    async def list_models(self) -> tuple[ModelDeploymentResponse, ...]: ...

    async def create_model(self, request: ModelDeploymentRequest) -> ModelDeploymentResponse: ...

    async def update_model(
        self, model_id: UUID, request: ModelDeploymentRequest
    ) -> ModelDeploymentResponse: ...

    async def delete_model(self, model_id: UUID) -> None: ...

    async def create_secret(self, request: SecretCreateRequest) -> SecretReferenceResponse: ...

    async def get_secret(self, ref: str) -> SecretReferenceResponse: ...

    async def probe_concurrency(self, request: ProbeRequest) -> ProbeResponse: ...

    async def save_draft(self, request: DraftRequest) -> PublishResponse: ...

    async def diff_draft(self, request: DraftRequest) -> DiffResponse: ...

    async def publish(self, request: PublishRequest) -> PublishResponse: ...

    async def rollback(self, version: int) -> PublishResponse: ...

    async def list_agents(self) -> tuple[AgentResourceResponse, ...]: ...

    async def upsert_agent(self, request: AgentResourceRequest) -> AgentResourceResponse: ...

    async def delete_agent(self, agent_id: str) -> None: ...

    async def list_workflows(self) -> tuple[WorkflowResourceResponse, ...]: ...

    async def upsert_workflow(
        self, request: WorkflowResourceRequest
    ) -> WorkflowResourceResponse: ...

    async def delete_workflow(self, workflow_id: str) -> None: ...

    async def get_settings(self) -> SystemSettingsResponse: ...

    async def update_settings(self, request: SystemSettingsRequest) -> SystemSettingsResponse: ...

    async def get_main_agent_config(self) -> MainAgentConfigResponse: ...

    async def update_main_agent_config(
        self, request: MainAgentConfigRequest
    ) -> MainAgentConfigResponse: ...

    async def list_runs(self) -> tuple[RunListItem, ...]: ...

    async def get_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def download_run_artifact(
        self, run_id: UUID, artifact_id: UUID, *, tenant_id: UUID | None = None
    ) -> GeneratedArtifactDownload: ...

    async def get_conversation(self, conversation_id: str) -> ConversationResponse: ...

    async def pause_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def resume_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def delete_run(self, run_id: UUID) -> RunDeleteResponse: ...

    async def list_evolution_runs(self) -> tuple[EvolutionRunResponse, ...]: ...

    async def create_evolution_run(
        self,
        request: EvolutionRunRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse: ...

    async def get_evolution_run(self, run_id: str) -> EvolutionRunResponse: ...

    async def plan_evolution_next_round(
        self,
        run_id: str,
        *,
        actor: str,
    ) -> EvolutionNextRoundPlanResponse: ...

    async def execute_evolution_next_round(
        self,
        run_id: str,
        request: EvolutionNextRoundExecutionRequest,
        *,
        actor: str,
    ) -> EvolutionNextRoundExecutionResponse: ...

    async def ingest_evolution_execution_run(
        self,
        run_id: str,
        execution_run_id: UUID,
        *,
        actor: str,
    ) -> EvolutionRunResponse: ...

    async def approve_evolution_run(
        self,
        run_id: str,
        request: EvolutionApprovalRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse: ...

    async def record_evolution_round(
        self,
        run_id: str,
        request: EvolutionRoundRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse: ...

    async def list_skills(self) -> tuple[SkillResponse, ...]: ...

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse: ...

    async def upload_skill_archive(
        self, filename: str, archive_bytes: bytes, *, strategy: str | None = None
    ) -> SkillArchiveUploadResponse: ...

    async def activate_skill_version(self, skill_id: str, version_id: str) -> SkillResponse: ...

    async def approve_skill(self, skill_id: str) -> SkillResponse: ...

    async def delete_skill(self, skill_id: str) -> None: ...

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]: ...

    async def list_plugin_signing_keys(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginSigningKeyResponse, ...]: ...

    async def upsert_plugin_signing_key(
        self,
        request: PluginSigningKeyRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginSigningKeyResponse: ...

    async def delete_plugin_signing_key(
        self,
        key_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None: ...

    async def upsert_plugin(
        self,
        request: PluginResourceRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
        source_filename: str | None = None,
        content_sha256: str | None = None,
        package_metadata: PluginPackageMetadata | None = None,
    ) -> PluginResourceResponse: ...

    async def start_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def enable_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def disable_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def stop_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def reload_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def approve_plugin_package(
        self,
        plugin_id: str,
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def reject_plugin_package(
        self,
        plugin_id: str,
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse: ...

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None: ...

    async def delete_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None: ...

    async def list_mcp_servers(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[McpServerResponse, ...]: ...

    async def upsert_mcp_server(self, request: McpServerRequest) -> McpServerResponse: ...

    async def delete_mcp_server(self, server_id: str) -> None: ...

    async def list_channels(self) -> tuple[ChannelStatusResponse, ...]: ...

    async def save_channel_config(
        self, channel_id: str, request: ChannelConfigRequest
    ) -> ChannelConfigSaveResponse: ...

    async def clear_channel_config(
        self, channel_id: str, *, actor: str
    ) -> ChannelConfigSaveResponse: ...

    async def channel_runtime_config(self) -> dict[str, str]: ...

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]: ...

    async def create_memory(self, request: MemoryCreateRequest) -> MemoryRecordResponse: ...

    async def update_memory(
        self, memory_id: str, request: MemoryRecordRequest
    ) -> MemoryRecordResponse: ...

    async def forget_memory(self, memory_id: str) -> None: ...

    async def list_audit_events(
        self, action: str | None = None
    ) -> tuple[AuditEventResponse, ...]: ...

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
        tenant_id: UUID | None = None,
    ) -> AuditEventResponse: ...

    async def create_openclaw_operation(
        self,
        request: OpenClawOperationRequest,
        *,
        actor: str,
        mode: str,
    ) -> OpenClawOperationResponse: ...

    async def get_openclaw_operation(self, operation_id: str) -> OpenClawOperationResponse: ...

    async def resolve_openclaw_operation(
        self,
        operation_id: str,
        request: OpenClawResolveRequest,
        *,
        actor: str,
    ) -> OpenClawOperationResponse: ...

    async def execute_openclaw_operation(
        self,
        operation_id: str,
        settings: SystemSettingsResponse,
        *,
        actor: str,
    ) -> OpenClawExecutionResponse: ...

    async def create_openclaw_session(
        self,
        request: OpenClawSessionRequest,
        *,
        actor: str,
        mode: str,
        settings: SystemSettingsResponse,
    ) -> OpenClawSessionResponse: ...

    async def list_openclaw_sessions(self) -> tuple[OpenClawSessionResponse, ...]: ...

    async def update_openclaw_session(
        self,
        session_id: str,
        request: OpenClawSessionActionRequest,
        *,
        actor: str,
    ) -> OpenClawSessionResponse: ...

    async def attach_openclaw_operation_to_session(
        self,
        session_id: str,
        operation_id: str,
        request: OpenClawOperationRequest,
        *,
        actor: str,
    ) -> OpenClawSessionResponse: ...

    async def list_logs(self, category: str | None = None) -> tuple[LogEntryResponse, ...]: ...

    async def list_hermes_insights(self) -> tuple[HermesInsightResponse, ...]: ...

    async def get_hermes_insight(self, insight_id: str) -> HermesInsightResponse: ...

    async def confirm_hermes_insight(self, insight_id: str) -> HermesInsightResponse: ...

    async def delete_hermes_insight(self, insight_id: str) -> None: ...

    async def record_hermes_feedback(
        self, request: HermesFeedbackRequest
    ) -> HermesInsightResponse: ...

    async def recommend_with_hermes(
        self, request: HermesRecommendationRequest
    ) -> HermesRecommendationResponse: ...


_SKILL_MANIFEST_NAMES = frozenset({"skill.yaml", "skill.yml", "skill.json"})
_SKILL_INSTRUCTION_NAMES = frozenset({"skill.md"})
_INSTRUCTION_SKILL_FORBIDDEN_EXTENSIONS = frozenset(
    {".exe", ".dll", ".dylib", ".so", ".pyc", ".pyo", ".bat", ".cmd", ".ps1", ".sh"}
)
_INSTRUCTION_SKILL_NESTED_ARCHIVE_EXTENSIONS = frozenset(
    {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar", ".whl"}
)
_TAR_METADATA_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)
_MAX_SKILL_BUNDLE_ITEMS = 4096
_MAX_PLUGIN_ARCHIVE_BYTES = 2_000_000
_MAX_PLUGIN_PACKAGE_FILE_COUNT = 512


@dataclass(frozen=True, slots=True)
class _ScannedSkillArchive:
    filename: str
    archive_bytes: bytes
    scan_report: SkillScanReport | None
    instruction_name: str | None = None
    instruction_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _SkippedSkillArchive:
    path: str
    reason: str


def _scan_skill_archive_upload(
    filename: str, archive_bytes: bytes
) -> tuple[bool, tuple[_ScannedSkillArchive, ...], tuple[_SkippedSkillArchive, ...]]:
    scanner = SkillScanner()
    try:
        return (
            False,
            (
                _ScannedSkillArchive(
                    filename=filename,
                    archive_bytes=archive_bytes,
                    scan_report=scanner.scan(archive_bytes),
                ),
            ),
            (),
        )
    except InvalidSkillPackage:
        try:
            instruction_scan = _scan_instruction_skill_archive(filename, archive_bytes)
        except InvalidSkillPackage:
            instruction_scan = None
        if instruction_scan is not None:
            return False, (instruction_scan,), ()
        split_archives = _split_skill_bundle_archive(filename, archive_bytes)
        if not split_archives:
            raise
        scanned: list[_ScannedSkillArchive] = []
        skipped: list[_SkippedSkillArchive] = []
        for item_path, item_filename, item_bytes in split_archives:
            try:
                scanned.append(
                    _ScannedSkillArchive(
                        filename=item_filename,
                        archive_bytes=item_bytes,
                        scan_report=scanner.scan(item_bytes),
                    )
                )
            except InvalidSkillPackage as scan_error:
                try:
                    instruction_item = _scan_instruction_skill_archive(item_filename, item_bytes)
                except InvalidSkillPackage as instruction_error:
                    skipped.append(
                        _SkippedSkillArchive(path=item_path, reason=str(instruction_error))
                    )
                    continue
                if instruction_item is None:
                    skipped.append(_SkippedSkillArchive(path=item_path, reason=str(scan_error)))
                    continue
                scanned.append(instruction_item)
        if not scanned:
            raise InvalidSkillPackage("skill archive contains no valid skill packages")
        return len(scanned) + len(skipped) > 1, tuple(scanned), tuple(skipped)


def _scan_instruction_skill_archive(
    filename: str, archive_bytes: bytes
) -> _ScannedSkillArchive | None:
    members = _instruction_skill_members(archive_bytes)
    if members is None:
        return None
    skill_md_path, skill_md_bytes, content_sha256 = members
    name = _instruction_skill_name(
        skill_md_bytes,
        fallback=_instruction_skill_fallback_name(filename, skill_md_path),
    )
    return _ScannedSkillArchive(
        filename=filename,
        archive_bytes=archive_bytes,
        scan_report=None,
        instruction_name=name,
        instruction_sha256=content_sha256,
    )


def _instruction_skill_members(archive_bytes: bytes) -> tuple[str, bytes, str] | None:
    if zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                normalized_files: dict[str, bytes] = {}
                for info in archive.infolist():
                    if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                        continue
                    mode = (info.external_attr >> 16) & 0o777777
                    if _skill_bundle_mode_is_unsafe(mode):
                        raise InvalidSkillPackage(
                            "instruction skill contains links or device files"
                        )
                    path = _safe_skill_bundle_path(info.filename)
                    if _skill_bundle_path_is_ignored(path):
                        continue
                    _validate_instruction_skill_file(path)
                    normalized_files[path] = archive.read(info.filename)
                candidates = [
                    name
                    for name in normalized_files
                    if PurePosixPath(name).name.lower() in _SKILL_INSTRUCTION_NAMES
                ]
                candidate = _select_instruction_skill_path(candidates)
                if candidate is None:
                    return None
                if len(normalized_files) > _MAX_SKILL_BUNDLE_ITEMS:
                    raise InvalidSkillPackage("instruction skill contains too many files")
                return (
                    candidate,
                    normalized_files[candidate],
                    _instruction_skill_content_sha256(normalized_files.items()),
                )
        except zipfile.BadZipFile as exc:
            raise InvalidSkillPackage("skill archive must be a valid zip file") from exc
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            members = archive.getmembers()
            files: list[tuple[str, bytes]] = []
            for member in members:
                if _tar_member_is_metadata(member):
                    continue
                if member.isdir():
                    continue
                if (
                    member.issym()
                    or member.islnk()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                ):
                    raise InvalidSkillPackage("instruction skill contains links or device files")
                if not member.isfile():
                    raise InvalidSkillPackage("instruction skill contains unsupported file types")
                path = _safe_skill_bundle_path(member.name)
                if _skill_bundle_path_is_ignored(path):
                    continue
                _validate_instruction_skill_file(path)
                source = archive.extractfile(member)
                if source is None:
                    raise InvalidSkillPackage("skill instruction file cannot be read")
                files.append((path, source.read()))
            candidate = _select_instruction_skill_path(
                path
                for path, _content in files
                if PurePosixPath(path).name.lower() in _SKILL_INSTRUCTION_NAMES
            )
            if candidate is None:
                return None
            if len(members) > _MAX_SKILL_BUNDLE_ITEMS:
                raise InvalidSkillPackage("instruction skill contains too many files")
            path, content = next((path, content) for path, content in files if path == candidate)
            return path, content, _instruction_skill_content_sha256(files)
    except tarfile.TarError as exc:
        raise InvalidSkillPackage("skill archive must be a valid zip or tar archive") from exc


def _instruction_skill_content_sha256(files: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, content in sorted(files, key=lambda item: item[0]):
        encoded_path = path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _package_archive_content_sha256(archive_bytes: bytes) -> str | None:
    """Hash skill package contents without ZIP/TAR container metadata."""

    if zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                files: list[tuple[str, bytes]] = []
                for info in archive.infolist():
                    if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                        continue
                    path = _safe_skill_bundle_path(info.filename)
                    if _skill_bundle_path_is_ignored(path):
                        continue
                    files.append((path, archive.read(info.filename)))
                return _instruction_skill_content_sha256(files) if files else None
        except (InvalidSkillPackage, zipfile.BadZipFile):
            return None
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            files = []
            for member in archive.getmembers():
                if _tar_member_is_metadata(member) or member.isdir():
                    continue
                path = _safe_skill_bundle_path(member.name)
                if _skill_bundle_path_is_ignored(path):
                    continue
                source = archive.extractfile(member)
                if source is not None:
                    files.append((path, source.read()))
            return _instruction_skill_content_sha256(files) if files else None
    except (InvalidSkillPackage, tarfile.TarError):
        return None


def _select_instruction_skill_path(candidates: Iterable[str]) -> str | None:
    ordered = tuple(candidates)
    if not ordered:
        return None
    shallowest_depth = min(len(PurePosixPath(candidate).parts) for candidate in ordered)
    shallowest = [
        candidate
        for candidate in ordered
        if len(PurePosixPath(candidate).parts) == shallowest_depth
    ]
    if len(shallowest) != 1:
        return None
    return shallowest[0]


def _instruction_skill_fallback_name(filename: str, skill_md_path: str) -> str:
    parent = PurePosixPath(skill_md_path).parent
    if parent.as_posix() not in {"", "."}:
        return parent.name
    return PurePosixPath(filename).stem


def _instruction_skill_name(skill_md_bytes: bytes, *, fallback: str) -> str:
    try:
        text = skill_md_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSkillPackage("SKILL.md must be utf-8") from exc
    raw_name = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                front_matter = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError as exc:
                raise InvalidSkillPackage("SKILL.md front matter cannot be parsed") from exc
            if isinstance(front_matter, dict):
                value = front_matter.get("name")
                if isinstance(value, str):
                    raw_name = value
    slug = _skill_name_slug_or_none(raw_name) if raw_name else None
    return slug or _skill_name_slug(fallback)


def _skill_name_slug(value: str) -> str:
    slug = _skill_name_slug_or_none(value)
    if slug is None:
        raise InvalidSkillPackage("instruction skill name is invalid")
    return slug


def _skill_name_slug_or_none(value: str) -> str | None:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return slug[:128] if slug else None


def _validate_instruction_skill_file(path: str) -> None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _INSTRUCTION_SKILL_FORBIDDEN_EXTENSIONS:
        raise InvalidSkillPackage("instruction skill contains forbidden file extensions")
    if suffix in _INSTRUCTION_SKILL_NESTED_ARCHIVE_EXTENSIONS:
        raise InvalidSkillPackage("instruction skill contains nested archives")


def _skill_response_from_scan(
    filename: str,
    skill_id: str,
    scan_report: SkillScanReport,
    *,
    content_sha256: str | None = None,
) -> SkillResponse:
    inspection = scan_report.inspection
    content_sha256 = content_sha256 or inspection.content_sha256
    return SkillResponse(
        id=skill_id,
        name=inspection.manifest.name,
        status="scanned",
        scan_diff=[
            f"package {filename} scanned",
            f"entry point: {inspection.manifest.entry_point}",
            f"content sha256: {content_sha256}",
        ],
        requested_permissions=list(inspection.requested_capabilities),
        source_filename=filename,
        package_version_id=f"pkg_{content_sha256}",
        content_sha256=content_sha256,
    )


def _skill_response_from_scanned_archive(
    scanned: _ScannedSkillArchive, skill_id: str
) -> SkillResponse:
    if scanned.scan_report is not None:
        return _skill_response_from_scan(
            scanned.filename,
            skill_id,
            scanned.scan_report,
            content_sha256=_package_archive_content_sha256(scanned.archive_bytes),
        )
    return SkillResponse(
        id=skill_id,
        name=scanned.instruction_name or _skill_name_slug(PurePosixPath(scanned.filename).stem),
        status="scanned",
        scan_diff=[
            f"instruction package {scanned.filename} scanned",
            "SKILL.md detected",
            "no executable entry point; available as an instruction skill",
            f"content sha256: {scanned.instruction_sha256}",
        ],
        requested_permissions=[],
        source_filename=scanned.filename,
        package_version_id=f"pkg_{scanned.instruction_sha256}",
        content_sha256=scanned.instruction_sha256,
    )


def _skill_id_from_scanned_archive(scanned: _ScannedSkillArchive) -> str:
    if scanned.scan_report is not None:
        manifest = scanned.scan_report.inspection.manifest
        return _stable_skill_id("package", manifest.name, manifest.version)
    name = scanned.instruction_name or _skill_name_slug(PurePosixPath(scanned.filename).stem)
    return _stable_skill_id("instruction", name, "instruction-only")


def _skill_version_id_from_scanned_archive(scanned: _ScannedSkillArchive) -> str:
    response = _skill_response_from_scanned_archive(scanned, "skill_pending")
    return _stable_skill_id("version", response.name, response.content_sha256 or response.id)


def _manual_skill_upload_id(filename: str) -> str:
    name = _skill_name_slug(PurePosixPath(filename).stem)
    return _stable_skill_id("manual", name, "metadata-only")


def _stable_skill_id(kind: str, name: str, version: str) -> str:
    slug = _skill_name_slug(name)[:64]
    version_slug = _skill_name_slug(version)
    digest = hashlib.sha256(f"{kind}:{slug}:{version_slug}".encode()).hexdigest()[:16]
    return f"skill_{slug}_{digest}"


@dataclass(frozen=True, slots=True)
class _SkillVersionRecord:
    response: SkillResponse
    created_at: datetime | None
    updated_at: datetime | None
    ordinal: int


def _skill_response_from_payload(
    payload: Mapping[str, object], *, resource_id: str | None = None
) -> SkillResponse:
    data = dict(payload)
    if resource_id is not None and not isinstance(data.get("id"), str):
        data["id"] = resource_id
    return SkillResponse.model_validate(data)


def _group_skill_records(
    records: Iterable[_SkillVersionRecord],
    active_versions: Mapping[str, str],
) -> tuple[SkillResponse, ...]:
    groups: dict[str, list[_SkillVersionRecord]] = {}
    group_order: list[str] = []
    for record in records:
        name = record.response.name
        if name not in groups:
            groups[name] = []
            group_order.append(name)
        groups[name].append(record)

    grouped: list[SkillResponse] = []
    for name in group_order:
        versions = sorted(groups[name], key=_skill_version_sort_key, reverse=True)
        active_id = active_versions.get(name)
        current = next((record for record in versions if record.response.id == active_id), versions[0])
        current_id = current.response.id
        version_responses = [
            SkillVersionResponse(
                id=record.response.id,
                status=record.response.status,
                source_filename=record.response.source_filename,
                package_version_id=record.response.package_version_id,
                content_sha256=record.response.content_sha256,
                created_at=record.created_at,
                updated_at=record.updated_at,
                is_current=record.response.id == current_id,
            )
            for record in versions
        ]
        grouped.append(
            current.response.model_copy(
                update={"current_version_id": current_id, "versions": version_responses}
            )
        )
    return tuple(grouped)


def _skill_version_sort_key(record: _SkillVersionRecord) -> tuple[datetime, int]:
    timestamp = record.updated_at or record.created_at or datetime.min.replace(tzinfo=UTC)
    return timestamp, record.ordinal


_SKILL_ACTIVE_VERSIONS_SETTING_ID = "skill-active-versions"


def _skill_upload_strategy_or_error(strategy: str | None) -> str | None:
    if strategy in {None, "", "overwrite", "new_version"}:
        return strategy or None
    raise PublicAPIError(
        422,
        "request_validation",
        "skill upload strategy must be overwrite or new_version",
    )


def _matching_skill_content(
    versions: Iterable[SkillResponse], content_sha256: str | None
) -> SkillResponse | None:
    if not content_sha256:
        return None
    return next((skill for skill in versions if skill.content_sha256 == content_sha256), None)


def _current_skill_for_name(
    versions: Iterable[SkillResponse],
    active_versions: Mapping[str, str],
    name: str,
) -> SkillResponse:
    version_list = list(versions)
    active_id = active_versions.get(name)
    if active_id:
        active = next((skill for skill in version_list if skill.id == active_id), None)
        if active is not None:
            return active
    return version_list[-1]


def _skill_version_choice_error(candidate: SkillResponse, current: SkillResponse) -> PublicAPIError:
    return PublicAPIError(
        409,
        "skill_version_choice_required",
        "same-name skill already exists; choose overwrite or new_version",
        details={
            "skill_name": candidate.name,
            "current_version_id": current.id,
            "new_content_sha256": candidate.content_sha256 or "",
        },
    )


def _active_skill_versions_from_payload(payload: Mapping[str, object]) -> dict[str, str]:
    value = payload.get("active_versions")
    if not isinstance(value, dict):
        return {}
    return {
        str(name): str(version_id)
        for name, version_id in value.items()
        if isinstance(name, str) and isinstance(version_id, str)
    }


def _skill_response_with_archive_identity(
    response: SkillResponse, archive_path: Path
) -> SkillResponse:
    if response.content_sha256 and response.package_version_id and response.source_filename:
        return response
    if not archive_path.is_file():
        return response
    content_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return response.model_copy(
        update={
            "source_filename": response.source_filename or archive_path.name,
            "package_version_id": response.package_version_id or f"pkg_{content_sha256}",
            "content_sha256": response.content_sha256 or content_sha256,
        }
    )


def _skipped_skill_responses(
    skipped: tuple[_SkippedSkillArchive, ...],
) -> list[SkillArchiveSkippedResponse]:
    return [SkillArchiveSkippedResponse(path=item.path, reason=item.reason) for item in skipped]


def _split_skill_bundle_archive(
    filename: str, archive_bytes: bytes
) -> tuple[tuple[str, str, bytes], ...]:
    if zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        return _split_zip_skill_bundle(filename, archive_bytes)
    return _split_tar_skill_bundle(filename, archive_bytes)


def _split_zip_skill_bundle(
    filename: str, archive_bytes: bytes
) -> tuple[tuple[str, str, bytes], ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries: list[tuple[str, zipfile.ZipInfo]] = []
            for info in archive.infolist():
                if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                    continue
                mode = (info.external_attr >> 16) & 0o777777
                if _skill_bundle_mode_is_unsafe(mode):
                    raise InvalidSkillPackage("skill bundle contains links or device files")
                path = _safe_skill_bundle_path(info.filename)
                if _skill_bundle_path_is_ignored(path):
                    continue
                entries.append((path, info))
            groups = _skill_bundle_groups(entries)
            return tuple(
                (
                    group,
                    f"{PurePosixPath(filename).stem}-{_skill_bundle_group_filename(group)}.zip",
                    _zip_group_to_skill_archive(archive, group_entries),
                )
                for group, group_entries in groups
            )
    except zipfile.BadZipFile as exc:
        raise InvalidSkillPackage("skill archive must be a valid zip file") from exc


def _split_tar_skill_bundle(
    filename: str, archive_bytes: bytes
) -> tuple[tuple[str, str, bytes], ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            entries: list[tuple[str, tarfile.TarInfo]] = []
            for member in archive.getmembers():
                if _tar_member_is_metadata(member):
                    continue
                if member.isdir():
                    continue
                if (
                    member.issym()
                    or member.islnk()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                ):
                    raise InvalidSkillPackage("skill bundle contains links or device files")
                if not member.isfile():
                    raise InvalidSkillPackage("skill bundle contains unsupported file types")
                path = _safe_skill_bundle_path(member.name)
                if _skill_bundle_path_is_ignored(path):
                    continue
                entries.append((path, member))
            groups = _skill_bundle_groups(entries)
            return tuple(
                (
                    group,
                    f"{PurePosixPath(filename).stem}-{_skill_bundle_group_filename(group)}.zip",
                    _tar_group_to_skill_archive(archive, group_entries),
                )
                for group, group_entries in groups
            )
    except tarfile.TarError as exc:
        raise InvalidSkillPackage("skill archive must be a valid zip or tar archive") from exc


def _safe_skill_bundle_path(name: str) -> str:
    normalized = name.replace("\\", "/").rstrip("/")
    if normalized.startswith(("/", "../")) or "/../" in normalized:
        raise InvalidSkillPackage("skill bundle contains path traversal")
    if normalized in {"", ".", ".."} or normalized.endswith("/.."):
        raise InvalidSkillPackage("skill bundle contains path traversal")
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        raise InvalidSkillPackage("skill bundle contains absolute paths")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidSkillPackage("skill bundle contains unsafe paths")
    return path.as_posix()


def _skill_bundle_path_is_ignored(path: str) -> bool:
    return any(
        part.startswith(".") or part in {"__MACOSX", "__pycache__"}
        for part in PurePosixPath(path).parts
    )


def _skill_bundle_groups[T](
    entries: list[tuple[str, T]],
) -> tuple[tuple[str, list[tuple[str, T]]], ...]:
    roots: dict[str, None] = {}
    for path, _entry in entries:
        parts = PurePosixPath(path).parts
        if len(parts) >= 2 and (
            parts[-1] in _SKILL_MANIFEST_NAMES or parts[-1].lower() in _SKILL_INSTRUCTION_NAMES
        ):
            roots[PurePosixPath(*parts[:-1]).as_posix()] = None
    groups: list[tuple[str, list[tuple[str, T]]]] = []
    for root in _select_skill_bundle_roots(roots):
        group_entries: list[tuple[str, T]] = []
        root_parts = PurePosixPath(root).parts
        for path, entry in entries:
            path_parts = PurePosixPath(path).parts
            if len(path_parts) <= len(root_parts) or path_parts[: len(root_parts)] != root_parts:
                continue
            inner_path = PurePosixPath(*path_parts[len(root_parts) :]).as_posix()
            group_entries.append((inner_path, entry))
        groups.append((root, group_entries))
    return tuple(groups)


def _select_skill_bundle_roots(roots: Iterable[str]) -> tuple[str, ...]:
    ordered = tuple(roots)
    selected: list[str] = []
    for root in sorted(ordered, key=_skill_bundle_root_depth):
        if any(_skill_bundle_root_contains(parent, root) for parent in selected):
            continue
        selected.append(root)
    selected_set = set(selected)
    return tuple(root for root in ordered if root in selected_set)


def _skill_bundle_root_depth(root: str) -> int:
    if root in {"", "."}:
        return 0
    return len(PurePosixPath(root).parts)


def _skill_bundle_root_contains(parent: str, child: str) -> bool:
    if parent == child:
        return False
    if parent in {"", "."}:
        return True
    parent_parts = PurePosixPath(parent).parts
    child_parts = PurePosixPath(child).parts
    return len(child_parts) > len(parent_parts) and child_parts[: len(parent_parts)] == parent_parts


def _skill_bundle_group_filename(group: str) -> str:
    return "-".join(PurePosixPath(group).parts)


def _skill_bundle_mode_is_unsafe(mode: int) -> bool:
    file_type = stat.S_IFMT(mode)
    return file_type in {
        stat.S_IFLNK,
        stat.S_IFCHR,
        stat.S_IFBLK,
        stat.S_IFIFO,
        stat.S_IFSOCK,
    }


def _tar_member_is_metadata(member: tarfile.TarInfo) -> bool:
    return member.type in _TAR_METADATA_TYPES


def _zip_group_to_skill_archive(
    archive: zipfile.ZipFile, entries: list[tuple[str, zipfile.ZipInfo]]
) -> bytes:
    if len(entries) > _MAX_SKILL_BUNDLE_ITEMS:
        raise InvalidSkillPackage("skill bundle item contains too many files")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for inner_path, source_info in entries:
            target_info = zipfile.ZipInfo(inner_path)
            target_info.external_attr = source_info.external_attr
            output.writestr(target_info, archive.read(source_info.filename))
    return buffer.getvalue()


def _tar_group_to_skill_archive(
    archive: tarfile.TarFile, entries: list[tuple[str, tarfile.TarInfo]]
) -> bytes:
    if len(entries) > _MAX_SKILL_BUNDLE_ITEMS:
        raise InvalidSkillPackage("skill bundle item contains too many files")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for inner_path, member in entries:
            source = archive.extractfile(member)
            if source is None:
                raise InvalidSkillPackage("skill bundle item cannot be read")
            target_info = zipfile.ZipInfo(inner_path)
            target_info.external_attr = (member.mode & 0o777) << 16
            output.writestr(target_info, source.read())
    return buffer.getvalue()


def _plugin_archive_manifest_from_archive(archive_bytes: bytes) -> PluginArchiveManifest:
    manifest_bytes = _plugin_manifest_bytes_from_archive(archive_bytes)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except UnicodeDecodeError:
        raise InvalidSkillPackage("plugin.json must be utf-8") from None
    except json.JSONDecodeError:
        raise InvalidSkillPackage("plugin.json must be valid json") from None
    _validate_plugin_package_metadata(manifest)
    try:
        plugin_manifest = PluginArchiveManifest.model_validate(manifest)
    except ValidationError as exc:
        raise InvalidSkillPackage(str(exc)) from None
    _validate_plugin_package_contract(
        plugin_manifest.package,
        archive_paths=_plugin_archive_member_paths(archive_bytes),
    )
    return plugin_manifest


def _plugin_request_from_archive(archive_bytes: bytes) -> PluginResourceRequest:
    manifest = _plugin_archive_manifest_from_archive(archive_bytes)
    return PluginResourceRequest.model_validate(manifest.model_dump(exclude={"package"}))


def _validate_plugin_package_metadata(manifest: object) -> None:
    if not isinstance(manifest, Mapping):
        return
    package = manifest.get("package")
    if not isinstance(package, Mapping):
        return
    if "activation_state" in package or "activation_reason" in package:
        raise InvalidSkillPackage("plugin package activation state is server-controlled")
    if (
        "approval_state" in package
        or "approval_reason" in package
        or "approved_by" in package
        or "approved_at" in package
    ):
        raise InvalidSkillPackage("plugin package approval state is server-controlled")
    if "signature_verification" in package:
        raise InvalidSkillPackage("plugin package signature verification is server-controlled")
    if "verified_public_key_sha256" in package:
        raise InvalidSkillPackage("plugin package verified public key is server-controlled")
    if "signature_trust_expires_at" in package:
        raise InvalidSkillPackage("plugin package signature trust expiry is server-controlled")
    if "artifact" in package:
        raise InvalidSkillPackage("plugin package artifact is server-controlled")
    dependencies = package.get("dependencies")
    if dependencies not in (None, []):
        raise InvalidSkillPackage(PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON)
    if package.get("kind") == "manifest_only" and set(package) & {
        "package_version",
        "adapter_id",
        "sdk_api_version",
        "signature",
        "runtime",
        "entrypoint",
        "isolation",
    }:
        raise InvalidSkillPackage("manifest-only plugin package cannot declare runtime execution")
    entrypoint = package.get("entrypoint")
    if entrypoint is None:
        return
    if not isinstance(entrypoint, str):
        return
    try:
        _safe_plugin_archive_member_path(entrypoint)
    except InvalidSkillPackage:
        raise InvalidSkillPackage("plugin package entrypoint is unsafe") from None


def _validate_plugin_package_contract(
    package: PluginPackageMetadata | None,
    *,
    archive_paths: frozenset[str],
) -> None:
    if package is None:
        return
    if package.dependencies:
        raise InvalidSkillPackage(PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON)
    if package.kind == "manifest_only":
        if (
            package.package_version is not None
            or package.signature is not None
            or package.adapter_id is not None
            or package.sdk_api_version is not None
            or package.runtime != "none"
            or package.entrypoint is not None
            or package.isolation != "none"
        ):
            raise InvalidSkillPackage(
                "manifest-only plugin package cannot declare runtime execution"
            )
        return
    if package.adapter_id is None or package.sdk_api_version is None:
        raise InvalidSkillPackage("adapter plugin package must declare sdk contract")
    if package.package_version is None:
        raise InvalidSkillPackage("adapter plugin package must declare package version")
    if package.runtime == "none" or package.isolation == "none":
        raise InvalidSkillPackage("adapter plugin package must declare runtime execution")
    if package.entrypoint is None or package.entrypoint not in archive_paths:
        raise InvalidSkillPackage("plugin package entrypoint is missing")
    if package.install_mode == "runtime_registered":
        if package.sdk_api_version not in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS:
            raise InvalidSkillPackage("plugin package SDK API version is not supported")
        if package.runtime not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_RUNTIMES:
            raise InvalidSkillPackage("runtime-registered plugin package runtime is not supported")
        if package.isolation not in SUPPORTED_RUNTIME_REGISTERED_PACKAGE_ISOLATIONS:
            raise InvalidSkillPackage("runtime-registered plugin package isolation is not supported")


def _validate_runtime_registered_plugin_package(
    request: Request,
    plugin: PluginResourceRequest,
    package: PluginPackageMetadata | None,
) -> None:
    if package is None or package.kind != "adapter_package":
        return
    if package.install_mode != "runtime_registered":
        return
    if package.dependencies:
        raise InvalidSkillPackage(PLUGIN_PACKAGE_DEPENDENCIES_UNSUPPORTED_REASON)
    descriptors = {descriptor.id: descriptor for descriptor in _plugin_adapter_descriptors(request)}
    descriptor = descriptors.get(package.adapter_id or "")
    if descriptor is None:
        raise InvalidSkillPackage(
            "runtime-registered adapter package requires a registered adapter descriptor"
        )
    runtime_profiles = set(descriptor.capability_contract.runtime_sandbox_profiles)
    if package.isolation not in runtime_profiles:
        raise InvalidSkillPackage(
            "runtime-registered adapter package isolation is not supported by adapter"
        )
    if not plugin.capabilities:
        raise InvalidSkillPackage(
            "runtime-registered adapter packages must declare at least one capability"
        )
    for capability in plugin.capabilities:
        if capability.adapter != package.adapter_id:
            raise InvalidSkillPackage(
                "runtime-registered adapter packages must route capabilities through package adapter_id"
            )
        if capability.sandbox_profile != package.isolation:
            raise InvalidSkillPackage(
                "runtime-registered adapter package capabilities must use package isolation"
            )


def _plugin_request_for_activation_check(
    plugin: PluginResourceResponse,
) -> PluginResourceRequest:
    return PluginResourceRequest(
        id=plugin.id,
        name=plugin.name,
        enabled=plugin.enabled,
        description=plugin.description,
        version=plugin.version,
        resource_config=plugin.resource_config,
        endpoint_url=plugin.endpoint_url,
        domain_allowlist=plugin.domain_allowlist,
        timeout_seconds=plugin.timeout_seconds,
        credential_ref=plugin.credential_ref,
        credential_header=plugin.credential_header,
        credential_scheme=plugin.credential_scheme,
        capabilities=list(plugin.capabilities),
    )


async def _current_plugin_for_activation_check(
    service: AdminResourceService,
    plugin_id: str,
    *,
    tenant_id: UUID,
) -> PluginResourceResponse:
    for plugin in await service.list_plugins(tenant_id=tenant_id):
        if plugin.id == plugin_id:
            return plugin
    raise KeyError(plugin_id)


def _ensure_runtime_registered_package_can_activate(
    request: Request,
    plugin: PluginResourceResponse,
    *,
    require_current_eligibility: bool = False,
) -> None:
    if require_current_eligibility:
        _ensure_plugin_package_activation_allowed(plugin)
    elif (
        plugin.package_metadata is not None
        and plugin.package_metadata.kind == "adapter_package"
        and plugin.package_metadata.install_mode == "runtime_registered"
        and plugin.package_metadata.signature_verification != "verified"
    ):
        raise PublicAPIError(
            409,
            "plugin_package_not_eligible",
            plugin.package_metadata.activation_reason
            or "adapter packages must include a trusted verified signature before activation",
        )
    try:
        _validate_runtime_registered_plugin_package(
            request,
            _plugin_request_for_activation_check(plugin),
            plugin.package_metadata,
        )
    except InvalidSkillPackage as error:
        raise PublicAPIError(
            409,
            "plugin_package_not_eligible",
            _safe_model_check_detail(str(error)),
        ) from None


def _plugins_with_runtime_registered_activation_check(
    request: Request,
    plugins: Iterable[PluginResourceResponse],
) -> tuple[PluginResourceResponse, ...]:
    return tuple(
        _plugin_with_runtime_registered_activation_check(request, plugin)
        for plugin in plugins
    )


def _plugin_with_runtime_registered_activation_check(
    request: Request,
    plugin: PluginResourceResponse,
) -> PluginResourceResponse:
    package = plugin.package_metadata
    if (
        package is None
        or package.kind != "adapter_package"
        or package.install_mode != "runtime_registered"
        or package.activation_state != "eligible"
    ):
        return plugin
    try:
        _validate_runtime_registered_plugin_package(
            request,
            _plugin_request_for_activation_check(plugin),
            package,
        )
    except InvalidSkillPackage as error:
        return plugin.model_copy(
            update={
                "package_metadata": package.model_copy(
                    update={
                        "activation_state": "blocked_unsupported_runtime",
                        "activation_reason": _safe_model_check_detail(str(error)),
                    }
                ),
            }
        )
    return plugin


def _verified_plugin_package_metadata(
    manifest: PluginArchiveManifest,
    archive_bytes: bytes,
    signing_keys: tuple[PluginSigningKeyResponse, ...],
) -> PluginPackageMetadata | None:
    package = manifest.package
    if package is None or package.signature is None:
        return package
    now = datetime.now(UTC)
    signing_key = next(
        (
            key
            for key in signing_keys
            if key.trusted
            and key.algorithm == package.signature.algorithm
            and key.key_id == package.signature.key_id
            and _plugin_signing_key_is_active(key, now)
        ),
        None,
    )
    if signing_key is None:
        return package.model_copy(update={"signature_verification": "untrusted_key"})
    payload = _plugin_signature_payload(manifest, archive_bytes)
    try:
        public_key_bytes = _decode_base64url_bytes(
            signing_key.public_key,
            expected_length=32,
            label="public_key",
        )
        signature_bytes = _decode_base64url_bytes(
            package.signature.value,
            expected_length=64,
            label="signature",
        )
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, payload)
    except InvalidSignature:
        raise InvalidSkillPackage("plugin package signature verification failed") from None
    except ValueError:
        raise InvalidSkillPackage("trusted plugin signing key is invalid") from None
    return package.model_copy(
        update={
            "signature_verification": "verified",
            "verified_public_key_sha256": _plugin_public_key_sha256(public_key_bytes),
            "signature_trust_expires_at": signing_key.not_after,
        }
    )


def _plugin_signing_key_is_active(key: PluginSigningKeyResponse, now: datetime) -> bool:
    active_at = now.astimezone(UTC)
    if key.not_before is not None and active_at < key.not_before.astimezone(UTC):
        return False
    return key.not_after is None or active_at < key.not_after.astimezone(UTC)


def _plugin_public_key_sha256(public_key_bytes: bytes) -> str:
    return hashlib.sha256(public_key_bytes).hexdigest()


def _plugin_signing_key_matches_verified_package(
    key: PluginSigningKeyResponse,
    package: PluginPackageMetadata,
) -> bool:
    if package.verified_public_key_sha256 is None:
        return False
    try:
        public_key_bytes = _decode_base64url_bytes(
            key.public_key,
            expected_length=32,
            label="public_key",
        )
    except ValueError:
        return False
    return _plugin_public_key_sha256(public_key_bytes) == package.verified_public_key_sha256


def _plugin_with_effective_package_trust(
    plugin: PluginResourceResponse,
    signing_keys: Iterable[PluginSigningKeyResponse],
    now: datetime,
) -> PluginResourceResponse:
    package = plugin.package_metadata
    if (
        package is None
        or package.signature is None
        or package.signature_verification != "verified"
    ):
        return plugin
    active_signing_key = next(
        (
            key
            for key in signing_keys
            if key.trusted
            and key.algorithm == package.signature.algorithm
            and key.key_id == package.signature.key_id
            and _plugin_signing_key_is_active(key, now)
            and _plugin_signing_key_matches_verified_package(key, package)
        ),
        None,
    )
    if active_signing_key is not None:
        if package.signature_trust_expires_at == active_signing_key.not_after:
            return plugin
        return plugin.model_copy(
            update={
                "package_metadata": package.model_copy(
                    update={"signature_trust_expires_at": active_signing_key.not_after}
                )
            }
        )
    effective_package = PluginPackageMetadata.model_validate(
        {
            **package.model_dump(mode="json"),
            "signature_verification": "untrusted_key",
            "signature_trust_expires_at": None,
        }
    )
    return plugin.model_copy(update={"package_metadata": effective_package})


def _plugin_signature_payload(manifest: PluginArchiveManifest, archive_bytes: bytes) -> bytes:
    manifest_payload = manifest.model_dump(mode="json")
    package = manifest_payload.get("package")
    if isinstance(package, dict):
        for field in (
            "activation_state",
            "activation_reason",
            "approval_state",
            "approval_reason",
            "approved_by",
            "approved_at",
            "signature_trust_expires_at",
            "signature_verification",
            "verified_public_key_sha256",
            "artifact",
        ):
            package.pop(field, None)
        signature = package.get("signature")
        if isinstance(signature, dict):
            signature.pop("value", None)
    payload = {
        "schema": "mofang-plugin-package-v1",
        "manifest": manifest_payload,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for path, content in _plugin_archive_signature_files(archive_bytes)
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _plugin_archive_signature_files(archive_bytes: bytes) -> tuple[tuple[str, bytes], ...]:
    archive_buffer = io.BytesIO(archive_bytes)
    if zipfile.is_zipfile(archive_buffer):
        return _plugin_archive_signature_files_from_zip(archive_bytes)
    return _plugin_archive_signature_files_from_tar(archive_bytes)


def _plugin_package_store_root(request: Request) -> Path:
    settings = getattr(request.app.state, "settings", None)
    configured = getattr(
        settings,
        "plugin_package_store_dir",
        Path("/var/lib/agent-hub/plugin-packages"),
    )
    return Path(configured)


def _ensure_path_inside(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError:
        raise InvalidSkillPackage("plugin archive contains unsafe paths") from None


def _stored_plugin_package_metadata(
    request: Request,
    *,
    tenant_id: UUID,
    plugin_id: str,
    package: PluginPackageMetadata | None,
    archive_bytes: bytes,
    content_sha256: str,
) -> PluginPackageMetadata | None:
    if (
        package is None
        or package.kind != "adapter_package"
        or package.signature_verification != "verified"
    ):
        return package
    files = _plugin_archive_signature_files(archive_bytes)
    if len(files) > _MAX_PLUGIN_PACKAGE_FILE_COUNT:
        raise InvalidSkillPackage("plugin archive contains too many package files")
    total_size_bytes = sum(len(content) for _path, content in files)
    if total_size_bytes > _MAX_PLUGIN_ARCHIVE_BYTES:
        raise InvalidSkillPackage("plugin archive uncompressed package files exceed size limit")
    store_root = _plugin_package_store_root(request)
    storage_key = f"{tenant_id}/{plugin_id}/{content_sha256}"
    artifact_root = store_root / str(tenant_id) / plugin_id / content_sha256
    temp_root = artifact_root.parent / f"tmp-{uuid4().hex}"
    try:
        artifact_root.parent.mkdir(parents=True, exist_ok=True)
        _ensure_path_inside(store_root, artifact_root)
        _ensure_path_inside(store_root, temp_root)
        for path, content in files:
            target = temp_root.joinpath(*PurePosixPath(path).parts)
            _ensure_path_inside(temp_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
        temp_root.replace(artifact_root)
    except OSError as error:
        with contextlib.suppress(OSError):
            if temp_root.exists():
                shutil.rmtree(temp_root)
        raise PublicAPIError(
            503,
            "plugin_package_store_unavailable",
            "plugin package store is unavailable",
        ) from error
    return package.model_copy(
        update={
            "artifact": PluginPackageArtifactMetadata(
                storage_key=storage_key,
                content_sha256=content_sha256,
                file_count=len(files),
                total_size_bytes=total_size_bytes,
                stored_at=datetime.now(UTC),
                quarantine_state="stored",
            )
        }
    )


def _cleanup_plugin_package_artifact(
    request: Request,
    plugin: PluginResourceResponse,
    *,
    tenant_id: UUID,
) -> None:
    artifact = plugin.package_metadata.artifact if plugin.package_metadata is not None else None
    if artifact is None:
        return
    expected_storage_key = f"{tenant_id}/{plugin.id}/{artifact.content_sha256}"
    if artifact.storage_key != expected_storage_key:
        _LOGGER.warning(
            "skipping plugin package artifact cleanup with mismatched storage key",
            extra={"plugin_id": plugin.id},
        )
        return
    try:
        store_root = _plugin_package_store_root(request)
        artifact_root = store_root.joinpath(*artifact.storage_key.split("/"))
        _ensure_path_inside(store_root, artifact_root)
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
    except (InvalidSkillPackage, OSError):
        _LOGGER.warning(
            "failed to cleanup plugin package artifact",
            extra={"plugin_id": plugin.id},
            exc_info=True,
        )


def _plugin_archive_signature_files_from_zip(archive_bytes: bytes) -> tuple[tuple[str, bytes], ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            files: dict[str, bytes] = {}
            for info in archive.infolist():
                if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                    continue
                mode = (info.external_attr >> 16) & 0o777777
                if _skill_bundle_mode_is_unsafe(mode):
                    raise InvalidSkillPackage("plugin archive contains links or device files")
                path = _safe_plugin_archive_member_path(info.filename)
                if PurePosixPath(path).name == "plugin.json":
                    continue
                if path in files:
                    raise InvalidSkillPackage("plugin archive contains duplicate package files")
                files[path] = archive.read(info.filename)
    except zipfile.BadZipFile:
        raise InvalidSkillPackage("plugin archive must be a valid zip file") from None
    return tuple(sorted(files.items()))


def _plugin_archive_signature_files_from_tar(archive_bytes: bytes) -> tuple[tuple[str, bytes], ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            files: dict[str, bytes] = {}
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise InvalidSkillPackage("plugin archive contains links or device files")
                path = _safe_plugin_archive_member_path(member.name)
                if PurePosixPath(path).name == "plugin.json":
                    continue
                if path in files:
                    raise InvalidSkillPackage("plugin archive contains duplicate package files")
                source = archive.extractfile(member)
                if source is None:
                    raise InvalidSkillPackage("plugin archive item cannot be read")
                files[path] = source.read()
    except tarfile.TarError:
        raise InvalidSkillPackage("plugin archive must be a valid zip or tar archive") from None
    return tuple(sorted(files.items()))


def _decode_base64url_bytes(value: str, *, expected_length: int, label: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise ValueError(f"{label} must be base64url") from None
    if len(decoded) != expected_length:
        raise ValueError(f"{label} has invalid length")
    return decoded


def _plugin_archive_member_paths(archive_bytes: bytes) -> frozenset[str]:
    archive_buffer = io.BytesIO(archive_bytes)
    if zipfile.is_zipfile(archive_buffer):
        return _plugin_archive_member_paths_from_zip(archive_bytes)
    return _plugin_archive_member_paths_from_tar(archive_bytes)


def _plugin_archive_member_paths_from_zip(archive_bytes: bytes) -> frozenset[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            paths: set[str] = set()
            for info in archive.infolist():
                if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                    continue
                mode = (info.external_attr >> 16) & 0o777777
                if _skill_bundle_mode_is_unsafe(mode):
                    raise InvalidSkillPackage("plugin archive contains links or device files")
                paths.add(_safe_plugin_archive_member_path(info.filename))
    except zipfile.BadZipFile:
        raise InvalidSkillPackage("plugin archive must be a valid zip file") from None
    return frozenset(paths)


def _plugin_archive_member_paths_from_tar(archive_bytes: bytes) -> frozenset[str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            paths: set[str] = set()
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise InvalidSkillPackage("plugin archive contains links or device files")
                paths.add(_safe_plugin_archive_member_path(member.name))
    except tarfile.TarError:
        raise InvalidSkillPackage("plugin archive must be a valid zip or tar archive") from None
    return frozenset(paths)


def _plugin_manifest_bytes_from_archive(archive_bytes: bytes) -> bytes:
    if len(archive_bytes) > _MAX_PLUGIN_ARCHIVE_BYTES:
        raise PublicAPIError(413, "request_too_large", "plugin archive exceeds size limit")
    archive_buffer = io.BytesIO(archive_bytes)
    if zipfile.is_zipfile(archive_buffer):
        return _plugin_manifest_bytes_from_zip(archive_bytes)
    return _plugin_manifest_bytes_from_tar(archive_bytes)


def _plugin_manifest_bytes_from_zip(archive_bytes: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            candidates: list[tuple[str, bytes]] = []
            for info in archive.infolist():
                if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                    continue
                mode = (info.external_attr >> 16) & 0o777777
                if _skill_bundle_mode_is_unsafe(mode):
                    raise InvalidSkillPackage("plugin archive contains links or device files")
                path = _safe_plugin_archive_member_path(info.filename)
                if PurePosixPath(path).name == "plugin.json":
                    candidates.append((path, archive.read(info.filename)))
    except zipfile.BadZipFile:
        raise InvalidSkillPackage("plugin archive must be a valid zip file") from None
    return _single_plugin_manifest(candidates)


def _plugin_manifest_bytes_from_tar(archive_bytes: bytes) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            candidates: list[tuple[str, bytes]] = []
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise InvalidSkillPackage("plugin archive contains links or device files")
                path = _safe_plugin_archive_member_path(member.name)
                source = archive.extractfile(member)
                if source is None:
                    raise InvalidSkillPackage("plugin.json cannot be read")
                if PurePosixPath(path).name == "plugin.json":
                    candidates.append((path, source.read()))
    except tarfile.TarError:
        raise InvalidSkillPackage("plugin archive must be a valid zip or tar archive") from None
    return _single_plugin_manifest(candidates)


def _safe_plugin_archive_member_path(name: str) -> str:
    normalized = name.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvalidSkillPackage("plugin archive contains unsafe paths")
    return normalized


def _single_plugin_manifest(candidates: list[tuple[str, bytes]]) -> bytes:
    if not candidates:
        raise InvalidSkillPackage("plugin archive is missing plugin.json")
    if len(candidates) > 1:
        raise InvalidSkillPackage("plugin archive contains multiple plugin.json files")
    return candidates[0][1]


class CapabilityManifestProvider(Protocol):
    def capability_manifest(
        self,
        tenant_id: UUID,
        *,
        extra_sources: tuple[Any, ...] = (),
    ) -> Mapping[str, JsonValue]: ...


def _plugin_response_from_request(
    request: PluginResourceRequest,
    *,
    current: PluginResourceResponse | None = None,
    source_filename: str | None = None,
    content_sha256: str | None = None,
    package_metadata: PluginPackageMetadata | None = None,
) -> PluginResourceResponse:
    payload = request.model_dump()
    if not request.enabled:
        return PluginResourceResponse(
            **payload,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
            status="disabled",
            health="disabled",
            last_error_type=None,
        )
    if _plugin_package_activation_blocked(package_metadata):
        return PluginResourceResponse(
            **payload,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
            status="stopped",
            health="stopped",
            last_error_type=None,
        )
    if current is not None and current.status == "running":
        return PluginResourceResponse(
            **payload,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
            status="running",
            health="healthy",
            last_error_type=None,
        )
    return PluginResourceResponse(
        **payload,
        source_filename=source_filename,
        content_sha256=content_sha256,
        package_metadata=package_metadata,
        status="stopped",
        health="stopped",
        last_error_type=None,
    )


def _plugin_upsert_audit_details(plugin: PluginResourceResponse) -> dict[str, object]:
    resource_config_keys, redacted_resource_config_keys = _safe_plugin_config_key_summary(
        plugin.resource_config
    )
    capability_config_keys: set[str] = set()
    redacted_capability_config_keys = 0
    capability_config_key_count = 0
    for capability in plugin.capabilities:
        capability_config_key_count += len(capability.capability_config)
        safe_keys, redacted_keys = _safe_plugin_config_key_summary(capability.capability_config)
        capability_config_keys.update(safe_keys)
        redacted_capability_config_keys += redacted_keys
    return {
        "id": plugin.id,
        "capability_count": len(plugin.capabilities),
        "capability_ids": _sorted_csv(capability.id for capability in plugin.capabilities),
        "adapters": _sorted_csv(capability.adapter for capability in plugin.capabilities),
        "permission_classes": _sorted_csv(
            capability.permission_class for capability in plugin.capabilities
        ),
        "policy_effects": _sorted_csv(
            capability.policy_effect for capability in plugin.capabilities
        ),
        "sandbox_profiles": _sorted_csv(
            capability.sandbox_profile for capability in plugin.capabilities
        ),
        "resource_config_key_count": len(plugin.resource_config),
        "resource_config_keys": ",".join(resource_config_keys),
        "redacted_resource_config_key_count": redacted_resource_config_keys,
        "capability_config_key_count": capability_config_key_count,
        "capability_config_keys": ",".join(sorted(capability_config_keys)),
        "redacted_capability_config_key_count": redacted_capability_config_keys,
    }


def _plugin_policy_summary(plugin: PluginResourceResponse) -> PluginPolicySummaryResponse:
    resource_config_keys, redacted_resource_config_keys = _safe_plugin_config_key_summary(
        plugin.resource_config
    )
    capabilities = [
        _plugin_policy_capability_summary(capability) for capability in plugin.capabilities
    ]
    return PluginPolicySummaryResponse(
        id=plugin.id,
        name=plugin.name,
        version=plugin.version,
        enabled=plugin.enabled,
        status=plugin.status,
        health=plugin.health,
        capability_count=len(plugin.capabilities),
        adapters=sorted({capability.adapter for capability in plugin.capabilities}),
        permission_classes=sorted(
            {capability.permission_class for capability in plugin.capabilities}
        ),
        policy_effects=sorted({capability.policy_effect for capability in plugin.capabilities}),
        sandbox_profiles=sorted(
            {capability.sandbox_profile for capability in plugin.capabilities}
        ),
        resource_config_key_count=len(plugin.resource_config),
        resource_config_keys=list(resource_config_keys),
        redacted_resource_config_key_count=redacted_resource_config_keys,
        capabilities=capabilities,
    )


def _plugin_policy_capability_summary(
    capability: PluginCapabilityRequest,
) -> PluginPolicyCapabilitySummaryResponse:
    capability_config_keys, redacted_capability_config_keys = _safe_plugin_config_key_summary(
        capability.capability_config
    )
    return PluginPolicyCapabilitySummaryResponse(
        id=capability.id,
        adapter=capability.adapter,
        permission_class=capability.permission_class,
        sandbox_profile=capability.sandbox_profile,
        policy_effect=capability.policy_effect,
        replay_safe=capability.replay_safe,
        aliases=capability.aliases,
        input_schema_declared=capability.input_schema is not None,
        output_schema_declared=capability.output_schema is not None,
        capability_config_key_count=len(capability.capability_config),
        capability_config_keys=list(capability_config_keys),
        redacted_capability_config_key_count=redacted_capability_config_keys,
    )


def _plugin_policy_review(
    plugins: Iterable[PluginResourceResponse],
) -> PluginPolicyReviewResponse:
    summaries = [_plugin_policy_summary(plugin) for plugin in plugins]
    policy_effect_counts: dict[Literal["inherit", "allow", "require_approval", "deny"], int] = {}
    permission_class_counts: dict[str, int] = {}
    sandbox_profile_counts: dict[str, int] = {}
    capability_count = 0
    for summary in summaries:
        for capability in summary.capabilities:
            capability_count += 1
            policy_effect_counts[capability.policy_effect] = (
                policy_effect_counts.get(capability.policy_effect, 0) + 1
            )
            permission_class_counts[capability.permission_class] = (
                permission_class_counts.get(capability.permission_class, 0) + 1
            )
            sandbox_profile_counts[capability.sandbox_profile] = (
                sandbox_profile_counts.get(capability.sandbox_profile, 0) + 1
            )
    return PluginPolicyReviewResponse(
        plugin_count=len(summaries),
        capability_count=capability_count,
        policy_effect_counts=dict(sorted(policy_effect_counts.items())),
        permission_class_counts=dict(sorted(permission_class_counts.items())),
        sandbox_profile_counts=dict(sorted(sandbox_profile_counts.items())),
        plugins=summaries,
    )


def _plugin_policy_review_audit_details(
    review: PluginPolicyReviewResponse,
) -> dict[str, object]:
    details: dict[str, object] = {
        "plugin_count": review.plugin_count,
        "capability_count": review.capability_count,
        "policy_effects": _sorted_csv(review.policy_effect_counts),
        "permission_classes": _sorted_csv(review.permission_class_counts),
        "sandbox_profiles": _sorted_csv(review.sandbox_profile_counts),
    }
    for effect in ("allow", "deny", "inherit", "require_approval"):
        count = review.policy_effect_counts.get(effect)
        if count:
            details[f"{effect}_count"] = count
    return details


def _safe_plugin_config_key_summary(config: Mapping[str, JsonValue]) -> tuple[tuple[str, ...], int]:
    keys = sorted(str(key) for key in config)
    safe_keys = tuple(
        key
        for key in keys
        if not _contains_sensitive_marker(key) and not _is_sensitive_event_detail_key(key)
    )
    return safe_keys, len(keys) - len(safe_keys)


def _sorted_csv(values: Iterable[str]) -> str:
    return ",".join(sorted(set(values)))


def _plugin_started_response(plugin: PluginResourceResponse) -> PluginResourceResponse:
    if not plugin.enabled:
        raise PublicAPIError(409, "plugin_disabled", "plugin is disabled")
    _ensure_plugin_package_activation_allowed(plugin)
    return plugin.model_copy(
        update={"status": "running", "health": "healthy", "last_error_type": None}
    )


def _plugin_enabled_response(plugin: PluginResourceResponse) -> PluginResourceResponse:
    _ensure_plugin_package_activation_allowed(plugin)
    return plugin.model_copy(
        update={
            "enabled": True,
            "status": "stopped",
            "health": "stopped",
            "last_error_type": None,
        }
    )


def _plugin_disabled_response(plugin: PluginResourceResponse) -> PluginResourceResponse:
    return plugin.model_copy(
        update={
            "enabled": False,
            "status": "disabled",
            "health": "disabled",
            "last_error_type": None,
        }
    )


def _plugin_stopped_response(plugin: PluginResourceResponse) -> PluginResourceResponse:
    if not plugin.enabled:
        return plugin.model_copy(update={"status": "disabled", "health": "disabled"})
    return plugin.model_copy(
        update={"status": "stopped", "health": "stopped", "last_error_type": None}
    )


def _plugin_with_package_approval(
    plugin: PluginResourceResponse,
    approval_state: Literal["approved", "rejected"],
    request: PluginPackageApprovalRequest,
    *,
    actor_id: UUID | None,
) -> PluginResourceResponse:
    package = plugin.package_metadata
    if package is None or package.kind != "adapter_package":
        raise PublicAPIError(
            409,
            "plugin_package_not_approvable",
            "plugin does not have an adapter package requiring approval",
        )
    default_reason = (
        "adapter package approved for activation"
        if approval_state == "approved"
        else "adapter package approval was rejected"
    )
    approval_reason = request.reason.strip() or default_reason
    approved_package = PluginPackageMetadata.model_validate(
        {
            **package.model_dump(mode="json"),
            "approval_state": approval_state,
            "approval_reason": approval_reason,
            "approved_by": str(actor_id) if actor_id is not None else None,
            "approved_at": datetime.now(UTC).isoformat(),
        }
    )
    lifecycle = (
        {"status": "stopped", "health": "stopped", "last_error_type": None}
        if plugin.enabled
        else {"status": "disabled", "health": "disabled", "last_error_type": None}
    )
    return plugin.model_copy(update={"package_metadata": approved_package, **lifecycle})


def _ensure_plugin_package_activation_allowed(plugin: PluginResourceResponse) -> None:
    if _plugin_package_activation_blocked(plugin.package_metadata):
        raise PublicAPIError(
            409,
            "plugin_package_not_eligible",
            plugin.package_metadata.activation_reason
            if plugin.package_metadata is not None
            else "plugin package is not eligible for activation",
        )


def _plugin_package_activation_blocked(package: PluginPackageMetadata | None) -> bool:
    return (
        package is not None
        and package.kind == "adapter_package"
        and package.activation_state != "eligible"
    )


def _plugin_adapter_descriptors(request: Request) -> tuple[PluginAdapterDescriptorResponse, ...]:
    plugin_service = getattr(request.app.state, "plugin_service", None)
    provider = getattr(plugin_service, "adapter_descriptors", None)
    if callable(provider):
        try:
            raw_descriptors = tuple(provider())
        except Exception:
            _LOGGER.warning("plugin adapter descriptor catalog failed", exc_info=True)
        else:
            descriptors: list[PluginAdapterDescriptorResponse] = []
            for item in raw_descriptors:
                try:
                    descriptors.append(PluginAdapterDescriptorResponse.model_validate(item))
                except Exception:
                    _LOGGER.warning("plugin adapter descriptor skipped", exc_info=True)
            if descriptors:
                return tuple(descriptors)
    return (_default_http_json_adapter_descriptor(),)


def _default_http_json_adapter_descriptor() -> PluginAdapterDescriptorResponse:
    return PluginAdapterDescriptorResponse.model_validate(http_json_adapter_descriptor())


_PLUGIN_BUILTIN_RESOURCE_FIELDS = frozenset(
    (
        "endpoint_url",
        "domain_allowlist",
        "timeout_seconds",
        "credential_ref",
        "credential_header",
        "credential_scheme",
    )
)

_PLUGIN_BUILTIN_CAPABILITY_FIELDS = frozenset(
    (
        "id",
        "adapter",
        "permission_class",
        "sandbox_profile",
        "policy_effect",
        "replay_safe",
        "aliases",
        "input_schema",
        "output_schema",
    )
)


def _validate_plugin_resource_config(request: Request, plugin: PluginResourceRequest) -> None:
    descriptors = {descriptor.id: descriptor for descriptor in _plugin_adapter_descriptors(request)}
    fields: dict[str, Mapping[str, JsonValue]] = {}
    required_fields: set[str] = set()
    has_selected_descriptor = False
    all_selected_descriptors_allow_additional_properties = True
    for capability in plugin.capabilities:
        descriptor = descriptors.get(capability.adapter)
        if descriptor is None:
            continue
        has_selected_descriptor = True
        if descriptor.resource_schema.get("additionalProperties") is not True:
            all_selected_descriptors_allow_additional_properties = False
        properties = descriptor.resource_schema.get("properties")
        if isinstance(properties, Mapping):
            for property_name, schema in properties.items():
                if property_name in _PLUGIN_BUILTIN_RESOURCE_FIELDS:
                    continue
                if isinstance(property_name, str) and isinstance(schema, Mapping):
                    fields[property_name] = schema
        required = descriptor.resource_schema.get("required")
        if isinstance(required, (list, tuple)):
            for required_name in required:
                if (
                    isinstance(required_name, str)
                    and required_name not in _PLUGIN_BUILTIN_RESOURCE_FIELDS
                ):
                    required_fields.add(required_name)
    for name in sorted(required_fields):
        if name not in plugin.resource_config:
            raise PublicAPIError(
                422,
                "invalid_plugin_resource_config",
                f"plugin resource_config missing required field {name}",
            )
    for name, value in plugin.resource_config.items():
        schema = fields.get(name)
        if schema is None:
            if has_selected_descriptor and not all_selected_descriptors_allow_additional_properties:
                raise PublicAPIError(
                    422,
                    "invalid_plugin_resource_config",
                    f"plugin resource_config field {name} is not supported by selected adapters",
                )
            continue
        if not _plugin_resource_value_matches_schema(value, schema):
            raise PublicAPIError(
                422,
                "invalid_plugin_resource_config",
                f"plugin resource_config field {name} has invalid type",
            )


def _validate_plugin_capability_configs(request: Request, plugin: PluginResourceRequest) -> None:
    descriptors = {descriptor.id: descriptor for descriptor in _plugin_adapter_descriptors(request)}
    for capability in plugin.capabilities:
        descriptor = descriptors.get(capability.adapter)
        if descriptor is None:
            continue
        fields: dict[str, Mapping[str, JsonValue]] = {}
        required_fields: set[str] = set()
        properties = descriptor.capability_schema.get("properties")
        if isinstance(properties, Mapping):
            for property_name, schema in properties.items():
                if property_name in _PLUGIN_BUILTIN_CAPABILITY_FIELDS:
                    continue
                if isinstance(property_name, str) and isinstance(schema, Mapping):
                    fields[property_name] = schema
        required = descriptor.capability_schema.get("required")
        if isinstance(required, (list, tuple)):
            for required_name in required:
                if (
                    isinstance(required_name, str)
                    and required_name not in _PLUGIN_BUILTIN_CAPABILITY_FIELDS
                ):
                    required_fields.add(required_name)
        for name in sorted(required_fields):
            if name not in capability.capability_config:
                raise PublicAPIError(
                    422,
                    "invalid_plugin_capability_config",
                    f"plugin capability {capability.id} capability_config missing required field {name}",
                )
        allow_additional_properties = descriptor.capability_schema.get(
            "additionalProperties"
        ) is True
        for name, value in capability.capability_config.items():
            schema = fields.get(name)
            if schema is None:
                if allow_additional_properties:
                    continue
                raise PublicAPIError(
                    422,
                    "invalid_plugin_capability_config",
                    f"plugin capability {capability.id} capability_config field {name} is not supported by selected adapter",
                )
            if not _plugin_resource_value_matches_schema(value, schema):
                raise PublicAPIError(
                    422,
                    "invalid_plugin_capability_config",
                    f"plugin capability {capability.id} capability_config field {name} has invalid type",
                )


def _plugin_resource_value_matches_schema(value: JsonValue, schema: Mapping[str, JsonValue]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and _number_in_bounds(value, schema)
    if schema_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and _number_in_bounds(float(value), schema)
        )
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, Mapping) and items.get("type") == "string":
            return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return True


def _number_in_bounds(value: float, schema: Mapping[str, JsonValue]) -> bool:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        return False
    maximum = schema.get("maximum")
    return not (
        isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum
    )


@dataclass(slots=True)
class InMemoryAdminResourceService:
    models: dict[UUID, ModelDeploymentResponse] = field(default_factory=dict)
    secrets: dict[str, SecretReferenceResponse] = field(default_factory=dict)
    secret_values: dict[str, str] = field(default_factory=dict)
    secret_fingerprints: set[str] = field(default_factory=set)
    draft_yaml: str = ""
    published_yaml: str = ""
    version: int = 0
    agents: dict[str, AgentResourceResponse] = field(default_factory=dict)
    workflows: dict[str, WorkflowResourceResponse] = field(default_factory=dict)
    settings: SystemSettingsResponse = field(default_factory=SystemSettingsResponse)
    main_agent_config: MainAgentConfigResponse = field(default_factory=MainAgentConfigResponse)
    runs: dict[UUID, RunDetailResponse] = field(default_factory=dict)
    skills: dict[str, SkillResponse] = field(default_factory=dict)
    skill_active_versions: dict[str, str] = field(default_factory=dict)
    plugins: dict[str, PluginResourceResponse] = field(default_factory=dict)
    plugin_signing_keys: dict[tuple[UUID | None, str], PluginSigningKeyResponse] = field(
        default_factory=dict
    )
    mcp_servers: dict[str, McpServerResponse] = field(default_factory=dict)
    channel_config: dict[str, dict[str, str]] = field(default_factory=dict)
    memory: dict[str, MemoryRecordResponse] = field(default_factory=dict)
    audit_events: list[AuditEventResponse] = field(default_factory=list)
    logs: list[LogEntryResponse] = field(default_factory=list)
    hermes_insights: dict[str, HermesInsightResponse] = field(default_factory=dict)
    openclaw_operations: dict[str, OpenClawOperationResponse] = field(default_factory=dict)
    openclaw_sessions: dict[str, OpenClawSessionResponse] = field(default_factory=dict)
    evolution_runs: dict[str, EvolutionRunResponse] = field(default_factory=dict)
    generated_artifacts: dict[tuple[UUID, UUID], tuple[Path, str, str]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.runs:
            run_id = UUID("22222222-2222-4222-8222-222222222222")
            now = datetime.now(UTC)
            self.runs[run_id] = RunDetailResponse(
                id=run_id,
                status="running",
                mode="dispatch",
                queue_wait_ms=120,
                capacity_wait_ms=40,
                cost_usd="0.0132",
                request="Summarize current deployment readiness.",
                events=[
                    RunEventResponse(
                        sequence=1,
                        kind="queued",
                        message="Run accepted and queued.",
                        created_at=now,
                    ),
                    RunEventResponse(
                        sequence=2,
                        kind="step.started",
                        message="Planner started.",
                        created_at=now,
                    ),
                ],
                artifacts=[
                    RunArtifactResponse(id="artifact-1", kind="markdown", title="Readiness report")
                ],
                explicit_details={
                    "routing": "dispatch mode selected explicitly",
                    "saturation": "queue first, fallback after timeout",
                    "conversation_id": "conv-readiness",
                },
            )
        if not self.mcp_servers:
            self.mcp_servers["filesystem"] = McpServerResponse(
                id="filesystem",
                name="Filesystem MCP",
                health="healthy",
                allowed_tools=["read_file", "list_directory"],
                transport="stdio",
                command=None,
                args=[],
                executable_allowlist=[],
            )
        if not self.memory:
            self.memory["project-policy"] = MemoryRecordResponse(
                id="project-policy",
                scope="tenant",
                value="Only non-dangerous operations may run without approval.",
            )
        if not self.audit_events:
            self.audit_events.append(
                AuditEventResponse(
                    id="audit-1",
                    actor="system",
                    action="config.publish",
                    resource="configuration",
                    created_at=datetime.now(UTC),
                )
            )
        if not self.hermes_insights:
            self.hermes_insights["hermes-1"] = HermesInsightResponse(
                id="hermes-1",
                category="conversation",
                outcome="success",
                lesson="Use dispatch mode when the request has clear deliverables and separable steps.",
                summary=_hermes_feedback_summary(
                    outcome="success",
                    lesson="Use dispatch mode when the request has clear deliverables and separable steps.",
                    tags=["dispatch", "planning", "clear-task"],
                    weight=3,
                ),
                user_summary=_hermes_user_summary(
                    outcome="success",
                    lesson="Use dispatch mode when the request has clear deliverables and separable steps.",
                    category="conversation",
                ),
                run_id=None,
                conversation_id="conv-readiness",
                confirmed_at=None,
                tags=["dispatch", "planning", "clear-task"],
                weight=3,
                created_at=datetime.now(UTC),
            )

    async def list_models(self) -> tuple[ModelDeploymentResponse, ...]:
        return tuple(self.models.values())

    async def create_model(self, request: ModelDeploymentRequest) -> ModelDeploymentResponse:
        request = _normalize_model_request_api_base(request)
        response = ModelDeploymentResponse(
            **request.model_dump(),
            id=uuid4(),
            effective_slots=_model_effective_slots(
                request.max_concurrency,
                request.target_utilization,
                request.reserved_capacity,
            ),
            saturation_policy="queue_first_then_fallback",
        )
        self.models[response.id] = response
        return response

    async def update_model(
        self,
        model_id: UUID,
        request: ModelDeploymentRequest,
    ) -> ModelDeploymentResponse:
        if model_id not in self.models:
            raise PublicAPIError(404, "model_not_found", "model not found")
        request = _normalize_model_request_api_base(request)
        response = ModelDeploymentResponse(
            **request.model_dump(),
            id=model_id,
            effective_slots=_model_effective_slots(
                request.max_concurrency,
                request.target_utilization,
                request.reserved_capacity,
            ),
            saturation_policy="queue_first_then_fallback",
        )
        self.models[model_id] = response
        return response

    async def delete_model(self, model_id: UUID) -> None:
        if self.models.pop(model_id, None) is None:
            raise PublicAPIError(404, "model_not_found", "model not found")

    async def get_main_agent_config(self) -> MainAgentConfigResponse:
        return self.main_agent_config

    async def update_main_agent_config(
        self,
        request: MainAgentConfigRequest,
    ) -> MainAgentConfigResponse:
        response = MainAgentConfigResponse(**_normalize_main_agent_config(request).model_dump())
        self.main_agent_config = response
        return response

    async def create_secret(self, request: SecretCreateRequest) -> SecretReferenceResponse:
        raw = request.value.get_secret_value()
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        if fingerprint in self.secret_fingerprints:
            raise ValueError("duplicate secret")
        self.secret_fingerprints.add(fingerprint)
        ref = f"secret_{uuid4().hex}"
        response = SecretReferenceResponse(ref=ref, last_four=raw[-4:])
        self.secrets[ref] = response
        self.secret_values[ref] = raw
        return response

    async def get_secret(self, ref: str) -> SecretReferenceResponse:
        return self.secrets[ref]

    async def resolve_secret_value(self, ref: str) -> str:
        return self.secret_values[ref]

    async def probe_concurrency(self, request: ProbeRequest) -> ProbeResponse:
        recommended = max(1, min(request.desired_concurrency, 8))
        return ProbeResponse(
            recommended_concurrency=recommended,
            warning="same provider account keys may share quota; save explicitly to apply",
        )

    async def save_draft(self, request: DraftRequest) -> PublishResponse:
        self.draft_yaml = request.yaml
        return PublishResponse(version=self.version, status="draft")

    async def diff_draft(self, request: DraftRequest) -> DiffResponse:
        added: list[str] = [] if request.yaml == self.published_yaml else ["draft"]
        removed: list[str] = []
        changed: list[str] = [] if request.yaml == self.published_yaml else ["configuration"]
        return DiffResponse(added=added, removed=removed, changed=changed)

    async def publish(self, request: PublishRequest) -> PublishResponse:
        if request.expected_version != self.version:
            raise RuntimeError("publish conflict")
        self.version += 1
        self.published_yaml = self.draft_yaml
        return PublishResponse(version=self.version, status="published")

    async def rollback(self, version: int) -> PublishResponse:
        if version < 0 or version > self.version:
            raise ValueError("invalid rollback version")
        self.version = version
        return PublishResponse(version=self.version, status="rolled_back")

    async def list_agents(self) -> tuple[AgentResourceResponse, ...]:
        return tuple(self.agents.values())

    async def upsert_agent(self, request: AgentResourceRequest) -> AgentResourceResponse:
        if request.model is None:
            await self.record_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent model is required",
                source="agents.upsert",
                details={"agent_id": request.id, "reason": "missing_model"},
            )
        if request.prompt is None:
            await self.record_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent prompt is required",
                source="agents.upsert",
                details={"agent_id": request.id, "reason": "missing_prompt"},
            )
        response = AgentResourceResponse(**request.model_dump())
        self.agents[response.id] = response
        return response

    async def delete_agent(self, agent_id: str) -> None:
        if agent_id not in self.agents:
            raise KeyError(agent_id)
        del self.agents[agent_id]

    async def list_workflows(self) -> tuple[WorkflowResourceResponse, ...]:
        return tuple(self.workflows.values())

    async def upsert_workflow(self, request: WorkflowResourceRequest) -> WorkflowResourceResponse:
        response = WorkflowResourceResponse(**request.model_dump())
        self.workflows[response.id] = response
        return response

    async def delete_workflow(self, workflow_id: str) -> None:
        if workflow_id not in self.workflows:
            raise KeyError(workflow_id)
        del self.workflows[workflow_id]

    async def get_settings(self) -> SystemSettingsResponse:
        return self.settings

    async def update_settings(self, request: SystemSettingsRequest) -> SystemSettingsResponse:
        response = SystemSettingsResponse(**_persisted_system_settings_payload(request))
        self.settings = response
        return response

    async def list_runs(self) -> tuple[RunListItem, ...]:
        return tuple(
            RunListItem(
                id=run.id,
                status=run.status,
                mode=run.mode,
                version=run.version,
                conversation_id=run.explicit_details.get("conversation_id"),
                request=run.request,
                created_at=run.events[0].created_at if run.events else None,
                queue_wait_ms=run.queue_wait_ms,
                capacity_wait_ms=run.capacity_wait_ms,
                cost_usd=run.cost_usd,
            )
            for run in self.runs.values()
        )

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        return self.runs[run_id]

    async def download_run_artifact(
        self, run_id: UUID, artifact_id: UUID, *, tenant_id: UUID | None = None
    ) -> GeneratedArtifactDownload:
        del tenant_id
        path, filename, mime_type = self.generated_artifacts[(run_id, artifact_id)]
        return GeneratedArtifactDownload(
            path=path,
            filename=validate_generated_filename(filename, mime_type),
            mime_type=mime_type,
        )

    async def get_conversation(self, conversation_id: str) -> ConversationResponse:
        return ConversationResponse(
            conversation_id=conversation_id,
            runs=[
                run
                for run in self.runs.values()
                if run.explicit_details.get("conversation_id") == conversation_id
            ],
        )

    async def pause_run(self, run_id: UUID) -> RunDetailResponse:
        return self._set_run_status(run_id, "paused")

    async def resume_run(self, run_id: UUID) -> RunDetailResponse:
        return self._set_run_status(run_id, "running")

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse:
        return self._set_run_status(run_id, "cancelled")

    async def delete_run(self, run_id: UUID) -> RunDeleteResponse:
        current = self.runs[run_id]
        if current.status not in {"completed", "failed", "cancelled"}:
            raise PublicAPIError(
                409,
                "run_conflict",
                "run must be completed, failed, or cancelled before deletion",
                details={"status": current.status},
            )
        del self.runs[run_id]
        return RunDeleteResponse(id=run_id, deleted=True)

    def _set_run_status(self, run_id: UUID, status: str) -> RunDetailResponse:
        current = self.runs[run_id]
        updated = current.model_copy(update={"status": status})
        self.runs[run_id] = updated
        return updated

    async def list_evolution_runs(self) -> tuple[EvolutionRunResponse, ...]:
        return tuple(
            sorted(self.evolution_runs.values(), key=lambda item: item.created_at, reverse=True)
        )

    async def create_evolution_run(
        self,
        request: EvolutionRunRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        response = create_evolution_run_response(request, actor=actor)
        self.evolution_runs[response.id] = response
        await self.record_audit_event(
            actor=actor,
            action="evolution.create",
            resource=f"evolution:{response.id}",
            details={
                "kind": response.kind,
                "mode": response.mode,
                "target_artifact_type": response.target_artifact_type,
            },
        )
        return response

    async def get_evolution_run(self, run_id: str) -> EvolutionRunResponse:
        try:
            return self.evolution_runs[run_id]
        except KeyError:
            raise PublicAPIError(404, "not_found", "not found") from None

    async def plan_evolution_next_round(
        self,
        run_id: str,
        *,
        actor: str,
    ) -> EvolutionNextRoundPlanResponse:
        del actor
        current = await self.get_evolution_run(run_id)
        if current.status in {"stopped", "completed"}:
            raise PublicAPIError(409, "evolution_run_closed", "evolution run is already closed")
        if current.status == "waiting_approval":
            raise PublicAPIError(
                409,
                "evolution_run_requires_approval",
                "evolution run requires approval before planning the next round",
            )
        return plan_evolution_next_round(current)

    async def execute_evolution_next_round(
        self,
        run_id: str,
        request: EvolutionNextRoundExecutionRequest,
        *,
        actor: str,
    ) -> EvolutionNextRoundExecutionResponse:
        del request
        current = await self.get_evolution_run(run_id)
        plan = await self.plan_evolution_next_round(run_id, actor=actor)
        execution_run_id = uuid4()
        conversation_id = _evolution_execution_conversation_id(run_id, plan.round)
        now = datetime.now(UTC)
        self.runs[execution_run_id] = RunDetailResponse(
            id=execution_run_id,
            status="queued",
            mode=current.mode,
            conversation_id=conversation_id,
            request=plan.task_prompt,
            created_at=now,
            queue_wait_ms=0,
            capacity_wait_ms=0,
            cost_usd="0",
            events=[
                RunEventResponse(
                    sequence=1,
                    kind="queued",
                    message="Evolution round execution queued.",
                    created_at=now,
                )
            ],
            artifacts=[],
            explicit_details={
                "source": "evolution",
                **_evolution_execution_routing_details(current, plan, conversation_id),
            },
        )
        await self.record_audit_event(
            actor=actor,
            action="evolution.round_execution_queued",
            resource=f"evolution:{run_id}",
            details={
                "round": plan.round,
                "action": plan.action,
                "execution_run_id": str(execution_run_id),
                "execution_conversation_id": conversation_id,
            },
        )
        return EvolutionNextRoundExecutionResponse(
            evolution_run_id=run_id,
            round=plan.round,
            action=plan.action,
            execution_run_id=str(execution_run_id),
            execution_conversation_id=conversation_id,
            status="queued",
            task_title=plan.task_title,
            task_prompt=plan.task_prompt,
        )

    async def ingest_evolution_execution_run(
        self,
        run_id: str,
        execution_run_id: UUID,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        current = await self.get_evolution_run(run_id)
        if current.status in {"stopped", "completed"}:
            raise PublicAPIError(409, "evolution_run_closed", "evolution run is already closed")
        if current.status == "waiting_approval":
            raise PublicAPIError(
                409,
                "evolution_run_requires_approval",
                "evolution run requires approval before ingesting execution results",
            )
        try:
            execution = self.runs[execution_run_id]
        except KeyError:
            raise PublicAPIError(404, "not_found", "execution run was not found") from None
        if execution.status != RunStatus.COMPLETED.value:
            raise PublicAPIError(
                409,
                "evolution_execution_not_completed",
                "execution run must be completed before it can be ingested",
                details={"status": execution.status},
            )
        _assert_evolution_execution_binding(
            run_id,
            execution_run_id,
            execution.explicit_details,
            expected_round=len(current.rounds) + 1,
        )
        request = _evolution_round_request_from_artifacts(
            execution.artifacts, execution_run_id=execution_run_id
        )
        updated = append_evolution_round(current, request)
        latest = updated.rounds[-1]
        self.evolution_runs[run_id] = updated
        await self.record_audit_event(
            actor=actor,
            action="evolution.round_ingested",
            resource=f"evolution:{run_id}",
            details={
                "round": latest.round,
                "delta": latest.delta,
                "accepted": latest.accepted,
                "recommendation": latest.recommendation,
                "status": updated.status,
                "next_action": updated.next_action,
                "execution_run_id": str(execution_run_id),
            },
        )
        return updated

    async def approve_evolution_run(
        self,
        run_id: str,
        request: EvolutionApprovalRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        current = await self.get_evolution_run(run_id)
        updated = approve_evolution_run_response(current, request, actor=actor)
        self.evolution_runs[run_id] = updated
        await self.record_audit_event(
            actor=actor,
            action="evolution.approve",
            resource=f"evolution:{run_id}",
            details={
                "approval_status": updated.approval_status,
                "status": updated.status,
                "baseline_agent_id": updated.baseline_agent_id,
                "evaluator_agent_id": updated.evaluator_agent_id,
                "next_action": updated.next_action,
            },
        )
        return updated

    async def record_evolution_round(
        self,
        run_id: str,
        request: EvolutionRoundRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        current = await self.get_evolution_run(run_id)
        if current.status in {"stopped", "completed"}:
            raise PublicAPIError(409, "evolution_run_closed", "evolution run is already closed")
        if current.status == "waiting_approval":
            raise PublicAPIError(
                409,
                "evolution_run_requires_approval",
                "evolution run requires approval before recording rounds",
            )
        updated = append_evolution_round(current, request)
        latest = updated.rounds[-1]
        self.evolution_runs[run_id] = updated
        await self.record_audit_event(
            actor=actor,
            action="evolution.round_recorded",
            resource=f"evolution:{run_id}",
            details={
                "round": latest.round,
                "delta": latest.delta,
                "accepted": latest.accepted,
                "recommendation": latest.recommendation,
                "status": updated.status,
                "next_action": updated.next_action,
            },
        )
        return updated

    async def list_skills(self) -> tuple[SkillResponse, ...]:
        records = [
            _SkillVersionRecord(response=skill, created_at=None, updated_at=None, ordinal=index)
            for index, skill in enumerate(self.skills.values())
        ]
        return _group_skill_records(records, self.skill_active_versions)

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse:
        skill_id = _manual_skill_upload_id(request.filename)
        response = SkillResponse(
            id=skill_id,
            name=_skill_name_slug(PurePosixPath(request.filename).stem),
            status="quarantined",
            scan_diff=["added SKILL.md", "no dangerous operations detected"],
            requested_permissions=["filesystem:read"],
        )
        self.skills[response.id] = response
        return response

    async def upload_skill_archive(
        self, filename: str, archive_bytes: bytes, *, strategy: str | None = None
    ) -> SkillArchiveUploadResponse:
        strategy = _skill_upload_strategy_or_error(strategy)
        try:
            bundle, scanned_archives, skipped_archives = _scan_skill_archive_upload(
                filename, archive_bytes
            )
        except InvalidSkillPackage as error:
            reason = _safe_model_check_detail(str(error))
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={"feature": "skills", "filename": filename, "reason": reason},
            )
            raise
        items: list[SkillResponse] = []
        seen_skill_ids: set[str] = set()
        skipped = list(skipped_archives)
        for scanned in scanned_archives:
            skill_id = _skill_id_from_scanned_archive(scanned)
            if skill_id in seen_skill_ids:
                skipped.append(
                    _SkippedSkillArchive(
                        path=scanned.filename,
                        reason="duplicate skill identity skipped",
                    )
                )
                continue
            seen_skill_ids.add(skill_id)
            response = _skill_response_from_scanned_archive(scanned, skill_id)
            existing_versions = [
                skill for skill in self.skills.values() if skill.name == response.name
            ]
            matching_content = _matching_skill_content(existing_versions, response.content_sha256)
            if matching_content is not None:
                items.append(matching_content)
                continue
            if existing_versions:
                current = _current_skill_for_name(
                    existing_versions, self.skill_active_versions, response.name
                )
                if strategy is None:
                    raise _skill_version_choice_error(response, current)
                skill_id = (
                    current.id if strategy == "overwrite" else _skill_version_id_from_scanned_archive(scanned)
                )
                response = _skill_response_from_scanned_archive(scanned, skill_id)
            self.skills[response.id] = response
            if existing_versions:
                self.skill_active_versions[response.name] = response.id
            items.append(response)
        return SkillArchiveUploadResponse(
            filename=filename,
            bundle=bundle,
            items=items,
            skipped=_skipped_skill_responses(tuple(skipped)),
        )

    async def approve_skill(self, skill_id: str) -> SkillResponse:
        current = self.skills[skill_id]
        updated = current.model_copy(update={"status": "enabled"})
        self.skills[skill_id] = updated
        return updated

    async def activate_skill_version(self, skill_id: str, version_id: str) -> SkillResponse:
        try:
            skill = self.skills[skill_id]
            version = self.skills[version_id]
        except KeyError as exc:
            raise KeyError(str(exc)) from None
        if skill.name != version.name:
            raise PublicAPIError(
                409,
                "skill_version_mismatch",
                "skill version does not belong to this skill",
            )
        self.skill_active_versions[skill.name] = version.id
        grouped = await self.list_skills()
        return next(item for item in grouped if item.name == skill.name)

    async def delete_skill(self, skill_id: str) -> None:
        del self.skills[skill_id]

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        signing_keys = await self.list_plugin_signing_keys(tenant_id=tenant_id)
        now = datetime.now(UTC)
        return tuple(
            _plugin_with_effective_package_trust(plugin, signing_keys, now)
            for plugin in self.plugins.values()
        )

    async def list_plugin_signing_keys(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginSigningKeyResponse, ...]:
        return tuple(
            key
            for (key_tenant_id, _key_id), key in self.plugin_signing_keys.items()
            if key_tenant_id == tenant_id
        )

    async def upsert_plugin_signing_key(
        self,
        request: PluginSigningKeyRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginSigningKeyResponse:
        del actor_id
        response = PluginSigningKeyResponse(**request.model_dump(), trusted=True)
        self.plugin_signing_keys[(tenant_id, response.key_id)] = response
        return response

    async def delete_plugin_signing_key(
        self,
        key_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        del actor_id
        del self.plugin_signing_keys[(tenant_id, key_id)]

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
        del tenant_id, actor_id
        current = self.plugins.get(request.id)
        response = _plugin_response_from_request(
            request,
            current=current,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
        )
        self.plugins[response.id] = response
        return response

    async def start_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id, actor_id
        current = self.plugins[plugin_id]
        updated = _plugin_started_response(current)
        self.plugins[plugin_id] = updated
        return updated

    async def enable_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id, actor_id
        current = self.plugins[plugin_id]
        updated = _plugin_enabled_response(current)
        self.plugins[plugin_id] = updated
        return updated

    async def disable_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id, actor_id
        current = self.plugins[plugin_id]
        updated = _plugin_disabled_response(current)
        self.plugins[plugin_id] = updated
        return updated

    async def stop_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id, actor_id
        current = self.plugins[plugin_id]
        updated = _plugin_stopped_response(current)
        self.plugins[plugin_id] = updated
        return updated

    async def reload_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id, actor_id
        current = self.plugins[plugin_id]
        updated = _plugin_started_response(current)
        self.plugins[plugin_id] = updated
        return updated

    async def approve_plugin_package(
        self,
        plugin_id: str,
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id
        current = self.plugins[plugin_id]
        updated = _plugin_with_package_approval(
            current,
            "approved",
            request,
            actor_id=actor_id,
        )
        self.plugins[plugin_id] = updated
        return updated

    async def reject_plugin_package(
        self,
        plugin_id: str,
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        del tenant_id
        current = self.plugins[plugin_id]
        updated = _plugin_with_package_approval(
            current,
            "rejected",
            request,
            actor_id=actor_id,
        )
        self.plugins[plugin_id] = updated
        return updated

    async def delete_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        del tenant_id, actor_id
        del self.plugins[plugin_id]

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        await self.delete_plugin(plugin_id, tenant_id=tenant_id, actor_id=actor_id)

    async def list_mcp_servers(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[McpServerResponse, ...]:
        return tuple(self.mcp_servers.values())

    async def upsert_mcp_server(self, request: McpServerRequest) -> McpServerResponse:
        response = McpServerResponse(
            id=request.id,
            name=request.name,
            health="configured",
            allowed_tools=request.allowed_tools,
            transport=request.transport,
            command=request.command,
            args=request.args,
            url=request.url,
            executable_allowlist=request.executable_allowlist,
            domain_allowlist=request.domain_allowlist,
            timeout_seconds=request.timeout_seconds,
        )
        self.mcp_servers[response.id] = response
        return response

    async def delete_mcp_server(self, server_id: str) -> None:
        del self.mcp_servers[server_id]

    async def list_channels(self) -> tuple[ChannelStatusResponse, ...]:
        return _channel_statuses_from_configuration(self.channel_config)

    async def save_channel_config(
        self, channel_id: str, request: ChannelConfigRequest
    ) -> ChannelConfigSaveResponse:
        definition = _channel_definition(channel_id)
        cleaned = _clean_channel_config_values(definition, request.values)
        if not cleaned:
            raise PublicAPIError(
                422, "request_validation", "at least one channel field is required"
            )
        existing = dict(self.channel_config.get(channel_id, {}))
        existing.update(cleaned)
        self.channel_config[channel_id] = existing
        return ChannelConfigSaveResponse(
            id=channel_id,
            saved=_ordered_channel_saved_fields(definition, cleaned),
            status=_channel_status_from_definition(definition, self.channel_config),
        )

    async def clear_channel_config(
        self, channel_id: str, *, actor: str
    ) -> ChannelConfigSaveResponse:
        definition = _channel_definition(channel_id)
        self.channel_config.pop(channel_id, None)
        await self.record_audit_event(
            actor=actor,
            action="channel.clear",
            resource=f"channel:{channel_id}",
            details={"cleared": ",".join(definition.required_env)},
        )
        return ChannelConfigSaveResponse(
            id=channel_id,
            saved=[],
            status=_channel_status_from_definition(definition, self.channel_config),
        )

    async def channel_runtime_config(self) -> dict[str, str]:
        return _flatten_channel_config(self.channel_config)

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]:
        return tuple(self.memory.values())

    async def create_memory(self, request: MemoryCreateRequest) -> MemoryRecordResponse:
        response = MemoryRecordResponse(**request.model_dump())
        self.memory[response.id] = response
        return response

    async def update_memory(
        self, memory_id: str, request: MemoryRecordRequest
    ) -> MemoryRecordResponse:
        current = self.memory[memory_id]
        updated = current.model_copy(update=request.model_dump(exclude_unset=True))
        self.memory[memory_id] = updated
        return updated

    async def forget_memory(self, memory_id: str) -> None:
        del self.memory[memory_id]

    async def list_audit_events(self, action: str | None = None) -> tuple[AuditEventResponse, ...]:
        events = self.audit_events
        if action is not None:
            events = [event for event in events if event.action == action]
        return tuple(events)

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
        tenant_id: UUID | None = None,
    ) -> AuditEventResponse:
        del tenant_id
        event = AuditEventResponse(
            id=f"audit_{uuid4().hex}",
            actor=actor,
            action=action,
            resource=resource,
            details=_safe_audit_details(details),
            created_at=datetime.now(UTC),
        )
        self.audit_events.append(event)
        return event

    async def create_openclaw_operation(
        self,
        request: OpenClawOperationRequest,
        *,
        actor: str,
        mode: str,
    ) -> OpenClawOperationResponse:
        operation_id = f"openclaw_{uuid4().hex}"
        response = _openclaw_operation_response(
            operation_id=operation_id,
            request=request,
            actor=actor,
            mode=mode,
        )
        self.openclaw_operations[operation_id] = response
        await self.record_audit_event(
            actor=actor,
            action="openclaw.approval_requested",
            resource=f"openclaw:{operation_id}",
            details=_openclaw_audit_details(request, mode),
        )
        return response

    async def get_openclaw_operation(self, operation_id: str) -> OpenClawOperationResponse:
        try:
            return self.openclaw_operations[operation_id]
        except KeyError:
            raise PublicAPIError(404, "not_found", "not found") from None

    async def resolve_openclaw_operation(
        self,
        operation_id: str,
        request: OpenClawResolveRequest,
        *,
        actor: str,
    ) -> OpenClawOperationResponse:
        current = await self.get_openclaw_operation(operation_id)
        if current.status != "waiting_user_approval":
            raise PublicAPIError(
                409,
                "openclaw_already_resolved",
                "OpenClaw operation is already resolved",
            )
        status = "approved" if request.decision == "approve" else "rejected"
        updated = current.model_copy(
            update={
                "status": status,
                "requires_user_approval": False,
                "resolved_by": actor,
                "resolved_at": datetime.now(UTC),
            }
        )
        self.openclaw_operations[operation_id] = updated
        await self.record_audit_event(
            actor=actor,
            action=f"openclaw.{status}",
            resource=f"openclaw:{operation_id}",
            details={"approval_id": current.approval_id},
        )
        return updated

    async def execute_openclaw_operation(
        self,
        operation_id: str,
        settings: SystemSettingsResponse,
        *,
        actor: str,
    ) -> OpenClawExecutionResponse:
        current = await self.get_openclaw_operation(operation_id)
        updated, result = await _execute_openclaw_operation(
            current,
            settings,
            actor=actor,
            adapter_token_resolver=self.resolve_secret_value,
        )
        self.openclaw_operations[operation_id] = updated
        await self.record_audit_event(
            actor=actor,
            action="openclaw.executed",
            resource=f"openclaw:{operation_id}",
            details={"approval_id": current.approval_id, "exit_code": result.exit_code},
        )
        return _openclaw_execution_response(updated, result)

    async def create_openclaw_session(
        self,
        request: OpenClawSessionRequest,
        *,
        actor: str,
        mode: str,
        settings: SystemSettingsResponse,
    ) -> OpenClawSessionResponse:
        session_id = f"openclaw_session_{uuid4().hex}"
        response = _openclaw_session_response(
            session_id=session_id,
            request=request,
            actor=actor,
            mode=mode,
            settings=settings,
        )
        self.openclaw_sessions[session_id] = response
        await self.record_audit_event(
            actor=actor,
            action="openclaw.session_created",
            resource=f"openclaw_session:{session_id}",
            details=_openclaw_session_audit_details(response),
        )
        return response

    async def list_openclaw_sessions(self) -> tuple[OpenClawSessionResponse, ...]:
        return tuple(self.openclaw_sessions.values())

    async def update_openclaw_session(
        self,
        session_id: str,
        request: OpenClawSessionActionRequest,
        *,
        actor: str,
    ) -> OpenClawSessionResponse:
        try:
            current = self.openclaw_sessions[session_id]
        except KeyError:
            raise PublicAPIError(404, "not_found", "not found") from None
        updated = _updated_openclaw_session(current, request)
        self.openclaw_sessions[session_id] = updated
        await self.record_audit_event(
            actor=actor,
            action=f"openclaw.session_{request.action}",
            resource=f"openclaw_session:{session_id}",
            details=_openclaw_session_audit_details(updated, action=request.action),
        )
        return updated

    async def attach_openclaw_operation_to_session(
        self,
        session_id: str,
        operation_id: str,
        request: OpenClawOperationRequest,
        *,
        actor: str,
    ) -> OpenClawSessionResponse:
        try:
            current = self.openclaw_sessions[session_id]
        except KeyError:
            raise PublicAPIError(404, "not_found", "not found") from None
        updated = _attach_openclaw_operation_to_session_payload(current, operation_id, request)
        self.openclaw_sessions[session_id] = updated
        await self.record_audit_event(
            actor=actor,
            action="openclaw.session_operation_attached",
            resource=f"openclaw_session:{session_id}",
            details={"operation_id": operation_id, **_openclaw_session_audit_details(updated)},
        )
        return updated

    def make_log(
        self,
        *,
        category: str,
        level: str,
        title: str,
        message: str,
        source: str,
        details: dict[str, str] | None = None,
    ) -> LogEntryResponse:
        return LogEntryResponse(
            id=f"log_{uuid4().hex}",
            category=category,
            level=level,
            title=title,
            message=message,
            source=source,
            details=_safe_log_details(details or {}),
            created_at=datetime.now(UTC),
        )

    async def record_log(
        self,
        *,
        category: str,
        level: str,
        title: str,
        message: str,
        source: str,
        details: dict[str, str] | None = None,
    ) -> LogEntryResponse:
        entry = self.make_log(
            category=category,
            level=level,
            title=title,
            message=message,
            source=source,
            details=details,
        )
        if entry.level != "info":
            self.logs.append(entry)
        return entry

    async def list_logs(self, category: str | None = None) -> tuple[LogEntryResponse, ...]:
        entries = [
            *(_audit_log_entry(event) for event in self.audit_events),
            *self.logs,
            *(
                _mode_error_log_from_run(run)
                for run in self.runs.values()
                if run.status == "failed"
            ),
            *_channel_error_logs_from_configuration(self.channel_config),
        ]
        if category is not None:
            entries = [entry for entry in entries if entry.category == category]
        return tuple(sorted(entries, key=lambda entry: entry.created_at, reverse=True))

    async def list_hermes_insights(self) -> tuple[HermesInsightResponse, ...]:
        return tuple(sorted(self.hermes_insights.values(), key=lambda insight: insight.created_at))

    async def get_hermes_insight(self, insight_id: str) -> HermesInsightResponse:
        return self.hermes_insights[insight_id]

    async def confirm_hermes_insight(self, insight_id: str) -> HermesInsightResponse:
        current = self.hermes_insights[insight_id]
        updated = current.model_copy(update={"confirmed_at": datetime.now(UTC)})
        self.hermes_insights[insight_id] = updated
        return updated

    async def delete_hermes_insight(self, insight_id: str) -> None:
        del self.hermes_insights[insight_id]

    async def record_hermes_feedback(self, request: HermesFeedbackRequest) -> HermesInsightResponse:
        if _contains_sensitive_marker(request.lesson):
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="Hermes feedback contains sensitive content",
                source="hermes.feedback",
                details={"feature": "hermes", "reason": "sensitive_content"},
            )
            raise ValueError("sensitive content")
        insight = HermesInsightResponse(
            id=f"hermes-{uuid4().hex}",
            outcome=request.outcome,
            lesson=request.lesson,
            summary=_hermes_feedback_summary(
                outcome=request.outcome,
                lesson=request.lesson,
                tags=request.tags,
                weight=request.weight,
            ),
            user_summary=_hermes_user_summary(
                outcome=request.outcome,
                lesson=request.lesson,
                category=request.category,
            ),
            run_id=request.run_id,
            conversation_id=request.conversation_id,
            confirmed_at=None,
            tags=request.tags,
            weight=request.weight,
            created_at=datetime.now(UTC),
        )
        self.hermes_insights[insight.id] = insight
        return insight

    async def recommend_with_hermes(
        self, request: HermesRecommendationRequest
    ) -> HermesRecommendationResponse:
        normalized_task = request.task.lower()
        reasons: list[str] = []
        recommended_mode = request.mode_candidates[0] if request.mode_candidates else "dispatch"
        if "讨论" in normalized_task or "debate" in normalized_task or "review" in normalized_task:
            if "group_chat" in request.mode_candidates:
                recommended_mode = "group_chat"
                reasons.append("Task benefits from multiple agent viewpoints.")
        elif "dispatch" in request.mode_candidates:
            recommended_mode = "dispatch"
            reasons.append("Task appears to have separable execution steps.")

        recommended_model = request.model_candidates[0] if request.model_candidates else None
        recommended_skills = [
            skill
            for skill in request.skill_candidates
            if skill.lower() in normalized_task
            or skill.lower().replace("-", " ") in normalized_task
        ][:3]
        if not recommended_skills and "skill" in normalized_task:
            recommended_skills = request.skill_candidates[:2]

        matching_insights = [
            insight
            for insight in self.hermes_insights.values()
            if any(tag.lower() in normalized_task for tag in insight.tags)
        ]
        if matching_insights:
            strongest = max(matching_insights, key=lambda insight: insight.weight)
            reasons.append(f"Matched prior Hermes lesson: {strongest.lesson}")

        if not reasons:
            reasons.append("No strong prior pattern matched; using conservative defaults.")

        confidence = min(0.9, 0.45 + 0.1 * len(matching_insights) + 0.05 * len(recommended_skills))
        return HermesRecommendationResponse(
            recommended_mode=recommended_mode,
            recommended_model=recommended_model,
            recommended_skills=recommended_skills,
            confidence=confidence,
            reasons=reasons,
            requires_approval=False,
        )


class PersistentAdminResourceService(InMemoryAdminResourceService):
    """Production admin resource service backed by config revisions and sealed secrets."""

    def __init__(
        self,
        *,
        config_service: ConfigService,
        secret_service: SecretService,
        tenant_id: UUID,
        actor_id: UUID,
        model_transport: ModelTransport | None = None,
        run_repository: RunRepository | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        skill_store_dir: Path | None = None,
        generated_artifact_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._secret_service = secret_service
        self._tenant_id = tenant_id
        self._actor_id = actor_id
        self._model_transport = model_transport or LiteLLMClient()
        self._run_repository = run_repository
        self._session_factory = session_factory
        self._skill_store_dir = skill_store_dir or Path("/var/lib/agent-hub/skills")
        self._generated_artifact_dir = generated_artifact_dir
        self._generated_file_store = (
            None if generated_artifact_dir is None else GeneratedFileStore(generated_artifact_dir)
        )

    def for_principal(
        self, tenant_id: UUID, actor_id: UUID
    ) -> PersistentAdminResourceService:
        if tenant_id == self._tenant_id and actor_id == self._actor_id:
            return self
        return PersistentAdminResourceService(
            config_service=self._config_service,
            secret_service=self._secret_service,
            tenant_id=tenant_id,
            actor_id=actor_id,
            model_transport=self._model_transport,
            run_repository=self._run_repository,
            session_factory=self._session_factory,
            skill_store_dir=self._skill_store_dir,
            generated_artifact_dir=self._generated_artifact_dir,
        )

    async def list_runs(self) -> tuple[RunListItem, ...]:
        if self._run_repository is None:
            return await super().list_runs()
        records = await self._run_repository.list_recent(self._tenant_id)
        items: list[RunListItem] = []
        for record in records:
            items.append(await self._run_list_item(record))
        return tuple(items)

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        if self._run_repository is None:
            return await super().get_run(run_id)
        try:
            record = await self._run_repository.get(self._tenant_id, run_id)
        except RunNotFound:
            raise KeyError(run_id) from None
        return await self._run_detail(record)

    async def download_run_artifact(
        self, run_id: UUID, artifact_id: UUID, *, tenant_id: UUID | None = None
    ) -> GeneratedArtifactDownload:
        if tenant_id is not None and tenant_id != self._tenant_id:
            raise KeyError(run_id)
        if self._run_repository is None or self._generated_file_store is None:
            return await super().download_run_artifact(
                run_id,
                artifact_id,
                tenant_id=tenant_id,
            )
        try:
            await self._run_repository.get(self._tenant_id, run_id)
            artifacts = await self._run_repository.raw_artifacts(self._tenant_id, run_id)
        except RunNotFound:
            raise KeyError(run_id) from None
        metadata = _find_generated_file_metadata(artifacts, artifact_id)
        try:
            path = self._generated_file_store.resolve_for(
                self._tenant_id,
                run_id,
                artifact_id,
                metadata["storage_key"],
            )
        except (FileNotFoundError, ValueError):
            raise KeyError(artifact_id) from None
        return GeneratedArtifactDownload(
            path=path,
            filename=metadata["filename"],
            mime_type=metadata["mime_type"],
        )

    async def get_conversation(self, conversation_id: str) -> ConversationResponse:
        if self._run_repository is None:
            return await super().get_conversation(conversation_id)
        records = await self._run_repository.list_recent(self._tenant_id, limit=200)
        runs: list[RunDetailResponse] = []
        for record in records:
            details = _routing_details(record.routing_decision)
            if details.get("conversation_id") == conversation_id:
                runs.append(await self._run_detail(record))
        runs.reverse()
        return ConversationResponse(conversation_id=conversation_id, runs=runs)

    async def pause_run(self, run_id: UUID) -> RunDetailResponse:
        if self._run_repository is None:
            return await super().pause_run(run_id)
        try:
            record = await self._run_repository.update_control_status(
                self._tenant_id,
                run_id,
                RunStatus.PAUSED,
            )
        except RunNotFound:
            raise KeyError(run_id) from None
        except RunConflict as error:
            reason = str(error) or "run state conflict"
            raise PublicAPIError(
                409,
                "run_conflict",
                reason,
                details={"reason": reason, "action": "pause"},
            ) from None
        return await self._run_detail(record)

    async def resume_run(self, run_id: UUID) -> RunDetailResponse:
        if self._run_repository is None:
            return await super().resume_run(run_id)
        try:
            record = await self._run_repository.enqueue_existing_run(
                tenant_id=self._tenant_id,
                run_id=run_id,
                from_status=RunStatus.PAUSED,
                idempotency_suffix="admin-resume",
            )
        except RunNotFound:
            raise KeyError(run_id) from None
        except RunConflict as error:
            reason = str(error) or "run state conflict"
            raise PublicAPIError(
                409,
                "run_conflict",
                reason,
                details={"reason": reason, "action": "resume"},
            ) from None
        return await self._run_detail(record)

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse:
        if self._run_repository is None:
            return await super().cancel_run(run_id)
        try:
            record = await self._run_repository.update_control_status(
                self._tenant_id,
                run_id,
                RunStatus.CANCELLED,
            )
        except RunNotFound:
            raise KeyError(run_id) from None
        except RunConflict as error:
            reason = str(error) or "run state conflict"
            raise PublicAPIError(
                409,
                "run_conflict",
                reason,
                details={"reason": reason, "action": "cancel"},
            ) from None
        return await self._run_detail(record)

    async def delete_run(self, run_id: UUID) -> RunDeleteResponse:
        if self._run_repository is None:
            return await super().delete_run(run_id)
        try:
            await self._run_repository.delete_run(self._tenant_id, run_id)
        except RunNotFound:
            raise KeyError(run_id) from None
        except RunConflict as error:
            reason = str(error) or "run state conflict"
            raise PublicAPIError(
                409,
                "run_conflict",
                reason,
                details={"reason": reason, "action": "delete"},
            ) from None
        if self._generated_file_store is not None:
            try:
                self._generated_file_store.delete_run(self._tenant_id, run_id)
            except (OSError, ValueError):
                _LOGGER.warning(
                    "failed to clean generated artifacts for deleted run %s",
                    run_id,
                    exc_info=True,
                )
        return RunDeleteResponse(id=run_id, deleted=True)

    async def _run_list_item(self, record: RunRecord) -> RunListItem:
        assert self._run_repository is not None
        return RunListItem(
            id=record.id,
            status=record.status.value,
            mode="auto" if record.mode is None else record.mode.value,
            version=record.version,
            conversation_id=_routing_details(record.routing_decision).get("conversation_id"),
            request=record.request,
            created_at=record.created_at,
            queue_wait_ms=0,
            capacity_wait_ms=0,
            cost_usd=str(await self._run_repository.usage_cost(self._tenant_id, record.id)),
        )

    async def _run_detail(self, record: RunRecord) -> RunDetailResponse:
        assert self._run_repository is not None
        list_item = await self._run_list_item(record)
        events = await self._run_repository.events(self._tenant_id, record.id)
        artifacts = await self._run_repository.artifacts(self._tenant_id, record.id)
        return RunDetailResponse(
            **list_item.model_dump(),
            events=[_admin_run_event(event) for event in events],
            artifacts=_admin_run_artifacts(artifacts, run_id=record.id),
            explicit_details={
                "source": "database",
                "version": str(record.version),
                **_routing_details(record.routing_decision),
            },
            decision_token=_waiting_mode_decision_token(record),
            temporary_agent_proposal=_temporary_agent_proposal(record.routing_decision),
            schedule_proposal=_schedule_proposal(record.routing_decision),
            evolution_proposal=_evolution_proposal(record.routing_decision),
            openclaw_proposal=_openclaw_proposal(record.routing_decision),
            repair_proposal=_repair_proposal(record.routing_decision),
        )

    async def list_models(self) -> tuple[ModelDeploymentResponse, ...]:
        revision = await self._config_service.get_current(self._tenant_id)
        if revision is None:
            return ()
        config = PlatformConfig.model_validate(revision.document)
        responses: list[ModelDeploymentResponse] = []
        for logical_model, definition in sorted(config.models.items()):
            for index, deployment in enumerate(definition.deployments):
                responses.append(
                    self._model_response(
                        logical_model,
                        definition.fallback_model,
                        index,
                        deployment,
                    )
                )
        return tuple(responses)

    async def create_model(self, request: ModelDeploymentRequest) -> ModelDeploymentResponse:
        request = _normalize_model_request_api_base(request)
        document = await self._current_document()
        models = cast(dict[str, object], document.setdefault("models", {}))
        existing = models.get(request.logical_model)
        if existing is None:
            logical_definition: dict[str, object] = {"deployments": []}
        else:
            logical_definition = dict(cast(dict[str, object], existing))
            logical_definition["deployments"] = list(
                cast(list[object], logical_definition.get("deployments", []))
            )
        deployment = _deployment_document_from_request(request)
        deployments = cast(list[object], logical_definition["deployments"])
        deployment_index = len(deployments)
        deployments.append(deployment)
        if request.fallback is not None:
            logical_definition["fallback_model"] = request.fallback
        models[request.logical_model] = logical_definition
        try:
            checked_deployment = (
                PlatformConfig.model_validate(document)
                .models[request.logical_model]
                .deployments[deployment_index]
            )
            await self._verify_model_availability(
                checked_deployment.to_deployment(
                    deployment_id=f"{request.logical_model}_{deployment_index + 1}",
                    logical_model=request.logical_model,
                )
            )
            draft = await self._config_service.create_draft(
                self._tenant_id,
                self._actor_id,
                document,
            )
            await self._config_service.publish(
                self._tenant_id,
                draft.version,
                self._actor_id,
            )
        except (ConfigValidationError, ValidationError) as error:
            await self._record_model_request_failure(
                request,
                reason=_safe_model_check_reason(error),
                stage="model_configuration_validation",
            )
            raise PublicAPIError(422, "request_validation", "request validation failed") from None
        current = await self._config_service.get_current(self._tenant_id)
        if current is None:
            raise PublicAPIError(503, "service_unavailable", "service unavailable")
        published = PlatformConfig.model_validate(current.document)
        created_definition = published.models[request.logical_model]
        await self._record_audit(
            "model.create",
            f"model:{request.logical_model}",
            {"provider": request.provider, "model": request.upstream_model},
        )
        return self._model_response(
            request.logical_model,
            created_definition.fallback_model,
            deployment_index,
            created_definition.deployments[deployment_index],
        )

    async def update_model(
        self, model_id: UUID, request: ModelDeploymentRequest
    ) -> ModelDeploymentResponse:
        request = _normalize_model_request_api_base(request)
        document = await self._current_document()
        models = cast(dict[str, object], document.setdefault("models", {}))
        target_logical_model: str | None = None
        target_index: int | None = None

        for logical_model, raw_definition in list(models.items()):
            logical_definition = dict(cast(dict[str, object], raw_definition))
            deployments = list(cast(list[object], logical_definition.get("deployments", [])))
            fallback_model = cast(str | None, logical_definition.get("fallback_model"))
            for index, deployment in enumerate(deployments):
                response = self._model_response(logical_model, fallback_model, index, deployment)
                if response.id == model_id:
                    target_logical_model = logical_model
                    target_index = index
                    break
            if target_logical_model is not None:
                break

        if target_logical_model is None or target_index is None:
            raise PublicAPIError(404, "model_not_found", "model not found")

        original_definition = dict(cast(dict[str, object], models[target_logical_model]))
        original_deployments = list(cast(list[object], original_definition.get("deployments", [])))
        original_deployments.pop(target_index)
        if original_deployments:
            original_definition["deployments"] = original_deployments
            models[target_logical_model] = original_definition
        else:
            del models[target_logical_model]

        target_definition_raw = dict(cast(dict[str, object], models.get(request.logical_model, {})))
        target_deployments = list(cast(list[object], target_definition_raw.get("deployments", [])))
        target_definition_raw["deployments"] = target_deployments
        target_deployments.append(_deployment_document_from_request(request))
        if request.fallback is not None:
            target_definition_raw["fallback_model"] = request.fallback
        models[request.logical_model] = target_definition_raw
        updated_index = len(target_deployments) - 1

        try:
            checked_deployment = (
                PlatformConfig.model_validate(document)
                .models[request.logical_model]
                .deployments[updated_index]
            )
            await self._verify_model_availability(
                checked_deployment.to_deployment(
                    deployment_id=f"{request.logical_model}_{updated_index + 1}",
                    logical_model=request.logical_model,
                ),
                source="models.update",
            )
            draft = await self._config_service.create_draft(
                self._tenant_id,
                self._actor_id,
                document,
            )
            await self._config_service.publish(
                self._tenant_id,
                draft.version,
                self._actor_id,
            )
        except (ConfigValidationError, ValidationError) as error:
            await self._record_model_request_failure(
                request,
                reason=_safe_model_check_reason(error),
                stage="model_configuration_validation",
                source="models.update",
            )
            raise PublicAPIError(422, "request_validation", "request validation failed") from None
        current = await self._config_service.get_current(self._tenant_id)
        if current is None:
            raise PublicAPIError(503, "service_unavailable", "service unavailable")
        published = PlatformConfig.model_validate(current.document)
        updated_definition = published.models[request.logical_model]
        await self._record_audit(
            "model.update",
            f"model:{request.logical_model}",
            {"provider": request.provider, "model": request.upstream_model},
        )
        return self._model_response(
            request.logical_model,
            updated_definition.fallback_model,
            updated_index,
            updated_definition.deployments[updated_index],
        )

    async def delete_model(self, model_id: UUID) -> None:
        document = await self._current_document()
        models = cast(dict[str, object], document.setdefault("models", {}))
        target_logical_model: str | None = None
        target_index: int | None = None
        target_response: ModelDeploymentResponse | None = None

        for logical_model, raw_definition in list(models.items()):
            logical_definition = dict(cast(dict[str, object], raw_definition))
            deployments = list(cast(list[object], logical_definition.get("deployments", [])))
            fallback_model = cast(str | None, logical_definition.get("fallback_model"))
            for index, deployment in enumerate(deployments):
                response = self._model_response(logical_model, fallback_model, index, deployment)
                if response.id == model_id:
                    target_logical_model = logical_model
                    target_index = index
                    target_response = response
                    break
            if target_response is not None:
                break

        if target_logical_model is None or target_index is None or target_response is None:
            raise PublicAPIError(404, "model_not_found", "model not found")

        logical_definition = dict(cast(dict[str, object], models[target_logical_model]))
        deployments = list(cast(list[object], logical_definition.get("deployments", [])))
        deployments.pop(target_index)
        if deployments:
            logical_definition["deployments"] = deployments
            models[target_logical_model] = logical_definition
        else:
            del models[target_logical_model]
            for raw_definition in models.values():
                if (
                    isinstance(raw_definition, dict)
                    and raw_definition.get("fallback_model") == target_logical_model
                ):
                    raw_definition.pop("fallback_model", None)

        try:
            PlatformConfig.model_validate(document)
            draft = await self._config_service.create_draft(
                self._tenant_id,
                self._actor_id,
                document,
            )
            await self._config_service.publish(
                self._tenant_id,
                draft.version,
                self._actor_id,
            )
        except (ConfigValidationError, ValidationError) as error:
            await self.record_log(
                category="model_error",
                level="error",
                title="模型删除失败",
                message=_safe_model_check_reason(error),
                source="models.delete",
                details={
                    "logical_model": target_logical_model,
                    "provider": target_response.provider,
                    "upstream_model": target_response.upstream_model,
                    "reason": _safe_model_check_reason(error),
                },
            )
            raise PublicAPIError(409, "model_in_use", "model is still referenced") from None

        await self._record_audit(
            "model.delete",
            f"model:{target_logical_model}",
            {"provider": target_response.provider, "model": target_response.upstream_model},
        )

    async def create_secret(self, request: SecretCreateRequest) -> SecretReferenceResponse:
        try:
            reference = await self._secret_service.create_or_get(
                self._tenant_id,
                self._actor_id,
                request.value.get_secret_value(),
            )
        except SecretValidationError:
            raise PublicAPIError(422, "request_validation", "request validation failed") from None
        value = request.value.get_secret_value()
        response = SecretReferenceResponse(ref=reference.reference, last_four=value[-4:])
        await self._record_audit("secret.create", "secret", {"label": request.label})
        return response

    async def resolve_secret_value(self, ref: str) -> str:
        return await self._secret_service.resolve(self._tenant_id, ref)

    async def list_agents(self) -> tuple[AgentResourceResponse, ...]:
        revision = await self._config_service.get_current(self._tenant_id)
        if revision is None:
            return ()
        config = PlatformConfig.model_validate(revision.document)
        return tuple(
            AgentResourceResponse(
                id=agent.id,
                name=agent.role,
                enabled=True,
                role=agent.role,
                prompt=agent.prompt,
                model=agent.model,
                skills=agent.skills,
            )
            for agent in config.agents
        )

    async def upsert_agent(self, request: AgentResourceRequest) -> AgentResourceResponse:
        if request.model is None:
            await self.record_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent model is required",
                source="agents.upsert",
                details={"agent_id": request.id, "reason": "missing_model"},
            )
            raise PublicAPIError(422, "request_validation", "agent model is required")
        if request.prompt is None:
            await self.record_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent prompt is required",
                source="agents.upsert",
                details={"agent_id": request.id, "reason": "missing_prompt"},
            )
            raise PublicAPIError(422, "request_validation", "agent prompt is required")
        role = request.role or request.name
        document = await self._current_document()
        agents = list(cast(list[object], document.setdefault("agents", [])))
        replacement = {
            "id": request.id,
            "role": role,
            "prompt": request.prompt,
            "model": request.model,
            "skills": request.skills,
        }
        replaced = False
        next_agents: list[object] = []
        for existing in agents:
            if isinstance(existing, dict) and existing.get("id") == request.id:
                next_agents.append(replacement)
                replaced = True
            else:
                next_agents.append(existing)
        if not replaced:
            next_agents.append(replacement)
        document["agents"] = next_agents
        try:
            PlatformConfig.model_validate(document)
            draft = await self._config_service.create_draft(
                self._tenant_id,
                self._actor_id,
                document,
            )
            await self._config_service.publish(
                self._tenant_id,
                draft.version,
                self._actor_id,
            )
        except (ConfigValidationError, ValidationError):
            raise PublicAPIError(422, "request_validation", "request validation failed") from None
        current = await self._config_service.get_current(self._tenant_id)
        if current is None:
            raise PublicAPIError(503, "service_unavailable", "service unavailable")
        config = PlatformConfig.model_validate(current.document)
        for agent in config.agents:
            if agent.id == request.id:
                response = AgentResourceResponse(
                    id=agent.id,
                    name=agent.role,
                    enabled=request.enabled,
                    role=agent.role,
                    prompt=agent.prompt,
                    model=agent.model,
                    skills=agent.skills,
                )
                await self._record_audit("agent.upsert", f"agent:{agent.id}", {"id": agent.id})
                return response
        raise PublicAPIError(503, "service_unavailable", "service unavailable")

    async def delete_agent(self, agent_id: str) -> None:
        document = await self._current_document()
        agents = list(cast(list[object], document.setdefault("agents", [])))
        next_agents = [
            existing
            for existing in agents
            if not (isinstance(existing, dict) and existing.get("id") == agent_id)
        ]
        if len(next_agents) == len(agents):
            raise KeyError(agent_id)
        document["agents"] = next_agents
        try:
            PlatformConfig.model_validate(document)
            draft = await self._config_service.create_draft(
                self._tenant_id,
                self._actor_id,
                document,
            )
            await self._config_service.publish(
                self._tenant_id,
                draft.version,
                self._actor_id,
            )
        except (ConfigValidationError, ValidationError):
            raise PublicAPIError(422, "request_validation", "request validation failed") from None
        await self._record_audit("agent.delete", f"agent:{agent_id}", {"id": agent_id})

    async def list_workflows(self) -> tuple[WorkflowResourceResponse, ...]:
        resources = await self._list_admin_payloads("workflow")
        if resources is None:
            return await super().list_workflows()
        return tuple(WorkflowResourceResponse.model_validate(payload) for payload in resources)

    async def upsert_workflow(self, request: WorkflowResourceRequest) -> WorkflowResourceResponse:
        response = WorkflowResourceResponse(**request.model_dump())
        if not await self._upsert_admin_payload(
            "workflow", response.id, response.model_dump(mode="json")
        ):
            return await super().upsert_workflow(request)
        await self._record_audit("workflow.upsert", f"workflow:{response.id}", {"id": response.id})
        return response

    async def delete_workflow(self, workflow_id: str) -> None:
        deleted = await self._delete_admin_payload("workflow", workflow_id)
        if deleted is None:
            return await super().delete_workflow(workflow_id)
        if not deleted:
            raise KeyError(workflow_id)
        await self._record_audit("workflow.delete", f"workflow:{workflow_id}", {"id": workflow_id})

    async def get_settings(self) -> SystemSettingsResponse:
        payload = await self._get_admin_payload("setting", "system")
        if payload is None:
            return await super().get_settings()
        if not payload:
            return SystemSettingsResponse()
        return SystemSettingsResponse.model_validate(_migrate_system_settings_payload(payload))

    async def update_settings(self, request: SystemSettingsRequest) -> SystemSettingsResponse:
        response = SystemSettingsResponse(**_persisted_system_settings_payload(request))
        if not await self._upsert_admin_payload(
            "setting",
            "system",
            _persisted_system_settings_payload(response),
        ):
            return await super().update_settings(request)
        await self._record_audit(
            "settings.update", "settings:system", {"default_mode": response.default_mode}
        )
        return response

    async def create_openclaw_operation(
        self,
        request: OpenClawOperationRequest,
        *,
        actor: str,
        mode: str,
    ) -> OpenClawOperationResponse:
        operation_id = f"openclaw_{uuid4().hex}"
        response = _openclaw_operation_response(
            operation_id=operation_id,
            request=request,
            actor=actor,
            mode=mode,
        )
        if not await self._upsert_admin_payload(
            "openclaw", operation_id, response.model_dump(mode="json")
        ):
            return await super().create_openclaw_operation(request, actor=actor, mode=mode)
        await self.record_audit_event(
            actor=actor,
            action="openclaw.approval_requested",
            resource=f"openclaw:{operation_id}",
            details=_openclaw_audit_details(request, mode),
        )
        return response

    async def get_openclaw_operation(self, operation_id: str) -> OpenClawOperationResponse:
        payload = await self._get_admin_payload("openclaw", operation_id)
        if payload is None:
            return await super().get_openclaw_operation(operation_id)
        if not payload:
            raise PublicAPIError(404, "not_found", "not found")
        return OpenClawOperationResponse.model_validate(payload)

    async def resolve_openclaw_operation(
        self,
        operation_id: str,
        request: OpenClawResolveRequest,
        *,
        actor: str,
    ) -> OpenClawOperationResponse:
        current = await self.get_openclaw_operation(operation_id)
        if current.status != "waiting_user_approval":
            raise PublicAPIError(
                409,
                "openclaw_already_resolved",
                "OpenClaw operation is already resolved",
            )
        status = "approved" if request.decision == "approve" else "rejected"
        updated = current.model_copy(
            update={
                "status": status,
                "requires_user_approval": False,
                "resolved_by": actor,
                "resolved_at": datetime.now(UTC),
            }
        )
        if not await self._upsert_admin_payload(
            "openclaw", operation_id, updated.model_dump(mode="json")
        ):
            return await super().resolve_openclaw_operation(operation_id, request, actor=actor)
        await self.record_audit_event(
            actor=actor,
            action=f"openclaw.{status}",
            resource=f"openclaw:{operation_id}",
            details={"approval_id": current.approval_id},
        )
        return updated

    async def execute_openclaw_operation(
        self,
        operation_id: str,
        settings: SystemSettingsResponse,
        *,
        actor: str,
    ) -> OpenClawExecutionResponse:
        current = await self.get_openclaw_operation(operation_id)
        updated, result = await _execute_openclaw_operation(
            current,
            settings,
            actor=actor,
            adapter_token_resolver=self.resolve_secret_value,
        )
        if not await self._upsert_admin_payload(
            "openclaw", operation_id, updated.model_dump(mode="json")
        ):
            return await super().execute_openclaw_operation(operation_id, settings, actor=actor)
        await self.record_audit_event(
            actor=actor,
            action="openclaw.executed",
            resource=f"openclaw:{operation_id}",
            details={"approval_id": current.approval_id, "exit_code": result.exit_code},
        )
        return _openclaw_execution_response(updated, result)

    async def create_openclaw_session(
        self,
        request: OpenClawSessionRequest,
        *,
        actor: str,
        mode: str,
        settings: SystemSettingsResponse,
    ) -> OpenClawSessionResponse:
        session_id = f"openclaw_session_{uuid4().hex}"
        response = _openclaw_session_response(
            session_id=session_id,
            request=request,
            actor=actor,
            mode=mode,
            settings=settings,
        )
        if not await self._upsert_admin_payload(
            "openclaw_session",
            session_id,
            response.model_dump(mode="json"),
        ):
            return await super().create_openclaw_session(
                request, actor=actor, mode=mode, settings=settings
            )
        await self.record_audit_event(
            actor=actor,
            action="openclaw.session_created",
            resource=f"openclaw_session:{session_id}",
            details=_openclaw_session_audit_details(response),
        )
        return response

    async def list_openclaw_sessions(self) -> tuple[OpenClawSessionResponse, ...]:
        payloads = await self._list_admin_payloads("openclaw_session")
        if payloads is None:
            return await super().list_openclaw_sessions()
        return tuple(OpenClawSessionResponse.model_validate(payload) for payload in payloads)

    async def update_openclaw_session(
        self,
        session_id: str,
        request: OpenClawSessionActionRequest,
        *,
        actor: str,
    ) -> OpenClawSessionResponse:
        payload = await self._get_admin_payload("openclaw_session", session_id)
        if payload is None:
            return await super().update_openclaw_session(session_id, request, actor=actor)
        if not payload:
            raise PublicAPIError(404, "not_found", "not found")
        current = OpenClawSessionResponse.model_validate(payload)
        updated = _updated_openclaw_session(current, request)
        if not await self._upsert_admin_payload(
            "openclaw_session",
            session_id,
            updated.model_dump(mode="json"),
        ):
            return await super().update_openclaw_session(session_id, request, actor=actor)
        await self.record_audit_event(
            actor=actor,
            action=f"openclaw.session_{request.action}",
            resource=f"openclaw_session:{session_id}",
            details=_openclaw_session_audit_details(updated, action=request.action),
        )
        return updated

    async def attach_openclaw_operation_to_session(
        self,
        session_id: str,
        operation_id: str,
        request: OpenClawOperationRequest,
        *,
        actor: str,
    ) -> OpenClawSessionResponse:
        payload = await self._get_admin_payload("openclaw_session", session_id)
        if payload is None:
            return await super().attach_openclaw_operation_to_session(
                session_id, operation_id, request, actor=actor
            )
        if not payload:
            raise PublicAPIError(404, "not_found", "not found")
        current = OpenClawSessionResponse.model_validate(payload)
        updated = _attach_openclaw_operation_to_session_payload(current, operation_id, request)
        if not await self._upsert_admin_payload(
            "openclaw_session",
            session_id,
            updated.model_dump(mode="json"),
        ):
            return await super().attach_openclaw_operation_to_session(
                session_id, operation_id, request, actor=actor
            )
        await self.record_audit_event(
            actor=actor,
            action="openclaw.session_operation_attached",
            resource=f"openclaw_session:{session_id}",
            details={"operation_id": operation_id, **_openclaw_session_audit_details(updated)},
        )
        return updated

    async def get_main_agent_config(self) -> MainAgentConfigResponse:
        payload = await self._get_admin_payload("main_agent", "default")
        if payload is None:
            return await super().get_main_agent_config()
        if not payload:
            request = MainAgentConfigRequest()
        else:
            request = MainAgentConfigRequest.model_validate(payload)
        return MainAgentConfigResponse(**request.model_dump())

    async def update_main_agent_config(
        self,
        request: MainAgentConfigRequest,
    ) -> MainAgentConfigResponse:
        request = _normalize_main_agent_config(request)
        if request.model is not None:
            await self._verify_model_availability(
                _main_agent_model_deployment(request.model),
                source="main_agent.update",
            )
        response = MainAgentConfigResponse(**request.model_dump())
        if not await self._upsert_admin_payload(
            "main_agent",
            "default",
            request.model_dump(mode="json"),
        ):
            return await super().update_main_agent_config(request)
        await self._record_audit(
            "main_agent.update",
            "main_agent:default",
            {
                "provider": request.model.provider if request.model is not None else "",
                "api_protocol": request.model.api_protocol if request.model is not None else "",
                "upstream_model": request.model.upstream_model if request.model is not None else "",
                "control_mode": request.control_mode,
                "hermes_policy": request.hermes_policy,
            },
        )
        return response

    async def list_evolution_runs(self) -> tuple[EvolutionRunResponse, ...]:
        resources = await self._list_admin_payloads("evolution")
        if resources is None:
            return await super().list_evolution_runs()
        runs = [EvolutionRunResponse.model_validate(payload) for payload in resources]
        return tuple(sorted(runs, key=lambda item: item.created_at, reverse=True))

    async def create_evolution_run(
        self,
        request: EvolutionRunRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        response = create_evolution_run_response(request, actor=actor)
        if not await self._upsert_admin_payload(
            "evolution", response.id, response.model_dump(mode="json")
        ):
            return await super().create_evolution_run(request, actor=actor)
        await self.record_audit_event(
            actor=actor,
            action="evolution.create",
            resource=f"evolution:{response.id}",
            details={
                "kind": response.kind,
                "mode": response.mode,
                "target_artifact_type": response.target_artifact_type,
            },
        )
        return response

    async def get_evolution_run(self, run_id: str) -> EvolutionRunResponse:
        payload = await self._get_admin_payload("evolution", run_id)
        if payload is None:
            return await super().get_evolution_run(run_id)
        if not payload:
            raise PublicAPIError(404, "not_found", "not found")
        return EvolutionRunResponse.model_validate(payload)

    async def plan_evolution_next_round(
        self,
        run_id: str,
        *,
        actor: str,
    ) -> EvolutionNextRoundPlanResponse:
        del actor
        current = await self.get_evolution_run(run_id)
        if current.status in {"stopped", "completed"}:
            raise PublicAPIError(409, "evolution_run_closed", "evolution run is already closed")
        if current.status == "waiting_approval":
            raise PublicAPIError(
                409,
                "evolution_run_requires_approval",
                "evolution run requires approval before planning the next round",
            )
        return plan_evolution_next_round(current)

    async def execute_evolution_next_round(
        self,
        run_id: str,
        request: EvolutionNextRoundExecutionRequest,
        *,
        actor: str,
    ) -> EvolutionNextRoundExecutionResponse:
        if self._run_repository is None:
            return await super().execute_evolution_next_round(run_id, request, actor=actor)
        current = await self.get_evolution_run(run_id)
        plan = await self.plan_evolution_next_round(run_id, actor=actor)
        conversation_id = _evolution_execution_conversation_id(run_id, plan.round)
        idempotency_key = (
            request.idempotency_key or f"evolution:{run_id}:round:{plan.round}:execute"
        )
        record = await self._run_repository.create_run(
            tenant_id=self._tenant_id,
            actor_id=_uuid_or_default(actor, self._actor_id),
            actor_role=Role.OPERATOR,
            request=plan.task_prompt,
            mode=TaskMode(current.mode),
            status=RunStatus.QUEUED,
            idempotency_key=idempotency_key,
            routing_decision=_evolution_execution_routing_decision(current, plan, conversation_id),
            enqueue=True,
        )
        await self.record_audit_event(
            actor=actor,
            action="evolution.round_execution_queued",
            resource=f"evolution:{run_id}",
            details={
                "round": plan.round,
                "action": plan.action,
                "execution_run_id": str(record.id),
                "execution_conversation_id": conversation_id,
                "idempotency_key": idempotency_key,
            },
        )
        return EvolutionNextRoundExecutionResponse(
            evolution_run_id=run_id,
            round=plan.round,
            action=plan.action,
            execution_run_id=str(record.id),
            execution_conversation_id=conversation_id,
            status=record.status.value,
            task_title=plan.task_title,
            task_prompt=plan.task_prompt,
        )

    async def ingest_evolution_execution_run(
        self,
        run_id: str,
        execution_run_id: UUID,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        if self._run_repository is None:
            return await super().ingest_evolution_execution_run(
                run_id, execution_run_id, actor=actor
            )
        current = await self.get_evolution_run(run_id)
        if current.status in {"stopped", "completed"}:
            raise PublicAPIError(409, "evolution_run_closed", "evolution run is already closed")
        if current.status == "waiting_approval":
            raise PublicAPIError(
                409,
                "evolution_run_requires_approval",
                "evolution run requires approval before ingesting execution results",
            )
        try:
            execution = await self._run_repository.get(self._tenant_id, execution_run_id)
        except RunNotFound:
            raise PublicAPIError(404, "not_found", "execution run was not found") from None
        if execution.status is not RunStatus.COMPLETED:
            raise PublicAPIError(
                409,
                "evolution_execution_not_completed",
                "execution run must be completed before it can be ingested",
                details={"status": execution.status.value},
            )
        _assert_evolution_execution_binding(
            run_id,
            execution_run_id,
            execution.routing_decision,
            expected_round=len(current.rounds) + 1,
        )
        artifacts = await self._run_repository.artifacts(self._tenant_id, execution_run_id)
        request = _evolution_round_request_from_artifacts(
            artifacts, execution_run_id=execution_run_id
        )
        updated = append_evolution_round(current, request)
        latest = updated.rounds[-1]
        if not await self._upsert_admin_payload(
            "evolution", run_id, updated.model_dump(mode="json")
        ):
            return await super().ingest_evolution_execution_run(
                run_id, execution_run_id, actor=actor
            )
        await self.record_audit_event(
            actor=actor,
            action="evolution.round_ingested",
            resource=f"evolution:{run_id}",
            details={
                "round": latest.round,
                "delta": latest.delta,
                "accepted": latest.accepted,
                "recommendation": latest.recommendation,
                "status": updated.status,
                "next_action": updated.next_action,
                "execution_run_id": str(execution_run_id),
            },
        )
        return updated

    async def approve_evolution_run(
        self,
        run_id: str,
        request: EvolutionApprovalRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        current = await self.get_evolution_run(run_id)
        updated = approve_evolution_run_response(current, request, actor=actor)
        if not await self._upsert_admin_payload(
            "evolution", run_id, updated.model_dump(mode="json")
        ):
            return await super().approve_evolution_run(run_id, request, actor=actor)
        await self.record_audit_event(
            actor=actor,
            action="evolution.approve",
            resource=f"evolution:{run_id}",
            details={
                "approval_status": updated.approval_status,
                "status": updated.status,
                "baseline_agent_id": updated.baseline_agent_id,
                "evaluator_agent_id": updated.evaluator_agent_id,
                "next_action": updated.next_action,
            },
        )
        return updated

    async def record_evolution_round(
        self,
        run_id: str,
        request: EvolutionRoundRequest,
        *,
        actor: str,
    ) -> EvolutionRunResponse:
        current = await self.get_evolution_run(run_id)
        if current.status in {"stopped", "completed"}:
            raise PublicAPIError(409, "evolution_run_closed", "evolution run is already closed")
        if current.status == "waiting_approval":
            raise PublicAPIError(
                409,
                "evolution_run_requires_approval",
                "evolution run requires approval before recording rounds",
            )
        updated = append_evolution_round(current, request)
        latest = updated.rounds[-1]
        if not await self._upsert_admin_payload(
            "evolution", run_id, updated.model_dump(mode="json")
        ):
            return await super().record_evolution_round(run_id, request, actor=actor)
        await self.record_audit_event(
            actor=actor,
            action="evolution.round_recorded",
            resource=f"evolution:{run_id}",
            details={
                "round": latest.round,
                "delta": latest.delta,
                "accepted": latest.accepted,
                "recommendation": latest.recommendation,
                "status": updated.status,
                "next_action": updated.next_action,
            },
        )
        return updated

    async def list_skills(self) -> tuple[SkillResponse, ...]:
        rows = await self._list_admin_payloads_with_metadata("skill")
        if rows is None:
            resources = await self._list_admin_payloads("skill")
            if resources is None:
                return await super().list_skills()
            records = [
                _SkillVersionRecord(
                    response=_skill_response_from_payload(payload),
                    created_at=None,
                    updated_at=None,
                    ordinal=index,
                )
                for index, payload in enumerate(resources)
            ]
        else:
            records = [
                _SkillVersionRecord(
                    response=_skill_response_with_archive_identity(
                        _skill_response_from_payload(payload, resource_id=resource_id),
                        self._skill_archive_path(resource_id),
                    ),
                    created_at=created_at,
                    updated_at=updated_at,
                    ordinal=index,
                )
                for index, (resource_id, payload, created_at, updated_at) in enumerate(rows)
            ]
        return _group_skill_records(records, await self._active_skill_versions())

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse:
        skill_id = _manual_skill_upload_id(request.filename)
        response = SkillResponse(
            id=skill_id,
            name=_skill_name_slug(PurePosixPath(request.filename).stem),
            status="quarantined",
            scan_diff=["metadata recorded; package scan requires ZIP upload endpoint"],
            requested_permissions=["filesystem:read"],
        )
        if not await self._upsert_admin_payload(
            "skill", skill_id, response.model_dump(mode="json")
        ):
            return await super().upload_skill(request)
        await self._record_audit(
            "skill.upload", f"skill:{skill_id}", {"filename": request.filename}
        )
        return response

    async def upload_skill_archive(
        self, filename: str, archive_bytes: bytes, *, strategy: str | None = None
    ) -> SkillArchiveUploadResponse:
        strategy = _skill_upload_strategy_or_error(strategy)
        try:
            bundle, scanned_archives, skipped_archives = _scan_skill_archive_upload(
                filename, archive_bytes
            )
        except InvalidSkillPackage as error:
            reason = _safe_model_check_detail(str(error))
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={
                    "feature": "skills",
                    "filename": _safe_model_check_detail(filename),
                    "reason": reason,
                },
            )
            raise PublicAPIError(
                422,
                "invalid_skill_package",
                "skill package is invalid",
                details={"reason": reason},
            ) from None
        items: list[SkillResponse] = []
        active_upload_ids: set[str] = set()
        seen_skill_ids: set[str] = set()
        skipped = list(skipped_archives)
        try:
            for scanned in scanned_archives:
                skill_id = _skill_id_from_scanned_archive(scanned)
                if skill_id in seen_skill_ids:
                    skipped.append(
                        _SkippedSkillArchive(
                            path=scanned.filename,
                            reason="duplicate skill identity skipped",
                        )
                    )
                    continue
                seen_skill_ids.add(skill_id)
                response = _skill_response_from_scanned_archive(scanned, skill_id)
                existing_versions = await self._skill_versions_by_name(response.name)
                matching_content = _matching_skill_content(
                    (record.response for record in existing_versions),
                    response.content_sha256,
                )
                if matching_content is not None:
                    items.append(matching_content)
                    continue
                if existing_versions:
                    current = _current_skill_for_name(
                        (record.response for record in existing_versions),
                        await self._active_skill_versions(),
                        response.name,
                    )
                    if strategy is None:
                        raise _skill_version_choice_error(response, current)
                    skill_id = (
                        current.id
                        if strategy == "overwrite"
                        else _skill_version_id_from_scanned_archive(scanned)
                    )
                    response = _skill_response_from_scanned_archive(scanned, skill_id)
                archive_path = self._skill_archive_path(response.id)
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_bytes(scanned.archive_bytes)
                active_upload_ids.add(response.id)
                items.append(response)
        except OSError:
            await self.record_log(
                category="feature_error",
                level="error",
                title="主要功能运行错误",
                message="skill store is unavailable",
                source="skills.upload",
                details={"feature": "skills", "filename": _safe_model_check_detail(filename)},
            )
            raise PublicAPIError(
                503, "skill_store_unavailable", "skill store is unavailable"
            ) from None
        for response in items:
            if response.id not in active_upload_ids:
                continue
            if not await self._upsert_admin_payload(
                "skill", response.id, response.model_dump(mode="json")
            ):
                return await super().upload_skill_archive(
                    filename, archive_bytes, strategy=strategy
                )
            await self._set_active_skill_version(response.name, response.id)
            await self._record_audit("skill.upload", f"skill:{response.id}", {"filename": filename})
        return SkillArchiveUploadResponse(
            filename=filename,
            bundle=bundle,
            items=items,
            skipped=_skipped_skill_responses(tuple(skipped)),
        )

    async def activate_skill_version(self, skill_id: str, version_id: str) -> SkillResponse:
        skill_payload = await self._get_admin_payload("skill", skill_id)
        version_payload = await self._get_admin_payload("skill", version_id)
        if skill_payload is None or version_payload is None:
            return await super().activate_skill_version(skill_id, version_id)
        if not skill_payload or not version_payload:
            raise KeyError(skill_id if not skill_payload else version_id)
        skill = _skill_response_from_payload(skill_payload, resource_id=skill_id)
        version = _skill_response_from_payload(version_payload, resource_id=version_id)
        if skill.name != version.name:
            raise PublicAPIError(
                409,
                "skill_version_mismatch",
                "skill version does not belong to this skill",
            )
        await self._set_active_skill_version(skill.name, version.id)
        await self._record_audit(
            "skill.version.activate",
            f"skill:{skill.id}",
            {"version_id": version.id, "skill_name": skill.name},
        )
        grouped = await self.list_skills()
        return next(item for item in grouped if item.name == skill.name)

    async def approve_skill(self, skill_id: str) -> SkillResponse:
        payload = await self._get_admin_payload("skill", skill_id)
        if payload is None:
            return await super().approve_skill(skill_id)
        if not payload:
            raise KeyError(skill_id)
        if not self._skill_archive_path(skill_id).is_file():
            raise PublicAPIError(409, "skill_archive_missing", "approved skill archive is missing")
        current = SkillResponse.model_validate(payload)
        response = SkillResponse(
            **{
                **current.model_dump(),
                "status": "enabled",
                "scan_diff": [*current.scan_diff, "approved by production admin"],
            }
        )
        await self._upsert_admin_payload("skill", skill_id, response.model_dump(mode="json"))
        await self._record_audit("skill.approve", f"skill:{skill_id}", {"id": skill_id})
        return response

    async def delete_skill(self, skill_id: str) -> None:
        deleted = await self._delete_admin_payload("skill", skill_id)
        if deleted is None:
            await super().delete_skill(skill_id)
            return
        if not deleted:
            raise KeyError(skill_id)
        try:
            self._skill_archive_path(skill_id).unlink(missing_ok=True)
        except OSError:
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill archive could not be removed",
                source="skills.delete",
                details={"feature": "skills", "skill_id": skill_id},
            )
        await self._record_audit("skill.delete", f"skill:{skill_id}", {"id": skill_id})

    async def list_plugins(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginResourceResponse, ...]:
        resources = await self._list_admin_payloads("plugin", tenant_id=tenant_id)
        if resources is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                return ()
            return await super().list_plugins()
        signing_keys = await self.list_plugin_signing_keys(tenant_id=tenant_id)
        now = datetime.now(UTC)
        return tuple(
            _plugin_with_effective_package_trust(
                PluginResourceResponse.model_validate(payload),
                signing_keys,
                now,
            )
            for payload in resources
        )

    async def list_plugin_signing_keys(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[PluginSigningKeyResponse, ...]:
        resources = await self._list_admin_payloads("plugin_signing_key", tenant_id=tenant_id)
        if resources is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                return ()
            return await super().list_plugin_signing_keys(tenant_id=tenant_id)
        return tuple(PluginSigningKeyResponse.model_validate(payload) for payload in resources)

    async def upsert_plugin_signing_key(
        self,
        request: PluginSigningKeyRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginSigningKeyResponse:
        response = PluginSigningKeyResponse(**request.model_dump(), trusted=True)
        if not await self._upsert_admin_payload(
            "plugin_signing_key",
            response.key_id,
            response.model_dump(mode="json"),
            tenant_id=tenant_id,
        ):
            if tenant_id is not None and tenant_id != self._tenant_id:
                raise KeyError(response.key_id)
            return await super().upsert_plugin_signing_key(
                request,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
        await self._record_audit(
            "plugin.signing_key.upsert",
            f"plugin_signing_key:{response.key_id}",
            {
                "key_id": response.key_id,
                "algorithm": response.algorithm,
                "trusted": response.trusted,
            },
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        return response

    async def delete_plugin_signing_key(
        self,
        key_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        current = await self._get_admin_payload(
            "plugin_signing_key",
            key_id,
            tenant_id=tenant_id,
        )
        deleted = await self._delete_admin_payload(
            "plugin_signing_key",
            key_id,
            tenant_id=tenant_id,
        )
        if deleted is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                raise KeyError(key_id)
            await super().delete_plugin_signing_key(
                key_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
            return
        if not deleted:
            raise KeyError(key_id)
        key = PluginSigningKeyResponse.model_validate(current or {"key_id": key_id})
        await self._record_audit(
            "plugin.signing_key.delete",
            f"plugin_signing_key:{key_id}",
            {
                "key_id": key.key_id,
                "algorithm": key.algorithm,
                "trusted": False,
            },
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

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
        current = await self._plugin_response(request.id, tenant_id=tenant_id)
        response = _plugin_response_from_request(
            request,
            current=current,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
        )
        if not await self._upsert_admin_payload(
            "plugin",
            response.id,
            response.model_dump(mode="json"),
            tenant_id=tenant_id,
        ):
            if tenant_id is not None and tenant_id != self._tenant_id:
                raise KeyError(response.id)
            return await super().upsert_plugin(
                request,
                source_filename=source_filename,
                content_sha256=content_sha256,
                package_metadata=package_metadata,
            )
        await self._record_audit(
            "plugin.upsert",
            f"plugin:{response.id}",
            _plugin_upsert_audit_details(response),
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        return response

    async def start_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_lifecycle(
            plugin_id,
            "start",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def enable_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_lifecycle(
            plugin_id,
            "enable",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def disable_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_lifecycle(
            plugin_id,
            "disable",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def stop_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_lifecycle(
            plugin_id,
            "stop",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def reload_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_lifecycle(
            plugin_id,
            "reload",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def approve_plugin_package(
        self,
        plugin_id: str,
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_package_approval(
            plugin_id,
            "approved",
            request,
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def reject_plugin_package(
        self,
        plugin_id: str,
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        return await self._set_plugin_package_approval(
            plugin_id,
            "rejected",
            request,
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def delete_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        await self._delete_plugin_resource(
            plugin_id,
            action="plugin.delete",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        await self._delete_plugin_resource(
            plugin_id,
            action="plugin.uninstall",
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def _delete_plugin_resource(
        self,
        plugin_id: str,
        *,
        action: str,
        tenant_id: UUID | None,
        actor_id: UUID | None,
    ) -> None:
        deleted = await self._delete_admin_payload("plugin", plugin_id, tenant_id=tenant_id)
        if deleted is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                raise KeyError(plugin_id)
            await super().delete_plugin(plugin_id)
            return
        if not deleted:
            raise KeyError(plugin_id)
        await self._record_audit(
            action,
            f"plugin:{plugin_id}",
            {"id": plugin_id},
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    async def _plugin_response(
        self,
        plugin_id: str,
        *,
        tenant_id: UUID | None = None,
    ) -> PluginResourceResponse | None:
        payload = await self._get_admin_payload("plugin", plugin_id, tenant_id=tenant_id)
        if payload is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                return None
            for plugin in await super().list_plugins():
                if plugin.id == plugin_id:
                    return plugin
            return None
        if not payload:
            return None
        return PluginResourceResponse.model_validate(payload)

    async def _set_plugin_lifecycle(
        self,
        plugin_id: str,
        action: str,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        payload = await self._get_admin_payload("plugin", plugin_id, tenant_id=tenant_id)
        if payload is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                raise KeyError(plugin_id)
            if action == "start":
                return await super().start_plugin(plugin_id)
            if action == "enable":
                return await super().enable_plugin(plugin_id)
            if action == "disable":
                return await super().disable_plugin(plugin_id)
            if action == "stop":
                return await super().stop_plugin(plugin_id)
            return await super().reload_plugin(plugin_id)
        if not payload:
            raise KeyError(plugin_id)
        current = PluginResourceResponse.model_validate(payload)
        if action == "enable":
            updated = _plugin_enabled_response(current)
        elif action == "disable":
            updated = _plugin_disabled_response(current)
        elif action == "stop":
            updated = _plugin_stopped_response(current)
        else:
            updated = _plugin_started_response(current)
        if not await self._upsert_admin_payload(
            "plugin",
            updated.id,
            updated.model_dump(mode="json"),
            tenant_id=tenant_id,
        ):
            raise KeyError(plugin_id)
        await self._record_audit(
            f"plugin.{action}",
            f"plugin:{updated.id}",
            {"id": updated.id},
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        return updated

    async def _set_plugin_package_approval(
        self,
        plugin_id: str,
        approval_state: Literal["approved", "rejected"],
        request: PluginPackageApprovalRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PluginResourceResponse:
        payload = await self._get_admin_payload("plugin", plugin_id, tenant_id=tenant_id)
        if payload is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                raise KeyError(plugin_id)
            if approval_state == "approved":
                return await super().approve_plugin_package(
                    plugin_id,
                    request,
                    actor_id=actor_id,
                )
            return await super().reject_plugin_package(
                plugin_id,
                request,
                actor_id=actor_id,
            )
        if not payload:
            raise KeyError(plugin_id)
        current = PluginResourceResponse.model_validate(payload)
        updated = _plugin_with_package_approval(
            current,
            approval_state,
            request,
            actor_id=actor_id,
        )
        if not await self._upsert_admin_payload(
            "plugin",
            updated.id,
            updated.model_dump(mode="json"),
            tenant_id=tenant_id,
        ):
            raise KeyError(plugin_id)
        await self._record_audit(
            f"plugin.package.{approval_state}",
            f"plugin:{updated.id}",
            {
                "id": updated.id,
                "approval_state": approval_state,
                "activation_state": (
                    updated.package_metadata.activation_state
                    if updated.package_metadata is not None
                    else "not_applicable"
                ),
                "approval_reason": (
                    updated.package_metadata.approval_reason
                    if updated.package_metadata is not None
                    else ""
                ),
            },
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        return updated

    def _skill_archive_path(self, skill_id: str) -> Path:
        if not _is_safe_admin_identifier(skill_id):
            raise PublicAPIError(422, "request_validation", "invalid skill id")
        root = self._skill_store_dir.resolve()
        target = (root / str(self._tenant_id) / f"{skill_id}.zip").resolve()
        if not target.is_relative_to(root):
            raise PublicAPIError(422, "request_validation", "invalid skill id")
        return target

    async def list_mcp_servers(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> tuple[McpServerResponse, ...]:
        resources = await self._list_admin_payloads("mcp", tenant_id=tenant_id)
        if resources is None:
            if tenant_id is not None and tenant_id != self._tenant_id:
                return ()
            return await super().list_mcp_servers()
        return tuple(McpServerResponse.model_validate(payload) for payload in resources)

    async def upsert_mcp_server(self, request: McpServerRequest) -> McpServerResponse:
        response = McpServerResponse(
            id=request.id,
            name=request.name,
            health="configured",
            allowed_tools=request.allowed_tools,
            transport=request.transport,
            command=request.command,
            args=request.args,
            url=request.url,
            executable_allowlist=request.executable_allowlist,
            domain_allowlist=request.domain_allowlist,
            timeout_seconds=request.timeout_seconds,
        )
        if not await self._upsert_admin_payload(
            "mcp", response.id, response.model_dump(mode="json")
        ):
            return await super().upsert_mcp_server(request)
        await self._record_audit("mcp.upsert", f"mcp:{response.id}", {"id": response.id})
        return response

    async def delete_mcp_server(self, server_id: str) -> None:
        deleted = await self._delete_admin_payload("mcp", server_id)
        if deleted is None:
            await super().delete_mcp_server(server_id)
            return
        if not deleted:
            raise KeyError(server_id)
        await self._record_audit("mcp.delete", f"mcp:{server_id}", {"id": server_id})

    async def list_channels(self) -> tuple[ChannelStatusResponse, ...]:
        config = await self._channel_config_values()
        if config is None:
            return await super().list_channels()
        return _channel_statuses_from_configuration(config)

    async def save_channel_config(
        self, channel_id: str, request: ChannelConfigRequest
    ) -> ChannelConfigSaveResponse:
        config = await self._channel_config_values()
        if config is None:
            return await super().save_channel_config(channel_id, request)
        definition = _channel_definition(channel_id)
        cleaned = _clean_channel_config_values(definition, request.values)
        if not cleaned:
            raise PublicAPIError(
                422, "request_validation", "at least one channel field is required"
            )
        existing = dict(config.get(channel_id, {}))
        existing.update(cleaned)
        config[channel_id] = existing
        payload: dict[str, object] = {"id": channel_id, "values": existing}
        if not await self._upsert_admin_payload("channel", channel_id, payload):
            return await super().save_channel_config(channel_id, request)
        await self._record_audit(
            "channel.update",
            f"channel:{channel_id}",
            {"saved": ",".join(_ordered_channel_saved_fields(definition, cleaned))},
        )
        return ChannelConfigSaveResponse(
            id=channel_id,
            saved=_ordered_channel_saved_fields(definition, cleaned),
            status=_channel_status_from_definition(definition, config),
        )

    async def clear_channel_config(
        self, channel_id: str, *, actor: str
    ) -> ChannelConfigSaveResponse:
        config = await self._channel_config_values()
        if config is None:
            return await super().clear_channel_config(channel_id, actor=actor)
        definition = _channel_definition(channel_id)
        config.pop(channel_id, None)
        deleted = await self._delete_admin_payload("channel", channel_id)
        if deleted is None:
            return await super().clear_channel_config(channel_id, actor=actor)
        await self._record_audit(
            "channel.clear",
            f"channel:{channel_id}",
            {"cleared": ",".join(definition.required_env)},
        )
        return ChannelConfigSaveResponse(
            id=channel_id,
            saved=[],
            status=_channel_status_from_definition(definition, config),
        )

    async def channel_runtime_config(self) -> dict[str, str]:
        config = await self._channel_config_values()
        if config is None:
            return await super().channel_runtime_config()
        return _flatten_channel_config(config)

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]:
        resources = await self._list_admin_payloads("memory")
        if resources is None:
            return await super().list_memory()
        return tuple(MemoryRecordResponse.model_validate(payload) for payload in resources)

    async def create_memory(self, request: MemoryCreateRequest) -> MemoryRecordResponse:
        response = MemoryRecordResponse(**request.model_dump())
        if not await self._upsert_admin_payload(
            "memory", response.id, response.model_dump(mode="json")
        ):
            return await super().create_memory(request)
        await self._record_audit("memory.upsert", f"memory:{response.id}", {"id": response.id})
        return response

    async def update_memory(
        self, memory_id: str, request: MemoryRecordRequest
    ) -> MemoryRecordResponse:
        existing = await self._get_admin_payload("memory", memory_id)
        if existing is None:
            return await super().update_memory(memory_id, request)
        if not existing:
            raise KeyError(memory_id)
        current = MemoryRecordResponse.model_validate(existing)
        response = current.model_copy(update=request.model_dump(exclude_unset=True))
        await self._upsert_admin_payload("memory", memory_id, response.model_dump(mode="json"))
        await self._record_audit("memory.update", f"memory:{memory_id}", {"id": memory_id})
        return response

    async def forget_memory(self, memory_id: str) -> None:
        deleted = await self._delete_admin_payload("memory", memory_id)
        if deleted is None:
            await super().forget_memory(memory_id)
            return
        if not deleted:
            raise KeyError(memory_id)
        await self._record_audit("memory.forget", f"memory:{memory_id}", {"id": memory_id})

    async def list_audit_events(self, action: str | None = None) -> tuple[AuditEventResponse, ...]:
        resources = await self._list_admin_payloads("audit")
        if resources is None:
            return await super().list_audit_events(action)
        events = tuple(_audit_response_from_payload(payload) for payload in resources)
        if action is not None:
            events = tuple(event for event in events if event.action == action)
        return tuple(sorted(events, key=lambda event: event.created_at, reverse=True))

    async def record_log(
        self,
        *,
        category: str,
        level: str,
        title: str,
        message: str,
        source: str,
        details: dict[str, str] | None = None,
    ) -> LogEntryResponse:
        entry = self.make_log(
            category=category,
            level=level,
            title=title,
            message=message,
            source=source,
            details=details,
        )
        if entry.level == "info":
            return entry
        if not await self._upsert_admin_payload("log", entry.id, entry.model_dump(mode="json")):
            return await super().record_log(
                category=category,
                level=level,
                title=title,
                message=message,
                source=source,
                details=details,
            )
        return entry

    async def list_logs(self, category: str | None = None) -> tuple[LogEntryResponse, ...]:
        resources = await self._list_admin_payloads("log")
        if resources is None:
            return await super().list_logs(category)
        entries = [
            *(_audit_log_entry(event) for event in await self.list_audit_events()),
            *(_log_response_from_payload(payload) for payload in resources),
            *(await self._mode_error_logs_from_repository()),
            *_channel_error_logs_from_configuration(await self._channel_config_values() or {}),
        ]
        if category is not None:
            entries = [entry for entry in entries if entry.category == category]
        return tuple(sorted(entries, key=lambda entry: entry.created_at, reverse=True))

    async def list_hermes_insights(self) -> tuple[HermesInsightResponse, ...]:
        resources = await self._list_admin_payloads("hermes")
        if resources is None:
            return await super().list_hermes_insights()
        return tuple(_hermes_response_from_payload(payload) for payload in resources)

    async def get_hermes_insight(self, insight_id: str) -> HermesInsightResponse:
        payload = await self._get_admin_payload("hermes", insight_id)
        if payload is None:
            return await super().get_hermes_insight(insight_id)
        if not payload:
            raise KeyError(insight_id)
        return _hermes_response_from_payload(payload)

    async def confirm_hermes_insight(self, insight_id: str) -> HermesInsightResponse:
        payload = await self._get_admin_payload("hermes", insight_id)
        if payload is None:
            return await super().confirm_hermes_insight(insight_id)
        if not payload:
            raise KeyError(insight_id)
        payload["confirmed_at"] = datetime.now(UTC).isoformat()
        if not await self._upsert_admin_payload("hermes", insight_id, payload):
            return await super().confirm_hermes_insight(insight_id)
        await self._record_audit("hermes.confirm", f"hermes:{insight_id}", {"id": insight_id})
        return _hermes_response_from_payload(payload)

    async def delete_hermes_insight(self, insight_id: str) -> None:
        deleted = await self._delete_admin_payload("hermes", insight_id)
        if deleted is None:
            await super().delete_hermes_insight(insight_id)
            return
        if not deleted:
            raise KeyError(insight_id)
        await self._record_audit("hermes.delete", f"hermes:{insight_id}", {"id": insight_id})

    async def record_hermes_feedback(self, request: HermesFeedbackRequest) -> HermesInsightResponse:
        if _contains_sensitive_marker(request.lesson):
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="Hermes feedback contains sensitive content",
                source="hermes.feedback",
                details={"feature": "hermes", "reason": "sensitive_content"},
            )
            raise ValueError("sensitive content")
        insight_id = f"hermes_{uuid4().hex}"
        response = HermesInsightResponse(
            id=insight_id,
            category=request.category,
            outcome=request.outcome,
            lesson=request.lesson,
            summary=_hermes_feedback_summary(
                outcome=request.outcome,
                lesson=request.lesson,
                tags=request.tags,
                weight=request.weight,
            ),
            user_summary=_hermes_user_summary(
                outcome=request.outcome,
                lesson=request.lesson,
                category=request.category,
            ),
            run_id=request.run_id,
            conversation_id=request.conversation_id,
            confirmed_at=None,
            tags=request.tags,
            weight=request.weight,
            created_at=datetime.now(UTC),
        )
        if not await self._upsert_admin_payload(
            "hermes", insight_id, response.model_dump(mode="json")
        ):
            return await super().record_hermes_feedback(request)
        await self._record_audit("hermes.feedback", f"hermes:{insight_id}", {"id": insight_id})
        return response

    async def recommend_with_hermes(
        self, request: HermesRecommendationRequest
    ) -> HermesRecommendationResponse:
        if self._session_factory is None:
            return await super().recommend_with_hermes(request)
        previous = await self.list_hermes_insights()
        lowered_task = request.task.lower()
        matched = [
            insight
            for insight in previous
            if any(tag.lower() in lowered_task for tag in insight.tags)
            or any(word in lowered_task for word in insight.lesson.lower().split())
        ]
        if not matched:
            return HermesRecommendationResponse(
                recommended_mode=request.mode_candidates[0]
                if request.mode_candidates
                else "dispatch",
                recommended_model=request.model_candidates[0] if request.model_candidates else None,
                recommended_skills=request.skill_candidates[:2],
                confidence=0.35,
                reasons=["No matching Hermes lesson was found in persistent memory."],
                requires_approval=True,
            )
        best = max(matched, key=lambda insight: insight.weight)
        recommended_mode = (
            "group_chat"
            if "debate" in lowered_task or "review" in lowered_task
            else (request.mode_candidates[0] if request.mode_candidates else "dispatch")
        )
        if recommended_mode not in request.mode_candidates and request.mode_candidates:
            recommended_mode = request.mode_candidates[0]
        return HermesRecommendationResponse(
            recommended_mode=recommended_mode,
            recommended_model=request.model_candidates[0] if request.model_candidates else None,
            recommended_skills=request.skill_candidates[:2],
            confidence=min(0.95, 0.45 + best.weight / 20),
            reasons=[f"Hermes lesson matched: {best.lesson}"],
            requires_approval=True,
        )

    async def _mode_error_logs_from_repository(self) -> tuple[LogEntryResponse, ...]:
        if self._run_repository is None:
            return ()
        entries: list[LogEntryResponse] = []
        for record in await self._run_repository.list_recent(self._tenant_id, limit=100):
            if record.status is not RunStatus.FAILED:
                continue
            mode = "unknown" if record.mode is None else record.mode.value
            events = await self._run_repository.events(self._tenant_id, record.id)
            reason = _failure_reason_from_event_dicts(events)
            display_reason = _mode_error_display_reason(reason)
            details = {
                "run_id": str(record.id),
                "mode": mode,
                "status": record.status.value,
                "reason": display_reason,
            }
            if reason is None:
                details["diagnosis"] = _MODE_ERROR_REASON_NOT_RECORDED_DIAGNOSIS
            elif is_legacy_generic_failure_reason(reason):
                details["diagnosis"] = _MODE_ERROR_LEGACY_GENERIC_DIAGNOSIS
            entries.append(
                self.make_log(
                    category="mode_error",
                    level="error",
                    title="模式运行失败",
                    message=_mode_error_message(mode, display_reason),
                    source="runs.execute",
                    details=details,
                )
            )
        return tuple(entries)

    async def _list_admin_payloads(
        self,
        kind: str,
        *,
        tenant_id: UUID | None = None,
    ) -> list[dict[str, object]] | None:
        if self._session_factory is None:
            return None
        target_tenant_id = self._tenant_id if tenant_id is None else tenant_id
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == target_tenant_id)
                    .where(AdminResourceRow.kind == kind)
                    .order_by(AdminResourceRow.created_at)
                )
            ).scalars()
            return [dict(row.payload) for row in rows]

    async def _list_admin_payloads_with_metadata(
        self, kind: str
    ) -> list[tuple[str, dict[str, object], datetime | None, datetime | None]] | None:
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == self._tenant_id)
                    .where(AdminResourceRow.kind == kind)
                    .order_by(AdminResourceRow.created_at)
                )
            ).scalars()
            return [
                (row.resource_id, dict(row.payload), row.created_at, row.updated_at)
                for row in rows
            ]

    async def _active_skill_versions(self) -> dict[str, str]:
        payload = await self._get_admin_payload("setting", _SKILL_ACTIVE_VERSIONS_SETTING_ID)
        if payload is None:
            return dict(self.skill_active_versions)
        if not payload:
            return {}
        return _active_skill_versions_from_payload(payload)

    async def _set_active_skill_version(self, name: str, version_id: str) -> None:
        payload = await self._get_admin_payload("setting", _SKILL_ACTIVE_VERSIONS_SETTING_ID)
        if payload is None:
            self.skill_active_versions[name] = version_id
            return
        active_versions = _active_skill_versions_from_payload(payload)
        active_versions[name] = version_id
        await self._upsert_admin_payload(
            "setting",
            _SKILL_ACTIVE_VERSIONS_SETTING_ID,
            {"active_versions": active_versions},
        )

    async def _skill_versions_by_name(self, name: str) -> tuple[_SkillVersionRecord, ...]:
        rows = await self._list_admin_payloads_with_metadata("skill")
        if rows is None:
            return tuple(
                _SkillVersionRecord(skill, None, None, index)
                for index, skill in enumerate(self.skills.values())
                if skill.name == name
            )
        records: list[_SkillVersionRecord] = []
        for index, (resource_id, payload, created_at, updated_at) in enumerate(rows):
            response = _skill_response_with_archive_identity(
                _skill_response_from_payload(payload, resource_id=resource_id),
                self._skill_archive_path(resource_id),
            )
            if response.name == name:
                records.append(_SkillVersionRecord(response, created_at, updated_at, index))
        return tuple(records)

    async def _channel_config_values(self) -> dict[str, dict[str, str]] | None:
        resources = await self._list_admin_payloads("channel")
        if resources is None:
            return None
        config: dict[str, dict[str, str]] = {}
        for payload in resources:
            channel_id = payload.get("id")
            values = payload.get("values")
            if not isinstance(channel_id, str) or not isinstance(values, dict):
                continue
            config[channel_id] = {
                str(name): str(value)
                for name, value in values.items()
                if isinstance(name, str) and isinstance(value, str)
            }
        return config

    async def _get_admin_payload(
        self,
        kind: str,
        resource_id: str,
        *,
        tenant_id: UUID | None = None,
    ) -> dict[str, object] | None:
        if self._session_factory is None:
            return None
        target_tenant_id = self._tenant_id if tenant_id is None else tenant_id
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == target_tenant_id)
                    .where(AdminResourceRow.kind == kind)
                    .where(AdminResourceRow.resource_id == resource_id)
                )
            ).scalar_one_or_none()
            return {} if row is None else dict(row.payload)

    async def _upsert_admin_payload(
        self,
        kind: str,
        resource_id: str,
        payload: dict[str, object],
        *,
        tenant_id: UUID | None = None,
    ) -> bool:
        if self._session_factory is None:
            return False
        target_tenant_id = self._tenant_id if tenant_id is None else tenant_id
        statement = (
            insert(AdminResourceRow)
            .values(
                id=uuid4(),
                tenant_id=target_tenant_id,
                kind=kind,
                resource_id=resource_id,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=[
                    AdminResourceRow.tenant_id,
                    AdminResourceRow.kind,
                    AdminResourceRow.resource_id,
                ],
                set_={"payload": payload},
            )
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)
        return True

    async def _delete_admin_payload(
        self,
        kind: str,
        resource_id: str,
        *,
        tenant_id: UUID | None = None,
    ) -> bool | None:
        if self._session_factory is None:
            return None
        target_tenant_id = self._tenant_id if tenant_id is None else tenant_id
        existing = await self._get_admin_payload(kind, resource_id, tenant_id=target_tenant_id)
        if not existing:
            return False
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == target_tenant_id)
                .where(AdminResourceRow.kind == kind)
                .where(AdminResourceRow.resource_id == resource_id)
            )
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
        if self._session_factory is None:
            return
        target_actor_id = self._actor_id if actor_id is None else actor_id
        event = AuditEventResponse(
            id=f"audit_{uuid4().hex}",
            actor=str(target_actor_id),
            action=action,
            resource=resource,
            details=_safe_audit_details(payload),
            created_at=datetime.now(UTC),
        )
        await self._upsert_admin_payload(
            "audit",
            event.id,
            event.model_dump(mode="json"),
            tenant_id=tenant_id,
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
        event = AuditEventResponse(
            id=f"audit_{uuid4().hex}",
            actor=actor,
            action=action,
            resource=resource,
            details=_safe_audit_details(details),
            created_at=datetime.now(UTC),
        )
        await self._upsert_admin_payload(
            "audit",
            event.id,
            event.model_dump(mode="json"),
            tenant_id=tenant_id,
        )
        return event

    async def _verify_model_availability(
        self,
        deployment: Deployment,
        *,
        source: str = "models.create",
    ) -> None:
        if ModelCapability.TEXT not in deployment.capabilities:
            if deployment.capabilities.intersection(
                {
                    ModelCapability.AUDIO_GENERATION,
                    ModelCapability.IMAGE_GENERATION,
                    ModelCapability.VIDEO_GENERATION,
                }
            ):
                return
            reason = "model availability check requires text capability"
            details = await self._record_model_availability_failure(
                deployment,
                reason,
                source=source,
            )
            raise PublicAPIError(
                422,
                "model_unavailable",
                reason,
                details=details,
            )
        try:
            api_key = await self._secret_service.resolve(self._tenant_id, deployment.secret_ref)
            await self._model_transport.complete(
                deployment,
                ModelRequest(
                    logical_model=deployment.logical_model,
                    messages=(
                        ModelMessage(
                            role="user",
                            content="Reply with the exact text: agent-hub-model-check-ok",
                        ),
                    ),
                    required_capabilities=frozenset({ModelCapability.TEXT}),
                    timeout_seconds=30,
                    allow_fallback=False,
                    max_output_tokens=32,
                ),
                api_key,
            )
        except PublicAPIError as error:
            details = await self._record_model_availability_failure(
                deployment,
                error.public_message,
                status_code=str(error.status_code),
                source=source,
            )
            raise PublicAPIError(
                error.status_code,
                error.code,
                error.public_message,
                details=details if error.details is None else error.details,
                headers=error.headers,
            ) from None
        except Exception as error:  # noqa: BLE001 - redact provider/SDK failures.
            reason = _safe_model_check_reason(error)
            details = _model_check_error_details(deployment, error, reason)
            await self._record_model_availability_failure(
                deployment,
                reason,
                status_code=details["status_code"],
                source=source,
            )
            raise PublicAPIError(
                422,
                "model_unavailable",
                f"model availability check failed: {reason}",
                details=details,
            ) from None

    async def _record_model_availability_failure(
        self,
        deployment: Deployment,
        reason: str,
        *,
        status_code: str = "unknown",
        source: str = "models.create",
    ) -> dict[str, str]:
        details = _model_check_failure_details(deployment, reason, status_code=status_code)
        _LOGGER.warning(
            "model_availability_check_failed provider=%s logical_model=%s upstream_model=%s "
            "api_base=%s status_code=%s reason=%s",
            details["provider"],
            details["logical_model"],
            details["upstream_model"],
            details["api_base"],
            details["status_code"],
            details["reason"],
        )
        await self.record_log(
            category="model_error",
            level="error",
            title="模型可用性测试失败",
            message=reason,
            source=source,
            details=details,
        )
        return details

    async def _record_model_request_failure(
        self,
        request: ModelDeploymentRequest,
        *,
        reason: str,
        stage: str,
        source: str = "models.create",
    ) -> None:
        await self.record_log(
            category="model_error",
            level="error",
            title="模型配置错误",
            message=reason,
            source=source,
            details={
                "stage": stage,
                "provider": request.provider,
                "api_base": request.api_base,
                "logical_model": request.logical_model,
                "upstream_model": request.upstream_model,
                "status_code": "unknown",
                "reason": reason,
                "hint": _MODEL_CHECK_HINT,
            },
        )

    async def _current_document(self) -> dict[str, object]:
        revision = await self._config_service.get_current(self._tenant_id)
        if revision is None:
            return {"models": {}, "agents": []}
        config = PlatformConfig.model_validate(revision.document)
        return config.model_dump(mode="json")

    def _model_response(
        self,
        logical_model: str,
        fallback_model: str | None,
        index: int,
        deployment: object,
    ) -> ModelDeploymentResponse:
        parsed = (
            PlatformConfig.model_validate(
                {"models": {logical_model: {"deployments": [deployment]}}, "agents": []}
            )
            .models[logical_model]
            .deployments[0]
        )
        response_id = uuid5(
            NAMESPACE_URL,
            ":".join(
                (
                    "agent-hub-model",
                    str(self._tenant_id),
                    logical_model,
                    str(index),
                    parsed.provider,
                    parsed.model,
                    parsed.secret_ref,
                )
            ),
        )
        return ModelDeploymentResponse(
            id=response_id,
            provider=parsed.provider,
            api_base=parsed.api_base or "http://litellm:4000/v1",
            api_protocol="anthropic_messages"
            if (parsed.api_base or "").rstrip("/").lower().endswith("/messages")
            else "openai_compatible",
            upstream_model=parsed.model,
            logical_model=logical_model,
            capabilities=sorted(parsed.capabilities),
            credential_ref=parsed.secret_ref,
            quota_scope=parsed.quota_scope_id,
            max_concurrency=parsed.max_concurrency,
            target_utilization=parsed.target_utilization,
            reserved_capacity=parsed.reserved_slots,
            rpm=parsed.rpm,
            tpm=parsed.tpm,
            queue_timeout_seconds=60,
            fallback=fallback_model,
            weight=100,
            effective_slots=_model_effective_slots(
                parsed.max_concurrency,
                parsed.target_utilization,
                parsed.reserved_slots,
            ),
            saturation_policy="queue_first_then_fallback",
        )


def _service(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
) -> AdminResourceService:
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    scope_for_principal = getattr(service, "for_principal", None)
    if callable(scope_for_principal):
        return cast(
            AdminResourceService,
            scope_for_principal(principal.tenant_id, principal.user_id),
        )
    return cast(AdminResourceService, service)


def _runtime_capability_gateway(request: Request) -> CapabilityManifestProvider:
    gateway = getattr(request.app.state, "runtime_capability_gateway", None)
    if gateway is None or not callable(getattr(gateway, "capability_manifest", None)):
        raise PublicAPIError(
            503,
            "capability_manifest_unavailable",
            "capability manifest unavailable",
        )
    return cast(CapabilityManifestProvider, gateway)


def _multimedia_generation_executor(request: Request) -> MultimediaGenerationExecutorProtocol:
    executor = getattr(request.app.state, "multimedia_generation_executor", None)
    required_methods = ("submit", "get_job", "run_job", "generate")
    if executor is None or any(not hasattr(executor, method) for method in required_methods):
        raise PublicAPIError(
            503,
            "multimedia_generation_unavailable",
            "multimedia generation executor is unavailable",
        )
    return cast(MultimediaGenerationExecutorProtocol, executor)


def _tenant_multimedia_generation_executor(
    request: Request,
    tenant_id: UUID,
) -> MultimediaGenerationExecutorProtocol:
    executor = _multimedia_generation_executor(request)
    scope_for_tenant = getattr(executor, "for_tenant", None)
    if callable(scope_for_tenant):
        return cast(MultimediaGenerationExecutorProtocol, scope_for_tenant(tenant_id))
    return executor


def _scheduler_service(request: Request) -> SchedulerServiceProtocol:
    service = getattr(request.app.state, "schedule_service", None)
    required_methods = (
        "add_schedule",
        "create_schedule",
        "delete_schedule",
        "list_schedules",
        "tick",
    )
    if service is None or any(not hasattr(service, method) for method in required_methods):
        raise PublicAPIError(503, "scheduler_unavailable", "scheduler service is unavailable")
    return cast(SchedulerServiceProtocol, service)


def _schedule_to_payload(schedule: ScheduleDefinition) -> dict[str, object]:
    response = _schedule_response(schedule).model_dump(mode="json")
    response["tenant_id"] = str(schedule.tenant_id)
    response["owner_id"] = str(schedule.owner_id)
    response["idempotency_key"] = schedule.idempotency_key
    return response


def _schedule_from_payload(payload: Mapping[str, object]) -> ScheduleDefinition:
    kind = str(payload["kind"])
    misfire_policy = ScheduleMisfirePolicy(str(payload.get("misfire_policy", "fire_once")))
    timezone = str(payload["timezone"])
    if kind == "one_time":
        raw_run_at = payload.get("run_at")
        if not isinstance(raw_run_at, str):
            raise ValueError("one-time schedule payload requires run_at")
        spec: OneTimeScheduleSpec | CronScheduleSpec = OneTimeScheduleSpec(
            run_at=datetime.fromisoformat(raw_run_at),
            timezone=timezone,
            misfire_policy=misfire_policy,
        )
    else:
        raw_cron = payload.get("cron")
        if not isinstance(raw_cron, str):
            raise ValueError("cron schedule payload requires cron")
        spec = CronScheduleSpec(
            expression=raw_cron,
            timezone=timezone,
            misfire_policy=misfire_policy,
        )
    raw_next_fire_at = payload.get("next_fire_at")
    next_fire_at = (
        None if not isinstance(raw_next_fire_at, str) else datetime.fromisoformat(raw_next_fire_at)
    )
    raw_metadata = payload.get("metadata")
    metadata = cast(Mapping[str, object], raw_metadata) if isinstance(raw_metadata, dict) else {}
    raw_budget = payload.get("budget", 16_384)
    if type(raw_budget) is not int and not isinstance(raw_budget, str):
        raise ValueError("schedule payload budget is invalid")
    return ScheduleDefinition(
        id=UUID(str(payload["id"])),
        tenant_id=UUID(str(payload["tenant_id"])),
        owner_id=UUID(str(payload["owner_id"])),
        name=str(payload["name"]),
        message=str(payload["message"]),
        mode=TaskMode(str(payload["mode"])),
        workflow=str(payload["workflow_id"]),
        budget=int(raw_budget),
        spec=spec,
        idempotency_key=str(payload.get("idempotency_key") or payload["name"]),
        next_fire_at=next_fire_at,
        status=ScheduleStatus(str(payload.get("status", "active"))),
        user_visible=True,
        metadata=metadata,
    )


async def _restore_persisted_schedules(
    service: SchedulerServiceProtocol,
    resources: AdminResourceService,
    *,
    tenant_id: UUID,
) -> None:
    if not hasattr(resources, "_list_admin_payloads"):
        return
    payloads = await cast(Any, resources)._list_admin_payloads("schedule")
    if payloads is None:
        return
    existing_ids = {schedule.id for schedule in await service.list_schedules(tenant_id=tenant_id)}
    for payload in payloads:
        try:
            schedule = _schedule_from_payload(payload)
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("skipping invalid persisted schedule payload")
            continue
        if schedule.tenant_id != tenant_id or schedule.id in existing_ids:
            continue
        try:
            await service.add_schedule(schedule, now=datetime.now(UTC))
            existing_ids.add(schedule.id)
        except ValueError:
            _LOGGER.warning("skipping duplicate persisted schedule %s", schedule.id)


async def _persist_schedules(
    service: SchedulerServiceProtocol,
    resources: AdminResourceService,
    *,
    tenant_id: UUID,
) -> None:
    if not hasattr(resources, "_upsert_admin_payload"):
        return
    for schedule in await service.list_schedules(tenant_id=tenant_id):
        await cast(Any, resources)._upsert_admin_payload(
            "schedule",
            str(schedule.id),
            _schedule_to_payload(schedule),
        )


def _schedule_spec_from_request(
    body: ScheduleCreateRequest,
) -> OneTimeScheduleSpec | CronScheduleSpec:
    try:
        misfire_policy = ScheduleMisfirePolicy(body.misfire_policy)
        if body.kind == "one_time":
            if body.run_at is None:
                raise ValueError("one-time schedules require run_at")
            return OneTimeScheduleSpec(
                run_at=body.run_at,
                timezone=body.timezone,
                misfire_policy=misfire_policy,
            )
        if body.cron is None or not body.cron.strip():
            raise ValueError("cron schedules require cron")
        return CronScheduleSpec(
            expression=body.cron.strip(),
            timezone=body.timezone,
            misfire_policy=misfire_policy,
        )
    except ValueError as error:
        raise PublicAPIError(
            422,
            "request_validation",
            "request validation failed",
            details={"reason": str(error)},
        ) from error


def _schedule_response(schedule: ScheduleDefinition) -> ScheduleResponse:
    assert schedule.spec is not None
    run_at: datetime | None = None
    cron: str | None = None
    if isinstance(schedule.spec, OneTimeScheduleSpec):
        run_at = schedule.spec.run_at
    else:
        cron = schedule.spec.expression
    return ScheduleResponse(
        id=schedule.id,
        name=schedule.name,
        status=schedule.status.value,
        kind=schedule.schedule_kind.value,
        mode=schedule.mode.value,
        workflow_id=schedule.workflow,
        message=schedule.message,
        timezone=schedule.timezone_name,
        next_fire_at=schedule.next_fire_at,
        run_at=run_at,
        cron=cron,
        misfire_policy=schedule.misfire_policy.value,
        budget=schedule.budget,
        metadata=_safe_audit_details(schedule.metadata),
    )


def _multimedia_job_response(job: MultimediaGenerationJob) -> MultimediaGenerationJobResponse:
    return MultimediaGenerationJobResponse(
        id=job.id,
        kind=job.kind.value,
        logical_model=job.logical_model,
        prompt=job.prompt,
        status=job.status.value,
        artifacts=[_multimedia_artifact_response(artifact) for artifact in job.artifacts],
        executor_id=job.executor_id,
        error=job.error,
    )


def _multimedia_artifact_response(artifact: MultimediaArtifact) -> MultimediaArtifactResponse:
    return MultimediaArtifactResponse(
        kind=artifact.kind.value,
        uri=artifact.uri,
        text=artifact.text,
    )


def _deployment_document_from_request(request: ModelDeploymentRequest) -> dict[str, object]:
    return {
        "provider": request.provider,
        "model": request.upstream_model,
        "api_base": request.api_base,
        "credential_ref": request.credential_ref,
        "quota_scope_id": request.quota_scope,
        "max_concurrency": request.max_concurrency,
        "target_utilization": request.target_utilization,
        "reserved_slots": request.reserved_capacity,
        "rpm": request.rpm,
        "tpm": request.tpm,
        "capabilities": request.capabilities,
    }


def _require(principal: AuthenticatedPrincipal, permission: str) -> None:
    try:
        Authorizer().require(principal, permission)
    except PermissionDenied:
        raise PublicAPIError(403, "permission_denied", "permission denied") from None


def _contains_sensitive_marker(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "api_key",
            "authorization:",
            "bearer ",
            "password",
            "private output",
            "private-token",
            "secret",
            "sk-",
            "token_",
        )
    )


def _is_safe_admin_identifier(value: str) -> bool:
    return (
        1 <= len(value) <= 128
        and value[0].isalnum()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _decode_upload_filename_header(value: str | None, encoding: str | None) -> str | None:
    if value is None or encoding is None:
        return value
    if encoding.strip().lower() != "percent":
        raise PublicAPIError(422, "request_validation", "unsupported filename header encoding")
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        raise PublicAPIError(
            422, "request_validation", "invalid filename header encoding"
        ) from None


def _safe_skill_upload_filename(value: str | None) -> str:
    if value is None:
        raise PublicAPIError(422, "request_validation", "skill filename is required")
    filename = value.strip()
    lowered = filename.lower()
    if (
        not filename
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or not lowered.endswith((".zip", ".tar", ".tar.gz", ".tgz"))
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise PublicAPIError(
            422,
            "request_validation",
            "skill filename must be a safe .zip, .tar, .tar.gz, or .tgz name",
        )
    return filename


def _safe_plugin_upload_filename(value: str | None) -> str:
    if value is None:
        raise PublicAPIError(422, "request_validation", "plugin filename is required")
    filename = value.strip()
    lowered = filename.lower()
    if (
        not filename
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or not lowered.endswith((".zip", ".tar", ".tar.gz", ".tgz"))
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise PublicAPIError(
            422,
            "request_validation",
            "plugin filename must be a safe .zip, .tar, .tar.gz, or .tgz name",
        )
    return filename


def _normalize_model_request_api_base(request: ModelDeploymentRequest) -> ModelDeploymentRequest:
    normalized = _normalized_model_api_base(request.api_protocol, request.api_base)
    capabilities = [
        capability.value
        for capability in infer_model_capabilities(
            provider=request.provider,
            upstream_model=request.upstream_model,
            declared=request.capabilities,
        )
    ]
    if normalized == request.api_base and capabilities == request.capabilities:
        return request
    return request.model_copy(update={"api_base": normalized, "capabilities": capabilities})


def _normalize_main_agent_config(request: MainAgentConfigRequest) -> MainAgentConfigRequest:
    if request.model is None:
        return request
    normalized = _normalized_model_api_base(request.model.api_protocol, request.model.api_base)
    if normalized == request.model.api_base:
        return request
    return request.model_copy(
        update={"model": request.model.model_copy(update={"api_base": normalized})}
    )


def _main_agent_model_deployment(model: MainAgentModelConfig) -> Deployment:
    return Deployment(
        id="main_agent_1",
        logical_model="main_agent",
        provider_model=f"{model.provider}/{model.upstream_model}",
        request_model=model.upstream_model,
        api_base=model.api_base,
        secret_ref=model.credential_ref,
        quota_scope_id="main-agent",
        max_concurrency=model.max_concurrency,
        target_utilization=0.8,
        reserved_slots=0,
        capabilities=frozenset(ModelCapability(item) for item in model.capabilities),
    )


def _normalized_model_api_base(api_protocol: str, api_base: str) -> str:
    stripped = api_base.strip().rstrip("/")
    if api_protocol == "anthropic_messages":
        return _normalized_anthropic_messages_base(stripped)
    return _normalized_openai_compatible_base(stripped)


def _normalized_openai_compatible_base(fallback: str) -> str:
    path = urlsplit(fallback).path.rstrip("/")
    if path.lower().endswith("/chat/completions"):
        without_suffix = fallback[: -len("/chat/completions")].rstrip("/")
        return without_suffix
    if path in {"", "/"}:
        parsed_url = urlsplit(fallback)
        return urlunsplit((parsed_url.scheme, parsed_url.netloc, "/v1", "", ""))
    return fallback


def _normalized_anthropic_messages_base(fallback: str) -> str:
    parsed_url = urlsplit(fallback)
    path = parsed_url.path.rstrip("/")
    if path.lower().endswith("/messages"):
        return fallback
    if path.lower().endswith("/v1"):
        next_path = f"{path}/messages"
    elif path in {"", "/"}:
        next_path = "/v1/messages"
    else:
        return fallback
    return urlunsplit((parsed_url.scheme, parsed_url.netloc, next_path, "", ""))


def _safe_model_check_reason(error: Exception) -> str:
    message = str(error).strip()
    if not message or _contains_sensitive_marker(message):
        return "provider request failed"
    return message[:300]


def _model_check_error_details(
    deployment: Deployment,
    error: Exception,
    reason: str,
) -> dict[str, str]:
    return _model_check_failure_details(
        deployment,
        reason,
        status_code=_model_check_status_code(error) or "unknown",
    )


def _model_check_failure_details(
    deployment: Deployment,
    reason: str,
    *,
    status_code: str,
) -> dict[str, str]:
    provider, upstream_model = _deployment_provider_and_model(deployment)
    return {
        "stage": "model_availability_check",
        "provider": _safe_model_check_detail(provider),
        "api_base": _safe_model_check_detail(deployment.api_base),
        "logical_model": _safe_model_check_detail(deployment.logical_model),
        "upstream_model": _safe_model_check_detail(upstream_model),
        "status_code": _safe_model_check_detail(status_code),
        "reason": _safe_model_check_detail(reason),
        "hint": _model_check_hint(provider, deployment.api_base, status_code),
    }


def _model_check_hint(provider: str, api_base: str, status_code: str) -> str:
    normalized_provider = provider.strip().lower()
    normalized_base = api_base.strip().lower()
    if _is_dashscope_provider(normalized_provider, normalized_base) and status_code in {
        "401",
        "403",
    }:
        return _DASHSCOPE_AUTH_HINT
    if status_code in {"401", "403"} and not _is_known_official_provider(
        normalized_provider, normalized_base
    ):
        return _OPENAI_COMPATIBLE_AUTH_HINT
    if _is_dashscope_provider(normalized_provider, normalized_base):
        return (
            "DashScope/Qwen 配置请确认：API Base 为 "
            "https://dashscope.aliyuncs.com/compatible-mode/v1，上游模型名使用 qwen-max、"
            "qwen-plus 等百炼控制台可用模型，API Key 只填写 sk-... 原文。"
        )
    return _MODEL_CHECK_HINT


def _is_dashscope_provider(provider: str, api_base: str) -> bool:
    return (
        provider in {"qwen", "dashscope", "aliyun", "alibaba"}
        or "dashscope.aliyuncs.com" in api_base
    )


def _is_known_official_provider(provider: str, api_base: str) -> bool:
    return (
        (provider == "deepseek" and "api.deepseek.com" in api_base)
        or (provider == "openai" and "api.openai.com" in api_base)
        or (provider == "anthropic" and "api.anthropic.com" in api_base)
        or (provider == "moonshot" and "api.moonshot.cn" in api_base)
        or (provider == "minimax" and "api.minimax" in api_base)
    )


def _deployment_provider_and_model(deployment: Deployment) -> tuple[str, str]:
    provider, _, provider_model = deployment.provider_model.partition("/")
    upstream_model = deployment.request_model or provider_model or deployment.provider_model
    return provider or "unknown", upstream_model


def _safe_model_check_detail(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or _contains_sensitive_marker(cleaned):
        return "redacted"
    return cleaned[:300]


def _safe_log_details(details: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in details.items():
        if _contains_sensitive_marker(key):
            continue
        safe[key[:80]] = _safe_model_check_detail(str(value))
    return safe


def _safe_audit_details(details: Mapping[str, object] | None) -> dict[str, str]:
    if not details:
        return {}
    return _safe_log_details({str(key): str(value) for key, value in details.items()})


def _audit_log_entry(event: AuditEventResponse) -> LogEntryResponse:
    return LogEntryResponse(
        id=f"log_{event.id}",
        category="audit",
        level="info",
        title="审计日志",
        message=event.action,
        source="admin.audit",
        details=_safe_log_details(
            {
                **event.details,
                "actor": event.actor,
                "resource": event.resource,
                "action": event.action,
            }
        ),
        created_at=event.created_at,
    )


def _mode_error_log_from_run(run: RunDetailResponse) -> LogEntryResponse:
    reason = _failure_reason_from_run_events(run.events)
    display_reason = _mode_error_display_reason(reason)
    details = {
        "run_id": str(run.id),
        "mode": run.mode,
        "status": run.status,
        "reason": display_reason,
    }
    if reason is None:
        details["diagnosis"] = _MODE_ERROR_REASON_NOT_RECORDED_DIAGNOSIS
    elif is_legacy_generic_failure_reason(reason):
        details["diagnosis"] = _MODE_ERROR_LEGACY_GENERIC_DIAGNOSIS
    return LogEntryResponse(
        id=f"log_run_{run.id}",
        category="mode_error",
        level="error",
        title="模式运行失败",
        message=_mode_error_message(run.mode, display_reason),
        source="runs.execute",
        details=_safe_log_details(details),
        created_at=datetime.now(UTC),
    )


_MODE_ERROR_REASON_NOT_RECORDED = "failure reason was not recorded"
_MODE_ERROR_REASON_NOT_RECORDED_DIAGNOSIS = (
    "No runtime.failed, step.failed, or tool.failed event reason was recorded for this run. "
    "For older runs, check the worker/system logs; new runs preserve safe runtime errors."
)
_MODE_ERROR_LEGACY_GENERIC_DIAGNOSIS = (
    "This run only recorded a legacy generic failure reason. Rerun the task after this update "
    "to capture safe diagnostics such as HTTP status, deployment availability, and capacity state."
)
_MODEL_OUTCOME_EVENT_PRIORITY = (
    "model.completed",
    "artifact.created",
    "runtime.completed",
)
_MAX_MODEL_OUTCOME_VALUES = 16


def _model_outcome_summary_from_run_events(
    events: Iterable[RunEventResponse],
) -> ModelOutcomeSummaryResponse:
    selected = _model_outcome_events(events)
    requested_models: list[str] = []
    actual_models: list[str] = []
    attempted_models: list[str] = []
    provider_ids: list[str] = []
    fallback_used = False
    fallback_attempt_count = 0
    last_requested_model: str | None = None
    last_actual_model: str | None = None
    last_provider_id: str | None = None

    for event in selected:
        payload = event.payload
        actual_model = _model_outcome_string(payload.get("logical_model")) or _model_outcome_string(
            payload.get("model")
        )
        requested_model = _model_outcome_string(payload.get("requested_logical_model")) or actual_model
        provider_id = _model_outcome_string(payload.get("provider_id")) or _model_outcome_string(
            payload.get("provider")
        )
        if requested_model is not None:
            _append_unique_bounded(requested_models, requested_model)
            last_requested_model = requested_model
        if actual_model is not None:
            _append_unique_bounded(actual_models, actual_model)
            last_actual_model = actual_model
        for attempted_model in _model_outcome_string_list(payload.get("attempted_logical_models")):
            _append_unique_bounded(attempted_models, attempted_model)
        if provider_id is not None:
            _append_unique_bounded(provider_ids, provider_id)
            last_provider_id = provider_id
        fallback_used = fallback_used or payload.get("fallback_used") is True
        fallback_attempt_count += _model_outcome_non_negative_int(
            payload.get("fallback_attempt_count")
        )

    return ModelOutcomeSummaryResponse(
        completion_count=len(selected),
        fallback_used=fallback_used,
        fallback_attempt_count=fallback_attempt_count,
        requested_logical_models=requested_models,
        actual_logical_models=actual_models,
        attempted_logical_models=attempted_models,
        provider_ids=provider_ids,
        last_requested_logical_model=last_requested_model,
        last_logical_model=last_actual_model,
        last_provider_id=last_provider_id,
    )


def _orchestration_protocol_summary_from_run_events(
    events: Iterable[RunEventResponse],
) -> OrchestrationProtocolSummaryResponse | None:
    ordered = sorted(events, key=lambda item: item.sequence)
    latest_protocol: Mapping[str, object] | None = None
    latest_contracts: Mapping[str, object] | None = None
    step_statuses: dict[str, str] = {}
    active_steps: set[str] = set()
    contract_statuses: dict[str, Literal["blocked", "completed"]] = {}

    for event in ordered:
        if event.step_id is not None:
            if event.kind == "step.completed":
                step_statuses[event.step_id] = "completed"
            elif event.kind in {"step.failed", "runtime.failed"}:
                step_statuses[event.step_id] = "failed"
            elif event.step_id != "main_agent_plan" and (
                event.kind.startswith("step.") or event.kind.startswith("model.")
            ):
                active_steps.add(event.step_id)
        for completed_contract_id in _model_outcome_string_list(
            event.payload.get("completed_contract_ids")
        ):
            contract_statuses[completed_contract_id] = "completed"
        for blocked_contract_id in _model_outcome_string_list(
            event.payload.get("blocked_contract_ids")
        ):
            contract_statuses[blocked_contract_id] = "blocked"
        plan = event.payload.get("model_execution_plan")
        if not isinstance(plan, Mapping):
            continue
        protocol = plan.get("orchestration_protocol")
        if isinstance(protocol, Mapping):
            latest_protocol = cast(Mapping[str, object], protocol)
        contracts = plan.get("orchestration_contracts")
        if isinstance(contracts, Mapping):
            latest_contracts = cast(Mapping[str, object], contracts)

    if latest_protocol is None:
        return None
    protocol_id = _orchestration_protocol_id(latest_protocol.get("protocol"))
    if protocol_id is None:
        return None

    role_count = _orchestration_protocol_int(latest_protocol.get("role_count"))
    handoff_count = _orchestration_protocol_int(latest_protocol.get("handoff_count"))
    contract_count = _orchestration_protocol_int(latest_protocol.get("contract_count"))
    if role_count is None or handoff_count is None or contract_count is None:
        return None

    blocked_contract_count = 0
    completed_contract_count = 0
    counted_contract_ids: set[str] = set()
    for contract_status in contract_statuses.values():
        if contract_status == "blocked":
            blocked_contract_count += 1
        elif contract_status == "completed":
            completed_contract_count += 1
    counted_contract_ids.update(contract_statuses)
    if latest_contracts is not None:
        contract_items = latest_contracts.get("items")
        if isinstance(contract_items, list | tuple):
            for raw_item in contract_items:
                if not isinstance(raw_item, Mapping):
                    continue
                contract_id = _model_outcome_string(raw_item.get("contract_id"))
                if contract_id is not None and contract_id in counted_contract_ids:
                    continue
                source_step = _model_outcome_string(raw_item.get("source_step_id"))
                target_step = _model_outcome_string(raw_item.get("target_step_id"))
                blocking_statuses = set(_model_outcome_string_list(raw_item.get("blocking_statuses")))
                legacy_contract_status = _model_outcome_string(raw_item.get("status"))
                source_failed = source_step is not None and step_statuses.get(source_step) == "failed"
                target_failed = target_step is not None and step_statuses.get(target_step) == "failed"
                if source_failed or target_failed or legacy_contract_status in blocking_statuses:
                    blocked_contract_count += 1
                if target_step is not None and step_statuses.get(target_step) == "completed":
                    completed_contract_count += 1

    status: Literal["planned", "active", "blocked", "completed", "unknown"] = "planned"
    if blocked_contract_count > 0:
        status = "blocked"
    elif contract_count > 0 and completed_contract_count >= contract_count:
        status = "completed"
    elif active_steps:
        status = "active"
    elif contract_count == 0 and handoff_count == 0:
        status = "unknown"

    truncated = latest_protocol.get("truncated") is True
    if latest_contracts is not None:
        truncated = truncated or latest_contracts.get("truncated") is True
    return OrchestrationProtocolSummaryResponse(
        protocol=protocol_id,
        status=status,
        role_count=role_count,
        handoff_count=handoff_count,
        contract_count=contract_count,
        blocked_contract_count=blocked_contract_count,
        truncated=truncated,
    )


def _model_capability_negotiation_summary_from_run_events(
    events: Iterable[RunEventResponse],
) -> ModelCapabilityNegotiationSummaryResponse | None:
    latest: Mapping[str, object] | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        plan = event.payload.get("model_execution_plan")
        if not isinstance(plan, Mapping):
            continue
        negotiation = plan.get("model_capability_negotiation")
        if isinstance(negotiation, Mapping):
            latest = cast(Mapping[str, object], negotiation)

    if latest is None:
        return None
    role_count = _orchestration_protocol_int(latest.get("role_count"))
    satisfied_count = _orchestration_protocol_int(latest.get("satisfied_count"))
    missing_count = _orchestration_protocol_int(latest.get("missing_count"))
    unknown_count = _orchestration_protocol_int(latest.get("unknown_count"))
    if (
        role_count is None
        or satisfied_count is None
        or missing_count is None
        or unknown_count is None
    ):
        return None
    missing_capability_counts: dict[str, int] = {}
    raw_items = latest.get("items")
    if isinstance(raw_items, list | tuple):
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                continue
            for raw_capability in _model_outcome_string_list(
                raw_item.get("missing_capabilities")
            ):
                capability = _safe_model_capability_value(raw_capability)
                if capability is None:
                    continue
                missing_capability_counts[capability] = (
                    missing_capability_counts.get(capability, 0) + 1
                )
    return ModelCapabilityNegotiationSummaryResponse(
        role_count=role_count,
        satisfied_count=satisfied_count,
        missing_count=missing_count,
        unknown_count=unknown_count,
        missing_capability_counts=dict(sorted(missing_capability_counts.items())),
        truncated=latest.get("truncated") is True,
    )


def _capability_execution_summary_from_run_events(
    events: Iterable[RunEventResponse],
) -> CapabilityExecutionSummaryResponse | None:
    latest: Mapping[str, object] | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        plan = event.payload.get("capability_execution_plan")
        if isinstance(plan, Mapping):
            latest = cast(Mapping[str, object], plan)
            continue
        model_plan = event.payload.get("model_execution_plan")
        if isinstance(model_plan, Mapping):
            nested_plan = model_plan.get("capability_execution_plan")
            if isinstance(nested_plan, Mapping):
                latest = cast(Mapping[str, object], nested_plan)

    if latest is None:
        return None
    if latest.get("permission_boundary") != "runtime_capability_gateway":
        return None
    role_count = 0
    capability_count = 0
    assignments = latest.get("role_capability_assignments")
    if isinstance(assignments, list | tuple):
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                continue
            role_count += 1
            capabilities = assignment.get("capabilities")
            if not isinstance(capabilities, list | tuple):
                continue
            for capability in capabilities:
                if not isinstance(capability, Mapping):
                    continue
                capability_count += 1

    inventory_count = 0
    inventory_truncated = False
    inventory = latest.get("capability_inventory")
    if isinstance(inventory, Mapping):
        inventory_items = inventory.get("items")
        if isinstance(inventory_items, list | tuple):
            inventory_count = len(inventory_items)
        inventory_truncated = inventory.get("truncated") is True

    if role_count == 0 and capability_count == 0 and inventory_count == 0:
        return None
    return CapabilityExecutionSummaryResponse(
        permission_boundary="runtime_capability_gateway",
        role_count=role_count,
        capability_count=capability_count,
        inventory_count=inventory_count,
        truncated=latest.get("truncated") is True or inventory_truncated,
    )


def _runtime_recovery_summary_from_run_events(
    events: Iterable[RunEventResponse],
) -> RuntimeRecoverySummaryResponse | None:
    recovered_events = [
        event
        for event in sorted(events, key=lambda item: item.sequence)
        if event.kind == "runtime.recovered"
    ]
    if not recovered_events:
        return None
    latest = recovered_events[-1]
    payload = latest.payload
    completed_steps = _orchestration_protocol_int(payload.get("completed_steps"))
    total_steps = _orchestration_protocol_int(payload.get("total_steps"))
    review_artifacts = _orchestration_protocol_int(payload.get("review_artifacts"))
    if (
        completed_steps is None
        or total_steps is None
        or review_artifacts is None
        or completed_steps > total_steps
    ):
        return None
    return RuntimeRecoverySummaryResponse(
        recovery_count=len(recovered_events),
        last_completed_steps=completed_steps,
        last_total_steps=total_steps,
        model_status_counts=_runtime_recovery_status_counts(
            payload.get("model_status_counts")
        ),
        tool_status_counts=_runtime_recovery_status_counts(
            payload.get("tool_status_counts")
        ),
        review_artifacts=review_artifacts,
    )


def _runtime_recovery_status_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        if len(counts) >= 8:
            break
        key = _model_outcome_string(raw_key)
        if key is None or type(raw_count) is not int or raw_count < 0:
            continue
        counts[key] = raw_count
    return counts


def _orchestration_protocol_id(value: object) -> str | None:
    protocol = _model_outcome_string(value)
    return protocol if protocol == "role_handoff_contract_v1" else None


def _safe_model_capability_value(value: object) -> str | None:
    text = _model_outcome_string(value)
    if text is None:
        return None
    try:
        return ModelCapability(text).value
    except ValueError:
        return None


def _orchestration_protocol_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _model_outcome_events(events: Iterable[RunEventResponse]) -> list[RunEventResponse]:
    ordered = sorted(events, key=lambda item: item.sequence)
    for kind in _MODEL_OUTCOME_EVENT_PRIORITY:
        selected = [
            event
            for event in ordered
            if event.kind == kind
            and (
                _model_outcome_string(event.payload.get("logical_model")) is not None
                or _model_outcome_string(event.payload.get("model")) is not None
            )
        ]
        if selected:
            return selected
    return []


def _model_outcome_string(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    safe = _safe_event_detail(value)
    if type(safe) is str and safe and safe != "[redacted]":
        return safe[:128]
    return None


def _model_outcome_string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    values: list[str] = []
    for item in value:
        text = _model_outcome_string(item)
        if text is not None:
            _append_unique_bounded(values, text)
    return tuple(values)


def _model_outcome_non_negative_int(value: object) -> int:
    if type(value) is int and value > 0:
        return value
    return 0


def _append_unique_bounded(values: list[str], value: str) -> None:
    if value not in values and len(values) < _MAX_MODEL_OUTCOME_VALUES:
        values.append(value)


def _mode_error_display_reason(reason: str | None) -> str:
    return reason if reason else _MODE_ERROR_REASON_NOT_RECORDED


def _mode_error_message(mode: str, reason: str) -> str:
    return f"{mode} run failed: {reason}"


def _failure_reason_from_run_events(events: Iterable[RunEventResponse]) -> str | None:
    fallback: str | None = None
    for event in sorted(events, key=lambda item: item.sequence, reverse=True):
        if event.kind in {"runtime.failed", "step.failed", "tool.failed"} and event.message:
            reason = _safe_model_check_detail(event.message)
            if fallback is None:
                fallback = reason
            if not is_legacy_generic_failure_reason(reason):
                return reason
    return fallback


_RUN_DEBUG_MAX_EVENTS = 50
_RUN_DEBUG_PREVIEW_CHARS = 2000


def _run_debug_from_detail(run: RunDetailResponse) -> RunDebugResponse:
    sorted_events = sorted(run.events, key=lambda item: item.sequence)
    failure_event = _failure_event_from_run_events(sorted_events)
    failure_reason = (
        _safe_model_check_detail(failure_event.message)
        if failure_event is not None
        else _mode_error_display_reason(_failure_reason_from_run_events(sorted_events))
    )
    artifacts = [_run_debug_artifact(artifact) for artifact in run.artifacts]
    return RunDebugResponse(
        run_id=run.id,
        status=run.status,
        mode=run.mode,
        failed_stage=failure_event.kind if failure_event is not None else None,
        failure_reason=failure_reason,
        partial_output_available=any(artifact.has_text for artifact in artifacts),
        request_preview=_safe_debug_preview(run.request, max_chars=1000) or "",
        events=[_safe_debug_event(event) for event in sorted_events[-_RUN_DEBUG_MAX_EVENTS:]],
        artifacts=artifacts,
        explicit_details=dict(run.explicit_details),
        recommendation=_run_debug_recommendation(failure_reason, run.status),
        generated_at=datetime.now(UTC),
    )


def _failure_event_from_run_events(events: Iterable[RunEventResponse]) -> RunEventResponse | None:
    fallback: RunEventResponse | None = None
    for event in sorted(events, key=lambda item: item.sequence, reverse=True):
        if event.kind not in {"runtime.failed", "step.failed", "tool.failed"} or not event.message:
            continue
        safe_event = event.model_copy(update={"message": _safe_model_check_detail(event.message)})
        if fallback is None:
            fallback = safe_event
        if not is_legacy_generic_failure_reason(safe_event.message):
            return safe_event
    return fallback


def _failure_diagnostics_from_run_events(
    events: Iterable[RunEventResponse],
) -> list[FailureDiagnosticResponse]:
    sorted_events = sorted(events, key=lambda item: item.sequence)
    diagnostics: list[FailureDiagnosticResponse] = []
    seen: set[tuple[object, ...]] = set()
    for event in sorted_events:
        if event.kind == "tool.failed":
            _append_failure_diagnostic(
                diagnostics,
                seen,
                _tool_failure_diagnostic(event, sorted_events),
            )
            continue
        if event.kind not in {"runtime.failed", "step.failed"}:
            continue
        if _wrapped_tool_failure_event(event, sorted_events) is not None:
            continue
        if _secondary_generic_runtime_failure(event, diagnostics):
            continue
        _append_failure_diagnostic(
            diagnostics,
            seen,
            _runtime_or_step_failure_diagnostic(event),
        )
    for diagnostic in _pending_approval_diagnostics(sorted_events):
        _append_failure_diagnostic(diagnostics, seen, diagnostic)
    return diagnostics


def _append_failure_diagnostic(
    diagnostics: list[FailureDiagnosticResponse],
    seen: set[tuple[object, ...]],
    diagnostic: FailureDiagnosticResponse,
) -> None:
    key = (
        diagnostic.category,
        diagnostic.stage,
        diagnostic.sequence,
        diagnostic.actor,
        diagnostic.step_id,
        diagnostic.tool_name,
        diagnostic.approval_id,
    )
    if key in seen:
        return
    seen.add(key)
    diagnostics.append(diagnostic)


def _tool_failure_diagnostic(
    event: RunEventResponse,
    events: list[RunEventResponse],
) -> FailureDiagnosticResponse:
    failure_kind = _safe_diagnostic_payload_value(event, "failure_kind")
    exit_code = _safe_diagnostic_payload_value(event, "exit_code")
    output_bytes = _safe_diagnostic_payload_value(event, "output_bytes")
    reason_parts = [
        f"failure_kind={failure_kind}" if failure_kind else "tool_failed",
        f"exit_code={exit_code}" if exit_code else "",
        f"output_bytes={output_bytes}" if output_bytes else "",
    ]
    wrapper = _tool_failure_wrapper(event, events)
    error_code = _tool_failure_error_code(failure_kind)
    return FailureDiagnosticResponse(
        category="tool",
        stage=event.kind,
        reason="; ".join(part for part in reason_parts if part),
        recommendation=_tool_failure_recommendation(failure_kind),
        sequence=event.sequence,
        actor=event.actor,
        step_id=event.step_id,
        tool_name=event.tool_name,
        tool_call_id=event.tool_call_id,
        failure_kind=failure_kind,
        error_stage="capability",
        error_category=failure_kind or "tool_failed",
        error_code=error_code,
        retryable=error_code != "capability.outcome_uncertain",
        wrapped_by=wrapper.sequence if wrapper is not None else None,
    )


def _secondary_generic_runtime_failure(
    event: RunEventResponse,
    diagnostics: list[FailureDiagnosticResponse],
) -> bool:
    runtime_diagnostic = runtime_failure_diagnostic_from_reason(event.message)
    if (
        event.kind != "runtime.failed"
        or not diagnostics
        or runtime_diagnostic.get("error_code") != "runtime.failed"
    ):
        return False
    previous = diagnostics[-1]
    if event.actor and previous.actor and event.actor != previous.actor:
        return False
    return not (event.step_id and previous.step_id and event.step_id != previous.step_id)


def _tool_failure_error_code(failure_kind: str | None) -> str:
    if failure_kind == "waiting_approval":
        return "capability.waiting_approval"
    if failure_kind == "invalid_request":
        return "capability.invalid_request"
    if failure_kind == "uncertain":
        return "capability.outcome_uncertain"
    if failure_kind == "capability_failed":
        return "capability.execution_failed"
    return "capability.execution_failed"


def _tool_failure_recommendation(failure_kind: str | None) -> str:
    if failure_kind == "waiting_approval":
        return "等待用户审批或拒绝该工具动作，再继续运行。"
    if failure_kind == "invalid_request":
        return "工具请求参数不合法；修正参数、权限或文件范围后重试。"
    if failure_kind == "uncertain":
        return "工具结果状态不确定；先核验副作用和已有产物，再决定是否重试。"
    return "检查工具权限、参数和运行环境；保留已有产物，只重试最小必要步骤。"


def _runtime_or_step_failure_diagnostic(event: RunEventResponse) -> FailureDiagnosticResponse:
    runtime_diagnostic = runtime_failure_diagnostic_from_reason(event.message)
    runtime_error_code = _safe_diagnostic_text(runtime_diagnostic.get("error_code"))
    prefer_reason_diagnostic = runtime_error_code == "model.empty_response"
    error_stage = (
        _safe_diagnostic_text(runtime_diagnostic.get("error_stage"))
        if prefer_reason_diagnostic
        else _safe_diagnostic_payload_value(event, "error_stage")
        or _safe_diagnostic_text(runtime_diagnostic.get("error_stage"))
    )
    error_category = (
        _safe_diagnostic_text(runtime_diagnostic.get("error_category"))
        if prefer_reason_diagnostic
        else _safe_diagnostic_payload_value(event, "error_category")
        or _safe_diagnostic_text(runtime_diagnostic.get("error_category"))
    )
    error_code = (
        runtime_error_code
        if prefer_reason_diagnostic
        else _safe_diagnostic_payload_value(event, "error_code") or runtime_error_code
    )
    retryable = None if prefer_reason_diagnostic else _bool_diagnostic_payload_value(event, "retryable")
    if retryable is None and type(runtime_diagnostic.get("retryable")) is bool:
        retryable = bool(runtime_diagnostic["retryable"])
    category = "model" if _is_model_failure_event(event) or error_code == "model.empty_response" else "runtime"
    suggested_action = (
        _safe_diagnostic_text(runtime_diagnostic.get("suggested_action"))
        if prefer_reason_diagnostic
        else _safe_diagnostic_payload_value(event, "suggested_action")
        or _safe_diagnostic_text(runtime_diagnostic.get("suggested_action"))
    )
    status_code = _failure_status_code(event)
    failure_kind = _safe_diagnostic_payload_value(event, "failure_kind") or error_category
    logical_model = _event_logical_model(event)
    reason = _safe_model_check_detail(event.message)
    if reason == "redacted":
        reason = "runtime failure was redacted"
    return FailureDiagnosticResponse(
        category=category,
        stage=event.kind,
        reason=reason,
        recommendation=suggested_action
        or (
            "Check model config, API key, upstream status code, and rate limits; retry or switch model."
            if category == "model"
            else "Inspect the failed stage, keep existing artifacts, and retry the smallest safe scope."
        ),
        sequence=event.sequence,
        actor=event.actor,
        step_id=event.step_id,
        failure_kind=failure_kind,
        status_code=status_code,
        logical_model=logical_model,
        error_stage=error_stage,
        error_category=error_category,
        error_code=error_code,
        retryable=retryable,
    )


def _pending_approval_diagnostics(
    events: list[RunEventResponse],
) -> list[FailureDiagnosticResponse]:
    pending: list[RunEventResponse] = []
    diagnostics: list[FailureDiagnosticResponse] = []
    for event in events:
        if event.kind == "approval.resolved" and event.approval_id is not None:
            for index in range(len(pending) - 1, -1, -1):
                if pending[index].approval_id == event.approval_id:
                    pending.pop(index)
                    break
            continue
        if event.kind == "approval.requested":
            pending.append(event)
    for event in pending:
        action = _safe_diagnostic_identifier(event.action)
        diagnostics.append(
            FailureDiagnosticResponse(
                category="approval",
                stage=event.kind,
                reason=action or "approval_required",
                recommendation="Approve or reject the pending action before the run can continue.",
                sequence=event.sequence,
                actor=event.actor,
                approval_id=event.approval_id,
                action=action,
            )
        )
    return diagnostics


def _wrapped_tool_failure_event(
    event: RunEventResponse,
    events: list[RunEventResponse],
) -> RunEventResponse | None:
    if event.kind not in {"runtime.failed", "step.failed"}:
        return None
    for candidate in reversed([item for item in events if item.sequence < event.sequence]):
        if candidate.kind == "tool.failed" and _tool_failure_wraps_event(candidate, event, events):
            return candidate
    return None


def _tool_failure_wrapper(
    tool_event: RunEventResponse,
    events: list[RunEventResponse],
) -> RunEventResponse | None:
    for event in events:
        if event.sequence <= tool_event.sequence or event.kind not in {"runtime.failed", "step.failed"}:
            continue
        if _tool_failure_wraps_event(tool_event, event, events):
            return event
    return None


def _tool_failure_wraps_event(
    tool_event: RunEventResponse,
    wrapper_event: RunEventResponse,
    events: list[RunEventResponse],
) -> bool:
    if _is_model_failure_event(wrapper_event):
        return False
    if tool_event.step_id and wrapper_event.step_id and tool_event.step_id == wrapper_event.step_id:
        return True
    between = [
        event
        for event in events
        if tool_event.sequence < event.sequence < wrapper_event.sequence
    ]
    return not any(_is_failure_boundary_event(event) for event in between)


def _is_failure_boundary_event(event: RunEventResponse) -> bool:
    if event.kind.endswith(".failed") or event.kind == "runtime.failed":
        return True
    return event.kind in {
        "approval.requested",
        "approval.resolved",
        "model.started",
        "step.started",
        "tool.started",
        "tool.completed",
        "artifact.created",
        "message.created",
    }


def _safe_diagnostic_payload_value(event: RunEventResponse, key: str) -> str | None:
    value = event.payload.get(key)
    return _safe_diagnostic_text(value)


def _safe_diagnostic_text(value: object) -> str | None:
    if value is None:
        return None
    if type(value) in {str, int, float, bool}:
        safe = _safe_model_check_detail(str(value))
        return None if safe == "redacted" else safe
    return None


def _bool_diagnostic_payload_value(event: RunEventResponse, key: str) -> bool | None:
    value = event.payload.get(key)
    return value if type(value) is bool else None


def _tool_lifecycle_from_run_events(events: Iterable[RunEventResponse]) -> list[ToolLifecycleResponse]:
    sorted_events = sorted(events, key=lambda item: item.sequence)
    grouped: dict[str, dict[str, object]] = {}
    for event in sorted_events:
        if event.kind.startswith("tool."):
            key = _tool_lifecycle_key(event)
            item = grouped.setdefault(
                key,
                {
                    "tool_call_id": key,
                    "tool_name": event.tool_name or _safe_diagnostic_payload_value(event, "name") or "tool",
                    "status": "started",
                    "operation_kind": "generic",
                    "actor": None,
                    "step_id": None,
                    "started_sequence": None,
                    "terminal_sequence": None,
                    "sequences": [],
                    "approval_id": None,
                    "replay_safe": None,
                    "argument_bytes": None,
                    "output_bytes": None,
                    "exit_code": None,
                    "artifact_id": None,
                    "failure_kind": None,
                },
            )
            _merge_tool_lifecycle_event(item, event)
            continue
        if event.kind.startswith("approval."):
            _merge_tool_lifecycle_approval(grouped, event)
    return [ToolLifecycleResponse.model_validate(item) for item in grouped.values()]


def _tool_lifecycle_key(event: RunEventResponse) -> str:
    return (
        event.tool_call_id
        or _safe_diagnostic_payload_value(event, "tool_call_id")
        or _safe_diagnostic_payload_value(event, "id")
        or f"{event.tool_name or 'tool'}-{event.sequence}"
    )


def _merge_tool_lifecycle_event(item: dict[str, object], event: RunEventResponse) -> None:
    sequences = cast(list[int], item["sequences"])
    sequences.append(event.sequence)
    status = _safe_diagnostic_payload_value(event, "status") or event.kind.replace("tool.", "")
    item["status"] = status
    if event.kind == "tool.started" and item["started_sequence"] is None:
        item["started_sequence"] = event.sequence
    if event.kind in {"tool.completed", "tool.failed"}:
        item["terminal_sequence"] = event.sequence
    if event.tool_name:
        item["tool_name"] = event.tool_name
    if event.actor:
        item["actor"] = event.actor
    if event.step_id:
        item["step_id"] = event.step_id
    for source_key, target_key in (
        ("operation_kind", "operation_kind"),
        ("approval_id", "approval_id"),
        ("artifact_id", "artifact_id"),
        ("failure_kind", "failure_kind"),
    ):
        value = _safe_diagnostic_payload_value(event, source_key)
        if value:
            item[target_key] = value
    for source_key, target_key in (
        ("argument_bytes", "argument_bytes"),
        ("result_bytes", "output_bytes"),
        ("output_bytes", "output_bytes"),
        ("stdout_bytes", "output_bytes"),
        ("exit_code", "exit_code"),
    ):
        numeric_value = event.payload.get(source_key)
        if type(numeric_value) is int and numeric_value >= 0:
            item[target_key] = numeric_value
    replay_safe = event.payload.get("replay_safe")
    if type(replay_safe) is bool:
        item["replay_safe"] = replay_safe


def _merge_tool_lifecycle_approval(
    grouped: dict[str, dict[str, object]],
    event: RunEventResponse,
) -> None:
    approval_id = event.approval_id
    if approval_id is None:
        return
    if event.step_id is None:
        return
    for item in grouped.values():
        if item.get("step_id") != event.step_id:
            continue
        cast(list[int], item["sequences"]).append(event.sequence)
        item["approval_id"] = approval_id
        replay_safe = event.payload.get("replay_safe")
        if type(replay_safe) is bool:
            item["replay_safe"] = replay_safe


_SAFE_DIAGNOSTIC_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_diagnostic_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if _SAFE_DIAGNOSTIC_IDENTIFIER_RE.fullmatch(stripped) is None:
        return None
    safe = _safe_model_check_detail(stripped)
    return None if safe == "redacted" else safe


def _failure_status_code(event: RunEventResponse) -> str | None:
    for key in ("status_code", "http_status"):
        value = _safe_diagnostic_payload_value(event, key)
        if value and re.fullmatch(r"\d{3}", value):
            return value
    match = re.search(r"\bstatus=(?P<status>\d{3})\b", event.message, flags=re.IGNORECASE)
    return match.group("status") if match is not None else None


def _event_logical_model(event: RunEventResponse) -> str | None:
    for key in ("logical_model", "model", "upstream_model"):
        value = _safe_diagnostic_payload_value(event, key)
        if value:
            return value
    return None


def _is_model_failure_event(event: RunEventResponse) -> bool:
    text = " ".join(
        item
        for item in (
            event.message,
            _safe_diagnostic_payload_value(event, "failure_kind") or "",
            _safe_diagnostic_payload_value(event, "provider") or "",
            _event_logical_model(event) or "",
        )
        if item
    ).lower()
    return any(
        token in text
        for token in ("model", "gateway", "transport", "provider", "litellm")
    ) or (_failure_status_code(event) is not None and _event_logical_model(event) is not None)


def _safe_debug_event(event: RunEventResponse) -> RunEventResponse:
    payload = (
        _tool_event_payload(event.payload)
        if event.kind.startswith("tool.")
        else _event_payload(event.payload)
    )
    return event.model_copy(update={"payload": payload})


def _run_debug_artifact(artifact: RunArtifactResponse) -> RunDebugArtifactResponse:
    preview = _safe_debug_preview(artifact.text)
    return RunDebugArtifactResponse(
        id=artifact.id,
        kind=artifact.kind,
        title=artifact.title,
        has_text=preview is not None,
        text_preview=preview,
    )


def _safe_debug_preview(
    value: str | None, *, max_chars: int = _RUN_DEBUG_PREVIEW_CHARS
) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or _contains_sensitive_marker(stripped):
        return None
    return stripped[:max_chars]


def _run_debug_recommendation(reason: str, status: str) -> str:
    lowered = reason.lower()
    if "model gateway" in lowered or "model transport" in lowered or "status=" in lowered:
        return (
            "模型服务链路失败：检查主 Agent/子 Agent 绑定的模型、API Base、模型名、API Key、"
            "LiteLLM 服务状态、上游状态码以及并发/限流配置。"
        )
    if "agent identifier" in lowered or "safe identifier" in lowered:
        return "角色标识不合法：检查 Agent ID/角色 ID，只使用字母、数字、下划线或短横线。"
    if "tool" in lowered or "mcp" in lowered or "skill" in lowered:
        return "工具链路失败：检查 Skill/MCP/插件是否已启用、权限是否允许、参数是否完整。"
    if status == "failed":
        return "运行已失败：按事件顺序查看最后一条 failed 事件，并保留中断前产物用于复盘。"
    return "运行未处于失败状态：可查看最近事件确认当前进度。"


def _failure_reason_from_event_dicts(events: Iterable[dict[str, object]]) -> str | None:
    sorted_events = sorted(
        events,
        key=_event_sequence,
        reverse=True,
    )
    fallback: str | None = None
    for event in sorted_events:
        kind = event.get("kind")
        if kind not in {"runtime.failed", "step.failed", "tool.failed"}:
            continue
        reason = event.get("reason") or event.get("message")
        if isinstance(reason, str) and reason:
            safe_reason = _safe_model_check_detail(reason)
            if fallback is None:
                fallback = safe_reason
            if not is_legacy_generic_failure_reason(safe_reason):
                return safe_reason
    return fallback


def _event_sequence(event: dict[str, object]) -> int:
    sequence = event.get("sequence")
    return sequence if type(sequence) is int else 0


def _log_response_from_payload(payload: dict[str, object]) -> LogEntryResponse:
    details = payload.get("details")
    return LogEntryResponse(
        id=str(payload.get("id", "")),
        category=str(payload.get("category", "feature_error")),
        level=str(payload.get("level", "warning")),
        title=str(payload.get("title", "主要功能运行错误")),
        message=str(payload.get("message", "runtime error")),
        source=str(payload.get("source", "system")),
        details=_safe_log_details(
            {str(key): str(value) for key, value in details.items()}
            if isinstance(details, dict)
            else {}
        ),
        created_at=_datetime_from_json(payload.get("created_at")),
    )


def _model_check_status_code(error: Exception) -> str | None:
    if isinstance(error, ModelTransportError) and error.status_code is not None:
        return str(error.status_code)
    match = _MODEL_CHECK_STATUS_RE.search(str(error))
    if match is None:
        return None
    return match.group("status")


def _audit_response_from_payload(payload: dict[str, object]) -> AuditEventResponse:
    created_at = payload.get("created_at")
    raw_details = payload.get("details")
    return AuditEventResponse(
        id=str(payload.get("id", "")),
        actor=str(payload.get("actor", "system")),
        action=str(payload.get("action", "unknown")),
        resource=str(payload.get("resource", "unknown")),
        details=_safe_audit_details(raw_details if isinstance(raw_details, Mapping) else None),
        created_at=_datetime_from_json(created_at),
    )


def _hermes_response_from_payload(payload: dict[str, object]) -> HermesInsightResponse:
    tags = payload.get("tags")
    raw_weight = payload.get("weight", 1)
    normalized_tags = [str(tag) for tag in tags] if isinstance(tags, list) else []
    weight = raw_weight if type(raw_weight) is int else 1
    outcome = str(payload.get("outcome", "neutral"))
    raw_category = payload.get("category", "conversation")
    category = str(raw_category) if raw_category in {"conversation", "scheduler"} else "conversation"
    lesson = str(payload.get("lesson", ""))
    raw_summary = payload.get("summary")
    raw_user_summary = payload.get("user_summary")
    run_id = _uuid_from_json(payload.get("run_id"))
    raw_conversation_id = payload.get("conversation_id")
    raw_confirmed_at = payload.get("confirmed_at")
    return HermesInsightResponse(
        id=str(payload.get("id", "")),
        category=category,
        outcome=outcome,
        lesson=lesson,
        summary=raw_summary
        if isinstance(raw_summary, str) and raw_summary
        else _hermes_feedback_summary(
            outcome=outcome,
            lesson=lesson,
            tags=normalized_tags,
            weight=weight,
        ),
        user_summary=raw_user_summary
        if isinstance(raw_user_summary, str) and raw_user_summary.strip()
        else _hermes_user_summary(outcome=outcome, lesson=lesson, category=category),
        run_id=run_id,
        conversation_id=raw_conversation_id if isinstance(raw_conversation_id, str) else None,
        confirmed_at=_datetime_from_json(raw_confirmed_at) if raw_confirmed_at else None,
        tags=normalized_tags,
        weight=weight,
        created_at=_datetime_from_json(payload.get("created_at")),
    )


def _hermes_feedback_summary(
    *,
    outcome: str,
    lesson: str,
    tags: list[str],
    weight: int,
) -> str:
    label = {
        "success": "Learned success pattern",
        "failure": "Learned failure pattern",
        "neutral": "Learned neutral observation",
    }.get(outcome, "Learned observation")
    normalized_tags = ", ".join(tag for tag in tags if tag)
    tags_part = normalized_tags or "none"
    return f"{label}: {lesson.strip()} Tags: {tags_part}. Weight: {weight}."


def _hermes_user_summary(*, outcome: str, lesson: str, category: str) -> str:
    normalized = " ".join(lesson.split()).strip()
    if len(normalized) > 72:
        normalized = f"{normalized[:71]}..."
    category_label = "调度观察" if category == "scheduler" else "对话记忆"
    outcome_label = {
        "success": "有效经验",
        "failure": "风险提醒",
        "neutral": "中性观察",
    }.get(outcome, "学习记录")
    return f"{category_label}记录了一条{outcome_label}：{normalized or '暂无具体内容'}"


def _datetime_from_json(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


def _uuid_from_json(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _channel_definition(channel_id: str) -> ChannelDefinition:
    for definition in CHANNEL_DEFINITIONS:
        if definition.id == channel_id:
            return definition
    raise PublicAPIError(404, "channel_not_found", "channel not found")


def _channel_config_value(
    name: str,
    channel_id: str,
    config: Mapping[str, Mapping[str, str]],
) -> str:
    channel_value = config.get(channel_id, {}).get(name)
    if channel_value:
        return channel_value
    for values in config.values():
        shared_value = values.get(name)
        if shared_value:
            return shared_value
    return os.environ.get(name, "")


def _channel_status_from_definition(
    definition: ChannelDefinition,
    config: Mapping[str, Mapping[str, str]],
) -> ChannelStatusResponse:
    public_url = _channel_config_value("AGENT_HUB_PUBLIC_URL", definition.id, config).rstrip("/")
    required_env = _channel_required_env(definition, config)
    configured_sources = _channel_configured_sources(definition, config)
    configured = list(configured_sources)
    missing = [name for name in required_env if name not in configured]
    public_webhook_url = (
        f"{public_url}{definition.webhook_path}"
        if public_url and definition.webhook_path is not None
        else None
    )
    transports = list(definition.transports)
    if definition.id == "feishu":
        configured_transport = _feishu_transport(config)
        if configured_transport:
            transports = [configured_transport]
    status = "missing_config" if missing else "configured"
    return ChannelStatusResponse(
        id=definition.id,
        name=definition.name,
        status=status,
        transports=transports,
        webhook_path=definition.webhook_path,
        public_webhook_url=public_webhook_url,
        missing=missing,
        configured=configured,
        configured_sources=configured_sources,
        command_aliases=_channel_command_aliases(definition, config),
        notes=_channel_notes(definition, config),
    )


def _channel_configured_fields(
    definition: ChannelDefinition,
    config: Mapping[str, Mapping[str, str]],
) -> list[str]:
    return list(_channel_configured_sources(definition, config))


def _channel_configured_sources(
    definition: ChannelDefinition,
    config: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    allowed = _channel_config_allowed_names(definition)
    if definition.id == "feishu" and _feishu_transport(config) == "websocket":
        allowed = allowed - {
            "AGENT_HUB_PUBLIC_URL",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_VERIFICATION_TOKEN",
            "FEISHU_WEBHOOK_PATH",
        }
    sources: dict[str, str] = {}
    for name in sorted(allowed):
        source = _channel_config_source(name, definition.id, config)
        if source is not None:
            sources[name] = source
    return sources


def _channel_config_source(
    name: str,
    channel_id: str,
    config: Mapping[str, Mapping[str, str]],
) -> str | None:
    if config.get(channel_id, {}).get(name):
        return "saved"
    for other_channel_id, values in config.items():
        if other_channel_id == channel_id:
            continue
        if values.get(name):
            return "shared_saved"
    if os.environ.get(name, ""):
        return "environment"
    return None


def _channel_command_aliases(
    definition: ChannelDefinition,
    config: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    del definition, config
    return {}


def _channel_required_env(
    definition: ChannelDefinition,
    config: Mapping[str, Mapping[str, str]],
) -> tuple[str, ...]:
    if definition.id != "feishu":
        return definition.required_env
    transport = _feishu_transport(config)
    if transport == "websocket":
        return ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
    required = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN"]
    if transport in {"webhook", "both"}:
        required.append("AGENT_HUB_PUBLIC_URL")
    return tuple(required)


def _feishu_transport(config: Mapping[str, Mapping[str, str]]) -> str:
    raw = _channel_config_value("FEISHU_TRANSPORT", "feishu", config).strip().lower()
    if raw in {"websocket", "webhook", "both"}:
        return raw
    return "websocket"


def _feishu_app_type(config: Mapping[str, Mapping[str, str]]) -> str:
    raw = _channel_config_value("FEISHU_APP_TYPE", "feishu", config).strip().lower()
    if raw in {"bot_template", "template_bot", "template"}:
        return "bot_template"
    return "custom_app"


def _channel_notes(
    definition: ChannelDefinition,
    config: Mapping[str, Mapping[str, str]],
) -> list[str]:
    notes = list(definition.notes)
    if definition.id == "feishu":
        transport = _feishu_transport(config)
        if transport == "websocket":
            notes.append(
                "当前按飞书长连接配置：只要求 App ID 和 App Secret；不需要公网 Webhook URL。"
            )
        elif _feishu_app_type(config) == "bot_template":
            notes.append(
                "当前按机器人模板应用配置：只要求 App ID 和 App Secret；公开事件回调仍需要飞书事件订阅校验信息或其他可信接入方式。"
            )
        else:
            notes.append(
                "当前按飞书 Webhook 配置：Verification Token 和公网 URL 必填；Encrypt Key 仅在飞书开启事件加密时填写。"
            )
    return notes


def _channel_statuses_from_configuration(
    config: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[ChannelStatusResponse, ...]:
    channel_config = config or {}
    statuses: list[ChannelStatusResponse] = []
    for definition in CHANNEL_DEFINITIONS:
        statuses.append(_channel_status_from_definition(definition, channel_config))
    return tuple(statuses)


def _channel_statuses_from_environment() -> tuple[ChannelStatusResponse, ...]:
    return _channel_statuses_from_configuration({})


def _channel_config_allowed_names(definition: ChannelDefinition) -> set[str]:
    allowed = set(definition.required_env)
    if definition.id == "feishu":
        allowed.update(
            {
                "AGENT_HUB_PUBLIC_URL",
                "FEISHU_ALLOWED_TENANT_KEYS",
                "FEISHU_APP_TYPE",
                "FEISHU_BOT_OPEN_ID",
                "FEISHU_COMMAND_ALIASES",
                "FEISHU_ENCRYPT_KEY",
                "FEISHU_TIMESTAMP_TOLERANCE_SECONDS",
                "FEISHU_TRANSPORT",
                "FEISHU_VERIFICATION_TOKEN",
                "FEISHU_WEBHOOK_PATH",
            }
        )
    return allowed


def _clean_channel_config_values(
    definition: ChannelDefinition, values: Mapping[str, str]
) -> dict[str, str]:
    allowed = _channel_config_allowed_names(definition)
    cleaned: dict[str, str] = {}
    for name, raw_value in values.items():
        if name not in allowed:
            raise PublicAPIError(
                422,
                "request_validation",
                "channel field is not allowed for this channel",
                details={"field": name, "channel": definition.id},
            )
        value = raw_value.strip()
        if not value:
            continue
        if len(value) > 4096:
            raise PublicAPIError(
                422,
                "request_validation",
                "channel field value is too long",
                details={"field": name, "channel": definition.id},
            )
        cleaned[name] = value
    return cleaned


def _ordered_channel_saved_fields(
    definition: ChannelDefinition, values: Mapping[str, str]
) -> list[str]:
    ordered = [name for name in definition.required_env if name in values]
    extras = sorted(name for name in values if name not in set(definition.required_env))
    return [*ordered, *extras]


def _flatten_channel_config(config: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    for channel_id in [definition.id for definition in CHANNEL_DEFINITIONS]:
        for name, value in config.get(channel_id, {}).items():
            if value:
                flattened[name] = value
    return flattened


def _channel_error_logs_from_configuration(
    config: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[LogEntryResponse, ...]:
    entries: list[LogEntryResponse] = []
    for status in _channel_statuses_from_configuration(config or {}):
        if status.status == "configured":
            continue
        entries.append(
            LogEntryResponse(
                id=f"log_channel_{status.id}",
                category="channel_error",
                level="warning",
                title="通道连接配置错误",
                message=f"{status.name} missing configuration",
                source="channels.status",
                details=_safe_log_details(
                    {
                        "channel": status.id,
                        "missing": ",".join(status.missing),
                        "webhook_path": status.webhook_path or "",
                    }
                ),
                created_at=datetime.now(UTC),
            )
        )
    return tuple(entries)


def _channel_error_logs_from_environment() -> tuple[LogEntryResponse, ...]:
    return _channel_error_logs_from_configuration({})


def _admin_run_event(event: dict[str, object]) -> RunEventResponse:
    sequence = event.get("sequence")
    kind = event.get("kind")
    kind_text = kind if type(kind) is str else "event"
    message = event.get("reason") or event.get("message") or kind_text
    payload = event.get("payload")
    artifact = event.get("artifact")
    actor = _optional_event_string(event.get("actor"))
    action = _optional_event_action(event.get("action"))
    decision = _optional_event_string(event.get("decision"))
    if kind_text.startswith("tool."):
        safe_payload = _tool_event_payload(payload)
    elif kind_text == "runtime.recovered":
        safe_payload = _runtime_recovered_event_payload(payload)
    else:
        safe_payload = _event_payload(payload)
    safe_artifact = _admin_run_artifact(artifact) if isinstance(artifact, dict) else None
    summary = _event_source_summary(event.get("summary")) or _event_summary(
        kind_text,
        actor=actor,
        action=action,
        decision=decision,
        payload=safe_payload,
    )
    return RunEventResponse(
        sequence=sequence if type(sequence) is int else 1,
        kind=kind_text,
        message=_event_message(kind_text, message),
        created_at=_event_created_at(event.get("created_at")),
        summary=summary,
        actor=actor,
        participants=_event_string_list(event.get("participants")),
        tool_call_id=_optional_event_string(event.get("tool_call_id")),
        tool_name=_optional_event_string(event.get("tool_name")),
        step_id=_optional_event_string(event.get("step_id")),
        approval_id=_optional_event_string(event.get("approval_id")),
        action=action,
        decision=decision,
        payload=safe_payload,
        artifact=safe_artifact,
    )


def _event_created_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if type(value) is str:
        normalized = value.strip()
        if normalized:
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return datetime.now(UTC)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _event_source_summary(value: object) -> str | None:
    if type(value) is not str:
        return None
    summary = _safe_model_check_detail(value)
    return summary or None


def _event_message(kind: str, value: object) -> str:
    if kind.startswith("tool."):
        return kind
    if type(value) is not str:
        return "event recorded"
    return _safe_model_check_detail(value)


def _event_summary(
    kind: str,
    *,
    actor: str | None,
    action: str | None,
    decision: str | None,
    payload: Mapping[str, JsonValue],
) -> str:
    actor_label = actor or "agent"
    if kind == "review.completed":
        return _safe_model_check_detail(f"{actor_label} completed review")
    if kind == "approval.requested":
        return _safe_model_check_detail(f"approval requested: {action or 'approval_required'}")
    if kind == "approval.resolved":
        return _safe_model_check_detail(f"approval resolved: {decision or 'handled'}")
    if kind == "step.retrying":
        attempt = payload.get("attempt")
        suffix = f" attempt {attempt}" if type(attempt) in {str, int, float} else ""
        return _safe_model_check_detail(f"{actor_label} retrying step{suffix}")
    if kind == "runtime.failed":
        return "runtime failure was redacted"
    if kind == "step.failed":
        return "step failure was redacted"
    if kind == "artifact.created":
        return _safe_model_check_detail(f"{actor_label} produced artifact")
    if kind == "message.created":
        return _safe_model_check_detail(f"{actor_label} produced message")
    candidate = payload.get("summary")
    if type(candidate) is str:
        summary = _safe_model_check_detail(candidate)
        if summary != "redacted":
            return summary
    return _safe_model_check_detail(f"{actor_label} recorded {kind}")


_SENSITIVE_EVENT_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "credential_ref",
        "password",
        "secret",
        "secret_ref",
        "token",
        "traceback",
    }
)
_SENSITIVE_EVENT_DETAIL_KEY_PARTS = frozenset({"capacity", "lease", "quota"})
_SENSITIVE_EVENT_DETAIL_EXACT_KEYS = frozenset(
    {
        "api_base",
        "lease_id",
        "quota_scope_id",
    }
)


def _optional_event_string(value: object) -> str | None:
    return value if type(value) is str and value else None


def _optional_event_action(value: object) -> str | None:
    return _safe_diagnostic_identifier(value if type(value) is str else None)


def _event_string_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if type(item) is str and item]


def _event_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, JsonValue] = {}
    for key, item in value.items():
        key_text = str(key)
        payload[key_text] = _safe_event_detail(item, key=key_text)
    return payload


_SAFE_RUNTIME_RECOVERED_PAYLOAD_KEYS = frozenset(
    {
        "completed_steps",
        "total_steps",
        "model_status_counts",
        "tool_status_counts",
        "review_artifacts",
    }
)


def _runtime_recovered_event_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, JsonValue] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text not in _SAFE_RUNTIME_RECOVERED_PAYLOAD_KEYS:
            continue
        payload[key_text] = _safe_event_detail(item, key=key_text)
    return payload


_SAFE_TOOL_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "id",
        "name",
        "schema_version",
        "approval_id",
        "status",
        "operation_kind",
        "sandbox",
        "replay_safe",
        "argument_keys",
        "argument_key_count",
        "redacted_argument_key_count",
        "argument_bytes",
        "result_bytes",
        "output_bytes",
        "stdout_bytes",
        "stderr_bytes",
        "exit_code",
        "truncated",
        "artifact_id",
        "failure_kind",
    }
)


def _tool_event_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, JsonValue] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text not in _SAFE_TOOL_EVENT_PAYLOAD_KEYS:
            continue
        payload[key_text] = _safe_event_detail(item, key=key_text)
    return payload


def _safe_event_detail(value: object, *, key: str | None = None, depth: int = 0) -> JsonValue:
    if key is not None and _is_sensitive_event_detail_key(key):
        return "[redacted]"
    if depth >= 6:
        return "[truncated]"
    if type(value) is str and _contains_sensitive_marker(value):
        return "[redacted]"
    if value is None or type(value) in {str, int, float, bool}:
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        safe_mapping: dict[str, JsonValue] = {}
        for nested_key, nested_value in value.items():
            nested_key_text = str(nested_key)
            safe_mapping[nested_key_text] = _safe_event_detail(
                nested_value,
                key=nested_key_text,
                depth=depth + 1,
            )
        return safe_mapping
    if isinstance(value, list | tuple):
        return tuple(_safe_event_detail(item, depth=depth + 1) for item in value)
    return str(value)


def _is_sensitive_event_detail_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_EVENT_DETAIL_EXACT_KEYS:
        return True
    if any(part in _SENSITIVE_EVENT_DETAIL_KEY_PARTS for part in re.split(r"[_\-.]+", lowered)):
        return True
    return any(sensitive in lowered for sensitive in _SENSITIVE_EVENT_DETAIL_KEYS)


def _admin_run_artifact(
    artifact: dict[str, object], *, run_id: UUID | None = None
) -> RunArtifactResponse:
    artifact_id = artifact.get("id")
    artifact_type = artifact.get("type")
    producer = artifact.get("producer")
    content = artifact.get("content")
    title = producer if type(producer) is str and producer else artifact_id
    text = _artifact_text(content)
    file_metadata = _artifact_file_metadata(artifact, run_id=run_id)
    filename = file_metadata.get("filename")
    mime_type = file_metadata.get("mime_type")
    size_bytes = file_metadata.get("size_bytes")
    sha256 = file_metadata.get("sha256")
    download_url = file_metadata.get("download_url")
    presentation = _artifact_presentation(artifact, file_metadata=file_metadata)
    return RunArtifactResponse(
        id=artifact_id if type(artifact_id) is str and artifact_id else "artifact",
        kind=artifact_type if type(artifact_type) is str and artifact_type else "artifact",
        title=title if type(title) is str and title else "artifact",
        text=text,
        filename=filename if type(filename) is str else None,
        mime_type=mime_type if type(mime_type) is str else None,
        size_bytes=size_bytes if type(size_bytes) is int else None,
        sha256=sha256 if type(sha256) is str else None,
        download_url=download_url if type(download_url) is str else None,
        presentation=presentation,
    )


def _admin_run_artifacts(
    artifacts: Iterable[dict[str, object]],
    *,
    run_id: UUID | None = None,
) -> list[RunArtifactResponse]:
    responses: list[RunArtifactResponse] = []
    seen_download_urls: set[str] = set()
    for artifact in artifacts:
        response = _admin_run_artifact(artifact, run_id=run_id)
        if response.download_url is not None:
            if response.download_url in seen_download_urls:
                continue
            seen_download_urls.add(response.download_url)
        responses.append(response)
    return responses


def _artifact_presentation(
    artifact: Mapping[str, object], *, file_metadata: Mapping[str, str | int]
) -> str | None:
    content = artifact.get("content")
    containers: list[Mapping[str, object]] = []
    if isinstance(content, Mapping):
        containers.append(content)
        result = content.get("result")
        if isinstance(result, Mapping):
            containers.append(result)
            file_payload = result.get("file")
            if isinstance(file_payload, Mapping):
                containers.append(file_payload)
            metadata_payload = result.get("metadata")
            if isinstance(metadata_payload, Mapping):
                containers.append(metadata_payload)
    for container in containers:
        presentation = container.get("presentation")
        if presentation in {"final_attachment", "step_detail", "diagnostic", "internal"}:
            return presentation
    if file_metadata:
        return "step_detail"
    return None


def _artifact_file_metadata(
    artifact: Mapping[str, object], *, run_id: UUID | None
) -> dict[str, str | int]:
    result = _artifact_result_content(artifact.get("content"))
    if result is None:
        return {}
    file_payload = result.get("file")
    if not isinstance(file_payload, Mapping):
        return {}

    metadata: dict[str, str | int] = {}
    for key in ("filename", "mime_type", "sha256", "download_url"):
        value = file_payload.get(key)
        if type(value) is str and value:
            metadata[key] = value
    size_bytes = file_payload.get("size_bytes")
    if type(size_bytes) is int and size_bytes >= 0:
        metadata["size_bytes"] = size_bytes

    artifact_id = artifact.get("id")
    if "download_url" not in metadata and run_id is not None and type(artifact_id) is str:
        try:
            parsed_artifact_id = UUID(artifact_id)
        except ValueError:
            return metadata
        metadata["download_url"] = (
            f"/api/v1/runs/{run_id}/artifacts/{parsed_artifact_id}/download"
        )
    return metadata


def _find_generated_file_metadata(
    artifacts: Iterable[Mapping[str, object]], artifact_id: UUID
) -> dict[str, str]:
    for artifact in artifacts:
        result = _artifact_result_content(artifact.get("content"))
        if result is None:
            continue
        file_payload = result.get("file")
        metadata_payload = result.get("metadata")
        if not isinstance(file_payload, Mapping) or not isinstance(metadata_payload, Mapping):
            continue
        if str(artifact_id) not in _generated_file_artifact_ids(artifact, file_payload, metadata_payload):
            continue
        filename = metadata_payload.get("filename", file_payload.get("filename"))
        mime_type = metadata_payload.get("mime_type", file_payload.get("mime_type"))
        storage_key = metadata_payload.get("storage_key")
        if type(filename) is not str or type(mime_type) is not str or type(storage_key) is not str:
            continue
        return {
            "filename": validate_generated_filename(filename, mime_type),
            "mime_type": mime_type,
            "storage_key": storage_key,
        }
    raise KeyError(artifact_id)


def _generated_file_artifact_ids(
    artifact: Mapping[str, object],
    file_payload: Mapping[str, object],
    metadata_payload: Mapping[str, object],
) -> set[str]:
    ids: set[str] = set()
    for value in (
        artifact.get("id"),
        file_payload.get("artifact_id"),
        file_payload.get("id"),
        metadata_payload.get("artifact_id"),
        metadata_payload.get("id"),
    ):
        if type(value) is str and value:
            ids.add(value)
    return ids


def _artifact_result_content(content: object) -> Mapping[str, object] | None:
    if not isinstance(content, Mapping):
        return None
    result = content.get("result")
    if isinstance(result, Mapping):
        return result
    return content


def _artifact_text(content: object) -> str | None:
    if not isinstance(content, dict):
        return None
    text = content.get("text")
    if type(text) is not str:
        return None
    stripped = text.strip()
    if not stripped or _contains_sensitive_marker(stripped):
        return None
    return stripped


def _uuid_or_default(value: str, default: UUID) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return default


def _evolution_round_request_from_artifacts(
    artifacts: Iterable[RunArtifactResponse | dict[str, object]],
    *,
    execution_run_id: UUID,
) -> EvolutionRoundRequest:
    last_validation_error: ValidationError | None = None
    for artifact in artifacts:
        response = (
            artifact if isinstance(artifact, RunArtifactResponse) else _admin_run_artifact(artifact)
        )
        if response.text is None:
            continue
        for candidate in _json_object_candidates(response.text):
            try:
                request = EvolutionRoundRequest.model_validate(candidate)
            except ValidationError as exc:
                last_validation_error = exc
                continue
            artifact_refs = list(request.artifact_refs)
            execution_ref = f"run://{execution_run_id}"
            if execution_ref not in artifact_refs:
                artifact_refs.append(execution_ref)
            return request.model_copy(update={"artifact_refs": artifact_refs})
    if last_validation_error is not None:
        raise PublicAPIError(
            422,
            "invalid_evolution_round_output",
            "execution artifact does not match the required Evolution round schema",
        ) from None
    raise PublicAPIError(
        422,
        "missing_evolution_round_output",
        "execution run did not produce a readable Evolution round artifact",
    )


def _json_object_candidates(text: str) -> tuple[dict[str, object], ...]:
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(candidate: object) -> None:
        if not isinstance(candidate, dict):
            return
        key = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            add(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    for match in re.finditer(
        r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL
    ):
        try:
            add(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        add(candidate)
    return tuple(candidates)


def _assert_evolution_execution_binding(
    run_id: str,
    execution_run_id: UUID,
    routing: Mapping[str, object] | None,
    *,
    expected_round: int,
) -> None:
    if not routing:
        raise PublicAPIError(
            409,
            "evolution_execution_mismatch",
            "execution run is not linked to an Evolution run",
            details={"execution_run_id": str(execution_run_id)},
        )
    if (
        str(routing.get("source") or "") != "evolution"
        or str(routing.get("evolution_run_id") or "") != run_id
    ):
        raise PublicAPIError(
            409,
            "evolution_execution_mismatch",
            "execution run is not linked to this Evolution run",
            details={"execution_run_id": str(execution_run_id)},
        )
    round_marker = routing.get("evolution_round")
    if type(round_marker) is int:
        round_number = round_marker
    elif type(round_marker) is str:
        try:
            round_number = int(round_marker)
        except ValueError:
            raise PublicAPIError(
                409,
                "evolution_execution_round_mismatch",
                "execution run has no valid Evolution round marker",
                details={"execution_run_id": str(execution_run_id)},
            ) from None
    else:
        raise PublicAPIError(
            409,
            "evolution_execution_round_mismatch",
            "execution run has no valid Evolution round marker",
            details={"execution_run_id": str(execution_run_id)},
        )
    if round_number != expected_round:
        raise PublicAPIError(
            409,
            "evolution_execution_round_mismatch",
            "execution run round does not match the next expected Evolution round",
            details={
                "execution_run_id": str(execution_run_id),
                "execution_round": str(round_number),
                "expected_round": str(expected_round),
            },
        )


def _evolution_execution_conversation_id(run_id: str, round_number: int) -> str:
    safe_run_id = re.sub(r"[^a-zA-Z0-9_-]", "-", run_id).strip("-") or "evolution"
    return f"{safe_run_id}-round-{round_number}"


def _evolution_execution_routing_decision(
    current: EvolutionRunResponse,
    plan: EvolutionNextRoundPlanResponse,
    conversation_id: str,
) -> dict[str, object]:
    return {
        "source": "evolution",
        "conversation_id": conversation_id,
        "evolution_run_id": current.id,
        "evolution_round": plan.round,
        "evolution_action": plan.action,
        "evolution_target_artifact_type": current.target_artifact_type,
        "evolution_memory_policy": plan.memory_policy,
        "evolution_required_output_schema": plan.required_output_schema,
        "evolution_previous_rounds": plan.previous_rounds,
        "baseline_agent_id": plan.baseline_agent_id,
        "candidate_agent_ids": plan.candidate_agent_ids,
        "evaluator_agent_id": plan.evaluator_agent_id,
        "selected_agent_ids": list(
            dict.fromkeys([*plan.candidate_agent_ids, plan.evaluator_agent_id])
        ),
        "requested_skills": ", ".join(current.source_skill_ids),
        "reason": "approved_evolution_next_round_execution",
    }


def _evolution_execution_routing_details(
    current: EvolutionRunResponse,
    plan: EvolutionNextRoundPlanResponse,
    conversation_id: str,
) -> dict[str, str]:
    return _routing_details(_evolution_execution_routing_decision(current, plan, conversation_id))


def _routing_details(routing_decision: dict[str, object] | None) -> dict[str, str]:
    if not routing_decision:
        return {}
    details: dict[str, str] = {}
    workflow_id = routing_decision.get("workflow_id")
    if isinstance(workflow_id, str) and workflow_id:
        details["workflow_id"] = workflow_id
    source = routing_decision.get("source")
    if isinstance(source, str) and source:
        details["source"] = source[:128]
    conversation_id = routing_decision.get("conversation_id")
    if isinstance(conversation_id, str) and conversation_id:
        details["conversation_id"] = conversation_id
    reference_conversation_id = routing_decision.get("reference_conversation_id")
    if isinstance(reference_conversation_id, str) and reference_conversation_id:
        details["reference_conversation_id"] = reference_conversation_id
    for key in (
        "project_id",
        "project_label",
        "workspace_session_id",
        "workspace_project_path",
        "workspace_session_path",
        "workspace_artifacts_path",
        "sandbox_profile",
        "sandbox_permission_policy",
    ):
        value = routing_decision.get(key)
        if isinstance(value, str) and value:
            details[key] = value[:512]
    requested_permissions = routing_decision.get("requested_permissions")
    if isinstance(requested_permissions, list):
        safe_permissions = [
            item for item in requested_permissions if isinstance(item, str) and item
        ]
        if safe_permissions:
            details["requested_permissions"] = ", ".join(safe_permissions)
    adjustment_policy = routing_decision.get("workflow_adjustment_policy")
    if adjustment_policy == "ask_before_apply":
        details["workflow_adjustment_policy"] = "ask_before_apply"
    elif adjustment_policy == "strict_preset":
        details["workflow_adjustment_policy"] = "strict_preset"
    if routing_decision.get("vibe_coding") is True:
        details["vibe_coding"] = "enabled"
    capability = routing_decision.get("capability")
    if isinstance(capability, str) and capability:
        details["capability"] = capability
    details.update(_harness_routing_details(routing_decision.get("harness_decision")))
    for key in (
        "requested_channel_features",
        "requested_skills",
        "requested_mcp_servers",
        "requested_plugins",
    ):
        value = routing_decision.get(key)
        if isinstance(value, str) and value:
            details[key] = value[:512]
    selected_agent_ids = routing_decision.get("selected_agent_ids")
    if isinstance(selected_agent_ids, list):
        safe_ids = [item for item in selected_agent_ids if isinstance(item, str) and item]
        if safe_ids:
            details["selected_agent_ids"] = ", ".join(safe_ids)
    candidate_agent_ids = routing_decision.get("candidate_agent_ids")
    if isinstance(candidate_agent_ids, list):
        safe_candidates = [item for item in candidate_agent_ids if isinstance(item, str) and item]
        if safe_candidates:
            details["candidate_agent_ids"] = ", ".join(safe_candidates)
    for key in (
        "evolution_run_id",
        "evolution_action",
        "evolution_target_artifact_type",
        "evolution_memory_policy",
        "baseline_agent_id",
        "evaluator_agent_id",
    ):
        value = routing_decision.get(key)
        if isinstance(value, str) and value:
            details[key] = value[:512]
    evolution_round = routing_decision.get("evolution_round")
    if isinstance(evolution_round, int):
        details["evolution_round"] = str(evolution_round)
    reason = routing_decision.get("reason")
    if isinstance(reason, str) and reason:
        details["routing_reason"] = reason
    approval_kind = routing_decision.get("approval_kind")
    if approval_kind == "capability_tool":
        details["approval_kind"] = approval_kind
        approval_id = routing_decision.get("approval_id")
        if isinstance(approval_id, str) and approval_id:
            details["approval_id"] = approval_id[:128]
    hermes = routing_decision.get("hermes")
    if isinstance(hermes, dict):
        confidence = hermes.get("confidence")
        mode = hermes.get("recommended_mode")
        reasons = hermes.get("reasons")
        if isinstance(mode, str) and mode:
            details["hermes_recommended_mode"] = mode
        if isinstance(confidence, (int, float)):
            details["hermes_confidence"] = f"{confidence:.2f}"
        if isinstance(reasons, list):
            safe_reasons = [str(item)[:160] for item in reasons[:3]]
            if safe_reasons:
                details["hermes_reasons"] = " | ".join(safe_reasons)
    return details


def _harness_routing_details(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    details: dict[str, str] = {}
    for source_key, target_key in (
        ("selected_provider", "harness_provider"),
        ("selected_model", "harness_model"),
        ("selected_logical_model", "harness_logical_model"),
    ):
        text = _safe_harness_detail_text(value.get(source_key))
        if text:
            details[target_key] = text
    approval = value.get("requires_approval")
    if type(approval) is bool:
        details["harness_requires_approval"] = "true" if approval else "false"
    for source_key, target_key in (
        ("capability_reasons", "harness_capabilities"),
        ("policy_reasons", "harness_policy"),
        ("context_reasons", "harness_context"),
        ("fallbacks_considered", "harness_fallbacks"),
    ):
        text = _safe_harness_detail_list(value.get(source_key))
        if text:
            details[target_key] = text
    return details


def _safe_harness_detail_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or _contains_sensitive_marker(stripped):
        return None
    return stripped[:160]


def _safe_harness_detail_list(value: object) -> str | None:
    if not isinstance(value, list | tuple):
        return None
    items: list[str] = []
    for item in value[:6]:
        text = _safe_harness_detail_text(item)
        if text:
            items.append(text)
    return ", ".join(items) if items else None


def _waiting_mode_decision_token(record: RunRecord) -> str | None:
    if (
        record.status not in {RunStatus.WAITING_USER_MODE, RunStatus.WAITING_APPROVAL}
        and not (
            record.status is RunStatus.FAILED
            and record.routing_decision
            and record.routing_decision.get("approval_kind") == "self_repair"
        )
        or not record.routing_decision
    ):
        return None
    token = record.routing_decision.get("decision_token")
    if isinstance(token, str) and token:
        return token
    return None


def _temporary_agent_proposal(
    routing_decision: dict[str, object] | None,
) -> dict[str, JsonValue] | None:
    if not routing_decision:
        return None
    proposal = routing_decision.get("temporary_agent_proposal")
    if not isinstance(proposal, dict):
        return None
    safe: dict[str, JsonValue] = {}
    for key, value in proposal.items():
        if isinstance(key, str) and isinstance(value, str | int | float | bool):
            safe[key] = value
        elif isinstance(key, str) and isinstance(value, list):
            safe[key] = tuple(item for item in value if isinstance(item, str))
    return safe or None


def _evolution_proposal(
    routing_decision: Mapping[str, object] | None,
) -> dict[str, JsonValue] | None:
    if routing_decision is None:
        return None
    proposal = routing_decision.get("evolution_proposal")
    if not isinstance(proposal, dict):
        return None
    return cast(dict[str, JsonValue], proposal)


def _openclaw_proposal(
    routing_decision: Mapping[str, object] | None,
) -> dict[str, JsonValue] | None:
    if routing_decision is None:
        return None
    proposal = routing_decision.get("openclaw_proposal")
    if not isinstance(proposal, dict):
        return None
    safe = _safe_proposal_json_mapping(proposal)
    return safe or None


def _repair_proposal(
    routing_decision: Mapping[str, object] | None,
) -> dict[str, JsonValue] | None:
    if routing_decision is None:
        return None
    proposal = routing_decision.get("repair_proposal")
    if not isinstance(proposal, dict):
        return None
    return repair_proposal_projection(proposal)


def _schedule_proposal(
    routing_decision: dict[str, object] | None,
) -> dict[str, JsonValue] | None:
    if not routing_decision:
        return None
    proposal = routing_decision.get("schedule_proposal")
    if not isinstance(proposal, dict):
        return None
    safe = _safe_proposal_json_mapping(proposal)
    return safe or None


def _safe_proposal_json_mapping(value: dict[object, object]) -> dict[str, JsonValue]:
    safe: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        converted = _safe_proposal_json_value(item)
        if converted is not None:
            safe[key] = converted
    return safe


def _safe_proposal_json_value(value: object) -> JsonValue | None:
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return tuple(
            converted
            for item in value
            if (converted := _safe_proposal_json_value(item)) is not None
        )
    if isinstance(value, dict):
        return _safe_proposal_json_mapping(value)
    return None


@router.get(
    "/models",
    response_model=list[ModelDeploymentResponse],
    responses=error_responses(401, 403, 422),
)
async def list_models(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[ModelDeploymentResponse]:
    _require(principal, "config:read")
    return list(await service.list_models())


@router.post(
    "/models", response_model=ModelDeploymentResponse, responses=error_responses(401, 403, 422)
)
async def create_model(
    body: ModelDeploymentRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> ModelDeploymentResponse:
    _require(principal, "config:write")
    return await service.create_model(body)


@router.delete(
    "/models/{model_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def delete_model(
    model_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "config:write")
    await service.delete_model(model_id)
    return OperationStatusResponse(status="deleted")


@router.put(
    "/models/{model_id}",
    response_model=ModelDeploymentResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def update_model(
    model_id: UUID,
    body: ModelDeploymentRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> ModelDeploymentResponse:
    _require(principal, "config:write")
    return await service.update_model(model_id, body)


@router.post(
    "/models/probe", response_model=ProbeResponse, responses=error_responses(401, 403, 422)
)
async def probe_model(
    body: ProbeRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> ProbeResponse:
    _require(principal, "config:read")
    return await service.probe_concurrency(body)


@router.post(
    "/secrets",
    response_model=SecretReferenceResponse,
    responses=error_responses(401, 403, 409, 422),
)
async def create_secret(
    body: SecretCreateRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SecretReferenceResponse:
    _require(principal, "config:write")
    try:
        return await service.create_secret(body)
    except ValueError:
        raise PublicAPIError(409, "duplicate_secret", "secret is already registered") from None


@router.get(
    "/secrets/{ref}",
    response_model=SecretReferenceResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def get_secret(
    ref: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SecretReferenceResponse:
    _require(principal, "config:read")
    try:
        return await service.get_secret(ref)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.put(
    "/config/draft", response_model=PublishResponse, responses=error_responses(401, 403, 422)
)
async def save_draft(
    body: DraftRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PublishResponse:
    _require(principal, "config:write")
    return await service.save_draft(body)


@router.post("/config/diff", response_model=DiffResponse, responses=error_responses(401, 403, 422))
async def diff_draft(
    body: DraftRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> DiffResponse:
    _require(principal, "config:read")
    return await service.diff_draft(body)


@router.post(
    "/config/publish", response_model=PublishResponse, responses=error_responses(401, 403, 409, 422)
)
async def publish(
    body: PublishRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PublishResponse:
    _require(principal, "config:publish")
    try:
        return await service.publish(body)
    except RuntimeError:
        raise PublicAPIError(409, "publish_conflict", "configuration changed") from None


@router.post(
    "/config/rollback/{version}",
    response_model=PublishResponse,
    responses=error_responses(401, 403, 422),
)
async def rollback(
    version: int,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PublishResponse:
    _require(principal, "config:publish")
    return await service.rollback(version)


@router.get(
    "/agents", response_model=list[AgentResourceResponse], responses=error_responses(401, 403, 422)
)
async def list_agents(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[AgentResourceResponse]:
    _require(principal, "agent:read")
    return list(await service.list_agents())


@router.post(
    "/agents", response_model=AgentResourceResponse, responses=error_responses(401, 403, 422)
)
async def upsert_agent(
    body: AgentResourceRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> AgentResourceResponse:
    _require(principal, "agent:write")
    return await service.upsert_agent(body)


@router.delete(
    "/agents/{agent_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_agent(
    agent_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "agent:write")
    try:
        await service.delete_agent(agent_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    return OperationStatusResponse(status="deleted")


@router.get(
    "/workflows",
    response_model=list[WorkflowResourceResponse],
    responses=error_responses(401, 403, 422),
)
async def list_workflows(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[WorkflowResourceResponse]:
    _require(principal, "agent:read")
    return list(await service.list_workflows())


@router.post(
    "/workflows", response_model=WorkflowResourceResponse, responses=error_responses(401, 403, 422)
)
async def upsert_workflow(
    body: WorkflowResourceRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> WorkflowResourceResponse:
    _require(principal, "agent:write")
    return await service.upsert_workflow(body)


@router.delete(
    "/workflows/{workflow_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_workflow(
    workflow_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "agent:write")
    try:
        await service.delete_workflow(workflow_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    return OperationStatusResponse(status="deleted")


@router.get(
    "/settings", response_model=SystemSettingsResponse, responses=error_responses(401, 403, 422)
)
async def get_settings(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SystemSettingsResponse:
    _require(principal, "config:read")
    return _settings_with_runtime_status(await service.get_settings(), request)


@router.put(
    "/settings", response_model=SystemSettingsResponse, responses=error_responses(401, 403, 422)
)
async def update_settings(
    body: SystemSettingsRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SystemSettingsResponse:
    _require(principal, "config:write")
    return _settings_with_runtime_status(await service.update_settings(body), request)


@router.post(
    "/schedules",
    response_model=ScheduleResponse,
    status_code=201,
    responses=error_responses(401, 403, 422, 503),
)
async def create_schedule(
    body: ScheduleCreateRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    resources: Annotated[AdminResourceService, Depends(_service)],
) -> ScheduleResponse:
    _require(principal, "run:create")
    service = _scheduler_service(request)
    try:
        schedule = await service.create_schedule(
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            name=body.name,
            message=body.message,
            mode=TaskMode(body.mode),
            workflow=body.workflow_id,
            budget=body.budget,
            spec=_schedule_spec_from_request(body),
            idempotency_key=body.idempotency_key or body.name,
            now=datetime.now(UTC),
            user_visible=True,
            metadata=body.metadata,
        )
    except ValueError as error:
        raise PublicAPIError(
            422,
            "request_validation",
            "request validation failed",
            details={"reason": str(error)},
        ) from error
    if hasattr(resources, "_upsert_admin_payload"):
        try:
            persisted = await cast(Any, resources)._upsert_admin_payload(
                "schedule",
                str(schedule.id),
                _schedule_to_payload(schedule),
            )
        except Exception as error:
            _LOGGER.exception("failed to persist schedule %s", schedule.id)
            raise PublicAPIError(
                503,
                "schedule_persistence_unavailable",
                "schedule persistence is unavailable",
            ) from error
        if not persisted:
            raise PublicAPIError(
                503,
                "schedule_persistence_unavailable",
                "schedule persistence is unavailable",
            )
    try:
        await resources.record_audit_event(
            actor=str(principal.user_id),
            action="schedule.create",
            resource=f"schedule:{schedule.id}",
            details={"name": schedule.name, "kind": schedule.schedule_kind.value},
        )
    except Exception:
        _LOGGER.exception("failed to record schedule create audit %s", schedule.id)
    return _schedule_response(schedule)


@router.get(
    "/schedules",
    response_model=list[ScheduleResponse],
    responses=error_responses(401, 403, 503),
)
async def list_schedules(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    resources: Annotated[AdminResourceService, Depends(_service)],
) -> list[ScheduleResponse]:
    _require(principal, "run:read")
    service = _scheduler_service(request)
    await _restore_persisted_schedules(service, resources, tenant_id=principal.tenant_id)
    return [
        _schedule_response(schedule)
        for schedule in await service.list_schedules(tenant_id=principal.tenant_id)
    ]


@router.post(
    "/schedules/tick",
    response_model=ScheduleTickResponse,
    responses=error_responses(401, 403, 422, 503),
)
async def tick_schedules(
    body: ScheduleTickRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    resources: Annotated[AdminResourceService, Depends(_service)],
) -> ScheduleTickResponse:
    _require(principal, "run:create")
    service = _scheduler_service(request)
    await _restore_persisted_schedules(service, resources, tenant_id=principal.tenant_id)
    try:
        fired = await service.tick(tenant_id=principal.tenant_id, now=body.now)
    except ValueError as error:
        raise PublicAPIError(
            422,
            "request_validation",
            "request validation failed",
            details={"reason": str(error)},
        ) from error
    await _persist_schedules(service, resources, tenant_id=principal.tenant_id)
    return ScheduleTickResponse(fired=list(fired))


@router.delete(
    "/schedules/{schedule_id}",
    response_model=RunDeleteResponse,
    responses=error_responses(401, 403, 404, 422, 503),
)
async def delete_schedule(
    schedule_id: UUID,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    resources: Annotated[AdminResourceService, Depends(_service)],
) -> RunDeleteResponse:
    _require(principal, "run:create")
    service = _scheduler_service(request)
    await _restore_persisted_schedules(service, resources, tenant_id=principal.tenant_id)
    try:
        await service.delete_schedule(tenant_id=principal.tenant_id, schedule_id=schedule_id)
    except KeyError:
        raise PublicAPIError(404, "schedule_not_found", "schedule not found") from None
    if hasattr(resources, "_delete_admin_payload"):
        await cast(Any, resources)._delete_admin_payload("schedule", str(schedule_id))
    await resources.record_audit_event(
        actor=str(principal.user_id),
        action="schedule.delete",
        resource=f"schedule:{schedule_id}",
        details={"id": str(schedule_id)},
    )
    return RunDeleteResponse(id=schedule_id, deleted=True)


@router.post(
    "/multimedia/jobs",
    response_model=MultimediaGenerationJobResponse,
    status_code=202,
    responses=error_responses(401, 403, 409, 422, 503),
)
async def submit_multimedia_job(
    body: MultimediaGenerationRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MultimediaGenerationJobResponse:
    _require(principal, "run:create")
    await _require_multimedia_generation_enabled(service)
    executor = _tenant_multimedia_generation_executor(request, principal.tenant_id)
    job = executor.submit(
        kind=MultimediaGenerationKind(body.kind),
        logical_model=body.logical_model,
        prompt=body.prompt,
    )
    return _multimedia_job_response(job)


@router.get(
    "/multimedia/jobs/{job_id}",
    response_model=MultimediaGenerationJobResponse,
    responses=error_responses(401, 403, 404, 422, 503),
)
async def get_multimedia_job(
    job_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
) -> MultimediaGenerationJobResponse:
    _require(principal, "run:read")
    executor = _tenant_multimedia_generation_executor(request, principal.tenant_id)
    try:
        return _multimedia_job_response(executor.get_job(job_id))
    except KeyError as error:
        raise PublicAPIError(
            404, "multimedia_job_not_found", "multimedia generation job not found"
        ) from error


@router.post(
    "/multimedia/jobs/{job_id}/run",
    response_model=MultimediaGenerationJobResponse,
    status_code=202,
    responses=error_responses(401, 403, 404, 409, 422, 429, 502, 503),
)
async def run_multimedia_job(
    job_id: str,
    body: MultimediaGenerationJobRunRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MultimediaGenerationJobResponse:
    _require(principal, "run:create")
    await _require_multimedia_generation_enabled(service)
    executor = _tenant_multimedia_generation_executor(request, principal.tenant_id)
    try:
        job = await executor.run_job(job_id, executor_id=body.executor_id)
    except KeyError as error:
        raise PublicAPIError(
            404, "multimedia_job_not_found", "multimedia generation job not found"
        ) from error
    except RuntimeError as error:
        raise PublicAPIError(
            409, "multimedia_job_not_queued", "multimedia generation job is not queued"
        ) from error
    except NoCapableDeployment as error:
        raise PublicAPIError(
            422,
            "model_capability_unavailable",
            "no configured model can satisfy the requested multimedia capability",
            details={"reason": str(error)},
        ) from error
    except MultimediaDailyLimitExceeded as error:
        raise PublicAPIError(
            429,
            "multimedia_daily_limit_exceeded",
            "daily multimedia generation limit exceeded",
        ) from error
    except VideoProviderGenerationError as error:
        raise PublicAPIError(
            502,
            "multimedia_provider_failed",
            "multimedia provider generation failed",
            details={
                "provider_code": error.provider_code or "unknown",
                "reason": str(error),
            },
        ) from error
    return _multimedia_job_response(job)


@router.post(
    "/multimedia/generate",
    response_model=MultimediaGenerationResponse,
    status_code=202,
    responses=error_responses(401, 403, 409, 422, 503),
)
async def generate_multimedia(
    body: MultimediaGenerationRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MultimediaGenerationResponse:
    _require(principal, "run:create")
    await _require_multimedia_generation_enabled(service)
    executor = _tenant_multimedia_generation_executor(request, principal.tenant_id)
    try:
        result = await executor.generate(
            kind=MultimediaGenerationKind(body.kind),
            logical_model=body.logical_model,
            prompt=body.prompt,
        )
    except NoCapableDeployment as error:
        raise PublicAPIError(
            422,
            "model_capability_unavailable",
            "no configured model can satisfy the requested multimedia capability",
            details={"reason": str(error)},
        ) from error
    except MultimediaDailyLimitExceeded as error:
        raise PublicAPIError(
            429,
            "multimedia_daily_limit_exceeded",
            "daily multimedia generation limit exceeded",
        ) from error
    except VideoProviderGenerationError as error:
        raise PublicAPIError(
            502,
            "multimedia_provider_failed",
            "multimedia provider generation failed",
            details={
                "provider_code": error.provider_code or "unknown",
                "reason": str(error),
            },
        ) from error
    return MultimediaGenerationResponse(
        kind=result.kind.value,
        logical_model=result.logical_model,
        deployment_id=result.deployment_id,
        text=result.text,
    )


async def _require_multimedia_generation_enabled(service: AdminResourceService) -> None:
    settings = await service.get_settings()
    if not settings.multimedia_generation_enabled:
        raise PublicAPIError(
            409,
            "multimedia_generation_disabled",
            "multimedia generation is disabled",
        )


async def _require_openclaw_bound_session_active(
    service: AdminResourceService,
    request: OpenClawOperationRequest,
) -> None:
    if request.session_id is None:
        return
    session = next(
        (item for item in await service.list_openclaw_sessions() if item.id == request.session_id),
        None,
    )
    if session is None:
        raise PublicAPIError(404, "not_found", "not found")
    _validate_openclaw_session_for_operation(session, request)


@router.post(
    "/openclaw/operations",
    response_model=OpenClawOperationResponse,
    status_code=202,
    responses=error_responses(401, 403, 409, 422),
)
async def create_openclaw_operation(
    body: OpenClawOperationRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawOperationResponse:
    _require(principal, "config:write")
    settings = await service.get_settings()
    if not settings.openclaw_enabled:
        raise PublicAPIError(409, "openclaw_disabled", "OpenClaw is disabled")
    if settings.openclaw_mode == "read_only" and body.kind not in {"screen_read", "file_read"}:
        raise PublicAPIError(
            403, "openclaw_read_only", "OpenClaw read-only mode blocks this operation"
        )
    await _require_openclaw_bound_session_active(service, body)
    operation = await service.create_openclaw_operation(
        body,
        actor=str(principal.user_id),
        mode=settings.openclaw_mode,
    )
    if body.session_id is not None:
        await service.attach_openclaw_operation_to_session(
            body.session_id,
            operation.id,
            body,
            actor=str(principal.user_id),
        )
    if _openclaw_can_auto_approve(body, settings):
        return await service.resolve_openclaw_operation(
            operation.id,
            OpenClawResolveRequest(decision="approve"),
            actor=str(principal.user_id),
        )
    return operation


@router.post(
    "/openclaw/operations/from-run/{run_id}",
    response_model=OpenClawOperationResponse,
    status_code=202,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def create_openclaw_operation_from_run(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawOperationResponse:
    _require(principal, "config:write")
    settings = await service.get_settings()
    if not settings.openclaw_enabled:
        raise PublicAPIError(409, "openclaw_disabled", "OpenClaw is disabled")
    try:
        run = await service.get_run(run_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    body = _openclaw_operation_request_from_run_detail(run)
    if settings.openclaw_mode == "read_only" and body.kind not in {"screen_read", "file_read"}:
        raise PublicAPIError(
            403, "openclaw_read_only", "OpenClaw read-only mode blocks this operation"
        )
    operation = await service.create_openclaw_operation(
        body,
        actor=str(principal.user_id),
        mode=settings.openclaw_mode,
    )
    if _openclaw_can_auto_approve(body, settings):
        return await service.resolve_openclaw_operation(
            operation.id,
            OpenClawResolveRequest(decision="approve"),
            actor=str(principal.user_id),
        )
    return operation


@router.get(
    "/openclaw/adapters",
    response_model=list[OpenClawAdapterResponse],
    responses=error_responses(401, 403, 422),
)
async def list_openclaw_adapters(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> tuple[OpenClawAdapterResponse, ...]:
    _require(principal, "config:read")
    return _openclaw_adapter_responses(await service.get_settings())


@router.post(
    "/openclaw/sessions",
    response_model=OpenClawSessionResponse,
    status_code=201,
    responses=error_responses(401, 403, 409, 422),
)
async def create_openclaw_session(
    body: OpenClawSessionRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawSessionResponse:
    _require(principal, "config:write")
    settings = await service.get_settings()
    if not settings.openclaw_enabled:
        raise PublicAPIError(409, "openclaw_disabled", "OpenClaw is disabled")
    return await service.create_openclaw_session(
        body,
        actor=str(principal.user_id),
        mode=settings.openclaw_mode,
        settings=settings,
    )


@router.get(
    "/openclaw/sessions",
    response_model=list[OpenClawSessionResponse],
    responses=error_responses(401, 403, 422),
)
async def list_openclaw_sessions(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> tuple[OpenClawSessionResponse, ...]:
    _require(principal, "config:read")
    return await service.list_openclaw_sessions()


@router.patch(
    "/openclaw/sessions/{session_id}",
    response_model=OpenClawSessionResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def update_openclaw_session(
    session_id: str,
    body: OpenClawSessionActionRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawSessionResponse:
    _require(principal, "config:write")
    return await service.update_openclaw_session(
        session_id,
        body,
        actor=str(principal.user_id),
    )


@router.get(
    "/openclaw/operations/{operation_id}",
    response_model=OpenClawOperationResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def get_openclaw_operation(
    operation_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawOperationResponse:
    _require(principal, "config:read")
    return await service.get_openclaw_operation(operation_id)


@router.patch(
    "/openclaw/operations/{operation_id}",
    response_model=OpenClawOperationResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def resolve_openclaw_operation(
    operation_id: str,
    body: OpenClawResolveRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawOperationResponse:
    _require(principal, "config:write")
    return await service.resolve_openclaw_operation(
        operation_id,
        body,
        actor=str(principal.user_id),
    )


@router.post(
    "/openclaw/operations/{operation_id}/execute",
    response_model=OpenClawExecutionResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def execute_openclaw_operation(
    operation_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OpenClawExecutionResponse:
    _require(principal, "config:write")
    operation = await service.get_openclaw_operation(operation_id)
    request = OpenClawOperationRequest.model_validate(operation.operation)
    await _require_openclaw_bound_session_active(service, request)
    settings = await service.get_settings()
    return await service.execute_openclaw_operation(
        operation_id,
        settings,
        actor=str(principal.user_id),
    )


@router.get(
    "/main-agent", response_model=MainAgentConfigResponse, responses=error_responses(401, 403, 422)
)
async def get_main_agent_config(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MainAgentConfigResponse:
    _require(principal, "config:read")
    return await service.get_main_agent_config()


@router.put(
    "/main-agent", response_model=MainAgentConfigResponse, responses=error_responses(401, 403, 422)
)
async def update_main_agent_config(
    body: MainAgentConfigRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MainAgentConfigResponse:
    _require(principal, "config:write")
    return await service.update_main_agent_config(body)


@router.get("/runs", response_model=list[RunListItem], responses=error_responses(401, 403, 422))
async def list_operational_runs(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[RunListItem]:
    _require(principal, "run:read")
    return list(await service.list_runs())


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    responses=error_responses(401, 403, 422),
)
async def get_conversation(
    conversation_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> ConversationResponse:
    _require(principal, "run:read")
    return await service.get_conversation(conversation_id)


@router.post(
    "/runs/bulk-delete",
    response_model=RunBulkDeleteResponse,
    responses=error_responses(401, 403, 422),
)
async def bulk_delete_operational_runs(
    body: RunBulkDeleteRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunBulkDeleteResponse:
    _require(principal, "run:delete")
    deleted: list[RunDeleteResponse] = []
    failed: list[BulkFailureResponse] = []
    for run_id in dict.fromkeys(body.ids):
        try:
            deleted.append(await service.delete_run(run_id))
        except PublicAPIError as error:
            failed.append(
                BulkFailureResponse(
                    id=str(run_id),
                    code=error.code,
                    message=error.public_message,
                )
            )
        except KeyError:
            failed.append(
                BulkFailureResponse(
                    id=str(run_id),
                    code="not_found",
                    message="not found",
                )
            )
    return RunBulkDeleteResponse(deleted=deleted, failed=failed)


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def get_operational_run(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunDetailResponse:
    _require(principal, "run:read")
    try:
        return await service.get_run(run_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.get(
    "/runs/{run_id}/artifacts/{artifact_id}/download",
    response_class=FileResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def download_operational_run_artifact(
    run_id: UUID,
    artifact_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> FileResponse:
    _require(principal, "run:read")
    try:
        download = await service.download_run_artifact(
            run_id,
            artifact_id,
            tenant_id=principal.tenant_id,
        )
    except (KeyError, FileNotFoundError, ValueError):
        raise PublicAPIError(404, "not_found", "not found") from None
    return FileResponse(
        download.path,
        media_type=download.mime_type,
        filename=download.filename,
    )


@router.get(
    "/runs/{run_id}/debug",
    response_model=RunDebugResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def get_operational_run_debug(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunDebugResponse:
    _require(principal, "run:read")
    try:
        return _run_debug_from_detail(await service.get_run(run_id))
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.post(
    "/runs/{run_id}/pause",
    response_model=RunDetailResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def pause_operational_run(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunDetailResponse:
    _require(principal, "run:pause")
    try:
        return await service.pause_run(run_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.post(
    "/runs/{run_id}/resume",
    response_model=RunDetailResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def resume_operational_run(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunDetailResponse:
    _require(principal, "run:resume")
    try:
        return await service.resume_run(run_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunDetailResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def cancel_operational_run(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunDetailResponse:
    _require(principal, "run:cancel")
    try:
        return await service.cancel_run(run_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.delete(
    "/runs/{run_id}",
    response_model=RunDeleteResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def delete_operational_run(
    run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> RunDeleteResponse:
    _require(principal, "run:delete")
    try:
        return await service.delete_run(run_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.get(
    "/evolution-runs",
    response_model=list[EvolutionRunResponse],
    responses=error_responses(401, 403, 422),
)
async def list_evolution_runs(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[EvolutionRunResponse]:
    _require(principal, "skill:read")
    return list(await service.list_evolution_runs())


@router.post(
    "/evolution-runs", response_model=EvolutionRunResponse, responses=error_responses(401, 403, 422)
)
async def create_evolution_run(
    body: EvolutionRunRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> EvolutionRunResponse:
    _require(principal, "skill:write")
    return await service.create_evolution_run(body, actor=str(principal.user_id))


@router.get(
    "/evolution-runs/{run_id}",
    response_model=EvolutionRunResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def get_evolution_run(
    run_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> EvolutionRunResponse:
    _require(principal, "skill:read")
    return await service.get_evolution_run(run_id)


@router.get(
    "/evolution-runs/{run_id}/next-round-plan",
    response_model=EvolutionNextRoundPlanResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def plan_evolution_next_round_route(
    run_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> EvolutionNextRoundPlanResponse:
    _require(principal, "skill:read")
    return await service.plan_evolution_next_round(run_id, actor=str(principal.user_id))


@router.post(
    "/evolution-runs/{run_id}/execute-next-round",
    response_model=EvolutionNextRoundExecutionResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def execute_evolution_next_round_route(
    run_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    body: EvolutionNextRoundExecutionRequest | None = None,
) -> EvolutionNextRoundExecutionResponse:
    _require(principal, "skill:write")
    request = body or EvolutionNextRoundExecutionRequest()
    return await service.execute_evolution_next_round(run_id, request, actor=str(principal.user_id))


@router.post(
    "/evolution-runs/{run_id}/execution-runs/{execution_run_id}/ingest",
    response_model=EvolutionRunResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def ingest_evolution_execution_run(
    run_id: str,
    execution_run_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> EvolutionRunResponse:
    _require(principal, "skill:write")
    return await service.ingest_evolution_execution_run(
        run_id, execution_run_id, actor=str(principal.user_id)
    )


@router.post(
    "/evolution-runs/{run_id}/approve",
    response_model=EvolutionRunResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def approve_evolution_run(
    run_id: str,
    body: EvolutionApprovalRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> EvolutionRunResponse:
    _require(principal, "skill:write")
    return await service.approve_evolution_run(run_id, body, actor=str(principal.user_id))


@router.post(
    "/evolution-runs/{run_id}/rounds",
    response_model=EvolutionRunResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def record_evolution_round(
    run_id: str,
    body: EvolutionRoundRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> EvolutionRunResponse:
    _require(principal, "skill:write")
    return await service.record_evolution_round(run_id, body, actor=str(principal.user_id))


@router.get("/skills", response_model=list[SkillResponse], responses=error_responses(401, 403, 422))
async def list_skills(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[SkillResponse]:
    _require(principal, "skill:read")
    return list(await service.list_skills())


@router.post("/skills", response_model=SkillResponse, responses=error_responses(401, 403, 422))
async def upload_skill(
    body: SkillUploadRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SkillResponse:
    _require(principal, "skill:write")
    return await service.upload_skill(body)


@router.post(
    "/skills/upload",
    response_model=SkillArchiveUploadResponse,
    responses=error_responses(401, 403, 409, 413, 422, 503),
)
async def upload_skill_archive(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    strategy: Annotated[
        str | None,
        Query(description="Same-name upload strategy: overwrite or new_version."),
    ] = None,
) -> SkillArchiveUploadResponse:
    _require(principal, "skill:write")
    filename = _safe_skill_upload_filename(
        _decode_upload_filename_header(
            request.headers.get("x-agent-hub-skill-filename"),
            request.headers.get("x-agent-hub-skill-filename-encoding"),
        )
    )
    archive_bytes = await request.body()
    if not archive_bytes:
        raise PublicAPIError(422, "request_validation", "skill archive is empty")
    upload_strategy = _skill_upload_strategy_or_error(strategy)
    if upload_strategy is not None:
        _require(principal, "skill:approve")
    try:
        return await service.upload_skill_archive(
            filename, archive_bytes, strategy=upload_strategy
        )
    except InvalidSkillPackage as error:
        raise PublicAPIError(
            422,
            "invalid_skill_package",
            "skill package is invalid",
            details={"reason": _safe_model_check_detail(str(error))},
        ) from None


@router.post(
    "/skills/{skill_id}/versions/{version_id}/activate",
    response_model=SkillResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def activate_skill_version(
    skill_id: str,
    version_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SkillResponse:
    _require(principal, "skill:approve")
    try:
        return await service.activate_skill_version(skill_id, version_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.post(
    "/skills/{skill_id}/approve",
    response_model=SkillResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def approve_skill(
    skill_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SkillResponse:
    _require(principal, "skill:approve")
    try:
        return await service.approve_skill(skill_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.post(
    "/skills/bulk-delete",
    response_model=SkillBulkDeleteResponse,
    responses=error_responses(401, 403, 422),
)
async def bulk_delete_skills(
    body: SkillBulkDeleteRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SkillBulkDeleteResponse:
    _require(principal, "skill:approve")
    deleted: list[str] = []
    failed: list[BulkFailureResponse] = []
    for skill_id in dict.fromkeys(body.ids):
        try:
            await service.delete_skill(skill_id)
        except PublicAPIError as error:
            failed.append(
                BulkFailureResponse(id=skill_id, code=error.code, message=error.public_message)
            )
        except KeyError:
            failed.append(BulkFailureResponse(id=skill_id, code="not_found", message="not found"))
        else:
            deleted.append(skill_id)
    return SkillBulkDeleteResponse(deleted=deleted, failed=failed)


@router.delete(
    "/skills/{skill_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_skill(
    skill_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "skill:approve")
    try:
        await service.delete_skill(skill_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    return OperationStatusResponse(status="deleted")


@router.get(
    "/plugins",
    response_model=list[PluginResourceResponse],
    responses=error_responses(401, 403, 422),
)
async def list_plugins(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[PluginResourceResponse]:
    _require(principal, "plugin:read")
    return list(
        _plugins_with_runtime_registered_activation_check(
            request,
            await service.list_plugins(tenant_id=principal.tenant_id),
        )
    )


@router.get(
    "/plugins/policy-summary",
    response_model=list[PluginPolicySummaryResponse],
    responses=error_responses(401, 403, 422),
)
async def list_plugin_policy_summary(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[PluginPolicySummaryResponse]:
    _require(principal, "plugin:read")
    return [
        _plugin_policy_summary(plugin)
        for plugin in await service.list_plugins(tenant_id=principal.tenant_id)
    ]


@router.post(
    "/plugins/policy-review",
    response_model=PluginPolicyReviewResponse,
    responses=error_responses(401, 403, 422),
)
async def review_plugin_policy(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginPolicyReviewResponse:
    _require(principal, "plugin:read")
    review = _plugin_policy_review(
        await service.list_plugins(tenant_id=principal.tenant_id)
    )
    await service.record_audit_event(
        actor=str(principal.user_id),
        action="plugin.policy_review",
        resource="plugin:policy",
        tenant_id=principal.tenant_id,
        details=_plugin_policy_review_audit_details(review),
    )
    return review


@router.get(
    "/plugins/signing-keys",
    response_model=list[PluginSigningKeyResponse],
    responses=error_responses(401, 403, 422),
)
async def list_plugin_signing_keys(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[PluginSigningKeyResponse]:
    _require(principal, "plugin:read")
    return list(await service.list_plugin_signing_keys(tenant_id=principal.tenant_id))


@router.post(
    "/plugins/signing-keys",
    response_model=PluginSigningKeyResponse,
    responses=error_responses(401, 403, 422),
)
async def upsert_plugin_signing_key(
    body: PluginSigningKeyRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginSigningKeyResponse:
    _require(principal, "plugin:approve")
    response = await service.upsert_plugin_signing_key(
        body,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
    )
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.delete(
    "/plugins/signing-keys/{key_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_plugin_signing_key(
    key_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "plugin:approve")
    try:
        await service.delete_plugin_signing_key(
            key_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except (KeyError, ValidationError):
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return OperationStatusResponse(status="deleted")


@router.post(
    "/plugins",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 409, 422),
)
async def upsert_plugin(
    body: PluginResourceRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:write")
    _validate_plugin_resource_config(request, body)
    _validate_plugin_capability_configs(request, body)
    response = await service.upsert_plugin(
        body,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
    )
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.get(
    "/plugins/adapters",
    response_model=list[PluginAdapterDescriptorResponse],
    responses=error_responses(401, 403),
)
async def list_plugin_adapters(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
) -> list[PluginAdapterDescriptorResponse]:
    _require(principal, "plugin:read")
    return list(_plugin_adapter_descriptors(request))


@router.post(
    "/plugins/install",
    response_model=PluginArchiveInstallResponse,
    responses=error_responses(401, 403, 413, 422, 503),
)
async def install_plugin_archive(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginArchiveInstallResponse:
    _require(principal, "plugin:write")
    filename = _safe_plugin_upload_filename(
        _decode_upload_filename_header(
            request.headers.get("x-agent-hub-plugin-filename"),
            request.headers.get("x-agent-hub-plugin-filename-encoding"),
        )
    )
    archive_bytes = await request.body()
    if not archive_bytes:
        raise PublicAPIError(422, "request_validation", "plugin archive is empty")
    content_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    try:
        plugin_manifest = _plugin_archive_manifest_from_archive(archive_bytes)
        package_metadata = _verified_plugin_package_metadata(
            plugin_manifest,
            archive_bytes,
            await service.list_plugin_signing_keys(tenant_id=principal.tenant_id),
        )
    except InvalidSkillPackage as error:
        raise PublicAPIError(
            422,
            "invalid_plugin_package",
            "plugin package is invalid",
            details={"reason": _safe_model_check_detail(str(error))},
        ) from None
    plugin_request = PluginResourceRequest.model_validate(
        plugin_manifest.model_dump(exclude={"package"})
    )
    _validate_plugin_resource_config(request, plugin_request)
    _validate_plugin_capability_configs(request, plugin_request)
    try:
        _validate_runtime_registered_plugin_package(request, plugin_request, package_metadata)
        package_metadata = _stored_plugin_package_metadata(
            request,
            tenant_id=principal.tenant_id,
            plugin_id=plugin_request.id,
            package=package_metadata,
            archive_bytes=archive_bytes,
            content_sha256=content_sha256,
        )
    except InvalidSkillPackage as error:
        raise PublicAPIError(
            422,
            "invalid_plugin_package",
            "plugin package is invalid",
            details={"reason": _safe_model_check_detail(str(error))},
        ) from None
    plugin = await service.upsert_plugin(
        plugin_request,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        source_filename=filename,
        content_sha256=content_sha256,
        package_metadata=package_metadata,
    )
    await service.record_audit_event(
        actor=str(principal.user_id),
        action="plugin.install",
        resource=f"plugin:{plugin.id}",
        tenant_id=principal.tenant_id,
        details={
            "id": plugin.id,
            "filename": filename,
            "content_sha256": content_sha256,
            "capability_count": len(plugin.capabilities),
        },
    )
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return PluginArchiveInstallResponse(
        filename=filename,
        content_sha256=content_sha256,
        plugin=plugin,
    )


@router.post(
    "/plugins/{plugin_id}/package/approve",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def approve_plugin_package(
    plugin_id: str,
    body: PluginPackageApprovalRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:approve")
    try:
        current = await _current_plugin_for_activation_check(
            service,
            plugin_id,
            tenant_id=principal.tenant_id,
        )
        _ensure_runtime_registered_package_can_activate(request, current)
        response = await service.approve_plugin_package(
            plugin_id,
            body,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/package/reject",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def reject_plugin_package(
    plugin_id: str,
    body: PluginPackageApprovalRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:approve")
    try:
        response = await service.reject_plugin_package(
            plugin_id,
            body,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/start",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def start_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:write")
    try:
        current = await _current_plugin_for_activation_check(
            service,
            plugin_id,
            tenant_id=principal.tenant_id,
        )
        _ensure_runtime_registered_package_can_activate(
            request,
            current,
            require_current_eligibility=True,
        )
        response = await service.start_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/enable",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def enable_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:write")
    try:
        current = await _current_plugin_for_activation_check(
            service,
            plugin_id,
            tenant_id=principal.tenant_id,
        )
        _ensure_runtime_registered_package_can_activate(
            request,
            current,
            require_current_eligibility=True,
        )
        response = await service.enable_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/disable",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def disable_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:write")
    try:
        response = await service.disable_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/stop",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def stop_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:write")
    try:
        response = await service.stop_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/reload",
    response_model=PluginResourceResponse,
    responses=error_responses(401, 403, 404, 409, 422),
)
async def reload_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PluginResourceResponse:
    _require(principal, "plugin:write")
    try:
        current = await _current_plugin_for_activation_check(
            service,
            plugin_id,
            tenant_id=principal.tenant_id,
        )
        _ensure_runtime_registered_package_can_activate(
            request,
            current,
            require_current_eligibility=True,
        )
        response = await service.reload_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return response


@router.post(
    "/plugins/{plugin_id}/uninstall",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def uninstall_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "plugin:write")
    try:
        current = await _current_plugin_for_activation_check(
            service,
            plugin_id,
            tenant_id=principal.tenant_id,
        )
        await service.uninstall_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    _cleanup_plugin_package_artifact(request, current, tenant_id=principal.tenant_id)
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return OperationStatusResponse(status="uninstalled")


@router.delete(
    "/plugins/{plugin_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_plugin(
    plugin_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "plugin:write")
    try:
        current = await _current_plugin_for_activation_check(
            service,
            plugin_id,
            tenant_id=principal.tenant_id,
        )
        await service.delete_plugin(
            plugin_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
        )
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    _cleanup_plugin_package_artifact(request, current, tenant_id=principal.tenant_id)
    await _reload_plugin_runtime_config(request, principal.tenant_id)
    return OperationStatusResponse(status="deleted")


@router.get(
    "/capabilities/manifest",
    response_model=CapabilityManifestResponse,
    responses=error_responses(401, 403, 503),
)
async def capability_manifest(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> CapabilityManifestResponse:
    _require(principal, "plugin:read")
    _require(principal, "mcp:read")
    plugin_source = PluginConfigCapabilityManifestSource(
        cast(
            Any,
            _plugins_with_runtime_registered_activation_check(
                request,
                await service.list_plugins(tenant_id=principal.tenant_id),
            ),
        )
    )
    mcp_source = McpConfigCapabilityManifestSource(
        await service.list_mcp_servers(tenant_id=principal.tenant_id)
    )
    return CapabilityManifestResponse.model_validate(
        _runtime_capability_gateway(request).capability_manifest(
            principal.tenant_id,
            extra_sources=(plugin_source, mcp_source),
        )
    )


@router.get(
    "/mcp", response_model=list[McpServerResponse], responses=error_responses(401, 403, 422)
)
async def list_mcp_servers(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[McpServerResponse]:
    _require(principal, "mcp:read")
    return list(await service.list_mcp_servers(tenant_id=principal.tenant_id))


@router.post("/mcp", response_model=McpServerResponse, responses=error_responses(401, 403, 422))
async def upsert_mcp_server(
    body: McpServerRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> McpServerResponse:
    _require(principal, "mcp:write")
    response = await service.upsert_mcp_server(body)
    await _reload_mcp_runtime_config(request, principal.tenant_id)
    return response


@router.delete(
    "/mcp/{server_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_mcp_server(
    server_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "mcp:write")
    try:
        await service.delete_mcp_server(server_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    await _reload_mcp_runtime_config(request, principal.tenant_id)
    return OperationStatusResponse(status="deleted")


async def _reload_mcp_runtime_config(request: Request, tenant_id: UUID) -> None:
    callback = getattr(request.app.state, "reload_mcp_runtime_config", None)
    if not callable(callback):
        return
    try:
        result = callback(tenant_id)
        if inspect.isawaitable(result):
            await result
    except Exception as error:  # noqa: BLE001 - MCP reload must not break config writes.
        _LOGGER.warning(
            "mcp runtime reload failed tenant_id=%s error_type=%s",
            tenant_id,
            type(error).__name__,
        )


async def _reload_plugin_runtime_config(request: Request, tenant_id: UUID) -> None:
    callback = getattr(request.app.state, "reload_plugin_runtime_config", None)
    if not callable(callback):
        return
    try:
        result = callback(tenant_id)
        if inspect.isawaitable(result):
            await result
    except Exception as error:  # noqa: BLE001 - plugin reload must not break config writes.
        _LOGGER.warning(
            "plugin runtime reload failed tenant_id=%s error_type=%s",
            tenant_id,
            type(error).__name__,
        )


def _channels_with_runtime_status(
    channels: Iterable[ChannelStatusResponse], request: Request
) -> list[ChannelStatusResponse]:
    enriched: list[ChannelStatusResponse] = []
    for channel in channels:
        if channel.id != "feishu" or "websocket" not in channel.transports:
            enriched.append(channel)
            continue
        enriched.append(channel.model_copy(update={"runtime": _feishu_runtime_status(request)}))
    return enriched


def _feishu_runtime_status(request: Request) -> ChannelRuntimeStatusResponse:
    connector = getattr(request.app.state, "feishu_websocket_connector", None)
    task = getattr(request.app.state, "feishu_websocket_task", None)
    metrics = getattr(connector, "metrics", None)
    task_done = getattr(task, "done", None)
    task_running = callable(task_done) and not bool(task_done())
    ready = bool(getattr(connector, "ready", False))
    if connector is None:
        status = "not_started"
    elif task_running and ready:
        status = "running"
    elif task_running:
        status = "starting"
    else:
        status = "stopped"
    return ChannelRuntimeStatusResponse(
        status=status,
        ready=ready,
        connection_attempts=_runtime_metric_int(metrics, "connection_attempts"),
        reconnects=_runtime_metric_int(metrics, "reconnects"),
        received_events=_runtime_metric_int(metrics, "received_events"),
        submitted_messages=_runtime_metric_int(metrics, "submitted_messages"),
        ignored_events=_runtime_metric_int(metrics, "ignored_events"),
        failures=_runtime_metric_int(metrics, "failures"),
        last_error_type=_runtime_metric_str(metrics, "last_error_type"),
        last_error_message=_runtime_metric_str(metrics, "last_error_message"),
    )


def _runtime_metric_int(metrics: object | None, name: str) -> int:
    value = getattr(metrics, name, 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _runtime_metric_str(metrics: object | None, name: str) -> str | None:
    value = getattr(metrics, name, None)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


@router.get(
    "/channels",
    response_model=list[ChannelStatusResponse],
    responses=error_responses(401, 403, 422),
)
async def list_channels(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    request: Request,
) -> list[ChannelStatusResponse]:
    _require(principal, "config:read")
    return _channels_with_runtime_status(await service.list_channels(), request)


@router.post(
    "/channels/{channel_id}/config",
    response_model=ChannelConfigSaveResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def save_channel_config(
    channel_id: str,
    body: ChannelConfigRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    request: Request,
) -> ChannelConfigSaveResponse:
    _require(principal, "config:write")
    response = await service.save_channel_config(channel_id, body)
    await _refresh_channel_runtime_config(request, service)
    return response


@router.delete(
    "/channels/{channel_id}/config",
    response_model=ChannelConfigSaveResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def clear_channel_config(
    channel_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    request: Request,
) -> ChannelConfigSaveResponse:
    _require(principal, "config:write")
    response = await service.clear_channel_config(channel_id, actor=str(principal.user_id))
    await _refresh_channel_runtime_config(request, service)
    return response


async def _refresh_channel_runtime_config(
    request: Request,
    service: AdminResourceService,
) -> None:
    runtime_config = await service.channel_runtime_config()
    request.app.state.channel_runtime_config = runtime_config
    refresh = getattr(request.app.state, "refresh_channel_runtime_config", None)
    if not callable(refresh):
        return
    result = refresh(runtime_config)
    if inspect.isawaitable(result):
        await result


@router.get(
    "/memory", response_model=list[MemoryRecordResponse], responses=error_responses(401, 403, 422)
)
async def list_memory(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[MemoryRecordResponse]:
    _require(principal, "memory:read")
    return list(await service.list_memory())


@router.post(
    "/memory", response_model=MemoryRecordResponse, responses=error_responses(401, 403, 422)
)
async def create_memory(
    body: MemoryCreateRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    return await service.create_memory(body)


@router.patch(
    "/memory/{memory_id}",
    response_model=MemoryRecordResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def update_memory(
    memory_id: str,
    body: MemoryRecordRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    try:
        return await service.update_memory(memory_id, body)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None


@router.post(
    "/memory/{memory_id}/lock",
    response_model=MemoryRecordResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def lock_memory(
    memory_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    current = await _admin_memory_or_404(service, memory_id)
    return await service.update_memory(memory_id, _memory_request_from_response(current, locked=True))


@router.post(
    "/memory/{memory_id}/unlock",
    response_model=MemoryRecordResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def unlock_memory(
    memory_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    current = await _admin_memory_or_404(service, memory_id)
    return await service.update_memory(memory_id, _memory_request_from_response(current, locked=False))


@router.delete(
    "/memory/{memory_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def forget_memory(
    memory_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "memory:write")
    try:
        await service.forget_memory(memory_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    return OperationStatusResponse(status="forgotten")


async def _admin_memory_or_404(
    service: AdminResourceService, memory_id: str
) -> MemoryRecordResponse:
    for item in await service.list_memory():
        if item.id == memory_id:
            return item
    raise PublicAPIError(404, "not_found", "not found")


def _memory_request_from_response(
    current: MemoryRecordResponse,
    **updates: object,
) -> MemoryRecordRequest:
    data = current.model_dump()
    data.pop("id", None)
    data.pop("scope", None)
    data.update(updates)
    return MemoryRecordRequest.model_validate(data)


@router.get(
    "/audit", response_model=list[AuditEventResponse], responses=error_responses(401, 403, 422)
)
async def list_audit_events(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    action: str | None = None,
) -> list[AuditEventResponse]:
    _require(principal, "audit:read")
    return list(await service.list_audit_events(action))


@router.get(
    "/logs", response_model=list[LogEntryResponse], responses=error_responses(401, 403, 422)
)
async def list_logs(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    category: str | None = None,
) -> list[LogEntryResponse]:
    _require(principal, "audit:read")
    if category is not None and category not in {
        "audit",
        "model_error",
        "mode_error",
        "feature_error",
        "agent_error",
        "channel_error",
    }:
        raise PublicAPIError(422, "request_validation", "invalid log category")
    return list(await service.list_logs(category))


@router.get(
    "/hermes", response_model=list[HermesInsightResponse], responses=error_responses(401, 403, 422)
)
async def list_hermes_insights(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[HermesInsightResponse]:
    _require(principal, "hermes:read")
    return list(await service.list_hermes_insights())


@router.post(
    "/hermes/feedback",
    response_model=HermesInsightResponse,
    responses=error_responses(401, 403, 422),
)
async def record_hermes_feedback(
    body: HermesFeedbackRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesInsightResponse:
    _require(principal, "hermes:write")
    try:
        return await service.record_hermes_feedback(body)
    except ValueError:
        raise PublicAPIError(
            422,
            "sensitive_content",
            "feedback contains sensitive content",
        ) from None


@router.post(
    "/hermes/recommend",
    response_model=HermesRecommendationResponse,
    responses=error_responses(401, 403, 422),
)
async def recommend_with_hermes(
    body: HermesRecommendationRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesRecommendationResponse:
    _require(principal, "hermes:read")
    return await service.recommend_with_hermes(body)


@router.post(
    "/hermes/bulk-confirm",
    response_model=HermesBulkConfirmResponse,
    responses=error_responses(401, 403, 422),
)
async def bulk_confirm_hermes_insights(
    body: HermesBulkConfirmRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesBulkConfirmResponse:
    _require(principal, "hermes:write")
    confirmed: list[HermesInsightResponse] = []
    failed: list[BulkFailureResponse] = []
    for insight_id in body.ids:
        try:
            confirmed.append(await service.confirm_hermes_insight(insight_id))
        except KeyError:
            failed.append(
                BulkFailureResponse(
                    id=insight_id,
                    code="hermes_not_found",
                    message="Hermes learning record was not found",
                )
            )
    return HermesBulkConfirmResponse(confirmed=confirmed, failed=failed)


@router.post(
    "/hermes/bulk-delete",
    response_model=HermesBulkDeleteResponse,
    responses=error_responses(401, 403, 422),
)
async def bulk_delete_hermes_insights(
    body: HermesBulkDeleteRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesBulkDeleteResponse:
    _require(principal, "hermes:write")
    deleted: list[str] = []
    failed: list[BulkFailureResponse] = []
    for insight_id in body.ids:
        try:
            await service.delete_hermes_insight(insight_id)
        except KeyError:
            failed.append(
                BulkFailureResponse(
                    id=insight_id,
                    code="hermes_not_found",
                    message="Hermes learning record was not found",
                )
            )
        else:
            deleted.append(insight_id)
    return HermesBulkDeleteResponse(deleted=deleted, failed=failed)


@router.get(
    "/hermes/{insight_id}",
    response_model=HermesInsightResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def get_hermes_insight(
    insight_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesInsightResponse:
    _require(principal, "hermes:read")
    try:
        return await service.get_hermes_insight(insight_id)
    except KeyError:
        raise PublicAPIError(
            404, "hermes_not_found", "Hermes learning record was not found"
        ) from None


@router.post(
    "/hermes/{insight_id}/confirm",
    response_model=HermesInsightResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def confirm_hermes_insight(
    insight_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesInsightResponse:
    _require(principal, "hermes:write")
    try:
        return await service.confirm_hermes_insight(insight_id)
    except KeyError:
        raise PublicAPIError(
            404, "hermes_not_found", "Hermes learning record was not found"
        ) from None


@router.delete(
    "/hermes/{insight_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_hermes_insight(
    insight_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "hermes:write")
    try:
        await service.delete_hermes_insight(insight_id)
    except KeyError:
        raise PublicAPIError(
            404, "hermes_not_found", "Hermes learning record was not found"
        ) from None
    return OperationStatusResponse(status="deleted")


__all__ = ["InMemoryAdminResourceService", "PersistentAdminResourceService", "router"]
