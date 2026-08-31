"""Persistent Hermes advice for run submission and bounded outcome learning."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import AdminResourceRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import HermesRunAdvice, HermesRunOutcome


class PersistentHermesRunAdvisor:
    """Use stored Hermes lessons as bounded runtime advice.

    Hermes never stores the raw user request here. Outcome learning records only
    mode, workflow, selected role IDs, and terminal status, so it can improve
    future routing without memorizing secrets from prompts.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def advise(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
    ) -> HermesRunAdvice | None:
        del actor_id, mode, agent_ids
        if not await self._enabled(tenant_id):
            return None
        policy = await self._main_agent_hermes_policy(tenant_id)
        if policy in {"off", "observe"}:
            return None
        lessons = [lesson for lesson in await self._lessons(tenant_id) if _lesson_is_conversation_advice(lesson)]
        if not lessons:
            return None

        lowered = message.lower()
        matched = [
            lesson
            for lesson in lessons
            if _lesson_is_confirmed(lesson) and _lesson_matches(lowered, lesson, workflow_id)
        ]
        if not matched:
            return None
        best = max(matched, key=_lesson_weight)
        weight = _lesson_weight(best)
        recommended_mode = _recommended_mode(best, lowered)
        confidence = min(0.95, 0.55 + weight / 20)
        return HermesRunAdvice(
            recommended_mode=recommended_mode,
            confidence=confidence,
            reasons=(f"Hermes matched stored lesson {best.get('id', 'unknown')}",),
            recommended_skills=(),
            requires_approval=policy in {"suggest", "confirm_before_apply"} or confidence < 0.75,
        )

    async def record_outcome(self, outcome: HermesRunOutcome) -> None:
        if outcome.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return
        if not await self._enabled(outcome.tenant_id):
            return
        mode = "unknown" if outcome.mode is None else outcome.mode.value
        workflow = outcome.workflow_id or "no-workflow"
        conversation_id = outcome.conversation_id or "unknown-conversation"
        status = outcome.status.value
        lesson_id = f"hermes_run_{uuid4().hex}"
        scheduler_notices = _safe_scheduler_notices(outcome.scheduler_notices)
        if scheduler_notices:
            lesson = _scheduler_notice_lesson(
                status=status,
                mode=mode,
                workflow=workflow,
                notices=scheduler_notices,
            )
            tags = _unique_tags(
                [
                    status,
                    mode,
                    workflow,
                    *outcome.agent_ids[:8],
                    *_scheduler_notice_tags(scheduler_notices),
                ]
            )
            weight = min(10, 5 + len(scheduler_notices))
            summary = (
                f"调度观察：Hermes learned from conversation {conversation_id}: "
                f"{lesson} Tags: {', '.join(tags) or 'none'}. Weight: {weight}."
            )
            user_summary = f"本次调度观察提醒：{_runtime_lesson_summary(lesson)}"
        else:
            lesson = f"Run {status} with mode={mode}, workflow={workflow}."
            tags = _unique_tags([status, mode, workflow, *outcome.agent_ids[:8]])
            weight = 4 if outcome.status is RunStatus.COMPLETED else 2
            summary = (
                f"Hermes learned from conversation {conversation_id}: "
                f"{lesson} Tags: {status}, {mode}, {workflow}. "
                f"Weight: {weight}."
            )
            label = "成功经验" if outcome.status is RunStatus.COMPLETED else "失败教训"
            user_summary = f"本次调度学习记录了一个{label}：{_runtime_lesson_summary(lesson)}"
        payload: dict[str, object] = {
            "id": lesson_id,
            "category": "scheduler",
            "outcome": "success" if outcome.status is RunStatus.COMPLETED else "failure",
            "lesson": lesson,
            "summary": summary,
            "user_summary": user_summary,
            "tags": tags,
            "weight": weight,
            "created_at": datetime.now(UTC).isoformat(),
            "run_id": str(outcome.run_id),
            "conversation_id": outcome.conversation_id,
            "confirmed_at": None,
        }
        await self._upsert(outcome.tenant_id, lesson_id, payload)

    async def _enabled(self, tenant_id: UUID) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == tenant_id)
                    .where(AdminResourceRow.kind == "setting")
                    .where(AdminResourceRow.resource_id == "system")
                )
            ).scalar_one_or_none()
        if row is None:
            return True
        return dict(row.payload).get("hermes_enabled", True) is True

    async def _main_agent_hermes_policy(self, tenant_id: UUID) -> str:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == tenant_id)
                    .where(AdminResourceRow.kind == "main_agent")
                    .where(AdminResourceRow.resource_id == "default")
                )
            ).scalar_one_or_none()
        if row is None:
            return "observe"
        policy = dict(row.payload).get("hermes_policy")
        if policy in {"off", "observe", "suggest", "confirm_before_apply"}:
            return str(policy)
        return "observe"

    async def _lessons(self, tenant_id: UUID) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == tenant_id)
                    .where(AdminResourceRow.kind == "hermes")
                    .order_by(AdminResourceRow.created_at.desc())
                    .limit(200)
                )
            ).scalars()
            return [dict(row.payload) for row in rows]

    async def _upsert(self, tenant_id: UUID, resource_id: str, payload: dict[str, object]) -> None:
        statement = (
            insert(AdminResourceRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                kind="hermes",
                resource_id=resource_id,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=[
                    AdminResourceRow.tenant_id,
                    AdminResourceRow.kind,
                    AdminResourceRow.resource_id,
                ],
                set_={"payload": payload},
            )
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)


