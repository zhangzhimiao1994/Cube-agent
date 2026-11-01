"""Real v11/controller/Root-resolver composition; only native syscall/process edges simulate."""

import asyncio
import re
import stat
import sys
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Never, Unpack

import pytest

from agent_hub.skills.sandbox.base import ProcessLimits, SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, ProjectInvocation, _Execution
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionRequest, ResolutionStatus
from agent_hub.skills.sandbox.python_resolution_store import ResolutionLeaseWitness, ResolutionStore
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills import test_python_registry_fetch as fetch_tests
from tests.unit.skills.resolution_overlay_process_typed_fixture import (
    NetworkCase,
    ProcessNotice,
    RunOptions,
    Stage,
    get_worker_fetch,
    present,
)

network_case = fetch_tests.case
root_store = fetch_tests.root_store
offline_network = fetch_tests.offline_network

in_root_unit = ContextVar("g1_writable_root_unit", default=False)


@pytest.fixture
async def process_case(network_case: NetworkCase, monkeypatch: pytest.MonkeyPatch) -> NetworkCase:
    c = network_case
    from agent_hub.skills.sandbox import python_metadata_preparation_broker as api

    c.manager = ProjectExecutionBroker(c.policy, slots=asyncio.Semaphore(1))
    c.root_events = []
    c.entered = asyncio.Event()
    c.failure = None

    c.stage = Stage(
        c.root.stage_root / (c.request.claim.planning.invocation.execution_id + "-abcdefgh")
    )
    # Model the actual private stage, not a replacement witness implementation.
    # Permanent supply-state IO retains the existing simulator's stricter guards.
    stage_path = c.stage.as_posix()
    for path in (*reversed(PurePosixPath(stage_path).parents), PurePosixPath(stage_path)):
        if path.as_posix() not in c.fs.nodes:
            c.fs.add(path.as_posix(), stat.S_IFDIR | 0o700)
    original_flock, original_unlink = c.fs.flock, c.fs.unlink

    def flock(fd: int, flags: int) -> None:
        if c.fs.fds[fd][1] != stage_path:
            return original_flock(fd, flags)
        assert flags in {5, 6}
        c.fs.locks[fd] = flags & 3

    def unlink(name: str, *, dir_fd: int) -> None:
        if c.fs.fds[dir_fd][1] != stage_path:
            return original_unlink(name, dir_fd=dir_fd)
        assert re.fullmatch(r"[.]resolution-lease-[a-f0-9]{32}", name)
        assert c.fs.locks.get(dir_fd) == 2
        node = c.fs.nodes.pop(c.fs.path(name, dir_fd))
        node.nlink -= 1
        node.ctime += 1

    monkeypatch.setattr(sys.modules["fcntl"], "flock", flock)
    monkeypatch.setattr(c.fs, "unlink", unlink)
    c.fs.supports_dir_fd = c.fs.supports_dir_fd | {unlink}

    async def admit(manager: ProjectExecutionBroker, job: _Execution) -> None:
        c.root_events.append(("admit", job.unit))

    async def kill(job: _Execution) -> None:
        c.root_events.append(("kill", job.unit))

    async def cleanup(job: _Execution) -> None:
        c.root_events.append(("cleanup", job.unit))
        job.quiet = True

    def metadata(job: _Execution, state: str) -> None:
        c.root_events.append(("stage", state))

    def write(stage: Path, request: ResolutionRequest) -> None:
        assert not in_root_unit.get()
        assert stage == c.stage
        c.staged = request
        c.root_events.append(("request", str(stage)))

    store_type = type(c.store)
    original_create = store_type._create

    def create(self: ResolutionStore, name: str, value: object, deadline: float) -> None:
        assert in_root_unit.get(), "long-lived readonly broker attempted Root state write"
        return original_create(self, name, value, deadline)

    monkeypatch.setattr(store_type, "_create", create)

    async def run(
        command: tuple[str, ...], limits: ProcessLimits, **kwargs: Unpack[RunOptions]
    ) -> SkillResult:
        assert not in_root_unit.get()
        assert command[-3:] == (
            "agent_hub.skills.sandbox.python_resolution_worker",
            "--stage",
            c.stage.as_posix(),
        )
        assert "RestrictAddressFamilies=AF_UNIX" in command
        writable = [p for p in command if p.startswith("ReadWritePaths=")]
        assert writable == [
            "ReadWritePaths="
            + " ".join(
                p.as_posix() for p in (c.root.stage_root, c.root.wheel_root, c.supply.state_root)
            )
        ]
        assert "User=root" in command and "ProtectSystem=strict" in command
        c.root_events.append(("root-spawn", limits.timeout_seconds))
        present(kwargs["process_started"])(ProcessNotice(None))
        token = in_root_unit.set(True)
        try:
            try:
                status = await c.worker.execute_resolution(
                    c.staged,
                    policy=c.policy,
                    root=c.root,
                    supply=c.supply,
                    lease_witness=ResolutionLeaseWitness(c.stage, c.staged),
                )
                return SkillResult(
                    exit_code=0, stdout=status.model_dump_json(), stderr="", timed_out=False
                )
            except ProcessCleanupIncomplete:
                return SkillResult(
                    exit_code=3, stdout="", stderr="resolution_cleanup_incomplete", timed_out=False
                )
            except ValueError:
                return SkillResult(
                    exit_code=2, stdout="", stderr="resolution_unavailable", timed_out=False
                )
        finally:
            in_root_unit.reset(token)

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "_private_project_runtime", lambda path: None)
    monkeypatch.setattr(api, "read_root_configuration", lambda: c.root)
    monkeypatch.setattr(api, "read_supply_configuration", lambda: c.supply)
    monkeypatch.setattr(api, "validate_configuration", lambda *args: None)
    monkeypatch.setattr(api, "_write_request", write)
    monkeypatch.setattr(api, "Path", Stage)
    monkeypatch.setattr(api, "tempfile", SimpleNamespace(mkdtemp=lambda **kwargs: str(c.stage)))
    monkeypatch.setattr(api, "_admit_unit", admit)
    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    monkeypatch.setattr(c.manager, "_kill_and_confirm", kill)
    monkeypatch.setattr(c.manager, "_cleanup", cleanup)
    monkeypatch.setattr(c.manager, "_metadata", metadata)
    return c


