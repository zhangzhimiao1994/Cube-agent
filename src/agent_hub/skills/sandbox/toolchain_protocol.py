"""v7 SDK supply data: separate from Node/Python install and project execution.

Valid messages and stored receipts are not permission. The service authorizes
the exact scope/plan; Root independently rederives it from captured project
controls and its own catalog before publishing an SDK. No model commands/URLs.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.skills.sandbox.project import OriginalProjectReference, ProjectInvocation
from agent_hub.skills.sandbox.toolchain_catalog import (
    ToolchainPlanScope,
    ToolchainSupplyPlan,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ExecutionId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]
TOOLCHAIN_DRAIN_SECONDS = 16
TOOLCHAIN_TERMINATION_SECONDS = 20
ToolchainAction = Literal[
    "plan_project_toolchain",
    "supply_project_toolchain",
    "inspect_project_toolchain_supply",
    "inspect_project_toolchain",
]
ToolchainError = Literal[
    "toolchain_request_invalid",
    "toolchain_unsupported",
    "toolchain_unavailable",
    "toolchain_configuration_missing",
    "toolchain_configuration_untrusted",
    "toolchain_catalog_no_match",
    "toolchain_plan_changed",
    "toolchain_scope_changed",
    "toolchain_capacity",
    "toolchain_timeout",
    "toolchain_consumed",
    "toolchain_unknown",
    "toolchain_cleanup_incomplete",
    "toolchain_artifact_integrity",
    "toolchain_not_ready",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_versions(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("toolchain_request_invalid")
        return value


class _Envelope(_Strict):
    version: Literal[7] = 7

    @field_validator("version", mode="before")
    @classmethod
    def integer_protocol(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("toolchain_request_invalid")
        return value


class ToolchainInvocation(ProjectInvocation):
    original_reference: OriginalProjectReference
    scope: ToolchainPlanScope
    install_allowed: Literal[False] = False

    @field_validator("install_allowed", mode="before")
    @classmethod
    def no_legacy_install_flag(cls, value: object) -> object:
        if value is not False:
            raise ValueError("toolchain_request_invalid")
        return value

    @model_validator(mode="after")
    def captured_scope(self) -> Self:
        s = self.scope
        if (
            self.sandbox_profile != "workspace_write"
            or (s.tenant_id, s.run_id, s.project_id, s.session_id)
            != (self.tenant_id, self.run_id, self.project_id, self.session_id)
            or s.source_sha256 != self.original_reference.input_source_sha256
            or s.contract_sha256 != self.original_reference.contract_sha256
        ):
            raise ValueError("toolchain_scope_changed")
        return self


def base_toolchain_invocation(invocation: ToolchainInvocation) -> ProjectInvocation:
    return ProjectInvocation.model_validate(
        {name: getattr(invocation, name) for name in ProjectInvocation.model_fields}, strict=True
    )


def require_plan_scope(plan: ToolchainSupplyPlan, invocation: ToolchainInvocation) -> None:
    checked = ToolchainSupplyPlan.from_json(plan.to_json())
    if (
        checked.scope != invocation.scope
        or checked.execution_bytes
        != invocation.workspace_limit_bytes + invocation.source_limit_bytes
        or checked.source_limit_bytes != invocation.source_limit_bytes
        or checked.source_entry_limit != 10 * invocation.file_limit
    ):
        raise ValueError("toolchain_scope_changed")


class ToolchainReference(_Strict):
    """Compact measured identity, never a host path, lease or execution receipt."""

    schema_version: Literal[1] = 1
    kind: Literal["go_toolchain_v1"] = "go_toolchain_v1"
    artifact_sha256: Digest
    archive_sha256: Digest
    tree_sha256: Digest
    binary_sha256: Digest
    gofmt_sha256: Digest
    total_bytes: int = Field(gt=0, le=2**63 - 1)
    file_count: int = Field(ge=3, le=1_000_000)
    version: str = Field(pattern=r"^1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$", max_length=32)
    platform: Literal["linux_amd64"]

    # SDK version is text, unlike envelope/schema version numbers.
    @field_validator("version", mode="before")
    @classmethod
    def sdk_version_text(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("toolchain_request_invalid")
        return value

    def require_plan(self, plan: ToolchainSupplyPlan) -> None:
        checked = ToolchainReference.model_validate_json(self.model_dump_json(), strict=True)
        a = plan.artifact
        if (
            checked.artifact_sha256 != a.artifact_sha256
            or checked.archive_sha256 != a.sha256
            or checked.version != a.version
            or checked.platform != a.platform
            or checked.total_bytes > a.unpacked_bytes_limit
            or checked.file_count > a.archive_entries_limit
        ):
            raise ValueError("toolchain_plan_changed")


class ToolchainSupplyReceipt(_Strict):
    schema_version: Literal[1] = 1
    plan: ToolchainSupplyPlan
    supply_execution_id: ExecutionId
    environment: ToolchainReference
    reused: bool

    @model_validator(mode="after")
    def measured_plan(self) -> Self:
        ToolchainSupplyPlan.from_json(self.plan.to_json())
        self.environment.require_plan(self.plan)
        return self


class ToolchainSupplyStatus(_Strict):
    state: Literal["absent", "in_progress", "supplied", "unknown"]
    plan_sha256: Digest
    supply_execution_id: ExecutionId
    receipt: ToolchainSupplyReceipt | None = None

    @model_validator(mode="after")
    def complete_outcome(self) -> Self:
        if (
            (self.state == "supplied") != (self.receipt is not None)
            or self.receipt is not None
            and (
                self.receipt.plan.sha256 != self.plan_sha256
                or self.receipt.supply_execution_id != self.supply_execution_id
            )
        ):
            raise ValueError("toolchain_request_invalid")
        return self


class ToolchainRequest(_Envelope):
    action: ToolchainAction
    invocation: ToolchainInvocation
    plan: ToolchainSupplyPlan | None = None
    supply_execution_id: ExecutionId | None = None
    expected_environment: ToolchainReference | None = None

    @model_validator(mode="after")
    def exclusive_actions(self) -> Self:
        planning = self.action == "plan_project_toolchain"
        inspecting = self.action == "inspect_project_toolchain_supply"
        profile = self.action == "inspect_project_toolchain"
        if (
            planning != (self.plan is None)
            or inspecting != (self.supply_execution_id is not None)
            or profile != (self.expected_environment is not None)
            or self.supply_execution_id == self.invocation.execution_id
        ):
            raise ValueError("toolchain_request_invalid")
        if self.plan is not None:
            require_plan_scope(self.plan, self.invocation)
            if self.expected_environment is not None:
                self.expected_environment.require_plan(self.plan)
        return self


class ToolchainResponse(_Envelope):
    action: ToolchainAction
    execution_id: ExecutionId
    scope: ToolchainPlanScope
    ok: bool
    error: ToolchainError | None = None
    plan: ToolchainSupplyPlan | None = None
    missing: bool | None = None
    environment: ToolchainReference | None = None
    receipt: ToolchainSupplyReceipt | None = None
    status: ToolchainSupplyStatus | None = None

    @model_validator(mode="after")
    def exclusive_result(self) -> Self:
        present = tuple(
            value is not None
            for value in (self.plan, self.missing, self.environment, self.receipt, self.status)
        )
        if not self.ok:
            if self.error is None or any(present):
                raise ValueError("toolchain_request_invalid")
            return self
        if self.error is not None:
            raise ValueError("toolchain_request_invalid")
        shapes = {
            "plan_project_toolchain": (True, True, not self.missing, False, False),
            "supply_project_toolchain": (False, False, False, True, False),
            "inspect_project_toolchain_supply": (False, False, False, False, True),
            "inspect_project_toolchain": (False, False, True, False, False),
        }
        if present != shapes[self.action]:
            raise ValueError("toolchain_request_invalid")
        if self.plan is not None:
            if self.plan.scope != self.scope:
                raise ValueError("toolchain_scope_changed")
            if self.environment is not None:
                self.environment.require_plan(self.plan)
        return self

    def require_for(self, request: ToolchainRequest) -> None:
        request = parse_toolchain_request(request.model_dump_json().encode())
        response = parse_toolchain_response(self.model_dump_json().encode())
        if (response.action, response.execution_id, response.scope) != (
            request.action,
            request.invocation.execution_id,
            request.invocation.scope,
        ):
            raise ValueError("toolchain_scope_changed")
        if response.plan is not None:
            require_plan_scope(response.plan, request.invocation)
        if response.receipt is not None and (
            request.plan is None
            or response.receipt.plan.sha256 != request.plan.sha256
            or response.receipt.supply_execution_id != request.invocation.execution_id
        ):
            raise ValueError("toolchain_plan_changed")
        if response.status is not None and (
            request.plan is None
            or response.status.plan_sha256 != request.plan.sha256
            or response.status.supply_execution_id != request.supply_execution_id
        ):
            raise ValueError("toolchain_plan_changed")
        if (
            response.action == "inspect_project_toolchain"
            and response.ok
            and response.environment != request.expected_environment
        ):
            raise ValueError("toolchain_plan_changed")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("toolchain_request_invalid")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("toolchain_request_invalid")


def _json(raw: bytes) -> None:
    if type(raw) is not bytes or not 0 < len(raw) <= 512 * 1024:
        raise ValueError("toolchain_request_invalid")
    if type(json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)) is not dict:
        raise ValueError("toolchain_request_invalid")


def parse_toolchain_request(raw: bytes) -> ToolchainRequest:
    try:
        _json(raw)
        return ToolchainRequest.model_validate_json(raw, strict=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError("toolchain_request_invalid") from None


def parse_toolchain_response(raw: bytes) -> ToolchainResponse:
    try:
        _json(raw)
        return ToolchainResponse.model_validate_json(raw, strict=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError("toolchain_request_invalid") from None
