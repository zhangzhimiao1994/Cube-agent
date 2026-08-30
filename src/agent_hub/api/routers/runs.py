"""Tenant-scoped durable run control endpoints."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Annotated, Protocol, cast
from urllib.parse import unquote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound
from agent_hub.runs.service import RunSummary, SubmittedRun

router = APIRouter(
    prefix="/api/v1/runs",
    tags=["runs"],
    responses=error_responses(401, 403, 404, 405, 409, 413, 422, 500, 503),
)

ARCHIVE_EXTENSIONS = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tgz",
    ".tbz2",
    ".txz",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".cab",
    ".iso",
    ".jar",
    ".war",
    ".ear",
    ".apk",
    ".ipa",
)
ARCHIVE_MANIFEST_MAX_FILES = 512


class RunServiceProtocol(Protocol):
    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_role: Role | None = None,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...] = (),
        workflow_id: str | None = None,
        allow_workflow_adjustment: bool = False,
        conversation_id: str | None = None,
        reference_conversation_id: str | None = None,
        attachment_ids: tuple[str, ...] = (),
        direct_model: str | None = None,
        vibe_coding: bool = False,
        skip_evolution_proposal: bool = False,
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

    async def accept_self_repair(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
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
    vibe_coding: bool = False
    skip_evolution_proposal: bool = False

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


class AcceptSelfRepairRequest(BaseModel):
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
    temporary_agent_proposal: dict[str, object] | None = None
    schedule_proposal: dict[str, object] | None = None
    evolution_proposal: dict[str, object] | None = None
    openclaw_proposal: dict[str, object] | None = None
    repair_proposal: dict[str, object] | None = None

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
            schedule_proposal=run.schedule_proposal,
            evolution_proposal=run.evolution_proposal,
            openclaw_proposal=run.openclaw_proposal,
            repair_proposal=_repair_proposal_response(run.repair_proposal),
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


def _repair_proposal_response(proposal: dict[str, object] | None) -> dict[str, object] | None:
    if not proposal:
        return None
    allowed = {
        "kind",
        "title",
        "summary",
        "repair_action",
        "failure_kind",
        "source_run_id",
        "source_event_sequence",
        "requires_approval",
        "replay_safe",
        "automatic_execution",
        "fingerprint",
    }
    return {key: value for key, value in proposal.items() if key in allowed}


class AttachmentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    expires_at: datetime


class AttachmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AttachmentUploadResponse]


class AttachmentDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    deleted: bool


class AttachmentBulkDeleteFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    code: str
    message: str


class AttachmentBulkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if re.fullmatch(r"att_[a-f0-9]{32}", item) is None:
                raise ValueError("attachment ids must be safe attachment identifiers")
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


class AttachmentBulkDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: list[str]
    failed: list[AttachmentBulkDeleteFailure]


def _run_service(request: Request) -> RunServiceProtocol:
    service = getattr(request.app.state, "run_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(RunServiceProtocol, service)


async def _vibe_coding_enabled(request: Request) -> bool:
    service = getattr(request.app.state, "admin_resource_service", None)
    if service is None or not hasattr(service, "get_settings"):
        return False
    try:
        settings = await service.get_settings()
    except (AttributeError, RuntimeError, TypeError, ValueError, SQLAlchemyError):
        return False
    return getattr(settings, "vibe_coding_enabled", False) is True


async def _record_run_submit_audit(
    request: Request,
    principal: AuthenticatedPrincipal,
    body: CreateRunRequest,
    submitted: SubmittedRun,
) -> None:
    service = getattr(request.app.state, "admin_resource_service", None)
    recorder = getattr(service, "record_audit_event", None)
    if recorder is None:
        return
    preview = body.message.strip().replace("\r", " ").replace("\n", " ")[:120]
    details: dict[str, object] = {
        "user_id": str(principal.user_id),
        "user_role": principal.role.value,
        "run_id": str(submitted.id),
        "conversation_id": submitted.conversation_id,
        "reference_conversation_id": submitted.reference_conversation_id,
        "mode": body.mode.value,
        "accepted_mode": submitted.mode.value if submitted.mode is not None else None,
        "status": submitted.status.value,
        "agent_ids": list(body.agent_ids),
        "workflow_id": body.workflow_id,
        "direct_model": body.direct_model,
        "vibe_coding": body.vibe_coding,
        "attachment_count": len(body.attachment_ids),
        "message_preview": preview,
        "message_sha256": hashlib.sha256(body.message.encode("utf-8")).hexdigest(),
    }
    await recorder(
        actor=str(principal.user_id),
        action="run.submit",
        resource=str(submitted.id),
        details=details,
    )


def _attachment_store_dir(request: Request) -> Path:
    configured = getattr(request.app.state, "attachment_store_dir", None)
    if isinstance(configured, Path):
        return configured
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and hasattr(settings, "attachment_store_dir"):
        return cast(Path, settings.attachment_store_dir)
    return Path("/var/lib/agent-hub/attachments")


def _tenant_attachment_dir(request: Request, principal: AuthenticatedPrincipal) -> Path:
    return (_attachment_store_dir(request) / str(principal.tenant_id)).resolve()


def _safe_attachment_id(value: str) -> str:
    if re.fullmatch(r"att_[a-f0-9]{32}", value) is None:
        raise PublicAPIError(422, "request_validation", "invalid attachment id")
    return value


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


def _decode_upload_filename_header(value: str | None, encoding: str | None) -> str | None:
    if value is None or encoding is None:
        return value
    if encoding.strip().lower() != "percent":
        raise PublicAPIError(422, "request_validation", "unsupported filename header encoding")
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        raise PublicAPIError(422, "request_validation", "invalid filename header encoding") from None


def _safe_attachment_filename(value: str | None) -> str:
    raw = (value or "attachment.bin").strip().replace("\\", "/").split("/")[-1]
    cleaned = "".join(
        "_" if character in '<>:"|?*' or ord(character) < 32 or ord(character) == 127 else character
        for character in raw
    )[:180].strip(" ._")
    return cleaned or "attachment.bin"


def _is_archive_filename(filename: str) -> bool:
    lowered = filename.lower()
    return any(lowered.endswith(extension) for extension in ARCHIVE_EXTENSIONS)


def _attachment_kind(filename: str, content_type: str) -> str:
    lowered = filename.lower()
    if content_type.startswith("image/"):
        return "image"
    if _is_archive_filename(lowered):
        return "archive"
    return "context"


def _safe_archive_member_path(name: str) -> str | None:
    normalized = name.replace("\\", "/").strip()
    if not normalized:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return None
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _write_archive_member(root: Path, relative_path: str, content: bytes) -> Path:
    target = (root / Path(*relative_path.split("/"))).resolve()
    if not target.is_relative_to(root):
        raise ValueError("archive member escapes extraction root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _archive_member_entry(relative_path: str, size: int, *, extracted: bool) -> dict[str, object]:
    return {"path": relative_path, "size_bytes": size, "extracted": extracted}


def _extract_zip_manifest(
    *,
    body: bytes,
    extract_dir: Path,
    max_bytes: int,
) -> tuple[list[dict[str, object]], bool]:
    files: list[dict[str, object]] = []
    total_bytes = 0
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative_path = _safe_archive_member_path(info.filename)
            if relative_path is None:
                continue
            total_bytes += max(0, info.file_size)
            if total_bytes > max_bytes or len(files) >= ARCHIVE_MANIFEST_MAX_FILES:
                files.append(_archive_member_entry(relative_path, info.file_size, extracted=False))
                return files, False
            content = archive.read(info)
            _write_archive_member(extract_dir, relative_path, content)
            files.append(_archive_member_entry(relative_path, len(content), extracted=True))
    return files, True


def _extract_tar_manifest(
    *,
    body: bytes,
    extract_dir: Path,
    max_bytes: int,
) -> tuple[list[dict[str, object]], bool]:
    files: list[dict[str, object]] = []
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative_path = _safe_archive_member_path(member.name)
            if relative_path is None:
                continue
            total_bytes += max(0, member.size)
            if total_bytes > max_bytes or len(files) >= ARCHIVE_MANIFEST_MAX_FILES:
                files.append(_archive_member_entry(relative_path, member.size, extracted=False))
                return files, False
            member_file = archive.extractfile(member)
            if member_file is None:
                files.append(_archive_member_entry(relative_path, member.size, extracted=False))
                continue
            content = member_file.read()
            _write_archive_member(extract_dir, relative_path, content)
            files.append(_archive_member_entry(relative_path, len(content), extracted=True))
    return files, True


def _write_archive_manifest(
    *,
    attachment_id: str,
    filename: str,
    body: bytes,
    tenant_dir: Path,
    max_bytes: int,
) -> None:
    if not _is_archive_filename(filename):
        return

    extract_dir = (tenant_dir / attachment_id).resolve()
    if not extract_dir.is_relative_to(tenant_dir):
        raise ValueError("archive extraction root escaped tenant directory")
    manifest: dict[str, object] = {
        "archive": {
            "filename": filename,
            "extracted": False,
            "format": "unsupported",
        },
        "files": [],
    }

    try:
        if zipfile.is_zipfile(io.BytesIO(body)):
            files, complete = _extract_zip_manifest(body=body, extract_dir=extract_dir, max_bytes=max_bytes)
            manifest["archive"] = {"filename": filename, "extracted": complete, "format": "zip"}
            manifest["files"] = files
        else:
            files, complete = _extract_tar_manifest(body=body, extract_dir=extract_dir, max_bytes=max_bytes)
            manifest["archive"] = {"filename": filename, "extracted": complete, "format": "tar"}
            manifest["files"] = files
    except (tarfile.TarError, zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
        manifest["archive"] = {
            "filename": filename,
            "extracted": False,
            "format": "unsupported",
            "error": str(exc),
        }

    (tenant_dir / f"{attachment_id}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@router.post(
    "/attachments/upload",
    response_model=AttachmentUploadResponse,
    responses=error_responses(401, 403, 413, 422, 500),
)
async def upload_attachment(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
    filename_header: Annotated[str | None, Header(alias="X-Agent-Hub-Filename")] = None,
    filename_encoding_header: Annotated[str | None, Header(alias="X-Agent-Hub-Filename-Encoding")] = None,
    content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
) -> AttachmentUploadResponse:
    filename = _safe_attachment_filename(_decode_upload_filename_header(filename_header, filename_encoding_header))
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
    tenant_dir = _tenant_attachment_dir(request, principal)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    data_path = tenant_dir / f"{attachment_id}.bin"
    meta_path = tenant_dir / f"{attachment_id}.json"
    kind = _attachment_kind(filename, media_type)
    data_path.write_bytes(body)
    _write_archive_manifest(
        attachment_id=attachment_id,
        filename=filename,
        body=body,
        tenant_dir=tenant_dir,
        max_bytes=max_bytes,
    )
    meta_path.write_text(
        json.dumps(
            {
                "id": attachment_id,
                "filename": filename,
                "kind": kind,
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
        kind=kind,
        content_type=media_type,
        size_bytes=len(body),
        sha256=digest,
        expires_at=expires_at,
    )


def _read_attachment_metadata(path: Path) -> AttachmentUploadResponse | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = {key: raw[key] for key in AttachmentUploadResponse.model_fields if key in raw}
        return AttachmentUploadResponse.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


@router.get(
    "/attachments",
    response_model=AttachmentListResponse,
    responses=error_responses(401, 403, 500),
)
async def list_attachments(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:read"))],
) -> AttachmentListResponse:
    tenant_dir = _tenant_attachment_dir(request, principal)
    if not tenant_dir.is_dir():
        return AttachmentListResponse(items=[])
    metadata: list[tuple[float, AttachmentUploadResponse]] = []
    for path in tenant_dir.glob("att_*.json"):
        if path.name.endswith(".manifest.json"):
            continue
        item = _read_attachment_metadata(path)
        if item is not None:
            metadata.append((path.stat().st_mtime, item))
    metadata.sort(key=lambda item: item[0], reverse=True)
    return AttachmentListResponse(items=[item for _, item in metadata])


def _delete_attachment_files(tenant_dir: Path, attachment_id: str) -> bool:
    deleted = False
    for path in (
        tenant_dir / f"{attachment_id}.bin",
        tenant_dir / f"{attachment_id}.json",
        tenant_dir / f"{attachment_id}.manifest.json",
    ):
        if path.exists():
            path.unlink()
            deleted = True
    extract_dir = tenant_dir / attachment_id
    if extract_dir.exists():
        if not extract_dir.is_dir():
            raise PublicAPIError(500, "attachment_store_error", "attachment store is inconsistent")
        shutil.rmtree(extract_dir)
        deleted = True
    return deleted


@router.post(
    "/attachments/bulk-delete",
    response_model=AttachmentBulkDeleteResponse,
    responses=error_responses(401, 403, 422, 500),
)
async def bulk_delete_attachments(
    body: AttachmentBulkDeleteRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> AttachmentBulkDeleteResponse:
    tenant_dir = _tenant_attachment_dir(request, principal)
    deleted: list[str] = []
    failed: list[AttachmentBulkDeleteFailure] = []
    for attachment_id in body.ids:
        if _delete_attachment_files(tenant_dir, attachment_id):
            deleted.append(attachment_id)
        else:
            failed.append(
                AttachmentBulkDeleteFailure(
                    id=attachment_id,
                    code="attachment_not_found",
                    message="attachment was not found",
                )
            )
    return AttachmentBulkDeleteResponse(deleted=deleted, failed=failed)


@router.delete(
    "/attachments/{attachment_id}",
    response_model=AttachmentDeleteResponse,
    responses=error_responses(401, 403, 404, 422, 500),
)
async def delete_attachment(
    attachment_id: str,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> AttachmentDeleteResponse:
    safe_id = _safe_attachment_id(attachment_id)
    tenant_dir = _tenant_attachment_dir(request, principal)
    if not _delete_attachment_files(tenant_dir, safe_id):
        raise PublicAPIError(404, "attachment_not_found", "attachment was not found")
    return AttachmentDeleteResponse(id=safe_id, deleted=True)


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
    request: Request,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SubmittedRunResponse:
    if body.vibe_coding and not await _vibe_coding_enabled(request):
        raise PublicAPIError(
            409,
            "vibe_coding_disabled",
            "Vibe Coding is disabled in system settings",
        )
    submitted = await service.submit(
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        actor_role=principal.role,
        message=body.message,
        mode=body.mode,
        agent_ids=body.agent_ids,
        workflow_id=body.workflow_id,
        allow_workflow_adjustment=body.allow_workflow_adjustment,
        conversation_id=body.conversation_id,
        reference_conversation_id=body.reference_conversation_id,
        attachment_ids=body.attachment_ids,
        direct_model=body.direct_model,
        vibe_coding=body.vibe_coding,
        skip_evolution_proposal=body.skip_evolution_proposal,
        idempotency_key=idempotency_key,
    )
    await _record_run_submit_audit(request, principal, body, submitted)
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


@router.post(
    "/{run_id}/accept-repair",
    response_model=SubmittedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def accept_self_repair(
    run_id: UUID,
    body: AcceptSelfRepairRequest,
    service: Annotated[RunServiceProtocol, Depends(_run_service)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission("run:create"))],
) -> SubmittedRunResponse:
    try:
        submitted = await service.accept_self_repair(
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
