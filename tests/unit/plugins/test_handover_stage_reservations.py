"""Cooperative filesystem evidence; Windows has no directory durability credit."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest

TENANT = UUID("11111111-1111-4111-8111-111111111111")
DIGEST = "b" * 64
# Independent accounting: source + journal + record + per-node metadata.
MEMBERS = 2 + 4096 + 9
BYTES = 5 + 16 * 1024 * 1024 + 16384 + MEMBERS * 4096
BASE_MEMBERS = 4
BASE_BYTES = 32768


def module() -> Any:
    name = "agent_hub.plugins.handover_stage_reservations"
    assert importlib.util.find_spec(name) is not None, "reservation module is missing"
    return importlib.import_module(name)


def private_directory(path: Path) -> None:
    if sys.platform == "win32":
        from agent_hub.plugins.handover_snapshots import _windows_security

        _windows_security(path, private=True, create=True)
    else:
        path.mkdir(mode=0o700)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    private_directory(path)
    return path


def policy(**changes: object) -> Any:
    return replace(
        module().ReservationPolicy(
            max_bytes=BASE_BYTES + 2 * BYTES,
            max_members=BASE_MEMBERS + 2 * MEMBERS,
            max_operations=2,
            min_available_bytes=0,
        ),
        **changes,
    )


def reserve(store: Any, **changes: object) -> Any:
    operation = uuid4()
    args: dict[str, object] = {
        "operation_id": operation,
        "tenant": TENANT,
        "plugin_id": "example.plugin",
        "stage_name": "tmp-gh-" + operation.hex,
        "target_digest": DIGEST,
        "source_bytes": 5,
        "source_members": 2,
        "deadline": datetime.now(UTC) + timedelta(seconds=30),
        "revalidate": lambda: True,
    }
    args.update(changes)
    return store.reserve(**args)


def records(root: Path) -> list[Path]:
    return sorted(
        p for p in (root / ".stage-reservations").glob("*.json") if p.name != "domain.json"
    )


def test_token_is_immutable_bound_and_persisted_before_return(root: Path) -> None:
    m = module()
    p = policy()
    store = m.StageReservationStore(root, p)
    token = reserve(store)
    assert token.tenant == TENANT and token.plugin_id == "example.plugin"
    assert token.target_digest == DIGEST and token.stage_name.startswith("tmp-gh-")
    assert token.policy == p and token.store_root == root
    assert token.root_identity[:2] == (root.stat().st_dev, root.stat().st_ino)
    assert token.created_at.utcoffset() == timedelta(0)
    assert token.directory_durability is (sys.platform != "win32")
    assert token.charged_bytes == BYTES and token.charged_members == MEMBERS
    data = json.loads(records(root)[0].read_bytes())
    assert data["operation_id"] == str(token.operation_id)
    assert data["directory_durability"] is (sys.platform != "win32")
    with pytest.raises(FrozenInstanceError):
        token.source_bytes = 0
    with pytest.raises(FrozenInstanceError):
        p.max_operations = 99


@pytest.mark.parametrize("source_members", [2049, 8193])
def test_observed_source_members_use_policy_budget_not_legacy_default(
    root: Path, source_members: int
) -> None:
    charged_members = source_members + 4096 + 9
    charged_bytes = 5 + 16 * 1024 * 1024 + 16384 + charged_members * 4096
    p = policy(
        max_bytes=BASE_BYTES + charged_bytes,
        max_members=BASE_MEMBERS + charged_members,
        max_operations=1,
    )
    store = module().StageReservationStore(root, p)
    token = reserve(store, source_members=source_members)
    assert token.source_members == source_members
    assert token.charged_members == charged_members
    assert module().StageReservationStore(root, p).usage().members == BASE_MEMBERS + charged_members


@pytest.mark.parametrize("budget", ["bytes", "members", "operations"])
def test_exact_budget_and_one_over_survive_restart(root: Path, budget: str) -> None:
    changes = {
        "max_" + budget: {
            "bytes": BASE_BYTES + BYTES,
            "members": BASE_MEMBERS + MEMBERS,
            "operations": 1,
        }[budget]
    }
    p = policy(**changes)
    store = module().StageReservationStore(root, p)
    reserve(store)
    restarted = module().StageReservationStore(root, p)
    assert restarted.usage().operations == 1
    assert restarted.usage().bytes == BASE_BYTES + BYTES
    assert restarted.usage().members == BASE_MEMBERS + MEMBERS
    with pytest.raises(RuntimeError, match="budget"):
        reserve(restarted, tenant=uuid4(), plugin_id="other")
    assert len(records(root)) == 1


@pytest.mark.parametrize("available", [None, True, -1, "lots", BASE_BYTES + BYTES - 1])
def test_unknown_or_insufficient_capacity_refuses(
    root: Path, monkeypatch: Any, available: object
) -> None:
    m = module()
    monkeypatch.setattr(m, "_available_bytes", lambda d: available)
    with pytest.raises(RuntimeError, match="capacity"):
        reserve(m.StageReservationStore(root, policy()))
    assert records(root) == []


def test_watermark_includes_retained_charge(root: Path, monkeypatch: Any) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    reserve(store)
    monkeypatch.setattr(m, "_available_bytes", lambda d: BASE_BYTES + 2 * BYTES - 1)
    with pytest.raises(RuntimeError, match="capacity"):
        reserve(store)
    assert len(records(root)) == 1


def test_policy_change_and_foreign_root_ledger_refuse(root: Path, tmp_path: Path) -> None:
    m = module()
    reserve(m.StageReservationStore(root, policy()))
    with pytest.raises(RuntimeError, match="binding|policy"):
        m.StageReservationStore(root, policy(max_operations=3)).usage()
    other = tmp_path / "other"
    private_directory(other)
    private_directory(other / ".stage-reservations")
    for item in (root / ".stage-reservations").iterdir():
        destination = other / ".stage-reservations" / item.name
        destination.write_bytes(item.read_bytes())
        if sys.platform != "win32":
            destination.chmod(0o600)
    with pytest.raises((RuntimeError, ValueError), match="binding|identity|policy"):
        m.StageReservationStore(other, policy()).usage()


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant", str(uuid4())),
        ("charged_bytes", 0),
        ("source_members", True),
        ("schema_version", 2),
        ("directory_durability", sys.platform == "win32"),
    ],
)
def test_record_tampering_refuses(root: Path, field: str, value: object) -> None:
    store = module().StageReservationStore(root, policy())
    reserve(store)
    path = records(root)[0]
    data = json.loads(path.read_bytes())
    data[field] = value
    path.write_text(json.dumps(data), encoding="ascii")
    with pytest.raises((RuntimeError, ValueError)):
        reserve(store)
    assert len(records(root)) == 1


@pytest.mark.parametrize(
    "kind", ["unknown", "partial", "duplicate_key", "alias", "oversize", "duplicate_stage"]
)
def test_entire_ledger_refuses_unknown_corrupt_duplicate_and_alias(root: Path, kind: str) -> None:
    store = module().StageReservationStore(root, policy())
    first = reserve(store)
    path = records(root)[0]
    if kind == "unknown":
        (path.parent / "unknown").touch()
    elif kind == "partial":
        path.write_bytes(b'{"operation_id":')
    elif kind == "duplicate_key":
        path.write_bytes(b'{"schema_version":1,"schema_version":1}')
    elif kind == "alias":
        os.link(path, path.parent / (str(uuid4()) + ".json"))
    elif kind == "oversize":
        path.write_bytes(b" " * 16385)
    else:
        with pytest.raises(RuntimeError, match="duplicate"):
            reserve(store, stage_name=first.stage_name)
        assert len(records(root)) == 1
        return
    with pytest.raises((RuntimeError, ValueError)):
        reserve(store)


@pytest.mark.parametrize("kind", ["stage", "journal", "arbitrary"])
def test_unaccounted_store_is_never_initialized(root: Path, kind: str) -> None:
    if kind == "journal":
        private_directory(root / ".stage-ownership")
    elif kind == "stage":
        private_directory(root / str(TENANT))
        private_directory(root / str(TENANT) / "example.plugin")
        private_directory(root / str(TENANT) / "example.plugin" / ("tmp-gh-" + "a" * 32))
    else:
        (root / "unknown").write_bytes(b"not accounted")
    with pytest.raises((RuntimeError, ValueError), match="unaccounted|unknown"):
        reserve(module().StageReservationStore(root, policy()))
    assert not (root / ".stage-reservations" / "domain.json").exists()


@pytest.mark.parametrize("fault", ["short_write", "file_sync", "parent_sync"])
def test_uncertain_record_is_retained_without_token(
    root: Path, monkeypatch: Any, fault: str
) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    store.usage()  # Establish domain first, inject only the reservation boundary.
    if fault == "short_write":
        original_write = os.write
        monkeypatch.setattr(m.os, "write", lambda fd, data: original_write(fd, data[:3]))
    elif fault == "file_sync":
        original_fsync = os.fsync

        def fail(fd: int) -> None:
            if records(root):
                raise OSError("injected fsync failure")
            original_fsync(fd)

        monkeypatch.setattr(m.os, "fsync", fail)
    else:
        original_sync = m._Directory.sync

        def fail_parent(directory: Any) -> None:
            if records(root):
                raise OSError("injected parent sync failure")
            original_sync(directory)

        monkeypatch.setattr(m._Directory, "sync", fail_parent)
    with pytest.raises((RuntimeError, OSError)):
        reserve(store)
    assert len(records(root)) == 1
    retained = records(root)[0].read_bytes()
    monkeypatch.undo()
    if fault == "short_write":
        with pytest.raises((RuntimeError, ValueError)):
            store.usage()
    else:
        assert store.usage().operations == 1
    assert records(root)[0].read_bytes() == retained


def test_lock_hardlink_never_touches_victim(root: Path, tmp_path: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    store.usage()
    lock = root / ".stage-reservations" / "lock"
    victim = tmp_path / "victim"
    os.link(lock, victim)
    before = victim.read_bytes()
    with pytest.raises((RuntimeError, ValueError)):
        reserve(store)
    assert victim.read_bytes() == before
    assert records(root) == []


def test_live_and_restarted_handles_refuse_rebound_root(root: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    reserve(store)
    moved = root.with_name("old")
    root.rename(moved)
    private_directory(root)
    (moved / ".stage-reservations").rename(root / ".stage-reservations")
    for handle in (store, m.StageReservationStore(root, policy())):
        with pytest.raises((RuntimeError, ValueError)):
            reserve(handle)
    assert len(records(root)) == 1


def test_bounded_scan_refuses_even_when_operation_budget_remains(root: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy(max_ledger_records=1))
    reserve(store)
    with pytest.raises(RuntimeError, match="scan|limit"):
        reserve(store)
    assert len(records(root)) == 1


def test_wait_revalidates_callback_and_deadline_without_record(root: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    store.usage()
    from agent_hub.plugins.handover_snapshots import _Directory, _lock

    directory = _Directory(root / ".stage-reservations", private=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with _lock(directory):
                future = executor.submit(
                    reserve, store, deadline=datetime.now(UTC) + timedelta(milliseconds=100)
                )
                time.sleep(0.2)
            with pytest.raises(RuntimeError, match="deadline"):
                future.result(timeout=10)
    finally:
        directory.close()
    with pytest.raises(RuntimeError, match="valid"):
        reserve(store, revalidate=lambda: False)
    assert records(root) == []


@pytest.mark.parametrize("budget", ["bytes", "members", "operations"])
def test_processes_across_tenants_compete_for_one_slot(root: Path, budget: str) -> None:
    m = module()
    changes = {
        "max_" + budget: {
            "bytes": BASE_BYTES + BYTES,
            "members": BASE_MEMBERS + MEMBERS,
            "operations": 1,
        }[budget]
    }
    p = policy(**changes)
    m.StageReservationStore(root, p).usage()
    script = """
