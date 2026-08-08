from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import BASE_ERROR_RESPONSES, PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService, ConfigValidationError
from agent_hub.domain.runs import RunStatus
from agent_hub.models.gateway import ModelTransport
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.types import Deployment, ModelCapability, ModelMessage, ModelRequest
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRecord, RunRepository
from agent_hub.security.secrets import SecretService, SecretValidationError

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], responses=BASE_ERROR_RESPONSES)


class ModelDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    api_base: str = Field(min_length=1, max_length=2048)
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


class RunListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: str
    mode: str
    queue_wait_ms: int = Field(ge=0)
    capacity_wait_ms: int = Field(ge=0)
    cost_usd: str


class RunEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    kind: str
    message: str
    created_at: datetime


class RunArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    title: str


class RunDetailResponse(RunListItem):
    request: str
    events: list[RunEventResponse]
    artifacts: list[RunArtifactResponse]
    explicit_details: dict[str, str]


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


class MemoryRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=20_000)


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


class HermesFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID | None = None
    outcome: str = Field(pattern=r"^(success|failure|neutral)$")
    lesson: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    weight: int = Field(default=1, ge=1, le=10)


class HermesInsightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    outcome: str
    lesson: str
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


class AdminResourceService(Protocol):
    async def list_models(self) -> tuple[ModelDeploymentResponse, ...]: ...

    async def create_model(self, request: ModelDeploymentRequest) -> ModelDeploymentResponse: ...

    async def create_secret(self, request: SecretCreateRequest) -> SecretReferenceResponse: ...

    async def get_secret(self, ref: str) -> SecretReferenceResponse: ...

    async def probe_concurrency(self, request: ProbeRequest) -> ProbeResponse: ...

    async def save_draft(self, request: DraftRequest) -> PublishResponse: ...

    async def diff_draft(self, request: DraftRequest) -> DiffResponse: ...

    async def publish(self, request: PublishRequest) -> PublishResponse: ...

    async def rollback(self, version: int) -> PublishResponse: ...

    async def list_agents(self) -> tuple[NamedResourceResponse, ...]: ...

    async def upsert_agent(self, request: NamedResourceRequest) -> NamedResourceResponse: ...

    async def list_workflows(self) -> tuple[NamedResourceResponse, ...]: ...

    async def upsert_workflow(self, request: NamedResourceRequest) -> NamedResourceResponse: ...

    async def list_runs(self) -> tuple[RunListItem, ...]: ...

    async def get_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def pause_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def resume_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse: ...

    async def list_skills(self) -> tuple[SkillResponse, ...]: ...

    async def upload_skill(self, request: SkillUploadRequest) -> SkillResponse: ...

    async def approve_skill(self, skill_id: str) -> SkillResponse: ...

    async def list_mcp_servers(self) -> tuple[McpServerResponse, ...]: ...

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]: ...

    async def update_memory(self, memory_id: str, request: MemoryRecordRequest) -> MemoryRecordResponse: ...

    async def forget_memory(self, memory_id: str) -> None: ...

    async def list_audit_events(self, action: str | None = None) -> tuple[AuditEventResponse, ...]: ...

    async def list_hermes_insights(self) -> tuple[HermesInsightResponse, ...]: ...

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
    agents: dict[str, NamedResourceResponse] = field(default_factory=dict)
    workflows: dict[str, NamedResourceResponse] = field(default_factory=dict)
    runs: dict[UUID, RunDetailResponse] = field(default_factory=dict)
    skills: dict[str, SkillResponse] = field(default_factory=dict)
    mcp_servers: dict[str, McpServerResponse] = field(default_factory=dict)
    memory: dict[str, MemoryRecordResponse] = field(default_factory=dict)
    audit_events: list[AuditEventResponse] = field(default_factory=list)
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
                },
            )
        if not self.mcp_servers:
            self.mcp_servers["filesystem"] = McpServerResponse(
                id="filesystem",
                name="Filesystem MCP",
                health="healthy",
                allowed_tools=["read_file", "list_directory"],
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
                tags=["dispatch", "planning", "clear-task"],
                weight=3,
                created_at=datetime.now(UTC),
            )

    async def list_models(self) -> tuple[ModelDeploymentResponse, ...]:
        return tuple(self.models.values())

    async def create_model(self, request: ModelDeploymentRequest) -> ModelDeploymentResponse:
        response = ModelDeploymentResponse(
            **request.model_dump(),
            id=uuid4(),
            effective_slots=max(0, request.max_concurrency - request.reserved_capacity),
            saturation_policy="queue_first_then_fallback",
        )
        self.models[response.id] = response
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

    async def list_agents(self) -> tuple[NamedResourceResponse, ...]:
        return tuple(self.agents.values())

    async def upsert_agent(self, request: NamedResourceRequest) -> NamedResourceResponse:
        response = NamedResourceResponse(**request.model_dump())
        self.agents[response.id] = response
        return response

    async def list_workflows(self) -> tuple[NamedResourceResponse, ...]:
        return tuple(self.workflows.values())

    async def upsert_workflow(self, request: NamedResourceRequest) -> NamedResourceResponse:
        response = NamedResourceResponse(**request.model_dump())
        self.workflows[response.id] = response
        return response

    async def list_runs(self) -> tuple[RunListItem, ...]:
        return tuple(
            RunListItem(
                id=run.id,
                status=run.status,
                mode=run.mode,
                queue_wait_ms=run.queue_wait_ms,
                capacity_wait_ms=run.capacity_wait_ms,
                cost_usd=run.cost_usd,
            )
            for run in self.runs.values()
        )

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        return self.runs[run_id]

    async def pause_run(self, run_id: UUID) -> RunDetailResponse:
        return self._set_run_status(run_id, "paused")

    async def resume_run(self, run_id: UUID) -> RunDetailResponse:
        return self._set_run_status(run_id, "running")

    async def cancel_run(self, run_id: UUID) -> RunDetailResponse:
        return self._set_run_status(run_id, "cancelled")

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

    async def approve_skill(self, skill_id: str) -> SkillResponse:
        current = self.skills[skill_id]
        updated = current.model_copy(update={"status": "enabled"})
        self.skills[skill_id] = updated
        return updated

    async def list_mcp_servers(self) -> tuple[McpServerResponse, ...]:
        return tuple(self.mcp_servers.values())

    async def list_memory(self) -> tuple[MemoryRecordResponse, ...]:
        return tuple(self.memory.values())

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

    async def list_hermes_insights(self) -> tuple[HermesInsightResponse, ...]:
        return tuple(sorted(self.hermes_insights.values(), key=lambda insight: insight.created_at))

    async def record_hermes_feedback(
        self, request: HermesFeedbackRequest
    ) -> HermesInsightResponse:
        if _contains_sensitive_marker(request.lesson):
            raise ValueError("sensitive content")
        insight = HermesInsightResponse(
            id=f"hermes-{uuid4().hex}",
            outcome=request.outcome,
            lesson=request.lesson,
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
    ) -> None:
        super().__init__()
        self._config_service = config_service
        self._secret_service = secret_service
        self._tenant_id = tenant_id
        self._actor_id = actor_id
        self._model_transport = model_transport or LiteLLMClient()
        self._run_repository = run_repository

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
        except RunConflict:
            raise PublicAPIError(409, "run_conflict", "run state conflict") from None
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
        except RunConflict:
            raise PublicAPIError(409, "run_conflict", "run state conflict") from None
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
        except RunConflict:
            raise PublicAPIError(409, "run_conflict", "run state conflict") from None
        return await self._run_detail(record)

    async def _run_list_item(self, record: RunRecord) -> RunListItem:
        assert self._run_repository is not None
        return RunListItem(
            id=record.id,
            status=record.status.value,
            mode="auto" if record.mode is None else record.mode.value,
            queue_wait_ms=0,
            capacity_wait_ms=0,
            cost_usd=str(await self._run_repository.usage_cost(self._tenant_id, record.id)),
        )

    async def _run_detail(self, record: RunRecord) -> RunDetailResponse:
        assert self._run_repository is not None
        list_item = await self._run_list_item(record)
        events = await self._run_repository.events(self._tenant_id, record.id)
        artifact_ids = await self._run_repository.artifact_ids(self._tenant_id, record.id)
        return RunDetailResponse(
            **list_item.model_dump(),
            request=record.request,
            events=[_admin_run_event(event) for event in events],
            artifacts=[
                RunArtifactResponse(
                    id=str(artifact_id),
                    kind="artifact",
                    title=str(artifact_id),
                )
                for artifact_id in artifact_ids
            ],
            explicit_details={
                "source": "database",
                "version": str(record.version),
            },
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
        deployment = {
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
        except ConfigValidationError:
            raise PublicAPIError(422, "request_validation", "request validation failed") from None
        current = await self._config_service.get_current(self._tenant_id)
        if current is None:
            raise PublicAPIError(503, "service_unavailable", "service unavailable")
        published = PlatformConfig.model_validate(current.document)
        created_definition = published.models[request.logical_model]
        return self._model_response(
            request.logical_model,
            created_definition.fallback_model,
            deployment_index,
            created_definition.deployments[deployment_index],
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
        return SecretReferenceResponse(ref=reference.reference, last_four=value[-4:])

    async def _verify_model_availability(self, deployment: Deployment) -> None:
        if ModelCapability.TEXT not in deployment.capabilities:
            raise PublicAPIError(
                422,
                "model_unavailable",
                "model availability check requires text capability",
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
        except PublicAPIError:
            raise
        except Exception as error:  # noqa: BLE001 - redact provider/SDK failures.
            reason = _safe_model_check_reason(error)
            raise PublicAPIError(
                422,
                "model_unavailable",
                f"model availability check failed: {reason}",
            ) from None

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


def _safe_model_check_reason(error: Exception) -> str:
    message = str(error).strip()
    if not message or _contains_sensitive_marker(message):
        return "provider request failed"
    return message[:300]


def _admin_run_event(event: dict[str, object]) -> RunEventResponse:
    sequence = event.get("sequence")
    kind = event.get("kind")
    message = event.get("reason") or event.get("message") or kind
    return RunEventResponse(
        sequence=sequence if type(sequence) is int else 1,
        kind=kind if type(kind) is str else "event",
        message=message if type(message) is str else "event recorded",
        created_at=datetime.now(UTC),
    )


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


@router.get("/agents", response_model=list[NamedResourceResponse], responses=error_responses(401, 403, 422))
async def list_agents(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[NamedResourceResponse]:
    _require(principal, "agent:read")
    return list(await service.list_agents())


@router.post("/agents", response_model=NamedResourceResponse, responses=error_responses(401, 403, 422))
async def upsert_agent(
    body: NamedResourceRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> NamedResourceResponse:
    _require(principal, "agent:write")
    return await service.upsert_agent(body)


@router.get("/workflows", response_model=list[NamedResourceResponse], responses=error_responses(401, 403, 422))
async def list_workflows(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[NamedResourceResponse]:
    _require(principal, "agent:read")
    return list(await service.list_workflows())


@router.post("/workflows", response_model=NamedResourceResponse, responses=error_responses(401, 403, 422))
async def upsert_workflow(
    body: NamedResourceRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> NamedResourceResponse:
    _require(principal, "agent:write")
    return await service.upsert_workflow(body)


@router.get("/runs", response_model=list[RunListItem], responses=error_responses(401, 403, 422))
async def list_operational_runs(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[RunListItem]:
    _require(principal, "run:read")
    return list(await service.list_runs())


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


@router.get("/mcp", response_model=list[McpServerResponse], responses=error_responses(401, 403, 422))
async def list_mcp_servers(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[McpServerResponse]:
    _require(principal, "mcp:read")
    return list(await service.list_mcp_servers())


@router.get("/memory", response_model=list[MemoryRecordResponse], responses=error_responses(401, 403, 422))
async def list_memory(
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
    service: Annotated[AdminResourceService, Depends(_service)],
) -> list[MemoryRecordResponse]:
    _require(principal, "memory:read")
    return list(await service.list_memory())


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


__all__ = ["InMemoryAdminResourceService", "PersistentAdminResourceService", "router"]
