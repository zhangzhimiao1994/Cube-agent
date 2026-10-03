"""Offline security/command fixtures, never real Linux or 20-project acceptance."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest


def broker() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.dynamic_broker"), "Task1 broker missing"
    return importlib.import_module("agent_hub.previews.dynamic_broker")


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
    for required in ["KillMode=control-group", "TasksMax=", "MemoryMax=", "CPUQuota=",
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

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        observed.append(argv)
        if "show" in argv:
            return subprocess.CompletedProcess(argv, 0,
                b"LoadState=loaded\nActiveState=deactivating\nMainPID=0\nControlGroup=/fixture\n")
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(service, "_command", command)
    with pytest.raises(RuntimeError, match="cgroup"):
        service._stop_unit("agent-hub-preview-fixture-start.service")
    assert observed[0][1] == "stop"
    assert "show" in observed[1]


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


def test_broker_root_disk_is_private_before_reserving(tmp_path: Path) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    runtime_root = tmp_path / "broker-owned"
    runtime_root.mkdir()
    service = mod.PreviewBroker(replace(policy, runtime_root=runtime_root))
    service._initialize_runtime_root()
    if os.name != "nt":
        assert runtime_root.stat().st_mode & 0o777 == 0o700
    assert service.policy.runtime_root == runtime_root


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
    expected = "storage_prepare/failed" if action == "probe" else "fixture stops before OS execution"
    with pytest.raises(RuntimeError, match=expected):
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


def test_recovery_key_survives_restart_without_unbounded_tombstones(tmp_path: Path) -> None:
    from dataclasses import replace
    mod = broker()
    _, policy = prepared(tmp_path)
    policy = replace(policy, runtime_root=tmp_path / "broker-owned")
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
