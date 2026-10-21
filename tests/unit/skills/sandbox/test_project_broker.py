from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy, BrokerRequest, SkillBroker


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    name = "agent_hub.skills.sandbox.project"
    assert importlib.util.find_spec(name) is not None, "project execution layer is missing"
    module = importlib.import_module(name)
    # These protocol/lifecycle tests use user-owned temp dirs. Ownership and
    # rename denial are checked separately with real Linux root/service UIDs.
    monkeypatch.setattr(module, '_private_project_runtime', module._directory, raising=False)
    return module


def test_project_execution_layer_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.project") is not None


def test_default_project_runtime_has_root_private_native_provisioning() -> None:
    policy = BrokerPolicy()
    assert policy.project_runtime_root == Path('/run/agent-hub-project')
    assert policy.project_runtime_root != policy.runtime_root
    root = Path(__file__).resolve().parents[4]
    declarations = (root / 'deploy/native/agent-hub.tmpfiles').read_text().splitlines()
    assert 'd /run/agent-hub-project 0700 root root -' in declarations
    unit = (root / 'deploy/native/systemd/agent-hub-skill-broker.service').read_text()
    assert ('ReadWritePaths=/run/agent-hub /run/agent-hub-project '
            '/var/lib/agent-hub-toolchain-stages\n') in unit


def invocation(project: ModuleType, **changes: Any) -> Any:
    values = {
        "execution_id": "project_1",
        "tenant_id": UUID(int=1),
        "run_id": UUID(int=2),
        "project_id": "demo",
        "session_id": "session-a",
        "sandbox_profile": "restricted",
        "timeout_seconds": 3,
    }
    values.update(changes)
    return project.ProjectInvocation(**values)


def source_tree(root: Path, inv: Any) -> Path:
    source = (
        root
        / str(inv.tenant_id)
        / "projects"
        / str(inv.project_id)
        / "sessions"
        / str(inv.session_id)
    )
    source.mkdir(parents=True)
    (source / "package.json").write_text('{"scripts":{"test":"node test.js"}}')
    (source / "test.js").write_text("console.log('ok')")
    return source


def test_invocation_is_strict_frozen_without_skill_payload(project: ModuleType) -> None:
    inv = invocation(project)
    assert inv.input == {}
    inv.input["ignored"] = True
    assert inv.input == {}
    assert "input" not in inv.model_dump()
    assert inv.output_limit_bytes == 262144
    assert inv.memory_limit_bytes == 1024**3
    assert inv.cpu_quota_percent == 100
    assert inv.workspace_limit_bytes == 512 * 1024**2
    assert inv.source_limit_bytes == 100 * 1024**2
    assert inv.file_limit == 10000
    with pytest.raises(ValidationError):
        inv.install_allowed = True
    assert project.ProjectBrokerRequest(invocation=inv, action="run_project").version == 2
    assert BrokerRequest.terminate("project_1").model_dump() == {
        "version": 1,
        "backend": "systemd",
        "action": "terminate",
        "invocation": None,
        "execution_id": "project_1",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "../escape"},
        {"execution_id": "x" * 129},
        {"project_id": "Demo"},
        {"session_id": "x" * 65},
        {"project_id": ".."},
        {"tenant_id": "00000000-0000-0000-0000-000000000001"},
        {"run_id": "bad"},
        {"sandbox_profile": "read_only"},
        {"install_allowed": 1},
        {"timeout_seconds": 0},
        {"timeout_seconds": 3601},
        {"timeout_seconds": True},
        {"output_limit_bytes": 1024**2 + 1},
        {"memory_limit_bytes": 4 * 1024**3 + 1},
        {"cpu_quota_percent": 401},
        {"workspace_limit_bytes": 2 * 1024**3 + 1},
        {"source_limit_bytes": 500 * 1024**2 + 1},
        {"file_limit": 100001},
        {"file_limit": 0},
        {"argv": ["id"]},
        {"input": {}},
        {"backend": "docker"},
    ],
)
def test_invocation_rejects_unsafe_identity_and_limits(project: ModuleType, changes: Any) -> None:
    with pytest.raises(ValidationError):
        invocation(project, **changes)


