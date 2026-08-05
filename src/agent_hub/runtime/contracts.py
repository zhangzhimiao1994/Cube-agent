"""Immutable, framework-neutral contracts shared by every execution runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.domain.runs import TaskMode

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")
_EXECUTABLE_MODES = frozenset(TaskMode) - {TaskMode.AUTO}
_MAX_JSON_DEPTH = 20
_MAX_JSON_NODES = 2_048
_MAX_JSON_STRING = 65_536
_MAX_JSON_BYTES = 262_144
_MAX_TEXT_BYTES = 65_536
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_ref",
        "password",
        "token",
        "authorization",
        "raw_prompt",
        "prompt",
        "hidden_reasoning",
        "reasoning",
        "chain_of_thought",
        "cot",
    }
)


def _mutable_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


def _strict_json_input(value: JsonValue) -> object:
    """Return built-in containers while preserving tuples required by strict parsing."""
    if isinstance(value, Mapping):
        return {key: _strict_json_input(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_strict_json_input(item) for item in value)
    return value


def _canonical_json_bytes(value: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        _mutable_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _freeze_json(value: object) -> JsonValue:
    nodes = 0

    def visit(item: object, depth: int) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("JSON value exceeds structural limits")
        if item is None or type(item) is bool or type(item) is int:
            return cast(JsonScalar, item)
        if type(item) is float:
            number = item
            if not math.isfinite(number):
                raise ValueError("JSON numbers must be finite")
            return number
        if type(item) is str:
            text = item
            if len(text.encode("utf-8")) > _MAX_JSON_STRING:
                raise ValueError("JSON string exceeds size limit")
            return text
        if isinstance(item, Mapping):
            frozen: dict[str, JsonValue] = {}
            for key, nested in item.items():
                if type(key) is not str:
                    raise TypeError("JSON object keys must be strings")
                frozen[key] = visit(nested, depth + 1)
            return MappingProxyType(frozen)
        if isinstance(item, list | tuple):
            return tuple(visit(nested, depth + 1) for nested in item)
        raise TypeError("value is not JSON-compatible")

    frozen = visit(value, 0)
    if isinstance(frozen, Mapping) and len(_canonical_json_bytes(frozen)) > _MAX_JSON_BYTES:
        raise ValueError("JSON value exceeds serialized size limit")
    return frozen


def _freeze_object(value: object, *, name: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError(f"{name} must be a JSON object")
    return frozen


def _contains_sensitive_key(value: JsonValue) -> bool:
    if isinstance(value, tuple):
        return any(_contains_sensitive_key(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    for key, item in value.items():
        normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        if normalized in _SENSITIVE_KEYS or normalized.endswith(
            ("_api_key", "_secret", "_password", "_access_token", "_refresh_token")
        ):
            return True
        if _contains_sensitive_key(item):
            return True
    return False


def _safe_free_text(value: str, *, name: str, max_bytes: int) -> str:
    if not value.strip() or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} must be nonblank and bounded")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{name} must use normalized Unicode")
    for character in value:
        category = unicodedata.category(character)
        if category == "Cf" or (category == "Cc" and character not in "\n\t"):
            raise ValueError(f"{name} contains unsafe control characters")
    return value


def _safe_identifier(value: str, *, name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


class RuntimeContractError(ValueError):
    """A stable error that never exposes hostile contract input."""


class _RuntimeContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        failed = False
        validated: Self | None = None
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            validated = cls.model_validate_json(encoded, strict=True)
        except Exception as error:  # noqa: BLE001 - hostile serialization boundary
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
            failed = True
        if failed or validated is None:
            raise RuntimeContractError("invalid runtime contract") from None
        return validated


class GatewayProvenance(_RuntimeContractModel):
    """Only gateway-trusted supplier metadata; credentials never belong here."""

    logical_model: str
    deployment_id: str
    provider_id: str
    provider_model: str = Field(repr=False, min_length=1, max_length=512)

    @field_validator("logical_model", "deployment_id", "provider_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _safe_identifier(value, name="provenance")

    @field_validator("provider_model")
    @classmethod
    def validate_provider_model(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("provider model must be unpadded printable text")
        return value

    @model_validator(mode="after")
    def provider_invariants(self) -> GatewayProvenance:
        if self.provider_model.split("/", 1)[0] != self.provider_id:
            raise ValueError("provider provenance is inconsistent")
        return self

    def to_payload(self) -> dict[str, object]:
        return {
            "logical_model": self.logical_model,
            "deployment_id": self.deployment_id,
            "provider_id": self.provider_id,
            "provider_model": self.provider_model,
        }


class Artifact(_RuntimeContractModel):
    """A bounded, content-addressed runtime output."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: UUID
    version: int = Field(default=1, ge=1, le=2**31 - 1)
    type: str
    producer: str
    content: Mapping[str, JsonValue] = Field(repr=False)
    source_ids: tuple[str, ...] = Field(default=(), max_length=64)
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    provenance: GatewayProvenance | None = None

    @field_validator("type", "producer")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _safe_identifier(value, name="artifact identifier")

    @field_validator("version")
    @classmethod
    def strict_version(cls, value: int) -> int:
        if type(value) is not int:
            raise TypeError("artifact version must be an integer")
        return value

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, value: object) -> object:
        return _strict_json_input(_freeze_object(value, name="artifact content"))

    @field_validator("source_ids", mode="before")
    @classmethod
    def freeze_source_ids(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple):
            raise TypeError("source_ids must be a list or tuple")
        result = tuple(value)
        if not all(type(item) is str for item in result):
            raise TypeError("source_ids must contain strings")
        for item in result:
            if str(UUID(item)) != item:
                raise ValueError("source_ids must contain canonical UUIDs")
        if len(set(result)) != len(result):
            raise ValueError("source_ids must be unique")
        return cast(tuple[str, ...], result)

    @model_validator(mode="after")
    def content_invariants(self) -> Artifact:
        object.__setattr__(self, "content", _freeze_object(self.content, name="artifact content"))
        if str(self.id) in self.source_ids:
            raise ValueError("artifact cannot list itself as a source")
        if _contains_sensitive_key(self.content):
            raise ValueError("artifact content contains hidden reasoning or sensitive state")
        if self.type == "text":
            text = self.content.get("text")
            if type(text) is not str:
                raise ValueError("text artifacts require text content")
            _safe_free_text(text, name="artifact text", max_bytes=_MAX_TEXT_BYTES)
        digest = self.recompute_content_sha256()
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("artifact content hash does not match content")
        object.__setattr__(self, "content_sha256", digest)
        return self

    def recompute_content_sha256(self) -> str:
        provenance: Mapping[str, JsonValue] | None = None
        if self.provenance is not None:
            provenance = MappingProxyType(
                cast(dict[str, JsonValue], GatewayProvenance.to_payload(self.provenance))
            )
        envelope: Mapping[str, JsonValue] = MappingProxyType(
            {
                "type": self.type,
                "producer": self.producer,
                "version": self.version,
                "content": self.content,
                "source_ids": self.source_ids,
                "provenance": provenance,
            }
        )
        return hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest()

    def to_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "version": self.version,
            "type": self.type,
            "producer": self.producer,
            "content": _mutable_json(self.content),
            "source_ids": list(self.source_ids),
            "content_sha256": self.content_sha256,
            "provenance": None
            if self.provenance is None
            else GatewayProvenance.to_payload(self.provenance),
        }


