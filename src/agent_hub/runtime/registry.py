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
        runtime_values = self._safe_iterable(runtimes)
        registered: dict[TaskMode, ExecutionRuntime] = {}
        for runtime in runtime_values:
            if len(registered) >= 16:
                raise InvalidRuntimeRegistration("runtime registration limit exceeded")
            mode = self._safe_mode(runtime)
            if not isinstance(mode, TaskMode) or mode is TaskMode.AUTO:
                raise InvalidRuntimeRegistration("runtime mode must be explicitly executable")
            if mode in registered:
                raise InvalidRuntimeRegistration("duplicate runtime mode registration")
            registered[mode] = runtime
        if not registered:
            raise InvalidRuntimeRegistration("at least one runtime is required")
        object.__setattr__(self, "_runtimes", MappingProxyType(registered))

    @staticmethod
    def _safe_iterable(runtimes: Iterable[ExecutionRuntime]) -> tuple[ExecutionRuntime, ...]:
        failed = False
        values: tuple[ExecutionRuntime, ...] = ()
        try:
            values = tuple(runtimes)
        except Exception as error:  # noqa: BLE001 - hostile plugin boundary
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
            failed = True
        if failed:
            raise InvalidRuntimeRegistration("invalid runtime registration") from None
        return values

    @staticmethod
    def _safe_mode(runtime: ExecutionRuntime) -> object:
        failed = False
        mode: object = None
        try:
            mode = runtime.mode
        except Exception as error:  # noqa: BLE001 - hostile plugin boundary
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
            failed = True
        if failed:
            raise InvalidRuntimeRegistration("invalid runtime registration") from None
        return mode

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
