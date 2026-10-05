"""Immutable cleanup facts shared with the privileged broker (stdlib only)."""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Self, cast
from uuid import UUID, uuid4

STAGES = ("build", "install", "probe", "start")
BROKER_RESOURCES = (
    "attachment", *(f"unit_{s}" for s in STAGES), *(f"cgroup_{s}" for s in STAGES),
    "mount_unit", "work_path", "owned_directory",
)
MANAGER_RESOURCES = ("capability", "snapshot")
STATIC_RESOURCES = (
    "serving_thread", "listener", "accepted_threads", "accepted_sockets",
    "proxy_operations", "proxy_sockets",
)
DYNAMIC_UNOBSERVED = ("private_network_namespace", "private_port", "other_mount_namespaces")
REASONS = ("explicit", "expired", "replaced", "disconnect", "shutdown", "recovery")
REASON_CODES = (
    "observed", "not_attempted", "observation_failed", "resource_present", "budget_exhausted",
    "interrupted", "persistence_failed", "unbound_owner",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid observation timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid observation timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("observation timestamp needs timezone")
    try:
        return result.astimezone(UTC)
    except OverflowError:
        raise ValueError("observation timestamp is outside UTC range") from None


def _time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation timestamp needs timezone")


def wire_object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid cleanup fields")
    return cast(dict[str, object], value)


def strict_json(data: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate cleanup field")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except RecursionError:
        raise ValueError("cleanup JSON nesting exceeds parser limit") from None


def _uuid(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid canonical UUID")
    return value


def _label(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) is None:
        raise ValueError("invalid owner label")
    return value


def display_path(value: object, *, root: bool = False) -> str:
    if root and value == ".":
        return "."
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or any(part in {"", ".", ".."} or part.startswith(".")
                   for part in value.split("/"))
            or re.fullmatch(r"[A-Za-z0-9_./-]+", value) is None):
        raise ValueError("invalid relative display path")
    return value


def framed(*parts: bytes) -> bytes:
    return b"".join(struct.pack("!Q", len(part)) + part for part in parts)


def tree_entry(relative: str, *, directory: bool, executable: bool, data: bytes) -> bytes:
    return framed(relative.encode("utf-8"), b"dir" if directory else b"exec" if executable
                  else b"file", data)


@dataclass(frozen=True, slots=True)
class PreviewOwnerScope:
    tenant_id: str
    user_id: str | None
    project_id: str
    conversation_id: str
    workspace_session_id: str
    display_root: str = "."
    display_entrypoint: str = "index.html"

    def __post_init__(self) -> None:
        _uuid(self.tenant_id)
        if self.user_id is not None:
            _uuid(self.user_id)
        for value in (self.project_id, self.conversation_id, self.workspace_session_id):
            _label(value)
        display_path(self.display_root, root=True)
        display_path(self.display_entrypoint)

    def to_wire(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {f.name for f in fields(cls)})
        return cls(**cast(dict[str, str], data))


@dataclass(frozen=True, slots=True)
class PreviewIdentityV1:
    preview_id: str
    kind: str
    tenant_id: str
    user_id: str | None
    project_id: str
    conversation_id: str
    workspace_session_id: str
    runtime_handle: str | None
    source_scheme: str
    source_sha256: str
    display_root: str
    display_entrypoint: str

    def __post_init__(self) -> None:
        _uuid(self.preview_id)
        _ = self.scope
        if not isinstance(self.kind, str) or self.kind not in {"static", "dynamic"}:
            raise ValueError("invalid preview kind")
        expected = "preview-broker-tree-v2" if self.kind == "dynamic" else "preview-static-tree-v1"
        if self.source_scheme != expected:
            raise ValueError("invalid source identity scheme")
        if not isinstance(self.source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None:
            raise ValueError("invalid source digest")
        if self.kind == "static":
            if self.runtime_handle is not None:
                raise ValueError("static preview cannot have a broker handle")
        elif not isinstance(self.runtime_handle, str) or re.fullmatch(r"[0-9a-f]{32}", self.runtime_handle) is None:
            raise ValueError("invalid broker identity")

    @property
    def scope(self) -> PreviewOwnerScope:
        return PreviewOwnerScope(self.tenant_id, self.user_id, self.project_id,
                                 self.conversation_id, self.workspace_session_id,
                                 self.display_root, self.display_entrypoint)

    @classmethod
    def create(cls, preview_id: str, scope: PreviewOwnerScope, *, kind: str,
               digest: str, handle: str | None = None) -> Self:
        return cls(preview_id, kind, scope.tenant_id, scope.user_id, scope.project_id,
                   scope.conversation_id, scope.workspace_session_id, handle,
                   "preview-broker-tree-v2" if kind == "dynamic" else "preview-static-tree-v1",
                   digest, scope.display_root, scope.display_entrypoint)

    def to_wire(self) -> dict[str, object]:
        return {"preview_id": self.preview_id, "kind": self.kind, **self.scope.to_wire(),
                "runtime_handle": self.runtime_handle,
                "source": {"scheme": self.source_scheme, "sha256": self.source_sha256}}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {"preview_id", "kind", "runtime_handle", "source",
                                  *(f.name for f in fields(PreviewOwnerScope))})
        source = wire_object(data["source"], {"scheme", "sha256"})
        values = {key: val for key, val in data.items() if key != "source"}
        values.update(source_scheme=source["scheme"], source_sha256=source["sha256"])
        return cls(**cast(dict[str, str], values))

    def binding_bytes(self, allowed_uid: int, runtime_root: str) -> bytes:
        encoded = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":")).encode()
        return framed(b"preview-identity-v2", b"2", str(allowed_uid).encode(),
                      runtime_root.encode(), encoded)


