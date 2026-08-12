from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import BASE_ERROR_RESPONSES, PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService, ConfigValidationError
from agent_hub.db.models import AdminResourceRow
from agent_hub.domain.runs import RunStatus
from agent_hub.models.gateway import ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient, ModelTransportError
from agent_hub.models.types import Deployment, ModelCapability, ModelMessage, ModelRequest
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRecord, RunRepository
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.failure_reason import is_legacy_generic_failure_reason
from agent_hub.security.secrets import SecretService, SecretValidationError
from agent_hub.skills.package import InvalidSkillPackage
from agent_hub.skills.scanner import SkillScanner

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], responses=BASE_ERROR_RESPONSES)
_LOGGER = logging.getLogger(__name__)
_MODEL_CHECK_STATUS_RE = re.compile(r"\bstatus[=_: ](?P<status>[1-5][0-9]{2})\b")
_MODEL_CHECK_HINT = "检查 API Key 是否有效、API Base 是否可从服务器访问、模型名是否属于该服务商账号。"
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
    conversation_id: str | None = None
    queue_wait_ms: int = Field(ge=0)
    capacity_wait_ms: int = Field(ge=0)
    cost_usd: str


class RunArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    title: str
    text: str | None = None


class RunEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    kind: str
    message: str
    created_at: datetime
    actor: str | None = None
    participants: list[str] = Field(default_factory=list)
    tool_name: str | None = None
    step_id: str | None = None
    action: str | None = None
    decision: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    artifact: RunArtifactResponse | None = None


class RunDetailResponse(RunListItem):
    request: str
    events: list[RunEventResponse]
    artifacts: list[RunArtifactResponse]
    explicit_details: dict[str, str]
    decision_token: str | None = None
    temporary_agent_proposal: dict[str, JsonValue] | None = None


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

    ids: list[UUID] = Field(min_length=1, max_length=100)


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


class SkillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: str
    scan_diff: list[str]
    requested_permissions: list[str]


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


class ChannelStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: str
    transports: list[str]
    webhook_path: str | None = None
    public_webhook_url: str | None = None
    missing: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


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
            "FEISHU_VERIFICATION_TOKEN",
            "FEISHU_ENCRYPT_KEY",
            "AGENT_HUB_PUBLIC_URL",
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


class MemoryCreateRequest(MemoryRecordRequest):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    scope: str = Field(default="tenant", min_length=1, max_length=128)


class MemoryRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    value: str


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    actor: str
    action: str
    resource: str
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
    allow_main_agent_override: bool = False
    allow_temporary_agents: bool = False
    temporary_agent_policy: str = Field(
        default="主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
        max_length=10_000,
    )
    channel_entry: str = Field(default="web", max_length=64)
    attachment_retention_days: int = Field(default=7, ge=1, le=365)
    attachment_max_mb: int = Field(default=25, ge=1, le=200)


class SystemSettingsResponse(SystemSettingsRequest):
    pass


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
    outcome: str = Field(pattern=r"^(success|failure|neutral)$")
    lesson: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    weight: int = Field(default=1, ge=1, le=10)


class HermesInsightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    outcome: str
    lesson: str
    summary: str
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


class HermesBulkConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if re.fullmatch(r"hermes[-_][a-f0-9-]{1,64}", item) is None:
                raise ValueError("Hermes ids must be safe learning identifiers")
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


class HermesBulkConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: list[HermesInsightResponse]
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

    async def upsert_workflow(self, request: WorkflowResourceRequest) -> WorkflowResourceResponse: ...

    async def delete_workflow(self, workflow_id: str) -> None: ...

    async def get_settings(self) -> SystemSettingsResponse: ...

    async def update_settings(self, request: SystemSettingsRequest) -> SystemSettingsResponse: ...

    async def get_main_agent_config(self) -> MainAgentConfigResponse: ...

    async def update_main_agent_config(
        self, request: MainAgentConfigRequest
    ) -> MainAgentConfigResponse: ...

    async def list_runs(self) -> tuple[RunListItem, ...]: ...

    async def get_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def get_conversation(self, conversation_id: str) -> ConversationResponse: ...

    async def pause_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def resume_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def delete_run(self, run_id: UUID) -> RunDeleteResponse: ...

    async def list_skills(self) -> tuple[SkillResponse, ...]: ...

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse: ...

    async def upload_skill_archive(self, filename: str, archive_bytes: bytes) -> SkillResponse: ...

    async def approve_skill(self, skill_id: str) -> SkillResponse: ...

    async def delete_skill(self, skill_id: str) -> None: ...

    async def list_mcp_servers(self) -> tuple[McpServerResponse, ...]: ...

    async def upsert_mcp_server(self, request: McpServerRequest) -> McpServerResponse: ...

    async def delete_mcp_server(self, server_id: str) -> None: ...

    async def list_channels(self) -> tuple[ChannelStatusResponse, ...]: ...

    async def save_channel_config(
        self, channel_id: str, request: ChannelConfigRequest
    ) -> ChannelConfigSaveResponse: ...

    async def channel_runtime_config(self) -> dict[str, str]: ...

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]: ...

    async def create_memory(self, request: MemoryCreateRequest) -> MemoryRecordResponse: ...

    async def update_memory(self, memory_id: str, request: MemoryRecordRequest) -> MemoryRecordResponse: ...

    async def forget_memory(self, memory_id: str) -> None: ...

    async def list_audit_events(self, action: str | None = None) -> tuple[AuditEventResponse, ...]: ...

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
    ) -> AuditEventResponse: ...

    async def list_logs(self, category: str | None = None) -> tuple[LogEntryResponse, ...]: ...

    async def list_hermes_insights(self) -> tuple[HermesInsightResponse, ...]: ...

    async def get_hermes_insight(self, insight_id: str) -> HermesInsightResponse: ...

    async def confirm_hermes_insight(self, insight_id: str) -> HermesInsightResponse: ...

    async def record_hermes_feedback(
        self, request: HermesFeedbackRequest
    ) -> HermesInsightResponse: ...

    async def recommend_with_hermes(
        self, request: HermesRecommendationRequest
    ) -> HermesRecommendationResponse: ...


