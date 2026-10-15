"""Durable scope infrastructure admission, with real shared lock callers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.plugins import handover_stage_reservations as reservations
from agent_hub.plugins.handover_confirmation import _shared_fs_lock_at_root
from agent_hub.plugins.handover_snapshots import _Directory
from agent_hub.plugins.handover_stage_journal import StagePublicationJournal
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
from tests.unit.plugins.test_handover_stage_reservations import private_directory

TENANT = UUID("11111111-1111-4111-8111-111111111111")
PLUGIN = "example.plugin"
SCOPE_MEMBERS = 5  # intent, binding, namespace, tenant parent, lock
SCOPE_BYTES = 2 * 16384 + SCOPE_MEMBERS * 4096
OP_MEMBERS = 2 + 4096 + 9
OP_BYTES = 5 + 16 * 1024 * 1024 + 16384 + OP_MEMBERS * 4096


def policy() -> ReservationPolicy:
    return ReservationPolicy(
        max_bytes=200_000_000,
        max_members=50_000,
        max_operations=1,
        min_available_bytes=0,
    )


def make_store(tmp_path: Path, changes: dict[str, Any] | None = None) -> StageReservationStore:
    root = tmp_path / "store"
    private_directory(root)
    return StageReservationStore(root, replace(policy(), **(changes or {})))


def prepare(store: StageReservationStore, plugin: str = PLUGIN, **changes: Any) -> Any:
    return store.prepare_scope_lock(
        tenant=TENANT,
        plugin_id=plugin,
        deadline=changes.get("deadline", datetime.now(UTC) + timedelta(seconds=30)),
        revalidate=changes.get("revalidate", lambda: True),
    )


def reserve(store: Any, plugin: str = PLUGIN, *, valid: bool = True) -> Any:
    operation = uuid4()
    return store.reserve(
        operation_id=operation,
        tenant=TENANT,
        plugin_id=plugin,
        stage_name="tmp-gh-" + operation.hex,
        target_digest="b" * 64,
        source_bytes=5,
        source_members=2,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        revalidate=lambda: valid,
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_scope_has_independent_durable_charge_and_immutable_binding(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    scope = prepare(store)
    assert scope.lock_path == store.store_root / ".uninstall-locks" / str(TENANT) / (
        PLUGIN + ".lock"
    )
    assert scope.scope_lock_identity == tuple(
        value
        for p in (scope.lock_path.parent.parent, scope.lock_path.parent, scope.lock_path)
        for value in reservations._identity(p.stat())
    )
    assert scope.charged_bytes == SCOPE_BYTES and scope.charged_members == SCOPE_MEMBERS
    assert scope.policy == policy() and scope.root_identity == reservations._identity(
        store.store_root.stat()
    )
    assert scope.lock_path.read_bytes() == b"\0"
    with pytest.raises(FrozenInstanceError):
        scope.charged_bytes = 0
    usage = StageReservationStore(store.store_root, policy()).usage()
    assert (usage.bytes, usage.members, usage.operations) == (
        32768 + SCOPE_BYTES,
        4 + SCOPE_MEMBERS,
        0,
    )
    before = snapshot(store.store_root)
    assert prepare(StageReservationStore(store.store_root, policy())) == scope
    assert snapshot(store.store_root) == before


@pytest.mark.parametrize("refusal", ["quota", "revalidate"])
def test_actual_caller_refusal_keeps_scope_safe_for_other_scope(
    tmp_path: Path, refusal: str
) -> None:
    store = make_store(tmp_path, {"max_bytes": 32768 + 2 * SCOPE_BYTES})
    prepare(store)
    with (
        _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN),
        store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication,
    ):
        with pytest.raises(RuntimeError, match="budget|valid"):
            reserve(publication, valid=refusal != "revalidate")
        assert publication.usage().operations == 0
    first = snapshot(store.store_root)
    prepare(store, "other.plugin")
    assert all(snapshot(store.store_root)[name] == data for name, data in first.items())
    assert store.usage().operations == 0
    assert store.usage().bytes == 32768 + 2 * SCOPE_BYTES


def test_operation_does_not_double_charge_enrolled_scope(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        {
            "max_bytes": 32768 + SCOPE_BYTES + OP_BYTES,
            "max_members": 4 + SCOPE_MEMBERS + OP_MEMBERS,
        },
    )
    scope = prepare(store)
    with (
        _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN),
        store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication,
    ):
        token = reserve(publication)
        publication.validate_token(token)
        assert token.scope_lock_identity == scope.scope_lock_identity
        assert (token.charged_bytes, token.charged_members) == (OP_BYTES, OP_MEMBERS)
    assert store.usage().operations == 1
    prepare(store, PLUGIN)  # Known infrastructure is reusable at operation/byte/member limits.
    with pytest.raises(RuntimeError, match="budget"):
        prepare(store, "other.plugin")


@pytest.mark.parametrize(
    "budget", ["max_bytes", "max_members", "max_ledger_records", "max_ledger_bytes"]
)
def test_infrastructure_budget_refusal_creates_no_scope_objects(
    tmp_path: Path, budget: str
) -> None:
    store = make_store(
        tmp_path,
        {
            budget: {
                "max_bytes": 32768 + SCOPE_BYTES - 1,
                "max_members": 4 + SCOPE_MEMBERS - 1,
                "max_ledger_records": 1,
                "max_ledger_bytes": 32768,
            }[budget]
        },
    )
    with pytest.raises(RuntimeError, match="budget|scan limit"):
        prepare(store)
    assert not (store.store_root / ".uninstall-locks").exists()
    assert store.usage().operations == 0


@pytest.mark.parametrize("failure", ["write", "fsync", "parent", "lock", "binding"])
def test_partial_infrastructure_is_retained_and_refuses_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = make_store(tmp_path)
    # Initialize the domain so faults are specifically in scope enrollment.
    store.usage()
    real_write, real_fsync, real_child = os.write, os.fsync, _Directory.child
    real_record = StageReservationStore._write

    def write(fd: int, data: bytes) -> int:
        if failure == "write" and b'"kind":"scope_intent"' in data:
            return real_write(fd, data[:1])
        if failure == "lock" and data == b"\0":
            raise OSError("scope lock fault")
        return real_write(fd, data)

    def fsync(fd: int) -> None:
        if failure == "fsync":
            raise OSError("scope fsync fault")
        real_fsync(fd)

    def child(value: _Directory, name: str) -> _Directory:
        if failure == "parent" and name == ".uninstall-locks":
            raise OSError("scope parent fault")
        return real_child(value, name)

    def record(
        value: StageReservationStore, directory: Any, name: str, payload: dict[str, object]
    ) -> None:
        if failure == "binding" and payload.get("kind") == "scope_binding":
            raise OSError("scope binding fault")
        real_record(value, directory, name, payload)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", write)
        patch.setattr(_Directory, "child", child)
        patch.setattr(StageReservationStore, "_write", record)
        if failure == "fsync":
            # Target by record hook instead of reading lock byte zero.
            def faulty_record(
                value: StageReservationStore, directory: Any, name: str, payload: dict[str, object]
            ) -> None:
                if payload.get("kind") == "scope_intent":
                    patch.setattr(os, "fsync", fsync)
                real_record(value, directory, name, payload)

            patch.setattr(StageReservationStore, "_write", faulty_record)
        with pytest.raises((RuntimeError, OSError)):
            prepare(store)
    before = snapshot(store.store_root)
    assert any("scope-" in name for name in before)
    for action in (
        lambda: store.usage(),
        lambda: prepare(store),
        lambda: prepare(store, "other.plugin"),
    ):
        with pytest.raises(RuntimeError):
            action()
    assert snapshot(store.store_root) == before


def test_intent_is_synced_before_any_infrastructure_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    persisted = False
    real_write, real_child = StageReservationStore._write, _Directory.child

    def record(
        value: StageReservationStore, directory: Any, name: str, payload: dict[str, object]
    ) -> None:
        nonlocal persisted
        real_write(value, directory, name, payload)
        if payload.get("kind") == "scope_intent":
            persisted = True

    def child(value: _Directory, name: str) -> _Directory:
        if name == ".uninstall-locks" or name == str(TENANT):
            assert persisted, "infrastructure created before durable intent"
        return real_child(value, name)

    monkeypatch.setattr(StageReservationStore, "_write", record)
    monkeypatch.setattr(_Directory, "child", child)
    prepare(store)


@pytest.mark.parametrize("damage", ["unknown", "corrupt", "extra", "lock_size", "hardlink"])
def test_full_scan_rejects_unknown_corrupt_and_aliased_scope_without_cleanup(
    tmp_path: Path,
    damage: str,
) -> None:
    store = make_store(tmp_path)
    scope = prepare(store)
    if damage == "unknown":
        (scope.lock_path.parent / "unknown.lock").write_bytes(b"\0")
    elif damage == "lock_size":
        scope.lock_path.write_bytes(b"bad")
    elif damage == "hardlink":
        os.link(scope.lock_path, tmp_path / "external-alias")
    else:
        intent = next((store.store_root / ".stage-reservations").glob("scope-*.json"))
        if damage == "corrupt":
            intent.write_bytes(b"{")
        else:
            payload = json.loads(intent.read_bytes())
            payload["unaccounted_metadata"] = 1
            body = {k: v for k, v in payload.items() if k != "record_sha256"}
            payload["record_sha256"] = hashlib.sha256(reservations._canonical(body)).hexdigest()
            intent.write_bytes(reservations._canonical(payload))
    before = snapshot(store.store_root)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        prepare(store, "other.plugin")
    assert snapshot(store.store_root) == before


def test_legacy_lock_is_not_adopted_even_for_requested_scope(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN):
        pass
    before = snapshot(store.store_root)
    with pytest.raises(RuntimeError, match="unaccounted"):
        prepare(store)
    assert snapshot(store.store_root) == before


def test_prepare_refuses_nested_domain_without_deadlock(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    prepare(store)
    with (
        store.publication(tenant=TENANT, plugin_id=PLUGIN),
        pytest.raises(RuntimeError, match="nested domain"),
    ):
        prepare(store)


@pytest.mark.parametrize("invalid", ["expired", "revalidate"])
def test_invalid_prepare_does_not_initialize(tmp_path: Path, invalid: str) -> None:
    store = make_store(tmp_path)
    with pytest.raises(RuntimeError, match="expired|valid"):
        prepare(
            store,
            deadline=datetime.now(UTC) + timedelta(seconds=-1 if invalid == "expired" else 30),
            revalidate=lambda: invalid != "revalidate",
        )
    assert snapshot(store.store_root) == {}


@pytest.mark.parametrize("record", ["scope-", "scope-binding-"])
def test_scope_record_mutation_after_read_refuses_in_same_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: str,
) -> None:
    store = make_store(tmp_path)
    prepare(store)
    real_read = _Directory.read
    mutated = False

    def read(value: _Directory, name: str, limit: int, *, links: tuple[int, ...] = (1,)) -> bytes:
        nonlocal mutated
        data = real_read(value, name, limit, links=links)
        if (
            not mutated
            and name.startswith(record)
            and (record == "scope-binding-" or not name.startswith("scope-binding-"))
        ):
            with (value.path / name).open("ab") as stream:
                stream.write(b" ")
            mutated = True
        return data

    monkeypatch.setattr(_Directory, "read", read)
    with pytest.raises(RuntimeError, match="changed"):
        store.usage()
    assert mutated


def test_enrolled_scope_binds_journal_and_holds_domain(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    scope = prepare(store)
    with (
        _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN),
        store.publication(tenant=TENANT, plugin_id=PLUGIN) as publication,
    ):
        token = reserve(publication)
        private_directory(store.store_root / str(TENANT))
        private_directory(store.store_root / str(TENANT) / PLUGIN)
        with StagePublicationJournal(
            store.store_root,
            TENANT,
            PLUGIN,
            token.stage_name,
            token.target_digest,
            5,
            2,
            reservation=token,
            publication=publication,
        ) as journal:
            journal.record("recording", {})
            publication.validate_token(token)
            event = json.loads((journal.root / "000001.json").read_bytes())
            assert event["reservation"]["scope_lock_accounted"] is True
            assert event["reservation"]["scope_lock_identity"] == list(scope.scope_lock_identity)
    assert store.usage().operations == 1


def test_known_scope_reuse_and_other_scope_scan_never_read_locked_byte_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    scope = prepare(store)
    real_read = _Directory.read

    def read(value: _Directory, name: str, limit: int, *, links: tuple[int, ...] = (1,)) -> bytes:
        if sys.platform == "win32" and name.endswith(".lock"):
            pytest.fail("enrolled lock byte zero was read")
        return real_read(value, name, limit, links=links)

    monkeypatch.setattr(_Directory, "read", read)
    with _shared_fs_lock_at_root(store.store_root, TENANT, PLUGIN):
        assert prepare(store) == scope
        prepare(store, "other.plugin")
        assert store.usage().operations == 0


@pytest.mark.parametrize("same_scope", [True, False])
def test_process_preparation_waits_for_intent_binding_and_reuses_or_admits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_scope: bool,
) -> None:
    store = make_store(tmp_path)
    paused, resume = Event(), Event()
    real_write = StageReservationStore._write

    def record(
        value: StageReservationStore, directory: Any, name: str, payload: dict[str, object]
    ) -> None:
        real_write(value, directory, name, payload)
        if payload.get("kind") == "scope_intent":
            paused.set()
            assert resume.wait(15)

    monkeypatch.setattr(StageReservationStore, "_write", record)
    code = """
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from agent_hub.plugins.handover_stage_reservations import ReservationPolicy, StageReservationStore
store = StageReservationStore(Path(sys.argv[1]), ReservationPolicy(
    max_bytes=200000000, max_members=50000, max_operations=1, min_available_bytes=0))