def _success(resource: str) -> set[str]:
    if resource.startswith("cgroup_"):
        return {"absent", "empty"}
    if resource == "work_path":
        return {"absent", "not_mountpoint"}
    if resource.startswith("unit_"):
        return {"inactive", "failed"}
    if resource == "mount_unit":
        return {"inactive"}
    if resource == "capability":
        return {"revoked"}
    if resource in {"listener", "accepted_sockets", "proxy_sockets"}:
        return {"closed"}
    if resource in {"snapshot", "owned_directory"}:
        return {"absent"}
    return {"exited"} if resource in {"attachment", "serving_thread"} else {"drained"}


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    resource: str
    observer: str
    observed_at: datetime
    result: str
    reason_code: str
    load_state: str | None = None
    active_state: str | None = None
    main_pid: int | None = None
    identity_match: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resource, str) or self.resource not in (*BROKER_RESOURCES, *MANAGER_RESOURCES, *STATIC_RESOURCES):
            raise ValueError("unknown cleanup resource")
        if self.observer != ("broker" if self.resource in BROKER_RESOURCES else "manager"):
            raise ValueError("invalid resource observer")
        _time(self.observed_at)
        if not isinstance(self.result, str) or self.result not in _success(self.resource) | {"present", "unknown"}:
            raise ValueError("invalid resource observation")
        if self.reason_code not in REASON_CODES:
            raise ValueError("invalid observation reason")
        unit = self.resource.startswith("unit_") or self.resource == "mount_unit"
        if unit and self.result in _success(self.resource):
            if (self.identity_match is not True or self.load_state not in ("loaded", "not-found")
                    or self.active_state != self.result
                    or (self.load_state == "not-found" and self.result != "inactive")):
                raise ValueError("incomplete systemd observation")
            if self.resource != "mount_unit" and (type(self.main_pid) is not int or self.main_pid != 0):
                raise ValueError("incomplete process observation")
            if self.resource == "mount_unit" and self.main_pid is not None:
                raise ValueError("invalid mount process observation")
        elif any(v is not None for v in (self.load_state, self.active_state, self.main_pid, self.identity_match)):
            raise ValueError("unexpected systemd fields")

    def to_wire(self) -> dict[str, object]:
        return {**{f.name: getattr(self, f.name) for f in fields(self)},
                "observed_at": self.observed_at.isoformat()}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {f.name for f in fields(cls)})
        return cls(cast(str, data["resource"]), cast(str, data["observer"]),
                   timestamp(data["observed_at"]), cast(str, data["result"]),
                   cast(str, data["reason_code"]), cast(str | None, data["load_state"]),
                   cast(str | None, data["active_state"]), cast(int | None, data["main_pid"]),
                   cast(bool | None, data["identity_match"]))


def complete_facts(required: tuple[str, ...], observations: tuple[ResourceObservation, ...],
                   at: datetime, reason_code: str = "not_attempted") -> tuple[ResourceObservation, ...]:
    found = {fact.resource: fact for fact in observations}
    if len(found) != len(observations) or not found.keys() <= set(required):
        raise ValueError("invalid cleanup coverage")
    return tuple(found.get(resource) or ResourceObservation(
        resource, "broker" if resource in BROKER_RESOURCES else "manager", at, "unknown", reason_code,
    ) for resource in required)


def _status(observations: tuple[ResourceObservation, ...]) -> str:
    if any(f.result == "unknown" for f in observations):
        return "unknown"
    return "pending" if any(f.result == "present" for f in observations) else "confirmed"


def _validate_attempt(observation_id: str, requested: datetime, observed: datetime,
                      observations: tuple[ResourceObservation, ...], required: tuple[str, ...],
                      status: str) -> None:
    _uuid(observation_id)
    _time(requested)
    _time(observed)
    if requested > observed or any(f.observed_at > observed for f in observations):
        raise ValueError("invalid observation time order")
    if not isinstance(observations, tuple) or tuple(f.resource for f in observations) != required:
        raise ValueError("missing or duplicate coverage")
    if status != _status(observations):
        raise ValueError("inconsistent cleanup summary")


