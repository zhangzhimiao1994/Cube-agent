"""Lease continuity is independent of the immutable resolution task deadline."""

from __future__ import annotations

import asyncio
import os
import posixpath
import stat
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.skills.sandbox import python_dependency_index_store as file_api
from agent_hub.skills.sandbox import python_input_supply_catalog as catalog_api
from agent_hub.skills.sandbox import python_metadata_preparation_broker as broker_api
from agent_hub.skills.sandbox import python_resolution_store as store_api
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, ProjectInvocation, _Execution
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataDraft,
    PythonMetadataPlanningRequest,
    PythonMetadataScope,
)
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionClaim, ResolutionRequest


def request() -> ResolutionRequest:
    value = ResolutionClaim(planning=PythonMetadataPlanningRequest(
        invocation=ProjectInvocation(execution_id="resolution_test", tenant_id=UUID(int=1),
            run_id=UUID(int=2), project_id="project", session_id="session",
            sandbox_profile="restricted", timeout_seconds=30),
        scope=PythonMetadataScope(tenant_id=UUID(int=1), run_id=UUID(int=2), user_id=UUID(int=3),
            project_id="project", session_id="session", actor="implementer", actor_role="operator",
            request_sha256="1" * 64, permissions_sha256="4" * 64),
        draft=PythonMetadataDraft(source_sha256="2" * 64, acceptance_sha256="5" * 64,
            manifest_sha256="6" * 64, lock_sha256="7" * 64),
        expected_root_config_sha256="8" * 64, expected_catalog_sha256="3" * 64),
        worker_id="worker", lease_token=UUID(int=4),
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        original_deadline=datetime.now(UTC) + timedelta(seconds=120),
        monotonic_deadline=time.monotonic() + 120)
    env = {"implementation_name": "cpython", "implementation_version": "3.12.14",
        "os_name": "posix", "platform_machine": "x86_64", "platform_release": "test",
        "platform_system": "Linux", "platform_version": "test",
        "platform_python_implementation": "CPython", "python_full_version": "3.12.14",
        "python_version": "3.12", "sys_platform": "linux"}
    return ResolutionRequest(action="resolve_python_inputs", claim=value,
        requirements=(("demo>=1", "a" * 64),), environment=env)


def renewed(original: ResolutionClaim) -> ResolutionClaim:
    return original.model_copy(update={
        "lease_expires_at": original.lease_expires_at + timedelta(seconds=30),
        "monotonic_deadline": original.monotonic_deadline - 0.001,
    })


def test_witness_api_exists() -> None:
    assert hasattr(store_api, "ResolutionLeaseWitness"), "Root private lease witness missing"


@pytest.mark.parametrize("change", ["token", "owner", "root", "scope", "planning", "deadline", "monotonic"])
def test_claim_continuity_rejects_changes(change: str) -> None:
    first = request()
    current = renewed(first.claim)
    if change == "token":
        current = current.model_copy(update={"lease_token": UUID(int=999)})
    elif change == "owner":
        current = current.model_copy(update={"worker_id": "other"})
    elif change in {"root", "scope", "planning"}:
        planning = current.planning
        if change == "root":
            planning = planning.model_copy(update={"expected_root_config_sha256": "e" * 64})
        elif change == "scope":
            planning = planning.model_copy(update={"scope": planning.scope.model_copy(
                update={"permissions_sha256": "e" * 64})})
        else:
            planning = planning.model_copy(update={"draft": planning.draft.model_copy(
                update={"source_sha256": "e" * 64})})
        current = current.model_copy(update={"planning": planning})
    elif change == "deadline":
        current = current.model_copy(update={"original_deadline": first.claim.original_deadline + timedelta(microseconds=1)})
    else:
        current = current.model_copy(update={"monotonic_deadline": first.claim.monotonic_deadline + 1})
    with pytest.raises(ValueError):
        store_api.ResolutionLeaseWitness.require_renewal(first.claim, current)


