"""Real filesystem boundaries for metadata-only publication intent."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OPERATION = UUID("22222222-2222-4222-8222-222222222222")
PLUGIN = "example.plugin"
STAGE = "tmp-gh-" + "a" * 32
DIGEST = "b" * 64


def module() -> Any:
    name = "agent_hub.plugins.handover_stage_journal"
    assert importlib.util.find_spec(name) is not None, "publication journal is missing"
    return importlib.import_module(name)


def private_directory(path: Path) -> None:
    if sys.platform == "win32":
        from agent_hub.plugins.handover_snapshots import _windows_security

        _windows_security(path, private=True, create=True)
    else:
        path.mkdir(mode=0o700)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "packages"
    private_directory(root)
    private_directory(root / str(TENANT))
    private_directory(root / str(TENANT) / PLUGIN)
    return root


def journal(store: Path, **changes: object) -> Any:
    args: dict[str, object] = {
        "store_root": store,
        "tenant": TENANT,
        "plugin_id": PLUGIN,
        "stage_name": STAGE,
        "target_digest": DIGEST,
        "reserved_bytes": 5,
        "reserved_members": 2,
    }
    args.update(changes)
    return module().StagePublicationJournal(**args)


def events(store: Path) -> list[dict[str, Any]]:
    roots = list((store / ".stage-ownership" / str(TENANT) / PLUGIN).iterdir())
    assert len(roots) == 1
    return [json.loads(path.read_bytes()) for path in sorted(roots[0].iterdir())]


def test_durable_utc_times_are_bound_to_each_publication_event(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = module()
    start = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    clock = iter([start, start, start + timedelta(seconds=1)])
    monkeypatch.setattr(m, "_utc_now", lambda: next(clock), raising=False)
    with journal(store) as log:
        log.record("recording", {"kind": "stage_created"})
    first, second = events(store)
    assert first["created_at"] == second["created_at"] == "2026-10-13T04:00:00.000000Z"
    assert first["recorded_at"] == first["created_at"]
    assert second["recorded_at"] == "2026-10-13T04:00:01.000000Z"


def test_insufficient_disk_headroom_creates_no_journal_or_stage(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = module()
    monkeypatch.setattr(m, "_available_bytes", lambda directory: 0, raising=False)
    with pytest.raises(RuntimeError, match="capacity"):
        journal(store)
    assert not (store / ".stage-ownership").exists()
    assert not (store / str(TENANT) / PLUGIN / STAGE).exists()


def test_disk_exhaustion_during_publication_prevents_rename_fence(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = module()
    with journal(store) as log:
        log.record("ready", {})
        monkeypatch.setattr(m, "_available_bytes", lambda directory: 0, raising=False)
        with pytest.raises(RuntimeError, match="capacity"):
            log.record("rename_attempted", {})
        monkeypatch.setattr(m, "_available_bytes", lambda directory: 10**12)
        with pytest.raises(RuntimeError, match="uncertain"):
            log.record("rename_attempted", {})
    assert [event["state"] for event in events(store)] == ["creating", "ready"]


@pytest.mark.parametrize("available", [True, -1, None, "1000000000"])
def test_invalid_disk_metadata_fails_closed_before_intent(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: object,
) -> None:
    monkeypatch.setattr(module(), "_available_bytes", lambda directory: available, raising=False)
    with pytest.raises(RuntimeError, match="capacity"):
        journal(store)
    assert not (store / ".stage-ownership").exists()


def test_different_volume_publication_parent_is_rejected_before_intent(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = module()
    inode = (store / str(TENANT) / PLUGIN).stat().st_ino
    original = m._identity

    def different_device(info: os.stat_result) -> tuple[int, ...]:
        value = cast(tuple[int, ...], original(info))
        return (value[0] + 1, *value[1:]) if info.st_ino == inode else value

    monkeypatch.setattr(m, "_identity", different_device)
    with pytest.raises(RuntimeError, match="volume"):
        journal(store)
    assert not (store / ".stage-ownership").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows caller quota API")
def test_windows_capacity_uses_caller_available_bytes_not_volume_free(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    calls: list[str] = []

    def free_space(path: str, caller: Any, total: Any, free: Any) -> int:
        calls.append(path)
        caller._obj.value = 3
        assert total is None and free is None
        return 1

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *args, **kwargs: SimpleNamespace(
            GetDiskFreeSpaceExW=free_space,
        ),
    )
    directory = SimpleNamespace(fd=None, path=store)
    assert module()._available_bytes(directory) == 3
    assert calls == [str(module()._native_path(store))]


@pytest.mark.parametrize("margin", [-1, 0, 1])
def test_disk_headroom_exact_boundary(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    margin: int,
) -> None:
    m = module()
    required = m._MIN_FREE_BYTES + m._MAX_TOTAL_BYTES + 5
    monkeypatch.setattr(m, "_available_bytes", lambda directory: required + margin)
    if margin < 0:
        with pytest.raises(RuntimeError, match="capacity"):
            journal(store)
        assert not (store / ".stage-ownership").exists()
    else:
        with journal(store):
            assert events(store)[0]["state"] == "creating"


@pytest.mark.parametrize("invalid", [None, -1, OSError("capacity read failed")])
def test_invalid_later_capacity_poison_is_permanent(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    m = module()
    with journal(store) as log:

        def unavailable(directory: object) -> object:
            if isinstance(invalid, BaseException):
                raise invalid
            return invalid

        monkeypatch.setattr(m, "_available_bytes", unavailable)
        with pytest.raises((RuntimeError, OSError)):
            log.record("ready", {})
        monkeypatch.setattr(m, "_available_bytes", lambda directory: 10**12)
        with pytest.raises(RuntimeError, match="uncertain"):
            log.record("ready", {})
    assert [event["state"] for event in events(store)] == ["creating"]


@pytest.mark.parametrize("invalid", [None, OSError("clock read failed")])
def test_invalid_later_clock_poison_is_permanent(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    m = module()
    with journal(store) as log:
        original_clock = m._utc_now

        def unavailable() -> object:
            if isinstance(invalid, BaseException):
                raise invalid
            return invalid

        monkeypatch.setattr(m, "_utc_now", unavailable)
        with pytest.raises((RuntimeError, OSError)):
            log.record("ready", {})
        monkeypatch.setattr(m, "_utc_now", original_clock)
        with pytest.raises(RuntimeError, match="uncertain"):
            log.record("ready", {})
    assert [event["state"] for event in events(store)] == ["creating"]


def test_invalid_first_intent_clock_retains_no_event(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([datetime(2026, 10, 13, tzinfo=UTC), None])
    monkeypatch.setattr(module(), "_utc_now", lambda: next(clock))
    with pytest.raises(RuntimeError, match="clock"):
        journal(store)
    assert not list((store / ".stage-ownership").rglob("*.json"))
    assert not (store / str(TENANT) / PLUGIN / STAGE).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux descriptor capacity sampling")
def test_posix_capacity_uses_bound_descriptor_and_available_blocks(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def capacity(fd: int) -> object:
        calls.append(fd)
        return SimpleNamespace(f_bavail=3, f_bfree=10**9, f_frsize=4096)

    monkeypatch.setattr(os, "fstatvfs", capacity)
    assert module()._available_bytes(SimpleNamespace(fd=2345, path=store)) == 12_288
    assert calls == [2345]


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "2026-10-13",
        datetime(2026, 10, 13, tzinfo=UTC).replace(tzinfo=None),
        datetime(2026, 10, 13, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_invalid_initial_clock_creates_no_publication_namespace(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    monkeypatch.setattr(module(), "_utc_now", lambda: invalid, raising=False)
    with pytest.raises(RuntimeError, match="clock"):
        journal(store)
    assert not (store / ".stage-ownership").exists()


@pytest.mark.parametrize("advance", [-1, 0])
def test_regressed_clock_retains_original_intent_and_poisoned_handle(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    advance: int,
) -> None:
    m = module()
    now = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    monkeypatch.setattr(m, "_utc_now", lambda: now, raising=False)
    with journal(store) as log:
        now += timedelta(seconds=1)
        log.record("ready", {})
        now += timedelta(seconds=advance)
        if advance < 0:
            with pytest.raises(RuntimeError, match="clock"):
                log.record("rename_attempted", {})
            now += timedelta(seconds=10)
            with pytest.raises(RuntimeError, match="uncertain"):
                log.record("rename_attempted", {})
            assert [event["state"] for event in events(store)] == ["creating", "ready"]
        else:
            log.record("rename_attempted", {})
            assert events(store)[-1]["recorded_at"] == "2026-10-13T04:00:01.000000Z"


@pytest.mark.parametrize("label", ["namespace", "ledger_tenant", "ledger_plugin"])
def test_shared_ledger_parent_allows_other_owned_child_link_counts(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    m = module()
    with journal(store) as log:
        inode = log._directories[label].path.stat().st_ino
        original = m._identity

        def changed_child_links(info: os.stat_result) -> tuple[int, ...]:
            identity = cast(tuple[int, ...], original(info))
            if stat.S_ISDIR(info.st_mode) and info.st_ino == inode:
                return (*identity[:-1], identity[-1] + 1)
            return identity

        # Model only POSIX's mutable directory link count; actual files/ACLs/handles remain real.
        monkeypatch.setattr(m, "_identity", changed_child_links)
        log.record("recording", {"kind": "other_owned_sibling_creation"})
        assert events(store)[-1]["state"] == "recording"


@pytest.mark.parametrize("label", ["store", "namespace", "ledger_tenant", "ledger_plugin"])
def test_shared_parent_interleaved_descriptor_link_counts(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    m = module()
    with journal(store) as log:
        directory = log._directories[label]
        current = directory.path.stat()
        original_fstat = os.fstat
        fake_fd = 999_999
        original_fd = directory.fd

        def fstat(fd: int) -> os.stat_result:
            if fd == fake_fd:
                return cast(
                    os.stat_result,
                    SimpleNamespace(
                        st_dev=current.st_dev,
                        st_ino=current.st_ino,
                        st_uid=current.st_uid,
                        st_gid=current.st_gid,
                        st_mode=current.st_mode,
                        st_nlink=current.st_nlink + 1,
                    ),
                )
            return original_fstat(fd)

        # Isolate only the interleaved stat/fstat boundary, not a claimed Linux filesystem proof.
        monkeypatch.setattr(m.os, "fstat", fstat)
        monkeypatch.setattr(directory, "check", lambda: None)
        directory.fd = fake_fd
        try:
            log._check()
        finally:
            directory.fd = original_fd


def test_reused_ledger_namespace_syncs_its_parent_before_new_intent(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = module()
    with journal(store):
        pass
    original = m._Directory.sync
    synced: list[Path] = []

    def sync(directory: Any) -> None:
        synced.append(directory.path)
        original(directory)

    monkeypatch.setattr(m._Directory, "sync", sync)
    with journal(store):
        assert store in synced
        assert store / ".stage-ownership" in synced
        assert store / ".stage-ownership" / str(TENANT) in synced


def test_creating_intent_precedes_stage_and_events_remain_after_close(store: Path) -> None:
    with journal(store) as log:
        assert not (store / str(TENANT) / PLUGIN / STAGE).exists()
        first = events(store)[0]
        assert first["state"] == "creating" and first["details"] == {}
        assert first["tenant"] == str(TENANT) and first["plugin_id"] == PLUGIN
        assert first["stage_name"] == STAGE and first["target_digest"] == DIGEST
        assert first["reserved_bytes"] == 5 and first["reserved_members"] == 2
        assert first["directory_durability"] is (sys.platform != "win32")
        assert first["bindings"]["plugin_parent"]["nlink"] >= 1
        private_directory(store / str(TENANT) / PLUGIN / STAGE)
        log.record("recording", {"kind": "stage_created"})
        log.record("recording", {"kind": "file_created", "path": "plugin.json"})
        log.record("ready", {"members": [{"path": "plugin.json", "size_bytes": 5}]})
        log.record("rename_attempted", {})
        log.record("published", {})
    assert [event["state"] for event in events(store)] == [
        "creating",
        "recording",
        "recording",
        "ready",
        "rename_attempted",
        "published",
    ]
    with pytest.raises(RuntimeError):
        log.record("published", {})
    log.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant": str(TENANT)},
        {"tenant": None},
        {"plugin_id": "../evil"},
        {"plugin_id": "CON"},
        {"plugin_id": "a/evil"},
        {"plugin_id": "a" * 129},
        {"stage_name": "tmp-gh-" + "A" * 32},
        {"stage_name": "../tmp-gh-" + "a" * 32},
        {"target_digest": "B" * 64},
        {"target_digest": "b" * 63},
        {"reserved_bytes": -1},
        {"reserved_bytes": 2_000_001},
        {"reserved_bytes": True},
        {"reserved_members": 0},
        {"reserved_members": 2049},
        {"reserved_members": True},
        {"store_root": Path("relative")},
        {"store_root": "relative"},
    ],
)
def test_invalid_parameters_never_create_ledger(store: Path, changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        journal(store, **changes)
    assert not (store / ".stage-ownership").exists()


def test_missing_store_or_plugin_parent_is_not_created(store: Path) -> None:
    for changes in ({"store_root": store / "missing"}, {"plugin_id": "absent"}):
        with pytest.raises((OSError, ValueError, RuntimeError)):
            journal(store, **changes)
    assert not (store / ".stage-ownership").exists()


@pytest.mark.parametrize(
    "state",
    [
        "creating",
        "published",
        "aborted",
        "confirmed_aborted",
        "unknown",
        "cleanup_pending",
        "reclaimed",
    ],
)
def test_unapproved_or_out_of_order_states_do_not_write(store: Path, state: str) -> None:
    with journal(store) as log, pytest.raises(ValueError):
        log.record(state, {})
    assert len(events(store)) == 1


@pytest.mark.parametrize(
    "attempt",
    ["creating", "recording", "ready", "rename_attempted", "aborted", "confirmed_aborted"],
)
def test_rename_fence_cannot_regress(store: Path, attempt: str) -> None:
    with journal(store) as log:
        log.record("ready", {})
        log.record("rename_attempted", {})
        with pytest.raises(ValueError):
            log.record(attempt, {})
        log.record("published", {})
    assert [event["state"] for event in events(store)] == [
        "creating",
        "ready",
        "rename_attempted",
        "published",
    ]


def test_operation_uuid_collision_preserves_existing_object(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module(), "uuid4", lambda: OPERATION)
    with journal(store):
        pass
    before = events(store)
    root = store / ".stage-ownership" / str(TENANT) / PLUGIN / str(OPERATION)
    identity = root.stat()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        journal(store)
    assert events(store) == before
    assert root.stat().st_ino == identity.st_ino
    assert root.stat().st_mode == identity.st_mode


@pytest.mark.parametrize("kind", ["file", "directory", "hardlink", "symlink"])
def test_next_event_collision_is_never_replaced(store: Path, tmp_path: Path, kind: str) -> None:
    with journal(store) as log:
        first = next(log.root.iterdir())
        second = first.with_name(first.name.replace("000001", "000002"))
        assert second != first
        victim = tmp_path / "victim"
        victim.write_bytes(b"unowned")
        if kind == "file":
            second.write_bytes(b"unowned")
        elif kind == "directory":
            second.mkdir()
        elif kind == "hardlink":
            os.link(victim, second)
        else:
            try:
                second.symlink_to(victim)
            except OSError:
                pytest.skip("symlink creation privilege unavailable")
        before = second.lstat()
        with pytest.raises((OSError, ValueError, RuntimeError)):
            log.record("ready", {})
        assert second.lstat().st_ino == before.st_ino
        assert victim.read_bytes() == b"unowned"


@pytest.mark.parametrize("component", ["namespace", "tenant", "plugin", "operation"])
def test_ledger_rebinding_is_rejected_without_writing(store: Path, component: str) -> None:
    with journal(store) as log:
        paths = list(log.root.parents)
        path = {
            "namespace": paths[2],
            "tenant": paths[1],
            "plugin": paths[0],
            "operation": log.root,
        }[component]
        moved = path.with_name(path.name + "-moved")
        path.rename(moved)
        private_directory(path)
        with pytest.raises((OSError, ValueError, RuntimeError)):
            log.record("ready", {})
        assert not list(path.iterdir())
        assert len(list(moved.rglob("*.json"))) == 1


def test_plugin_parent_rebinding_is_rejected(store: Path) -> None:
    with journal(store) as log:
        parent = store / str(TENANT) / PLUGIN
        parent.rename(parent.with_name("uninstalled"))
        private_directory(parent)
        with pytest.raises((OSError, ValueError, RuntimeError)):
            log.record("ready", {})
    assert len(events(store)) == 1


def test_public_parent_directory_growth_does_not_mean_rebinding(store: Path) -> None:
    with journal(store) as log:
        private_directory(store / str(TENANT) / PLUGIN / STAGE)
        log.record("recording", {"kind": "stage_created"})
        private_directory(store / "another-tenant")
        private_directory(store / str(TENANT) / "another-plugin")
        log.record("ready", {})
        log.record("rename_attempted", {})
    assert events(store)[-1]["state"] == "rename_attempted"


def test_public_parent_nlink_stat_double_is_not_stable_identity(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_stat, real_fstat = os.stat, os.fstat
    public_nodes = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (
            store,
            store / str(TENANT),
            store / str(TENANT) / PLUGIN,
        )
    }
    with journal(store) as log:

        def changed_links(info: os.stat_result) -> os.stat_result:
            if (info.st_dev, info.st_ino) not in public_nodes:
                return info
            attributes = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            attributes["st_nlink"] += 7
            return cast(os.stat_result, SimpleNamespace(**attributes))

        def stat_with_growth(*args: Any, **kwargs: Any) -> os.stat_result:
            return changed_links(real_stat(*args, **kwargs))

        def fstat_with_growth(fd: int) -> os.stat_result:
            return changed_links(real_fstat(fd))

        monkeypatch.setattr(os, "stat", stat_with_growth)
        monkeypatch.setattr(os, "fstat", fstat_with_growth)
        log.record("recording", {"kind": "stage_created"})
        log.record("ready", {})
        log.record("rename_attempted", {})
    assert events(store)[-1]["state"] == "rename_attempted"


def test_aggregate_record_budget_rejects_before_open_and_poison_handle(store: Path) -> None:
    with journal(store) as log:
        for _ in range(100):
            before = {path.name: path.stat().st_size for path in log.root.iterdir()}
            try:
                log.record("recording", {"metadata": "m" * 900_000})
            except ValueError:
                break
        else:
            pytest.fail("aggregate metadata exceeded 16 MiB")
        assert {path.name: path.stat().st_size for path in log.root.iterdir()} == before
        assert sum(before.values()) <= 16 * 1024 * 1024
        with pytest.raises(RuntimeError):
            log.record("ready", {})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX private mode boundary")
@pytest.mark.parametrize("component", ["store", "tenant", "plugin", "namespace"])
def test_unsafe_permissions_are_rejected_without_chmod(store: Path, component: str) -> None:
    paths = {
        "store": store,
        "tenant": store / str(TENANT),
        "plugin": store / str(TENANT) / PLUGIN,
        "namespace": store / ".stage-ownership",
    }
    path = paths[component]
    if component == "namespace":
        path.mkdir(mode=0o700)
    path.chmod(0o777)
    before = path.stat()
    with pytest.raises((ValueError, RuntimeError)):
        journal(store)
    assert path.stat().st_mode == before.st_mode


def test_record_completes_short_writes(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write
    with journal(store) as log:

        def short_write(fd: int, data: bytes | memoryview) -> int:
            return real_write(fd, data[:7])

        monkeypatch.setattr(os, "write", short_write)
        log.record("ready", {"members": [{"path": "a", "sha256": DIGEST}]})
    assert events(store)[-1]["details"]["members"][0]["path"] == "a"


@pytest.mark.parametrize("point", ["file", "directory"])
def test_event_corruption_during_sync_is_rejected(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    if point == "directory" and sys.platform == "win32":
        pytest.skip("Windows has no directory fsync")
    real_sync = os.fsync
    with journal(store) as log:

        def corrupt_event(fd: int) -> None:
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            real_sync(fd)
            if directory == (point == "directory"):
                path = max(log.root.iterdir())
                data = path.read_bytes()
                path.write_bytes(b"!" + data[1:])
                os.utime(path, ns=(1_000_000_000, 2_000_000_000))

        monkeypatch.setattr(os, "fsync", corrupt_event)
        with pytest.raises((OSError, ValueError, RuntimeError)):
            log.record("ready", {})
        assert len(list(log.root.iterdir())) == 2
        with pytest.raises(RuntimeError):
            log.record("rename_attempted", {})


def test_new_event_hardlink_during_sync_is_retained_and_rejected(
    store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sync = os.fsync
    alias = tmp_path / "linked-event"
    with journal(store) as log:

        def link_event(fd: int) -> None:
            real_sync(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.link(max(log.root.iterdir()), alias)

        monkeypatch.setattr(os, "fsync", link_event)
        with pytest.raises(RuntimeError):
            log.record("ready", {})
        assert alias.exists() and alias.stat().st_nlink == 2
        assert len(list(log.root.iterdir())) == 2


def test_event_corruption_after_directory_sync_boundary_is_rejected(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sync = module()._Directory.sync
    with journal(store) as log:

        def corrupt_after_directory_sync(directory: Any) -> None:
            real_sync(directory)
            path = max(log.root.iterdir())
            data = path.read_bytes()
            path.write_bytes(b"!" + data[1:])
            os.utime(path, ns=(1_000_000_000, 2_000_000_000))

        monkeypatch.setattr(module()._Directory, "sync", corrupt_after_directory_sync)
        with pytest.raises(RuntimeError):
            log.record("ready", {})
        assert len(list(log.root.iterdir())) == 2
        with pytest.raises(RuntimeError):
            log.record("rename_attempted", {})


@pytest.mark.parametrize("fault", ["open", "sync", "identity"])
def test_constructor_failures_close_all_descriptors(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    tracked: set[int] = set()
    real_open, real_close, real_sync = os.open, os.close, os.fsync

    def open_or_fail(*args: Any, **kwargs: Any) -> int:
        if fault == "open" and str(args[0]).endswith(".json"):
            raise OSError("constructor open fault")
        fd = real_open(*args, **kwargs)
        tracked.add(fd)
        return fd

    def track_close(fd: int) -> None:
        real_close(fd)
        tracked.discard(fd)

    def sync_or_fail(fd: int) -> None:
        if fault == "sync":
            raise OSError("constructor sync fault")
        real_sync(fd)
        if fault == "identity" and stat.S_ISREG(os.fstat(fd).st_mode):
            path = next((store / ".stage-ownership").rglob("*.json"))
            os.link(path, store / "unexpected-alias")

    monkeypatch.setattr(os, "open", open_or_fail)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(os, "fsync", sync_or_fail)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        journal(store)
    assert not tracked


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX nonregular leaf")
def test_fifo_event_collision_does_not_block_or_get_repaired(store: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX FIFO fixture required")
    with journal(store) as log:
        first = next(log.root.iterdir())
        second = first.with_name(first.name.replace("000001", "000002"))
        os.mkfifo(second, mode=0o600)
        with pytest.raises((OSError, ValueError, RuntimeError)):
            log.record("ready", {})
        assert stat.S_ISFIFO(second.lstat().st_mode)


@pytest.mark.parametrize("fault", ["write", "zero_write", "file_sync", "directory_sync"])
def test_failed_record_retains_unknown_event_and_disables_further_writes(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real_write, real_sync = os.write, os.fsync
    with journal(store) as log:
        log.record("ready", {})
        calls = 0

        def fail_write(fd: int, data: bytes | memoryview) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:7])
            if fault == "zero_write":
                return 0
            raise OSError("injected write failure")

        def fail_sync(fd: int) -> None:
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if directory == (fault == "directory_sync"):
                raise OSError("injected fsync failure")
            real_sync(fd)

        if fault == "directory_sync" and sys.platform == "win32":
            pytest.skip("Windows provides no directory fsync")
        with monkeypatch.context() as faults:
            faults.setattr(
                os,
                "write" if fault in {"write", "zero_write"} else "fsync",
                fail_write if fault in {"write", "zero_write"} else fail_sync,
            )
            with pytest.raises((OSError, RuntimeError)):
                log.record("rename_attempted", {})
        saved = {path.name: path.read_bytes() for path in log.root.iterdir()}
        assert len(saved) == 3
        with pytest.raises(RuntimeError):
            log.record("published", {})
        assert {path.name: path.read_bytes() for path in log.root.iterdir()} == saved


def test_record_byte_and_event_count_limits(store: Path) -> None:
    with journal(store) as log:
        with pytest.raises(ValueError):
            log.record("recording", {"path": "x" * 1_048_576})
        assert len(events(store)) == 1
        for _ in range(4095):
            log.record("recording", {})
        with pytest.raises(ValueError):
            log.record("ready", {})
        assert len(events(store)) == 4096
        assert max(path.stat().st_size for path in log.root.iterdir()) <= 1_048_576


@pytest.mark.parametrize(
    "details", [{"bytes": b"source"}, {"value": float("nan")}, {"object": object()}, [], {1: "key"}]
)
def test_non_json_metadata_does_not_create_event(store: Path, details: object) -> None:
    with journal(store) as log, pytest.raises((TypeError, ValueError)):
        log.record("recording", details)
    assert len(events(store)) == 1


def test_constructor_failure_closes_opened_descriptors(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked: set[int] = set()
    real_open, real_close = os.open, os.close

    def track_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        tracked.add(fd)
        return fd

    def track_close(fd: int) -> None:
        real_close(fd)
        tracked.discard(fd)

    def fail_write(fd: int, data: bytes | memoryview) -> int:
        raise OSError("intent write failed")

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="intent write failed"):
        journal(store)
    assert not tracked
    assert len(list((store / ".stage-ownership").rglob("*.json"))) == 1


def test_caller_exception_does_not_write_aborted_or_get_masked(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = os.close
    log = journal(store)

    def failed_close(fd: int) -> None:
        real_close(fd)
        raise OSError("close failed after closing")

    monkeypatch.setattr(os, "close", failed_close)
    with pytest.raises(LookupError, match="caller failure"), log:
        raise LookupError("caller failure")
    assert [event["state"] for event in events(store)] == ["creating"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and fsync ordering")
def test_private_nodes_and_file_then_directory_sync(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sync = os.fsync
    order: list[str] = []

    def observe(fd: int) -> None:
        order.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_sync(fd)

    with journal(store) as log:
        monkeypatch.setattr(os, "fsync", observe)
        log.record("ready", {})
        assert order == ["file", "directory"]
        for path in [log.root, *log.root.parents[:3]]:
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in log.root.iterdir():
            info = path.stat()
            assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            assert stat.S_IMODE(info.st_mode) == 0o600