import sys
from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime, timedelta
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
root = Path(sys.argv[1])
p = ReservationPolicy(max_bytes=int(sys.argv[2]), max_members=int(sys.argv[3]),
                      max_operations=int(sys.argv[4]), min_available_bytes=0)
operation = uuid4()
print("ready", flush=True)
sys.stdin.readline()
try:
    StageReservationStore(root, p).reserve(operation_id=operation, tenant=uuid4(),
        plugin_id="plugin-"+operation.hex, stage_name="tmp-gh-"+operation.hex, target_digest="b"*64,
        source_bytes=5, source_members=2,
        deadline=datetime.now(UTC)+timedelta(seconds=20), revalidate=lambda: True)
except RuntimeError as error:
    if "budget" not in str(error):
        raise
    sys.exit(3)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3] / "src"))
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(root),
                str(p.max_bytes),
                str(p.max_members),
                str(p.max_operations),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline() == (
                b"ready\r\n" if sys.platform == "win32" else b"ready\n"
            )
        for process in processes:
            assert process.stdin is not None
            process.stdin.write(b"go\n")
            process.stdin.flush()
        results = [process.communicate(timeout=30) for process in processes]
        assert sorted(process.returncode for process in processes) == [0, 3], results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    assert len(records(root)) == 1
    assert m.StageReservationStore(root, p).usage().operations == 1


