"""Compact v9 metadata planning values, without IO or execution authority.

Plans describe planner inputs, not measured cache state. Transport identity and
remaining deadlines belong to the envelope and never affect the approval hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_project_profile import (
    Digest,
    PythonProjectProfileScope,
    ScopedName,
)

_EMPTY_IDENTITY_SHA256 = hashlib.sha256(b"[]").hexdigest()
_REQUEST_INVALID = "metadata_plan_request_invalid"
_RESPONSE_INVALID = "metadata_plan_response_invalid"
_PLAN_INVALID = "metadata_plan_invalid"
type MetadataPlanningError = Literal[
    "metadata_plan_request_invalid", "metadata_plan_unavailable", "metadata_plan_draft_changed",
    "metadata_plan_catalog_changed", "metadata_plan_metadata_untrusted",
    "metadata_plan_cache_untrusted", "metadata_plan_quota", "metadata_plan_timeout",
]
type MetadataPurpose = Literal["python_dependency_metadata"]
Actor = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.-]*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always",
    )

    @field_validator("version", "schema_version", "extractor_version", mode="before",
                     check_fields=False)
    @classmethod
    def integer_versions(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError(_REQUEST_INVALID)
        return value


def _base_invocation(value: object) -> object:
    if isinstance(value, ProjectInvocation):
        if type(value) is not ProjectInvocation:
            raise ValueError(_REQUEST_INVALID)
        # Preserve Python types and extra copied fields when rechecking frozen instances.
        return ProjectInvocation.model_validate(vars(value), strict=True)
    return value


class PythonMetadataScope(_Strict):
    tenant_id: UUID
    run_id: UUID
    user_id: UUID
    project_id: ScopedName
    session_id: ScopedName
    actor: Actor
    actor_role: Literal["super_admin", "admin", "operator"]
    request_sha256: Digest
    permissions_sha256: Digest

    def require_invocation(self, base: ProjectInvocation) -> None:
        if type(base) is not ProjectInvocation:
            raise ValueError(_REQUEST_INVALID)
        checked = ProjectInvocation.model_validate(vars(base), strict=True)
        scope = PythonMetadataScope.model_validate(self, strict=True)
        if any(getattr(scope, name) != getattr(checked, name) for name in (
            "tenant_id", "run_id", "project_id", "session_id",
        )):
            raise ValueError(_REQUEST_INVALID)


class PythonMetadataDraft(_Strict):
    source_sha256: Digest
    acceptance_sha256: Digest
    manifest_sha256: Digest
    lock_sha256: Digest


class PythonMetadataPlan(_Strict):
    schema_version: Literal[1] = 1
    purpose: MetadataPurpose = "python_dependency_metadata"
    scope: PythonMetadataScope
    draft: PythonMetadataDraft
    root_config_sha256: Digest
    catalog_sha256: Digest
    catalog_identity_sha256: Digest
    catalog_count: int = Field(ge=1, le=1000)
    catalog_bytes: int = Field(gt=0, lt=2**63)
    missing_identity_sha256: Digest
    missing_count: int = Field(ge=0, le=1000)
    missing_bytes: int = Field(ge=0, lt=2**63)
    max_total_bytes: int = Field(gt=0, le=64 * 1024**2)
    wheel_max_bytes: int = Field(gt=0, lt=2**63)
    min_free_bytes: int = Field(ge=0, lt=2**63)
    fetch_timeout_seconds: int = Field(ge=1, le=600)
    extractor_version: Literal[1] = 1
    mode: Literal["ready", "publish_cached", "download_required"]
    metadata_index_sha256: Digest | None = None

    @model_validator(mode="after")
    def consistent_metadata_state(self) -> Self:
        if (self.catalog_identity_sha256 == _EMPTY_IDENTITY_SHA256
                or self.catalog_bytes < self.catalog_count
                or self.missing_bytes < self.missing_count
                or self.catalog_bytes - self.missing_bytes < self.catalog_count - self.missing_count):
            raise ValueError(_PLAN_INVALID)
        if self.mode == "ready":
            if self.metadata_index_sha256 is None:
                raise ValueError(_PLAN_INVALID)
        elif (self.metadata_index_sha256 is not None
              or self.catalog_bytes > min(self.max_total_bytes, self.wheel_max_bytes)):
            raise ValueError(_PLAN_INVALID)
        if self.mode == "download_required":
            if (not 0 < self.missing_count <= self.catalog_count
                    or not 0 < self.missing_bytes <= self.catalog_bytes
                    or self.missing_identity_sha256 == _EMPTY_IDENTITY_SHA256):
                raise ValueError(_PLAN_INVALID)
            if self.missing_count == self.catalog_count:
                if (self.missing_bytes != self.catalog_bytes
                        or self.missing_identity_sha256 != self.catalog_identity_sha256):
                    raise ValueError(_PLAN_INVALID)
            elif (self.missing_bytes >= self.catalog_bytes
                  or self.missing_identity_sha256 == self.catalog_identity_sha256):
                raise ValueError(_PLAN_INVALID)
        elif (self.missing_count != 0 or self.missing_bytes != 0
              or self.missing_identity_sha256 != _EMPTY_IDENTITY_SHA256):
            raise ValueError(_PLAN_INVALID)
        return self

    @property
    def sha256(self) -> str:
        checked = PythonMetadataPlan.model_validate(self, strict=True)
        raw = json.dumps(checked.model_dump(mode="json"), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        return hashlib.sha256(raw).hexdigest()


class _Envelope(_Strict):
    version: Literal[9] = 9
    action: Literal["plan_python_dependency_metadata"] = "plan_python_dependency_metadata"


class PythonMetadataPlanningRequest(_Envelope):
    purpose: MetadataPurpose = "python_dependency_metadata"
    invocation: ProjectInvocation
    scope: PythonMetadataScope
    draft: PythonMetadataDraft
    expected_root_config_sha256: Digest
    expected_catalog_sha256: Digest

    @field_validator("invocation", mode="before")
    @classmethod
    def validated_base_invocation(cls, value: object) -> object:
        return _base_invocation(value)

    @model_validator(mode="after")
    def read_only_scope(self) -> Self:
        if (type(self.invocation) is not ProjectInvocation
                or self.invocation.sandbox_profile != "restricted"
                or self.invocation.install_allowed is not False):
            raise ValueError(_REQUEST_INVALID)
        self.scope.require_invocation(self.invocation)
        return self


class PythonMetadataPlanningResponse(_Envelope):
    scope: PythonProjectProfileScope
    ok: bool
    error: MetadataPlanningError | None = None
    plan: PythonMetadataPlan | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def validated_transport_scope(cls, value: object) -> object:
        if isinstance(value, PythonProjectProfileScope):
            if type(value) is not PythonProjectProfileScope:
                raise ValueError(_RESPONSE_INVALID)
            return PythonProjectProfileScope.model_validate(vars(value), strict=True)
        return value

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if (self.ok and (self.error is not None or self.plan is None)
                or not self.ok and (self.error is None or self.plan is not None)):
            raise ValueError(_RESPONSE_INVALID)
        return self

    def require_for(self, request: PythonMetadataPlanningRequest) -> None:
        if type(request) is not PythonMetadataPlanningRequest:
            raise ValueError(_REQUEST_INVALID)
        if type(self) is not PythonMetadataPlanningResponse:
            raise ValueError(_RESPONSE_INVALID)
        checked_request = PythonMetadataPlanningRequest.model_validate(request, strict=True)
        checked = PythonMetadataPlanningResponse.model_validate(self, strict=True)
        if checked.scope != PythonProjectProfileScope.for_invocation(checked_request.invocation):
            raise ValueError(_RESPONSE_INVALID)
        if checked.plan is not None and (
            checked.plan.scope != checked_request.scope
            or checked.plan.draft != checked_request.draft
            or checked.plan.root_config_sha256 != checked_request.expected_root_config_sha256
            or checked.plan.catalog_sha256 != checked_request.expected_catalog_sha256
        ):
            raise ValueError(_RESPONSE_INVALID)