def test_expired_previous_cannot_be_revived() -> None:
    previous = request().claim.model_copy(update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(ValueError, match="resolution_deadline"):
        store_api.ResolutionLeaseWitness.require_renewal(previous, renewed(previous))


async def test_admission_renewal_updates_only_same_live_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_api, "_systemd_supported", lambda: True)
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))
    first = request()
    job = broker_api._ResolutionPreparationExecution(first.invocation, asyncio.current_task(), original_request=first)
    broker.executions[first.invocation.execution_id] = job
    update = first.model_copy(update={"action": "renew_python_resolution", "claim": renewed(first.claim)})
    result = await broker_api.handle_resolution_preparation(broker, update, peer_uid=broker.policy.allowed_uid)
    assert result.state == "lease_renewed" and result.plan is None and result.descriptor is None
    assert job.current_claim == update.claim and job.stage is None
    assert len(broker.executions) == 1 and job.original_request == first
    job.finished.set()
    denied = await broker_api.handle_resolution_preparation(broker, update, peer_uid=broker.policy.allowed_uid)
    assert denied.state == "unknown"


@pytest.mark.parametrize("change", ["missing", "cancelled", "token", "requirements", "environment", "limits", "plan", "peer", "expired", "closing", "disconnected"])
async def test_renewal_mismatch_allocates_no_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    monkeypatch.setattr(broker_api, "_systemd_supported", lambda: True)
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))
    first = request()
    job = broker_api._ResolutionPreparationExecution(first.invocation, asyncio.current_task(), original_request=first)
    if change != "missing":
        broker.executions[first.invocation.execution_id] = job
    if change == "cancelled":
        broker._cancelled_ids.add(first.invocation.execution_id)
    elif change == "expired":
        job.current_claim = first.claim.model_copy(update={
            "lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    elif change == "closing":
        job.lease_closed = True
    elif change == "disconnected":
        job.task = None
    previous = job.current_claim
    update = first.model_copy(update={"action": "renew_python_resolution", "claim": renewed(first.claim)})
    if change == "token":
        update = update.model_copy(update={"claim": update.claim.model_copy(update={"lease_token": UUID(int=999)})})
    elif change == "requirements":
        update = update.model_copy(update={"requirements": (("other", "b" * 64),)})
    elif change == "environment":
        update = update.model_copy(update={"environment": {**first.environment, "platform_release": "different"}})
    elif change == "limits":
        update = update.model_copy(update={"limits": {**first.limits, "max_packages": 1}})
    elif change == "plan":
        update = update.model_copy(update={"expected_plan_sha256": "c" * 64})
    result = await broker_api.handle_resolution_preparation(broker, update,
        peer_uid=0 if change == "peer" else broker.policy.allowed_uid)
    assert result.state == "unknown"
    assert job.current_claim == previous and job.stage is None
    assert len(broker.executions) == (0 if change == "missing" else 1)


@dataclass
class Node:
    inode: int
    mode: int
    data: bytes = b""
    uid: int = 0
    gid: int = 0
    links: int = 1
    device: int = 1
    version: int = 1


class RootFS:
    """Portable FD model; real production readers/identity guards run unchanged."""

    O_DIRECTORY = 1 << 20
    O_NOFOLLOW = 1 << 21
    O_CLOEXEC = 1 << 22
    O_NONBLOCK = 1 << 23
    O_WRONLY = os.O_WRONLY
    O_CREAT = os.O_CREAT
    O_EXCL = os.O_EXCL
    O_RDONLY = os.O_RDONLY
    name = "posix"

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {"/": Node(1, stat.S_IFDIR | 0o755),
            "/private": Node(2, stat.S_IFDIR | 0o700),
            "/private/stage": Node(3, stat.S_IFDIR | 0o700)}
        self.fds: dict[int, tuple[Node, str, int]] = {}
        self.inode = 10
        self.supports_dir_fd = {self.open, self.stat, self.link, self.unlink}
        self.supports_follow_symlinks = {self.stat, self.link}

    def path(self, name: str, parent: int | None) -> str:
        return posixpath.normpath(name if name.startswith("/") else posixpath.join(
            self.fds[cast(int, parent)][1], name))

    def open(self, name: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        assert flags & self.O_NOFOLLOW and flags & self.O_CLOEXEC
        path = self.path(name, dir_fd)
        if flags & self.O_CREAT:
            assert flags & self.O_EXCL
            if path in self.nodes:
                raise FileExistsError
            self.inode += 1
            self.nodes[path] = Node(self.inode, stat.S_IFREG | mode)
        if path not in self.nodes:
            raise FileNotFoundError
        node = self.nodes[path]
        if stat.S_ISLNK(node.mode):
            raise OSError("link refused")
        if flags & self.O_DIRECTORY and not stat.S_ISDIR(node.mode):
            raise NotADirectoryError
        fd = max(self.fds, default=10) + 1
        self.fds[fd] = node, path, 0
        return fd

    def close(self, fd: int) -> None:
        del self.fds[fd]

    def info(self, node: Node) -> os.stat_result:
        # os.stat_result's optional ns fields require attribute-shaped test data.
        class Info:
            st_dev = node.device
            st_ino = node.inode
            st_mode = node.mode
            st_uid = node.uid
            st_gid = node.gid
            st_nlink = node.links
            st_size = len(node.data)
            st_mtime_ns = node.version
            st_ctime_ns = node.version
        return cast(os.stat_result, Info())

    def stat(self, name: str, *, dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
        assert not follow_symlinks
        path = self.path(name, dir_fd)
        if path not in self.nodes:
            raise FileNotFoundError
        return self.info(self.nodes[path])

    def fstat(self, fd: int) -> os.stat_result:
        return self.info(self.fds[fd][0])

    def read(self, fd: int, count: int) -> bytes:
        node, path, offset = self.fds[fd]
        raw = node.data[offset:offset + min(count, 101)]
        self.fds[fd] = node, path, offset + len(raw)
        return raw

    def write(self, fd: int, raw: bytes) -> int:
        node, path, offset = self.fds[fd]
        chunk = raw[:97]
        node.data = node.data[:offset] + chunk
        node.version += 1
        self.fds[fd] = node, path, offset + len(chunk)
        return len(chunk)

    def fchmod(self, fd: int, mode: int) -> None:
        self.fds[fd][0].mode = stat.S_IFREG | mode

    def fsync(self, fd: int) -> None:
        assert fd in self.fds

    def link(self, source: str, target: str, *, src_dir_fd: int, dst_dir_fd: int,
        follow_symlinks: bool = True) -> None:
        assert not follow_symlinks
        target_path = self.path(target, dst_dir_fd)
        if target_path in self.nodes:
            raise FileExistsError
        node = self.nodes[self.path(source, src_dir_fd)]
        self.nodes[target_path] = node
        node.links += 1

    def unlink(self, name: str, *, dir_fd: int) -> None:
        self.nodes.pop(self.path(name, dir_fd)).links -= 1

    def replace(self, source: str, target: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        self.nodes[self.path(target, dst_dir_fd)] = self.nodes.pop(self.path(source, src_dir_fd))

    def geteuid(self) -> int:
        return 0

    def getegid(self) -> int:
        return 0


@pytest.fixture
def filesystem(monkeypatch: pytest.MonkeyPatch) -> RootFS:
    fs = RootFS()
    for module in (store_api, file_api, catalog_api):
        monkeypatch.setattr(module, "os", fs)
    linux = ModuleType("sys")
    monkeypatch.setattr(linux, "platform", "linux", raising=False)
    monkeypatch.setattr(file_api, "sys", linux)
    monkeypatch.setattr(store_api, "sys", linux)
    lock = ModuleType("fcntl")
    monkeypatch.setattr(lock, "LOCK_SH", 1, raising=False)
    monkeypatch.setattr(lock, "LOCK_EX", 2, raising=False)
    monkeypatch.setattr(lock, "LOCK_NB", 4, raising=False)

    def flock(fd: int, flags: int) -> None:
        assert fd in fs.fds and flags in {5, 6}

    monkeypatch.setattr(lock, "flock", flock, raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", lock)
    return fs


def witness(first: ResolutionRequest) -> store_api.ResolutionLeaseWitness:
    return store_api.ResolutionLeaseWitness(cast(Path, PurePosixPath("/private/stage")), first)


def test_private_witness_crosses_old_lease_without_changing_request_or_intent(filesystem: RootFS) -> None:
    first = request()
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.3)})})
    item = witness(first)
    item.initialize()
    current = renewed(first.claim)
    item.renew(first.claim, current)
    time.sleep(0.35)
    with pytest.raises(ValueError, match="resolution_deadline"):
        _ = first.claim.deadline
    assert item.current() == current
    assert first.claim != current and item.original == first
    assert list(filesystem.nodes) == ["/", "/private", "/private/stage", "/private/stage/resolution-lease.json"]
    assert filesystem.fds == {}


@pytest.mark.parametrize("change", ["symlink", "hardlink", "owner", "mode", "device", "stage", "ancestor", "mount", "stage_mode", "missing", "body", "oversize"])
def test_private_witness_rejects_replacement_or_untrusted_file(filesystem: RootFS, change: str) -> None:
    first = request()
    item = witness(first)
    item.initialize()
    path = "/private/stage/resolution-lease.json"
    node = filesystem.nodes[path]
    if change == "symlink":
        node.mode = stat.S_IFLNK | 0o600
    elif change == "hardlink":
        node.links = 2
    elif change == "owner":
        node.uid = 10001
    elif change == "mode":
        node.mode = stat.S_IFREG | 0o644
    elif change == "device":
        node.device = 2
    elif change == "stage":
        filesystem.nodes["/private/stage"] = replace(filesystem.nodes["/private/stage"], inode=999)
    elif change == "ancestor":
        filesystem.nodes["/private"].mode = stat.S_IFLNK | 0o700
    elif change == "mount":
        filesystem.nodes["/private/stage"].device = 2
        node.device = 2
    elif change == "stage_mode":
        filesystem.nodes["/private/stage"].mode = stat.S_IFDIR | 0o755
    elif change == "missing":
        del filesystem.nodes[path]
    elif change == "body":
        node.data = node.data.replace(b'"key":', b'"other":')
    else:
        node.data = b"x" * (2_000_000 + 1)
    with pytest.raises((ValueError, OSError)):
        item.current()
    assert filesystem.fds == {}


def test_no_renewal_or_expired_renewal_fails_closed(filesystem: RootFS) -> None:
    first = request()
    item = witness(first)
    item.initialize()
    previous = first.claim.model_copy(update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    raw = filesystem.nodes["/private/stage/resolution-lease.json"].data
    with pytest.raises(ValueError, match="resolution_deadline"):
        item.renew(previous, renewed(previous))
    assert filesystem.nodes["/private/stage/resolution-lease.json"].data == raw


def test_disconnected_owner_cannot_keep_witness_live(filesystem: RootFS) -> None:
    first = request()
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.3)})})
    item = witness(first)
    item.initialize()
    time.sleep(0.35)
    with pytest.raises(ValueError, match="resolution_deadline"):
        item.current()
    with pytest.raises(ValueError, match="resolution_deadline"):
        item.renew(first.claim, renewed(first.claim))
    assert filesystem.fds == {}