def test_retry_syncs_domain_after_uncertain_initial_manifest(root: Path, monkeypatch: Any) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    original = os.fsync

    def fail_domain(fd: int) -> None:
        if (root / ".stage-reservations" / "domain.json").exists():
            raise OSError("uncertain domain fsync")
        original(fd)

    monkeypatch.setattr(m.os, "fsync", fail_domain)
    with pytest.raises(OSError):
        reserve(store)
    monkeypatch.undo()
    synced: list[int] = []

    def observe(fd: int) -> None:
        synced.append(os.fstat(fd).st_ino)
        original(fd)

    monkeypatch.setattr(m.os, "fsync", observe)
    reserve(store)
    assert (root / ".stage-reservations" / "domain.json").stat().st_ino in synced


def test_stage_and_canonical_cannot_both_consume_one_source_charge(root: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    private_directory(root / str(token.tenant))
    plugin = root / str(token.tenant) / token.plugin_id
    private_directory(plugin)
    private_directory(plugin / token.stage_name)
    private_directory(plugin / token.target_digest)
    with pytest.raises(RuntimeError, match="duplicate|source"):
        reserve(store)
    assert len(records(root)) == 1


def test_journal_without_explicit_reservation_binding_refuses(root: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    directory = root
    for name in (".stage-ownership", str(token.tenant), token.plugin_id, str(token.operation_id)):
        directory /= name
        private_directory(directory)
    # A plausible legacy event is not a validated token-bound journal.
    event = {
        "schema_version": 1,
        "operation_id": str(token.operation_id),
        "tenant": str(token.tenant),
        "plugin_id": token.plugin_id,
        "stage_name": token.stage_name,
        "target_digest": token.target_digest,
        "reserved_bytes": 5,
        "reserved_members": 2,
        "ordinal": 1,
        "directory_durability": sys.platform != "win32",
    }
    path = directory / "000001.json"
    path.write_text(json.dumps(event), encoding="ascii")
    if sys.platform != "win32":
        path.chmod(0o600)
    with pytest.raises(RuntimeError, match="journal|unaccounted"):
        reserve(store)
    assert len(records(root)) == 1


@pytest.mark.parametrize("leaf", ["record", "domain", "lock"])
def test_replaced_ledger_file_refuses_even_identical_bytes(root: Path, leaf: str) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    reserve(store)
    path = (
        records(root)[0]
        if leaf == "record"
        else root / ".stage-reservations" / ("domain.json" if leaf == "domain" else "lock")
    )
    data = path.read_bytes()
    old = path.with_name(path.name + ".old")
    path.rename(old)
    path.write_bytes(data)
    if sys.platform != "win32":
        path.chmod(0o600)
    old.unlink()
    with pytest.raises((RuntimeError, ValueError)):
        m.StageReservationStore(root, policy()).usage()


@pytest.mark.parametrize("kind", ["bytes", "members", "hardlink", "unknown_leaf"])
def test_accounted_stage_payload_is_bounded_and_never_releases(root: Path, kind: str) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    private_directory(root / str(token.tenant))
    plugin = root / str(token.tenant) / token.plugin_id
    private_directory(plugin)
    stage = plugin / token.stage_name
    private_directory(stage)
    payload = stage / "payload"
    payload.write_bytes(b"12345")
    assert store.usage().bytes == BASE_BYTES + BYTES
    if kind == "bytes":
        payload.write_bytes(b"123456")
    elif kind == "members":
        (stage / "second").touch()
        (stage / "third").touch()
    elif kind == "hardlink":
        os.link(payload, stage / "alias")
    else:
        private_directory(plugin / ("tmp-gh-" + "c" * 32))
    with pytest.raises((RuntimeError, ValueError)):
        reserve(store)
    assert len(records(root)) == 1


def test_available_capacity_exact_boundary_with_watermark(root: Path, monkeypatch: Any) -> None:
    m = module()
    p = policy(min_available_bytes=17)
    monkeypatch.setattr(m, "_available_bytes", lambda d: BASE_BYTES + BYTES + 17)
    assert reserve(m.StageReservationStore(root, p)).charged_bytes == BYTES


def test_explicit_operation_collision_retains_original_record(root: Path) -> None:
    store = module().StageReservationStore(root, policy())
    token = reserve(store)
    before = records(root)[0].read_bytes()
    with pytest.raises(RuntimeError, match="duplicate"):
        reserve(store, operation_id=token.operation_id, tenant=uuid4(), plugin_id="another")
    assert records(root)[0].read_bytes() == before


def test_full_ledger_is_not_released_when_stage_disappears(root: Path) -> None:
    m = module()
    p = policy(max_operations=1)
    store = m.StageReservationStore(root, p)
    token = reserve(store)
    private_directory(root / str(token.tenant))
    plugin = root / str(token.tenant) / token.plugin_id
    private_directory(plugin)
    stage = plugin / token.stage_name
    private_directory(stage)
    stage.rmdir()
    assert m.StageReservationStore(root, p).usage().operations == 1
    with pytest.raises(RuntimeError, match="budget"):
        reserve(store)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_bytes", True),
        ("source_bytes", -1),
        ("source_members", 0),
        ("source_members", 2**31),
        ("plugin_id", "CON"),
        ("plugin_id", "con.txt"),
        ("stage_name", "../stage"),
        ("target_digest", "b" * 63),
        ("deadline", datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)),
        ("operation_id", "not-a-uuid"),
    ],
)
def test_invalid_request_never_creates_ledger(root: Path, field: str, value: object) -> None:
    store = module().StageReservationStore(root, policy())
    with pytest.raises(ValueError):
        reserve(store, **{field: value})
    assert not (root / ".stage-reservations").exists()


