"""Private PostgreSQL artifact hydration; publication remains in RunRepository."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import RunRow, RuntimeArtifactRow, RuntimeArtifactWriteRow
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepositoryError
from agent_hub.runtime.contracts import Artifact


class PostgresArtifactRepository:
    """Run-locked, bounded private storage implementing ArtifactRepository.

    Written reservations own a value; aborted reservations fence late writes.
    No operation publishes an event or grants authority over a worker lease.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_artifacts_per_run: int = 16_384,
        max_total_bytes_per_run: int = 64 * 1024 * 1024,
        max_artifact_bytes: int = 1024 * 1024,
        max_batch_size: int = 16_384,
        max_write_reservations_per_run: int = 65_536,
    ) -> None:
        limits = (
            max_artifacts_per_run, max_total_bytes_per_run, max_artifact_bytes,
            max_batch_size, max_write_reservations_per_run,
        )
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("artifact repository limits must be positive integers")
        self._session_factory = session_factory
        self._max_artifacts_per_run = max_artifacts_per_run
        self._max_total_bytes_per_run = max_total_bytes_per_run
        self._max_artifact_bytes = max_artifact_bytes
        self._max_batch_size = max_batch_size
        self._max_write_reservations_per_run = max_write_reservations_per_run

    @staticmethod
    def _scope(tenant_id: UUID, run_id: UUID) -> tuple[UUID, UUID]:
        try:
            tenant, run = UUID(str(tenant_id)), UUID(str(run_id))
            if str(tenant) != str(tenant_id) or str(run) != str(run_id):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ArtifactRepositoryError("artifact scope is invalid") from None
        return tenant, run

    @staticmethod
    def _write_identity(reference: ArtifactReference, write_id: UUID) -> None:
        if type(reference) is not ArtifactReference or type(write_id) is not UUID:
            raise ArtifactRepositoryError("artifact write identity is invalid")

    @staticmethod
    def _encode_payload(payload: dict[str, object]) -> str:
        return json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _reject_constant(value: str) -> object:
        raise ValueError("nonfinite JSON number")

    def _validated_payload(self, artifact: Artifact) -> tuple[str, int]:
        if type(artifact) is not Artifact:
            raise ArtifactRepositoryError("artifact is invalid")
        try:
            payload = artifact.to_payload()
            validated = Artifact.from_payload(payload)
            if validated.id != artifact.id or validated.content_sha256 != artifact.content_sha256:
                raise ValueError
            encoded = self._encode_payload(payload)
            size = len(encoded.encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            raise ArtifactRepositoryError("artifact is invalid") from None
        if size > self._max_artifact_bytes:
            raise ArtifactRepositoryError("artifact repository capacity exceeded")
        return encoded, size

    @classmethod
    def _hydrate(cls, row: RuntimeArtifactRow) -> Artifact:
        try:
            if type(row.payload) is not str or len(row.payload.encode("utf-8")) != row.byte_size:
                raise ValueError
            payload = json.loads(
                row.payload, object_pairs_hook=cls._unique_object, parse_constant=cls._reject_constant,
            )
            artifact = Artifact.from_payload(payload)
            if (
                artifact.id != row.id
                or artifact.content_sha256 != row.content_sha256
                or cls._encode_payload(artifact.to_payload()) != row.payload
            ):
                raise ValueError
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ArtifactRepositoryError("artifact is unavailable") from None
        return artifact

    @asynccontextmanager
    async def _transaction(self, tenant_id: UUID, run_id: UUID) -> AsyncIterator[AsyncSession]:
        failed = False
        try:
            async with self._session_factory() as session, session.begin():
                run = await session.scalar(select(RunRow.id).where(
                    RunRow.tenant_id == tenant_id, RunRow.id == run_id,
                ).with_for_update())
                if run is None:
                    raise ArtifactRepositoryError("artifact is unavailable")
                yield session
        except SQLAlchemyError:
            failed = True
        # Raise outside the handler so raw SQL/parameters are not retained as context.
        if failed:
            raise ArtifactRepositoryError("artifact repository unavailable")

    async def _new_reservation(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID,
        reference: ArtifactReference, write_id: UUID, *, status: str,
    ) -> RuntimeArtifactWriteRow:
        count = await session.scalar(select(func.count()).select_from(RuntimeArtifactWriteRow).where(
            RuntimeArtifactWriteRow.tenant_id == tenant_id, RuntimeArtifactWriteRow.run_id == run_id,
        ))
        if count is not None and count >= self._max_write_reservations_per_run:
            raise ArtifactRepositoryError("artifact repository capacity exceeded")
        row = RuntimeArtifactWriteRow(
            tenant_id=tenant_id, run_id=run_id, write_id=write_id, artifact_id=reference.id,
            content_sha256=reference.sha256, status=status,
        )
        session.add(row)
        return row

    async def _reserve(
        self, session: AsyncSession, tenant_id: UUID, run_id: UUID,
        reference: ArtifactReference, write_id: UUID,
    ) -> RuntimeArtifactWriteRow:
        row = await session.get(RuntimeArtifactWriteRow, (tenant_id, run_id, write_id))
        if row is None:
            return await self._new_reservation(
                session, tenant_id, run_id, reference, write_id, status="reserved",
            )
        if (
            row.artifact_id != reference.id or row.content_sha256 != reference.sha256
            or row.status not in {"reserved", "written"}
        ):
            raise ArtifactRepositoryError("artifact is unavailable")
        return row

    async def reserve_write(
        self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID,
    ) -> None:
        tenant_id, run_id = self._scope(tenant_id, run_id)
        self._write_identity(reference, write_id)
        async with self._transaction(tenant_id, run_id) as session:
            await self._reserve(session, tenant_id, run_id, reference, write_id)

    async def put(
        self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *, write_id: UUID | None = None,
    ) -> None:
        tenant_id, run_id = self._scope(tenant_id, run_id)
        if write_id is not None and type(write_id) is not UUID:
            raise ArtifactRepositoryError("artifact write identity is invalid")
        payload, size = self._validated_payload(artifact)
        reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
        async with self._transaction(tenant_id, run_id) as session:
            reservation = None if write_id is None else await self._reserve(
                session, tenant_id, run_id, reference, write_id,
            )
            existing = await session.get(RuntimeArtifactRow, (tenant_id, run_id, artifact.id))
            if existing is not None:
                if self._hydrate(existing).content_sha256 != reference.sha256:
                    raise ArtifactRepositoryError("artifact is unavailable")
                if write_id is None:
                    existing.permanent = True
            else:
                usage = (await session.execute(select(
                    func.count(), func.coalesce(func.sum(RuntimeArtifactRow.byte_size), 0),
                ).where(
                    RuntimeArtifactRow.tenant_id == tenant_id, RuntimeArtifactRow.run_id == run_id,
                ))).one()
                if usage[0] >= self._max_artifacts_per_run or usage[1] + size > self._max_total_bytes_per_run:
                    raise ArtifactRepositoryError("artifact repository capacity exceeded")
                session.add(RuntimeArtifactRow(
                    tenant_id=tenant_id, run_id=run_id, id=artifact.id,
                    content_sha256=artifact.content_sha256, payload=payload, byte_size=size,
                    permanent=write_id is None,
                ))
            if reservation is not None:
                reservation.status = "written"

    async def get_many(
        self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...],
    ) -> tuple[Artifact, ...]:
        tenant_id, run_id = self._scope(tenant_id, run_id)
        if (
            type(references) is not tuple or len(references) > self._max_batch_size
            or not all(type(reference) is ArtifactReference for reference in references)
            or len({reference.id for reference in references}) != len(references)
        ):
            raise ArtifactRepositoryError("artifact references are invalid")
        async with self._transaction(tenant_id, run_id) as session:
            if not references:
                return ()
            rows = (await session.scalars(select(RuntimeArtifactRow).where(
                RuntimeArtifactRow.tenant_id == tenant_id, RuntimeArtifactRow.run_id == run_id,
                RuntimeArtifactRow.id.in_(reference.id for reference in references),
            ))).all()
            by_id = {row.id: row for row in rows}
            resolved: list[Artifact] = []
            total = 0
            for reference in references:
                row = by_id.get(reference.id)
                if row is None or row.content_sha256 != reference.sha256:
                    raise ArtifactRepositoryError("artifact is unavailable")
                resolved.append(self._hydrate(row))
                total += row.byte_size
                if total > self._max_total_bytes_per_run:
                    raise ArtifactRepositoryError("artifact repository capacity exceeded")
            return tuple(resolved)

    async def abort_write(
        self, tenant_id: UUID, run_id: UUID, reference: ArtifactReference, *, write_id: UUID,
    ) -> bool:
        tenant_id, run_id = self._scope(tenant_id, run_id)
        self._write_identity(reference, write_id)
        async with self._transaction(tenant_id, run_id) as session:
            reservation = await session.get(RuntimeArtifactWriteRow, (tenant_id, run_id, write_id))
            previous_status = None if reservation is None else reservation.status
            if reservation is None:
                reservation = await self._new_reservation(
                    session, tenant_id, run_id, reference, write_id, status="aborted",
                )
            else:
                if reservation.artifact_id != reference.id or reservation.content_sha256 != reference.sha256:
                    return False
                reservation.status = "aborted"
            row = await session.get(RuntimeArtifactRow, (tenant_id, run_id, reference.id))
            if row is None or row.content_sha256 != reference.sha256:
                return previous_status in {None, "reserved", "written"}
            self._hydrate(row)
            if row.permanent or previous_status != "written":
                return False
            owners = await session.scalar(select(func.count()).select_from(RuntimeArtifactWriteRow).where(
                RuntimeArtifactWriteRow.tenant_id == tenant_id, RuntimeArtifactWriteRow.run_id == run_id,
                RuntimeArtifactWriteRow.artifact_id == reference.id,
                RuntimeArtifactWriteRow.content_sha256 == reference.sha256,
                RuntimeArtifactWriteRow.status == "written", RuntimeArtifactWriteRow.write_id != write_id,
            ))
            if owners:
                return False
            await session.delete(row)
            return True
