from agent_hub.runs.repository import _is_recovery_replayable_event_kind


def test_harness_started_is_recovery_replayable_observability() -> None:
    assert _is_recovery_replayable_event_kind("harness.started")


def test_runtime_and_tool_events_remain_recovery_blocking() -> None:
    assert not _is_recovery_replayable_event_kind("runtime.completed")
    assert not _is_recovery_replayable_event_kind("tool.completed")
