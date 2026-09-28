from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

from autogen_core.models import SystemMessage, UserMessage

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen import adapter
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint, TaskContext


class _UnusedGateway:
    async def complete_with_context(self, request: Any) -> Any:
        del request
        raise AssertionError("checkpoint tests must not invoke the model gateway")


def _plan(
    *,
    soft_turns: int = 2,
    wall_time_seconds: float = 20.0,
    token_budget: int = 100,
) -> DiscussionPlan:
    return DiscussionPlan(
        participants=(
            DiscussionParticipant(
                id="analyst",
                role="Analyst",
                goal="Analyze",
                logical_model="shared",
            ),
            DiscussionParticipant(
                id="critic",
                role="Critic",
                goal="Review",
                logical_model="shared",
            ),
        ),
        selector_model="shared",
        max_turns=soft_turns,
        wall_time_seconds=wall_time_seconds,
        token_budget=token_budget,
        cost_budget_usd=Decimal(1),
    )


def _context(*, token_budget: int = 100, timeout_seconds: float = 60.0) -> TaskContext:
    return TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS,
        request="Compare evidence",
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
    )


def _control(
    *,
    soft_turns: int = 2,
    wall_time_seconds: float = 20.0,
) -> Any:
    control_type = getattr(adapter, "_DiscussionControl", None)
    assert control_type is not None, "dynamic discussion control is not implemented"
    return control_type.create(
        _plan(soft_turns=soft_turns, wall_time_seconds=wall_time_seconds),
        _context(),
        DiscussionUsage(),
    )


def test_soft_limit_extends_only_after_new_progress() -> None:
    control = _control()

    assert control.observe("analyst", "Initial position") is None
    assert control.observe("critic", "[EVIDENCE] independent review") is None
    assert control.soft_limit == 4
    assert control.turns == 2

    assert control.observe("critic", "[EVIDENCE] independent review") is None
    assert control.observe("critic", "[EVIDENCE] independent review") == "no_progress"


def test_repeated_error_stops_before_soft_limit() -> None:
    control = _control(soft_turns=4)

    assert control.observe("analyst", "ERROR: dependency timeout") is None
    assert control.observe("analyst", "ERROR: dependency timeout") == "repeated_error"
    assert control.turns == 2


def test_hard_limit_uses_token_and_time_capacity_without_fixed_turn_cap() -> None:
    limit = getattr(adapter, "_discussion_hard_turn_limit", None)
    assert limit is not None, "dynamic hard limit is not implemented"

    token_limited = limit(_plan(wall_time_seconds=100.0), _context(token_budget=20), 0)
    time_limited = limit(
        _plan(wall_time_seconds=100.0),
        _context(token_budget=1_000, timeout_seconds=5.0),
        0,
    )
    large_project_limit = limit(
        _plan(wall_time_seconds=1_000.0, token_budget=1_000),
        _context(token_budget=1_000, timeout_seconds=1_000.0),
        0,
    )

    assert token_limited == 10
    assert time_limited == 5
    assert large_project_limit == 500


def test_soft_limit_can_expand_past_legacy_sixty_four_turn_boundary() -> None:
    control_type = getattr(adapter, "_DiscussionControl", None)
    assert control_type is not None
    control = control_type.create(
        _plan(soft_turns=2, wall_time_seconds=1_000.0, token_budget=1_000),
        _context(token_budget=1_000, timeout_seconds=1_000.0),
        DiscussionUsage(),
    )

    for index in range(70):
        assert control.observe("analyst", f"[EVIDENCE] new evidence {index}") is None

    assert control.turns == 70
    assert control.soft_limit > 64
    assert control.hard_limit == 500


def test_long_history_is_compacted_with_task_and_recent_messages_preserved() -> None:
    compact = getattr(adapter, "_compact_llm_messages", None)
    assert compact is not None, "discussion history compaction is not implemented"
    messages = tuple(
        UserMessage(content=f"message-{index}", source="user") for index in range(129)
    )

    compacted = compact(messages)

    assert len(compacted) < 128
    assert compacted[0].content == "message-0"
    assert compacted[-1].content == "message-128"
    assert any(
        "COMPRESSED_DISCUSSION_HISTORY_JSON=" in str(message.content)
        for message in compacted
    )


def test_long_history_preserves_system_message_and_original_user_task() -> None:
    compact = getattr(adapter, "_compact_llm_messages", None)
    assert compact is not None, "discussion history compaction is not implemented"
    messages = (
        SystemMessage(content="system policy"),
        UserMessage(content="original task", source="user"),
        *(
            UserMessage(content=f"turn-{index}", source="user")
            for index in range(127)
        ),
    )

    compacted = compact(messages)

    assert compacted[0].content == "system policy"
    assert compacted[1].content == "original task"
    assert compacted[-1].content == "turn-126"