def test_revoked_request_is_revalidated_after_real_lock_wait(root: Path) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    store.usage()
    from agent_hub.plugins.handover_snapshots import _Directory, _lock

    started = Event()
    revoked = Event()

    def valid() -> bool:
        started.set()
        return not revoked.is_set()

    directory = _Directory(root / ".stage-reservations", private=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with _lock(directory):
                future = executor.submit(reserve, store, revalidate=valid)
                assert started.wait(timeout=5)
                revoked.set()
            with pytest.raises(RuntimeError, match="valid"):
                future.result(timeout=10)
    finally:
        directory.close()
    assert records(root) == []


def test_killed_writer_retains_complete_charge_and_unlocks(root: Path) -> None:
    m = module()
    p = policy(max_operations=1)
    m.StageReservationStore(root, p).usage()
    script = """
import sys, time
from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime, timedelta
import agent_hub.plugins.handover_stage_reservations as m
root = Path(sys.argv[1])
p = m.ReservationPolicy(max_bytes=int(sys.argv[2]), max_members=int(sys.argv[3]),
    max_operations=1, min_available_bytes=0)
original = m._Directory.sync
def pause(directory):
    if len(list((root/".stage-reservations").glob("*.json"))) > 1:
        print("persisted", flush=True)
        time.sleep(60)
    original(directory)
m._Directory.sync = pause
operation = uuid4()
m.StageReservationStore(root, p).reserve(operation_id=operation, tenant=uuid4(),
    plugin_id="worker", stage_name="tmp-gh-"+operation.hex, target_digest="b"*64,
    source_bytes=5, source_members=2, deadline=datetime.now(UTC)+timedelta(seconds=30),
    revalidate=lambda: True)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3] / "src"))
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(p.max_bytes), str(p.max_members)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == (
            b"persisted\r\n" if sys.platform == "win32" else b"persisted\n"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    restarted = m.StageReservationStore(root, p)
    assert restarted.usage().operations == 1
    assert restarted.usage().bytes == BASE_BYTES + BYTES
    with pytest.raises(RuntimeError, match="budget"):
        reserve(restarted)
    assert len(records(root)) == 1


def wired_event_bytes(event: dict[str, Any]) -> bytes:
    return json.dumps(
        event, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def wired_journal(
    root: Path, token: Any, states: tuple[str, ...] = ("creating",)
) -> tuple[Path, list[dict[str, Any]]]:
    """Independent fixture matching the publication journal's fixed manifest."""
    tenant = root / str(token.tenant)
    plugin = tenant / token.plugin_id
    for path in (tenant, plugin):
        if not path.exists():
            private_directory(path)
    directories = {"store": root, "tenant_parent": tenant, "plugin_parent": plugin}
    parent = root
    for label, name in (
        ("namespace", ".stage-ownership"),
        ("ledger_tenant", str(token.tenant)),
        ("ledger_plugin", token.plugin_id),
        ("operation", str(token.operation_id)),
    ):
        parent /= name
        if not parent.exists():
            private_directory(parent)
        directories[label] = parent
    bindings: dict[str, dict[str, object]] = {}
    for label, path in directories.items():
        info = path.lstat()
        bindings[label] = {
            "path": str(path),
            "dev": info.st_dev,
            "ino": info.st_ino,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": info.st_mode,
            "nlink": info.st_nlink,
        }
    created = token.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    manifest = {
        "schema_version": 1,
        "operation_id": str(token.operation_id),
        "tenant": str(token.tenant),
        "plugin_id": token.plugin_id,
        "stage_name": token.stage_name,
        "target_digest": token.target_digest,
        "reserved_bytes": token.source_bytes,
        "reserved_members": token.source_members,
        "member_budget": max(2048, token.source_members),
        "created_at": created,
        "directory_durability": token.directory_durability,
        "bindings": bindings,
        "reservation": module().reservation_reference(token),
    }
    events = []
    for ordinal, state in enumerate(states, 1):
        event = {
            **deepcopy(manifest),
            "ordinal": ordinal,
            "state": state,
            "details": {},
            "recorded_at": created,
        }
        path = parent / f"{ordinal:06d}.json"
        path.write_bytes(wired_event_bytes(event))
        if sys.platform != "win32":
            path.chmod(0o600)
        events.append(event)
    return parent, events


@pytest.mark.parametrize("feature", ["reference", "validate"])
def test_wired_interfaces_validate_held_token_and_reference_without_file_reads(
    root: Path,
    monkeypatch: Any,
    feature: str,
) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    if feature == "validate":
        assert store.validate_token(token) is None
        assert m.StageReservationStore(root, policy()).validate_token(token) is None
    else:

        def refuse_io(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("reference attempted file I/O")

        with monkeypatch.context() as patch:
            patch.setattr(m.os, "stat", refuse_io)
            patch.setattr(m.os, "open", refuse_io)
            reference = m.reservation_reference(token)
        assert reference["schema_version"] == 1
        assert reference["operation_id"] == str(token.operation_id)
        assert reference["tenant"] == str(TENANT)
        assert reference["plugin_id"] == token.plugin_id
        assert reference["stage_name"] == token.stage_name
        assert reference["target_digest"] == DIGEST
        assert reference["source_bytes"] == 5 and reference["source_members"] == 2
        assert reference["charged_bytes"] == BYTES and reference["charged_members"] == MEMBERS
        assert reference["root_identity"] == list(token.root_identity)
        assert reference["store_root"] == str(token.store_root)
        assert len(reference["policy_fingerprint"]) == 64
        assert reference["created_at"].endswith("Z")
        assert json.loads(wired_event_bytes(reference)) == reference
        assert m.reservation_reference(token) == reference
        reference["root_identity"].clear()
        assert m.reservation_reference(token)["root_identity"] == list(token.root_identity)


def test_wired_store_root_is_readonly_canonical_path(root: Path) -> None:
    store = module().StageReservationStore(root, policy())
    assert store.store_root == root
    with pytest.raises(AttributeError):
        store.store_root = root.parent


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant", uuid4()),
        ("operation_id", uuid4()),
        ("plugin_id", "other"),
        ("stage_name", "tmp-gh-" + "d" * 32),
        ("target_digest", "c" * 64),
        ("source_bytes", 6),
        ("source_members", 3),
        ("charged_bytes", BYTES + 1),
        ("charged_members", MEMBERS + 1),
        ("created_at", datetime(2026, 1, 1, tzinfo=UTC)),
        ("root_identity", (1, 2, 3, 4, 5)),
        ("source_bytes", True),
    ],
)
def test_wired_validate_rejects_any_held_token_scope_change(
    root: Path,
    field: str,
    value: object,
) -> None:
    store = module().StageReservationStore(root, policy())
    token = reserve(store)
    changed = replace(token, **{field: value})
    with pytest.raises((RuntimeError, ValueError)):
        store.validate_token(changed)
    assert len(records(root)) == 1


