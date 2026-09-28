from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from autogen_core.models import SystemMessage, UserMessage

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
from agent_hub.runtime.autogen import adapter
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    GatewayChatCompletionClient,
    RuntimeExecutionError,
)
from agent_hub.runtime.autogen.termination import (
    CompositeDiscussionTermination,
    DiscussionUsage,
)
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


async def test_internal_wall_termination_follows_extended_adaptive_deadline() -> None:
    now = 0.0
    deadline = AdaptiveDeadline.create(
        now=now,
        initial_seconds=2.0,
        soft_seconds=4.0,
        absolute_seconds=10.0,
        complexity_units=2,
    )
    termination = CompositeDiscussionTermination(
        usage=DiscussionUsage(),
        max_turns=10,
        token_budget=1_000,
        cost_budget_usd=Decimal(10),
        wall_time_seconds=2.0,
        consensus_votes=2,
        cancelled=lambda: False,
        monotonic=lambda: now,
        adaptive_deadline=deadline,
    )

    now = 1.5
    assert deadline.observe(progress_units=1, now=now)
    now = 2.5
    assert await termination(()) is None

    now = 4.1
    stopped = await termination(())
    assert stopped is not None
    assert stopped.content == "wall_time"


async def test_internal_wall_termination_never_exceeds_absolute_deadline() -> None:
    now = 0.0
    deadline = AdaptiveDeadline.create(
        now=now,
        initial_seconds=2.0,
        soft_seconds=20.0,
        absolute_seconds=5.0,
        complexity_units=1,
    )
    termination = CompositeDiscussionTermination(
        usage=DiscussionUsage(),
        max_turns=10,
        token_budget=1_000,
        cost_budget_usd=Decimal(10),
        wall_time_seconds=2.0,
        consensus_votes=2,
        cancelled=lambda: False,
        monotonic=lambda: now,
        adaptive_deadline=deadline,
    )

    now = 1.0
    assert deadline.observe(progress_units=1, now=now)
    assert deadline.deadline == deadline.absolute_deadline == 5.0
    now = 5.0
    stopped = await termination(())

    assert stopped is not None
    assert stopped.content == "wall_time"


async def test_adaptive_termination_reads_extended_active_context_budget() -> None:
    context = _context(token_budget=100)
    plan = _plan(token_budget=200)
    usage = DiscussionUsage(tokens=150)
    base = CompositeDiscussionTermination(
        usage=usage,
        max_turns=10,
        token_budget=100,
        cost_budget_usd=Decimal(1),
        wall_time_seconds=60.0,
        consensus_votes=2,
        cancelled=lambda: False,
    )
    termination = adapter._AdaptiveDiscussionTermination(
        base=base,
        control=adapter._DiscussionControl.create(plan, context, usage),
        participants=frozenset({"analyst", "critic"}),
        context=context,
        plan_token_budget=plan.token_budget,
    )
    object.__setattr__(context, "token_budget", 200)

    assert await termination(()) is None
    assert base.token_budget == 200


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


def test_hard_limit_does_not_preempt_configured_soft_turn_window() -> None:
    limit = getattr(adapter, "_discussion_hard_turn_limit", None)
    assert limit is not None, "dynamic hard limit is not implemented"

    token_constrained = limit(
        _plan(soft_turns=4, token_budget=2),
        _context(token_budget=2),
        0,
    )
    time_constrained = limit(
        _plan(soft_turns=4, wall_time_seconds=0.2),
        _context(timeout_seconds=0.2),
        0,
    )

    assert token_constrained == 4
    assert time_constrained == 4


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


def test_history_compaction_window_scales_with_context_budget_and_project_size() -> None:
    window = getattr(adapter, "_message_compaction_window", None)
    assert window is not None, "dynamic discussion history window is not implemented"
    compact = getattr(adapter, "_compact_llm_messages", None)
    assert compact is not None
    small_context = _context(token_budget=4_096)
    large_context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS,
        request="Design a very large project",
        token_budget=65_536,
        timeout_seconds=600.0,
        routing_decision={
            "runtime_timeout_source": "project_scale_soft_budget",
            "critical_path_complexity_units": 8,
        },
    )
    small_window = window(small_context)
    large_window = window(large_context)
    messages = tuple(
        UserMessage(content=f"message-{index}", source="user")
        for index in range(small_window.watermark + 1)
    )

    assert large_window.watermark > small_window.watermark
    assert large_window.target > small_window.target
    assert len(compact(messages, window=small_window)) <= small_window.target
    assert compact(messages, window=large_window) == messages


def test_history_compaction_window_honors_deployment_context_window() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS,
        request="Design a large project",
        token_budget=1_000_000,
        timeout_seconds=600.0,
        routing_decision={
            "critical_path_complexity_units": 1,
            "main_agent_context_window_tokens": 32_768,
        },
    )

    window = adapter._message_compaction_window(context)

    assert window.allowed_messages == 144
    assert window.allowed_messages < adapter._ABSOLUTE_MESSAGE_SAFETY_LIMIT


async def test_large_context_gateway_preserves_more_than_128_messages() -> None:
    class RecordingGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text="complete",
                    usage=TokenUsage(
                        prompt_tokens=160,
                        completion_tokens=1,
                        total_tokens=161,
                    ),
                ),
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="test",
                provider_model="test/model",
                cost_usd=Decimal(0),
            )

    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS,
        request="Design a very large project",
        token_budget=131_072,
        timeout_seconds=600.0,
        routing_decision={"critical_path_complexity_units": 8},
    )
    gateway = RecordingGateway()
    client = GatewayChatCompletionClient(
        gateway,
        "shared",
        DiscussionUsage(),
        compaction_window=adapter._message_compaction_window(context),
    )
    messages = tuple(
        UserMessage(content=f"message-{index}", source="user") for index in range(160)
    )

    await client.create(messages)

    assert len(gateway.requests) == 1
    assert len(gateway.requests[0].messages) == 160


async def test_gateway_rejects_history_beyond_absolute_message_fuse() -> None:
    class UnusedGateway:
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            del request
            raise AssertionError("absolute message fuse must reject before gateway dispatch")

    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=Role.OPERATOR,
        mode=TaskMode.DISCUSS,
        request="Design an ultra project",
        token_budget=10_000_000,
        timeout_seconds=3_600.0,
        routing_decision={"critical_path_complexity_units": 64},
    )
    client = GatewayChatCompletionClient(
        UnusedGateway(),
        "shared",
        DiscussionUsage(),
        compaction_window=adapter._message_compaction_window(context),
    )
    messages = tuple(
        UserMessage(content=f"message-{index}", source="user") for index in range(1_025)
    )

    with pytest.raises(RuntimeExecutionError, match="absolute message limit"):
        await client.create(messages)


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


def test_v2_checkpoint_restore_preserves_progress_deadline_extension() -> None:
    plan = _plan(wall_time_seconds=20.0)
    context = _context(timeout_seconds=60.0)
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
            "remaining_timeout_seconds": 35.0,
        },
    )
    runtime = AutoGenDiscussionRuntime(_UnusedGateway(), plan)
    runtime._validate_checkpoint(checkpoint, context)

    assert adapter._checkpoint_wall_time_seconds(plan, context, checkpoint) == 35.0


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
