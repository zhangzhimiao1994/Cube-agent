"""Bounded public handover HTTP boundary; current SQL authorization belongs to the service."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from agent_hub.api.dependencies import current_principal
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied
from agent_hub.plugins.handover_reviews import StoredPluginReviewReport
from agent_hub.plugins.handover_snapshots import InvalidSnapshot, SnapshotUnavailable
from agent_hub.plugins.handover_sources import SourceChanged, SourceLimits
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    Conflict,
    HandoverReceipt,
    NotFound,
    StorageUnavailable,
    WorkspaceSource,
)
from agent_hub.runtime.contracts import _mutable_json

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_DIGEST = r"^[a-f0-9]{64}$"
_UUID = r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
_CONFIRM_QUERY = frozenset({"expected_version", "plan_hash", "confirm", "key_id"})


class GeneratedPluginHandoverService(Protocol):
    async def create_plan(
        self, principal: AuthenticatedPrincipal, run_id: UUID,
        source: ArtifactSource | WorkspaceSource, *, idempotency_key: str, expires_at: datetime,
    ) -> HandoverReceipt: ...

    async def get_receipt(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str,
    ) -> HandoverReceipt: ...

    async def cancel_plan(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str,
        expected_version: int, plan_hash: str,
    ) -> HandoverReceipt: ...

    async def confirm(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str, *,
        expected_version: int, plan_hash: str, confirm: bool, idempotency_key: str,
        archive_bytes: bytes, key_id: str, request: Request,
    ) -> HandoverReceipt: ...


class GeneratedPluginReviewService(Protocol):
    async def scan_pending_package(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str, *,
        archive_bytes: bytes, key_id: str, idempotency_key: str,
    ) -> StoredPluginReviewReport: ...

    async def get_report(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str, report_id: str,
    ) -> StoredPluginReviewReport: ...


class _SafeRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                return await handler(request)
            except (ClientDisconnect, PublicAPIError):
                raise
            except PermissionDenied:
                raise PublicAPIError(403, "permission_denied", "permission denied") from None
            except NotFound:
                raise PublicAPIError(404, "not_found", "handover was not found") from None
            except (Conflict, SourceChanged):
                raise PublicAPIError(409, "handover_conflict", "handover conflict") from None
            except (StorageUnavailable, SnapshotUnavailable, InvalidSnapshot):
                raise PublicAPIError(503, "service_unavailable", "service unavailable") from None
            except (RequestValidationError, ValueError, TypeError):
                raise PublicAPIError(
                    422, "request_validation", "request validation failed",
                ) from None
            except Exception:  # noqa: BLE001 - public boundary never renders backend errors.
                raise PublicAPIError(500, "internal_error", "internal server error") from None

        return safe


router = APIRouter(
    prefix="/api/v1/runs/{run_id}/plugin-handovers",
    tags=["plugin-handovers"], route_class=_SafeRoute,
    responses=error_responses(401, 403, 404, 405, 409, 413, 422, 500, 503),
)


class _WireDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ArtifactSourceBody(_WireDTO):
    kind: Literal["artifact"]
    artifact_id: Annotated[str, Field(pattern=_UUID)]


class WorkspaceSourceBody(_WireDTO):
    kind: Literal["workspace"]
    project_id: Annotated[str, Field(pattern=_ID, max_length=128)]
    session_id: Annotated[str, Field(pattern=_ID, max_length=128)]
    paths: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=255)]],
        Field(min_length=1, max_length=200),
    ]


class CreatePlanBody(_WireDTO):
    source: Annotated[ArtifactSourceBody | WorkspaceSourceBody, Field(discriminator="kind")]
    expires_at: AwareDatetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def wire_datetime(cls, value: object) -> datetime:
        if type(value) is not str or len(value) > 64:
            raise ValueError("invalid expiry")
        return datetime.fromisoformat(value)


class CancelPlanBody(_WireDTO):
    expected_version: Annotated[int, Field(ge=1)]
    plan_hash: Annotated[str, Field(pattern=_DIGEST, min_length=64, max_length=64)]


class PluginSourceReviewResponse(_WireDTO):
    schema_version: Literal[1] = 1
    report_id: Annotated[str, Field(pattern=r"^gs-[a-f0-9]{64}$")]
    report_sha256: Annotated[str, Field(pattern=_DIGEST)]
    created_at: AwareDatetime
    coverage: Literal["complete", "incomplete"]
    body: dict[str, JsonValue]


def _review_response(stored: StoredPluginReviewReport) -> PluginSourceReviewResponse:
    return PluginSourceReviewResponse(
        report_id=stored.report_id, report_sha256=stored.report_sha256,
        created_at=stored.created_at, coverage=stored.report.coverage,
        body=cast(dict[str, JsonValue], _mutable_json(stored.report.body)),
    )


def get_plugin_handover_services(request: Request) -> GeneratedPluginHandoverService:
    services = getattr(request.app.state, "plugin_handover_services", None)
    if services is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(GeneratedPluginHandoverService, services)


def get_plugin_review_services(request: Request) -> GeneratedPluginReviewService:
    services = getattr(request.app.state, "plugin_review_services", None)
    if services is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(GeneratedPluginReviewService, services)


def _single_idempotency_key(request: Request) -> None:
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise PublicAPIError(422, "request_validation", "request validation failed")


async def _archive(request: Request) -> bytes:
    limit = SourceLimits().max_archive_bytes
    data = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(data):
            raise PublicAPIError(413, "request_too_large", "request body is too large")
        data.extend(chunk)
    if not data:
        raise PublicAPIError(422, "request_validation", "request validation failed")
    return bytes(data)


Principal = Annotated[AuthenticatedPrincipal, Depends(current_principal)]
Services = Annotated[GeneratedPluginHandoverService, Depends(get_plugin_handover_services)]
ReviewServices = Annotated[GeneratedPluginReviewService, Depends(get_plugin_review_services)]
HandoverPath = Annotated[str, Path(pattern=r"^gh-[a-f0-9]{64}$", min_length=67, max_length=67)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", pattern=_ID, min_length=1, max_length=128),
]


@router.post("", response_model=HandoverReceipt)
async def create_plan(
    run_id: UUID, body: CreatePlanBody, request: Request,
    principal: Principal, services: Services, idempotency_key: IdempotencyKey,
) -> HandoverReceipt:
    _single_idempotency_key(request)
    source: ArtifactSource | WorkspaceSource
    if isinstance(body.source, ArtifactSourceBody):
        source = ArtifactSource(artifact_id=str(UUID(body.source.artifact_id)))
    else:
        source = WorkspaceSource(
            project_id=body.source.project_id, session_id=body.source.session_id,
            paths=tuple(body.source.paths),
        )
    return await services.create_plan(
        principal, run_id, source,
        idempotency_key=idempotency_key, expires_at=body.expires_at,
    )


@router.get("/{handover_id}", response_model=HandoverReceipt)
async def get_receipt(
    run_id: UUID, handover_id: HandoverPath, principal: Principal, services: Services,
) -> HandoverReceipt:
    return await services.get_receipt(principal, run_id, handover_id)


@router.post("/{handover_id}/cancel", response_model=HandoverReceipt)
async def cancel_plan(
    run_id: UUID, handover_id: HandoverPath, body: CancelPlanBody,
    principal: Principal, services: Services,
) -> HandoverReceipt:
    return await services.cancel_plan(
        principal, run_id, handover_id, body.expected_version, body.plan_hash,
    )


@router.post("/{handover_id}/confirm", response_model=HandoverReceipt)
async def confirm_plan(
    run_id: UUID, handover_id: HandoverPath, request: Request,
    principal: Principal, services: Services, idempotency_key: IdempotencyKey,
    expected_version: Annotated[str, Query(pattern=r"^[1-9][0-9]{0,19}$")],
    plan_hash: Annotated[str, Query(pattern=_DIGEST, min_length=64, max_length=64)],
    confirm: Annotated[Literal["true"], Query()],
    key_id: Annotated[str, Query(pattern=_ID, min_length=1, max_length=128)],
) -> HandoverReceipt:
    _single_idempotency_key(request)
    if set(request.query_params) != _CONFIRM_QUERY or any(
        len(request.query_params.getlist(name)) != 1 for name in _CONFIRM_QUERY
    ):
        raise PublicAPIError(422, "request_validation", "request validation failed")
    archive = await _archive(request)
    return await services.confirm(
        principal, run_id, handover_id, expected_version=int(expected_version),
        plan_hash=plan_hash, confirm=confirm == "true", idempotency_key=idempotency_key,
        archive_bytes=archive, key_id=key_id, request=request,
    )


@router.post("/{handover_id}/scan-reports", response_model=PluginSourceReviewResponse)
async def scan_source_package(
    run_id: UUID, handover_id: HandoverPath, request: Request,
    principal: Principal, services: ReviewServices, idempotency_key: IdempotencyKey,
    key_id: Annotated[str, Query(pattern=_ID, min_length=1, max_length=128)],
) -> PluginSourceReviewResponse:
    _single_idempotency_key(request)
    if set(request.query_params) != {"key_id"} or len(request.query_params.getlist("key_id")) != 1:
        raise PublicAPIError(422, "request_validation", "request validation failed")
    archive = await _archive(request)
    stored = await services.scan_pending_package(
        principal, run_id, handover_id, archive_bytes=archive, key_id=key_id,
        idempotency_key=idempotency_key,
    )
    return _review_response(stored)


@router.get("/{handover_id}/scan-reports/{report_id}", response_model=PluginSourceReviewResponse)
async def get_source_package_report(
    run_id: UUID, handover_id: HandoverPath,
    report_id: Annotated[str, Path(pattern=r"^gs-[a-f0-9]{64}$", min_length=67, max_length=67)],
    principal: Principal, services: ReviewServices,
) -> PluginSourceReviewResponse:
    return _review_response(await services.get_report(principal, run_id, handover_id, report_id))