async def test_snapshot_and_fingerprint_share_delivery_filter(
    project: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inv = invocation(project)
    root = tmp_path / "workspaces"
    source = source_tree(root, inv)
    for name in ("node_modules", ".venv", ".ssh", ".aws", ".credentials", ".git", "venv"):
        (source / name).mkdir()
        (source / name / "secret").write_text("never read")
    for name in (
        ".env",
        ".env.local",
        ".npmrc",
        "credentials.json",
        "id_rsa",
    ):
        (source / name).write_text("secret")
    (source / "DELIVERY_MANIFEST.json").write_text("delivery metadata")
    (source / ".gitignore").write_text("node_modules")
    destination = tmp_path / "snapshot"
    original_mode = stat.S_IMODE(source.stat().st_mode)
    calls = []
    read = project.scoped_read._read_no_links

    def observed(root: Path, parts: tuple[str, ...], **kwargs: Any) -> bytes:
        calls.append(parts)
        return cast(bytes, read(root, parts, **kwargs))

    monkeypatch.setattr(project.scoped_read, "_read_no_links", observed)
    digest = await project.snapshot_project_source(root, inv, destination=destination)
    expected = {
        name: hashlib.sha256(
            (b"x\0" if (source / name).lstat().st_mode & 0o111 else b"f\0")
            + (source / name).read_bytes()
        ).hexdigest()
        for name in ("package.json", "test.js", ".gitignore", "DELIVERY_MANIFEST.json")
    }
    assert (
        digest
        == hashlib.sha256(
            json.dumps(
                expected,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()
    )
    assert await project.project_source_fingerprint(root, inv) == digest
    assert {p.name for p in destination.iterdir()} == set(expected)
    assert {parts[-1] for parts in calls} == set(expected)
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o555
        assert stat.S_IMODE((destination / "test.js").stat().st_mode) == 0o444


@pytest.mark.parametrize("source_mode,snapshot_mode", [(0o6755, 0o555), (0o6644, 0o444)])
async def test_snapshot_preserves_safe_read_only_modes_with_mode_bound_digest(
    project: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_mode: int,
    snapshot_mode: int,
) -> None:
    inv = invocation(project)
    root = tmp_path / "workspaces"
    source = source_tree(root, inv)
    script = source / "helper.sh"
    script.write_bytes(b"#!/bin/sh\nprintf 'helper-ok\\n'\n")
    script.chmod(source_mode)
    destination = tmp_path / "snapshot"
    target = destination / script.name
    requested_modes: list[int] = []
    chmod = Path.chmod

    def observe_chmod(path: Path, mode: int) -> None:
        if path == target:
            requested_modes.append(mode)
        chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", observe_chmod)
    if os.name == "nt":
        # Windows cannot store POSIX execute/special bits; supply that source metadata only.
        lstat = Path.lstat

        def source_lstat(path: Path) -> Any:
            info = lstat(path)
            if path == script:
                return SimpleNamespace(
                    st_mode=stat.S_IFREG | source_mode,
                    st_nlink=info.st_nlink,
                    st_size=info.st_size,
                    st_file_attributes=getattr(info, 'st_file_attributes', 0),
                )
            return info

        monkeypatch.setattr(Path, "lstat", source_lstat)
    expected_files = {
        name: hashlib.sha256(
            (b"x\0" if (source / name).lstat().st_mode & 0o111 else b"f\0")
            + (source / name).read_bytes()
        ).hexdigest()
        for name in ("package.json", "test.js", "helper.sh")
    }
    expected = hashlib.sha256(
        json.dumps(expected_files, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()

    digest = await project.snapshot_project_source(root, inv, destination=destination)

    assert requested_modes == [snapshot_mode]
    assert target.read_bytes() == script.read_bytes()
    assert digest == expected == await project.project_source_fingerprint(root, inv)
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == snapshot_mode
        assert stat.S_IMODE(script.stat().st_mode) == source_mode


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod cannot change POSIX execute bits")
@pytest.mark.parametrize("execute_bit", [0o100, 0o010, 0o001])
async def test_execute_mode_changes_source_fingerprint(
    project: ModuleType,
    tmp_path: Path,
    execute_bit: int,
) -> None:
    inv = invocation(project)
    source = source_tree(tmp_path, inv)
    script = source / "test.js"
    content = script.read_bytes()
    script.chmod(0o644)
    before = await project.project_source_fingerprint(tmp_path, inv)

    script.chmod(0o644 | execute_bit)
    assert script.stat().st_mode & 0o111 == execute_bit
    assert script.read_bytes() == content
    assert await project.project_source_fingerprint(tmp_path, inv) != before

    script.chmod(0o644)
    assert await project.project_source_fingerprint(tmp_path, inv) == before


@pytest.mark.parametrize("limit", [{"source_limit_bytes": 1}, {"file_limit": 1}])
async def test_source_limits_fail_closed(project: ModuleType, tmp_path: Path, limit: Any) -> None:
    inv = invocation(project, **limit)
    source_tree(tmp_path, inv)
    with pytest.raises(RuntimeError):
        await project.project_source_fingerprint(tmp_path, inv)


async def test_missing_scope_and_hardlinks_fail_closed(project: ModuleType, tmp_path: Path) -> None:
    inv = invocation(project)
    with pytest.raises(RuntimeError):
        await project.project_source_fingerprint(tmp_path, inv)
    source = source_tree(tmp_path, inv)
    os.link(source / "test.js", source / "linked.js")
    with pytest.raises(RuntimeError):
        await project.project_source_fingerprint(tmp_path, inv)


def test_command_is_fixed_and_has_private_writable_tmpfs(
    project: ModuleType, tmp_path: Path
) -> None:
    inv = invocation(project, install_allowed=True)
    command = project.build_project_systemd_command(inv, BrokerPolicy(), tmp_path)
    properties = [command[i + 1] for i, v in enumerate(command) if v == "-p"]
    assert command[:5] == ("/usr/bin/systemd-run", "--wait", "--collect", "--pipe", "--quiet")
    assert command[command.index("--unit") + 1] == "agent-hub-project-project_1"
    for value in (
        "DynamicUser=yes",
        "SupplementaryGroups=agent-hub",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "CapabilityBoundingSet=",
        "TasksMax=128",
        "MemoryMax=1073741824",
        "CPUQuota=100%",
        "RuntimeMaxSec=3s",
    ):
        assert value in properties
    assert (
        "TemporaryFileSystem=/run/agent-hub-project-work:rw,nosuid,nodev,size=536870912,nr_inodes=100000,mode=1777"
        in properties
    )
    assert not any("PrivateNetwork" in p or "IPAddressDeny" in p for p in properties)
    assert not any(p.startswith("ProtectKernelTunables=") for p in properties)
    assert [p for p in properties if p.startswith("CapabilityBoundingSet=")] == [
        "CapabilityBoundingSet="
    ]
    assert command[-11:] == (
        "/usr/bin/python3",
        "-I",
        "-B",
        "/opt/agent-hub/current/src/agent_hub/skills/sandbox/project_runner.py",
        "--input",
        "/run/agent-hub-project-input",
        "--work",
        "/run/agent-hub-project-work/project",
        "--timeout",
        "3",
        "--install-allowed",
    )


async def test_project_peer_auth_and_no_host_fallback(project: ModuleType) -> None:
    broker = SkillBroker(BrokerPolicy())
    request = project.ProjectBrokerRequest(action="run_project", invocation=invocation(project))
    response = await broker.handle_project(request, peer_uid=0)
    assert not response.ok and "uid" in (response.error or "")
    if sys.platform != "linux":
        response = await broker.handle_project(request, peer_uid=10001)
        assert not response.ok and "unsupported" in (response.error or "")


@pytest.fixture
async def systemd(
    project: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Any]:
    commands: list[tuple[str, ...]] = []
    processes = []
    real_create = asyncio.create_subprocess_exec
    state = {"quiet": True, "delay": False, "report": "passed"}
    started = asyncio.Event()

    async def create(*command: str, **kwargs: Any) -> asyncio.subprocess.Process:
        commands.append(command)
        if command[0] == "/usr/bin/systemd-run":
            stage = next((tmp_path / "runtime").glob("project_1-*/snapshot"))
            hashes = {
                p.relative_to(stage).as_posix(): hashlib.sha256(
                    (b"x\0" if p.stat().st_mode & 0o111 else b"f\0") + p.read_bytes()
                ).hexdigest()
                for p in stage.rglob("*")
                if p.is_file()
            }
            digest = hashlib.sha256(
                json.dumps(
                    hashes,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest()
            report = {
                "schema_version": 1,
                "status": state["report"],
                "source_sha256": digest,
                "checks": [],
                "runtime": {"node": "v22", "npm": "10"},
            }
            code = (
                "import time; time.sleep(60)"
                if state["delay"]
                else "print(" + repr(json.dumps(report)) + ")"
            )
            started.set()
        elif command[1] == "show":
            code = (
                "print("
                + repr(
                    "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
                    "MainPID=0\nControlPID=0\nControlGroup=\nTasksCurrent=0\n"
                    if state["quiet"]
                    else "LoadState=loaded\nActiveState=active\nMainPID=33\n"
                )
                + ")"
            )
        else:
            code = "import time; time.sleep(60)" if state.get("helper_delay") else "pass"
        process = await real_create(sys.executable, "-B", "-c", code, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(project, "_systemd_supported", lambda: True)
    monkeypatch.setattr(project.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(project, "_CGROUP_ROOT", tmp_path / "cgroup")
    try:
        yield commands, state, started
        assert all(p.returncode is not None for p in processes), "owned helper was not reaped"
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 2)


def configured(project: ModuleType, tmp_path: Path) -> tuple[Any, Any]:
    inv = invocation(project)
    root = tmp_path / "workspaces"
    source_tree(root, inv)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    broker = SkillBroker(BrokerPolicy(project_workspace_root=root,
        runtime_root=tmp_path / 'skill-runtime', project_runtime_root=runtime))
    return inv, broker


@pytest.mark.parametrize("status", ["passed", "failed", "blocked"])
async def test_real_process_report_and_owned_stage_cleanup(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
    status: str,
) -> None:
    inv, broker = configured(project, tmp_path)
    systemd[1]["report"] = status
    response = await broker.handle_project(
        project.ProjectBrokerRequest(action="run_project", invocation=inv),
        peer_uid=10001,
    )
    assert response.ok, response.error
    assert response.result.exit_code == 0
    assert json.loads(response.result.stdout)["status"] == status
    assert list((tmp_path / "runtime").iterdir()) == []
    assert any(c[1] == "kill" for c in systemd[0])
    assert any(c[1] == "show" for c in systemd[0])
    again = await broker.handle_project(
        project.ProjectBrokerRequest(action="run_project", invocation=inv),
        peer_uid=10001,
    )
    assert not again.ok and "duplicate" in again.error


async def test_unknown_cleanup_retains_metadata_and_never_succeeds(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inv, broker = configured(project, tmp_path)
    systemd[1]["quiet"] = False
    monkeypatch.setattr(project, "_CLEANUP_SECONDS", 0.4)
    response = await broker.handle_project(
        project.ProjectBrokerRequest(action="run_project", invocation=inv),
        peer_uid=10001,
    )
    assert not response.ok and "cleanup" in response.error
    stage = next((tmp_path / "runtime").iterdir())
    assert (stage / "snapshot" / "package.json").is_file()
    metadata = json.loads((stage / "execution.json").read_text())
    assert metadata["state"] == "UNKNOWN"
    assert metadata["execution_id"] == inv.execution_id
    assert metadata["unit"] == "agent-hub-project-project_1.service"
    assert len(metadata["source_sha256"]) == 64


async def test_queue_deadline_and_cancellation_cannot_launch_later(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
) -> None:
    inv, broker = configured(project, tmp_path)
    request = project.ProjectBrokerRequest(
        action="run_project",
        invocation=inv.model_copy(update={"timeout_seconds": 1}),
    )
    for _ in range(8):
        await broker._execution_slots.acquire()
    try:
        response = await broker.handle_project(request, peer_uid=10001)
        assert not response.ok and "deadline" in response.error
        assert systemd[0] == []
    finally:
        for _ in range(8):
            broker._execution_slots.release()
    again = await broker.handle_project(request, peer_uid=10001)
    assert not again.ok and "duplicate" in again.error


async def test_cancel_run_waits_for_unit_quiet_before_removing_snapshot(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
) -> None:
    inv, broker = configured(project, tmp_path)
    systemd[1]["delay"] = True
    task = asyncio.create_task(
        broker.handle_project(
            project.ProjectBrokerRequest(action="run_project", invocation=inv),
            peer_uid=10001,
        )
    )
    await asyncio.wait_for(systemd[2].wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "runtime").iterdir()) == []
    assert any(c[1] == "kill" for c in systemd[0])
    assert any(c[1] == "show" for c in systemd[0])


@pytest.mark.parametrize("state", ["populated 1\nfrozen 0\n", "populated 0\nfrozen 0\n"])
async def test_cgroup_population_is_authoritative(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    inv, broker = configured(project, tmp_path)
    group = tmp_path / "cgroup/system.slice/agent-hub-project-project_1.service"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text(state)
    monkeypatch.setattr(project, "_CLEANUP_SECONDS", 0.5)
    response = await broker.handle_project(
        project.ProjectBrokerRequest(invocation=inv), peer_uid=10001
    )
    assert response.ok == state.startswith("populated 0"), response.error
    assert bool(list((tmp_path / "runtime").iterdir())) == (not response.ok)


@pytest.mark.parametrize("quiet", [True, False])
async def test_hung_kill_helper_is_bounded_and_reaped_before_quiet_success(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
    monkeypatch: pytest.MonkeyPatch,
    quiet: bool,
) -> None:
    inv, broker = configured(project, tmp_path)
    systemd[1]["helper_delay"] = True
    systemd[1]["quiet"] = quiet
    monkeypatch.setattr(project, "_CLEANUP_SECONDS", 1.5)
    response = await asyncio.wait_for(
        broker.handle_project(
            project.ProjectBrokerRequest(invocation=inv),
            peer_uid=10001,
        ),
        3,
    )
    assert response.ok is quiet, response.error
    assert any(command[1] == "kill" for command in systemd[0])
    assert any(command[1] == "show" for command in systemd[0])
    stages = list((tmp_path / "runtime").iterdir())
    if quiet:
        assert stages == []
    else:
        assert response.error == "project_termination_cleanup_incomplete"
        assert len(stages) == 1
        assert json.loads((stages[0] / "execution.json").read_text())["state"] == "UNKNOWN"


async def test_explicit_project_terminate_waits_for_running_job(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
) -> None:
    inv, broker = configured(project, tmp_path)
    systemd[1]["delay"] = True
    task = asyncio.create_task(
        broker.handle_project(
            project.ProjectBrokerRequest(invocation=inv),
            peer_uid=10001,
        )
    )
    await systemd[2].wait()
    # Wait for the launcher to be registered, exercising cancellation after spawn.
    for _ in range(100):
        if broker._project_broker.executions[inv.execution_id].process is not None:
            break
        await asyncio.sleep(0.01)
    response = await broker.handle_project(
        project.ProjectTerminateRequest(execution_id=inv.execution_id),
        peer_uid=10001,
    )
    assert response.ok, response.error
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "runtime").iterdir()) == []
    assert broker._project_broker.executions[inv.execution_id].quiet


async def test_execution_slot_is_held_until_owned_unit_is_quiet(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inv, broker = configured(project, tmp_path)
    cleanup_started, release = asyncio.Event(), asyncio.Event()
    original = broker._project_broker._kill_and_confirm

    async def delayed(job: Any) -> None:
        cleanup_started.set()
        await release.wait()
        await original(job)

    monkeypatch.setattr(broker._project_broker, "_kill_and_confirm", delayed)
    task = asyncio.create_task(
        broker.handle_project(
            project.ProjectBrokerRequest(invocation=inv),
            peer_uid=10001,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), 2)
    try:
        assert broker._execution_slots._value == 7
    finally:
        release.set()
        await task


async def test_terminate_unknown_retained_stage_cannot_claim_success(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
) -> None:
    inv, broker = configured(project, tmp_path)
    stage = tmp_path / "runtime" / "project_1-retained"
    stage.mkdir()
    (stage / "execution.json").write_text(
        json.dumps({"execution_id": inv.execution_id, "state": "UNKNOWN"})
    )
    response = await broker.handle_project(
        project.ProjectTerminateRequest(execution_id=inv.execution_id),
        peer_uid=10001,
    )
    assert not response.ok and "cleanup" in response.error
    assert stage.exists()


async def test_termination_before_run_reserves_id_without_killing_any_unit(
    project: ModuleType,
    tmp_path: Path,
    systemd: Any,
) -> None:
    inv, broker = configured(project, tmp_path)
    response = await broker.handle_project(
        project.ProjectTerminateRequest(execution_id=inv.execution_id),
        peer_uid=10001,
    )
    assert response.ok
    response = await broker.handle_project(
        project.ProjectBrokerRequest(invocation=inv), peer_uid=10001
    )
    assert not response.ok and "duplicate" in response.error
    assert systemd[0] == []


class FrameWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


async def test_client_uses_v2_framed_transport(
    project: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.broker import BrokerResponse

    inv = invocation(project)
    writer = FrameWriter()
    result = SkillResult(exit_code=0, stdout='{"status":"failed"}', stderr="", timed_out=False)
    body = BrokerResponse(ok=True, result=result).model_dump_json().encode()
    reader = asyncio.StreamReader()
    reader.feed_data(len(body).to_bytes(4) + body)
    reader.feed_eof()

    async def connect(path: str) -> Any:
        assert path == "/fake/broker.sock"
        return reader, writer

    monkeypatch.setattr(project, "_systemd_supported", lambda: True)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    sandbox = project.ProjectSystemdSandbox(socket_path=Path("/fake/broker.sock"))
    assert await sandbox.run(inv) == result
    assert len(writer.data) - 4 == int.from_bytes(writer.data[:4])
    sent = json.loads(writer.data[4:])
    assert sent["version"] == 2 and sent["action"] == "run_project"
    assert "input" not in sent["invocation"] and "package_path" not in sent["invocation"]
    assert writer.closed


async def test_hidden_directory_named_like_metadata_is_never_traversed(
    project: ModuleType,
    tmp_path: Path,
) -> None:
    inv = invocation(project)
    source = source_tree(tmp_path, inv)
    before = await project.project_source_fingerprint(tmp_path, inv)
    (source / ".gitignore").mkdir()
    (source / ".gitignore/secret").write_text("secret")
    assert await project.project_source_fingerprint(tmp_path, inv) == before


@pytest.mark.parametrize("member", ["DELIVERY_MANIFEST.json", "src/DELIVERY_MANIFEST.json"])
async def test_root_and_nested_delivery_manifests_are_fingerprinted(
    project: ModuleType,
    tmp_path: Path,
    member: str,
) -> None:
    inv = invocation(project)
    source = source_tree(tmp_path, inv)
    (source / "src").mkdir()
    without_manifest = await project.project_source_fingerprint(tmp_path, inv)
    nested = source / member
    nested.write_text("one")
    before = await project.project_source_fingerprint(tmp_path, inv)
    assert before != without_manifest
    nested.write_text("two")
    assert await project.project_source_fingerprint(tmp_path, inv) != before


@pytest.mark.parametrize("scope_alias", [True, False])
async def test_directory_symlinks_or_windows_junctions_fail_closed(
    project: ModuleType,
    tmp_path: Path,
    scope_alias: bool,
) -> None:
    inv = invocation(project)
    root = tmp_path / "workspaces"
    source = source_tree(root, inv)
    outside = tmp_path / "outside"
    if scope_alias:
        source.rename(outside)
        linked = source
    else:
        outside.mkdir()
        (outside / "credential.txt").write_text("secret")
        linked = source / "linked"
    if os.name == "nt":
        importlib.import_module("_winapi").CreateJunction(str(outside), str(linked))
    else:
        linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError):
        await project.project_source_fingerprint(root, inv)


async def test_client_cancellation_waits_for_explicit_project_termination(
    project: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox.broker import BrokerResponse

    started, killed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []

    async def request(req: Any, **kwargs: Any) -> Any:
        requests.append(req)
        if req.action == "run_project":
            started.set()
            await asyncio.Future()
        killed.set()
        await release.wait()
        return BrokerResponse(ok=True)

    monkeypatch.setattr(project, "_systemd_supported", lambda: True)
    monkeypatch.setattr(project, "request_project_broker", request)
    task = asyncio.create_task(project.ProjectSystemdSandbox().run(invocation(project)))
    await started.wait()
    task.cancel()
    await asyncio.wait_for(killed.wait(), 1)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r.action for r in requests] == ["run_project", "terminate_project"]


async def test_serve_client_dispatches_only_explicit_v2_and_closes_cancelled_run(
    project: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox import broker as module

    async def handle(req: Any, *, peer_uid: int) -> Any:
        assert isinstance(req, project.ProjectBrokerRequest)
        assert peer_uid == 10001
        raise asyncio.CancelledError

    broker = SkillBroker(BrokerPolicy())
    monkeypatch.setattr(broker, "handle_project", handle)
    monkeypatch.setattr(module, "_peer_uid", lambda writer: 10001)
    reader, writer = asyncio.StreamReader(), FrameWriter()
    body = project.ProjectBrokerRequest(invocation=invocation(project)).model_dump_json().encode()
    reader.feed_data(len(body).to_bytes(4) + body)
    reader.feed_eof()
    await module._serve_client(reader, cast(asyncio.StreamWriter, writer), broker)
    response = json.loads(writer.data[4:])
    assert not response["ok"]
    assert writer.closed


async def test_v3_socket_uses_separate_private_project_runtime_for_original_staging(
    project: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.skills.sandbox import broker as module
    from agent_hub.skills.sandbox import project_contract_broker

    runtime = tmp_path / 'service-runtime'
    runtime.mkdir(mode=0o700)
    policy = BrokerPolicy(runtime_root=tmp_path / 'service-runtime', project_runtime_root=runtime,
        project_workspace_root=tmp_path / 'workspaces')
    broker = SkillBroker(policy)
    inv = project.ContractProjectInvocation.model_validate({
        **invocation(project).model_dump(), 'source_kind': 'original',
        'original_reference': {'schema_version': 1, 'contract_sha256': 'a' * 64,
            'input_source_sha256': 'b' * 64, 'file_count': 2},
    }, strict=True)
    calls: list[Path] = []

    def stage(*args: object, **kwargs: Any) -> Any:
        assert kwargs['stage'].parent == policy.project_runtime_root
        assert kwargs['allowed_uid'] == policy.allowed_uid
        calls.append(kwargs['stage'].parent)
        raise RuntimeError('trusted_runtime_forwarded')

    monkeypatch.setattr(project_contract_broker, 'stage_original_contract', stage)
    monkeypatch.setattr(project, '_systemd_supported', lambda: True)
    monkeypatch.setattr(module, '_peer_uid', lambda writer: policy.allowed_uid)
    reader, writer = asyncio.StreamReader(), FrameWriter()
    body = project.ContractProjectBrokerRequest(invocation=inv).model_dump_json().encode()
    reader.feed_data(len(body).to_bytes(4) + body)
    reader.feed_eof()
    await module._serve_client(reader, cast(asyncio.StreamWriter, writer), broker)
    assert calls == [runtime]
    assert json.loads(writer.data[4:]) == {'ok': False, 'error': 'trusted_runtime_forwarded', 'result': None}
    assert writer.closed and list(runtime.iterdir()) == []


async def test_project_runtime_move_keeps_legacy_unknown_stages_nonreplayable(
    project: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project, '_systemd_supported', lambda: True)
    runtime, legacy = tmp_path / 'private', tmp_path / 'legacy'
    runtime.mkdir(mode=0o700)
    retained = legacy / 'project_1-retained'
    retained.mkdir(parents=True)
    marker = retained / 'execution.json'
    marker.write_bytes(b'{"state":"UNKNOWN"}')
    before = marker.read_bytes()
    broker = SkillBroker(BrokerPolicy(runtime_root=legacy, project_runtime_root=runtime))
    result = await broker.handle_project(project.ProjectBrokerRequest(invocation=invocation(project)), peer_uid=10001)
    assert result.ok is False and result.error == 'duplicate project execution_id: retained stage'
    assert marker.read_bytes() == before and list(runtime.iterdir()) == []
