"""Concrete values around the existing offline FD simulator and real stream writer."""
import asyncio
import posixpath
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol

from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, ResolutionDescriptor
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim as ClaimValue,
)
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
)
from agent_hub.skills.sandbox.python_resolution_store import ResolutionStore
from tests.unit.skills.test_python_dependency_index_store import FileSystem
from tests.unit.skills.test_python_registry_read import Wire


class ResolutionProtocolAPI(Protocol):
    ResolutionClaim: type[ClaimValue]
    ResolutionRequest: type[ResolutionRequest]
    ResolutionStatus: type[ResolutionStatus]

    def decode_descriptor(self, value: object) -> ResolutionDescriptor: ...

    def resolution_key(
        self, claim: ClaimValue, needs: tuple[DependencyNeed, ...],
        environment: dict[str, str],
    ) -> str: ...


class StoreAPI(Protocol):
    ResolutionStore: type[ResolutionStore]

    def overlay_supply(
        self, base: RootSupplyConfig, descriptor: ResolutionDescriptor,
    ) -> RootSupplyConfig: ...

    def freeze_plan(
        self, descriptor: ResolutionDescriptor, *, original: ClaimValue,
        current: ClaimValue, overlay_sha256: str,
    ) -> ResolutionPlan: ...


class WorkerAPI(Protocol):
    async def execute_resolution(
        self, request: ResolutionRequest, *, policy: BrokerPolicy,
        root: RootConfig, supply: RootSupplyConfig,
    ) -> ResolutionStatus: ...

    async def _fetch(
        self, entry: WheelDownload, request: ResolutionRequest, policy: BrokerPolicy,
        root: RootConfig, supply: RootSupplyConfig, deadline: float,
    ) -> bytes: ...


@dataclass(frozen=True)
class DirectoryEntry:
    name: str


class ResolutionFileSystem(FileSystem):
    """Keep the shared FD implementation; add only this fixture's scandir seam."""
    O_WRONLY = 1

    @contextmanager
    def scandir(self, parent: int) -> Iterator[Iterator[DirectoryEntry]]:
        directory = self.fds[parent][1]
        self.events.append(("scandir", directory))
        yield (DirectoryEntry(name=posixpath.basename(path))
               for path in tuple(self.nodes) if posixpath.dirname(path) == directory)


class FcntlModule(ModuleType):
    LOCK_SH, LOCK_EX, LOCK_NB = 1, 2, 4

    def __init__(self, fs: FileSystem) -> None:
        super().__init__("fcntl")
        self.flock = fs.flock


@dataclass
class RootStoreCase:
    fs: ResolutionFileSystem
    supply: RootSupplyConfig
    store: ResolutionStore
    base_raw: bytes


@dataclass
class WorkerCase:
    worker: WorkerAPI
    request: ResolutionRequest
    root: RootConfig
    supply: RootSupplyConfig
    events: list[str]
    wire: Wire
    bodies: dict[str, bytes]
    fs: ResolutionFileSystem
    store: ResolutionStore
    policy: BrokerPolicy
    locked: bool = False
    fail: str | None = None


class RecordingTransport(asyncio.Transport):
    def __init__(self, protocol: asyncio.StreamReaderProtocol) -> None:
        super().__init__()
        self.protocol = protocol
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.data.extend(data)

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.protocol.connection_lost(None)


class MemoryWriter(asyncio.StreamWriter):
    def __init__(
        self, reader: asyncio.StreamReader, *, on_drain: Callable[[], None] | None = None,
    ) -> None:
        protocol = asyncio.StreamReaderProtocol(reader)
        self.recording = RecordingTransport(protocol)
        self.on_drain = on_drain
        super().__init__(self.recording, protocol, reader, asyncio.get_running_loop())

    @property
    def data(self) -> bytearray:
        return self.recording.data

    @property
    def closed(self) -> bool:
        return self.recording.closed

    async def drain(self) -> None:
        if self.on_drain is not None:
            self.on_drain()
        await super().drain()
