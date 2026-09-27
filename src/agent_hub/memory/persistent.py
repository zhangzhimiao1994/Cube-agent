from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import AdminResourceRow

_VALID_LAYERS = {"working", "episodic", "core"}
_VALID_CATEGORIES = {
    "preference",
    "fact",
    "task",
    "summary",
    "decision",
    "lesson",
    "other",
}
_SECRET_PATTERN = re.compile(
    r"(?:\bsk-[a-z0-9_-]{6,}\b|\b(?:api[_ -]?key|password|secret|token)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RuntimeMemoryItem:
    id: str
    summary: str
    layer: str
    category: str
    score: float
    reason: str


class RuntimeMemoryRecall(Protocol):
    async def recall(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        query: str,
        project_id: str | None,
        conversation_id: str | None,
        limit: int = 3,
    ) -> tuple[RuntimeMemoryItem, ...]: ...


class PersistentRuntimeMemoryRecall:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def recall(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        query: str,
        project_id: str | None,
        conversation_id: str | None,
        limit: int = 3,
    ) -> tuple[RuntimeMemoryItem, ...]:
        bounded_limit = max(1, min(10, limit))
        actor_text = str(actor_id)
        project_filter = AdminResourceRow.payload["project_id"].astext
        conversation_filter = AdminResourceRow.payload["conversation_id"].astext
        owner_filter = AdminResourceRow.payload["owner_actor_id"].astext
        scope_filter = AdminResourceRow.payload["scope"].astext
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(AdminResourceRow)
                        .where(AdminResourceRow.tenant_id == tenant_id)
                        .where(AdminResourceRow.kind == "memory")
                        .where(
                            or_(
                                and_(scope_filter == "tenant", owner_filter.is_(None)),
                                and_(
                                    scope_filter == f"user:{actor_text}",
                                    or_(owner_filter.is_(None), owner_filter == actor_text),
                                ),
                            )
                        )
                        .where(AdminResourceRow.payload["deleted_at"].astext.is_(None))
                        .where(
                            or_(
                                AdminResourceRow.payload["runtime_enabled"].astext.is_(None),
                                AdminResourceRow.payload["runtime_enabled"].astext != "false",
                            )
                        )
                        .where(
                            or_(
                                project_filter.is_(None),
                                project_filter == project_id,
                            )
                        )
                        .where(
                            or_(
                                conversation_filter.is_(None),
                                conversation_filter == conversation_id,
                            )
                        )
                        .order_by(AdminResourceRow.resource_id.asc())
                    )
                ).scalars()
            )
        selected = select_runtime_memories(
            tuple((row.resource_id, dict(row.payload)) for row in rows),
            actor_id=actor_id,
            query=query,
            project_id=project_id,
            conversation_id=conversation_id,
            limit=bounded_limit,
        )
        return selected


def select_runtime_memories(
    rows: tuple[tuple[str, dict[str, object]], ...],
    *,
    actor_id: UUID,
    query: str,
    project_id: str | None,
    conversation_id: str | None,
    limit: int = 3,
) -> tuple[RuntimeMemoryItem, ...]:
    if limit < 1:
        return ()
    query_terms = _terms(query)
    ranked: list[tuple[float, RuntimeMemoryItem]] = []
    for resource_id, payload in rows:
        candidate = _runtime_candidate(
            resource_id,
            payload,
            actor_id=actor_id,
            query_terms=query_terms,
            project_id=project_id,
            conversation_id=conversation_id,
        )
        if candidate is not None:
            ranked.append(candidate)
    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return tuple(item for _, item in ranked[: min(limit, 10)])


def _runtime_candidate(
    resource_id: str,
    payload: dict[str, object],
    *,
    actor_id: UUID,
    query_terms: set[str],
    project_id: str | None,
    conversation_id: str | None,
) -> tuple[float, RuntimeMemoryItem] | None:
    if payload.get("runtime_enabled") is False or payload.get("deleted_at") is not None:
        return None
    scope = _string(payload.get("scope"))
    owner = _string(payload.get("owner_actor_id"))
    actor_text = str(actor_id)
    if owner is not None and owner != actor_text:
        return None
    if scope == "tenant" and owner is not None:
        return None
    if scope not in {"tenant", f"user:{actor_text}"}:
        return None

    value = _string(payload.get("value"))
    if value is None or _SECRET_PATTERN.search(value):
        return None
    record_project = _string(payload.get("project_id"))
    record_conversation = _string(payload.get("conversation_id"))
    if record_project is not None and record_project != project_id:
        return None
    if record_conversation is not None and record_conversation != conversation_id:
        return None

    layer = _string(payload.get("layer")) or "core"
    if layer not in _VALID_LAYERS:
        return None
    if layer == "working" and (
        record_conversation is None or record_conversation != conversation_id
    ):
        return None
    category = _string(payload.get("category")) or "other"
    if category not in _VALID_CATEGORIES:
        category = "other"

    overlap = len(query_terms & _terms(value))
    exact_project = record_project is not None and record_project == project_id
    exact_conversation = (
        record_conversation is not None and record_conversation == conversation_id
    )
    if layer != "core" and overlap == 0:
        return None
    raw_score = overlap * 20.0
    raw_score += {"working": 30.0, "episodic": 20.0, "core": 40.0}[layer]
    raw_score += 100.0 if exact_conversation else 0.0
    raw_score += 60.0 if exact_project else 0.0
    raw_score += 20.0 if payload.get("locked") is True else 0.0
    raw_score += _bounded_float(payload.get("heat"), 0.5) * 20.0
    raw_score += _bounded_float(payload.get("confidence"), 1.0) * 20.0
    reason = "长期核心记忆"
    if exact_conversation:
        reason = "当前会话记忆"
    elif exact_project:
        reason = "当前项目记忆"
    elif overlap:
        reason = "与当前问题相关"
    return (
        raw_score,
        RuntimeMemoryItem(
            id=resource_id,
            summary=value[:1000],
            layer=layer,
            category=category,
            score=round(min(1.0, raw_score / 300.0), 2),
            reason=reason,
        ),
    )


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", value.casefold()))


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _bounded_float(value: object, default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return default