async def test_staged_job_renews_only_private_witness(filesystem: RootFS, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_api, "_systemd_supported", lambda: True)
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))
    first = request()
    job = broker_api._ResolutionPreparationExecution(first.invocation, asyncio.current_task(), original_request=first)
    job.stage = cast(Path, PurePosixPath("/private/stage"))
    job.witness = witness(first)
    job.witness.initialize()
    broker.executions[first.invocation.execution_id] = job
    current = renewed(first.claim)
    update = first.model_copy(update={"action": "renew_python_resolution", "claim": current})
    result = await broker_api.handle_resolution_preparation(broker, update, peer_uid=broker.policy.allowed_uid)
    assert result.state == "lease_renewed" and job.witness.current() == current
    assert job.original_request == first and not job.launched and not job.slot_acquired
    assert all("intent" not in key and "receipt" not in key for key in filesystem.nodes)
    job.lease_closed = True
    assert (await broker_api.handle_resolution_preparation(broker, update,
        peer_uid=broker.policy.allowed_uid)).state == "unknown"


def test_initialize_is_once_and_mismatched_request_cannot_read(filesystem: RootFS) -> None:
    first = request()
    witness(first).initialize()
    with pytest.raises(FileExistsError):
        witness(first).initialize()
    changed = first.model_copy(update={"limits": {**first.limits, "max_packages": 1}})
    with pytest.raises(ValueError, match="resolution_scope_changed"):
        witness(changed).current()


