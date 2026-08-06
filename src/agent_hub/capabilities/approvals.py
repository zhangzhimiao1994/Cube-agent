from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from agent_hub.capabilities.policy import normalize_resource
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus


class ApprovalMismatch(RuntimeError):
    pass


class ApprovalExpired(RuntimeError):
    pass


class ApprovalRejected(RuntimeError):
    pass


class RunStatusRepository(Protocol):
    async def begin_capability_approval(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
    ) -> object: ...

    async def resolve_capability_approval(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        approval_id: str,
        approval_fingerprint: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    request_fingerprint: str
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    expires_at: datetime
    status: str = "pending"


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    async def put(self, record: ApprovalRecord) -> None:
        self._records[record.id] = record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    async def get_pending_by_fingerprint(self, fingerprint: str) -> ApprovalRecord | None:
        for record in self._records.values():
            if record.status == "pending" and record.request_fingerprint == fingerprint:
                return record
        return None

    async def replace(self, record: ApprovalRecord) -> None:
        self._records[record.id] = record

    async def list_pending(self) -> tuple[ApprovalRecord, ...]:
        return tuple(record for record in self._records.values() if record.status == "pending")


class ApprovalService:
    def __init__(
        self,
        store: InMemoryApprovalStore,
        *,
        expires_in: timedelta = timedelta(minutes=10),
        now: object | None = None,
    ) -> None:
        self._store = store
        self._expires_in = expires_in
        self._now = now
        self._run_repository: RunStatusRepository | None = None
        self._lock = asyncio.Lock()

    def bind_run_repository(self, repository: RunStatusRepository) -> None:
        self._run_repository = repository

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return await self._store.get(approval_id)

    async def create(self, request: CapabilityRequest) -> ApprovalRecord:
        async with self._lock:
            record = await self._create_unlocked(request)
            return record

    async def start_waiting(
        self,
        request: CapabilityRequest,
        begin_wait: Callable[[ApprovalRecord], Awaitable[None]],
    ) -> ApprovalRecord:
        async with self._lock:
            record = await self._create_unlocked(request)
            await begin_wait(record)
            return record

    async def approve(
        self,
        approval_id: str,
        request: CapabilityRequest,
        *,
        actor_id: UUID,
    ) -> ApprovalRecord:
        async with self._lock:
            record = await self._matching_record(approval_id, request, actor_id=actor_id)
            approved = ApprovalRecord(
                id=record.id,
                request_fingerprint=record.request_fingerprint,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                run_id=record.run_id,
                expires_at=record.expires_at,
                status="approved",
            )
            if self._run_repository is not None:
                await self._run_repository.resolve_capability_approval(
                    record.tenant_id,
                    record.run_id,
                    RunStatus.RUNNING,
                    approval_id=record.id,
                    approval_fingerprint=record.request_fingerprint,
                )
            await self._store.replace(approved)
            return approved

    async def reject(self, approval_id: str, *, actor_id: UUID) -> ApprovalRecord:
        async with self._lock:
            record = await self._record(approval_id)
            if record.status != "pending":
                raise ApprovalMismatch("approval does not match request")
            if actor_id != record.user_id:
                raise ApprovalMismatch("approval does not match request")
            if self._clock() >= record.expires_at:
                if self._run_repository is not None:
                    await self._run_repository.resolve_capability_approval(
                        record.tenant_id,
                        record.run_id,
                        RunStatus.CANCELLED,
                        approval_id=record.id,
                        approval_fingerprint=record.request_fingerprint,
                    )
                await self._store.replace(
                    ApprovalRecord(
                        id=record.id,
                        request_fingerprint=record.request_fingerprint,
                        tenant_id=record.tenant_id,
                        user_id=record.user_id,
                        run_id=record.run_id,
                        expires_at=record.expires_at,
                        status="expired",
                    )
                )
                raise ApprovalExpired("approval expired")
            rejected = ApprovalRecord(
                id=record.id,
                request_fingerprint=record.request_fingerprint,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                run_id=record.run_id,
                expires_at=record.expires_at,
                status="rejected",
            )
            if self._run_repository is not None:
                await self._run_repository.resolve_capability_approval(
                    record.tenant_id,
                    record.run_id,
                    RunStatus.CANCELLED,
                    approval_id=record.id,
                    approval_fingerprint=record.request_fingerprint,
                )
            await self._store.replace(rejected)
            raise ApprovalRejected("approval rejected")

    async def expire_due(self) -> int:
        async with self._lock:
            expired = 0
            for record in await self._store.list_pending():
                if self._clock() < record.expires_at:
                    continue
                expired_record = ApprovalRecord(
                    id=record.id,
                    request_fingerprint=record.request_fingerprint,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    run_id=record.run_id,
                    expires_at=record.expires_at,
                    status="expired",
                )
                if self._run_repository is not None:
                    await self._run_repository.resolve_capability_approval(
                        record.tenant_id,
                        record.run_id,
                        RunStatus.CANCELLED,
                        approval_id=record.id,
                        approval_fingerprint=record.request_fingerprint,
                    )
                await self._store.replace(expired_record)
                expired += 1
            return expired

    async def _matching_record(
        self,
        approval_id: str,
        request: CapabilityRequest,
        *,
        actor_id: UUID,
    ) -> ApprovalRecord:
        record = await self._record(approval_id)
        if record.status != "pending":
            raise ApprovalMismatch("approval does not match request")
        if actor_id != record.user_id or _fingerprint_request(request) != record.request_fingerprint:
            raise ApprovalMismatch("approval does not match request")
        if self._clock() >= record.expires_at:
            if self._run_repository is not None:
                await self._run_repository.resolve_capability_approval(
                    record.tenant_id,
                    record.run_id,
                    RunStatus.CANCELLED,
                    approval_id=record.id,
                    approval_fingerprint=record.request_fingerprint,
                )
            await self._store.replace(
                ApprovalRecord(
                    id=record.id,
                    request_fingerprint=record.request_fingerprint,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    run_id=record.run_id,
                    expires_at=record.expires_at,
                    status="expired",
                )
            )
            raise ApprovalExpired("approval expired")
        return record

    async def _create_unlocked(self, request: CapabilityRequest) -> ApprovalRecord:
        fingerprint = _fingerprint_request(request)
        existing = await self._store.get_pending_by_fingerprint(fingerprint)
        if existing is not None:
            return existing
        record = ApprovalRecord(
            id=f"approval_{uuid4().hex}",
            request_fingerprint=fingerprint,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            run_id=request.run_id,
            expires_at=self._clock() + self._expires_in,
        )
        await self._store.put(record)
        return record

    async def _record(self, approval_id: str) -> ApprovalRecord:
        record = await self._store.get(approval_id)
        if record is None:
            raise ApprovalMismatch("approval does not match request")
        return record

    def _clock(self) -> datetime:
        if self._now is None:
            return datetime.now(UTC)
        return self._now()  # type: ignore[operator,no-any-return]


def _fingerprint_request(request: CapabilityRequest) -> str:
    normalized_resource = normalize_resource(request.resource)
    payload = {
        "tenant_id": str(request.tenant_id),
        "user_id": str(request.user_id),
        "run_id": str(request.run_id),
        "agent_id": request.agent_id,
        "capability": request.capability,
        "operation": request.operation,
        "resource": normalized_resource,
        "arguments": request.arguments,
        "idempotency_key": request.idempotency_key,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
