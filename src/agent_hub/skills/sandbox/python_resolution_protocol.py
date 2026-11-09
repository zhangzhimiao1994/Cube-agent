"""Strict v11 resolution transport: data provenance is never an execution grant."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_hub.capabilities.python_dependency_closure import PythonWheelMetadata
from agent_hub.skills.sandbox.project import OriginalProjectReference, ProjectInvocation
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataPlanningRequest,
    PythonMetadataScope,
    _base_invocation,
)
from agent_hub.skills.sandbox.python_project_profile import Digest
from agent_hub.skills.sandbox.python_registry_protocol import (
    DependencyNeed,
    RegistryCandidate,
    RegistryProvenance,
    ResolutionDescriptor,
    ResolutionLimits,
    ResolutionScope,
    ResolvedWheel,
    digest,
    need,
    require_sha,
    target_environment,
)

MAX_CONTROL = 2_000_000


def encoded(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        need(key not in result)
        result[key] = value
    return result


def decode(raw: bytes) -> object:
    need(type(raw) is bytes and 0 < len(raw) <= MAX_CONTROL)
    return json.loads(raw, object_pairs_hook=_pairs)


def _object(value: object, keys: set[str]) -> dict[str, object]:
    need(type(value) is dict and set(cast(dict[str, object], value)) == keys)
    return cast(dict[str, object], value)


def decode_descriptor(value: object) -> ResolutionDescriptor:
    item = _object(value, {"scope", "needs", "environment", "wheels", "edges"})
    scope = _object(
        item["scope"],
        {
            "tenant_id",
            "run_id",
            "user_id",
            "actor",
            "project_id",
            "session_id",
            "request_sha256",
            "source_sha256",
            "catalog_sha256",
        },
    )
    need(all(type(v) is str for v in scope.values()))
    source = dict(scope)
    for key in ("tenant_id", "run_id", "user_id"):
        source[key] = UUID(cast(str, source[key]))
    need(
        type(item["needs"]) is list and type(item["wheels"]) is list and type(item["edges"]) is list
    )
    needs = tuple(
        DependencyNeed(**cast(dict[str, str], _object(n, {"requirement", "evidence_sha256"})))
        for n in cast(list[object], item["needs"])
    )
    wheels = []
    for wheel_value in cast(list[object], item["wheels"]):
        row = _object(wheel_value, {"candidate", "metadata"})
        candidate = _object(row["candidate"], {"download", "provenance", "index_provenance"})
        download = _object(
            candidate["download"], {"filename", "name", "version", "sha256", "size_bytes", "url"}
        )
        wheel = WheelDownload(**download)  # type: ignore[arg-type]
        provenance = RegistryProvenance(
            **cast(dict[str, str], _object(candidate["provenance"], {"url", "sha256"}))
        )
        index = RegistryProvenance(
            **cast(dict[str, str], _object(candidate["index_provenance"], {"url", "sha256"}))
        )
        wheels.append(
            ResolvedWheel(
                RegistryCandidate(wheel, provenance, index),
                PythonWheelMetadata.from_payload(row["metadata"]),
            )
        )
    env = item["environment"]
    need(type(env) is dict)
    target_environment(cast(dict[str, str], env))
    edges = cast(list[list[str]], item["edges"])
    need(all(type(e) is list and len(e) == 2 and all(type(v) is str for v in e) for e in edges))
    result = ResolutionDescriptor(
        ResolutionScope(
            cast(UUID, source["tenant_id"]),
            cast(UUID, source["run_id"]),
            cast(UUID, source["user_id"]),
            cast(str, source["actor"]),
            cast(str, source["project_id"]),
            cast(str, source["session_id"]),
            cast(str, source["request_sha256"]),
            cast(str, source["source_sha256"]),
            cast(str, source["catalog_sha256"]),
        ),
        needs,
        tuple(sorted(cast(dict[str, str], env).items())),
        tuple(wheels),
        tuple((e[0], e[1]) for e in edges),
    )
    need(result.to_payload() == item)
    return result


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class ProtectedOriginalResolutionSource(_Strict):
    """Identity of an already captured original, with no generated controls."""

    original_reference: OriginalProjectReference
    protected_sha256: Digest
    manifest_sha256: Digest

    @property
    def source_sha256(self) -> str:
        return self.original_reference.input_source_sha256


class ProtectedOriginalResolutionPlanning(_Strict):
    kind: Literal["protected_original_python"] = "protected_original_python"
    invocation: ProjectInvocation
    scope: PythonMetadataScope
    draft: ProtectedOriginalResolutionSource
    expected_root_config_sha256: Digest
    expected_catalog_sha256: Digest

    @model_validator(mode="before")
    @classmethod
    def validated_base_invocation(cls, value: object) -> object:
        if isinstance(value, dict) and "invocation" in value:
            return {**value, "invocation": _base_invocation(value["invocation"])}
        return value

    @model_validator(mode="after")
    def readonly(self) -> Self:
        need(self.invocation.sandbox_profile == "restricted" and self.invocation.install_allowed is False,
             "resolution_request_invalid")
        self.scope.require_invocation(self.invocation)
        return self


type ResolutionPlanning = PythonMetadataPlanningRequest | ProtectedOriginalResolutionPlanning


class ResolutionClaim(_Strict):
    """Trusted service-UID witness, derived from original worker plus a fresh DB read.

    Root independently checks topology/source/config/once. This is not a signed
    client grant; arbitrary API callers must never construct the privileged IPC.
    """

    planning: ResolutionPlanning
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: UUID
    lease_expires_at: datetime
    original_deadline: datetime
    monotonic_deadline: float

    @model_validator(mode="after")
    def valid(self) -> Self:
        need(
            self.worker_id.strip() == self.worker_id
            and all(32 <= ord(c) < 127 for c in self.worker_id)
        )
        need(math.isfinite(self.monotonic_deadline) and self.monotonic_deadline > 0)
        need(self.lease_expires_at.tzinfo is not None and self.original_deadline.tzinfo is not None)
        return self

    @property
    def task_deadline(self) -> float:
        """Immutable task budget; this alone never establishes live lease authority."""
        now = datetime.now(UTC)
        remaining = (self.original_deadline - now).total_seconds()
        value = min(self.monotonic_deadline, time.monotonic() + remaining)
        need(value > time.monotonic(), "resolution_deadline")
        return value

    @property
    def deadline(self) -> float:
        now = datetime.now(UTC)
        value = min(self.task_deadline,
            time.monotonic() + (self.lease_expires_at - now).total_seconds())
        need(value > time.monotonic(), "resolution_deadline")
        return value

    def anchor(self) -> dict[str, object]:
        result: dict[str, object] = {
            "scope": self.planning.scope.model_dump(mode="json"),
            "worker_id": self.worker_id,
            "lease_token": str(self.lease_token),
            "original_deadline": self.original_deadline.isoformat(),
            "root": self.planning.expected_root_config_sha256,
            "catalog": self.planning.expected_catalog_sha256,
        }
        if isinstance(self.planning, ProtectedOriginalResolutionPlanning):
            result["protected_original"] = self.planning.draft.model_dump(mode="json")
        return result

    def require_continuity(self, original: ResolutionClaim) -> None:
        current = ResolutionClaim.model_validate(self, strict=True)
        previous = ResolutionClaim.model_validate(original, strict=True)
        need(current.anchor() == previous.anchor(), "resolution_scope_changed")
        need(current.monotonic_deadline <= previous.monotonic_deadline, "resolution_deadline")
        _ = current.deadline

    def data_identity(self) -> dict[str, object]:
        result: dict[str, object] = {"scope": self.planning.scope.model_dump(mode="json"),
            "root": self.planning.expected_root_config_sha256,
            "catalog": self.planning.expected_catalog_sha256}
        if isinstance(self.planning, ProtectedOriginalResolutionPlanning):
            result["protected_original"] = self.planning.draft.model_dump(mode="json")
        return result

    def require_data_origin(self, original: ResolutionClaim) -> None:
        """Immutable bytes may cross a fresh bound worker; its grant never does."""
        current = ResolutionClaim.model_validate(self, strict=True)
        previous = ResolutionClaim.model_validate(original, strict=True)
        need(current.data_identity() == previous.data_identity(), "resolution_scope_changed")
        need(current.original_deadline <= previous.original_deadline, "resolution_deadline")
        _ = current.deadline

    def approval_payload(self) -> dict[str, object]:
        value = self.effect_payload()
        value["anchor"] = {**self.data_identity(), "original_deadline": self.original_deadline.isoformat()}
        return value

    def effect_payload(self) -> dict[str, object]:
        invocation = self.planning.invocation.model_dump(mode="json")
        invocation.pop("execution_id")
        invocation.pop("timeout_seconds")
        return {
            "anchor": self.anchor(),
            "draft": self.planning.draft.model_dump(mode="json"),
            "invocation": invocation,
        }

    def resolution_scope(self) -> ResolutionScope:
        s = self.planning.scope
        return ResolutionScope(
            s.tenant_id,
            s.run_id,
            s.user_id,
            s.actor,
            s.project_id,
            s.session_id,
            s.request_sha256,
            self.planning.draft.source_sha256,
            self.planning.expected_catalog_sha256,
        )


class ResolutionPlan(_Strict):
    schema_version: Literal[1] = 1
    claim: ResolutionClaim
    resolution_key: str = Field(pattern="^[a-f0-9]{64}$")
    descriptor_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    overlay_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    artifacts: tuple[tuple[str, str, int], ...]

    @model_validator(mode="after")
    def sizes(self) -> Self:
        need(0 < len(self.artifacts) <= 128 and sum(a[2] for a in self.artifacts) <= 64 * 1024**2)
        for filename, sha, size in self.artifacts:
            need(
                type(filename) is str
                and filename.endswith("-py3-none-any.whl")
                and "/" not in filename
                and "\\" not in filename
                and 0 < size <= 64 * 1024**2
            )
            require_sha(sha)
        need(tuple(sorted(set(self.artifacts))) == self.artifacts)
        return self

    @property
    def sha256(self) -> str:
        return digest({**self.model_dump(mode="json"), "claim": self.claim.approval_payload()})


class ResolutionStatus(_Strict):
    state: Literal["absent", "unknown", "resolved", "frozen", "published", "lease_renewed"]
    key: str = Field(pattern="^[a-f0-9]{64}$")
    descriptor: dict[str, object] | None = None
    plan: ResolutionPlan | None = None


class ResolutionRequest(_Strict):
    version: Literal[11] = 11
    action: Literal[
        "resolve_python_inputs",
        "freeze_python_resolution",
        "publish_python_resolution",
        "inspect_python_resolution",
        "renew_python_resolution",
    ]
    claim: ResolutionClaim
    requirements: tuple[tuple[str, str], ...]
    environment: dict[str, str]
    limits: dict[str, int] = Field(default_factory=lambda: asdict(ResolutionLimits()))
    expected_plan_sha256: str | None = None

    @model_validator(mode="after")
    def bound(self) -> Self:
        need(0 < len(self.requirements) <= 128)
        for text, sha in self.requirements:
            DependencyNeed(text, sha)
        target_environment(self.environment)
        ResolutionLimits(**self.limits)
        if self.action == "publish_python_resolution":
            need(self.expected_plan_sha256 is not None)
        if self.expected_plan_sha256 is not None:
            require_sha(self.expected_plan_sha256)
        need(len(encoded(self.model_dump(mode="json"))) <= 64 * 1024)
        return self

    @property
    def invocation(self) -> ProjectInvocation:
        return self.claim.planning.invocation

    @property
    def needs(self) -> tuple[DependencyNeed, ...]:
        return tuple(DependencyNeed(text, sha) for text, sha in self.requirements)

    @property
    def key(self) -> str:
        return resolution_key(self.claim, self.needs, self.environment)


def resolution_key(
    claim: ResolutionClaim, needs: tuple[DependencyNeed, ...], environment: dict[str, str]
) -> str:
    target_environment(environment)
    # Source/transport IDs/lease heartbeat do not permit a second networking attempt.
    s = claim.planning.scope
    return digest(
        {
            "scope": s.model_dump(mode="json"),
            "needs": [asdict(n) for n in needs],
            "environment": environment,
        }
    )
