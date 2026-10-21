"""Path-only discovery and explicit rejection until a stack has a real executor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from agent_hub.capabilities.project_stack_contracts import StackObservation
from agent_hub.capabilities.project_stack_registry import detect_stacks
from agent_hub.runtime.contracts import JsonValue, _freeze_object


class UnsupportedProjectStack(ValueError):
    """Detection is not permission or proof that the project can be executed."""


def additional_stack_discovery(paths: Sequence[str]) -> Mapping[str, JsonValue] | None:
    detected = detect_stacks(paths)
    if not any(stack not in {'node', 'python'} for stack in detected):
        return None
    selected = detected[0] if len(detected) == 1 else None
    observation = StackObservation(
        detected_stacks=detected, stack=selected,
        reason='mixed_stack_unsupported' if selected is None else 'recipe_not_inspected',
    )
    return _freeze_object(cast(Mapping[str, JsonValue], observation.to_payload()),
                          name='additional project stack discovery')


def require_supported_project_paths(paths: Sequence[str]) -> None:
    if additional_stack_discovery(paths) is not None:
        raise UnsupportedProjectStack('project stack execution is unsupported')
