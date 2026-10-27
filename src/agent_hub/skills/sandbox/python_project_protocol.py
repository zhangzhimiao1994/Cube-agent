"""Python broker v4, distinct from the unchanged Node v2/v3 requests.

These envelopes identify a trusted stored recipe, not a grant. The caller must
authorize that recipe and the broker must independently observe its inputs.
"""

from __future__ import annotations

from typing import BinaryIO, Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_recipe import PythonProjectDeclaration
from agent_hub.capabilities.python_project_wheels import (
    PythonWheelBinding,
    bind_python_project_wheels,
)
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import (
    ContractProjectInvocation,
    OriginalProjectReference,
    ProjectInvocation,
)

PYTHON_PROJECT_NATIVE_UNCERTAIN_EXIT_CODE = 3


class PythonProjectInvocation(ContractProjectInvocation):
    recipe_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PythonProjectProbeInvocation(ProjectInvocation):
    original_reference: OriginalProjectReference
    install_allowed: Literal[False] = False

    @field_validator("install_allowed", mode="before")
    @classmethod
    def no_install_authority(cls, value: object) -> object:
        if type(value) is not bool or value is not False:
            raise ValueError("Python input probe cannot install")
        return value


class _PythonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[4] = 4

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Python broker version must be an integer")
        return value


class PythonProjectBrokerRequest(_PythonRequest):
    action: Literal["run_python_project"] = "run_python_project"
    invocation: PythonProjectInvocation
    product_transfer_version: Literal[1] | None = None

    @field_validator("product_transfer_version", mode="before")
    @classmethod
    def strict_transfer(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value != 1):
            raise ValueError("invalid product transfer version")
        return value


class PythonProjectProbeRequest(_PythonRequest):
    action: Literal["probe_python_project"] = "probe_python_project"
    invocation: PythonProjectProbeInvocation


class PythonProjectScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tenant_id: UUID
    run_id: UUID
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    session_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    original_reference: OriginalProjectReference


class PythonEnvironmentReference(BaseModel):
    """Root-issued cache metadata, never a service-selected host path or grant."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    scope: PythonProjectScope
    recipe_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    security_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    origin_execution_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid Python environment reference")
        return value


def _scope_payload(
    invocation: PythonProjectInvocation | PythonProjectProbeInvocation,
) -> dict[str, object]:
    return {
        "tenant_id": invocation.tenant_id,
        "run_id": invocation.run_id,
        "project_id": invocation.project_id,
        "session_id": invocation.session_id,
        "original_reference": invocation.original_reference.model_dump(),
    }


def _reference_matches(
    invocation: PythonProjectInvocation, reference: PythonEnvironmentReference
) -> bool:
    return (
        reference.scope.model_dump() == _scope_payload(invocation)
        and reference.recipe_sha256 == invocation.recipe_sha256
        and reference.environment_input_sha256 == invocation.environment_input_sha256
    )


class PythonProjectInspectEnvironmentRequest(_PythonRequest):
    """Read-only live cache inspection; existing invocation flags grant nothing."""

    action: Literal["inspect_python_project_environment"] = "inspect_python_project_environment"
    invocation: PythonProjectInvocation
    expected_environment: PythonEnvironmentReference

    @model_validator(mode="after")
    def expected_context(self) -> Self:
        if not _reference_matches(self.invocation, self.expected_environment):
            raise ValueError("Python inspection expected environment context mismatch")
        return self


class PythonProjectReleaseEnvironmentRequest(_PythonRequest):
    """Internal lifecycle RPC. Terminal authority is never a request argument."""

    action: Literal["release_python_project_environment"] = "release_python_project_environment"
    invocation: PythonProjectInvocation
    expected_environment: PythonEnvironmentReference

    @model_validator(mode="after")
    def expected_context(self) -> Self:
        if self.invocation.install_allowed or not _reference_matches(
            self.invocation, self.expected_environment
        ):
            raise ValueError("invalid Python release context")
        if self.invocation.execution_id == self.expected_environment.origin_execution_id:
            raise ValueError("Python release requires a separate execution identity")
        return self


class PythonProjectInputsObservation(BaseModel):
    """Measured inputs from the root probe; not an installed environment."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scope: PythonProjectScope
    declaration: dict[str, object]
    interpreter: dict[str, object]
    wheels: tuple[dict[str, object], ...]

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        declaration = PythonProjectDeclaration.from_payload(self.declaration)
        PythonInterpreterIdentity.from_payload(self.interpreter)
        selected: list[PythonWheelBinding] = []
        for item in self.wheels:
            if set(item) != {"filename", "name", "version", "sha256"}:
                raise ValueError("invalid Python wheel observation")
            selected.append(PythonWheelBinding(**cast(dict[str, str], item)))
        if bind_python_project_wheels(declaration.requirements, tuple(selected)) != tuple(selected):
            raise ValueError("invalid Python wheel observation order")
        return self


PythonProjectBrokerError = Literal[
    "python_project_request_invalid",
    "python_project_unsupported",
    "python_project_recipe_unavailable",
    "python_project_recipe_approval_required",
    "python_project_runtime_missing",
    "python_project_runtime_untrusted",
    "python_project_wheel_cache_missing",
    "python_project_wheel_missing",
    "python_project_wheel_integrity",
    "python_project_environment_unavailable",
    "python_project_environment_changed",
    "python_project_environment_quota",
    "python_project_execution_unavailable",
    "python_project_cleanup_incomplete",
]