def test_wired_validate_never_initializes_empty_foreign_root(root: Path, tmp_path: Path) -> None:
    m = module()
    token = reserve(m.StageReservationStore(root, policy()))
    other = tmp_path / "foreign"
    private_directory(other)
    foreign = m.StageReservationStore(other, policy())
    with pytest.raises((RuntimeError, ValueError)):
        foreign.validate_token(token)
    assert list(other.iterdir()) == []
    with pytest.raises((RuntimeError, ValueError)):
        m.StageReservationStore(root, policy(max_operations=3)).validate_token(token)


@pytest.mark.parametrize(
    "states",
    [
        ("creating",),
        ("creating", "recording", "recording", "ready"),
        ("creating", "ready", "rename_attempted", "published"),
    ],
)
def test_wired_complete_charged_journal_keeps_charge_and_allows_next_admission(
    root: Path,
    states: tuple[str, ...],
) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    wired_journal(root, token, states)
    assert store.validate_token(token) is None
    assert store.usage().bytes == BASE_BYTES + BYTES
    next_token = reserve(store, tenant=uuid4(), plugin_id="other")
    assert next_token.operation_id != token.operation_id
    assert store.usage().operations == 2


@pytest.mark.parametrize(
    "fault",
    [
        "legacy",
        "foreign_reference",
        "prefix",
        "ordinal",
        "state",
        "unknown_field",
        "duplicate_key",
        "noncanonical",
        "partial",
        "gap",
        "hardlink",
        "empty",
        "external_binding",
        "binding_inode",
        "binding_nlink",
        "wrong_utc",
        "decreasing_utc",
        "source_count",
        "unknown_entry",
        "unknown_operation",
    ],
)
def test_wired_journal_fault_is_retained_and_refuses_admission(root: Path, fault: str) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    directory, events = wired_journal(root, token, ("creating", "recording"))
    event = deepcopy(events[1])
    path = directory / "000002.json"
    if fault == "legacy":
        event.pop("reservation")
    elif fault == "foreign_reference":
        event["reservation"]["operation_id"] = str(uuid4())
    elif fault == "prefix":
        event["member_budget"] += 1
    elif fault == "ordinal":
        event["ordinal"] = True
    elif fault == "state":
        event["state"] = "published"
    elif fault == "unknown_field":
        event["unknown"] = "ignored?"
    elif fault == "external_binding":
        event["bindings"]["store"]["path"] = str(root.parent / "outside")
    elif fault == "binding_inode":
        event["bindings"]["operation"]["ino"] += 1
    elif fault == "binding_nlink":
        event["bindings"]["operation"]["nlink"] += 1
    elif fault == "wrong_utc":
        event["recorded_at"] = "2026-01-01T00:00:00"
    elif fault == "decreasing_utc":
        event["recorded_at"] = "2026-01-01T00:00:00.000000Z"
    elif fault == "source_count":
        event["reserved_members"] = 3
    if fault == "duplicate_key":
        path.write_bytes(b'{"state":"recording","state":"recording"}')
    elif fault == "noncanonical":
        path.write_text(json.dumps(event), encoding="ascii")
    elif fault == "partial":
        path.write_bytes(b'{"state":')
    elif fault == "gap":
        path.rename(directory / "000003.json")
    elif fault == "hardlink":
        os.link(path, root.parent / "event-alias")
    elif fault == "empty":
        for file in directory.iterdir():
            file.unlink()
    elif fault == "unknown_entry":
        (directory / "unknown").touch()
    elif fault == "unknown_operation":
        directory.rename(directory.with_name(str(uuid4())))
    else:
        path.write_bytes(wired_event_bytes(event))
    before = records(root)[0].read_bytes()
    with pytest.raises((RuntimeError, ValueError, OSError)):
        reserve(store)
    assert len(records(root)) == 1
    assert records(root)[0].read_bytes() == before