@dataclass(slots=True)
class InMemoryAdminResourceService:
    models: dict[UUID, ModelDeploymentResponse] = field(default_factory=dict)
    secrets: dict[str, SecretReferenceResponse] = field(default_factory=dict)
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
    mcp_servers: dict[str, McpServerResponse] = field(default_factory=dict)
    channel_config: dict[str, dict[str, str]] = field(default_factory=dict)
    memory: dict[str, MemoryRecordResponse] = field(default_factory=dict)
    audit_events: list[AuditEventResponse] = field(default_factory=list)
    logs: list[LogEntryResponse] = field(default_factory=list)
    hermes_insights: dict[str, HermesInsightResponse] = field(default_factory=dict)

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
                outcome="success",
                lesson="Use dispatch mode when the request has clear deliverables and separable steps.",
                summary=_hermes_feedback_summary(
                    outcome="success",
                    lesson="Use dispatch mode when the request has clear deliverables and separable steps.",
                    tags=["dispatch", "planning", "clear-task"],
                    weight=3,
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
            effective_slots=max(0, request.max_concurrency - request.reserved_capacity),
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
            effective_slots=max(0, request.max_concurrency - request.reserved_capacity),
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
        return response

    async def get_secret(self, ref: str) -> SecretReferenceResponse:
        return self.secrets[ref]

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
        response = SystemSettingsResponse(**request.model_dump())
        self.settings = response
        return response

    async def list_runs(self) -> tuple[RunListItem, ...]:
        return tuple(
            RunListItem(
                id=run.id,
                status=run.status,
                mode=run.mode,
                conversation_id=run.explicit_details.get("conversation_id"),
                queue_wait_ms=run.queue_wait_ms,
                capacity_wait_ms=run.capacity_wait_ms,
                cost_usd=run.cost_usd,
            )
            for run in self.runs.values()
        )

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        return self.runs[run_id]

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

    async def list_skills(self) -> tuple[SkillResponse, ...]:
        return tuple(self.skills.values())

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse:
        skill_id = request.filename.rsplit(".", 1)[0].lower().replace("_", "-")
        response = SkillResponse(
            id=skill_id,
            name=skill_id,
            status="quarantined",
            scan_diff=["added SKILL.md", "no dangerous operations detected"],
            requested_permissions=["filesystem:read"],
        )
        self.skills[response.id] = response
        return response

    async def upload_skill_archive(self, filename: str, archive_bytes: bytes) -> SkillResponse:
        try:
            inspection = SkillScanner().scan(archive_bytes).inspection
        except InvalidSkillPackage:
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={"feature": "skills", "filename": filename},
            )
            raise
        response = SkillResponse(
            id=f"skill_{uuid4().hex}",
            name=inspection.manifest.name,
            status="scanned",
            scan_diff=[
                f"package {filename} scanned",
                f"entry point: {inspection.manifest.entry_point}",
                f"content sha256: {inspection.content_sha256}",
            ],
            requested_permissions=list(inspection.requested_capabilities),
        )
        self.skills[response.id] = response
        return response

    async def approve_skill(self, skill_id: str) -> SkillResponse:
        current = self.skills[skill_id]
        updated = current.model_copy(update={"status": "enabled"})
        self.skills[skill_id] = updated
        return updated

    async def delete_skill(self, skill_id: str) -> None:
        del self.skills[skill_id]

    async def list_mcp_servers(self) -> tuple[McpServerResponse, ...]:
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
            raise PublicAPIError(422, "request_validation", "at least one channel field is required")
        existing = dict(self.channel_config.get(channel_id, {}))
        existing.update(cleaned)
        self.channel_config[channel_id] = existing
        return ChannelConfigSaveResponse(
            id=channel_id,
            saved=_ordered_channel_saved_fields(definition, cleaned),
            status=_channel_status_from_definition(definition, self.channel_config),
        )

    async def channel_runtime_config(self) -> dict[str, str]:
        return _flatten_channel_config(self.channel_config)

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]:
        return tuple(self.memory.values())

    async def create_memory(self, request: MemoryCreateRequest) -> MemoryRecordResponse:
        response = MemoryRecordResponse(id=request.id, scope=request.scope, value=request.value)
        self.memory[response.id] = response
        return response

    async def update_memory(
        self, memory_id: str, request: MemoryRecordRequest
    ) -> MemoryRecordResponse:
        current = self.memory[memory_id]
        updated = current.model_copy(update={"value": request.value})
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
    ) -> AuditEventResponse:
        del details
        event = AuditEventResponse(
            id=f"audit_{uuid4().hex}",
            actor=actor,
            action=action,
            resource=resource,
            created_at=datetime.now(UTC),
        )
        self.audit_events.append(event)
        return event

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
            *(_mode_error_log_from_run(run) for run in self.runs.values() if run.status == "failed"),
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

    async def record_hermes_feedback(
        self, request: HermesFeedbackRequest
    ) -> HermesInsightResponse:
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
            if skill.lower() in normalized_task or skill.lower().replace("-", " ") in normalized_task
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
        return RunDeleteResponse(id=run_id, deleted=True)

    async def _run_list_item(self, record: RunRecord) -> RunListItem:
        assert self._run_repository is not None
        return RunListItem(
            id=record.id,
            status=record.status.value,
            mode="auto" if record.mode is None else record.mode.value,
            conversation_id=_routing_details(record.routing_decision).get("conversation_id"),
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
            request=record.request,
            events=[_admin_run_event(event) for event in events],
            artifacts=[_admin_run_artifact(artifact) for artifact in artifacts],
            explicit_details={
                "source": "database",
                "version": str(record.version),
                **_routing_details(record.routing_decision),
            },
            decision_token=_waiting_mode_decision_token(record),
            temporary_agent_proposal=_temporary_agent_proposal(record.routing_decision),
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
            checked_deployment = PlatformConfig.model_validate(document).models[
                request.logical_model
            ].deployments[deployment_index]
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
            checked_deployment = PlatformConfig.model_validate(document).models[
                request.logical_model
            ].deployments[updated_index]
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
                if isinstance(raw_definition, dict) and raw_definition.get("fallback_model") == target_logical_model:
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
        if not await self._upsert_admin_payload("workflow", response.id, response.model_dump(mode="json")):
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
        return SystemSettingsResponse.model_validate(payload)

    async def update_settings(self, request: SystemSettingsRequest) -> SystemSettingsResponse:
        response = SystemSettingsResponse(**request.model_dump())
        if not await self._upsert_admin_payload("setting", "system", response.model_dump(mode="json")):
            return await super().update_settings(request)
        await self._record_audit("settings.update", "settings:system", {"default_mode": response.default_mode})
        return response

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

    async def list_skills(self) -> tuple[SkillResponse, ...]:
        resources = await self._list_admin_payloads("skill")
        if resources is None:
            return await super().list_skills()
        return tuple(SkillResponse.model_validate(payload) for payload in resources)

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse:
        skill_id = f"skill_{uuid4().hex}"
        response = SkillResponse(
            id=skill_id,
            name=request.filename,
            status="quarantined",
            scan_diff=["metadata recorded; package scan requires ZIP upload endpoint"],
            requested_permissions=["filesystem:read"],
        )
        if not await self._upsert_admin_payload("skill", skill_id, response.model_dump(mode="json")):
            return await super().upload_skill(request)
        await self._record_audit("skill.upload", f"skill:{skill_id}", {"filename": request.filename})
        return response

    async def upload_skill_archive(self, filename: str, archive_bytes: bytes) -> SkillResponse:
        try:
            scan_report = SkillScanner().scan(archive_bytes)
        except InvalidSkillPackage:
            await self.record_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={"feature": "skills", "filename": _safe_model_check_detail(filename)},
            )
            raise PublicAPIError(422, "invalid_skill_package", "skill package is invalid") from None
        skill_id = f"skill_{uuid4().hex}"
        try:
            archive_path = self._skill_archive_path(skill_id)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(archive_bytes)
        except OSError:
            await self.record_log(
                category="feature_error",
                level="error",
                title="主要功能运行错误",
                message="skill store is unavailable",
                source="skills.upload",
                details={"feature": "skills", "filename": _safe_model_check_detail(filename)},
            )
            raise PublicAPIError(503, "skill_store_unavailable", "skill store is unavailable") from None
        inspection = scan_report.inspection
        response = SkillResponse(
            id=skill_id,
            name=inspection.manifest.name,
            status="scanned",
            scan_diff=[
                f"package {filename} scanned",
                f"entry point: {inspection.manifest.entry_point}",
                f"content sha256: {inspection.content_sha256}",
            ],
            requested_permissions=list(inspection.requested_capabilities),
        )
        if not await self._upsert_admin_payload("skill", skill_id, response.model_dump(mode="json")):
            return await super().upload_skill_archive(filename, archive_bytes)
        await self._record_audit("skill.upload", f"skill:{skill_id}", {"filename": filename})
        return response

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

    def _skill_archive_path(self, skill_id: str) -> Path:
        if not _is_safe_admin_identifier(skill_id):
            raise PublicAPIError(422, "request_validation", "invalid skill id")
        root = self._skill_store_dir.resolve()
        target = (root / str(self._tenant_id) / f"{skill_id}.zip").resolve()
        if not target.is_relative_to(root):
            raise PublicAPIError(422, "request_validation", "invalid skill id")
        return target

    async def list_mcp_servers(self) -> tuple[McpServerResponse, ...]:
        resources = await self._list_admin_payloads("mcp")
        if resources is None:
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
        if not await self._upsert_admin_payload("mcp", response.id, response.model_dump(mode="json")):
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
            raise PublicAPIError(422, "request_validation", "at least one channel field is required")
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
        response = MemoryRecordResponse(id=request.id, scope=request.scope, value=request.value)
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
        response = MemoryRecordResponse(id=current.id, scope=current.scope, value=request.value)
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
        if payload:
            return _hermes_response_from_payload(payload)
        return await super().get_hermes_insight(insight_id)

    async def confirm_hermes_insight(self, insight_id: str) -> HermesInsightResponse:
        payload = await self._get_admin_payload("hermes", insight_id)
        if not payload:
            return await super().confirm_hermes_insight(insight_id)
        payload["confirmed_at"] = datetime.now(UTC).isoformat()
        if not await self._upsert_admin_payload("hermes", insight_id, payload):
            return await super().confirm_hermes_insight(insight_id)
        await self._record_audit("hermes.confirm", f"hermes:{insight_id}", {"id": insight_id})
        return _hermes_response_from_payload(payload)

    async def record_hermes_feedback(
        self, request: HermesFeedbackRequest
    ) -> HermesInsightResponse:
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
            outcome=request.outcome,
            lesson=request.lesson,
            summary=_hermes_feedback_summary(
                outcome=request.outcome,
                lesson=request.lesson,
                tags=request.tags,
                weight=request.weight,
            ),
            run_id=request.run_id,
            conversation_id=request.conversation_id,
            confirmed_at=None,
            tags=request.tags,
            weight=request.weight,
            created_at=datetime.now(UTC),
        )
        if not await self._upsert_admin_payload("hermes", insight_id, response.model_dump(mode="json")):
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
                recommended_mode=request.mode_candidates[0] if request.mode_candidates else "dispatch",
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

    async def _list_admin_payloads(self, kind: str) -> list[dict[str, object]] | None:
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
            return [dict(row.payload) for row in rows]

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

    async def _get_admin_payload(self, kind: str, resource_id: str) -> dict[str, object] | None:
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == self._tenant_id)
                    .where(AdminResourceRow.kind == kind)
                    .where(AdminResourceRow.resource_id == resource_id)
                )
            ).scalar_one_or_none()
            return {} if row is None else dict(row.payload)

    async def _upsert_admin_payload(
        self, kind: str, resource_id: str, payload: dict[str, object]
    ) -> bool:
        if self._session_factory is None:
            return False
        statement = (
            insert(AdminResourceRow)
            .values(
                id=uuid4(),
                tenant_id=self._tenant_id,
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

    async def _delete_admin_payload(self, kind: str, resource_id: str) -> bool | None:
        if self._session_factory is None:
            return None
        existing = await self._get_admin_payload(kind, resource_id)
        if not existing:
            return False
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == self._tenant_id)
                .where(AdminResourceRow.kind == kind)
                .where(AdminResourceRow.resource_id == resource_id)
            )
        return True

    async def _record_audit(
        self, action: str, resource: str, payload: dict[str, object] | None = None
    ) -> None:
        if self._session_factory is None:
            return
        event = AuditEventResponse(
            id=f"audit_{uuid4().hex}",
            actor=str(self._actor_id),
            action=action,
            resource=resource,
            created_at=datetime.now(UTC),
        )
        audit_payload = event.model_dump(mode="json")
        if payload:
            audit_payload["details"] = payload
        await self._upsert_admin_payload("audit", event.id, audit_payload)

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
    ) -> AuditEventResponse:
        event = AuditEventResponse(
            id=f"audit_{uuid4().hex}",
            actor=actor,
            action=action,
            resource=resource,
            created_at=datetime.now(UTC),
        )
        audit_payload = event.model_dump(mode="json")
        if details:
            audit_payload["details"] = details
        await self._upsert_admin_payload("audit", event.id, audit_payload)
        return event

    async def _verify_model_availability(
        self,
        deployment: Deployment,
        *,
        source: str = "models.create",
    ) -> None:
        if ModelCapability.TEXT not in deployment.capabilities:
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
        parsed = PlatformConfig.model_validate(
            {"models": {logical_model: {"deployments": [deployment]}}, "agents": []}
        ).models[logical_model].deployments[0]
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
            effective_slots=max(0, parsed.max_concurrency - parsed.reserved_slots),
            saturation_policy="queue_first_then_fallback",
        )


