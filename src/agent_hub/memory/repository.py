from __future__ import annotations

import asyncio
from uuid import UUID

from agent_hub.memory.types import MemoryAuditEvent, MemoryRecord


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self._records: dict[UUID, MemoryRecord] = {}
        self._audit: list[MemoryAuditEvent] = []
        self._lock = asyncio.Lock()

    async def upsert(self, record: MemoryRecord) -> MemoryRecord:
        async with self._lock:
            self._records[record.id] = record
            return record

    async def get(self, memory_id: UUID) -> MemoryRecord | None:
        async with self._lock:
            return self._records.get(memory_id)

    async def list_for_user(self, tenant_id: UUID, user_id: UUID) -> tuple[MemoryRecord, ...]:
        async with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id and record.user_id == user_id
            )

    async def list_all(self) -> tuple[MemoryRecord, ...]:
        async with self._lock:
            return tuple(self._records.values())

    async def append_audit(self, event: MemoryAuditEvent) -> None:
        async with self._lock:
            self._audit.append(event)

    async def audit_events(self) -> tuple[MemoryAuditEvent, ...]:
        async with self._lock:
            return tuple(self._audit)