def test_wired_shared_directory_nlink_changes_are_not_rebinding(root: Path) -> None:
    store = module().StageReservationStore(root, policy())
    first, second = reserve(store), reserve(store)
    wired_journal(root, first)
    wired_journal(root, second)
    # POSIX shared journal parent nlink grows when the sibling operation is created.
    assert store.validate_token(first) is None
    assert store.validate_token(second) is None


@pytest.mark.parametrize(
    "fault",
    ["none", "other_tenant", "other_plugin", "payload", "hardlink", "unknown_lock", "lock_content"],
)
def test_wired_incoming_scope_allows_only_empty_parents_and_safe_scope_lock(
    root: Path,
    fault: str,
) -> None:
    m = module()
    private_directory(root / str(TENANT))
    plugin = root / str(TENANT) / "example.plugin"
    private_directory(plugin)
    locks = root / ".uninstall-locks"
    private_directory(locks)
    private_directory(locks / str(TENANT))
    lock = locks / str(TENANT) / "example.plugin.lock"
    lock.write_bytes(b"\0")
    if sys.platform != "win32":
        lock.chmod(0o600)
    if fault == "other_tenant":
        private_directory(root / str(uuid4()))
    elif fault == "other_plugin":
        private_directory(root / str(TENANT) / "other")
    elif fault == "payload":
        (plugin / "unaccounted").touch()
    elif fault == "hardlink":
        os.link(lock, root.parent / "lock-alias")
    elif fault == "unknown_lock":
        (lock.parent / "other.lock").touch()
    elif fault == "lock_content":
        lock.write_bytes(b"unexpected")
    store = m.StageReservationStore(root, policy())
    if fault == "none":
        token = reserve(store)
        assert store.validate_token(token) is None
        assert store.usage().operations == 1
    else:
        with pytest.raises((RuntimeError, ValueError, OSError)):
            reserve(store)
        assert not (root / ".stage-reservations" / "domain.json").exists()


