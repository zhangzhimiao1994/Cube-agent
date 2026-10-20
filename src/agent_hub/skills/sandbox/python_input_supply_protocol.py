"""Separate v5 input-supply envelopes; plans and receipts never grant authority."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectProbeInvocation,
    PythonProjectScope,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ExecutionId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]
SupplyAction = Literal[
    "plan_python_project_inputs", "supply_python_project_inputs", "inspect_python_project_input_supply",
]
SupplyError = Literal[
    "input_supply_request_invalid", "input_supply_unsupported", "input_supply_unavailable",
    "input_supply_configuration_missing", "input_supply_configuration_untrusted",
    "input_supply_catalog_unavailable", "input_supply_plan_changed", "input_supply_scope_changed",
    "input_supply_artifact_missing", "input_supply_artifact_integrity", "input_supply_quota",
    "input_supply_timeout", "input_supply_consumed", "input_supply_unknown",
    "input_supply_cleanup_incomplete",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def strict_scalar_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("input_supply_request_invalid")
        return value


class _Envelope(_Strict):
    version: Literal[5] = 5

    @field_validator("version", mode="before")
    @classmethod
    def integer_envelope_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("input_supply_request_invalid")
        return value


class PythonInputSupplyArtifact(_Strict):
    filename: str
    name: str
    version: str
    sha256: Digest
    size_bytes: int = Field(gt=0, le=64 * 1024**2)

    @model_validator(mode="after")
    def universal_wheel(self) -> Self:
        PythonWheelBinding(self.filename, self.name, self.version, self.sha256)
        return self


class PythonSupplyInterpreter(_Strict):
    implementation: str
    version: str
    abi: str
    platform: str
    binary_sha256: Digest
    base_runtime_sha256: Digest

    @model_validator(mode="after")
    def supported_identity(self) -> Self:
        PythonInterpreterIdentity.from_payload(self.model_dump())
        return self


class PythonInputSupplyPlan(_Strict):
    schema_version: Literal[1] = 1
    scope: PythonProjectScope
    declaration_sha256: Digest
    manifest_sha256: Digest
    lock_sha256: Digest
    catalog_sha256: Digest
    interpreter: PythonSupplyInterpreter
    artifacts: tuple[PythonInputSupplyArtifact, ...] = Field(min_length=1, max_length=1000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("input_supply_request_invalid")
        return value

    @model_validator(mode="after")
    def unique_sorted_artifacts(self) -> Self:
        names = tuple(item.name for item in self.artifacts)
        if names != tuple(sorted(set(names))) or len({item.filename for item in self.artifacts}) != len(names):
            raise ValueError("input_supply_request_invalid")
        return self

    @property
    def sha256(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    def require_scope(self, invocation: PythonProjectProbeInvocation) -> None:
        expected = {name: getattr(invocation, name) for name in (
            "tenant_id", "run_id", "project_id", "session_id", "original_reference")}
        if self.scope != PythonProjectScope.model_validate(expected, strict=True):
            raise ValueError("input_supply_scope_changed")


class PythonInputSupplyRequest(_Envelope):
    action: SupplyAction
    invocation: PythonProjectProbeInvocation
    expected_plan_sha256: Digest | None = None
    supply_execution_id: ExecutionId | None = None

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        planning = self.action == "plan_python_project_inputs"
        inspecting = self.action == "inspect_python_project_input_supply"
        if (planning != (self.expected_plan_sha256 is None)
                or inspecting != (self.supply_execution_id is not None)
                or self.supply_execution_id == self.invocation.execution_id):
            raise ValueError("input_supply_request_invalid")
        return self


class PythonInputSupplyReceipt(_Strict):
    schema_version: Literal[1] = 1
    plan: PythonInputSupplyPlan
    supply_execution_id: ExecutionId
    published: tuple[str, ...]
    reused: tuple[str, ...]

    @model_validator(mode="after")
    def complete_partition(self) -> Self:
        expected = {item.filename for item in self.plan.artifacts}
        if (self.published != tuple(sorted(set(self.published)))
                or self.reused != tuple(sorted(set(self.reused)))
                or set(self.published) & set(self.reused)
                or set(self.published) | set(self.reused) != expected
                or type(self.schema_version) is not int):
            raise ValueError("input_supply_request_invalid")
        return self


class PythonInputSupplyStatus(_Strict):
    state: Literal["absent", "in_progress", "supplied", "unknown"]
    plan_sha256: Digest
    supply_execution_id: ExecutionId
    receipt: PythonInputSupplyReceipt | None = None

    @model_validator(mode="after")
    def exclusive_receipt(self) -> Self:
        if ((self.state == "supplied") != (self.receipt is not None)
                or self.receipt is not None and (
                    self.receipt.plan.sha256 != self.plan_sha256
                    or self.receipt.supply_execution_id != self.supply_execution_id)):
            raise ValueError("input_supply_request_invalid")
        return self


class PythonInputSupplyResponse(_Envelope):
    action: SupplyAction
    execution_id: ExecutionId
    ok: bool
    error: SupplyError | None = None
    plan: PythonInputSupplyPlan | None = None
    missing_wheels: tuple[str, ...] | None = None
    receipt: PythonInputSupplyReceipt | None = None
    status: PythonInputSupplyStatus | None = None

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if not self.ok:
            if self.error is None or any(value is not None for value in (
                    self.plan, self.missing_wheels, self.receipt, self.status)):
                raise ValueError("input_supply_request_invalid")
            return self
        if self.error is not None:
            raise ValueError("input_supply_request_invalid")
        expected = {
            "plan_python_project_inputs": (True, True, False, False),
            "supply_python_project_inputs": (False, False, True, False),
            "inspect_python_project_input_supply": (False, False, False, True),
        }[self.action]
        if tuple(value is not None for value in (
                self.plan, self.missing_wheels, self.receipt, self.status)) != expected:
            raise ValueError("input_supply_request_invalid")
        if self.plan is not None:
            assert self.missing_wheels is not None
            if (self.missing_wheels != tuple(sorted(set(self.missing_wheels)))
                    or not set(self.missing_wheels) <= {a.filename for a in self.plan.artifacts}):
                raise ValueError("input_supply_request_invalid")
        return self

    def require_for(self, request: PythonInputSupplyRequest) -> None:
        if (self.execution_id != request.invocation.execution_id or self.action != request.action):
            raise ValueError("input_supply_request_invalid")
        if self.plan is not None:
            self.plan.require_scope(request.invocation)
        if self.receipt is not None:
            self.receipt.plan.require_scope(request.invocation)
            if (self.receipt.plan.sha256 != request.expected_plan_sha256
                    or self.receipt.supply_execution_id != request.invocation.execution_id):
                raise ValueError("input_supply_request_invalid")
        if self.status is not None:
            if (self.status.plan_sha256 != request.expected_plan_sha256
                    or self.status.supply_execution_id != request.supply_execution_id):
                raise ValueError("input_supply_request_invalid")
            if self.status.receipt is not None:
                self.status.receipt.plan.require_scope(request.invocation)
