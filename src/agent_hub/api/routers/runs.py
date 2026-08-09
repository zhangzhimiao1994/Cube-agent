"""Tenant-scoped durable run control endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field

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
    workflow_id: str | None = Field(default=None, max_length=128)
    allow_workflow_adjustment: bool = False
    conversation_id: str | None = Field(default=None, min_length=4, max_length=128)
    reference_conversation_id: str | None = Field(default=None, min_length=4, max_length=128)


class ChooseModeRequest(BaseModel):
    mode: TaskMode
    decision_token: str = Field(min_length=32, max_length=160)
    version: int = Field(ge=1)


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


def _run_service(request: Request) -> RunServiceProtocol:
    service = getattr(request.app.state, "run_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(RunServiceProtocol, service)


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
        )
    except RunNotFound as error:
        raise _run_not_found() from error
    except RunConflict as error:
        raise _run_conflict(error) from error
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
