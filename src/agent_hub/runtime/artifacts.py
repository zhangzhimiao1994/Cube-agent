"""Bounded, tenant-scoped durable artifact repository contracts."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_hub.runtime.contracts import Artifact

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAX_ARTIFACTS_PER_RUN = 16_384
_DEFAULT_MAX_TOTAL_BYTES_PER_RUN = 64 * 1024 * 1024
_DEFAULT_MAX_ARTIFACT_BYTES = 1024 * 1024
_DEFAULT_MAX_BATCH_SIZE = 16_384
_DEFAULT_MAX_WRITE_RESERVATIONS_PER_RUN = 65_536


class ArtifactRepositoryError(RuntimeError):
    """Stable repository failure that never exposes artifact content."""


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    id: UUID
    sha256: str

    def __post_init__(self) -> None:
        if type(self.id) is not UUID or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("artifact reference is invalid")

    def to_payload(self) -> dict[str, str]:
        return {"id": str(self.id), "sha256": self.sha256}


class ArtifactRepository(Protocol):
    async def reserve_write(
        self,
        tenant_id: UUID,
        run_id: UUID,
        reference: ArtifactReference,
        *,
        write_id: UUID,
    ) -> None: ...

    async def put(
        self,
        tenant_id: UUID,
        run_id: UUID,
        artifact: Artifact,
        *,
        write_id: UUID | None = None,
    ) -> None: ...

    async def get_many(
        self,
        tenant_id: UUID,
        run_id: UUID,
        references: tuple[ArtifactReference, ...],
    ) -> tuple[Artifact, ...]: ...

    async def abort_write(
        self,
        tenant_id: UUID,
        run_id: UUID,
        reference: ArtifactReference,
        *,
        write_id: UUID,
    ) -> bool:
        """Fence and release a write; implementations must propagate cancellation."""
        ...


class InMemoryArtifactRepository:
    """Single-process repository; inject one shared instance for cross-runtime recovery.

    A ``written`` reservation is the durable ownership proof for a checkpointed
    artifact.  Publication deliberately requires no second repository transition.
    """

    def __init__(
        self,
        *,
        max_artifacts_per_run: int = _DEFAULT_MAX_ARTIFACTS_PER_RUN,
        max_total_bytes_per_run: int = _DEFAULT_MAX_TOTAL_BYTES_PER_RUN,
        max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        max_write_reservations_per_run: int = _DEFAULT_MAX_WRITE_RESERVATIONS_PER_RUN,
    ) -> None:
        limits = (
            max_artifacts_per_run,
            max_total_bytes_per_run,
            max_artifact_bytes,
            max_batch_size,
            max_write_reservations_per_run,
        )
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("artifact repository limits must be positive integers")
        self._max_artifacts_per_run = max_artifacts_per_run
        self._max_total_bytes_per_run = max_total_bytes_per_run
        self._max_artifact_bytes = max_artifact_bytes
        self._max_batch_size = max_batch_size
        self._max_write_reservations_per_run = max_write_reservations_per_run
        self._artifacts: dict[tuple[UUID, UUID, UUID], Artifact] = {}
        self._sizes: dict[tuple[UUID, UUID, UUID], int] = {}
        self._scope_counts: dict[tuple[UUID, UUID], int] = {}
        self._scope_bytes: dict[tuple[UUID, UUID], int] = {}
        self._write_owners: dict[tuple[UUID, UUID, UUID], set[UUID] | None] = {}
        self._write_reservations: dict[tuple[UUID, UUID, UUID], tuple[ArtifactReference, str]] = {}
        self._scope_reservation_counts: dict[tuple[UUID, UUID], int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _write_identity(
        reference: ArtifactReference,
        write_id: UUID,
    ) -> None:
        if type(reference) is not ArtifactReference or type(write_id) is not UUID:
            raise ArtifactRepositoryError("artifact write identity is invalid")

    def _reserve_locked(
        self,
        scope: tuple[UUID, UUID],
        reference: ArtifactReference,
        write_id: UUID,
    ) -> None:
        reservation_key = (*scope, write_id)
        existing = self._write_reservations.get(reservation_key)
        if existing is not None:
            if existing[0] != reference or existing[1] not in {"reserved", "written"}:
                raise ArtifactRepositoryError("artifact is unavailable")
            return
        count = self._scope_reservation_counts.get(scope, 0)
        if count >= self._max_write_reservations_per_run:
            raise ArtifactRepositoryError("artifact repository capacity exceeded")
        self._write_reservations[reservation_key] = (reference, "reserved")
        self._scope_reservation_counts[scope] = count + 1

    async def reserve_write(
        self,
        tenant_id: UUID,
        run_id: UUID,
        reference: ArtifactReference,
        *,
        write_id: UUID,
    ) -> None:
        scope = self._scope(tenant_id, run_id)
        self._write_identity(reference, write_id)
        async with self._lock:
            self._reserve_locked(scope, reference, write_id)

    @staticmethod
    def _scope(tenant_id: UUID | str, run_id: UUID | str) -> tuple[UUID, UUID]:
        try:
            normalized_tenant_id = (
                tenant_id if type(tenant_id) is UUID else UUID(str(tenant_id))
            )
            normalized_run_id = run_id if type(run_id) is UUID else UUID(str(run_id))
        except (TypeError, ValueError):
            raise ArtifactRepositoryError("artifact scope is invalid")
        if (
            str(normalized_tenant_id) != str(tenant_id)
            or str(normalized_run_id) != str(run_id)
        ):
            raise ArtifactRepositoryError("artifact scope is invalid")
        return normalized_tenant_id, normalized_run_id

    def _encoded_size(self, artifact: Artifact) -> int:
        if type(artifact) is not Artifact:
            raise ArtifactRepositoryError("artifact is invalid")
        try:
            encoded = json.dumps(
                artifact.to_payload(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ArtifactRepositoryError("artifact is invalid") from None
        size = len(encoded)
        if size > self._max_artifact_bytes:
            raise ArtifactRepositoryError("artifact repository capacity exceeded")
        return size

    async def put(
        self,
        tenant_id: UUID,
        run_id: UUID,
        artifact: Artifact,
        *,
        write_id: UUID | None = None,
    ) -> None:
        scope = self._scope(tenant_id, run_id)
        if write_id is not None and type(write_id) is not UUID:
            raise ArtifactRepositoryError("artifact write identity is invalid")
        size = self._encoded_size(artifact)
        key = (*scope, artifact.id)
        async with self._lock:
            if write_id is not None:
                reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
                self._reserve_locked(scope, reference, write_id)
            existing = self._artifacts.get(key)
            if existing is not None:
                if existing.content_sha256 != artifact.content_sha256:
                    raise ArtifactRepositoryError("artifact is unavailable")
                owners = self._write_owners[key]
                if owners is not None:
                    if write_id is None:
                        self._write_owners[key] = None
                    else:
                        owners.add(write_id)
                if write_id is not None:
                    self._write_reservations[(*scope, write_id)] = (
                        reference,
                        "written",
                    )
                return
            count = self._scope_counts.get(scope, 0)
            total = self._scope_bytes.get(scope, 0)
            if count >= self._max_artifacts_per_run or total + size > self._max_total_bytes_per_run:
                raise ArtifactRepositoryError("artifact repository capacity exceeded")
            self._artifacts[key] = artifact
            self._sizes[key] = size
            self._scope_counts[scope] = count + 1
            self._scope_bytes[scope] = total + size
            self._write_owners[key] = None if write_id is None else {write_id}
            if write_id is not None:
                self._write_reservations[(*scope, write_id)] = (
                    reference,
                    "written",
                )

    async def get_many(
        self,
        tenant_id: UUID,
        run_id: UUID,
        references: tuple[ArtifactReference, ...],
    ) -> tuple[Artifact, ...]:
        scope = self._scope(tenant_id, run_id)
        if (
            type(references) is not tuple
            or len(references) > self._max_batch_size
            or not all(type(reference) is ArtifactReference for reference in references)
            or len({reference.id for reference in references}) != len(references)
        ):
            raise ArtifactRepositoryError("artifact references are invalid")
        async with self._lock:
            resolved: list[Artifact] = []
            total = 0
            for reference in references:
                key = (*scope, reference.id)
                artifact = self._artifacts.get(key)
                if (
                    artifact is None
                    or artifact.content_sha256 != reference.sha256
                    or artifact.recompute_content_sha256() != reference.sha256
                ):
                    raise ArtifactRepositoryError("artifact is unavailable")
                total += self._sizes[key]
                if total > self._max_total_bytes_per_run:
                    raise ArtifactRepositoryError("artifact repository capacity exceeded")
                resolved.append(artifact)
            return tuple(resolved)

    async def abort_write(
        self,
        tenant_id: UUID,
        run_id: UUID,
        reference: ArtifactReference,
        *,
        write_id: UUID,
    ) -> bool:
        scope = self._scope(tenant_id, run_id)
        self._write_identity(reference, write_id)
        key = (*scope, reference.id)
        async with self._lock:
            reservation_key = (*scope, write_id)
            reservation = self._write_reservations.get(reservation_key)
            if reservation is None:
                count = self._scope_reservation_counts.get(scope, 0)
                if count >= self._max_write_reservations_per_run:
                    raise ArtifactRepositoryError("artifact repository capacity exceeded")
                self._write_reservations[reservation_key] = (reference, "aborted")
                self._scope_reservation_counts[scope] = count + 1
            else:
                if reservation[0] != reference:
                    return False
                self._write_reservations[reservation_key] = (reference, "aborted")
            artifact = self._artifacts.get(key)
            if (
                artifact is None
                or artifact.content_sha256 != reference.sha256
                or artifact.recompute_content_sha256() != reference.sha256
            ):
                return reservation is None or reservation[1] in {"reserved", "written"}
            owners = self._write_owners[key]
            if owners is None or write_id not in owners:
                return False
            owners.remove(write_id)
            if owners:
                return False
            size = self._sizes.pop(key)
            del self._artifacts[key]
            del self._write_owners[key]
            count = self._scope_counts[scope] - 1
            total = self._scope_bytes[scope] - size
            if count:
                self._scope_counts[scope] = count
                self._scope_bytes[scope] = total
            else:
                del self._scope_counts[scope]
                del self._scope_bytes[scope]
            return True


__all__ = [
    "ArtifactReference",
    "ArtifactRepository",
    "ArtifactRepositoryError",
    "InMemoryArtifactRepository",
]