print('ready', flush=True)
sys.stdin.readline()
store.prepare_scope_lock(tenant=UUID(sys.argv[2]), plugin_id=sys.argv[3],
    deadline=datetime.now(UTC)+timedelta(seconds=30), revalidate=lambda: True)
print(store.usage().bytes, flush=True)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(store.store_root),
            str(TENANT),
            PLUGIN if same_scope else "other.plugin",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        assert process.stdout.readline().strip() == b"ready"
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(prepare, store)
            try:
                assert paused.wait(10)
                process.stdin.write(b"go\n")
                process.stdin.flush()
                reader = executor.submit(process.communicate, timeout=20)
                with pytest.raises(TimeoutError):
                    reader.result(timeout=0.3)
            finally:
                resume.set()
            writer.result(timeout=10)
            out, err = reader.result(timeout=20)
            assert process.returncode == 0, err.decode(errors="replace")
            assert int(out.strip()) == 32768 + SCOPE_BYTES * (1 if same_scope else 2)
    finally:
        resume.set()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_post_binding_revalidation_refusal_retains_known_safe_infrastructure(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)

    def valid() -> bool:
        return not any((store.store_root / ".stage-reservations").glob("scope-binding-*.json"))

    with pytest.raises(RuntimeError, match="valid"):
        prepare(store, revalidate=valid)
    assert store.usage().bytes == 32768 + SCOPE_BYTES
    prepare(store, "other.plugin")
    assert store.usage().bytes == 32768 + 2 * SCOPE_BYTES