def test_checkpoint_state_restores_dynamic_progress_without_reset() -> None:
    control = _control(soft_turns=1)
    control_type = type(control)
    assert control.observe("analyst", "Initial position") is None
    assert control.turns == 1
    assert control.soft_limit == 3

    restored = control_type.from_state(
        control.to_state(),
        _plan(soft_turns=1),
        _context(),
        DiscussionUsage(),
    )

    assert restored.turns == 1
    assert restored.soft_limit == 3
    assert restored.observe("analyst", "Initial position") is None
    assert restored.observe("analyst", "Initial position") == "no_progress"


def test_legacy_checkpoint_transcript_rebuilds_dynamic_progress_window() -> None:
    control_type = getattr(adapter, "_DiscussionControl", None)
    assert control_type is not None
    artifacts = tuple(
        Artifact(
            id=uuid4(),
            type="text",
            producer="analyst" if index % 2 == 0 else "critic",
            content={"text": f"legacy conclusion {index}"},
        )
        for index in range(6)
    )

    restored = control_type.from_legacy_transcript(
        _plan(soft_turns=2),
        _context(),
        DiscussionUsage(tokens=6),
        artifacts,
    )

    assert restored.turns == 6
    assert restored.soft_limit >= restored.turns
    assert restored.repeated_error_count == 0


async def test_v2_checkpoint_persists_remaining_wall_time() -> None:
    plan = _plan(wall_time_seconds=20.0)
    context = _context(timeout_seconds=30.0)
    runtime = AutoGenDiscussionRuntime(_UnusedGateway(), plan)
    runtime._discussion_control = adapter._DiscussionControl.create(
        plan,
        context,
        DiscussionUsage(),
    )
    runtime._wall_deadline = asyncio.get_running_loop().time() + 5.0

    checkpoint = runtime._checkpoint(
        context,
        next_sequence=2,
        terminal=False,
        reason=None,
        artifacts=(),
        usage=DiscussionUsage(),
    )

    remaining = checkpoint.state["remaining_timeout_seconds"]
    assert isinstance(remaining, float)
    assert 4.0 < remaining <= 5.0


def test_v2_checkpoint_restore_uses_saved_remaining_wall_time() -> None:
    plan = _plan(wall_time_seconds=20.0)
    context = _context(timeout_seconds=30.0)
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        runtime_type="autogen",
        runtime_version="2",
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=context.mode,
        state={
            "plan_digest": plan.digest,
            "autogen_version": "0.7.5",
            "terminal": False,
            "reason": None,
            "next_sequence": 2,
            "artifact_registry": {},
            "usage": {"tokens": 0, "cost_usd": "0"},
            "discussion_control": adapter._DiscussionControl.create(
                plan,
                context,
                DiscussionUsage(),
            ).to_state(),
            "model_ledger": (),
            "tool_ledger": (),
            "remaining_timeout_seconds": 5.0,
        },
    )
    runtime = AutoGenDiscussionRuntime(_UnusedGateway(), plan)
    runtime._validate_checkpoint(checkpoint, context)
    wall_time = getattr(adapter, "_checkpoint_wall_time_seconds", None)
    assert wall_time is not None, "checkpoint wall-time restoration is not implemented"

    assert wall_time(plan, context, checkpoint) == 5.0


def test_v1_checkpoint_without_remaining_wall_time_uses_current_context_budget() -> None:
    plan = _plan(wall_time_seconds=20.0)
    context = _context(timeout_seconds=30.0)
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        runtime_type="autogen",
        runtime_version="1",
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        mode=context.mode,
        state={
            "plan_digest": plan.digest,
            "autogen_version": "0.7.5",
            "terminal": False,
            "reason": None,
            "next_sequence": 2,
            "artifact_registry": {},
            "usage": {"tokens": 0, "cost_usd": "0"},
            "model_ledger": (),
            "tool_ledger": (),
        },
    )
    runtime = AutoGenDiscussionRuntime(_UnusedGateway(), plan)
    runtime._validate_checkpoint(checkpoint, context)
    wall_time = getattr(adapter, "_checkpoint_wall_time_seconds", None)
    assert wall_time is not None, "checkpoint wall-time restoration is not implemented"

    assert wall_time(plan, context, checkpoint) == 20.0