class RuntimeCheckpoint(_RuntimeContractModel):
    """A resumable boundary whose immutable state is locally content-addressed."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: UUID
    runtime_type: str
    runtime_version: str
    run_id: UUID
    tenant_id: UUID
    mode: TaskMode
    state: Mapping[str, JsonValue] = Field(repr=False)
    state_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$", repr=False)

    @field_validator("runtime_type")
    @classmethod
    def validate_runtime_type(cls, value: str) -> str:
        return _safe_identifier(value, name="runtime type")

    @field_validator("runtime_version")
    @classmethod
    def validate_runtime_version(cls, value: str) -> str:
        if _VERSION.fullmatch(value) is None:
            raise ValueError("runtime version is invalid")
        return value

    @field_validator("mode")
    @classmethod
    def executable_mode(cls, value: TaskMode) -> TaskMode:
        if value not in _EXECUTABLE_MODES:
            raise ValueError("checkpoint mode must be executable")
        return value

    @field_validator("state", mode="before")
    @classmethod
    def validate_state(cls, value: object) -> object:
        return _strict_json_input(_freeze_object(value, name="checkpoint state"))

    @model_validator(mode="after")
    def state_invariants(self) -> RuntimeCheckpoint:
        object.__setattr__(self, "state", _freeze_object(self.state, name="checkpoint state"))
        if _contains_sensitive_key(self.state):
            raise ValueError("checkpoint state contains sensitive data")
        digest = self.recompute_state_sha256()
        if self.state_sha256 and self.state_sha256 != digest:
            raise ValueError("checkpoint state hash does not match state")
        object.__setattr__(self, "state_sha256", digest)
        return self

    def recompute_state_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.state)).hexdigest()

    def to_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "runtime_type": self.runtime_type,
            "runtime_version": self.runtime_version,
            "run_id": str(self.run_id),
            "tenant_id": str(self.tenant_id),
            "mode": self.mode.value,
            "state": _mutable_json(self.state),
            "state_sha256": self.state_sha256,
        }


class EventKind(StrEnum):
    MODEL_STARTED = "model.started"
    ARTIFACT_CREATED = "artifact.created"
    CHECKPOINT_SAVED = "checkpoint.saved"
    RUNTIME_COMPLETED = "runtime.completed"
    RUNTIME_FAILED = "runtime.failed"
    RUNTIME_CANCELLED = "runtime.cancelled"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    STEP_RETRYING = "step.retrying"
    REVIEW_COMPLETED = "review.completed"
    DISCUSSION_STARTED = "discussion.started"
    MESSAGE_CREATED = "message.created"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    COST_RECORDED = "cost.recorded"


class RunEvent(_RuntimeContractModel):
    """One monotonically sequenced event within a run."""

    kind: EventKind | str
    sequence: int = Field(ge=1, le=2**63 - 1)
    run_id: UUID
    artifact: Artifact | None = None
    checkpoint: RuntimeCheckpoint | None = None
    reason: str | None = Field(default=None, repr=False, max_length=512)
    inputs: tuple[Artifact, ...] = Field(default=(), max_length=64)
    step_id: str | None = None
    actor: str | None = None
    message: str | None = Field(default=None, repr=False)
    payload: Mapping[str, JsonValue] = Field(default_factory=dict, repr=False)

    @field_validator("kind", mode="before")
    @classmethod
    def safe_kind(cls, value: object) -> EventKind | str:
        if isinstance(value, EventKind):
            return value
        if type(value) is not str or re.fullmatch(
            r"[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}", value
        ) is None:
            raise ValueError("event kind must be a safe namespaced identifier")
        try:
            return EventKind(value)
        except ValueError:
            return value

    @field_validator("sequence")
    @classmethod
    def strict_sequence(cls, value: int) -> int:
        if type(value) is not int:
            raise TypeError("sequence must be an integer")
        return value

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_free_text(value, name="event reason", max_bytes=512)
        return value

    @field_validator("step_id", "actor")
    @classmethod
    def safe_optional_identifier(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_identifier(value, name="event identifier")
        return value

    @field_validator("message")
    @classmethod
    def safe_message(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_free_text(value, name="event message", max_bytes=65_536)
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> object:
        return _strict_json_input(_freeze_object(value, name="event payload"))

    @field_validator("inputs", mode="before")
    @classmethod
    def freeze_inputs(cls, value: object) -> tuple[Artifact, ...]:
        if not isinstance(value, list | tuple):
            raise TypeError("event inputs must be a list or tuple")
        return tuple(value)

    @model_validator(mode="after")
    def event_invariants(self) -> RunEvent:
        object.__setattr__(self, "payload", _freeze_object(self.payload, name="event payload"))
        if _contains_sensitive_key(self.payload):
            raise ValueError("event payload contains sensitive data")
        if self.kind is EventKind.ARTIFACT_CREATED:
            if self.artifact is None or self.checkpoint is not None or self.reason is not None:
                raise ValueError("artifact.created requires only an artifact")
        elif self.kind is EventKind.CHECKPOINT_SAVED:
            if self.checkpoint is None or self.artifact is not None or self.reason is not None:
                raise ValueError("checkpoint.saved requires only a checkpoint")
            if self.checkpoint.run_id != self.run_id:
                raise ValueError("checkpoint event run_id mismatch")
        elif self.kind is EventKind.RUNTIME_FAILED:
            if self.reason is None or self.artifact is not None or self.checkpoint is not None:
                raise ValueError("runtime.failed requires only a reason")
        elif self.kind in {EventKind.MODEL_STARTED, EventKind.RUNTIME_CANCELLED}:
            if self.artifact is not None or self.checkpoint is not None:
                raise ValueError("event kind forbids artifact and checkpoint")
            if self.kind is EventKind.MODEL_STARTED and (self.reason is not None or self.inputs):
                raise ValueError("model.started forbids reason and inputs")
        elif self.kind is EventKind.RUNTIME_COMPLETED and (
            self.artifact is not None or self.checkpoint is not None
        ):
            raise ValueError("runtime.completed forbids artifact and checkpoint")
        if self.kind in {
            EventKind.ARTIFACT_CREATED,
            EventKind.CHECKPOINT_SAVED,
            EventKind.RUNTIME_FAILED,
        } and self.inputs:
            raise ValueError("event kind forbids inputs")
        step_kinds = {
            EventKind.STEP_STARTED,
            EventKind.STEP_COMPLETED,
            EventKind.STEP_FAILED,
            EventKind.STEP_RETRYING,
        }
        if self.kind in step_kinds and (self.step_id is None or self.actor is None):
            raise ValueError("step events require step_id and actor")
        if self.kind in {EventKind.STEP_FAILED, EventKind.TOOL_FAILED} and self.reason is None:
            raise ValueError("failed events require a reason")
        if self.kind is EventKind.MESSAGE_CREATED and (
            self.actor is None or self.message is None
        ):
            raise ValueError("message.created requires actor and message")
        direct_kinds = {
            EventKind.MODEL_STARTED,
            EventKind.ARTIFACT_CREATED,
            EventKind.CHECKPOINT_SAVED,
            EventKind.RUNTIME_COMPLETED,
            EventKind.RUNTIME_FAILED,
            EventKind.RUNTIME_CANCELLED,
        }
        if self.kind in direct_kinds and (
            self.step_id is not None or self.actor is not None or self.message is not None or self.payload
        ):
            raise ValueError("direct runtime events forbid extension fields")
        if self.artifact is not None and self.kind is not EventKind.ARTIFACT_CREATED:
            raise ValueError("only artifact.created may carry an artifact")
        if self.checkpoint is not None and self.kind is not EventKind.CHECKPOINT_SAVED:
            raise ValueError("only checkpoint.saved may carry a checkpoint")
        if self.message is not None and self.kind is not EventKind.MESSAGE_CREATED:
            raise ValueError("only message.created may carry a message")
        if (
            isinstance(self.kind, str)
            and not isinstance(self.kind, EventKind)
            and (not self.payload or self.artifact is not None or self.checkpoint is not None)
        ):
            raise ValueError("extension events require payload and forbid direct objects")
        return self


class TaskContext(_RuntimeContractModel):
    """The only durable input accepted by an execution runtime."""

    run_id: UUID
    tenant_id: UUID
    mode: TaskMode
    request: str = Field(repr=False)
    artifacts: tuple[Artifact, ...] = Field(default=(), max_length=64)
    checkpoint: RuntimeCheckpoint | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600, allow_inf_nan=False)
    token_budget: int = Field(default=16_384, ge=1, le=10_000_000)

    @field_validator("mode")
    @classmethod
    def executable_mode(cls, value: TaskMode) -> TaskMode:
        if value not in _EXECUTABLE_MODES:
            raise ValueError("task mode must be executable")
        return value

    @field_validator("request")
    @classmethod
    def safe_request(cls, value: str) -> str:
        return _safe_free_text(value, name="request", max_bytes=65_536)

    @field_validator("artifacts", mode="before")
    @classmethod
    def freeze_artifacts(cls, value: object) -> tuple[Artifact, ...]:
        if not isinstance(value, list | tuple):
            raise TypeError("artifacts must be a list or tuple")
        return tuple(value)

    @field_validator("timeout_seconds")
    @classmethod
    def strict_timeout(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise TypeError("timeout must be a finite number")
        return float(value)

    @field_validator("token_budget")
    @classmethod
    def strict_budget(cls, value: int) -> int:
        if type(value) is not int:
            raise TypeError("token budget must be an integer")
        return value

    @model_validator(mode="after")
    def context_invariants(self) -> TaskContext:
        artifact_ids = [item.id for item in self.artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("task artifacts must be unique")
        if self.checkpoint is not None and (
            self.checkpoint.run_id != self.run_id
            or self.checkpoint.tenant_id != self.tenant_id
            or self.checkpoint.mode is not self.mode
        ):
            raise ValueError("checkpoint run, tenant, or mode does not match task context")
        return self

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "tenant_id": str(self.tenant_id),
            "mode": self.mode.value,
            "request": self.request,
            "artifacts": [Artifact.to_payload(item) for item in self.artifacts],
            "checkpoint": None
            if self.checkpoint is None
            else RuntimeCheckpoint.to_payload(self.checkpoint),
            "timeout_seconds": self.timeout_seconds,
            "token_budget": self.token_budget,
        }


class ExecutionRuntime(Protocol):
    mode: TaskMode

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]: ...

    async def save_checkpoint(self) -> RuntimeCheckpoint: ...

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None: ...

    async def cancel(self) -> None: ...


Artifact.__hash__ = None  # type: ignore[assignment]
RuntimeCheckpoint.__hash__ = None  # type: ignore[assignment]
RunEvent.__hash__ = None  # type: ignore[assignment]
TaskContext.__hash__ = None  # type: ignore[assignment]
