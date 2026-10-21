"""Trusted SDK data and conservative supply plans, never installation authority.

The initial catalog describes Linux/amd64 Go archives only. No model URL, shell
command, download, filesystem write or project execution occurs here. Callers
must independently authorize the exact plan and hold native reservations while
remeasuring space and performing supply; a serialized plan is not a reservation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.capabilities.go_project_recipe import GoProjectDeclaration

_CONTROL_LIMIT = 512 * 1024
_MIN_RESERVE = 3 * 1024**3
_MAX_INTEGER = 2**63 - 1
_VERSION = re.compile(r"1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_ROLES = ("execution", "runtime", "stage", "state")
_CONTROL_ALLOWANCE = 1024**2
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PositiveBytes = Annotated[int, Field(gt=0, le=_MAX_INTEGER)]
ScopedName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]
FilesystemRole = Literal["runtime", "stage", "execution", "state"]


class ToolchainCatalogError(ValueError):
    """Finite errors omit supplied controls, URLs, paths and credentials."""

    def __init__(self, code: str = "toolchain_catalog_invalid") -> None:
        self.code = code
        super().__init__(code)


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ToolchainCatalogError()
        result[name] = value
    return result


def _constant(value: str) -> object:
    raise ToolchainCatalogError()


def _path(value: str) -> str:
    path = PurePosixPath(value)
    if (not path.is_absolute() or len(path.parts) < 2 or len(path.parts) > 64
            or path.as_posix() != value or ".." in path.parts
            or len(value.encode("utf-8")) > 4096
            or any(ord(c) < 33 or ord(c) > 126 or c in "\\:" for c in value)):
        raise ValueError("invalid root")
    return value


def _version(value: str, *, patch_optional: bool = False) -> tuple[int, int, int]:
    if patch_optional and re.fullmatch(r"1\.(0|[1-9][0-9]*)", value):
        value += ".0"
    if len(value) > 32 or _VERSION.fullmatch(value) is None:
        raise ValueError("invalid version")
    parts = value.split(".")
    return int(parts[0]), int(parts[1]), int(parts[2])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid schema")
        return value

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            if type(raw) is not bytes or not 0 < len(raw) <= _CONTROL_LIMIT:
                raise ValueError
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
            if type(value) is not dict:
                raise ValueError
            return cls.model_validate_json(raw, strict=True)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ToolchainCatalogError() from None

    @classmethod
    def from_payload(cls, value: object) -> Self:
        try:
            if type(value) is not dict:
                raise ValueError
            return cls.from_json(_encoded(value))
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ToolchainCatalogError() from None

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def to_json(self) -> bytes:
        return _encoded(self.to_payload())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json()).hexdigest()


class ToolchainArtifact(_Strict):
    stack: Literal["go"]
    version: str = Field(max_length=32)
    platform: Literal["linux_amd64"]
    url: str = Field(max_length=512)
    sha256: Digest
    archive_bytes: PositiveBytes
    unpacked_bytes_limit: PositiveBytes
    archive_entries_limit: int = Field(gt=0, le=1_000_000)
    directory_entries_limit: int = Field(gt=0, le=1_000_000)
    tar_bytes_limit: PositiveBytes

    @model_validator(mode="after")
    def fixed_distribution_only(self) -> Self:
        _version(self.version)
        expected = f"https://dl.google.com/go/go{self.version}.linux-amd64.tar.gz"
        if (self.url != expected or self.tar_bytes_limit < 10240
                or self.tar_bytes_limit % 512 or self.unpacked_bytes_limit > self.tar_bytes_limit):
            raise ValueError("invalid artifact")
        return self

    @property
    def artifact_sha256(self) -> str:
        """Bind the entire catalog entry, separately from the archive bytes."""
        return hashlib.sha256(self.to_json()).hexdigest()


class ToolchainCatalog(_Strict):
    schema_version: Literal[1] = 1
    runtime_root: str
    stage_root: str
    state_root: str
    min_free_bytes: int = Field(ge=_MIN_RESERVE, le=_MAX_INTEGER)
    artifacts: tuple[ToolchainArtifact, ...] = Field(min_length=1, max_length=1000)

    @property
    def sha256(self) -> str:
        return self.digest

    @field_validator("runtime_root", "stage_root", "state_root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        return _path(value)

    @model_validator(mode="after")
    def disjoint_roots_and_canonical_catalog(self) -> Self:
        roots = tuple(PurePosixPath(p) for p in (self.runtime_root, self.stage_root, self.state_root))
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(roots) for b in roots[index + 1:]):
            raise ValueError("overlapping roots")
        versions = tuple(_version(entry.version) for entry in self.artifacts)
        if versions != tuple(sorted(set(versions))):
            raise ValueError("noncanonical catalog")
        return self

    def select(self, declaration: GoProjectDeclaration) -> ToolchainArtifact:
        try:
            if type(declaration) is not GoProjectDeclaration:
                raise ValueError
            replace(declaration)
            minimum = _version(declaration.go_version, patch_optional=True)
            exact = declaration.toolchain
            selected = next((entry for entry in self.artifacts
                if _version(entry.version) >= minimum
                and (exact in {None, "default"} or "go" + entry.version == exact)), None)
            if selected is not None:
                return selected
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass
        raise ToolchainCatalogError("toolchain_catalog_no_match")


class ToolchainPlanScope(_Strict):
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    project_id: ScopedName
    session_id: ScopedName
    actor_id: ScopedName
    actor_role: ScopedName
    source_sha256: Digest
    contract_sha256: Digest


class FilesystemSpace(_Strict):
    role: FilesystemRole
    device: int = Field(ge=0, le=_MAX_INTEGER)
    available_bytes: int = Field(ge=0, le=_MAX_INTEGER)
    allocation_unit: int = Field(gt=0, le=1024**2)


def _rounded(size: int, block: int) -> int:
    return ((size + block - 1) // block) * block


def _required(artifact: ToolchainArtifact, roles: tuple[FilesystemRole, ...],
              block: int, execution_bytes: int, source_limit_bytes: int = 0,
              source_entry_limit: int = 0) -> int:
    # Round every possible entry, not just the sum of regular file bytes. Keep
    # both stage and published copies for EXDEV, plus metadata and execution.
    tree = (_rounded(artifact.unpacked_bytes_limit, block)
            + (artifact.archive_entries_limit + artifact.directory_entries_limit) * block)
    source = source_copy_peak(source_limit_bytes, source_entry_limit, block)
    demands = {"stage": _rounded(artifact.archive_bytes, block) + tree + _CONTROL_ALLOWANCE + source,
               "runtime": _rounded(artifact.archive_bytes, block) + tree + _CONTROL_ALLOWANCE,
               "execution": _rounded(execution_bytes, block) + _CONTROL_ALLOWANCE,
               "state": 2 * _CONTROL_ALLOWANCE}
    total = sum(demands[role] for role in roles)
    if total > _MAX_INTEGER:
        raise ValueError("peak overflow")
    return total


def source_copy_peak(source_limit_bytes: int, source_entry_limit: int, block: int) -> int:
    # The existing baseline copier bounds all entries at 10 * file_limit.
    # Include its 32MiB control envelope and round every possible entry.
    return _rounded(source_limit_bytes, block) + source_entry_limit * block + 32 * _CONTROL_ALLOWANCE


class ToolchainFilesystemPeak(_Strict):
    device: int = Field(ge=0, le=_MAX_INTEGER)
    roles: tuple[FilesystemRole, ...] = Field(min_length=1, max_length=4)
    allocation_unit: int = Field(gt=0, le=1024**2)
    available_bytes: int = Field(ge=0, le=_MAX_INTEGER)
    required_bytes: PositiveBytes
    reserve_bytes: int = Field(ge=_MIN_RESERVE, le=_MAX_INTEGER)


class ToolchainSupplyPlan(_Strict):
    schema_version: Literal[1] = 1
    kind: Literal["go_toolchain_supply_v1"] = "go_toolchain_supply_v1"
    scope: ToolchainPlanScope
    catalog_sha256: Digest
    declaration_sha256: Digest
    artifact: ToolchainArtifact
    runtime_root: str
    stage_root: str
    state_root: str
    execution_bytes: PositiveBytes
    source_limit_bytes: int = Field(default=0, ge=0, le=500 * 1024**2)
    source_entry_limit: int = Field(default=0, ge=0, le=1_000_000)
    filesystems: tuple[ToolchainFilesystemPeak, ...] = Field(min_length=1, max_length=4)

    @property
    def sha256(self) -> str:
        # Observed free space is not work approved by the user. Every consumer
        # must remeasure it under native admission rather than treating a saved
        # observation as either fresh capacity or a reason to request approval.
        approved = self.model_dump(mode="json", exclude={
            "filesystems": {"__all__": {"available_bytes"}},
        })
        return hashlib.sha256(_encoded({"purpose": "toolchain_supply_approval_v1",
                                        "plan": approved})).hexdigest()

    @field_validator("runtime_root", "stage_root", "state_root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        return _path(value)

    @model_validator(mode="after")
    def full_peak_and_partition(self) -> Self:
        devices = tuple(p.device for p in self.filesystems)
        roles = tuple(sorted(role for p in self.filesystems for role in p.roles))
        if devices != tuple(sorted(set(devices))) or roles != _ROLES:
            raise ValueError("invalid filesystem partition")
        roots = tuple(PurePosixPath(p) for p in (self.runtime_root, self.stage_root, self.state_root))
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(roots) for b in roots[index + 1:]):
            raise ValueError("overlapping roots")
        for peak in self.filesystems:
            if (peak.roles != tuple(sorted(set(peak.roles)))
                    or peak.required_bytes != _required(self.artifact, peak.roles,
                                                       peak.allocation_unit, self.execution_bytes,
                                                       self.source_limit_bytes, self.source_entry_limit)
                    or peak.available_bytes - peak.required_bytes < peak.reserve_bytes):
                raise ValueError("invalid peak")
        return self


def plan_toolchain_supply(
    catalog: ToolchainCatalog, declaration: GoProjectDeclaration, scope: ToolchainPlanScope,
    spaces: tuple[FilesystemSpace, ...], *, execution_bytes: int,
    source_limit_bytes: int = 0, source_entry_limit: int = 0,
) -> ToolchainSupplyPlan:
    """Plan all copies and future execution, grouping destinations by real device.

    Native callers must observe destinations beneath trusted open directory FDs
    and reserve/recheck under their global lock. This pure function performs
    neither step and cannot authorize supply or claim a live environment ready.
    """
    try:
        if (type(catalog) is not ToolchainCatalog or type(scope) is not ToolchainPlanScope
                or type(spaces) is not tuple or len(spaces) != 4
                or type(execution_bytes) is not int or not 0 < execution_bytes <= _MAX_INTEGER):
            raise ValueError
        if (type(source_limit_bytes) is not int or not 0 <= source_limit_bytes <= 500 * 1024**2
                or type(source_entry_limit) is not int or not 0 <= source_entry_limit <= 1_000_000):
            raise ValueError
        catalog = ToolchainCatalog.from_json(catalog.to_json())
        scope = ToolchainPlanScope.from_json(scope.to_json())
        observed = tuple(FilesystemSpace.from_json(s.to_json()) for s in spaces
                         if type(s) is FilesystemSpace)
        if len(observed) != 4 or tuple(sorted(s.role for s in observed)) != _ROLES:
            raise ValueError
        artifact = catalog.select(declaration)
        peaks: list[ToolchainFilesystemPeak] = []
        for device in sorted({s.device for s in observed}):
            same = tuple(s for s in observed if s.device == device)
            if len({(s.available_bytes, s.allocation_unit) for s in same}) != 1:
                raise ValueError
            roles = cast(tuple[FilesystemRole, ...], tuple(sorted(s.role for s in same)))
            required = _required(artifact, roles, same[0].allocation_unit, execution_bytes,
                                 source_limit_bytes, source_entry_limit)
            if same[0].available_bytes - required < catalog.min_free_bytes:
                raise ToolchainCatalogError("toolchain_capacity")
            peaks.append(ToolchainFilesystemPeak(device=device, roles=roles,
                allocation_unit=same[0].allocation_unit, available_bytes=same[0].available_bytes,
                required_bytes=required, reserve_bytes=catalog.min_free_bytes))
        return ToolchainSupplyPlan(scope=scope, catalog_sha256=catalog.sha256,
            declaration_sha256=declaration.sha256, artifact=artifact, runtime_root=catalog.runtime_root,
            stage_root=catalog.stage_root, state_root=catalog.state_root,
            execution_bytes=execution_bytes, source_limit_bytes=source_limit_bytes,
            source_entry_limit=source_entry_limit, filesystems=tuple(peaks))
    except ToolchainCatalogError:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ToolchainCatalogError() from None