@dataclass(frozen=True, slots=True)
class BrokerCleanupObservation:
    identity: PreviewIdentityV1
    observation_id: str
    requested_at: datetime
    observed_at: datetime
    status: str
    observations: tuple[ResourceObservation, ...]

    def __post_init__(self) -> None:
        if self.identity.kind != "dynamic":
            raise ValueError("broker requires dynamic identity")
        _validate_attempt(self.observation_id, self.requested_at, self.observed_at,
                          self.observations, BROKER_RESOURCES, self.status)
        if any(f.observed_at < self.requested_at for f in self.observations):
            raise ValueError("stale broker facts")

    @classmethod
    def create(cls, identity: PreviewIdentityV1, observations: tuple[ResourceObservation, ...],
               *, requested_at: datetime, reason_code: str = "not_attempted") -> Self:
        now = max(utc_now(), requested_at)
        facts = complete_facts(BROKER_RESOURCES, observations, now, reason_code)
        return cls(identity, str(uuid4()), requested_at, now, _status(facts), facts)

    def to_wire(self) -> dict[str, object]:
        return {"identity": self.identity.to_wire(), "observation_id": self.observation_id,
                "requested_at": self.requested_at.isoformat(), "observed_at": self.observed_at.isoformat(),
                "status": self.status, "observations": [f.to_wire() for f in self.observations]}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {f.name for f in fields(cls)})
        return cls(PreviewIdentityV1.from_wire(data["identity"]), cast(str, data["observation_id"]),
                   timestamp(data["requested_at"]), timestamp(data["observed_at"]),
                   cast(str, data["status"]), _facts(data["observations"]))


def _facts(value: object) -> tuple[ResourceObservation, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("invalid resource facts")
    return tuple(ResourceObservation.from_wire(f) for f in value)


@dataclass(frozen=True, slots=True)
class CleanupReceiptV1:
    schema_version: int
    identity: PreviewIdentityV1
    observation_id: str
    requested_at: datetime
    observed_at: datetime
    reason: str
    status: str
    coverage: str
    observations: tuple[ResourceObservation, ...]
    unobserved: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1 or self.reason not in REASONS:
            raise ValueError("invalid cleanup version/reason")
        dynamic = self.identity.kind == "dynamic"
        if (self.coverage != ("dynamic-systemd-tmpfs-v1" if dynamic else "static-loopback-v1")
                or self.unobserved != (DYNAMIC_UNOBSERVED if dynamic else ())):
            raise ValueError("invalid coverage profile")
        _validate_attempt(self.observation_id, self.requested_at, self.observed_at, self.observations,
                          (BROKER_RESOURCES if dynamic else STATIC_RESOURCES) + MANAGER_RESOURCES,
                          self.status)

    @classmethod
    def create(cls, identity: PreviewIdentityV1, observations: tuple[ResourceObservation, ...],
               *, requested_at: datetime, reason: str, reason_code: str = "not_attempted") -> Self:
        now = max(utc_now(), requested_at, *(f.observed_at for f in observations))
        dynamic = identity.kind == "dynamic"
        facts = complete_facts((BROKER_RESOURCES if dynamic else STATIC_RESOURCES) + MANAGER_RESOURCES,
                               observations, now, reason_code)
        return cls(1, identity, str(uuid4()), requested_at, now, reason, _status(facts),
                   "dynamic-systemd-tmpfs-v1" if dynamic else "static-loopback-v1", facts,
                   DYNAMIC_UNOBSERVED if dynamic else ())

    def to_wire(self) -> dict[str, object]:
        return {"schema_version": 1, "identity": self.identity.to_wire(),
                "observation_id": self.observation_id, "requested_at": self.requested_at.isoformat(),
                "observed_at": self.observed_at.isoformat(), "reason": self.reason, "status": self.status,
                "coverage": self.coverage, "observations": [f.to_wire() for f in self.observations],
                "unobserved": list(self.unobserved)}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {f.name for f in fields(cls)})
        if not isinstance(data["unobserved"], list):
            raise ValueError("invalid unobserved list")  # noqa: TRY004
        return cls(cast(int, data["schema_version"]), PreviewIdentityV1.from_wire(data["identity"]),
                   cast(str, data["observation_id"]), timestamp(data["requested_at"]),
                   timestamp(data["observed_at"]), cast(str, data["reason"]), cast(str, data["status"]),
                   cast(str, data["coverage"]), _facts(data["observations"]), tuple(data["unobserved"]))


@dataclass(frozen=True, slots=True)
class PreviewCleanupRecord:
    identity: PreviewIdentityV1
    cleanup_receipt: CleanupReceiptV1 | None
    retention_expires_at: datetime | None

    def __post_init__(self) -> None:
        if self.cleanup_receipt is not None and self.cleanup_receipt.identity != self.identity:
            raise ValueError("receipt identity mismatch")
        if self.retention_expires_at is not None:
            _time(self.retention_expires_at)

    def to_wire(self) -> dict[str, object]:
        return {"identity": self.identity.to_wire(), "cleanup_receipt":
                self.cleanup_receipt.to_wire() if self.cleanup_receipt else None,
                "retention_expires_at": self.retention_expires_at.isoformat()
                if self.retention_expires_at else None}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {f.name for f in fields(cls)})
        return cls(PreviewIdentityV1.from_wire(data["identity"]), CleanupReceiptV1.from_wire(
            data["cleanup_receipt"]) if data["cleanup_receipt"] is not None else None,
            timestamp(data["retention_expires_at"]) if data["retention_expires_at"] is not None else None)
