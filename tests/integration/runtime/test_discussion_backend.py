from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

import agent_hub.runtime.autogen as autogen_legacy
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import ExecutionRuntime, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.discussion import (
    DiscussionBackend,
    DiscussionBackendSelection,
    DiscussionBackendUnavailable,
    DiscussionRuntimeProvider,
    DiscussionRuntimeRegistry,
)


class SentinelRuntime:
    mode = TaskMode.DISCUSS

    def __init__(self, marker: str) -> None:
        self.marker = marker

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        if False:
            yield

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise NotImplementedError

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        return None


@dataclass(frozen=True)
class Provider:
    backend: DiscussionBackend
    marker: str
    available: bool = True
    unavailable_reason: str | None = None

    def create_runtime(self) -> ExecutionRuntime:
        return SentinelRuntime(self.marker)


class DirectRuntime:
    mode = TaskMode.DIRECT

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        if False:
            yield

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise NotImplementedError

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        return None


class HostileRuntime:
    @property
    def mode(self) -> TaskMode:
        raise RuntimeError("secret provider detail")

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        if False:
            yield

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise NotImplementedError

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        return None


@dataclass(frozen=True)
class RuntimeProvider:
    backend: DiscussionBackend
    runtime: ExecutionRuntime
    available: bool = True
    unavailable_reason: str | None = None

    def create_runtime(self) -> ExecutionRuntime:
        return self.runtime


@dataclass(frozen=True)
class ExplodingProvider:
    backend: DiscussionBackend
    available: bool = True
    unavailable_reason: str | None = None

    def create_runtime(self) -> ExecutionRuntime:
        raise RuntimeError("secret factory detail")


def test_default_discussion_selection_prefers_maf_then_ag2_and_excludes_autogen_legacy() -> None:
    registry = DiscussionRuntimeRegistry(
        (
            Provider(DiscussionBackend.AG2, "ag2"),
            Provider(DiscussionBackend.AUTOGEN_LEGACY, "autogen"),
        )
    )

    runtime = registry.create(DiscussionBackendSelection())

    assert isinstance(runtime, SentinelRuntime)
    assert runtime.marker == "ag2"


def test_maf_is_selected_before_ag2_when_both_are_configured() -> None:
    registry = DiscussionRuntimeRegistry(
        (
            Provider(DiscussionBackend.AG2, "ag2"),
            Provider(DiscussionBackend.MAF, "maf"),
        )
    )

    runtime = registry.create(DiscussionBackendSelection())

    assert isinstance(runtime, SentinelRuntime)
    assert runtime.marker == "maf"


def test_ag2_can_be_selected_explicitly_without_enabling_legacy_autogen() -> None:
    registry = DiscussionRuntimeRegistry(
        (
            Provider(DiscussionBackend.MAF, "maf"),
            Provider(DiscussionBackend.AG2, "ag2"),
        )
    )

    runtime = registry.create(DiscussionBackendSelection(backend=DiscussionBackend.AG2))

    assert isinstance(runtime, SentinelRuntime)
    assert runtime.marker == "ag2"


def test_autogen_legacy_requires_explicit_backend_selection() -> None:
    registry = DiscussionRuntimeRegistry(
        (Provider(DiscussionBackend.AUTOGEN_LEGACY, "autogen"),)
    )

    with pytest.raises(DiscussionBackendUnavailable, match="MAF or AG2"):
        registry.create(DiscussionBackendSelection())

    runtime = registry.create(
        DiscussionBackendSelection(backend=DiscussionBackend.AUTOGEN_LEGACY)
    )

    assert isinstance(runtime, SentinelRuntime)
    assert runtime.marker == "autogen"


def test_unavailable_explicit_backend_fails_closed_without_cross_framework_fallback() -> None:
    registry = DiscussionRuntimeRegistry(
        (
            Provider(
                DiscussionBackend.MAF,
                "maf",
                available=False,
                unavailable_reason="agent-framework is not installed",
            ),
            Provider(DiscussionBackend.AG2, "ag2"),
        )
    )

    with pytest.raises(DiscussionBackendUnavailable, match="agent-framework"):
        registry.create(DiscussionBackendSelection(backend=DiscussionBackend.MAF))


def test_registration_rejects_direct_autogen_defaulting() -> None:
    with pytest.raises(ValueError, match="legacy"):
        DiscussionRuntimeRegistry(
            (Provider(DiscussionBackend.AUTOGEN_LEGACY, "autogen"),),
            preferred_backends=(DiscussionBackend.AUTOGEN_LEGACY,),
        )


def test_provider_protocol_requires_discuss_runtime() -> None:
    provider: DiscussionRuntimeProvider = Provider(DiscussionBackend.MAF, "maf")
    registry = DiscussionRuntimeRegistry((provider,))

    runtime = registry.create(DiscussionBackendSelection())

    assert runtime.mode is TaskMode.DISCUSS


def test_malformed_runtime_fails_closed_without_exposing_hostile_details() -> None:
    registry = DiscussionRuntimeRegistry(
        (RuntimeProvider(DiscussionBackend.MAF, DirectRuntime()),)
    )

    with pytest.raises(DiscussionBackendUnavailable, match="Discuss runtime"):
        registry.create(DiscussionBackendSelection())

    hostile = DiscussionRuntimeRegistry(
        (RuntimeProvider(DiscussionBackend.MAF, HostileRuntime()),)  # type: ignore[arg-type]
    )

    with pytest.raises(DiscussionBackendUnavailable) as error:
        hostile.create(DiscussionBackendSelection())

    assert "secret provider detail" not in str(error.value)


def test_factory_failure_fails_closed_and_sanitizes_exception_text() -> None:
    registry = DiscussionRuntimeRegistry((ExplodingProvider(DiscussionBackend.MAF),))

    with pytest.raises(DiscussionBackendUnavailable) as error:
        registry.create(DiscussionBackendSelection())

    assert "secret factory detail" not in str(error.value)


def test_autogen_package_surface_identifies_legacy_adapter() -> None:
    assert "legacy" in (autogen_legacy.__doc__ or "").casefold()
    assert autogen_legacy.AutoGenLegacyDiscussionRuntime is autogen_legacy.AutoGenDiscussionRuntime
