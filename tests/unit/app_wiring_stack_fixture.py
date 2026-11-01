"""Observe real review assembly without substituting policy or harness objects."""
from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

import pytest

from agent_hub.capabilities.defaults import RuntimeCapabilityStack, build_runtime_capability_stack
from agent_hub.runs.project_inputs import OriginalProjectPreparer
from agent_hub.runtime.review_assembly import assemble_review_capability_stack


class WiringStacks:
    def __init__(self, captured: dict[str, object], key: str) -> None:
        self.captured, self.key = captured, key
        self.stacks: list[RuntimeCapabilityStack] = []
        self.options: list[dict[str, object]] = []

    @property
    def last(self) -> RuntimeCapabilityStack:
        assert self.stacks
        return self.stacks[-1]

    def record[**P](self, function: Callable[P, RuntimeCapabilityStack]) -> Callable[P, RuntimeCapabilityStack]:
        def call(*args: P.args, **kwargs: P.kwargs) -> RuntimeCapabilityStack:
            stack = function(*args, **kwargs)
            self.stacks.append(stack)
            self.options.append(dict(kwargs))
            self.captured[self.key] = dict(kwargs)
            self.captured['project_input_preparer'] = stack.project_input_preparer
            assert stack.harness_tool_gateway._backend is stack.runtime_gateway
            assert stack.harness_tool_gateway._policy_gateway is stack.policy_gateway
            preparer, cleanup = stack.project_input_preparer, stack.project_cache_cleanup_hook
            if kwargs.get('project_workspace_dir') is not None:
                assert isinstance(preparer, OriginalProjectPreparer)
                assert preparer.authorizer is stack.harness_tool_gateway
                assert preparer.verifier is stack.runtime_gateway._project_verification
                assert cleanup is not None
                assert cleanup.verifier is preparer.verifier
                assert preparer.capacity_maintenance == cleanup.maintain_capacity
            return stack
        return call


def available_builder[**P](function: Callable[P, RuntimeCapabilityStack]) -> Callable[P, RuntimeCapabilityStack]:
    def build(*args: P.args, **kwargs: P.kwargs) -> RuntimeCapabilityStack:
        result = function(*args, **kwargs)
        # Exercise Linux wiring on Windows too, without invoking any backend.
        # The real assembler must still create and wire the preparer itself.
        result.runtime_gateway._project_executor_configured = True
        return result
    return build


def capture_stacks(monkeypatch: pytest.MonkeyPatch, module: ModuleType,
                   captured: dict[str, object], key: str = 'runtime_stack_kwargs') -> WiringStacks:
    result = WiringStacks(captured, key)
    monkeypatch.setattr(module, 'build_runtime_capability_stack',
                        available_builder(build_runtime_capability_stack))
    monkeypatch.setattr(module, 'assemble_review_capability_stack',
                        result.record(assemble_review_capability_stack))
    return result
