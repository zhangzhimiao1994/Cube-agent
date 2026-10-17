"""Synchronous bounded-window regression; no runtime or provider execution."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from agent_hub.models.types import ModelMessage
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, _CrewContentLimits

NEW_PREFIX = "WORKSPACE_MAINTENANCE_DELIVERY_CONTINUATION:"
OLD_PREFIX = "WORKSPACE_DELIVERY_CONTINUATION:"
MAX_BYTES = 65_536


def size(messages: Sequence[ModelMessage]) -> int:
    return sum(len(json.dumps(message.content, ensure_ascii=False, allow_nan=False,
                              sort_keys=True, separators=(",", ":")).encode())
               for message in messages)


def window(prefix: str) -> tuple[tuple[ModelMessage, ...], ModelMessage]:
    control = ModelMessage(role="system", content=prefix + " Continue authorized repair. " * 17)
    anchors = (ModelMessage(role="system", content="Fixed anchor."),
               ModelMessage(role="user", content="Synthetic repair request."))
    feedback = ModelMessage(role="user", content="UNTRUSTED_CAPABILITY_RESULTS_JSON=" + json.dumps([
        {"name": "workspace.read", "result": {"path": "synthetic.ts", "text": "x" * (MAX_BYTES - 760)}}]))
    progress = ModelMessage(role="user", content="WORKSPACE_DELIVERY_PROGRESS_JSON=" + json.dumps(
        {"files": [], "file_count": 0, "omitted_files": 0}))
    messages = (*anchors, ModelMessage(role="assistant", content="x" * MAX_BYTES),
                feedback, control, progress)
    assert size((*anchors, feedback, control)) <= MAX_BYTES < size(messages)
    return messages, control


@pytest.mark.parametrize("prefix", (OLD_PREFIX, NEW_PREFIX))
def test_opted_in_compactor_keeps_control_and_complete_latest_feedback(prefix: str) -> None:
    messages, control = window(prefix)
    output = CrewDispatchRuntime._compact_interaction_messages(
        messages, max_prompt_bytes=MAX_BYTES, message_limit=48, preserve_feedback=True,
        preserve_maintenance_control=True,
    )
    assert output[:2] == messages[:2] and messages[3] in output
    assert control in output
    assert size(output) <= MAX_BYTES and len(output) <= 48


def test_missing_and_zero_policy_keep_exact_legacy_window() -> None:
    messages, control = window(NEW_PREFIX)
    legacy = CrewDispatchRuntime._compact_interaction_messages(
        messages, max_prompt_bytes=MAX_BYTES, message_limit=48, preserve_feedback=True)
    disabled = CrewDispatchRuntime._compact_interaction_messages(
        messages, max_prompt_bytes=MAX_BYTES, message_limit=48, preserve_feedback=True,
        preserve_maintenance_control=False)
    assert disabled == legacy and control not in legacy


def test_policy_does_not_enable_feedback_retention_by_itself() -> None:
    messages, _ = window(NEW_PREFIX)
    legacy = CrewDispatchRuntime._legacy_compact_interaction_messages(
        messages, max_prompt_bytes=MAX_BYTES, message_limit=48)
    assert CrewDispatchRuntime._compact_interaction_messages(
        messages, max_prompt_bytes=MAX_BYTES, message_limit=48,
        preserve_maintenance_control=True) == legacy


def test_response_contract_path_forwards_opt_in() -> None:
    messages, control = window(NEW_PREFIX)
    limits = _CrewContentLimits(MAX_BYTES, MAX_BYTES, MAX_BYTES, MAX_BYTES, 48, MAX_BYTES)
    # No schema/provider is needed to exercise the actual request assembler.
    output = CrewDispatchRuntime._response_contract_messages(
        messages, None, limits=limits, preserve_feedback=True, preserve_maintenance_control=True)
    assert control in output
    assert control not in CrewDispatchRuntime._response_contract_messages(
        messages, None, limits=limits, preserve_feedback=True)


@pytest.mark.parametrize("enabled", (False, True))
def test_small_window_reproduces_exact_opt_in_boundary(enabled: bool) -> None:
    control = ModelMessage(role="system", content=NEW_PREFIX + " keep " * 35)
    anchors = (ModelMessage(role="system", content="anchor"), ModelMessage(role="user", content="task"))
    feedback = ModelMessage(role="user", content="UNTRUSTED_CAPABILITY_RESULTS_JSON=" + json.dumps(
        [{"name": "workspace.list", "result": {"workspace_files": []}}], separators=(",", ":")))
    assert size((*anchors, control, feedback)) == 373
    output = CrewDispatchRuntime._compact_interaction_messages(
        (*anchors, ModelMessage(role="system", content="old " * 2000), control, feedback),
        max_prompt_bytes=512, message_limit=8, preserve_feedback=True,
        preserve_maintenance_control=enabled)
    assert output[:2] == anchors and feedback in output
    assert (control in output) is enabled
    assert size(output) <= 512