def _service(request: Request) -> AdminResourceService:
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(AdminResourceService, service)


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
        for marker in ("api_key", "authorization:", "bearer ", "password", "secret", "sk-")
    )


def _is_safe_admin_identifier(value: str) -> bool:
    return (
        1 <= len(value) <= 128
        and value[0].isalnum()
        and all(character.isalnum() or character in "_-" for character in value)
    )


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


def _normalize_model_request_api_base(request: ModelDeploymentRequest) -> ModelDeploymentRequest:
    normalized = _normalized_model_api_base(request.api_protocol, request.api_base)
    if normalized == request.api_base:
        return request
    return request.model_copy(update={"api_base": normalized})


def _normalize_main_agent_config(request: MainAgentConfigRequest) -> MainAgentConfigRequest:
    if request.model is None:
        return request
    normalized = _normalized_model_api_base(request.model.api_protocol, request.model.api_base)
    if normalized == request.model.api_base:
        return request
    return request.model_copy(update={"model": request.model.model_copy(update={"api_base": normalized})})


def _main_agent_model_deployment(model: MainAgentModelConfig) -> Deployment:
    return Deployment(
        id="main_agent_1",
        logical_model="main_agent",
        provider_model=f"{model.provider}/{model.upstream_model}",
        request_model=model.upstream_model,
        api_base=model.api_base,
        secret_ref=model.credential_ref,
        quota_scope_id="main-agent",
        max_concurrency=1,
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
    if _is_dashscope_provider(normalized_provider, normalized_base) and status_code in {"401", "403"}:
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
    return provider in {"qwen", "dashscope", "aliyun", "alibaba"} or "dashscope.aliyuncs.com" in api_base


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


def _safe_debug_event(event: RunEventResponse) -> RunEventResponse:
    return event.model_copy(update={"payload": _event_payload(event.payload)})


def _run_debug_artifact(artifact: RunArtifactResponse) -> RunDebugArtifactResponse:
    preview = _safe_debug_preview(artifact.text)
    return RunDebugArtifactResponse(
        id=artifact.id,
        kind=artifact.kind,
        title=artifact.title,
        has_text=preview is not None,
        text_preview=preview,
    )


def _safe_debug_preview(value: str | None, *, max_chars: int = _RUN_DEBUG_PREVIEW_CHARS) -> str | None:
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
    return AuditEventResponse(
        id=str(payload.get("id", "")),
        actor=str(payload.get("actor", "system")),
        action=str(payload.get("action", "unknown")),
        resource=str(payload.get("resource", "unknown")),
        created_at=_datetime_from_json(created_at),
    )


def _hermes_response_from_payload(payload: dict[str, object]) -> HermesInsightResponse:
    tags = payload.get("tags")
    raw_weight = payload.get("weight", 1)
    normalized_tags = [str(tag) for tag in tags] if isinstance(tags, list) else []
    weight = raw_weight if type(raw_weight) is int else 1
    outcome = str(payload.get("outcome", "neutral"))
    lesson = str(payload.get("lesson", ""))
    raw_summary = payload.get("summary")
    run_id = _uuid_from_json(payload.get("run_id"))
    raw_conversation_id = payload.get("conversation_id")
    raw_confirmed_at = payload.get("confirmed_at")
    return HermesInsightResponse(
        id=str(payload.get("id", "")),
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
    missing = [
        name
        for name in definition.required_env
        if not _channel_config_value(name, definition.id, config)
    ]
    public_webhook_url = (
        f"{public_url}{definition.webhook_path}"
        if public_url and definition.webhook_path is not None
        else None
    )
    transports = list(definition.transports)
    if definition.id == "feishu":
        configured_transport = _channel_config_value("FEISHU_TRANSPORT", definition.id, config)
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
        notes=list(definition.notes),
    )


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
        allowed.add("FEISHU_TRANSPORT")
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
    message = event.get("reason") or event.get("message") or kind
    payload = event.get("payload")
    artifact = event.get("artifact")
    return RunEventResponse(
        sequence=sequence if type(sequence) is int else 1,
        kind=kind if type(kind) is str else "event",
        message=message if type(message) is str else "event recorded",
        created_at=datetime.now(UTC),
        actor=_optional_event_string(event.get("actor")),
        participants=_event_string_list(event.get("participants")),
        tool_name=_optional_event_string(event.get("tool_name")),
        step_id=_optional_event_string(event.get("step_id")),
        action=_optional_event_string(event.get("action")),
        decision=_optional_event_string(event.get("decision")),
        payload=_event_payload(payload),
        artifact=_admin_run_artifact(artifact) if isinstance(artifact, dict) else None,
    )


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
    }
)