@pytest.mark.parametrize("leaf", ["lock", "tenant", "namespace", "root"])
def test_rebound_infrastructure_refuses_and_preserves_original_nodes(
    tmp_path: Path, leaf: str
) -> None:
    store = make_store(tmp_path)
    scope = prepare(store)
    victim = {
        "lock": scope.lock_path,
        "tenant": scope.lock_path.parent,
        "namespace": scope.lock_path.parent.parent,
        "root": store.store_root,
    }[leaf]
    moved = tmp_path / "retained-original"
    victim.rename(moved)
    if leaf == "lock":
        scope.lock_path.write_bytes(b"\0")
        if sys.platform != "win32":
            scope.lock_path.chmod(0o600)
    else:
        private_directory(victim)
        if leaf != "root":
            if leaf == "namespace":
                private_directory(scope.lock_path.parent)
            scope.lock_path.write_bytes(b"\0")
            if sys.platform != "win32":
                scope.lock_path.chmod(0o600)
    before = snapshot(store.store_root)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        prepare(store, "other.plugin")
    assert snapshot(store.store_root) == before
    assert moved.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "operation"),
        ("charged_bytes", 0),
        ("charged_members", True),
        ("tenant", str(uuid4())),
        ("plugin_id", "../outside"),
        ("root_identity", [0] * 5),
        ("scope_lock_identity", [0] * 15),
        ("intent_identity", [0] * 5),
        ("policy", {}),
    ],
)
def test_sealed_binding_tampering_does_not_grant_scope_authority(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    store = make_store(tmp_path)
    prepare(store)
    bound = next((store.store_root / ".stage-reservations").glob("scope-binding-*.json"))
    payload = json.loads(bound.read_bytes())
    payload[field] = value
    body = {k: v for k, v in payload.items() if k != "record_sha256"}
    payload["record_sha256"] = hashlib.sha256(reservations._canonical(body)).hexdigest()
    bound.write_bytes(reservations._canonical(payload))
    before = snapshot(store.store_root)
    with pytest.raises(RuntimeError):
        prepare(store)
    assert snapshot(store.store_root) == before


@pytest.mark.parametrize("available", [None, True, -1, "lots", 32768 + SCOPE_BYTES + 10 - 1])
def test_scope_watermark_refuses_unknown_or_insufficient_capacity_before_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: object,
) -> None:
    store = make_store(tmp_path, {"min_available_bytes": 10})
    monkeypatch.setattr(reservations, "_available_bytes", lambda directory: available)
    with pytest.raises(RuntimeError, match="capacity"):
        prepare(store)
    assert not (store.store_root / ".uninstall-locks").exists()
    assert not list((store.store_root / ".stage-reservations").glob("scope-*.json"))


