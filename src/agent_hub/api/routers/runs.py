"""Tenant-scoped durable run control endpoints."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Protocol, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound
from agent_hub.runs.service import RunSummary, SubmittedRun

router = APIRouter(
    prefix="/api/v1/runs",
    tags=["runs"],
    responses=error_responses(401, 403, 404, 405, 409, 413, 422, 500, 503),
)


class RunServiceProtocol(Protocol):
    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...] = (),
        workflow_id: str | None = None,
        allow_workflow_adjustment: bool = False,
        conversation_id: str | None = None,
        reference_conversation_id: str | None = None,
        attachment_ids: tuple[str, ...] = (),
        direct_model: str | None = None,
        idempotency_key: str | None = None,
    ) -> SubmittedRun: ...

    async def choose_mode(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        mode: TaskMode,
        decision_token: str,
        version: int,
        operator_note: str | None = None,
    ) -> SubmittedRun: ...

    async def approve_temporary_agent(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> SubmittedRun: ...

    async def revise_temporary_agent(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
        feedback: str,
    ) -> SubmittedRun: ...

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunSummary: ...

    async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]: ...

    async def pause(self, tenant_id: UUID, run_id: UUID) -> RunSummary: ...

    async def resume(self, tenant_id: UUID, run_id: UUID) -> RunSummary: ...

    async def cancel(self, tenant_id: UUID, run_id: UUID) -> RunSummary: ...


class CreateRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=65_536)
    mode: TaskMode = TaskMode.AUTO
    agent_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    direct_model: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    workflow_id: str | None = Field(default=None, max_length=128)
    allow_workflow_adjustment: bool = False
    conversation_id: str | None = Field(default=None, min_length=4, max_length=128)
    reference_conversation_id: str | None = Field(default=None, min_length=4, max_length=128)
    attachment_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def coerce_attachment_ids(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("attachment_ids")
    @classmethod
    def validate_attachment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for item in value:
            if item in seen or not re.fullmatch(r"att_[a-f0-9]{32}", item):
                raise ValueError("attachment_ids must be unique attachment identifiers")
            seen.add(item)
        return value


class ChooseModeRequest(BaseModel):
    mode: TaskMode
    decision_token: str = Field(min_length=32, max_length=160)
    version: int = Field(ge=1)
    operator_note: str | None = Field(default=None, max_length=2000)


class ApproveTemporaryAgentRequest(BaseModel):
    decision_token: str = Field(min_length=32, max_length=160)
    version: int = Field(ge=1)


class ReviseTemporaryAgentRequest(BaseModel):
    decision_token: str = Field(min_length=32, max_length=160)
    version: int = Field(ge=1)
    feedback: str = Field(min_length=1, max_length=2000)


class SubmittedRunResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    status: RunStatus
    mode: TaskMode | None
    decision_token: str | None = None
    version: int
    clarification_reason: str | None = None
    conversation_id: str | None = None
    reference_conversation_id: str | None = None
    temporary_agent_proposal: dict[str, object] | None = None

    @classmethod
    def from_submitted(cls, run: SubmittedRun) -> SubmittedRunResponse:
        return cls(
            id=run.id,
            tenant_id=run.tenant_id,
            status=run.status,
            mode=run.mode,
            decision_token=run.decision_token,
            version=run.version,
            clarification_reason=run.clarification_reason,
            conversation_id=run.conversation_id,
            reference_conversation_id=run.reference_conversation_id,
            temporary_agent_proposal=run.temporary_agent_proposal,
        )


class RunSummaryResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    status: RunStatus
    mode: TaskMode | None
    request: str
    completed_step_ids: tuple[str, ...]
    artifact_ids: tuple[UUID, ...]
    usage_cost_usd: Decimal

    @classmethod
    def from_summary(cls, run: RunSummary) -> RunSummaryResponse:
        return cls(
            id=run.id,
            tenant_id=run.tenant_id,
            status=run.status,
            mode=run.mode,
            request=run.request,
            completed_step_ids=run.completed_step_ids,
            artifact_ids=run.artifact_ids,
            usage_cost_usd=run.usage_cost_usd,
        )


class RunEventsResponse(BaseModel):
    items: tuple[dict[str, object], ...]


class AttachmentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    expires_at: datetime


def _run_service(request: Request) -> RunServiceProtocol:
    service = getattr(request.app.state, "run_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(RunServiceProtocol, service)


def _attachment_store_dir(request: Request) -> Path:
    configured = getattr(request.app.state, "attachment_store_dir", None)
    if isinstance(configured, Path):
        return configured
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and hasattr(settings, "attachment_store_dir"):
        return cast(Path, settings.attachment_store_dir)
    return Path("/var/lib/agent-hub/attachments")


def _attachment_limits(request: Request) -> tuple[int, int]:
    defaults = (25, 7)
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None:
        return defaults
    return defaults


async def _attachment_limits_from_settings(request: Request) -> tuple[int, int]:
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None:
        return _attachment_limits(request)
    try:
        settings = await service.get_settings()
    except (AttributeError, RuntimeError, TypeError, ValueError, SQLAlchemyError):
        return _attachment_limits(request)
    max_mb = getattr(settings, "attachment_max_mb", 25)
    retention_days = getattr(settings, "attachment_retention_days", 7)
    if type(max_mb) is not int or not 1 <= max_mb <= 200:
        max_mb = 25
    if type(retention_days) is not int or not 1 <= retention_days <= 365:
        retention_days = 7
    return max_mb, retention_days


def _safe_attachment_filename(value: str | None) -> str:
    raw = (value or "attachment.bin").strip().replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:180].strip("._")
    return cleaned or "attachment.bin"


def _attachment_kind(filename: str, content_type: str) -> str:
    lowered = filename.lower()
    if content_type.startswith("image/"):
        return "image"
    if lowered.endswith(".zip"):
        return "code_archive"
    return "context"


@router.post(
    "/attachments/upload",
    response_model=AttachmentUploadResponse,
    responses=error_responses(401, 403, 413, 422, 500),
)
async def upload_attachment(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
    filename_header: Annotated[str | None, Header(alias="X-Agent-Hub-Filename")] = None,
    content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
) -> AttachmentUploadResponse:
    filename = _safe_attachment_filename(filename_header)
    media_type = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    body = await request.body()
    max_mb, retention_days = await _attachment_limits_from_settings(request)
    max_bytes = max_mb * 1024 * 1024
    if not body:
        raise PublicAPIError(422, "empty_attachment", "attachment file is empty")
    if len(body) > max_bytes:
        raise PublicAPIError(
            413,
            "attachment_too_large",
            f"attachment exceeds configured limit of {max_mb} MB",
            details={"max_mb": str(max_mb), "size_bytes": str(len(body))},
        )

    attachment_id = f"att_{uuid4().hex}"
    digest = hashlib.sha256(body).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(days=retention_days)
    tenant_dir = (_attachment_store_dir(request) / str(principal.tenant_id)).resolve()
    tenant_dir.mkdir(parents=True, exist_ok=True)
    data_path = tenant_dir / f"{attachment_id}.bin"
    meta_path = tenant_dir / f"{attachment_id}.json"
    data_path.write_bytes(body)
    meta_path.write_text(
        json.dumps(
            {
                "id": attachment_id,
                "filename": filename,
                "kind": _attachment_kind(filename, media_type),
                "content_type": media_type,
                "size_bytes": len(body),
                "sha256": digest,
                "expires_at": expires_at.isoformat(),
                "source": "web",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return AttachmentUploadResponse(
        id=attachment_id,
        filename=filename,
        kind=_attachment_kind(filename, media_type),
        content_type=media_type,
        size_bytes=len(body),
        sha256=digest,
        expires_at=expires_at,
    )


def _run_not_found() -> PublicAPIError:
    return PublicAPIError(404, "run_not_found", "run was not found")


def _run_conflict(error: RunConflict) -> PublicAPIError:
    reason = str(error) or "run state conflict"
    return PublicAPIError(
        409,
        "run_conflict",
        reason,
        details={"reason": reason},
    )


@router.post(
    "",
    response_model=SubmittedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    body: CreateRunRequest,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SubmittedRunResponse:
    submitted = await service.submit(
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        message=body.message,
        mode=body.mode,
        agent_ids=body.agent_ids,
        workflow_id=body.workflow_id,
        allow_workflow_adjustment=body.allow_workflow_adjustment,
        conversation_id=body.conversation_id,
        reference_conversation_id=body.reference_conversation_id,
        attachment_ids=body.attachment_ids,
        direct_model=body.direct_model,
        idempotency_key=idempotency_key,
    )
    return SubmittedRunResponse.from_submitted(submitted)


@router.post(
    "/{run_id}/choose-mode",
    response_model=SubmittedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def choose_mode(
    run_id: UUID,
    body: ChooseModeRequest,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> SubmittedRunResponse:
    if body.mode is TaskMode.AUTO:
        raise PublicAPIError(422, "request_validation", "request validation failed")
    try:
        submitted = await service.choose_mode(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            run_id=run_id,
            mode=body.mode,
            decision_token=body.decision_token,
            version=body.version,
            operator_note=body.operator_note,
        )
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
    except ValueError as error:
        raise PublicAPIError(
            422,
            "request_validation",
            str(error),
            details={"reason": str(error)},
        ) from error
    return SubmittedRunResponse.from_submitted(submitted)


@router.post(
    "/{run_id}/approve-temporary-agent",
    response_model=SubmittedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_temporary_agent(
    run_id: UUID,
    body: ApproveTemporaryAgentRequest,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> SubmittedRunResponse:
    try:
        submitted = await service.approve_temporary_agent(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            run_id=run_id,
            decision_token=body.decision_token,
            version=body.version,
        )
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
    except ValueError as error:
        raise PublicAPIError(
            422,
            "request_validation",
            str(error),
            details={"reason": str(error)},
        ) from error
    return SubmittedRunResponse.from_submitted(submitted)


@router.post(
    "/{run_id}/revise-temporary-agent",
    response_model=SubmittedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def revise_temporary_agent(
    run_id: UUID,
    body: ReviseTemporaryAgentRequest,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> SubmittedRunResponse:
    try:
        submitted = await service.revise_temporary_agent(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            run_id=run_id,
            decision_token=body.decision_token,
            version=body.version,
            feedback=body.feedback,
        )
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
    except ValueError as error:
        raise PublicAPIError(
            422,
            "request_validation",
            str(error),
            details={"reason": str(error)},
        ) from error
    return SubmittedRunResponse.from_submitted(submitted)


@router.get("/{run_id}", response_model=RunSummaryResponse)
async def get_run(
    run_id: UUID,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> RunSummaryResponse:
    try:
        summary = await service.get(principal.tenant_id, run_id)
    except RunNotFound as error:
        raise _run_not_found() from error
    return RunSummaryResponse.from_summary(summary)


@router.get("/{run_id}/events", response_model=RunEventsResponse)
async def run_events(
    run_id: UUID,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> RunEventsResponse:
    try:
        events = await service.events(principal.tenant_id, run_id)
    except RunNotFound as error:
        raise _run_not_found() from error
    return RunEventsResponse(items=events)


@router.get("/{run_id}/details", response_model=RunSummaryResponse)
async def run_details(
    run_id: UUID,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> RunSummaryResponse:
    try:
        summary = await service.get(principal.tenant_id, run_id)
    except RunNotFound as error:
        raise _run_not_found() from error
    return RunSummaryResponse.from_summary(summary)


@router.post("/{run_id}/pause", response_model=RunSummaryResponse)
async def pause_run(
    run_id: UUID,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:pause"))],
) -> RunSummaryResponse:
    try:
        summary = await service.pause(principal.tenant_id, run_id)
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
    return RunSummaryResponse.from_summary(summary)


@router.post("/{run_id}/resume", response_model=RunSummaryResponse)
async def resume_run(
    run_id: UUID,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:resume"))],
) -> RunSummaryResponse:
    try:
        summary = await service.resume(principal.tenant_id, run_id)
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
    return RunSummaryResponse.from_summary(summary)


@router.post("/{run_id}/cancel", response_model=RunSummaryResponse)
async def cancel_run(
    run_id: UUID,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:cancel"))],
) -> RunSummaryResponse:
    try:
        summary = await service.cancel(principal.tenant_id, run_id)
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
    return RunSummaryResponse.from_summary(summary)