def _optional_event_string(value: object) -> str | None:
    return value if type(value) is str and value else None


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


def _safe_event_detail(value: object, *, key: str | None = None, depth: int = 0) -> JsonValue:
    if key is not None and _is_sensitive_event_detail_key(key):
        return "[redacted]"
    if depth >= 6:
        return "[truncated]"
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
    return any(sensitive in lowered for sensitive in _SENSITIVE_EVENT_DETAIL_KEYS)


def _admin_run_artifact(artifact: dict[str, object]) -> RunArtifactResponse:
    artifact_id = artifact.get("id")
    artifact_type = artifact.get("type")
    producer = artifact.get("producer")
    content = artifact.get("content")
    title = producer if type(producer) is str and producer else artifact_id
    text = _artifact_text(content)
    return RunArtifactResponse(
        id=artifact_id if type(artifact_id) is str and artifact_id else "artifact",
        kind=artifact_type if type(artifact_type) is str and artifact_type else "artifact",
        title=title if type(title) is str and title else "artifact",
        text=text,
    )


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


def _routing_details(routing_decision: dict[str, object] | None) -> dict[str, str]:
    if not routing_decision:
        return {}
    details: dict[str, str] = {}
    workflow_id = routing_decision.get("workflow_id")
    if isinstance(workflow_id, str) and workflow_id:
        details["workflow_id"] = workflow_id
    conversation_id = routing_decision.get("conversation_id")
    if isinstance(conversation_id, str) and conversation_id:
        details["conversation_id"] = conversation_id
    reference_conversation_id = routing_decision.get("reference_conversation_id")
    if isinstance(reference_conversation_id, str) and reference_conversation_id:
        details["reference_conversation_id"] = reference_conversation_id
    adjustment_policy = routing_decision.get("workflow_adjustment_policy")
    if adjustment_policy == "ask_before_apply":
        details["workflow_adjustment_policy"] = "ask_before_apply"
    elif adjustment_policy == "strict_preset":
        details["workflow_adjustment_policy"] = "strict_preset"
    selected_agent_ids = routing_decision.get("selected_agent_ids")
    if isinstance(selected_agent_ids, list):
        safe_ids = [item for item in selected_agent_ids if isinstance(item, str) and item]
        if safe_ids:
            details["selected_agent_ids"] = ", ".join(safe_ids)
    reason = routing_decision.get("reason")
    if isinstance(reason, str) and reason:
        details["routing_reason"] = reason
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


