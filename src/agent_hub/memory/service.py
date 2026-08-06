from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from agent_hub.memory.repository import InMemoryMemoryRepository
from agent_hub.memory.types import (
    MemoryAddResult,
    MemoryAddStatus,
    MemoryAuditEvent,
    MemoryCategory,
    MemoryLayer,
    MemoryRecord,
)

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\b(api[_ -]?key|password|secret|token)\b", re.IGNORECASE),
)
_PROMPT_LIKE = re.compile(
    r"\b(ignore previous|system prompt|developer message|you must|do not reveal|jailbreak)\b",
    re.IGNORECASE,
)


class MemoryNotFound(LookupError):
    pass


class MemoryForbidden(PermissionError):
    pass


class MemoryService:
    def __init__(
        self,
        repository: InMemoryMemoryRepository | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository or InMemoryMemoryRepository()
        self._now = now or (lambda: datetime.now(UTC))

    async def add_candidate(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        text: str,
        layer: MemoryLayer = MemoryLayer.EPISODIC,
        category: MemoryCategory = MemoryCategory.OTHER,
        confidence: float = 0.5,
        source_run_id: UUID | None = None,
        source_event_id: UUID | None = None,
        expires_at: datetime | None = None,
        stable_fact: bool = False,
        user_confirmed: bool = False,
        from_external_content: bool = False,
    ) -> MemoryAddResult:
        normalized = _normalize_text(text)
        if _looks_sensitive(normalized):
            await self._audit("memory.rejected", tenant_id, user_id, None, "rejected_sensitive")
            return MemoryAddResult(
                status=MemoryAddStatus.REJECTED_SENSITIVE,
                reason="memory candidate contains sensitive material",
            )
        if from_external_content and _looks_prompt_like(normalized):
            await self._audit("memory.rejected", tenant_id, user_id, None, "rejected_prompt_like")
            return MemoryAddResult(
                status=MemoryAddStatus.REJECTED_PROMPT_LIKE,
                reason="external prompt-like content cannot be promoted",
            )
        if layer is MemoryLayer.CORE and not (stable_fact or user_confirmed):
            await self._audit("memory.rejected", tenant_id, user_id, None, "rejected_unconfirmed_core")
            return MemoryAddResult(
                status=MemoryAddStatus.REJECTED_UNCONFIRMED_CORE,
                reason="core memory requires a stable fact or explicit confirmation",
            )
        existing = await self._find_duplicate(tenant_id, user_id, normalized)
        if existing is not None:
            await self._audit("memory.deduplicated", tenant_id, user_id, existing.id, "deduplicated")
            return MemoryAddResult(status=MemoryAddStatus.DEDUPLICATED, record=existing)
        now = self._now()
        record = MemoryRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            layer=layer,
            category=category,
            text=normalized,
            confidence=confidence,
            source_run_id=source_run_id,
            source_event_id=source_event_id,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        await self._repository.upsert(record)
        await self._audit("memory.stored", tenant_id, user_id, record.id, "stored")
        return MemoryAddResult(status=MemoryAddStatus.STORED, record=record)

    async def search(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        query: str,
        limit: int = 10,
    ) -> tuple[MemoryRecord, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("memory search limit must be between 1 and 100")
        query_terms = _terms(query)
        records = await self._active_records(tenant_id, user_id)
        scored: list[tuple[int, MemoryRecord]] = []
        for record in records:
            score = len(query_terms & _terms(record.text))
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], -item[1].confidence, item[1].created_at))
        return tuple(record for _, record in scored[:limit])

    async def inspect(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        include_deleted: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        if include_deleted:
            return await self._repository.list_for_user(tenant_id, user_id)
        return await self._active_records(tenant_id, user_id)

    async def edit(
        self,
        memory_id: UUID,
        *,
        tenant_id: UUID,
        user_id: UUID,
        text: str,
        category: MemoryCategory | None = None,
    ) -> MemoryRecord:
        record = await self._owned_record(memory_id, tenant_id, user_id)
        normalized = _normalize_text(text)
        if _looks_sensitive(normalized):
            raise ValueError("memory text contains sensitive material")
        updated = _validated_update(
            record,
            {
                "text": normalized,
                "category": category or record.category,
                "updated_at": self._now(),
            },
        )
        await self._repository.upsert(updated)
        await self._audit("memory.edited", tenant_id, user_id, memory_id, "edited")
        return updated

    async def forget(
        self,
        memory_id: UUID,
        *,
        tenant_id: UUID,
        user_id: UUID,
        reason: str = "user_request",
    ) -> MemoryRecord:
        record = await self._owned_record(memory_id, tenant_id, user_id)
        now = self._now()
        deleted = _validated_update(
            record,
            {
                "deleted_at": now,
                "updated_at": now,
                "tombstone_reason": reason,
            },
        )
        await self._repository.upsert(deleted)
        await self._audit("memory.forgotten", tenant_id, user_id, memory_id, "forgotten", reason=reason)
        return deleted

    async def expire_due(self) -> int:
        expired = 0
        now = self._now()
        for record in await self._repository.list_all():
            if record.deleted_at is None and record.expires_at is not None and record.expires_at <= now:
                tombstoned = _validated_update(
                    record,
                    {
                        "deleted_at": now,
                        "updated_at": now,
                        "tombstone_reason": "retention_expired",
                    },
                )
                await self._repository.upsert(tombstoned)
                await self._audit(
                    "memory.expired",
                    record.tenant_id,
                    record.user_id,
                    record.id,
                    "expired",
                    reason="retention_expired",
                )
                expired += 1
        return expired

    async def audit_events(
        self,
        *,
        tenant_id: UUID | None = None,
        user_id: UUID | None = None,
        include_all: bool = False,
    ) -> tuple[MemoryAuditEvent, ...]:
        if tenant_id is None and not include_all:
            raise ValueError("tenant_id is required for memory audit reads")
        events = await self._repository.audit_events()
        if tenant_id is not None:
            events = tuple(event for event in events if event.tenant_id == tenant_id)
        if user_id is not None:
            events = tuple(event for event in events if event.user_id == user_id)
        return events

    async def _active_records(self, tenant_id: UUID, user_id: UUID) -> tuple[MemoryRecord, ...]:
        now = self._now()
        records = await self._repository.list_for_user(tenant_id, user_id)
        return tuple(
            record
            for record in records
            if record.deleted_at is None and (record.expires_at is None or record.expires_at > now)
        )

    async def _owned_record(self, memory_id: UUID, tenant_id: UUID, user_id: UUID) -> MemoryRecord:
        record = await self._repository.get(memory_id)
        if record is None:
            raise MemoryNotFound("memory not found")
        if record.tenant_id != tenant_id or record.user_id != user_id:
            raise MemoryForbidden("memory is not visible to caller")
        return record

    async def _find_duplicate(
        self,
        tenant_id: UUID,
        user_id: UUID,
        normalized_text: str,
    ) -> MemoryRecord | None:
        for record in await self._active_records(tenant_id, user_id):
            if record.text.casefold() == normalized_text.casefold():
                return record
        return None

    async def _audit(
        self,
        kind: str,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID | None,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self._repository.append_audit(
            MemoryAuditEvent(
                kind=kind,
                tenant_id=tenant_id,
                user_id=user_id,
                memory_id=memory_id,
                status=status,
                reason=reason,
                created_at=self._now(),
            )
        )


def _normalize_text(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        raise ValueError("memory text must be non-empty")
    return normalized


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.casefold()))


def _looks_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _looks_prompt_like(text: str) -> bool:
    return _PROMPT_LIKE.search(text) is not None


def _validated_update(record: MemoryRecord, updates: dict[str, object]) -> MemoryRecord:
    data = record.model_dump(mode="python")
    data.update(updates)
    return MemoryRecord.model_validate(data, strict=True)
