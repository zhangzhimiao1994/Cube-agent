"""Tenant-scoped versioned platform configuration endpoints."""

from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from agent_hub.api.dependencies import require_permission
from agent_hub.api.errors import PublicAPIError, error_responses
from agent_hub.api.schemas import (
    ConfigDiffResponse,
    ConfigHistoryResponse,
    ConfigRevisionResponse,
    PublishedRevisionResponse,
    ValidationResponse,
)
from agent_hub.auth.models import AuthenticatedPrincipal
from agent_hub.config.repository import (
    ConfigNotFoundError,
    ConfigRevision,
    ConfigStatusError,
)
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import (
    ConfigValidationError,
    PostCommitNotificationError,
)

router = APIRouter(
    prefix="/api/v1/config",
    tags=["config"],
    responses=error_responses(401, 405, 500, 503),
)


class ConfigurationService(Protocol):
    async def create_draft(
        self, tenant_id: UUID, actor_id: UUID, document: object
    ) -> ConfigRevision: ...

    async def publish_revision(
        self, tenant_id: UUID, revision_id: UUID, actor_id: UUID
    ) -> ConfigRevision: ...

    async def rollback(
        self, tenant_id: UUID, source_version: int, actor_id: UUID
    ) -> ConfigRevision: ...

    async def get_version(self, tenant_id: UUID, version: int) -> ConfigRevision: ...

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None: ...

    async def list_versions(
        self, tenant_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> list[ConfigRevision]: ...


def _config_service(request: Request) -> ConfigurationService:
    service = getattr(request.app.state, "config_service", None)
    if service is None:
        raise PublicAPIError(503, "service_unavailable", "service unavailable")
    return cast(ConfigurationService, service)


def _raise_config_error(error: Exception) -> None:
    if isinstance(error, ConfigValidationError):
        raise PublicAPIError(422, "config_validation", "configuration is invalid") from None
    if isinstance(error, ConfigNotFoundError):
        raise PublicAPIError(404, "config_not_found", "configuration was not found") from None
    if isinstance(error, ConfigStatusError):
        raise PublicAPIError(409, "config_conflict", "configuration state conflict") from None
    raise error


@router.post(
    "/validate",
    response_model=ValidationResponse,
    responses=error_responses(403, 413, 422),
)
async def validate_config(
    body: PlatformConfig,
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:write"))
    ],
) -> ValidationResponse:
    del body, principal
    return ValidationResponse()


@router.post(
    "/drafts",
    response_model=ConfigRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(403, 404, 409, 413, 422),
)
async def create_draft(
    body: PlatformConfig,
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:write"))
    ],
) -> ConfigRevisionResponse:
    try:
        revision = await service.create_draft(
            principal.tenant_id,
            principal.user_id,
            body.model_dump(mode="json", exclude_unset=True),
        )
    except (ConfigValidationError, ConfigNotFoundError, ConfigStatusError) as error:
        _raise_config_error(error)
        raise AssertionError("unreachable")
    return ConfigRevisionResponse.from_revision(revision)


@router.post(
    "/drafts/{revision_id}/publish",
    response_model=PublishedRevisionResponse,
    responses=error_responses(403, 404, 409, 413, 422),
)
async def publish_draft(
    revision_id: UUID,
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:publish"))
    ],
) -> PublishedRevisionResponse:
    try:
        revision = await service.publish_revision(
            principal.tenant_id, revision_id, principal.user_id
        )
    except PostCommitNotificationError as error:
        revision = await service.get_version(principal.tenant_id, error.event.version)
        return PublishedRevisionResponse.from_published(revision, "failed")
    except (ConfigValidationError, ConfigNotFoundError, ConfigStatusError) as error:
        _raise_config_error(error)
        raise AssertionError("unreachable")
    return PublishedRevisionResponse.from_published(revision)


@router.get(
    "/current",
    response_model=ConfigRevisionResponse,
    responses=error_responses(404),
)
async def current_config(
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:read"))
    ],
) -> ConfigRevisionResponse:
    revision = await service.get_current(principal.tenant_id)
    if revision is None:
        raise PublicAPIError(404, "config_not_found", "configuration was not found")
    return ConfigRevisionResponse.from_revision(revision)


@router.get(
    "/history",
    response_model=ConfigHistoryResponse,
    responses=error_responses(422),
)
async def config_history(
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:read"))
    ],
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> ConfigHistoryResponse:
    revisions = await service.list_versions(
        principal.tenant_id, limit=limit, offset=offset
    )
    return ConfigHistoryResponse(
        items=[ConfigRevisionResponse.from_revision(item) for item in revisions],
        limit=limit,
        offset=offset,
    )


@router.get(
    "/history/{version}",
    response_model=ConfigRevisionResponse,
    responses=error_responses(404, 422),
)
async def config_version(
    version: int,
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:read"))
    ],
) -> ConfigRevisionResponse:
    try:
        revision = await service.get_version(principal.tenant_id, version)
    except ConfigNotFoundError as error:
        _raise_config_error(error)
        raise AssertionError("unreachable")
    return ConfigRevisionResponse.from_revision(revision)


@router.get(
    "/diff",
    response_model=ConfigDiffResponse,
    responses=error_responses(404, 422),
)
async def config_diff(
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:read"))
    ],
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
) -> ConfigDiffResponse:
    try:
        before = await service.get_version(principal.tenant_id, from_version)
        after = await service.get_version(principal.tenant_id, to_version)
    except ConfigNotFoundError as error:
        _raise_config_error(error)
        raise AssertionError("unreachable")
    diff = structured_diff(before.document, after.document)
    return ConfigDiffResponse.model_validate(
        {"from_version": from_version, "to_version": to_version, **diff}
    )


@router.post(
    "/history/{version}/rollback",
    response_model=PublishedRevisionResponse,
    responses=error_responses(403, 404, 409, 413, 422),
)
async def rollback_config(
    version: int,
    service: Annotated[ConfigurationService, Depends(_config_service)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_permission("config:publish"))
    ],
) -> PublishedRevisionResponse:
    try:
        revision = await service.rollback(
            principal.tenant_id, version, principal.user_id
        )
    except PostCommitNotificationError as error:
        revision = await service.get_version(principal.tenant_id, error.event.version)
        return PublishedRevisionResponse.from_published(revision, "failed")
    except (ConfigValidationError, ConfigNotFoundError, ConfigStatusError) as error:
        _raise_config_error(error)
        raise AssertionError("unreachable")
    return PublishedRevisionResponse.from_published(revision)


def structured_diff(before: object, after: object) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {
        "added": [],
        "removed": [],
        "changed": [],
    }
    _walk_diff(before, after, "", result)
    return result


def _walk_diff(
    before: object,
    after: object,
    path: str,
    result: dict[str, list[dict[str, object]]],
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(after_keys - before_keys):
            result["added"].append(
                {"path": _child_path(path, key), "value": after[key]}
            )
        for key in sorted(before_keys - after_keys):
            result["removed"].append(
                {"path": _child_path(path, key), "old_value": before[key]}
            )
        for key in sorted(before_keys & after_keys):
            _walk_diff(before[key], after[key], _child_path(path, key), result)
        return
    # Lists are atomic so reordering has deterministic, unsurprising semantics.
    if before != after:
        result["changed"].append(
            {"path": path or "/", "from": before, "to": after}
        )


def _child_path(path: str, key: object) -> str:
    segment = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{segment}"
