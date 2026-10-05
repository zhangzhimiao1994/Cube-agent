"""Offline security/command fixtures, never real Linux or 20-project acceptance."""

from __future__ import annotations

import errno
import importlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest


def broker() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.dynamic_broker"), "Task1 broker missing"
    return importlib.import_module("agent_hub.previews.dynamic_broker")


def linux_metadata(monkeypatch: pytest.MonkeyPatch, metadata: dict[Path, tuple[int, int]]) -> None:
    """Model only listed inode ownership/modes; keep real guards and file I/O."""
    from types import SimpleNamespace
    original_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        actual = original_lstat(path)
        if path not in metadata:
            return actual
        uid, mode = metadata[path]
        values = list(actual)
        values[0], values[4] = mode, uid
        return os.stat_result(values, {"st_file_attributes": getattr(actual, "st_file_attributes", 0)})

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(broker(), "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(broker(), "_PLATFORM", "linux")


def prepared(tmp_path: Path) -> tuple[Path, Any]:
    mod = broker()
    root = tmp_path / ".preview-staging" / "fixture"
    root.mkdir(parents=True)
    (root / "package.json").write_text('{"scripts":{"start":"node server.js"}}')
    (root / "server.js").write_text("fixture only")
    uid = getattr(os, "getuid", lambda: 10001)()
    return root, mod.PreviewBrokerPolicy(workspace_root=tmp_path, allowed_uid=uid)


def test_uid_and_snapshot_identity_are_fixed(tmp_path: Path) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    request = {"version": 1, "action": "start", "source_root": str(root),
               "preview_id": "fixture", "lifetime_seconds": 30}
    with pytest.raises(ValueError, match="uid"):
        mod.validate_broker_request(request, peer_uid=policy.allowed_uid + 1, policy=policy)
    mod.validate_broker_request(request, peer_uid=policy.allowed_uid, policy=policy)
    before = mod.inspect_source(root, policy)
    (root / "server.js").write_text("changed")
    assert mod.inspect_source(root, policy) != before
    request["host"] = "localhost"
    with pytest.raises(ValueError):
        mod.validate_broker_request(request, peer_uid=policy.allowed_uid, policy=policy)


@pytest.mark.parametrize("name,content", [
    (".npmrc", "registry=evil"), ("node_modules/x", "evil"), (".env", "sentinel"),
    ("package.json", '{}'), ("package.json", '{"scripts":{"start":3}}'),
    ("package.json", '{"scripts":{"start":"node a"},"dependencies":{"x":"file:../x"}}'),
])
def test_rejects_unprepared_manifest_or_host_inputs(tmp_path: Path, name: str, content: str) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(ValueError):
        mod.inspect_source(root, policy)


def test_rejects_outside_staging_and_size_overflow(tmp_path: Path) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    with pytest.raises(ValueError):
        mod.inspect_source(tmp_path, policy)
    (root / "huge").write_bytes(b"x" * 100)
    from dataclasses import replace
    with pytest.raises(ValueError, match="size"):
        mod.inspect_source(root, replace(policy, max_source_bytes=50))


@pytest.mark.skipif(os.name == "nt", reason="Linux file link fixture; no Windows host execution")
def test_rejects_links(tmp_path: Path) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    (root / "link").symlink_to(root / "server.js")
    with pytest.raises(ValueError):
        mod.inspect_source(root, policy)


@pytest.mark.parametrize("stage", ["install", "build", "start", "probe"])
def test_fixed_systemd_isolation_and_resource_contract(tmp_path: Path, stage: str) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    command = mod.build_systemd_command(policy, "a" * 32, stage, Path("/run/preview/owned"), 30)
    joined = " ".join(command)
    assert command[0] == "/usr/bin/systemd-run"
    assert "SupplementaryGroups=agent-hub" not in joined
    for required in ["Slice=system.slice", "KillMode=control-group", "TasksMax=", "MemoryMax=", "CPUQuota=",
                     "LimitFSIZE=", "NoNewPrivileges=yes", "ProtectHome=yes", "RootDirectory=",
                     "InaccessiblePaths=", "/usr/bin/python3", " -I ", "dynamic_runner.py"]:
        assert required in joined
    assert ("PrivateNetwork=yes" in joined) == (stage != "install")
    assert "--pipe" in command
    assert " -m agent_hub" not in joined
    if stage == "probe":
        assert ":/preview/source" not in joined


@pytest.mark.parametrize("payload", [
    {"version": True, "action": "probe"}, {"version": 1, "action": "probe", "command": "id"},
    {"version": 1, "action": "stop", "handle": "../evil"},
    {"version": 1, "action": "start", "source_root": "/", "preview_id": "../x",
     "lifetime_seconds": True},
])
def test_strict_broker_actions(tmp_path: Path, payload: dict[str, object]) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    with pytest.raises(ValueError):
        mod.validate_broker_request(payload, peer_uid=policy.allowed_uid, policy=policy)


def test_snapshot_is_frozen_and_not_affected_by_caller_writes(tmp_path: Path) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    frozen = tmp_path / "root-owned-source"
    frozen.mkdir()
    digest = mod._snapshot(root, policy, frozen)
    (root / "server.js").write_text("caller changed")
    assert (frozen / "server.js").read_text() == "fixture only"
    assert digest != mod.inspect_source(root, policy)


@pytest.mark.parametrize("stage", ["install", "build", "start", "probe"])
def test_install_metadata_file_uses_existing_disk_budget_only(tmp_path: Path, stage: str) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    owned = Path("/run/preview/owned")
    command = mod.build_systemd_command(policy, "a" * 32, stage, owned, 30)
    storage = mod.build_storage_command(policy, "a" * 32, owned)
    options = next(item.removeprefix("--options=") for item in storage
                   if item.startswith("--options="))
    size = dict(item.split("=", 1) for item in options.split(",") if "=" in item)["size"]
    assert size == "256M"
    expected = int(size[:-1]) * 1024 * 1024 if stage == "install" else 32 * 1024 * 1024
    assert [item for item in command if item.startswith("LimitFSIZE=")] == [
        f"LimitFSIZE={expected}",
    ]
    memory = 384 + int(size[:-1]) if stage == "install" else 384
    assert [item for item in command if item.startswith("MemoryMax=")] == [
        f"MemoryMax={memory}M",
    ]
    assert "LimitNOFILE=256" in command
    assert "MemorySwapMax=0" in command
    assert "TasksMax=64" in command
    assert "CPUQuota=50%" in command


def test_full_tree_cleanup_failure_retains_quota_and_owned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root, max_sessions=1))
    owner = object()
    session = service._reserve("fixture", owner, 30)
    session.units.add("agent-hub-preview-fixture-start.service")

    def fail(unit: str) -> None:
        raise RuntimeError("tree still populated fixture")

    monkeypatch.setattr(service, "_stop_unit", fail)
    with pytest.raises(RuntimeError, match="populated"):
        service.disconnect(owner)
    assert session.revoked
    assert session.owned.exists()
    with pytest.raises(RuntimeError, match="capacity"):
        service._reserve("other", object(), 30)
    monkeypatch.setattr(service, "_stop_unit", lambda unit: None)
    service.disconnect(owner)
    assert not session.owned.exists()
    assert not service._sessions