class PythonProductDescriptor(BaseModel):
    """Root-measured bytes, never a path or authorization grant."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    execution_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    tenant_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    run_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    session_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipe_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(min_length=5, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_product_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid Python product version")
        return value

    def require_for(self, invocation: PythonProjectInvocation) -> None:
        if (self.execution_id, self.tenant_id, self.run_id, self.project_id, self.session_id,
            self.recipe_sha256) != (invocation.execution_id, str(invocation.tenant_id),
            str(invocation.run_id), invocation.project_id, invocation.session_id,
            invocation.recipe_sha256) or self.size > invocation.workspace_limit_bytes:
            raise ValueError("python_project_product_scope_invalid")


class PythonProjectBrokerResponse(_PythonRequest):
    action: Literal[
        "probe_python_project", "run_python_project", "inspect_python_project_environment",
        "release_python_project_environment",
    ]
    execution_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    ok: bool
    result: SkillResult | None = None
    observation: PythonProjectInputsObservation | None = None
    environment_reference: PythonEnvironmentReference | None = None
    released_environment: PythonEnvironmentReference | None = None
    release_outcome: Literal["released", "absent"] | None = None
    error: PythonProjectBrokerError | None = None
    product: PythonProductDescriptor | None = None
    _product_stream: BinaryIO | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if self.product is not None and (not self.ok or self.action != "run_python_project"):
            raise ValueError("unexpected Python product")
        if not self.ok:
            if self.error is None or any(
                value is not None
                for value in (
                    self.result, self.observation, self.environment_reference,
                    self.released_environment, self.release_outcome
                )
            ):
                raise ValueError("invalid Python broker failure")
        elif self.error is not None:
            raise ValueError("invalid Python broker outcome")
        elif self.action == "release_python_project_environment":
            if self.released_environment is None or self.release_outcome is None or any(
                value is not None for value in (
                    self.result, self.observation, self.environment_reference
                )
            ):
                raise ValueError("invalid Python release outcome")
        elif self.released_environment is not None or self.release_outcome is not None:
            raise ValueError("unexpected Python release outcome")
        elif self.action == "probe_python_project":
            if (
                self.observation is None
                or self.result is not None
                or self.environment_reference is not None
            ):
                raise ValueError("invalid Python probe outcome")
        elif self.action == "inspect_python_project_environment":
            if (
                self.environment_reference is None
                or self.result is not None
                or self.observation is not None
            ):
                raise ValueError("invalid Python environment inspection outcome")
        elif self.result is None or self.observation is not None:
            raise ValueError("invalid Python execution outcome")
        return self

    def require_for(
        self,
        request: PythonProjectBrokerRequest
        | PythonProjectProbeRequest
        | PythonProjectInspectEnvironmentRequest
        | PythonProjectReleaseEnvironmentRequest,
    ) -> None:
        """Bind the authenticated root reply to the actual invocation, not report fields."""
        if type(request) not in {
            PythonProjectBrokerRequest,
            PythonProjectProbeRequest,
            PythonProjectInspectEnvironmentRequest,
            PythonProjectReleaseEnvironmentRequest,
        }:
            raise ValueError("invalid Python broker request type")
        invocation = request.invocation
        if self.product is not None:
            if not isinstance(request, PythonProjectBrokerRequest) or request.product_transfer_version != 1:
                raise ValueError("unsolicited Python product")
            self.product.require_for(request.invocation)
        if (self.action, self.execution_id) != (request.action, invocation.execution_id):
            raise ValueError("Python broker response context mismatch")
        if (
            isinstance(request, PythonProjectReleaseEnvironmentRequest) and self.ok
            and self.released_environment != request.expected_environment
        ):
            raise ValueError("Python broker release reference mismatch")
        scope = (
            self.environment_reference.scope
            if self.environment_reference is not None
            else self.observation.scope
            if self.observation is not None
            else None
        )
        if scope is not None and scope.model_dump() != _scope_payload(invocation):
            raise ValueError("Python broker response scope mismatch")
        if self.environment_reference is not None and (
            type(invocation) is not PythonProjectInvocation
            or self.environment_reference.recipe_sha256 != invocation.recipe_sha256
            or self.environment_reference.environment_input_sha256
            != invocation.environment_input_sha256
        ):
            raise ValueError("Python broker response recipe mismatch")
        if (
            isinstance(request, PythonProjectInspectEnvironmentRequest)
            and self.ok
            and self.environment_reference != request.expected_environment
        ):
            raise ValueError("Python broker inspection reference mismatch")


def base_python_invocation(
    invocation: PythonProjectInvocation | PythonProjectProbeInvocation,
) -> ProjectInvocation:
    if type(invocation) not in {PythonProjectInvocation, PythonProjectProbeInvocation}:
        raise ValueError("invalid Python project invocation")
    validated = type(invocation).model_validate(invocation.model_dump(), strict=True)
    return ProjectInvocation.model_validate(
        {name: getattr(validated, name) for name in ProjectInvocation.model_fields}, strict=True
    )