def waiting_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectExecutionBroker:
    monkeypatch.setattr(broker_api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(broker_api, "read_root_configuration", lambda: SimpleNamespace(stage_root=tmp_path))
    monkeypatch.setattr(broker_api, "read_supply_configuration", lambda: SimpleNamespace())
    monkeypatch.setattr(broker_api, "validate_configuration", lambda *args: None)
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))
    broker.slots = asyncio.Semaphore(0)
    return broker


async def test_waiting_slot_stops_at_unrenewed_lease_not_task_budget(tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    broker = waiting_broker(tmp_path, monkeypatch)
    first = request()
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.12)})})
    operation = asyncio.create_task(broker_api.handle_resolution_preparation(
        broker, first, peer_uid=broker.policy.allowed_uid))
    try:
        _, pending = await asyncio.wait({operation}, timeout=0.8)
        assert not pending, "slot admission outlived the expired worker lease"
        assert operation.result().state == "unknown"
        job = broker.executions[first.invocation.execution_id]
        assert job.finished.is_set() and job.quiet and job.task is None
        assert job.stage is None and not job.launched and not job.slot_acquired
        assert broker.slots._value == 0 and list(tmp_path.iterdir()) == []
        update = first.model_copy(update={"action": "renew_python_resolution", "claim": renewed(first.claim)})
        assert (await broker_api.handle_resolution_preparation(broker, update,
            peer_uid=broker.policy.allowed_uid)).state == "unknown"
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_expired_initial_request_never_registers_or_reads_configuration(tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    broker = waiting_broker(tmp_path, monkeypatch)
    reads: list[str] = []

    def read() -> SimpleNamespace:
        reads.append("root")
        return SimpleNamespace(stage_root=tmp_path)

    monkeypatch.setattr(broker_api, "read_root_configuration", read)
    first = request()
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})})
    operation = asyncio.create_task(broker_api.handle_resolution_preparation(
        broker, first, peer_uid=broker.policy.allowed_uid))
    try:
        _, pending = await asyncio.wait({operation}, timeout=0.3)
        assert not pending, "expired initial authority entered admission"
        assert operation.result().state == "unknown"
        assert reads == [] and broker.executions == {}
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("phase", ["slots", "admit"])
async def test_admission_renewal_reschedules_original_owner_timeout(tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    broker = waiting_broker(tmp_path, monkeypatch)
    admitted = asyncio.Event()

    async def admit(actual: ProjectExecutionBroker, job: _Execution) -> None:
        assert actual is broker and job.task is operation
        admitted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(broker_api, "_admit_unit", admit)
    if phase == "admit":
        broker.slots.release()
    first = request()
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.35)})})
    operation = asyncio.create_task(broker_api.handle_resolution_preparation(
        broker, first, peer_uid=broker.policy.allowed_uid))
    try:
        await asyncio.sleep(0.03)
        job = broker.executions[first.invocation.execution_id]
        assert isinstance(job, broker_api._ResolutionPreparationExecution)
        timer = job.lease_timeout
        assert timer is not None and job.task is operation
        assert admitted.is_set() == (phase == "admit")
        current = renewed(first.claim).model_copy(update={
            "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.6)})
        update = first.model_copy(update={"action": "renew_python_resolution", "claim": current})
        ack = await broker_api.handle_resolution_preparation(broker, update, peer_uid=broker.policy.allowed_uid)
        assert ack.state == "lease_renewed" and job.lease_timeout is timer
        assert timer.when() is not None and job.task_bound is not None
        assert cast(float, timer.when()) <= job.task_bound
        await asyncio.sleep(0.36)
        assert not operation.done() and job.stage is None
        _, pending = await asyncio.wait({operation}, timeout=0.8)
        assert not pending and operation.result().state == "unknown"
        assert job.finished.is_set() and job.quiet and job.lease_closed
        assert job.original_request == first and job.current_claim == current
        assert not job.launched and not job.slot_acquired and operation.cancelling() == 0
        assert broker.slots._value == (0 if phase == "slots" else 1)
        assert (await broker_api.handle_resolution_preparation(broker, update,
            peer_uid=broker.policy.allowed_uid)).state == "unknown"
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_renewal_cannot_extend_fixed_task_budget(tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    broker = waiting_broker(tmp_path, monkeypatch)
    first = request()
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.2),
        "original_deadline": datetime.now(UTC) + timedelta(seconds=0.4)})})
    operation = asyncio.create_task(broker_api.handle_resolution_preparation(
        broker, first, peer_uid=broker.policy.allowed_uid))
    try:
        await asyncio.sleep(0.03)
        job = broker.executions[first.invocation.execution_id]
        assert isinstance(job, broker_api._ResolutionPreparationExecution)
        fixed = job.task_bound
        update = first.model_copy(update={"action": "renew_python_resolution", "claim": renewed(first.claim)})
        assert (await broker_api.handle_resolution_preparation(broker, update,
            peer_uid=broker.policy.allowed_uid)).state == "lease_renewed"
        assert job.lease_timeout is not None and job.lease_timeout.when() is not None
        assert fixed is not None and cast(float, job.lease_timeout.when()) <= fixed
        _, pending = await asyncio.wait({operation}, timeout=0.8)
        assert not pending and operation.result().state == "unknown"
        assert job.task_bound == fixed and job.finished.is_set() and job.quiet
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("error", [BlockingIOError, InterruptedError])
def test_witness_contended_read_has_independent_short_io_deadline(filesystem: RootFS,
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]) -> None:
    first = request()
    # Keep RED bounded too: an incorrectly task-bound read takes about 0.9s,
    # rather than blocking this regression for an actual one-hour task budget.
    first = first.model_copy(update={"claim": first.claim.model_copy(update={
        "original_deadline": datetime.now(UTC) + timedelta(seconds=0.9)})})
    item = witness(first)
    item.initialize()
    original_raw = filesystem.nodes["/private/stage/resolution-lease.json"].data

    def occupied(fd: int, flags: int) -> None:
        assert fd in filesystem.fds and flags == 5
        raise error

    monkeypatch.setattr(sys.modules["fcntl"], "flock", occupied)
    started = time.monotonic()
    with pytest.raises(file_api.PythonDependencyIndexError, match="timeout"):
        item.current()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, "lease watchdog read waited for the task budget"
    assert filesystem.fds == {}
    assert filesystem.nodes["/private/stage/resolution-lease.json"].data == original_raw
