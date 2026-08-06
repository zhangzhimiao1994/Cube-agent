"""Framework-neutral discussion runtime backend selection.

The user-facing execution mode remains ``Discuss``.  This module chooses which
long-running group-discussion framework implements that mode without making
AutoGen the product default.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import ExecutionRuntime


class DiscussionBackend(StrEnum):
    """Supported implementation families for the Discuss mode."""

    MAF = "maf"
    AG2 = "ag2"
    AUTOGEN_LEGACY = "autogen_legacy"


class DiscussionBackendUnavailable(RuntimeError):
    """Stable configuration/runtime error for unavailable discussion backends."""


class DiscussionRuntimeProvider(Protocol):
    """Preconfigured factory for one discussion runtime backend."""

    @property
    def backend(self) -> DiscussionBackend:
        """Backend implemented by this provider."""

    @property
    def available(self) -> bool:
        """Whether dependencies and configuration are ready."""

    @property
    def unavailable_reason(self) -> str | None:
        """Stable operator-facing reason when unavailable."""

    def create_runtime(self) -> ExecutionRuntime:
        """Build a fresh or single-flight-safe runtime for this backend."""


@dataclass(frozen=True, slots=True)
class CallableDiscussionRuntimeProvider:
    """Small provider helper for wiring preconfigured adapter factories."""

    backend: DiscussionBackend
    factory: Callable[[], ExecutionRuntime]
    available: bool = True
    unavailable_reason: str | None = None

    def create_runtime(self) -> ExecutionRuntime:
        return self.factory()


@dataclass(frozen=True, slots=True)
class DiscussionBackendSelection:
    """Backend choice supplied by configuration or an explicit operator override."""

    backend: DiscussionBackend | None = None

    def __post_init__(self) -> None:
        if self.backend is not None and type(self.backend) is not DiscussionBackend:
            raise ValueError("discussion backend is invalid")


class DiscussionRuntimeRegistry:
    """Select Discuss runtimes without silently changing framework semantics.

    Default selection considers production backends only, in long-term support
    order: Microsoft Agent Framework first, then AG2.  AutoGen AgentChat is a
    legacy compatibility backend and can be created only when explicitly
    selected.
    """

    __slots__ = ("_preferred_backends", "_providers")

    _DEFAULT_PREFERRED = (DiscussionBackend.MAF, DiscussionBackend.AG2)

    def __init__(
        self,
        providers: Iterable[DiscussionRuntimeProvider],
        *,
        preferred_backends: tuple[DiscussionBackend, ...] = _DEFAULT_PREFERRED,
    ) -> None:
        self._validate_preferred(preferred_backends)
        provider_map: dict[DiscussionBackend, DiscussionRuntimeProvider] = {}
        for provider in providers:
            backend = self._provider_backend(provider)
            if backend in provider_map:
                raise ValueError("duplicate discussion backend registration")
            provider_map[backend] = provider
        object.__setattr__(self, "_providers", MappingProxyType(provider_map))
        object.__setattr__(self, "_preferred_backends", preferred_backends)

    @staticmethod
    def _validate_preferred(backends: tuple[DiscussionBackend, ...]) -> None:
        if not backends:
            raise ValueError("at least one preferred discussion backend is required")
        if any(type(backend) is not DiscussionBackend for backend in backends):
            raise ValueError("discussion backend is invalid")
        if DiscussionBackend.AUTOGEN_LEGACY in backends:
            raise ValueError("AutoGen legacy cannot be a default discussion backend")
        if len(set(backends)) != len(backends):
            raise ValueError("duplicate preferred discussion backend")

    @staticmethod
    def _provider_backend(provider: DiscussionRuntimeProvider) -> DiscussionBackend:
        try:
            backend = provider.backend
        except Exception as error:  # noqa: BLE001 - hostile adapter boundary
            DiscussionRuntimeRegistry._scrub_exception(error)
            raise ValueError("discussion backend is invalid") from None
        if type(backend) is not DiscussionBackend:
            raise ValueError("discussion backend is invalid")
        return backend

    _providers: Mapping[DiscussionBackend, DiscussionRuntimeProvider]
    _preferred_backends: tuple[DiscussionBackend, ...]

    @property
    def preferred_backends(self) -> tuple[DiscussionBackend, ...]:
        return self._preferred_backends

    def create(self, selection: DiscussionBackendSelection) -> ExecutionRuntime:
        if type(selection) is not DiscussionBackendSelection:
            raise DiscussionBackendUnavailable("discussion backend selection is invalid")
        if selection.backend is not None:
            return self._create_explicit(selection.backend)
        for backend in self._preferred_backends:
            provider = self._providers.get(backend)
            if provider is None:
                continue
            return self._create_from_provider(provider)
        raise DiscussionBackendUnavailable(
            "No default discussion runtime backend is configured; configure MAF or AG2. "
            "AutoGen is legacy compatibility and requires explicit selection."
        )

    def _create_explicit(self, backend: DiscussionBackend) -> ExecutionRuntime:
        provider = self._providers.get(backend)
        if provider is None:
            raise DiscussionBackendUnavailable(
                f"Discussion backend '{backend.value}' is not configured"
            )
        return self._create_from_provider(provider)

    def _create_from_provider(self, provider: DiscussionRuntimeProvider) -> ExecutionRuntime:
        backend = self._provider_backend(provider)
        try:
            available = provider.available
        except Exception as error:  # noqa: BLE001 - hostile adapter boundary
            self._scrub_exception(error)
            raise DiscussionBackendUnavailable(
                f"Discussion backend '{backend.value}' availability check failed"
            ) from None
        if not available:
            try:
                reason = provider.unavailable_reason
            except Exception as error:  # noqa: BLE001 - hostile adapter boundary
                self._scrub_exception(error)
                reason = None
            reason = reason or "dependency or configuration is unavailable"
            raise DiscussionBackendUnavailable(
                f"Discussion backend '{backend.value}' is unavailable: {reason}"
            )
        try:
            runtime = provider.create_runtime()
        except Exception as error:  # noqa: BLE001 - hostile adapter boundary
            self._scrub_exception(error)
            raise DiscussionBackendUnavailable(
                f"Discussion backend '{backend.value}' runtime factory failed"
            ) from None
        if not self._is_discuss_runtime(runtime):
            raise DiscussionBackendUnavailable(
                f"Discussion backend '{backend.value}' did not create a Discuss runtime"
            )
        return runtime

    @staticmethod
    def _is_discuss_runtime(runtime: object) -> bool:
        try:
            mode = cast(ExecutionRuntime, runtime).mode
            methods = (
                cast(ExecutionRuntime, runtime).run,
                cast(ExecutionRuntime, runtime).save_checkpoint,
                cast(ExecutionRuntime, runtime).restore_checkpoint,
                cast(ExecutionRuntime, runtime).cancel,
            )
        except Exception as error:  # noqa: BLE001 - hostile adapter boundary
            DiscussionRuntimeRegistry._scrub_exception(error)
            return False
        return mode is TaskMode.DISCUSS and all(callable(method) for method in methods)

    @staticmethod
    def _scrub_exception(error: BaseException) -> None:
        error.__traceback__ = None
        error.__context__ = None
        error.__cause__ = None


__all__ = [
    "CallableDiscussionRuntimeProvider",
    "DiscussionBackend",
    "DiscussionBackendSelection",
    "DiscussionBackendUnavailable",
    "DiscussionRuntimeProvider",
    "DiscussionRuntimeRegistry",
]
