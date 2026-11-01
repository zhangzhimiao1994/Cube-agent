"""Offline Root-store contract; OS-only shim keeps native ownership out of synthetic claims."""

import importlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tests.unit.capabilities.test_python_dependency_resolution import resolve
from tests.unit.skills import test_python_registry_read as registry

offline_network = registry.offline_network


def api():
    return importlib.import_module(
        "agent_hub.skills.sandbox.python_resolution_protocol"
    ), importlib.import_module("agent_hub.skills.sandbox.python_resolution_store")


def claim():
    p, _ = api()
    from agent_hub.skills.sandbox.project import ProjectInvocation
    from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
        PythonMetadataDraft,
        PythonMetadataPlanningRequest,
        PythonMetadataScope,
    )

    return p.ResolutionClaim(
        planning=PythonMetadataPlanningRequest(
            invocation=ProjectInvocation(
                execution_id="resolution_test",
                tenant_id=UUID(int=1),
                run_id=UUID(int=2),
                project_id="project",
                session_id="session",
                sandbox_profile="restricted",
                timeout_seconds=30,
            ),
            scope=PythonMetadataScope(
                tenant_id=UUID(int=1),
                run_id=UUID(int=2),
                user_id=UUID(int=3),
                project_id="project",
                session_id="session",
                actor="implementer",
                actor_role="operator",
                request_sha256="1" * 64,
                permissions_sha256="4" * 64,
            ),
            draft=PythonMetadataDraft(
                source_sha256="2" * 64,
                acceptance_sha256="5" * 64,
                manifest_sha256="6" * 64,
                lock_sha256="7" * 64,
            ),
            expected_root_config_sha256="8" * 64,
            expected_catalog_sha256="3" * 64,
        ),
        worker_id="worker",
        lease_token=UUID(int=4),
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        original_deadline=datetime.now(UTC) + timedelta(seconds=25),
        monotonic_deadline=time.monotonic() + 25,
    )


async def data():
    wire = registry.Wire()
    row, raw = wire.release("demo")
    result = await resolve(wire, {row["filename"]: raw}, ["demo>=1"])
    return result, {row["filename"]: raw}


async def test_descriptor_wire_decoder_is_exact_and_never_a_receipt():
    p, _ = api()
    descriptor, _ = await data()
    assert p.decode_descriptor(descriptor.to_payload()) == descriptor
    forged = descriptor.to_payload() | {"approved": True}
    with pytest.raises(ValueError):
        p.decode_descriptor(forged)
    assert not hasattr(descriptor, "authority")


@pytest.mark.parametrize(
    "change", ["expired", "nan", "new_token", "other_actor", "permission", "base"]
)
def test_original_claim_rejects_extension_or_identity_drift(change):
    p, _ = api()
    first = claim()
    changes = {
        "expired": {"original_deadline": datetime.now(UTC) - timedelta(seconds=1)},
        "nan": {"monotonic_deadline": float("nan")},
        "new_token": {"lease_token": UUID(int=9)},
        "other_actor": {
            "planning": first.planning.model_copy(
                update={"scope": first.planning.scope.model_copy(update={"actor": "neighbor"})}
            )
        },
        "permission": {
            "planning": first.planning.model_copy(
                update={
                    "scope": first.planning.scope.model_copy(
                        update={"permissions_sha256": "a" * 64}
                    )
                }
            )
        },
        "base": {
            "planning": first.planning.model_copy(update={"expected_catalog_sha256": "b" * 64})
        },
    }
    with pytest.raises(ValueError):
        newer = p.ResolutionClaim.model_validate(vars(first) | changes[change], strict=True)
        newer.require_continuity(first)


async def test_run_overlay_uses_verified_descriptor_and_refuses_catalog_collision():
    _, s = api()
    from pathlib import Path

    from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig

    descriptor, _ = await data()
    entry = descriptor.wheels[0].candidate.download
    base = RootSupplyConfig(
        Path("/var/lib/agent-hub-python-input-supply"), (entry,), 64 * 1024**2, 0, 30
    )
    assert s.overlay_supply(base, descriptor).catalog == (entry,)
    changed = entry.__class__(
        entry.filename, entry.name, entry.version, "a" * 64, entry.size_bytes, entry.url
    )
    conflict = RootSupplyConfig(base.state_root, (changed,), base.max_total_bytes, 0, 30)
    with pytest.raises(ValueError):
        s.overlay_supply(conflict, descriptor)


