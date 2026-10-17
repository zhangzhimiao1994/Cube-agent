"""Private transport continuation, never a public Artifact or checkpoint payload."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_hub.models.native_messages import NativeAssistantMessage
from agent_hub.runtime.contracts import GatewayProvenance

MAX_SNAPSHOT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LEDGER_KEY = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")
_FIELDS = frozenset({
    "version", "protocol", "id", "tenant_id", "run_id", "ledger_key", "request_sha256",
    "model_artifact_sha256", "provenance", "assistant",
})
_PROVENANCE_FIELDS = frozenset({"logical_model", "deployment_id", "provider_id", "provider_model"})


class ProviderContinuationError(RuntimeError):
    """Fixed, repr-safe private contract error."""


class ProviderContinuationRepositoryError(ProviderContinuationError):
    """Fixed storage failure; never include payloads, SQL or driver parameters."""


def _canonical(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )


def _is_digest(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, repr=False)
class ProviderContinuationSnapshot:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    ledger_key: str
    request_sha256: str
    model_artifact_sha256: str
    provenance: GatewayProvenance
    assistant: NativeAssistantMessage

    def __repr__(self) -> str:
        return "ProviderContinuationSnapshot(<private>)"

    def __post_init__(self) -> None:
        valid = False
        try:
            if (
                all(type(value) is UUID for value in (self.id, self.tenant_id, self.run_id))
                and type(self.ledger_key) is str and _LEDGER_KEY.fullmatch(self.ledger_key)
                and _is_digest(self.request_sha256) and _is_digest(self.model_artifact_sha256)
                and type(self.provenance) is GatewayProvenance
                and type(self.assistant) is NativeAssistantMessage
                and self.provenance.deployment_id == self.assistant.deployment_id
                and GatewayProvenance.model_validate(
                    self.provenance.to_payload(), strict=True,
                ) == self.provenance
                and NativeAssistantMessage.from_payload(self.assistant.to_payload()) == self.assistant
            ):
                valid = len(_canonical(self.to_payload()).encode("utf-8")) <= MAX_SNAPSHOT_BYTES
        except (TypeError, ValueError, OverflowError, RecursionError):
            pass
        if not valid:
            raise ProviderContinuationError("provider continuation is invalid")

    @property
    def protocol(self) -> str:
        return "chat_completions"

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 1, "protocol": self.protocol, "id": str(self.id),
            "tenant_id": str(self.tenant_id), "run_id": str(self.run_id),
            "ledger_key": self.ledger_key, "request_sha256": self.request_sha256,
            "model_artifact_sha256": self.model_artifact_sha256,
            "provenance": self.provenance.to_payload(), "assistant": self.assistant.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProviderContinuationSnapshot:
        result = None
        try:
            if (
                type(payload) is dict and payload.keys() == _FIELDS
                and type(payload["version"]) is int and payload["version"] == 1
                and payload["protocol"] == "chat_completions"
                and all(type(payload[key]) is str for key in ("id", "tenant_id", "run_id"))
                and type(payload["provenance"]) is dict
                and payload["provenance"].keys() == _PROVENANCE_FIELDS
            ):
                candidate = cls(
                    id=UUID(payload["id"]), tenant_id=UUID(payload["tenant_id"]),
                    run_id=UUID(payload["run_id"]), ledger_key=payload["ledger_key"],
                    request_sha256=payload["request_sha256"],
                    model_artifact_sha256=payload["model_artifact_sha256"],
                    provenance=GatewayProvenance.model_validate(payload["provenance"], strict=True),
                    assistant=NativeAssistantMessage.from_payload(payload["assistant"]),
                )
                if _canonical(candidate.to_payload()) == _canonical(payload):
                    result = candidate
        except (TypeError, ValueError, OverflowError, RecursionError, KeyError, ProviderContinuationError):
            pass
        if result is None:
            raise ProviderContinuationError("provider continuation is invalid")
        return result

    @property
    def sha256(self) -> str:
        return hashlib.sha256(encode_snapshot(self).encode("utf-8")).hexdigest()

    def reference(self) -> dict[str, str]:
        return {"id": str(self.id), "sha256": self.sha256}


def encode_snapshot(snapshot: ProviderContinuationSnapshot) -> str:
    result = None
    try:
        if type(snapshot) is ProviderContinuationSnapshot:
            validated = ProviderContinuationSnapshot.from_payload(snapshot.to_payload())
            result = _canonical(validated.to_payload())
    except (TypeError, ValueError, OverflowError, RecursionError, AttributeError, ProviderContinuationError):
        pass
    if result is None:
        raise ProviderContinuationRepositoryError("provider continuation is invalid")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate private payload key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("invalid private JSON number")


def decode_snapshot(payload: str) -> ProviderContinuationSnapshot:
    result = None
    try:
        if type(payload) is str and 0 < len(payload.encode("utf-8")) <= MAX_SNAPSHOT_BYTES:
            candidate = ProviderContinuationSnapshot.from_payload(json.loads(
                payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
            ))
            if encode_snapshot(candidate) == payload:
                result = candidate
    except (TypeError, ValueError, OverflowError, RecursionError, ProviderContinuationError):
        pass
    if result is None:
        raise ProviderContinuationRepositoryError("provider continuation is unavailable")
    return result


def validate_reference(tenant_id: UUID, run_id: UUID, id: UUID, sha256: str) -> None:
    if not all(type(value) is UUID for value in (tenant_id, run_id, id)) or not _is_digest(sha256):
        raise ProviderContinuationRepositoryError("provider continuation reference is invalid")


def validate_limits(*limits: int) -> None:
    if any(type(limit) is not int or limit < 1 for limit in limits):
        raise ValueError("provider continuation limits must be positive integers")


class ProviderContinuationRepository(Protocol):
    async def put(self, snapshot: ProviderContinuationSnapshot) -> None: ...

    async def get(
        self, tenant_id: UUID, run_id: UUID, id: UUID, sha256: str,
    ) -> ProviderContinuationSnapshot: ...


class InMemoryProviderContinuationRepository:
    """Single-process equivalent; only the PostgreSQL implementation checks RunRow existence.

    A stored orphan is not a published model-ledger reference or permission to retry.
    Publication belongs to the caller's existing usage/checkpoint commit fence.
    """

    def __init__(
        self, *, max_snapshots_per_run: int = 16_384,
        max_total_bytes_per_run: int = 64 * 1024 * 1024,
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        validate_limits(max_snapshots_per_run, max_total_bytes_per_run, max_snapshot_bytes)
        self._max_snapshots_per_run = max_snapshots_per_run
        self._max_total_bytes_per_run = max_total_bytes_per_run
        self._max_snapshot_bytes = max_snapshot_bytes
        self._payloads: dict[UUID, str] = {}
        self._ledger_ids: dict[tuple[UUID, UUID, str], UUID] = {}
        self._scope_counts: dict[tuple[UUID, UUID], int] = {}
        self._scope_bytes: dict[tuple[UUID, UUID], int] = {}
        self._lock = asyncio.Lock()

    async def put(self, snapshot: ProviderContinuationSnapshot) -> None:
        payload = encode_snapshot(snapshot)
        size = len(payload.encode("utf-8"))
        if size > self._max_snapshot_bytes:
            raise ProviderContinuationRepositoryError("provider continuation capacity exceeded")
        scope = snapshot.tenant_id, snapshot.run_id
        ledger = (*scope, snapshot.ledger_key)
        async with self._lock:
            existing = self._payloads.get(snapshot.id)
            if existing is not None:
                if existing != payload or self._ledger_ids.get(ledger) != snapshot.id:
                    raise ProviderContinuationRepositoryError("provider continuation is unavailable")
                return
            if ledger in self._ledger_ids:
                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
            count, total = self._scope_counts.get(scope, 0), self._scope_bytes.get(scope, 0)
            if count >= self._max_snapshots_per_run or total + size > self._max_total_bytes_per_run:
                raise ProviderContinuationRepositoryError("provider continuation capacity exceeded")
            self._payloads[snapshot.id] = payload
            self._ledger_ids[ledger] = snapshot.id
            self._scope_counts[scope], self._scope_bytes[scope] = count + 1, total + size

    async def get(
        self, tenant_id: UUID, run_id: UUID, id: UUID, sha256: str,
    ) -> ProviderContinuationSnapshot:
        validate_reference(tenant_id, run_id, id, sha256)
        async with self._lock:
            payload = self._payloads.get(id)
            if payload is None:
                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
            snapshot = decode_snapshot(payload)
            if (
                snapshot.id != id or snapshot.tenant_id != tenant_id or snapshot.run_id != run_id
                or snapshot.sha256 != sha256 or len(payload.encode()) > self._max_snapshot_bytes
                or len(payload.encode()) > self._max_total_bytes_per_run
                or self._ledger_ids.get((tenant_id, run_id, snapshot.ledger_key)) != id
            ):
                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
            return snapshot