def test_reaper_cleans_other_sessions_when_one_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    failed = service._reserve("failed", object(), 30)
    other = service._reserve("other", object(), 30)
    failed.revoked = other.revoked = True
    failed.units.add("failed-unit")

    def fail(unit: str) -> None:
        raise RuntimeError("cleanup fixture")

    monkeypatch.setattr(service, "_stop_unit", fail)
    with pytest.raises(RuntimeError):
        service.reap()
    assert not other.owned.exists()
    assert failed.handle in service._sessions


def test_cgroup_descendants_block_cleanup_even_without_main_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    observed: list[tuple[str, ...]] = []
    unit = f"agent-hub-preview-{'a' * 32}-start.service"

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        observed.append(argv)
        if "show" in argv:
            return subprocess.CompletedProcess(argv, 0,
                (f"Id={unit}\nLoadState=loaded\nActiveState=deactivating\n"
                 f"MainPID=0\nControlGroup=/system.slice/{unit}\n").encode())
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(service, "_command", command)
    with pytest.raises(RuntimeError, match="cgroup"):
        service._stop_unit(unit)
    assert [call[1] for call in observed] == ["show", "stop", "show"]


_CLEANUP_UNIT = "agent-hub-preview-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-start.service"
_CLEANUP_GROUP = "/system.slice/" + _CLEANUP_UNIT
_CLEANUP_ABSENT = b"LoadState=not-found\nActiveState=inactive\nMainPID=0\nControlGroup=\n"
_CLEANUP_LOADED = b"LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n"