async def test_freeze_changes_source_without_reissuing_data_or_deadline():
    _, s = api()
    descriptor, _ = await data()
    original = claim()
    newer = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={
                    "draft": original.planning.draft.model_copy(
                        update={"source_sha256": "a" * 64, "lock_sha256": "b" * 64}
                    )
                }
            )
        }
    )
    newer.require_continuity(original)
    plan = s.freeze_plan(descriptor, original=original, current=newer, overlay_sha256="c" * 64)
    assert plan.claim.planning.draft.source_sha256 == "a" * 64
    assert plan.descriptor_sha256 == descriptor.sha256
    assert plan.claim.original_deadline == original.original_deadline
    assert descriptor.scope.source_sha256 == "2" * 64
    assert (
        plan.sha256
        != s.freeze_plan(
            descriptor, original=original, current=original, overlay_sha256="c" * 64
        ).sha256
    )


async def test_unknown_claim_is_inspect_only_even_after_new_source(monkeypatch):
    p, s = api()
    # This test replaces only the privileged FD boundary; state machine/serialization execute.
    files = {}

    def create(name, value, deadline):
        if name in files:
            raise FileExistsError(name)
        files[name] = json.dumps(value)

    def read(name, deadline):
        if name not in files:
            raise FileNotFoundError(name)
        return json.loads(files[name])

    store = s.ResolutionStore.__new__(s.ResolutionStore)
    monkeypatch.setattr(store, "_create", create)
    monkeypatch.setattr(store, "_read", read)
    first = claim()
    key = p.resolution_key(
        first, (registry.api()[0].DependencyNeed("demo>=1", "a" * 64),), registry.environment()
    )
    store.begin(key, first)
    assert store.inspect(key, first).state == "unknown"
    with pytest.raises(ValueError, match="consumed"):
        store.begin(key, first)
    assert len(files) == 1


async def test_transport_ids_and_heartbeat_do_not_mint_a_different_action():
    _, s = api()
    descriptor, _ = await data()
    original = claim()
    changed = original.model_copy(
        update={
            "lease_expires_at": original.lease_expires_at + timedelta(seconds=1),
            "planning": original.planning.model_copy(
                update={
                    "invocation": original.planning.invocation.model_copy(
                        update={"execution_id": "fresh_transport"}
                    )
                }
            ),
        }
    )
    first = s.freeze_plan(descriptor, original=original, current=original, overlay_sha256="c" * 64)
    second = s.freeze_plan(descriptor, original=original, current=changed, overlay_sha256="c" * 64)
    assert first.sha256 == second.sha256


def test_new_monotonic_cap_must_not_extend_original():
    api()
    original = claim()
    with pytest.raises(ValueError):
        original.model_copy(
            update={"monotonic_deadline": original.monotonic_deadline + 5}
        ).require_continuity(original)


async def test_stale_source_publication_cannot_use_data_rebind(monkeypatch):
    p, s = api()
    descriptor, _ = await data()
    original = claim()
    files = {}
    store = s.ResolutionStore.__new__(s.ResolutionStore)

    def create(name, value, deadline):
        if name in files:
            raise FileExistsError
        files[name] = json.loads(json.dumps(value))

    monkeypatch.setattr(store, "_create", create)
    monkeypatch.setattr(store, "_read", lambda name, deadline: files[name])
    key = p.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store.begin(key, original)
    plan = s.freeze_plan(descriptor, original=original, current=original, overlay_sha256="c" * 64)
    store._create(
        store._name(key, "plan-" + plan.sha256), plan.model_dump(mode="json"), original.deadline
    )
    fresh = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={
                    "draft": original.planning.draft.model_copy(update={"source_sha256": "d" * 64})
                }
            )
        }
    )
    with pytest.raises(ValueError, match="source"):
        store.require_plan(key, fresh, plan.sha256)


