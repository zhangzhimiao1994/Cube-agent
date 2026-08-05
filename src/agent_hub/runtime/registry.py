"""Immutable runtime selection by explicit executable mode."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import ExecutionRuntime


class InvalidRuntimeRegistration(ValueError):
    """A stable configuration failure without runtime object details."""


class RuntimeRegistry:
    """An immutable lookup; mutable runtime instances enforce their own single-flight policy."""

    __slots__ = ("_runtimes",)

    def __init__(self, runtimes: Iterable[ExecutionRuntime]) -> None:
        registered: dict[TaskMode, ExecutionRuntime] = {}
        for runtime in runtimes:
            if len(registered) >= 16:
                raise InvalidRuntimeRegistration("runtime registration limit exceeded")
            mode = runtime.mode
            if not isinstance(mode, TaskMode) or mode is TaskMode.AUTO:
                raise InvalidRuntimeRegistration("runtime mode must be explicitly executable")
            if mode in registered:
                raise InvalidRuntimeRegistration("duplicate runtime mode registration")
            registered[mode] = runtime
        if not registered:
            raise InvalidRuntimeRegistration("at least one runtime is required")
        object.__setattr__(self, "_runtimes", MappingProxyType(registered))

    _runtimes: Mapping[TaskMode, ExecutionRuntime]

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("runtime registry is immutable")

    def get(self, mode: TaskMode) -> ExecutionRuntime:
        if not isinstance(mode, TaskMode) or mode is TaskMode.AUTO:
            raise LookupError("runtime unavailable")
        try:
            return self._runtimes[mode]
        except KeyError:
            raise LookupError("runtime unavailable") from None
