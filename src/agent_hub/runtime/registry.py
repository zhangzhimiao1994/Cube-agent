"""Immutable runtime selection by explicit executable mode."""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Never, cast

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import ExecutionRuntime


class InvalidRuntimeRegistration(ValueError):
    """A stable configuration failure without runtime object details."""


def _raise_registration_error(message: str) -> Never:
    raise InvalidRuntimeRegistration(message) from None


class RuntimeRegistry:
    """An immutable lookup; mutable runtime instances enforce their own single-flight policy."""

    __slots__ = ("_runtimes",)

    def __init__(self, runtimes: Iterable[ExecutionRuntime]) -> None:
        registered, error_code = self._collect(runtimes)
        del runtimes
        if registered is None:
            message = error_code or "invalid runtime registration"
            del error_code
            _raise_registration_error(message)
        object.__setattr__(self, "_runtimes", MappingProxyType(registered))

    @staticmethod
    def _collect(
        runtimes: Iterable[ExecutionRuntime],
    ) -> tuple[dict[TaskMode, ExecutionRuntime] | None, str | None]:
        iterator: Iterator[object] | None = None
        try:
            iterator = iter(runtimes)
        except Exception as error:  # noqa: BLE001 - hostile plugin boundary
            RuntimeRegistry._scrub_exception(error)
            del error
        del runtimes
        if iterator is None:
            return None, "invalid runtime registration"

        registered: dict[TaskMode, ExecutionRuntime] = {}
        while True:
            status, value = RuntimeRegistry._next(iterator)
            if status == "stop":
                del iterator
                if not registered:
                    return None, "at least one runtime is required"
                return registered, None
            if status == "error":
                del iterator, registered
                return None, "invalid runtime registration"
            if len(registered) >= 16:
                del value, iterator, registered
                return None, "runtime registration limit exceeded"
            valid_mode, mode = RuntimeRegistry._runtime_contract(value)
            if not valid_mode or not isinstance(mode, TaskMode) or mode is TaskMode.AUTO:
                del value, iterator, registered
                return None, "runtime mode must be explicitly executable"
            if mode in registered:
                del value, iterator, registered
                return None, "duplicate runtime mode registration"
            registered[mode] = cast(ExecutionRuntime, value)

    @staticmethod
    def _next(iterator: Iterator[object]) -> tuple[str, object | None]:
        try:
            return "value", next(iterator)
        except StopIteration:
            return "stop", None
        except Exception as error:  # noqa: BLE001 - hostile plugin boundary
            RuntimeRegistry._scrub_exception(error)
            del error
            return "error", None

    @staticmethod
    def _runtime_contract(runtime: object) -> tuple[bool, object]:
        mode: object = None
        try:
            mode = cast(ExecutionRuntime, runtime).mode
            members = tuple(
                getattr(runtime, name)
                for name in ("run", "save_checkpoint", "restore_checkpoint", "cancel")
            )
        except Exception as error:  # noqa: BLE001 - hostile plugin boundary
            RuntimeRegistry._scrub_exception(error)
            del error
            return False, None
        valid = type(mode) is TaskMode and all(callable(member) for member in members)
        del members, runtime
        return valid, mode

    @staticmethod
    def _scrub_exception(error: BaseException) -> None:
        error.__traceback__ = None
        error.__context__ = None
        error.__cause__ = None

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