@pytest.fixture
def root_store(monkeypatch):
    import sys
    from pathlib import Path
    from types import ModuleType, SimpleNamespace

    from agent_hub.skills.sandbox import project_contract_broker as contracts
    from agent_hub.skills.sandbox import python_dependency_index_store as indexes
    from agent_hub.skills.sandbox import python_input_supply_catalog as catalog
    from agent_hub.skills.sandbox import python_input_supply_worker as supply_worker
    from tests.unit.skills.test_python_dependency_index_store import FileSystem

    _, s = api()
    fs = FileSystem()
    fs.O_WRONLY = 1
    import posixpath
    from contextlib import contextmanager

    @contextmanager
    def scandir(parent):
        directory = fs.fds[parent][1]
        fs.events.append(("scandir", directory))
        yield (SimpleNamespace(name=posixpath.basename(path))
               for path in tuple(fs.nodes) if posixpath.dirname(path) == directory)

    fs.scandir = scandir
    for module in (s, indexes, catalog, supply_worker, contracts):
        monkeypatch.setattr(module, "os", fs)
    monkeypatch.setattr(indexes, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(s, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(supply_worker, "sys", SimpleNamespace(platform="linux"))
    lock = ModuleType("fcntl")
    lock.LOCK_SH, lock.LOCK_EX, lock.LOCK_NB, lock.flock = 1, 2, 4, fs.flock
    monkeypatch.setitem(sys.modules, "fcntl", lock)
    entry, raw = registry.wheel("base")
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload

    base = WheelDownload(
        entry["filename"], "base", "1.0", entry["digests"]["sha256"], entry["size"], entry["url"]
    )
    supply = catalog.RootSupplyConfig(Path("/private/state"), (base,), 64 * 1024**2, 0, 30)
    result = SimpleNamespace(fs=fs, supply=supply, store=s.ResolutionStore(supply), base_raw=raw)
    yield result
    assert fs.fds == {}
    assert not fs.locks


async def test_real_once_serialization_and_wheel_cache_survive_new_source(root_store):
    p, _ = api()
    descriptor, bodies = await data()
    first = claim()
    first = first.model_copy(
        update={
            "planning": first.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    from dataclasses import replace

    descriptor = replace(descriptor, scope=first.resolution_scope())
    key = p.resolution_key(first, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    assert store.inspect(key, first).state == "absent"
    store.begin(key, first)
    assert store.inspect(key, first).state == "unknown"
    for wheel in descriptor.wheels:
        e = wheel.candidate.download
        store.put_wheel(key, e.sha256, bodies[e.filename], deadline=first.deadline)
        assert (
            store.wheel(key, e.sha256, e.size_bytes, deadline=first.deadline) == bodies[e.filename]
        )
    store.complete(key, first, descriptor)
    original_bytes = {name: node.data for name, node in root_store.fs.nodes.items()}
    fresh = first.model_copy(
        update={
            "planning": first.planning.model_copy(
                update={
                    "draft": first.planning.draft.model_copy(
                        update={"source_sha256": "d" * 64, "lock_sha256": "e" * 64}
                    )
                }
            )
        }
    )
    plan = store.freeze(key, fresh)
    assert plan.claim.planning.draft.source_sha256 == "d" * 64
    assert plan.overlay_sha256 != root_store.supply.digest
    assert store.require_plan(key, fresh, plan.sha256).sha256 == plan.sha256
    assert all(root_store.fs.nodes[name].data == raw for name, raw in original_bytes.items())
    with pytest.raises(ValueError, match="consumed"):
        store.begin(key, fresh)
    store.publication_begin(key, fresh, plan)
    assert store.publication_status(key, fresh, plan) == "unknown"
    with pytest.raises(ValueError, match="consumed"):
        store.publication_begin(key, fresh, plan)
    store.publication_finish(key, fresh, plan, "a" * 64)
    assert store.publication_status(key, fresh, plan) == "published"


@pytest.mark.parametrize("attack", ["symlink", "owner", "mode", "hardlink", "changed_bytes"])
async def test_root_receipt_tampering_is_rejected_before_overlay(root_store, attack):
    import stat

    p, _ = api()
    descriptor, _ = await data()
    original = claim()
    key = p.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    name = "/private/state/" + store._name(key, "receipt")
    node = root_store.fs.nodes[name]
    if attack == "symlink":
        node.mode = stat.S_IFLNK | 0o600
    if attack == "owner":
        node.uid = 10001
    if attack == "mode":
        node.mode = stat.S_IFREG | 0o644
    if attack == "hardlink":
        node.nlink = 2
    if attack == "changed_bytes":
        node.data = node.data.replace(
            b'"source_sha256":"' + b"2" * 64, b'"source_sha256":"' + b"9" * 64
        )
    with pytest.raises((ValueError, OSError)):
        store.freeze(key, original)


@pytest.fixture
def worker_case(root_store, monkeypatch):
    import stat
    from contextlib import contextmanager
    from types import SimpleNamespace

    from tests.unit.skills.test_python_dependency_index_store import root_configuration

    worker = importlib.import_module("agent_hub.skills.sandbox.python_resolution_worker")
    p, _ = api()
    root = root_configuration()
    env = registry.environment() | {
        "python_full_version": "3.12.4",
        "implementation_version": "3.12.4",
    }
    wire = registry.Wire()
    row, raw = wire.release("demo", deps=("helper>=1",))
    row2, raw2 = wire.release("helper")
    bodies = {row["filename"]: raw, row2["filename"]: raw2}
    original = claim()
    planning = original.planning.model_copy(
        update={
            "expected_root_config_sha256": p.digest(root.to_payload()),
            "expected_catalog_sha256": root_store.supply.digest,
        }
    )
    original = original.model_copy(update={"planning": planning})
    request = p.ResolutionRequest(
        action="resolve_python_inputs",
        claim=original,
        requirements=(("demo>=1", "a" * 64),),
        environment=env,
    )
    case = SimpleNamespace(
        worker=worker,
        request=request,
        root=root,
        supply=root_store.supply,
        events=[],
        wire=wire,
        bodies=bodies,
        locked=False,
        fail=None,
        fs=root_store.fs,
        store=root_store.store,
    )

    async def source(*args):
        case.events.append("source")
        if case.fail == "source":
            raise ValueError("source changed")

    def controls(*args):
        if case.fail == "controls":
            raise ValueError("controls changed")

    async def fetch(entries, invocation, policy, root, supply, deadline):
        assert not case.locked
        assert deadline <= original.monotonic_deadline
        case.events.append("fetch")
        if case.fail == "download":
            raise ValueError("fetch failed")
        return {e.filename: case.bodies[e.filename] for e in entries}

    @contextmanager
    def native_claim(*args, **kwargs):
        yield 90, []

    @contextmanager
    def admission(*args):
        assert not case.locked
        case.locked = True
        try:
            yield
        finally:
            case.locked = False

    def publish(cache, claim_path, entry, raw, *, deadline):
        assert case.locked
        case.events.append("publish")
        from agent_hub.skills.sandbox.python_dependency_metadata import (
            extract_python_wheel_metadata,
        )

        extract_python_wheel_metadata(entry, raw, deadline=deadline)
        case.fs.add(cache.as_posix() + "/" + entry.filename, stat.S_IFREG | 0o444, raw)

    monkeypatch.setattr(worker, "read_root_configuration", lambda: root)
    monkeypatch.setattr(worker, "read_supply_configuration", lambda: case.supply)
    monkeypatch.setattr(worker, "validate_configuration", lambda *args: None)
    monkeypatch.setattr(worker, "_source", source)
    monkeypatch.setattr(worker, "_controls", controls)
    monkeypatch.setattr(worker, "RegistryReader", lambda *args, **kwargs: registry.reader(wire))
    monkeypatch.setattr(worker, "fetch_catalog_wheels", fetch)
    monkeypatch.setattr(
        worker, "shutil", SimpleNamespace(disk_usage=lambda path: SimpleNamespace(free=2**40))
    )
    monkeypatch.setattr(worker, "_claim", native_claim)
    monkeypatch.setattr(worker, "_root", lambda *a, **k: 91)
    monkeypatch.setattr(worker, "_admission", admission)
    monkeypatch.setattr(worker, "publish_verified_wheel", publish)
    case.fs.add(
        "/private/wheels/" + case.supply.catalog[0].filename,
        stat.S_IFREG | 0o444,
        root_store.base_raw,
    )
    return case


async def test_actual_root_resolver_freeze_publication_and_complete_overlay_index(worker_case):
    c = worker_case
    resolved = await c.worker.execute_resolution(
        c.request, policy=None, root=c.root, supply=c.supply
    )
    assert resolved.state == "resolved"
    assert len(c.wire.requests) == 4 and c.events.count("fetch") == 2
    frozen = await c.worker.execute_resolution(
        c.request.model_copy(update={"action": "freeze_python_resolution"}),
        policy=None,
        root=c.root,
        supply=c.supply,
    )
    assert frozen.state == "frozen" and frozen.plan is not None
    request = c.request.model_copy(
        update={"action": "publish_python_resolution", "expected_plan_sha256": frozen.plan.sha256}
    )
    response = await c.worker.execute_resolution(request, policy=None, root=c.root, supply=c.supply)
    assert response.state == "published" and c.events.count("publish") == 2
    from agent_hub.skills.sandbox import python_dependency_index_store as indexes

    p, s = api()
    effective = s.overlay_supply(c.supply, p.decode_descriptor(resolved.descriptor))
    actual = indexes.read_dependency_metadata_index(effective, deadline=c.request.claim.deadline)
    assert {m.wheel.name for m in actual.records} == {"base", "demo", "helper"}
    inspected = await c.worker.execute_resolution(
        request.model_copy(update={"action": "inspect_python_resolution"}),
        policy=None,
        root=c.root,
        supply=c.supply,
    )
    assert inspected.state == "published"
    assert len(c.wire.requests) == 4


@pytest.mark.parametrize("fault", ["source", "controls", "download"])
async def test_root_changed_inputs_and_unknown_never_publish_or_refetch(worker_case, fault):
    c = worker_case
    c.fail = fault
    with pytest.raises(ValueError):
        await c.worker.execute_resolution(c.request, policy=None, root=c.root, supply=c.supply)
    assert "publish" not in c.events
    if fault == "download":
        c.fail = None
        inspected = await c.worker.execute_resolution(
            c.request.model_copy(update={"action": "inspect_python_resolution"}),
            policy=None,
            root=c.root,
            supply=c.supply,
        )
        assert inspected.state == "unknown"
        reads = len(c.wire.requests)
        with pytest.raises(ValueError, match="consumed"):
            await c.worker.execute_resolution(c.request, policy=None, root=c.root, supply=c.supply)
        assert len(c.wire.requests) == reads


async def test_fetch_caller_cancel_drains_exact_child_before_return(worker_case, monkeypatch):
    import asyncio

    c = worker_case
    entered, closed = asyncio.Event(), asyncio.Event()

    async def waiting(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            closed.set()

    monkeypatch.setattr(c.worker, "fetch_catalog_wheels", waiting)
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload

    row, raw = registry.wheel("test")
    e = WheelDownload(
        row["filename"], "test", "1.0", row["digests"]["sha256"], len(raw), row["url"]
    )
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(
        c.worker._fetch(e, c.request, None, c.root, c.supply, c.request.claim.deadline)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert set(asyncio.all_tasks()) == before


async def test_v11_dispatch_is_reachable_through_existing_root_peer_broker(monkeypatch):
    import asyncio
    import struct
    from types import SimpleNamespace

    broker = importlib.import_module("agent_hub.skills.sandbox.broker")
    worker = importlib.import_module("agent_hub.skills.sandbox.python_resolution_worker")
    p, _ = api()
    request = p.ResolutionRequest(
        action="inspect_python_resolution",
        claim=claim(),
        requirements=(("demo", "a" * 64),),
        environment=registry.environment(),
    )
    called = []

    async def handle(project, request, *, peer_uid):
        called.append(peer_uid)
        return p.ResolutionStatus(state="unknown", key=request.key)

    monkeypatch.setattr(worker, "handle_resolution", handle)
    monkeypatch.setattr(broker, "_peer_uid", lambda writer: 10001)

    class Writer:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    writer = Writer()
    reader = asyncio.StreamReader()
    raw = request.model_dump_json().encode()
    reader.feed_data(struct.pack("!I", len(raw)) + raw)
    reader.feed_eof()
    await broker._serve_client(
        reader,
        writer,
        SimpleNamespace(_policy=SimpleNamespace(allowed_uid=10001), _project_broker=object()),
    )
    assert called == [10001]
    assert json.loads(writer.data[4:])["key"] == request.key and writer.closed




def test_publication_validator_preserves_original_absolute_deadline(monkeypatch):
    publisher = importlib.import_module("agent_hub.skills.sandbox.python_input_publish")
    fetch = importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")
    row, raw = registry.wheel("bounded")
    e = fetch.WheelDownload(
        row["filename"], "bounded", "1.0", row["digests"]["sha256"], len(raw), row["url"]
    )
    deadline = time.monotonic() + 1
    observed = []
    original = fetch.validate_wheel_bytes

    def validate(entry, data, *, deadline=None):
        observed.append(deadline)
        original(entry, data, deadline=deadline)

    monkeypatch.setattr(fetch, "validate_wheel_bytes", validate)
    publisher._validate(e, raw, deadline)
    assert observed == [deadline]


async def test_complete_index_extraction_preserves_original_deadline(worker_case, monkeypatch):
    c = worker_case
    indexes = importlib.import_module("agent_hub.skills.sandbox.python_dependency_index_store")
    observed = []
    original = indexes.extract_python_wheel_metadata

    def extract(entry, raw, *, deadline=None):
        observed.append(deadline)
        return original(entry, raw, deadline=deadline)

    monkeypatch.setattr(indexes, "extract_python_wheel_metadata", extract)
    deadline = c.request.claim.deadline
    indexes.publish_dependency_metadata_index(c.root, c.supply, deadline=deadline)
    assert observed and all(value == deadline for value in observed)


async def test_failed_fetch_drain_preserves_unknown_instead_of_quiet(worker_case, monkeypatch):
    import asyncio

    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    c = worker_case
    entered = asyncio.Event()

    async def broken(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")

    monkeypatch.setattr(c.worker, "fetch_catalog_wheels", broken)
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload

    row, raw = registry.wheel("test")
    e = WheelDownload(
        row["filename"], "test", "1.0", row["digests"]["sha256"], len(raw), row["url"]
    )
    task = asyncio.create_task(
        c.worker._fetch(e, c.request, None, c.root, c.supply, c.request.claim.deadline)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(ProcessCleanupIncomplete):
        await task


async def test_sdk_cancel_invokes_existing_exact_project_termination(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agent_hub.skills.sandbox import project
    from agent_hub.skills.sandbox import python_metadata_preparation_client as peer

    worker = importlib.import_module("agent_hub.skills.sandbox.python_resolution_worker")
    p, _ = api()
    request = p.ResolutionRequest(
        action="inspect_python_resolution",
        claim=claim(),
        requirements=(("demo", "a" * 64),),
        environment=registry.environment(),
    )
    entered = asyncio.Event()
    terminated = []

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            entered.set()

        def close(self):
            pass

        async def wait_closed(self):
            pass

    reader = asyncio.StreamReader()

    async def connect(*args):
        return reader, Writer()

    async def terminate(self, key):
        terminated.append(key)

    monkeypatch.setattr(project, "_systemd_supported", lambda: True)
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    monkeypatch.setattr(peer, "_root_peer", lambda writer: None)
    monkeypatch.setattr(project.ProjectSystemdSandbox, "terminate", terminate)
    task = asyncio.create_task(worker.request_resolution(request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert terminated == [request.claim.planning.invocation.execution_id]
