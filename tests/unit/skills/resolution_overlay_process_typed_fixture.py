"""Typed local resolution/registry/process boundaries; no native or network grant."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import NotRequired, Protocol, TypedDict, runtime_checkable

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.skills.sandbox import python_resolution_worker as worker
from agent_hub.skills.sandbox.base import ProcessLimits
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, ProjectInvocation
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlanningRequest
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
)
from agent_hub.skills.sandbox.python_resolution_store import ResolutionLeaseWitness, ResolutionStore
from tests.unit.runs.project_auto_repair_typed_fixture import present, sequence, string
from tests.unit.skills.python_registry_typed_fixture import object_array, object_map
from tests.unit.skills.python_resolution_store_typed_fixture import (
    DirectoryEntry,
    ResolutionFileSystem,
    RootStoreCase,
    WorkerCase,
)
from tests.unit.skills.sandbox.python_input_supply_worker_typed_fixture import RunOptions
from tests.unit.skills.test_python_registry_read import Wire

if sys.platform == "win32":
    from pathlib import WindowsPath as NativePath
else:
    from pathlib import PosixPath as NativePath

__all__ = [
    "DirectoryEntry",
    "RootStoreCase",
    "RunOptions",
    "object_array",
    "object_map",
    "present",
    "sequence",
    "string",
]


class WorkerSurface(Protocol):
    async def execute_resolution(
        self,
        request: ResolutionRequest,
        *,
        policy: BrokerPolicy,
        root: RootConfig,
        supply: RootSupplyConfig,
        lease_witness: ResolutionLeaseWitness | None = None,
    ) -> ResolutionStatus: ...

    async def handle_resolution(
        self,
        broker: ProjectExecutionBroker,
        request: ResolutionRequest,
        *,
        peer_uid: int,
    ) -> ResolutionStatus: ...


@runtime_checkable
class WheelFetcher(Protocol):
    async def __call__(
        self,
        entries: tuple[WheelDownload, ...],
        invocation: ProjectInvocation,
        policy: BrokerPolicy,
        root: RootConfig,
        supply: RootSupplyConfig,
        deadline: float,
    ) -> dict[str, bytes]: ...


def get_worker_fetch() -> WheelFetcher:
    """Keep the currently patched imported callable, including its closure state."""
    callback: object = vars(worker)["fetch_catalog_wheels"]
    assert isinstance(callback, WheelFetcher)
    return callback


@dataclass
class NetworkCase:
    """Keep the original WorkerCase and every closure-owned mutable field aliased."""

    original: WorkerCase
    processes: list[tuple[tuple[str, ...], ProcessLimits, RunOptions]] = field(default_factory=list)
    child_reads: list[tuple[object, ...]] = field(default_factory=list)
    root_dns: list[tuple[object, ...]] = field(default_factory=list)
    manager: ProjectExecutionBroker = field(init=False)
    root_events: list[tuple[str, str | float]] = field(init=False)
    entered: asyncio.Event = field(init=False)
    failure: str | None = field(init=False)
    stage: Stage = field(init=False)
    staged: ResolutionRequest = field(init=False)

    @property
    def worker(self) -> WorkerSurface:
        return worker

    @property
    def request(self) -> ResolutionRequest:
        return self.original.request

    @property
    def root(self) -> RootConfig:
        return self.original.root

    @property
    def supply(self) -> RootSupplyConfig:
        return self.original.supply

    @property
    def events(self) -> list[str]:
        return self.original.events

    @property
    def wire(self) -> Wire:
        return self.original.wire

    @property
    def fs(self) -> ResolutionFileSystem:
        return self.original.fs

    @property
    def store(self) -> ResolutionStore:
        return self.original.store

    @property
    def policy(self) -> BrokerPolicy:
        return self.original.policy

    @policy.setter
    def policy(self, value: BrokerPolicy) -> None:
        self.original.policy = value

    @property
    def fail(self) -> str | None:
        return self.original.fail

    @fail.setter
    def fail(self, value: str | None) -> None:
        self.original.fail = value


@runtime_checkable
class WorkerCaseFactory(Protocol):
    __wrapped__: Callable[[RootStoreCase, pytest.MonkeyPatch], WorkerCase]


def configured_worker(
    factory: object,
    root_store: RootStoreCase,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkerCase:
    """Invoke the actual pytest-wrapped factory and check its concrete DTO."""
    assert isinstance(factory, WorkerCaseFactory)
    result: object = factory.__wrapped__(root_store, monkeypatch)
    assert isinstance(result, WorkerCase)
    return result


class Stage(NativePath):
    def chmod(self, *args: object, **kwargs: object) -> None:
        pass

    def is_absolute(self) -> bool:
        return PurePosixPath(self.as_posix()).is_absolute()


class ProcessNotice(asyncio.subprocess.Process):
    """Only the broker's observed return-code edge; no OS process is started."""

    def __init__(self, code: int | None) -> None:
        self.code = code

    @property
    def returncode(self) -> int | None:
        return self.code


class SendArgs(TypedDict):
    needs: tuple[DependencyNeed, ...]
    environment: dict[str, str]
    planning: PythonMetadataPlanningRequest
    plan: NotRequired[ResolutionPlan | None]
    expected_plan_sha256: NotRequired[str | None]


class ResolveArgs(TypedDict):
    needs: tuple[DependencyNeed, ...]
    environment: dict[str, str]
    planning: PythonMetadataPlanningRequest


async def no_current() -> None:
    return None


@dataclass
class StagedReference:
    reference: ProjectBaselineReference


@dataclass
class ConsumerRoots:
    stage_root_fd: int
    pins: list[object]
