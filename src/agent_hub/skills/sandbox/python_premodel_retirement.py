"""Typed, non-replaying retirement of a stopped pre-model owner's READY cache.

These DTOs carry measured private controls, not DB lifecycle authority or proof
that a process is quiet. Only the trusted Root callback can supply that proof.
Old retained stages remain untouched and the retired owner stays disabled.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from typing import TYPE_CHECKING, Final, Literal, Self, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.project_contract_broker import _encoded
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonFailedMaintenanceInvocation,
    PythonProjectBrokerError,
    PythonProjectScope,
)

if TYPE_CHECKING:
    from agent_hub.runtime.contracts import Artifact

_ACTION: Final = "maintain_stopped_premodel_python_project_environment"
_KIND = "python_stopped_premodel_owner_retired"
_SHA = r"^[a-f0-9]{64}$"
_OWNER = ("tenant_id", "run_id", "project_id", "session_id")
_MAX_SNAPSHOT = 1024**2


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate retirement control field")
        value[key] = item
    return value


def _control(raw: str, *, canonical: bool = False) -> dict[str, object]:
    body = raw.encode("ascii")
    if not 0 < len(body) <= 16384:
        raise ValueError("retirement control bound")
    value = json.loads(body, object_pairs_hook=_object)
    if type(value) is not dict or (canonical and _encoded(value) != body):
        raise ValueError("invalid retirement control object")
    return value


def _stamp(value: tuple[int, ...], *, mode: int, size: int | None = None) -> None:
    if (
        len(value) != 9
        or any(type(n) is not int or not 0 <= n < 2**63 for n in value)
        or value[0] == 0
        or value[1] == 0
        or value[3] != 0
        or value[2] != mode
        or value[5] < 1
        or (stat.S_ISREG(mode) and value[5] != 1)
        or (size is not None and value[6] != size)
    ):
        raise ValueError("invalid retirement native identity")


class PythonStoppedPremodelStage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    root: Literal["runtime", "stage"]
    name: str = Field(min_length=1, max_length=145)
    record: str = Field(min_length=1, max_length=16384, repr=False)
    record_sha256: str = Field(pattern=_SHA)
    identities: tuple[tuple[int, ...], tuple[int, ...]]

    @model_validator(mode="after")
    def valid_stage(self) -> Self:
        value = _control(self.record)
        if set(value) != {*_OWNER, "execution_id", "source_sha256", "state", "unit"}:
            raise ValueError("invalid retained stage fields")
        item = ProjectInvocation.model_validate_json(
            _encoded(
                {key: value[key] for key in (*_OWNER, "execution_id")}
                | {"sandbox_profile": "restricted", "timeout_seconds": 1}
            ),
            strict=True,
        )
        if (
            not re.fullmatch(re.escape(item.execution_id) + r"-[a-z0-9_]{1,16}", self.name)
            or type(value["state"]) is not str
            or value["state"] not in {"STAGED", "RUNNING", "UNKNOWN"}
            or value["unit"] != f"agent-hub-project-{item.execution_id}.service"
            or (
                value["source_sha256"] is not None
                and (
                    type(value["source_sha256"]) is not str
                    or not re.fullmatch(_SHA, value["source_sha256"])
                )
            )
            or _digest(self.record.encode("ascii")) != self.record_sha256
        ):
            raise ValueError("invalid retained stage binding")
        _stamp(self.identities[0], mode=stat.S_IFDIR | 0o700)
        mode = self.identities[1][2] if len(self.identities[1]) == 9 else 0
        if mode not in {stat.S_IFREG | 0o600, stat.S_IFREG | 0o644}:
            raise ValueError("invalid retained control mode")
        _stamp(self.identities[1], mode=mode, size=len(self.record.encode("ascii")))
        return self

    @property
    def execution_id(self) -> str:
        return cast(str, _control(self.record)["execution_id"])

    @property
    def scope(self) -> dict[str, str]:
        value = _control(self.record)
        return {key: cast(str, value[key]) for key in _OWNER}


class PythonStoppedPremodelSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    cache_key: str = Field(pattern=_SHA)
    scope: PythonProjectScope
    reservation: str = Field(min_length=1, max_length=16384, repr=False)
    reference: str = Field(min_length=1, max_length=16384, repr=False)
    reservation_sha256: str = Field(pattern=_SHA)
    reference_sha256: str = Field(pattern=_SHA)
    environment_tree_sha256: str = Field(pattern=_SHA)
    identities: tuple[tuple[int, ...], ...] = Field(min_length=4, max_length=4)
    stages: tuple[PythonStoppedPremodelStage, ...] = Field(min_length=1, max_length=64)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("invalid stopped pre-model schema")
        return value

    @model_validator(mode="after")
    def valid_snapshot(self) -> Self:
        # Lazy imports avoid making the original protocol depend on this module.
        from agent_hub.skills.sandbox.python_project_cache import (
            _bind_reference,
            _validate_reservation,
        )

        reservation = _control(self.reservation, canonical=True)
        _validate_reservation(reservation, self.cache_key, 2**63 - 1)
        reference = PythonEnvironmentReference.model_validate_json(self.reference, strict=True)
        _control(self.reference, canonical=True)
        _bind_reference(reference, reservation)
        if (
            reference.scope != self.scope
            or _digest(self.reservation.encode("ascii")) != self.reservation_sha256
            or _digest(self.reference.encode("ascii")) != self.reference_sha256
        ):
            raise ValueError("invalid stopped READY control binding")
        modes = (
            stat.S_IFDIR | 0o700,
            stat.S_IFREG | 0o600,
            stat.S_IFREG | 0o600,
            stat.S_IFDIR | 0o555,
        )
        for identity, mode in zip(self.identities, modes, strict=True):
            _stamp(identity, mode=mode)
        if self.identities[1][6] != len(self.reservation.encode("ascii")) or self.identities[2][
            6
        ] != len(self.reference.encode("ascii")):
            raise ValueError("retirement control identity size mismatch")
        names = [(item.root, item.name) for item in self.stages]
        owner = {key: str(getattr(self.scope, key)) for key in _OWNER}
        if (
            names != sorted(names)
            or len(set(names)) != len(names)
            or any(item.scope != owner for item in self.stages)
            or self.origin_execution_id not in {item.execution_id for item in self.stages}
            or len(_encoded(self.model_dump(mode="json"))) > _MAX_SNAPSHOT
        ):
            raise ValueError("invalid stopped READY scope inventory")
        return self

    @property
    def origin_execution_id(self) -> str:
        return cast(str, _control(self.reservation)["origin_execution_id"])

    def archive(self) -> Artifact:
        from agent_hub.runtime.contracts import Artifact

        payload = self.model_dump(mode="json")
        identity = _digest(_encoded(payload))
        return Artifact(
            id=uuid5(NAMESPACE_URL, "agent-hub:stopped-premodel-ready:" + identity),
            type="python_stopped_premodel_cache_archive",
            producer="python_terminal_maintenance",
            content={"schema_version": 1, "snapshot": payload},
        )


class PythonStoppedPremodelMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[4] = 4
    action: Literal["maintain_stopped_premodel_python_project_environment"] = _ACTION
    invocation: PythonFailedMaintenanceInvocation
    phase: Literal["inspect", "commit"]
    snapshot: PythonStoppedPremodelSnapshot | None = None
    archive_id: UUID | None = None
    archive_sha256: str | None = Field(default=None, pattern=_SHA)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 4:
            raise ValueError("invalid stopped maintenance request version")
        return value

    @model_validator(mode="after")
    def valid_phase(self) -> Self:
        if self.phase == "inspect":
            if any(
                item is not None for item in (self.snapshot, self.archive_id, self.archive_sha256)
            ):
                raise ValueError("unexpected stopped inspect material")
        else:
            if self.snapshot is None:
                raise ValueError("missing stopped commit snapshot")
            archive = self.snapshot.archive()
            if (
                (archive.id, archive.content_sha256) != (self.archive_id, self.archive_sha256)
                or any(
                    getattr(self.invocation, key) != getattr(self.snapshot.scope, key)
                    for key in _OWNER
                )
                or self.invocation.execution_id
                in {
                    self.snapshot.origin_execution_id,
                    *(item.execution_id for item in self.snapshot.stages),
                }
            ):
                raise ValueError("invalid stopped commit binding")
        return self


class PythonStoppedPremodelMaintenanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    action: Literal["maintain_stopped_premodel_python_project_environment"] = _ACTION
    execution_id: str = Field(pattern=r"^python_release_expired_[a-f0-9]{32}$")
    ok: bool
    error: PythonProjectBrokerError | None = None
    retirement_phase: Literal["inspect", "commit"] | None = None
    retirement_snapshot: PythonStoppedPremodelSnapshot | None = None
    retirement_removed: bool | None = None

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if not self.ok:
            if self.error is None or any(
                value is not None
                for value in (
                    self.retirement_phase,
                    self.retirement_snapshot,
                    self.retirement_removed,
                )
            ):
                raise ValueError("invalid stopped maintenance failure")
        elif (
            self.error is not None
            or self.retirement_phase is None
            or (self.retirement_phase == "inspect" and self.retirement_removed is not None)
            or (
                self.retirement_phase == "commit"
                and (
                    type(self.retirement_removed) is not bool
                    or self.retirement_snapshot is not None
                )
            )
        ):
            raise ValueError("invalid stopped maintenance outcome")
        return self

    def require_for(self, request: PythonStoppedPremodelMaintenanceRequest) -> None:
        if type(request) is not PythonStoppedPremodelMaintenanceRequest:
            raise ValueError("invalid stopped maintenance request type")
        request = type(request).model_validate_json(request.model_dump_json(), strict=True)
        if (self.action, self.execution_id) != (request.action, request.invocation.execution_id):
            raise ValueError("stopped maintenance response context mismatch")
        if self.ok:
            if self.retirement_phase != request.phase:
                raise ValueError("stopped maintenance response phase mismatch")
            if self.retirement_snapshot is not None and (
                any(
                    getattr(request.invocation, key) != getattr(self.retirement_snapshot.scope, key)
                    for key in _OWNER
                )
                or request.invocation.execution_id
                in {
                    self.retirement_snapshot.origin_execution_id,
                    *(item.execution_id for item in self.retirement_snapshot.stages),
                }
            ):
                raise ValueError("stopped maintenance response scope mismatch")


def stopped_premodel_tombstone(snapshot: PythonStoppedPremodelSnapshot) -> dict[str, object]:
    archive = snapshot.archive()
    return {
        "schema_version": 1,
        "kind": _KIND,
        "owner": {key: str(getattr(snapshot.scope, key)) for key in _OWNER},
        "cache_key": snapshot.cache_key,
        "origin_execution_id": snapshot.origin_execution_id,
        "reservation_sha256": snapshot.reservation_sha256,
        "reference_sha256": snapshot.reference_sha256,
        "environment_tree_sha256": snapshot.environment_tree_sha256,
        "snapshot_sha256": _digest(_encoded(snapshot.model_dump(mode="json"))),
        "archive_id": str(archive.id),
        "archive_sha256": archive.content_sha256,
    }


def validate_stopped_premodel_tombstone(name: str, value: object) -> None:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "kind",
        "owner",
        "cache_key",
        "origin_execution_id",
        "reservation_sha256",
        "reference_sha256",
        "environment_tree_sha256",
        "snapshot_sha256",
        "archive_id",
        "archive_sha256",
    }:
        raise ValueError("invalid stopped owner tombstone fields")
    owner = value["owner"]
    if (
        type(owner) is not dict
        or set(owner) != set(_OWNER)
        or any(type(owner[key]) is not str for key in _OWNER)
        or any(str(UUID(owner[key])) != owner[key] for key in ("tenant_id", "run_id"))
    ):
        raise ValueError("invalid stopped owner tombstone scope")
    ProjectInvocation.model_validate_json(
        _encoded(
            owner
            | {
                "execution_id": "retirement_validation",
                "sandbox_profile": "restricted",
                "timeout_seconds": 1,
            }
        ),
        strict=True,
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != _KIND
        or name != "retired-" + _digest(_encoded(owner)) + ".json"
        or len(_encoded(value)) > 4096
        or any(
            type(value[key]) is not str or not re.fullmatch(_SHA, value[key])
            for key in (
                "cache_key",
                "reservation_sha256",
                "reference_sha256",
                "environment_tree_sha256",
                "snapshot_sha256",
                "archive_sha256",
            )
        )
        or type(value["origin_execution_id"]) is not str
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value["origin_execution_id"])
        or type(value["archive_id"]) is not str
        or str(UUID(value["archive_id"])) != value["archive_id"]
    ):
        raise ValueError("invalid stopped owner tombstone binding")
