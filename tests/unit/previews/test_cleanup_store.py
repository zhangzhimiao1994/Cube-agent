from __future__ import annotations

import importlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.unit.previews.test_cleanup import contract, identity


def store_module() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.cleanup_store"), "receipt store missing"
    return importlib.import_module("agent_hub.previews.cleanup_store")


def test_store_roundtrip_reload_and_incomplete_restart(tmp_path: Path) -> None:
    mod = store_module()
    c = contract()
    now = datetime.now(UTC)
    store = mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now)
    record = c.PreviewCleanupRecord(identity(), None, None)
    store.put(record)
    assert store.get(record.identity.preview_id) == record
    reloaded = mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now)
    abandoned = reloaded.get(record.identity.preview_id)
    assert abandoned.cleanup_receipt.status == "unknown"
    assert abandoned.retention_expires_at == now + timedelta(hours=24)
    assert b"recovery_token" not in next((tmp_path / ".preview-receipts").glob("*.json")).read_bytes()


def test_store_rejects_oversized_or_truncated_record(tmp_path: Path) -> None:
    mod = store_module()
    c = contract()
    now = datetime.now(UTC)
    store = mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now)
    record = c.PreviewCleanupRecord(identity(), None, None)
    store.put(record)
    path = next((tmp_path / ".preview-receipts").glob("*.json"))
    bad_time = json.dumps(record.to_wire() | {
        "retention_expires_at": "0001-01-01T00:00:00+23:59",
    }).encode()
    for data in (b"{", b" " * 65537, b"[" * 20000 + b"]" * 20000, bad_time):
        path.write_bytes(data)
        assert mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now).get(
            record.identity.preview_id
        ) is None
        assert path.read_bytes() == data


def test_store_root_alias_is_rejected(tmp_path: Path) -> None:
    mod = store_module()
    target = tmp_path / "target"
    target.mkdir()
    try:
        (tmp_path / ".preview-receipts").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises((OSError, ValueError)):
        mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: datetime.now(UTC))


def test_corrupted_record_is_preserved_when_update_is_attempted(tmp_path: Path) -> None:
    mod, c = store_module(), contract()
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: datetime.now(UTC))
    record = c.PreviewCleanupRecord(identity(), None, None)
    store.put(record)
    path = next((tmp_path / ".preview-receipts").glob("*.json"))
    path.write_bytes(b'{"corruption-evidence":')
    with pytest.raises(ValueError):
        store.put(record)
    assert path.read_bytes() == b'{"corruption-evidence":'


def test_failed_atomic_replace_preserves_previous_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, c = store_module(), contract()
    now = datetime.now(UTC)
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now)
    record = c.PreviewCleanupRecord(identity(), None, None)
    store.put(record)
    changed = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now, reason="explicit"), None)
    with monkeypatch.context() as patch:
        patch.setattr(mod.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("atomic fixture")))
        with pytest.raises(OSError):
            store.put(changed)
    assert store.get(record.identity.preview_id) == record
    assert len(list((tmp_path / ".preview-receipts").iterdir())) == 1


def test_terminal_ttl_cap_and_active_record_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from uuid import uuid4
    mod, c = store_module(), contract()
    assert mod.MAX_TERMINAL == 256
    monkeypatch.setattr(mod, "MAX_TERMINAL", 2)
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    active = c.PreviewCleanupRecord(identity(), None, None)
    store.put(active)
    with pytest.raises(ValueError, match="capacity"):
        store.put(c.PreviewCleanupRecord(replace(identity(), preview_id=str(uuid4())), None, None))
    terminal_ids = []
    for _ in range(3):
        now[0] += timedelta(seconds=1)
        bound = replace(identity(), preview_id=str(uuid4()))
        store.put(c.PreviewCleanupRecord(bound, c.CleanupReceiptV1.create(bound, (),
                  requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24)))
        terminal_ids.append(bound.preview_id)
    assert store.get(terminal_ids[0]) is None
    assert store.get(terminal_ids[1]) is not None
    now[0] += timedelta(hours=25)
    assert store.get(terminal_ids[2]) is None
    assert store.get(active.identity.preview_id) == active