def _waiting_mode_decision_token(record: RunRecord) -> str | None:
    if (
        record.status not in {RunStatus.WAITING_USER_MODE, RunStatus.WAITING_APPROVAL}
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


@router.get("/models", response_model=list[ModelDeploymentResponse], responses=error_responses(401, 403, 422))
async def list_models(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[ModelDeploymentResponse]:
    _require(principal, "config:read")
    return list(await service.list_models())


@router.post("/models", response_model=ModelDeploymentResponse, responses=error_responses(401, 403, 422))
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


@router.post("/models/probe", response_model=ProbeResponse, responses=error_responses(401, 403, 422))
async def probe_model(
    body: ProbeRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> ProbeResponse:
    _require(principal, "config:read")
    return await service.probe_concurrency(body)


@router.post("/secrets", response_model=SecretReferenceResponse, responses=error_responses(401, 403, 409, 422))
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


@router.get("/secrets/{ref}", response_model=SecretReferenceResponse, responses=error_responses(401, 403, 404, 422))
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


@router.put("/config/draft", response_model=PublishResponse, responses=error_responses(401, 403, 422))
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


@router.post("/config/publish", response_model=PublishResponse, responses=error_responses(401, 403, 409, 422))
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


@router.post("/config/rollback/{version}", response_model=PublishResponse, responses=error_responses(401, 403, 422))
async def rollback(
    version: int,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> PublishResponse:
    _require(principal, "config:publish")
    return await service.rollback(version)


@router.get("/agents", response_model=list[AgentResourceResponse], responses=error_responses(401, 403, 422))
async def list_agents(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[AgentResourceResponse]:
    _require(principal, "agent:read")
    return list(await service.list_agents())


@router.post("/agents", response_model=AgentResourceResponse, responses=error_responses(401, 403, 422))
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


@router.get("/workflows", response_model=list[WorkflowResourceResponse], responses=error_responses(401, 403, 422))
async def list_workflows(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[WorkflowResourceResponse]:
    _require(principal, "agent:read")
    return list(await service.list_workflows())


@router.post("/workflows", response_model=WorkflowResourceResponse, responses=error_responses(401, 403, 422))
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


@router.get("/settings", response_model=SystemSettingsResponse, responses=error_responses(401, 403, 422))
async def get_settings(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SystemSettingsResponse:
    _require(principal, "config:read")
    return await service.get_settings()


@router.put("/settings", response_model=SystemSettingsResponse, responses=error_responses(401, 403, 422))
async def update_settings(
    body: SystemSettingsRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SystemSettingsResponse:
    _require(principal, "config:write")
    return await service.update_settings(body)


@router.get("/main-agent", response_model=MainAgentConfigResponse, responses=error_responses(401, 403, 422))
async def get_main_agent_config(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MainAgentConfigResponse:
    _require(principal, "config:read")
    return await service.get_main_agent_config()


@router.put("/main-agent", response_model=MainAgentConfigResponse, responses=error_responses(401, 403, 422))
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


@router.get("/runs/{run_id}", response_model=RunDetailResponse, responses=error_responses(401, 403, 404, 422))
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


@router.post("/runs/{run_id}/pause", response_model=RunDetailResponse, responses=error_responses(401, 403, 404, 422))
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


@router.post("/runs/{run_id}/resume", response_model=RunDetailResponse, responses=error_responses(401, 403, 404, 422))
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


@router.post("/runs/{run_id}/cancel", response_model=RunDetailResponse, responses=error_responses(401, 403, 404, 422))
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


@router.post("/skills/upload", response_model=SkillResponse, responses=error_responses(401, 403, 413, 422, 503))
async def upload_skill_archive(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> SkillResponse:
    _require(principal, "skill:write")
    filename = _safe_skill_upload_filename(request.headers.get("x-agent-hub-skill-filename"))
    archive_bytes = await request.body()
    if not archive_bytes:
        raise PublicAPIError(422, "request_validation", "skill archive is empty")
    try:
        return await service.upload_skill_archive(filename, archive_bytes)
    except InvalidSkillPackage:
        raise PublicAPIError(422, "invalid_skill_package", "skill package is invalid") from None


@router.post("/skills/{skill_id}/approve", response_model=SkillResponse, responses=error_responses(401, 403, 404, 422))
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


@router.get("/mcp", response_model=list[McpServerResponse], responses=error_responses(401, 403, 422))
async def list_mcp_servers(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[McpServerResponse]:
    _require(principal, "mcp:read")
    return list(await service.list_mcp_servers())


@router.post("/mcp", response_model=McpServerResponse, responses=error_responses(401, 403, 422))
async def upsert_mcp_server(
    body: McpServerRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> McpServerResponse:
    _require(principal, "mcp:write")
    return await service.upsert_mcp_server(body)


@router.delete(
    "/mcp/{server_id}",
    response_model=OperationStatusResponse,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_mcp_server(
    server_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> OperationStatusResponse:
    _require(principal, "mcp:write")
    try:
        await service.delete_mcp_server(server_id)
    except KeyError:
        raise PublicAPIError(404, "not_found", "not found") from None
    return OperationStatusResponse(status="deleted")


@router.get("/channels", response_model=list[ChannelStatusResponse], responses=error_responses(401, 403, 422))
async def list_channels(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[ChannelStatusResponse]:
    _require(principal, "config:read")
    return list(await service.list_channels())


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
    request.app.state.channel_runtime_config = await service.channel_runtime_config()
    return response


@router.get("/memory", response_model=list[MemoryRecordResponse], responses=error_responses(401, 403, 422))
async def list_memory(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[MemoryRecordResponse]:
    _require(principal, "memory:read")
    return list(await service.list_memory())


@router.post("/memory", response_model=MemoryRecordResponse, responses=error_responses(401, 403, 422))
async def create_memory(
    body: MemoryCreateRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    return await service.create_memory(body)


@router.patch("/memory/{memory_id}", response_model=MemoryRecordResponse, responses=error_responses(401, 403, 404, 422))
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


@router.get("/audit", response_model=list[AuditEventResponse], responses=error_responses(401, 403, 422))
async def list_audit_events(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
    action: str | None = None,
) -> list[AuditEventResponse]:
    _require(principal, "audit:read")
    return list(await service.list_audit_events(action))


@router.get("/logs", response_model=list[LogEntryResponse], responses=error_responses(401, 403, 422))
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


@router.get("/hermes", response_model=list[HermesInsightResponse], responses=error_responses(401, 403, 422))
async def list_hermes_insights(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[HermesInsightResponse]:
    _require(principal, "hermes:read")
    return list(await service.list_hermes_insights())


@router.post("/hermes/feedback", response_model=HermesInsightResponse, responses=error_responses(401, 403, 422))
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


@router.post("/hermes/recommend", response_model=HermesRecommendationResponse, responses=error_responses(401, 403, 422))
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


@router.get("/hermes/{insight_id}", response_model=HermesInsightResponse, responses=error_responses(401, 403, 404, 422))
async def get_hermes_insight(
    insight_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesInsightResponse:
    _require(principal, "hermes:read")
    try:
        return await service.get_hermes_insight(insight_id)
    except KeyError:
        raise PublicAPIError(404, "hermes_not_found", "Hermes learning record was not found") from None


@router.post("/hermes/{insight_id}/confirm", response_model=HermesInsightResponse, responses=error_responses(401, 403, 404, 422))
async def confirm_hermes_insight(
    insight_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> HermesInsightResponse:
    _require(principal, "hermes:write")
    try:
        return await service.confirm_hermes_insight(insight_id)
    except KeyError:
        raise PublicAPIError(404, "hermes_not_found", "Hermes learning record was not found") from None


__all__ = ["InMemoryAdminResourceService", "PersistentAdminResourceService", "router"]