@pytest.mark.parametrize(
    "boundary", ["event_exact", "event_over", "total_exact", "total_over", "events_over"]
)
def test_wired_journal_byte_and_event_bounds_are_enforced(root: Path, boundary: str) -> None:
    store = module().StageReservationStore(root, policy())
    token = reserve(store)
    count = 16 if boundary == "total_exact" else 17 if boundary == "total_over" else 1
    directory, events = wired_journal(root, token, ("creating",) + ("recording",) * (count - 1))
    if boundary == "events_over":
        for ordinal in range(2, 4098):
            (directory / f"{ordinal:06d}.json").touch()
    else:
        size = 1024 * 1024 + (1 if boundary == "event_over" else 0)
        for ordinal, event in enumerate(events, 1):
            event["details"] = {"padding": ""}
            event["details"]["padding"] = "x" * (size - len(wired_event_bytes(event)))
            data = wired_event_bytes(event)
            assert len(data) == size
            (directory / f"{ordinal:06d}.json").write_bytes(data)
    if boundary.endswith("exact"):
        assert store.validate_token(token) is None
        assert store.usage().operations == 1
    else:
        with pytest.raises((RuntimeError, ValueError)):
            reserve(store)
        assert len(records(root)) == 1


@pytest.mark.parametrize(
    "states",
    [
        ("ready",),
        ("creating", "creating"),
        ("creating", "ready", "recording"),
        ("creating", "ready", "rename_attempted", "published", "published"),
        ("creating", "UNKNOWN"),
    ],
)
def test_wired_journal_invalid_state_prefixes_refuse(root: Path, states: tuple[str, ...]) -> None:
    store = module().StageReservationStore(root, policy())
    token = reserve(store)
    wired_journal(root, token, states)
    with pytest.raises(RuntimeError, match="state"):
        store.validate_token(token)
    assert len(records(root)) == 1


