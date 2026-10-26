"""Strict v10 metadata preparation contracts, without IO or install authority.

Receipts bind a nonready approval plan to an index digest. They neither authorize
installation nor claim that an index is currently present or trusted on disk.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_input_supply_protocol import ExecutionId
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    MetadataPurpose,
    PythonMetadataDraft,
    PythonMetadataPlan,
    PythonMetadataPlanningRequest,
    PythonMetadataScope,
)
from agent_hub.skills.sandbox.python_project_profile import Digest, PythonProjectProfileScope

_REQUEST_INVALID = "metadata_prepare_request_invalid"
_RESPONSE_INVALID = "metadata_prepare_response_invalid"
_PREPARATION_ID = re.compile(r"metadata_prepare_[a-f0-9]{32}")
_INSPECTION_ID = re.compile(r"metadata_inspect_[a-f0-9]{32}")

type MetadataPreparationAction = Literal[
    "prepare_python_dependency_metadata", "inspect_python_dependency_metadata_preparation",
]
type MetadataPreparationError = Literal[
    "metadata_prepare_request_invalid", "metadata_prepare_unavailable",
    "metadata_prepare_plan_changed", "metadata_prepare_scope_changed",
    "metadata_prepare_catalog_changed", "metadata_prepare_draft_changed",
    "metadata_prepare_artifact_integrity", "metadata_prepare_quota", "metadata_prepare_timeout",
    "metadata_prepare_consumed", "metadata_prepare_unknown", "metadata_prepare_cleanup_incomplete",
    "metadata_prepare_metadata_untrusted",
]


def _fields(value: object, expected: type[BaseModel]) -> object:
    if isinstance(value, BaseModel):
        if type(value) is not expected:
            raise ValueError(_REQUEST_INVALID)
        # model_dump can erase extra copied fields, and older DTOs skip revalidation.
        return dict(vars(value)) | (value.model_extra or {})
    return value


def _planning_fields(value: object) -> object:
    fields = _fields(value, PythonMetadataPlanningRequest)
    if isinstance(fields, dict):
        fields = fields.copy()
        for name, expected in (
            ("invocation", ProjectInvocation), ("scope", PythonMetadataScope),
            ("draft", PythonMetadataDraft),
        ):
            if name in fields:
                fields[name] = _fields(fields[name], expected)
    return fields


def _plan_fields(value: object) -> object:
    fields = _fields(value, PythonMetadataPlan)
    if isinstance(fields, dict):
        fields = fields.copy()
        for name, expected in (("scope", PythonMetadataScope), ("draft", PythonMetadataDraft)):
            if name in fields:
                fields[name] = _fields(fields[name], expected)
    return fields


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def untrusted_model_fields(cls, value: object) -> object:
        return _fields(value, cls)

    @field_validator("version", "schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_versions(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError(_REQUEST_INVALID)
        return value

    @field_validator("preparation_execution_id", mode="after", check_fields=False)
    @classmethod
    def preparation_id(cls, value: str | None) -> str | None:
        if value is not None and _PREPARATION_ID.fullmatch(value) is None:
            raise ValueError(_REQUEST_INVALID)
        return value


class _Envelope(_Strict):
    version: Literal[10] = 10
    action: MetadataPreparationAction


class PythonMetadataPreparationRequest(_Envelope):
    planning: PythonMetadataPlanningRequest
    expected_plan_sha256: Digest
    preparation_execution_id: ExecutionId | None = None

    @field_validator("planning", mode="before")
    @classmethod
    def validated_planning(cls, value: object) -> object:
        return _planning_fields(value)

    @model_validator(mode="after")
    def action_identity(self) -> Self:
        inspecting = self.action == "inspect_python_dependency_metadata_preparation"
        namespace = _INSPECTION_ID if inspecting else _PREPARATION_ID
        if (namespace.fullmatch(self.planning.invocation.execution_id) is None
                or inspecting != (self.preparation_execution_id is not None)
                or self.preparation_execution_id == self.planning.invocation.execution_id):
            raise ValueError(_REQUEST_INVALID)
        return self

    @property
    def invocation(self) -> ProjectInvocation:
        return self.planning.invocation


class PythonMetadataPreparationReceipt(_Strict):
    schema_version: Literal[1] = 1
    purpose: MetadataPurpose = "python_dependency_metadata"
    plan: PythonMetadataPlan
    preparation_execution_id: ExecutionId
    index_sha256: Digest
    index_record_count: int = Field(ge=1, le=1000)

    @field_validator("plan", mode="before")
    @classmethod
    def validated_plan(cls, value: object) -> object:
        return _plan_fields(value)

    @model_validator(mode="after")
    def complete_metadata_index(self) -> Self:
        if self.plan.mode == "ready" or self.index_record_count != self.plan.catalog_count:
            raise ValueError(_REQUEST_INVALID)
        return self


class PythonMetadataPreparationStatus(_Strict):
    state: Literal["absent", "in_progress", "prepared", "unknown"]
    plan_sha256: Digest
    preparation_execution_id: ExecutionId
    receipt: PythonMetadataPreparationReceipt | None = None

    @field_validator("receipt", mode="before")
    @classmethod
    def validated_receipt(cls, value: object) -> object:
        return _fields(value, PythonMetadataPreparationReceipt)

    @model_validator(mode="after")
    def exclusive_receipt(self) -> Self:
        if ((self.state == "prepared") != (self.receipt is not None)
                or self.receipt is not None and (
                    self.receipt.plan.sha256 != self.plan_sha256
                    or self.receipt.preparation_execution_id != self.preparation_execution_id)):
            raise ValueError(_REQUEST_INVALID)
        return self


class PythonMetadataPreparationResponse(_Envelope):
    scope: PythonProjectProfileScope
    ok: bool
    error: MetadataPreparationError | None = None
    receipt: PythonMetadataPreparationReceipt | None = None
    status: PythonMetadataPreparationStatus | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def validated_transport_scope(cls, value: object) -> object:
        return _fields(value, PythonProjectProfileScope)

    @field_validator("receipt", mode="before")
    @classmethod
    def validated_receipt(cls, value: object) -> object:
        return _fields(value, PythonMetadataPreparationReceipt)

    @field_validator("status", mode="before")
    @classmethod
    def validated_status(cls, value: object) -> object:
        return _fields(value, PythonMetadataPreparationStatus)

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if not self.ok:
            if self.error is None or self.receipt is not None or self.status is not None:
                raise ValueError(_RESPONSE_INVALID)
        elif (self.error is not None
                or (self.receipt is not None, self.status is not None) != (
                    self.action == "prepare_python_dependency_metadata",
                    self.action == "inspect_python_dependency_metadata_preparation")):
            raise ValueError(_RESPONSE_INVALID)
        return self

    def require_for(self, request: PythonMetadataPreparationRequest) -> None:
        if type(request) is not PythonMetadataPreparationRequest:
            raise ValueError(_REQUEST_INVALID)
        if type(self) is not PythonMetadataPreparationResponse:
            raise ValueError(_RESPONSE_INVALID)
        checked_request = PythonMetadataPreparationRequest.model_validate(request, strict=True)
        checked = PythonMetadataPreparationResponse.model_validate(self, strict=True)
        if (checked.action != checked_request.action
                or checked.scope != PythonProjectProfileScope.for_invocation(
                    checked_request.invocation)):
            raise ValueError(_RESPONSE_INVALID)
        receipt = checked.receipt
        if checked.status is not None:
            if (checked.status.plan_sha256 != checked_request.expected_plan_sha256
                    or checked.status.preparation_execution_id
                    != checked_request.preparation_execution_id):
                raise ValueError(_RESPONSE_INVALID)
            receipt = checked.status.receipt
        if receipt is not None:
            planning = checked_request.planning
            expected_id = (checked_request.preparation_execution_id
                if checked.status is not None else checked_request.invocation.execution_id)
            if (receipt.preparation_execution_id != expected_id
                    or receipt.plan.sha256 != checked_request.expected_plan_sha256
                    or receipt.plan.scope != planning.scope
                    or receipt.plan.draft != planning.draft
                    or receipt.plan.root_config_sha256 != planning.expected_root_config_sha256
                    or receipt.plan.catalog_sha256 != planning.expected_catalog_sha256):
                raise ValueError(_RESPONSE_INVALID)
