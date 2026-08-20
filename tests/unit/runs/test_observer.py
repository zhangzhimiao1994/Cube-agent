from __future__ import annotations

from uuid import uuid4

from agent_hub.runs.observer import ObserverPolicy, RunMonitor
from agent_hub.runtime.contracts import EventKind, RunEvent


def test_monitor_emits_capacity_pressure_notice_without_message_body() -> None:
    run_id = uuid4()
    monitor = RunMonitor()

    decision = monitor.observe(
        RunEvent(
            kind=EventKind.STEP_FAILED,
            sequence=3,
            run_id=run_id,
            step_id="planner_step",
            actor="planner",
            reason="model gateway failed: model capacity unavailable",
            payload={"detail": "safe"},
        )
    )

    assert decision is not None
    assert decision.trigger == "model_capacity_pressure"
    event = decision.to_event(run_id=run_id, sequence=4)
    assert event.kind == "observer.notice"
    assert event.payload["action"] == "reschedule_or_reassign_model"
    assert event.payload["source_kind"] == "step.failed"
    assert "message" not in event.payload
    assert "prompt" not in event.payload


def test_monitor_emits_empty_model_response_notice_once() -> None:
    run_id = uuid4()
    monitor = RunMonitor()
    event = RunEvent(
        kind=EventKind.RUNTIME_FAILED,
        sequence=2,
        run_id=run_id,
        reason="model gateway failed: model response text is empty",
    )

    first = monitor.observe(event)
    second = monitor.observe(event)

    assert first is not None
    assert first.trigger == "empty_model_response"
    assert second is None


def test_monitor_emits_repeated_failure_after_threshold() -> None:
    run_id = uuid4()
    monitor = RunMonitor(ObserverPolicy(repeated_failure_threshold=2))

    first = monitor.observe(
        RunEvent(
            kind=EventKind.STEP_FAILED,
            sequence=1,
            run_id=run_id,
            step_id="planner_step",
            actor="planner",
            reason="planner failed",
        )
    )
    second = monitor.observe(
        RunEvent(
            kind=EventKind.TOOL_FAILED,
            sequence=2,
            run_id=run_id,
            actor="planner",
            tool_call_id="tool_1",
            tool_name="read_context",
            reason="tool failed",
        )
    )

    assert first is not None
    assert first.trigger == "runtime_failure"
    assert second is not None
    assert second.trigger == "repeated_failure"
    assert second.counters["failure_events"] == 2


def test_monitor_recommends_compaction_from_counts_not_content() -> None:
    run_id = uuid4()
    monitor = RunMonitor(ObserverPolicy(max_message_events_before_compaction=2))

    assert (
        monitor.observe(
            RunEvent(
                kind=EventKind.MESSAGE_CREATED,
                sequence=1,
                run_id=run_id,
                actor="planner",
                message="第一条很长的正文不应该进入 observer payload。",
            )
        )
        is None
    )
    decision = monitor.observe(
        RunEvent(
            kind=EventKind.MESSAGE_CREATED,
            sequence=2,
            run_id=run_id,
            actor="reviewer",
            message="第二条正文同样不应该进入 observer payload。",
        )
    )

    assert decision is not None
    assert decision.trigger == "context_compaction_recommended"
    payload = decision.to_event(run_id=run_id, sequence=3).payload
    assert payload["action"] == "compact_context_before_next_model_call"
    assert "正文" not in repr(payload)