def test_wired_validate_does_not_restore_missing_record(root: Path) -> None:
    store = module().StageReservationStore(root, policy())
    token = reserve(store)
    records(root)[0].unlink()
    with pytest.raises(RuntimeError, match="durable"):
        store.validate_token(token)
    assert records(root) == []


def test_wired_scan_detects_journal_mutation_after_a_valid_read(
    root: Path, monkeypatch: Any
) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    directory, _ = wired_journal(root, token, ("creating", "recording"))
    original = m._Directory.read

    def corrupt_after_read(parent: Any, name: str, limit: int, **options: Any) -> bytes:
        data: bytes = original(parent, name, limit, **options)
        if parent.path == directory and name == "000002.json":
            (directory / "000001.json").write_bytes(b"unknown partial")
        return data

    monkeypatch.setattr(m._Directory, "read", corrupt_after_read)
    with pytest.raises(RuntimeError, match="changed"):
        reserve(store)
    assert len(records(root)) == 1


def test_wired_scanning_never_reads_metadata_external_binding(root: Path, monkeypatch: Any) -> None:
    m = module()
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    directory, events = wired_journal(root, token)
    external = root.parent / "external"
    events[0]["bindings"]["operation"]["path"] = str(external)
    (directory / "000001.json").write_bytes(wired_event_bytes(events[0]))
    original = m._Directory.__init__

    def trusted_only(parent: Any, path: Path, *, private: bool) -> None:
        assert path != external, "scanner opened metadata-controlled path"
        original(parent, path, private=private)

    monkeypatch.setattr(m._Directory, "__init__", trusted_only)
    with pytest.raises(RuntimeError, match="binding"):
        store.validate_token(token)


def test_wired_incoming_safe_lock_is_charged_and_pinned_across_restart(root: Path) -> None:
    m = module()
    namespace = root / ".uninstall-locks"
    private_directory(namespace)
    private_directory(namespace / str(TENANT))
    lock = namespace / str(TENANT) / "example.plugin.lock"
    lock.write_bytes(b"\0")
    if sys.platform != "win32":
        lock.chmod(0o600)
    store = m.StageReservationStore(root, policy())
    token = reserve(store)
    assert token.charged_members == MEMBERS + 2
    assert token.charged_bytes == BYTES + 8192
    assert store.validate_token(token) is None
    old = lock.with_name("old")
    lock.rename(old)
    lock.write_bytes(b"\0")
    if sys.platform != "win32":
        lock.chmod(0o600)
    old.unlink()
    with pytest.raises(RuntimeError, match="lock"):
        m.StageReservationStore(root, policy()).validate_token(token)
    assert len(records(root)) == 1


@pytest.mark.parametrize("kind", ["empty_root", "file", "nested_directory"])
def test_wired_source_budget_counts_payload_root_once_and_rejects_extra_zero_file(
    root: Path,
    kind: str,
) -> None:
    store = module().StageReservationStore(root, policy())
    members = {"empty_root": 1, "file": 2, "nested_directory": 3}[kind]
    token = reserve(store, source_members=members, source_bytes=0 if kind == "empty_root" else 5)
    private_directory(root / str(token.tenant))
    plugin = root / str(token.tenant) / token.plugin_id
    private_directory(plugin)
    stage = plugin / token.stage_name
    private_directory(stage)
    directory = stage
    if kind == "nested_directory":
        directory = stage / "nested"
        private_directory(directory)
    if kind != "empty_root":
        (directory / "payload").write_bytes(b"12345")
    # These exact root-inclusive budgets must pass before adding a zero-byte node.
    assert store.usage().operations == 1
    assert store.validate_token(token) is None
    extra = directory / "extra"
    extra.touch()
    with pytest.raises(RuntimeError, match="source budget"):
        store.validate_token(token)
    assert extra.exists() and len(records(root)) == 1
