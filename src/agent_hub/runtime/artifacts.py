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
    async def put(self, tenant_id: UUID, run_id: UUID, artifact: Artifact) -> None: ...

    async def get_many(
        self,
        tenant_id: UUID,
        run_id: UUID,
        references: tuple[ArtifactReference, ...],
    ) -> tuple[Artifact, ...]: ...


class InMemoryArtifactRepository:
    """Single-process repository; inject one shared instance for cross-runtime recovery."""

    def __init__(
        self,
        *,
        max_artifacts_per_run: int = _DEFAULT_MAX_ARTIFACTS_PER_RUN,
        max_total_bytes_per_run: int = _DEFAULT_MAX_TOTAL_BYTES_PER_RUN,
        max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
    ) -> None:
        limits = (
            max_artifacts_per_run,
            max_total_bytes_per_run,
            max_artifact_bytes,
            max_batch_size,
        )
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("artifact repository limits must be positive integers")
        self._max_artifacts_per_run = max_artifacts_per_run
        self._max_total_bytes_per_run = max_total_bytes_per_run
        self._max_artifact_bytes = max_artifact_bytes
        self._max_batch_size = max_batch_size
        self._artifacts: dict[tuple[UUID, UUID, UUID], Artifact] = {}
        self._sizes: dict[tuple[UUID, UUID, UUID], int] = {}
        self._scope_counts: dict[tuple[UUID, UUID], int] = {}
        self._scope_bytes: dict[tuple[UUID, UUID], int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _scope(tenant_id: UUID, run_id: UUID) -> tuple[UUID, UUID]:
        if type(tenant_id) is not UUID or type(run_id) is not UUID:
            raise ArtifactRepositoryError("artifact scope is invalid")
        return tenant_id, run_id

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

    async def put(self, tenant_id: UUID, run_id: UUID, artifact: Artifact) -> None:
        scope = self._scope(tenant_id, run_id)
        size = self._encoded_size(artifact)
        key = (*scope, artifact.id)
        async with self._lock:
            existing = self._artifacts.get(key)
            if existing is not None:
                if existing.content_sha256 != artifact.content_sha256:
                    raise ArtifactRepositoryError("artifact is unavailable")
                return
            count = self._scope_counts.get(scope, 0)
            total = self._scope_bytes.get(scope, 0)
            if (
                count >= self._max_artifacts_per_run
                or total + size > self._max_total_bytes_per_run
            ):
                raise ArtifactRepositoryError("artifact repository capacity exceeded")
            self._artifacts[key] = artifact
            self._sizes[key] = size
            self._scope_counts[scope] = count + 1
            self._scope_bytes[scope] = total + size

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


__all__ = [
    "ArtifactReference",
    "ArtifactRepository",
    "ArtifactRepositoryError",
    "InMemoryArtifactRepository",
]