def _lesson_is_conversation_advice(lesson: dict[str, object]) -> bool:
    category = lesson.get("category")
    return category in {None, "conversation"}


def _lesson_matches(lowered_message: str, lesson: dict[str, object], workflow_id: str | None) -> bool:
    tags = lesson.get("tags")
    if isinstance(tags, list) and any(
        isinstance(tag, str) and tag.lower() in lowered_message for tag in tags
    ):
        return True
    if workflow_id and isinstance(tags, list) and workflow_id in tags:
        return True
    text = lesson.get("lesson")
    if not isinstance(text, str):
        return False
    return any(word and len(word) >= 4 and word in lowered_message for word in text.lower().split())


def _lesson_is_confirmed(lesson: dict[str, object]) -> bool:
    confirmed_at = lesson.get("confirmed_at")
    return isinstance(confirmed_at, str) and bool(confirmed_at.strip())


def _lesson_weight(lesson: dict[str, object]) -> int:
    value = lesson.get("weight", 1)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 1


def _recommended_mode(lesson: dict[str, object], lowered_message: str) -> TaskMode:
    raw_tags = lesson.get("tags")
    tags = [tag.lower() for tag in raw_tags if isinstance(tag, str)] if isinstance(raw_tags, list) else []
    haystack = " ".join([lowered_message, str(lesson.get("lesson", "")).lower(), *tags])
    if any(token in haystack for token in ("hybrid", "混合")):
        return TaskMode.HYBRID
    if any(token in haystack for token in ("discuss", "discussion", "group_chat", "debate", "review", "讨论")):
        return TaskMode.DISCUSS
    if any(token in haystack for token in ("direct", "直接")):
        return TaskMode.DIRECT
    return TaskMode.DISPATCH

def _safe_scheduler_notices(
    notices: tuple[dict[str, object], ...],
) -> tuple[dict[str, str], ...]:
    safe: list[dict[str, str]] = []
    allowed_keys = ("trigger", "action", "severity", "source_kind", "actor")
    for notice in notices[:4]:
        item: dict[str, str] = {}
        for key in allowed_keys:
            value = notice.get(key)
            if isinstance(value, str) and value.strip():
                item[key] = value.strip()[:96]
        if item:
            safe.append(item)
    return tuple(safe)


def _scheduler_notice_lesson(
    *,
    status: str,
    mode: str,
    workflow: str,
    notices: tuple[dict[str, str], ...],
) -> str:
    notice_text = "; ".join(
        ", ".join(f"{key}={value}" for key, value in notice.items()) for notice in notices
    )
    return f"Run {status} with mode={mode}, workflow={workflow}. Scheduler notices: {notice_text}."


def _scheduler_notice_tags(notices: tuple[dict[str, str], ...]) -> list[str]:
    tags: list[str] = []
    for notice in notices:
        for key in ("trigger", "action", "severity", "source_kind", "actor"):
            value = notice.get(key)
            if value:
                tags.append(value)
    return tags


def _unique_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= 16:
            break
    return result


def _compact_sentence(value: str, *, limit: int = 96) -> str:
    cleaned = " ".join(value.strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}..."


def _runtime_lesson_summary(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    prefix = "Run "
    if not cleaned.startswith(prefix):
        return _compact_sentence(cleaned)
    head = cleaned.split(". Scheduler notices:", 1)[0]
    parts = head.removeprefix(prefix).split(" with mode=", 1)
    if len(parts) != 2:
        return _compact_sentence(cleaned)
    status, rest = parts
    mode, _, workflow = rest.partition(", workflow=")
    workflow = workflow.removesuffix(".")
    status_label = {
        "completed": "成功完成",
        "failed": "运行失败",
        "cancelled": "被取消",
    }.get(status, status)
    summary = f"{workflow} 工作流以 {mode} 模式{status_label}。"
    if ". Scheduler notices:" in cleaned:
        summary = f"{summary} 已记录调度告警。"
    return summary