def test_hardlinked_record_is_not_loaded_or_deleted(tmp_path: Path) -> None:
    import os
    mod, c = store_module(), contract()
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: datetime.now(UTC))
    record = c.PreviewCleanupRecord(identity(), None, None)
    store.put(record)
    path = next((tmp_path / ".preview-receipts").glob("*.json"))
    alias = tmp_path / "alias.json"
    os.link(path, alias)
    reloaded = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: datetime.now(UTC))
    assert reloaded.get(record.identity.preview_id) is None
    assert path.read_bytes() == alias.read_bytes()


@pytest.mark.parametrize("damage", ["truncated", "oversized", "hardlink", "unreadable", "changed", "owner"])
def test_invalid_expired_record_does_not_block_unrelated_puts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    mod, c = store_module(), contract()
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    path = tmp_path / ".preview-receipts" / (old.identity.preview_id + ".json")
    if damage == "truncated":
        path.write_bytes(b"{corruption-evidence")
    elif damage == "oversized":
        path.write_bytes(b" " * 65537)
    elif damage == "hardlink":
        os.link(path, tmp_path / "evidence.json")
    elif damage == "changed":
        path.write_text(json.dumps(replace(old, cleanup_receipt=None).to_wire()))
    elif damage == "owner":
        path.write_text(json.dumps(replace(old, identity=replace(
            old.identity, user_id=str(uuid4())), cleanup_receipt=None).to_wire()))
    evidence = path.read_bytes()
    real_open = mod.os.open

    def guarded_open(target: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if damage == "unreadable" and target == path:
            raise PermissionError("unreadable historical record fixture")
        return int(real_open(target, flags, mode, dir_fd=dir_fd))

    now[0] += timedelta(hours=25)
    with monkeypatch.context() as patch:
        patch.setattr(mod.os, "open", guarded_open)
        for _ in range(2):
            record = c.PreviewCleanupRecord(replace(identity(), preview_id=str(uuid4())), None, None)
            store.put(record)
            assert store.get(record.identity.preview_id) == record
        assert store.get(old.identity.preview_id) is None
        assert old.identity.preview_id not in store._records
        with pytest.raises(ValueError):
            store.put(old)
    assert path.read_bytes() == evidence


def test_preserved_invalid_history_has_a_bounded_directory_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod, c = store_module(), contract()
    monkeypatch.setattr(mod, "MAX_TERMINAL", 1)
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    budget = 2 * (mod.MAX_TERMINAL + 1)
    for _ in range(budget // 2):
        bound = replace(identity(), preview_id=str(uuid4()))
        record = c.PreviewCleanupRecord(bound, c.CleanupReceiptV1.create(
            bound, (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
        store.put(record)
        (tmp_path / ".preview-receipts" / (bound.preview_id + ".json")).write_bytes(b"{evidence")
        now[0] += timedelta(hours=25)
        store._prune()
    fresh = c.PreviewCleanupRecord(replace(identity(), preview_id=str(uuid4())), None, None)
    with pytest.raises(ValueError, match="budget"):
        store.put(fresh)
    paths = tuple((tmp_path / ".preview-receipts").iterdir())
    assert len(paths) == budget
    assert all(path.read_bytes() == b"{evidence" for path in paths if path.suffix == ".json")
    assert len([path for path in paths if path.suffix == ".isolated"]) == budget // 2
    assert store.get(fresh.identity.preview_id) is None


@pytest.mark.parametrize("guard", ["_guard", "_guard_parents"])
def test_historical_isolation_does_not_swallow_global_storage_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: str,
) -> None:
    mod, c = store_module(), contract()
    now = datetime.now(UTC)
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now)
    record = c.PreviewCleanupRecord(identity(), None, None)

    def fail_guard() -> None:
        raise PermissionError("global storage guard fixture")

    monkeypatch.setattr(store, guard, fail_guard)
    with pytest.raises(PermissionError, match="global storage guard"):
        store.put(record)
    assert not tuple((tmp_path / ".preview-receipts").iterdir())


def test_global_guard_failure_during_invalid_record_prune_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod, c = store_module(), contract()
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    path = tmp_path / ".preview-receipts" / (old.identity.preview_id + ".json")
    path.write_bytes(b"{preserved evidence")
    now[0] += timedelta(hours=25)
    new = c.PreviewCleanupRecord(replace(identity(), preview_id=str(uuid4())), None, None)
    real_lstat = Path.lstat
    global_failure = False

    def lstat(target: Path) -> os.stat_result:
        nonlocal global_failure
        if target == path:
            global_failure = True
            raise PermissionError("leaf read fixture")
        if global_failure and target == path.parent:
            raise PermissionError("global directory fixture")
        return real_lstat(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        with pytest.raises(PermissionError, match="global directory fixture"):
            store.put(new)
    assert old.identity.preview_id in store._records
    assert new.identity.preview_id not in store._records
    assert path.read_bytes() == b"{preserved evidence"
    assert not (path.parent / (new.identity.preview_id + ".json")).exists()


@pytest.mark.parametrize("changed_owner", [False, True])
@pytest.mark.parametrize("future", [False, True])
def test_valid_replacement_is_durably_isolated_across_reload(
    tmp_path: Path, changed_owner: bool, future: bool,
) -> None:
    mod, c = store_module(), contract()
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    now[0] += timedelta(hours=25)
    bound = replace(old.identity, user_id=str(uuid4())) if changed_owner else old.identity
    results = {"serving_thread": "exited", "listener": "closed", "accepted_threads": "drained",
               "accepted_sockets": "closed", "proxy_operations": "drained", "proxy_sockets": "closed",
               "capability": "revoked", "snapshot": "absent"}
    facts = tuple(c.ResourceObservation(resource, "manager", now[0], results[resource], "observed")
                  for resource in (*c.STATIC_RESOURCES, *c.MANAGER_RESOURCES))
    receipt = c.CleanupReceiptV1.create(bound, facts, requested_at=now[0], reason="explicit")
    assert receipt.status == "confirmed"
    replacement = c.PreviewCleanupRecord(bound, receipt,
        now[0] + (timedelta(hours=24) if future else -timedelta(hours=1)))
    path = tmp_path / ".preview-receipts" / (bound.preview_id + ".json")
    evidence = json.dumps(replacement.to_wire()).encode()
    path.write_bytes(evidence)
    unrelated = c.PreviewCleanupRecord(replace(identity(), preview_id=str(uuid4())), None, None)
    store.put(unrelated)
    assert store.get(bound.preview_id) is None
    assert store.get(unrelated.identity.preview_id) == unrelated
    for _ in range(2):
        store = mod.PreviewReceiptStore(tmp_path, max_active=2, clock=lambda: now[0])
        assert store.get(bound.preview_id) is None
        assert store.get(unrelated.identity.preview_id) is not None
        for record in (old, replacement):
            with pytest.raises(ValueError, match="isolated"):
                store.put(record)
        assert path.read_bytes() == evidence
        assert path.with_suffix(".isolated").read_bytes() == b""


@pytest.mark.parametrize("failure", ["create", "file_sync", "directory_sync"])
def test_isolation_failure_is_fail_closed_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    mod, c = store_module(), contract()
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    path = tmp_path / ".preview-receipts" / (old.identity.preview_id + ".json")
    path.write_bytes(b"{preserved evidence")
    now[0] += timedelta(hours=25)
    new = c.PreviewCleanupRecord(replace(identity(), preview_id=str(uuid4())), None, None)
    real_open = mod.os.open

    def failing_open(target: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if target == path.with_suffix(".isolated"):
            raise PermissionError("isolation fixture")
        return int(real_open(target, flags, mode, dir_fd=dir_fd))

    def fail_sync(*args: object) -> None:
        raise OSError("isolation fixture")

    with monkeypatch.context() as patch:
        if failure == "create":
            patch.setattr(mod.os, "open", failing_open)
        elif failure == "file_sync":
            patch.setattr(mod.os, "fsync", fail_sync)
        else:
            patch.setattr(mod.PreviewReceiptStore, "_sync_directory", fail_sync)
        with pytest.raises(OSError, match="isolation fixture"):
            store.put(new)
        if failure != "create":
            with pytest.raises(OSError, match="isolation fixture"):
                mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    assert old.identity.preview_id in store._records
    assert new.identity.preview_id not in store._records
    assert not (path.parent / (new.identity.preview_id + ".json")).exists()
    assert path.read_bytes() == b"{preserved evidence"
    store.put(new)
    reloaded = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    assert reloaded.get(old.identity.preview_id) is None
    assert reloaded.get(new.identity.preview_id) is not None
    assert path.read_bytes() == b"{preserved evidence"
    with pytest.raises(ValueError, match="isolated"):
        reloaded.put(old)


@pytest.mark.parametrize("damage", ["missing_json", "marker_bytes", "marker_hardlink"])
def test_isolation_metadata_never_permits_adoption_or_identity_reuse(
    tmp_path: Path, damage: str,
) -> None:
    mod, c = store_module(), contract()
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    path = tmp_path / ".preview-receipts" / (old.identity.preview_id + ".json")
    path.write_bytes(b"{preserved evidence")
    now[0] += timedelta(hours=25)
    store._prune()
    marker = path.with_suffix(".isolated")
    if damage == "missing_json":
        path.unlink()
        reloaded = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
        assert reloaded.get(old.identity.preview_id) is None
        with pytest.raises(ValueError, match="isolated"):
            reloaded.put(old)
        assert not path.exists()
    else:
        if damage == "marker_bytes":
            marker.write_bytes(b"invalid isolation metadata")
        else:
            os.link(marker, tmp_path / "marker-alias")
        with pytest.raises(ValueError):
            mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
        with pytest.raises(ValueError, match="isolated"):
            store.put(old)
        assert path.read_bytes() == b"{preserved evidence"


def test_isolation_budget_exhaustion_preserves_cache_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod, c = store_module(), contract()
    monkeypatch.setattr(mod, "MAX_TERMINAL", 1)
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    path = tmp_path / ".preview-receipts" / (old.identity.preview_id + ".json")
    path.write_bytes(b"{preserved evidence")
    junk = [path.parent / str(index) for index in range(3)]
    for entry in junk:
        entry.write_bytes(b"unrelated evidence")
    now[0] += timedelta(hours=25)
    with pytest.raises(ValueError, match="budget"):
        store._prune()
    assert old.identity.preview_id in store._records
    assert not path.with_suffix(".isolated").exists()
    assert path.read_bytes() == b"{preserved evidence"
    # Model operator removal of unrelated entries, never receipt evidence.
    junk[0].unlink()
    store._prune()
    assert len(tuple(path.parent.iterdir())) == 4
    assert path.with_suffix(".isolated").read_bytes() == b""
    assert old.identity.preview_id not in store._records


@pytest.mark.parametrize("expire_during_put", [False, True])
@pytest.mark.parametrize("active_replacement", [False, True])
def test_put_rejects_same_identity_isolated_by_its_own_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    expire_during_put: bool, active_replacement: bool,
) -> None:
    mod, c = store_module(), contract()
    now = [datetime.now(UTC)]
    store = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    old = c.PreviewCleanupRecord(identity(), c.CleanupReceiptV1.create(
        identity(), (), requested_at=now[0], reason="recovery"), now[0] + timedelta(hours=24))
    store.put(old)
    path = tmp_path / ".preview-receipts" / (old.identity.preview_id + ".json")
    marker = path.with_suffix(".isolated")
    path.unlink()
    assert not marker.exists()
    assert old.identity.preview_id in store._records
    real_prune = store._prune

    def prune_after_expiry() -> None:
        now[0] += timedelta(hours=25)
        real_prune()

    if expire_during_put:
        monkeypatch.setattr(store, "_prune", prune_after_expiry)
    else:
        now[0] += timedelta(hours=25)
    replacement = c.PreviewCleanupRecord(old.identity, None, None) if active_replacement else replace(
        old, cleanup_receipt=replace(old.cleanup_receipt, observation_id=str(uuid4())),
        retention_expires_at=now[0] + timedelta(hours=50))
    with pytest.raises(ValueError, match="isolated"):
        store.put(replacement)
    assert marker.read_bytes() == b""
    assert not path.exists()
    assert old.identity.preview_id not in store._records
    assert store.get(old.identity.preview_id) is None
    assert tuple(path.parent.iterdir()) == (marker,)
    reloaded = mod.PreviewReceiptStore(tmp_path, max_active=1, clock=lambda: now[0])
    assert reloaded.get(old.identity.preview_id) is None
    with pytest.raises(ValueError, match="isolated"):
        reloaded.put(replacement)
    assert not path.exists()