async def handle_bounded(
    c: NetworkCase, request: ResolutionRequest | None = None
) -> ResolutionStatus:
    result = await asyncio.wait_for(
        c.worker.handle_resolution(
            c.manager, c.request if request is None else request, peer_uid=c.policy.allowed_uid
        ),
        30,
    )
    assert isinstance(result, ResolutionStatus)
    return result


async def require_entered(task: asyncio.Task[ResolutionStatus], entered: asyncio.Event) -> None:
    waiter = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait({task, waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if not entered.is_set():
            if task.done():
                result = await task
                raise AssertionError("fetch never entered; early broker result: " + result.state)
            raise AssertionError("fetch entry wait exceeded 2 seconds")
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_readonly_af_unix_broker_delegates_writes_to_exact_root_unit(
    process_case: NetworkCase,
) -> None:
    c = process_case
    status = await handle_bounded(c)
    assert status.state == "resolved"
    assert any(p[0] == "root-spawn" for p in c.root_events) and not c.root_dns
    assert len([p for p in c.root_events if p[0] == "kill"]) == 2
    assert c.manager.slots._value == 1
    job = c.manager.executions[c.request.claim.planning.invocation.execution_id]
    assert job.finished.is_set() and job.quiet


@pytest.mark.parametrize("failure", ["normal", "cancel", "expiry", "ordinary"])
async def test_actual_controller_resolver_fetch_preserves_uncertain_cleanup(
    process_case: NetworkCase, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import time

    c = process_case

    async def fetch(*args: object) -> Never:
        c.entered.set()
        if failure == "ordinary":
            raise ValueError("known fetch failure")
        if failure == "normal":
            raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")
        try:
            await asyncio.Future[None]()
        finally:
            raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")

    monkeypatch.setattr(c.worker, "fetch_catalog_wheels", fetch)
    request = c.request
    if failure == "expiry":
        request = request.model_copy(
            update={
                "claim": request.claim.model_copy(
                    update={"monotonic_deadline": time.monotonic() + 0.1}
                )
            }
        )
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(handle_bounded(c, request))
    if failure == "cancel":
        await require_entered(task, c.entered)
        task.cancel()
    result = await task
    job = c.manager.executions[request.claim.planning.invocation.execution_id]
    assert result.state == "unknown" and job.finished.is_set()
    if failure == "expiry":
        # Evidence-only read of the synthetic Root filesystem, never renew the expired grant.
        prefix = c.supply.state_root.as_posix() + "/"
        assert prefix + c.store._name(request.key, "intent") in c.fs.nodes
        assert prefix + c.store._name(request.key, "receipt") not in c.fs.nodes
    else:
        assert c.store.inspect(request.key, request.claim).state == "unknown"
    if failure == "ordinary":
        assert job.quiet and c.manager.slots._value == 1
    else:
        assert not job.quiet and job.slot_acquired and c.manager.slots.locked()
        assert ("stage", "UNKNOWN") in c.root_events
    assert set(asyncio.all_tasks()) == before


async def test_existing_execution_registry_owns_v11_and_rejects_duplicate_id(
    process_case: NetworkCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = process_case
    release = asyncio.Event()
    entered = asyncio.Event()
    original = get_worker_fetch()

    async def hold(
        entries: tuple[WheelDownload, ...],
        invocation: ProjectInvocation,
        policy: BrokerPolicy,
        root: RootConfig,
        supply: RootSupplyConfig,
        deadline: float,
    ) -> dict[str, bytes]:
        entered.set()
        await release.wait()
        return await original(entries, invocation, policy, root, supply, deadline)

    monkeypatch.setattr(c.worker, "fetch_catalog_wheels", hold)
    first = asyncio.create_task(handle_bounded(c))
    await require_entered(first, entered)
    try:
        second = await handle_bounded(c)
        assert second.state == "unknown"
        assert len([r for r in c.root_events if r[0] == "root-spawn"]) == 1
    finally:
        release.set()
        await first
    job = c.manager.executions[c.request.invocation.execution_id]
    assert job.finished.is_set() and job.quiet and c.manager.slots._value == 1


async def test_uncertain_native_drain_keeps_slot_and_returns_inspect_only(
    process_case: NetworkCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = process_case
    from agent_hub.skills.sandbox import python_metadata_preparation_broker as api

    async def exit3(
        command: tuple[str, ...], limits: ProcessLimits, **kwargs: Unpack[RunOptions]
    ) -> SkillResult:
        present(kwargs["process_started"])(ProcessNotice(3))
        return SkillResult(
            exit_code=3, stdout="", stderr="resolution_cleanup_incomplete", timed_out=False
        )

    monkeypatch.setattr(api, "run_subprocess_with_limits", exit3)
    result = await handle_bounded(c)
    job = c.manager.executions[c.request.invocation.execution_id]
    assert result.state == "unknown" and job.finished.is_set() and not job.quiet
    assert job.slot_acquired and c.manager.slots.locked()
    assert ("stage", "UNKNOWN") in c.root_events
    assert len([r for r in c.root_events if r[0] == "kill"]) == 2
