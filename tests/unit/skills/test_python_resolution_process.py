"""Real v11/controller/Root-resolver composition; only native syscall/process edges simulate."""

import asyncio
import importlib
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectExecutionBroker
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills import test_python_registry_fetch as fetch_tests

network_case = fetch_tests.case
root_store = fetch_tests.root_store
offline_network = fetch_tests.offline_network

in_root_unit = ContextVar("g1_writable_root_unit", default=False)


@pytest.fixture
async def process_case(network_case, monkeypatch):
    c = network_case
    api = importlib.import_module("agent_hub.skills.sandbox.python_metadata_preparation_broker")
    c.manager = ProjectExecutionBroker(c.policy, slots=asyncio.Semaphore(1))
    c.root_events = []
    c.entered = asyncio.Event()
    c.failure = None

    class Stage(type(Path())):
        def chmod(self, *args, **kwargs):
            pass

    c.stage = Stage(
        c.root.stage_root / (c.request.claim.planning.invocation.execution_id + "-abcdefgh")
    )

    async def admit(manager, job):
        c.root_events.append(("admit", job.unit))

    async def kill(job):
        c.root_events.append(("kill", job.unit))

    async def cleanup(job):
        c.root_events.append(("cleanup", job.unit))
        job.quiet = True

    def metadata(job, state):
        c.root_events.append(("stage", state))

    def write(stage, request):
        assert not in_root_unit.get()
        assert stage == c.stage
        c.staged = request
        c.root_events.append(("request", str(stage)))

    store_type = type(c.store)
    original_create = store_type._create

    def create(self, *args):
        assert in_root_unit.get(), "long-lived readonly broker attempted Root state write"
        return original_create(self, *args)

    monkeypatch.setattr(store_type, "_create", create)

    async def run(command, limits, **kwargs):
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
        kwargs["process_started"](SimpleNamespace(returncode=None))
        token = in_root_unit.set(True)
        try:
            try:
                status = await c.worker.execute_resolution(
                    c.staged, policy=c.policy, root=c.root, supply=c.supply
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


async def test_readonly_af_unix_broker_delegates_writes_to_exact_root_unit(process_case):
    c = process_case
    status = await c.worker.handle_resolution(c.manager, c.request, peer_uid=c.policy.allowed_uid)
    assert status.state == "resolved"
    assert any(p[0] == "root-spawn" for p in c.root_events) and not c.root_dns
    assert len([p for p in c.root_events if p[0] == "kill"]) == 2
    assert c.manager.slots._value == 1
    job = c.manager.executions[c.request.claim.planning.invocation.execution_id]
    assert job.finished.is_set() and job.quiet


@pytest.mark.parametrize("failure", ["normal", "cancel", "expiry", "ordinary"])
async def test_actual_controller_resolver_fetch_preserves_uncertain_cleanup(
    process_case, monkeypatch, failure
):
    import time

    c = process_case

    async def fetch(*args):
        c.entered.set()
        if failure == "ordinary":
            raise ValueError("known fetch failure")
        if failure == "normal":
            raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")
        try:
            await asyncio.Future()
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
    task = asyncio.create_task(
        c.worker.handle_resolution(c.manager, request, peer_uid=c.policy.allowed_uid)
    )
    if failure == "cancel":
        waiter = asyncio.create_task(c.entered.wait())
        try:
            await asyncio.wait({task, waiter}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
            if not c.entered.is_set():
                await task
                raise AssertionError("native execution never entered fetch")
            task.cancel()
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
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
    process_case, monkeypatch
):
    c = process_case
    release = asyncio.Event()
    entered = asyncio.Event()
    original = c.worker.fetch_catalog_wheels

    async def hold(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(c.worker, "fetch_catalog_wheels", hold)
    first = asyncio.create_task(
        c.worker.handle_resolution(c.manager, c.request, peer_uid=c.policy.allowed_uid)
    )
    await asyncio.wait_for(entered.wait(), 1)
    try:
        second = await c.worker.handle_resolution(
            c.manager, c.request, peer_uid=c.policy.allowed_uid
        )
        assert second.state == "unknown"
        assert len([r for r in c.root_events if r[0] == "root-spawn"]) == 1
    finally:
        release.set()
        await first
    job = c.manager.executions[c.request.invocation.execution_id]
    assert job.finished.is_set() and job.quiet and c.manager.slots._value == 1


async def test_uncertain_native_drain_keeps_slot_and_returns_inspect_only(
    process_case, monkeypatch
):
    c = process_case
    api = importlib.import_module("agent_hub.skills.sandbox.python_metadata_preparation_broker")

    async def exit3(command, limits, **kwargs):
        kwargs["process_started"](SimpleNamespace(returncode=3))
        return SkillResult(
            exit_code=3, stdout="", stderr="resolution_cleanup_incomplete", timed_out=False
        )

    monkeypatch.setattr(api, "run_subprocess_with_limits", exit3)
    result = await c.worker.handle_resolution(c.manager, c.request, peer_uid=c.policy.allowed_uid)
    job = c.manager.executions[c.request.invocation.execution_id]
    assert result.state == "unknown" and job.finished.is_set() and not job.quiet
    assert job.slot_acquired and c.manager.slots.locked()
    assert ("stage", "UNKNOWN") in c.root_events
    assert len([r for r in c.root_events if r[0] == "kill"]) == 2
