from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.hermes.advisor import PersistentHermesRunAdvisor, _runtime_lesson_summary


@dataclass(slots=True)
class FakeRow:
    payload: dict[str, object]


class FakeResult:
    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> FakeRow | None:
        return self._rows[0] if self._rows else None

    def scalars(self) -> list[FakeRow]:
        return self._rows


class FakeSession:
    def __init__(self, result_sets: list[list[FakeRow]]) -> None:
        self._result_sets = result_sets

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, statement: object) -> FakeResult:
        del statement
        if not self._result_sets:
            raise AssertionError("unexpected query")
        return FakeResult(self._result_sets.pop(0))


class FakeSessionFactory:
    def __init__(self, result_sets: list[list[FakeRow]]) -> None:
        self._result_sets = result_sets

    def __call__(self) -> FakeSession:
        return FakeSession(self._result_sets)


@pytest.mark.asyncio
async def test_runtime_advice_ignores_confirmed_scheduler_observations() -> None:
    scheduler_lesson = {
        "id": "hermes_scheduler_capacity",
        "category": "scheduler",
        "outcome": "failure",
        "lesson": "Run failed with mode=hybrid. Scheduler notices: trigger=model_capacity_pressure.",
        "summary": "调度观察：capacity pressure should not become ordinary conversation advice.",
        "tags": ["planning", "hybrid", "model_capacity_pressure"],
        "weight": 10,
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": str(uuid4()),
        "conversation_id": "conv-scheduler-only",
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    session_factory = FakeSessionFactory(
        [
            [],
            [FakeRow({"hermes_policy": "suggest"})],
            [FakeRow(scheduler_lesson)],
        ]
    )
    advisor = PersistentHermesRunAdvisor(session_factory)  # type: ignore[arg-type]

    advice = await advisor.advise(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="planning task needs a routing suggestion",
        mode=TaskMode.AUTO,
        agent_ids=(),
        workflow_id=None,
    )

    assert advice is None


@pytest.mark.asyncio
async def test_runtime_advice_can_use_confirmed_conversation_lessons() -> None:
    conversation_lesson = {
        "id": "hermes_conversation_review",
        "category": "conversation",
        "outcome": "success",
        "lesson": "Use group chat when debate review is required.",
        "summary": "Learned success pattern: debate review.",
        "tags": ["debate", "review"],
        "weight": 10,
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": None,
        "conversation_id": "conv-review",
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    session_factory = FakeSessionFactory(
        [
            [],
            [FakeRow({"hermes_policy": "suggest"})],
            [FakeRow(conversation_lesson)],
        ]
    )
    advisor = PersistentHermesRunAdvisor(session_factory)  # type: ignore[arg-type]

    advice = await advisor.advise(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="please run a debate review",
        mode=TaskMode.AUTO,
        agent_ids=(),
        workflow_id=None,
    )

    assert advice is not None
    assert advice.recommended_mode is TaskMode.DISCUSS


def test_runtime_lesson_summary_localizes_scheduler_outcomes_for_users() -> None:
    assert (
        _runtime_lesson_summary("Run failed with mode=hybrid, workflow=quality-review.")
        == "quality-review 工作流以 hybrid 模式运行失败。"
    )
    assert (
        _runtime_lesson_summary(
            "Run completed with mode=dispatch, workflow=short-video-dispatch. "
            "Scheduler notices: trigger=model_capacity_pressure."
        )
        == "short-video-dispatch 工作流以 dispatch 模式成功完成。 已记录调度告警。"
    )