def test_scope_records_share_scan_slots_with_operations(tmp_path: Path) -> None:
    store = make_store(tmp_path, {"max_ledger_records": 2})
    prepare(store)
    with pytest.raises(RuntimeError, match="record scan limit"):
        reserve(store)
    assert store.usage().operations == 0
    prepare(store)


@pytest.mark.parametrize("accounted", [False, None, 1])
def test_scope_accounting_flag_cannot_mutate_issued_operation_token(
    tmp_path: Path, accounted: object
) -> None:
    store = make_store(tmp_path)
    prepare(store)
    token = reserve(store)
    with pytest.raises((RuntimeError, ValueError)):
        store.validate_token(replace(token, scope_lock_accounted=accounted))
    store.validate_token(token)


def test_domain_wait_revalidates_scope_request_before_creation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    waiting = Event()
    valid = True

    def revalidate() -> bool:
        waiting.set()
        return valid

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.publication(tenant=TENANT, plugin_id=PLUGIN):
            pending = executor.submit(prepare, store, revalidate=revalidate)
            assert waiting.wait(5)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.3)
            valid = False
        with pytest.raises(RuntimeError, match="valid"):
            pending.result(timeout=10)
    assert not (store.store_root / ".uninstall-locks").exists()


def test_known_scope_remains_reusable_below_current_capacity_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    scope = prepare(store)
    before = snapshot(store.store_root)
    monkeypatch.setattr(reservations, "_available_bytes", lambda directory: 0)
    assert prepare(store) == scope
    assert snapshot(store.store_root) == before
