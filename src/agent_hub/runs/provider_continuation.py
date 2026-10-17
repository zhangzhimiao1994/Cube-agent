"""Immutable private native continuation storage, with no publication interface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import ProviderContinuationRow, RunRow
from agent_hub.runtime.provider_continuation import (
    MAX_SNAPSHOT_BYTES,
    ProviderContinuationRepositoryError,
    ProviderContinuationSnapshot,
    decode_snapshot,
    encode_snapshot,
    validate_limits,
    validate_reference,
)


class PostgresProviderContinuationRepository:
    """Serialize quota/ledger writes using the same-tenant parent run lock.

    Commit private bytes before returning; only the caller's existing run-state
    commit may publish a reference. Cancellation propagates, including at commit:
    uncertain/orphan storage never implies model success or permission to retry.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *,
        max_snapshots_per_run: int = 16_384,
        max_total_bytes_per_run: int = 64 * 1024 * 1024,
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        validate_limits(max_snapshots_per_run, max_total_bytes_per_run, max_snapshot_bytes)
        self._session_factory = session_factory
        self._max_snapshots_per_run = max_snapshots_per_run
        self._max_total_bytes_per_run = max_total_bytes_per_run
        self._max_snapshot_bytes = max_snapshot_bytes

    @asynccontextmanager
    async def _transaction(self, tenant_id: UUID, run_id: UUID) -> AsyncIterator[AsyncSession]:
        failed = False
        cancelled = None
        try:
            async with self._session_factory() as session:
                try:
                    async with session.begin():
                        try:
                            run = await session.scalar(select(RunRow.id).where(
                                RunRow.tenant_id == tenant_id, RunRow.id == run_id,
                            ).with_for_update())
                            if run is None:
                                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
                            yield session
                        except asyncio.CancelledError as error:
                            cancelled = error
                            raise
                except asyncio.CancelledError as error:
                    if cancelled is None:
                        cancelled = error
                    raise
        except asyncio.CancelledError as error:
            if cancelled is None:
                cancelled = error
        except SQLAlchemyError:
            failed = True
        if cancelled is not None:
            raise cancelled
        if failed:
            # Public put/get sanitize after contextlib finishes handling yield errors.
            raise ProviderContinuationRepositoryError("provider continuation repository unavailable")

    def _hydrate(self, row: ProviderContinuationRow) -> ProviderContinuationSnapshot:
        snapshot = None
        try:
            if (
                type(row.payload) is str and type(row.byte_size) is int
                and 0 < row.byte_size <= min(self._max_snapshot_bytes, self._max_total_bytes_per_run)
                and len(row.payload.encode("utf-8")) == row.byte_size
            ):
                candidate = decode_snapshot(row.payload)
                if (
                    candidate.id == row.id and candidate.tenant_id == row.tenant_id
                    and candidate.run_id == row.run_id and candidate.ledger_key == row.ledger_key
                    and candidate.sha256 == row.sha256
                ):
                    snapshot = candidate
        except (TypeError, ValueError, OverflowError, RecursionError, ProviderContinuationRepositoryError):
            pass
        if snapshot is None:
            raise ProviderContinuationRepositoryError("provider continuation is unavailable")
        return snapshot

    async def put(self, snapshot: ProviderContinuationSnapshot) -> None:
        failure = None
        cancelled = None
        try:
            await self._put(snapshot)
        except asyncio.CancelledError as error:
            cancelled = error
        except ProviderContinuationRepositoryError as error:
            failure = ProviderContinuationRepositoryError(str(error))
        except SQLAlchemyError:
            failure = ProviderContinuationRepositoryError("provider continuation repository unavailable")
        if cancelled is not None:
            cancelled.__context__ = cancelled.__cause__ = None
            raise cancelled.with_traceback(None)
        if failure is not None:
            raise failure

    async def _put(self, snapshot: ProviderContinuationSnapshot) -> None:
        payload = encode_snapshot(snapshot)
        size = len(payload.encode("utf-8"))
        if size > min(self._max_snapshot_bytes, self._max_total_bytes_per_run):
            raise ProviderContinuationRepositoryError("provider continuation capacity exceeded")
        async with self._transaction(snapshot.tenant_id, snapshot.run_id) as session:
            # Global immutable ID prevents reassignment to another tenant/run.
            existing = await session.get(ProviderContinuationRow, snapshot.id)
            if existing is not None:
                if self._hydrate(existing) != snapshot or existing.payload != payload:
                    raise ProviderContinuationRepositoryError("provider continuation is unavailable")
                return
            ledger = await session.scalar(select(ProviderContinuationRow.id).where(
                ProviderContinuationRow.tenant_id == snapshot.tenant_id,
                ProviderContinuationRow.run_id == snapshot.run_id,
                ProviderContinuationRow.ledger_key == snapshot.ledger_key,
            ))
            if ledger is not None:
                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
            usage = (await session.execute(select(
                func.count(), func.coalesce(func.sum(ProviderContinuationRow.byte_size), 0),
            ).where(
                ProviderContinuationRow.tenant_id == snapshot.tenant_id,
                ProviderContinuationRow.run_id == snapshot.run_id,
            ))).one()
            if (
                usage[0] >= self._max_snapshots_per_run
                or usage[1] + size > self._max_total_bytes_per_run
            ):
                raise ProviderContinuationRepositoryError("provider continuation capacity exceeded")
            session.add(ProviderContinuationRow(
                id=snapshot.id, tenant_id=snapshot.tenant_id, run_id=snapshot.run_id,
                ledger_key=snapshot.ledger_key, sha256=snapshot.sha256, payload=payload, byte_size=size,
            ))

    async def get(
        self, tenant_id: UUID, run_id: UUID, id: UUID, sha256: str,
    ) -> ProviderContinuationSnapshot:
        failure = None
        cancelled = None
        snapshot = None
        try:
            snapshot = await self._get(tenant_id, run_id, id, sha256)
        except asyncio.CancelledError as error:
            cancelled = error
        except ProviderContinuationRepositoryError as error:
            failure = ProviderContinuationRepositoryError(str(error))
        except SQLAlchemyError:
            failure = ProviderContinuationRepositoryError("provider continuation repository unavailable")
        if cancelled is not None:
            cancelled.__context__ = cancelled.__cause__ = None
            raise cancelled.with_traceback(None)
        if failure is not None:
            raise failure
        assert snapshot is not None
        return snapshot

    async def _get(
        self, tenant_id: UUID, run_id: UUID, id: UUID, sha256: str,
    ) -> ProviderContinuationSnapshot:
        validate_reference(tenant_id, run_id, id, sha256)
        async with self._transaction(tenant_id, run_id) as session:
            row = await session.scalar(select(ProviderContinuationRow).where(
                ProviderContinuationRow.tenant_id == tenant_id,
                ProviderContinuationRow.run_id == run_id, ProviderContinuationRow.id == id,
            ))
            if row is None:
                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
            snapshot = self._hydrate(row)
            if (
                snapshot.tenant_id != tenant_id or snapshot.run_id != run_id
                or snapshot.id != id or snapshot.sha256 != sha256
            ):
                raise ProviderContinuationRepositoryError("provider continuation is unavailable")
        return snapshot