@pytest.fixture
def cleanup_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An owned temporary filesystem, with Linux directory metadata on Windows."""
    root = tmp_path / "cgroup" / "system.slice"
    root.mkdir(parents=True)
    group = root / _CLEANUP_UNIT
    metadata = dict.fromkeys((group, root, *root.parents), (0, stat.S_IFDIR | 0o755))
    linux_metadata(monkeypatch, metadata)
    monkeypatch.setattr(broker(), "_CGROUP_ROOT", root, raising=False)
    return group


def cleanup_commands(
    monkeypatch: pytest.MonkeyPatch, service: Any, unit: str, output: bytes,
    *, query_rc: int = 0, stop_rc: int = 0,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert argv[0] == "/usr/bin/systemctl"
        if argv[1] == "show":
            properties = "Id,LoadState,ActiveState"
            if not unit.endswith(".mount"):
                properties += ",MainPID,ControlGroup"
            assert argv == (argv[0], "show", unit, "--no-pager", "--property=" + properties)
            return subprocess.CompletedProcess(argv, query_rc, b"Id=" + unit.encode() + b"\n" + output)
        if argv[1] == "stop":
            assert argv == (argv[0], "stop", unit)
            return subprocess.CompletedProcess(argv, stop_rc, b"")
        assert argv == (argv[0], "kill", "--kill-whom=all", "--signal=KILL", unit)
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(service, "_command", command)
    return calls


def cleanup_identity_commands(
    monkeypatch: pytest.MonkeyPatch, service: Any, unit: str, before: bytes, after: bytes,
    *, pre_rc: int = 0, stop_rc: int = 0,
) -> list[str]:
    calls: list[str] = []

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        assert argv[0] == "/usr/bin/systemctl" and unit in argv
        calls.append(argv[1])
        if argv[1] == "show":
            properties = "Id,LoadState,ActiveState"
            if unit.endswith(".service"):
                properties += ",MainPID,ControlGroup"
            assert argv == (argv[0], "show", unit, "--no-pager", "--property=" + properties)
            first = calls.count("show") == 1
            return subprocess.CompletedProcess(argv, pre_rc if first else 0,
                                               before if first else after)
        return subprocess.CompletedProcess(argv, stop_rc if argv[1] == "stop" else 0, b"")

    monkeypatch.setattr(service, "_command", command)
    return calls


@pytest.mark.parametrize("fault", [
    "alias", "foreign-cgroup", "missing-id", "duplicate-id", "query-failed",
    "malformed", "missing-state", "unknown-state", "malformed-pid", "contradictory-absent",
])
@pytest.mark.parametrize("stop_rc", [0, 1])
def test_cleanup_prequery_rejects_unowned_or_unknown_service_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, stop_rc: int,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    before = b"Id=" + _CLEANUP_UNIT.encode() + b"\n" + _CLEANUP_LOADED
    if fault == "alias":
        before = before.replace(_CLEANUP_UNIT.encode(), _CLEANUP_UNIT.replace("start", "build").encode())
    elif fault == "foreign-cgroup":
        before = before.replace(b"ControlGroup=", b"ControlGroup=/system.slice/unrelated.service")
    elif fault == "missing-id":
        before = _CLEANUP_LOADED
    elif fault == "duplicate-id":
        before += b"Id=" + _CLEANUP_UNIT.encode() + b"\n"
    elif fault == "malformed":
        before += b"malformed\n"
    elif fault == "missing-state":
        before = before.replace(b"ActiveState=inactive\n", b"")
    elif fault == "unknown-state":
        before = before.replace(b"inactive", b"unknown")
    elif fault == "malformed-pid":
        before = before.replace(b"MainPID=0", b"MainPID=-1")
    elif fault == "contradictory-absent":
        before = before.replace(b"loaded", b"not-found").replace(b"MainPID=0", b"MainPID=12")
    calls = cleanup_identity_commands(monkeypatch, service, _CLEANUP_UNIT, before, before,
                                      pre_rc=int(fault == "query-failed"), stop_rc=stop_rc)
    with pytest.raises(RuntimeError):
        service._stop_unit(_CLEANUP_UNIT)
    assert calls == ["show"]


@pytest.mark.parametrize("stage", ["install", "build", "start", "probe"])
@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_cleanup_prequery_accepts_running_owned_service_then_checks_stopped_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
    stage: str, state: str,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    unit = _CLEANUP_UNIT.replace("-start", "-" + stage)
    identity = b"Id=" + unit.encode() + b"\n"
    before = identity + (f"LoadState=loaded\nActiveState={state}\nMainPID=123\n"
                         f"ControlGroup=/system.slice/{unit}\n").encode()
    calls = cleanup_identity_commands(monkeypatch, service, unit, before, identity + _CLEANUP_ABSENT)
    service._stop_unit(unit)
    assert calls == ["show", "stop", "show"]


@pytest.mark.parametrize("after", [
    b"Id=unrelated.service\n" + _CLEANUP_ABSENT,
    b"Id=" + _CLEANUP_UNIT.encode() + b"\n" + _CLEANUP_LOADED.replace(b"MainPID=0", b"MainPID=12"),
    b"Id=" + _CLEANUP_UNIT.encode() + b"\n" + _CLEANUP_LOADED.replace(b"ControlGroup=", b"ControlGroup=/other"),
])
def test_cleanup_prequery_does_not_replace_post_stop_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: bytes,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    before = b"Id=" + _CLEANUP_UNIT.encode() + b"\n" + _CLEANUP_LOADED
    calls = cleanup_identity_commands(monkeypatch, service, _CLEANUP_UNIT, before, after)
    with pytest.raises(RuntimeError):
        service._stop_unit(_CLEANUP_UNIT)
    assert calls == ["show", "stop", "show"]


@pytest.mark.parametrize("fault", ["alias", "missing-id", "query-failed", "contradictory-absent"])
def test_cleanup_prequery_rejects_unowned_or_unknown_mount_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    mod = broker()
    service = mod.PreviewBroker(prepared(tmp_path)[1])
    session = mod._Session("a" * 32, "fixture", object(), tmp_path, 0,
                           mounted=True, mount_unit="fixture.mount")
    before = b"Id=fixture.mount\nLoadState=loaded\nActiveState=active\n"
    if fault == "alias":
        before = before.replace(b"Id=fixture.mount", b"Id=foreign.mount")
    elif fault == "missing-id":
        before = before.replace(b"Id=fixture.mount\n", b"")
    elif fault == "contradictory-absent":
        before = before.replace(b"loaded", b"not-found")
    calls = cleanup_identity_commands(monkeypatch, service, "fixture.mount", before, before,
                                      pre_rc=int(fault == "query-failed"))
    with pytest.raises(RuntimeError):
        service._stop_storage(session)
    assert calls == ["show"] and session.mounted


@pytest.mark.parametrize("entry", ["stop", "restart"])
@pytest.mark.parametrize("target", ["work", "owned"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_cleanup_mount_lstat_error_retains_quota_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    entry: str, target: str, error_number: int,
) -> None:
    import posixpath
    from dataclasses import replace
    mod = broker()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    service = mod.PreviewBroker(replace(prepared(tmp_path)[1], runtime_root=runtime, max_sessions=1))
    session = service._reserve("fixture", object(), 30)
    linux_metadata(monkeypatch, {session.owned: (0, stat.S_IFDIR | 0o755)})
    session.mounted = True
    session.mount_unit = "fixture.mount"
    work = session.owned / "work"
    work.mkdir()
    before = b"Id=fixture.mount\nLoadState=loaded\nActiveState=active\n"
    after = b"Id=fixture.mount\nLoadState=loaded\nActiveState=inactive\n"
    calls: list[str] = []

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv[1])
        if argv[1] == "show":
            assert argv[-1] == "--property=Id,LoadState,ActiveState"
            data = before if calls.count("show") == 1 else after
            return subprocess.CompletedProcess(argv, 0, data)
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(service, "_command", command)
    monkeypatch.setattr(service, "_mount_unit_name", lambda owned: "fixture.mount")
    monkeypatch.setattr(service, "_stop_unit", lambda unit: None)
    monkeypatch.setattr(os.path, "ismount", posixpath.ismount)
    actual_lstat = os.lstat
    failures: list[int] = []
    selected = work if target == "work" else session.owned

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path) == selected and not failures and (target == "work" or "show" in calls):
            failures.append(error_number)
            raise OSError(error_number, "fixture mount observation unavailable")
        return actual_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    if entry == "restart":
        service._sessions.clear()
    with pytest.raises((OSError, RuntimeError)):
        service.recover() if entry == "restart" else service._stop(session)
    assert failures == [error_number]
    retained = service._sessions[session.handle]
    assert retained.mounted and not retained.stopped and retained.revoked
    assert work.is_dir() and session.handle not in service._completed
    with pytest.raises(RuntimeError, match="capacity"):
        service._reserve("blocked", object(), 30)
    service.reap()
    assert not service._sessions and not session.owned.exists()
    assert session.handle in service._completed
    assert service._reserve("new", object(), 30).handle in service._sessions


@pytest.mark.parametrize("state", ["pending", "never-created"])
@pytest.mark.parametrize("work_exists", [False, True])
def test_cleanup_restart_checks_mount_unit_even_without_visible_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, work_exists: bool,
) -> None:
    from dataclasses import replace
    mod = broker()
    runtime = tmp_path / "runtime"
    owned = runtime / ("a" * 32)
    owned.mkdir(parents=True)
    if work_exists:
        (owned / "work").mkdir()
    service = mod.PreviewBroker(replace(prepared(tmp_path)[1], runtime_root=runtime))
    linux_metadata(monkeypatch, {owned: (0, stat.S_IFDIR | 0o755)})
    monkeypatch.setattr(service, "_stop_unit", lambda unit: None)
    monkeypatch.setattr(service, "_mount_unit_name", lambda path: "fixture.mount")
    before = b"Id=fixture.mount\nLoadState=loaded\nActiveState=activating\n"
    after = b"Id=fixture.mount\nLoadState=not-found\nActiveState=inactive\n"
    calls = cleanup_identity_commands(monkeypatch, service, "fixture.mount",
                                      before if state == "pending" else after, after,
                                      stop_rc=0 if state == "pending" else 5)
    service.recover()
    assert calls == ["show", "stop", "show"]
    assert not owned.exists() and not service._sessions
    assert owned.name in service._completed


@pytest.mark.parametrize("output,query_rc", [
    pytest.param(_CLEANUP_ABSENT, 1, id="failed-not-found-query"),
    pytest.param(_CLEANUP_LOADED, 1, id="failed-loaded-query"),
    pytest.param(b"", 0, id="empty"),
    pytest.param(_CLEANUP_ABSENT.replace(b"inactive", b"active"), 0, id="not-found-active"),
    pytest.param(_CLEANUP_ABSENT.replace(b"inactive", b"failed"), 0, id="not-found-failed"),
    pytest.param(_CLEANUP_ABSENT.replace(b"MainPID=0", b"MainPID=9"), 0, id="not-found-pid"),
    pytest.param(_CLEANUP_ABSENT.replace(b"ControlGroup=", b"ControlGroup=" + _CLEANUP_GROUP.encode()), 0, id="not-found-group"),
    pytest.param(_CLEANUP_LOADED.replace(b"LoadState=loaded\n", b""), 0, id="missing-load"),
    pytest.param(_CLEANUP_ABSENT.replace(b"ActiveState=inactive\n", b""), 0, id="missing-active"),
    pytest.param(_CLEANUP_ABSENT.replace(b"MainPID=0\n", b""), 0, id="missing-pid"),
    pytest.param(_CLEANUP_LOADED.replace(b"ControlGroup=\n", b""), 0, id="missing-group"),
    pytest.param(_CLEANUP_LOADED.replace(b"loaded", b"error"), 0, id="unknown-load"),
    pytest.param(_CLEANUP_LOADED.replace(b"inactive", b"deactivating"), 0, id="still-stopping"),
    pytest.param(_CLEANUP_LOADED.replace(b"MainPID=0", b"MainPID=00"), 0, id="malformed-pid"),
    pytest.param(_CLEANUP_LOADED + b"garbage\n", 0, id="malformed-line"),
    pytest.param(_CLEANUP_LOADED.replace(b"\n", b"\v"), 0, id="malformed-line-ending"),
    pytest.param(_CLEANUP_LOADED + b"Unknown=value\n", 0, id="unexpected-field"),
    pytest.param(_CLEANUP_LOADED + b"\xff\n", 0, id="invalid-encoding"),
    *[pytest.param(_CLEANUP_LOADED + line + b"\n", 0, id="duplicate-" + line.split(b"=")[0].decode())
      for line in _CLEANUP_LOADED.splitlines()],
])
def test_cleanup_rejects_unknown_service_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
    output: bytes, query_rc: int,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, output, query_rc=query_rc)
    with pytest.raises(RuntimeError):
        service._stop_unit(_CLEANUP_UNIT)


@pytest.mark.parametrize("unit", [
    "other.service", "agent-hub-preview-fixture-start.service",
    _CLEANUP_UNIT.replace("-start", "-shell"), _CLEANUP_UNIT.replace("aaaa", "AAAA"),
    "../" + _CLEANUP_UNIT, _CLEANUP_UNIT + "/child", _CLEANUP_UNIT + "\n",
])
def test_cleanup_rejects_nonowned_unit_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit: str,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    calls = cleanup_commands(monkeypatch, service, unit, _CLEANUP_ABSENT)
    with pytest.raises(RuntimeError):
        service._stop_unit(unit)
    assert calls == []


@pytest.mark.parametrize("reported", [
    "/unrelated.service", _CLEANUP_GROUP + "/child", _CLEANUP_GROUP + "/../other",
    "/system.slice//" + _CLEANUP_UNIT, "/system.slice/./" + _CLEANUP_UNIT,
    _CLEANUP_GROUP.replace("system.slice", "user.slice"),
    _CLEANUP_GROUP.replace("aaaa", "bbbb"), _CLEANUP_GROUP.replace("-start", "-build"),
    _CLEANUP_GROUP + "/", _CLEANUP_GROUP.removeprefix("/"),
    _CLEANUP_GROUP.replace("-start", "\\x2dstart"),
])
def test_cleanup_rejects_cgroup_alias_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path, reported: str,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    output = _CLEANUP_LOADED.replace(b"ControlGroup=", b"ControlGroup=" + reported.encode())
    cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, output)

    def unexpected_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("mismatched systemd path must not cause filesystem reads")

    monkeypatch.setattr(Path, "lstat", unexpected_read)
    with pytest.raises(RuntimeError):
        service._stop_unit(_CLEANUP_UNIT)


@pytest.mark.parametrize("output", [_CLEANUP_ABSENT, _CLEANUP_LOADED])
@pytest.mark.parametrize("events", [
    None, b"", b"frozen 0\n", b"populated 1\n", b"populated 2\n", b"populated 00\n",
    b"populated 0\npopulated 0\n", b"populated 1\npopulated 0\n",
    b"populated=0\n", b"populated 0 extra\n", b"populated 0\ngarbage\n", b"\xff\n",
    pytest.param(b"populated 0\vfrozen 0\n", id="malformed-line-ending"),
])
def test_cleanup_rejects_surviving_canonical_cgroup_without_empty_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
    output: bytes, events: bytes | None,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, output)
    cleanup_cgroup.mkdir()
    if events is not None:
        (cleanup_cgroup / "cgroup.events").write_bytes(events)
    with pytest.raises((OSError, RuntimeError)):
        service._stop_unit(_CLEANUP_UNIT)


@pytest.mark.parametrize("output,stop_rc", [
    (_CLEANUP_ABSENT, 0), (_CLEANUP_ABSENT, 5), (_CLEANUP_LOADED, 0),
    (_CLEANUP_LOADED.replace(b"inactive", b"failed"), 0),
    (_CLEANUP_LOADED.replace(b"ControlGroup=", b"ControlGroup=" + _CLEANUP_GROUP.encode()), 0),
])
@pytest.mark.parametrize("exists", [False, True])
def test_cleanup_accepts_confirmed_absent_or_empty_cgroup_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
    output: bytes, stop_rc: int, exists: bool,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, output, stop_rc=stop_rc)
    if exists:
        cleanup_cgroup.mkdir()
        (cleanup_cgroup / "cgroup.events").write_bytes(b"populated 0\nfrozen 0\n")
    service._stop_unit(_CLEANUP_UNIT)
    service._stop_unit(_CLEANUP_UNIT)


def test_cleanup_loaded_unit_retains_failed_stop_even_with_empty_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    calls = cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, _CLEANUP_LOADED, stop_rc=1)
    with pytest.raises(RuntimeError):
        service._stop_unit(_CLEANUP_UNIT)
    assert [call[1] for call in calls] == ["show", "stop", "kill", "show"]


@pytest.mark.parametrize("target", ["parent", "group", "events"])
@pytest.mark.parametrize("kind", ["permission", "io", "link", "reparse", "wrong-type"])
def test_cleanup_rejects_unobservable_or_aliased_cgroup_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
    target: str, kind: str,
) -> None:
    from types import SimpleNamespace
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, _CLEANUP_ABSENT)
    cleanup_cgroup.mkdir()
    events = cleanup_cgroup / "cgroup.events"
    events.write_bytes(b"populated 0\n")
    selected = {"parent": cleanup_cgroup.parent, "group": cleanup_cgroup, "events": events}[target]
    original = Path.lstat

    def lstat(path: Path) -> Any:
        info = original(path)
        if path != selected:
            return info
        if kind == "permission":
            raise PermissionError("fixture")
        if kind == "io":
            raise OSError("fixture")
        return SimpleNamespace(
            st_mode=(stat.S_IFLNK if kind == "link" else stat.S_IFIFO if kind == "wrong-type"
                     else info.st_mode) | 0o555,
            st_uid=0, st_file_attributes=0x400 if kind == "reparse" else 0, st_nlink=1,
        )

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises((OSError, RuntimeError)):
        service._stop_unit(_CLEANUP_UNIT)


@pytest.mark.parametrize("disappear", ["none", "group", "parent"])
@pytest.mark.parametrize("error_type", [PermissionError, FileNotFoundError, OSError])
def test_cleanup_event_read_failure_requires_explicit_group_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path,
    disappear: str, error_type: type[OSError],
) -> None:
    service = broker().PreviewBroker(prepared(tmp_path)[1])
    cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, _CLEANUP_ABSENT)
    cleanup_cgroup.mkdir()
    events = cleanup_cgroup / "cgroup.events"
    events.write_bytes(b"populated 0\n")
    original = os.open
    attempted = False

    def open_events(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal attempted
        if Path(path) == events:
            attempted = True
            if disappear != "none":
                events.unlink()
                cleanup_cgroup.rmdir()
                if disappear == "parent":
                    cleanup_cgroup.parent.rmdir()
            raise error_type("fixture")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_events)
    if disappear == "group":
        service._stop_unit(_CLEANUP_UNIT)
    else:
        with pytest.raises((OSError, RuntimeError)):
            service._stop_unit(_CLEANUP_UNIT)
    assert attempted


@pytest.mark.parametrize("output,query_rc,stop_rc,mounted", [
    (b"LoadState=not-found\nActiveState=inactive\n", 1, 0, False),
    (b"LoadState=not-found\nActiveState=active\n", 0, 0, False),
    (b"LoadState=not-found\n", 0, 0, False),
    (b"ActiveState=inactive\n", 0, 0, False),
    (b"LoadState=error\nActiveState=inactive\n", 0, 0, False),
    (b"LoadState=loaded\nActiveState=inactive\nLoadState=not-found\n", 0, 0, False),
    (b"LoadState=loaded\nActiveState=active\nActiveState=inactive\n", 0, 0, False),
    (b"LoadState=loaded\nActiveState=inactive\nmalformed\n", 0, 0, False),
    pytest.param(b"LoadState=loaded\vActiveState=inactive\n", 0, 0, False,
                 id="malformed-line-ending"),
    (b"LoadState=loaded\nActiveState=inactive\n\xff\n", 0, 0, False),
    (b"LoadState=loaded\nActiveState=inactive\n", 1, 0, False),
    (b"LoadState=loaded\nActiveState=inactive\n", 0, 1, False),
    (b"LoadState=loaded\nActiveState=active\n", 0, 0, False),
    (b"LoadState=not-found\nActiveState=inactive\n", 0, 0, True),
    (b"LoadState=loaded\nActiveState=inactive\n", 0, 0, True),
])
def test_cleanup_rejects_unknown_or_surviving_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    output: bytes, query_rc: int, stop_rc: int, mounted: bool,
) -> None:
    mod = broker()
    service = mod.PreviewBroker(prepared(tmp_path)[1])
    session = mod._Session("a" * 32, "fixture", object(), tmp_path, 0,
                           mounted=True, mount_unit="fixture.mount")
    cleanup_commands(monkeypatch, service, "fixture.mount", output,
                     query_rc=query_rc, stop_rc=stop_rc)
    work = tmp_path / "work"
    work.mkdir()
    actual_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = actual_lstat(path, *args, **kwargs)
        if Path(path) == work and mounted:
            values = list(info)
            values[2] += 1
            return os.stat_result(values, {"st_file_attributes": getattr(info, "st_file_attributes", 0)})
        return info

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(RuntimeError):
        service._stop_storage(session)
    assert session.mounted


@pytest.mark.parametrize("load,stop_rc", [("loaded", 0), ("not-found", 0), ("not-found", 5)])
def test_cleanup_accepts_confirmed_unmounted_storage_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, load: str, stop_rc: int,
) -> None:
    mod = broker()
    service = mod.PreviewBroker(prepared(tmp_path)[1])
    session = mod._Session("a" * 32, "fixture", object(), tmp_path, 0,
                           mounted=True, mount_unit="fixture.mount")
    output = f"LoadState={load}\nActiveState=inactive\n".encode()
    cleanup_commands(monkeypatch, service, "fixture.mount", output, stop_rc=stop_rc)
    service._stop_storage(session)
    assert not session.mounted
    service._stop_storage(session)


@pytest.mark.parametrize("failure", ["query", "cgroup", "mount", "remove"])
def test_cleanup_failure_retains_resources_quota_and_reaper_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cgroup: Path, failure: str,
) -> None:
    import shutil
    from dataclasses import replace
    from types import SimpleNamespace
    mod = broker()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    service = mod.PreviewBroker(replace(prepared(tmp_path)[1], runtime_root=runtime, max_sessions=2))
    with monkeypatch.context() as fixed_handle:
        fixed_handle.setattr(mod.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
        failed = service._reserve("failed", object(), 30)
    other = service._reserve("other", object(), 30)
    failed.revoked = other.revoked = True
    failed.units.add(_CLEANUP_UNIT)
    (failed.owned / "sentinel").write_text("retained")
    cleanup_cgroup.mkdir()
    events = cleanup_cgroup / "cgroup.events"
    events.write_bytes(b"populated 1\n" if failure == "cgroup" else b"populated 0\n")
    if failure == "mount":
        failed.units.clear()
        failed.mounted = True
        failed.mount_unit = "fixture.mount"
        cleanup_commands(monkeypatch, service, "fixture.mount",
                         b"LoadState=not-found\nActiveState=active\n")
    else:
        cleanup_commands(monkeypatch, service, _CLEANUP_UNIT, _CLEANUP_ABSENT,
                         query_rc=1 if failure == "query" else 0)
    remove = shutil.rmtree

    def remove_owned(path: Path) -> None:
        if failure == "remove" and path == failed.owned:
            raise PermissionError("fixture")
        remove(path)

    monkeypatch.setattr(shutil, "rmtree", remove_owned)
    with pytest.raises(RuntimeError, match="retained for retry"):
        service.reap()
    assert failed.handle in service._sessions
    assert failed.handle not in service._completed
    assert not failed.stopped
    assert (failed.owned / "sentinel").read_text() == "retained"
    assert failed.units == ({_CLEANUP_UNIT} if failure in {"query", "cgroup"} else set())
    assert failed.mounted == (failure == "mount")
    assert other.stopped and not other.owned.exists()
    service.policy = replace(service.policy, max_sessions=1)
    with pytest.raises(RuntimeError, match="capacity"):
        service._reserve("new", object(), 30)
    events.write_bytes(b"populated 0\n")
    unit = "fixture.mount" if failure == "mount" else _CLEANUP_UNIT
    output = b"LoadState=not-found\nActiveState=inactive\n" if failure == "mount" else _CLEANUP_ABSENT
    cleanup_commands(monkeypatch, service, unit, output, stop_rc=5)
    monkeypatch.setattr(shutil, "rmtree", remove)
    service.reap()
    assert failed.stopped and not failed.owned.exists()
    assert failed.handle in service._completed
    assert not service._sessions
    assert service._reserve("new", object(), 30).handle in service._sessions


def test_ownership_rejected_and_expiry_cannot_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    owner = object()
    session = service._reserve("fixture", owner, 30)
    monkeypatch.setattr(mod, "_PLATFORM", "linux", raising=False)
    payload = {"version": 1, "action": "request", "handle": session.handle,
               "request": {"method": "POST", "target": "/tasks", "headers": [], "body": ""}}
    with pytest.raises(ValueError, match="ownership"):
        service.handle(payload, peer_uid=policy.allowed_uid, owner=object())
    session.expires_at = 0
    with pytest.raises(ValueError, match="revoked"):
        service.handle(payload, peer_uid=policy.allowed_uid, owner=owner)
    service.disconnect(owner)


def test_restart_reclaims_only_fixed_owned_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    handle = "a" * 32
    owned = runtime_root / handle
    owned.mkdir()
    (owned / "work").mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    stopped: list[str] = []
    monkeypatch.setattr(service, "_stop_unit", stopped.append)
    monkeypatch.setattr(service, "_mount_unit_name", lambda path: "fixture.mount")
    cleanup_commands(monkeypatch, service, "fixture.mount",
                     b"LoadState=not-found\nActiveState=inactive\n", stop_rc=5)
    linux_metadata(monkeypatch, {owned: (0, stat.S_IFDIR | 0o755)})
    service.recover()
    assert set(stopped) == {f"agent-hub-preview-{handle}-{stage}.service"
                            for stage in ("install", "build", "start", "probe")}
    assert not owned.exists()
    assert not service._sessions


@pytest.mark.parametrize("ready", [None, {"ok": False, "error": "failed fixture"},
                                  {"ok": True, "state": "starting"}])
def test_launch_requires_real_ready_and_tracks_failed_unit_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: dict[str, object] | None,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    session = service._reserve("fixture", object(), 30)

    class FixtureProcess:
        pass

    observed: list[tuple[str, ...]] = []

    def popen(argv: tuple[str, ...], **kwargs: object) -> FixtureProcess:
        observed.append(argv)
        return FixtureProcess()

    def read(process: object, timeout: float) -> dict[str, object]:
        if ready is None:
            raise TimeoutError("ready timeout fixture")
        return ready

    # OS command execution is deliberately replaced: no Linux unit or generated
    # command is run by this Windows fixture, and it is not an isolation proof.
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(mod, "_pipe_read", read)
    monkeypatch.setattr(mod, "build_systemd_command", lambda *args: ("/usr/bin/systemd-run",))
    with pytest.raises((RuntimeError, TimeoutError)):
        service._launch(session, "start")
    assert session.process is not None
    assert session.units == {f"agent-hub-preview-{session.handle}-start.service"}
    assert observed == [("/usr/bin/systemd-run",)]


def test_snapshot_identity_includes_executable_mode(tmp_path: Path) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    before = mod.inspect_source(root, policy)
    path = root / "server.js"
    path.chmod(0o755)
    if os.name == "nt":
        pytest.skip("Windows cannot represent POSIX executable-bit changes")
    assert mod.inspect_source(root, policy) != before


def test_prepared_runtime_source_has_no_writable_mount_alias(tmp_path: Path) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    command = mod.build_systemd_command(policy, "a" * 32, "start", Path("/run/preview/owned"), 30)
    assert any("/work/app:/preview/app" in part and "/work/app:/preview/work/app" in part
               for part in command)


def test_broker_root_disk_is_private_before_reserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    metadata = dict.fromkeys(runtime_root.parents, (0, stat.S_IFDIR | 0o755))
    metadata[runtime_root] = (0, stat.S_IFDIR | 0o755)
    linux_metadata(monkeypatch, metadata)
    service._initialize_runtime_root()
    if os.name != "nt":
        assert runtime_root.stat().st_mode & 0o777 == 0o700
    assert service.policy.runtime_root == runtime_root


@pytest.mark.parametrize("ancestor,uid,mode", [
    (False, 1001, stat.S_IFDIR | 0o700),
    (True, 1001, stat.S_IFDIR | 0o755),
    (True, 0, stat.S_IFDIR | 0o775),
    (True, 0, stat.S_IFDIR | 0o757),
    (True, 0, stat.S_IFLNK | 0o755),
])
def test_runtime_initialization_rejects_nonroot_or_unsafe_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestor: bool, uid: int, mode: int,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    root = tmp_path / "broker-owned"
    root.mkdir()
    metadata = dict.fromkeys(root.parents, (0, stat.S_IFDIR | 0o755))
    metadata[root] = (0, stat.S_IFDIR | 0o700)
    metadata[root.parent if ancestor else root] = (uid, mode)
    linux_metadata(monkeypatch, metadata)
    service = mod.PreviewBroker(replace(policy, runtime_root=root))
    with pytest.raises(ValueError, match="immutable root-owned"):
        service._initialize_runtime_root()
    assert not (root / ".recovery-key").exists()


@pytest.mark.parametrize("uid,mode,error", [
    (1001, stat.S_IFDIR | 0o755, "unrecognized preview recovery state"),
    (0, stat.S_IFDIR | 0o777, "unsafe preview recovery directory"),
])
def test_recovery_rejects_untrusted_directory_before_stopping_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uid: int, mode: int, error: str,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    root = tmp_path / "broker-owned"
    owned = root / ("a" * 32)
    owned.mkdir(parents=True)
    linux_metadata(monkeypatch, {owned: (uid, mode)})
    service = mod.PreviewBroker(replace(policy, runtime_root=root))
    stopped: list[str] = []
    monkeypatch.setattr(service, "_stop_unit", stopped.append)
    with pytest.raises(RuntimeError, match=error):
        service.recover()
    assert stopped == []
    assert owned.exists()
    assert not service._sessions


def test_manager_two_hour_lifetime_is_supported(tmp_path: Path) -> None:
    mod = broker()
    root, policy = prepared(tmp_path)
    mod.validate_broker_request({"version": 1, "action": "start", "source_root": str(root),
                                "preview_id": "fixture", "lifetime_seconds": 7200},
                               peer_uid=policy.allowed_uid, policy=policy)


def test_storage_mount_is_owned_by_pid1_without_broker_sys_admin(tmp_path: Path) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    command = mod.build_storage_command(policy, "a" * 32, Path("/run/preview/owned"))
    assert command[0] == "/usr/bin/systemd-mount"
    assert "--type=tmpfs" in command
    assert any("size=256M" in part and "nr_inodes=16384" in part for part in command)
    assert "--property=BindsTo=agent-hub-preview-broker.service" in command
    assert command[-1] == "/run/preview/owned/work"


def test_application_units_die_with_broker_and_own_private_disk(tmp_path: Path) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    command = mod.build_systemd_command(policy, "a" * 32, "start", Path("/run/preview/owned"), 7200)
    assert "BindsTo=agent-hub-preview-broker.service" in command
    assert "RequiresMountsFor=/run/preview/owned/work" in command
    assert any("/bin:/bin" in part for part in command if part.startswith("BindReadOnlyPaths="))


@pytest.mark.parametrize("action", ["probe", "start"])
def test_fixed_trusted_copies_are_readable_before_any_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    from dataclasses import replace
    mod = broker()
    source, policy = prepared(tmp_path)
    release = tmp_path / "release"
    fixed = ("previews/dynamic_runner.py", "harness/project_validation_sandbox.py")
    for name in fixed:
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"trusted fixture, not generated code")
        path.chmod(0o640)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime, trusted_source_root=release))
    validated: list[Path] = []
    modes: dict[Path, int] = {}
    chmod = Path.chmod

    def record_chmod(path: Path, mode: int) -> None:
        modes[path] = mode
        chmod(path, mode)

    def storage(session: Any) -> None:
        trusted = session.owned / "trusted"
        assert modes[trusted] == 0o755
        assert modes[trusted / "harness"] == 0o755
        for origin, target in zip(fixed, ("dynamic_runner.py", "harness/project_validation_sandbox.py"), strict=True):
            assert (trusted / target).read_bytes() == (release / origin).read_bytes()
            assert modes[trusted / target] == 0o444
        assert validated == [release / name for name in fixed]
        raise RuntimeError("fixture stops before OS execution")

    monkeypatch.setattr(mod, "_PLATFORM", "linux")
    monkeypatch.setattr(mod, "_require_root_path", validated.append)
    monkeypatch.setattr(Path, "chmod", record_chmod)
    monkeypatch.setattr(service, "_prepare_storage", storage)
    monkeypatch.setattr(service, "_stop", lambda session: None)
    payload: dict[str, object] = {"version": 1, "action": action}
    if action == "start":
        payload.update(source_root=str(source), preview_id="fixture", lifetime_seconds=30)
    expected_type = mod.ProbeFailure if action == "probe" else mod.PreviewStartupFailure
    with pytest.raises(expected_type, match="storage_prepare/failed"):
        service.handle(payload, peer_uid=policy.allowed_uid, owner=object())


@pytest.mark.parametrize("stage", ["probe", "install", "build", "start"])
def test_units_bind_only_owned_trusted_copies(tmp_path: Path, stage: str) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    owned = Path("/run/preview/owned")
    command = mod.build_systemd_command(policy, "a" * 32, stage, owned, 30)
    assert "BindReadOnlyPaths=/run/preview/owned/trusted/dynamic_runner.py:/preview/trusted/dynamic_runner.py" in command
    assert "BindReadOnlyPaths=/run/preview/owned/trusted/harness/project_validation_sandbox.py:/preview/trusted/harness/project_validation_sandbox.py" in command
    assert not any(str(policy.trusted_source_root) in item for item in command)
    assert "SupplementaryGroups=" in command


@pytest.mark.parametrize("stderr,reason", [
    (b"/usr/bin/python3: can't open file '/preview/trusted/dynamic_runner.py': [Errno 13] Permission denied", "permission_denied"),
    (b"/usr/bin/python3: can't open file '/preview/trusted/dynamic_runner.py': [Errno 2] No such file or directory", "not_found"),
    (b"secret fixture unrecognized stderr", "bootstrap_failed"),
])
def test_probe_bootstrap_diagnostics_are_fixed_and_probe_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: bytes, reason: str,
) -> None:
    from types import SimpleNamespace
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    session = mod._Session("a" * 32, "probe", object(), Path("/run/preview/owned"), 1e20)
    observed: list[object] = []

    def popen(argv: object, **kwargs: object) -> object:
        observed.append(kwargs["stderr"])
        return SimpleNamespace()

    def read(process: object, timeout: float, diagnostic: bytearray | None = None) -> dict[str, object]:
        if diagnostic is not None:
            diagnostic.extend(stderr)
        raise EOFError("fixture")

    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(mod, "_pipe_read", read)
    with pytest.raises(mod.ProbeFailure) as failure:
        service._launch(session, "probe")
    assert (failure.value.phase, failure.value.reason) == ("bootstrap", reason)
    with pytest.raises(EOFError):
        service._launch(session, "start")
    assert observed == [mod.subprocess.PIPE, mod.subprocess.DEVNULL]


def test_probe_stderr_capture_is_bounded_and_separate_from_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from types import SimpleNamespace
    mod = broker()
    output = io.BytesIO()
    mod.write_frame(output, {"ok": True, "state": "probe"})
    output.seek(0)
    stdout = SimpleNamespace(fileno=lambda: 1)
    stderr = SimpleNamespace(fileno=lambda: 2)
    process = SimpleNamespace(stdout=stdout, stderr=stderr)
    diagnostic = bytearray()

    def read(fd: int, count: int) -> bytes:
        return output.read(count) if fd == 1 else b"x" * count

    monkeypatch.setattr(mod.os, "read", read)
    monkeypatch.setattr(mod.select, "select", lambda readers, *args: (readers, [], []))
    assert mod._pipe_read(process, 1, diagnostic) == {"ok": True, "state": "probe"}
    assert diagnostic == b"x" * 4096


def test_stage_work_permissions_are_handed_off_by_root_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    work = tmp_path / "private-work"
    (work / "app").mkdir(parents=True)
    (work / "home").mkdir()
    (work / "app/package.json").write_text("fixture")
    owners: list[Path] = []

    def chown(path: Path, uid: int, gid: int, *, follow_symlinks: bool) -> None:
        assert uid == gid == 0
        assert follow_symlinks is False
        owners.append(path)

    monkeypatch.setattr(os, "chown", chown, raising=False)
    mod.PreviewBroker(policy)._handoff_work(work)
    assert work / "app/package.json" in owners
    assert work / "home" in owners
    assert (work / "app/package.json").read_text() == "fixture"


def test_all_scratch_mounts_have_size_and_inode_limits(tmp_path: Path) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    command = mod.build_systemd_command(policy, "a" * 32, "start", Path("/run/preview/owned"), 30)
    scratch = next(part for part in command if part.startswith("TemporaryFileSystem="))
    for target in ("/tmp:", "/var/tmp:", "/run:"):
        assert target in scratch
    for spec in scratch.split("=", 1)[1].split():
        assert "size=" in spec and "nr_inodes=" in spec


def test_repeated_stop_tombstone_is_connection_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    owner = object()
    session = service._reserve("fixture", owner, 30)
    monkeypatch.setattr(mod, "_PLATFORM", "linux")
    request = {"version": 1, "action": "stop", "handle": session.handle}
    assert service.handle(request, peer_uid=policy.allowed_uid, owner=owner) == {"ok": True, "state": "stopped"}
    assert service.handle(request, peer_uid=policy.allowed_uid, owner=owner) == {"ok": True, "state": "stopped"}
    with pytest.raises(ValueError, match="ownership"):
        service.handle(request, peer_uid=policy.allowed_uid, owner=object())


def test_disconnect_recovery_requires_signed_stop_only_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    owner = object()
    session = service._reserve("fixture", owner, 30)
    token = service._recovery_token(session.handle)
    service.disconnect(owner)
    monkeypatch.setattr(mod, "_PLATFORM", "linux")
    request = {"version": 1, "action": "recover_stop", "handle": session.handle,
               "recovery_token": token}
    assert service.handle(request, peer_uid=policy.allowed_uid, owner=object()) == {"ok": True, "state": "stopped"}
    request["recovery_token"] = "0" * 64
    with pytest.raises(ValueError, match="ownership"):
        service.handle(request, peer_uid=policy.allowed_uid, owner=object())


def test_recovery_key_survives_restart_without_unbounded_tombstones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    policy = replace(policy, runtime_root=tmp_path / "broker-owned")
    metadata = dict.fromkeys(policy.runtime_root.parents, (0, stat.S_IFDIR | 0o755))
    metadata[policy.runtime_root] = (0, stat.S_IFDIR | 0o700)
    metadata[policy.runtime_root / ".recovery-key"] = (0, stat.S_IFREG | 0o600)
    linux_metadata(monkeypatch, metadata)
    first = mod.PreviewBroker(policy)
    first._initialize_runtime_root()
    token = first._recovery_token("a" * 32)
    for index in range(150):
        first._remember_stopped(f"{index:032x}", object())
    assert len(first._completed) <= 128
    second = mod.PreviewBroker(policy)
    second._initialize_runtime_root()
    assert second._recovery_token("a" * 32) == token


def test_evicted_tombstone_recovery_checks_every_fixed_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    handle = "a" * 32
    units: list[str] = []
    disks: list[str] = []
    monkeypatch.setattr(service, "_stop_unit", units.append)
    monkeypatch.setattr(service, "_stop_storage", lambda session: disks.append(session.handle))
    assert service._recover_stop(handle, service._recovery_token(handle), object()) == {"ok": True, "state": "stopped"}
    assert set(units) == {f"agent-hub-preview-{handle}-{stage}.service"
                          for stage in ("install", "build", "start", "probe")}
    assert disks == [handle]


def test_recovery_proof_cannot_select_another_preview(tmp_path: Path) -> None:
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    with pytest.raises(ValueError, match="ownership"):
        service._recover_stop("b" * 32, service._recovery_token("a" * 32), object())


def test_disk_stop_failure_cannot_publish_terminal_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    root = tmp_path / "broker-owned"
    root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=root))
    session = service._reserve("fixture", object(), 30)
    session.mounted = True

    def fail(session: object) -> None:
        raise RuntimeError("mount remains fixture")

    monkeypatch.setattr(service, "_stop_storage", fail)
    with pytest.raises(RuntimeError, match="mount remains"):
        service._stop(session)
    assert session.handle in service._sessions
    assert session.handle not in service._completed
    assert session.owned.exists()


def test_reaper_reclaims_crashed_ready_runner_without_waiting_for_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace
    mod = broker()
    _, policy = prepared(tmp_path)
    root = tmp_path / "broker-owned"
    root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=root))
    session = service._reserve("fixture", object(), 7200)
    session.ready = True
    session.process = SimpleNamespace(poll=lambda: 1)
    monkeypatch.setattr(service, "_close_process", lambda process: None)
    service.reap()
    assert session.handle not in service._sessions
    assert session.handle in service._completed
    assert not session.owned.exists()


@pytest.mark.parametrize("stage", ["install", "build", "start"])
@pytest.mark.parametrize("reason", [
    "nonzero_exit", "permission_denied", "storage_full", "read_only", "resource_limit",
    "registry_unavailable", "dependency_unavailable", "dependency_conflict", "package_invalid",
    "certificate_error", "dependency_rejected", "supervisor_exit",
    "storage_conflict", "runtime_incompatible", "integrity_error", "registry_denied",
    "npm_exit_incomplete", "npm_internal_error",
    "signal_abort", "signal_kill", "signal_segv", "signal_term", "file_size_limit", "signal_exit",
])
def test_launch_preserves_whitelisted_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, reason: str,
) -> None:
    from types import SimpleNamespace
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    session = mod._Session("a" * 32, "fixture", object(), Path("/run/preview/owned"), 1e20)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(mod, "_pipe_read", lambda *args: {
        "ok": False, "error": "preview startup failed", "phase": stage, "reason": reason,
    })
    with pytest.raises(RuntimeError) as failure:
        service._launch(session, stage)
    assert getattr(failure.value, "phase", None) == stage
    assert getattr(failure.value, "reason", None) == reason
    assert session.units == {f"agent-hub-preview-{'a' * 32}-{stage}.service"}


@pytest.mark.parametrize("stage", ["install", "build", "probe"])
@pytest.mark.parametrize("exit_code", [0, 23])
def test_launch_distinguishes_supervisor_exit_after_success_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, exit_code: int,
) -> None:
    from types import SimpleNamespace
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    session = mod._Session("a" * 32, "fixture", object(), Path("/run/preview/owned"), 1e20)
    process = SimpleNamespace(wait=lambda timeout: exit_code)
    result = {"ok": True, "state": "probe" if stage == "probe" else "prepared"}
    stopped: list[str] = []
    closed: list[object] = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(mod, "_pipe_read", lambda *args: result)
    monkeypatch.setattr(service, "_stop_unit", stopped.append)
    monkeypatch.setattr(service, "_close_process", closed.append)
    unit = f"agent-hub-preview-{'a' * 32}-{stage}.service"
    if exit_code:
        failure_type = mod.ProbeFailure if stage == "probe" else mod.PreviewStartupFailure
        with pytest.raises(failure_type) as failure:
            service._launch(session, stage)
        assert failure.value.phase == ("runner_exit" if stage == "probe" else stage)
        assert failure.value.reason == ("nonzero_exit" if stage == "probe" else "supervisor_exit")
        assert session.units == {unit}
        assert session.process is process
        assert stopped == closed == []
    else:
        assert service._launch(session, stage) == result
        assert stopped == [unit]
        assert closed == [process]
        assert not session.units
        assert session.process is None


@pytest.mark.parametrize("payload", [
    {"ok": False, "error": "preview startup failed", "phase": "install", "reason": "sentinel"},
    {"ok": False, "error": "preview startup failed", "phase": [], "reason": "failed"},
    {"ok": False, "error": "sentinel", "phase": "install", "reason": "failed"},
    {"ok": False, "error": "preview startup failed", "phase": "install", "reason": "failed", "stderr": "sentinel"},
    {"ok": 0, "error": "preview startup failed", "phase": "install", "reason": "failed"},
])
def test_launch_rejects_untrusted_startup_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object],
) -> None:
    from types import SimpleNamespace
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    session = mod._Session("a" * 32, "fixture", object(), Path("/run/preview/owned"), 1e20)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(mod, "_pipe_read", lambda *args: payload)
    with pytest.raises(RuntimeError) as failure:
        service._launch(session, "install")
    assert getattr(failure.value, "phase", None) == "install"
    assert getattr(failure.value, "reason", None) == "invalid_result"
    assert "sentinel" not in str(failure.value)


@pytest.mark.parametrize("boundary,reason", [
    ("install_validate", "unsafe_tree"), ("install_handoff", "permission_denied"),
    ("build_validate", "unsafe_tree"), ("build_handoff", "permission_denied"),
])
def test_startup_transition_failure_preserves_phase_and_cleans_owned_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, reason: str,
) -> None:
    from dataclasses import replace
    mod = broker()
    source, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    stages: list[str] = []
    monkeypatch.setattr(service, "_prepare_trusted", lambda session: None)
    monkeypatch.setattr(service, "_prepare_storage", lambda session: None)
    if os.name == "nt":
        # Windows cannot unlink the read-only snapshots; Linux root can.
        remove_tree = mod.shutil.rmtree

        def remove_readonly_tree(path: Path) -> None:
            for child in path.rglob("*"):
                child.chmod(0o777)
            remove_tree(path)

        monkeypatch.setattr(mod.shutil, "rmtree", remove_readonly_tree)

    def chown(*args: object, **kwargs: object) -> None:
        if boundary == stages[-1] + "_handoff":
            raise PermissionError("/private/cache token=sentinel")

    monkeypatch.setattr(os, "chown", chown, raising=False)

    def launch(session: Any, stage: str) -> dict[str, object]:
        stages.append(stage)
        app = session.owned / "work/app"
        app.mkdir(exist_ok=True)
        (app / "package.json").write_text("fixture")
        if boundary == stage + "_validate":
            os.link(app / "package.json", app / "linked.json")
        return {"ok": True, "state": "prepared"}

    monkeypatch.setattr(service, "_launch", launch)
    with pytest.raises((RuntimeError, ValueError, OSError)) as failure:
        service._start({"preview_id": "fixture", "source_root": str(source),
                        "lifetime_seconds": 30}, object())
    assert getattr(failure.value, "phase", None) == boundary
    assert getattr(failure.value, "reason", None) == reason
    assert stages == (["install"] if boundary.startswith("install") else ["install", "build"])
    assert not service._sessions
    assert not list(runtime_root.iterdir())
    assert "sentinel" not in str(failure.value)


@pytest.mark.parametrize("kind", ["startup", "probe", "unclassified"])
def test_broker_serves_only_safe_diagnostics_and_fixed_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    import io
    import struct
    import threading
    mod = broker()
    _, policy = prepared(tmp_path)
    service = mod.PreviewBroker(policy)
    stopping = threading.Event()
    incoming, outgoing = io.BytesIO(), io.BytesIO()
    mod.write_frame(incoming, {"version": 1, "action": "start"})
    incoming.seek(0)

    class Stream:
        def read(self, size: int = -1) -> bytes:
            return incoming.read(size)

        def write(self, data: bytes) -> int:
            return outgoing.write(data)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    class Connection:
        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, policy.allowed_uid, 123)

        def settimeout(self, timeout: float) -> None:
            pass

        def makefile(self, mode: str, *, buffering: int) -> Stream:
            return Stream()

        def close(self) -> None:
            stopping.set()

        def shutdown(self, how: int) -> None:
            pass

    class Listener:
        accepted = False

        def settimeout(self, timeout: float) -> None:
            pass

        def accept(self) -> tuple[Connection, None]:
            if not self.accepted:
                self.accepted = True
                return Connection(), None
            assert stopping.wait(2), "fixture worker did not finish"
            raise TimeoutError

    def fail(*args: object, **kwargs: object) -> None:
        try:
            raise PermissionError("source /private/path stderr token=sentinel")
        except PermissionError:
            if kind == "startup":
                raise mod.PreviewStartupFailure("install_handoff", "permission_denied") from None
            if kind == "probe":
                raise mod.ProbeFailure("storage_prepare", "permission_denied") from None
            raise

    monkeypatch.setattr(service, "handle", fail)
    mod.serve_broker(Listener(), service, stopping)
    outgoing.seek(0)
    expected: dict[str, object] = {"ok": False, "error": "preview broker operation failed"}
    journal = ""
    if kind != "unclassified":
        phase = "install_handoff" if kind == "startup" else "storage_prepare"
        expected = {"ok": False, "error": f"preview {kind} failed",
                    "phase": phase, "reason": "permission_denied"}
        journal = f"preview {kind} failed: {phase}/permission_denied\n"
    assert mod.read_frame(outgoing) == expected
    assert capsys.readouterr().err == journal
    assert b"sentinel" not in outgoing.getvalue()
